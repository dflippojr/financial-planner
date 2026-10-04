"""Debt payoff estimates. Projections are estimates, not advice."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from types import SimpleNamespace

CENTS = Decimal("1")
MONTHS_PER_YEAR = Decimal("12")
PERCENT = Decimal("100")
MAX_MONTHS = 600
# A balance past this is never going to be repaid; stop compounding it so the
# simulation stays within Decimal precision.
GROWTH_CAP_MINOR = 10**15
STALL_WINDOW_MONTHS = 12
NEVER_PAYS_OFF = "never pays off"
BEYOND_LIMIT = "more than 50 years"
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


def _allocate_payments(debts_in_order, balances, extra_minor, *, rollover=True):
    payments = {debt.account_id: 0 for debt in debts_in_order}
    pool = extra_minor
    for debt in debts_in_order:
        due = min(debt.minimum_payment_minor, balances[debt.account_id])
        payments[debt.account_id] = due
        unused = debt.minimum_payment_minor - due
        # Minimums only pays each debt its own minimum; nothing moves between debts.
        if unused > 0 and rollover:
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


def _unpaid_at_horizon(debts, balances, months, frozen, *, stalled, strategy):
    """Split debts still owed into "never pays off" and "past the horizon".

    A debt never pays off when it hit the growth cap or when every balance stopped
    falling. Otherwise, over the last year: with minimums only, each debt is judged
    on its own balance, since nothing moves between debts. With a rollover strategy
    a debt can sit unchanged while it waits for freed payments, so the remaining
    debts are still being paid down as long as the total owed is falling.
    """
    never_ids = set()
    beyond_ids = set()
    owed = [debt for debt in debts if balances[debt.account_id] > 0]
    year_ago = months[-(STALL_WINDOW_MONTHS + 1)].remaining_by_id if len(months) > STALL_WINDOW_MONTHS else None

    def falling(ids):
        if year_ago is None:
            return True
        now_total = sum(balances[account_id] for account_id in ids)
        then_total = sum(max(0, year_ago[account_id]) for account_id in ids)
        return now_total < then_total

    if strategy != STRATEGY_MINIMUMS:
        active = [debt.account_id for debt in owed if debt.account_id not in frozen]
        total_falling = falling(active)
    for debt in owed:
        account_id = debt.account_id
        if stalled or account_id in frozen:
            never_ids.add(account_id)
        elif strategy == STRATEGY_MINIMUMS:
            (beyond_ids if falling([account_id]) else never_ids).add(account_id)
        else:
            (beyond_ids if total_falling else never_ids).add(account_id)
    return never_ids, beyond_ids


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
    frozen = set()
    for offset in range(MAX_MONTHS):
        if all(balance <= 0 for balance in balances.values()):
            break
        before = dict(balances)
        interest_by_id = {}
        for debt in debts:
            if balances[debt.account_id] <= 0 or debt.account_id in frozen:
                interest_by_id[debt.account_id] = 0
                continue
            interest = monthly_interest_minor(balances[debt.account_id], debt.apr_percent)
            interest_by_id[debt.account_id] = interest
            balances[debt.account_id] += interest
            total_interest += interest
        ordered = [
            debt for debt in _strategy_order(debts, balances, strategy, custom_order) if debt.account_id not in frozen
        ]
        # Snowball, avalanche, and custom keep the monthly total constant: a
        # paid-off debt's minimum rolls into the next debt in order.
        freed_minor = 0
        if strategy != STRATEGY_MINIMUMS:
            freed_minor = sum(debt.minimum_payment_minor for debt in debts if before[debt.account_id] <= 0)
        payments = _allocate_payments(
            ordered,
            balances,
            extra_minor + freed_minor,
            rollover=strategy != STRATEGY_MINIMUMS,
        )
        paid_total = 0
        for debt in debts:
            paid = payments.get(debt.account_id, 0)
            balances[debt.account_id] -= paid
            paid_total += paid
            if balances[debt.account_id] <= 0 and debt.account_id not in payoff_months:
                payoff_months[debt.account_id] = add_calendar_months(start, offset)
            if balances[debt.account_id] > GROWTH_CAP_MINOR:
                frozen.add(debt.account_id)
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
    never_ids, beyond_ids = _unpaid_at_horizon(debts, balances, months, frozen, stalled=never, strategy=strategy)

    summaries = []
    for debt in debts:
        remaining = balances[debt.account_id]
        if debt.account_id in never_ids:
            status = NEVER_PAYS_OFF
            payoff = None
        elif debt.account_id in beyond_ids:
            status = BEYOND_LIMIT
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
        never_pays_off=bool(never_ids),
        beyond_limit=bool(beyond_ids),
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
    # Interest past the horizon is unknown, so a truncated plan can't be compared.
    if chosen.never_pays_off or baseline.never_pays_off or chosen.beyond_limit or baseline.beyond_limit:
        interest_saved = None
    else:
        interest_saved = baseline.total_interest_minor - chosen.total_interest_minor
    return SimpleNamespace(chosen=chosen, baseline=baseline, interest_saved_minor=interest_saved)
