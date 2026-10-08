"""Trusted execution context for delegated work; never populated from HTTP input."""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

from django.db import transaction

from .models import AuditEvent


@dataclass(frozen=True)
class Execution:
    actor_kind: str
    source: str
    run_id: uuid.UUID
    declared_operator: object = None
    job_id: int | None = None
    turn_id: int | None = None
    attempt: int | None = None


execution = ContextVar("audit_execution", default=None)


def member_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if execution.get() is not None:
            return function(*args, **kwargs)
        with operation(actor_kind=AuditEvent.ActorKind.MEMBER, source=AuditEvent.Source.UI):
            return function(*args, **kwargs)
    return wrapped


def scheduled_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if execution.get() is not None:
            return function(*args, **kwargs)
        with operation():
            return function(*args, **kwargs)
    return wrapped


def journal_run(name):
    """Operation/outcome only for operator-wide work, outside the DB audience."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            from ops.backup.audit_journal import append

            def run():
                current = execution.get()
                append(name, "started", current.run_id, actor=current.actor_kind)
                try:
                    result = function(*args, **kwargs)
                except Exception:
                    append(name, "failed", current.run_id, actor=current.actor_kind)
                    raise
                append(name, "succeeded", current.run_id, actor=current.actor_kind)
                return result

            if execution.get() is not None:
                return run()
            with operation():
                return run()
        return wrapped
    return decorate


@contextmanager
def operation(*, actor_kind=AuditEvent.ActorKind.SCHEDULER, source=AuditEvent.Source.JOB,
              run_id=None, declared_operator=None, job_id=None, turn_id=None, attempt=None):
    token = execution.set(Execution(actor_kind, source, run_id or uuid.uuid4(), declared_operator,
                                    job_id, turn_id, attempt))
    try:
        yield execution.get()
    finally:
        execution.reset(token)


def outcome(person, operation_name, target_id=None, *, phase="succeeded", metadata=None,
            effective_member=None, account=None):
    """Personal bounded outcome; caller has already authorized this member's work."""
    from .audit_services import append_event

    current = execution.get()
    details = {"operation": operation_name, **(metadata or {})}
    if current is not None:
        for key in ("job_id", "turn_id", "attempt"):
            value = getattr(current, key)
            if value is not None:
                details[key] = value
    target_type = {"simplefin_sync": "connection", "ai_inference": "connection",
                   "ai_claim": "ai_job", "ai_attempt": "ai_job", "ai_recovery": "ai_job", "ai_job": "ai_job",
                   "chat_turn": "chat_turn", "monthly_review": "review", "alert_delivery": "alert",
                   "email_delivery": "setting", "transfer_rebuild": "member"}[operation_name]
    audience = {"account": account} if account is not None else {"private_owner": person}
    with transaction.atomic():
        return append_event(
            action=AuditEvent.Action.OPERATION, target_type=target_type,
            target_id=target_id or person.pk, actor=person, **audience,
            affected_member=person, effective_member=effective_member,
            outcome=phase, metadata=details, verified=True,
        )
