"""Hand-entered transactions: cash, checks and accounts the app does not import.

Each manual entry is stored as its own one-row ImportBatch with source
"manual", so provenance, visibility, reports and correction history work the
same as for imported rows. Its fingerprint is salted with the batch id, so it
never matches an imported row during overlap classification.
"""

import hashlib
import secrets

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .audit_services import record
from .category_services import assignable_categories
from .csv_import.fingerprint import manual_entry_fingerprint
from .csv_import.services import archive_batch, categorize_imported_batch
from .lifecycle_services import _DENIED, _person_for, _visible_account_for_update, lock_actor_household
from .models import Account, AuditEvent, ImportBatch, Tag, Transaction, TransactionCorrectionHistory


MANUAL_ENTRY_ACCOUNT_TYPES = (Account.Type.CHECKING, Account.Type.SAVINGS, Account.Type.CREDIT_CARD)
MANUAL_ENTRY_MARKER = {"entry": "manual"}
DESCRIPTION_REQUIRED = "Enter a description."
NOTE_TOO_LONG = "Notes must be 2,000 characters or fewer."


def manual_entry_accounts(principal):
    """Active checking, savings and credit-card accounts the member can edit."""
    return (
        Account.objects.visible_to(principal)
        .filter(
            status=Account.Status.ACTIVE,
            archived_at__isnull=True,
            account_type__in=MANUAL_ENTRY_ACCOUNT_TYPES,
        )
        .order_by("name", "pk")
    )


def _assignable_category(person, category_id):
    if not category_id:
        return None
    category = assignable_categories(person).filter(pk=category_id).first()
    if category is None:
        raise PermissionDenied(_DENIED)
    return category


def _active_tags(person, tag_ids):
    requested = {int(item) for item in tag_ids or ()}
    if not requested:
        return []
    tags = list(Tag.objects.visible_to(person).active().filter(pk__in=requested))
    if {tag.pk for tag in tags} != requested:
        raise PermissionDenied(_DENIED)
    return tags


@transaction.atomic
def _create_manual_entry(person, account_id, *, transaction_date, amount_minor, description, category_id, note, tag_ids):
    lock_actor_household(person)
    account = _visible_account_for_update(person, account_id)
    if not manual_entry_accounts(person).filter(pk=account.pk).exists():
        raise PermissionDenied(_DENIED)
    cleaned_description = (description or "").strip()
    if not cleaned_description:
        raise ValidationError(DESCRIPTION_REQUIRED)
    cleaned_note = note or ""
    if len(cleaned_note) > 2000:
        raise ValidationError(NOTE_TOO_LONG)
    category = _assignable_category(person, category_id)
    tags = _active_tags(person, tag_ids)
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=person,
        source=ImportBatch.Source.MANUAL,
        source_file_sha256=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
        date_range_start=transaction_date,
        date_range_end=transaction_date,
    )
    entry = Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        currency=account.currency,
        description=cleaned_description,
        note=cleaned_note,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=manual_entry_fingerprint(
            account.pk, transaction_date, amount_minor, cleaned_description, batch.pk
        ),
        original_fields=dict(MANUAL_ENTRY_MARKER),
        category=category,
        # A category chosen on the form is a manual choice, which rules never override.
        category_source=Transaction.CategorySource.MANUAL if category else Transaction.CategorySource.UNSET,
    )
    entry.tags.set(tags)
    record(person, AuditEvent.Action.RECORD_CREATED, AuditEvent.TargetType.TRANSACTION, entry.pk,
           audience={"account": account}, metadata={"batch_id": batch.pk, "format": "manual"})
    return entry


def add_manual_transaction(
    principal,
    account_id,
    *,
    transaction_date,
    amount_minor,
    description,
    category_id=None,
    note="",
    tag_ids=(),
):
    """Record one hand-entered transaction, then run the new-transaction pipeline.

    Raises PermissionDenied for an account, category or tag the member cannot
    use, with the same message as a missing record.
    """
    person = _person_for(principal)
    entry = _create_manual_entry(
        person,
        account_id,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        category_id=category_id,
        note=note,
        tag_ids=tag_ids,
    )
    # Transfer matching, rules, suggestions and alerts run after the entry
    # commits, in the same order and with the same lock rules as an import.
    categorize_imported_batch(person, entry.import_batch)
    return entry


@transaction.atomic
def delete_manual_transaction(principal, transaction_id):
    """Soft-delete a manual entry and record the deletion in correction history.

    Imported transactions are refused with the same response as a missing one.
    """
    person = _person_for(principal)
    entry = (
        Transaction.objects.visible_to(person)
        .filter(
            pk=transaction_id,
            status=Transaction.Status.ACTIVE,
            import_batch__source=ImportBatch.Source.MANUAL,
        )
        .only("pk", "account_id", "import_batch_id")
        .first()
    )
    if entry is None:
        raise PermissionDenied(_DENIED)
    archive_batch(person, entry.account_id, entry.import_batch_id, manual=True)
    history = TransactionCorrectionHistory.objects.create(
        transaction_id=entry.pk,
        actor=person,
        recorded_at=timezone.now(),
        field_name=TransactionCorrectionHistory.Field.DELETED,
        previous_description="Active",
        new_description="Deleted",
    )
    record(person, AuditEvent.Action.RECORD_DELETED, AuditEvent.TargetType.TRANSACTION, entry.pk,
           audience={"account": Account.objects.get(pk=entry.account_id)},
           metadata={"batch_id": entry.import_batch_id, "history_id": history.pk})
    return entry
