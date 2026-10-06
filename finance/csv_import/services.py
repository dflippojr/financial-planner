import hashlib
from collections import Counter
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from finance.csv_import.fingerprint import transaction_fingerprint
from finance.csv_import.parser import Preview, preview_csv
from finance.lifecycle_services import (
    _DENIED,
    _lock_visible_account_with_pair_counterparts,
    _person_for,
    _visible_account_for_update,
    lock_actor_household,
)
from finance.csv_import.saved_mappings import lock_saved_mapping
from finance.models import Account, ImportBatch, SavedCsvMapping, Transaction


@dataclass(frozen=True)
class ImportCommitResult:
    new_count: int
    duplicate_count: int
    invalid_count: int
    batch: ImportBatch | None


def _active_account(principal, account_id):
    account = _visible_account_for_update(principal, account_id)
    if account.status != Account.Status.ACTIVE or account.archived_at is not None:
        raise PermissionDenied(_DENIED)
    return account


def _fingerprint_for(account, row):
    return transaction_fingerprint(account.pk, row.transaction_date, row.amount_minor, row.description)


def _active_fingerprint_counts(account):
    fingerprints = Transaction.objects.filter(
        account=account,
        status=Transaction.Status.ACTIVE,
    ).values_list("fingerprint", flat=True)
    return Counter(fingerprints)


def classify_overlap(account, preview: Preview) -> Preview:
    """Mark valid rows as new or duplicate using per-account fingerprint multiplicity.

    Two identical purchases on the same day are both new when the account has no
    matching active rows, and only extras beyond the stored count are new on a
    reimport. Matching is limited to this account so a private ledger cannot
    hide overlap on a household account, and archived (undone) rows do not count.
    """
    remaining = _active_fingerprint_counts(account)
    classified = []
    for row in preview.rows:
        if not row.is_valid:
            classified.append(row.classified("invalid"))
            continue
        digest = _fingerprint_for(account, row)
        if remaining[digest] > 0:
            remaining[digest] -= 1
            classified.append(row.classified("duplicate"))
        else:
            classified.append(row.classified("new"))
    return Preview(tuple(classified))


def _original_fields(document, row_number, mapping):
    source_row = next(row for row in document.rows if row.number == row_number)
    excluded = set(mapping.excluded_original_columns)
    return {
        header: cell
        for header, cell in zip(document.headers, source_row.cells)
        if header not in excluded
    }


def _kind_for(source):
    if source == ImportBatch.Source.VANGUARD:
        return Transaction.Kind.INVESTMENT_ACTIVITY
    return Transaction.Kind.CASH_FLOW


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
    person = _person_for(principal)
    lock_actor_household(person)
    account = _active_account(person, account_id)
    if saved_csv_mapping is not None:
        saved_csv_mapping = (
            SavedCsvMapping.objects.visible_to(person)
            .filter(
                pk=saved_csv_mapping.pk,
                status=SavedCsvMapping.Status.ACTIVE,
                archived_at__isnull=True,
            )
            .first()
        )
        if saved_csv_mapping is None:
            raise PermissionDenied(_DENIED)
    list(
        Transaction.objects.select_for_update().filter(
            account=account,
            status=Transaction.Status.ACTIVE,
        )
    )

    if date_range_end < date_range_start:
        raise ValidationError("The import date range must end on or after it starts.")
    if source not in ImportBatch.Source.values or source == ImportBatch.Source.MANUAL:
        raise ValidationError("Choose a supported import source.")

    preview = classify_overlap(account, preview_csv(document, mapping))
    new_rows = [row for row in preview.rows if row.overlap_status == "new"]
    source_file_sha256 = hashlib.sha256(content).hexdigest()
    batch = None
    if new_rows:
        batch = ImportBatch.objects.create(
            account=account,
            imported_by=person,
            source=source,
            source_file_sha256=source_file_sha256,
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            saved_csv_mapping=saved_csv_mapping,
        )
        lock_saved_mapping(saved_csv_mapping)
        kind = _kind_for(source)
        Transaction.objects.bulk_create(
            [
                Transaction(
                    account=account,
                    import_batch=batch,
                    transaction_date=row.transaction_date,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    description=row.description,
                    kind=kind,
                    source_row_number=row.row_number,
                    source_transaction_id=row.source_transaction_id,
                    fingerprint=_fingerprint_for(account, row),
                    original_fields=_original_fields(document, row.row_number, mapping),
                )
                for row in new_rows
            ]
        )
    return ImportCommitResult(preview.new_count, preview.duplicate_count, preview.invalid_count, batch)


@transaction.atomic
def categorize_imported_batch(principal, batch):
    """Match transfers, then apply enabled rules to a just-committed batch.

    Runs after the import commits: refreshing transfers locks affected
    accounts in id order, which must not happen while the import still holds
    its own account lock.
    """
    from finance.category_services import refresh_transfer_pairs
    from finance.rule_services import apply_enabled_rules_to_transactions

    if batch is None:
        return []
    created = list(Transaction.objects.filter(import_batch=batch, status=Transaction.Status.ACTIVE))
    refresh_transfer_pairs(principal, transaction_ids=[row.pk for row in created])
    applied = apply_enabled_rules_to_transactions(principal, created)
    from finance.category_suggestion_services import queue_category_suggestions_for

    queue_category_suggestions_for(principal, created)
    from finance.alert_services import schedule_after_new_transactions

    schedule_after_new_transactions(created)
    return applied


def undo_import_batch(principal, account_id, batch_id):
    """Archive one import batch and only its transactions.

    Manual entries are one-row batches too, but they are removed only through
    delete_manual_transaction, which also records the deletion in history.
    """
    return archive_batch(principal, account_id, batch_id, manual=False)


@transaction.atomic
def archive_batch(principal, account_id, batch_id, *, manual):
    """Archive one active batch of the given kind (manual entry or import)."""
    person = _person_for(principal)
    lock_actor_household(person)
    if not Account.objects.visible_to(person).filter(pk=account_id).exists():
        raise PermissionDenied(_DENIED)
    seed_leg_ids = list(
        Transaction.objects.filter(import_batch_id=batch_id, account_id=account_id).values_list("pk", flat=True)
    )
    account = _lock_visible_account_with_pair_counterparts(person, account_id, seed_leg_ids=seed_leg_ids)
    if account.status != Account.Status.ACTIVE or account.archived_at is not None:
        raise PermissionDenied(_DENIED)
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
        raise PermissionDenied(_DENIED)
    from finance.receipt_services import delete_receipts_for_transactions

    removed_ids = list(
        Transaction.objects.filter(import_batch=batch, status=Transaction.Status.ACTIVE).values_list("pk", flat=True)
    )
    delete_receipts_for_transactions(removed_ids)
    now = timezone.now()
    Transaction.objects.select_for_update().filter(
        import_batch=batch,
        status=Transaction.Status.ACTIVE,
    ).update(status=Transaction.Status.ARCHIVED, archived_at=now)
    from finance.models import BalanceSnapshot

    BalanceSnapshot.objects.filter(import_batch=batch).delete()
    batch.status = ImportBatch.Status.ARCHIVED
    batch.archived_at = now
    batch.save(update_fields=("status", "archived_at"))
    from finance.category_services import refresh_transfer_pairs, revalidate_pairs_touching_import_batch

    revalidate_pairs_touching_import_batch(person, batch_id)
    transaction.on_commit(lambda: refresh_transfer_pairs(person, transaction_ids=seed_leg_ids))
    return batch
