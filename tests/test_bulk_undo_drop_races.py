"""Bulk mark undo against a concurrent member deletion (#797, review r4 on #809).

Undo locks the saved drops' member and pair rows before it checks them, so a
concurrent `delete_member_data` either lands first (undo skips the drop
visibly) or waits for the undo. Synthetic data only.
"""

from datetime import date

import pytest
from django.db import connection
from django.db.models import Q

from finance import bulk_edit_services, lifecycle_services
from finance.bulk_edit_services import ACTION_MARK_UNTRACKED, apply_bulk_edit, undo_bulk_edit
from finance.lifecycle_services import delete_account, delete_member_data, leave_household
from finance.models import (
    Account,
    Person,
    RefundLink,
    RefundLinkDrop,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)
from finance.refund_links import DROP_SKIPPED_MEMBER, DROP_SKIPPED_PAIR, SAVED_DROP_LABEL, link_refund
from finance.transfer_pairing import confirm_transfer_pair, refresh_transfer_pairs
from tests.lock_races import race_two_transactions
from tests.test_categorization import make_account, make_household, make_person, make_transaction

AUGUST = date(2026, 8, 12)


def _member_left_with_a_saved_drop():
    """A bulk mark whose saved drop is for a member who then left and deleted the pair's leg account.

    The purchase and refund sit in the remaining member's household account, so
    nothing the leaving member's deletion locks is a row the undo locks first.
    """
    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    shared = make_account(member, name="Member Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    savings = make_account(leaver, name="Leaver Savings", account_type=Account.Type.SAVINGS)
    purchase = make_transaction(member, shared, transaction_date=AUGUST, amount_minor=-6_000,
                                description="SYNTHETIC ONLINE TRANSFER TO SAVINGS")
    make_transaction(leaver, savings, transaction_date=AUGUST, amount_minor=6_000,
                     description="SYNTHETIC ONLINE TRANSFER FROM CHECKING")
    refresh_transfer_pairs(leaver)
    pair = TransferPair.objects.get(Q(leg_a=purchase) | Q(leg_b=purchase))
    if pair.status == TransferPair.Status.SUGGESTED:
        confirm_transfer_pair(leaver, pair.pk)
    refund = make_transaction(member, shared, transaction_date=AUGUST, amount_minor=1_500,
                              description="SYNTHETIC SAVINGS REFUND")
    link_refund(member, refund.pk, purchase.pk)
    # Only the leaver sees both legs, so the link is dropped for them alone.
    assert list(RefundLinkDrop.objects.values_list("person_id", "pair_id")) == [(leaver.pk, pair.pk)]
    _preview, undo = apply_bulk_edit(
        member, matching=Transaction.objects.none(), transaction_ids=[purchase.pk], action=ACTION_MARK_UNTRACKED,
    )
    leave_household(leaver)
    delete_account(leaver, savings.pk)
    assert not TransferPair.objects.filter(pk=pair.pk).exists()
    return leaver, member, purchase, refund, undo


def _skip_rows(refund):
    return sorted(
        TransactionCorrectionHistory.objects.filter(transaction=refund, previous_description=SAVED_DROP_LABEL)
        .values_list("only_for_id", "new_description")
    )


@pytest.mark.django_db
def test_undo_after_the_member_left_skips_their_drop_for_them_alone():
    leaver, member, purchase, refund, undo = _member_left_with_a_saved_drop()

    undo_bulk_edit(member, undo.pk)

    assert RefundLink.objects.get(refund=refund).original_id == purchase.pk
    assert not RefundLinkDrop.objects.exists()
    assert _skip_rows(refund) == [(leaver.pk, DROP_SKIPPED_PAIR)]


def _race(first_action, competing_action, pause_module, pause_name):
    """Run `first_action` until it returns from `pause_name`, then `competing_action`; both must succeed."""
    assert race_two_transactions(first_action, competing_action, pause_module, pause_name) == {
        "first": None, "competing": None,
    }


def _require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("row locks against a concurrent deletion can only be exercised on PostgreSQL")


@pytest.mark.django_db(transaction=True)
def test_a_member_deletion_that_lands_first_makes_undo_skip_the_drop_visibly():
    _require_postgres()
    leaver, member, purchase, refund, undo = _member_left_with_a_saved_drop()

    # The deletion has removed the member but not committed when the undo starts.
    _race(
        lambda: delete_member_data(leaver), lambda: undo_bulk_edit(member, undo.pk),
        lifecycle_services, "_delete_login_user",
    )

    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert RefundLink.objects.get(refund=refund).original_id == purchase.pk
    assert not RefundLinkDrop.objects.exists()
    # The member is gone and so is the pair, so the remaining member who sees the purchase is told.
    assert _skip_rows(refund) == [(member.pk, DROP_SKIPPED_MEMBER)]


@pytest.mark.django_db(transaction=True)
def test_a_member_deletion_waits_for_an_undo_that_locked_the_saved_drop_first():
    _require_postgres()
    leaver, member, purchase, refund, undo = _member_left_with_a_saved_drop()

    _race(
        lambda: undo_bulk_edit(member, undo.pk), lambda: delete_member_data(leaver),
        bulk_edit_services, "lock_saved_drop_rows",
    )

    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert RefundLink.objects.get(refund=refund).original_id == purchase.pk
    assert not RefundLinkDrop.objects.exists()
    # The undo told the leaver while they existed; their deletion then removed that row with them.
    assert _skip_rows(refund) == []
    assert not Transaction.objects.filter(pk=purchase.pk, untracked_transfer=True).exists()
