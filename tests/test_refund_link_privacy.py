"""A refund follows its original's category only while the same members see both (#567).

Unsharing the original's account, or the refund owner leaving the household,
moves the boundary between them. Category copying must stop there in both
directions, while the link itself still counts toward spending as before.
"""

import pytest

from finance.bulk_edit_services import ACTION_CATEGORY, apply_bulk_edit, undo_bulk_edit
from finance.category_services import (
    assign_category,
    income_and_spending_totals,
    link_refund,
    split_transaction,
    undo_transfer_pair,
)
from finance.lifecycle_services import leave_household, unshare_account
from finance.models import (
    Account,
    Category,
    RefundLink,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)
from finance.rule_services import apply_rule, reverse_application, save_category_rule
from tests.test_categorization import make_account, make_household, make_person, make_transaction

pytestmark = pytest.mark.django_db


def _category_history(txn):
    return list(
        TransactionCorrectionHistory.objects.filter(
            transaction=txn, field_name=TransactionCorrectionHistory.Field.CATEGORY
        ).values_list("pk", flat=True)
    )


def _unshared_original():
    """Alice's card and Joint are shared; the Joint refund links to a card purchase, then she unshares the card."""
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    groceries = household.categories.get(name="Groceries")
    housing = household.categories.get(name="Housing")
    secret = Category.objects.create(household=household, name="SYN-SECRET-MEDICAL")
    card = make_account(
        alice,
        name="Synthetic Card",
        account_type=Account.Type.CREDIT_CARD,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    joint = make_account(alice, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    purchase = make_transaction(alice, card, amount_minor=-5000, description="Synthetic pharmacy")
    refund = make_transaction(alice, joint, amount_minor=2000, description="Synthetic pharmacy refund")
    assign_category(alice, purchase.pk, groceries.pk)
    link_refund(alice, refund.pk, purchase.pk)
    unshare_account(alice, card.pk)
    assert not Transaction.objects.visible_to(bob).filter(pk=purchase.pk).exists()
    return alice, bob, purchase, refund, groceries, housing, secret


def test_private_recategorization_does_not_reach_the_shared_refund():
    alice, bob, purchase, refund, groceries, _housing, secret = _unshared_original()
    history = _category_history(refund)
    spending = income_and_spending_totals(bob).spending_minor

    assign_category(alice, purchase.pk, secret.pk)

    seen = Transaction.objects.visible_to(bob).get(pk=refund.pk)
    assert seen.category_id == groceries.pk, "private recategorization copied onto shared refund"
    assert _category_history(refund) == history
    assert income_and_spending_totals(bob).spending_minor == spending


def test_private_split_does_not_reach_the_shared_refund():
    alice, bob, purchase, refund, groceries, housing, secret = _unshared_original()
    history = _category_history(refund)

    split_transaction(
        alice,
        purchase.pk,
        [{"category_id": secret.pk, "amount_minor": -3000}, {"category_id": housing.pk, "amount_minor": -2000}],
        refund_assignments={refund.pk: 0},
    )

    seen = Transaction.objects.visible_to(bob).get(pk=refund.pk)
    assert seen.category_id == groceries.pk, "private split copied onto shared refund"
    assert _category_history(refund) == history


def _former_member_refund():
    """Alice links her private refund to a purchase on Bob's shared account, then leaves."""
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    shared = make_account(bob, name="Synthetic Bob Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(alice, name="Synthetic Alice Private")
    purchase = make_transaction(bob, shared, amount_minor=-5000, description="Synthetic kroger")
    refund = make_transaction(alice, private, amount_minor=2000, description="Synthetic store credit")
    Transaction.objects.filter(pk=purchase.pk).update(category=groceries)
    link_refund(alice, refund.pk, purchase.pk)
    refund.refresh_from_db()
    assert refund.category_id == groceries.pk
    leave_household(alice)
    return alice, bob, household, purchase, refund, groceries, dining


def _assert_untouched(alice, refund, groceries, history, spending):
    refund.refresh_from_db()
    assert refund.category_id == groceries.pk, "Bob rewrote Alice's private refund after she left"
    assert refund.category_source == Transaction.CategorySource.INHERITED
    assert _category_history(refund) == history
    assert income_and_spending_totals(alice).spending_minor == spending


def test_remaining_member_recategorization_leaves_former_member_refund_alone():
    alice, bob, _household, purchase, refund, groceries, dining = _former_member_refund()
    history = _category_history(refund)
    spending = income_and_spending_totals(alice).spending_minor

    assign_category(bob, purchase.pk, dining.pk)

    _assert_untouched(alice, refund, groceries, history, spending)


def test_remaining_member_bulk_edit_and_undo_leave_former_member_refund_alone():
    alice, bob, _household, purchase, refund, groceries, dining = _former_member_refund()
    history = _category_history(refund)
    spending = income_and_spending_totals(alice).spending_minor

    _preview, undo = apply_bulk_edit(
        bob,
        matching=Transaction.objects.visible_to(bob).filter(status=Transaction.Status.ACTIVE),
        transaction_ids=[purchase.pk],
        action=ACTION_CATEGORY,
        category_id=dining.pk,
    )
    _assert_untouched(alice, refund, groceries, history, spending)

    undo_bulk_edit(bob, undo.pk)
    purchase.refresh_from_db()
    assert purchase.category_id == groceries.pk
    _assert_untouched(alice, refund, groceries, history, spending)


def test_remaining_member_rule_apply_and_reverse_leave_former_member_refund_alone():
    alice, bob, _household, purchase, refund, groceries, dining = _former_member_refund()
    history = _category_history(refund)
    spending = income_and_spending_totals(alice).spending_minor
    rule = save_category_rule(
        bob,
        owner_kind="personal",
        description_contains="kroger",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=dining.pk,
        priority=0,
    )

    application, _skipped = apply_rule(bob, rule.pk)
    purchase.refresh_from_db()
    assert purchase.category_id == dining.pk
    _assert_untouched(alice, refund, groceries, history, spending)

    reverse_application(bob, application.pk)
    purchase.refresh_from_db()
    assert purchase.category_id == groceries.pk
    _assert_untouched(alice, refund, groceries, history, spending)


def test_remaining_member_pair_unmark_leaves_former_member_refund_alone():
    alice, bob, household, purchase, refund, groceries, _dining = _former_member_refund()
    savings = make_account(
        bob,
        name="Synthetic Bob Savings",
        account_type=Account.Type.SAVINGS,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    inflow = make_transaction(bob, savings, amount_minor=5000, description="Synthetic transfer in")
    first, second = sorted((purchase, inflow), key=lambda item: item.pk)
    pair = TransferPair.objects.create(
        leg_a=first,
        leg_b=second,
        status=TransferPair.Status.CONFIRMED,
        kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.HIGH,
        reasons=["synthetic"],
    )
    history = _category_history(refund)
    spending = income_and_spending_totals(alice).spending_minor

    undo_transfer_pair(bob, pair.pk)

    purchase.refresh_from_db()
    assert purchase.category_id is None
    _assert_untouched(alice, refund, groceries, history, spending)


def test_category_still_follows_while_the_same_members_see_both_sides():
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    card = make_account(alice, name="Synthetic Card", scope=Account.Scope.HOUSEHOLD, household=household)
    joint = make_account(bob, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(alice, name="Synthetic Alice Private")
    purchase = make_transaction(alice, card, amount_minor=-5000, description="Synthetic store")
    shared_refund = make_transaction(bob, joint, amount_minor=2000, description="Synthetic store refund")
    private_purchase = make_transaction(alice, private, amount_minor=-4000, description="Synthetic private store")
    private_refund = make_transaction(alice, private, amount_minor=1000, description="Synthetic private refund")
    assign_category(alice, purchase.pk, groceries.pk)
    assign_category(alice, private_purchase.pk, groceries.pk)
    link_refund(alice, shared_refund.pk, purchase.pk)
    link_refund(alice, private_refund.pk, private_purchase.pk)
    history = _category_history(shared_refund)

    assign_category(bob, purchase.pk, dining.pk)
    assign_category(alice, private_purchase.pk, dining.pk)

    shared_refund.refresh_from_db()
    private_refund.refresh_from_db()
    assert shared_refund.category_id == dining.pk
    assert private_refund.category_id == dining.pk
    assert len(_category_history(shared_refund)) == len(history) + 1


def test_member_never_writes_another_members_private_refund_but_its_owner_still_does():
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    housing = household.categories.get(name="Housing")
    shared = make_account(bob, name="Synthetic Bob Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(alice, name="Synthetic Alice Private")
    purchase = make_transaction(bob, shared, amount_minor=-9000, description="Synthetic warehouse")
    refund = make_transaction(alice, private, amount_minor=1500, description="Synthetic warehouse refund")
    assign_category(bob, purchase.pk, groceries.pk)
    link_refund(alice, refund.pk, purchase.pk)
    history = _category_history(refund)
    spending = income_and_spending_totals(alice).spending_minor

    assign_category(bob, purchase.pk, dining.pk)
    _assert_untouched(alice, refund, groceries, history, spending)

    split_transaction(
        bob,
        purchase.pk,
        [{"category_id": housing.pk, "amount_minor": -6000}, {"category_id": dining.pk, "amount_minor": -3000}],
    )
    _assert_untouched(alice, refund, groceries, history, spending)
    link = RefundLink.objects.get(refund=refund)
    assert link.original_part.category_id == housing.pk

    split_transaction(
        alice,
        purchase.pk,
        [{"category_id": dining.pk, "amount_minor": -6000}, {"category_id": housing.pk, "amount_minor": -3000}],
        refund_assignments={refund.pk: 1},
    )
    refund.refresh_from_db()
    assert refund.category_id == housing.pk
    assert len(_category_history(refund)) == len(history) + 1
