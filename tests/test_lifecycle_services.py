import threading
import time
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.utils import timezone

from finance import lifecycle_services
from finance.lifecycle_services import (
    archive_account,
    leave_household,
    share_account,
    unshare_account,
)
from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction


def make_person(username):
    user = get_user_model().objects.create_user(
        username=username,
        password="Synthetic-passphrase-42!",
    )
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def add_history(account, imported_by):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=imported_by,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="a" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    transaction = Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=-1234,
        description="Synthetic lifecycle transaction",
        source_row_number=2,
        fingerprint="b" * 64,
        original_fields={"synthetic": "value"},
    )
    return batch, transaction


@pytest.mark.django_db
def test_owner_shares_full_account_history_with_current_household():
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    account = Account.objects.create(
        name="Synthetic Private",
        account_type=Account.Type.CHECKING,
        owner=owner,
    )
    batch, transaction = add_history(account, owner)

    share_account(owner.user, account.pk)

    account.refresh_from_db()
    assert account.scope == Account.Scope.HOUSEHOLD
    assert account.household == household
    assert ImportBatch.objects.visible_to(member).filter(pk=batch.pk).exists()
    assert Transaction.objects.visible_to(member.user).filter(pk=transaction.pk).exists()
    assert ImportBatch.objects.filter(account=account).count() == 1
    assert Transaction.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_current_member_unshares_full_history_to_owner_only_without_returning_it():
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    batch, transaction = add_history(account, owner)

    result = unshare_account(member, account.pk)

    account.refresh_from_db()
    assert result is None
    assert account.scope == Account.Scope.PRIVATE
    assert account.household is None
    assert not Account.objects.visible_to(member).filter(pk=account.pk).exists()
    assert not ImportBatch.objects.visible_to(member).filter(pk=batch.pk).exists()
    assert not Transaction.objects.visible_to(member).filter(pk=transaction.pk).exists()
    assert Transaction.objects.visible_to(owner).filter(pk=transaction.pk).exists()


@pytest.mark.django_db
def test_share_rejects_other_members_private_account_like_a_missing_account():
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    private = Account.objects.create(
        name="Owner Private",
        account_type=Account.Type.CHECKING,
        owner=owner,
    )

    with pytest.raises(PermissionDenied) as private_error:
        share_account(member, private.pk)
    with pytest.raises(PermissionDenied) as missing_error:
        share_account(member, private.pk + 1000)

    assert str(private_error.value) == str(missing_error.value)
    private.refresh_from_db()
    assert private.scope == Account.Scope.PRIVATE


@pytest.mark.django_db
def test_unshare_rejects_former_member_without_disclosing_account_state():
    owner = make_person("owner")
    former_member = make_person("former")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    membership = Membership.objects.create(person=former_member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    membership.ended_at = membership.joined_at
    membership.save(update_fields=("ended_at",))

    with pytest.raises(PermissionDenied) as inaccessible_error:
        unshare_account(former_member, account.pk)
    with pytest.raises(PermissionDenied) as missing_error:
        unshare_account(former_member, account.pk + 1000)

    assert str(inaccessible_error.value) == str(missing_error.value)


@pytest.mark.django_db
def test_ended_owner_membership_does_not_bypass_household_scope():
    owner = make_person("owner")
    household = Household.objects.create(name="Synthetic Household")
    membership = Membership.objects.create(person=owner, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    membership.ended_at = membership.joined_at
    membership.save(update_fields=("ended_at",))

    assert not Account.objects.visible_to(owner).filter(pk=account.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("actor_kind", ("owner", "member"))
def test_current_member_archives_shared_account_and_all_history(actor_kind):
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    batch, financial_transaction = add_history(account, owner)

    archive_account(owner if actor_kind == "owner" else member.user, account.pk)

    account.refresh_from_db()
    batch.refresh_from_db()
    financial_transaction.refresh_from_db()
    assert account.status == Account.Status.ARCHIVED
    assert batch.status == ImportBatch.Status.ARCHIVED
    assert financial_transaction.status == Transaction.Status.ARCHIVED
    assert account.archived_at == batch.archived_at == financial_transaction.archived_at
    assert financial_transaction.original_fields == {"synthetic": "value"}
    assert Account.objects.visible_to(member).filter(pk=account.pk).exists()
    assert ImportBatch.objects.visible_to(member).filter(pk=batch.pk).exists()
    assert Transaction.objects.visible_to(member).filter(pk=financial_transaction.pk).exists()


@pytest.mark.django_db
def test_owner_archives_private_account_but_other_person_cannot():
    owner = make_person("owner")
    other = make_person("other")
    account = Account.objects.create(
        name="Synthetic Private",
        account_type=Account.Type.CHECKING,
        owner=owner,
    )
    batch, financial_transaction = add_history(account, owner)

    with pytest.raises(PermissionDenied) as private_error:
        archive_account(other, account.pk)
    with pytest.raises(PermissionDenied) as missing_error:
        archive_account(other, account.pk + 1000)
    assert str(private_error.value) == str(missing_error.value)

    archive_account(owner, account.pk)

    assert Account.objects.get(pk=account.pk).status == Account.Status.ARCHIVED
    assert ImportBatch.objects.get(pk=batch.pk).status == ImportBatch.Status.ARCHIVED
    assert Transaction.objects.get(pk=financial_transaction.pk).status == Transaction.Status.ARCHIVED


@pytest.mark.django_db
def test_archiving_twice_preserves_original_archive_timestamp():
    owner = make_person("owner")
    account = Account.objects.create(
        name="Synthetic Private",
        account_type=Account.Type.CHECKING,
        owner=owner,
    )
    batch, financial_transaction = add_history(account, owner)
    archive_account(owner, account.pk)
    account.refresh_from_db()
    first_archived_at = account.archived_at

    archive_account(owner, account.pk)

    account.refresh_from_db()
    batch.refresh_from_db()
    financial_transaction.refresh_from_db()
    assert account.archived_at == first_archived_at
    assert batch.archived_at == first_archived_at
    assert financial_transaction.archived_at == first_archived_at


@pytest.mark.django_db
def test_leaving_revokes_shared_history_access_and_allows_joining_another_household():
    owner = make_person("owner")
    leaving_member = make_person("leaving")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    membership = Membership.objects.create(person=leaving_member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    batch, financial_transaction = add_history(account, owner)

    leave_household(leaving_member.user)

    membership.refresh_from_db()
    assert membership.ended_at is not None
    assert not Account.objects.visible_to(leaving_member).filter(pk=account.pk).exists()
    assert not ImportBatch.objects.visible_to(leaving_member).filter(pk=batch.pk).exists()
    assert not Transaction.objects.visible_to(leaving_member).filter(pk=financial_transaction.pk).exists()
    assert Account.objects.filter(pk=account.pk).exists()
    assert ImportBatch.objects.filter(pk=batch.pk).exists()
    assert Transaction.objects.filter(pk=financial_transaction.pk).exists()

    another_household = Household.objects.create(name="Another Synthetic Household")
    Membership.objects.create(person=leaving_member, household=another_household)
    assert Membership.objects.filter(person=leaving_member, ended_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_application_has_no_member_removal_service():
    assert not hasattr(lifecycle_services, "remove_household_member")


@pytest.mark.django_db
def test_owner_leaving_transfers_shared_accounts_to_longest_serving_member():
    owner = make_person("owner")
    replacement = make_person("replacement")
    later_member = make_person("later")
    household = Household.objects.create(name="Synthetic Household")
    owner_membership = Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=replacement, household=household)
    Membership.objects.create(person=later_member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    batch, financial_transaction = add_history(account, owner)

    leave_household(owner.user)

    owner_membership.refresh_from_db()
    account.refresh_from_db()
    assert owner_membership.ended_at is not None
    assert account.owner == replacement
    assert account.scope == Account.Scope.HOUSEHOLD
    assert account.household == household
    assert not Account.objects.visible_to(owner).filter(pk=account.pk).exists()
    assert not Transaction.objects.visible_to(owner).filter(pk=financial_transaction.pk).exists()
    assert ImportBatch.objects.visible_to(replacement).filter(pk=batch.pk).exists()
    assert Transaction.objects.visible_to(later_member).filter(pk=financial_transaction.pk).exists()


@pytest.mark.django_db
def test_last_member_leaves_and_owned_shared_history_becomes_private():
    owner = make_person("owner")
    household = Household.objects.create(name="Synthetic Household")
    membership = Membership.objects.create(person=owner, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    batch, financial_transaction = add_history(account, owner)

    leave_household(owner)

    membership.refresh_from_db()
    account.refresh_from_db()
    assert membership.ended_at is not None
    assert account.owner == owner
    assert account.scope == Account.Scope.PRIVATE
    assert account.household is None
    assert Account.objects.visible_to(owner).filter(pk=account.pk).exists()
    assert ImportBatch.objects.visible_to(owner).filter(pk=batch.pk).exists()
    assert Transaction.objects.visible_to(owner).filter(pk=financial_transaction.pk).exists()


@pytest.mark.django_db
def test_eviction_command_transfers_shared_accounts_like_leaving():
    owner = make_person("owner")
    replacement = make_person("replacement")
    household = Household.objects.create(name="Synthetic Household")
    owner_membership = Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=replacement, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    batch, financial_transaction = add_history(account, owner)
    output = StringIO()

    call_command("evict_household_member", username="owner", stdout=output)

    owner_membership.refresh_from_db()
    account.refresh_from_db()
    printed = output.getvalue()
    assert "Ended current household membership for owner." in printed
    assert "Synthetic Shared" not in printed
    assert "1234" not in printed
    assert owner_membership.ended_at is not None
    assert account.owner == replacement
    assert account.scope == Account.Scope.HOUSEHOLD
    assert account.household == household
    assert not Account.objects.visible_to(owner).filter(pk=account.pk).exists()
    assert ImportBatch.objects.visible_to(replacement).filter(pk=batch.pk).exists()
    assert Transaction.objects.visible_to(replacement).filter(pk=financial_transaction.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("username", "message"),
    [
        ("nobody", "Unknown username."),
        ("outsider", "That person has no current household membership."),
    ],
)
def test_eviction_command_rejects_unknown_and_non_member_usernames(username, message):
    make_person("outsider")
    with pytest.raises(CommandError, match=message):
        call_command("evict_household_member", username=username)


@pytest.mark.django_db
def test_eviction_command_rejects_a_repeated_eviction():
    owner = make_person("owner")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)

    call_command("evict_household_member", username="owner")
    with pytest.raises(CommandError, match="That person has no current household membership."):
        call_command("evict_household_member", username="owner")


@pytest.mark.django_db
def test_visible_accounts_query_is_lockable_and_never_duplicates_rows():
    # PostgreSQL rejects SELECT ... FOR UPDATE combined with DISTINCT, and the
    # in-memory SQLite used by default ignores FOR UPDATE entirely, so this
    # asserts on the query itself rather than relying on the backend to fail.
    owner = make_person("owner")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    ended = Household.objects.create(name="Former Household")
    now = timezone.now()
    Membership.objects.create(
        person=owner,
        household=ended,
        joined_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=1),
    )
    shared = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )

    visible = Account.objects.visible_to(owner)

    assert visible.query.distinct is False
    assert list(visible) == [shared]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("exit_kind", ["leave", "evict"])
def test_unshare_and_membership_exit_concurrently_do_not_deadlock(exit_kind):
    # Lock order must be the same everywhere: memberships, then accounts.
    # unshare_account used to lock the account first and a membership second
    # while leaving and eviction lock every membership first and the owner's
    # shared accounts second, so each transaction could hold what the other
    # awaited. SQLite ignores row locks entirely, so this only means something
    # on PostgreSQL (scripts/test_postgres.sh).
    if connection.vendor != "postgresql":
        pytest.skip("row-lock ordering can only be exercised on PostgreSQL")

    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    account = Account.objects.create(
        name="Synthetic Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )

    first_lock_held = threading.Event()
    other_is_waiting = threading.Event()
    errors = []
    original_lock = lifecycle_services._visible_account_for_update

    def lock_then_pause(principal, account_id):
        locked = original_lock(principal, account_id)
        first_lock_held.set()
        other_is_waiting.wait(timeout=10)
        return locked

    def run(action):
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    def exit_membership():
        if exit_kind == "leave":
            leave_household(owner)
        else:
            call_command("evict_household_member", username="owner")

    with patch.object(lifecycle_services, "_visible_account_for_update", lock_then_pause):
        unsharing = threading.Thread(target=run, args=(lambda: unshare_account(member, account.pk),))
        unsharing.start()
        assert first_lock_held.wait(timeout=10)
        exiting = threading.Thread(target=run, args=(exit_membership,))
        exiting.start()
        time.sleep(1.5)  # let the second transaction reach whatever it blocks on
        other_is_waiting.set()
        unsharing.join(timeout=30)
        exiting.join(timeout=30)

    assert not unsharing.is_alive()
    assert not exiting.is_alive()
    assert errors == []
