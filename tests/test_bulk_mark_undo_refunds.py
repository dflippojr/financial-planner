"""Undoing a bulk untracked-transfer mark restores the refund links it dropped (#797).

Owner decision on #679: bulk mark undo is a full undo. Rule reversal and
single unmarking keep their behavior and leave a dropped link dropped.
All data is synthetic.
"""

import pytest
from django.utils import timezone

from finance.bulk_edit_services import ACTION_MARK_UNTRACKED, apply_bulk_edit, undo_bulk_edit
from finance.category_services import assign_category
from finance.models import Category, RefundLink, Transaction, TransactionCorrectionHistory
from finance.refund_links import link_refund
from finance.rule_services import apply_rule, reverse_application
from finance.splits import split_transaction
from finance.untracked_transfers import mark_untracked_transfer, unmark_untracked_transfer
from tests.test_untracked_transfers import _totals, _untracked_rule, world  # noqa: F401 - pytest fixture

pytestmark = pytest.mark.django_db


def _purchase_with_refund(world, description="SYNTHETIC STORE"):
    owner = world["owner"]
    purchase = world["txn"](world["shared"], -6_000, description)
    refund = world["txn"](world["shared"], 1_500, f"{description} REFUND")
    assign_category(owner, purchase.pk, world["shopping"].pk)
    link_refund(owner, refund.pk, purchase.pk)
    return purchase, refund


def _bulk_mark(person, rows):
    _preview, undo = apply_bulk_edit(
        person, matching=Transaction.objects.none(), transaction_ids=[row.pk for row in rows],
        action=ACTION_MARK_UNTRACKED,
    )
    return undo


def _link_history(refund):
    return list(
        TransactionCorrectionHistory.objects.filter(
            transaction=refund, field_name=TransactionCorrectionHistory.Field.REFUND_LINK
        )
        .order_by("pk")
        .values_list("previous_description", "new_description")
    )


def test_undoing_a_bulk_mark_restores_the_refund_link_and_totals(world):
    owner, member = world["owner"], world["member"]
    purchase, refund = _purchase_with_refund(world)
    before = {person.pk: _totals(person) for person in (owner, member)}

    undo = _bulk_mark(member, [purchase])
    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _totals(owner) != before[owner.pk]

    undo_bulk_edit(member, undo.pk)

    link = RefundLink.objects.get(refund=refund)
    assert link.original_id == purchase.pk
    assert link.original_part_id is None
    refund.refresh_from_db()
    assert refund.category == world["shopping"]
    assert refund.category_source == Transaction.CategorySource.INHERITED
    assert {person.pk: _totals(person) for person in (owner, member)} == before
    assert _link_history(refund) == [("unlinked", "linked"), ("linked", "unlinked"), ("unlinked", "linked")]


def test_undo_restores_a_link_to_a_split_part(world):
    owner, household = world["owner"], world["household"]
    dining = Category.objects.get(household=household, name="Dining")
    purchase, refund = _purchase_with_refund(world)
    RefundLink.objects.filter(refund=refund).delete()
    split_transaction(
        owner, purchase.pk,
        [{"amount_minor": -4_000, "category_id": world["shopping"].pk},
         {"amount_minor": -2_000, "category_id": dining.pk}],
    )
    purchase.refresh_from_db()
    dining_part = purchase.splits.get(category=dining)
    link_refund(owner, refund.pk, purchase.pk, dining_part.pk)
    before = _totals(owner)

    undo = _bulk_mark(owner, [purchase])
    assert not RefundLink.objects.filter(refund=refund).exists()
    undo_bulk_edit(owner, undo.pk)

    link = RefundLink.objects.get(refund=refund)
    assert (link.original_id, link.original_part_id) == (purchase.pk, dining_part.pk)
    refund.refresh_from_db()
    assert (refund.category, refund.category_source) == (dining, Transaction.CategorySource.INHERITED)
    assert _totals(owner) == before


def _relink_elsewhere(world, refund):
    other = world["txn"](world["shared"], -9_000, "SYNTHETIC OTHER STORE")
    link_refund(world["owner"], refund.pk, other.pk)


def _recategorize(world, refund):
    assign_category(world["owner"], refund.pk, world["income"].pk)


def _mark_refund(world, refund):
    mark_untracked_transfer(world["owner"], refund.pk)


def _archive(_world, refund):
    Transaction.objects.filter(pk=refund.pk).update(status=Transaction.Status.ARCHIVED, archived_at=timezone.now())


@pytest.mark.parametrize("change", [_relink_elsewhere, _recategorize, _mark_refund, _archive])
def test_undo_skips_a_link_whose_refund_changed_since(world, change):
    owner = world["owner"]
    purchase, refund = _purchase_with_refund(world)
    kept_purchase, kept_refund = _purchase_with_refund(world, "SYNTHETIC BOOKSHOP")
    undo = _bulk_mark(owner, [purchase, kept_purchase])

    change(world, refund)
    refund.refresh_from_db()
    state = (refund.category_id, refund.category_source, refund.untracked_transfer, refund.status)
    link_before = list(RefundLink.objects.filter(refund=refund).values_list("original_id", flat=True))
    history_before = _link_history(refund)

    undo_bulk_edit(owner, undo.pk)

    # The marks are undone and the untouched link comes back; the changed refund is left as it is.
    assert not Transaction.objects.filter(pk__in=(purchase.pk, kept_purchase.pk), untracked_transfer=True).exists()
    assert RefundLink.objects.get(refund=kept_refund).original_id == kept_purchase.pk
    assert list(RefundLink.objects.filter(refund=refund).values_list("original_id", flat=True)) == link_before
    refund.refresh_from_db()
    assert (refund.category_id, refund.category_source, refund.untracked_transfer, refund.status) == state
    assert _link_history(refund) == history_before


def test_single_unmarking_still_leaves_a_dropped_link_dropped(world):
    owner = world["owner"]
    purchase, refund = _purchase_with_refund(world)
    mark_untracked_transfer(owner, purchase.pk)
    unmark_untracked_transfer(owner, purchase.pk)
    assert not RefundLink.objects.filter(refund=refund).exists()


def test_rule_reversal_still_leaves_a_dropped_link_dropped(world):
    owner = world["owner"]
    purchase, refund = _purchase_with_refund(world)
    application, _ = apply_rule(owner, _untracked_rule(owner, "synthetic store").pk)
    purchase.refresh_from_db()
    assert purchase.untracked_transfer is True
    assert reverse_application(owner, application.pk).restored == 1
    purchase.refresh_from_db()
    assert purchase.untracked_transfer is False
    assert not RefundLink.objects.filter(refund=refund).exists()
