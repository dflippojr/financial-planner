from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q

from .access import DENIED as _DENIED
from .access import require_person as _person
from .cash_flow import cash_flow_report, selected_accounts
from .category_services import current_household
from .audit_services import changed_names, owned_audience, record, snapshot
from .bill_grouping import eligible_bill_charges, group_bills, stored_charge_ids
from .models import (
    Account,
    AuditEvent,
    Membership,
    PlannedItem,
    RecurringSeries,
    RecurringSeriesMember,
    SavingsGoal,
)
from .recurring_services import (
    ANCHOR_CHARGES,
    next_charge_date,
    projection_anchor_day,
    resolve_household_bills,
)
from .projection import (
    DEFAULT_HORIZON,
    KIND_EXPENSE,
    SOURCE_PLANNED,
    SOURCE_SERIES,
    project_cash_flow,
)
from .scenario import apply_scenario, compare_projected_months
from .savings_goal_services import goal_progress


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


def _series_input(series, charge_dates, last_charge=None):
    """A series' projection input from its charges' dates, oldest first.

    `last_charge` is the bill's latest charge when it is newer than the stored ones.
    """
    start = next_charge_date(charge_dates, series.cadence)
    if start is None:
        return None
    return SimpleNamespace(
        name=series.display_name,
        kind=KIND_EXPENSE,
        amount_minor=abs(series.typical_amount_minor),
        currency=series.currency,
        start=start,
        end=None,
        cadence=series.cadence,
        anchor_day=projection_anchor_day(charge_dates, series.cadence),
        source=SOURCE_SERIES,
        source_id=series.pk,
        last_account_id=last_charge.account_id if last_charge else None,
        last_transaction_id=last_charge.pk if last_charge else None,
    )


def confine_recurring_series_to_accounts(series_qs, accounts):
    """Keep a series only when every member already sits in `accounts`.

    A mixed private/household series is dropped entirely when household
    accounts are outside the allowed set, so its typical amount cannot leak.
    """
    if hasattr(accounts, "values"):
        allowed_ids = accounts.values("pk")
    else:
        allowed_ids = [item.pk for item in accounts]
    hidden = RecurringSeriesMember.objects.filter(series_id=OuterRef("pk")).exclude(
        transaction__account_id__in=allowed_ids
    )
    return series_qs.exclude(Exists(hidden))


def _household_members(principal):
    """The viewer's current household and its current members' ids."""
    person = _person(principal, check_authenticated=False)
    household = current_household(person)
    if household is None:
        return None, [person.pk]
    ids = Membership.objects.filter(household=household, ended_at__isnull=True, person__isnull=False)
    return household, list(ids.values_list("person_id", flat=True))


def _one_series_per_bill(rows, replaced, member_ids, household, eligible, *, selected=None):
    """Collapse members' series for one joint bill into a single series.

    Two members may each confirm the same household bill; it must project
    once. Grouping and the choice of copy are the page's and cancel's own
    (`bill_grouping`): series belong to one bill when shared charges link them,
    directly or through a third series, so two distinct bills from one
    merchant both stay. The copy with the latest eligible charge wins (it
    holds the current price), then the lowest id, and the bill's last date
    comes from the household's current charges, not the stored series. A
    planned item replacing a series replaces the whole bill, even when that
    series is no longer active, because its charges still identify the bill.
    A confirmed series that is cancelled or inactive still links the histories
    it shares charges with, as long as every charge sits in a household
    account, so private history never shapes the plan.

    `eligible` is the queryset of charges that count. `selected` narrows a
    single-account view: a bill stays only when one of its current charges, on
    any series that belongs to it or still to be caught up, sits in that
    queryset of transactions. The amount and schedule still come from the
    whole bill.
    """
    row_ids = {row.pk for row in rows}
    linkers = (
        RecurringSeries.objects.filter(
            Q(pk__in=replaced)
            | Q(person_id__in=member_ids, status=RecurringSeries.Status.CONFIRMED)
        )
        .exclude(pk__in=row_ids)
    )
    linkers = confine_recurring_series_to_accounts(
        linkers, Account.objects.filter(scope=Account.Scope.HOUSEHOLD, household=household)
    )
    bills = resolve_household_bills([*rows, *linkers], eligible)
    touched = None
    if selected is not None:
        # The bill's current charges count, so a payment that moved to the
        # selected account keeps the bill in that view.
        touched = set(
            selected.filter(pk__in={txn.pk for bill in bills for txn in bill.charges}).values_list("pk", flat=True)
        )
    by_pk = {row.pk: row for row in rows}
    chosen = []
    for bill in bills:
        if any(series.pk in replaced for series in bill.copies):
            continue
        if touched is not None and not any(txn.pk in touched for txn in bill.charges):
            continue
        current = bill.current
        if current is None or current.pk not in by_pk:
            continue
        row = by_pk[current.pk]
        row.last_on = bill.last_on
        row.last_charge = bill.charges[-1]
        row.charge_dates = [txn.transaction_date for txn in bill.charges[-ANCHOR_CHARGES:]]
        chosen.append(row)
    return sorted(chosen, key=lambda row: (row.display_name, row.pk))


def _with_whole_bills(principal, rows, replaced):
    """`replaced` widened to every series that shares a household bill with a replaced one (#522).

    A planned item that replaces one member's copy of a joint bill replaces the
    whole bill in the viewer's own view too, so the viewer's copy does not
    count beside the plan. Bills are grouped as in household scope: series
    built wholly from household charges, linked by shared stored charges,
    directly or through another member's confirmed copy. Two distinct bills
    from one merchant share no charge, so both stay.
    """
    if not replaced:
        return replaced
    household, member_ids = _household_members(principal)
    if household is None:
        return replaced
    candidates = RecurringSeries.objects.filter(
        Q(pk__in=replaced)
        | Q(pk__in=[row.pk for row in rows])
        | Q(person_id__in=member_ids, status=RecurringSeries.Status.CONFIRMED)
    )
    candidates = confine_recurring_series_to_accounts(
        candidates, Account.objects.filter(scope=Account.Scope.HOUSEHOLD, household=household)
    )
    widened = set(replaced)
    for bill in group_bills(stored_charge_ids(candidates.values_list("pk", flat=True))):
        if replaced.intersection(bill):
            widened.update(bill)
    return widened


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
        person = _person(principal, check_authenticated=False)
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
    selection = None
    household_only = False
    if account is not None or scope or accounts is not None:
        selection = selected_accounts(
            principal,
            account=account,
            scope=scope,
            cash_flow_only=True,
            accounts=accounts,
        )
        # A household-only selection judges transfer pairs by their household
        # legs, as the actual totals do, so every member projects the same.
        household_only = bool(selection) and all(
            item.scope == Account.Scope.HOUSEHOLD for item in selection
        )
    eligible = eligible_bill_charges(principal, household_only=household_only)
    # Only an explicit household scope shares series between members; a combined
    # view that happens to hold only household accounts stays the viewer's own.
    shared_series = household_only and scope == Account.Scope.HOUSEHOLD
    selected_charges = None
    if selection is not None:
        selected_charges = eligible.filter(account__in=selection)
    narrowed = False
    if shared_series:
        # A bill is the whole bill, whichever account a view selects: its
        # candidates, amount and schedule come from every household account,
        # and a single-account view only decides whether the bill shows.
        household_cash_flow = selected_accounts(
            principal, scope=Account.Scope.HOUSEHOLD, cash_flow_only=True
        )
        eligible = eligible.filter(account__in=household_cash_flow)
        narrowed = {item.pk for item in selection} != {item.pk for item in household_cash_flow}
        # Series are stored per person, so a household selection reads every
        # member's series whose charges all sit in household accounts. Every
        # member then projects the same bills.
        household, member_ids = _household_members(principal)
        series = RecurringSeries.objects.filter(person_id__in=member_ids)
    else:
        if selected_charges is not None:
            eligible = selected_charges
        series = RecurringSeries.objects.visible_to(principal)
    series = series.filter(
        status=RecurringSeries.Status.CONFIRMED,
        is_active=True,
        cancelled_at__isnull=True,
        members__transaction__in=eligible,
    )
    if shared_series:
        # Judge every charge against all household accounts, not just the
        # selected one, so a bill that moved between accounts stays whole.
        series = confine_recurring_series_to_accounts(
            series, selected_accounts(principal, scope=Account.Scope.HOUSEHOLD)
        )
    elif not include_household or accounts is not None:
        # Report filters select eligible charges; the allowed account set
        # independently protects every member behind a series's typical amount.
        # Earlier charges on another authorized account must not erase a series
        # that still has an eligible charge on the selected account.
        allowed = selected_accounts(
            principal,
            scope=Account.Scope.PRIVATE if not include_household else "",
            accounts=accounts,
        )
        series = confine_recurring_series_to_accounts(series, allowed)
    series_rows = list(
        series.annotate(last_on=Max("members__transaction__transaction_date")).order_by("display_name", "pk")
    )
    if shared_series:
        series_rows = _one_series_per_bill(
            series_rows, replaced, member_ids, household, eligible, selected=selected_charges if narrowed else None
        )
    else:
        replaced = _with_whole_bills(principal, series_rows, replaced)
        series_rows = [row for row in series_rows if row.pk not in replaced]
        recent = _recent_charge_dates([row.pk for row in series_rows], eligible)
        for row in series_rows:
            row.charge_dates = recent.get(row.pk, [])
    for series in series_rows:
        item = _series_input(series, series.charge_dates, getattr(series, "last_charge", None))
        # None when there is no charge, or when the next one would fall past
        # the calendar's end, so it cannot appear in any projection window.
        if item is not None:
            inputs.append(item)
    return inputs


def _recent_charge_dates(series_ids, eligible):
    """Each series' latest eligible charge dates, oldest first, enough to keep its anchor day."""
    dates = {}
    rows = (
        RecurringSeriesMember.objects.filter(series_id__in=series_ids, transaction__in=eligible)
        .order_by("series_id", "transaction__transaction_date", "transaction_id")
        .values_list("series_id", "transaction__transaction_date")
    )
    for series_id, transaction_date in rows:
        dates.setdefault(series_id, []).append(transaction_date)
    return {series_id: values[-ANCHOR_CHARGES:] for series_id, values in dates.items()}


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


def visible_savings_goal_dates(principal, *, today, scope=""):
    """Active visible goals with their target dates, for baseline and scenario."""
    goals = SavingsGoal.objects.visible_to(principal).filter(
        status=SavingsGoal.Status.ACTIVE,
        completed_at__isnull=True,
        target_date__isnull=False,
    )
    if scope:
        goals = goals.filter(scope=scope)
    rows = []
    for goal in goals.order_by("target_date", "name", "pk"):
        progress = goal_progress(principal, goal, today=today)
        rows.append(
            SimpleNamespace(
                name=goal.name,
                target_date=goal.target_date,
                remaining_display=progress.remaining_display,
            )
        )
    return tuple(rows)


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
    scenario_changes=(),
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
        actuals_only=True,
    )
    inputs = visible_projection_inputs(principal, account=account, scope=scope)
    report.projected_periods = project_cash_flow(inputs, today=today, horizon=horizon)
    scenario_inputs = apply_scenario(inputs, scenario_changes)
    report.scenario_projected_periods = project_cash_flow(scenario_inputs, today=today, horizon=horizon)
    report.scenario_comparison = compare_projected_months(
        report.projected_periods, report.scenario_projected_periods
    )
    report.projection_excludes_planned_items = account is not None
    report.horizon = horizon
    report.scenario_changes = tuple(scenario_changes)
    report.savings_goal_dates = visible_savings_goal_dates(principal, today=today, scope=scope)
    report.projection_inputs = inputs
    return report


PLANNED_AUDIT_FIELDS = {
    "scope": "scope", "name": "name", "kind": "kind", "amount": "amount_minor", "date": "start_date",
    "cadence": "cadence", "category": "category_id", "enabled": "enabled", "interval": "end_date",
}


def save_planned_item(principal, payload, *, item=None):
    person = _person(principal, check_authenticated=False)
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
        before = None
    elif item.owner_id != person.pk and item.scope != PlannedItem.Scope.HOUSEHOLD:
        raise PermissionDenied(_DENIED)
    elif item.scope == PlannedItem.Scope.HOUSEHOLD and (
        household is None or item.household_id != household.pk
    ):
        raise PermissionDenied(_DENIED)
    if item.pk is not None and item.owner_id != person.pk and scope != PlannedItem.Scope.HOUSEHOLD:
        raise PermissionDenied(_DENIED)
    if item.pk is not None:
        before = snapshot(item, PLANNED_AUDIT_FIELDS)
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
    with transaction.atomic():
        item.save()
        if before is None:
            record(person, AuditEvent.Action.RECORD_CREATED, AuditEvent.TargetType.PLANNED_ITEM, item.pk,
                   audience=owned_audience(item))
        else:
            changed = changed_names(before, snapshot(item, PLANNED_AUDIT_FIELDS))
            if changed == ["enabled"]:
                action = AuditEvent.Action.RECORD_ENABLED if item.enabled else AuditEvent.Action.RECORD_DISABLED
            else:
                action = AuditEvent.Action.RECORD_EDITED
            if changed:
                record(person, action, AuditEvent.TargetType.PLANNED_ITEM, item.pk,
                       audience=owned_audience(item), fields=changed)
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
