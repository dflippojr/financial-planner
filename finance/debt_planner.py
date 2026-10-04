"""Debt payoff estimates. Projections are estimates, not advice."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from types import SimpleNamespace

CENTS = Decimal("1")
MONTHS_PER_YEAR = Decimal("12")
PERCENT = Decimal("100")
MAX_MONTHS = 600
NEVER_PAYS_OFF = "never pays off"
NEEDS_DETAILS = "needs details"
STRATEGY_MINIMUMS = "minimums"
STRATEGY_SNOWBALL = "snowball"
STRATEGY_AVALANCHE = "avalanche"
STRATEGY_CUSTOM = "custom"
STRATEGIES = (
    STRATEGY_MINIMUMS,
    STRATEGY_SNOWBALL,
    STRATEGY_AVALANCHE,
    STRATEGY_CUSTOM,
)


@dataclass(frozen=True)
class DebtInput:
    account_id: int
    name: str
    balance_minor: int
    apr_percent: Decimal
    minimum_payment_minor: int


def monthly_interest_minor(balance_minor, apr_percent):
    """Interest for one month in integer minor units, rounded half-even."""
    if balance_minor <= 0:
        return 0
    raw = Decimal(balance_minor) * Decimal(apr_percent) / MONTHS_PER_YEAR / PERCENT
    return int(raw.quantize(CENTS, rounding=ROUND_HALF_EVEN))


def add_calendar_months(start, months):
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def month_label(value):
    return f"{value.year:04d}-{value.month:02d}"


def _strategy_order(debts, balances, strategy, custom_order):
    remaining = [debt for debt in debts if balances[debt.account_id] > 0]
    if strategy == STRATEGY_SNOWBALL:
        remaining.sort(key=lambda debt: (balances[debt.account_id], debt.apr_percent, debt.account_id))
    elif strategy == STRATEGY_AVALANCHE:
        remaining.sort(key=lambda debt: (-debt.apr_percent, balances[debt.account_id], debt.account_id))
    elif strategy == STRATEGY_CUSTOM:
        rank = {pk: index for index, pk in enumerate(custom_order or ())}
        remaining.sort(key=lambda debt: (rank.get(debt.account_id, 10**9), debt.account_id))
    else:
        remaining.sort(key=lambda debt: debt.account_id)
    return remaining


def _allocate_payments(debts_in_order, balances, extra_minor):
    payments = {debt.account_id: 0 for debt in debts_in_order}
    pool = extra_minor
    for debt in debts_in_order:
        due = min(debt.minimum_payment_minor, balances[debt.account_id])
        payments[debt.account_id] = due
        unused = debt.minimum_payment_minor - due
        if unused > 0:
            pool += unused
    for debt in debts_in_order:
        leftover = balances[debt.account_id] - payments[debt.account_id]
        if leftover <= 0:
            continue
        applied = min(pool, leftover)
        payments[debt.account_id] += applied
        pool -= applied
        if pool == 0:
            break
    return payments


def _month_stalled(before, after):
    return all(after[account_id] >= before[account_id] for account_id in before)


def simulate_payoff(debts, extra_minor=0, strategy=STRATEGY_MINIMUMS, custom_order=None, start=None):
    """Amortize included debts. Extra is applied after each debt's minimum."""
    start = start or date.today().replace(day=1)
    extra_minor = extra_minor or 0
    if strategy == STRATEGY_MINIMUMS:
        extra_minor = 0
    balances = {debt.account_id: debt.balance_minor for debt in debts}
    payoff_months = {}
    total_interest = 0
    months = []
    never = False
    for offset in range(MAX_MONTHS):
        if all(balance <= 0 for balance in balances.values()):
            break
        before = dict(balances)
        interest_by_id = {}
        for debt in debts:
            if balances[debt.account_id] <= 0:
                interest_by_id[debt.account_id] = 0
                continue
            interest = monthly_interest_minor(balances[debt.account_id], debt.apr_percent)
            interest_by_id[debt.account_id] = interest
            balances[debt.account_id] += interest
            total_interest += interest
        ordered = _strategy_order(debts, balances, strategy, custom_order)
        payments = _allocate_payments(ordered, balances, extra_minor)
        paid_total = 0
        for debt in debts:
            paid = payments.get(debt.account_id, 0)
            balances[debt.account_id] -= paid
            paid_total += paid
            if balances[debt.account_id] <= 0 and debt.account_id not in payoff_months:
                payoff_months[debt.account_id] = add_calendar_months(start, offset)
        month_date = add_calendar_months(start, offset)
        remaining_total = sum(max(0, amount) for amount in balances.values())
        months.append(
            SimpleNamespace(
                month=month_date,
                label=month_label(month_date),
                interest_minor=sum(interest_by_id.values()),
                paid_minor=paid_total,
                remaining_minor=remaining_total,
                remaining_by_id=dict(balances),
                interest_by_id=interest_by_id,
                paid_by_id=payments,
            )
        )
        if remaining_total > 0 and _month_stalled(before, balances):
            never = True
            break
    else:
        if any(balance > 0 for balance in balances.values()):
            never = True

    summaries = []
    for debt in debts:
        remaining = balances[debt.account_id]
        if never and remaining > 0:
            status = NEVER_PAYS_OFF
            payoff = None
        else:
            status = ""
            payoff = payoff_months.get(debt.account_id)
        summaries.append(
            SimpleNamespace(
                account_id=debt.account_id,
                name=debt.name,
                payoff_month=payoff,
                payoff_label=month_label(payoff) if payoff else status or NEVER_PAYS_OFF,
                remaining_minor=remaining,
            )
        )
    return SimpleNamespace(
        months=months,
        debts=summaries,
        total_interest_minor=total_interest,
        never_pays_off=never,
        extra_minor=extra_minor,
        strategy=strategy,
    )


def compare_to_minimums(debts, extra_minor, strategy, custom_order=None, start=None):
    chosen = simulate_payoff(
        debts,
        extra_minor=extra_minor,
        strategy=strategy,
        custom_order=custom_order,
        start=start,
    )
    baseline = simulate_payoff(debts, extra_minor=0, strategy=STRATEGY_MINIMUMS, start=start)
    if chosen.never_pays_off or baseline.never_pays_off:
        interest_saved = None
    else:
        interest_saved = baseline.total_interest_minor - chosen.total_interest_minor
    return SimpleNamespace(chosen=chosen, baseline=baseline, interest_saved_minor=interest_saved)
