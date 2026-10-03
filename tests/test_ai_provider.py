import threading
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from tests.fake_harness import start_fake_harness
from tests.helpers import stamp_recent_auth

from finance.ai_harness import failure_from_http, run_session
from finance.ai_http import HarnessHttpError
from finance.ai_jobs import enqueue_job, in_quiet_window, process_due_jobs
from finance.ai_services import (
    AiError,
    connect_harness,
    connection_for,
    disconnect_harness,
    discovered_backends,
    local_status,
    member_has_ai,
    run_conversation,
    run_structured,
    set_defaults,
    warm_for_chat,
)
from finance.ai_tools import default_tools
from finance.ai_types import (
    AUTHORIZATION_REQUIRED,
    LIMIT_REACHED,
    PROVIDER_ERROR,
    UNAVAILABLE,
)
from finance.ai_urls import HarnessUrlError, parse_harness_url
from finance.encryption import decrypt_secret
from finance.models import AiJob, AiProviderConnection, AiUsageEvent, Household, Membership, Person
from finance.policy_services import accept_policy, publish_policy


PASSWORD = "Synthetic-passphrase-42!"
TOKEN = "ha-synthetic-app-token"


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_member(username, household=None, policy=None):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    if household is None:
        household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    if policy is None:
        policy = publish_policy(material=True, body="Synthetic privacy policy for AI tests")
    accept_policy(person, policy)
    return user, person, household


@pytest.fixture
def harness():
    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.django_db
def test_unconnected_member_sees_no_ai_ui_and_makes_no_request(harness):
    state, url = harness
    user, _person, _household = make_member("solo")
    client = Client()
    client.force_login(user)
    with patch("finance.ai_http.json_request") as mocked:
        home = client.get(reverse("home"))
        settings_page = client.get(reverse("account-settings"))
    assert mocked.call_count == 0
    assert home.status_code == 200
    assert b'data-ai-ui=' not in home.content
    assert b'data-ai-ui=' not in settings_page.content
    assert b"Connect Agent Harness" in settings_page.content
    assert state.requests == []


@pytest.mark.django_db
def test_discovery_lists_backends_and_marks_hosted_unavailable(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    backends = {item.id: item for item in discovered_backends(person)}
    assert backends["local"].available
    assert backends["local"].slow_to_start
    assert backends["claude"].logged_in
    assert not backends["claude"].available
    assert "unavailable" in backends["claude"].status.lower() or "Hosted" in backends["claude"].status
    assert not backends["codex"].available
    connection = connection_for(person)
    assert connection.background_backend == "local"
    assert connection.chat_backend == "local"
    assert connection.harness_project == "financial-planner"
    assert decrypt_secret(connection.encrypted_token) == TOKEN
    assert TOKEN.encode() not in bytes(connection.encrypted_token)


@pytest.mark.django_db
def test_structured_request_returns_normalized_result(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    result = run_structured(person, "Return a short confirmation.", feature="structured")
    assert result.ok
    assert result.answer == "synthetic-ok"
    assert result.usage.prompt_tokens == 3
    event = AiUsageEvent.objects.visible_to(person).get()
    assert event.backend == "local"
    assert event.feature == "structured"
    assert event.outcome == "ok"
    assert event.prompt_tokens == 3


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("provider_unavailable", UNAVAILABLE),
        ("provider_auth_required", AUTHORIZATION_REQUIRED),
        ("quota_reached", LIMIT_REACHED),
        ("provider_error", PROVIDER_ERROR),
    ],
)
@pytest.mark.django_db
def test_each_failure_code_is_normalized(harness, code, expected):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    state.session_failure = code
    result = run_structured(person, "synthetic", feature="structured")
    assert not result.ok
    assert result.failure_code == expected
    assert result.answer is None


@pytest.mark.django_db
def test_tool_calling_conversation(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    state.need_tool = True
    result = run_conversation(
        person,
        "What accounts can I see?",
        feature="chat",
        tools=default_tools(),
    )
    assert result.ok
    assert result.answer.startswith("tool:")
    assert "Unknown tool" not in result.answer


@pytest.mark.django_db
def test_background_job_waits_while_local_model_sleeps(harness, monkeypatch):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured", input_refs={"transaction_ids": [1]})
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: False)
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.WAITING_MODEL
    state.model_state = "ready"
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED


@pytest.mark.django_db
def test_background_job_runs_in_quiet_window_while_asleep(harness, monkeypatch):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED
    assert state.model_state == "sleeping"


@pytest.mark.django_db
def test_token_rejected_after_connecting(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    state.revoked = True
    with pytest.raises(AiError) as caught:
        discovered_backends(person)
    assert caught.value.failure_code == AUTHORIZATION_REQUIRED
    assert TOKEN not in str(caught.value)
    connection = connection_for(person)
    assert connection.last_status == AUTHORIZATION_REQUIRED


@pytest.mark.django_db
def test_settings_connect_requires_reauth_and_hides_token(harness):
    _state, url = harness
    user, _person, _household = make_member("owner")
    client = Client()
    client.force_login(user)
    refused = client.post(reverse("ai-connect"), {"base_url": url, "token": TOKEN})
    assert refused.url.startswith(reverse("reauth"))
    stamp_recent_auth(client)
    saved = client.post(reverse("ai-connect"), {"base_url": url, "token": TOKEN})
    assert saved.url == reverse("account-settings")
    page = client.get(reverse("account-settings"))
    assert TOKEN not in page.content.decode()
    assert b'data-ai-ui="connected"' in page.content


@pytest.mark.django_db
def test_harness_url_rejects_off_host_and_plain_http_tailnet():
    with pytest.raises(HarnessUrlError):
        parse_harness_url("https://example.invalid")
    with pytest.raises(HarnessUrlError):
        parse_harness_url("http://tower.example.ts.net")
    with pytest.raises(HarnessUrlError):
        parse_harness_url("https://user:pass@127.0.0.1:8100")
    with pytest.raises(HarnessUrlError):
        parse_harness_url("http://127.0.0.1:8100/path?x=1")
    assert parse_harness_url("https://tower.example.ts.net") == "https://tower.example.ts.net"


@pytest.mark.django_db
def test_disconnect_erases_the_token(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    disconnect_harness(person)
    assert not AiProviderConnection.objects.owned_by(person).exists()


@pytest.mark.django_db
def test_session_poll_backs_off_until_the_last_get_completes(harness):
    state, url = harness
    state.running_polls = 3
    clock = _FakeClock()
    result = run_session(
        url,
        TOKEN,
        prompt="synthetic",
        backend="local",
        project="financial-planner",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.ok
    assert result.answer == "synthetic-ok"
    assert clock.sleeps == [0.5, 1.0, 2.0]
    session_gets = [
        item for item in state.requests if item == ("GET", f"/api/v1/sessions/{result.session_id}")
    ]
    assert len(session_gets) == 3


@pytest.mark.django_db
def test_session_timeout_returns_unavailable_without_another_create(harness, settings):
    state, url = harness
    state.running_polls = 40
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 2
    clock = _FakeClock()
    result = run_session(
        url,
        TOKEN,
        prompt="synthetic",
        backend="local",
        project="financial-planner",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert not result.ok
    assert result.failure_code == UNAVAILABLE
    assert result.session_id
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1
    assert clock.sleeps[0] == 0.5
    assert clock.sleeps[1] == 1.0
    assert max(clock.sleeps) <= 5.0


@pytest.mark.django_db
def test_background_job_resumes_the_same_harness_session(harness, monkeypatch, settings):
    state, url = harness
    state.model_state = "ready"
    state.running_polls = 40
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 2
    clock = _FakeClock()
    monkeypatch.setattr("finance.ai_harness.time.sleep", clock.sleep)
    monkeypatch.setattr("finance.ai_harness.time.monotonic", clock.monotonic)
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    moment = timezone.now()
    process_due_jobs(now=moment)
    job.refresh_from_db()
    assert job.status == job.Status.QUEUED
    assert job.failure_code == UNAVAILABLE
    assert job.harness_session_id
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1
    state.session_polls_left[job.harness_session_id] = 1
    clock.now = 0.0
    process_due_jobs(now=moment + timedelta(hours=2))
    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1


def _mark_running(job, *, session_id, updated_at, attempts=1):
    AiJob.objects.filter(pk=job.pk).update(
        status=AiJob.Status.RUNNING,
        attempts=attempts,
        harness_session_id=session_id,
        updated_at=updated_at,
    )


@pytest.mark.django_db
def test_stale_running_job_resumes_without_creating_a_session(harness, settings):
    state, url = harness
    state.model_state = "ready"
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 2
    settings.AGENT_HARNESS_STALE_JOB_MARGIN_SECONDS = 1
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    session_id = "ses-stale-resume"
    state.sessions[session_id] = {
        "id": session_id,
        "status": "running",
        "prompt_tokens": 3,
        "completion_tokens": 0,
    }
    state.session_polls_left[session_id] = 1
    job = enqueue_job(person, feature="structured")
    _mark_running(job, session_id=session_id, updated_at=timezone.now() - timedelta(seconds=60))
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED
    assert job.harness_session_id == session_id
    assert state.requests.count(("POST", "/api/v1/sessions")) == 0
    assert ("GET", f"/api/v1/sessions/{session_id}") in state.requests


@pytest.mark.django_db
def test_fresh_running_job_is_left_alone(harness, settings):
    state, url = harness
    state.model_state = "ready"
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 600
    settings.AGENT_HARNESS_STALE_JOB_MARGIN_SECONDS = 120
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    session_id = "ses-fresh-running"
    state.sessions[session_id] = {
        "id": session_id,
        "status": "running",
        "prompt_tokens": 3,
        "completion_tokens": 0,
    }
    job = enqueue_job(person, feature="structured")
    _mark_running(job, session_id=session_id, updated_at=timezone.now())
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.RUNNING
    assert job.harness_session_id == session_id
    assert state.requests.count(("POST", "/api/v1/sessions")) == 0
    assert ("GET", f"/api/v1/sessions/{session_id}") not in state.requests


@pytest.mark.django_db
def test_stale_running_job_without_session_is_requeued(harness, settings):
    state, url = harness
    state.model_state = "ready"
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 2
    settings.AGENT_HARNESS_STALE_JOB_MARGIN_SECONDS = 1
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    moment = timezone.now()
    _mark_running(job, session_id="", updated_at=moment - timedelta(seconds=60), attempts=1)
    process_due_jobs(now=moment)
    job.refresh_from_db()
    assert job.status == job.Status.QUEUED
    assert job.attempts == 1
    assert moment + timedelta(seconds=120) <= job.next_attempt_at <= timezone.now() + timedelta(seconds=120)
    assert state.requests.count(("POST", "/api/v1/sessions")) == 0


@pytest.mark.django_db(transaction=True)
def test_two_runners_claim_the_same_job_once(harness):
    if connection.vendor != "postgresql":
        pytest.skip("atomic job claims need PostgreSQL row locks")
    state, url = harness
    state.model_state = "ready"
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    barrier = threading.Barrier(2)
    errors = []

    def run():
        try:
            barrier.wait(timeout=10)
            process_due_jobs()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            connections.close_all()

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)
    assert errors == []
    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1


@pytest.mark.django_db
def test_undecryptable_token_fails_only_that_members_job(harness):
    state, url = harness
    state.model_state = "ready"
    _user_a, person_a, _household_a = make_member("alpha")
    policy = person_a.privacy_policy_acceptances.get().policy_version
    _user_b, person_b, _household_b = make_member("beta", policy=policy)
    connect_harness(person_a, base_url=url, token=TOKEN)
    connect_harness(person_b, base_url=url, token=TOKEN)
    set_defaults(person_a, chat_backend="local", background_backend="local")
    set_defaults(person_b, chat_backend="local", background_backend="local")
    broken = enqueue_job(person_a, feature="structured")
    other = enqueue_job(person_b, feature="structured")
    connection = connection_for(person_a)
    connection.encrypted_token = b"not-a-valid-fernet-token"
    connection.save(update_fields=("encrypted_token",))
    process_due_jobs()
    broken.refresh_from_db()
    other.refresh_from_db()
    assert broken.status == broken.Status.FAILED
    assert broken.failure_code == AUTHORIZATION_REQUIRED
    assert other.status == other.Status.SUCCEEDED


@pytest.mark.django_db
def test_unexpected_job_error_does_not_stop_the_runner(harness, monkeypatch):
    state, url = harness
    state.model_state = "ready"
    _user_a, person_a, _household_a = make_member("alpha")
    policy = person_a.privacy_policy_acceptances.get().policy_version
    _user_b, person_b, _household_b = make_member("beta", policy=policy)
    connect_harness(person_a, base_url=url, token=TOKEN)
    connect_harness(person_b, base_url=url, token=TOKEN)
    set_defaults(person_a, chat_backend="local", background_backend="local")
    set_defaults(person_b, chat_backend="local", background_backend="local")
    boom = enqueue_job(person_a, feature="structured")
    other = enqueue_job(person_b, feature="structured")
    original = run_structured

    def explode(person, prompt, **kwargs):
        if person.pk == person_a.pk:
            raise RuntimeError("synthetic job crash")
        return original(person, prompt, **kwargs)

    monkeypatch.setattr("finance.ai_jobs.run_structured", explode)
    process_due_jobs()
    boom.refresh_from_db()
    other.refresh_from_db()
    assert boom.status == boom.Status.QUEUED
    assert boom.failure_code == PROVIDER_ERROR
    assert other.status == other.Status.SUCCEEDED


@pytest.mark.django_db
def test_hosted_backends_selectable_when_sessions_enabled(harness, settings):
    _state, url = harness
    settings.AGENT_HARNESS_HOSTED_SESSIONS = True
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    backends = {item.id: item for item in discovered_backends(person)}
    assert backends["claude"].available
    connection = connection_for(person)
    assert connection.chat_backend == "claude"
    assert connection.background_backend == "local"


@pytest.mark.django_db
def test_warm_and_local_status_use_the_saved_connection(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    rows = local_status(person)
    assert rows[0].state == "sleeping"
    warmed = warm_for_chat(person)
    assert warmed[0].name == "tower"
    assert ("POST", "/api/v1/models/warm") in state.requests


@pytest.mark.django_db
def test_quiet_window_wraps_midnight_and_rejects_bad_ranges(settings):
    settings.AI_LOCAL_QUIET_WINDOW = "22:00-06:00"
    night = timezone.make_aware(datetime(2026, 1, 2, 23, 30))
    morning = timezone.make_aware(datetime(2026, 1, 2, 5, 0))
    noon = timezone.make_aware(datetime(2026, 1, 2, 12, 0))
    assert in_quiet_window(night)
    assert in_quiet_window(morning)
    assert not in_quiet_window(noon)
    settings.AI_LOCAL_QUIET_WINDOW = "09:00-17:00"
    assert in_quiet_window(timezone.make_aware(datetime(2026, 1, 2, 10, 0)))
    assert not in_quiet_window(timezone.make_aware(datetime(2026, 1, 2, 20, 0)))
    settings.AI_LOCAL_QUIET_WINDOW = "not-a-window"
    assert not in_quiet_window(noon)
    settings.AI_LOCAL_QUIET_WINDOW = ""
    assert not in_quiet_window(night)


def test_http_failures_map_to_stable_codes():
    assert failure_from_http(HarnessHttpError(401, {})) == AUTHORIZATION_REQUIRED
    assert failure_from_http(HarnessHttpError(403, {})) == AUTHORIZATION_REQUIRED
    assert failure_from_http(HarnessHttpError(429, {})) == LIMIT_REACHED
    assert failure_from_http(HarnessHttpError(0, {})) == UNAVAILABLE
    assert failure_from_http(HarnessHttpError(502, {"error": {"code": "quota_reached"}})) == LIMIT_REACHED
    assert failure_from_http(HarnessHttpError(502, {"failure": {"code": "provider_unavailable"}})) == UNAVAILABLE
    assert failure_from_http(HarnessHttpError(500, {})) == PROVIDER_ERROR


@pytest.mark.django_db
def test_settings_disconnect_and_defaults_require_reauth(harness):
    _state, url = harness
    user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(user)
    refused = client.post(reverse("ai-disconnect"))
    assert refused.url.startswith(reverse("reauth"))
    stamp_recent_auth(client)
    disconnected = client.post(reverse("ai-disconnect"))
    assert disconnected.url == reverse("account-settings")
    assert not AiProviderConnection.objects.owned_by(person).exists()
    connect_harness(person, base_url=url, token=TOKEN)
    stamp_recent_auth(client)
    saved = client.post(
        reverse("ai-defaults"),
        {"chat_backend": "local", "background_backend": "local", "chat_model": "", "background_model": ""},
    )
    assert saved.url == reverse("account-settings")
    person.refresh_from_db()
    connection = connection_for(person)
    assert connection.chat_backend == "local"
    invalid = client.post(reverse("ai-connect"), {"base_url": "", "token": ""})
    assert invalid.url == reverse("account-settings")


@pytest.mark.django_db
def test_member_without_connection_is_not_ai_ready():
    _user, person, _household = make_member("solo")
    assert not member_has_ai(person)


@pytest.mark.django_db
def test_connect_rejects_non_app_token(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    with pytest.raises(AiError):
        connect_harness(person, base_url=url, token="not-an-app-token")


@pytest.mark.django_db
def test_set_defaults_rejects_unavailable_hosted_backend(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    with pytest.raises(AiError):
        set_defaults(person, chat_backend="claude", background_backend="local")


@pytest.mark.django_db
def test_json_request_unreachable_is_unavailable():
    with pytest.raises(HarnessHttpError) as caught:
        from finance.ai_http import json_request

        json_request("http://127.0.0.1:1/missing", token=TOKEN, timeout=1)
    assert caught.value.status == 0


@pytest.mark.django_db
def test_job_without_connection_fails_unavailable():
    _user, person, _household = make_member("solo")
    job = enqueue_job(person, feature="monthly_review")
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.FAILED
    assert job.failure_code == UNAVAILABLE


@pytest.mark.django_db
def test_waiting_app_without_pending_calls_still_times_out(harness, settings):
    state, url = harness
    state.stuck_waiting_app = True
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 3
    clock = _FakeClock()
    calls = []

    def tool_runner(name, args):
        calls.append(name)
        return "unused", True

    result = run_session(
        url,
        TOKEN,
        prompt="synthetic",
        backend="local",
        project="financial-planner",
        tool_runner=tool_runner,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert not result.ok
    assert result.failure_code == UNAVAILABLE
    assert calls == []
    assert clock.sleeps
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1


@pytest.mark.django_db
def test_answering_a_tool_call_finishes_the_session(harness):
    state, url = harness
    state.need_tool = True
    clock = _FakeClock()
    calls = []

    def tool_runner(name, args):
        calls.append(name)
        return "synthetic-accounts", True

    result = run_session(
        url,
        TOKEN,
        prompt="synthetic",
        backend="local",
        project="financial-planner",
        tool_runner=tool_runner,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.ok
    assert calls == ["list_accounts"]
    assert result.answer.startswith("tool:synthetic-accounts")


@pytest.mark.django_db
def test_revoked_token_fails_a_local_job_outside_the_quiet_window(harness, monkeypatch):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: False)
    state.revoked = True

    process_due_jobs()

    job.refresh_from_db()
    assert job.status == job.Status.FAILED
    assert job.failure_code == AUTHORIZATION_REQUIRED


@pytest.mark.django_db
def test_stale_runner_copy_does_not_overwrite_a_finished_job(harness, monkeypatch):
    from finance.ai_jobs import _process_one

    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    stale = AiJob.objects.get(pk=job.pk)
    state.model_state = "ready"
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED

    state.model_state = "sleeping"
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: False)
    _process_one(stale, timezone.now())

    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED


@pytest.mark.django_db
def test_failed_session_is_not_resumed_on_retry(harness):
    state, url = harness
    state.model_state = "ready"
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    state.session_failure = "provider_unavailable"

    process_due_jobs()

    job.refresh_from_db()
    assert job.status == job.Status.QUEUED
    assert job.failure_code == UNAVAILABLE
    assert job.harness_session_id == ""
    AiJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())

    process_due_jobs()

    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED
    assert state.requests.count(("POST", "/api/v1/sessions")) == 2


@pytest.mark.django_db
def test_poll_error_keeps_the_session_and_the_retry_resumes_it(harness, monkeypatch):
    state, url = harness
    state.model_state = "ready"
    state.running_polls = 2
    state.session_get_errors = 1
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    monkeypatch.setattr("finance.ai_harness.time.sleep", lambda seconds: None)

    process_due_jobs()

    job.refresh_from_db()
    assert job.status == job.Status.QUEUED
    assert job.harness_session_id
    state.model_state = "sleeping"
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: False)
    AiJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())

    process_due_jobs()

    job.refresh_from_db()
    assert job.status == job.Status.SUCCEEDED
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1


@pytest.mark.django_db
def test_tool_calls_stop_when_a_new_material_policy_is_published(harness):
    from finance.ai_tools import run_tool

    _state, _url = harness
    _user, person, _household = make_member("owner")
    tools = default_tools()
    output, ok = run_tool(person, tools, "list_accounts", {})
    assert ok

    publish_policy(material=True, body="Synthetic policy, second material version")

    output, ok = run_tool(person, tools, "list_accounts", {})
    assert not ok
    assert "privacy and data policy" in output


@pytest.mark.django_db
def test_resuming_an_open_session_spends_no_attempts_until_the_age_cap(harness, settings):
    from datetime import timedelta

    state, url = harness
    state.model_state = "ready"
    state.running_polls = 1000
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 0
    settings.AI_JOB_MAX_ATTEMPTS = 2
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")

    for _ in range(4):
        AiJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
        process_due_jobs()
        job.refresh_from_db()
        assert job.status == job.Status.QUEUED
        assert job.harness_session_id
    assert job.attempts == 1
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1

    AiJob.objects.filter(pk=job.pk).update(
        next_attempt_at=timezone.now(), created_at=timezone.now() - timedelta(days=2)
    )
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == job.Status.FAILED
    assert job.harness_session_id == ""


def test_redirects_are_not_followed_so_the_token_stays_put():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from finance.ai_http import HarnessHttpError, json_request

    received = []

    class Elsewhere(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    elsewhere = HTTPServer(("127.0.0.1", 0), Elsewhere)
    target = f"http://127.0.0.1:{elsewhere.server_port}/steal"

    class Redirector(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

    redirector = HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (elsewhere, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(HarnessHttpError) as caught:
            json_request(f"http://127.0.0.1:{redirector.server_port}/api/v1", token=TOKEN)
        assert caught.value.status == 302
        assert received == []
    finally:
        for server in (elsewhere, redirector):
            server.shutdown()
            server.server_close()


@pytest.mark.django_db
def test_hosted_backend_is_refused_at_run_time_once_the_operator_turns_it_off(harness, settings):
    state, url = harness
    settings.AGENT_HARNESS_HOSTED_SESSIONS = True
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="claude", background_backend="claude")
    settings.AGENT_HARNESS_HOSTED_SESSIONS = False

    result = run_structured(person, "synthetic", feature="structured")

    assert not result.ok
    assert result.failure_code == UNAVAILABLE
    assert ("POST", "/api/v1/sessions") not in state.requests


@pytest.mark.django_db
def test_claim_respects_a_backoff_set_by_another_runner(harness):
    from datetime import timedelta

    from finance.ai_jobs import _process_one

    state, url = harness
    state.model_state = "ready"
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    stale = AiJob.objects.get(pk=job.pk)
    AiJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now() + timedelta(minutes=10))

    _process_one(stale, timezone.now())

    job.refresh_from_db()
    assert job.status == job.Status.QUEUED
    assert job.attempts == 0
    assert ("POST", "/api/v1/sessions") not in state.requests


@pytest.mark.django_db
def test_resume_delay_counts_from_when_the_wait_ended(harness, settings):
    from datetime import timedelta

    state, url = harness
    state.model_state = "ready"
    state.running_polls = 1000
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 0
    settings.AI_JOB_RESUME_DELAY_SECONDS = 300
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    started = timezone.now() - timedelta(minutes=20)
    AiJob.objects.filter(pk=job.pk).update(next_attempt_at=started)

    process_due_jobs(now=started)

    job.refresh_from_db()
    assert job.status == job.Status.QUEUED
    assert job.next_attempt_at >= timezone.now() + timedelta(seconds=290)


@pytest.mark.django_db
def test_connect_refuses_when_the_dedicated_project_is_missing(harness):
    state, url = harness
    state.projects = [{"name": "personal-code", "description": "", "target": "local"}]
    _user, person, _household = make_member("owner")

    with pytest.raises(AiError) as caught:
        connect_harness(person, base_url=url, token=TOKEN)

    assert "financial-planner" in str(caught.value)
    assert connection_for(person) is None


@pytest.mark.django_db
def test_unreadable_token_shows_reconnect_and_disconnect_still_works(harness):
    _state, url = harness
    user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    AiProviderConnection.objects.filter(owner=person).update(encrypted_token=b"not-a-valid-ciphertext")
    client = Client()
    client.force_login(user)

    page = client.get(reverse("account-settings"))
    assert b"Disconnect it and connect again" in page.content

    stamp_recent_auth(client)
    client.post(reverse("ai-disconnect"))
    assert connection_for(person) is None
