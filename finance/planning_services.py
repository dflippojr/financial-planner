from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Max

from .cash_flow import cash_flow_report, selected_accounts
from .category_services import current_household, exclusion_exists_for
from .models import Account, PlannedItem, RecurringSeries, Transaction
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


def visible_projection_inputs(principal, *, account=None, scope="", accounts=None, include_household=True):
    """Planned items and confirmed series behind the projection.

    Filters match the actual report: a scope keeps planned items and series
    of that scope; one account keeps only that account's series, because
    planned items are not tied to an account.
    """
    planned = PlannedItem.objects.visible_to(principal).filter(enabled=True)
    if scope:
        planned = planned.filter(scope=scope)
    if not include_household:
        person = _person(principal)
        planned = planned.filter(scope=PlannedItem.Scope.PRIVATE, owner=person)
    planned_rows = list(planned.select_related("replaces_series").order_by("start_date", "pk"))
    if account is not None:
        # Planned items are left out for one account, so the series they
        # replace must stay in; otherwise the charge vanishes from both.
        planned_rows = []
    replaced = {item.replaces_series_id for item in planned_rows if item.replaces_series_id}
    inputs = [_planned_input(item) for item in planned_rows]
    # Only members still counted in actual cash flow keep a series going: an
    # archived account or transaction must not keep projecting charges.
    eligible = (
        Transaction.objects.visible_to(principal)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            account__status=Account.Status.ACTIVE,
            account__archived_at__isnull=True,
        )
        .annotate(_excluded=exclusion_exists_for(principal))
        .filter(_excluded=False)
        .values("pk")
    )
    if account is not None or scope or accounts is not None:
        eligible = eligible.filter(
            account__in=selected_accounts(
                principal,
                account=account,
                scope=scope,
                cash_flow_only=True,
                accounts=accounts,
            )
        )
    series = RecurringSeries.objects.visible_to(principal).filter(
        status=RecurringSeries.Status.CONFIRMED,
        is_active=True,
        cancelled_at__isnull=True,
        members__transaction__in=eligible,
    )
    series_rows = (
        series.exclude(pk__in=replaced)
        .annotate(last_on=Max("members__transaction__transaction_date"))
        .order_by("display_name", "pk")
    )
    for series in series_rows:
        if series.last_on is None:
            continue
        inputs.append(_series_input(series, series.last_on))
    return inputs


def projected_months_for(
    principal, *, today, horizon=DEFAULT_HORIZON, account=None, scope="", accounts=None, include_household=True
):
    inputs = visible_projection_inputs(
        principal,
        account=account,
        scope=scope,
        accounts=accounts,
        include_household=include_household,
    )
    return project_cash_flow(inputs, today=today, horizon=horizon)


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
    tag=None,
):
    report = cash_flow_report(
        principal,
        date_from=date_from,
        date_to=date_to,
        grouping=grouping,
        account=account,
        scope=scope,
        today=today,
        tag=tag,
    )
    report.projected_periods = projected_months_for(
        principal, today=today, horizon=horizon, account=account, scope=scope
    )
    report.projection_excludes_planned_items = account is not None
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
    if item.pk is not None and item.owner_id != person.pk and scope != PlannedItem.Scope.HOUSEHOLD:
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
    item.replaces_series = _replacement_series(person, item, payload.get("replaces_series"))
    if "enabled" in payload:
        item.enabled = payload["enabled"]
    item.save()
    return item


def _replacement_series(person, item, chosen):
    """Keep a series the editor cannot see; they could not have meant to clear it."""
    if chosen is not None or item.replaces_series_id is None:
        return chosen
    if RecurringSeries.objects.visible_to(person).filter(pk=item.replaces_series_id).exists():
        return None
    return item.replaces_series


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
