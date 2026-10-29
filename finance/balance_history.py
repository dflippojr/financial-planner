"""Each account's balance snapshots in order, for net worth and debt planning (#586).

A history is a pair of lists: the snapshots sorted oldest first and their dates.
Later dates win; on the same date SimpleFIN outranks a manual entry, then the
higher pk wins. An account with no eligible snapshot has an empty history, which
callers treat as an unknown balance, never as zero.
"""

from bisect import bisect_right

from .models import BalanceSnapshot


def _source_rank(source):
    return 1 if source == BalanceSnapshot.Source.SIMPLEFIN else 0


def ordered_history(rows):
    ordered = sorted(rows, key=lambda row: (row.snapshot_date, _source_rank(row.source), row.pk))
    return ordered, [row.snapshot_date for row in ordered]


def last_on_or_before(ordered, dates, as_of):
    index = bisect_right(dates, as_of) - 1
    if index < 0:
        return None
    return ordered[index]


def indexed_histories(principal, accounts, date_to):
    """The history of each selected account's visible snapshots on or before date_to.

    One query loads every account; no account selected means no query.
    """
    if not accounts:
        return {}
    grouped = {account.pk: [] for account in accounts}
    rows = BalanceSnapshot.objects.visible_to(principal).filter(  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every BalanceSnapshot.objects.visible_to in the baseline
        account_id__in=grouped,
        snapshot_date__lte=date_to,
    )
    for row in rows:
        grouped[row.account_id].append(row)
    return {account.pk: ordered_history(grouped[account.pk]) for account in accounts}
