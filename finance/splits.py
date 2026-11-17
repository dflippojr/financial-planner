"""Split transactions: split one row across categories, recategorize a part, and unsplit."""

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .access import DENIED as _DENIED
from .access import require_person as _person_for
from .access import visible_rows
from .category_services import assignable_categories
from .ledger_counting import with_transfer_state
from .ledger_edits import history_label, lock_owned_transactions, record_text_history
from .lifecycle_services import lock_actor_household
from .models import RefundLink, Transaction, TransactionCorrectionHistory, TransactionSplit
from .refund_links import (
    SPLIT_PART_REQUIRED,
    apply_inherited_category,
    refunds_following_originals,
)
from .transfer_pairing import refresh_household_transfer_pairs


SPLIT_TRANSFER_ERROR = "Unpair the transfer before splitting."
SPLIT_REFUND_ERROR = "Unlink the refund before splitting."
SPLIT_SUM_ERROR = "Parts must add up exactly to the transaction amount."
SPLIT_SIGN_ERROR = "Each part must be non-zero and have the same sign as the transaction."
SPLIT_COUNT_ERROR = "A split needs at least two parts."
SPLIT_AMOUNT_ERROR = "Unsplit to change the amount"


@transaction.atomic
def _split_amount_display(amount_minor):
    amount = Decimal(abs(amount_minor)) / Decimal(100)
    return f"${amount:,.2f}"


def _split_history_label(parts):
    bits = [f"{part.category.name} {_split_amount_display(part.amount_minor)}" for part in parts]
    return "Split: " + ", ".join(bits)


def _parts_with_categories(parent):
    return list(TransactionSplit.objects.filter(transaction=parent).select_related("category"))


def _normalized_parts(person, parent, parts):
    if parent.amount_minor == 0:
        raise ValidationError(SPLIT_SIGN_ERROR)
    if len(parts) < 2:
        raise ValidationError(SPLIT_COUNT_ERROR)
    parent_positive = parent.amount_minor > 0
    assignable = {item.pk: item for item in assignable_categories(person)}
    normalized = []
    total = 0
    for index, part in enumerate(parts):
        if isinstance(part, dict):
            category_id = part.get("category_id")
            amount_minor = part.get("amount_minor")
        else:
            category_id, amount_minor = part[0], part[1]
        if not amount_minor or (amount_minor > 0) != parent_positive:
            raise ValidationError(SPLIT_SIGN_ERROR)
        category = assignable.get(category_id)
        if category is None:
            raise PermissionDenied(_DENIED)
        total += amount_minor
        normalized.append((index, category, amount_minor))
    if total != parent.amount_minor:
        raise ValidationError(SPLIT_SUM_ERROR)
    return normalized


def _linked_original_refunds(parent):
    return list(
        Transaction.objects.filter(
            refund_link__original=parent,
            status=Transaction.Status.ACTIVE,
        )
        .select_related("account", "category", "refund_link")
        .order_by("pk")
    )


def _visible_linked_original_refund_ids(person, parent):
    return set(
        visible_rows(RefundLink, person)
        .filter(
            original=parent,
            refund_id__in=visible_rows(Transaction, person)
            .filter(status=Transaction.Status.ACTIVE)
            .values("pk"),
        )
        .values_list("refund_id", flat=True)
    )


def _partition_linked_refunds(person, refunds, parent):
    visible_ids = _visible_linked_original_refund_ids(person, parent)
    visible = [item for item in refunds if item.pk in visible_ids]
    hidden = [item for item in refunds if item.pk not in visible_ids]
    return visible, hidden


def _default_part_for_hidden_refunds(created_parts):
    return min(created_parts.values(), key=lambda part: (-abs(part.amount_minor), part.position))


def _inherit_refund_part(person, refund, part, following_ids):
    link = refund.refund_link
    link.original_part = part
    link.save(update_fields=("original_part",))
    if refund.pk in following_ids:
        apply_inherited_category(person, refund, part.category)


def _following_refund_ids(person, refunds):
    return set(
        refunds_following_originals(person, Transaction.objects.filter(pk__in=[item.pk for item in refunds]))
        .values_list("pk", flat=True)
    )


def _apply_refund_assignments(person, visible_refunds, hidden_refunds, created_parts, refund_assignments):
    following_ids = _following_refund_ids(person, (*visible_refunds, *hidden_refunds))
    assigned = {}
    if visible_refunds:
        if refund_assignments is None:
            raise ValidationError(SPLIT_PART_REQUIRED)
        for refund in visible_refunds:
            raw = refund_assignments.get(refund.pk, refund_assignments.get(str(refund.pk)))
            if raw is None:
                raise ValidationError(SPLIT_PART_REQUIRED)
            try:
                index = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValidationError(SPLIT_PART_REQUIRED) from exc
            if index not in created_parts:
                raise ValidationError(SPLIT_PART_REQUIRED)
            assigned[refund.pk] = created_parts[index]
    default_part = _default_part_for_hidden_refunds(created_parts) if hidden_refunds else None
    for refund in visible_refunds:
        _inherit_refund_part(person, refund, assigned[refund.pk], following_ids)
    for refund in hidden_refunds:
        _inherit_refund_part(person, refund, default_part, following_ids)


def _replace_splits(parent, normalized):
    RefundLink.objects.filter(original=parent).update(original_part=None)
    parent.splits.all().delete()
    created = {}
    for index, category, amount_minor in normalized:
        created[index] = TransactionSplit.objects.create(
            transaction=parent,
            category=category,
            amount_minor=amount_minor,
            position=index,
        )
    return created


@transaction.atomic
def split_transaction(principal, txn_id, parts, refund_assignments=None):
    person = _person_for(principal)
    financial_transaction = (
        visible_rows(Transaction, person)
        .filter(pk=txn_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    if financial_transaction is None:
        raise PermissionDenied(_DENIED)
    refunds = _linked_original_refunds(financial_transaction)
    lock_actor_household(person)
    locked = lock_owned_transactions([financial_transaction, *refunds])
    if not visible_rows(Transaction, person).filter(pk=financial_transaction.pk).exists():
        raise PermissionDenied(_DENIED)
    by_id = {item.pk: item for item in locked}
    financial_transaction = by_id[financial_transaction.pk]
    refunds = [by_id[item.pk] for item in refunds]
    visible_refunds, hidden_refunds = _partition_linked_refunds(person, refunds, financial_transaction)
    if with_transfer_state(person, financial_transaction).is_excluded_transfer:
        raise ValidationError(SPLIT_TRANSFER_ERROR)
    if RefundLink.objects.filter(refund=financial_transaction).exists():
        raise ValidationError(SPLIT_REFUND_ERROR)
    normalized = _normalized_parts(person, financial_transaction, parts)
    previous_label = (
        _split_history_label(_parts_with_categories(financial_transaction))
        if financial_transaction.category_source == Transaction.CategorySource.SPLIT
        else history_label(financial_transaction.category)
    )
    created_parts = _replace_splits(financial_transaction, normalized)
    financial_transaction.category = None
    financial_transaction.category_source = Transaction.CategorySource.SPLIT
    financial_transaction.save(update_fields=("category", "category_source", "updated_at"))
    _apply_refund_assignments(person, visible_refunds, hidden_refunds, created_parts, refund_assignments)
    new_label = _split_history_label([created_parts[index] for index, _, _ in normalized])
    record_text_history(
        financial_transaction,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        previous_label,
        new_label,
    )
    refresh_household_transfer_pairs(person, [financial_transaction.pk])
    from finance.alert_services import schedule_after_category_change

    schedule_after_category_change()
    return financial_transaction


@transaction.atomic
def unsplit_transaction(principal, txn_id, category_id):
    person = _person_for(principal)
    financial_transaction = (
        visible_rows(Transaction, person)
        .filter(pk=txn_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    if financial_transaction is None:
        raise PermissionDenied(_DENIED)
    if financial_transaction.category_source != Transaction.CategorySource.SPLIT:
        raise PermissionDenied(_DENIED)
    category = None
    if category_id:
        category = assignable_categories(person).filter(pk=category_id).first()
        if category is None:
            raise PermissionDenied(_DENIED)
    refunds = _linked_original_refunds(financial_transaction)
    lock_actor_household(person)
    locked = lock_owned_transactions([financial_transaction, *refunds])
    if not visible_rows(Transaction, person).filter(pk=financial_transaction.pk).exists():
        raise PermissionDenied(_DENIED)
    by_id = {item.pk: item for item in locked}
    financial_transaction = by_id[financial_transaction.pk]
    refunds = [by_id[item.pk] for item in refunds]
    previous_label = _split_history_label(_parts_with_categories(financial_transaction))
    RefundLink.objects.filter(original=financial_transaction).update(original_part=None)
    TransactionSplit.objects.filter(transaction=financial_transaction).delete()
    financial_transaction.category = category
    financial_transaction.category_source = Transaction.CategorySource.MANUAL
    financial_transaction.save(update_fields=("category", "category_source", "updated_at"))
    record_text_history(
        financial_transaction,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        previous_label,
        history_label(category),
    )
    following_ids = _following_refund_ids(person, refunds)
    for refund in refunds:
        if refund.pk in following_ids:
            apply_inherited_category(person, refund, category)
    refresh_household_transfer_pairs(person, [financial_transaction.pk])
    from finance.alert_services import schedule_after_category_change

    schedule_after_category_change()
    return financial_transaction


@transaction.atomic
def assign_split_part_category(principal, part_id, category_id):
    person = _person_for(principal)
    part = (
        TransactionSplit.objects.select_related("transaction", "transaction__account", "category")
        .filter(pk=part_id)
        .first()
    )
    if part is None:
        raise PermissionDenied(_DENIED)
    parent = part.transaction
    if not visible_rows(Transaction, person).filter(pk=parent.pk, status=Transaction.Status.ACTIVE).exists():
        raise PermissionDenied(_DENIED)
    if parent.category_source != Transaction.CategorySource.SPLIT:
        raise PermissionDenied(_DENIED)
    category = assignable_categories(person).filter(pk=category_id).first()
    if category is None:
        raise PermissionDenied(_DENIED)
    refunds = list(
        Transaction.objects.filter(
            refund_link__original_part=part,
            status=Transaction.Status.ACTIVE,
        ).select_related("account", "category")
    )
    lock_actor_household(person)
    locked = lock_owned_transactions([parent, *refunds])
    if not visible_rows(Transaction, person).filter(pk=parent.pk).exists():
        raise PermissionDenied(_DENIED)
    by_id = {item.pk: item for item in locked}
    parent = by_id[parent.pk]
    refunds = [by_id[item.pk] for item in refunds]
    part = TransactionSplit.objects.select_for_update().get(pk=part.pk)
    previous_label = _split_history_label(_parts_with_categories(parent))
    part.category = category
    part.save(update_fields=("category", "updated_at"))
    new_label = _split_history_label(_parts_with_categories(parent))
    record_text_history(
        parent,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        previous_label,
        new_label,
    )
    following_ids = _following_refund_ids(person, refunds)
    for refund in refunds:
        if refund.pk in following_ids:
            apply_inherited_category(person, refund, category)
    from finance.alert_services import schedule_after_category_change

    schedule_after_category_change()
    return part
