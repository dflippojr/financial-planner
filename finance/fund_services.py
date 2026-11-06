"""Sinking fund changes (#670): assign, release, confirm a month's set-asides, undo a draw-down.

Money never moves on its own, except a draw-down when the linked planned
expense posts (`fund_draw_downs`). Every change is a `FundEntry` row, so a
fund's history is its rows. Writes that add money lock the account row first,
then the goal rows in id order, so two members confirming at once can never
assign more than the balance together.
"""

from datetime import date
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .access import visible_rows
from .access import DENIED as _DENIED
from .access import require_person as _person
from .audit_services import owned_audience, record
from .cash_flow import format_minor
from .edit_permission_services import check_can_edit
from .funds import (
    assigned_by_goal,
    assigned_minor,
    fund_due_date,
    fund_matches_account,
    latest_signed_balance,
    matching_funds,
    open_funds,
)
from .models import Account, AuditEvent, FundEntry, SavingsGoal
from .months import month_start
from .savings_goal_services import months_left

NOT_A_FUND_ERROR = "This goal does not hold a share of an account."
FUND_ACCOUNT_ERROR = "This fund's account can't take new money; edit the goal to choose its account."
ARCHIVED_ACCOUNT_ERROR = "This fund's account is archived."
NO_BALANCE_ERROR = "Record a balance for {account} before assigning money to its funds."
OVER_ASSIGN_ERROR = "That is more than {account}'s unassigned balance of {unassigned}."
POSITIVE_ERROR = "Enter an amount above zero."
OVER_RELEASE_ERROR = "That is more than the {assigned} assigned to {goal}."
ALREADY_CONFIRMED_ERROR = "{goal}'s set-aside for this month is already confirmed."
NOTHING_CHOSEN_ERROR = "Choose at least one fund to set money aside for."
NOT_DRAW_DOWN_ERROR = "Only a draw-down can be undone."
ALREADY_UNDONE_ERROR = "That draw-down was already undone."
NOT_ON_ACCOUNT_ERROR = "That fund is not in this account."


def _record(person, goal, *, metadata=None):
    record(person, AuditEvent.Action.RECORD_EDITED, AuditEvent.TargetType.GOAL, goal.pk,
           audience=owned_audience(goal), fields=("assigned",), metadata=metadata)


def account_position(account, *, today):
    """Balance, assigned and unassigned money for one account.

    `funds` lists every matching fund, archived and completed ones too, since
    they hold money until it is released. A shortfall is the amount assigned
    beyond the balance (a missing balance counts as zero), at most what the
    funds hold.
    """
    balance, as_of = latest_signed_balance(account, as_of=today)
    funds = list(matching_funds(account).select_related("funds_planned_item"))
    assigned = assigned_by_goal([goal.pk for goal in funds])
    total = sum(assigned.values())
    unassigned = (balance or 0) - total
    # Capped at what the funds hold: an overdrawn account with empty funds has nothing to reduce.
    shortfall = min(max(total - (balance or 0), 0), max(total, 0))
    rows = [
        SimpleNamespace(
            goal=goal,
            assigned_minor=assigned[goal.pk],
            assigned_display=format_minor(assigned[goal.pk], goal.currency),
            archived=goal.status == SavingsGoal.Status.ARCHIVED,
            completed=goal.completed_at is not None,
        )
        for goal in funds
    ]
    return SimpleNamespace(
        account=account,
        balance_minor=balance,
        balance_display=format_minor(balance) if balance is not None else None,
        balance_as_of=as_of,
        funds=rows,
        assigned_minor=total,
        assigned_display=format_minor(total),
        unassigned_minor=unassigned,
        unassigned_display=format_minor(unassigned),
        shortfall_minor=shortfall,
        shortfall_display=format_minor(shortfall),
    )


def visible_fund_accounts(principal, *, today):
    """The position of every visible, active account that holds at least one fund."""
    accounts = (
        visible_rows(Account, principal)
        .filter(status=Account.Status.ACTIVE, archived_at__isnull=True)
        .filter(savings_goals__share_of_account=True)
        .distinct()
        .order_by("name", "pk")
    )
    positions = []
    for account in accounts:
        position = account_position(account, today=today)
        if position.funds:
            positions.append(position)
    return positions


def account_fund_summary(principal, account, *, today):
    """The account page's fund panel, or None when the account holds no fund the viewer may see."""
    if not visible_rows(Account, principal).filter(pk=account.pk).exists():
        raise PermissionDenied(_DENIED)
    position = account_position(account, today=today)
    if not position.funds:
        return None
    return position


def _check_fund(person, goal):
    check_can_edit(person, goal)
    if not goal.share_of_account:
        raise ValidationError(NOT_A_FUND_ERROR)


def _lock_account(person, goal):
    """Lock and return the fund's account, refusing one that cannot take money."""
    if goal.linked_account_id is None:
        raise ValidationError(FUND_ACCOUNT_ERROR)
    account = visible_rows(Account, person).select_for_update().filter(pk=goal.linked_account_id).first()
    if account is None:
        raise PermissionDenied(_DENIED)
    if not fund_matches_account(goal, account):
        raise ValidationError(FUND_ACCOUNT_ERROR)
    if account.status != Account.Status.ACTIVE or account.archived_at is not None:
        raise ValidationError(ARCHIVED_ACCOUNT_ERROR)
    return account


def _lock_goals(person, goals):
    """Lock the goals in id order and re-check each with its saved state."""
    ids = sorted({goal.pk for goal in goals})
    locked = {
        goal.pk: goal
        for goal in visible_rows(SavingsGoal, person).select_for_update(of=("self",)).filter(pk__in=ids).order_by("pk")
    }
    if len(locked) != len(ids):
        raise PermissionDenied(_DENIED)
    for goal in locked.values():
        _check_fund(person, goal)
    return locked


def _check_room(position, amount_minor):
    if position.balance_minor is None:
        raise ValidationError(NO_BALANCE_ERROR.format(account=position.account.name))
    if amount_minor > position.unassigned_minor:
        raise ValidationError(
            OVER_ASSIGN_ERROR.format(
                account=position.account.name, unassigned=format_minor(max(position.unassigned_minor, 0))
            )
        )


def assign_to_fund(principal, goal, amount_minor, *, today=None):
    """Move money from the account's unassigned balance into one fund."""
    person = _person(principal, check_authenticated=False)
    today = today or timezone.localdate()
    if amount_minor <= 0:
        raise ValidationError(POSITIVE_ERROR)
    _check_fund(person, goal)
    with transaction.atomic():
        account = _lock_account(person, goal)
        goal = _lock_goals(person, [goal])[goal.pk]
        if goal.linked_account_id != account.pk:
            raise ValidationError(FUND_ACCOUNT_ERROR)
        _check_room(account_position(account, today=today), amount_minor)
        entry = FundEntry.objects.create(
            goal=goal, kind=FundEntry.Kind.ASSIGN, reason=FundEntry.Reason.MANUAL,
            amount_minor=amount_minor, entry_date=today, actor=person,
        )
        _record(person, goal)
    return entry


def _release(person, goal, amount_minor, *, reason, today):
    if amount_minor <= 0:
        raise ValidationError(POSITIVE_ERROR)
    assigned = assigned_minor(goal)
    if amount_minor > assigned:
        raise ValidationError(
            OVER_RELEASE_ERROR.format(assigned=format_minor(max(assigned, 0), goal.currency), goal=goal.name)
        )
    entry = FundEntry.objects.create(
        goal=goal, kind=FundEntry.Kind.RELEASE, reason=reason,
        amount_minor=-amount_minor, entry_date=today, actor=person,
    )
    _record(person, goal)
    return entry


def release_from_fund(principal, goal, amount_minor, *, today=None):
    """Move money out of a fund back to the account's unassigned balance."""
    person = _person(principal, check_authenticated=False)
    today = today or timezone.localdate()
    _check_fund(person, goal)
    with transaction.atomic():
        goal = _lock_goals(person, [goal])[goal.pk]
        return _release(person, goal, amount_minor, reason=FundEntry.Reason.MANUAL, today=today)


def resolve_shortfall(principal, account, reductions, *, today=None):
    """Release the chosen amounts from an account's funds after its balance fell below them.

    `reductions` is a list of (goal, amount_minor); a zero amount is skipped.
    All or nothing.
    """
    person = _person(principal, check_authenticated=False)
    today = today or timezone.localdate()
    if not visible_rows(Account, person).filter(pk=account.pk).exists():
        raise PermissionDenied(_DENIED)
    chosen = [(goal, amount) for goal, amount in reductions if amount]
    if not chosen:
        raise ValidationError("Enter an amount to take out of at least one fund.")
    on_account = set(matching_funds(account).values_list("pk", flat=True))
    with transaction.atomic():
        locked = _lock_goals(person, [goal for goal, _amount in chosen])
        entries = []
        for goal, amount in chosen:
            current = locked[goal.pk]
            if current.pk not in on_account:
                raise ValidationError(NOT_ON_ACCOUNT_ERROR)
            entries.append(_release(person, current, amount, reason=FundEntry.Reason.BALANCE_DROP, today=today))
    return entries


def _fund_order_key(row):
    goal = row.goal
    return (
        goal.priority is None,
        goal.priority or 0,
        row.due_date is None,
        row.due_date or date.min,
        goal.name.casefold(),
        goal.pk,
    )


def suggested_set_aside(target_minor, assigned, due_date, *, on):
    """(target − assigned) ÷ months until the due date, rounded up; None when nothing is due.

    Months count as `months_left` does: the current month counts, so money due
    later this month is asked for in full.
    """
    need = max(target_minor - assigned, 0)
    if need == 0 or due_date is None or due_date < on:
        return None
    months = months_left(on, due_date)
    return -(-need // months)


def monthly_set_asides(principal, *, today):
    """This month's suggested set-aside for each open fund the viewer can see.

    Each row carries the formula's amount (`suggested_minor`) and the amount
    the confirm form starts with (`default_minor`): suggestions are filled in
    fund order (priority, due date, name) and capped by what is left of each
    account's unassigned balance, so confirming the defaults never fails.
    A fund already confirmed this month shows its entry instead.
    """
    month = month_start(today)
    goals = open_funds(principal)
    assigned = assigned_by_goal([goal.pk for goal in goals])
    confirmed = {
        entry.goal.pk: entry
        for entry in FundEntry.objects.filter(goal_id__in=[goal.pk for goal in goals], month=month).select_related(
            "goal", "actor"
        )
    }
    positions = {}
    rows = []
    for goal in goals:
        if goal.linked_account_id not in positions:
            positions[goal.linked_account_id] = account_position(goal.linked_account, today=today)
        due = fund_due_date(goal, today)
        rows.append(
            SimpleNamespace(
                goal=goal,
                account=goal.linked_account,
                assigned_minor=assigned[goal.pk],
                assigned_display=format_minor(assigned[goal.pk], goal.currency),
                target_display=format_minor(goal.target_amount_minor, goal.currency),
                due_date=due,
                suggested_minor=suggested_set_aside(goal.target_amount_minor, assigned[goal.pk], due, on=today),
                confirmed=confirmed.get(goal.pk),
            )
        )
    rows.sort(key=_fund_order_key)
    room = {pk: max(position.unassigned_minor, 0) for pk, position in positions.items()}
    for row in rows:
        row.default_minor = 0
        if row.confirmed is None and row.suggested_minor:
            row.default_minor = min(row.suggested_minor, room[row.goal.linked_account_id])
            room[row.goal.linked_account_id] -= row.default_minor
        row.limited = row.confirmed is None and bool(row.suggested_minor) and row.default_minor < row.suggested_minor
        row.suggested_display = (
            format_minor(row.suggested_minor, row.goal.currency) if row.suggested_minor is not None else None
        )
        row.default_display = format_minor(row.default_minor, row.goal.currency)
    pending = [row for row in rows if row.confirmed is None and row.suggested_minor]
    return SimpleNamespace(month=month, rows=rows, pending=pending, positions=list(positions.values()))


def confirm_set_asides(principal, picks, *, today=None):
    """Record this month's set-asides for the chosen funds; all or nothing.

    `picks` is a list of (goal, amount_minor). Each fund can be confirmed once
    a month, and each account's picks together must fit its unassigned balance.
    """
    person = _person(principal, check_authenticated=False)
    today = today or timezone.localdate()
    month = month_start(today)
    if not picks:
        raise ValidationError(NOTHING_CHOSEN_ERROR)
    for goal, amount in picks:
        _check_fund(person, goal)
        if amount <= 0:
            raise ValidationError(POSITIVE_ERROR)
    with transaction.atomic():
        accounts = {}
        for goal, _amount in sorted(picks, key=lambda pick: pick[0].linked_account_id or 0):
            if goal.linked_account_id not in accounts:
                accounts[goal.linked_account_id] = _lock_account(person, goal)
        locked = _lock_goals(person, [goal for goal, _amount in picks])
        totals = {}
        for goal, amount in picks:
            current = locked[goal.pk]
            if current.linked_account_id != goal.linked_account_id:
                raise ValidationError(FUND_ACCOUNT_ERROR)
            if FundEntry.objects.filter(goal=current, month=month).exists():
                raise ValidationError(ALREADY_CONFIRMED_ERROR.format(goal=current.name))
            totals[current.linked_account_id] = totals.get(current.linked_account_id, 0) + amount
        for account_id, total in totals.items():
            _check_room(account_position(accounts[account_id], today=today), total)
        entries = []
        for goal, amount in picks:
            current = locked[goal.pk]
            entries.append(
                FundEntry.objects.create(
                    goal=current, kind=FundEntry.Kind.ASSIGN, reason=FundEntry.Reason.MONTHLY,
                    amount_minor=amount, entry_date=today, month=month, actor=person,
                )
            )
            _record(person, current)
    return entries


def undo_draw_down(principal, entry):
    """Put a draw-down's money back in its fund; the row stays so the transaction never draws again."""
    person = _person(principal, check_authenticated=False)
    with transaction.atomic():
        goal = _lock_goals(person, [entry.goal])[entry.goal_id]
        current = FundEntry.objects.select_for_update().filter(pk=entry.pk, goal=goal).first()
        if current is None:
            raise PermissionDenied(_DENIED)
        if current.kind != FundEntry.Kind.DRAW_DOWN:
            raise ValidationError(NOT_DRAW_DOWN_ERROR)
        if current.undone_at is not None:
            raise ValidationError(ALREADY_UNDONE_ERROR)
        current.undone_at = timezone.now()
        current.undone_by = person
        current.save(update_fields=("undone_at", "undone_by"))
        _record(person, goal)
    return current


def fund_history(principal, goal):
    """A fund's entries, newest first, for a viewer who can see the goal.

    A draw-down names its transaction only while the viewer can still see it.
    """
    from .models import Transaction

    if not visible_rows(SavingsGoal, principal).filter(pk=goal.pk).exists():
        raise PermissionDenied(_DENIED)
    entries = list(
        FundEntry.objects.filter(goal=goal)
        .select_related("actor", "undone_by", "transaction")
        .order_by(F("entry_date").desc(), "-pk")
    )
    visible = set(
        visible_rows(Transaction, principal)
        .filter(pk__in=[entry.transaction.pk for entry in entries if entry.transaction is not None])
        .values_list("pk", flat=True)
    )
    counted = set(FundEntry.objects.filter(goal=goal).filter(FundEntry.counted_filter()).values_list("pk", flat=True))
    rows = []
    for entry in entries:
        transaction_id = entry.transaction.pk if entry.transaction is not None else None
        is_counted = entry.pk in counted
        rows.append(
            SimpleNamespace(
                entry=entry,
                visible_transaction=entry.transaction if transaction_id in visible else None,
                counted=is_counted,
                can_undo=(
                    entry.kind == FundEntry.Kind.DRAW_DOWN and entry.amount_minor < 0
                    and entry.undone_at is None and is_counted
                ),
            )
        )
    return rows


def evaluate_fund_alerts(*, today=None):
    """Raise this month's set-aside notice and any balance-below-assigned notice.

    One set-aside alert per member and month, while they have a pending
    suggestion; its sources are the accounts those funds sit in, so a member who
    loses sight of one no longer sees it. One shortfall alert per account and
    balance date, to the account's audience. Titles carry no amounts.
    """
    from django.urls import reverse

    from .alert_services import audience_for_account, raise_alert
    from .models import Alert, Person

    today = today or timezone.localdate()
    created = []
    month = month_start(today)
    fund_owners = SavingsGoal.objects.filter(share_of_account=True, status=SavingsGoal.Status.ACTIVE)
    if not fund_owners.exists():
        return created
    for person in Person.objects.filter(user__is_active=True).order_by("pk"):
        plan = monthly_set_asides(person, today=today)
        if not plan.pending:
            continue
        sources = {row.account.pk: row.account for row in plan.pending}
        created.extend(
            raise_alert(
                [person],
                Alert.Kind.FUND,
                f"Set aside money for your funds: {month:%B %Y}",
                reverse("fund-set-asides"),
                f"fund-set-aside:{month:%Y-%m}",
                source_accounts=list(sources.values()),
            )
        )
    accounts = Account.objects.filter(
        status=Account.Status.ACTIVE, archived_at__isnull=True, savings_goals__share_of_account=True
    ).distinct().select_related("owner", "household")
    for account in accounts.order_by("pk"):
        position = account_position(account, today=today)
        if not position.funds or position.shortfall_minor <= 0:
            continue
        stamp = position.balance_as_of.isoformat() if position.balance_as_of else "none"
        created.extend(
            raise_alert(
                audience_for_account(account),
                Alert.Kind.FUND,
                f"{account.name} holds less than its funds: choose which to reduce",
                reverse("fund-shortfall", args=[account.pk]),
                f"fund-shortfall:{account.pk}:{stamp}",
                account=account,
            )
        )
    return created
