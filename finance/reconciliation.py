"""Reconcile stored transactions against bank-reported balances (issue #535).

The institution's balance is a number this code did not produce. For each pair
of consecutive balance snapshots (d1, d2] on a cash account, the change in
balance must equal the sum of active transactions dated in that window.

Window states:

- ``ok``: the change matches the transactions exactly.
- ``ok-late``: it differs, but the difference is undone within the next
  LATE_WINDOWS window(s), which is pending or posting-date skew.
- ``mismatch``: it differs and the difference is not undone in time.
- ``insufficient``: some day in the window has no active import batch, so the
  transactions cannot be complete.

``reconcile()`` and the ``reconcile_balances`` command only read. The daily
pass step ``evaluate_reconciliation_alerts()`` writes operator alerts, their
alerted-window state and the run time. Callers decide who may see which
account; amounts and account names never go to logs or the ``--json`` output.
"""

import hashlib
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from .cash_flow import format_minor
from .models import Account, AccountLink, Alert, AlertSettings, BalanceSnapshot, ImportBatch, ReconciliationRun, Transaction
from .net_worth import signed_balance_minor

OK = "ok"
OK_LATE = "ok-late"
MISMATCH = "mismatch"
INSUFFICIENT = "insufficient"
STATES = (OK, OK_LATE, MISMATCH, INSUFFICIENT)

# How many following windows may undo a difference before it counts as a mismatch.
LATE_WINDOWS = 1

# Only cash accounts: their balance is the sum of their transactions. Investment
# values move with the market, and physical assets and loans hold no transactions.
RECONCILED_TYPES = (Account.Type.CHECKING, Account.Type.SAVINGS, Account.Type.CREDIT_CARD)

# Settings → Data lists at most this many mismatched windows, newest first.
MAX_LISTED_MISMATCHES = 50


@dataclass(frozen=True)
class Window:
    account_id: int
    start: date  # d1, exclusive
    end: date  # d2, inclusive
    state: str
    # Balance change minus the transactions, in transaction sign. Set only for a mismatch.
    difference_minor: int | None = None


def eligible_accounts(accounts=None):
    """Active cash accounts with a transactions-mode link or at least two snapshots."""
    accounts = Account.objects.all() if accounts is None else accounts
    linked = AccountLink.objects.filter(account_id=OuterRef("pk"), mode=AccountLink.Mode.TRANSACTIONS)
    snapshot_days = (
        BalanceSnapshot.objects.filter(account_id=OuterRef("pk"))
        .order_by()
        .values("account_id")
        .annotate(days=Count("snapshot_date", distinct=True))
        .values("days")
    )
    return (
        accounts.filter(status=Account.Status.ACTIVE, archived_at__isnull=True, account_type__in=RECONCILED_TYPES)
        .alias(_linked=Exists(linked), _snapshot_days=Coalesce(Subquery(snapshot_days), 0))
        .filter(Q(_linked=True) | Q(_snapshot_days__gte=2))
        .order_by("pk")
    )


def _balances(accounts):
    """{account id: [(date, balance in transaction sign)]}, oldest first, SimpleFIN over manual.

    Transaction sign is net worth's signed balance: money owed is negative,
    whether a card balance came from SimpleFIN or was typed as the amount owed.
    """
    types = {account.pk: account.account_type for account in accounts}
    chosen = {}
    rows = BalanceSnapshot.objects.filter(account_id__in=types).values_list(
        "account_id", "snapshot_date", "source", "amount_minor"
    )
    for account_id, day, source, amount in rows:
        key = (account_id, day)
        if key in chosen and chosen[key][0] == BalanceSnapshot.Source.SIMPLEFIN:
            continue
        chosen[key] = (source, signed_balance_minor(types[account_id], source, amount))
    balances = {account_id: [] for account_id in types}
    for (account_id, day), (_source, amount) in sorted(chosen.items()):
        balances[account_id].append((day, amount))
    return balances


class _RunningTotals:
    """Cumulative transaction totals by date, so any window sums in O(log n)."""

    def __init__(self):
        self.days = []
        self.cumulative = []

    def add(self, day, total):
        self.days.append(day)
        self.cumulative.append((self.cumulative[-1] if self.cumulative else 0) + total)

    def through(self, day):
        index = bisect_right(self.days, day)
        return self.cumulative[index - 1] if index else 0

    def between(self, start, end):
        return self.through(end) - self.through(start)


def _daily_totals(account_ids):
    totals = {account_id: _RunningTotals() for account_id in account_ids}
    rows = (
        Transaction.objects.filter(account_id__in=account_ids, status=Transaction.Status.ACTIVE)
        .values_list("account_id", "transaction_date")
        .annotate(total=Sum("amount_minor"))
        .order_by("account_id", "transaction_date")
    )
    for account_id, day, total in rows:
        totals[account_id].add(day, total)
    return totals


def _coverage(account_ids):
    """{account id: merged [(start, end)]} of import batches that vouch for every row in their range.

    A manual entry covers one row, not a whole statement period. A SimpleFIN
    sync batch (one with a remote account id) proves its days were synced only
    while that remote account is linked in transactions mode or the batch holds
    a row: a balances-only link also writes empty batches through today, and a
    batch does not record its link's mode (as in import_due.py).
    """
    ranges = {account_id: [] for account_id in account_ids}
    transactions_link = AccountLink.objects.filter(
        account_id=OuterRef("account_id"),
        simplefin_account_id=OuterRef("simplefin_account_id"),
        mode=AccountLink.Mode.TRANSACTIONS,
    )
    rows = (
        ImportBatch.objects.filter(account_id__in=account_ids, status=ImportBatch.Status.ACTIVE)
        .exclude(source=ImportBatch.Source.MANUAL)
        .alias(
            _linked=Exists(transactions_link),
            _has_rows=Exists(Transaction.objects.filter(import_batch_id=OuterRef("pk"))),
        )
        .exclude(source=ImportBatch.Source.SIMPLEFIN, simplefin_account_id__gt="", _linked=False, _has_rows=False)
        .order_by("account_id", "date_range_start")
        .values_list("account_id", "date_range_start", "date_range_end")
    )
    for account_id, start, end in rows:
        merged = ranges[account_id]
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return ranges


def _covered(ranges, first, last):
    return any(start <= first and last <= end for start, end in ranges)


def _classify(account_id, raw):
    """raw is [(start, end, difference or None when insufficient)], oldest first."""
    windows = []
    index = 0
    while index < len(raw):
        start, end, difference = raw[index]
        if difference is None:
            windows.append(Window(account_id, start, end, INSUFFICIENT))
            index += 1
            continue
        if difference == 0:
            windows.append(Window(account_id, start, end, OK))
            index += 1
            continue
        resolved_at = None
        running = difference
        for ahead in range(index + 1, min(index + 1 + LATE_WINDOWS, len(raw))):
            if raw[ahead][2] is None:
                break
            running += raw[ahead][2]
            if running == 0:
                resolved_at = ahead
                break
        if resolved_at is None:
            windows.append(Window(account_id, start, end, MISMATCH, difference))
            index += 1
            continue
        for later_start, later_end, later_difference in raw[index:resolved_at + 1]:
            state = OK if later_difference == 0 else OK_LATE
            windows.append(Window(account_id, later_start, later_end, state))
        index = resolved_at + 1
    return windows


def reconcile(accounts, *, since=None):
    """{account id: [Window]} for the given eligible accounts, oldest window first.

    since keeps windows that end on or after that date; earlier windows still
    count when deciding whether a later difference was skew.
    """
    accounts = list(accounts)
    account_ids = [account.pk for account in accounts]
    balances = _balances(accounts)
    totals = _daily_totals(account_ids)
    coverage = _coverage(account_ids)
    results = {}
    for account_id in account_ids:
        raw = []
        points = balances[account_id]
        for (start, opening), (end, closing) in pairwise(points):
            if not _covered(coverage[account_id], start + timedelta(days=1), end):
                raw.append((start, end, None))
                continue
            raw.append((start, end, (closing - opening) - totals[account_id].between(start, end)))
        windows = _classify(account_id, raw)
        if since is not None:
            windows = [window for window in windows if window.end >= since]
        results[account_id] = windows
    return results


def state_counts(results):
    counts = dict.fromkeys(STATES, 0)
    for windows in results.values():
        for window in windows:
            counts[window.state] += 1
    return counts


def machine_summary(results):
    """Counts and window states only: no amounts and no account names."""
    return {
        "version": 1,
        "accounts": len(results),
        "counts": state_counts(results),
        "windows": [
            {"account": account_id, "start": window.start.isoformat(), "end": window.end.isoformat(),
             "state": window.state}
            for account_id, windows in results.items()
            for window in windows
        ],
    }


def visible_results(principal, results):
    visible = set(Account.objects.visible_to(principal).filter(pk__in=list(results)).values_list("pk", flat=True))  # pyright: ignore[reportAttributeAccessIssue] - AccountQuerySet.as_manager() is untyped, as for every Account.objects.visible_to in the baseline
    return {account_id: windows for account_id, windows in results.items() if account_id in visible}


def mismatched_windows(results):
    return [window for windows in results.values() for window in windows if window.state == MISMATCH]


def _window_key(window):
    return f"{window.account_id}:{window.start.isoformat()}:{window.end.isoformat()}"


def reconciliation_alert_title(mismatches):
    accounts = {window.account_id for window in mismatches}
    return (
        f"Bank balances disagree with transactions in {len(mismatches)} period(s) "
        f"on {len(accounts)} account(s)"
    )


def evaluate_reconciliation_alerts(*, now=None):
    """Daily pass step: alert each operator about mismatches on accounts they can see.

    An operator is alerted again only when a window they were not yet alerted
    about mismatches. The windows already alerted are kept on their alert
    settings, not inferred from the alert row, which retention purges. The title
    holds counts only. An operator with no visible mismatch has their
    reconciliation alerts and that state cleared. The run time is recorded for
    Settings → Data.
    """
    from .backup_health import operator_people

    now = now or timezone.now()
    results = reconcile(eligible_accounts())
    created = []
    for operator in operator_people():
        with transaction.atomic():
            created.extend(_alert_operator(operator, mismatched_windows(visible_results(operator, results)), now))
    record_run(now)
    return created


def _alert_operator(operator, mismatches, now):
    from .alert_services import raise_alert, settings_for

    # Locked, so overlapping passes cannot both alert about the same windows.
    prefs = AlertSettings.objects.select_for_update().get(pk=settings_for(operator).pk)
    current = sorted({_window_key(window) for window in mismatches})
    if not current:
        Alert.objects.filter(recipient=operator, kind=Alert.Kind.RECONCILIATION).delete()
    created = []
    if set(current) - set(prefs.reconciliation_alerted_windows):
        # The saved windows decide whether to alert, so each alert gets its own
        # key: a set seen before (a mismatch that resolved and came back) must
        # not collide with an older, already read alert row.
        digest = hashlib.sha256("\n".join(current).encode("utf-8")).hexdigest()[:16]
        created = raise_alert(
            [operator],
            Alert.Kind.RECONCILIATION,
            reconciliation_alert_title(mismatches),
            reverse("settings-data"),
            f"reconcile:{now.strftime('%Y%m%dT%H%M%S%f')}:{digest}",
            source_accounts=Account.objects.filter(pk__in={window.account_id for window in mismatches}),
        )
    # Forget resolved windows, so one that mismatches again later alerts again.
    if prefs.reconciliation_alerted_windows != current:
        prefs.reconciliation_alerted_windows = current
        prefs.save(update_fields=("reconciliation_alerted_windows",))
    return created


@transaction.atomic
def record_run(now):
    ReconciliationRun.objects.all().delete()
    ReconciliationRun.objects.create(ran_at=now)


def settings_reconciliation_context(person):
    """Operator-only Settings → Data section, limited to accounts that operator can see."""
    from .backup_health import person_is_operator

    if not person_is_operator(person):
        return {"reconciliation": None}
    accounts = eligible_accounts(Account.objects.visible_to(person))  # pyright: ignore[reportAttributeAccessIssue] - AccountQuerySet.as_manager() is untyped, as for every Account.objects.visible_to in the baseline
    names = {account.pk: account.name for account in accounts}
    results = reconcile(accounts)
    mismatches = sorted(
        mismatched_windows(results),
        key=lambda window: (window.end, window.account_id),
        reverse=True,
    )
    last_run = ReconciliationRun.objects.order_by("-ran_at").values_list("ran_at", flat=True).first()
    return {
        "reconciliation": {
            "accounts": len(results),
            "counts": state_counts(results),
            "last_run": last_run,
            "mismatches": [
                {"account": names[window.account_id], "start": window.start, "end": window.end,
                 "difference_display": format_minor(window.difference_minor)}
                for window in mismatches[:MAX_LISTED_MISMATCHES]
            ],
            "more_mismatches": max(0, len(mismatches) - MAX_LISTED_MISMATCHES),
        }
    }
