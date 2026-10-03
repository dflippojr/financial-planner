from datetime import date, datetime, timedelta, timezone as dt_timezone
from hashlib import sha256
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.category_services import ensure_household_categories
from finance.models import (
    Account,
    Alert,
    Household,
    ImportBatch,
    Membership,
    Person,
    RecurringExclusion,
    RecurringSeries,
    RecurringSeriesMember,
    Transaction,
)
from finance.planning_services import projected_months_for
from finance.recurring_review import (
    is_resumed,
    missed_charge,
    price_change,
    raise_recurring_review_alerts,
    upcoming_charge,
    upcoming_charges,
)
from finance.recurring_services import (
    cancel_recurring_series,
    confirm_resume_recurring_series,
    confirmed_totals,
    dismiss_price_change,
    keep_cancelled_recurring_series,
    undo_cancel_recurring_series,
)


PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 10, 3)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(
    owner,
    *,
    name="Synthetic Checking",
    account_type=Account.Type.CHECKING,
    scope=Account.Scope.PRIVATE,
    household=None,
    share_mode=None,
):
    if share_mode is None:
        share_mode = Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else ""
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=share_mode,
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 2),
    amount_minor=-1000,
    description="Synthetic row",
    range_start=date(2026, 1, 1),
    range_end=date(2026, 12, 31),
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=sha256(
            f"{account.pk}-{transaction_date}-{amount_minor}-{description}".encode()
        ).hexdigest(),
        date_range_start=range_start,
        date_range_end=range_end,
    )
    digest = sha256(f"txn-{account.pk}-{amount_minor}-{transaction_date}-{description}".encode()).hexdigest()
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def make_series(person, *, name, cadence, typical_minor=-1000, fingerprint=None, cancelled_at=None):
    digest = fingerprint or sha256(f"{person.pk}-{name}-{cadence}".encode()).hexdigest()
    return RecurringSeries.objects.create(
        person=person,
        merchant_key=name.casefold(),
        display_name=name,
        cadence=cadence,
        typical_amount_minor=typical_minor,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=digest,
        cancelled_at=cancelled_at,
    )


def add_member(series, txn):
    return RecurringSeriesMember.objects.create(series=series, transaction=txn)


@pytest.mark.django_db
def test_upcoming_lists_next_dates_for_each_cadence():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    cases = (
        (RecurringSeries.Cadence.WEEKLY, date(2026, 9, 28), date(2026, 10, 5)),
        (RecurringSeries.Cadence.BIWEEKLY, date(2026, 9, 21), date(2026, 10, 5)),
        (RecurringSeries.Cadence.MONTHLY, date(2026, 9, 15), date(2026, 10, 15)),
        (RecurringSeries.Cadence.QUARTERLY, date(2026, 7, 15), date(2026, 10, 15)),
        (RecurringSeries.Cadence.ANNUAL, date(2025, 10, 20), date(2026, 10, 20)),
    )
    series_rows = []
    for cadence, last_on, _expected in cases:
        series = make_series(owner, name=f"Synthetic {cadence}", cadence=cadence)
        add_member(
            series,
            make_transaction(owner, account, transaction_date=last_on, description=f"Synthetic {cadence}"),
        )
        series_rows.append(series)

    upcoming = upcoming_charges(series_rows, today=TODAY)
    found = {item.series.cadence: item.expected_on for item in upcoming}
    for cadence, _last_on, expected in cases:
        assert found[cadence] == expected
    dates = [item.expected_on for item in upcoming]
    assert dates == sorted(dates)


@pytest.mark.django_db
def test_ten_percent_price_rise_is_flagged_and_nine_percent_is_not():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    flagged = make_series(
        owner, name="Synthetic Rise", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    stable = make_series(
        owner, name="Synthetic Stable", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    for day, amount in (
        (date(2026, 7, 10), -1000),
        (date(2026, 8, 10), -1000),
        (date(2026, 9, 10), -1100),
    ):
        add_member(
            flagged,
            make_transaction(
                owner, account, transaction_date=day, amount_minor=amount, description="Synthetic Rise"
            ),
        )
    for day, amount in (
        (date(2026, 7, 10), -1000),
        (date(2026, 8, 10), -1000),
        (date(2026, 9, 10), -1090),
    ):
        add_member(
            stable,
            make_transaction(
                owner, account, transaction_date=day, amount_minor=amount, description="Synthetic Stable"
            ),
        )

    change = price_change(flagged)
    assert change is not None
    assert change.previous_minor == -1000
    assert change.new_minor == -1100
    assert change.percent_display == "10.0%"
    assert change.charge_date == date(2026, 9, 10)
    assert price_change(stable) is None

    dismiss_price_change(owner, flagged.pk)
    flagged.refresh_from_db()
    assert price_change(flagged) is None
    extra = make_transaction(
        owner, account, transaction_date=date(2026, 10, 10), amount_minor=-1210, description="Synthetic Rise later"
    )
    add_member(flagged, extra)
    again = price_change(flagged)
    assert again is not None
    assert again.new_minor == -1210


@pytest.mark.django_db
def test_missed_charge_shows_missing_import_state():
    owner = make_person("owner")
    make_household(owner)
    missing_account = make_account(owner, name="Missing Account")
    present_account = make_account(owner, name="Present Account")
    missing_series = make_series(owner, name="Synthetic Missing", cadence=RecurringSeries.Cadence.MONTHLY)
    present_series = make_series(owner, name="Synthetic Present", cadence=RecurringSeries.Cadence.MONTHLY)
    add_member(
        missing_series,
        make_transaction(
            owner,
            missing_account,
            transaction_date=date(2026, 8, 15),
            description="Synthetic Missing",
            range_start=date(2026, 8, 1),
            range_end=date(2026, 8, 31),
        ),
    )
    add_member(
        present_series,
        make_transaction(
            owner,
            present_account,
            transaction_date=date(2026, 8, 15),
            description="Synthetic Present",
            range_start=date(2026, 1, 1),
            range_end=date(2026, 12, 31),
        ),
    )

    missing = missed_charge(missing_series, owner, today=TODAY)
    present = missed_charge(present_series, owner, today=TODAY)
    assert missing is not None
    assert missing.expected_on == date(2026, 9, 15)
    assert missing.missing_import is True
    assert present is not None
    assert present.missing_import is False


@pytest.mark.django_db
def test_cancel_leaves_totals_and_projection_and_resume_resolutions_work():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    txn = make_transaction(
        owner, account, transaction_date=date(2026, 9, 10), amount_minor=-2500, description="Synthetic Stream"
    )
    series = make_series(
        owner, name="Synthetic Stream", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-2500
    )
    add_member(series, txn)
    before_months, before_annual = confirmed_totals([series])
    assert before_months > 0
    projected = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    assert projected[0].spending_minor == 2500

    cancel_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is not None
    assert confirmed_totals([series]) == (0, 0)
    cancelled_projection = projected_months_for(owner, today=date(2026, 10, 1), horizon=3)
    assert cancelled_projection[0].spending_minor == 0

    later = make_transaction(
        owner,
        account,
        transaction_date=timezone.localdate() + timedelta(days=14),
        amount_minor=-2500,
        description="Synthetic Stream",
    )
    add_member(series, later)
    assert is_resumed(series) is True
    confirm_resume_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is None
    assert confirmed_totals([series]) == (before_months, before_annual)

    cancel_recurring_series(owner, series.pk)
    keep_cancelled_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is not None
    assert RecurringExclusion.objects.filter(person=owner, transaction=later).exists()
    assert not RecurringSeriesMember.objects.filter(series=series, transaction=later).exists()
    assert is_resumed(series) is False
    undo_cancel_recurring_series(owner, series.pk)
    series.refresh_from_db()
    assert series.cancelled_at is None


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=TODAY)
@patch("finance.recurring_services.timezone.localdate", return_value=TODAY)
def test_recurring_page_shows_review_sections_and_actions(_refresh_today, _view_today):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(
        owner, name="Synthetic Monthly", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    add_member(
        series,
        make_transaction(owner, account, transaction_date=date(2026, 9, 15), description="Synthetic Monthly"),
    )
    client = Client()
    client.force_login(owner.user)
    page = client.get(reverse("recurring-review"))
    html = page.content.decode()
    assert page.status_code == 200
    assert "Upcoming" in html
    expected_dates = [item.expected_on for item in page.context["upcoming"]]
    assert date(2026, 10, 15) in expected_dates
    assert "Mark cancelled" in html

    cancel = client.post(reverse("recurring-review"), {"series_id": series.pk, "action": "cancel"})
    assert cancel.status_code == 302
    series.refresh_from_db()
    cancelled_page = client.get(reverse("recurring-review"))
    cancelled_html = cancelled_page.content.decode()
    assert series.cancelled_at is not None
    assert "Cancelled" in cancelled_html
    assert cancelled_page.context["monthly_minor"] == 0


@pytest.mark.django_db
def test_price_and_missed_alerts_dedupe_and_follow_account_visibility():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private_series = make_series(
        owner, name="Synthetic Private Bill", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    shared_series = make_series(
        owner, name="Synthetic Shared Bill", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-1000
    )
    for day, amount in (
        (date(2026, 7, 10), -1000),
        (date(2026, 8, 10), -1000),
        (date(2026, 9, 10), -1100),
    ):
        add_member(
            private_series,
            make_transaction(
                owner,
                private,
                transaction_date=day,
                amount_minor=amount,
                description="Synthetic Private Bill",
            ),
        )
    add_member(
        shared_series,
        make_transaction(
            owner,
            shared,
            transaction_date=date(2026, 8, 15),
            amount_minor=-1000,
            description="Synthetic Shared Bill",
            range_start=date(2026, 8, 1),
            range_end=date(2026, 8, 31),
        ),
    )

    rows = [private_series, shared_series]
    first = raise_recurring_review_alerts(owner, rows, today=TODAY)
    second = raise_recurring_review_alerts(owner, rows, today=TODAY)
    assert second == []
    price_keys = list(
        Alert.objects.filter(kind=Alert.Kind.RECURRING_PRICE).values_list("recipient_id", "dedupe_key")
    )
    missed_keys = list(
        Alert.objects.filter(kind=Alert.Kind.RECURRING_MISSED).values_list("recipient_id", "dedupe_key")
    )
    assert (owner.pk, f"recurring:{private_series.pk}:-1100") in price_keys
    assert (member.pk, f"recurring:{private_series.pk}:-1100") not in price_keys
    assert (owner.pk, f"recurring:{shared_series.pk}:missed:2026-09-15") in missed_keys
    assert (member.pk, f"recurring:{shared_series.pk}:missed:2026-09-15") in missed_keys
    assert len(first) >= 2


@pytest.mark.django_db
def test_member_does_not_see_private_series_review():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    series = make_series(owner, name="Synthetic Secret", cadence=RecurringSeries.Cadence.MONTHLY)
    add_member(
        series,
        make_transaction(owner, private, transaction_date=date(2026, 9, 15), description="Synthetic Secret"),
    )
    client = Client()
    client.force_login(member.user)
    page = client.get(reverse("recurring-review"))
    html = page.content.decode()
    assert "Synthetic Secret" not in html
    forbidden = client.post(reverse("recurring-review"), {"series_id": series.pk, "action": "cancel"})
    assert forbidden.status_code == 404
    series.refresh_from_db()
    assert series.cancelled_at is None


@pytest.mark.django_db
def test_cancelled_series_is_not_upcoming():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(
        owner,
        name="Synthetic Ended",
        cadence=RecurringSeries.Cadence.MONTHLY,
        cancelled_at=datetime(2026, 9, 1, tzinfo=dt_timezone.utc),
    )
    add_member(
        series,
        make_transaction(owner, account, transaction_date=date(2026, 9, 15), description="Synthetic Ended"),
    )
    assert upcoming_charge(series, today=TODAY) is None
    assert missed_charge(series, owner, today=TODAY) is None
    assert price_change(series) is None

