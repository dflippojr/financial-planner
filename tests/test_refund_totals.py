"""A refund link goes when its original stops counting (#570).

The original stops counting when its import is undone, when it is deleted as a
manual entry, or when it becomes a confirmed or auto-marked transfer or
card-payment leg. The link is deleted with correction history on the refund,
so the refund counts as ordinary income and spending never goes negative.
"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from finance.category_services import (
    confirm_transfer_pair,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
    undo_transfer_pair,
)
from finance.csv_import.services import undo_import_batch
from finance.lifecycle_services import archive_account
from finance.manual_entry_services import add_manual_transaction, delete_manual_transaction
from finance.models import (
    Account,
    AuditEvent,
    RefundLink,
    TransactionCorrectionHistory,
    TransferPair,
)
from tests.test_categorization import make_account, make_household, make_person, make_transaction

pytestmark = pytest.mark.django_db


def _totals(person):
    totals = income_and_spending_totals(person)
    return totals.income_minor, totals.spending_minor


def _link_history(refund):
    return list(
        TransactionCorrectionHistory.objects.filter(
            transaction=refund, field_name=TransactionCorrectionHistory.Field.REFUND_LINK
        )
        .order_by("pk")
        .values_list("previous_description", "new_description")
    )


def _assert_unlinked_income(owner, refund, *, income=2000, spending=0):
    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _totals(owner) == (income, spending)
    assert _link_history(refund) == [("unlinked", "linked"), ("linked", "unlinked")]


def _checking_with_linked_refund(outflow_description):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    outflow = make_transaction(owner, checking, amount_minor=-5000, description=outflow_description)
    refund = make_transaction(owner, checking, amount_minor=2000, description="Synthetic return credit")
    link_refund(owner, refund.pk, outflow.pk)
    assert _totals(owner) == (0, 3000)
    return owner, savings, outflow, refund


def test_confirming_the_original_as_a_transfer_leg_drops_its_refund_link():
    owner, savings, _outflow, refund = _checking_with_linked_refund("Synthetic purchase")
    make_transaction(owner, savings, amount_minor=5000, description="Synthetic deposit")
    refresh_transfer_pairs(owner)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.SUGGESTED

    confirm_transfer_pair(owner, pair.pk)

    _assert_unlinked_income(owner, refund)


def test_an_auto_marked_original_drops_its_refund_link():
    owner, savings, _outflow, refund = _checking_with_linked_refund("Synthetic transfer to savings")
    make_transaction(owner, savings, amount_minor=5000, description="Synthetic transfer from checking")

    refresh_transfer_pairs(owner)

    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    _assert_unlinked_income(owner, refund)


def test_undoing_the_pair_does_not_bring_the_link_back():
    owner, savings, _outflow, refund = _checking_with_linked_refund("Synthetic transfer to savings")
    make_transaction(owner, savings, amount_minor=5000, description="Synthetic transfer from checking")
    refresh_transfer_pairs(owner)

    undo_transfer_pair(owner, TransferPair.objects.get().pk)

    # Both legs count again, so the +50.00 deposit is income beside the refund.
    _assert_unlinked_income(owner, refund, income=7000, spending=5000)


def test_a_dropped_refund_keeps_its_category():
    owner, savings, _outflow, refund = _checking_with_linked_refund("Synthetic transfer to savings")
    refund.refresh_from_db()
    category, source = refund.category_id, refund.category_source
    make_transaction(owner, savings, amount_minor=5000, description="Synthetic transfer from checking")

    refresh_transfer_pairs(owner)

    refund.refresh_from_db()
    assert (refund.category_id, refund.category_source) == (category, source)


def test_undoing_the_originals_import_drops_its_refund_link():
    owner, _savings, outflow, refund = _checking_with_linked_refund("Synthetic purchase")

    undo_import_batch(owner, outflow.account_id, outflow.import_batch_id)

    _assert_unlinked_income(owner, refund)


def test_deleting_a_manual_original_drops_its_refund_link():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    outflow = add_manual_transaction(
        owner, checking.pk, transaction_date=date(2026, 1, 2), amount_minor=-5000, description="SYNTHETIC GROCER"
    )
    refund = make_transaction(owner, checking, amount_minor=2000, description="Synthetic return credit")
    link_refund(owner, refund.pk, outflow.pk)

    delete_manual_transaction(owner, outflow.pk)

    _assert_unlinked_income(owner, refund)


def test_linking_after_pairing_ends_the_same_way():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    outflow = make_transaction(owner, checking, amount_minor=-5000, description="Synthetic transfer to savings")
    refund = make_transaction(owner, checking, amount_minor=2000, description="Synthetic return credit")
    make_transaction(owner, savings, amount_minor=5000, description="Synthetic transfer from checking")
    refresh_transfer_pairs(owner)

    with pytest.raises(ValidationError):
        link_refund(owner, refund.pk, outflow.pk)

    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _totals(owner) == (2000, 0)


def test_a_hidden_private_refund_loses_its_link_without_the_actor_learning_of_it():
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    joint = make_account(alice, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    hidden = make_account(bob, name="SYN-HIDDEN-PRIVATE")
    purchase = make_transaction(alice, joint, amount_minor=-5000, description="Synthetic shared purchase")
    refund = make_transaction(bob, hidden, amount_minor=2000, description="SYN-HIDDEN-REFUND")
    link_refund(bob, refund.pk, purchase.pk)
    assert _totals(bob) == (0, 3000)
    events_before = set(AuditEvent.objects.values_list("pk", flat=True))

    client = Client()
    client.force_login(alice.user)
    response = client.post(
        reverse("csv-import-undo", args=[joint.pk, purchase.import_batch_id]), follow=True
    )

    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _totals(bob) == (2000, 0)
    history = TransactionCorrectionHistory.objects.filter(
        transaction=refund, field_name=TransactionCorrectionHistory.Field.REFUND_LINK
    ).latest("pk")
    assert (history.previous_description, history.new_description, history.actor_id) == ("linked", "unlinked", alice.pk)
    new_events = AuditEvent.objects.exclude(pk__in=events_before)
    assert list(new_events.values_list("action", flat=True)) == [AuditEvent.Action.IMPORT_UNDONE]
    for event in new_events:
        assert event.account_id == joint.pk
        assert str(refund.pk) not in str(event.metadata)
        assert "SYN-HIDDEN" not in str(event.metadata)
    assert response.status_code == 200
    page = response.content.decode()
    assert "SYN-HIDDEN" not in page
    assert "20.00" not in page


def test_archiving_the_originals_account_drops_its_refund_link():
    owner = make_person("owner")
    make_household(owner)
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    checking = make_account(owner, name="Synthetic Checking")
    outflow = make_transaction(owner, card, amount_minor=-5000, description="Synthetic purchase")
    refund = make_transaction(owner, checking, amount_minor=2000, description="Synthetic return credit")
    link_refund(owner, refund.pk, outflow.pk)

    archive_account(owner, card.pk)

    _assert_unlinked_income(owner, refund)


# A pair with a leg in one member's private account is excluded only for that
# member, so the link goes only for them (#570, owner decision 2026-10-23,
# option a). Bob and the household scope keep the link, totals and history.


def _household_refund_linked_by_bob(outflow_description, deposit_description):
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    joint = make_account(alice, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    savings = make_account(alice, name="Synthetic Private Savings", account_type=Account.Type.SAVINGS)
    purchase = make_transaction(alice, joint, amount_minor=-5000, description=outflow_description)
    refund = make_transaction(alice, joint, amount_minor=2000, description="Synthetic return credit")
    link_refund(bob, refund.pk, purchase.pk)
    make_transaction(alice, savings, amount_minor=5000, description=deposit_description)
    assert _totals(bob) == (0, 3000)
    assert _totals(alice) == (5000, 3000)
    return alice, bob, joint, refund


def _visible_link_history(person, refund):
    return list(
        TransactionCorrectionHistory.objects.visible_to(person)
        .filter(transaction=refund, field_name=TransactionCorrectionHistory.Field.REFUND_LINK)
        .order_by("pk")
        .values_list("previous_description", "new_description")
    )


def _assert_dropped_only_for_alice(alice, bob, joint, refund, events_before):
    assert _totals(alice) == (2000, 0)
    assert _totals(bob) == (0, 3000)
    for person in (alice, bob):
        household_scope = income_and_spending_totals(person, accounts=[joint])
        assert (household_scope.income_minor, household_scope.spending_minor) == (0, 3000)
    assert RefundLink.objects.visible_to(bob).filter(refund=refund).exists()
    assert not RefundLink.objects.visible_to(alice).filter(refund=refund).exists()
    assert _visible_link_history(bob, refund) == [("unlinked", "linked")]
    assert _visible_link_history(alice, refund) == [("unlinked", "linked"), ("linked", "unlinked")]
    new_events = AuditEvent.objects.exclude(pk__in=events_before)
    assert not new_events.filter(target_type=AuditEvent.TargetType.TRANSACTION, target_id=refund.pk).exists()


def test_confirming_a_private_leg_pair_drops_the_link_only_for_the_member_who_sees_both_legs():
    alice, bob, joint, refund = _household_refund_linked_by_bob("Synthetic purchase", "Synthetic deposit")
    refresh_transfer_pairs(alice)
    pair = TransferPair.objects.get()
    assert pair.status == TransferPair.Status.SUGGESTED
    events_before = set(AuditEvent.objects.values_list("pk", flat=True))

    confirm_transfer_pair(alice, pair.pk)

    _assert_dropped_only_for_alice(alice, bob, joint, refund, events_before)


def test_auto_marking_a_private_leg_pair_drops_the_link_only_for_the_member_who_sees_both_legs():
    alice, bob, joint, refund = _household_refund_linked_by_bob(
        "Synthetic transfer to savings", "Synthetic transfer from checking"
    )
    events_before = set(AuditEvent.objects.values_list("pk", flat=True))

    refresh_transfer_pairs(alice)

    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    _assert_dropped_only_for_alice(alice, bob, joint, refund, events_before)


def test_undoing_a_private_leg_pair_keeps_the_link_dropped_for_its_owner():
    alice, bob, joint, refund = _household_refund_linked_by_bob(
        "Synthetic transfer to savings", "Synthetic transfer from checking"
    )
    refresh_transfer_pairs(alice)

    undo_transfer_pair(alice, TransferPair.objects.get().pk)

    # The purchase and deposit count again for Alice; the refund stays income.
    assert _totals(alice) == (7000, 5000)
    assert _totals(bob) == (0, 3000)


def test_a_link_made_after_a_private_leg_pairing_is_dropped_for_the_member_who_sees_both_legs():
    """F2 in the review: Bob cannot see the pair, so his link succeeds; it must not count for Alice."""
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    joint = make_account(alice, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    savings = make_account(alice, name="Synthetic Private Savings", account_type=Account.Type.SAVINGS)
    purchase = make_transaction(alice, joint, amount_minor=-5000, description="Synthetic transfer to savings")
    refund = make_transaction(alice, joint, amount_minor=2000, description="Synthetic return credit")
    make_transaction(alice, savings, amount_minor=5000, description="Synthetic transfer from checking")
    refresh_transfer_pairs(alice)
    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    with pytest.raises(ValidationError):
        link_refund(alice, refund.pk, purchase.pk)
    events_before = set(AuditEvent.objects.values_list("pk", flat=True))

    link_refund(bob, refund.pk, purchase.pk)

    assert _totals(alice) == (2000, 0)
    assert _totals(bob) == (0, 3000)
    assert not RefundLink.objects.visible_to(alice).filter(refund=refund).exists()
    assert _visible_link_history(bob, refund) == [("unlinked", "linked")]
    assert _visible_link_history(alice, refund) == [("unlinked", "linked"), ("linked", "unlinked")]
    dropped = TransactionCorrectionHistory.objects.filter(
        transaction=refund, field_name=TransactionCorrectionHistory.Field.REFUND_LINK, new_description="unlinked"
    )
    new_events = AuditEvent.objects.exclude(pk__in=events_before)
    assert not new_events.filter(metadata__history_id__in=list(dropped.values_list("pk", flat=True))).exists()


def test_a_private_refund_is_untouched_by_a_pair_its_owner_cannot_see():
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    joint = make_account(alice, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    savings = make_account(alice, name="Synthetic Private Savings", account_type=Account.Type.SAVINGS)
    bobs = make_account(bob, name="Synthetic Bob Checking")
    purchase = make_transaction(alice, joint, amount_minor=-5000, description="Synthetic transfer to savings")
    refund = make_transaction(bob, bobs, amount_minor=2000, description="Synthetic return credit")
    link_refund(bob, refund.pk, purchase.pk)
    make_transaction(alice, savings, amount_minor=5000, description="Synthetic transfer from checking")

    refresh_transfer_pairs(alice)

    assert TransferPair.objects.get().status == TransferPair.Status.AUTO_MARKED
    assert _totals(bob) == (0, 3000)
    assert RefundLink.objects.visible_to(bob).filter(refund=refund).exists()
    assert _link_history(refund) == [("unlinked", "linked")]
