"""Household categories: seed, list, add, rename and assign."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .access import DENIED
from .access import require_person
from .access import visible_rows
from .audit_services import record
from .households import current_household
from .ledger_edits import history_label, lock_owned_transactions, record_text_history
from .lifecycle_services import lock_actor_household
from .models import AuditEvent, Category, Transaction, TransactionCorrectionHistory
from .refund_links import apply_inherited_category, linked_refunds_for_originals


UNSPLIT_TO_CATEGORIZE = "Unsplit to assign a single category."

STARTER_CUSTOM_NAMES = (
    "Income",
    "Groceries",
    "Dining",
    "Transportation",
    "Housing",
    "Utilities",
    "Health",
    "Insurance",
    "Shopping",
    "Entertainment",
    "Subscriptions",
    "Travel",
    "Education",
    "Personal care",
    "Gifts and donations",
    "Fees and interest",
    "Taxes",
)


def ensure_household_categories(household):
    existing = list(household.categories.all())
    system_codes = {category.code for category in existing if category.code != Category.Code.CUSTOM}
    # Starter names are seeded once. Categories can be renamed but not deleted,
    # so any custom category means the household already has its own list.
    starters_seeded = Category.Code.CUSTOM in {category.code for category in existing}
    created = []
    if Category.Code.UNCATEGORIZED not in system_codes:
        created.append(
            Category(household=household, name="Uncategorized", code=Category.Code.UNCATEGORIZED)
        )
    if Category.Code.TRANSFER not in system_codes:
        created.append(Category(household=household, name="Transfer", code=Category.Code.TRANSFER))
    if not starters_seeded:
        taken = {category.name for category in existing}
        created.extend(
            Category(household=household, name=name, code=Category.Code.CUSTOM)
            for name in STARTER_CUSTOM_NAMES
            if name not in taken
        )
    if created:
        Category.objects.bulk_create(created)
    return household.categories.order_by("name", "pk")


def assignable_categories(principal):
    person = require_person(principal)
    household = current_household(person)
    if household is None:
        return Category.objects.none()
    ensure_household_categories(household)
    return visible_rows(Category, person).exclude(code=Category.Code.TRANSFER).order_by("name", "pk")


def apply_manual_category(person, txn, category):
    previous = txn.category
    txn.category = category
    txn.category_source = Transaction.CategorySource.MANUAL
    txn.save(update_fields=("category", "category_source", "updated_at"))
    record_text_history(
        txn,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        history_label(previous),
        history_label(category),
    )


@transaction.atomic
def assign_category(principal, transaction_id, category_id):
    person = require_person(principal)
    financial_transaction = (
        visible_rows(Transaction, person)
        .filter(pk=transaction_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    if financial_transaction is None:
        raise PermissionDenied(DENIED)
    if financial_transaction.category_source == Transaction.CategorySource.SPLIT:
        raise ValidationError(UNSPLIT_TO_CATEGORIZE)
    category = None
    if category_id:
        category = assignable_categories(person).filter(pk=category_id).first()
        if category is None:
            raise PermissionDenied(DENIED)
    # Look up refunds under the household lock, which link_refund also takes first.
    lock_actor_household(person)
    linked_refunds = linked_refunds_for_originals(person, [financial_transaction])
    locked = lock_owned_transactions([financial_transaction, *linked_refunds])
    # Recheck after locking: the account may have been unshared between the
    # first visibility query and the locks.
    if not visible_rows(Transaction, person).filter(pk=financial_transaction.pk).exists():
        raise PermissionDenied(DENIED)
    by_id = {item.pk: item for item in locked}
    financial_transaction = by_id[financial_transaction.pk]
    apply_manual_category(person, financial_transaction, category)
    for refund in linked_refunds:
        apply_inherited_category(person, by_id[refund.pk], category)
    from finance.alert_services import schedule_after_category_change

    schedule_after_category_change()
    return financial_transaction


@transaction.atomic
def add_category(principal, name):
    person = require_person(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(DENIED)
    lock_actor_household(person)
    ensure_household_categories(household)
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("Enter a category name.")
    if Category.objects.filter(household=household, name=cleaned).exists():
        raise ValidationError("A category with that name already exists.")
    category = Category.objects.create(household=household, name=cleaned, code=Category.Code.CUSTOM)
    record(person, AuditEvent.Action.RECORD_CREATED, AuditEvent.TargetType.CATEGORY, category.pk,
           audience={"household": household})
    return category


@transaction.atomic
def rename_category(principal, category_id, name):
    person = require_person(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(DENIED)
    lock_actor_household(person)
    category = visible_rows(Category, person).select_for_update().filter(pk=category_id).first()
    if category is None or category.code == Category.Code.TRANSFER:
        raise PermissionDenied(DENIED)
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("Enter a category name.")
    if Category.objects.filter(household=household).exclude(pk=category.pk).filter(name=cleaned).exists():
        raise ValidationError("A category with that name already exists.")
    renamed = category.name != cleaned
    category.name = cleaned
    category.save(update_fields=("name", "updated_at"))
    if renamed:
        record(person, AuditEvent.Action.RECORD_EDITED, AuditEvent.TargetType.CATEGORY, category.pk,
               audience={"household": household}, fields=("name",))
    return category

