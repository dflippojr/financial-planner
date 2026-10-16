"""The daily alert pass refreshes recurring series and raises their review alerts (#426)."""

import logging
from datetime import date
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from finance.alert_services import DailyAlertPassIncomplete, run_daily_alert_pass, settings_for
from finance.models import Account, Alert, Person, RecurringSeries
from finance.recurring_review import missed_charge
from finance.recurring_services import confirm_recurring_series, refresh_recurring_series
from finance.weekly_digest import build_weekly_digest
from tests.test_recurring import (
    add_monthly_charges,
    make_account,
    make_household,
    make_person,
    make_transaction,
)

pytestmark = pytest.mark.django_db


def confirmed_monthly(owner, account, *, description, amount_minor=-2000):
    add_monthly_charges(owner, account, description=description, amount_minor=amount_minor, count=6)
    refresh_recurring_series(owner)
    series = RecurringSeries.objects.get(person=owner, display_name=description)
    confirm_recurring_series(owner, series.pk)
    # Only alerts raised by the pass itself count below.
    Alert.objects.all().delete()
    return series


def recurring_alerts(kind):
    return list(Alert.objects.filter(kind=kind).values_list("recipient_id", "dedupe_key"))


def test_daily_pass_alone_raises_one_missed_alert_for_the_household():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    series = confirmed_monthly(owner, shared, description="Synthetic Shared Bill")

    run_daily_alert_pass(today=date(2026, 9, 3))

    key = f"recurring:{series.pk}:missed:2026-07-15"
    assert sorted(recurring_alerts(Alert.Kind.RECURRING_MISSED)) == sorted(
        [(owner.pk, key), (member.pk, key)]
    )


def test_private_missed_alert_reaches_only_the_owner():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    series = confirmed_monthly(owner, private, description="Synthetic Private Bill")

    run_daily_alert_pass(today=date(2026, 9, 3))

    assert recurring_alerts(Alert.Kind.RECURRING_MISSED) == [
        (owner.pk, f"recurring:{series.pk}:missed:2026-07-15")
    ]


def test_imported_higher_charge_raises_price_alert_and_no_missed_alert():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    account = make_account(owner)
    series = confirmed_monthly(owner, account, description="Synthetic Price Bill")
    today = date(2026, 7, 28)
    series.refresh_from_db()
    # Judged on its stored charges, the series would look missed.
    assert missed_charge(series, owner, today=today) is not None
    make_transaction(
        owner, account, transaction_date=date(2026, 7, 15), amount_minor=-2400, description="Synthetic Price Bill"
    )

    run_daily_alert_pass(today=today)

    # The account is private, so the price alert never reaches the other member.
    assert recurring_alerts(Alert.Kind.RECURRING_PRICE) == [(owner.pk, f"recurring:{series.pk}:-2400")]
    assert recurring_alerts(Alert.Kind.RECURRING_MISSED) == []
    assert series.members.count() == 7


def test_price_alert_respects_each_recipients_threshold():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    prefs = settings_for(member)
    prefs.recurring_price_percent = 30
    prefs.save()
    series = confirmed_monthly(owner, shared, description="Synthetic Shared Price")
    make_transaction(
        owner, shared, transaction_date=date(2026, 7, 15), amount_minor=-2400, description="Synthetic Shared Price"
    )

    run_daily_alert_pass(today=date(2026, 7, 20))

    assert recurring_alerts(Alert.Kind.RECURRING_PRICE) == [(owner.pk, f"recurring:{series.pk}:-2400")]
    assert "20.00 USD to 24.00 USD" in Alert.objects.get(kind=Alert.Kind.RECURRING_PRICE).title


def test_second_pass_adds_no_duplicates_and_empty_members_are_fine():
    owner = make_person("owner")
    make_household(owner)
    make_person("loner")
    make_household(make_person("no-accounts"), name="Synthetic Empty Household")
    account = make_account(owner)
    confirmed_monthly(owner, account, description="Synthetic Repeat Bill")

    run_daily_alert_pass(today=date(2026, 9, 3))
    first = Alert.objects.count()
    run_daily_alert_pass(today=date(2026, 9, 3))

    assert first >= 1
    assert Alert.objects.count() == first


def test_inactive_users_are_not_refreshed():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account, description="Synthetic Dormant Stream", count=3)
    owner.user.is_active = False
    owner.user.save()

    run_daily_alert_pass()

    assert not RecurringSeries.objects.filter(person=owner).exists()


def test_one_members_failure_does_not_stop_others_or_later_steps(caplog):
    failing = make_person("failing")
    make_household(failing)
    other = make_person("other")
    make_household(other, name="Synthetic Other Household")
    add_monthly_charges(other, make_account(other), description="Synthetic Other Stream", count=3)
    real_refresh = refresh_recurring_series

    def refresh(person, **kwargs):
        if person.pk == failing.pk:
            raise RuntimeError("Synthetic Merchant -12.34 USD")
        return real_refresh(person, **kwargs)

    with (
        patch("finance.recurring_services.refresh_recurring_series", side_effect=refresh),
        patch("finance.receipt_services.sweep_orphan_receipt_files") as sweep,
        caplog.at_level(logging.ERROR, logger="finance.alert_services"),
        pytest.raises(DailyAlertPassIncomplete) as raised,
    ):
        run_daily_alert_pass()

    # The pass finishes every step, then reports the failed one (#513).
    assert raised.value.steps == ("recurring_refresh",)
    assert RecurringSeries.objects.filter(person=other).exists()
    sweep.assert_called_once()
    assert f"member {failing.pk} failed (RuntimeError)" in caplog.text
    assert "12.34" not in caplog.text
    assert "Synthetic Merchant" not in caplog.text


def test_email_notice_includes_recurring_alerts_from_the_pass(settings, django_capture_on_commit_callbacks):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EMAIL_HOST = "smtp.example.invalid"
    settings.DEFAULT_FROM_EMAIL = "planner@example.invalid"
    settings.ALERT_EMAIL_BASE_URL = "https://planner.example.invalid"
    owner = make_person("owner")
    make_household(owner)
    confirmed_monthly(owner, make_account(owner), description="Synthetic Mailed Bill")
    prefs = settings_for(owner)
    prefs.email_enabled = True
    prefs.notification_email = "owner@example.invalid"
    prefs.save()

    with django_capture_on_commit_callbacks(execute=True):
        run_daily_alert_pass(today=date(2026, 9, 3))

    assert len(mail.outbox) == 1
    assert Alert.Kind.RECURRING_MISSED.label in mail.outbox[0].body
    assert not Alert.objects.filter(kind=Alert.Kind.RECURRING_MISSED, email_notice_sent_at__isnull=True).exists()


def test_new_series_from_the_daily_refresh_reaches_the_weekly_digest():
    owner = make_person("owner")
    make_household(owner)
    add_monthly_charges(owner, make_account(owner), description="Synthetic New Stream", count=3)
    assert not Person.objects.get(pk=owner.pk).recurring_inputs_signature

    run_daily_alert_pass()

    digest = build_weekly_digest(owner, today=timezone.localdate(), week="current")
    assert [item.title for item in digest.new_series] == ["Synthetic New Stream"]
