"""Background AI jobs. Run from the scheduler container, never from a web request."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .ai_harness import failure_from_http, local_model_ready, model_status
from .ai_http import HarnessHttpError
from .ai_services import AiError, _token, connection_for, run_structured
from .ai_types import AUTHORIZATION_REQUIRED, LOCAL_BACKEND, PROVIDER_ERROR, UNAVAILABLE
from .models import AiJob
from .policy_services import may_use_ai

FEATURE_PROMPTS = {
    "structured": "Reply with a short confirmation that the structured request ran.",
    "category_suggestions": "Reply with a short confirmation that category suggestions ran.",
    "monthly_review": "Reply with a short confirmation that the monthly review ran.",
}


def enqueue_job(person, *, feature, input_refs=None, backend=""):
    connection_row = connection_for(person)
    chosen = backend or (connection_row.background_backend if connection_row else "")
    return AiJob.objects.create(
        member=person,
        feature=feature,
        backend=chosen,
        input_refs=input_refs or {},
        status=AiJob.Status.QUEUED,
        next_attempt_at=timezone.now(),
    )


def process_due_jobs(*, now=None):
    moment = now or timezone.now()
    cutoff = _stale_running_cutoff(moment)
    jobs = list(
        AiJob.objects.filter(
            Q(status__in=(AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL), next_attempt_at__lte=moment)
            | Q(status=AiJob.Status.RUNNING, updated_at__lte=cutoff)
        ).order_by("pk")
    )
    processed = 0
    for job in jobs:
        try:
            if _process_one(job, moment):
                processed += 1
        except Exception as exc:
            _isolate_job_failure(job, moment, exc)
    return processed


def in_quiet_window(moment=None):
    window = (getattr(settings, "AI_LOCAL_QUIET_WINDOW", "") or "").strip()
    if not window or "-" not in window:
        return False
    start_text, end_text = window.split("-", 1)
    start = _parse_hhmm(start_text)
    end = _parse_hhmm(end_text)
    if start is None or end is None:
        return False
    current = (moment or timezone.localtime()).time()
    current_minutes = current.hour * 60 + current.minute
    if start <= end:
        return start <= current_minutes < end
    return current_minutes >= start or current_minutes < end


def _stale_running_cutoff(moment):
    timeout = int(getattr(settings, "AGENT_HARNESS_SESSION_TIMEOUT_SECONDS", 600))
    margin = int(getattr(settings, "AGENT_HARNESS_STALE_JOB_MARGIN_SECONDS", 120))
    return moment - timedelta(seconds=timeout + margin)


def _lock_qs(qs):
    if not connection.features.has_select_for_update:
        return qs
    kwargs = {}
    if connection.features.has_select_for_update_skip_locked:
        kwargs["skip_locked"] = True
    return qs.select_for_update(**kwargs)


def _process_one(job, moment):
    cutoff = _stale_running_cutoff(moment)
    member_connection = connection_for(job.member)
    session_id = _session_for(job, member_connection)
    if job.harness_session_id and not session_id:
        # Saved on an earlier connection: never resume it on this one.
        _write_if_unchanged(job, harness_session_id="")
        return False
    if job.status == AiJob.Status.RUNNING and not session_id:
        return _requeue_stale_running(job, moment, cutoff)
    if not may_use_ai(job.member):
        return _fail_if_unchanged(job, UNAVAILABLE)
    if member_connection is None:
        return _fail_if_unchanged(job, UNAVAILABLE)
    backend = job.backend or member_connection.background_backend
    # A job with a session id resumes that session (it may already be done), so it
    # skips the local-model readiness gate whether it is queued or stale-running.
    resuming = bool(session_id)
    if not resuming and backend == LOCAL_BACKEND and not _local_may_run(member_connection):
        _write_if_unchanged(job, status=AiJob.Status.WAITING_MODEL, next_attempt_at=moment)
        return False
    claimed = _claim_for_run(job, moment, cutoff)
    if claimed is None:
        return False
    job = claimed
    marker = _connection_marker(member_connection)

    def remember_session(new_id):
        job.harness_session_id = new_id
        job.input_refs = {**(job.input_refs or {}), SESSION_CONNECTION_KEY: marker}
        job.save(update_fields=("harness_session_id", "input_refs", "updated_at"))

    if job.feature == "category_suggestions":
        from .category_suggestion_services import run_category_suggestion_job

        result = run_category_suggestion_job(
            job.member,
            job,
            backend=backend,
            session_id=session_id,
            on_session=remember_session,
        )
    else:
        prompt = FEATURE_PROMPTS.get(job.feature, FEATURE_PROMPTS["structured"])
        result = run_structured(
            job.member,
            prompt,
            feature=job.feature,
            backend=backend,
            session_id=session_id,
            on_session=remember_session,
        )
    if result.session_id:
        job.harness_session_id = result.session_id
        job.input_refs = {**(job.input_refs or {}), SESSION_CONNECTION_KEY: marker}
        job.save(update_fields=("harness_session_id", "input_refs", "updated_at"))
    if result.ok:
        job.status = AiJob.Status.SUCCEEDED
        job.result_ref = result.session_id or "ok"
        job.failure_code = ""
        job.finished_at = timezone.now()
        job.save(
            update_fields=(
                "status",
                "result_ref",
                "failure_code",
                "finished_at",
                "harness_session_id",
                "updated_at",
            )
        )
        return True
    if result.failure_code == AUTHORIZATION_REQUIRED:
        _fail(job, AUTHORIZATION_REQUIRED)
        return True
    if result.session_open:
        return _wait_for_open_session(job, moment, result.failure_code or UNAVAILABLE)
    job.harness_session_id = ""
    return _retry_or_fail(job, moment, result.failure_code or UNAVAILABLE)


SESSION_CONNECTION_KEY = "harness_connection"


def _connection_marker(connection):
    """Identifies one connect: a reconnect, even to the same URL, gets a new marker."""
    if connection is None:
        return ""
    return f"{connection.pk}:{connection.connected_at.isoformat() if connection.connected_at else ''}"


def _session_for(job, connection):
    """The saved session id, only if it was created on the member's current connection."""
    saved = (job.harness_session_id or "").strip()
    if not saved or connection is None:
        return ""
    if (job.input_refs or {}).get(SESSION_CONNECTION_KEY) != _connection_marker(connection):
        return ""
    return saved


def _wait_for_open_session(job, moment, code):
    """Check the open session again later without spending an attempt, up to a maximum job age."""
    max_age = int(getattr(settings, "AI_JOB_RESUME_MAX_AGE_SECONDS", 24 * 60 * 60))
    if job.created_at <= moment - timedelta(seconds=max_age):
        job.harness_session_id = ""
        _fail(job, code)
        return True
    job.status = AiJob.Status.QUEUED
    job.failure_code = code
    job.next_attempt_at = max(moment, timezone.now()) + timedelta(
        seconds=int(getattr(settings, "AI_JOB_RESUME_DELAY_SECONDS", 300))
    )
    job.save(
        update_fields=("status", "failure_code", "next_attempt_at", "harness_session_id", "updated_at")
    )
    return False


def _claim_for_run(job, moment, cutoff):
    due = Q(status__in=(AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL), next_attempt_at__lte=moment)
    stale_resume = Q(status=AiJob.Status.RUNNING, updated_at__lte=cutoff) & ~Q(harness_session_id="")
    with transaction.atomic():
        locked = _lock_qs(AiJob.objects.filter(pk=job.pk).filter(due | stale_resume)).first()
        if locked is None:
            return None
        if locked.status != AiJob.Status.RUNNING:
            locked.status = AiJob.Status.RUNNING
            # Attempts count new harness sessions; resuming an open one is not a new try.
            if not (locked.harness_session_id or "").strip():
                locked.attempts += 1
        locked.save(update_fields=("status", "attempts", "updated_at"))
        return locked


def _requeue_stale_running(job, moment, cutoff):
    with transaction.atomic():
        locked = _lock_qs(
            AiJob.objects.filter(
                pk=job.pk,
                status=AiJob.Status.RUNNING,
                harness_session_id="",
                updated_at__lte=cutoff,
            )
        ).first()
        if locked is None:
            return False
        return _retry_or_fail(locked, moment, locked.failure_code or UNAVAILABLE)


def _local_may_run(member_connection):
    try:
        statuses = model_status(member_connection.base_url, _token(member_connection))
    except HarnessHttpError as exc:
        if failure_from_http(exc) == AUTHORIZATION_REQUIRED:
            raise
        return in_quiet_window()
    if local_model_ready(statuses):
        return True
    return in_quiet_window()


def _isolate_job_failure(job, moment, exc):
    try:
        job.refresh_from_db()
    except Exception:
        return
    if job.status in (AiJob.Status.SUCCEEDED, AiJob.Status.FAILED):
        return
    if isinstance(exc, AiError):
        code = exc.failure_code or PROVIDER_ERROR
    elif isinstance(exc, HarnessHttpError):
        code = failure_from_http(exc)
    else:
        code = PROVIDER_ERROR
    if code == AUTHORIZATION_REQUIRED:
        _fail(job, AUTHORIZATION_REQUIRED)
        return
    _retry_or_fail(job, moment, code)


def _retry_or_fail(job, moment, code):
    if job.attempts == 0:
        job.attempts = 1
    if job.attempts < int(getattr(settings, "AI_JOB_MAX_ATTEMPTS", 5)):
        delay = min(2 ** job.attempts, 32) * 60
        job.status = AiJob.Status.QUEUED
        job.failure_code = code
        # Measured from now: a session wait can outlast the poll that started it.
        job.next_attempt_at = max(moment, timezone.now()) + timedelta(seconds=delay)
        job.save(
            update_fields=(
                "status",
                "attempts",
                "failure_code",
                "next_attempt_at",
                "harness_session_id",
                "updated_at",
            )
        )
        return False
    _fail(job, code)
    return True


def _write_if_unchanged(job, **fields):
    """Update a job this runner has not claimed, only if nobody changed it since it was read."""
    fields["updated_at"] = timezone.now()
    return bool(
        AiJob.objects.filter(pk=job.pk, status=job.status, updated_at=job.updated_at).update(**fields)
    )


def _fail_if_unchanged(job, code):
    return _write_if_unchanged(
        job,
        status=AiJob.Status.FAILED,
        failure_code=code,
        finished_at=timezone.now(),
    )


def _fail(job, code):
    job.status = AiJob.Status.FAILED
    job.failure_code = code
    job.finished_at = timezone.now()
    job.save(update_fields=("status", "failure_code", "finished_at", "harness_session_id", "updated_at"))


def _parse_hhmm(text):
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute
