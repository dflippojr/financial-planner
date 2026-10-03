from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .lifecycle_services import lock_actor_household
from .models import (
    Account,
    Category,
    Membership,
    Person,
    RefundLink,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
    TransferPair,
)


_DENIED = "Operation is not permitted."
REFUND_LINK_RULE = (
    "A refund must be a positive amount linked to a negative original purchase of the same kind."
)
SPLIT_TRANSFER_ERROR = "Unpair the transfer before splitting."
SPLIT_REFUND_ERROR = "Unlink the refund before splitting."
SPLIT_SUM_ERROR = "Parts must add up exactly to the transaction amount."
SPLIT_SIGN_ERROR = "Each part must be non-zero and have the same sign as the transaction."
SPLIT_COUNT_ERROR = "A split needs at least two parts."
SPLIT_AMOUNT_ERROR = "Unsplit to change the amount"
SPLIT_PART_REQUIRED = "Choose a split part for each linked refund."
SPLIT_PART_MISMATCH = "A refund linked to a split purchase must use a part of that purchase."
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

HIGH_CONFIDENCE_UNIQUE_BOTH = "only candidate for both legs in the window"
LOW_CONFIDENCE_MULTIPLE = "multiple possible counterparts in the window"


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist:
            pass
    raise PermissionDenied(_DENIED)


def current_household(person):
    membership = (
        Membership.objects.filter(person=person, ended_at__isnull=True)
        .select_related("household")
        .first()
    )
    return None if membership is None else membership.household


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
    return Category.objects.visible_to(person).exclude(code=Category.Code.TRANSFER).order_by("name", "pk")


def exclusion_exists_for(principal):
    visible = Transaction.objects.visible_to(principal).values("pk")
    return Exists(
        TransferPair.objects.excluding_income_and_spending().filter(
            Q(leg_a_id=OuterRef("pk")) | Q(leg_b_id=OuterRef("pk")),
            leg_a_id__in=visible,
            leg_b_id__in=visible,
        )
    )


def _history_label(category):
    return "Uncategorized" if category is None else category.name


def _record_text_history(transaction, actor, field_name, previous, new):
    if previous == new:
        return
    TransactionCorrectionHistory.objects.create(
        transaction=transaction,
        actor=actor,
        recorded_at=timezone.now(),
        field_name=field_name,
        previous_description=previous,
        new_description=new,
    )


def _lock_accounts_and_transactions(person, transactions):
    lock_actor_household(person)
    account_ids = sorted({item.account_id for item in transactions})
    list(Account.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
    ids = sorted(item.pk for item in transactions)
    locked = list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "category")
        .filter(pk__in=ids, status=Transaction.Status.ACTIVE)
        .order_by("pk")
    )
    if len(locked) != len(ids):
        raise PermissionDenied(_DENIED)
    visible = set(Transaction.objects.visible_to(person).filter(pk__in=ids).values_list("pk", flat=True))
    if visible != set(ids):
        raise PermissionDenied(_DENIED)
    return locked


def _lock_owned_transactions(transactions):
    account_ids = sorted({item.account_id for item in transactions})
    list(Account.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
    ids = sorted(item.pk for item in transactions)
    locked = list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "category")
        .filter(pk__in=ids, status=Transaction.Status.ACTIVE)
        .order_by("pk")
    )
    if len(locked) != len(ids):
        raise PermissionDenied(_DENIED)
    return locked


@transaction.atomic
def assign_category(principal, transaction_id, category_id):
    person = _person_for(principal)
    financial_transaction = (
        Transaction.objects.visible_to(person)
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
    linked_refunds = list(
        Transaction.objects.filter(
            refund_link__original=financial_transaction,
            status=Transaction.Status.ACTIVE,
        ).select_related("account", "category")
    )
    lock_actor_household(person)
    locked = _lock_owned_transactions([financial_transaction, *linked_refunds])
    # Recheck after locking: the account may have been unshared between the
    # first visibility query and the locks.
    if not Transaction.objects.visible_to(person).filter(pk=financial_transaction.pk).exists():
        raise PermissionDenied(_DENIED)
    by_id = {item.pk: item for item in locked}
    financial_transaction = by_id[financial_transaction.pk]
    previous = financial_transaction.category
    financial_transaction.category = category
    financial_transaction.category_source = Transaction.CategorySource.MANUAL
    financial_transaction.save(update_fields=("category", "category_source", "updated_at"))
    _record_text_history(
        financial_transaction,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        _history_label(previous),
        _history_label(category),
    )
    for refund in linked_refunds:
        locked_refund = by_id[refund.pk]
        previous_refund_category = locked_refund.category
        locked_refund.category = category
        locked_refund.category_source = Transaction.CategorySource.INHERITED
        locked_refund.save(update_fields=("category", "category_source", "updated_at"))
        _record_text_history(
            locked_refund,
            person,
            TransactionCorrectionHistory.Field.CATEGORY,
            _history_label(previous_refund_category),
            _history_label(category),
        )
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
    if household.categories.filter(name=cleaned).exists():
        raise ValidationError("A category with that name already exists.")
    return Category.objects.create(household=household, name=cleaned, code=Category.Code.CUSTOM)


@transaction.atomic
def rename_category(principal, category_id, name):
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    category = Category.objects.visible_to(person).select_for_update().filter(pk=category_id).first()
    if category is None or category.code == Category.Code.TRANSFER:
        raise PermissionDenied(_DENIED)
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("Enter a category name.")
    if household.categories.exclude(pk=category.pk).filter(name=cleaned).exists():
        raise ValidationError("A category with that name already exists.")
    category.name = cleaned
    category.save(update_fields=("name", "updated_at"))
    return category


@transaction.atomic
def set_transfer_window_days(principal, days):
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    if days < 0 or days > 366:
        raise ValidationError("Choose a match window between 0 and 366 days.")
    household.transfer_match_window_days = days
    household.save(update_fields=("transfer_match_window_days", "updated_at"))
    # The window applies to every member's pairs, including pairs between a
    # member's private accounts that the actor cannot see.
    member_ids = Membership.objects.filter(household=household, ended_at__isnull=True).values_list(
        "person_id", flat=True
    )
    for member in Person.objects.filter(pk__in=list(member_ids)).order_by("pk"):
        refresh_transfer_pairs(member, actor=person)
    return household


def _refresh_household_transfer_pairs(person):
    household = current_household(person)
    if household is None:
        refresh_transfer_pairs(person, actor=person)
        return
    member_ids = Membership.objects.filter(household=household, ended_at__isnull=True).values_list(
        "person_id", flat=True
    )
    for member in Person.objects.filter(pk__in=list(member_ids)).order_by("pk"):
        refresh_transfer_pairs(member, actor=person)


def _accounts_share_a_viewer(account_a, account_b):
    if account_a.pk == account_b.pk:
        return False
    private = Account.Scope.PRIVATE
    household = Account.Scope.HOUSEHOLD
    if account_a.scope == private and account_b.scope == private:
        return account_a.owner_id == account_b.owner_id
    if account_a.scope == household and account_b.scope == household:
        return account_a.household_id is not None and account_a.household_id == account_b.household_id
    shared = account_a if account_a.scope == household else account_b
    owned = account_b if account_a.scope == household else account_a
    if owned.scope != private or shared.scope != household or shared.household_id is None:
        return False
    return Membership.objects.filter(
        person_id=owned.owner_id,
        household_id=shared.household_id,
        ended_at__isnull=True,
    ).exists()


def _window_days(account_a, account_b):
    for account in (account_a, account_b):
        if account.scope == Account.Scope.HOUSEHOLD and account.household_id:
            return account.household.transfer_match_window_days
    owner_id = account_a.owner_id
    membership = (
        Membership.objects.filter(person_id=owner_id, ended_at__isnull=True)
        .select_related("household")
        .first()
    )
    if membership is not None:
        return membership.household.transfer_match_window_days
    return settings.TRANSFER_MATCH_WINDOW_DAYS


def _pair_kind(tx_a, tx_b):
    types = {tx_a.account.account_type, tx_b.account.account_type}
    if Account.Type.CREDIT_CARD not in types or len(types) == 1:
        return TransferPair.Kind.TRANSFER
    card = tx_a if tx_a.account.account_type == Account.Type.CREDIT_CARD else tx_b
    payer = tx_b if card is tx_a else tx_a
    if payer.amount_minor < 0 and card.amount_minor > 0:
        return TransferPair.Kind.CARD_PAYMENT
    return TransferPair.Kind.TRANSFER


def _ordered_legs(tx_a, tx_b):
    return (tx_a, tx_b) if tx_a.pk < tx_b.pk else (tx_b, tx_a)


def _amounts_and_accounts_can_pair(tx_a, tx_b):
    if tx_a.pk == tx_b.pk or tx_a.account_id == tx_b.account_id:
        return False
    if tx_a.amount_minor == 0 or tx_a.amount_minor + tx_b.amount_minor != 0:
        return False
    return _accounts_share_a_viewer(tx_a.account, tx_b.account)


def _is_split(txn):
    return txn.category_source == Transaction.CategorySource.SPLIT


def _is_candidate(tx_a, tx_b):
    if _is_split(tx_a) or _is_split(tx_b):
        return False
    if not _amounts_and_accounts_can_pair(tx_a, tx_b):
        return False
    window = _window_days(tx_a.account, tx_b.account)
    return abs((tx_a.transaction_date - tx_b.transaction_date).days) <= window


def _candidate_pairs(transactions):
    by_abs = defaultdict(list)
    for item in transactions:
        by_abs[abs(item.amount_minor)].append(item)
    raw_pairs = []
    for group in by_abs.values():
        for index, left in enumerate(group):
            raw_pairs.extend(_ordered_legs(left, right) for right in group[index + 1 :] if _is_candidate(left, right))
    return raw_pairs


def _scored_pair(left, right, unique):
    window = _window_days(left.account, right.account)
    return {
        "left": left,
        "right": right,
        "confidence": TransferPair.Confidence.HIGH if unique else TransferPair.Confidence.LOW,
        "kind": _pair_kind(left, right),
        "reasons": [
            "exact opposite amounts",
            f"dates within {window} days",
            "both accounts visible to the same person",
            HIGH_CONFIDENCE_UNIQUE_BOTH if unique else LOW_CONFIDENCE_MULTIPLE,
        ],
    }


def _score_pairs(transactions):
    raw_pairs = _candidate_pairs(transactions)
    counts = defaultdict(int)
    for left, right in raw_pairs:
        counts[left.pk] += 1
        counts[right.pk] += 1
    return [
        _scored_pair(left, right, counts[left.pk] == 1 and counts[right.pk] == 1)
        for left, right in raw_pairs
    ]


def _occupied_transaction_ids(pairs):
    occupied = set()
    for pair in pairs:
        if pair.status in (
            TransferPair.Status.SUGGESTED,
            TransferPair.Status.AUTO_MARKED,
            TransferPair.Status.CONFIRMED,
        ):
            occupied.add(pair.leg_a_id)
            occupied.add(pair.leg_b_id)
    return occupied


def _snapshot_and_mark(pair, left, right, status, actor):
    pair.leg_a_category_id_at_mark = left.category_id
    pair.leg_b_category_id_at_mark = right.category_id
    pair.status = status
    pair.save(
        update_fields=(
            "status",
            "confidence",
            "kind",
            "reasons",
            "leg_a_category_id_at_mark",
            "leg_b_category_id_at_mark",
            "updated_at",
        )
    )
    label = "card payment" if pair.kind == TransferPair.Kind.CARD_PAYMENT else "transfer"
    _record_text_history(left, actor, TransactionCorrectionHistory.Field.EXCLUSION, "included", f"excluded:{label}")
    _record_text_history(right, actor, TransactionCorrectionHistory.Field.EXCLUSION, "included", f"excluded:{label}")


_SETTLED_PAIR_STATUSES = (
    TransferPair.Status.DISMISSED,
    TransferPair.Status.UNDONE,
    TransferPair.Status.CONFIRMED,
    TransferPair.Status.AUTO_MARKED,
)


def _lock_visible_transactions(person):
    transactions = list(
        Transaction.objects.visible_to(person)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "account__household", "account__owner")
    )
    if not transactions:
        return []
    account_ids = sorted({item.account_id for item in transactions})
    list(Account.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
    tx_ids = sorted(item.pk for item in transactions)
    return list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "account__household", "account__owner", "category")
        .filter(pk__in=tx_ids)
        .order_by("pk")
    )


def _update_existing_pair(pair, scored, person):
    if pair.status in _SETTLED_PAIR_STATUSES:
        return
    pair.confidence = scored["confidence"]
    pair.kind = scored["kind"]
    pair.reasons = scored["reasons"]
    if scored["confidence"] == TransferPair.Confidence.HIGH:
        _snapshot_and_mark(pair, scored["left"], scored["right"], TransferPair.Status.AUTO_MARKED, person)
    else:
        pair.save(update_fields=("confidence", "kind", "reasons", "updated_at"))


def _restore_refund_categories(original, actor):
    """Keep linked refunds in their original's category, as assign_category does."""
    refunds = (
        Transaction.objects.select_for_update(of=("self",))
        .select_related("category")
        .filter(refund_link__original=original, status=Transaction.Status.ACTIVE)
        .exclude(category_id=original.category_id)
        .order_by("pk")
    )
    new_category = None if original.category_id is None else Category.objects.get(pk=original.category_id)
    for refund in refunds:
        previous = refund.category
        refund.category = new_category
        refund.category_source = Transaction.CategorySource.INHERITED
        refund.save(update_fields=("category", "category_source", "updated_at"))
        _record_text_history(
            refund,
            actor,
            TransactionCorrectionHistory.Field.CATEGORY,
            _history_label(previous),
            _history_label(new_category),
        )


def _restore_snapshot_category(leg, category_id, actor):
    if leg.category_id == category_id:
        return
    previous = leg.category
    restored = None if category_id is None else Category.objects.get(pk=category_id)
    leg.category = restored
    leg.save(update_fields=("category", "updated_at"))
    _record_text_history(
        leg,
        actor,
        TransactionCorrectionHistory.Field.CATEGORY,
        _history_label(previous),
        _history_label(restored),
    )


def _unmark_exclusion(pair, left, right, actor):
    _restore_snapshot_category(left, pair.leg_a_category_id_at_mark, actor)
    _restore_snapshot_category(right, pair.leg_b_category_id_at_mark, actor)
    _restore_refund_categories(left, actor)
    _restore_refund_categories(right, actor)
    pair.status = TransferPair.Status.UNDONE
    pair.save(update_fields=("status", "updated_at"))
    _record_text_history(left, actor, TransactionCorrectionHistory.Field.EXCLUSION, "excluded", "included")
    _record_text_history(right, actor, TransactionCorrectionHistory.Field.EXCLUSION, "excluded", "included")


def _invalidate_suggestion(pair):
    pair.status = TransferPair.Status.UNDONE
    pair.save(update_fields=("status", "updated_at"))


def _pair_legs(pair, locked_by_id):
    missing_ids = [pk for pk in (pair.leg_a_id, pair.leg_b_id) if pk not in locked_by_id]
    extra = {}
    if missing_ids:
        extra = {
            item.pk: item
            for item in Transaction.objects.select_for_update(of=("self",))
            .select_related("account", "category")
            .filter(pk__in=missing_ids)
            .order_by("pk")
        }
    return locked_by_id.get(pair.leg_a_id) or extra.get(pair.leg_a_id), locked_by_id.get(pair.leg_b_id) or extra.get(
        pair.leg_b_id
    )


def _both_legs_active(left, right):
    return (
        left is not None
        and right is not None
        and left.status == Transaction.Status.ACTIVE
        and right.status == Transaction.Status.ACTIVE
    )


def _legs_still_cancel(left, right):
    return _both_legs_active(left, right) and _is_candidate(left, right)


def _confirmed_pair_still_holds(left, right):
    if left is None or right is None or _is_split(left) or _is_split(right):
        return False
    return _both_legs_active(left, right) and _amounts_and_accounts_can_pair(left, right)


def _pair_survives_revalidation(pair, left, right):
    if pair.status == TransferPair.Status.CONFIRMED:
        return _confirmed_pair_still_holds(left, right)
    return _legs_still_cancel(left, right)


def _revalidate_marked_pairs(existing, locked_by_id, person):
    for pair in existing.values():
        if pair.status not in (
            TransferPair.Status.AUTO_MARKED,
            TransferPair.Status.CONFIRMED,
            TransferPair.Status.SUGGESTED,
        ):
            continue
        left, right = _pair_legs(pair, locked_by_id)
        if _pair_survives_revalidation(pair, left, right):
            continue
        if pair.status == TransferPair.Status.SUGGESTED:
            _invalidate_suggestion(pair)
        elif left is not None and right is not None:
            _unmark_exclusion(pair, left, right, person)
        else:
            pair.status = TransferPair.Status.UNDONE
            pair.save(update_fields=("status", "updated_at"))


def _pair_rows_touching(leg_ids):
    if not leg_ids:
        return []
    return list(
        TransferPair.objects.filter(Q(leg_a_id__in=leg_ids) | Q(leg_b_id__in=leg_ids)).values_list(
            "pk", "leg_a_id", "leg_b_id"
        )
    )


def account_ids_in_pairs_touching_transactions(transaction_ids):
    """Account pks for every leg of pairs that include any of these transactions."""
    rows = _pair_rows_touching(transaction_ids)
    if not rows:
        return set()
    all_legs = {leg_id for _pk, left_id, right_id in rows for leg_id in (left_id, right_id)}
    return set(Transaction.objects.filter(pk__in=all_legs).values_list("account_id", flat=True))


def _refund_rows_touching(transaction_ids):
    if not transaction_ids:
        return []
    return list(
        RefundLink.objects.filter(Q(refund_id__in=transaction_ids) | Q(original_id__in=transaction_ids)).values_list(
            "pk", "refund_id", "original_id"
        )
    )


def account_ids_in_refunds_touching_transactions(transaction_ids):
    """Account pks for every side of refund links that include any of these transactions."""
    rows = _refund_rows_touching(transaction_ids)
    if not rows:
        return set()
    all_ids = {tx_id for _pk, refund_id, original_id in rows for tx_id in (refund_id, original_id)}
    return set(Transaction.objects.filter(pk__in=all_ids).values_list("account_id", flat=True))


_MARKED_PAIR_STATUSES = (TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED)


def unmark_locked_pairs(pairs, locked_by_id, person):
    """Restore snapshot categories on marked pairs. Caller holds the row locks."""
    for pair in pairs:
        if pair.status not in _MARKED_PAIR_STATUSES:
            continue
        left, right = _pair_legs(pair, locked_by_id)
        if left is not None and right is not None:
            _unmark_exclusion(pair, left, right, person)
        else:
            pair.status = TransferPair.Status.UNDONE
            pair.save(update_fields=("status", "updated_at"))


def _revalidate_pairs_for_leg_ids(person, seed_leg_ids):
    rows = _pair_rows_touching(seed_leg_ids)
    if not rows:
        return
    all_leg_ids = sorted({leg_id for _pk, left_id, right_id in rows for leg_id in (left_id, right_id)})
    account_ids = sorted(set(Transaction.objects.filter(pk__in=all_leg_ids).values_list("account_id", flat=True)))
    list(Account.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
    locked = list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "account__household", "account__owner", "category")
        .filter(pk__in=all_leg_ids)
        .order_by("pk")
    )
    pair_ids = sorted(pk for pk, _left, _right in rows)
    existing = {
        (pair.leg_a_id, pair.leg_b_id): pair
        for pair in TransferPair.objects.select_for_update(of=("self",)).filter(pk__in=pair_ids).order_by("pk")
    }
    _revalidate_marked_pairs(existing, {item.pk: item for item in locked}, person)


@transaction.atomic
def revalidate_pairs_touching_account(principal, account_id):
    person = _person_for(principal)
    lock_actor_household(person)
    seed = list(Transaction.objects.filter(account_id=account_id).values_list("pk", flat=True))
    _revalidate_pairs_for_leg_ids(person, seed)


@transaction.atomic
def revalidate_pairs_touching_import_batch(principal, batch_id):
    person = _person_for(principal)
    lock_actor_household(person)
    seed = list(Transaction.objects.filter(import_batch_id=batch_id).values_list("pk", flat=True))
    _revalidate_pairs_for_leg_ids(person, seed)


@transaction.atomic
def refresh_transfer_pairs(principal, *, actor=None):
    person = _person_for(principal)
    actor = person if actor is None else actor
    lock_actor_household(person)
    locked = _lock_visible_transactions(person)
    if not locked:
        return []
    tx_ids = [item.pk for item in locked]
    existing = {
        (pair.leg_a_id, pair.leg_b_id): pair
        for pair in TransferPair.objects.select_for_update(of=("self",)).filter(
            Q(leg_a_id__in=tx_ids) | Q(leg_b_id__in=tx_ids)
        )
    }
    locked_by_id = {item.pk: item for item in locked}
    _revalidate_marked_pairs(existing, locked_by_id, actor)
    occupied = _occupied_transaction_ids(existing.values())
    created = []
    scored_pairs = _score_pairs(locked)
    scored_pairs.sort(
        key=lambda item: (
            0 if item["confidence"] == TransferPair.Confidence.HIGH else 1,
            item["left"].pk,
            item["right"].pk,
        )
    )
    for scored in scored_pairs:
        left, right = scored["left"], scored["right"]
        key = (left.pk, right.pk)
        if key in existing:
            _update_existing_pair(existing[key], scored, actor)
            continue
        if left.pk in occupied or right.pk in occupied:
            continue
        pair = TransferPair(
            leg_a=left,
            leg_b=right,
            status=TransferPair.Status.SUGGESTED,
            kind=scored["kind"],
            confidence=scored["confidence"],
            reasons=scored["reasons"],
        )
        pair.save()
        existing[key] = pair
        occupied.add(left.pk)
        occupied.add(right.pk)
        if scored["confidence"] == TransferPair.Confidence.HIGH:
            _snapshot_and_mark(pair, left, right, TransferPair.Status.AUTO_MARKED, actor)
        created.append(pair)
    return created


def _visible_pair(principal, pair_id):
    person = _person_for(principal)
    pair = (
        TransferPair.objects.visible_to(person)
        .select_related("leg_a", "leg_b", "leg_a__account", "leg_b__account", "leg_a__category", "leg_b__category")
        .filter(pk=pair_id)
        .first()
    )
    if pair is None:
        raise PermissionDenied(_DENIED)
    return person, pair


@transaction.atomic
def confirm_transfer_pair(principal, pair_id):
    person, pair = _visible_pair(principal, pair_id)
    if pair.status != TransferPair.Status.SUGGESTED:
        raise PermissionDenied(_DENIED)
    locked = _lock_accounts_and_transactions(person, [pair.leg_a, pair.leg_b])
    left, right = locked[0], locked[1]
    if not _is_candidate(left, right):
        raise PermissionDenied(_DENIED)
    pair = TransferPair.objects.select_for_update(of=("self",)).get(pk=pair.pk)
    _snapshot_and_mark(pair, left, right, TransferPair.Status.CONFIRMED, person)
    return pair


@transaction.atomic
def dismiss_transfer_pair(principal, pair_id):
    person, pair = _visible_pair(principal, pair_id)
    if pair.status != TransferPair.Status.SUGGESTED:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    pair = TransferPair.objects.select_for_update(of=("self",)).get(pk=pair.pk)
    pair.status = TransferPair.Status.DISMISSED
    pair.save(update_fields=("status", "updated_at"))
    return pair


@transaction.atomic
def undo_transfer_pair(principal, pair_id):
    person, pair = _visible_pair(principal, pair_id)
    if pair.status not in (TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED):
        raise PermissionDenied(_DENIED)
    locked = _lock_accounts_and_transactions(person, [pair.leg_a, pair.leg_b])
    by_id = {item.pk: item for item in locked}
    left = by_id[pair.leg_a_id]
    right = by_id[pair.leg_b_id]
    pair = TransferPair.objects.select_for_update(of=("self",)).get(pk=pair.pk)
    _unmark_exclusion(pair, left, right, person)
    return pair


@transaction.atomic
def _split_amount_display(amount_minor):
    amount = Decimal(abs(amount_minor)) / Decimal(100)
    return f"${amount:,.2f}"


def _split_history_label(parts):
    bits = [f"{part.category.name} {_split_amount_display(part.amount_minor)}" for part in parts]
    return "Split: " + ", ".join(bits)


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
        RefundLink.objects.visible_to(person)
        .filter(
            original=parent,
            refund_id__in=Transaction.objects.visible_to(person)
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


def _inherit_refund_part(person, refund, part):
    previous = refund.category
    refund.category = part.category
    refund.category_source = Transaction.CategorySource.INHERITED
    refund.save(update_fields=("category", "category_source", "updated_at"))
    link = refund.refund_link
    link.original_part = part
    link.save(update_fields=("original_part",))
    _record_text_history(
        refund,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        _history_label(previous),
        _history_label(part.category),
    )


def _apply_refund_assignments(person, visible_refunds, hidden_refunds, created_parts, refund_assignments):
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
        _inherit_refund_part(person, refund, assigned[refund.pk])
    for refund in hidden_refunds:
        _inherit_refund_part(person, refund, default_part)


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
        Transaction.objects.visible_to(person)
        .filter(pk=txn_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    if financial_transaction is None:
        raise PermissionDenied(_DENIED)
    refunds = _linked_original_refunds(financial_transaction)
    lock_actor_household(person)
    locked = _lock_owned_transactions([financial_transaction, *refunds])
    if not Transaction.objects.visible_to(person).filter(pk=financial_transaction.pk).exists():
        raise PermissionDenied(_DENIED)
    by_id = {item.pk: item for item in locked}
    financial_transaction = by_id[financial_transaction.pk]
    refunds = [by_id[item.pk] for item in refunds]
    visible_refunds, hidden_refunds = _partition_linked_refunds(person, refunds, financial_transaction)
    if financial_transaction.is_excluded_transfer:
        raise ValidationError(SPLIT_TRANSFER_ERROR)
    if RefundLink.objects.filter(refund=financial_transaction).exists():
        raise ValidationError(SPLIT_REFUND_ERROR)
    normalized = _normalized_parts(person, financial_transaction, parts)
    previous_label = (
        _split_history_label(list(financial_transaction.splits.select_related("category")))
        if financial_transaction.category_source == Transaction.CategorySource.SPLIT
        else _history_label(financial_transaction.category)
    )
    created_parts = _replace_splits(financial_transaction, normalized)
    financial_transaction.category = None
    financial_transaction.category_source = Transaction.CategorySource.SPLIT
    financial_transaction.save(update_fields=("category", "category_source", "updated_at"))
    _apply_refund_assignments(person, visible_refunds, hidden_refunds, created_parts, refund_assignments)
    new_label = _split_history_label([created_parts[index] for index, _, _ in normalized])
    _record_text_history(
        financial_transaction,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        previous_label,
        new_label,
    )
    _refresh_household_transfer_pairs(person)
    from finance.alert_services import schedule_after_category_change

    schedule_after_category_change()
    return financial_transaction


@transaction.atomic
def unsplit_transaction(principal, txn_id, category_id):
    person = _person_for(principal)
    financial_transaction = (
        Transaction.objects.visible_to(person)
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
    locked = _lock_owned_transactions([financial_transaction, *refunds])
    if not Transaction.objects.visible_to(person).filter(pk=financial_transaction.pk).exists():
        raise PermissionDenied(_DENIED)
    by_id = {item.pk: item for item in locked}
    financial_transaction = by_id[financial_transaction.pk]
    refunds = [by_id[item.pk] for item in refunds]
    previous_label = _split_history_label(list(financial_transaction.splits.select_related("category")))
    RefundLink.objects.filter(original=financial_transaction).update(original_part=None)
    financial_transaction.splits.all().delete()
    financial_transaction.category = category
    financial_transaction.category_source = Transaction.CategorySource.MANUAL
    financial_transaction.save(update_fields=("category", "category_source", "updated_at"))
    _record_text_history(
        financial_transaction,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        previous_label,
        _history_label(category),
    )
    for refund in refunds:
        previous = refund.category
        refund.category = category
        refund.category_source = Transaction.CategorySource.INHERITED
        refund.save(update_fields=("category", "category_source", "updated_at"))
        _record_text_history(
            refund,
            person,
            TransactionCorrectionHistory.Field.CATEGORY,
            _history_label(previous),
            _history_label(category),
        )
    _refresh_household_transfer_pairs(person)
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
    if not Transaction.objects.visible_to(person).filter(pk=parent.pk, status=Transaction.Status.ACTIVE).exists():
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
    locked = _lock_owned_transactions([parent, *refunds])
    if not Transaction.objects.visible_to(person).filter(pk=parent.pk).exists():
        raise PermissionDenied(_DENIED)
    by_id = {item.pk: item for item in locked}
    parent = by_id[parent.pk]
    refunds = [by_id[item.pk] for item in refunds]
    part = TransactionSplit.objects.select_for_update().get(pk=part.pk)
    previous_label = _split_history_label(list(parent.splits.select_related("category")))
    part.category = category
    part.save(update_fields=("category", "updated_at"))
    new_label = _split_history_label(list(parent.splits.select_related("category")))
    _record_text_history(
        parent,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        previous_label,
        new_label,
    )
    for refund in refunds:
        previous = refund.category
        refund.category = category
        refund.category_source = Transaction.CategorySource.INHERITED
        refund.save(update_fields=("category", "category_source", "updated_at"))
        _record_text_history(
            refund,
            person,
            TransactionCorrectionHistory.Field.CATEGORY,
            _history_label(previous),
            _history_label(category),
        )
    from finance.alert_services import schedule_after_category_change

    schedule_after_category_change()
    return part


@transaction.atomic
def link_refund(principal, refund_id, original_id, original_part_id=None):
    person = _person_for(principal)
    if refund_id == original_id:
        raise PermissionDenied(_DENIED)
    refund = (
        Transaction.objects.visible_to(person)
        .filter(pk=refund_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    original = (
        Transaction.objects.visible_to(person)
        .filter(pk=original_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    if refund is None or original is None:
        raise PermissionDenied(_DENIED)
    locked = _lock_accounts_and_transactions(person, [refund, original])
    by_id = {item.pk: item for item in locked}
    refund = by_id[refund_id]
    original = by_id[original_id]
    if RefundLink.objects.filter(refund=refund).exists():
        raise PermissionDenied(_DENIED)
    if (
        refund.amount_minor <= 0
        or original.amount_minor >= 0
        or refund.kind != original.kind
    ):
        raise ValidationError(REFUND_LINK_RULE)
    inherited = original.category
    original_part = None
    if original.category_source == Transaction.CategorySource.SPLIT:
        if original_part_id is None:
            raise ValidationError(SPLIT_PART_REQUIRED)
        original_part = original.splits.filter(pk=original_part_id).select_related("category").first()
        if original_part is None:
            raise ValidationError(SPLIT_PART_MISMATCH)
        inherited = original_part.category
    elif original_part_id is not None:
        raise ValidationError(SPLIT_PART_MISMATCH)
    previous_category = refund.category
    refund.category = inherited
    refund.category_source = Transaction.CategorySource.INHERITED
    refund.save(update_fields=("category", "category_source", "updated_at"))
    RefundLink.objects.create(refund=refund, original=original, original_part=original_part)
    _record_text_history(
        refund,
        person,
        TransactionCorrectionHistory.Field.REFUND_LINK,
        "unlinked",
        "linked",
    )
    _record_text_history(
        refund,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        _history_label(previous_category),
        _history_label(inherited),
    )
    return refund


def income_and_spending_totals(principal, *, date_from=None, date_to=None, accounts=None):
    """Access-filtered income and spending for later cash-flow views.

    Transfers are excluded only when both legs are visible. Linked refunds are
    never income; they reduce spending from the refund's own stored category and
    amount even when the original purchase is no longer visible. Unverified
    investment activity is omitted. Optional `accounts` must already be visible;
    ids the viewer cannot see are dropped rather than queried.
    """
    person = _person_for(principal)
    visible_accounts = Account.objects.visible_to(person)
    if accounts is not None:
        visible_accounts = visible_accounts.filter(pk__in=[getattr(item, "pk", item) for item in accounts])
    transactions = (
        Transaction.objects.visible_to(person)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            account_id__in=visible_accounts.values("pk"),
        )
        .select_related("category")
    )
    if date_from:
        transactions = transactions.filter(transaction_date__gte=date_from)
    if date_to:
        transactions = transactions.filter(transaction_date__lte=date_to)

    excluded = {
        tx_id
        for pair in TransferPair.objects.excluding_income_and_spending().visible_to(person)
        for tx_id in (pair.leg_a_id, pair.leg_b_id)
    }
    rows = list(transactions)
    refunds = set(
        RefundLink.objects.filter(refund_id__in=[item.pk for item in rows]).values_list("refund_id", flat=True)
    )
    split_ids = [item.pk for item in rows if item.category_source == Transaction.CategorySource.SPLIT]
    splits_by_txn = defaultdict(list)
    if split_ids:
        for part in TransactionSplit.objects.filter(transaction_id__in=split_ids):
            splits_by_txn[part.transaction_id].append(part)

    income = 0
    spending = 0
    by_category = defaultdict(int)
    for item in rows:
        if item.pk in excluded:
            continue
        if item.pk in refunds:
            spending -= item.amount_minor
            by_category[item.category_id] -= item.amount_minor
            continue
        if item.amount_minor > 0:
            income += item.amount_minor
        elif item.amount_minor < 0:
            magnitude = -item.amount_minor
            spending += magnitude
            parts = splits_by_txn.get(item.pk)
            if item.category_source == Transaction.CategorySource.SPLIT and parts:
                for part in parts:
                    by_category[part.category_id] += -part.amount_minor
            else:
                by_category[item.category_id] += magnitude
    return SimpleNamespace(
        income_minor=income,
        spending_minor=spending,
        net_minor=income - spending,
        spending_by_category_id=dict(by_category),
    )
