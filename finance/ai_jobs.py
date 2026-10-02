"""Background AI jobs. Run from the scheduler container, never from a web request."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .ai_harness import local_model_ready, model_status
from .ai_http import HarnessHttpError
from .ai_services import _token, connection_for, run_structured
from .ai_types import LOCAL_BACKEND, UNAVAILABLE
from .models import AiJob
from .policy_services import may_use_ai

FEATURE_PROMPTS = {
    "structured": "Reply with a short confirmation that the structured request ran.",
    "category_suggestions": "Reply with a short confirmation that category suggestions ran.",
    "monthly_review": "Reply with a short confirmation that the monthly review ran.",
}


def enqueue_job(person, *, feature, input_refs=None, backend=""):
    connection = connection_for(person)
    chosen = backend or (connection.background_backend if connection else "")
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
    jobs = list(
        AiJob.objects.filter(
            status__in=(AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL),
            next_attempt_at__lte=moment,
        ).order_by("pk")
    )
    processed = 0
    for job in jobs:
        if _process_one(job, moment):
            processed += 1
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


def _process_one(job, moment):
    if not may_use_ai(job.member):
        _fail(job, UNAVAILABLE)
        return True
    connection = connection_for(job.member)
    if connection is None:
        _fail(job, UNAVAILABLE)
        return True
    backend = job.backend or connection.background_backend
    if backend == LOCAL_BACKEND and not _local_may_run(connection):
        job.status = AiJob.Status.WAITING_MODEL
        job.next_attempt_at = moment
        job.save(update_fields=("status", "next_attempt_at", "updated_at"))
        return False
    job.status = AiJob.Status.RUNNING
    job.attempts += 1
    job.save(update_fields=("status", "attempts", "updated_at"))
    if job.feature == "category_suggestions":
        from .category_suggestion_services import run_category_suggestion_job

        result = run_category_suggestion_job(job.member, job, backend=backend)
    else:
        prompt = FEATURE_PROMPTS.get(job.feature, FEATURE_PROMPTS["structured"])
        result = run_structured(job.member, prompt, feature=job.feature, backend=backend)
    if result.ok:
        job.status = AiJob.Status.SUCCEEDED
        job.result_ref = result.session_id or "ok"
        job.failure_code = ""
        job.finished_at = timezone.now()
        job.save(update_fields=("status", "result_ref", "failure_code", "finished_at", "updated_at"))
        return True
    if job.attempts < int(getattr(settings, "AI_JOB_MAX_ATTEMPTS", 5)):
        delay = min(2 ** job.attempts, 32) * 60
        job.status = AiJob.Status.QUEUED
        job.failure_code = result.failure_code or UNAVAILABLE
        job.next_attempt_at = moment + timedelta(seconds=delay)
        job.save(update_fields=("status", "failure_code", "next_attempt_at", "updated_at"))
        return False
    _fail(job, result.failure_code or UNAVAILABLE)
    return True


def _local_may_run(connection):
    try:
        statuses = model_status(connection.base_url, _token(connection))
    except HarnessHttpError:
        return in_quiet_window()
    if local_model_ready(statuses):
        return True
    return in_quiet_window()


def _fail(job, code):
    job.status = AiJob.Status.FAILED
    job.failure_code = code
    job.finished_at = timezone.now()
    job.save(update_fields=("status", "failure_code", "finished_at", "updated_at"))


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
