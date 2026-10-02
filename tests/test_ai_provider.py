from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness
from tests.helpers import stamp_recent_auth

from finance.ai_jobs import enqueue_job, process_due_jobs
from finance.ai_services import (
    AiError,
    connect_harness,
    connection_for,
    disconnect_harness,
    discovered_backends,
    run_conversation,
    run_structured,
    set_defaults,
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
from finance.models import AiProviderConnection, AiUsageEvent, Household, Membership, Person
from finance.policy_services import accept_policy, publish_policy


PASSWORD = "Synthetic-passphrase-42!"
TOKEN = "ha-synthetic-app-token"


def make_member(username, household=None):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    if household is None:
        household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
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
    assert parse_harness_url("http://127.0.0.1:8100") == "http://127.0.0.1:8100"
    assert parse_harness_url("https://host.docker.internal:8100") == "https://host.docker.internal:8100"


@pytest.mark.django_db
def test_disconnect_erases_the_token(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    disconnect_harness(person)
    assert not AiProviderConnection.objects.owned_by(person).exists()
