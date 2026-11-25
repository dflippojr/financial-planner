"""delete_member_data takes every account it touches in one pk-ordered pass (#810).

A member who already left their household deletes their data. Their private
accounts still have refund links to the household's accounts, so deleting
them writes rows in those counterpart accounts. Bulk undo and link_refund
lock both accounts of a link in one pk-ordered pass; if the deletion reached
a counterpart only later, inside delete_account, the two could each hold an
account the other awaits and PostgreSQL would abort one of them.

Each race runs in both orders. The leaver holds no household membership
lock, so nothing but the account locks orders the two transactions.
Synthetic data only.
"""

import threading
from datetime import date
from time import monotonic
from unittest.mock import patch

import pytest
from django.db import connection, connections

from finance import bulk_edit_services, lifecycle_services, refund_links
from finance.bulk_edit_services import ACTION_MARK_UNTRACKED, apply_bulk_edit, undo_bulk_edit
from finance.lifecycle_services import delete_member_data, leave_household
from finance.models import Account, Person, RefundLink, Transaction
from finance.refund_links import link_refund
from tests.lock_races import JOIN_SECONDS, WAIT_SECONDS, wait_until_blocked_by
from tests.test_categorization import make_account, make_household, make_person, make_transaction

AUGUST = date(2026, 8, 12)


def _require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("deadlocks between two transactions can only be exercised on PostgreSQL")


def _race(first_action, competing_action, pause_module, pause_name):
    """Run `first_action` until its first return from `pause_name`, then `competing_action`.

    Waits until PostgreSQL reports the competitor blocked on the first
    transaction, releases the first, and returns each worker's error (or
    None). A deadlock aborts one worker with an error, so callers require
    both to be None.
    """
    first_holds = threading.Event()
    release_first = threading.Event()
    competing_done = threading.Event()
    errors = {}
    pids = {}
    original = getattr(pause_module, pause_name)

    def pause_after(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread() is first and not first_holds.is_set():
            first_holds.set()
            if not release_first.wait(timeout=JOIN_SECONDS):
                raise TimeoutError("First worker was never released")
        return result

    def run(action, role, done):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '15s'")
                cursor.execute("SET statement_timeout = '20s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[role] = cursor.fetchone()[0]
            action()
            errors[role] = None
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors[role] = exc
        finally:
            try:
                connections.close_all()
            finally:
                done.set()

    first_done = threading.Event()
    first = threading.Thread(target=run, args=(first_action, "first", first_done), daemon=True)
    competing = threading.Thread(target=run, args=(competing_action, "competing", competing_done), daemon=True)
    with patch.object(pause_module, pause_name, pause_after):
        try:
            first.start()
            assert first_holds.wait(timeout=WAIT_SECONDS), f"First worker did not reach its pause: {errors}"
            competing.start()
            deadline = monotonic() + WAIT_SECONDS
            while "competing" not in pids:
                assert monotonic() < deadline and not competing_done.is_set(), f"Competitor did not connect: {errors}"
                competing_done.wait(0.02)
            wait_until_blocked_by(pids["competing"], pids["first"], competing_done, list(errors.values()))
        finally:
            release_first.set()
            first.join(timeout=JOIN_SECONDS)
            competing.join(timeout=JOIN_SECONDS)
    assert not first.is_alive() and not competing.is_alive(), "A race worker did not finish"
    return errors


def _left_member_with_two_linked_accounts():
    """A leaver whose private accounts P1 < P2 have refunds linked to shared C1 and C2, with C2 < C1.

    The leaver linked them while still a member, then left. The remaining
    member has an unlinked refund in C2 and purchase in C1 to link.
    """
    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    c2 = make_account(member, name="Member Card", scope=Account.Scope.HOUSEHOLD, household=household)
    c1 = make_account(member, name="Member Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    p1 = make_account(leaver, name="Leaver Checking")
    p2 = make_account(leaver, name="Leaver Savings", account_type=Account.Type.SAVINGS)
    assert c2.pk < c1.pk < p1.pk < p2.pk
    for refund_account, original_account in ((p1, c1), (p2, c2)):
        original = make_transaction(member, original_account, transaction_date=AUGUST, amount_minor=-4_000,
                                    description=f"SYNTHETIC PURCHASE {original_account.pk}")
        refund = make_transaction(leaver, refund_account, transaction_date=AUGUST, amount_minor=1_000,
                                  description=f"SYNTHETIC REFUND {refund_account.pk}")
        link_refund(leaver, refund.pk, original.pk)
    purchase = make_transaction(member, c1, transaction_date=AUGUST, amount_minor=-9_000,
                                description="SYNTHETIC MEMBER PURCHASE")
    member_refund = make_transaction(member, c2, transaction_date=AUGUST, amount_minor=2_500,
                                     description="SYNTHETIC MEMBER REFUND")
    leave_household(leaver)
    return leaver, member, purchase, member_refund


@pytest.mark.django_db(transaction=True)
def test_link_refund_waits_for_a_member_deletion_that_reached_one_counterpart():
    """The deletion holds C1 after deleting P1 and needs C2 next; link_refund locks C2 then C1."""
    _require_postgres()
    leaver, member, purchase, member_refund = _left_member_with_two_linked_accounts()

    errors = _race(
        lambda: delete_member_data(leaver),
        lambda: link_refund(member, member_refund.pk, purchase.pk),
        lifecycle_services, "delete_account",
    )

    assert errors == {"first": None, "competing": None}
    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert RefundLink.objects.get(refund=member_refund).original_id == purchase.pk


@pytest.mark.django_db(transaction=True)
def test_a_member_deletion_waits_for_link_refund_that_locked_the_counterparts_first():
    _require_postgres()
    leaver, member, purchase, member_refund = _left_member_with_two_linked_accounts()

    errors = _race(
        lambda: link_refund(member, member_refund.pk, purchase.pk),
        lambda: delete_member_data(leaver),
        refund_links, "lock_accounts_and_transactions",
    )

    assert errors == {"first": None, "competing": None}
    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert RefundLink.objects.get(refund=member_refund).original_id == purchase.pk


def _left_member_with_a_bulk_mark_to_undo():
    """A member's bulk untracked mark dropped a link whose refund is in the leaver's account P.

    The purchase is in the member's shared account C (C < P). The leaver has a
    second refund in P linked to another purchase in C, so deleting P writes
    in C. Undo locks C and P in one pass, whatever the member can see.
    """
    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    shared = make_account(member, name="Member Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(leaver, name="Leaver Checking")
    assert shared.pk < private.pk
    kept = make_transaction(member, shared, transaction_date=AUGUST, amount_minor=-4_000,
                            description="SYNTHETIC KEPT PURCHASE")
    kept_refund = make_transaction(leaver, private, transaction_date=AUGUST, amount_minor=1_000,
                                   description="SYNTHETIC KEPT REFUND")
    link_refund(leaver, kept_refund.pk, kept.pk)
    marked = make_transaction(member, shared, transaction_date=AUGUST, amount_minor=-7_000,
                              description="SYNTHETIC MARKED PURCHASE")
    marked_refund = make_transaction(leaver, private, transaction_date=AUGUST, amount_minor=2_000,
                                     description="SYNTHETIC MARKED REFUND")
    link_refund(leaver, marked_refund.pk, marked.pk)
    _preview, undo = apply_bulk_edit(
        member, matching=Transaction.objects.none(), transaction_ids=[marked.pk], action=ACTION_MARK_UNTRACKED,
    )
    assert not RefundLink.objects.filter(refund=marked_refund).exists()
    assert [link["refund_id"] for item in undo.snapshot["rows"] for link in item["dropped_refund_links"]] == [
        marked_refund.pk
    ]
    leave_household(leaver)
    return leaver, member, marked, undo


@pytest.mark.django_db(transaction=True)
def test_bulk_undo_waits_for_a_member_deletion_that_holds_the_leavers_account():
    """The deletion holds P and needs C later; undo locks C then P."""
    _require_postgres()
    leaver, member, marked, undo = _left_member_with_a_bulk_mark_to_undo()

    errors = _race(
        lambda: delete_member_data(leaver),
        lambda: undo_bulk_edit(member, undo.pk),
        lifecycle_services, "_handover_lent_accounts",
    )

    assert errors == {"first": None, "competing": None}
    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert not Transaction.objects.get(pk=marked.pk).untracked_transfer


@pytest.mark.django_db(transaction=True)
def test_a_member_deletion_waits_for_a_bulk_undo_that_locked_both_accounts_first():
    _require_postgres()
    leaver, member, marked, undo = _left_member_with_a_bulk_mark_to_undo()

    errors = _race(
        lambda: undo_bulk_edit(member, undo.pk),
        lambda: delete_member_data(leaver),
        bulk_edit_services, "_lock_rows",
    )

    assert errors == {"first": None, "competing": None}
    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert not Transaction.objects.get(pk=marked.pk).untracked_transfer


def _member_with_every_kind_of_counterpart():
    """The leaver stays a member until the deletion; every account kind the deletion reaches is present."""
    from finance.models import SavedCsvMapping

    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    shared = make_account(member, name="Member Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    lent = make_account(leaver, name="Leaver Lent", scope=Account.Scope.HOUSEHOLD, household=household,
                        share_mode=Account.ShareMode.LENT)
    private = make_account(leaver, name="Leaver Private")
    paired = make_account(member, name="Member Paired")
    refunds = make_account(member, name="Member Refunds")
    loan = make_account(member, name="Member Loan", account_type=Account.Type.LOAN)
    mapped = make_account(member, name="Member Mapped")
    untouched = make_account(member, name="Member Untouched")
    # Pair from the household to the member's private account: the exit revalidates it.
    shared_out = make_transaction(member, shared, transaction_date=AUGUST, amount_minor=-3_000,
                                  description="SYNTHETIC TRANSFER OUT")
    paired_in = make_transaction(member, paired, transaction_date=AUGUST, amount_minor=3_000,
                                 description="SYNTHETIC TRANSFER IN")
    from finance.models import TransferPair

    TransferPair.objects.create(leg_a=shared_out, leg_b=paired_in, status="confirmed", kind="transfer",
                                confidence="high", reasons=[])
    # Refund in the member's private account for a purchase in the leaver's lent account.
    purchase = make_transaction(leaver, lent, transaction_date=AUGUST, amount_minor=-5_000,
                                description="SYNTHETIC LENT PURCHASE")
    refund = make_transaction(member, refunds, transaction_date=AUGUST, amount_minor=1_000,
                              description="SYNTHETIC MEMBER REFUND")
    link_refund(member, refund.pk, purchase.pk)
    # Legacy data: a loan still secured by the leaver's private account.
    Account.objects.filter(pk=loan.pk).update(secured_asset=private)
    mapping = SavedCsvMapping.objects.create(
        household=household, name="Synthetic mapping", headers=["When", "Memo", "Amount"], date_column="When",
        description_column="Memo", date_format="iso", number_format="dot_none", amount_mode="signed",
        amount_column="Amount", created_by=leaver,
    )
    Account.objects.filter(pk=mapped.pk).update(default_saved_csv_mapping=mapping)
    touched = {shared.pk, lent.pk, private.pk, paired.pk, refunds.pk, loan.pk, mapped.pk}
    return leaver, lent, touched, untouched


@pytest.mark.django_db
def test_the_first_lock_pass_covers_every_account_the_deletion_reaches():
    leaver, _lent, touched, untouched = _member_with_every_kind_of_counterpart()
    own, memberships = lifecycle_services.lock_actor_household(leaver)
    remaining = [membership for membership in memberships if membership.pk != own.pk]

    ids = lifecycle_services._accounts_member_deletion_touches(leaver, own, remaining)

    assert ids == touched
    assert untouched.pk not in ids


def _account_lock_ids(sql):
    import re

    match = re.search(r'"finance_account"\."id" (?:IN \(([\d, ]+)\)|= (\d+))', sql)
    assert match, f"Account lock without an id filter: {sql}"
    return {int(pk) for pk in (match.group(1) or match.group(2)).split(",")}


@pytest.mark.django_db
def test_no_account_lock_after_the_first_pass_reaches_a_new_account():
    """Every later `FOR UPDATE` on accounts, in the exit and in each delete_account, names only held accounts."""
    _require_postgres()
    from django.test.utils import CaptureQueriesContext

    leaver, lent, touched, _untouched = _member_with_every_kind_of_counterpart()

    with CaptureQueriesContext(connection) as queries:
        delete_member_data(leaver, {lent.pk: lifecycle_services.LENT_DELETE})

    account_locks = [
        _account_lock_ids(query["sql"]) for query in queries.captured_queries
        if query["sql"].lstrip().startswith("SELECT") and 'FROM "finance_account"' in query["sql"]
        and "FOR UPDATE" in query["sql"]
    ]
    assert account_locks[0] == touched
    assert len(account_locks) > 2, "the exit and delete_account lock accounts again"
    assert all(ids <= touched for ids in account_locks[1:])
    assert not Person.objects.filter(pk=leaver.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_a_link_committed_between_the_read_and_the_lock_restarts_the_first_pass():
    """Review r1 on #811: an undo restores a link from the leaver's account P to shared C (C < P) in that window.

    The deletion must not then take C after P. It starts again with C and P in
    one first pass, and every later account lock stays inside it.
    """
    _require_postgres()
    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    shared = make_account(member, name="Member Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(leaver, name="Leaver Checking")
    assert shared.pk < private.pk
    marked = make_transaction(member, shared, transaction_date=AUGUST, amount_minor=-7_000,
                              description="SYNTHETIC MARKED PURCHASE")
    marked_refund = make_transaction(leaver, private, transaction_date=AUGUST, amount_minor=2_000,
                                     description="SYNTHETIC MARKED REFUND")
    link_refund(leaver, marked_refund.pk, marked.pk)
    _preview, undo = apply_bulk_edit(
        member, matching=Transaction.objects.none(), transaction_ids=[marked.pk], action=ACTION_MARK_UNTRACKED,
    )
    leave_household(leaver)
    # Nothing links the leaver's account to the household now, so the first read finds only P.
    assert not RefundLink.objects.exists()

    calls = []
    errors = []
    original = lifecycle_services._lock_accounts_in_pk_order

    def undo_in_its_own_transaction():
        try:
            undo_bulk_edit(member, undo.pk)
        except Exception as exc:  # noqa: BLE001 - reported to the test thread
            errors.append(exc)
        finally:
            connections.close_all()

    def record(account_ids):
        ids = set(account_ids)
        if not calls:
            worker = threading.Thread(target=undo_in_its_own_transaction, daemon=True)
            worker.start()
            worker.join(timeout=JOIN_SECONDS)
            assert not worker.is_alive(), "the undo did not finish"
        calls.append(ids)
        return original(ids)

    with patch.object(lifecycle_services, "_lock_accounts_in_pk_order", record):
        delete_member_data(leaver)

    assert errors == []
    assert calls[0] == {private.pk}
    assert calls[1] == {shared.pk, private.pk}
    assert all(ids <= {shared.pk, private.pk} for ids in calls[2:])
    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert not Transaction.objects.get(pk=marked.pk).untracked_transfer
    assert not RefundLink.objects.exists()
