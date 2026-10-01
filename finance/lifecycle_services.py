from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import Account, ImportBatch, Membership, Person, Transaction


_DENIED = "Operation is not permitted."


def _person_for(principal):
    if isinstance(principal, Person):
        return principal
    if getattr(principal, "is_authenticated", False):
        try:
            return principal.person
        except Person.DoesNotExist:
            pass
    raise PermissionDenied(_DENIED)


# Lock order, for every operation in this module: the household's current
# memberships (in primary-key order), then accounts, then import batches, then
# transactions. Two transactions that take the same locks in a different order
# can each hold what the other awaits, and PostgreSQL then aborts one of them
# with a deadlock error. Always call lock_actor_household() first.
def lock_actor_household(person):
    """Lock every current membership of the person's household, in pk order.

    Returns (own_membership, memberships). own_membership is the person's own
    current membership, re-checked under the lock, or None when they have no
    current membership (then nothing is locked).
    """
    reference = (
        Membership.objects.filter(person=person, ended_at__isnull=True)
        .values("pk", "household_id")
        .first()
    )
    if reference is None:
        return None, []
    memberships = list(
        Membership.objects.select_for_update()
        .filter(household_id=reference["household_id"], ended_at__isnull=True)
        .order_by("pk")
    )
    own = next((m for m in memberships if m.pk == reference["pk"]), None)
    return own, memberships


def _visible_account_for_update(principal, account_id):
    account = (
        Account.objects.visible_to(principal)
        .select_for_update()
        .filter(pk=account_id)
        .first()
    )
    if account is None:
        raise PermissionDenied(_DENIED)
    return account


@transaction.atomic
def share_account(principal, account_id):
    """Share the actor's private account with their current household."""
    person = _person_for(principal)
    membership, _memberships = lock_actor_household(person)
    account = _visible_account_for_update(person, account_id)
    if (
        membership is None
        or account.owner_id != person.pk
        or account.scope != Account.Scope.PRIVATE
    ):
        raise PermissionDenied(_DENIED)

    account.scope = Account.Scope.HOUSEHOLD
    account.household = membership.household
    account.save(update_fields=("scope", "household", "updated_at"))


def _pair_counterpart_account_ids(account_id, seed_leg_ids=None):
    from finance.category_services import account_ids_in_pairs_touching_transactions

    if seed_leg_ids is None:
        seed_leg_ids = list(Transaction.objects.filter(account_id=account_id).values_list("pk", flat=True))
    return account_ids_in_pairs_touching_transactions(seed_leg_ids) - {account_id}


def _lock_accounts_in_pk_order(account_ids):
    ids = sorted(account_ids)
    if not ids:
        return
    list(Account.objects.select_for_update().filter(pk__in=ids).order_by("pk"))


def _lock_visible_account_with_pair_counterparts(person, account_id, seed_leg_ids=None):
    extras = _pair_counterpart_account_ids(account_id, seed_leg_ids)
    _lock_accounts_in_pk_order(pk for pk in extras if pk < account_id)
    account = _visible_account_for_update(person, account_id)
    _lock_accounts_in_pk_order(pk for pk in extras if pk > account_id)
    return account


@transaction.atomic
def rename_account(principal, account_id, name):
    """Rename an active account the actor can still see once the locks are held."""
    person = _person_for(principal)
    lock_actor_household(person)
    account = _visible_account_for_update(person, account_id)
    if account.status != Account.Status.ACTIVE or account.archived_at is not None:
        raise PermissionDenied(_DENIED)
    account.name = name
    account.save(update_fields=("name", "updated_at"))
    return account


@transaction.atomic
def unshare_account(principal, account_id):
    """Return a visible household account to its owner's private scope."""
    person = _person_for(principal)
    membership, _memberships = lock_actor_household(person)
    if not Account.objects.visible_to(person).filter(pk=account_id).exists():
        raise PermissionDenied(_DENIED)
    account = _lock_visible_account_with_pair_counterparts(person, account_id)
    if (
        account.scope != Account.Scope.HOUSEHOLD
        or membership is None
        or account.household_id != membership.household_id
    ):
        raise PermissionDenied(_DENIED)

    account.scope = Account.Scope.PRIVATE
    account.household = None
    account.save(update_fields=("scope", "household", "updated_at"))
    from finance.category_services import revalidate_pairs_touching_account

    revalidate_pairs_touching_account(person, account_id)


@transaction.atomic
def archive_account(principal, account_id):
    """Soft-delete a visible account and every active provenance row beneath it."""
    person = _person_for(principal)
    lock_actor_household(person)
    if not Account.objects.visible_to(person).filter(pk=account_id).exists():
        raise PermissionDenied(_DENIED)
    account = _lock_visible_account_with_pair_counterparts(person, account_id)
    now = timezone.now()

    ImportBatch.objects.select_for_update().filter(
        account=account,
        status=ImportBatch.Status.ACTIVE,
    ).update(status=ImportBatch.Status.ARCHIVED, archived_at=now)
    Transaction.objects.select_for_update().filter(
        account=account,
        status=Transaction.Status.ACTIVE,
    ).update(status=Transaction.Status.ARCHIVED, archived_at=now)
    if account.status == Account.Status.ACTIVE:
        account.status = Account.Status.ARCHIVED
        account.archived_at = now
        account.save(update_fields=("status", "archived_at", "updated_at"))
    from finance.category_services import refresh_transfer_pairs, revalidate_pairs_touching_account

    revalidate_pairs_touching_account(person, account_id)
    refresh_transfer_pairs(person)


@transaction.atomic
def end_current_membership(person):
    """End this person's current membership and apply shared-account exit rules.

    Shared by leave_household and the operator eviction command so the two
    cannot drift. Locks the household's current memberships in primary-key
    order, then the person's household-scoped accounts.
    """
    own_membership, current_memberships = lock_actor_household(person)
    if own_membership is None:
        raise PermissionDenied(_DENIED)

    remaining_memberships = sorted(
        (
            membership
            for membership in current_memberships
            if membership.pk != own_membership.pk
        ),
        key=lambda membership: (membership.joined_at, membership.pk),
    )
    owned_shared_accounts = Account.objects.select_for_update().filter(
        owner_id=person.pk,
        scope=Account.Scope.HOUSEHOLD,
        household_id=own_membership.household_id,
    )
    transitioned_at = timezone.now()
    if remaining_memberships:
        owned_shared_accounts.update(
            owner_id=remaining_memberships[0].person_id,
            updated_at=transitioned_at,
        )
    else:
        owned_shared_accounts.update(
            scope=Account.Scope.PRIVATE,
            household=None,
            updated_at=transitioned_at,
        )

    own_membership.ended_at = transitioned_at
    own_membership.save(update_fields=("ended_at",))


def leave_household(principal):
    """End the actor's current membership and apply shared-account exit rules."""
    end_current_membership(_person_for(principal))
