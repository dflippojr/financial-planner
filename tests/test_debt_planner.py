from datetime import date
from decimal import Decimal

from finance.debt_planner import (
    BEYOND_LIMIT,
    add_calendar_months,
    NEVER_PAYS_OFF,
    STRATEGY_AVALANCHE,
    STRATEGY_CUSTOM,
    STRATEGY_MINIMUMS,
    STRATEGY_SNOWBALL,
    DebtInput,
    compare_to_minimums,
    monthly_interest_minor,
    simulate_payoff,
)


def _debt(account_id, balance, apr, minimum, name="Synthetic debt"):
    return DebtInput(
        account_id=account_id,
        name=name,
        balance_minor=balance,
        apr_percent=Decimal(apr),
        minimum_payment_minor=minimum,
    )


def test_monthly_interest_rounds_half_even_to_the_cent():
    # 18% APR / 12 = 1.5% : 100 cents * 1.5% = 1.5 cents -> 2 (half to even).
    assert monthly_interest_minor(100, Decimal("18.000")) == 2
    # 200 cents * 1.5% = 3.0 cents, already exact.
    assert monthly_interest_minor(200, Decimal("18.000")) == 3
    # 12% APR / 12 = 1% of $100.00 is exactly $1.00.
    assert monthly_interest_minor(10_000, Decimal("12.000")) == 100


def test_hand_computed_three_month_payoff_matches_to_the_cent():
    # Balance $100.00, 12.000% APR, $50.00 monthly payment.
    # Month 1: interest 100, pay 5000, remaining 5100
    # Month 2: interest 51, pay 5000, remaining 151
    # Month 3: interest 2 (1.51 half-even to 2), pay 153, remaining 0
    start = date(2026, 10, 1)
    plan = simulate_payoff(
        [_debt(1, 10_000, "12.000", 5_000, "Synthetic card")],
        extra_minor=0,
        strategy=STRATEGY_MINIMUMS,
        start=start,
    )
    assert not plan.never_pays_off
    assert plan.total_interest_minor == 153
    assert [row.remaining_minor for row in plan.months] == [5100, 151, 0]
    assert [row.interest_minor for row in plan.months] == [100, 51, 2]
    assert plan.debts[0].payoff_label == "2026-12"


def test_payment_below_interest_is_never_pays_off():
    # $100.00 at 12% accrues $1.00 interest; a $0.50 payment never catches up.
    plan = simulate_payoff(
        [_debt(1, 10_000, "12.000", 50)],
        extra_minor=0,
        strategy=STRATEGY_MINIMUMS,
        start=date(2026, 1, 1),
    )
    assert plan.never_pays_off
    assert plan.debts[0].payoff_label == NEVER_PAYS_OFF
    assert len(plan.months) == 1


def test_snowball_pays_smallest_balance_first():
    small = _debt(1, 20_000, "10.000", 500, "Small")
    large = _debt(2, 50_000, "20.000", 1_000, "Large")
    extra = 2_000
    start = date(2026, 1, 1)
    snowball = simulate_payoff(
        [small, large], extra_minor=extra, strategy=STRATEGY_SNOWBALL, start=start
    )
    avalanche = simulate_payoff(
        [small, large], extra_minor=extra, strategy=STRATEGY_AVALANCHE, start=start
    )
    snowball_small = next(row for row in snowball.debts if row.account_id == 1)
    snowball_large = next(row for row in snowball.debts if row.account_id == 2)
    avalanche_small = next(row for row in avalanche.debts if row.account_id == 1)
    avalanche_large = next(row for row in avalanche.debts if row.account_id == 2)
    assert snowball_small.payoff_month < snowball_large.payoff_month
    assert avalanche_large.payoff_month < avalanche_small.payoff_month
    assert snowball_small.payoff_month < avalanche_small.payoff_month
    assert avalanche_large.payoff_month < snowball_large.payoff_month


def test_custom_order_follows_the_given_sequence():
    first = _debt(1, 40_000, "8.000", 1_000, "First")
    second = _debt(2, 10_000, "22.000", 500, "Second")
    start = date(2026, 1, 1)
    custom = simulate_payoff(
        [first, second],
        extra_minor=3_000,
        strategy=STRATEGY_CUSTOM,
        custom_order=(1, 2),
        start=start,
    )
    snowball = simulate_payoff(
        [first, second], extra_minor=3_000, strategy=STRATEGY_SNOWBALL, start=start
    )
    custom_first = next(row for row in custom.debts if row.account_id == 1)
    custom_second = next(row for row in custom.debts if row.account_id == 2)
    snowball_first = next(row for row in snowball.debts if row.account_id == 1)
    assert custom_first.payoff_month < custom_second.payoff_month
    assert custom_first.payoff_month <= snowball_first.payoff_month


def test_compare_to_minimums_reports_interest_saved():
    debt = _debt(1, 10_000, "12.000", 1_000)
    start = date(2026, 10, 1)
    comparison = compare_to_minimums(
        [debt], extra_minor=4_000, strategy=STRATEGY_SNOWBALL, start=start
    )
    assert comparison.interest_saved_minor is not None
    assert comparison.interest_saved_minor > 0
    assert comparison.chosen.total_interest_minor < comparison.baseline.total_interest_minor
    assert comparison.baseline.strategy == STRATEGY_MINIMUMS


def test_snowball_rolls_a_paid_off_minimum_into_the_next_debt():
    start = date(2026, 1, 1)
    small = _debt(1, 10_000, "0", 10_000, name="Synthetic small")
    large = _debt(2, 30_000, "0", 1_000, name="Synthetic large")

    plan = simulate_payoff([small, large], extra_minor=1_000, strategy=STRATEGY_SNOWBALL, start=start)

    by_id = {row.account_id: row for row in plan.debts}
    assert by_id[1].payoff_month == start
    # 30,000 - 2,000 in month one, then 12,000 a month with the freed 10,000 minimum.
    assert by_id[2].payoff_month == add_calendar_months(start, 3)
    assert [month.paid_minor for month in plan.months] == [12_000, 12_000, 12_000, 4_000]


def test_minimums_only_does_not_roll_freed_minimums():
    start = date(2026, 1, 1)
    small = _debt(1, 10_000, "0", 10_000)
    large = _debt(2, 3_000, "0", 1_000)

    plan = simulate_payoff([small, large], strategy=STRATEGY_MINIMUMS, start=start)

    assert [month.paid_minor for month in plan.months] == [11_000, 1_000, 1_000]


def test_a_payoff_past_the_horizon_is_not_reported_as_never():
    plan = simulate_payoff([_debt(1, 100_000, "0", 100)], strategy=STRATEGY_MINIMUMS, start=date(2026, 1, 1))

    assert plan.never_pays_off is False
    assert plan.beyond_limit is True
    assert plan.debts[0].payoff_label == BEYOND_LIMIT


def test_minimums_only_keeps_an_unused_minimum_with_its_own_debt():
    tiny = _debt(1, 100, "0", 10_000)
    other = _debt(2, 30_000, "0", 1_000)

    plan = simulate_payoff([tiny, other], strategy=STRATEGY_MINIMUMS, start=date(2026, 1, 1))

    assert plan.months[0].paid_by_id == {1: 100, 2: 1_000}


def test_interest_savings_are_not_compared_past_the_horizon():
    slow = _debt(1, 100_000, "12", 1_001)

    comparison = compare_to_minimums([slow], extra_minor=10_000, strategy=STRATEGY_SNOWBALL, start=date(2026, 1, 1))

    assert comparison.baseline.beyond_limit is True
    assert comparison.interest_saved_minor is None


def test_a_growing_debt_is_never_while_a_slow_one_is_past_the_horizon():
    growing = _debt(1, 10_000, "99", 0)
    slow = _debt(2, 100_000, "0", 100)

    plan = simulate_payoff([growing, slow], strategy=STRATEGY_MINIMUMS, start=date(2026, 1, 1))

    labels = {row.account_id: row.payoff_label for row in plan.debts}
    assert labels == {1: NEVER_PAYS_OFF, 2: BEYOND_LIMIT}
    assert plan.never_pays_off is True
    assert plan.beyond_limit is True


def test_runaway_interest_stops_at_the_growth_cap_instead_of_crashing():
    runaway = _debt(1, 10_000, "200", 0)
    slow = _debt(2, 100_000, "0", 100)

    plan = simulate_payoff([runaway, slow], strategy=STRATEGY_MINIMUMS, start=date(2026, 1, 1))

    assert {row.account_id: row.payoff_label for row in plan.debts}[1] == NEVER_PAYS_OFF


def test_a_debt_waiting_for_rolled_payments_is_past_the_horizon_not_never():
    first = _debt(1, 60_100, "0", 100)
    waiting = _debt(2, 100, "0", 0)

    plan = simulate_payoff([first, waiting], strategy=STRATEGY_CUSTOM, custom_order=[1, 2], start=date(2026, 1, 1))

    assert {row.account_id: row.payoff_label for row in plan.debts} == {1: BEYOND_LIMIT, 2: BEYOND_LIMIT}
    assert plan.never_pays_off is False
