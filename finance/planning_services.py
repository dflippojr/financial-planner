from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Max

from .cash_flow import cash_flow_report
from .category_services import current_household
from .models import PlannedItem, RecurringSeries
from .projection import (
    DEFAULT_HORIZON,
    KIND_EXPENSE,
    SOURCE_PLANNED,
    SOURCE_SERIES,
    project_cash_flow,
    step_occurrence,
)

_DENIED = "Operation is not permitted."


def _planned_input(item):
    return SimpleNamespace(
        name=item.name,
        kind=item.kind,
        amount_minor=item.amount_minor,
        currency=item.currency,
        start=item.start_date,
        end=item.end_date,
        cadence=item.cadence,
        source=SOURCE_PLANNED,
        source_id=item.pk,
    )


def _series_input(series, last_on):
    return SimpleNamespace(
        name=series.display_name,
        kind=KIND_EXPENSE,
        amount_minor=abs(series.typical_amount_minor),
        currency=series.currency,
        start=step_occurrence(last_on, series.cadence),
        end=None,
        cadence=series.cadence,
        source=SOURCE_SERIES,
        source_id=series.pk,
    )


def visible_projection_inputs(principal):
    planned_rows = list(
        PlannedItem.objects.visible_to(principal)
        .filter(enabled=True)
        .select_related("replaces_series")
        .order_by("start_date", "pk")
    )
    replaced = {item.replaces_series_id for item in planned_rows if item.replaces_series_id}
    inputs = [_planned_input(item) for item in planned_rows]
    series_rows = (
        RecurringSeries.objects.visible_to(principal)
        .filter(status=RecurringSeries.Status.CONFIRMED, is_active=True)
        .exclude(pk__in=replaced)
        .annotate(last_on=Max("members__transaction__transaction_date"))
        .order_by("display_name", "pk")
    )
    for series in series_rows:
        if series.last_on is None:
            continue
        inputs.append(_series_input(series, series.last_on))
    return inputs


def projected_months_for(principal, *, today, horizon=DEFAULT_HORIZON):
    return project_cash_flow(visible_projection_inputs(principal), today=today, horizon=horizon)


def cash_flow_with_projection(
    principal,
    *,
    date_from,
    date_to,
    grouping,
    account=None,
    scope="",
    today,
    horizon=DEFAULT_HORIZON,
):
    report = cash_flow_report(
        principal,
        date_from=date_from,
        date_to=date_to,
        grouping=grouping,
        account=account,
        scope=scope,
        today=today,
    )
    report.projected_periods = projected_months_for(principal, today=today, horizon=horizon)
    report.horizon = horizon
    return report


def _person(principal):
    from .models import Person

    if isinstance(principal, Person):
        return principal
    try:
        return principal.person
    except Person.DoesNotExist as exc:
        raise PermissionDenied(_DENIED) from exc


def save_planned_item(principal, payload, *, item=None):
    person = _person(principal)
    household = current_household(person)
    scope = payload["scope"]
    if scope == PlannedItem.Scope.HOUSEHOLD:
        if household is None:
            raise ValidationError("Join a household before adding a household planned item.")
        assigned_household = household
    else:
        assigned_household = None
    if item is None:
        item = PlannedItem(owner=person)
    elif item.owner_id != person.pk and item.scope != PlannedItem.Scope.HOUSEHOLD:
        raise PermissionDenied(_DENIED)
    elif item.scope == PlannedItem.Scope.HOUSEHOLD and (
        household is None or item.household_id != household.pk
    ):
        raise PermissionDenied(_DENIED)
    item.scope = scope
    item.household = assigned_household
    item.name = payload["name"]
    item.kind = payload["kind"]
    item.amount_minor = payload["amount_minor"]
    item.currency = "USD"
    item.start_date = payload["start_date"]
    item.end_date = payload.get("end_date")
    item.cadence = payload["cadence"]
    item.category = payload.get("category")
    item.replaces_series = payload.get("replaces_series")
    if "enabled" in payload:
        item.enabled = payload["enabled"]
    item.save()
    return item


def set_planned_item_enabled(principal, item, enabled):
    save_planned_item(
        principal,
        {
            "scope": item.scope,
            "name": item.name,
            "kind": item.kind,
            "amount_minor": item.amount_minor,
            "start_date": item.start_date,
            "end_date": item.end_date,
            "cadence": item.cadence,
            "category": item.category,
            "replaces_series": item.replaces_series,
            "enabled": enabled,
        },
        item=item,
    )
