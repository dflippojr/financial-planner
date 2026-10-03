from calendar import monthrange
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from .cash_flow import format_minor, spending_by_category_report
from .category_services import current_household
from .models import Account, Budget, BudgetAmount, BudgetRolloverReset, Category

_DENIED = "Operation is not permitted."
DUPLICATE_BUDGET = "An active budget already exists for this category and scope."


def month_start(value):
    return date(value.year, value.month, 1)


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def add_months(start, delta):
    start = month_start(start)
    index = start.year * 12 + (start.month - 1) + delta
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, 1)


def parse_month(raw, *, today=None):
    today = today or timezone.localdate()
    if not raw:
        return month_start(today)
    try:
        year_s, month_s = raw.split("-", 1)
        parsed = date(int(year_s), int(month_s), 1)
    except (TypeError, ValueError):
        return month_start(today)
    return parsed


def _person(principal):
    from .models import Person

    if isinstance(principal, Person):
        return principal
    try:
        return principal.person
    except Person.DoesNotExist as exc:
        raise PermissionDenied(_DENIED) from exc


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


def _reports_for_months(principal, months, scope):
    cache = {}
    for month in months:
        cache[month] = spending_by_category_report(
            principal,
            date_from=month,
            date_to=month_end(month),
            scope=scope,
        )
    return cache


def last_reset_month(budget, month):
    row = (
        BudgetRolloverReset.objects.filter(budget=budget, month__lte=month_start(month))
        .order_by("-month")
        .first()
    )
    return None if row is None else row.month


def carry_start_month(budget, month):
    if not budget.rollover_enabled or budget.rollover_started_month is None:
        return None
    start = month_start(budget.rollover_started_month)
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


def month_budget_cards(principal, month, *, include_archived=False):
    month = month_start(month)
    budgets = Budget.objects.visible_to(principal).select_related("category")
    if not include_archived:
        budgets = budgets.filter(status=Budget.Status.ACTIVE)
    budgets = list(budgets.order_by("scope", "category__name", "pk"))
    by_scope = {}
    for budget in budgets:
        by_scope.setdefault(report_scope_for(budget), []).append(budget)
    cards = []
    for scope, group in by_scope.items():
        months = _needed_months(group, month)
        reports = _reports_for_months(principal, months, scope)
        for budget in group:
            cards.append(progress_for(budget, month, reports))
    cards.sort(key=lambda card: (card.budget.scope, card.name.lower(), card.budget.pk))
    return cards


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
    return SimpleNamespace(
        month=month,
        cards=cards,
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
    person = _person(principal)
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
    effective_month = month_start(payload["effective_month"])
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
    budget.scope = scope
    budget.household = assigned_household
    if budget.pk is None:
        budget.category = category
        if payload.get("rollover_enabled"):
            budget.rollover_enabled = True
            budget.rollover_started_month = effective_month
    try:
        with transaction.atomic():
            budget.save()
            amount, _created = BudgetAmount.objects.update_or_create(
                budget=budget,
                effective_month=effective_month,
                defaults={"amount_minor": amount_minor, "currency": "USD"},
            )
            amount.amount_minor = amount_minor
            amount.currency = "USD"
            amount.save()
    except IntegrityError as exc:
        raise ValidationError(DUPLICATE_BUDGET) from exc
    return budget


def set_budget_archived(principal, budget, archived):
    person = _person(principal)
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
    except IntegrityError as exc:
        raise ValidationError(DUPLICATE_BUDGET) from exc
    return budget


def set_budget_rollover(principal, budget, enabled, *, month):
    person = _person(principal)
    _check_can_edit(person, budget)
    month = month_start(month)
    if enabled:
        budget.rollover_enabled = True
        if budget.rollover_started_month is None:
            budget.rollover_started_month = month
    else:
        budget.rollover_enabled = False
        budget.rollover_started_month = None
    budget.save(update_fields=("rollover_enabled", "rollover_started_month", "updated_at"))
    return budget


def reset_budget_rollover(principal, budget, *, month):
    person = _person(principal)
    _check_can_edit(person, budget)
    month = month_start(month)
    try:
        with transaction.atomic():
            BudgetRolloverReset.objects.update_or_create(
                budget=budget,
                month=month,
                defaults={"actor": person},
            )
    except IntegrityError as exc:
        raise ValidationError("Could not reset rollover for this month.") from exc
    return budget
