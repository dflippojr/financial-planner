import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from finance.audit_services import record
from finance.calculated_balances import (
    has_calculated_expression,
    refresh_calculated_balances,
    refresh_locked_calculated_balances,
)
from finance.input_limits import MAX_DESCRIPTION_CHARS
from finance.csv_import.fingerprint import file_import_fingerprint_description, transaction_fingerprint
from finance.csv_import.parser import Preview, preview_csv
from finance.csv_import.profiles import CSV_FILE_SOURCES, OFX_MAPPING
from finance.access import DENIED, require_person
from finance.lifecycle_services import (
    _lock_visible_account_with_pair_and_refund_counterparts,
    _visible_account_for_update,
    lock_actor_household,
    lock_ledger_writer,
)
from finance.csv_import.saved_mappings import headers_match, lock_saved_mapping, mapping_from_saved
from finance.models import (
    Account,
    AccountLink,
    AuditEvent,
    ImportBatch,
    SavedCsvMapping,
    Transaction,
    TransactionCorrectionHistory,
)
from finance.simplefin_file_days import FILE_IMPORT_SOURCES
from finance.simplefin_health import last_synced_days, link_is_failing

MAPPING_CHANGED = "This mapping changed; preview the file again."


@dataclass(frozen=True)
class ImportCommitResult:
    new_count: int
    duplicate_count: int
    invalid_count: int
    batch: ImportBatch


def _active_account(principal, account_id):
    # The lock query also reads whether the account has saved calculated
    # balances, so an import pays no query to learn it.
    account = _visible_account_for_update(principal, account_id, _has_calculated=has_calculated_expression())
    if account.status != Account.Status.ACTIVE or account.archived_at is not None:
        raise PermissionDenied(DENIED)
    return account


def _fingerprint_for(account, row):
    return transaction_fingerprint(
        account.pk, row.transaction_date, row.amount_minor, file_import_fingerprint_description(row.description)
    )


def _active_fingerprint_counts(account):
    fingerprints = Transaction.objects.filter(
        account=account,
        status=Transaction.Status.ACTIVE,
    ).values_list("fingerprint", flat=True)
    return Counter(fingerprints)


@dataclass(frozen=True)
class SimpleFinWindow:
    """The days a transactions-mode SimpleFIN link claims from a file import.

    A healthy link claims every day from its cut-over. While the link is
    failing (#735) it claims only the days through its last SimpleFIN row, so
    a file may fill the gap after it; with no SimpleFIN row it claims none.
    """

    cutover: date
    failing: bool
    last_synced: date | None

    @property
    def accepted_from(self) -> date | None:
        """The first day on or after the cut-over a file import may supply, or None for a healthy link."""
        if not self.failing:
            return None
        if self.last_synced is None or self.last_synced < self.cutover:
            return self.cutover
        return self.last_synced + timedelta(days=1)

    def refusal(self, day) -> str | None:
        """Why a row compared on `day` is refused, or None when a file import may supply it."""
        if day < self.cutover:
            return None
        accepted_from = self.accepted_from
        if accepted_from is None:
            return f"Covered by SimpleFIN from {self.cutover.isoformat()}."
        if day >= accepted_from:
            return None
        # Only a failing link with a SimpleFIN row on or after its cut-over gets here.
        return f"Covered by SimpleFIN through {(accepted_from - timedelta(days=1)).isoformat()}."

    def owned_through(self, rows, date_range_end) -> date | None:
        """The last day a file imported now owns once its rows are classified (#743), or None.

        Only while the link is failing: the later of the confirmed range end and
        the latest day the import compared among the rows it accepted (new or
        duplicate), so a card row that posted after the range ended still counts.
        A file reaching no day the failing link accepts owns none: those days
        are SimpleFIN's, and owning them would drop its late-posting rows.
        """
        accepted_from = self.accepted_from
        if accepted_from is None:
            return None
        days = [row.cutover_date for row in rows if row.overlap_status in ("new", "duplicate")]
        if date_range_end is not None:
            days.append(date_range_end)
        through = max(days, default=None)
        return through if through is not None and through >= accepted_from else None


def simplefin_window(account, now=None) -> SimpleFinWindow | None:
    """The account's transactions-mode SimpleFIN window, or None when it has no such link."""
    link = (
        AccountLink.objects.select_related("connection")
        .filter(account=account, mode=AccountLink.Mode.TRANSACTIONS)
        .first()
    )
    if link is None:
        return None
    failing = link_is_failing(link, now)
    return SimpleFinWindow(
        cutover=link.cutover_date,
        failing=failing,
        last_synced=last_synced_days({account.pk}).get(account.pk) if failing else None,
    )


def classify_overlap(account, preview: Preview) -> Preview:
    """Mark valid rows as new or duplicate using per-account fingerprint multiplicity.

    Two identical purchases on the same day are both new when the account has no
    matching active rows, and only extras beyond the stored count are new on a
    reimport. Matching is limited to this account so a private ledger cannot
    hide overlap on a household account, and archived (undone) rows do not count.
    Rows on or after a transactions-mode SimpleFIN cut-over are invalid: the sync
    supplies that period, and its descriptions differ from the bank's CSV. While
    the link is failing, only rows through its last SimpleFIN row are (#735). A
    card row is compared by its posted date, the day SimpleFIN dates it by.
    """
    remaining = _active_fingerprint_counts(account)
    window = simplefin_window(account)
    classified = []
    for row in preview.rows:
        refusal = window.refusal(row.cutover_date) if row.is_valid and window is not None else None
        if refusal is not None:
            row = row.with_error(refusal)
        if not row.is_valid:
            classified.append(row.classified("invalid"))
            continue
        digest = _fingerprint_for(account, row)
        if remaining[digest] > 0:
            remaining[digest] -= 1
            classified.append(row.classified("duplicate"))
        else:
            classified.append(row.classified("new"))
    return Preview(tuple(classified), simplefin_window=window)


def _original_fields(headers, source_row, mapping):
    excluded = set(mapping.excluded_original_columns)
    return {
        header: cell
        # A ragged row keeps only the cells that have a header.
        for header, cell in zip(headers, source_row.cells, strict=False)
        if header not in excluded
    }


def _require_file_source(source, mapping):
    if mapping == OFX_MAPPING:
        allowed = source == ImportBatch.Source.OFX
    else:
        allowed = source in CSV_FILE_SOURCES
    if not allowed:
        raise ValidationError("Choose a supported import source.")


def _locked_saved_mapping(person, saved_csv_mapping, mapping, document):
    """Re-read the saved mapping under the household lock and require the caller parsed with it.

    A household member can edit an unused mapping between the view's read and
    this commit. Parsing with the old fields but locking the new ones would make
    every later reimport fingerprint differently, so the commit is refused.
    """
    locked = (
        SavedCsvMapping.objects.visible_to(person)
        .select_for_update()
        .filter(
            pk=saved_csv_mapping.pk,
            status=SavedCsvMapping.Status.ACTIVE,
            archived_at__isnull=True,
        )
        .first()
    )
    if locked is None:
        raise PermissionDenied(DENIED)
    if mapping_from_saved(locked) != mapping or not headers_match(locked, document.headers):
        raise ValidationError(MAPPING_CHANGED)
    return locked


def _owned_through(preview, date_range_end):
    """The day a file committed from `preview` owns through (#743), or None; see SimpleFinWindow.owned_through."""
    window = preview.simplefin_window
    return window.owned_through(preview.rows, date_range_end) if isinstance(window, SimpleFinWindow) else None


@transaction.atomic
def commit_csv_import(
    principal,
    account_id,
    *,
    content,
    document,
    mapping,
    source,
    date_range_start,
    date_range_end,
    saved_csv_mapping=None,
):
    person = require_person(principal)
    own_membership, _memberships = lock_ledger_writer(person)
    account = _active_account(person, account_id)
    if saved_csv_mapping is not None:
        saved_csv_mapping = _locked_saved_mapping(person, saved_csv_mapping, mapping, document)
    # _active_account holds the account row lock, serializing concurrent imports
    # and ledger lifecycle changes without materializing the entire history.

    if date_range_end < date_range_start:
        raise ValidationError("The import date range must end on or after it starts.")
    _require_file_source(source, mapping)

    preview = classify_overlap(account, preview_csv(document, mapping))
    new_rows = [row for row in preview.rows if row.overlap_status == "new"]
    # What this file held but did not store, so undoing the batch that did
    # store it can hand the row to this one instead of leaving a gap (#796).
    skipped_duplicates = Counter(
        _fingerprint_for(account, row) for row in preview.rows if row.overlap_status == "duplicate"
    )
    source_file_sha256 = hashlib.sha256(content).hexdigest()
    # The batch is always written, even with no new rows: it records that the
    # member imported this date range, which Import due and Missing import read.
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=person,
        source=source,
        source_file_sha256=source_file_sha256,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        saved_csv_mapping=saved_csv_mapping,
        owns_simplefin_through=_owned_through(preview, date_range_end),
        skipped_duplicates=dict(skipped_duplicates),
    )
    if saved_csv_mapping is not None:
        lock_saved_mapping(saved_csv_mapping)
    if new_rows:
        source_rows = {row.number: row for row in document.rows}
        Transaction.objects.bulk_create(
            [
                Transaction(
                    account=account,
                    import_batch=batch,
                    transaction_date=row.transaction_date,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    description=row.description[:MAX_DESCRIPTION_CHARS],
                    kind=Transaction.Kind.CASH_FLOW,
                    source_row_number=row.row_number,
                    source_transaction_id=row.source_transaction_id,
                    fingerprint=_fingerprint_for(account, row),
                    original_fields=_original_fields(document.headers, source_rows[row.row_number], mapping),
                )
                for row in new_rows
            ]
        )
        # Late rows dated on or before the anchor change saved calculated balances (#641).
        if own_membership is None:
            # Nothing serialized this import with a concurrent Save calculated
            # balances before the account lock, so the lock query may have
            # waited for it and read a stale answer: check again now.
            refresh_calculated_balances(person, account.pk)
        elif account._has_calculated:
            # Save takes the same household lock first, so the lock query
            # started after any concurrent Save committed and its answer is current.
            refresh_locked_calculated_balances(person, account)
    counts = {"new_count": preview.new_count, "duplicate_count": preview.duplicate_count,
              "invalid_count": preview.invalid_count, "format": source, "batch_id": batch.pk}
    if new_rows:
        record(person, AuditEvent.Action.IMPORT_COMMITTED, AuditEvent.TargetType.IMPORT_BATCH, batch.pk,
               audience={"account": account}, metadata=counts, verified=True)
    else:
        record(person, AuditEvent.Action.IMPORT_NO_NEW_ROWS, AuditEvent.TargetType.ACCOUNT, account.pk,
               audience={"account": account}, metadata=counts, verified=True)
    return ImportCommitResult(preview.new_count, preview.duplicate_count, preview.invalid_count, batch)


@transaction.atomic
def categorize_imported_batch(principal, batch):
    """Match transfers, then apply enabled rules to a just-committed batch.

    Runs after the import commits: refreshing transfers locks affected
    accounts in id order, which must not happen while the import still holds
    its own account lock.
    """
    from finance.transfer_pairing import refresh_transfer_pairs
    from finance.rule_services import apply_enabled_rules_to_transactions

    if batch is None:
        return []
    person = require_person(principal)
    created = list(Transaction.objects.filter(import_batch=batch, status=Transaction.Status.ACTIVE))
    if not created:
        return []
    refresh_transfer_pairs(person, transaction_ids=[row.pk for row in created])
    applied = apply_enabled_rules_to_transactions(person, created)
    from finance.category_suggestion_services import queue_category_suggestions_for

    queue_category_suggestions_for(person, created)
    from finance.alert_services import schedule_after_new_transactions

    schedule_after_new_transactions(created)
    return applied


@dataclass(frozen=True)
class CoverageHole:
    """Removed rows that an active batch still marks as covered but could not take (#796)."""

    batch: ImportBatch
    days: tuple[date, ...]
    row_count: int


@dataclass(frozen=True)
class ImportUndoResult:
    batch: ImportBatch
    # Rows moved to another active batch that also holds them, instead of being archived.
    rehomed_count: int = 0
    holes: tuple[CoverageHole, ...] = ()


def undo_import_batch(principal, account_id, batch_id) -> ImportUndoResult:
    """Archive one import batch and the transactions only it accounts for.

    A row that another active file import on the account skipped as a duplicate
    stays active and moves to that batch (#796); see _rehome_rows_other_batches_skipped.
    `holes` names each remaining batch that still covers an archived row's day
    but could not take the row; see _coverage_holes. Manual entries are one-row
    batches too, but they are removed only through delete_manual_transaction,
    which also records the deletion in history.
    """
    return archive_batch(principal, account_id, batch_id, manual=False)


def _other_active_file_batches(account, batch):
    """Every other active file batch on the account, oldest first, locked."""
    return list(
        ImportBatch.objects.select_for_update()
        .filter(
            account=account,
            status=ImportBatch.Status.ACTIVE,
            archived_at__isnull=True,
            source__in=FILE_IMPORT_SOURCES,
        )
        .exclude(pk=batch.pk)
        .order_by("pk")
    )


def _active_counts_by_batch(account_id, fingerprints, *, excluding=None):
    """Active rows on the account per fingerprint and batch: {fingerprint: Counter({batch id: rows})}."""
    rows = Transaction.objects.filter(
        account_id=account_id, status=Transaction.Status.ACTIVE, fingerprint__in=fingerprints
    )
    if excluding is not None:
        rows = rows.exclude(import_batch=excluding)
    counts = defaultdict(Counter)
    for batch_id, fingerprint in rows.values_list("import_batch_id", "fingerprint"):
        counts[fingerprint][batch_id] += 1
    return counts


def _rehome_rows_other_batches_skipped(person, batch, others, now):
    """Move the rows of `batch` that another active batch skipped as duplicates to that batch (#796).

    A file that held a row another batch had already stored skipped it and
    recorded its fingerprint in `skipped_duplicates`. Archiving the row with
    the batch that stored it would leave a gap that the other batch's date
    range hides, so the row moves to the oldest batch that still needs it. The
    rule is the one classify_overlap uses: fingerprints on this account,
    counted. A batch needs a row only for the skipped copies that no other
    active row on the account stands in for, so a file with two copies keeps
    two rows and no more. Every other active batch is asked, earlier or later:
    a row can come to rest on a batch committed after one that relies on it.
    Rows no batch needs are left for the caller to archive.

    Only a batch of the same source takes a row: `original_fields` is read by
    the batch's source (a card's posted date column), so a row moved to another
    source's batch would be misread. _coverage_holes reports those instead.
    Returns the ids of the moved rows. The caller holds the account lock, which
    every import and undo on the account takes.
    """
    takers = [other for other in others if other.source == batch.source and other.skipped_duplicates]
    if not takers:
        return []
    candidates = defaultdict(list)
    for pk, fingerprint in (
        Transaction.objects.filter(import_batch=batch, status=Transaction.Status.ACTIVE)
        .order_by("pk")
        .values_list("pk", "fingerprint")
    ):
        candidates[fingerprint].append(pk)
    wanted = {fingerprint for taker in takers for fingerprint in taker.skipped_duplicates} & set(candidates)
    if not wanted:
        return []
    standing = _active_counts_by_batch(batch.account_id, wanted, excluding=batch)
    history = []
    for taker in takers:
        moved = []
        for fingerprint in sorted(wanted & set(taker.skipped_duplicates)):
            elsewhere = sum(count for batch_id, count in standing[fingerprint].items() if batch_id != taker.pk)
            take = min(taker.skipped_duplicates[fingerprint] - elsewhere, len(candidates[fingerprint]))
            if take <= 0:
                continue
            moved.extend(candidates[fingerprint][:take])
            del candidates[fingerprint][:take]
            standing[fingerprint][taker.pk] += take
            # The moved rows are this batch's own now, no longer skipped.
            taker.skipped_duplicates[fingerprint] -= take
            if not taker.skipped_duplicates[fingerprint]:
                del taker.skipped_duplicates[fingerprint]
        if not moved:
            continue
        Transaction.objects.filter(pk__in=moved).update(import_batch=taker, updated_at=now)
        taker.save(update_fields=("skipped_duplicates",))
        history.extend(
            TransactionCorrectionHistory(
                transaction_id=pk,
                actor=person,
                recorded_at=now,
                field_name=TransactionCorrectionHistory.Field.IMPORT_BATCH,
                previous_description=str(batch),
                new_description=str(taker),
            )
            for pk in moved
        )
    TransactionCorrectionHistory.objects.bulk_create(history)
    return [row.transaction_id for row in history]


def _coverage_holes(batch, others, removed_ids):
    """Each remaining batch that still covers a just-archived row but could not take it (#796).

    The one place an undo answers for every other active file batch, earlier or
    later, so no undo leaves a gap unsaid. Re-homing has already given each
    same-source batch with a record the rows it needs. Two kinds of batch are
    left. A batch of another source recorded the row but cannot take it; it has
    a hole while fewer active rows with the fingerprint remain on the account
    than its file held. A batch from before migration 0093 (`skipped_duplicates`
    null) recorded nothing, so every removed row whose import-time day is
    inside its range may be one it held, and the batch is named whether it was
    committed before or after the undone one (owner decision 2026-11-25). That
    warns more often than needed while such batches remain, and is still best
    effort: nothing stored can show a row such a file held outside the range
    the member confirmed for it. The days a hole names are import-time days,
    the ones the batch marks covered.
    """
    unknown = [other for other in others if other.skipped_duplicates is None]
    recorded = [other for other in others if other.skipped_duplicates and other.source != batch.source]
    if not removed_ids or not (unknown or recorded):
        return ()
    # A row is matched by the day it was imported with, which its fingerprint
    # was built from: the first date its correction history replaced, or its
    # current date when the date was never corrected. A row moved out of a
    # batch's range afterwards is still that batch's row.
    imported_days = {}
    for pk, day in (
        TransactionCorrectionHistory.objects.filter(
            transaction_id__in=removed_ids, field_name=TransactionCorrectionHistory.Field.TRANSACTION_DATE
        )
        .order_by("-recorded_at", "-pk")
        .values_list("transaction_id", "previous_date")
    ):
        imported_days[pk] = day
    removed = sorted(
        (
            (pk, fingerprint, imported_days.get(pk, day))
            for pk, fingerprint, day in Transaction.objects.filter(pk__in=removed_ids).values_list(
                "pk", "fingerprint", "transaction_date"
            )
        ),
        key=lambda row: (row[2], row[0]),
    )
    fingerprints = {fingerprint for _pk, fingerprint, _day in removed}
    active = _active_counts_by_batch(batch.account_id, fingerprints) if recorded else {}
    holes = []
    for other in sorted(unknown + recorded, key=lambda found: found.pk):
        days = []
        if other.skipped_duplicates is None:
            days = [
                day for _pk, _fingerprint, day in removed
                if other.date_range_start <= day <= other.date_range_end
            ]
        else:
            short = {
                fingerprint: other.skipped_duplicates[fingerprint]
                - sum(count for batch_id, count in active[fingerprint].items() if batch_id != other.pk)
                for fingerprint in fingerprints & set(other.skipped_duplicates)
            }
            for _pk, fingerprint, day in removed:
                if short.get(fingerprint, 0) > 0:
                    short[fingerprint] -= 1
                    days.append(day)
        if days:
            holes.append(CoverageHole(other, tuple(sorted(set(days))), len(days)))
    return tuple(holes)


@transaction.atomic
def archive_batch(principal, account_id, batch_id, *, manual) -> ImportUndoResult:
    """Archive one active batch of the given kind (manual entry or import)."""
    person = require_person(principal)
    lock_actor_household(person)
    if not Account.objects.visible_to(person).filter(pk=account_id).exists():
        raise PermissionDenied(DENIED)
    # A manual entry's batch holds its one row for good, so its rows can seed
    # the ordered counterpart locks. An import's cannot: until the account lock
    # is held, a concurrent undo may re-home a row into the batch (#796), and a
    # counterpart account missed here would be reached later out of order. Every
    # row of the account seeds the locks instead; rows never change account.
    manual_leg_ids = None
    if manual:
        manual_leg_ids = list(
            Transaction.objects.filter(import_batch_id=batch_id, account_id=account_id).values_list("pk", flat=True)
        )
    account = _lock_visible_account_with_pair_and_refund_counterparts(person, account_id, seed_leg_ids=manual_leg_ids)
    if account.status != Account.Status.ACTIVE or account.archived_at is not None:
        raise PermissionDenied(DENIED)
    batch = (
        ImportBatch.objects.select_for_update()
        .filter(
            pk=batch_id,
            account=account,
            status=ImportBatch.Status.ACTIVE,
            archived_at__isnull=True,
        )
        .first()
    )
    if batch is None or (batch.source == ImportBatch.Source.MANUAL) != manual:
        raise PermissionDenied(DENIED)
    from finance.receipt_services import delete_receipts_for_transactions

    now = timezone.now()
    # Read under the account lock, before any row leaves the batch.
    seed_leg_ids = list(Transaction.objects.filter(import_batch=batch).values_list("pk", flat=True))
    # Deleting a manual entry removes that entry, whatever a file also holds.
    others = [] if manual else _other_active_file_batches(account, batch)
    rehomed_ids = _rehome_rows_other_batches_skipped(person, batch, others, now)
    removed_ids = list(
        Transaction.objects.filter(import_batch=batch, status=Transaction.Status.ACTIVE).values_list("pk", flat=True)
    )
    delete_receipts_for_transactions(removed_ids)
    Transaction.objects.select_for_update().filter(
        import_batch=batch,
        status=Transaction.Status.ACTIVE,
    ).update(status=Transaction.Status.ARCHIVED, archived_at=now)
    from finance.refund_links import drop_refund_links_to_originals
    from finance.transfer_pairing import refresh_transfer_pairs, revalidate_pairs_touching_import_batch

    drop_refund_links_to_originals(removed_ids, person)
    from finance.models import BalanceSnapshot

    BalanceSnapshot.objects.filter(import_batch=batch).delete()
    batch.status = ImportBatch.Status.ARCHIVED
    batch.archived_at = now
    # An undone file hands the days it owned back to SimpleFIN (#743).
    batch.owns_simplefin_through = None
    batch.save(update_fields=("status", "archived_at", "owns_simplefin_through"))
    refresh_calculated_balances(person, account.pk)
    revalidate_pairs_touching_import_batch(person, batch_id)
    transaction.on_commit(lambda: refresh_transfer_pairs(person, transaction_ids=seed_leg_ids))
    if not manual:
        record(person, AuditEvent.Action.IMPORT_UNDONE, AuditEvent.TargetType.IMPORT_BATCH, batch.pk,
               audience={"account": account},
               metadata={"batch_id": batch.pk, "row_count": len(removed_ids), "rehomed_count": len(rehomed_ids),
                         "format": batch.source})
    return ImportUndoResult(batch, len(rehomed_ids), _coverage_holes(batch, others, removed_ids))
