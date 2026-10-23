"""A refund link goes when its original stops counting (#570).

The original stops counting when its import is undone, when it is deleted as a
manual entry, or when it becomes a confirmed or auto-marked transfer or
card-payment leg. The link is deleted with correction history on the refund,
so the refund counts as ordinary income and spending never goes negative.
"""

import threading
from datetime import date
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from finance import category_services, lifecycle_services
from finance.category_services import (
    confirm_transfer_pair,
    downgrade_unevidenced_auto_marked_pairs,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
    undo_transfer_pair,
)
from finance.csv_import.services import undo_import_batch
from finance.lifecycle_services import archive_account, delete_account, leave_household
from finance.manual_entry_services import add_manual_transaction, delete_manual_transaction
from finance.models import (
    Account,
    AuditEvent,
    RefundLink,
    RefundLinkDrop,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)
from tests.lock_races import backend_pid, wait_until_blocked_by
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


def _unmark_by_undo(alice, savings, deposit):
    undo_transfer_pair(alice, TransferPair.objects.get().pk)


def _unmark_by_downgrade(alice, savings, deposit):
    # A description edit takes away the transfer wording the auto-mark relied on.
    Transaction.objects.filter(transfer_pairs_as_a__isnull=False).update(description="Synthetic purchase")
    Transaction.objects.filter(pk=deposit.pk).update(description="Synthetic deposit")
    downgrade_unevidenced_auto_marked_pairs()


def _unmark_by_undoing_the_deposit_import(alice, savings, deposit):
    undo_import_batch(alice, savings.pk, deposit.import_batch_id)


def _unmark_by_archiving_the_savings(alice, savings, deposit):
    archive_account(alice, savings.pk)


def _unmark_by_deleting_the_savings(alice, savings, deposit):
    delete_account(alice, savings.pk)


@pytest.mark.parametrize(
    ("unmark", "alice_after"),
    [
        (_unmark_by_undo, (5000, 3000)),
        (_unmark_by_downgrade, (5000, 3000)),
        (_unmark_by_undoing_the_deposit_import, (0, 3000)),
        (_unmark_by_archiving_the_savings, (0, 3000)),
        (_unmark_by_deleting_the_savings, (0, 3000)),
    ],
)
def test_unmarking_a_private_leg_pair_brings_the_link_back_for_its_owner(unmark, alice_after):
    """Owner decision 2026-10-23, option (a): a drop never outlives the pair that caused it."""
    alice, bob, joint, refund = _household_refund_linked_by_bob(
        "Synthetic transfer to savings", "Synthetic transfer from checking"
    )
    savings = Account.objects.get(name="Synthetic Private Savings")
    deposit = savings.transactions.get()
    refresh_transfer_pairs(alice)
    assert _totals(alice) == (2000, 0)
    events_before = set(AuditEvent.objects.values_list("pk", flat=True))

    unmark(alice, savings, deposit)

    assert not RefundLinkDrop.objects.exists()
    # Alice is back where she was before the mark, less any deposit that stopped counting.
    assert _totals(alice) == alice_after
    assert RefundLink.objects.visible_to(alice).filter(refund=refund).exists()
    assert _visible_link_history(alice, refund) == [
        ("unlinked", "linked"),
        ("linked", "unlinked"),
        ("unlinked", "linked"),
    ]
    # Bob saw nothing of the pair: his link, totals and history never moved.
    assert _totals(bob) == (0, 3000)
    assert RefundLink.objects.visible_to(bob).filter(refund=refund).exists()
    assert _visible_link_history(bob, refund) == [("unlinked", "linked")]
    new_events = AuditEvent.objects.exclude(pk__in=events_before)
    assert not new_events.filter(target_type=AuditEvent.TargetType.TRANSACTION, target_id=refund.pk).exists()


def test_unmarking_one_of_two_marked_pairs_keeps_the_link_dropped_for_the_other():
    """The database allows two marked pairs on one original; each holds its own drop."""
    alice, bob, joint, refund = _household_refund_linked_by_bob("Synthetic purchase", "Synthetic deposit")
    purchase = RefundLink.objects.get(refund=refund).original
    first_savings = Account.objects.get(name="Synthetic Private Savings")
    second_savings = make_account(alice, name="Synthetic Second Savings", account_type=Account.Type.SAVINGS)
    second_deposit = make_transaction(alice, second_savings, amount_minor=5000, description="Synthetic deposit")
    first = TransferPair.objects.create(
        leg_a=purchase,
        leg_b=first_savings.transactions.get(),
        status=TransferPair.Status.SUGGESTED,
        kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.LOW,
        reasons=[],
    )
    second = TransferPair.objects.create(
        leg_a=purchase,
        leg_b=second_deposit,
        status=TransferPair.Status.SUGGESTED,
        kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.LOW,
        reasons=[],
    )
    confirm_transfer_pair(alice, first.pk)
    confirm_transfer_pair(alice, second.pk)
    assert _totals(alice) == (2000, 0)

    undo_transfer_pair(alice, first.pk)

    # The purchase is still a leg of the second pair for Alice, so the refund stays income.
    assert _totals(alice) == (7000, 0)
    assert not RefundLink.objects.visible_to(alice).filter(refund=refund).exists()
    assert _visible_link_history(alice, refund) == [("unlinked", "linked"), ("linked", "unlinked")]

    undo_transfer_pair(alice, second.pk)

    assert _totals(alice) == (10000, 3000)
    assert RefundLink.objects.visible_to(alice).filter(refund=refund).exists()
    assert _visible_link_history(alice, refund) == [
        ("unlinked", "linked"),
        ("linked", "unlinked"),
        ("unlinked", "linked"),
    ]
    assert _totals(bob) == (0, 3000)
    assert _visible_link_history(bob, refund) == [("unlinked", "linked")]


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


def test_a_members_dropped_link_history_stays_hidden_after_the_link_is_deleted():
    """The member-only history row outlives the link: an undo later must not show Bob Alice's pairing."""
    alice, bob, joint, refund = _household_refund_linked_by_bob(
        "Synthetic transfer to savings", "Synthetic transfer from checking"
    )
    refresh_transfer_pairs(alice)
    purchase = RefundLink.objects.get(refund=refund).original

    undo_import_batch(bob, joint.pk, purchase.import_batch_id)

    assert not RefundLink.objects.filter(refund=refund).exists()
    assert _visible_link_history(bob, refund) == [("unlinked", "linked"), ("linked", "unlinked")]
    assert _visible_link_history(alice, refund) == [
        ("unlinked", "linked"),
        ("linked", "unlinked"),
        ("linked", "unlinked"),
    ]


# Archiving writes history on the refunds of its originals, which may sit in
# other accounts, so it locks those accounts up front, in pk order, as account
# delete does. Without that, two former household members whose private
# accounts still hold refunds linked across them could each lock their own rows
# and then wait on the other's refund.


def _cross_linked_accounts_of_former_members():
    alice = make_person("alice")
    bob = make_person("bob")
    household = make_household(alice, bob)
    lent = {"scope": Account.Scope.HOUSEHOLD, "household": household, "share_mode": Account.ShareMode.LENT}
    alice_card = make_account(alice, name="Synthetic Alice Card", **lent)
    bob_card = make_account(bob, name="Synthetic Bob Card", **lent)
    alice_purchase = make_transaction(alice, alice_card, amount_minor=-5000, description="Synthetic purchase")
    alice_refund = make_transaction(alice, alice_card, amount_minor=1500, description="Synthetic return credit")
    bob_purchase = make_transaction(bob, bob_card, amount_minor=-4000, description="Synthetic purchase")
    bob_refund = make_transaction(bob, bob_card, amount_minor=2000, description="Synthetic return credit")
    link_refund(alice, alice_refund.pk, bob_purchase.pk)
    link_refund(bob, bob_refund.pk, alice_purchase.pk)
    leave_household(alice)
    assert not Account.objects.visible_to(bob).filter(pk=alice_card.pk).exists()
    assert not Account.objects.visible_to(alice).filter(pk=bob_card.pk).exists()
    return alice, bob, alice_card, bob_card, alice_purchase


def _spy_on_account_locks(monkeypatch):
    locked = []
    lock_in_pk_order = lifecycle_services._lock_accounts_in_pk_order

    def spy(account_ids):
        account_ids = list(account_ids)
        locked.extend(account_ids)
        return lock_in_pk_order(account_ids)

    monkeypatch.setattr(lifecycle_services, "_lock_accounts_in_pk_order", spy)
    return locked


def test_archiving_an_account_locks_the_accounts_of_its_originals_refunds(monkeypatch):
    alice, _bob, alice_card, bob_card, _purchase = _cross_linked_accounts_of_former_members()
    locked = _spy_on_account_locks(monkeypatch)

    archive_account(alice, alice_card.pk)

    assert bob_card.pk in locked


def test_undoing_an_import_locks_the_accounts_of_its_originals_refunds(monkeypatch):
    alice, _bob, alice_card, bob_card, purchase = _cross_linked_accounts_of_former_members()
    locked = _spy_on_account_locks(monkeypatch)

    undo_import_batch(alice, alice_card.pk, purchase.import_batch_id)

    assert bob_card.pk in locked
    assert not RefundLink.objects.filter(original=purchase).exists()


@pytest.mark.django_db(transaction=True)
def test_former_members_archiving_cross_linked_accounts_concurrently_do_not_deadlock():
    if connection.vendor != "postgresql":
        pytest.skip("row-lock ordering can only be exercised on PostgreSQL")

    alice, bob, alice_card, bob_card, _purchase = _cross_linked_accounts_of_former_members()
    alice_paused = threading.Event()
    release_alice = threading.Event()
    bob_started = threading.Event()
    bob_done = threading.Event()
    pids = {}
    errors = []
    drop_links = category_services.drop_refund_links_to_originals

    def pause_alice_before_dropping(original_ids, actor):
        # Alice holds her account's rows here, about to write history on Bob's refund.
        if actor.pk == alice.pk:
            alice_paused.set()
            assert release_alice.wait(timeout=30)
        return drop_links(original_ids, actor)

    def archive(person, account, role, started=None, done=None):
        try:
            pids[role] = backend_pid()
            if started is not None:
                started.set()
            archive_account(person, account.pk)
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            if done is not None:
                done.set()
            connections.close_all()

    with patch.object(category_services, "drop_refund_links_to_originals", pause_alice_before_dropping):
        alice_archiving = threading.Thread(target=archive, args=(alice, alice_card, "alice"))
        alice_archiving.start()
        bob_archiving = threading.Thread(target=archive, args=(bob, bob_card, "bob", bob_started, bob_done))
        try:
            assert alice_paused.wait(timeout=10)
            bob_archiving.start()
            assert bob_started.wait(timeout=10)
            wait_until_blocked_by(pids["bob"], pids["alice"], bob_done, errors)
        finally:
            release_alice.set()
            alice_archiving.join(timeout=30)
            bob_archiving.join(timeout=30)

    assert errors == []
    assert not RefundLink.objects.exists()
    assert set(Account.objects.values_list("status", flat=True)) == {Account.Status.ARCHIVED}
