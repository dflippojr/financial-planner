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
    account = None
    if account_id:
        account = Account.objects.visible_to(person).filter(pk=account_id).first()
        if account is None:
            raise PermissionDenied(_DENIED)
    if rule_id is None:
        rule = CategoryRule(enabled=enabled)
    else:
        _, rule = _rule_or_404(person, rule_id)
        if owner_kind == "personal" and rule.owner_person_id != person.pk:
            raise PermissionDenied(_DENIED)
        if owner_kind == "household" and rule.owner_household_id != household.pk:
            raise PermissionDenied(_DENIED)
    if owner_kind == "personal":
        rule.owner_person = person
        rule.owner_household = None
    elif owner_kind == "household":
        rule.owner_person = None
        rule.owner_household = household
    else:
        raise ValidationError("Choose personal or household.")
    rule.description_contains = cleaned
    rule.account = account
    rule.min_amount_minor = min_amount_minor
    rule.max_amount_minor = max_amount_minor
    rule.category = category
    rule.priority = priority
    rule.enabled = enabled
    _validate_rule_account(rule, person, household)
    _validate_rule_category(rule, household)
    rule.save()
    return rule


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


def ordered_rules_for_account(account):
    personal = list(
        CategoryRule.objects.filter(
            enabled=True,
            owner_household__isnull=True,
            owner_person_id__in=_people_who_can_see(account),
        ).select_related("category", "account")
        .order_by("priority", "pk")
    )
    if account.scope != Account.Scope.HOUSEHOLD:
        return personal
    household = list(
        CategoryRule.objects.filter(enabled=True, owner_household_id=account.household_id)
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


def rule_matches_transaction(rule, txn):
    if rule.description_contains.casefold() not in txn.description.casefold():
        return False
    if rule.account_id and txn.account_id != rule.account_id:
        return False
    if rule.min_amount_minor is not None and txn.amount_minor < rule.min_amount_minor:
        return False
    if rule.max_amount_minor is not None and txn.amount_minor > rule.max_amount_minor:
        return False
    if rule.owner_household_id:
        if txn.account.scope != Account.Scope.HOUSEHOLD or txn.account.household_id != rule.owner_household_id:
            return False
    elif rule.owner_person_id and txn.account_id not in _owner_account_ids(rule):
        return False
    return True


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
    qs = qs.filter(description__icontains=rule.description_contains)
    if rule.account_id:
        qs = qs.filter(account_id=rule.account_id)
    if rule.min_amount_minor is not None:
        qs = qs.filter(amount_minor__gte=rule.min_amount_minor)
    if rule.max_amount_minor is not None:
        qs = qs.filter(amount_minor__lte=rule.max_amount_minor)
    if rule.owner_household_id:
        qs = qs.filter(account__scope=Account.Scope.HOUSEHOLD, account__household_id=rule.owner_household_id)
    elif rule.owner_person_id:
        qs = qs.filter(account_id__in=_personal_account_ids(rule.owner_person))
    return qs


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


def _lock_transactions(person, transactions):
    lock_actor_household(person)
    account_ids = sorted({item.account_id for item in transactions})
    list(Account.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
    ids = sorted(item.pk for item in transactions)
    return list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "category", "account__owner", "account__household")
        .filter(pk__in=ids, status=Transaction.Status.ACTIVE)
        .order_by("pk")
    )


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
    preview = [
        txn for txn in _matching_queryset(person, rule).order_by("pk") if _is_winning_rule(txn, rule)
    ]
    if not preview:
        lock_actor_household(person)
        return None, 0
    locked = _lock_transactions(person, preview)
    return _apply_to_locked(person, rule, locked)


@transaction.atomic
def apply_enabled_rules_to_transactions(principal, transactions):
    """Apply enabled rules to newly imported rows. Call after transfer refresh."""
    if not transactions:
        return []
    person = _person_for(principal)
    locked = _lock_transactions(person, transactions)
    applications = []
    by_rule = {}
    for txn in locked:
        if _protected_source(txn):
            continue
        winner = first_matching_rule(txn)
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
    application = (
        RuleApplication.objects.visible_to(person)
        .select_related("rule")
        .filter(pk=application_id)
        .first()
    )
    if application is None:
        raise PermissionDenied(_DENIED)
    entries = list(
        RuleApplicationEntry.objects.visible_to(person)
        .select_related("transaction", "previous_category")
        .filter(application=application)
        .order_by("transaction_id")
    )
    locked = _lock_transactions(person, [entry.transaction for entry in entries])
    by_id = {item.pk: item for item in locked}
    restored = 0
    skipped_manual = 0
    for entry in entries:
        txn = by_id.get(entry.transaction_id)
        if txn is None:
            continue
        if txn.category_source == Transaction.CategorySource.MANUAL:
            skipped_manual += 1
            continue
        previous = txn.category
        restored_category = entry.previous_category
        txn.category = restored_category
        txn.category_source = entry.previous_category_source
        txn.save(update_fields=("category", "category_source", "updated_at"))
        _record_text_history(
            txn,
            person,
            TransactionCorrectionHistory.Field.CATEGORY,
            _history_label(previous),
            _history_label(restored_category),
        )
        restored += 1
    return ReverseResult(restored=restored, skipped_manual=skipped_manual)


def list_visible_rules(principal):
    person = _person_for(principal)
    return list(
        CategoryRule.objects.visible_to(person)
        .select_related("category", "account", "owner_person", "owner_household")
        .order_by("owner_household_id", "priority", "pk")
    )


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
