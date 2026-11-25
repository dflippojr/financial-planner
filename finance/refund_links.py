"""Refund links: link a refund, inherit its original's category, and drop or restore links.

A link lasts only while its original counts (#570): archiving the original
deletes it, and marking the original as a transfer leg drops it for the
viewers who see both legs until the pair is unmarked. Marking the original as an
untracked transfer deletes it too; undoing a bulk mark restores it (#797).
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q

from .access import DENIED as _DENIED
from .access import require_person as _person_for
from .access import visible_rows
from .ledger_counting import exclusion_exists_for
from .ledger_edits import (
    account_viewer_ids,
    every_viewer_always_sees_legs,
    history_label,
    legs_seeing_viewers,
    lock_accounts_and_transactions,
    record_member_history,
    record_text_history,
)
from .models import (
    Account,
    Category,
    Membership,
    RefundLink,
    RefundLinkDrop,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
    TransferPair,
)


REFUND_LINK_RULE = (
    "A refund must be a positive amount linked to a negative original purchase of the same kind."
)
REFUND_LINK_STANDS = "Another member's link for this refund still stands, so it can't be relinked here."
SPLIT_PART_REQUIRED = "Choose a split part for each linked refund."
SPLIT_PART_MISMATCH = "A refund linked to a split purchase must use a part of that purchase."


def _refund_viewers_see_original():
    """Every member who can see the refund can also see its original.

    A private refund needs an original its owner sees: one of their own private
    accounts, or a household account of the household they belong to now. A
    household refund needs an original in an account of the same household.
    """
    original_account = "refund_link__original__account__"
    owner_sees_original = Q(
        **{f"{original_account}scope": Account.Scope.PRIVATE, f"{original_account}owner_id": F("account__owner_id")}
    ) | Q(
        Exists(
            Membership.objects.filter(
                person_id=OuterRef("account__owner_id"),
                household_id=OuterRef(f"{original_account}household_id"),
                ended_at__isnull=True,
            )
        ),
        **{f"{original_account}scope": Account.Scope.HOUSEHOLD},
    )
    same_household = Q(
        account__household_id__isnull=False,
        **{
            f"{original_account}scope": Account.Scope.HOUSEHOLD,
            f"{original_account}household_id": F("account__household_id"),
        },
    )
    return Q(owner_sees_original, account__scope=Account.Scope.PRIVATE) | Q(
        same_household, account__scope=Account.Scope.HOUSEHOLD
    )


def refunds_following_originals(actor, refunds):
    """Narrow a queryset of linked refunds to those that may copy their original's category.

    Every path that copies an original's category onto its refunds goes through
    here (#567). A refund follows only while every member who sees it also sees
    the original, and only when `actor` sees the refund. Others keep their
    category and get no correction history; their link still counts as before.
    `actor` is None only for operator maintenance, which no viewer drives.
    """
    following = refunds.filter(_refund_viewers_see_original())
    if actor is not None:
        visible = Transaction.objects.visible_to(actor)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every Transaction.objects.visible_to in the baseline
        following = following.filter(pk__in=visible.values("pk"))
    return following


def linked_refunds_for_originals(actor, originals):
    ids = [txn.pk for txn in originals]
    if not ids:
        return []
    return list(
        refunds_following_originals(
            actor,
            Transaction.objects.filter(
                refund_link__original_id__in=ids,
                status=Transaction.Status.ACTIVE,
            ),
        )
        .select_related("account", "category")
        .prefetch_related("tags")
        .order_by("pk")
    )


def transaction_is_linked_refund(txn):
    return hasattr(txn, "refund_link")


def apply_inherited_category(person, refund, category, record_history=record_text_history):
    previous = refund.category
    refund.category = category
    refund.category_source = Transaction.CategorySource.INHERITED
    refund.save(update_fields=("category", "category_source", "updated_at"))
    record_history(
        refund,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        history_label(previous),
        history_label(category),
    )


def locked_refund_links(original_ids):
    original_ids = sorted(set(original_ids))
    if not original_ids:
        return []
    return list(
        RefundLink.objects.select_for_update(of=("self",))
        .select_related("refund__account")
        .filter(original_id__in=original_ids)
        .order_by("pk")
    )


def _delete_refund_links(links, actor):
    RefundLink.objects.filter(pk__in=[link.pk for link in links]).delete()
    for link in links:
        record_text_history(link.refund, actor, TransactionCorrectionHistory.Field.REFUND_LINK, "linked", "unlinked")


def drop_refund_links_to_originals(original_ids, actor):
    """Delete the refund links of archived originals (#570).

    An original stops counting for every viewer when it is archived (an undone
    import, a deleted manual entry, an archived account). Its refunds then count
    as ordinary income and keep their category. Each gets "linked" -> "unlinked"
    history whoever can see it; the audit event is skipped for a refund the
    actor cannot see. The caller holds the originals' row locks, so no link can
    be added to them meanwhile.
    """
    links = locked_refund_links(original_ids)
    if links:
        _delete_refund_links(links, actor)


def _record_member_link_history(refund, actor, person_id, previous, new):
    record_member_history(refund, actor, person_id, TransactionCorrectionHistory.Field.REFUND_LINK, previous, new)


def drop_refund_links_for_pair(links, pair, legs, actor):
    """Drop locked refund links whose original is a leg of a marked pair (#570).

    A marked pair is excluded only for viewers who see both legs
    (`exclusion_exists_for`), so a link stops counting only for those viewers of
    its refund. When every viewer of the refund sees both legs, now and after any
    member joins (a private refund whose owner sees them, or a household refund
    whose legs are both in that household), the link is deleted as for an
    archived original. Otherwise each such viewer gets a `RefundLinkDrop` for
    this pair and, unless another pair already dropped the link for them, a
    history row `only_for` them, with no audit event, so members who cannot
    see a leg keep their link, totals and history unchanged.
    """
    if not links:
        return
    leg_accounts = [leg.account for leg in legs]
    see_both_legs = legs_seeing_viewers(leg_accounts)
    deleted = []
    for link in links:
        refund_account = link.refund.account
        seeing = account_viewer_ids(refund_account) & see_both_legs
        if every_viewer_always_sees_legs(refund_account, leg_accounts, see_both_legs):
            deleted.append(link)
            continue
        drops = list(link.drops.values_list("person_id", "pair_id"))
        dropped_for = {person_id for person_id, _pair_id in drops}
        held_by_pair = {person_id for person_id, pair_id in drops if pair_id == pair.pk}
        for person_id in sorted(seeing - held_by_pair):
            if person_id not in dropped_for:
                _record_member_link_history(link.refund, actor, person_id, "linked", "unlinked")
            RefundLinkDrop.objects.create(link=link, person_id=person_id, pair=pair)
    if deleted:
        _delete_refund_links(deleted, actor)


def _marked_pairs_touching_account(account_id):
    marked = TransferPair.objects.excluding_income_and_spending()  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other excluding_income_and_spending calls in the baseline
    return marked.filter(Q(leg_a__account_id=account_id) | Q(leg_b__account_id=account_id))


def account_ids_for_refund_link_rule(account_id):
    """Accounts `reapply_refund_link_drops_for_account` reads or writes for this account.

    The other leg's account, and the account of any refund linked to either leg,
    which can be neither leg's account and is reached only through the pair.
    """
    legs = set()
    for leg_ids in _marked_pairs_touching_account(account_id).values_list("leg_a_id", "leg_b_id"):
        legs.update(leg_ids)
    if not legs:
        return set()
    legs_and_refunds = Q(pk__in=legs) | Q(refund_link__original_id__in=legs)
    return set(Transaction.objects.filter(legs_and_refunds).values_list("account_id", flat=True))


def reapply_refund_link_drops_for_account(account_id, actor):
    """Run the drop rule again for marked pairs with a leg in this account (#579).

    Sharing a leg's account, or changing its share mode, can make the pair
    excluded for more members, so a link standing for them no longer has a
    counted original. Owner decision 2026-11-02: only these two changes run it.
    Unsharing and leaving do not, so they neither delete a link nor touch a
    `RefundLinkDrop`. The caller has saved the account and holds the locks of
    `account_ids_for_refund_link_rule`; legs are read here so that their accounts
    show the new scope.
    """
    pairs = (
        _marked_pairs_touching_account(account_id)
        .select_for_update(of=("self",))
        .select_related("leg_a__account", "leg_b__account")
        .order_by("pk")
    )
    for pair in pairs:
        links = locked_refund_links((pair.leg_a_id, pair.leg_b_id))
        drop_refund_links_for_pair(links, pair, (pair.leg_a, pair.leg_b), actor)


def restore_refund_links_for_pair(pair, actor):
    """Clear the drops an unmarked pair made, so those links count again (#570).

    Owner decision 2026-10-23, option (a): a drop never outlives the pair that
    caused it. A member whose link no other marked pair still drops gets an
    "unlinked" -> "linked" history row `only_for` them, with no audit event.
    The caller holds the legs' and the pair's row locks; the links are locked
    here in pk order, as the mark path does.
    """
    links = {link.pk: link for link in locked_refund_links((pair.leg_a_id, pair.leg_b_id))}
    drops = RefundLinkDrop.objects.filter(pair=pair, link_id__in=links)
    cleared = list(drops.order_by("pk").values_list("link_id", "person_id"))
    if not cleared:
        return
    drops.delete()
    still_dropped = set(RefundLinkDrop.objects.filter(link_id__in=links).values_list("link_id", "person_id"))
    for link_id, person_id in cleared:
        if (link_id, person_id) not in still_dropped:
            _record_member_link_history(links[link_id].refund, actor, person_id, "unlinked", "linked")


def _marked_pairs_touching(transaction_ids):
    marked = TransferPair.objects.excluding_income_and_spending()  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other excluding_income_and_spending calls in the baseline
    return marked.filter(Q(leg_a_id__in=transaction_ids) | Q(leg_b_id__in=transaction_ids))


def dropped_refund_links(original_ids):
    """The links of these originals, as `restore_dropped_refund_links` takes them back.

    Read under the originals' row locks, before the mark drops them (#797).
    Each link keeps its `RefundLinkDrop` rows exactly as they stand, for undo to
    put back verbatim, and the marked pairs that touch its original or refund,
    so undo can tell a pair that came later from one that was already there.
    """
    links = list(
        RefundLink.objects.filter(original_id__in=original_ids)
        .order_by("pk")
        .values("pk", "refund_id", "original_part_id", "original_id")
    )
    drops = {}
    for link_id, person_id, pair_id in (
        RefundLinkDrop.objects.filter(link_id__in=[link["pk"] for link in links])
        .order_by("link_id", "person_id", "pair_id")
        .values_list("link_id", "person_id", "pair_id")
    ):
        drops.setdefault(link_id, []).append([person_id, pair_id])
    for link in links:
        link_id = link.pop("pk")
        original_id = link.pop("original_id")
        link["drops"] = drops.get(link_id, [])
        touching = _marked_pairs_touching((original_id, link["refund_id"]))
        link["marked_pair_ids"] = sorted(touching.values_list("pk", flat=True))
    return links


def _restore_drops(link, saved_drops, actor):
    """Put back the stored drops whose pair still exists and still leaves its legs out.

    Owner decision via the liaison on #809: the drops come back as they were,
    never re-derived under today's viewer rules. A drop whose pair has since
    been undone, dismissed or deleted is not restored, as unmarking that pair
    would have cleared it. Each member with a drop gets a "linked" -> "unlinked"
    history row `only_for` them after the restore's "unlinked" -> "linked" row,
    as the pair rule writes it, so their history ends unlinked.
    """
    pair_ids = {pair_id for _person_id, pair_id in saved_drops}
    still_marked = set(_marked_pairs_touching((link.original_id,)).filter(pk__in=pair_ids).values_list("pk", flat=True))
    restored_for = set()
    for person_id, pair_id in saved_drops:
        if pair_id not in still_marked:
            continue
        RefundLinkDrop.objects.create(link=link, person_id=person_id, pair_id=pair_id)
        if person_id not in restored_for:
            restored_for.add(person_id)
            _record_member_link_history(link.refund, actor, person_id, "linked", "unlinked")


def restore_dropped_refund_links(original, saved_links, refunds_by_id, actor):
    """Link refunds back to `original` after a bulk mark is undone (#797).

    Owner decision on #679: undoing a bulk untracked-transfer mark is a full
    undo. `saved_links` are the links the mark dropped, each with the refund's
    `updated_at` read after the drop, its `RefundLinkDrop` rows and the marked
    pairs touching either row then. A link comes back only when nothing has
    moved since: the refund is still active, unlinked and unedited, a split part
    still exists, and no marked pair touches either row that did not touch it at
    the mark. Any other link is skipped, so the refund keeps counting as income,
    and the undo goes on: a refusal would tell the actor that a row they may not
    see has changed. A restored link gets its stored drops back verbatim
    (`_restore_drops`). The caller holds the row locks of `original` and of
    every refund in `refunds_by_id`, and has already unmarked `original`.
    Returns the number of links restored.
    """
    if not saved_links:
        return 0
    refund_ids = [item["refund_id"] for item in saved_links]
    linked = set(RefundLink.objects.filter(refund_id__in=refund_ids).values_list("refund_id", flat=True))
    part_ids = set(TransactionSplit.objects.filter(transaction=original).values_list("pk", flat=True))
    restored = 0
    for item in saved_links:
        refund = refunds_by_id.get(item["refund_id"])
        part_id = item["original_part_id"]
        if (
            refund is None
            or refund.status != Transaction.Status.ACTIVE
            or refund.updated_at.isoformat() != item["refund_updated_at"]
            or refund.pk in linked
            or (part_id is not None and part_id not in part_ids)
            or set(_marked_pairs_touching((original.pk, refund.pk)).values_list("pk", flat=True))
            - set(item.get("marked_pair_ids", []))
        ):
            continue
        link = RefundLink.objects.create(refund=refund, original=original, original_part_id=part_id)
        record_text_history(refund, actor, TransactionCorrectionHistory.Field.REFUND_LINK, "unlinked", "linked")
        _restore_drops(link, item.get("drops", []), actor)
        restored += 1
    if restored:
        restore_refund_categories(original, actor)
    return restored


def restore_refund_categories(original, actor, record_history=record_text_history):
    """Keep linked refunds in their original's category, as assign_category does.

    A refund linked to a part of a split original follows that part, since a
    split original's own category is empty. A refund already there is untouched.
    """
    refunds = (
        refunds_following_originals(
            actor,
            Transaction.objects.filter(refund_link__original=original, status=Transaction.Status.ACTIVE),
        )
        .select_for_update(of=("self",))
        .select_related("account", "category")
        .order_by("pk")
    )
    links = RefundLink.objects.filter(original=original).select_related("refund", "original_part__category")
    parts = {link.refund.pk: link.original_part for link in links}
    whole_category = None if original.category_id is None else Category.objects.get(pk=original.category_id)
    for refund in refunds:
        part = parts.get(refund.pk)
        new_category = whole_category if part is None else part.category
        if refund.category != new_category:
            apply_inherited_category(actor, refund, new_category, record_history)


def refund_rows_touching(transaction_ids):
    if not transaction_ids:
        return []
    return list(
        RefundLink.objects.filter(Q(refund_id__in=transaction_ids) | Q(original_id__in=transaction_ids)).values_list(
            "pk", "refund_id", "original_id"
        )
    )


def account_ids_in_refunds_touching_transactions(transaction_ids):
    """Account pks for every side of refund links that include any of these transactions."""
    rows = refund_rows_touching(transaction_ids)
    if not rows:
        return set()
    all_ids = {tx_id for _pk, refund_id, original_id in rows for tx_id in (refund_id, original_id)}
    return set(Transaction.objects.filter(pk__in=all_ids).values_list("account_id", flat=True))


@transaction.atomic
def link_refund(principal, refund_id, original_id, original_part_id=None):
    person = _person_for(principal)
    if refund_id == original_id:
        raise PermissionDenied(_DENIED)
    refund = (
        visible_rows(Transaction, person)
        .filter(pk=refund_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    original = (
        visible_rows(Transaction, person)
        .filter(pk=original_id, status=Transaction.Status.ACTIVE)
        .select_related("account", "category")
        .first()
    )
    if refund is None or original is None:
        raise PermissionDenied(_DENIED)
    locked = lock_accounts_and_transactions(person, [refund, original])
    by_id = {item.pk: item for item in locked}
    refund = by_id[refund_id]
    original = by_id[original_id]
    existing = RefundLink.objects.filter(refund=refund).first()
    if existing is not None:
        # Owner decision 2026-10-23, option (c): a link dropped for this member
        # alone still stands for another, so they cannot relink beside it.
        if RefundLinkDrop.objects.filter(link=existing, person=person).exists():
            raise ValidationError(REFUND_LINK_STANDS)
        raise PermissionDenied(_DENIED)
    if (
        refund.amount_minor <= 0
        or original.amount_minor >= 0
        or refund.kind != original.kind
        # Excluded legs were never counted, so a refund cannot offset one.
        # The check sees only pairs whose legs are both visible to this person.
        or Transaction.objects.filter(pk__in=(refund.pk, original.pk))
        .filter(exclusion_exists_for(person))
        .exists()
    ):
        raise ValidationError(REFUND_LINK_RULE)
    inherited = original.category
    original_part = None
    if original.category_source == Transaction.CategorySource.SPLIT:
        if original_part_id is None:
            raise ValidationError(SPLIT_PART_REQUIRED)
        original_part = (
            TransactionSplit.objects.filter(transaction=original, pk=original_part_id).select_related("category").first()
        )
        if original_part is None:
            raise ValidationError(SPLIT_PART_MISMATCH)
        inherited = original_part.category
    elif original_part_id is not None:
        raise ValidationError(SPLIT_PART_MISMATCH)
    previous_category = refund.category
    refund.category = inherited
    refund.category_source = Transaction.CategorySource.INHERITED
    refund.save(update_fields=("category", "category_source", "updated_at"))
    link = RefundLink.objects.create(refund=refund, original=original, original_part=original_part)
    record_text_history(
        refund,
        person,
        TransactionCorrectionHistory.Field.REFUND_LINK,
        "unlinked",
        "linked",
    )
    # The original may be a marked leg for another viewer of the refund, as for
    # a link made before the pairing; that viewer's link is dropped the same way.
    marked = TransferPair.objects.excluding_income_and_spending()  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for the other excluding_income_and_spending calls in the baseline
    for pair in marked.filter(Q(leg_a=original) | Q(leg_b=original)).select_related("leg_a__account", "leg_b__account"):
        drop_refund_links_for_pair([link], pair, (pair.leg_a, pair.leg_b), person)
    record_text_history(
        refund,
        person,
        TransactionCorrectionHistory.Field.CATEGORY,
        history_label(previous_category),
        history_label(inherited),
    )
    return refund
