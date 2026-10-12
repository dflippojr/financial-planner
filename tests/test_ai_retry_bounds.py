"""Failure budgets cover work before claim and saved-session exception recovery."""

import uuid
from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from django.db import DatabaseError
from django.utils import timezone

from finance import ai_harness, ai_jobs
from finance.ai_http import HarnessHttpError
from finance.ai_types import PROVIDER_ERROR, ModelStatus, ProviderResult
from finance.encryption import encrypt_secret
from finance.models import AiJob, AiProviderConnection, MonthlyReview
from tests.test_ai_provider import make_member


@pytest.fixture(autouse=True)
def allowed_harness(settings):
    settings.AGENT_HARNESS_ALLOWED_ORIGINS = ["http://127.0.0.1:12345"]


@pytest.fixture
def connected_member(db):
    _user, person, _household = make_member("retry-owner")
    connection = AiProviderConnection.objects.create(
        owner=person, base_url="http://127.0.0.1:12345", encrypted_token=encrypt_secret("synthetic-token"),
        harness_project="financial-planner", background_backend="local",
    )
    return person, connection


def saved_job(person, connection, *, moment, status=AiJob.Status.QUEUED, age=0):
    job = ai_jobs.enqueue_job(person, feature="structured")
    AiJob.objects.filter(pk=job.pk).update(
        harness_session_id="synthetic-saved-session", attempts=1, status=status,
        input_refs={ai_jobs.SESSION_CONNECTION_KEY: ai_jobs._connection_marker(connection)},
        created_at=moment - timedelta(seconds=age), updated_at=moment - timedelta(hours=1),
        next_attempt_at=moment,
    )
    job.refresh_from_db()
    return job


@pytest.mark.parametrize("value", ["later-private-provider-text", {"private": "text"}, [1], float("inf")])
def test_malformed_model_status_has_only_a_safe_error(monkeypatch, value):
    monkeypatch.setattr(ai_harness, "json_request", Mock(return_value=[
        {"name": "synthetic-model", "state": "ready", "waking_seconds": value},
    ]))
    with pytest.raises(HarnessHttpError) as caught:
        ai_harness.model_status("http://127.0.0.1:12345", "synthetic-token")
    assert caught.value.payload == {}
    assert caught.value.__suppress_context__
    assert str(caught.value) == "The AI provider could not be reached."
    assert ai_harness.failure_from_http(caught.value) == PROVIDER_ERROR


@pytest.mark.parametrize("budget", [1, 3, 5])
@pytest.mark.parametrize("quiet", [False, True])
def test_malformed_readiness_exhausts_the_configured_budget(connected_member, monkeypatch, settings, caplog, budget, quiet):
    person, _connection = connected_member
    settings.AI_JOB_MAX_ATTEMPTS = budget
    monkeypatch.setattr(ai_jobs, "in_quiet_window", Mock(return_value=quiet))
    reply = Mock(return_value=[{"name": "synthetic-model", "state": "ready", "waking_seconds": "private-provider-text"}])
    monkeypatch.setattr(ai_harness, "json_request", reply)
    run = Mock()
    monkeypatch.setattr(ai_jobs, "run_structured", run)
    job = ai_jobs.enqueue_job(person, feature="structured")
    for attempt in range(1, budget + 1):
        ai_jobs.process_due_jobs(now=job.next_attempt_at)
        job.refresh_from_db()
        assert job.attempts == attempt
        assert job.failure_code == PROVIDER_ERROR
        assert job.status == (AiJob.Status.FAILED if attempt == budget else AiJob.Status.QUEUED)
    assert job.finished_at is not None
    ai_jobs.process_due_jobs(now=job.next_attempt_at + timedelta(days=1))
    assert reply.call_count == budget
    run.assert_not_called()
    assert "private-provider-text" not in caplog.text


def test_preclaim_errors_after_an_inference_failure_are_also_bounded(connected_member, monkeypatch, settings):
    person, _connection = connected_member
    settings.AI_JOB_MAX_ATTEMPTS = 3
    monkeypatch.setattr(ai_jobs, "model_status", Mock(return_value=[ModelStatus("synthetic", "ready")]))
    run = Mock(return_value=ProviderResult(ok=False, failure_code=PROVIDER_ERROR))
    monkeypatch.setattr(ai_jobs, "run_structured", run)
    job = ai_jobs.enqueue_job(person, feature="structured")
    ai_jobs.process_due_jobs()
    job.refresh_from_db()
    assert job.attempts == 1
    monkeypatch.setattr(ai_jobs, "_job_connection", Mock(side_effect=RuntimeError("private-provider-text")))
    for _ in range(2):
        ai_jobs.process_due_jobs(now=job.next_attempt_at)
        job.refresh_from_db()
    assert job.status == AiJob.Status.FAILED
    assert job.attempts == 3
    assert job.finished_at is not None
    run.assert_called_once()


@pytest.mark.parametrize("status", [AiJob.Status.QUEUED, AiJob.Status.RUNNING])
@pytest.mark.parametrize("before_claim", [False, True])
def test_expired_exceptional_resumes_become_terminal(connected_member, monkeypatch, settings, status, before_claim):
    person, connection = connected_member
    settings.AI_JOB_RESUME_MAX_AGE_SECONDS = 60
    moment = timezone.now()
    job = saved_job(person, connection, moment=moment, status=status, age=60)
    target = "_job_connection" if before_claim else "run_structured"
    failure = Mock(side_effect=RuntimeError("private-provider-text"))
    monkeypatch.setattr(ai_jobs, target, failure)
    ai_jobs.process_due_jobs(now=moment)
    job.refresh_from_db()
    assert job.status == AiJob.Status.FAILED
    assert job.failure_code == PROVIDER_ERROR
    assert job.finished_at is not None
    assert job.harness_session_id == ""
    assert job.attempts == 1
    ai_jobs.process_due_jobs(now=moment + timedelta(days=1))
    failure.assert_called_once()


def test_resume_exceptions_preserve_the_session_and_do_not_spend_inference_attempts(connected_member, monkeypatch, settings):
    person, connection = connected_member
    settings.AI_JOB_MAX_ATTEMPTS = 2
    settings.AI_JOB_RESUME_DELAY_SECONDS = 10
    job = saved_job(person, connection, moment=timezone.now())
    run = Mock(side_effect=[RuntimeError("synthetic-error")] * 4 + [
        ProviderResult(ok=True, session_id=job.harness_session_id),
    ])
    monkeypatch.setattr(ai_jobs, "run_structured", run)
    readiness = Mock()
    monkeypatch.setattr(ai_jobs, "model_status", readiness)
    for _ in range(4):
        moment = job.next_attempt_at
        ai_jobs.process_due_jobs(now=moment)
        job.refresh_from_db()
        assert job.status == AiJob.Status.QUEUED
        assert job.attempts == 1
        assert job.harness_session_id == "synthetic-saved-session"
        assert job.next_attempt_at >= moment + timedelta(seconds=10)
    ai_jobs.process_due_jobs(now=job.next_attempt_at)
    job.refresh_from_db()
    assert job.status == AiJob.Status.SUCCEEDED
    assert job.attempts == 1
    assert all(call.kwargs["session_id"] == "synthetic-saved-session" for call in run.call_args_list)
    readiness.assert_not_called()


@pytest.mark.parametrize("new_status", [AiJob.Status.QUEUED, AiJob.Status.RUNNING, AiJob.Status.SUCCEEDED, AiJob.Status.FAILED])
def test_preclaim_failure_cannot_overwrite_a_changed_job(connected_member, monkeypatch, new_status):
    person, _connection = connected_member
    job = ai_jobs.enqueue_job(person, feature="structured")
    newer_time = job.updated_at + timedelta(seconds=1)
    newer_due = job.next_attempt_at + timedelta(hours=1)

    def competing_write(_job):
        AiJob.objects.filter(pk=job.pk).update(status=new_status, attempts=2, updated_at=newer_time, next_attempt_at=newer_due)
        raise RuntimeError("synthetic-error")

    monkeypatch.setattr(ai_jobs, "_job_connection", competing_write)
    ai_jobs.process_due_jobs()
    job.refresh_from_db()
    assert job.status == new_status
    assert job.attempts == 2
    assert job.updated_at == newer_time
    assert job.next_attempt_at == newer_due
    assert job.failure_code == ""


@pytest.mark.parametrize("new_status", [AiJob.Status.RUNNING, AiJob.Status.SUCCEEDED, AiJob.Status.FAILED])
def test_claimed_exception_cannot_overwrite_a_new_attempt_or_terminal_job(connected_member, monkeypatch, new_status):
    person, connection = connected_member
    job = saved_job(person, connection, moment=timezone.now())
    new_run = uuid.uuid4() if new_status == AiJob.Status.RUNNING else job.audit_run_id

    def competing_write(*_args, **_kwargs):
        AiJob.objects.filter(pk=job.pk).update(status=new_status, attempts=2, audit_run_id=new_run)
        raise RuntimeError("synthetic-error")

    monkeypatch.setattr(ai_jobs, "run_structured", competing_write)
    ai_jobs.process_due_jobs()
    job.refresh_from_db()
    assert job.status == new_status
    assert job.attempts == 2
    assert job.audit_run_id == new_run
    assert job.failure_code == ""


@pytest.mark.parametrize("expired_session", [False, True])
def test_terminal_failure_releases_phrasing_capacity(connected_member, monkeypatch, settings, expired_session):
    person, connection = connected_member
    settings.AI_JOB_MAX_QUEUED_PER_MEMBER = 1
    settings.AI_JOB_MAX_ATTEMPTS = 2
    moment = timezone.now()
    review = MonthlyReview.objects.create(person=person, month=date(2026, 9, 1),
                                         visibility_key="a" * 64, facts={}, generated_at=moment)
    first = ai_jobs.enqueue_review_phrasing(person, feature="monthly_review", review=review)
    other = MonthlyReview.objects.create(person=person, month=date(2026, 8, 1),
                                        visibility_key="a" * 64, facts={}, generated_at=moment)
    assert ai_jobs.enqueue_review_phrasing(person, feature="monthly_review", review=other) is None
    if expired_session:
        AiJob.objects.filter(pk=first.pk).update(
            harness_session_id="synthetic-saved-session", attempts=1, created_at=moment - timedelta(days=2),
            input_refs={**first.input_refs, ai_jobs.SESSION_CONNECTION_KEY: ai_jobs._connection_marker(connection)},
        )
    monkeypatch.setattr(ai_jobs, "_job_connection", Mock(side_effect=RuntimeError("synthetic-error")))
    for _ in range(1 if expired_session else 2):
        ai_jobs.process_due_jobs(now=first.next_attempt_at)
        first.refresh_from_db()
    assert first.status == AiJob.Status.FAILED
    replacement = ai_jobs.enqueue_review_phrasing(person, feature="monthly_review", review=other)
    assert replacement is not None
    assert replacement.pk != first.pk
    assert AiJob.objects.filter(status__in=ai_jobs.ACTIVE_STATUSES).count() == 1


@pytest.mark.parametrize("http_status", [0, 429, 502])
def test_readiness_http_failures_spend_the_budget(connected_member, monkeypatch, settings, http_status):
    person, _connection = connected_member
    settings.AI_JOB_MAX_ATTEMPTS = 2
    monkeypatch.setattr(ai_jobs, "model_status", Mock(side_effect=HarnessHttpError(http_status)))
    run = Mock()
    monkeypatch.setattr(ai_jobs, "run_structured", run)
    job = ai_jobs.enqueue_job(person, feature="structured")
    for _ in range(2):
        ai_jobs.process_due_jobs(now=job.next_attempt_at)
        job.refresh_from_db()
    assert job.status == AiJob.Status.FAILED
    assert job.attempts == 2
    assert job.finished_at is not None
    run.assert_not_called()


def test_claimed_new_session_exceptions_spend_one_attempt_each(connected_member, monkeypatch, settings):
    person, _connection = connected_member
    settings.AI_JOB_MAX_ATTEMPTS = 3
    monkeypatch.setattr(ai_jobs, "model_status", Mock(return_value=[ModelStatus("synthetic", "ready")]))
    run = Mock(side_effect=RuntimeError("synthetic-error"))
    monkeypatch.setattr(ai_jobs, "run_structured", run)
    job = ai_jobs.enqueue_job(person, feature="structured")
    for attempt in range(1, 4):
        ai_jobs.process_due_jobs(now=job.next_attempt_at)
        job.refresh_from_db()
        assert job.attempts == attempt
    assert job.status == AiJob.Status.FAILED
    assert job.finished_at is not None
    assert run.call_count == 3


@pytest.mark.parametrize("expired", [False, True])
def test_success_write_failure_recovers_the_stored_claimed_state(connected_member, monkeypatch, settings, expired):
    person, connection = connected_member
    settings.AI_JOB_RESUME_MAX_AGE_SECONDS = 60
    moment = timezone.now()
    job = saved_job(person, connection, moment=moment, age=60 if expired else 0)
    monkeypatch.setattr(ai_jobs, "run_structured", Mock(return_value=ProviderResult(
        ok=True, session_id=job.harness_session_id,
    )))
    monkeypatch.setattr(ai_jobs, "_save_job_success", Mock(side_effect=DatabaseError("synthetic deadlock")))
    ai_jobs.process_due_jobs(now=moment)
    job.refresh_from_db()
    assert job.status == (AiJob.Status.FAILED if expired else AiJob.Status.QUEUED)
    assert job.failure_code == PROVIDER_ERROR
    assert job.attempts == 1
    if expired:
        assert job.harness_session_id == ""
        assert job.finished_at is not None
    else:
        assert job.harness_session_id == "synthetic-saved-session"
        assert job.next_attempt_at > moment
        assert job.finished_at is None
