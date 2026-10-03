from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import (
    Account,
    Alert,
    ImportBatch,
    Membership,
    Person,
    RecurringExclusion,
    RecurringSeries,
    RecurringSeriesMember,
    RefundLink,
    Transaction,
    TransactionCorrectionHistory,
    TransactionTag,
    TransferPair,
    clear_invalid_loan_pairings,
)


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
# memberships (in primary-key order), then accounts in primary-key order
# (including counterpart accounts of transfer pairs and refund links), then
# the rows being changed or deleted. Two transactions that take the same locks
# in a different order can each hold what the other awaits, and PostgreSQL then
# aborts one of them with a deadlock error. Always call lock_actor_household()
# first.
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


def _parsed_share_mode(share_mode):
    if share_mode in (Account.ShareMode.CO_OWNED, Account.ShareMode.LENT):
        return share_mode
    raise PermissionDenied(_DENIED)


def _lock_ledgers(account_ids):
    ids = sorted({account_id for account_id in account_ids if account_id is not None})
    if not ids:
        return
    list(ImportBatch.objects.select_for_update().filter(account_id__in=ids).order_by("pk"))
    list(Transaction.objects.select_for_update().filter(account_id__in=ids).order_by("pk"))


def _make_account_private(account):
    account.scope = Account.Scope.PRIVATE
    account.household = None
    account.share_mode = ""
    account.save(update_fields=("scope", "household", "share_mode", "updated_at"))
    clear_invalid_loan_pairings(account)


def _actor_may_manage_sharing(person, account):
    if account.scope != Account.Scope.HOUSEHOLD:
        return False
    if account.share_mode == Account.ShareMode.LENT:
        return account.owner_id == person.pk
    return True


@transaction.atomic
def share_account(principal, account_id, share_mode):
    """Share the actor's private account with their current household."""
    person = _person_for(principal)
    membership, _memberships = lock_actor_household(person)
    account = _visible_account_for_update(person, account_id)
    _lock_ledgers((account.pk,))
    mode = _parsed_share_mode(share_mode)
    if (
        membership is None
        or account.owner_id != person.pk
        or account.scope != Account.Scope.PRIVATE
    ):
        raise PermissionDenied(_DENIED)

    account.scope = Account.Scope.HOUSEHOLD
    account.household = membership.household
    account.share_mode = mode
    account.save(update_fields=("scope", "household", "share_mode", "updated_at"))
    clear_invalid_loan_pairings(account)
    return account


@transaction.atomic
def change_account_share_mode(principal, account_id, share_mode, *, confirm_give_up_ownership=False):
    """Switch a household account between co-owned and lent. Owner only."""
    person = _person_for(principal)
    membership, _memberships = lock_actor_household(person)
    account = _visible_account_for_update(person, account_id)
    _lock_ledgers((account.pk,))
    mode = _parsed_share_mode(share_mode)
    if (
        membership is None
        or account.scope != Account.Scope.HOUSEHOLD
        or account.household_id != membership.household_id
        or account.owner_id != person.pk
    ):
        raise PermissionDenied(_DENIED)
    if account.share_mode == mode:
        return account
    if account.share_mode == Account.ShareMode.LENT and mode == Account.ShareMode.CO_OWNED:
        if not confirm_give_up_ownership:
            raise PermissionDenied(_DENIED)

    account.share_mode = mode
    account.save(update_fields=("share_mode", "updated_at"))
    return account


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
    _lock_ledgers((account.pk,))
    if (
        account.scope != Account.Scope.HOUSEHOLD
        or membership is None
        or account.household_id != membership.household_id
        or not _actor_may_manage_sharing(person, account)
    ):
        raise PermissionDenied(_DENIED)

    _make_account_private(account)
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
    _lock_ledgers((account.pk,))
    if account.scope == Account.Scope.HOUSEHOLD and not _actor_may_manage_sharing(person, account):
        raise PermissionDenied(_DENIED)
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
    household_accounts = list(
        Account.objects.select_for_update()
        .filter(
            scope=Account.Scope.HOUSEHOLD,
            household_id=own_membership.household_id,
        )
        .order_by("pk")
    )
    _lock_ledgers(account.pk for account in household_accounts)
    transitioned_at = timezone.now()
    _apply_shared_account_exit(
        person,
        household_accounts,
        remaining_memberships,
        transitioned_at,
    )

    own_membership.ended_at = transitioned_at
    own_membership.save(update_fields=("ended_at",))


def _apply_shared_account_exit(person, household_accounts, remaining_memberships, transitioned_at):
    account_ids = [account.pk for account in household_accounts]
    if not remaining_memberships:
        if account_ids:
            Account.objects.filter(pk__in=account_ids).update(
                owner_id=person.pk,
                scope=Account.Scope.PRIVATE,
                household=None,
                share_mode="",
                updated_at=transitioned_at,
            )
        return
    successor_id = remaining_memberships[0].person_id
    lent_ids = [
        account.pk
        for account in household_accounts
        if account.owner_id == person.pk and account.share_mode == Account.ShareMode.LENT
    ]
    co_owned_ids = [
        account.pk
        for account in household_accounts
        if account.owner_id == person.pk and account.share_mode == Account.ShareMode.CO_OWNED
    ]
    if lent_ids:
        Account.objects.filter(pk__in=lent_ids).update(
            scope=Account.Scope.PRIVATE,
            household=None,
            share_mode="",
            updated_at=transitioned_at,
        )
    if co_owned_ids:
        Account.objects.filter(pk__in=co_owned_ids).update(
            owner_id=successor_id,
            updated_at=transitioned_at,
        )


def leave_household(principal):
    """End the actor's current membership and apply shared-account exit rules."""
    end_current_membership(_person_for(principal))


def _delete_counterpart_account_ids(account_id):
    from finance.category_services import (
        account_ids_in_pairs_touching_transactions,
        account_ids_in_refunds_touching_transactions,
    )

    seed = list(Transaction.objects.filter(account_id=account_id).values_list("pk", flat=True))
    counterparts = account_ids_in_pairs_touching_transactions(seed)
    counterparts |= account_ids_in_refunds_touching_transactions(seed)
    return counterparts - {account_id}


def _lock_visible_account_for_delete(person, account_id):
    extras = _delete_counterpart_account_ids(account_id)
    _lock_accounts_in_pk_order(pk for pk in extras if pk < account_id)
    account = _visible_account_for_update(person, account_id)
    _lock_accounts_in_pk_order(pk for pk in extras if pk > account_id)
    return account


def _related_ids_for_account_delete(tx_ids):
    from finance.category_services import _pair_rows_touching, _refund_rows_touching

    pair_rows = _pair_rows_touching(tx_ids)
    refund_rows = _refund_rows_touching(tx_ids)
    pair_ids = [pk for pk, _left, _right in pair_rows]
    refund_ids = [pk for pk, _refund, _original in refund_rows]
    related_tx_ids = set(tx_ids)
    related_tx_ids.update(leg_id for _pk, left_id, right_id in pair_rows for leg_id in (left_id, right_id))
    related_tx_ids.update(tx_id for _pk, refund_id, original_id in refund_rows for tx_id in (refund_id, original_id))
    return pair_ids, refund_ids, related_tx_ids


def _lock_rows_for_account_delete(account):
    tx_ids = list(Transaction.objects.filter(account_id=account.pk).order_by("pk").values_list("pk", flat=True))
    pair_ids, refund_ids, related_tx_ids = _related_ids_for_account_delete(tx_ids)
    locked_txs = []
    if related_tx_ids:
        locked_txs = list(
            Transaction.objects.select_for_update(of=("self",))
            .select_related("account", "category")
            .filter(pk__in=related_tx_ids)
            .order_by("pk")
        )
    pairs = []
    if pair_ids:
        pairs = list(
            TransferPair.objects.select_for_update(of=("self",)).filter(pk__in=sorted(pair_ids)).order_by("pk")
        )
    refunds = []
    if refund_ids:
        refunds = list(RefundLink.objects.select_for_update().filter(pk__in=sorted(refund_ids)).order_by("pk"))
    members = []
    exclusions = []
    if tx_ids:
        members = list(
            RecurringSeriesMember.objects.select_for_update().filter(transaction_id__in=tx_ids).order_by("pk")
        )
        exclusions = list(
            RecurringExclusion.objects.select_for_update().filter(transaction_id__in=tx_ids).order_by("pk")
        )
    series_ids = sorted({member.series_id for member in members})
    if series_ids:
        list(RecurringSeries.objects.select_for_update(of=("self",)).filter(pk__in=series_ids).order_by("pk"))
    if tx_ids:
        list(
            TransactionTag.objects.select_for_update()
            .filter(transaction_id__in=tx_ids)
            .order_by("pk")
        )
        list(
            TransactionCorrectionHistory.objects.select_for_update()
            .filter(transaction_id__in=tx_ids)
            .order_by("pk")
        )
    list(ImportBatch.objects.select_for_update().filter(account_id=account.pk).order_by("pk"))
    return locked_txs, pairs, refunds, members, exclusions, series_ids, tx_ids


def _repair_then_delete_account_rows(person, account):
    from finance.category_services import unmark_locked_pairs
    from finance.recurring_services import revalidate_series_after_member_removal

    locked_txs, pairs, refunds, members, exclusions, series_ids, tx_ids = _lock_rows_for_account_delete(account)
    unmark_locked_pairs(pairs, {item.pk: item for item in locked_txs}, person)
    if pairs:
        TransferPair.objects.filter(pk__in=[pair.pk for pair in pairs]).delete()
    if refunds:
        RefundLink.objects.filter(pk__in=[link.pk for link in refunds]).delete()
    if members:
        RecurringSeriesMember.objects.filter(pk__in=[member.pk for member in members]).delete()
    if exclusions:
        RecurringExclusion.objects.filter(pk__in=[row.pk for row in exclusions]).delete()
    revalidate_series_after_member_removal(person, series_ids)
    _delete_rule_history_for_account(account, tx_ids)
    if tx_ids:
        TransactionTag.objects.filter(transaction_id__in=tx_ids).delete()
        TransactionCorrectionHistory.objects.filter(transaction_id__in=tx_ids).delete()
        Transaction.objects.filter(pk__in=tx_ids).delete()
    ImportBatch.objects.filter(account_id=account.pk).delete()
    Alert.objects.filter(account_id=account.pk).delete()
    account.delete()


def _delete_rule_history_for_account(account, tx_ids):
    """Remove rule rows that would block deleting the account.

    Rule-application entries for the account's transactions go with them.
    A rule limited to this account is deleted with its applications, unless
    an application still holds entries for other accounts (the rule once
    applied more widely): then the rule is kept, disabled, and no longer
    limited to an account, so those rows stay reversible.
    """
    from finance.models import CategoryRule, RuleApplication, RuleApplicationEntry

    if tx_ids:
        RuleApplicationEntry.objects.filter(transaction_id__in=tx_ids).delete()
    account_rule_ids = set(CategoryRule.objects.filter(account_id=account.pk).values_list("pk", flat=True))
    if not account_rule_ids:
        return
    still_used = set(
        RuleApplicationEntry.objects.filter(application__rule_id__in=account_rule_ids).values_list(
            "application__rule_id", flat=True
        )
    )
    CategoryRule.objects.filter(pk__in=still_used).update(account=None, enabled=False)
    unused = account_rule_ids - still_used
    RuleApplication.objects.filter(rule_id__in=unused).delete()
    CategoryRule.objects.filter(pk__in=unused).delete()


@transaction.atomic
def delete_account(principal, account_id):
    """Permanently delete an account and every row that belongs to it."""
    person = _person_for(principal)
    lock_actor_household(person)
    if not Account.objects.visible_to(person).filter(pk=account_id).exists():
        raise PermissionDenied(_DENIED)
    account = _lock_visible_account_for_delete(person, account_id)
    if account.owner_id != person.pk:
        raise PermissionDenied(_DENIED)
    if not Account.objects.visible_to(person).filter(pk=account.pk).exists():
        raise PermissionDenied(_DENIED)
    _repair_then_delete_account_rows(person, account)
    return account.name
