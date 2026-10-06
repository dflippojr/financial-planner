"""Batch scheduling stays bounded and uses the existing atomic job lifecycle."""

import threading
import time
from concurrent.futures import Future
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from finance import ai_jobs
from finance.models import AiJob
from tests.test_ai_provider import make_member


@pytest.fixture
def lane():
    runner = ai_jobs.AiJobLane()
    try:
        yield runner
    finally:
        runner.close()


@pytest.mark.parametrize("configured, expected", [(0, 1), (2, 2), (3, 3), (50, 3)])
def test_pool_never_exceeds_three(settings, configured, expected):
    settings.AI_JOB_WORKERS = configured
    runner = ai_jobs.AiJobLane()
    try:
        assert runner.workers == expected
    finally:
        runner.close()


@pytest.mark.django_db
def test_idle_poll_is_one_query(lane):
    with CaptureQueriesContext(connection) as queries:
        lane.tick()
    assert len(queries) == 1
    assert not lane._inflight


@pytest.mark.django_db
def test_blocks_only_three_workers_and_refills_without_duplicate_submission(lane, monkeypatch):
    _user, person, _household = make_member("batch")
    jobs = [AiJob.objects.create(member=person, feature="structured") for _ in range(4)]
    started = []
    release = threading.Event()
    all_started = threading.Event()
    lock = threading.Lock()

    def block(pk, _moment):
        with lock:
            started.append(pk)
            if len(started) == 3:
                all_started.set()
        assert release.wait(5)

    monkeypatch.setattr(lane, "_run", block)
    try:
        lane.tick()
        assert all_started.wait(2)
        with CaptureQueriesContext(connection) as queries:
            lane.tick()
        assert len(queries) == 0
        assert set(started) == {job.pk for job in jobs[:3]}
        # Model the result before releasing the synthetic worker.
        AiJob.objects.filter(pk__in=started).update(status=AiJob.Status.SUCCEEDED)
    finally:
        release.set()
    for future in lane._inflight.values():
        future.result(timeout=2)
    lane.tick()
    for future in lane._inflight.values():
        future.result(timeout=2)
    assert set(started) == {job.pk for job in jobs}
    assert len(started) == 4


@pytest.mark.django_db
def test_sleeping_model_jobs_do_not_starve_other_due_jobs(lane, monkeypatch):
    settings_workers = lane.workers
    _user, person, _household = make_member("waiting")
    waiting = [AiJob.objects.create(member=person, feature="structured") for _ in range(settings_workers)]
    queued = AiJob.objects.create(member=person, feature="structured")
    AiJob.objects.filter(pk=queued.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    # Reproduce the existing quiet-window gate moving the sleeping jobs to now.
    AiJob.objects.filter(pk__in=[job.pk for job in waiting]).update(
        status=AiJob.Status.WAITING_MODEL, next_attempt_at=timezone.now()
    )
    submitted = []

    def submit(_run, pk, _moment):
        submitted.append(pk)
        return Future()

    monkeypatch.setattr(lane.executor, "submit", submit)
    lane.tick()
    assert submitted[0] == queued.pk


@pytest.mark.django_db
def test_worker_closes_connections_after_missing_job_or_storage_failure(monkeypatch):
    close = Mock()
    # This direct worker call runs inside pytest's wrapping transaction.
    monkeypatch.setattr(ai_jobs, "close_old_connections", Mock())
    monkeypatch.setattr(ai_jobs.connections, "close_all", close)
    ai_jobs.AiJobLane._run(999999, timezone.now())
    _user, person, _household = make_member("failure")
    job = AiJob.objects.create(member=person, feature="structured")
    monkeypatch.setattr(ai_jobs, "_process_safely", Mock(side_effect=RuntimeError("synthetic storage failure")))
    ai_jobs.AiJobLane._run(job.pk, timezone.now())
    assert close.call_count == 2


@pytest.mark.django_db
def test_worker_isolates_one_failure_and_continues(monkeypatch):
    _user, person, _household = make_member("isolation")
    jobs = [AiJob.objects.create(member=person, feature="structured") for _ in range(2)]
    process = Mock(side_effect=[RuntimeError("synthetic job failure"), True])
    isolate = Mock()
    monkeypatch.setattr(ai_jobs, "_process_one", process)
    monkeypatch.setattr(ai_jobs, "close_old_connections", Mock())
    monkeypatch.setattr(ai_jobs, "_isolate_job_failure", isolate)
    monkeypatch.setattr(ai_jobs.connections, "close_all", Mock())
    for job in jobs:
        ai_jobs.AiJobLane._run(job.pk, timezone.now())
    assert process.call_count == 2
    assert isolate.call_args.args[0].pk == jobs[0].pk


@pytest.mark.django_db(transaction=True)
def test_competing_pools_process_200_jobs_once(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("concurrent claims require PostgreSQL row locks")
    from finance.ai_services import connect_harness, set_defaults
    from finance.ai_types import ProviderResult
    from tests.fake_harness import start_fake_harness

    state, url, server = start_fake_harness()
    runners = [ai_jobs.AiJobLane(), ai_jobs.AiJobLane()]
    try:
        _user, person, _household = make_member("two-pools")
        state.model_state = "ready"
        connect_harness(person, base_url=url, token=state.token)
        set_defaults(person, chat_backend="local", background_backend="local")
        AiJob.objects.bulk_create([AiJob(member=person, feature="structured", backend="local") for _ in range(200)])
        calls = []
        lock = threading.Lock()

        def run(_person, _prompt, *, on_session, **_kwargs):
            with lock:
                session = f"synthetic-{len(calls)}"
                calls.append(session)
            on_session(session)
            time.sleep(0.01)
            return ProviderResult(ok=True, session_id=session)

        monkeypatch.setattr(ai_jobs, "run_structured", run)
        deadline = time.monotonic() + 30
        while AiJob.objects.exclude(status=AiJob.Status.SUCCEEDED).exists():
            assert time.monotonic() < deadline
            for runner in runners:
                runner.tick()
            time.sleep(0.01)
        assert len(calls) == 200
        assert AiJob.objects.filter(attempts=1, status=AiJob.Status.SUCCEEDED).count() == 200
        assert len(set(AiJob.objects.values_list("harness_session_id", flat=True))) == 200
    finally:
        for runner in runners:
            runner.close()
        server.shutdown()
        server.server_close()
