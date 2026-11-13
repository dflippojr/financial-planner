"""The days file imports own after a transactions-mode SimpleFIN cut-over (#743).

A file import refuses rows on or after a link's cut-over, except while the
link is failing (#735): then a file may fill the days after the last synced
row. Those days belong to the file. The link gets a second boundary that is
derived from the ledger, not stored: the latest `date_range_end` of an active
file batch that reaches past the cut-over, meaning one that holds an active
row dated on or after it. The range is the one the member confirmed, not the
latest row date, so a file exported partway through its last day still owns
that day. The sync skips SimpleFIN rows
posted on or before it, as it skips rows before the cut-over, so a recovered
link never supplies a day the file already did. Undoing the batch removes the
boundary. Dates only: nothing here compares amounts (#671, #732).

Every day here is the date SimpleFIN would give a row: a card export's posted
date, read from `original_fields`, or the transaction date when that is
missing. A batch's own rows can reach past its confirmed range (a card row
posts after the range ends, or the member typed an earlier end), so a batch
owns through the later of its range end and its latest row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Max

from finance.csv_import.parser import parse_posted_date
from finance.csv_import.profiles import POSTED_DATE_MAPPINGS
from finance.models import ImportBatch, Transaction

FILE_IMPORT_SOURCES = (
    ImportBatch.Source.HUNTINGTON,
    ImportBatch.Source.CAPITAL_ONE,
    ImportBatch.Source.APPLE_CARD,
    ImportBatch.Source.VANGUARD,
    ImportBatch.Source.OFX,
)


def _row_day(source, original_fields, transaction_date) -> date:
    mapping = POSTED_DATE_MAPPINGS[source]
    cell = (original_fields or {}).get(mapping.posted_date_column)
    posted = parse_posted_date(cell, mapping.date_format) if isinstance(cell, str) else None
    return posted or transaction_date


def _file_rows(active):
    return active.filter(import_batch__source__in=FILE_IMPORT_SOURCES)


def _card_row_days(rows):
    """(batch id, day) for each card-export row in `rows`, by its posted date."""
    card_rows = rows.filter(import_batch__source__in=POSTED_DATE_MAPPINGS).values_list(
        "import_batch_id", "import_batch__source", "original_fields", "transaction_date"
    )
    for batch_id, source, original_fields, transaction_date in card_rows.iterator():
        yield batch_id, _row_day(source, original_fields, transaction_date)


def latest_file_day(active, *, owned: tuple[date, date] | None = None) -> date | None:
    """The latest day an active file-imported row covers, leaving out the days in `owned`.

    `owned` is an inclusive (first, last) span of days file imports own after
    the cut-over; their rows cannot collide with a sync, which skips those days.
    """
    rows = _file_rows(active)
    plain = rows.exclude(import_batch__source__in=POSTED_DATE_MAPPINGS)
    if owned is not None:
        plain = plain.exclude(transaction_date__range=owned)
    latest = plain.aggregate(day=Max("transaction_date"))["day"]
    for _batch_id, day in _card_row_days(rows):
        if owned is not None and owned[0] <= day <= owned[1]:
            continue
        if latest is None or day > latest:
            latest = day
    return latest


@dataclass(frozen=True)
class OwnedDays:
    """The days file imports own from a link's cut-over on.

    `through` is the boundary the sync honours: it skips SimpleFIN rows posted
    on or before it. `ranges` are the inclusive spans each file supplied after
    the newest SimpleFIN row before it, merged and oldest first, for display;
    a span SimpleFIN already supplied in full is left out of them.
    """

    through: date | None
    ranges: tuple[tuple[date, date], ...] = ()


def _batch_ends(account, cutover):
    """{batch id: (range start, the last day it owns)} for each active file batch past the cut-over.

    A batch reaches past the cut-over when it holds an active row dated on or
    after it: a file the import accepted while the link was failing. A file
    imported while the link was healthy had every such row refused, so however
    far its confirmed range runs it supplied no day and owns none; letting its
    range own days would make the sync drop transactions no file supplied.
    """
    batches = ImportBatch.objects.filter(
        account=account,
        source__in=FILE_IMPORT_SOURCES,
        status=ImportBatch.Status.ACTIVE,
        archived_at__isnull=True,
    )
    rows = Transaction.objects.filter(
        account=account, status=Transaction.Status.ACTIVE, import_batch__in=batches
    )
    row_days = dict(
        rows.exclude(import_batch__source__in=POSTED_DATE_MAPPINGS)
        .filter(transaction_date__gte=cutover)
        .values("import_batch_id")
        .annotate(day=Max("transaction_date"))
        .values_list("import_batch_id", "day")
    )
    for batch_id, day in _card_row_days(rows):
        if day >= cutover and (batch_id not in row_days or day > row_days[batch_id]):
            row_days[batch_id] = day
    if not row_days:
        return {}
    ranges = batches.filter(pk__in=row_days).values_list("pk", "date_range_start", "date_range_end")
    return {pk: (start, max(end, row_days[pk])) for pk, start, end in ranges}


def file_owned_through(account, cutover: date) -> date | None:
    """The last day file imports own after `cutover`, or None when no active file batch reaches past it."""
    ends = [end for _start, end in _batch_ends(account, cutover).values()]
    return max(ends) if ends else None


def file_owned_days(account, cutover: date) -> OwnedDays:
    """The boundary the sync honours, with the spans each file supplied after the cut-over."""
    spans = sorted(_batch_ends(account, cutover).values(), key=lambda span: (span[1], span[0]))
    if not spans:
        return OwnedDays(None)
    synced = Transaction.objects.filter(
        account=account, status=Transaction.Status.ACTIVE, import_batch__source=ImportBatch.Source.SIMPLEFIN
    )
    ranges: list[tuple[date, date]] = []
    for start, end in spans:
        before = synced.filter(transaction_date__lte=end).aggregate(day=Max("transaction_date"))["day"]
        first = max(start, cutover, before + timedelta(days=1) if before is not None else cutover)
        if first > end:
            continue
        while ranges and first <= ranges[-1][1] + timedelta(days=1):
            first = min(first, ranges[-1][0])
            ranges.pop()
        ranges.append((first, end))
    return OwnedDays(spans[-1][1], tuple(ranges))
