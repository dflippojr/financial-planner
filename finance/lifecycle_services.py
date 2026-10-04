from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from .models import (
    Account,
    Alert,
    ImportBatch,
    Membership,
    Person,
    Receipt,
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
LENT_HANDOVER = "handover"
LENT_DELETE = "delete"
LENT_CHOICES = (LENT_HANDOVER, LENT_DELETE)


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
        list(Receipt.objects.select_for_update().filter(transaction_id__in=tx_ids).order_by("pk"))
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
        from finance.receipt_services import delete_receipts_for_transactions

        TransactionTag.objects.filter(transaction_id__in=tx_ids).delete()
        TransactionCorrectionHistory.objects.filter(transaction_id__in=tx_ids).delete()
        delete_receipts_for_transactions(tx_ids)
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
    kept_id = account.pk
    name = account.name
    _repair_then_delete_account_rows(person, account)
    from finance.chat_services import delete_conversations_for_account

    delete_conversations_for_account(kept_id)
    return name


def last_household_member(person):
    membership = Membership.objects.filter(person=person, ended_at__isnull=True).first()
    if membership is None:
        return False
    return (
        Membership.objects.filter(household_id=membership.household_id, ended_at__isnull=True).count()
        == 1
    )


def lent_household_accounts(person):
    """Household accounts this member has lent, in primary-key order."""
    membership = Membership.objects.filter(person=person, ended_at__isnull=True).first()
    if membership is None:
        return []
    return list(
        Account.objects.filter(
            owner=person,
            scope=Account.Scope.HOUSEHOLD,
            household_id=membership.household_id,
            share_mode=Account.ShareMode.LENT,
        ).order_by("pk")
    )


def member_deletion_counts(person):
    """Counts shown on the delete-my-data confirmation page (counts only)."""
    from .models import (
        AiJob,
        AiProviderConnection,
        AiUsageEvent,
        Budget,
        CategoryRule,
        CategorySuggestion,
        PlannedItem,
        PrivacyPolicyAcceptance,
        RecoveryCode,
        RecurringSeries,
        RuleApplication,
        SavingsGoal,
        SimpleFinConnection,
    )

    private_ids = list(
        Account.objects.filter(owner=person, scope=Account.Scope.PRIVATE).values_list("pk", flat=True)
    )
    personal_records = (
        CategoryRule.objects.filter(owner_person=person).count()
        + RuleApplication.objects.filter(rule__owner_person=person).count()
        + Budget.objects.filter(owner=person, scope=Budget.Scope.PRIVATE).count()
        + PlannedItem.objects.filter(owner=person, scope=PlannedItem.Scope.PRIVATE).count()
        + SavingsGoal.objects.filter(owner=person, scope=SavingsGoal.Scope.PRIVATE).count()
        + RecurringSeries.objects.filter(person=person).count()
        + AiProviderConnection.objects.filter(owner=person).count()
        + AiJob.objects.filter(member=person).count()
        + AiUsageEvent.objects.filter(member=person).count()
        + CategorySuggestion.objects.filter(member=person).count()
        + SimpleFinConnection.objects.filter(owner=person).count()
        + PrivacyPolicyAcceptance.objects.filter(person=person).count()
        + RecoveryCode.objects.filter(user_id=person.user_id).count()
    )
    return {
        "private_account_count": len(private_ids),
        "transaction_count": Transaction.objects.filter(account_id__in=private_ids).count(),
        "import_count": ImportBatch.objects.filter(account_id__in=private_ids).count(),
        "personal_record_count": personal_records,
    }


def _parsed_lent_choices(person, lent_choices):
    lent = lent_household_accounts(person)
    required = {account.pk for account in lent}
    submitted = {} if lent_choices is None else {int(pk): value for pk, value in lent_choices.items()}
    if set(submitted) != required:
        raise ValidationError("Every lent account needs a choice.")
    if any(value not in LENT_CHOICES for value in submitted.values()):
        raise ValidationError("Every lent account needs a choice.")
    return lent, submitted


def _handover_lent_accounts(lent, submitted, successor):
    handover_ids = [account.pk for account in lent if submitted[account.pk] == LENT_HANDOVER]
    if handover_ids and successor is None:
        raise ValidationError("Every lent account needs a choice.")
    if handover_ids:
        Account.objects.filter(pk__in=handover_ids).update(
            share_mode=Account.ShareMode.CO_OWNED,
            updated_at=timezone.now(),
        )


def _release_household_owned_rows(person):
    """Hand household rows this person still owns to a current member, in every household.

    The person may own rows in a household they already left (or were evicted
    from). Each household's earliest current member takes them over; a household
    with nobody current keeps its other rows; only this person's rows there go.
    """
    from .models import Budget, PlannedItem, SavedCsvMapping, SavingsGoal

    owned = (
        PlannedItem.objects.filter(owner=person, scope=PlannedItem.Scope.HOUSEHOLD),
        SavingsGoal.objects.filter(owner=person, scope=SavingsGoal.Scope.HOUSEHOLD),
        Budget.objects.filter(owner=person, scope=Budget.Scope.HOUSEHOLD),
    )
    household_ids = set()
    for rows in owned:
        household_ids.update(rows.values_list("household_id", flat=True))
    household_ids.update(SavedCsvMapping.objects.filter(created_by=person).values_list("household_id", flat=True))
    for household_id in sorted(pk for pk in household_ids if pk is not None):
        successor_membership = (
            Membership.objects.filter(household_id=household_id, ended_at__isnull=True)
            .exclude(person=person)
            .select_related("person")
            .order_by("joined_at", "pk")
            .first()
        )
        if successor_membership is None:
            # Nobody current to hand them to: remove only this person's rows there,
            # never other former members' rows or the household itself.
            for rows in owned:
                rows.filter(household_id=household_id).delete()
            mine = list(
                SavedCsvMapping.objects.filter(created_by=person, household_id=household_id).values_list("pk", flat=True)
            )
            if mine:
                ImportBatch.objects.filter(saved_csv_mapping_id__in=mine).update(saved_csv_mapping=None)
                Account.objects.filter(default_saved_csv_mapping_id__in=mine).update(default_saved_csv_mapping=None)
                SavedCsvMapping.objects.filter(pk__in=mine).delete()
            continue
        successor = successor_membership.person
        for rows in owned:
            rows.filter(household_id=household_id).update(owner=successor)
        SavedCsvMapping.objects.filter(created_by=person, household_id=household_id).update(created_by=successor)


def _delete_personal_records(person):
    from .models import (
        Alert,
        AlertSettings,
        AiJob,
        AiProviderConnection,
        AiUsageEvent,
        Budget,
        CategoryRule,
        CategorySuggestion,
        PlannedItem,
        PrivacyPolicyAcceptance,
        RecurringExclusion,
        RecurringSeries,
        RecurringSeriesMember,
        RuleApplication,
        RuleApplicationEntry,
        SavingsGoal,
        SimpleFinConnection,
    )

    PlannedItem.objects.filter(owner=person, scope=PlannedItem.Scope.PRIVATE).delete()
    SavingsGoal.objects.filter(owner=person, scope=SavingsGoal.Scope.PRIVATE).delete()
    Budget.objects.filter(owner=person, scope=Budget.Scope.PRIVATE).delete()
    rule_ids = list(CategoryRule.objects.filter(owner_person=person).values_list("pk", flat=True))
    if rule_ids:
        RuleApplicationEntry.objects.filter(application__rule_id__in=rule_ids).delete()
        RuleApplication.objects.filter(rule_id__in=rule_ids).delete()
        CategoryRule.objects.filter(pk__in=rule_ids).delete()
    series_ids = list(RecurringSeries.objects.filter(person=person).values_list("pk", flat=True))
    if series_ids:
        RecurringSeriesMember.objects.filter(series_id__in=series_ids).delete()
        RecurringSeries.objects.filter(pk__in=series_ids).delete()
    SimpleFinConnection.objects.filter(owner=person).delete()
    CategorySuggestion.objects.filter(member=person).delete()
    AiJob.objects.filter(member=person).delete()
    AiUsageEvent.objects.filter(member=person).delete()
    AiProviderConnection.objects.filter(owner=person).delete()
    PrivacyPolicyAcceptance.objects.filter(person=person).delete()
    RecurringExclusion.objects.filter(person=person).delete()
    Alert.objects.filter(recipient=person).delete()
    AlertSettings.objects.filter(person=person).delete()
    from .models import MemberSecurityEvent, MemberSession

    MemberSecurityEvent.objects.filter(member=person).delete()
    MemberSession.objects.filter(member=person).delete()
    person.privacy_policy_declined_version = None
    person.save(update_fields=("privacy_policy_declined_version", "updated_at"))


def _anonymize_shared_actor_refs(person):
    from .models import BudgetRolloverReset, Invitation, RuleApplication

    TransactionCorrectionHistory.objects.filter(actor=person).update(actor=None)
    ImportBatch.objects.filter(imported_by=person).update(imported_by=None)
    RuleApplication.objects.filter(applied_by=person).update(applied_by=None)
    Invitation.objects.filter(invited_by=person).update(invited_by=None)
    BudgetRolloverReset.objects.filter(actor=person).update(actor=None)
    Membership.objects.filter(person=person).update(person=None)


def _tear_down_household(household_id):
    from .models import (
        Budget,
        Category,
        CategoryRule,
        Household,
        Invitation,
        PlannedItem,
        RuleApplication,
        RuleApplicationEntry,
        SavedCsvMapping,
        SavingsGoal,
        Tag,
    )

    if Membership.objects.filter(household_id=household_id, ended_at__isnull=True).exists():
        return
    rule_ids = list(CategoryRule.objects.filter(owner_household_id=household_id).values_list("pk", flat=True))
    if rule_ids:
        RuleApplicationEntry.objects.filter(application__rule_id__in=rule_ids).delete()
        RuleApplication.objects.filter(rule_id__in=rule_ids).delete()
        CategoryRule.objects.filter(pk__in=rule_ids).delete()
    PlannedItem.objects.filter(household_id=household_id).delete()
    SavingsGoal.objects.filter(household_id=household_id).delete()
    Budget.objects.filter(household_id=household_id).delete()
    Tag.objects.filter(household_id=household_id).delete()
    mapping_ids = list(SavedCsvMapping.objects.filter(household_id=household_id).values_list("pk", flat=True))
    if mapping_ids:
        ImportBatch.objects.filter(saved_csv_mapping_id__in=mapping_ids).update(saved_csv_mapping=None)
        Account.objects.filter(default_saved_csv_mapping_id__in=mapping_ids).update(default_saved_csv_mapping=None)
        SavedCsvMapping.objects.filter(pk__in=mapping_ids).delete()
    Category.objects.filter(household_id=household_id).delete()
    Invitation.objects.filter(household_id=household_id).delete()
    Membership.objects.filter(household_id=household_id).delete()
    Household.objects.filter(pk=household_id).delete()


def _delete_empty_household(household_id):
    """Remove a household with no current members, all at once or not at all.

    Former members' private transactions, splits, rules, or budgets may still use
    its categories or tags (PROTECT). Then the whole teardown rolls back and the
    household keeps every row, rather than changing another person's private data
    or deleting only part of the household.
    """
    try:
        with transaction.atomic():
            _tear_down_household(household_id)
    except ProtectedError:
        pass


def _delete_login_user(user):
    from allauth.socialaccount.models import SocialAccount

    from finance.auth_services import revoke_user_sessions

    SocialAccount.objects.filter(user=user).delete()
    revoke_user_sessions(user)
    user.delete()


@transaction.atomic
def delete_member_data(principal, lent_choices=None):
    """Remove this member's private data and login, keeping shared household rows."""
    person = _person_for(principal)
    user = person.user
    lent, submitted = _parsed_lent_choices(person, lent_choices)
    own_membership, current_memberships = lock_actor_household(person)
    household_id = own_membership.household_id if own_membership is not None else None
    remaining = []
    if own_membership is not None:
        remaining = sorted(
            (membership for membership in current_memberships if membership.pk != own_membership.pk),
            key=lambda membership: (membership.joined_at, membership.pk),
        )
    successor = remaining[0].person if remaining else None
    owned_ids = list(Account.objects.filter(owner=person).values_list("pk", flat=True))
    _lock_accounts_in_pk_order(owned_ids)
    _handover_lent_accounts(lent, submitted, successor)
    if own_membership is not None:
        end_current_membership(person)
    _release_household_owned_rows(person)
    remaining_private_ids = list(
        Account.objects.filter(owner=person).order_by("pk").values_list("pk", flat=True)
    )
    for account_id in remaining_private_ids:
        delete_account(person, account_id)
    _delete_personal_records(person)
    _anonymize_shared_actor_refs(person)
    if household_id is not None:
        _delete_empty_household(household_id)
    person.delete()
    _delete_login_user(user)

