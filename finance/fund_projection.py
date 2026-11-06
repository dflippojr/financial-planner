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

from django.db.models import F

from .access import visible_rows
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
from .models import FundEntry, SavingsGoal
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


def _unpaid_part(item, start, payment, drawn):
    """The month's projected payment for the item, less occurrences the fund already paid early."""
    hits = occurrence_dates(item.start_date, item.end_date, item.cadence, start, month_end(start))
    if not hits:
        return payment
    unpaid = sum(1 for hit in hits if hit not in drawn)
    return payment * unpaid // len(hits)


def _drawn_by_item(goals, drawn_by_goal, closed_draws):
    """{item id: (item, occurrence dates any fund already paid)}, open and closed funds together."""
    by_item = {}
    for goal in goals:
        item = linked_item(goal)
        if item is not None:
            _item, dates = by_item.setdefault(item.pk, (item, set()))
            dates |= (drawn_by_goal or {}).get(goal.pk, set())
    for item, dates in (closed_draws or {}).values():
        _item, known = by_item.setdefault(item.pk, (item, set()))
        known |= dates
    return by_item


def fund_schedule(goals, held_now, months, drawn_by_goal=None, closed_draws=None):
    """Per month start: set-asides [(goal, minor)], `covered` {item id: minor the fund pays} and
    `already_paid` {item id: minor of occurrences a fund paid early, which are no longer spending}.

    `months` are the projected months in order, `held_now` what each fund will
    hold when the projection starts. A fund pays its item what that month's
    projection spends on it (a scenario's changed amount included), up to what
    the fund holds by then. `closed_draws` {item id: (item, dates)} are the
    payments of funds no longer open (completed, archived, out of view of the
    set-aside list): they still mark those occurrences as paid, though those
    funds set nothing aside. Pure: no queries beyond the goals' loaded items.
    """
    held = dict(held_now)
    schedule = {}
    paid_dates = _drawn_by_item(goals, drawn_by_goal, closed_draws)
    for month in months:
        set_asides = []
        covered = {}
        already_paid = {}
        payments = _planned_payments(month)
        for item_id, (item, dates) in paid_dates.items():
            if payments.get(item_id):
                unpaid = _unpaid_part(item, month.start, payments[item_id], dates)
                if unpaid < payments[item_id]:
                    # Paid early from a fund, before the projection starts: not spending again.
                    already_paid[item_id] = payments[item_id] - unpaid
        for goal in goals:
            drawn = (drawn_by_goal or {}).get(goal.pk, set())
            due = fund_due_date(goal, month.start, drawn=drawn, overdue=False)
            amount = suggested_set_aside(goal.target_amount_minor, held[goal.pk], due, on=month.start)
            if amount:
                held[goal.pk] += amount
                set_asides.append((goal, amount))
            item = linked_item(goal)
            if item is None or not payments.get(item.pk):
                continue
            payment = payments[item.pk] - already_paid.get(item.pk, 0) - covered.get(item.pk, 0)
            paid = min(held[goal.pk], payment)
            if paid > 0:
                held[goal.pk] -= paid
                covered[item.pk] = covered.get(item.pk, 0) + paid
        schedule[month.start] = SimpleNamespace(set_asides=set_asides, covered=covered, already_paid=already_paid)
    return schedule


def held_when_projection_starts(goals, assigned, drawn_by_goal, *, today):
    """Each fund's assigned amount less the payments its item still owes this month.

    The projection starts next month, so an occurrence of the linked item this
    month, or a late one whose payment can still post (within the posting
    tolerance), that has not drawn the fund down yet is expected to, up to what
    the fund holds. An occurrence too early to draw this fund (before it was
    linked, less the posting tolerance) is not.
    """
    held = dict(assigned)
    for goal in goals:
        item = linked_item(goal)
        if item is None:
            continue
        drawn = drawn_by_goal.get(goal.pk, set())
        tolerance = timedelta(days=occurrence_tolerance_days(item.cadence))
        earliest = min(month_start(today), today - tolerance)
        if goal.planned_item_linked_on is not None:
            earliest = max(earliest, goal.planned_item_linked_on - tolerance)
        for hit in occurrence_dates(item.start_date, item.end_date, item.cadence, earliest, month_end(today)):
            if hit not in drawn:
                held[goal.pk] -= min(max(held[goal.pk], 0), item.amount_minor)
    return held


def _with_overlay(month, entry):
    contributions = []
    spending = month.spending_minor
    for contribution in month.contributions:
        planned = contribution.source == SOURCE_PLANNED and contribution.kind == KIND_EXPENSE
        paid = entry.covered.get(contribution.source_id, 0) if planned else 0
        early = entry.already_paid.get(contribution.source_id, 0) if planned else 0
        if paid or early:
            early = min(early, contribution.amount_minor)
            paid = min(paid, contribution.amount_minor - early)
            remaining = contribution.amount_minor - early - paid
            spending -= early + paid
            contribution = SimpleNamespace(
                **{
                    **vars(contribution),
                    "amount_minor": remaining,
                    "amount_display": format_minor(remaining),
                    "paid_from_fund_minor": paid,
                    "paid_from_fund_display": format_minor(paid) if paid else "",
                    "already_paid_minor": early,
                    "already_paid_display": format_minor(early) if early else "",
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


def _closed_draws(principal, open_ids, *, scope=""):
    """{item id: (item, paid dates)} for visible funds outside `open_ids` whose linked item they already paid.

    A fund completed or archived after paying an occurrence early still paid
    it, so the projection must not count that occurrence as spending again.
    """
    goals = visible_rows(SavingsGoal, principal).filter(share_of_account=True, funds_planned_item__isnull=False)
    if scope:
        goals = goals.filter(scope=scope)
    goals = goals.exclude(pk__in=open_ids)
    paid = {}
    for goal_id, occurrence in (
        FundEntry.objects.filter(
            goal__in=goals, kind=FundEntry.Kind.DRAW_DOWN, planned_item_id=F("goal__funds_planned_item_id")
        )
        .filter(FundEntry.counted_filter())
        .values_list("goal_id", "occurrence_date")
    ):
        paid.setdefault(goal_id, set()).add(occurrence)
    closed = {}
    if not paid:
        return closed
    for goal in goals.filter(pk__in=list(paid)).select_related("funds_planned_item").order_by("pk"):
        item = linked_item(goal)
        if item is not None:
            _item, known = closed.setdefault(item.pk, (item, set()))
            known |= paid[goal.pk]
    return closed


def apply_fund_set_asides(principal, months, *, today, scope=""):
    """Projected months with each visible fund's set-asides added and its planned item paid from the fund."""
    if not months:
        return months
    goals = open_funds(principal, scope=scope)
    closed = _closed_draws(principal, {goal.pk for goal in goals}, scope=scope)
    if not goals and not closed:
        return months
    drawn = {goal.pk: drawn_occurrences(goal, item) for goal in goals if (item := linked_item(goal)) is not None}
    held = held_when_projection_starts(goals, assigned_by_goal([goal.pk for goal in goals]), drawn, today=today)
    schedule = fund_schedule(goals, held, months, drawn, closed)
    return tuple(_with_overlay(month, schedule[month.start]) for month in months)
