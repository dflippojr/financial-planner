"""Past month-end balances calculated from transactions (#636).

A newly linked account has balances only from its link date on, so years of
imported transactions are missing from net worth. This works backwards from
the account's earliest SimpleFIN or manual balance (the anchor):

    balance(M) = balance(anchor) - sum(active transactions dated after M through the anchor date)

for each calendar month-end M before the anchor date, back to the month of
the account's first active transaction. Transactions of every kind count, as in
reconciliation; a split counts once, at its parent amount, because the split
parts live in their own table. Balances are in transaction sign (money owed is
negative), which is also how calculated rows are stored: the SimpleFIN
protocol sign.

A month-end is flagged incomplete when any day after it through the anchor
date has no active, non-manual import batch (reconciliation's coverage rule):
its balance depends on every transaction in that window, so one gap flags
every earlier month-end too.

Nothing is written until save_calculated_balances(), which recalculates under
the account lock and replaces only that account's calculated rows. After
that, refresh_calculated_balances() keeps the saved rows equal to calculate()
whenever an import, edit or deletion changes a transaction or a real balance
(owner decision for #641 and #676), and deletes them when calculate() no
longer can, for example once no real balance remains. Amounts
never go to logs or error messages.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Exists, Min, OuterRef

from .audit_services import record
from .lifecycle_services import _person_for
from .models import Account, AuditEvent, BalanceSnapshot, Transaction
from .months import add_months, month_end
from .net_worth import signed_balance_minor, source_rank_expression
from .reconciliation import RECONCILED_TYPES, _coverage, _covered, _daily_totals
from .snapshot_services import _lock_editable_account, _record_snapshot

# The balance of these accounts is the sum of their transactions. Investment
# values move with the market; loans and physical assets hold no transactions.
CALCULABLE_TYPES = RECONCILED_TYPES

NOT_ELIGIBLE = "Past balances can be calculated only for checking, savings and credit card accounts."
NO_TRANSACTIONS = "This account has no active transactions to calculate from."
NO_ANCHOR = "Record a balance first: past balances are calculated back from the earliest SimpleFIN or manual balance."
CONVERTED = (
    "This account's balances were converted from another currency, so past balances "
    "cannot be calculated exactly from its transactions."
)


class CalculationError(Exception):
    """Safe, non-leaking reason why past balances cannot be calculated."""


@dataclass(frozen=True)
class MonthEndBalance:
    month_end: date
    balance_minor: int  # transaction sign: money owed is negative
    incomplete: bool


@dataclass(frozen=True)
class Calculation:
    anchor_date: date
    anchor_source: str
    first_transaction_date: date
    rows: tuple[MonthEndBalance, ...]  # oldest first

    @property
    def incomplete_count(self):
        return sum(1 for row in self.rows if row.incomplete)


def is_calculable(account):
    return account.account_type in CALCULABLE_TYPES


def _anchor(account):
    """The earliest SimpleFIN or manual snapshot; on one date, the one net worth would pick."""
    return (
        BalanceSnapshot.objects.filter(account=account)
        .exclude(source=BalanceSnapshot.Source.CALCULATED)
        .annotate(_source_rank=source_rank_expression())
        .order_by("snapshot_date", "-_source_rank", "-pk")
        .first()
    )


def _month_ends(first_day, before):
    """Calendar month-ends from first_day's month through the last one before `before`."""
    ends = []
    current = month_end(first_day)
    while current < before:
        ends.append(current)
        current = month_end(add_months(current, 1))
    return ends


def calculate(account):
    """The month-end balances to save for an account, or CalculationError. Reads only."""
    if not is_calculable(account):
        raise CalculationError(NOT_ELIGIBLE)
    first_day = (
        Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE)
        .aggregate(first=Min("transaction_date"))["first"]
    )
    if first_day is None:
        raise CalculationError(NO_TRANSACTIONS)
    # As in reconciliation: a converted balance and each converted row use the
    # ECB rate of their own date, so the sums drift with the exchange rate (#441).
    if BalanceSnapshot.objects.filter(account=account).exclude(original_currency="").exists():
        raise CalculationError(CONVERTED)
    anchor = _anchor(account)
    if anchor is None:
        raise CalculationError(NO_ANCHOR)
    anchor_minor = signed_balance_minor(account.account_type, anchor.source, anchor.amount_minor)
    totals = _daily_totals([account.pk])[account.pk]
    coverage = _coverage([account.pk])[account.pk]
    rows = tuple(
        MonthEndBalance(
            month_end=end,
            balance_minor=anchor_minor - totals.between(end, anchor.snapshot_date),
            incomplete=not _covered(coverage, end + timedelta(days=1), anchor.snapshot_date),
        )
        for end in _month_ends(first_day, anchor.snapshot_date)
    )
    return Calculation(
        anchor_date=anchor.snapshot_date,
        anchor_source=anchor.source,
        first_transaction_date=first_day,
        rows=rows,
    )


@dataclass(frozen=True)
class SaveResult:
    saved: int
    created: int
    updated: int
    deleted: int


def _locked_calculated_rows(account):
    return list(
        BalanceSnapshot.objects.select_for_update(of=("self",))
        .filter(account=account, source=BalanceSnapshot.Source.CALCULATED)
        .order_by("snapshot_date", "pk")
    )


def _replace_calculated_rows(person, account, wanted):
    """Make the account's calculated rows equal `wanted` ({month_end: balance_minor}).

    Unchanged rows are kept and record nothing, as for other no-op writes; each
    created, changed or removed row records the same balance audit event a
    manual balance does. Returns (created, updated, deleted).
    """
    wanted = dict(wanted)
    created = updated = deleted = 0
    for existing in _locked_calculated_rows(account):
        amount = wanted.pop(existing.snapshot_date, None)
        if amount is None:
            target_id = existing.pk
            existing.delete()
            record(person, AuditEvent.Action.RECORD_DELETED, AuditEvent.TargetType.BALANCE, target_id,
                   audience={"account": account})
            deleted += 1
        elif amount != existing.amount_minor:
            existing.amount_minor = amount
            existing.save(update_fields=("amount_minor",))
            _record_snapshot(person, account, existing, AuditEvent.Action.RECORD_EDITED, ["amount"])
            updated += 1
    for day, amount in sorted(wanted.items()):
        snapshot = BalanceSnapshot.objects.create(
            account=account,
            snapshot_date=day,
            amount_minor=amount,
            currency=account.currency,
            source=BalanceSnapshot.Source.CALCULATED,
        )
        _record_snapshot(person, account, snapshot, AuditEvent.Action.RECORD_CREATED)
        created += 1
    return created, updated, deleted


@transaction.atomic
def save_calculated_balances(principal, account_id):
    """Recalculate under the account lock and replace that account's calculated rows.

    SimpleFIN and manual rows are never touched: every calculated date lies
    before the earliest of them. refresh_calculated_balances keeps that true
    after later imports, edits and deletions.
    """
    account = _lock_editable_account(principal, account_id)
    calculation = calculate(account)
    wanted = {row.month_end: row.balance_minor for row in calculation.rows}
    created, updated, deleted = _replace_calculated_rows(_person_for(principal), account, wanted)
    return SaveResult(saved=len(calculation.rows), created=created, updated=updated, deleted=deleted)


def refresh_calculated_balances(person, account_id):
    """Keep saved calculated rows equal to calculate() after a ledger change (#641, #676).

    Every path that adds, edits, archives or deletes an account's transactions,
    or its SimpleFIN or manual balances, calls this in the same database
    transaction, after its own permission checks. It takes the account row
    lock (a no-op when the caller already holds it) in the same single query
    that checks for calculated rows, so an import pays one query for it.

    An account with no calculated rows is left alone: calculating is the
    member's choice. When calculate() refuses, for example because no real
    balance remains, the calculated rows are deleted, since nothing could
    produce them now. A change only after the anchor leaves calculate()
    unchanged, so nothing is written or recorded. Returns (created, updated,
    deleted).
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("refresh_calculated_balances must run inside the caller's transaction.")
    account = Account.objects.select_for_update(of=("self",)).filter(has_calculated_expression(), pk=account_id).first()
    if account is None:
        return (0, 0, 0)
    return refresh_locked_calculated_balances(person, account)


def has_calculated_expression():
    """Annotate an account query with whether the account has calculated rows (no extra query)."""
    return Exists(BalanceSnapshot.objects.filter(account=OuterRef("pk"), source=BalanceSnapshot.Source.CALCULATED))


def refresh_locked_calculated_balances(person, account):
    """refresh_calculated_balances for a caller that already holds the account lock.

    For a hot path that read has_calculated_expression() with its own lock
    query and found calculated rows, so the refresh adds no lock query.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("refresh_locked_calculated_balances must run inside the caller's transaction.")
    try:
        wanted = {row.month_end: row.balance_minor for row in calculate(account).rows}
    except CalculationError:
        wanted = {}
    return _replace_calculated_rows(person, account, wanted)


@transaction.atomic
def delete_calculated_balances(principal, account_id):
    """Delete every calculated row on the account; returns how many were deleted."""
    account = _lock_editable_account(principal, account_id)
    person = _person_for(principal)
    rows = _locked_calculated_rows(account)
    for row in rows:
        target_id = row.pk
        row.delete()
        record(person, AuditEvent.Action.RECORD_DELETED, AuditEvent.TargetType.BALANCE, target_id,
               audience={"account": account})
    return len(rows)


def calculated_count(account):
    return BalanceSnapshot.objects.filter(account=account, source=BalanceSnapshot.Source.CALCULATED).count()

