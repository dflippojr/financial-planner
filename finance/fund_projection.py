"""Sinking-fund set-asides in the cash-flow projection (#670, owner decision 5).

Each projected month, every open fund the viewer can see sets aside its
suggested amount, (target − assigned) ÷ months until it is due, worked out
from the fund's assigned amount carried forward month by month. Set-asides are
outflows of spendable cash. When the fund's planned item occurs, the fund pays
it (up to what it holds by then) and that part of the item no longer counts as
spending, so committed money is not counted twice.

Only the cash-flow page applies this. The funding plan, bills calendar and
chat read the projection without set-asides: the funding plan already fills
goals from the surplus, so subtracting set-asides there would count a fund twice.
"""

from datetime import timedelta
from types import SimpleNamespace

from .cash_flow import format_minor
from .fund_services import suggested_set_aside
from .funds import (
    assigned_by_goal,
    drawn_occurrences,
    fund_due_date,
    linked_item,
    occurrence_tolerance_days,
    open_funds,
)
from .months import month_end, month_start
from .projection import KIND_EXPENSE, SOURCE_PLANNED, occurrence_dates

SOURCE_SET_ASIDE = "set_aside"


def _planned_payments(month):
    """{planned item id: minor} the month's projection spends on each planned expense."""
    paid = {}
    for contribution in month.contributions:
        if contribution.source == SOURCE_PLANNED and contribution.kind == KIND_EXPENSE:
            paid[contribution.source_id] = paid.get(contribution.source_id, 0) + contribution.amount_minor
    return paid


def fund_schedule(goals, held_now, months, drawn_by_goal=None):
    """Per month start: [(goal, set-aside minor)] and {planned item id: minor paid from funds}.

    `months` are the projected months in order, `held_now` what each fund will
    hold when the projection starts. A fund pays its item what that month's
    projection spends on it (a scenario's changed amount included), up to what
    the fund holds by then. Pure: no queries beyond the goals' loaded items.
    """
    held = dict(held_now)
    schedule = {}
    for month in months:
        set_asides = []
        covered = {}
        payments = _planned_payments(month)
        for goal in goals:
            due = fund_due_date(goal, month.start, drawn=(drawn_by_goal or {}).get(goal.pk, set()))
            amount = suggested_set_aside(goal.target_amount_minor, held[goal.pk], due, on=month.start)
            if amount:
                held[goal.pk] += amount
                set_asides.append((goal, amount))
            item = linked_item(goal)
            if item is None or not payments.get(item.pk):
                continue
            paid = min(held[goal.pk], payments[item.pk])
            if paid > 0:
                held[goal.pk] -= paid
                covered[item.pk] = covered.get(item.pk, 0) + paid
        schedule[month.start] = SimpleNamespace(set_asides=set_asides, covered=covered)
    return schedule


def held_when_projection_starts(goals, assigned, drawn_by_goal, *, today):
    """Each fund's assigned amount less the payments its item still owes this month.

    The projection starts next month, so an occurrence of the linked item this
    month that has not drawn the fund down yet is expected to, up to what the
    fund holds. An occurrence too early to draw this fund (before it was
    linked, less the posting tolerance) is not.
    """
    held = dict(assigned)
    for goal in goals:
        item = linked_item(goal)
        if item is None:
            continue
        drawn = drawn_by_goal.get(goal.pk, set())
        earliest = month_start(today)
        if goal.planned_item_linked_on is not None:
            tolerance = timedelta(days=occurrence_tolerance_days(item.cadence))
            earliest = max(earliest, goal.planned_item_linked_on - tolerance)
        for hit in occurrence_dates(item.start_date, item.end_date, item.cadence, earliest, month_end(today)):
            if hit not in drawn:
                held[goal.pk] -= min(max(held[goal.pk], 0), item.amount_minor)
    return held


def _with_overlay(month, entry):
    contributions = []
    spending = month.spending_minor
    for contribution in month.contributions:
        paid = entry.covered.get(contribution.source_id) if contribution.source == SOURCE_PLANNED else None
        if paid and contribution.kind == KIND_EXPENSE:
            paid = min(paid, contribution.amount_minor)
            remaining = contribution.amount_minor - paid
            spending -= paid
            contribution = SimpleNamespace(
                **{
                    **vars(contribution),
                    "amount_minor": remaining,
                    "amount_display": format_minor(remaining),
                    "paid_from_fund_minor": paid,
                    "paid_from_fund_display": format_minor(paid),
                }
            )
        contributions.append(contribution)
    for goal, amount in entry.set_asides:
        spending += amount
        contributions.append(
            SimpleNamespace(
                source=SOURCE_SET_ASIDE,
                source_id=goal.pk,
                name=goal.name,
                kind=KIND_EXPENSE,
                cadence="monthly",
                occurrence_count=1,
                amount_minor=amount,
                amount_display=format_minor(amount, goal.currency),
            )
        )
    net = month.income_minor - spending
    return SimpleNamespace(
        **{
            **vars(month),
            "spending_minor": spending,
            "spending_display": format_minor(spending),
            "net_minor": net,
            "net_display": format_minor(net),
            "contributions": tuple(contributions),
            "set_aside_minor": sum(amount for _goal, amount in entry.set_asides),
        }
    )


def apply_fund_set_asides(principal, months, *, today, scope=""):
    """Projected months with each visible fund's set-asides added and its planned item paid from the fund."""
    if not months:
        return months
    goals = open_funds(principal, scope=scope)
    if not goals:
        return months
    drawn = {goal.pk: drawn_occurrences(goal, item) for goal in goals if (item := linked_item(goal)) is not None}
    held = held_when_projection_starts(goals, assigned_by_goal([goal.pk for goal in goals]), drawn, today=today)
    schedule = fund_schedule(goals, held, months, drawn)
    return tuple(_with_overlay(month, schedule[month.start]) for month in months)
