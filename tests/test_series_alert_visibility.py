"""A recurring series alert stays visible only while every account behind it is (#520). Synthetic data only."""

from datetime import date

import pytest
from django.urls import reverse

from finance.alert_services import alerts_for, unread_alert_count
from finance.lifecycle_services import delete_account, leave_household, unshare_account
from finance.models import Account, Alert, RecurringSeries
from finance.recurring_review import raise_recurring_review_alerts, series_accounts
from finance.recurring_services import confirm_recurring_series, refresh_recurring_series
from tests.test_alerts import signed_in
from tests.test_recurring import add_monthly_charges, make_account, make_household, make_person, make_transaction

pytestmark = pytest.mark.django_db

TODAY = date(2026, 7, 20)
TITLE = "Synthetic Streaming changed from 10.00 USD to 12.34 USD"


def household_with_split_series():
    """Six 10.00 charges on a co-owned joint account, then 12.34 on the owner's lent account."""
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    joint = make_account(owner, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    lent = make_account(
        owner,
        name="Synthetic Lent",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.LENT,
    )
    series = confirmed_series(owner, joint, "Synthetic Streaming", -1000, lent, -1234)
    return owner, member, joint, lent, series


def confirmed_series(owner, account, description, amount_minor, latest_account, latest_minor):
    """Confirm a monthly series of six charges, then add a seventh at a new price."""
    add_monthly_charges(owner, account, description=description, amount_minor=amount_minor, count=6)
    refresh_recurring_series(owner, today=TODAY)
    series = RecurringSeries.objects.get(person=owner, display_name=description)
    confirm_recurring_series(owner, series.pk)
    make_transaction(
        owner, latest_account, transaction_date=date(2026, 7, 15), amount_minor=latest_minor, description=description
    )
    refresh_recurring_series(owner, today=TODAY)
    Alert.objects.all().delete()
    return RecurringSeries.objects.prefetch_related("members__transaction__account").get(pk=series.pk)


def visible_titles(person):
    return list(alerts_for(person).values_list("title", flat=True))


def test_price_alert_reaches_both_members_while_every_account_is_shared():
    owner, member, _joint, _lent, series = household_with_split_series()

    raise_recurring_review_alerts(owner, [series], today=TODAY)

    assert visible_titles(owner) == [TITLE]
    assert visible_titles(member) == [TITLE]


def test_unsharing_an_account_in_the_series_hides_the_alert_from_the_other_member():
    owner, member, _joint, lent, series = household_with_split_series()
    raise_recurring_review_alerts(owner, [series], today=TODAY)

    unshare_account(owner, lent.pk)

    assert visible_titles(member) == []
    assert unread_alert_count(member) == 0
    page = signed_in(member).get(reverse("alert-list"))
    assert b"12.34" not in page.content
    assert visible_titles(owner) == [TITLE]
    assert unread_alert_count(owner) == 1


def test_missed_charge_alert_is_hidden_the_same_way():
    owner, member, _joint, lent, series = household_with_split_series()
    # Long after the next expected charge, with none seen.
    raise_recurring_review_alerts(owner, [series], today=date(2026, 10, 1))
    missed = Alert.objects.filter(kind=Alert.Kind.RECURRING_MISSED)
    assert sorted(missed.values_list("recipient_id", flat=True)) == sorted([owner.pk, member.pk])

    unshare_account(owner, lent.pk)

    assert not alerts_for(member).filter(kind=Alert.Kind.RECURRING_MISSED).exists()
    assert alerts_for(owner).filter(kind=Alert.Kind.RECURRING_MISSED).count() == 1


def test_alerts_raised_after_the_unshare_go_only_to_the_owner():
    owner, member, _joint, lent, _series = household_with_split_series()
    unshare_account(owner, lent.pk)
    series = RecurringSeries.objects.prefetch_related("members__transaction__account").get(
        person=owner, display_name="Synthetic Streaming"
    )

    raise_recurring_review_alerts(owner, [series], today=TODAY)

    assert list(Alert.objects.values_list("recipient_id", flat=True)) == [owner.pk]


def test_a_member_who_leaves_no_longer_sees_the_series_alert():
    owner, member, _joint, _lent, series = household_with_split_series()
    raise_recurring_review_alerts(owner, [series], today=TODAY)

    leave_household(member)

    assert visible_titles(member) == []
    assert visible_titles(owner) == [TITLE]


def test_another_series_alert_is_unaffected_by_an_unrelated_unshare():
    owner, member, joint, lent, series = household_with_split_series()
    household = joint.household
    other = make_account(owner, name="Synthetic Other Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    gym = confirmed_series(owner, other, "Synthetic Gym", -2000, other, -2400)
    series = RecurringSeries.objects.prefetch_related("members__transaction__account").get(pk=series.pk)
    raise_recurring_review_alerts(owner, [series, gym], today=TODAY)

    unshare_account(owner, lent.pk)

    assert visible_titles(member) == ["Synthetic Gym changed from 20.00 USD to 24.00 USD"]


def test_alert_stays_hidden_after_the_series_drops_the_now_private_charge():
    # The series belongs to the member; its latest charge sits on the owner's lent account.
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    joint = make_account(member, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    lent = make_account(
        owner,
        name="Synthetic Lent",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.LENT,
    )
    series = confirmed_series(member, joint, "Synthetic Streaming", -1000, lent, -1234)
    assert {account.pk for account in series_accounts(series)} == {joint.pk, lent.pk}
    raise_recurring_review_alerts(member, [series], today=TODAY)
    assert visible_titles(member) == [TITLE]

    unshare_account(owner, lent.pk)
    # The member's next refresh drops the charge they can no longer see.
    refresh_recurring_series(member, today=TODAY)
    series = RecurringSeries.objects.get(pk=series.pk)
    assert {account.pk for account in series_accounts(series)} == {joint.pk}

    # A missed-charge alert for the joint-only series may follow; nothing quotes 12.34.
    assert not any("12.34" in title for title in visible_titles(member))
    assert TITLE in visible_titles(owner)


def test_deleting_an_account_removes_the_alerts_that_quote_it():
    owner, member, _joint, lent, series = household_with_split_series()
    raise_recurring_review_alerts(owner, [series], today=TODAY)

    delete_account(owner, lent.pk)

    assert not Alert.objects.filter(kind=Alert.Kind.RECURRING_PRICE).exists()


def test_migration_backfills_sources_for_existing_series_alerts():
    from importlib import import_module

    from django.apps import apps

    backfill = import_module("finance.migrations.0070_alert_source_accounts").backfill_series_alert_sources
    owner, member, joint, lent, series = household_with_split_series()
    raise_recurring_review_alerts(owner, [series], today=TODAY)
    Alert.source_accounts.through.objects.all().delete()  # As before the migration.

    backfill(apps, None)

    for alert in Alert.objects.all():
        assert {account.pk for account in alert.source_accounts.all()} == {joint.pk, lent.pk}
    unshare_account(owner, lent.pk)
    assert visible_titles(member) == []
