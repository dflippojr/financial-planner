"""Explicit append/query and narrowly scoped audit maintenance."""
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, models, transaction

from .models import Account, AuditEvent


logger = logging.getLogger(__name__)
audit_request = ContextVar("audit_request", default=None)
GAP_WARNING = "The action completed, but its audit event could not be recorded. Please notify the operator."
ACTION_FIELDS = {
    AuditEvent.Action.ACCOUNT_SHARED: {"scope", "share_mode"},
    AuditEvent.Action.ACCOUNT_UNSHARED: {"scope", "share_mode"},
    AuditEvent.Action.SHARE_MODE_CHANGED: {"share_mode"},
    AuditEvent.Action.ACCOUNT_ARCHIVED: {"status"},
    AuditEvent.Action.ACCOUNT_DELETED: set(),
}


def append_event(*, account, action, actor=None, actor_kind=AuditEvent.ActorKind.MEMBER,
                 effective_member=None, source=AuditEvent.Source.UI,
                 outcome=AuditEvent.Outcome.SUCCEEDED, correlation_id=None, changed_fields=()):
    """Call inside the action transaction with a server-authorized target.

    Never pass submitted actor/source metadata. System callers must supply the
    effective member whose account access authorized the action.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("Audit append requires the action transaction.")
    principal = actor if actor_kind == AuditEvent.ActorKind.MEMBER else effective_member
    if not Account.objects.visible_to(principal).filter(pk=account.pk).exists():
        raise PermissionDenied("Operation is not permitted.")
    if not isinstance(changed_fields, (list, tuple)):
        raise ValidationError("Unsupported audit field names.")
    fields = list(changed_fields)
    if action not in ACTION_FIELDS or any(not isinstance(field, str) or field not in ACTION_FIELDS[action] for field in fields):
        raise ValidationError("Unsupported audit action or field names.")
    event = AuditEvent(
        account=account, target_id=account.pk, action=action, outcome=outcome,
        actor=actor, actor_kind=actor_kind, effective_member=effective_member,
        private_owner_id=account.owner_id if action == AuditEvent.Action.ACCOUNT_DELETED and account.scope == Account.Scope.PRIVATE else None,
        household_id=account.household_id if action == AuditEvent.Action.ACCOUNT_DELETED and account.scope == Account.Scope.HOUSEHOLD else None,
        source=source, correlation_id=correlation_id or uuid.uuid4(), changed_fields=sorted(fields),
    )
    try:
        with transaction.atomic():
            event.save(force_insert=True)
    except DatabaseError:
        # Never log exception text: database errors may echo bound parameters.
        logger.error("Audit write gap: an event could not be recorded")
        request = audit_request.get()
        if request is not None:
            messages.warning(request, GAP_WARNING)
        return None
    return event


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
