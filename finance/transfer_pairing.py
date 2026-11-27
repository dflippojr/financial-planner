"""Transfer and card-payment pairing: find, score, auto-mark, revalidate, confirm, dismiss, undo and restore.

Not to be confused with loan-to-asset pairing in loan_asset_pairing.py.
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from contextvars import ContextVar
from datetime import timedelta
from functools import wraps
from itertools import chain

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .access import DENIED
from .access import require_person
from .access import visible_rows
from .audit_services import record
from .households import current_household
from .input_limits import MAX_TRANSFER_CANDIDATES
from .ledger_edits import (
    account_viewer_ids,
    every_viewer_always_sees_legs,
    history_label,
    legs_seeing_viewers,
    lock_accounts_and_transactions,
    record_member_history,
    record_text_history,
)
from .lifecycle_services import lock_actor_household
from .models import (
    Account,
    AuditEvent,
    Category,
    Membership,
    Person,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)
from .pairing_evidence import has_payment_wording
from .refund_links import (
    drop_refund_links_for_pair,
    locked_refund_links,
    restore_refund_categories,
    restore_refund_links_for_pair,
)
from .account_locks import lock_accounts


# The Transfers page offers Restore for pairs undone this recently, this many at most (#635).
RECENTLY_UNDONE_DAYS = 30
RECENTLY_UNDONE_LIMIT = 50

_PAIR_CACHE: ContextVar[dict | None] = ContextVar("pair_cache", default=None)


def _cached_pair_run(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _PAIR_CACHE.set({"memberships": {}, "windows": {}})
        try:
            return function(*args, **kwargs)
        finally:
            _PAIR_CACHE.reset(token)
    return wrapped


def _prime_pair_cache(rows):
    cache = _PAIR_CACHE.get()
    if cache is None:
        return
    owner_ids = {row.account.owner_id for row in rows}
    memberships = Membership.objects.filter(person_id__in=owner_ids, ended_at__isnull=True).select_related("household")
    cache["memberships"] = {owner_id: None for owner_id in owner_ids}
    for membership in memberships:
        cache["memberships"][membership.person_id] = membership.household_id  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        cache["windows"][membership.person_id] = membership.household.transfer_match_window_days  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
    for owner_id in owner_ids:
        cache["windows"].setdefault(owner_id, settings.TRANSFER_MATCH_WINDOW_DAYS)


HIGH_CONFIDENCE_UNIQUE_BOTH = "only candidate for both legs in the window"
LOW_CONFIDENCE_MULTIPLE = "multiple possible counterparts in the window"
EVIDENCE_PRESENT = "payment or transfer wording on a leg"
EVIDENCE_ABSENT = "no payment or transfer wording on either leg, so it needs confirmation"
DIRECTION_OK = "money moves in the expected direction"
DIRECTION_WRONG = "card pair runs the wrong way (bank credit with card debit), so it needs confirmation"


@transaction.atomic
def set_transfer_window_days(principal, days):
    person = require_person(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(DENIED)
    lock_actor_household(person)
    if days < 0 or days > 366:
        raise ValidationError("Choose a match window between 0 and 366 days.")
    household.transfer_match_window_days = days
    household.save(update_fields=("transfer_match_window_days", "updated_at"))
    # The window revalidates every member's pairs, including pairs between a
    # member's private accounts that the actor cannot see. New pairs are only
    # detected among rows the actor can see.
    member_ids = Membership.objects.filter(household=household, ended_at__isnull=True).values_list(
        "person_id", flat=True
    )
    for member in Person.objects.filter(pk__in=list(member_ids)).order_by("pk"):
        refresh_transfer_pairs(member, actor=person)
    return household


def refresh_household_transfer_pairs(person, transaction_ids):
    household = current_household(person)
    if household is None:
        refresh_transfer_pairs(person, actor=person, transaction_ids=transaction_ids)
        return
    member_ids = Membership.objects.filter(household=household, ended_at__isnull=True).values_list(
        "person_id", flat=True
    )
    for member in Person.objects.filter(pk__in=list(member_ids)).order_by("pk"):
        refresh_transfer_pairs(member, actor=person, transaction_ids=transaction_ids)


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
    cache = _PAIR_CACHE.get()
    if cache is not None and owned.owner_id in cache["memberships"]:
        return cache["memberships"][owned.owner_id] == shared.household_id
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
    cache = _PAIR_CACHE.get()
    if cache is not None and owner_id in cache["windows"]:
        return cache["windows"][owner_id]
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


def _direction_is_plausible(tx_a, tx_b):
    """A card pair must be a bank debit with a card credit; other pairs have no direction rule."""
    types = {tx_a.account.account_type, tx_b.account.account_type}
    involves_card = Account.Type.CREDIT_CARD in types and len(types) == 2
    return not involves_card or _pair_kind(tx_a, tx_b) == TransferPair.Kind.CARD_PAYMENT


def _has_payment_evidence(tx_a, tx_b):
    return has_payment_wording(tx_a.description) or has_payment_wording(tx_b.description)


def _meets_auto_mark_evidence(tx_a, tx_b):
    return _direction_is_plausible(tx_a, tx_b) and _has_payment_evidence(tx_a, tx_b)


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
    window = _window_days(tx_a.account, tx_b.account)
    if abs((tx_a.transaction_date - tx_b.transaction_date).days) > window:
        return False
    return _amounts_and_accounts_can_pair(tx_a, tx_b)


class _CandidatePairs(list):
    truncated = False


def _group_date_candidates(group):
    positives = sorted((row for row in group if row.amount_minor > 0),
                       key=lambda row: (row.transaction_date, row.pk))
    dates = [row.transaction_date for row in positives]
    negatives = sorted((row for row in group if row.amount_minor < 0),
                       key=lambda row: (row.transaction_date, row.pk))
    for left in negatives:
        window = _window_days(left.account, left.account)
        lower = bisect_left(dates, left.transaction_date - timedelta(days=window))
        upper = bisect_right(dates, left.transaction_date + timedelta(days=window))
        for index in range(lower, upper):
            yield left, positives[index]


def _candidate_pairs(transactions):
    by_abs = defaultdict(list)
    for item in transactions:
        if item.amount_minor and not _is_split(item):
            by_abs[abs(item.amount_minor)].append(item)
    raw_pairs = _CandidatePairs()
    candidates = chain.from_iterable(_group_date_candidates(group) for group in by_abs.values())
    for index, (left, right) in enumerate(candidates):
        if index >= MAX_TRANSFER_CANDIDATES:
            raw_pairs.truncated = True
            break
        if _is_candidate(left, right):
            raw_pairs.append(_ordered_legs(left, right))
    return raw_pairs


def _scored_pair(left, right, unique):
    window = _window_days(left.account, right.account)
    direction = _direction_is_plausible(left, right)
    evidence = _has_payment_evidence(left, right)
    return {
        "left": left,
        "right": right,
        "confidence": TransferPair.Confidence.HIGH if unique and direction and evidence else TransferPair.Confidence.LOW,
        "kind": _pair_kind(left, right),
        "reasons": [
            "exact opposite amounts",
            f"dates within {window} days",
            "both accounts visible to the same person",
            HIGH_CONFIDENCE_UNIQUE_BOTH if unique else LOW_CONFIDENCE_MULTIPLE,
            DIRECTION_OK if direction else DIRECTION_WRONG,
            EVIDENCE_PRESENT if evidence else EVIDENCE_ABSENT,
        ],
    }


def _score_pairs(transactions):
    raw_pairs = _candidate_pairs(transactions)
    counts = defaultdict(int)
    for left, right in raw_pairs:
        counts[left.pk] += 1
        counts[right.pk] += 1
    return [
        _scored_pair(left, right, not getattr(raw_pairs, "truncated", False)
                     and counts[left.pk] == 1 and counts[right.pk] == 1)
        for left, right in raw_pairs
    ]


def _freed_transaction_ids(pairs):
    """Legs of pairs a member undid: refresh may suggest a new pair on them but never auto-marks one (#635).

    An auto-marked pair would be a new Excluded row above the one the member
    just clicked, so the next click would land on another pair. Restore clears
    `undone_at`, which frees the legs for auto-marking again.
    """
    freed = set()
    for pair in pairs:
        if pair.status == TransferPair.Status.UNDONE and pair.undone_at is not None:
            freed.add(pair.leg_a_id)
            freed.add(pair.leg_b_id)
    return freed


def _touches(pair, legs):
    return pair.leg_a_id in legs or pair.leg_b_id in legs  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute


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
    record_text_history(left, actor, TransactionCorrectionHistory.Field.EXCLUSION, "included", f"excluded:{label}")
    record_text_history(right, actor, TransactionCorrectionHistory.Field.EXCLUSION, "included", f"excluded:{label}")
    drop_refund_links_for_pair(locked_refund_links((left.pk, right.pk)), pair, (left, right), actor)


def _pair_category_history_recorder(left, right):
    """Record an unmark's category changes so they reveal the pair to no one who cannot see it (#580).

    A row whose every viewer sees both legs, now and after any member joins, is
    an ordinary correction. Otherwise each viewer who sees both legs gets a row
    `only_for` them, with no audit event, and the others get nothing.
    """
    leg_accounts = (left.account, right.account)
    see_both_legs = legs_seeing_viewers(leg_accounts)

    def record(txn, actor, field_name, previous, new):
        if every_viewer_always_sees_legs(txn.account, leg_accounts, see_both_legs):
            record_text_history(txn, actor, field_name, previous, new)
        elif previous != new:
            for person_id in sorted(account_viewer_ids(txn.account) & see_both_legs):
                record_member_history(txn, actor, person_id, field_name, previous, new)

    return record


_SETTLED_PAIR_STATUSES = (
    TransferPair.Status.DISMISSED,
    TransferPair.Status.UNDONE,
    TransferPair.Status.CONFIRMED,
    TransferPair.Status.AUTO_MARKED,
)


def _lock_visible_transactions(person):
    transactions = list(
        visible_rows(Transaction, person)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "account__household", "account__owner")
    )
    if not transactions:
        return []
    lock_accounts({item.account_id for item in transactions}, pks_only=True)
    tx_ids = sorted(item.pk for item in transactions)
    return list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "account__household", "account__owner", "category")
        .filter(pk__in=tx_ids)
        .order_by("pk")
    )


def _update_existing_pair(pair, scored, person, freed=()):
    if pair.status in _SETTLED_PAIR_STATUSES:
        return
    pair.confidence = scored["confidence"]
    pair.kind = scored["kind"]
    pair.reasons = scored["reasons"]
    if scored["confidence"] == TransferPair.Confidence.HIGH and not _touches(pair, freed):
        _snapshot_and_mark(pair, scored["left"], scored["right"], TransferPair.Status.AUTO_MARKED, person)
    else:
        pair.save(update_fields=("confidence", "kind", "reasons", "updated_at"))


def _restore_snapshot_category(leg, category_id, actor, record_history):
    # A split leg's category stays empty; its parts hold the categories.
    if _is_split(leg) or leg.category_id == category_id:
        return
    previous = leg.category
    restored = None if category_id is None else Category.objects.get(pk=category_id)
    leg.category = restored
    leg.save(update_fields=("category", "updated_at"))
    record_history(
        leg,
        actor,
        TransactionCorrectionHistory.Field.CATEGORY,
        history_label(previous),
        history_label(restored),
    )


def _unmark_exclusion(pair, left, right, actor):
    record_history = _pair_category_history_recorder(left, right)
    _restore_snapshot_category(left, pair.leg_a_category_id_at_mark, actor, record_history)
    _restore_snapshot_category(right, pair.leg_b_category_id_at_mark, actor, record_history)
    restore_refund_categories(left, actor, record_history)
    restore_refund_categories(right, actor, record_history)
    pair.status = TransferPair.Status.UNDONE
    pair.save(update_fields=("status", "updated_at"))
    restore_refund_links_for_pair(pair, actor)
    record_text_history(left, actor, TransactionCorrectionHistory.Field.EXCLUSION, "excluded", "included")
    record_text_history(right, actor, TransactionCorrectionHistory.Field.EXCLUSION, "excluded", "included")


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


def _downgrade_to_suggested(pair, left, right, actor):
    """Undo the exclusion of an auto-marked pair and keep it as a low-confidence suggestion."""
    _unmark_exclusion(pair, left, right, actor)
    scored = _scored_pair(left, right, unique=True)
    pair.status = TransferPair.Status.SUGGESTED
    pair.confidence = TransferPair.Confidence.LOW
    pair.kind = scored["kind"]
    pair.reasons = scored["reasons"]
    pair.save(update_fields=("status", "confidence", "kind", "reasons", "updated_at"))


def _auto_mark_no_longer_qualifies(pair, left, right):
    return (
        pair.status == TransferPair.Status.AUTO_MARKED
        and _both_legs_active(left, right)
        and not _meets_auto_mark_evidence(left, right)
    )


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
            if _auto_mark_no_longer_qualifies(pair, left, right):
                _downgrade_to_suggested(pair, left, right, person)
            continue
        if pair.status == TransferPair.Status.SUGGESTED:
            _invalidate_suggestion(pair)
        elif left is not None and right is not None:
            _unmark_exclusion(pair, left, right, person)
        else:
            pair.status = TransferPair.Status.UNDONE
            pair.save(update_fields=("status", "updated_at"))
            restore_refund_links_for_pair(pair, person)


def downgrade_unevidenced_auto_marked_pairs():
    """One-off re-evaluation: auto-marked pairs lacking the evidence rule become suggestions.

    Returns (examined, downgraded) counts. Confirmed, dismissed and undone pairs are untouched,
    and a second run finds nothing left to downgrade.
    """
    pair_ids = list(
        TransferPair.objects.filter(status=TransferPair.Status.AUTO_MARKED).order_by("pk").values_list("pk", flat=True)
    )
    downgraded = 0
    for pair_id in pair_ids:
        with transaction.atomic():
            pair = TransferPair.objects.select_for_update(of=("self",)).filter(pk=pair_id).first()
            if pair is None or pair.status != TransferPair.Status.AUTO_MARKED:
                continue
            left, right = _pair_legs(pair, {})
            if _auto_mark_no_longer_qualifies(pair, left, right):
                _downgrade_to_suggested(pair, left, right, None)
                downgraded += 1
    return len(pair_ids), downgraded


def pair_rows_touching(leg_ids):
    if not leg_ids:
        return []
    return list(
        TransferPair.objects.filter(Q(leg_a_id__in=leg_ids) | Q(leg_b_id__in=leg_ids)).values_list(
            "pk", "leg_a_id", "leg_b_id"
        )
    )


def account_ids_in_pairs_touching_transactions(transaction_ids):
    """Account pks for every leg of pairs that include any of these transactions."""
    rows = pair_rows_touching(transaction_ids)
    if not rows:
        return set()
    all_legs = {leg_id for _pk, left_id, right_id in rows for leg_id in (left_id, right_id)}
    return set(Transaction.objects.filter(pk__in=all_legs).values_list("account_id", flat=True))


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
            restore_refund_links_for_pair(pair, person)


def revalidate_pairs_for_leg_ids(person, seed_leg_ids):
    rows = pair_rows_touching(seed_leg_ids)
    if not rows:
        return
    all_leg_ids = sorted({leg_id for _pk, left_id, right_id in rows for leg_id in (left_id, right_id)})
    lock_accounts(Transaction.objects.filter(pk__in=all_leg_ids).values_list("account_id", flat=True), pks_only=True)
    locked = list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "account__household", "account__owner", "category")
        .filter(pk__in=all_leg_ids)
        .order_by("pk")
    )
    pair_ids = sorted(pk for pk, _left, _right in rows)
    existing = {
        (pair.leg_a_id, pair.leg_b_id): pair  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        for pair in TransferPair.objects.select_for_update(of=("self",)).filter(pk__in=pair_ids).order_by("pk")
    }
    _revalidate_marked_pairs(existing, {item.pk: item for item in locked}, person)


@transaction.atomic
def revalidate_pairs_touching_account(principal, account_id):
    person = require_person(principal)
    lock_actor_household(person)
    seed = list(Transaction.objects.filter(account_id=account_id).values_list("pk", flat=True))
    revalidate_pairs_for_leg_ids(person, seed)


@transaction.atomic
def revalidate_pairs_touching_import_batch(principal, batch_id):
    person = require_person(principal)
    lock_actor_household(person)
    seed = list(Transaction.objects.filter(import_batch_id=batch_id).values_list("pk", flat=True))
    revalidate_pairs_for_leg_ids(person, seed)


def transfer_matching_key(txn):
    """Fields needed to revisit the old candidates after an edit."""
    return (txn.account_id, txn.transaction_date, txn.amount_minor)


def _candidate_neighbors(person, keys, window):
    # Bound SQL expression size even for large batches. No ledger-wide model
    # construction: PostgreSQL can use the active amount/date index here.
    keys = list(set(keys))
    found = {}
    for offset in range(0, len(keys), 100):
        predicate = Q(pk__in=[])
        for account_id, day, amount in keys[offset : offset + 100]:
            if amount:
                predicate |= Q(
                    amount_minor=-amount,
                    transaction_date__range=(day - timedelta(days=window), day + timedelta(days=window)),
                ) & ~Q(account_id=account_id)
        rows = (
            visible_rows(Transaction, person)
            .filter(predicate, status=Transaction.Status.ACTIVE)
            .exclude(category_source=Transaction.CategorySource.SPLIT)
            .select_related("account", "account__household", "category")
        )
        found.update((row.pk, row) for row in rows)
    return found


def _lock_affected_transactions(person, transaction_ids, previous_keys):
    visible = visible_rows(Transaction, person)
    affected = {
        row.pk: row for row in visible.filter(pk__in=transaction_ids)
        .select_related("account", "account__household", "category")
    }
    if not affected:
        return []
    household = current_household(person)
    window = household.transfer_match_window_days if household else settings.TRANSFER_MATCH_WINDOW_DAYS
    frontier = list(affected.values())
    old_keys = previous_keys
    while frontier or old_keys:
        # Follow stored pairs as well as candidate edges. An edit can disconnect
        # the old amount/date component, and settled pairs still occupy its legs.
        rows = pair_rows_touching([row.pk for row in frontier])
        partner_ids = {pk for _, left, right in rows for pk in (left, right)} - affected.keys()
        neighbors = {
            row.pk: row for row in Transaction.objects.filter(pk__in=partner_ids)
            .select_related("account", "account__household", "category")
        }
        keys = [transfer_matching_key(row) for row in frontier] + list(old_keys)
        neighbors.update(_candidate_neighbors(person, keys, window))
        frontier = [row for pk, row in neighbors.items() if pk not in affected]
        affected.update((row.pk, row) for row in frontier)
        old_keys = ()
    lock_accounts({row.account_id for row in affected.values()}, pks_only=True)
    return list(
        Transaction.objects.select_for_update(of=("self",))
        .filter(pk__in=sorted(affected))
        .select_related("account", "account__household", "category")
        .order_by("pk")
    )


@transaction.atomic
@_cached_pair_run
def refresh_transfer_pairs(principal, *, actor=None, transaction_ids=None, previous_keys=()):
    """Refresh affected candidate components; omit ids for a maintenance rebuild."""
    if transaction_ids is not None:
        transaction_ids = list(transaction_ids)
        if not transaction_ids:
            return []
    person = require_person(principal)
    actor = person if actor is None else actor
    lock_actor_household(person)
    locked = (
        _lock_visible_transactions(person) if transaction_ids is None
        else _lock_affected_transactions(person, transaction_ids, previous_keys)
    )
    if not locked:
        return []
    _prime_pair_cache(locked)
    tx_ids = [item.pk for item in locked]
    existing = {
        (pair.leg_a_id, pair.leg_b_id): pair  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        for pair in TransferPair.objects.select_for_update(of=("self",)).filter(
            Q(leg_a_id__in=tx_ids) | Q(leg_b_id__in=tx_ids)
        ).order_by("pk")
    }
    locked_by_id = {item.pk: item for item in locked}
    _revalidate_marked_pairs(existing, locked_by_id, actor)
    occupied = _occupied_transaction_ids(existing.values())
    freed = _freed_transaction_ids(existing.values())
    created = []
    visible_ids = set(visible_rows(Transaction, person).filter(
        pk__in=tx_ids, status=Transaction.Status.ACTIVE
    ).values_list("pk", flat=True))
    if actor.pk != person.pk:
        # Detection on another member's behalf only pairs rows the actor can
        # also see; that member's own refresh covers the rest.
        visible_ids &= set(visible_rows(Transaction, actor).filter(
            pk__in=visible_ids
        ).values_list("pk", flat=True))
    scored_pairs = _score_pairs([row for row in locked if row.pk in visible_ids])
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
            _update_existing_pair(existing[key], scored, actor, freed)
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
        if scored["confidence"] == TransferPair.Confidence.HIGH and not _touches(pair, freed):
            _snapshot_and_mark(pair, left, right, TransferPair.Status.AUTO_MARKED, actor)
        created.append(pair)
    return created


def _visible_pair(principal, pair_id):
    person = require_person(principal)
    pair = (
        visible_rows(TransferPair, person)
        .select_related("leg_a", "leg_b", "leg_a__account", "leg_b__account", "leg_a__category", "leg_b__category")
        .filter(pk=pair_id)
        .first()
    )
    if pair is None:
        raise PermissionDenied(DENIED)
    return person, pair


def _record_pair_event(person, leg, action, pair):
    # Transfer confirm/dismiss/undo is a member decision, recorded on the given leg's
    # account unless a viewer there, now or after joining, cannot see the other leg:
    # then on the other leg's account, so the event reveals the pair to no one who
    # cannot see it (#580).
    other = pair.leg_b if leg.pk == pair.leg_a_id else pair.leg_a
    leg_accounts = (leg.account, other.account)
    if not every_viewer_always_sees_legs(leg.account, leg_accounts, legs_seeing_viewers(leg_accounts)):
        leg, other = other, leg
    record(person, action, AuditEvent.TargetType.TRANSACTION, leg.pk,
           audience={"account": Account(pk=leg.account_id)}, fields=("kind",),
           metadata={"transaction_id": other.pk})


def _relocked_pair(pair, statuses, legs=None):
    """The pair row under lock, refused if it changed or vanished while this request waited.

    Restore deletes competing suggestions, and two quick clicks race, so a pair
    read before the locks may be gone or settled once they are held. `legs` are
    the legs locked with their accounts: the audit event reads their scopes, so
    they must be the ones read under the lock, never the copies loaded before
    it (an account made private meanwhile would otherwise land on a shared
    account's event). Without them the legs load lazily, after the locks.
    """
    locked = TransferPair.objects.select_for_update(of=("self",)).filter(pk=pair.pk).first()
    if locked is None or locked.status not in statuses:
        raise PermissionDenied(DENIED)
    if legs is not None:
        by_id = {leg.pk: leg for leg in legs}
        locked.leg_a, locked.leg_b = by_id[locked.leg_a_id], by_id[locked.leg_b_id]  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
    return locked


@transaction.atomic
def confirm_transfer_pair(principal, pair_id):
    person, pair = _visible_pair(principal, pair_id)
    if pair.status != TransferPair.Status.SUGGESTED:
        raise PermissionDenied(DENIED)
    locked = lock_accounts_and_transactions(person, [pair.leg_a, pair.leg_b])
    left, right = locked[0], locked[1]
    if not _is_candidate(left, right):
        raise PermissionDenied(DENIED)
    pair = _relocked_pair(pair, (TransferPair.Status.SUGGESTED,), locked)
    _snapshot_and_mark(pair, left, right, TransferPair.Status.CONFIRMED, person)
    _record_pair_event(person, left, AuditEvent.Action.RECORD_ENABLED, pair)
    return pair


@transaction.atomic
def dismiss_transfer_pair(principal, pair_id):
    person, pair = _visible_pair(principal, pair_id)
    if pair.status != TransferPair.Status.SUGGESTED:
        raise PermissionDenied(DENIED)
    lock_actor_household(person)
    pair = _relocked_pair(pair, (TransferPair.Status.SUGGESTED,))
    pair.status = TransferPair.Status.DISMISSED
    pair.save(update_fields=("status", "updated_at"))
    _record_pair_event(person, pair.leg_a, AuditEvent.Action.RECORD_DISABLED, pair)
    return pair


@transaction.atomic
def undo_transfer_pair(principal, pair_id):
    person, pair = _visible_pair(principal, pair_id)
    if pair.status not in (TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED):
        raise PermissionDenied(DENIED)
    locked = lock_accounts_and_transactions(person, [pair.leg_a, pair.leg_b])
    by_id = {item.pk: item for item in locked}
    left = by_id[pair.leg_a_id]
    right = by_id[pair.leg_b_id]
    pair = _relocked_pair(pair, _MARKED_PAIR_STATUSES, locked)
    _unmark_exclusion(pair, left, right, person)
    pair.undone_at = timezone.now()
    pair.undone_by = person
    pair.save(update_fields=("undone_at", "undone_by", "updated_at"))
    _record_pair_event(person, left, AuditEvent.Action.RECORD_DISABLED, pair)
    return pair


def _pairs_on_legs(pair, statuses):
    legs = (pair.leg_a_id, pair.leg_b_id)  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
    return (
        TransferPair.objects.filter(Q(leg_a_id__in=legs) | Q(leg_b_id__in=legs), status__in=statuses)
        .exclude(pk=pair.pk)
    )


def _leg_excluded_elsewhere(pair, left, right):
    # Not limited to pairs the member can see: a pair through another member's
    # private account excludes the leg all the same, and a second exclusion
    # would change totals.
    if left.untracked_transfer or right.untracked_transfer:
        return True
    return _pairs_on_legs(pair, _MARKED_PAIR_STATUSES).exists()


def recently_undone_pairs(principal):
    """Pairs a member undid in the last 30 days that this viewer can see, newest first."""
    since = timezone.now() - timedelta(days=RECENTLY_UNDONE_DAYS)
    return (
        visible_rows(TransferPair, principal)
        .filter(status=TransferPair.Status.UNDONE, undone_at__gte=since)
        .select_related("leg_a", "leg_b", "leg_a__account", "leg_b__account")
        .order_by("-undone_at", "-pk")[:RECENTLY_UNDONE_LIMIT]
    )


@transaction.atomic
def restore_transfer_pair(principal, pair_id):
    """Confirm again a pair a member undid, if both legs still pair and nothing else excludes them (#635)."""
    person, pair = _visible_pair(principal, pair_id)
    if pair.status != TransferPair.Status.UNDONE or pair.undone_at is None:
        raise PermissionDenied(DENIED)
    locked = lock_accounts_and_transactions(person, [pair.leg_a, pair.leg_b])
    left, right = locked[0], locked[1]
    pair = _relocked_pair(pair, (TransferPair.Status.UNDONE,), locked)
    if pair.undone_at is None:
        raise PermissionDenied(DENIED)
    if not _is_candidate(left, right) or _leg_excluded_elsewhere(pair, left, right):
        raise PermissionDenied(DENIED)
    # A suggestion found for a leg while the pair was undone would let a later
    # Confirm exclude that leg twice, and refresh never suggests a pair on an
    # excluded leg. Delete it rather than retire it: a suggestion holds no refund
    # drops or history, and refresh can suggest it again if this pair is undone.
    competing = _pairs_on_legs(pair, (TransferPair.Status.SUGGESTED,)).select_for_update(of=("self",)).order_by("pk")
    TransferPair.objects.filter(pk__in=[item.pk for item in competing]).delete()
    _snapshot_and_mark(pair, left, right, TransferPair.Status.CONFIRMED, person)
    pair.undone_at = None
    pair.undone_by = None
    pair.save(update_fields=("undone_at", "undone_by", "updated_at"))
    _record_pair_event(person, left, AuditEvent.Action.RECORD_ENABLED, pair)
    return pair
