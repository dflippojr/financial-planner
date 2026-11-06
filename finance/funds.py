"""Read helpers for sinking funds (#670): a savings goal holding a share of one account.

A share goal's current amount is the sum of its counted `FundEntry` rows. An
account's unassigned money is its balance minus every matching fund's assigned
amount. A fund matches its account only while their scopes agree: a household
fund in an account of the same household, or a private fund in its owner's own
private account. A private fund therefore never sits in an account another
member can see, so no member can see or work out another member's private
funds. A fund whose account later changed scope stops counting toward that
account and takes no new money until it is moved or its scope changes.
"""

from datetime import timedelta

from django.db.models import Sum

from .access import visible_rows
from .models import Account, BalanceSnapshot, FundEntry, PlannedItem, SavingsGoal
from .net_worth import signed_balance_minor, source_rank_expression
from .projection import occurrence_dates

# Cash accounts that can be split into funds.
SHARE_ACCOUNT_TYPES = (Account.Type.CHECKING, Account.Type.SAVINGS)
# Far enough ahead to find the next occurrence of any cadence (annual is the longest).
_NEXT_OCCURRENCE_DAYS = 400


def fund_matches_account(goal, account):
    """True when a share goal may hold money in `account` (same scope, same owner or household)."""
    if account is None:
        return False
    if goal.scope != account.scope:
        return False
    if goal.scope == SavingsGoal.Scope.HOUSEHOLD:
        return goal.household_id is not None and goal.household_id == account.household_id
    return goal.owner_id == account.owner_id


def latest_signed_balance(account, *, as_of):
    """The balance Net worth shows for `account` on `as_of`, or (None, None) without a snapshot.

    Same precedence as Accounts, Net worth and goal progress: the latest date on
    or before as_of, then SimpleFIN over manual over calculated.
    """
    snapshot = (
        BalanceSnapshot.objects.filter(account_id=account.pk, snapshot_date__lte=as_of)
        .annotate(_source_rank=source_rank_expression())
        .order_by("-snapshot_date", "-_source_rank", "-pk")
        .first()
    )
    if snapshot is None:
        return None, None
    return signed_balance_minor(account.account_type, snapshot.source, snapshot.amount_minor), snapshot.snapshot_date


def assigned_minor(goal):
    """A share goal's assigned amount: the sum of its counted fund entries."""
    total = FundEntry.objects.filter(goal_id=goal.pk).filter(FundEntry.counted_filter()).aggregate(total=Sum("amount_minor"))["total"]
    return total or 0


def assigned_by_goal(goal_ids):
    """{goal id: assigned minor} for each id, 0 for a goal without entries."""
    ids = list(goal_ids)
    totals = dict.fromkeys(ids, 0)
    rows = (
        FundEntry.objects.filter(goal_id__in=ids)
        .filter(FundEntry.counted_filter())
        .values("goal_id")
        .annotate(total=Sum("amount_minor"))
        .values_list("goal_id", "total")
    )
    for goal_id, total in rows:
        totals[goal_id] = total or 0
    return totals


def matching_funds(account):
    """Every share goal that counts toward `account`, whatever its status, oldest first.

    Archived and completed funds still hold their money until it is released.
    """
    goals = SavingsGoal.objects.filter(share_of_account=True, linked_account_id=account.pk, scope=account.scope)
    if account.scope == Account.Scope.HOUSEHOLD:
        goals = goals.filter(household_id=account.household_id)
    else:
        goals = goals.filter(owner_id=account.owner_id)
    return goals.order_by("pk")


def linked_item(goal):
    """The planned expense a share goal funds, while the link is still consistent.

    The item must be an enabled expense in the goal's own scope (the same
    household, or the goal owner's private items). Anything else, for example an
    item its owner later took private, is ignored rather than shown or matched.
    """
    if not goal.share_of_account or goal.funds_planned_item_id is None:
        return None
    item = goal.funds_planned_item
    if item is None or not item.enabled or item.kind != PlannedItem.Kind.EXPENSE:
        return None
    if item.scope != goal.scope:
        return None
    if goal.scope == SavingsGoal.Scope.HOUSEHOLD:
        if item.household_id is None or item.household_id != goal.household_id:
            return None
    elif item.owner_id != goal.owner_id:
        return None
    return item


def next_occurrence(item, on_or_after):
    """The planned item's first occurrence on or after a date, or None."""
    hits = occurrence_dates(
        item.start_date,
        item.end_date,
        item.cadence,
        on_or_after,
        on_or_after + timedelta(days=_NEXT_OCCURRENCE_DAYS),
    )
    return hits[0] if hits else None


def fund_due_date(goal, today):
    """When a share goal's money is needed: the linked item's next occurrence, else its target date."""
    item = linked_item(goal)
    if item is not None:
        return next_occurrence(item, today) or goal.target_date
    return goal.target_date


def open_funds(principal, *, scope=""):
    """Active, unfinished funds the viewer can see whose account they can see and that still match it."""
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
