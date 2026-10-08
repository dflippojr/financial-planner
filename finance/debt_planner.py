"""Debt payoff estimates. Projections are estimates, not advice."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from types import SimpleNamespace

from .months import add_months

CENTS = Decimal("1")
MONTHS_PER_YEAR = Decimal("12")
PERCENT = Decimal("100")
MAX_MONTHS = 600
# A balance past this is never going to be repaid; stop compounding it so the
# simulation stays within Decimal precision.
GROWTH_CAP_MINOR = 10**15
# Safety stop for the past-horizon label check. Each step jumps a whole stretch
# of months in which no balance changes course, so real plans need few steps.
MAX_LABEL_STEPS = 100_000
# Stand-in balance for a debt known never to pay off: it absorbs any payment
# that reaches it, so the debts behind it in order never see that money.
ABSORBING_MINOR = 10**30
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


def _monthly_rate(apr_percent):
    """The monthly rate as an exact (numerator, denominator) pair."""
    numerator, denominator = Decimal(apr_percent).as_integer_ratio()
    return numerator, denominator * 1200


def _exact_interest(balance_minor, rate):
    """monthly_interest_minor in exact integer arithmetic, for balances of any size."""
    numerator, denominator = rate
    if balance_minor <= 0 or numerator == 0:
        return 0
    interest, remainder = divmod(balance_minor * numerator, denominator)
    doubled = 2 * remainder
    if doubled > denominator or (doubled == denominator and interest % 2):
        interest += 1
    return interest


def _balance_near(halves, rate):
    """Roughly the balance whose exact monthly interest is halves / 2."""
    numerator, denominator = rate
    return halves * denominator // (2 * numerator)


def _interest_band(balance_minor, rate):
    """Lowest and highest balance that accrue the same interest (None: no upper limit)."""
    if rate[0] == 0:
        return 1, None
    interest = _exact_interest(balance_minor, rate)
    high = _balance_near(2 * interest + 1, rate)
    while _exact_interest(high + 1, rate) == interest:
        high += 1
    while _exact_interest(high, rate) > interest:
        high -= 1
    low = max(1, _balance_near(2 * interest - 1, rate))
    while low > 1 and _exact_interest(low - 1, rate) == interest:
        low -= 1
    while _exact_interest(low, rate) < interest:
        low += 1
    return low, high


def _months_until_crossing(gap, closing_speed, flips_on_tie):
    """Months before a gap that closes by closing_speed a month reverses an order."""
    if closing_speed <= 0:
        return None
    if flips_on_tie:
        return -(-gap // closing_speed)
    return gap // closing_speed + 1


class _LabelCheck:
    """Decides whether each debt still owed at the horizon ever pays off.

    It keeps applying the table's monthly rules. A debt is settled once one of
    these holds, because then no later month can change its outcome:
    - its interest is below its own minimum, which it always receives, so it shrinks to zero;
    - its interest is below this month's payment and that payment cannot fall later;
    - its interest is at least the largest payment it could ever receive, so it never shrinks;
    - its interest is at least its own minimum and a never-ending debt always sits
      ahead of it in order, absorbing any extra or freed money before it arrives.
    Between checks, a stretch of months in which every debt keeps the same
    interest and payment, and the order holds, is applied in one jump.
    """

    def __init__(self, debts, balances, strategy, custom_order, extra_minor, never_ids):
        self.debts = debts
        self.balances = dict(balances)
        self.strategy = strategy
        self.custom_order = custom_order
        self.rollover = strategy != STRATEGY_MINIMUMS
        self.extra_minor = extra_minor if self.rollover else 0
        self.rates = {debt.account_id: _monthly_rate(debt.apr_percent) for debt in debts}
        self.rank = {pk: index for index, pk in enumerate(custom_order or ())}
        self.never = set(never_ids)
        self.pays_off = set()
        self.absorbing = set()

    def run(self):
        for _ in range(MAX_LABEL_STEPS):
            self._settle()
            if not self._unsettled():
                return self.pays_off, self.never
            self._absorb()
            months = self._stretch_months()
            if months is None:
                # Nothing changes course again, so no other debt reaches zero.
                self.never.update(debt.account_id for debt in self._unsettled())
                return self.pays_off, self.never
            self._jump(months)
            self._step()
        # Not reached by any realistic plan; label what is left by its current direction.
        payments = self._payments()
        for debt in self._unsettled():
            shrinking = self._interest(debt) < payments[debt.account_id]
            (self.pays_off if shrinking else self.never).add(debt.account_id)
        return self.pays_off, self.never

    def all_never_ending(self):
        """Whether every debt still owed is already proven never to pay off."""
        self._settle()
        return all(debt.account_id in self.never for debt in self._live())

    def _live(self):
        return [debt for debt in self.debts if self.balances[debt.account_id] > 0]

    def _unsettled(self):
        settled = self.never | self.pays_off
        return [debt for debt in self._live() if debt.account_id not in settled]

    def _tracked(self):
        return [debt for debt in self._live() if debt.account_id not in self.absorbing]

    def _interest(self, debt):
        return _exact_interest(self.balances[debt.account_id], self.rates[debt.account_id])

    def _always_ahead(self, first, second):
        if self.strategy == STRATEGY_CUSTOM:
            return (self.rank.get(first.account_id, 10**9), first.account_id) < (
                self.rank.get(second.account_id, 10**9),
                second.account_id,
            )
        if self.strategy == STRATEGY_AVALANCHE:
            return first.apr_percent > second.apr_percent
        return False

    def _behind_a_never_debt(self, debt):
        return any(
            other.account_id in self.never and self._always_ahead(other, debt)
            for other in self._live()
            if other.account_id != debt.account_id
        )

    def _largest_payment(self, debt):
        """The most this debt can ever receive in a month from now on.

        Beyond its own minimum, money only comes from the extra amount and the
        minimums of debts that are paid off or may yet be.
        """
        if not self.rollover:
            return debt.minimum_payment_minor
        return self.extra_minor + sum(
            other.minimum_payment_minor
            for other in self.debts
            if other.account_id == debt.account_id
            or self.balances[other.account_id] <= 0
            or other.account_id not in self.never
        )

    def _payment_cannot_fall(self, debt, ordered):
        """Whether this debt's payment stays at least this month's until it is paid off."""
        others = [other for other in self._live() if other.account_id != debt.account_id]
        if self.strategy == STRATEGY_CUSTOM:
            # A fixed order: extra and freed money only ever move down the line.
            return True
        if self.strategy == STRATEGY_AVALANCHE:
            return all(other.apr_percent != debt.apr_percent for other in others)
        # Snowball: first in line and shrinking, while no other balance can fall to meet it.
        return ordered[0].account_id == debt.account_id and all(
            self._interest(other) >= other.minimum_payment_minor for other in others
        )

    def _settle(self):
        changed = True
        while changed:
            changed = False
            ordered, payments = self._month_plan()
            for debt in self._unsettled():
                interest = self._interest(debt)
                ceiling = self._largest_payment(debt)
                if interest < debt.minimum_payment_minor or (
                    interest < payments[debt.account_id] and self._payment_cannot_fall(debt, ordered)
                ):
                    self.pays_off.add(debt.account_id)
                elif interest >= ceiling or self._behind_a_never_debt(debt):
                    self.never.add(debt.account_id)
                else:
                    continue
                changed = True

    def _highest_reach(self, debt):
        """The largest balance, after interest, that a debt not known to be never-ending can reach."""
        balance = self.balances[debt.account_id]
        rate = self.rates[debt.account_id]
        owed = balance + self._interest(debt)
        if debt.account_id in self.pays_off or rate[0] == 0:
            return owed
        # Its payment ceiling only falls, and it is never-ending once interest reaches it.
        limit = self._largest_payment(debt)
        high = _balance_near(2 * limit - 1, rate)
        while _exact_interest(high + 1, rate) < limit:
            high += 1
        while _exact_interest(high, rate) >= limit:
            high -= 1
        return max(owed, high + limit)

    def _absorb(self):
        """Stop tracking a never-ending debt's balance once its place in order is fixed.

        A never-ending debt's balance never falls, so once it is above anything the
        debts that may still pay off can reach, it stays behind them in order.
        """
        live = self._live()
        for debt in live:
            if debt.account_id not in self.never or debt.account_id in self.absorbing:
                continue
            if self.strategy == STRATEGY_SNOWBALL:
                rivals = live
            elif self.strategy == STRATEGY_AVALANCHE:
                rivals = [other for other in live if other.apr_percent == debt.apr_percent]
            else:
                rivals = []
            owed = self.balances[debt.account_id] + self._interest(debt)
            if all(owed > self._highest_reach(other) for other in rivals if other.account_id not in self.never):
                self.absorbing.add(debt.account_id)

    def _month_plan(self):
        """Next month's order and payments under the table's rules."""
        after = dict(self.balances)
        for debt in self._live():
            if debt.account_id in self.absorbing:
                after[debt.account_id] = ABSORBING_MINOR
            else:
                after[debt.account_id] += self._interest(debt)
        ordered = _strategy_order(self._live(), after, self.strategy, self.custom_order)
        freed_minor = 0
        if self.rollover:
            freed_minor = sum(debt.minimum_payment_minor for debt in self.debts if self.balances[debt.account_id] <= 0)
        payments = _allocate_payments(ordered, after, self.extra_minor + freed_minor, rollover=self.rollover)
        return ordered, payments

    def _payments(self):
        return self._month_plan()[1]

    def _stretch_months(self):
        """Months from now in which every interest, payment and position stays the same."""
        payments = self._payments()
        limits = []
        moves = {}
        for debt in self._tracked():
            balance = self.balances[debt.account_id]
            interest = self._interest(debt)
            change = interest - payments[debt.account_id]
            moves[debt.account_id] = (balance + interest, change)
            low, high = _interest_band(balance, self.rates[debt.account_id])
            if change < 0:
                # Stay in the interest band, and stay owed after each month's payment.
                limits.append(min((balance - low) // -change, (balance - 1) // -change - 1) + 1)
            elif change > 0 and high is not None:
                limits.append((high - balance) // change + 1)
        if self.strategy in (STRATEGY_SNOWBALL, STRATEGY_AVALANCHE):
            limits.extend(limit for limit in self._order_limits(moves) if limit is not None)
        return max(0, min(limits)) if limits else None

    def _order_limits(self, moves):
        tracked = self._tracked()
        owed = {account_id: amount for account_id, (amount, _) in moves.items()}
        ordered = _strategy_order(tracked, owed, self.strategy, self.custom_order)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                if self.strategy == STRATEGY_AVALANCHE and first.apr_percent != second.apr_percent:
                    continue
                first_owed, first_change = moves[first.account_id]
                second_owed, second_change = moves[second.account_id]
                # Equal balances keep _strategy_order's tie-breaks.
                if self.strategy == STRATEGY_SNOWBALL:
                    second_wins_tie = (second.apr_percent, second.account_id) < (first.apr_percent, first.account_id)
                else:
                    second_wins_tie = second.account_id < first.account_id
                yield _months_until_crossing(second_owed - first_owed, first_change - second_change, second_wins_tie)

    def _jump(self, months):
        if months <= 0:
            return
        payments = self._payments()
        for debt in self._tracked():
            self.balances[debt.account_id] += months * (self._interest(debt) - payments[debt.account_id])

    def _step(self):
        payments = self._payments()
        for debt in self._tracked():
            self.balances[debt.account_id] += self._interest(debt) - payments[debt.account_id]
            if self.balances[debt.account_id] <= 0:
                self.pays_off.add(debt.account_id)


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
                payoff_months[debt.account_id] = add_months(start, offset)
            if balances[debt.account_id] > GROWTH_CAP_MINOR:
                frozen.add(debt.account_id)
        remaining_total = sum(max(0, amount) for amount in balances.values())
        month_date = add_months(start, offset)
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
        # Nothing shrank this month. The table stops once nothing still owed can
        # ever shrink again; until then a later month may still free up money.
        if (
            remaining_total > 0
            and _month_stalled(before, balances)
            and _LabelCheck(debts, balances, strategy, custom_order, extra_minor, frozen).all_never_ending()
        ):
            break
    balances_at_horizon = dict(balances)
    unpaid = {debt.account_id for debt in debts if balances[debt.account_id] > 0}
    beyond_ids = set()
    never_ids = set()
    if unpaid:
        pays_off, never = _LabelCheck(debts, balances, strategy, custom_order, extra_minor, frozen).run()
        beyond_ids = pays_off & unpaid
        never_ids = never & unpaid

    summaries = []
    for debt in debts:
        remaining = balances_at_horizon[debt.account_id]
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
