from datetime import date
from hashlib import sha256
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.alert_services import alerts_for, save_alert_settings, settings_for
from finance.bills_calendar import (
    build_month,
    calendar_inputs,
    evaluate_expected_balance_alert,
    expected_balances_by_day,
    first_below_in_next_days,
    save_calendar_settings,
)
from finance.category_services import ensure_household_categories
from finance.models import (
    Account,
    Alert,
    BalanceSnapshot,
    Household,
    ImportBatch,
    Membership,
    Person,
    PlannedItem,
    RecurringSeries,
    RecurringSeriesMember,
    Transaction,
)
from finance.recurring_review import upcoming_charges
from finance.recurring_services import cancel_recurring_series


PASSWORD = "Synthetic-passphrase-42!"
TODAY = date(2026, 10, 4)


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
    transaction_date=date(2026, 9, 10),
    amount_minor=-2500,
    description="Synthetic recurring charge",
    fingerprint=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(transaction_date.year, transaction_date.month, 1),
        date_range_end=date(transaction_date.year, transaction_date.month, 28),
        source_file_sha256=sha256(f"{account.pk}-{transaction_date}-{amount_minor}-{description}".encode()).hexdigest(),
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        currency="USD",
        description=description,
        source_row_number=1,
        fingerprint=fingerprint
        or sha256(f"txn-{account.pk}-{amount_minor}-{transaction_date}-{description}".encode()).hexdigest(),
        original_fields={"Date": transaction_date.isoformat()},
    )


def make_series(person, *, name, cadence, typical_minor=-2500):
    return RecurringSeries.objects.create(
        person=person,
        merchant_key=name.casefold(),
        display_name=name,
        cadence=cadence,
        typical_amount_minor=typical_minor,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint=sha256(f"{person.pk}-{name}-{cadence}".encode()).hexdigest(),
    )


def add_snapshot(account, snapshot_date, amount_minor):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def _alert_kwargs(**overrides):
    payload = dict(
        sync_enabled=True,
        recurring_price_enabled=True,
        recurring_missed_enabled=True,
        budget_enabled=True,
        large_transaction_enabled=True,
        monthly_review_enabled=True,
        monthly_review_ai_enabled=True,
        expected_balance_enabled=False,
        large_transaction_minor=None,
    )
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_calendar_items_match_recurring_review_and_planned_items():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(owner, name="Synthetic Stream", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-2500)
    RecurringSeriesMember.objects.create(
        series=series,
        transaction=make_transaction(owner, account, transaction_date=date(2026, 9, 15), description="Synthetic Stream"),
    )
    PlannedItem.objects.create(
        owner=owner,
        name="Synthetic paycheck",
        kind=PlannedItem.Kind.INCOME,
        amount_minor=100000,
        start_date=date(2026, 10, 20),
        cadence=PlannedItem.Cadence.ONE_TIME,
    )
    upcoming = upcoming_charges([series], today=TODAY)
    assert upcoming[0].expected_on == date(2026, 10, 15)
    calendar = build_month(owner, year=2026, month=10, today=TODAY, account_ids=[account.pk])
    by_date = {row.date: row for row in calendar.days}
    names_15 = [item.name for item in by_date[date(2026, 10, 15)].items]
    names_20 = [item.name for item in by_date[date(2026, 10, 20)].items]
    assert "Synthetic Stream" in names_15
    assert "Synthetic paycheck" in names_20


@pytest.mark.django_db
def test_balance_math_adds_expected_items_from_latest_snapshots():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    other = make_account(owner, name="Synthetic Other")
    add_snapshot(checking, date(2026, 10, 1), 50_000)
    add_snapshot(other, date(2026, 10, 1), 80_000)
    series = make_series(owner, name="Synthetic rent", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-10_000)
    RecurringSeriesMember.objects.create(
        series=series,
        transaction=make_transaction(owner, checking, transaction_date=date(2026, 9, 10), description="Synthetic rent"),
    )
    other_series = make_series(
        owner, name="Synthetic other bill", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-5_000
    )
    RecurringSeriesMember.objects.create(
        series=other_series,
        transaction=make_transaction(
            owner, other, transaction_date=date(2026, 9, 10), description="Synthetic other bill"
        ),
    )
    PlannedItem.objects.create(
        owner=owner,
        name="Synthetic bonus",
        kind=PlannedItem.Kind.INCOME,
        amount_minor=20_000,
        start_date=date(2026, 10, 6),
        cadence=PlannedItem.Cadence.ONE_TIME,
    )
    calendar = build_month(
        owner,
        year=2026,
        month=10,
        today=TODAY,
        account_ids=[checking.pk],
        threshold_minor=45_000,
    )
    by_date = {row.date: row for row in calendar.days}
    assert by_date[date(2026, 10, 4)].balance_minor == 50_000
    assert by_date[date(2026, 10, 6)].balance_minor == 70_000
    assert by_date[date(2026, 10, 10)].balance_minor == 60_000
    assert by_date[date(2026, 10, 10)].below_threshold is False
    tight = expected_balances_by_day(
        calendar_inputs(owner),
        start_balance=50_000,
        selected_ids={checking.pk},
        from_date=TODAY,
        through_date=date(2026, 10, 12),
    )
    assert tight[date(2026, 10, 4)] == 50_000
    assert tight[date(2026, 10, 10)] == 60_000


@pytest.mark.django_db
def test_past_days_show_actuals_instead_of_expected():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    series = make_series(owner, name="Synthetic Stream", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-2500)
    RecurringSeriesMember.objects.create(
        series=series,
        transaction=make_transaction(owner, account, transaction_date=date(2026, 9, 1), description="Synthetic Stream"),
    )
    actual = make_transaction(
        owner,
        account,
        transaction_date=date(2026, 10, 1),
        amount_minor=-2500,
        description="Synthetic posted stream",
    )
    calendar = build_month(owner, year=2026, month=10, today=TODAY, account_ids=[account.pk])
    by_date = {row.date: row for row in calendar.days}
    past_names = [item.name for item in by_date[date(2026, 10, 1)].items]
    assert "Synthetic posted stream" in past_names
    assert "Synthetic Stream" not in past_names
    assert any(item.url.endswith(f"/transactions/{actual.pk}/edit/") for item in by_date[date(2026, 10, 1)].items)


@pytest.mark.django_db
def test_cancelled_series_and_private_items_stay_out_of_scope():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    owner_account = make_account(owner)
    member_account = make_account(member, name="Member Checking")
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    owner_series = make_series(owner, name="Owner Stream", cadence=RecurringSeries.Cadence.MONTHLY)
    RecurringSeriesMember.objects.create(
        series=owner_series,
        transaction=make_transaction(owner, owner_account, transaction_date=date(2026, 9, 15), description="Owner Stream"),
    )
    cancel_recurring_series(owner, owner_series.pk)
    member_series = make_series(member, name="Member Secret", cadence=RecurringSeries.Cadence.MONTHLY)
    RecurringSeriesMember.objects.create(
        series=member_series,
        transaction=make_transaction(
            member, member_account, transaction_date=date(2026, 9, 15), description="Member Secret"
        ),
    )
    PlannedItem.objects.create(
        owner=member,
        name="Member private plan",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=1111,
        start_date=date(2026, 10, 12),
        cadence=PlannedItem.Cadence.ONE_TIME,
    )
    shared_series = make_series(owner, name="Shared Stream", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-3000)
    RecurringSeriesMember.objects.create(
        series=shared_series,
        transaction=make_transaction(owner, shared, transaction_date=date(2026, 9, 20), description="Shared Stream"),
    )
    owner_cal = build_month(owner, year=2026, month=10, today=TODAY, account_ids=[owner_account.pk, shared.pk])
    member_cal = build_month(member, year=2026, month=10, today=TODAY, account_ids=[member_account.pk, shared.pk])
    owner_names = [item.name for day in owner_cal.days for item in day.items]
    member_names = [item.name for day in member_cal.days for item in day.items]
    assert "Owner Stream" not in owner_names
    assert "Member Secret" not in owner_names
    assert "Member private plan" not in owner_names
    assert "Shared Stream" in owner_names
    assert "Member Secret" in member_names
    assert "Member private plan" in member_names
    assert "Owner Stream" not in member_names
    household_only = build_month(
        owner, year=2026, month=10, today=TODAY, scope="household", account_ids=[shared.pk]
    )
    household_names = [item.name for day in household_only.days for item in day.items]
    assert "Shared Stream" in household_names
    assert "Member Secret" not in household_names


@pytest.mark.django_db
@patch("finance.bills_calendar_views.timezone.localdate", return_value=TODAY)
def test_calendar_page_and_alert_setting(_localdate):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    add_snapshot(checking, date(2026, 10, 1), 10_000)
    series = make_series(owner, name="Synthetic rent", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-8_000)
    RecurringSeriesMember.objects.create(
        series=series,
        transaction=make_transaction(owner, checking, transaction_date=date(2026, 9, 10), description="Synthetic rent"),
    )
    client = signed_in(owner)
    page = client.get(reverse("bills-calendar"))
    html = page.content.decode()
    assert page.status_code == 200
    assert "expected, not guaranteed" in html
    assert "Bills" in html
    save = client.post(
        reverse("bills-calendar"),
        {"accounts": [str(checking.pk)], "threshold_amount": "50.00", "year": "2026", "month": "10"},
    )
    assert save.status_code == 302
    saved = client.get(reverse("bills-calendar") + "?year=2026&month=10")
    assert b"Synthetic rent" in saved.content
    assert b'id="series-' in client.get(reverse("recurring-review")).content

    save_alert_settings(owner, **_alert_kwargs(expected_balance_enabled=True))
    assert first_below_in_next_days(owner, today=TODAY) == date(2026, 10, 10)
    created = evaluate_expected_balance_alert(owner, today=TODAY)
    assert created
    assert created[0].title == "Expected balance below threshold in the next 7 days"
    assert alerts_for(owner).filter(kind=Alert.Kind.EXPECTED_BALANCE).exists()

    settings_page = client.get(reverse("settings-alerts"))
    assert b"Expected balance below threshold in the next 7 days" in settings_page.content
    client.post(
        reverse("settings-alerts"),
        {
            "sync_enabled": "on",
            "expected_balance_enabled": "on",
        },
    )
    assert settings_for(owner).expected_balance_enabled is True


@pytest.mark.django_db
def test_expected_balance_alert_is_off_by_default():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    add_snapshot(checking, date(2026, 10, 1), 1_000)
    save_calendar_settings(owner, account_ids=[checking.pk], threshold_minor=5_000)
    prefs = settings_for(owner)
    assert prefs.expected_balance_enabled is False
    assert evaluate_expected_balance_alert(owner, today=TODAY) == []
    assert not Alert.objects.filter(kind=Alert.Kind.EXPECTED_BALANCE).exists()


@pytest.mark.django_db
def test_starting_balance_includes_transactions_after_the_latest_snapshot():
    from finance.bills_calendar import starting_balance_minor

    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    add_snapshot(checking, date(2026, 10, 1), 100_000)
    make_transaction(owner, checking, transaction_date=date(2026, 10, 2), amount_minor=-80_000, description="Synthetic rent")
    make_transaction(owner, checking, transaction_date=date(2026, 9, 30), amount_minor=-5_000, description="Synthetic before snapshot")
    make_transaction(owner, checking, transaction_date=date(2026, 10, 6), amount_minor=-1_000, description="Synthetic after today")
    make_transaction(owner, checking, transaction_date=date(2026, 10, 4), amount_minor=-7_000, description="Synthetic posted today")

    assert starting_balance_minor([checking], as_of=date(2026, 10, 4)) == 13_000


@pytest.mark.django_db
def test_today_skips_a_posted_series_charge_but_keeps_planned_items_and_counts_unplanned():
    from finance.bills_calendar import starting_balance_minor

    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    today = date(2026, 10, 4)
    add_snapshot(checking, date(2026, 10, 3), 100_000)
    series = make_series(owner, name="Synthetic gym", cadence=RecurringSeries.Cadence.MONTHLY, typical_minor=-3_000)
    charge = make_transaction(owner, checking, transaction_date=today, amount_minor=-3_000, description="Synthetic gym")
    RecurringSeriesMember.objects.create(series=series, transaction=charge)
    PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.PRIVATE,
        name="Synthetic rent",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=10_000,
        start_date=today,
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    make_transaction(owner, checking, transaction_date=today, amount_minor=-10_000, description="Synthetic unrelated purchase")
    sources = [item for item in calendar_inputs(owner) if item.source_id in {series.pk} or item.name == "Synthetic rent"]
    start = starting_balance_minor([checking], as_of=today)

    balances = expected_balances_by_day(
        sources, start_balance=start, selected_ids={checking.pk}, from_date=today, through_date=today
    )

    # 1,000.00 - 30.00 gym (posted, not re-applied) - 100.00 purchase - 100.00 planned rent still due.
    assert balances[today] == 77_000
