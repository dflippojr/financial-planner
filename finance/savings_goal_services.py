from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .access import DENIED as _DENIED
from .access import require_person as _person
from .access import visible_rows
from .cash_flow import format_minor
from .category_services import current_household
from .audit_services import changed_names, owned_audience, record, snapshot
from .edit_permission_services import check_can_edit
from .models import Account, AuditEvent, BalanceSnapshot, PlannedItem, SavingsGoal
from .net_worth import signed_balance_minor, source_rank_expression

SOURCE_SNAPSHOT = "snapshot"
SOURCE_MANUAL = "manual"
SOURCE_NONE = "none"
# A sinking fund (#670): the goal's assigned share of its account.
SOURCE_FUND = "fund"


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


def _fund_amount(principal, goal, *, household_only):
    """A share goal's assigned amount; the account is named only when the viewer can see it."""
    from .funds import assigned_minor

    accounts = visible_rows(Account, principal)
    if household_only:
        accounts = accounts.filter(scope=Account.Scope.HOUSEHOLD)
    visible_account = None
    if goal.linked_account_id is not None:
        visible_account = accounts.filter(pk=goal.linked_account_id).first()
    return assigned_minor(goal), SOURCE_FUND, None, visible_account


def _current_amount(principal, goal, *, as_of, household_only=False):
    if goal.share_of_account:
        return _fund_amount(principal, goal, household_only=household_only)
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
            # manual one, and both outrank a calculated one.
            snapshot = (
                BalanceSnapshot.objects.filter(
                    account_id=goal.linked_account_id,
                    snapshot_date__lte=as_of,
                )
                .annotate(_source_rank=source_rank_expression())
                .order_by("-snapshot_date", "-_source_rank", "-pk")
                .first()
            )
            if snapshot is not None:
                # The balance Net worth shows: an amount owed is negative
                # whatever its source, so a debt never counts as money saved.
                current = signed_balance_minor(visible_account.account_type, snapshot.source, snapshot.amount_minor)
                return current, SOURCE_SNAPSHOT, snapshot.snapshot_date, visible_account
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
    due_date = goal.target_date
    funded_item = None
    if goal.share_of_account:
        from .funds import fund_due_date, linked_item

        # A fund for a planned item is due at the item's next occurrence.
        due_date = fund_due_date(goal, today)
        funded_item = linked_item(goal)
    dated = due_date is not None
    past_due = (not reached) and dated and due_date < today
    remaining_minor = max(target_minor - current_minor, 0)
    # Exact integer math: an unreached goal never rounds up to 100%.
    percent = 100 if reached else max(0, min(99, current_minor * 100 // target_minor))
    monthly_needed_minor = None
    if not reached and not past_due and dated:
        months = months_left(today, due_date)
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
        share_of_account=goal.share_of_account,
        due_date=due_date,
        funded_item=funded_item,
    )


GOAL_AUDIT_FIELDS = {
    "scope": "scope", "name": "name", "target": "target_amount_minor", "date": "target_date",
    "account": "linked_account_id", "amount": "manual_amount_minor", "priority": "priority",
    "dependency": "depends_on_id", "time_sensitive": "time_sensitive",
    "fund": "share_of_account", "planned_item": "funds_planned_item_id",
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


FUND_ACCOUNT_REQUIRED = "Choose the checking or savings account this fund holds a share of."
FUND_ACCOUNT_TYPE_ERROR = "A fund can only hold a share of a checking or savings account."
FUND_SCOPE_ERROR = (
    "A household fund needs a household account, and a private fund one of your own private accounts."
)
FUND_MOVE_ERROR = "Release this fund's assigned money before moving it to another account or scope."
FUND_OFF_ERROR = "Release this fund's assigned money before it stops holding a share of the account."
PLANNED_ITEM_SHARE_ERROR = "Only a goal that holds a share of an account can fund a planned item."
PLANNED_ITEM_KIND_ERROR = "Choose a planned expense to fund."
PLANNED_ITEM_SCOPE_ERROR = "A goal can only fund a planned item in the same scope (private or household)."
PLANNED_ITEM_TAKEN_ERROR = "Another goal already funds that planned item."
TARGET_REQUIRED_ERROR = "Enter a target amount, or choose a planned item to take it from."


def _planned_item_in_scope(person, item, *, scope, household):
    if scope == SavingsGoal.Scope.HOUSEHOLD:
        return item.scope == PlannedItem.Scope.HOUSEHOLD and household is not None and item.household_id == household.pk
    return item.scope == PlannedItem.Scope.PRIVATE and item.owner_id == person.pk


def validate_planned_item_link(person, goal, item, *, scope, household, share):
    """Reject a planned item the editor cannot see, of another scope, or already funded."""
    if item is None:
        return
    if not visible_rows(PlannedItem, person).filter(pk=item.pk).exists():
        raise PermissionDenied(_DENIED)
    if not share:
        raise ValidationError(PLANNED_ITEM_SHARE_ERROR)
    if item.kind != PlannedItem.Kind.EXPENSE:
        raise ValidationError(PLANNED_ITEM_KIND_ERROR)
    if not _planned_item_in_scope(person, item, scope=scope, household=household):
        raise ValidationError(PLANNED_ITEM_SCOPE_ERROR)
    taken = SavingsGoal.objects.filter(funds_planned_item_id=item.pk)
    if goal.pk is not None:
        taken = taken.exclude(pk=goal.pk)
    if taken.exists():
        raise ValidationError(PLANNED_ITEM_TAKEN_ERROR)


def validate_fund(goal, *, share, account, scope, household, owner_id):
    """Check a share goal's account against its scope, and protect money already assigned.

    `goal` holds the saved state (pk None for a new goal); the keyword
    arguments are the state being saved.
    """
    from .funds import SHARE_ACCOUNT_TYPES, assigned_minor, fund_matches_account

    assigned = assigned_minor(goal) if goal.pk is not None and goal.share_of_account else 0
    if not share:
        if assigned:
            raise ValidationError(FUND_OFF_ERROR)
        return
    if account is None:
        raise ValidationError(FUND_ACCOUNT_REQUIRED)
    if account.account_type not in SHARE_ACCOUNT_TYPES:
        raise ValidationError(FUND_ACCOUNT_TYPE_ERROR)
    candidate = SavingsGoal(
        scope=scope, household_id=household.pk if household is not None else None, owner_id=owner_id
    )
    if not fund_matches_account(candidate, account):
        raise ValidationError(FUND_SCOPE_ERROR)
    moved = goal.pk is not None and (
        goal.linked_account_id != account.pk or goal.scope != scope
    )
    if assigned and moved:
        raise ValidationError(FUND_MOVE_ERROR)


def _apply_payload(person, goal, payload):
    """Set the fields a payload carries; a key the payload omits stays as it is."""
    if "target_amount_minor" in payload and payload["target_amount_minor"] is not None:
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
    if "share_of_account" in payload:
        goal.share_of_account = bool(payload["share_of_account"])
    if goal.share_of_account:
        # A fund's amount comes from its entries, never a typed figure.
        goal.manual_amount_minor = None
        goal.manual_amount_date = None


def _apply_planned_item(goal, payload, *, today):
    """Link or unlink the funded planned item; a new link fills a blank target and date from it."""
    from .funds import next_occurrence

    if not goal.share_of_account:
        goal.funds_planned_item = None
        goal.planned_item_linked_on = None
        return
    if "funds_planned_item" not in payload:
        return
    item = payload["funds_planned_item"]
    if item is None:
        goal.funds_planned_item = None
        goal.planned_item_linked_on = None
        return
    newly_linked = goal.funds_planned_item_id != item.pk
    if newly_linked:
        goal.funds_planned_item = item
        goal.planned_item_linked_on = today
    # A blank target takes the item's amount; a payload that leaves the target
    # out keeps it, unless this is a new goal that has none yet.
    blank_target = "target_amount_minor" in payload and payload["target_amount_minor"] is None
    if blank_target or (goal.pk is None and "target_amount_minor" not in payload):
        goal.target_amount_minor = item.amount_minor
    if "target_date" in payload and payload["target_date"] is None:
        goal.target_date = next_occurrence(item, today)


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
        check_can_edit(person, goal)
        if goal.scope != scope and goal.owner_id != person.pk:
            raise PermissionDenied(_DENIED)
        _check_dependents_keep_scope(goal, scope)
        before = snapshot(goal, GOAL_AUDIT_FIELDS)
    was_household = goal.pk is not None and goal.scope == SavingsGoal.Scope.HOUSEHOLD
    dependency = payload["depends_on"] if "depends_on" in payload else goal.depends_on
    validate_dependency(person, goal, dependency, scope=scope, household=assigned_household)
    share = bool(payload["share_of_account"]) if "share_of_account" in payload else goal.share_of_account
    item = payload["funds_planned_item"] if "funds_planned_item" in payload else goal.funds_planned_item
    account = None
    if share or goal.share_of_account:
        account = (
            _linked_account(person, goal, payload["linked_account"])
            if "linked_account" in payload
            else goal.linked_account
        )
    with transaction.atomic():
        if share or goal.share_of_account:
            # Check the fund's money against its saved state under the locks
            # assignments take, so no assignment can land between this check
            # and the save that moves or ends the fund.
            saved = _lock_fund_rows(goal, account) if goal.pk is not None else goal
            validate_fund(
                saved, share=share, account=account, scope=scope, household=assigned_household,
                owner_id=goal.owner_id if goal.owner_id is not None else person.pk,  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
            )
        if share and item is not None and (
            "funds_planned_item" in payload or goal.scope != scope
        ):
            validate_planned_item_link(person, goal, item, scope=scope, household=assigned_household, share=share)
        if goal.pk is None and payload.get("target_amount_minor") is None and (not share or item is None):
            raise ValidationError(TARGET_REQUIRED_ERROR)
        _apply_payload(person, goal, payload)
        _apply_planned_item(goal, payload, today=timezone.localdate())
        goal.scope = scope
        goal.household = assigned_household
        goal.name = payload["name"] if "name" in payload else goal.name
        goal.currency = "USD"
        goal.depends_on = dependency
        goal.save()
        if before is None:
            record(person, AuditEvent.Action.RECORD_CREATED, AuditEvent.TargetType.GOAL, goal.pk,
                   audience=owned_audience(goal))
        else:
            changed = changed_names(before, snapshot(goal, GOAL_AUDIT_FIELDS))
            if changed:
                record(person, AuditEvent.Action.RECORD_EDITED, AuditEvent.TargetType.GOAL, goal.pk,
                       audience=owned_audience(goal), fields=changed)
        if was_household and goal.scope != SavingsGoal.Scope.HOUSEHOLD:
            from .chat_services import delete_conversations_without_access

            delete_conversations_without_access(goal_ids=(goal.pk,))
    return goal


def _lock_fund_rows(goal, account):
    """Lock a saved goal's old and new accounts in id order, then the goal; return the goal as saved.

    The same order as an assignment (account, then goal), so the two never deadlock.
    """
    saved_account_id = SavingsGoal.objects.filter(pk=goal.pk).values_list("linked_account_id", flat=True).first()
    ids = sorted({pk for pk in (saved_account_id, account.pk if account is not None else None) if pk is not None})
    list(Account.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk", flat=True))
    saved = SavingsGoal.objects.select_for_update(of=("self",)).get(pk=goal.pk)
    if saved.linked_account_id is not None and saved.linked_account_id not in ids:  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
        raise ValidationError(FUND_MOVE_ERROR)
    return saved


def set_savings_goal_completed(principal, goal, completed):
    person = _person(principal, check_authenticated=False)
    check_can_edit(person, goal)
    was_completed = goal.completed_at is not None
    goal.completed_at = timezone.now() if completed else None
    with transaction.atomic():
        goal.save(update_fields=["completed_at", "updated_at"])
        if was_completed != bool(completed):
            record(person, AuditEvent.Action.RECORD_COMPLETED if completed else AuditEvent.Action.RECORD_RESTORED,
                   AuditEvent.TargetType.GOAL, goal.pk, audience=owned_audience(goal), fields=("status",))


def set_savings_goal_archived(principal, goal, archived):
    person = _person(principal, check_authenticated=False)
    check_can_edit(person, goal)
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
