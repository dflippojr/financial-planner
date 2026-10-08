"""Explicit append/query and narrowly scoped audit maintenance."""
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, models, transaction

from .audit_models import ACTION_SPECS, AUDIENCE_ACCOUNT, AUDIENCE_DELETION, AUDIENCE_FLEXIBLE, AUDIENCE_PERSONAL
from .models import Account, AuditEvent, Membership, _person_for


logger = logging.getLogger(__name__)
audit_request = ContextVar("audit_request", default=None)
GAP_WARNING = "The action completed, but its audit event could not be recorded. Please notify the operator."


def append_event(*, action, account=None, actor=None, actor_kind=AuditEvent.ActorKind.MEMBER,
                 effective_member=None, affected_member=None, source=AuditEvent.Source.UI,
                 outcome=AuditEvent.Outcome.SUCCEEDED, correlation_id=None, changed_fields=(),
                 household=None, target_id=None, target_type=None, private_owner=None, metadata=None,
                 verified=False):
    """Call inside the action transaction with a server-authorized target.

    Never pass submitted actor/source metadata. System callers must supply the
    effective member whose account access authorized the action. Account actions
    take `account`; household actions take `household`; personal actions are
    visible to `actor` (or `effective_member` for system callers) only.
    `affected_member` names the person acted upon when it differs from the
    initiator; it is never the operator. Workflow actions (#273) take `target_type`,
    `target_id` and exactly one audience: `account`, `private_owner` or `household`;
    `verified=True` skips the audience authorization query when the caller already
    holds a row lock taken through the actor's own visibility in this transaction.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("Audit append requires the action transaction.")
    spec = ACTION_SPECS.get(action)
    if spec is None or not isinstance(changed_fields, (list, tuple)):
        raise ValidationError("Unsupported audit action or field names.")
    spec_target_type, audience, allowed = spec
    target_type = spec_target_type or target_type
    fields = list(changed_fields)
    if any(not isinstance(field, str) or field not in allowed for field in fields):
        raise ValidationError("Unsupported audit action or field names.")
    principal = actor if actor_kind == AuditEvent.ActorKind.MEMBER else effective_member
    private_owner_id = household_id = None
    if audience == AUDIENCE_FLEXIBLE:
        if target_type not in AuditEvent.TargetType.values or not isinstance(target_id, int) or isinstance(target_id, bool):
            raise ValidationError("Unsupported audit target.")
        if (account is not None) + (private_owner is not None) + (household is not None) != 1:
            raise ValidationError("Exactly one audit audience is required.")
        if account is not None:
            allowed_audience = verified or Account.objects.visible_to(principal).filter(pk=account.pk).exists()
            if target_type == AuditEvent.TargetType.ACCOUNT:
                target_id = account.pk
        elif private_owner is not None:
            person = _person_for(principal)
            allowed_audience = verified or (person is not None and person.pk == private_owner.pk)
            private_owner_id = private_owner.pk
        else:
            person = _person_for(principal)
            allowed_audience = verified or (person is not None and Membership.objects.filter(
                person=person, household=household, ended_at__isnull=True).exists())
            household_id = household.pk
        if not allowed_audience:
            raise PermissionDenied("Operation is not permitted.")
    elif audience in (AUDIENCE_ACCOUNT, AUDIENCE_DELETION):
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
        private_owner_id=private_owner_id, household_id=household_id, metadata=metadata or {},
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


@dataclass(frozen=True)
class Origin:
    source: str
    correlation_id: uuid.UUID | None = None
    proposal_id: int | None = None
    per_row: bool = True


audit_origin = ContextVar("audit_origin", default=None)


@contextmanager
def origin(source=None, *, correlation_id=None, proposal_id=None, per_row=True):
    """Label events written inside the block (bulk edit, rule run, chat confirmation).

    ``per_row=False`` suppresses per-transaction correction events: the caller
    records one bounded aggregate event instead. Trusted code only.
    """
    current = audit_origin.get()
    if current is not None:
        source = source or current.source
        correlation_id = correlation_id or current.correlation_id
        proposal_id = proposal_id or current.proposal_id
    token = audit_origin.set(Origin(source or AuditEvent.Source.UI, correlation_id, proposal_id, per_row))
    try:
        yield
    finally:
        audit_origin.reset(token)


def record(actor, action, target_type, target_id, *, audience, fields=(), metadata=None,
           source=None, correlation_id=None, verified=False):
    """Append a workflow event for an authorized member inside the action transaction."""
    current = audit_origin.get()
    metadata = dict(metadata or {})
    if current is not None:
        source = source or current.source
        correlation_id = correlation_id or current.correlation_id
        if current.proposal_id is not None:
            metadata.setdefault("proposal_id", current.proposal_id)
    return append_event(
        actor=_person_for(actor), action=action, target_type=target_type, target_id=target_id,
        changed_fields=fields, metadata=metadata, source=source or AuditEvent.Source.UI,
        correlation_id=correlation_id, verified=verified, **audience,
    )


CORRECTION_FIELDS = {"category": "category", "refund_link": "refund"}


def record_correction(txn, actor, history):
    """One event per user-driven correction, referencing the existing history row."""
    field = CORRECTION_FIELDS.get(history.field_name)
    person = _person_for(actor) if actor is not None else None
    current = audit_origin.get()
    if field is None or person is None or (current is not None and not current.per_row):
        return
    try:
        record(person, AuditEvent.Action.TRANSACTION_CORRECTED, AuditEvent.TargetType.TRANSACTION, txn.pk,
               audience={"account": Account(pk=txn.account_id)}, fields=(field,), metadata={"history_id": history.pk})
    except PermissionDenied:
        # Internal restores can touch a counterpart leg the actor cannot see; the
        # history row remains the record and no event may name a hidden account.
        return


def record_download(principal, *, export_kind, section=None, receipt=None):
    """Note that an authorized download response was prepared (not that it was received).

    Own transaction: downloads are read-only, so only the event is written.
    """
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied("Operation is not permitted.")
    metadata = {"export_kind": export_kind}
    if section is not None:
        metadata["section"] = section
    with transaction.atomic():
        if receipt is not None:
            metadata["transaction_id"] = receipt.transaction_id
            return record(person, AuditEvent.Action.DOWNLOAD_PREPARED, AuditEvent.TargetType.RECEIPT, receipt.pk,
                          audience={"account": Account(pk=receipt.transaction.account_id)}, metadata=metadata)
        return record(person, AuditEvent.Action.DOWNLOAD_PREPARED, AuditEvent.TargetType.EXPORT, person.pk,
                      audience=personal_audience(person), metadata=metadata)


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
