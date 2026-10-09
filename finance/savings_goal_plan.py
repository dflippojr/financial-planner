"""Read-only funding plan: when each ranked savings goal becomes affordable.

The plan takes the existing projection's monthly surplus, holds back a one-time
safety buffer, then fills goals in rank order. It never feeds back into the
projection, and all arithmetic is integer minor units.
"""

from collections import deque
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .access import DENIED as _DENIED
from .access import require_person as _person
from .audit_services import record
from .cash_flow import format_minor, selected_accounts
from .category_services import current_household, income_and_spending_by_window
from .lifecycle_services import lock_actor_household
from .models import Account, AuditEvent, SavingsGoal
from .months import add_months, month_end, month_start
from .planning_services import visible_projection_inputs
from .projection import DEFAULT_HORIZON, HORIZONS, project_cash_flow
from .savings_goal_services import goal_progress

SCOPE_ALL = ""
SCOPES = (SCOPE_ALL, SavingsGoal.Scope.PRIVATE, SavingsGoal.Scope.HOUSEHOLD)
BUFFER_MONTHS = 3
ALREADY_FUNDED = -1
BUFFER_DEFAULT = "default"
BUFFER_HOUSEHOLD = "household"
MAX_BUFFER_MINOR = 10**12

ON_TIME = "on_time"
LATE = "late"
BEYOND_HORIZON = "beyond_horizon"


def order_goals(goals):
    """Fill order: priority (unranked last), a goal only after its open dependency, then id."""
    pending = sorted(goals, key=lambda goal: (goal.priority is None, goal.priority or 0, goal.pk))
    open_ids = {goal.pk for goal in pending}
    placed = set()
    ordered = []
    while pending:
        # A dependency that is complete, archived or outside this plan no longer
        # holds the goal back. Saved goals cannot form a cycle; if one somehow
        # does, fall back to rank order rather than loop.
        chosen = next(
            (
                goal
                for goal in pending
                if goal.depends_on_id is None or goal.depends_on_id not in open_ids or goal.depends_on_id in placed
            ),
            pending[0],
        )
        pending.remove(chosen)
        placed.add(chosen.pk)
        ordered.append(chosen)
    return ordered


def fill_goals(remaining_by_goal, surplus_by_month, buffer_minor):
    """Fill goals in the given order from each month's surplus.

    `remaining_by_goal` is an ordered list of `(goal_id, remaining_minor)`.
    The one-time buffer is set aside first from positive surplus; a negative
    month adds nothing to goals or the buffer and never undoes progress. A
    goal that needs nothing more is `ALREADY_FUNDED`; one that does not finish
    inside the horizon is `None`; otherwise its value is the month index.
    Returns `(funded_by_goal, months)`, where each month holds its surplus,
    the amount set aside for the buffer and the amount offered to goals.
    """
    funded = {}
    remaining = {}
    queue = deque()
    for goal_id, remaining_minor in remaining_by_goal:
        if remaining_minor <= 0:
            funded[goal_id] = ALREADY_FUNDED
        else:
            funded[goal_id] = None
            remaining[goal_id] = remaining_minor
            queue.append(goal_id)
    reserve = buffer_minor
    months = []
    for index, surplus in enumerate(surplus_by_month):
        positive = max(surplus, 0)
        held = min(positive, reserve)
        reserve -= held
        available = positive - held
        months.append(SimpleNamespace(surplus_minor=surplus, held_minor=held, available_minor=available))
        while available > 0 and queue:
            goal_id = queue[0]
            take = min(available, remaining[goal_id])
            remaining[goal_id] -= take
            available -= take
            if remaining[goal_id] == 0:
                funded[goal_id] = index
                queue.popleft()
    return funded, months


def target_check(target_date, funded_index, months):
    """Compare a goal's funded month to its target month (the same month counts as on time)."""
    if target_date is None:
        return None
    target_month = month_start(target_date)
    if funded_index == ALREADY_FUNDED:
        return ON_TIME
    if funded_index is not None:
        return ON_TIME if months[funded_index].start <= target_month else LATE
    return LATE if target_month <= months[-1].start else BEYOND_HORIZON


def _scope_inputs(principal, scope):
    """Accounts and projection inputs for a plan scope.

    The household plan uses household accounts and items only, so it comes out
    the same for every member whatever their private data holds.
    """
    if scope == SavingsGoal.Scope.HOUSEHOLD:
        accounts = Account.objects.visible_to(principal).filter(scope=Account.Scope.HOUSEHOLD)
        inputs = visible_projection_inputs(principal, scope=scope, accounts=accounts)
    elif scope == SavingsGoal.Scope.PRIVATE:
        accounts = Account.objects.visible_to(principal).filter(scope=Account.Scope.PRIVATE)
        inputs = visible_projection_inputs(principal, scope=scope, accounts=accounts, include_household=False)
    else:
        accounts = None
        inputs = visible_projection_inputs(principal)
    return selected_accounts(principal, cash_flow_only=True, accounts=accounts), inputs


def default_buffer_minor(principal, accounts, today):
    """One month of average actual spending over the last complete months (integer division)."""
    this_month = month_start(today)
    windows = [
        (add_months(this_month, -offset), month_end(add_months(this_month, -offset)))
        for offset in range(BUFFER_MONTHS, 0, -1)
    ]
    totals = income_and_spending_by_window(principal, windows, accounts=accounts)
    return sum(pair[1] for pair in totals) // BUFFER_MONTHS


def _buffer(principal, household, accounts, today):
    if household is not None and household.savings_buffer_minor is not None:
        return household.savings_buffer_minor, BUFFER_HOUSEHOLD
    return default_buffer_minor(principal, accounts, today), BUFFER_DEFAULT


def _plan_goals(principal, scope):
    goals = SavingsGoal.objects.visible_to(principal).filter(
        status=SavingsGoal.Status.ACTIVE, completed_at__isnull=True
    )
    if scope:
        goals = goals.filter(scope=scope)
    return list(goals.select_related("depends_on"))


def _goal_row(goal, progress, *, funded_index, months):
    check = target_check(goal.target_date, funded_index, months)
    funded_month = months[funded_index].start if funded_index not in (None, ALREADY_FUNDED) else None
    return SimpleNamespace(
        goal=goal,
        target_display=progress.target_amount_display,
        current_display=progress.current_amount_display,
        remaining_minor=progress.remaining_minor,
        remaining_display=progress.remaining_display,
        already_funded=funded_index == ALREADY_FUNDED,
        funded_month=funded_month,
        within_horizon=funded_index is not None,
        check=check,
        flagged=goal.time_sensitive and check == LATE,
        depends_on_name=goal.depends_on.name if goal.depends_on_id is not None else None,
    )


def build_funding_plan(principal, *, today, scope=SCOPE_ALL, horizon=DEFAULT_HORIZON):
    """Per-goal affordability dates plus the assumptions behind them."""
    if scope not in SCOPES:
        raise ValueError("Unknown plan scope.")
    if horizon not in HORIZONS:
        raise ValueError("Horizon must be 3, 6, 12, or 24 months.")
    person = _person(principal, check_authenticated=False)
    household = current_household(person)
    goals = [] if scope == SavingsGoal.Scope.HOUSEHOLD and household is None else _plan_goals(principal, scope)
    accounts, inputs = _scope_inputs(principal, scope)
    months = project_cash_flow(inputs, today=today, horizon=horizon)
    buffer_minor, buffer_source = _buffer(principal, household, accounts, today)
    household_only = scope == SavingsGoal.Scope.HOUSEHOLD
    ordered = order_goals(goals)
    progress_by_id = {
        goal.pk: goal_progress(principal, goal, today=today, household_only=household_only) for goal in ordered
    }
    funded, filled = fill_goals(
        [(goal.pk, progress_by_id[goal.pk].remaining_minor) for goal in ordered],
        [month.net_minor for month in months],
        buffer_minor,
    )
    rows = [
        _goal_row(goal, progress_by_id[goal.pk], funded_index=funded[goal.pk], months=months) for goal in ordered
    ]
    return SimpleNamespace(
        scope=scope,
        horizon=horizon,
        rows=rows,
        flagged=[row for row in rows if row.flagged],
        months=[
            SimpleNamespace(
                start=month.start,
                surplus_display=format_minor(fill.surplus_minor),
                held_display=format_minor(fill.held_minor),
                available_display=format_minor(fill.available_minor),
            )
            for month, fill in zip(months, filled)
        ],
        buffer_minor=buffer_minor,
        buffer_display=format_minor(buffer_minor),
        buffer_source=buffer_source,
        can_set_buffer=household is not None,
        needs_household=household is None and scope == SavingsGoal.Scope.HOUSEHOLD,
    )


def set_savings_buffer(principal, buffer_minor):
    """Set the household's safety buffer; None restores the default formula."""
    person = _person(principal, check_authenticated=False)
    if buffer_minor is not None and not 0 <= buffer_minor <= MAX_BUFFER_MINOR:
        raise ValidationError("Enter a buffer of zero or more.")
    with transaction.atomic():
        household = current_household(person)
        if household is None:
            raise PermissionDenied(_DENIED)
        lock_actor_household(person)
        household.refresh_from_db(fields=["savings_buffer_minor"])
        if household.savings_buffer_minor == buffer_minor:
            return household
        household.savings_buffer_minor = buffer_minor
        household.save(update_fields=("savings_buffer_minor", "updated_at"))
        record(
            person,
            AuditEvent.Action.RECORD_EDITED,
            AuditEvent.TargetType.SETTING,
            household.pk,
            audience={"household": household},
            fields=("buffer",),
        )
    return household
