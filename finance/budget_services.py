from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import prefetch_related_objects
from django.urls import reverse
from django.utils import timezone

from .access import DENIED as _DENIED
from .access import require_person as _person
from .audit_services import owned_audience, record
from .cash_flow import _combine_category_spending, format_minor, selected_accounts
from .category_services import current_household, spending_by_category_by_window
from .input_limits import BUDGET_MONTH_FLOOR, BUDGET_FUTURE_MONTHS, MAX_CARRY_MONTHS
from .models import Account, AuditEvent, Budget, BudgetAmount, BudgetRolloverReset, Category
from .months import add_months, month_end, month_start



def validate_budget_month(value, *, today=None):
    month = month_start(value)
    latest = add_months(month_start(today or timezone.localdate()), BUDGET_FUTURE_MONTHS)
    if not BUDGET_MONTH_FLOOR <= month <= latest:
        raise ValidationError("Budget month must be from January 2000 through the next 24 months.")
    return month


DUPLICATE_BUDGET = "An active budget already exists for this category and scope."


def parse_month(raw, *, today=None):
    today = today or timezone.localdate()
    if not raw:
        return month_start(today)
    try:
        parts = str(raw).strip().split("-")
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2]) if len(parts) >= 3 else 1
        parsed = date(year, month, day)
    except (TypeError, ValueError, IndexError):
        return month_start(today)
    return min(max(month_start(parsed), BUDGET_MONTH_FLOOR),
               add_months(month_start(today), BUDGET_FUTURE_MONTHS))


def _check_can_edit(person, budget):
    household = current_household(person)
    if budget.scope == Budget.Scope.HOUSEHOLD:
        if household is None or budget.household_id != household.pk:
            raise PermissionDenied(_DENIED)
    elif budget.owner_id != person.pk:
        raise PermissionDenied(_DENIED)


def report_scope_for(budget):
    if budget.scope == Budget.Scope.HOUSEHOLD:
        return Account.Scope.HOUSEHOLD
    return ""


def amount_for(budget, month):
    cached = getattr(budget, "_prefetched_objects_cache", {}).get("amounts")
    if cached is not None:
        rows = [row for row in cached if row.effective_month <= month_start(month)]
        row = max(rows, key=lambda row: row.effective_month, default=None)
        return 0 if row is None else row.amount_minor
    row = (
        BudgetAmount.objects.filter(budget=budget, effective_month__lte=month_start(month))
        .order_by("-effective_month")
        .first()
    )
    return 0 if row is None else row.amount_minor


def _spent_from_report(budget, report):
    if budget.category_id is None:
        return report.total_spending_minor
    category = budget.category
    if category.code == Category.Code.UNCATEGORIZED:
        key = "uncategorized"
    else:
        key = str(category.pk)
    for row in report.rows:
        if row.key == key:
            return row.spending_minor
    return 0


def _reports_for_months(principal, months, scope, accounts=None):
    """Budget-only report data, using the report's account and category semantics."""
    months = list(months)
    selected = selected_accounts(principal, scope=scope, cash_flow_only=True, accounts=accounts)
    named = {item.pk: item for item in Category.objects.visible_to(principal)}
    totals = spending_by_category_by_window(
        principal, [(month, month_end(month)) for month in months], accounts=selected
    )
    reports = {}
    for month, (total, by_category) in zip(months, totals):
        combined = _combine_category_spending(by_category, named)
        reports[month] = SimpleNamespace(
            total_spending_minor=total,
            rows=[SimpleNamespace(key=item["filter_value"], spending_minor=item["spending_minor"])
                  for item in combined.values()],
        )
    return reports


def last_reset_month(budget, month):
    cached = getattr(budget, "_prefetched_objects_cache", {}).get("rollover_resets")
    if cached is not None:
        return max((row.month for row in cached
                    if row.month <= month_start(month)
                    and (budget.rollover_enabled_at is None or row.created_at >= budget.rollover_enabled_at)),
                   default=None)
    resets = BudgetRolloverReset.objects.filter(budget=budget, month__lte=month_start(month))
    if budget.rollover_enabled_at is not None:
        resets = resets.filter(created_at__gte=budget.rollover_enabled_at)
    row = resets.order_by("-month").first()
    return None if row is None else row.month


def carry_start_month(budget, month):
    if not budget.rollover_enabled or budget.rollover_started_month is None:
        return None
    start = max(month_start(budget.rollover_started_month), BUDGET_MONTH_FLOOR,
                add_months(month_start(month), -MAX_CARRY_MONTHS))
    reset = last_reset_month(budget, month)
    if reset is not None and reset > start:
        start = reset
    return start


def _iter_months(start, end_inclusive):
    current = month_start(start)
    last = month_start(end_inclusive)
    while current <= last:
        yield current
        current = add_months(current, 1)


def carry_for(budget, month, reports):
    start = carry_start_month(budget, month)
    if start is None:
        return 0
    prior = add_months(month, -1)
    if start > prior:
        return 0
    total = 0
    for window in _iter_months(start, prior):
        total += amount_for(budget, window) - _spent_from_report(budget, reports[window])
    return total


def drilldown_url(budget, month):
    start = month_start(month)
    end = month_end(month)
    query = {"date_from": start.isoformat(), "date_to": end.isoformat()}
    scope = report_scope_for(budget)
    if scope:
        query["scope"] = scope
    if budget.category_id is not None:
        if budget.category.code == Category.Code.UNCATEGORIZED:
            query["category"] = "uncategorized"
        else:
            query["category"] = str(budget.category_id)
    return f"{reverse('transaction-list')}?{urlencode(query)}"


def progress_for(budget, month, reports):
    amount_minor = amount_for(budget, month)
    spent_minor = _spent_from_report(budget, reports[month_start(month)])
    carry_minor = carry_for(budget, month, reports)
    available_minor = amount_minor + carry_minor if budget.rollover_enabled else amount_minor
    remaining_minor = available_minor - spent_minor
    over_by_minor = max(-remaining_minor, 0)
    if available_minor > 0:
        percent = min(100, max(0, round((spent_minor / available_minor) * 100)))
    elif spent_minor > 0:
        percent = 100
    else:
        percent = 0
    name = "Overall spending" if budget.category_id is None else budget.category.name
    return SimpleNamespace(
        budget=budget,
        name=name,
        amount_minor=amount_minor,
        amount_display=format_minor(amount_minor),
        carry_minor=carry_minor,
        carry_display=format_minor(carry_minor),
        spent_minor=spent_minor,
        spent_display=format_minor(spent_minor),
        available_minor=available_minor,
        remaining_minor=remaining_minor,
        remaining_display=format_minor(remaining_minor),
        over_budget=over_by_minor > 0,
        over_by_minor=over_by_minor,
        over_by_display=format_minor(over_by_minor),
        percent=percent,
        drilldown_url=drilldown_url(budget, month),
        rollover_enabled=budget.rollover_enabled,
        archived=budget.status == Budget.Status.ARCHIVED,
    )


def progress_snapshot(budget, month, principal):
    return progress_snapshots([budget], month, principal)[budget.pk]


def progress_snapshots(budgets, month, principal):
    """Share report reads across a member's budgets, keeping scopes separate."""
    month = month_start(month)
    budgets = list(budgets)
    prefetch_related_objects(budgets, "category", "amounts", "rollover_resets")
    groups = {}
    for budget in budgets:
        groups.setdefault(report_scope_for(budget), []).append(budget)
    cards = {}
    for scope, group in groups.items():
        reports = _reports_for_months(principal, _needed_months(group, month), scope)
        cards.update({budget.pk: progress_for(budget, month, reports) for budget in group})
    return cards


def _needed_months(budgets, month):
    needed = {month_start(month)}
    for budget in budgets:
        start = carry_start_month(budget, month)
        if start is None:
            continue
        prior = add_months(month, -1)
        if start <= prior:
            needed.update(_iter_months(start, prior))
    return sorted(needed)


def month_budget_cards(principal, month, *, include_archived=False, accounts=None, include_household=True):
    month = month_start(month)
    budgets = Budget.objects.visible_to(principal).select_related("category").prefetch_related(
        "amounts", "rollover_resets"
    )
    if not include_archived:
        budgets = budgets.filter(status=Budget.Status.ACTIVE)
    if not include_household:
        person = _person(principal, check_authenticated=False)
        budgets = budgets.filter(scope=Budget.Scope.PRIVATE, owner=person)
    budgets = list(budgets.order_by("scope", "category__name", "pk"))
    by_scope = {}
    for budget in budgets:
        by_scope.setdefault(report_scope_for(budget), []).append(budget)
    cards = []
    for scope, group in by_scope.items():
        months = _needed_months(group, month)
        reports = _reports_for_months(principal, months, scope, accounts=accounts)
        for budget in group:
            cards.append(progress_for(budget, month, reports))
    cards.sort(key=lambda card: (card.budget.scope, card.name.lower(), card.budget.pk))
    return cards


NEAR_LIMIT_PERCENT = 85


def dashboard_budget_summary(principal, *, today=None):
    today = today or timezone.localdate()
    month = month_start(today)
    cards = month_budget_cards(principal, month)
    over_count = sum(1 for card in cards if card.over_budget)
    remaining_count = len(cards) - over_count
    spent_minor = sum(card.spent_minor for card in cards if card.budget.category_id is None)
    amount_minor = sum(card.amount_minor for card in cards if card.budget.category_id is None)
    overall = next((card for card in cards if card.budget.category_id is None), None)
    if overall is None and cards:
        spent_minor = sum(card.spent_minor for card in cards)
        amount_minor = sum(card.available_minor for card in cards)
    # Phone home shows the few budgets that need a look first: over, then closest to the limit.
    top_cards = sorted(cards, key=lambda card: (not card.over_budget, -card.percent, card.name.lower()))
    return SimpleNamespace(
        month=month,
        cards=cards,
        top_cards=top_cards[:3],
        attention_cards=[card for card in top_cards if card.over_budget or card.percent >= NEAR_LIMIT_PERCENT],
        count=len(cards),
        over_count=over_count,
        remaining_count=remaining_count,
        overall=overall,
        spent_minor=spent_minor,
        spent_display=format_minor(spent_minor),
        amount_minor=amount_minor,
        amount_display=format_minor(amount_minor),
    )


def _ensure_unique_active(person, *, scope, household, category, budget=None):
    qs = Budget.objects.filter(status=Budget.Status.ACTIVE, scope=scope, category=category)
    if scope == Budget.Scope.PRIVATE:
        qs = qs.filter(owner=person)
    else:
        qs = qs.filter(household=household)
    if budget is not None:
        qs = qs.exclude(pk=budget.pk)
    if qs.exists():
        raise ValidationError(DUPLICATE_BUDGET)


def save_budget(principal, payload, *, budget=None):
    person = _person(principal, check_authenticated=False)
    if budget is not None:
        _check_can_edit(person, budget)
        if budget.scope != payload["scope"] and budget.owner_id != person.pk:
            raise PermissionDenied(_DENIED)
        incoming_category = payload.get("category")
        incoming_id = None if incoming_category is None else incoming_category.pk
        if budget.category_id != incoming_id:
            raise ValidationError("A budget's category cannot be changed. Archive it and add a new one.")
    household = current_household(person)
    scope = payload["scope"]
    if scope == Budget.Scope.HOUSEHOLD:
        if household is None:
            raise ValidationError("Join a household before adding a household budget.")
        assigned_household = household
    else:
        assigned_household = None
    category = payload.get("category")
    if category is not None:
        if not Category.objects.visible_to(principal).filter(pk=category.pk).exists():
            raise PermissionDenied(_DENIED)
        if assigned_household is not None and category.household_id != assigned_household.pk:
            raise PermissionDenied(_DENIED)
    effective_month = validate_budget_month(payload["effective_month"])
    amount_minor = payload["amount_minor"]
    if amount_minor <= 0:
        raise ValidationError("Budget amount must be greater than zero.")
    if budget is None:
        budget = Budget(owner=person)
    _ensure_unique_active(
        person,
        scope=scope,
        household=assigned_household,
        category=category,
        budget=budget,
    )
    is_new = budget.pk is None
    changed = [] if is_new else (["scope"] if budget.scope != scope else [])
    budget.scope = scope
    budget.household = assigned_household
    if budget.pk is None:
        budget.category = category
        if payload.get("rollover_enabled"):
            budget.rollover_enabled = True
            budget.rollover_started_month = effective_month
            budget.rollover_enabled_at = timezone.now()
    try:
        with transaction.atomic():
            budget.save()
            if not is_new and BudgetAmount.objects.filter(
                budget=budget, effective_month=effective_month, amount_minor=amount_minor
            ).count() == 0:
                changed.append("amount")
            amount, _created = BudgetAmount.objects.update_or_create(
                budget=budget,
                effective_month=effective_month,
                defaults={"amount_minor": amount_minor, "currency": "USD"},
            )
            amount.amount_minor = amount_minor
            amount.currency = "USD"
            amount.save()
            if is_new or changed:
                record(person, AuditEvent.Action.RECORD_CREATED if is_new else AuditEvent.Action.RECORD_EDITED,
                       AuditEvent.TargetType.BUDGET, budget.pk, audience=owned_audience(budget), fields=changed)
    except IntegrityError as exc:
        raise ValidationError(DUPLICATE_BUDGET) from exc
    return budget


def set_budget_archived(principal, budget, archived):
    person = _person(principal, check_authenticated=False)
    _check_can_edit(person, budget)
    if archived:
        if budget.status == Budget.Status.ARCHIVED:
            return budget
        budget.status = Budget.Status.ARCHIVED
        budget.archived_at = timezone.now()
    else:
        _ensure_unique_active(
            person,
            scope=budget.scope,
            household=budget.household,
            category=budget.category,
            budget=budget,
        )
        budget.status = Budget.Status.ACTIVE
        budget.archived_at = None
    try:
        with transaction.atomic():
            budget.save(update_fields=("status", "archived_at", "updated_at"))
            record(person, AuditEvent.Action.RECORD_ARCHIVED if archived else AuditEvent.Action.RECORD_RESTORED,
                   AuditEvent.TargetType.BUDGET, budget.pk, audience=owned_audience(budget), fields=("status",))
    except IntegrityError as exc:
        raise ValidationError(DUPLICATE_BUDGET) from exc
    return budget


def _new_rollover_period_start(budget):
    """Start a rollover period strictly after every existing reset.

    A reset and re-enabling can share a clock tick, so "now" alone would let
    an old reset count in the new period.
    """
    started = timezone.now()
    latest = (
        BudgetRolloverReset.objects.filter(budget=budget)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if latest is not None and latest >= started:
        started = latest + timedelta(microseconds=1)
    return started


@transaction.atomic
def set_budget_rollover(principal, budget, enabled, *, month):
    person = _person(principal, check_authenticated=False)
    _check_can_edit(person, budget)
    month = validate_budget_month(month)
    was_enabled = budget.rollover_enabled
    if enabled:
        turning_on = not budget.rollover_enabled
        budget.rollover_enabled = True
        if budget.rollover_started_month is None:
            budget.rollover_started_month = month
        if turning_on:
            budget.rollover_enabled_at = _new_rollover_period_start(budget)
    else:
        budget.rollover_enabled = False
        budget.rollover_started_month = None
    budget.save(
        update_fields=("rollover_enabled", "rollover_started_month", "rollover_enabled_at", "updated_at")
    )
    if was_enabled != bool(enabled):
        record(person, AuditEvent.Action.ROLLOVER_TOGGLED, AuditEvent.TargetType.BUDGET, budget.pk,
               audience=owned_audience(budget), fields=("rollover",))
    return budget


def reset_budget_rollover(principal, budget, *, month):
    person = _person(principal, check_authenticated=False)
    _check_can_edit(person, budget)
    month = validate_budget_month(month)
    try:
        with transaction.atomic():
            # Replace rather than update, so created_at marks this reset and it counts
            # in the current rollover period (which ignores resets created earlier).
            BudgetRolloverReset.objects.filter(budget=budget, month=month).delete()
            reset = BudgetRolloverReset.objects.create(budget=budget, month=month, actor=person)
            # Never earlier than the current period's start, so a reset made in the
            # same clock tick as re-enabling still counts in the new period.
            if budget.rollover_enabled_at is not None and reset.created_at < budget.rollover_enabled_at:
                BudgetRolloverReset.objects.filter(pk=reset.pk).update(created_at=budget.rollover_enabled_at)
            record(person, AuditEvent.Action.RECORD_EDITED, AuditEvent.TargetType.BUDGET, budget.pk,
                   audience=owned_audience(budget), fields=("rollover",), metadata={"history_id": reset.pk})
    except IntegrityError as exc:
        raise ValidationError("Could not reset rollover for this month.") from exc
    return budget
