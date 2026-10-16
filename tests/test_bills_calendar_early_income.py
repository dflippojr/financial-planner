"""A paycheck that posts a few days early counts once in the forecast (#508). Synthetic data only."""

from datetime import date, timedelta

import pytest

from finance.bills_calendar import build_month, first_below_in_next_days, save_calendar_settings
from finance.models import PlannedItem
from tests.test_bills_calendar import add_snapshot, make_account, make_household, make_person, make_transaction

PAYDAY = date(2026, 10, 16)


def _paycheck(owner, *, start=PAYDAY, cadence=PlannedItem.Cadence.BIWEEKLY):
    return PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.PRIVATE,
        name="Synthetic paycheck",
        kind=PlannedItem.Kind.INCOME,
        amount_minor=200_000,
        start_date=start,
        cadence=cadence,
    )


def _deposit(owner, account, on):
    return make_transaction(owner, account, transaction_date=on, amount_minor=200_000, description="Synthetic payroll")


@pytest.fixture
def checking():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_snapshot(account, date(2026, 10, 10), 50_000)
    return owner, account


def _balances(owner, account, today, days):
    calendar = build_month(owner, year=2026, month=10, today=today, account_ids=[account.pk])
    by_day = {row.date: row.balance_minor for row in calendar.days}
    return [by_day[today + timedelta(days=offset)] for offset in range(days)]


@pytest.mark.django_db
def test_a_paycheck_a_day_early_counts_once_on_the_posting_and_expected_days(checking):
    owner, account = checking
    _paycheck(owner)
    _deposit(owner, account, date(2026, 10, 15))

    assert _balances(owner, account, date(2026, 10, 15), 3) == [250_000, 250_000, 250_000]
    assert _balances(owner, account, date(2026, 10, 16), 2) == [250_000, 250_000]


@pytest.mark.django_db
@pytest.mark.parametrize("days_early", [1, 2, 3])
def test_a_paycheck_up_to_the_tolerance_early_counts_once(checking, days_early):
    owner, account = checking
    _paycheck(owner)
    _deposit(owner, account, PAYDAY - timedelta(days=days_early))
    today = PAYDAY - timedelta(days=1)

    assert _balances(owner, account, today, 3) == [250_000, 250_000, 250_000]


@pytest.mark.django_db
def test_a_deposit_past_the_tolerance_is_not_matched(checking):
    owner, account = checking
    _paycheck(owner)
    _deposit(owner, account, PAYDAY - timedelta(days=4))
    today = PAYDAY - timedelta(days=1)

    assert _balances(owner, account, today, 2) == [250_000, 450_000]


@pytest.mark.django_db
def test_the_previous_periods_deposit_is_never_matched(checking):
    owner, account = checking
    # The last paycheck posted on time two weeks ago; this one has not posted yet.
    _paycheck(owner, start=PAYDAY - timedelta(days=14))
    _deposit(owner, account, PAYDAY - timedelta(days=14))
    today = PAYDAY - timedelta(days=1)

    # The snapshot on Oct 10 already holds the Oct 2 deposit.
    assert _balances(owner, account, today, 2) == [50_000, 250_000]


@pytest.mark.django_db
def test_one_deposit_skips_only_one_of_two_equal_paychecks(checking):
    owner, account = checking
    _paycheck(owner)
    PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.PRIVATE,
        name="Synthetic second job",
        kind=PlannedItem.Kind.INCOME,
        amount_minor=200_000,
        start_date=PAYDAY,
        cadence=PlannedItem.Cadence.BIWEEKLY,
    )
    _deposit(owner, account, date(2026, 10, 15))

    assert _balances(owner, account, date(2026, 10, 15), 2) == [250_000, 450_000]


@pytest.mark.django_db
def test_the_expected_balance_alert_sees_the_corrected_balance(checking):
    owner, account = checking
    _paycheck(owner)
    PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.PRIVATE,
        name="Synthetic rent",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=100_000,
        start_date=date(2026, 10, 17),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    _deposit(owner, account, date(2026, 10, 15))
    save_calendar_settings(owner, account_ids=[account.pk], threshold_minor=200_000)

    # 500.00 + 2,000.00 early paycheck - 1,000.00 rent = 1,500.00 on Oct 17, under 2,000.00.
    assert first_below_in_next_days(owner, today=date(2026, 10, 15)) == date(2026, 10, 17)
