from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .category_services import (
    _DENIED,
    _history_label,
    _person_for,
    _record_text_history,
    _restore_refund_categories,
    assignable_categories,
    current_household,
    ensure_household_categories,
    exclusion_exists_for,
)
from .lifecycle_services import lock_actor_household
from .models import (
    Account,
    Category,
    CategoryRule,
    Membership,
    RefundLink,
    RuleApplication,
    RuleApplicationEntry,
    Transaction,
    TransactionCorrectionHistory,
    TransferPair,
)


def _rule_or_404(principal, rule_id):
    person = _person_for(principal)
    rule = (
        CategoryRule.objects.visible_to(person)
        .select_related("category", "account", "owner_person", "owner_household")
        .filter(pk=rule_id)
        .first()
    )
    if rule is None:
        raise PermissionDenied(_DENIED)
    return person, rule


def _personal_account_ids(person):
    return set(Account.objects.visible_to(person).values_list("pk", flat=True))


def _household_shared_account_ids(household):
    return set(
        Account.objects.filter(scope=Account.Scope.HOUSEHOLD, household=household).values_list("pk", flat=True)
    )


def _validate_rule_account(rule, person, household):
    if rule.account_id is None:
        return
    if rule.owner_person_id:
        if rule.account_id not in _personal_account_ids(person):
            raise ValidationError("Choose an account you can access.")
        return
    if rule.account.scope != Account.Scope.HOUSEHOLD or rule.account.household_id != household.pk:
        raise ValidationError("A household rule can only target a household-shared account.")


def _validate_rule_category(rule, household):
    if rule.category.household_id != household.pk or rule.category.code == Category.Code.TRANSFER:
        raise ValidationError("Choose a household category.")


def _visible_rule_account(person, account_id):
    if not account_id:
        return None
    account = Account.objects.visible_to(person).filter(pk=account_id).first()
    if account is None:
        raise PermissionDenied(_DENIED)
    return account


def _rule_for_save(person, household, rule_id, owner_kind, enabled):
    if rule_id is None:
        return CategoryRule(enabled=enabled)
    _, rule = _rule_or_404(person, rule_id)
    if owner_kind == "personal" and rule.owner_person_id != person.pk:
        raise PermissionDenied(_DENIED)
    if owner_kind == "household" and rule.owner_household_id != household.pk:
        raise PermissionDenied(_DENIED)
    return rule


def _set_rule_owner(rule, person, household, owner_kind):
    if owner_kind == "personal":
        rule.owner_person = person
        rule.owner_household = None
        return
    if owner_kind == "household":
        rule.owner_person = None
        rule.owner_household = household
        return
    raise ValidationError("Choose personal or household.")


@transaction.atomic
def save_category_rule(
    principal,
    *,
    rule_id=None,
    owner_kind,
    description_contains,
    account_id,
    min_amount_minor,
    max_amount_minor,
    category_id,
    priority,
    enabled=True,
):
    person = _person_for(principal)
    household = current_household(person)
    if household is None:
        raise PermissionDenied(_DENIED)
    lock_actor_household(person)
    ensure_household_categories(household)
    cleaned = (description_contains or "").strip()
    if not cleaned:
        raise ValidationError("Enter text to match in the description.")
    if min_amount_minor is not None and max_amount_minor is not None and min_amount_minor > max_amount_minor:
        raise ValidationError("The minimum amount must be at most the maximum.")
    category = assignable_categories(person).filter(pk=category_id).first()
    if category is None:
        raise PermissionDenied(_DENIED)
    rule = _rule_for_save(person, household, rule_id, owner_kind, enabled)
    _set_rule_owner(rule, person, household, owner_kind)
    rule.description_contains = cleaned
    rule.account = _visible_rule_account(person, account_id)
    rule.min_amount_minor = min_amount_minor
    rule.max_amount_minor = max_amount_minor
    rule.category = category
    rule.priority = priority
    rule.enabled = enabled
    # A new or edited rule must be previewed and applied again before it
    # categorizes imports and syncs on its own.
    rule.confirmed_at = None
    _validate_rule_account(rule, person, household)
    _validate_rule_category(rule, household)
    rule.save()
    return rule


@transaction.atomic
def set_rule_enabled(principal, rule_id, enabled):
    person, rule = _rule_or_404(principal, rule_id)
    lock_actor_household(person)
    rule.enabled = bool(enabled)
    rule.save(update_fields=("enabled", "updated_at"))
    return rule


def _people_who_can_see(account):
    if account.scope == Account.Scope.PRIVATE:
        return [account.owner_id]
    return list(
        Membership.objects.filter(household_id=account.household_id, ended_at__isnull=True).values_list(
            "person_id", flat=True
        )
    )


def personal_rule_is_inactive(rule):
    if not rule.owner_person_id:
        return False
    cached = getattr(rule, "_cached_inactive", None)
    if cached is None:
        cached = not Membership.objects.filter(
            person_id=rule.owner_person_id,
            household_id=rule.category.household_id,
            ended_at__isnull=True,
        ).exists()
        rule._cached_inactive = cached
    return cached


def ordered_rules_for_account(account, *, confirmed_only=False):
    enabled = CategoryRule.objects.filter(enabled=True)
    if confirmed_only:
        enabled = enabled.filter(confirmed_at__isnull=False)
    personal = [
        rule
        for rule in enabled.filter(
            owner_household__isnull=True,
            owner_person_id__in=_people_who_can_see(account),
        ).select_related("category", "account")
        .order_by("priority", "pk")
        if not personal_rule_is_inactive(rule)
    ]
    if account.scope != Account.Scope.HOUSEHOLD:
        return personal
    household = list(
        enabled.filter(owner_household_id=account.household_id)
        .select_related("category", "account")
        .order_by("priority", "pk")
    )
    return personal + household


def _owner_account_ids(rule):
    cached = getattr(rule, "_cached_owner_account_ids", None)
    if cached is None:
        cached = _personal_account_ids(rule.owner_person)
        rule._cached_owner_account_ids = cached
    return cached


def _rule_owner_matches_transaction(rule, txn):
    if rule.owner_household_id:
        return (
            txn.account.scope == Account.Scope.HOUSEHOLD
            and txn.account.household_id == rule.owner_household_id
        )
    if not rule.owner_person_id:
        return True
    if personal_rule_is_inactive(rule) or txn.account_id not in _owner_account_ids(rule):
        return False
    if txn.account.scope != Account.Scope.HOUSEHOLD:
        return True
    return txn.account.household_id == rule.category.household_id


def rule_matches_transaction(rule, txn):
    if rule.description_contains.casefold() not in txn.description.casefold():
        return False
    if rule.account_id and txn.account_id != rule.account_id:
        return False
    if rule.min_amount_minor is not None and txn.amount_minor < rule.min_amount_minor:
        return False
    if rule.max_amount_minor is not None and txn.amount_minor > rule.max_amount_minor:
        return False
    return _rule_owner_matches_transaction(rule, txn)


def first_matching_rule(txn, rules=None):
    candidates = rules if rules is not None else ordered_rules_for_account(txn.account)
    for rule in candidates:
        if rule_matches_transaction(rule, txn):
            return rule
    return None


def _is_winning_rule(txn, rule):
    winner = first_matching_rule(txn)
    return winner is not None and winner.pk == rule.pk


def _candidate_queryset(person):
    return (
        Transaction.objects.visible_to(person)
        .filter(status=Transaction.Status.ACTIVE, kind=Transaction.Kind.CASH_FLOW)
        .exclude(category_source=Transaction.CategorySource.MANUAL)
        .exclude(category_source=Transaction.CategorySource.INHERITED)
        .filter(Q(refund_link__isnull=True))
        .annotate(_excluded=exclusion_exists_for(person))
        .filter(_excluded=False)
        .select_related("account", "category", "account__owner", "account__household")
    )


def _matching_queryset(person, rule):
    qs = _candidate_queryset(person)
    if rule.account_id:
        qs = qs.filter(account_id=rule.account_id)
    if rule.min_amount_minor is not None:
        qs = qs.filter(amount_minor__gte=rule.min_amount_minor)
    if rule.max_amount_minor is not None:
        qs = qs.filter(amount_minor__lte=rule.max_amount_minor)
    if rule.owner_household_id:
        qs = qs.filter(account__scope=Account.Scope.HOUSEHOLD, account__household_id=rule.owner_household_id)
    elif rule.owner_person_id:
        if personal_rule_is_inactive(rule):
            return qs.none()
        qs = qs.filter(account_id__in=_personal_account_ids(rule.owner_person)).filter(
            Q(account__scope=Account.Scope.PRIVATE)
            | Q(account__scope=Account.Scope.HOUSEHOLD, account__household_id=rule.category.household_id)
        )
    # Match descriptions in Python with casefold, exactly like automatic
    # application, so preview, manual apply, and auto-apply agree (database
    # icontains folds case differently, for example STRASSE versus Straße).
    needle = rule.description_contains.casefold()
    matching_ids = [pk for pk, description in qs.values_list("pk", "description") if needle in description.casefold()]
    return qs.filter(pk__in=matching_ids)


def preview_rule(principal, rule_id):
    person, rule = _rule_or_404(principal, rule_id)
    matches = [
        txn
        for txn in _matching_queryset(person, rule).order_by("-transaction_date", "-pk")
        if _is_winning_rule(txn, rule)
    ]
    return rule, matches


def _history_new_label(category, rule):
    return f"{_history_label(category)} (rule: {rule.description_contains})"


def _linked_refunds(transactions):
    original_ids = [txn.pk for txn in transactions]
    if not original_ids:
        return []
    return list(
        Transaction.objects.filter(
            refund_link__original_id__in=original_ids,
            status=Transaction.Status.ACTIVE,
        )
    )


def _lock_transactions(person, transactions):
    lock_actor_household(person)
    to_lock = list(transactions) + _linked_refunds(transactions)
    account_ids = sorted({item.account_id for item in to_lock})
    list(Account.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
    ids = sorted({item.pk for item in to_lock})
    return list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "category", "account__owner", "account__household")
        .filter(pk__in=ids, status=Transaction.Status.ACTIVE)
        .order_by("pk")
    )


def _still_eligible(person, locked):
    """Recheck eligibility once rows are locked.

    A transfer confirmed (or a row reclassified) between the preview query and
    the lock must not be categorized by the rule.
    """
    eligible_ids = set(
        _candidate_queryset(person).filter(pk__in=[item.pk for item in locked]).values_list("pk", flat=True)
    )
    return [item for item in locked if item.pk in eligible_ids]


def _protected_source(txn):
    return txn.category_source in (
        Transaction.CategorySource.MANUAL,
        Transaction.CategorySource.INHERITED,
    )


def _apply_to_locked(person, rule, locked, *, require_first_match=True):
    changed = []
    skipped_manual = 0
    refund_ids = set(
        RefundLink.objects.filter(refund_id__in=[txn.pk for txn in locked]).values_list("refund_id", flat=True)
    )
    for txn in locked:
        if txn.category_source == Transaction.CategorySource.MANUAL:
            skipped_manual += 1
            continue
        if txn.category_source == Transaction.CategorySource.INHERITED or txn.pk in refund_ids:
            continue
        if require_first_match:
            winner = first_matching_rule(txn)
            if winner is None or winner.pk != rule.pk:
                continue
        elif not rule_matches_transaction(rule, txn):
            continue
        previous = txn.category
        previous_source = txn.category_source
        txn.category = rule.category
        txn.category_source = Transaction.CategorySource.RULE
        txn.save(update_fields=("category", "category_source", "updated_at"))
        _record_text_history(
            txn,
            person,
            TransactionCorrectionHistory.Field.CATEGORY,
            _history_label(previous),
            _history_new_label(rule.category, rule),
        )
        _restore_refund_categories(txn, person)
        changed.append((txn, previous, previous_source))
    if not changed:
        return None, skipped_manual
    application = RuleApplication.objects.create(rule=rule, applied_by=person, applied_at=timezone.now())
    RuleApplicationEntry.objects.bulk_create(
        [
            RuleApplicationEntry(
                application=application,
                transaction=txn,
                previous_category=previous,
                previous_category_source=previous_source,
            )
            for txn, previous, previous_source in changed
        ]
    )
    return application, skipped_manual


@transaction.atomic
def apply_rule(principal, rule_id):
    person, rule = _rule_or_404(principal, rule_id)
    if not rule.enabled:
        raise ValidationError("Enable the rule before applying it.")
    # Applying confirms the preview, so the rule now applies automatically.
    CategoryRule.objects.filter(pk=rule.pk).update(confirmed_at=timezone.now())
    preview = [
        txn for txn in _matching_queryset(person, rule).order_by("pk") if _is_winning_rule(txn, rule)
    ]
    if not preview:
        lock_actor_household(person)
        return None, 0
    locked = _still_eligible(person, _lock_transactions(person, preview))
    # Re-read the rule once rows are locked: a save that committed meanwhile
    # must not leave the old category applied under the updated rule.
    person, rule = _rule_or_404(principal, rule_id)
    if not rule.enabled:
        return None, 0
    return _apply_to_locked(person, rule, locked)


@transaction.atomic
def apply_enabled_rules_to_transactions(principal, transactions):
    """Apply enabled rules to newly imported rows. Call after transfer refresh."""
    if not transactions:
        return []
    person = _person_for(principal)
    # Same eligibility as preview and manual apply: active cash-flow rows that
    # are not excluded as transfers or card payments and are not refunds.
    requested_ids = {item.pk for item in transactions}
    locked = [
        item for item in _still_eligible(person, _lock_transactions(person, transactions)) if item.pk in requested_ids
    ]
    applications = []
    by_rule = {}
    for txn in locked:
        if _protected_source(txn):
            continue
        # Automatic application only uses rules whose preview was confirmed.
        winner = first_matching_rule(txn, ordered_rules_for_account(txn.account, confirmed_only=True))
        if winner is None:
            continue
        bucket = by_rule.setdefault(winner.pk, {"rule": winner, "rows": []})
        bucket["rows"].append(txn)
    for payload in by_rule.values():
        application, _skipped = _apply_to_locked(
            person,
            payload["rule"],
            payload["rows"],
            require_first_match=False,
        )
        if application is not None:
            applications.append(application)
    return applications


@transaction.atomic
def reverse_application(principal, application_id):
    person = _person_for(principal)
    application = _reversible_applications(person).select_related("rule").filter(pk=application_id).first()
    if application is None:
        raise PermissionDenied(_DENIED)
    if application.reversed_at is not None:
        return ReverseResult(restored=0, skipped_manual=0)
    # Only rows this person can see are reversed now. Rows hidden from them
    # (for example an account made private since) stay pending for their owner.
    entries = list(
        RuleApplicationEntry.objects.visible_to(person)
        .select_related("transaction", "previous_category")
        .filter(application=application, reversed_at__isnull=True)
        .order_by("transaction_id")
    )
    locked = _lock_transactions(person, [entry.transaction for entry in entries])
    # Recheck visibility once locked: an account made private meanwhile keeps
    # its rows and their entries pending for the owner.
    visible_ids = set(
        Transaction.objects.visible_to(person).filter(pk__in=[item.pk for item in locked]).values_list("pk", flat=True)
    )
    by_id = {item.pk: item for item in locked if item.pk in visible_ids}
    superseded_ids = set(
        RuleApplicationEntry.objects.filter(
            transaction_id__in=[entry.transaction_id for entry in entries],
            application_id__gt=application.pk,
            reversed_at__isnull=True,
        ).values_list("transaction_id", flat=True)
    )
    now = timezone.now()
    restored = 0
    skipped_manual = 0
    for entry in entries:
        txn = by_id.get(entry.transaction_id)
        if txn is None:
            continue
        entry.reversed_at = now
        entry.save(update_fields=("reversed_at",))
        if txn.pk in superseded_ids:
            # A later rule application changed this row; reversing this older
            # one must not undo the newer category.
            skipped_manual += 1
            continue
        if txn.category_source in (
            Transaction.CategorySource.MANUAL,
            Transaction.CategorySource.INHERITED,
        ):
            skipped_manual += 1
            continue
        previous = txn.category
        restored_category, restored_source = _category_before(entry, application)
        txn.category = restored_category
        txn.category_source = restored_source
        txn.save(update_fields=("category", "category_source", "updated_at"))
        _record_text_history(
            txn,
            person,
            TransactionCorrectionHistory.Field.CATEGORY,
            _history_label(previous),
            _history_label(restored_category),
        )
        _carry_into_transfer_snapshots(txn, previous, restored_category)
        _restore_refund_categories(txn, person)
        restored += 1
    if not RuleApplicationEntry.objects.filter(application=application, reversed_at__isnull=True).exists():
        application.reversed_at = now
        application.save(update_fields=("reversed_at",))
    return ReverseResult(restored=restored, skipped_manual=skipped_manual)


def _carry_into_transfer_snapshots(txn, removed, restored):
    """Keep a marked transfer's category snapshot in step with a reversal.

    Marking a transfer records each leg's category so undoing the transfer can
    put it back. If a rule's category is reversed while the leg is marked,
    that snapshot must not bring the reversed category back later.
    """
    removed_id = None if removed is None else removed.pk
    restored_id = None if restored is None else restored.pk
    for leg_field in ("leg_a", "leg_b"):
        snapshot_field = f"{leg_field}_category_id_at_mark"
        TransferPair.objects.excluding_income_and_spending().filter(
            **{leg_field: txn, snapshot_field: removed_id}
        ).update(**{snapshot_field: restored_id})


def _category_before(entry, application):
    """The category this row had before the chain of rules now being undone.

    An earlier application reversed while this one was in place could not
    restore its row then (this one superseded it), so its snapshot is the
    one to return to. Walk back through such reversals.
    """
    category = entry.previous_category
    source = entry.previous_category_source
    later_applied_at = application.applied_at
    earlier = (
        RuleApplicationEntry.objects.filter(transaction_id=entry.transaction_id, application_id__lt=application.pk)
        .select_related("application", "previous_category")
        .order_by("-application_id")
    )
    for older in earlier:
        if older.reversed_at is None:
            break
        if older.reversed_at <= later_applied_at:
            # Undone before the later application ran, so it was not in effect.
            continue
        category = older.previous_category
        source = older.previous_category_source
        later_applied_at = older.application.applied_at
    return category, source


def list_visible_rules(principal):
    person = _person_for(principal)
    rules = list(
        CategoryRule.objects.visible_to(person)
        .select_related("category", "account", "owner_person", "owner_household")
        .order_by("owner_household_id", "priority", "pk")
    )
    for rule in rules:
        rule.inactive = personal_rule_is_inactive(rule)
    return rules


def _pending_entries_visible_to(person):
    return RuleApplicationEntry.objects.visible_to(person).filter(reversed_at__isnull=True)


def _reversible_applications(person):
    """Applications this person may reverse.

    Any visible rule's applications, plus applications of rules they can no
    longer see that still have unreversed rows they can see: after leaving a
    household, its rule is hidden, yet only the former member can see (and so
    undo) its changes to their now-private account.
    """
    pending_ids = _pending_entries_visible_to(person).values("application_id")
    return RuleApplication.objects.filter(
        Q(pk__in=RuleApplication.objects.visible_to(person).values("pk")) | Q(pk__in=pending_ids)
    )


def list_unreachable_applications(principal):
    """Applications of rules this person cannot see that still changed their rows.

    Each item carries only the date and the number of this person's rows, never
    the hidden rule's text or category.
    """
    person = _person_for(principal)
    visible_rules = CategoryRule.objects.visible_to(person).values("pk")
    pending = _pending_entries_visible_to(person).exclude(application__rule_id__in=visible_rules)
    counts = {}
    for application_id in pending.values_list("application_id", flat=True):
        counts[application_id] = counts.get(application_id, 0) + 1
    applications = list(
        RuleApplication.objects.filter(pk__in=counts, reversed_at__isnull=True).order_by("-applied_at", "-pk")
    )
    for application in applications:
        application.your_row_count = counts[application.pk]
    return applications


def list_visible_applications(principal, rule):
    person = _person_for(principal)
    return list(
        RuleApplication.objects.visible_to(person)
        .filter(rule=rule)
        .select_related("applied_by")
        .order_by("-applied_at", "-pk")
    )


@dataclass(frozen=True)
class ReverseResult:
    restored: int
    skipped_manual: int
