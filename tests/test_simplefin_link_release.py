from datetime import timedelta

import pytest
from django.utils import timezone

from finance.import_due import due_import_accounts
from finance.lifecycle_services import leave_household, share_account, unshare_account
from finance.models import Account, AccountLink, AuditEvent, SimpleFinConnection, Transaction
from finance.simplefin_services import save_account_links
from tests.test_monthly_review import make_account, make_household, make_person, make_transaction


def _link(person, account, suffix):
    connection = SimpleFinConnection.objects.create(owner=person, encrypted_access_url=b"synthetic")
    save_account_links(person, connection.pk, [{
        "simplefin_account_id": f"CON-{suffix}:sf-1", "action": "link",
        "account_id": account.pk, "cutover_date": timezone.localdate(),
    }])
    return connection


def _setup():
    owner, member = make_person("owner"), make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Owner Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    old = timezone.localdate() - timedelta(days=90)
    make_transaction(owner, shared, transaction_date=old, range_start=old, range_end=old)
    return owner, member, shared, _link(member, shared, "M")


def _assert_free_for_owner(owner, shared):
    _link(owner, shared, "O")
    assert AccountLink.objects.get(account=shared).connection.owner == owner


@pytest.mark.django_db
def test_unshare_releases_former_sharers_link_and_keeps_transactions():
    owner, member, shared, member_connection = _setup()
    transactions = Transaction.objects.filter(account=shared).count()
    unshare_account(owner, shared.pk)
    assert not AccountLink.objects.filter(connection=member_connection).exists()
    assert Transaction.objects.filter(account=shared).count() == transactions
    assert [row.pk for row in due_import_accounts(Account.objects.visible_to(owner))] == [shared.pk]
    _assert_free_for_owner(owner, shared)


@pytest.mark.django_db
def test_leaving_releases_departed_members_link():
    owner, member, shared, member_connection = _setup()
    leave_household(member)
    assert not AccountLink.objects.filter(connection=member_connection).exists()
    assert [row.pk for row in due_import_accounts(Account.objects.visible_to(owner))] == [shared.pk]
    _assert_free_for_owner(owner, shared)


@pytest.mark.django_db
def test_resharing_does_not_restart_released_feed():
    owner, member, shared, member_connection = _setup()
    unshare_account(owner, shared.pk)
    share_account(owner, shared.pk, Account.ShareMode.CO_OWNED)
    assert not AccountLink.objects.filter(connection=member_connection).exists()


@pytest.mark.django_db
def test_release_is_audited_to_link_owner_with_unsharer_as_actor():
    owner, member, shared, member_connection = _setup()
    unshare_account(owner, shared.pk)
    events = AuditEvent.objects.filter(action=AuditEvent.Action.SIMPLEFIN_LINKS_CHANGED, affected_member=member)
    assert [(e.private_owner_id, e.actor_id, e.affected_member_id, e.target_id, e.changed_fields) for e in events] == [
        (member.pk, owner.pk, member.pk, member_connection.pk, ["account_links"])
    ]


@pytest.mark.django_db
def test_links_the_member_can_still_see_are_kept():
    owner, member, shared, member_connection = _setup()
    own_private = make_account(member, name="Member Savings")
    kept = _link(member, own_private, "K")
    other_shared = make_account(owner, name="Other Shared", scope=Account.Scope.HOUSEHOLD,
                                household=shared.household)
    still_visible = _link(member, other_shared, "V")
    unshare_account(owner, shared.pk)
    assert AccountLink.objects.filter(connection__in=(kept, still_visible)).count() == 2
    assert not AccountLink.objects.filter(connection=member_connection, account=shared).exists()
