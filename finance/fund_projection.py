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

from types import SimpleNamespace

from .access import visible_rows
from .cash_flow import format_minor
from .fund_services import suggested_set_aside
from .funds import assigned_by_goal, fund_due_date, fund_matches_account, linked_item
from .models import Account, SavingsGoal
from .months import month_end
from .projection import KIND_EXPENSE, occurrence_dates

SOURCE_SET_ASIDE = "set_aside"


def _open_funds(principal, *, scope=""):
    accounts = visible_rows(Account, principal).filter(status=Account.Status.ACTIVE, archived_at__isnull=True)
    goals = visible_rows(SavingsGoal, principal).filter(
        share_of_account=True,
        status=SavingsGoal.Status.ACTIVE,
        completed_at__isnull=True,
        linked_account__in=accounts,
    )
    if scope:
        goals = goals.filter(scope=scope)
    goals = goals.select_related("linked_account", "funds_planned_item").order_by("pk")
    return [goal for goal in goals if fund_matches_account(goal, goal.linked_account)]


def fund_schedule(goals, assigned, months):
    """Per month start: [(goal, set-aside minor)] and {planned item id: minor paid from funds}.

    `months` are the projection's month starts in order; `assigned` is each
    goal's assigned amount today. Pure: no queries beyond the goals' loaded
    planned items.
    """
    held = dict(assigned)
    schedule = {}
    for start in months:
        end = month_end(start)
        set_asides = []
        covered = {}
        for goal in goals:
            due = fund_due_date(goal, start)
            amount = suggested_set_aside(goal.target_amount_minor, held[goal.pk], due, on=start)
            if amount:
                held[goal.pk] += amount
                set_asides.append((goal, amount))
            item = linked_item(goal)
            if item is None:
                continue
            for _hit in occurrence_dates(item.start_date, item.end_date, item.cadence, start, end):
                paid = min(held[goal.pk], item.amount_minor)
                if paid > 0:
                    held[goal.pk] -= paid
                    covered[item.pk] = covered.get(item.pk, 0) + paid
        schedule[start] = SimpleNamespace(set_asides=set_asides, covered=covered)
    return schedule


def _with_overlay(month, entry):
    contributions = []
    spending = month.spending_minor
    for contribution in month.contributions:
        paid = entry.covered.get(contribution.source_id) if contribution.source == "planned" else None
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


def apply_fund_set_asides(principal, months, *, scope=""):
    """Projected months with each visible fund's set-asides added and its planned item paid from the fund."""
    if not months:
        return months
    goals = _open_funds(principal, scope=scope)
    if not goals:
        return months
    schedule = fund_schedule(goals, assigned_by_goal([goal.pk for goal in goals]), [month.start for month in months])
    return tuple(_with_overlay(month, schedule[month.start]) for month in months)
