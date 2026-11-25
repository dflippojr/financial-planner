"""Helpers every ledger edit shares: correction history, row locks, and who sees a pair's legs.

Both lock helpers lock accounts, then transactions, each in pk order, so every
caller locks in the same order. Only lock_accounts_and_transactions takes the
actor's household lock first; callers of lock_owned_transactions take it
themselves with lock_actor_household before calling.
"""

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .access import DENIED as _DENIED
from .access import visible_rows
from .audit_services import record_correction
from .lifecycle_services import lock_actor_household
from .models import Account, Membership, Transaction, TransactionCorrectionHistory
from .account_locks import lock_accounts


def history_label(category):
    return "Uncategorized" if category is None else category.name


def record_text_history(transaction, actor, field_name, previous, new):
    if previous == new:
        return
    history = TransactionCorrectionHistory.objects.create(
        transaction=transaction,
        actor=actor,
        recorded_at=timezone.now(),
        field_name=field_name,
        previous_description=previous,
        new_description=new,
    )
    record_correction(transaction, actor, history)
    return history


def lock_accounts_and_transactions(person, transactions):
    lock_actor_household(person)
    lock_accounts({item.account_id for item in transactions}, pks_only=True)
    ids = sorted(item.pk for item in transactions)
    locked = list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "category")
        .filter(pk__in=ids, status=Transaction.Status.ACTIVE)
        .order_by("pk")
    )
    if len(locked) != len(ids):
        raise PermissionDenied(_DENIED)
    visible = set(visible_rows(Transaction, person).filter(pk__in=ids).values_list("pk", flat=True))
    if visible != set(ids):
        raise PermissionDenied(_DENIED)
    return locked


def lock_owned_transactions(transactions):
    lock_accounts({item.account_id for item in transactions}, pks_only=True)
    ids = sorted(item.pk for item in transactions)
    locked = list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "category")
        .filter(pk__in=ids, status=Transaction.Status.ACTIVE)
        .order_by("pk")
    )
    if len(locked) != len(ids):
        raise PermissionDenied(_DENIED)
    return locked


def account_viewer_ids(account):
    """Members who see an account's rows now, as `Account.objects.visible_to` decides."""
    if account.scope == Account.Scope.PRIVATE:
        return {account.owner_id}
    return set(
        Membership.objects.filter(household_id=account.household_id, ended_at__isnull=True).values_list(
            "person_id", flat=True
        )
    )


def legs_seeing_viewers(leg_accounts):
    return set.intersection(*(account_viewer_ids(account) for account in leg_accounts))


def every_viewer_always_sees_legs(account, leg_accounts, see_both_legs):
    """Every viewer of `account` sees both legs, now and after any member joins.

    A private account's owner must see them; a household account needs both legs
    in that household, since a later member would see only the household's accounts.
    """
    if account.scope == Account.Scope.HOUSEHOLD:
        return all(
            leg_account.scope == Account.Scope.HOUSEHOLD and leg_account.household_id == account.household_id
            for leg_account in leg_accounts
        )
    return account.owner_id in see_both_legs


def record_member_history(txn, actor, person_id, field_name, previous, new):
    # Listed only to `person_id`; no audit event, so no one else learns of it.
    TransactionCorrectionHistory.objects.create(
        transaction=txn,
        actor=actor,
        recorded_at=timezone.now(),
        field_name=field_name,
        previous_description=previous,
        new_description=new,
        only_for_id=person_id,
    )
