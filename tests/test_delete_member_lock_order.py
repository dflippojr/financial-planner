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
from unittest.mock import patch

import pytest
from django.db import connection, connections

from finance import bulk_edit_services, lifecycle_services, refund_links
from finance.bulk_edit_services import ACTION_MARK_UNTRACKED, apply_bulk_edit, undo_bulk_edit
from finance.lifecycle_services import delete_member_data, leave_household
from finance.models import Account, Person, RefundLink, Transaction
from finance.refund_links import link_refund
from tests.lock_races import JOIN_SECONDS, race_two_transactions as _race
from tests.test_categorization import make_account, make_household, make_person, make_transaction

AUGUST = date(2026, 8, 12)


def _require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("deadlocks between two transactions can only be exercised on PostgreSQL")


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


# Review r2 F1 on #811: CSV mappings. update_csv_mapping locks the mapping and
# then writes accounts; an import locks the account and then the mapping. A
# former member's deletion writes the mapping it created and locks accounts
# in that household, so it locks the household's memberships first, as both do.

SYNTHETIC_CSV = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC GROCER,-12.34,USD\n"


def _synthetic_mapping():
    from finance.csv_import.parser import Mapping

    return Mapping(date_column="When", description_column="Memo", date_format="mdy_slash_4",
                   number_format="dot_comma", amount_mode="signed", amount_column="Amount",
                   currency_column="Currency")


def _former_member_with_a_mapping(*, refund_counterpart):
    """The leaver saved a household mapping that is the default for the member's shared account C, then left.

    With `refund_counterpart`, the leaver's private account P (C < P) also has a
    refund linked to a purchase in C, so deleting P writes in C.
    """
    from finance.csv_import.saved_mappings import save_csv_mapping

    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    shared = make_account(member, name="Member Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(leaver, name="Leaver Checking")
    assert shared.pk < private.pk
    if refund_counterpart:
        purchase = make_transaction(member, shared, transaction_date=AUGUST, amount_minor=-4_000,
                                    description="SYNTHETIC PURCHASE")
        refund = make_transaction(leaver, private, transaction_date=AUGUST, amount_minor=1_000,
                                  description="SYNTHETIC REFUND")
        link_refund(leaver, refund.pk, purchase.pk)
    mapping = save_csv_mapping(leaver, name="Synthetic store", headers=["When", "Memo", "Amount", "Currency"],
                               mapping=_synthetic_mapping(), account=shared, set_as_account_default=True)
    leave_household(leaver)
    return leaver, member, shared, mapping


def _update_mapping(member, mapping):
    from finance.csv_import.saved_mappings import update_csv_mapping

    return lambda: update_csv_mapping(member, mapping.pk, name="Synthetic renamed", default_account_ids=[])


def _import_into(member, account, mapping):
    from finance.csv_import.parser import read_csv
    from finance.csv_import.services import commit_csv_import
    from finance.models import ImportBatch

    return lambda: commit_csv_import(
        member, account.pk, content=SYNTHETIC_CSV, document=read_csv(SYNTHETIC_CSV), mapping=_synthetic_mapping(),
        source=ImportBatch.Source.HUNTINGTON, date_range_start=date(2026, 9, 1), date_range_end=date(2026, 9, 30),
        saved_csv_mapping=mapping,
    )


def _assert_mapping_updated(leaver, member, shared, mapping):
    from finance.models import SavedCsvMapping

    assert not Person.objects.filter(pk=leaver.pk).exists()
    saved = SavedCsvMapping.objects.get(pk=mapping.pk)
    assert (saved.name, saved.created_by_id) == ("Synthetic renamed", member.pk)
    assert Account.objects.get(pk=shared.pk).default_saved_csv_mapping_id is None


def _assert_imported(leaver, member, shared, mapping):
    from finance.models import SavedCsvMapping

    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert SavedCsvMapping.objects.get(pk=mapping.pk).created_by_id == member.pk
    assert Transaction.objects.filter(account=shared, description="SYNTHETIC GROCER").count() == 1


@pytest.mark.django_db(transaction=True)
def test_update_csv_mapping_waits_for_a_member_deletion_that_hands_its_mapping_over():
    _require_postgres()
    leaver, member, shared, mapping = _former_member_with_a_mapping(refund_counterpart=False)

    errors = _race(
        lambda: delete_member_data(leaver), _update_mapping(member, mapping),
        lifecycle_services, "_handover_lent_accounts",
    )

    assert errors == {"first": None, "competing": None}
    _assert_mapping_updated(leaver, member, shared, mapping)


@pytest.mark.django_db(transaction=True)
def test_a_member_deletion_waits_for_update_csv_mapping_that_locked_the_mapping_first():
    from finance.csv_import import saved_mappings

    _require_postgres()
    leaver, member, shared, mapping = _former_member_with_a_mapping(refund_counterpart=False)

    errors = _race(
        _update_mapping(member, mapping), lambda: delete_member_data(leaver),
        saved_mappings, "replace_mapping_account_defaults",
    )

    assert errors == {"first": None, "competing": None}
    _assert_mapping_updated(leaver, member, shared, mapping)


@pytest.mark.django_db(transaction=True)
def test_an_import_waits_for_a_member_deletion_that_wrote_its_mapping():
    """The deletion has handed the mapping over and has yet to delete P, which writes in C."""
    _require_postgres()
    leaver, member, shared, mapping = _former_member_with_a_mapping(refund_counterpart=True)

    errors = _race(
        lambda: delete_member_data(leaver), _import_into(member, shared, mapping),
        lifecycle_services, "_release_household_owned_rows",
    )

    assert errors == {"first": None, "competing": None}
    _assert_imported(leaver, member, shared, mapping)


@pytest.mark.django_db(transaction=True)
def test_a_member_deletion_waits_for_an_import_that_locked_the_account_and_mapping_first():
    from finance.csv_import import services

    _require_postgres()
    leaver, member, shared, mapping = _former_member_with_a_mapping(refund_counterpart=True)

    errors = _race(
        _import_into(member, shared, mapping), lambda: delete_member_data(leaver),
        services, "_locked_saved_mapping",
    )

    assert errors == {"first": None, "competing": None}
    _assert_imported(leaver, member, shared, mapping)


# Review r2 F2 on #811 (liaison decision, option A): every account lock while
# delete_member_data runs is checked against the accounts its first pass
# holds; any other account restarts the deletion with a fresh first pass.


def _record_attempts():
    """Patch the account lock and each attempt; returns the account sets locked, one list per attempt."""
    attempts = []
    lock = lifecycle_services._lock_accounts_in_pk_order
    once = lifecycle_services._delete_member_data_once

    def record_lock(account_ids):
        ids = set(account_ids)
        attempts[-1].append(ids)
        return lock(ids)

    def record_attempt(*args, **kwargs):
        attempts.append([])
        return once(*args, **kwargs)

    patches = (
        patch.object(lifecycle_services, "_lock_accounts_in_pk_order", record_lock),
        patch.object(lifecycle_services, "_delete_member_data_once", record_attempt),
    )
    return attempts, patches


def _in_another_transaction(action):
    """Run `action` in its own thread and connection, committed before this returns."""
    errors = []

    def run():
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - reported to the test thread
            errors.append(exc)
        finally:
            connections.close_all()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=JOIN_SECONDS)
    assert not worker.is_alive(), "the concurrent change did not finish"
    assert errors == []


def _run_deletion_with(leaver, pause_name, concurrent_change):
    """Delete the leaver's data; the first time `pause_name` returns, commit `concurrent_change` elsewhere."""
    attempts, (lock_patch, attempt_patch) = _record_attempts()
    done = []
    with lock_patch, attempt_patch:
        # Read after the recording patches, so pausing on the lock keeps recording it.
        original = getattr(lifecycle_services, pause_name)

        def change_once(*args, **kwargs):
            result = original(*args, **kwargs)
            if not done:
                done.append(True)
                _in_another_transaction(concurrent_change)
            return result

        with patch.object(lifecycle_services, pause_name, change_once):
            delete_member_data(leaver)
    return attempts


def _assert_restarted_with_one_first_pass(attempts, expected_ids):
    assert len(attempts) >= 2, f"the deletion did not start again: {attempts}"
    first, *later = attempts[-1]
    assert expected_ids <= first
    assert all(ids <= first for ids in later), attempts[-1]


def _left_member_with_private_account():
    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    shared = make_account(member, name="Member Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    member_private = make_account(member, name="Member Private")
    private = make_account(leaver, name="Leaver Checking")
    leave_household(leaver)
    return leaver, member, household, shared, member_private, private


@pytest.mark.django_db(transaction=True)
def test_grid_an_account_created_after_the_reread_restarts_the_deletion():
    _require_postgres()
    leaver, _member, _household, _shared, _member_private, private = _left_member_with_private_account()
    created = []

    def create_account():
        created.append(make_account(leaver, name="Leaver Late Savings", account_type=Account.Type.SAVINGS))

    attempts = _run_deletion_with(leaver, "_handover_lent_accounts", create_account)

    _assert_restarted_with_one_first_pass(attempts, {private.pk, created[0].pk})
    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert not Account.objects.filter(pk=created[0].pk).exists()


@pytest.mark.django_db(transaction=True)
def test_grid_a_pair_committed_between_the_read_and_the_lock_restarts_the_deletion():
    """The insert stands in for a pair refresh that committed just before the deletion's lock."""
    from finance.models import TransferPair

    _require_postgres()
    leaver, member, _household, _shared, member_private, private = _left_member_with_private_account()
    assert member_private.pk < private.pk
    out_leg = make_transaction(leaver, private, transaction_date=AUGUST, amount_minor=-3_000,
                               description="SYNTHETIC TRANSFER OUT")
    in_leg = make_transaction(member, member_private, transaction_date=AUGUST, amount_minor=3_000,
                              description="SYNTHETIC TRANSFER IN")

    def pair():
        TransferPair.objects.create(leg_a=out_leg, leg_b=in_leg, status="confirmed", kind="transfer",
                                    confidence="high", reasons=[])

    attempts = _run_deletion_with(leaver, "_lock_accounts_in_pk_order", pair)

    _assert_restarted_with_one_first_pass(attempts, {member_private.pk, private.pk})
    assert attempts[0][0] == {private.pk}
    assert not TransferPair.objects.exists()
    assert Transaction.objects.filter(pk=in_leg.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_grid_a_household_account_added_after_the_reread_restarts_the_deletion():
    _require_postgres()
    leaver = make_person("leaver")
    member = make_person("member")
    household = make_household(leaver, member)
    shared = make_account(member, name="Member Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(leaver, name="Leaver Checking")
    added = []

    def add_household_account():
        added.append(lifecycle_services.create_account(
            member, name="Member New Shared", account_type=Account.Type.SAVINGS, household=household,
            share_mode=Account.ShareMode.CO_OWNED,
        ))

    attempts = _run_deletion_with(leaver, "_handover_lent_accounts", add_household_account)

    _assert_restarted_with_one_first_pass(attempts, {shared.pk, private.pk, added[0].pk})
    assert not Person.objects.filter(pk=leaver.pk).exists()
    assert Account.objects.get(pk=added[0].pk).owner_id == member.pk


@pytest.mark.django_db
def test_a_deletion_whose_accounts_never_settle_fails_loudly_and_changes_nothing():
    leaver, _member, _household, _shared, _member_private, private = _left_member_with_private_account()
    grows = iter(range(10_000_000, 10_000_100))
    touches = lifecycle_services._accounts_member_deletion_touches

    def always_more(*args, **kwargs):
        return touches(*args, **kwargs) | {next(grows)}

    with (
        patch.object(lifecycle_services, "_accounts_member_deletion_touches", always_more),
        pytest.raises(lifecycle_services.MemberDeletionUnsettled),
    ):
        delete_member_data(leaver)

    assert Person.objects.filter(pk=leaver.pk).exists()
    assert Account.objects.filter(pk=private.pk).exists()
