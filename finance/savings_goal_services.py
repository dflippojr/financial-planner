from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Case, Value, When
from django.utils import timezone

from .access import DENIED as _DENIED
from .access import require_person as _person
from .cash_flow import format_minor
from .category_services import current_household
from .audit_services import changed_names, owned_audience, record, snapshot
from .models import Account, AuditEvent, BalanceSnapshot, SavingsGoal

SOURCE_SNAPSHOT = "snapshot"
SOURCE_MANUAL = "manual"
SOURCE_NONE = "none"


def months_left(today, target_date):
    """Calendar months from today to target_date, a partial month counting as one.

    Pinned by the issue owner so the progress math stays testable; see
    issue #77's comment for the formula and worked examples.
    """
    months = (target_date.year - today.year) * 12 + (target_date.month - today.month)
    if target_date.day >= today.day:
        months += 1
    if target_date >= today:
        months = max(months, 1)
    return months


def _current_amount(principal, goal, *, as_of):
    visible_account = None
    if goal.linked_account_id is not None:
        visible_account = Account.objects.visible_to(principal).filter(pk=goal.linked_account_id).first()
        if visible_account is not None:
            # Same precedence as Accounts and Net worth: the latest date on or
            # before as_of, and on one date a SimpleFIN snapshot outranks a
            # manual one.
            snapshot = (
                BalanceSnapshot.objects.filter(
                    account_id=goal.linked_account_id,
                    snapshot_date__lte=as_of,
                )
                .annotate(
                    _source_rank=Case(
                        When(source=BalanceSnapshot.Source.SIMPLEFIN, then=Value(1)),
                        default=Value(0),
                    )
                )
                .order_by("-snapshot_date", "-_source_rank", "-pk")
                .first()
            )
            if snapshot is not None:
                return snapshot.amount_minor, SOURCE_SNAPSHOT, snapshot.snapshot_date, visible_account
    if goal.manual_amount_minor is not None and goal.manual_amount_date is not None and goal.manual_amount_date <= as_of:
        return goal.manual_amount_minor, SOURCE_MANUAL, goal.manual_amount_date, visible_account
    return 0, SOURCE_NONE, None, visible_account


def goal_progress(principal, goal, *, today):
    """Informational progress toward a goal's target, from the viewer's perspective.

    A linked account the viewer can no longer see falls back to the manual
    amount without exposing the account's balance or name.
    """
    current_minor, source, as_of, visible_account = _current_amount(principal, goal, as_of=today)
    target_minor = goal.target_amount_minor
    reached = current_minor >= target_minor
    past_due = (not reached) and goal.target_date < today
    remaining_minor = max(target_minor - current_minor, 0)
    percent = min(100, max(0, round((current_minor / target_minor) * 100)))
    monthly_needed_minor = None
    if not reached and not past_due:
        months = months_left(today, goal.target_date)
        monthly_needed_minor = -(-remaining_minor // months)
    return SimpleNamespace(
        goal=goal,
        current_amount_minor=current_minor,
        current_amount_display=format_minor(current_minor, goal.currency),
        source=source,
        as_of=as_of,
        linked_account=visible_account,
        target_amount_minor=target_minor,
        target_amount_display=format_minor(target_minor, goal.currency),
        remaining_minor=remaining_minor,
        remaining_display=format_minor(remaining_minor, goal.currency),
        percent=percent,
        monthly_needed_minor=monthly_needed_minor,
        monthly_needed_display=(
            format_minor(monthly_needed_minor, goal.currency) if monthly_needed_minor is not None else None
        ),
        reached=reached,
        past_due=past_due,
        completed=goal.completed_at is not None,
        archived=goal.status == SavingsGoal.Status.ARCHIVED,
    )


def _check_can_edit(person, goal):
    household = current_household(person)
    if goal.scope == SavingsGoal.Scope.HOUSEHOLD:
        if household is None or goal.household_id != household.pk:
            raise PermissionDenied(_DENIED)
    elif goal.owner_id != person.pk:
        raise PermissionDenied(_DENIED)


GOAL_AUDIT_FIELDS = {
    "scope": "scope", "name": "name", "target": "target_amount_minor", "date": "target_date",
    "account": "linked_account_id", "amount": "manual_amount_minor",
}


def save_savings_goal(principal, payload, *, goal=None):
    person = _person(principal, check_authenticated=False)
    household = current_household(person)
    scope = payload["scope"]
    if scope == SavingsGoal.Scope.HOUSEHOLD:
        if household is None:
            raise ValidationError("Join a household before adding a household savings goal.")
        assigned_household = household
    else:
        assigned_household = None
    if goal is None:
        goal = SavingsGoal(owner=person)
        before = None
    else:
        _check_can_edit(person, goal)
        if goal.scope != scope and goal.owner_id != person.pk:
            raise PermissionDenied(_DENIED)
        before = snapshot(goal, GOAL_AUDIT_FIELDS)
    linked_account = payload.get("linked_account")
    if linked_account is not None and not Account.objects.visible_to(principal).filter(pk=linked_account.pk).exists():
        raise PermissionDenied(_DENIED)
    if linked_account is None and goal.linked_account_id is not None:
        # The edit form cannot offer an account the editor cannot see (for
        # example the owner's private account), so a blank choice there means
        # "unchanged", not "unlink".
        if not Account.objects.visible_to(principal).filter(pk=goal.linked_account_id).exists():
            linked_account = goal.linked_account
    goal.scope = scope
    goal.household = assigned_household
    goal.name = payload["name"]
    goal.target_amount_minor = payload["target_amount_minor"]
    goal.currency = "USD"
    goal.target_date = payload["target_date"]
    goal.linked_account = linked_account
    goal.manual_amount_minor = payload.get("manual_amount_minor")
    goal.manual_amount_date = payload.get("manual_amount_date")
    with transaction.atomic():
        goal.save()
        if before is None:
            record(person, AuditEvent.Action.RECORD_CREATED, AuditEvent.TargetType.GOAL, goal.pk,
                   audience=owned_audience(goal))
        else:
            changed = changed_names(before, snapshot(goal, GOAL_AUDIT_FIELDS))
            if changed:
                record(person, AuditEvent.Action.RECORD_EDITED, AuditEvent.TargetType.GOAL, goal.pk,
                       audience=owned_audience(goal), fields=changed)
    return goal


def set_savings_goal_completed(principal, goal, completed):
    person = _person(principal, check_authenticated=False)
    _check_can_edit(person, goal)
    was_completed = goal.completed_at is not None
    goal.completed_at = timezone.now() if completed else None
    with transaction.atomic():
        goal.save(update_fields=["completed_at", "updated_at"])
        if was_completed != bool(completed):
            record(person, AuditEvent.Action.RECORD_COMPLETED if completed else AuditEvent.Action.RECORD_RESTORED,
                   AuditEvent.TargetType.GOAL, goal.pk, audience=owned_audience(goal), fields=("status",))


def set_savings_goal_archived(principal, goal, archived):
    person = _person(principal, check_authenticated=False)
    _check_can_edit(person, goal)
    was_archived = goal.status == SavingsGoal.Status.ARCHIVED
    if archived:
        goal.status = SavingsGoal.Status.ARCHIVED
        goal.archived_at = timezone.now()
    else:
        goal.status = SavingsGoal.Status.ACTIVE
        goal.archived_at = None
    with transaction.atomic():
        goal.save(update_fields=["status", "archived_at", "updated_at"])
        if was_archived != bool(archived):
            record(person, AuditEvent.Action.RECORD_ARCHIVED if archived else AuditEvent.Action.RECORD_RESTORED,
                   AuditEvent.TargetType.GOAL, goal.pk, audience=owned_audience(goal), fields=("status",))
