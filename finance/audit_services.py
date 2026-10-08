"""Explicit append/query and narrowly scoped audit maintenance."""
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, models, transaction

from .audit_models import CHANGED_FIELDS
from .models import Account, AuditEvent, Membership, _person_for


logger = logging.getLogger(__name__)
audit_request = ContextVar("audit_request", default=None)
GAP_WARNING = "The action completed, but its audit event could not be recorded. Please notify the operator."
ACCOUNT_ACTION_FIELDS = {
    AuditEvent.Action.ACCOUNT_SHARED: {"scope", "share_mode"},
    AuditEvent.Action.ACCOUNT_UNSHARED: {"scope", "share_mode"},
    AuditEvent.Action.SHARE_MODE_CHANGED: {"share_mode"},
    AuditEvent.Action.ACCOUNT_ARCHIVED: {"status"},
    AuditEvent.Action.ACCOUNT_DELETED: set(),
}
# Workflow actions accept any allow-listed field name; metadata keys are validated
# by the model. Account lifecycle actions keep their narrower per-action lists.
ACTION_FIELDS = {action: CHANGED_FIELDS for action in AuditEvent.Action.values}
ACTION_FIELDS.update({str(action): fields for action, fields in ACCOUNT_ACTION_FIELDS.items()})


def append_event(*, account=None, action, actor=None, actor_kind=AuditEvent.ActorKind.MEMBER,
                 effective_member=None, source=AuditEvent.Source.UI,
                 outcome=AuditEvent.Outcome.SUCCEEDED, correlation_id=None, changed_fields=(),
                 target_type=AuditEvent.TargetType.ACCOUNT, target_id=None, private_owner=None,
                 household=None, metadata=None):
    """Call inside the action transaction with a server-authorized target.

    Never pass submitted actor/source metadata. System callers must supply the
    effective member whose account access authorized the action. The audience is
    exactly one of: the target's account, a private owner, or a household. Workflow
    targets that belong to an account use that account so access loss revokes
    history with it.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("Audit append requires the action transaction.")
    principal = actor if actor_kind == AuditEvent.ActorKind.MEMBER else effective_member
    if (account is not None) + (private_owner is not None) + (household is not None) != 1:
        raise ValidationError("Exactly one audit audience is required.")
    if account is not None:
        allowed = Account.objects.visible_to(principal).filter(pk=account.pk).exists()
        if target_type == AuditEvent.TargetType.ACCOUNT and target_id is None:
            target_id = account.pk
    elif private_owner is not None:
        person = _person_for(principal)
        allowed = person is not None and person.pk == private_owner.pk
    else:
        person = _person_for(principal)
        allowed = person is not None and Membership.objects.filter(
            person=person, household=household, ended_at__isnull=True).exists()
    if not allowed:
        raise PermissionDenied("Operation is not permitted.")
    if not isinstance(changed_fields, (list, tuple)):
        raise ValidationError("Unsupported audit field names.")
    fields = list(changed_fields)
    if action not in ACTION_FIELDS or any(not isinstance(field, str) or field not in ACTION_FIELDS[action] for field in fields):
        raise ValidationError("Unsupported audit action or field names.")
    if not isinstance(target_id, int) or isinstance(target_id, bool):
        raise ValidationError("Unsupported audit target.")
    event = AuditEvent(
        account=account, target_type=target_type, target_id=target_id, action=action, outcome=outcome,
        household_id=(account.household_id if action == AuditEvent.Action.ACCOUNT_DELETED and account.scope == Account.Scope.HOUSEHOLD
                      else household.pk if household is not None else None),
        actor=actor, actor_kind=actor_kind, effective_member=effective_member,
        private_owner_id=(account.owner_id if action == AuditEvent.Action.ACCOUNT_DELETED and account.scope == Account.Scope.PRIVATE
                          else private_owner.pk if private_owner is not None else None),
        metadata=metadata or {},
        source=source, correlation_id=correlation_id or uuid.uuid4(), changed_fields=sorted(fields),
    )
    try:
        with transaction.atomic():
            event.save(force_insert=True)
    except DatabaseError:
        request = audit_request.get()

        def report_gap():
            # Report only committed actions. A later action rollback discards
            # this callback, so it cannot falsely warn that the action completed.
            # Never log exception text: errors may echo bound parameters.
            logger.error("Audit write gap: an event could not be recorded")
            if request is not None:
                messages.warning(request, GAP_WARNING)

        transaction.on_commit(report_gap)
        return None
    return event


def owned_audience(obj):
    """Audience for a private/household-scoped row with owner, scope and household."""
    if obj.scope == "household":
        return {"household": obj.household}
    return {"private_owner": obj.owner}


def personal_audience(person):
    return {"private_owner": person}


def snapshot(obj, fields):
    """Current values of {audit field name: attribute} for later diffing; never stored."""
    return {name: getattr(obj, attr) for name, attr in fields.items()}


def changed_names(before, after):
    return sorted(name for name, value in after.items() if before.get(name) != value)


def record(actor, action, target_type, target_id, *, audience, fields=(), metadata=None,
           source=AuditEvent.Source.UI, correlation_id=None):
    """Append a workflow event for an authorized member inside the action transaction."""
    return append_event(
        actor=_person_for(actor), action=action, target_type=target_type, target_id=target_id,
        changed_fields=fields, metadata=metadata, source=source, correlation_id=correlation_id, **audience,
    )


def events_for(principal, *, action="", actor="", source="", date_from=None, date_to=None):
    rows = AuditEvent.objects.visible_to(principal)
    for field, value, choices in (
        ("action", action, AuditEvent.Action.values),
        ("source", source, AuditEvent.Source.values),
    ):
        if value:
            rows = rows.filter(**{field: value}) if value in choices else rows.none()
    if actor:
        try:
            rows = rows.filter(actor_id=int(actor))
        except (ValueError, TypeError):
            return rows.none()
    if date_from:
        rows = rows.filter(occurred_at__gte=datetime.combine(date_from, time.min, datetime_timezone.utc))
    if date_to:
        rows = rows.filter(occurred_at__lt=datetime.combine(date_to + timedelta(days=1), time.min, datetime_timezone.utc))
    return rows.order_by("-occurred_at", "-id")


def _maintenance_rows():
    # Bypass append-only guards only in these explicit deletion-policy helpers.
    return models.QuerySet(model=AuditEvent)


def prepare_account_deletion(account):
    """Only metadata-only deletion events survive the target's hard deletion."""
    _maintenance_rows().filter(account=account).exclude(action=AuditEvent.Action.ACCOUNT_DELETED).delete()


def cleanup_member_events(person):
    private_accounts = Account.objects.filter(owner=person, scope=Account.Scope.PRIVATE).values("pk")
    _maintenance_rows().filter(account_id__in=private_accounts).delete()
    _maintenance_rows().filter(account__isnull=True, private_owner=person).delete()
    _maintenance_rows().filter(actor=person).update(actor=None)
    _maintenance_rows().filter(effective_member=person).update(effective_member=None)


def purge_old_events(*, now=None):
    from django.utils import timezone

    cutoff = (now or timezone.now()) - timedelta(days=settings.AUDIT_RETENTION_DAYS)
    ids = list(AuditEvent.objects.filter(occurred_at__lt=cutoff).order_by("occurred_at", "id")
               .values_list("pk", flat=True)[:settings.AUDIT_PURGE_BATCH_SIZE])
    deleted, _detail = _maintenance_rows().filter(pk__in=ids).delete()
    return deleted


class AuditWarningMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = audit_request.set(request)
        try:
            return self.get_response(request)
        finally:
            audit_request.reset(token)
