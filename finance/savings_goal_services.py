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


def _current_amount(principal, goal, *, as_of, household_only=False):
    visible_account = None
    if goal.linked_account_id is not None:
        accounts = Account.objects.visible_to(principal)
        if household_only:
            # A shared plan must come out the same for every member, so it
            # ignores a balance held in the goal owner's private account.
            accounts = accounts.filter(scope=Account.Scope.HOUSEHOLD)
        visible_account = accounts.filter(pk=goal.linked_account_id).first()
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


def goal_progress(principal, goal, *, today, household_only=False):
    """Informational progress toward a goal's target, from the viewer's perspective.

    A linked account the viewer can no longer see falls back to the manual
    amount without exposing the account's balance or name.
    """
    current_minor, source, as_of, visible_account = _current_amount(
        principal, goal, as_of=today, household_only=household_only
    )
    target_minor = goal.target_amount_minor
    reached = current_minor >= target_minor
    dated = goal.target_date is not None
    past_due = (not reached) and dated and goal.target_date < today
    remaining_minor = max(target_minor - current_minor, 0)
    percent = min(100, max(0, round((current_minor / target_minor) * 100)))
    monthly_needed_minor = None
    if not reached and not past_due and dated:
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
    "account": "linked_account_id", "amount": "manual_amount_minor", "priority": "priority",
    "dependency": "depends_on_id", "time_sensitive": "time_sensitive",
}
DEPENDENCY_SCOPE_ERROR = "A goal can only depend on a goal in the same scope (private or household)."
DEPENDENCY_CYCLE_ERROR = "That dependency would make the goals wait on each other."
DEPENDENTS_SCOPE_ERROR = "Other goals depend on this one; change or clear their dependency before changing its scope."


def _goal_household(person, scope):
    if scope != SavingsGoal.Scope.HOUSEHOLD:
        return None
    household = current_household(person)
    if household is None:
        raise ValidationError("Join a household before adding a household savings goal.")
    return household


def _chain_reaches(start, goal_pk):
    """True when following depends_on links from `start` arrives at `goal_pk`.

    Reads each link from the database, because callers may hold copies of goals
    whose links an earlier save in the same transaction has already changed.
    """
    seen = set()
    current_pk = start.pk
    while current_pk is not None and current_pk not in seen:
        if current_pk == goal_pk:
            return True
        seen.add(current_pk)
        current_pk = SavingsGoal.objects.filter(pk=current_pk).values_list("depends_on_id", flat=True).first()
    return False


def validate_dependency(person, goal, dependency, *, scope, household):
    """Reject a dependency the editor cannot see, in another scope, or that loops.

    A dependency stays within the goal's own scope so a household goal can never
    point at a private goal the other members cannot see.
    """
    if dependency is None:
        return
    if not SavingsGoal.objects.visible_to(person).filter(pk=dependency.pk).exists():
        raise PermissionDenied(_DENIED)
    if goal.pk is not None and dependency.pk == goal.pk:
        raise ValidationError("A goal cannot depend on itself.")
    if scope == SavingsGoal.Scope.HOUSEHOLD:
        same_scope = dependency.scope == scope and dependency.household_id == household.pk
    else:
        same_scope = dependency.scope == scope and dependency.owner_id == person.pk
    if not same_scope:
        raise ValidationError(DEPENDENCY_SCOPE_ERROR)
    if goal.pk is not None and _chain_reaches(dependency, goal.pk):
        raise ValidationError(DEPENDENCY_CYCLE_ERROR)


def _check_dependents_keep_scope(goal, scope):
    if goal.pk is not None and goal.scope != scope and goal.dependents.exclude(scope=scope).exists():
        raise ValidationError(DEPENDENTS_SCOPE_ERROR)


def _apply_payload(person, goal, payload):
    """Set the fields a payload carries; a key the payload omits stays as it is."""
    if "target_amount_minor" in payload:
        goal.target_amount_minor = payload["target_amount_minor"]
    if "target_date" in payload:
        goal.target_date = payload["target_date"]
    if "priority" in payload:
        goal.priority = payload["priority"]
    if "time_sensitive" in payload:
        goal.time_sensitive = payload["time_sensitive"]
    if "manual_amount_minor" in payload:
        goal.manual_amount_minor = payload["manual_amount_minor"]
        goal.manual_amount_date = payload.get("manual_amount_date")
    if "linked_account" in payload:
        goal.linked_account = _linked_account(person, goal, payload["linked_account"])


def _linked_account(person, goal, linked_account):
    if linked_account is not None and not Account.objects.visible_to(person).filter(pk=linked_account.pk).exists():
        raise PermissionDenied(_DENIED)
    if linked_account is None and goal.linked_account_id is not None:
        # The edit form cannot offer an account the editor cannot see (for
        # example the owner's private account), so a blank choice there means
        # "unchanged", not "unlink".
        if not Account.objects.visible_to(person).filter(pk=goal.linked_account_id).exists():
            return goal.linked_account
    return linked_account


def save_savings_goal(principal, payload, *, goal=None):
    """Create or edit a goal. Optional payload keys that are absent are left unchanged."""
    person = _person(principal, check_authenticated=False)
    scope = payload["scope"]
    assigned_household = _goal_household(person, scope)
    if goal is None:
        goal = SavingsGoal(owner=person)
        before = None
    else:
        _check_can_edit(person, goal)
        if goal.scope != scope and goal.owner_id != person.pk:
            raise PermissionDenied(_DENIED)
        _check_dependents_keep_scope(goal, scope)
        before = snapshot(goal, GOAL_AUDIT_FIELDS)
    dependency = payload["depends_on"] if "depends_on" in payload else goal.depends_on
    validate_dependency(person, goal, dependency, scope=scope, household=assigned_household)
    _apply_payload(person, goal, payload)
    goal.scope = scope
    goal.household = assigned_household
    goal.name = payload["name"] if "name" in payload else goal.name
    goal.currency = "USD"
    goal.depends_on = dependency
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
