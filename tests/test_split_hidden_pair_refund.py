"""A pair unmarked behind a member's back must not move or explain their refund (#580).

Alice's private savings and a household purchase form a marked transfer pair
that Bob cannot see. When unmarking it restores the linked refund's category,
the refund takes the linked part's category, not the split original's empty one.
Nothing written for it may be listed to Bob.
"""

import pytest

from finance.audit_models import AuditEvent
from finance.category_services import (
    assign_category,
    link_refund,
    split_transaction,
    undo_transfer_pair,
)
from finance.models import Account, Transaction, TransactionCorrectionHistory, TransferPair
from tests.test_categorization import make_account, make_household, make_person, make_transaction

pytestmark = pytest.mark.django_db


class Scenario:
    def __init__(self, *, private_leg=True, inflow_first=False):
        self.alice = make_person("alice")
        self.bob = make_person("bob")
        household = make_household(self.alice, self.bob)
        self.groceries = household.categories.get(name="Groceries")
        self.dining = household.categories.get(name="Dining")
        self.housing = household.categories.get(name="Housing")
        joint = make_account(self.bob, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
        other_account = (
            make_account(self.alice, name="Synthetic Alice Savings", account_type=Account.Type.SAVINGS)
            if private_leg
            else make_account(
                self.alice,
                name="Synthetic Shared Savings",
                account_type=Account.Type.SAVINGS,
                scope=Account.Scope.HOUSEHOLD,
                household=household,
            )
        )
        if inflow_first:
            self.inflow = make_transaction(self.alice, other_account, amount_minor=5000, description="Synthetic in")
        self.purchase = make_transaction(self.bob, joint, amount_minor=-5000, description="Synthetic market")
        self.refund = make_transaction(self.bob, joint, amount_minor=2000, description="Synthetic market refund")
        if not inflow_first:
            self.inflow = make_transaction(self.alice, other_account, amount_minor=5000, description="Synthetic in")

    def mark_pair(self, category=None):
        """Marked after the purchase is categorized, so the snapshot holds `category`."""
        if category is not None:
            assign_category(self.bob, self.purchase.pk, category.pk)
        first, second = sorted((self.purchase, self.inflow), key=lambda item: item.pk)
        snapshot = None if category is None else category.pk
        purchase_is_a = first.pk == self.purchase.pk
        self.pair = TransferPair.objects.create(
            leg_a=first,
            leg_b=second,
            status=TransferPair.Status.CONFIRMED,
            kind=TransferPair.Kind.TRANSFER,
            confidence=TransferPair.Confidence.HIGH,
            reasons=["synthetic"],
            leg_a_category_id_at_mark=snapshot if purchase_is_a else None,
            leg_b_category_id_at_mark=None if purchase_is_a else snapshot,
        )

    def link(self, member=None):
        link_refund(member or self.bob, self.refund.pk, self.purchase.pk)

    def split(self):
        split_transaction(
            self.bob,
            self.purchase.pk,
            [
                {"category_id": self.groceries.pk, "amount_minor": -3000},
                {"category_id": self.dining.pk, "amount_minor": -2000},
            ],
            refund_assignments={self.refund.pk: 0},
        )


def _category_rows(viewer, txn):
    return list(
        TransactionCorrectionHistory.objects.visible_to(viewer)
        .filter(transaction=txn, field_name=TransactionCorrectionHistory.Field.CATEGORY)
        .order_by("pk")
        .values_list("previous_description", "new_description")
    )


def _refund_audit_events(txn):
    return AuditEvent.objects.filter(
        action=AuditEvent.Action.TRANSACTION_CORRECTED,
        target_type=AuditEvent.TargetType.TRANSACTION,
        target_id=txn.pk,
    ).count()


def test_split_that_unmarks_a_hidden_pair_keeps_the_refunds_part_category():
    scene = Scenario()
    scene.mark_pair()
    scene.link()
    assert _category_rows(scene.bob, scene.refund) == []
    events = _refund_audit_events(scene.refund)

    scene.split()

    scene.pair.refresh_from_db()
    assert scene.pair.status == TransferPair.Status.UNDONE
    scene.refund.refresh_from_db()
    assert scene.refund.category_id == scene.groceries.pk
    assert [new for _previous, new in _category_rows(scene.bob, scene.refund)] == ["Groceries"]
    assert _refund_audit_events(scene.refund) == events + 1, "only the part assignment is audited"


def test_split_does_not_restore_the_snapshot_onto_the_split_leg_or_its_refund():
    scene = Scenario()
    scene.mark_pair(category=scene.housing)
    scene.link()
    purchase_rows = _category_rows(scene.bob, scene.purchase)

    scene.split()

    scene.purchase.refresh_from_db()
    assert scene.purchase.category_id is None
    assert scene.purchase.category_source == Transaction.CategorySource.SPLIT
    new_purchase_rows = _category_rows(scene.bob, scene.purchase)[len(purchase_rows) :]
    assert len(new_purchase_rows) == 1, new_purchase_rows
    assert new_purchase_rows[0][1].startswith("Split: ")
    scene.refund.refresh_from_db()
    assert scene.refund.category_id == scene.groceries.pk


def test_no_row_when_the_refund_already_has_the_parts_category():
    scene = Scenario()
    scene.mark_pair()
    scene.link()
    assign_category(scene.bob, scene.purchase.pk, scene.groceries.pk)
    scene.refund.refresh_from_db()
    assert scene.refund.category_id == scene.groceries.pk
    rows = _category_rows(scene.bob, scene.refund)
    events = _refund_audit_events(scene.refund)

    scene.split()

    scene.pair.refresh_from_db()
    assert scene.pair.status == TransferPair.Status.UNDONE
    assert _category_rows(scene.bob, scene.refund) == rows
    assert _category_rows(scene.alice, scene.refund) == rows
    assert _refund_audit_events(scene.refund) == events


def test_undoing_a_hidden_pair_lists_the_refund_change_only_to_who_sees_the_pair():
    scene = Scenario()
    scene.mark_pair(category=scene.groceries)
    scene.link()
    assign_category(scene.bob, scene.refund.pk, scene.dining.pk)
    bob_rows = _category_rows(scene.bob, scene.refund)
    alice_rows = _category_rows(scene.alice, scene.refund)
    events = _refund_audit_events(scene.refund)

    undo_transfer_pair(scene.alice, scene.pair.pk)

    scene.refund.refresh_from_db()
    assert scene.refund.category_id == scene.groceries.pk
    assert _category_rows(scene.bob, scene.refund) == bob_rows
    assert _category_rows(scene.alice, scene.refund) == [*alice_rows, ("Dining", "Groceries")]
    assert _refund_audit_events(scene.refund) == events


def test_undoing_a_hidden_pair_lists_the_leg_restore_only_to_who_sees_the_pair():
    scene = Scenario()
    scene.mark_pair(category=scene.groceries)
    assign_category(scene.bob, scene.purchase.pk, scene.dining.pk)
    bob_rows = _category_rows(scene.bob, scene.purchase)
    events = _refund_audit_events(scene.purchase)

    undo_transfer_pair(scene.alice, scene.pair.pk)

    scene.purchase.refresh_from_db()
    assert scene.purchase.category_id == scene.groceries.pk
    assert _category_rows(scene.bob, scene.purchase) == bob_rows
    assert _category_rows(scene.alice, scene.purchase) == [*bob_rows, ("Dining", "Groceries")]
    assert _refund_audit_events(scene.purchase) == events


def test_undoing_a_pair_everyone_sees_still_records_for_everyone():
    scene = Scenario(private_leg=False)
    # Linked first: marking through the service deletes a link whose viewers all see the legs (#570).
    scene.link()
    scene.mark_pair(category=scene.groceries)
    assign_category(scene.bob, scene.refund.pk, scene.dining.pk)
    events = _refund_audit_events(scene.refund)

    undo_transfer_pair(scene.alice, scene.pair.pk)

    scene.refund.refresh_from_db()
    assert scene.refund.category_id == scene.groceries.pk
    assert _category_rows(scene.bob, scene.refund)[-1] == ("Dining", "Groceries")
    assert _category_rows(scene.alice, scene.refund)[-1] == ("Dining", "Groceries")
    assert _refund_audit_events(scene.refund) == events + 1


@pytest.mark.parametrize("inflow_first", [True, False])
def test_undoing_a_hidden_pair_writes_no_event_the_other_member_can_read(inflow_first):
    scene = Scenario(inflow_first=inflow_first)
    scene.mark_pair()
    bob_events = set(AuditEvent.objects.visible_to(scene.bob).values_list("pk", flat=True))
    alice_events = set(AuditEvent.objects.visible_to(scene.alice).values_list("pk", flat=True))

    undo_transfer_pair(scene.alice, scene.pair.pk)

    assert not AuditEvent.objects.visible_to(scene.bob).exclude(pk__in=bob_events).exists()
    assert AuditEvent.objects.visible_to(scene.alice).exclude(pk__in=alice_events).count() == 1
