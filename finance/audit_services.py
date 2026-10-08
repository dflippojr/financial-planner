"""Explicit append/query and narrowly scoped audit maintenance."""
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, models, transaction

from .audit_models import ACTION_SPECS, AUDIENCE_ACCOUNT, AUDIENCE_DELETION, AUDIENCE_PERSONAL
from .models import Account, AuditEvent


logger = logging.getLogger(__name__)
audit_request = ContextVar("audit_request", default=None)
GAP_WARNING = "The action completed, but its audit event could not be recorded. Please notify the operator."


def append_event(*, action, account=None, actor=None, actor_kind=AuditEvent.ActorKind.MEMBER,
                 effective_member=None, affected_member=None, source=AuditEvent.Source.UI,
                 outcome=AuditEvent.Outcome.SUCCEEDED, correlation_id=None, changed_fields=(),
                 household=None, target_id=None):
    """Call inside the action transaction with a server-authorized target.

    Never pass submitted actor/source metadata. System callers must supply the
    effective member whose account access authorized the action. Account actions
    take `account`; household actions take `household`; personal actions are
    visible to `actor` (or `effective_member` for system callers) only.
    `affected_member` names the person acted upon when it differs from the
    initiator; it is never the operator.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("Audit append requires the action transaction.")
    spec = ACTION_SPECS.get(action)
    if spec is None or not isinstance(changed_fields, (list, tuple)):
        raise ValidationError("Unsupported audit action or field names.")
    target_type, audience, allowed = spec
    fields = list(changed_fields)
    if any(not isinstance(field, str) or field not in allowed for field in fields):
        raise ValidationError("Unsupported audit action or field names.")
    principal = actor if actor_kind == AuditEvent.ActorKind.MEMBER else effective_member
    private_owner_id = household_id = None
    if audience in (AUDIENCE_ACCOUNT, AUDIENCE_DELETION):
        if account is None or not Account.objects.visible_to(principal).filter(pk=account.pk).exists():
            raise PermissionDenied("Operation is not permitted.")
        target_id = account.pk
        if audience == AUDIENCE_DELETION:
            if account.scope == Account.Scope.PRIVATE:
                private_owner_id = account.owner_id
            else:
                household_id = account.household_id
    else:
        if account is not None or principal is None or not target_id:
            raise ValidationError("Unsupported audit target.")
        if audience == AUDIENCE_PERSONAL:
            private_owner_id = principal.pk
        else:
            if household is None:
                raise ValidationError("Unsupported audit target.")
            household_id = household.pk
    event = AuditEvent(
        account=account, target_id=target_id, target_type=target_type, action=action, outcome=outcome,
        actor=actor, actor_kind=actor_kind, effective_member=effective_member, affected_member=affected_member,
        private_owner_id=private_owner_id, household_id=household_id,
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
    _maintenance_rows().filter(affected_member=person).update(affected_member=None)


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
