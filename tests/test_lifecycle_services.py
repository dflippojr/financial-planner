from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from finance.lifecycle_services import archive_account, share_account, unshare_account
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
