"""Household categories: seed, list, add, rename and assign."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .access import DENIED as _DENIED
from .access import require_person as _person_for
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
    person = _person_for(principal)
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
    person = _person_for(principal)
    financial_transaction = (
        visible_rows(Transaction, person)
        .filter(pk=transaction_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    if financial_transaction is None:
        raise PermissionDenied(_DENIED)
    if financial_transaction.category_source == Transaction.CategorySource.SPLIT:
        raise ValidationError(UNSPLIT_TO_CATEGORIZE)
    category = None
    if category_id:
        category = assignable_categories(person).filter(pk=category_id).first()
        if category is None:
            raise PermissionDenied(_DENIED)
    # Look up refunds under the household lock, which link_refund also takes first.
    lock_actor_household(person)
    linked_refunds = linked_refunds_for_originals(person, [financial_transaction])
    locked = lock_owned_transactions([financial_transaction, *linked_refunds])
    # Recheck after locking: the account may have been unshared between the
    # first visibility query and the locks.
    if not visible_rows(Transaction, person).filter(pk=financial_transaction.pk).exists():
        raise PermissionDenied(_DENIED)
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
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
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
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    category = visible_rows(Category, person).select_for_update().filter(pk=category_id).first()
    if category is None or category.code == Category.Code.TRANSFER:
        raise PermissionDenied(_DENIED)
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


# Stage 1 of #756: names that moved out stay importable from here until their callers move.
from .ledger_counting import excluded_transfer_ids as excluded_transfer_ids
from .ledger_counting import exclusion_exists_for as exclusion_exists_for
from .ledger_counting import income_and_spending_by_account as income_and_spending_by_account
from .ledger_counting import income_and_spending_by_tag as income_and_spending_by_tag
from .ledger_counting import income_and_spending_by_window as income_and_spending_by_window
from .ledger_counting import income_and_spending_totals as income_and_spending_totals
from .ledger_counting import spending_account_ids as spending_account_ids
from .ledger_counting import spending_by_category_by_window as spending_by_category_by_window
from .ledger_counting import with_transfer_state as with_transfer_state
from .ledger_edits import history_label as _history_label  # noqa: F401 - stage 1 re-export under the old private name (#756)
from .ledger_edits import lock_accounts_and_transactions as _lock_accounts_and_transactions  # noqa: F401 - stage 1 re-export under the old private name (#756)
from .ledger_edits import record_text_history as _record_text_history  # noqa: F401 - stage 1 re-export under the old private name (#756)
from .refund_links import REFUND_LINK_RULE as REFUND_LINK_RULE
from .refund_links import REFUND_LINK_STANDS as REFUND_LINK_STANDS
from .refund_links import SPLIT_PART_MISMATCH as SPLIT_PART_MISMATCH
from .refund_links import SPLIT_PART_REQUIRED as SPLIT_PART_REQUIRED
from .refund_links import account_ids_for_refund_link_rule as account_ids_for_refund_link_rule
from .refund_links import account_ids_in_refunds_touching_transactions as account_ids_in_refunds_touching_transactions
from .refund_links import drop_refund_links_to_originals as drop_refund_links_to_originals
from .refund_links import link_refund as link_refund
from .refund_links import reapply_refund_link_drops_for_account as reapply_refund_link_drops_for_account
from .refund_links import refund_rows_touching as _refund_rows_touching  # noqa: F401 - stage 1 re-export under the old private name (#756)
from .refund_links import restore_refund_categories as _restore_refund_categories  # noqa: F401 - stage 1 re-export under the old private name (#756)
from .refund_links import transaction_is_linked_refund as transaction_is_linked_refund
from .splits import SPLIT_AMOUNT_ERROR as SPLIT_AMOUNT_ERROR
from .splits import SPLIT_COUNT_ERROR as SPLIT_COUNT_ERROR
from .splits import SPLIT_REFUND_ERROR as SPLIT_REFUND_ERROR
from .splits import SPLIT_SIGN_ERROR as SPLIT_SIGN_ERROR
from .splits import SPLIT_SUM_ERROR as SPLIT_SUM_ERROR
from .splits import SPLIT_TRANSFER_ERROR as SPLIT_TRANSFER_ERROR
from .splits import assign_split_part_category as assign_split_part_category
from .splits import split_transaction as split_transaction
from .splits import unsplit_transaction as unsplit_transaction
from .transfer_pairing import account_ids_in_pairs_touching_transactions as account_ids_in_pairs_touching_transactions
from .transfer_pairing import confirm_transfer_pair as confirm_transfer_pair
from .transfer_pairing import dismiss_transfer_pair as dismiss_transfer_pair
from .transfer_pairing import downgrade_unevidenced_auto_marked_pairs as downgrade_unevidenced_auto_marked_pairs
from .transfer_pairing import pair_rows_touching as _pair_rows_touching  # noqa: F401 - stage 1 re-export under the old private name (#756)
from .transfer_pairing import refresh_transfer_pairs as refresh_transfer_pairs
from .transfer_pairing import revalidate_pairs_for_leg_ids as _revalidate_pairs_for_leg_ids  # noqa: F401 - stage 1 re-export under the old private name (#756)
from .transfer_pairing import revalidate_pairs_touching_account as revalidate_pairs_touching_account
from .transfer_pairing import revalidate_pairs_touching_import_batch as revalidate_pairs_touching_import_batch
from .transfer_pairing import set_transfer_window_days as set_transfer_window_days
from .transfer_pairing import transfer_matching_key as transfer_matching_key
from .transfer_pairing import undo_transfer_pair as undo_transfer_pair
from .transfer_pairing import unmark_locked_pairs as unmark_locked_pairs
