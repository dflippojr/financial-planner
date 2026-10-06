"""Bring-your-own Anthropic or OpenAI key, tested against fake HTTP providers."""

import logging
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from tests.chat_helpers import ask
from tests.fake_provider import (
    ANTHROPIC_KEY,
    OPENAI_KEY,
    anthropic_text,
    anthropic_tool,
    openai_text,
    openai_tool,
    start_fake_provider,
)
from tests.helpers import stamp_recent_auth
from tests.test_chat import TOKEN, add_txn, checking, harness, make_member  # noqa: F401 - harness is a fixture

from finance.ai_jobs import enqueue_job, process_due_jobs
from finance.ai_services import (
    AiError,
    api_usage_summary,
    connect_api_key,
    connect_harness,
    disconnect_api_key,
    disconnect_harness,
    member_has_ai,
    resolve_ai,
    run_structured,
    set_api_defaults,
    set_defaults,
)
from finance.encryption import decrypt_secret
from finance.models import AiConversationMessage, AiJob, AiProviderConnection, AiUsageEvent, Person
from finance.policy_services import accept_policy, current_policy, may_use_ai, publish_policy

ANTHROPIC = "anthropic_api"
OPENAI = "openai_api"
KEYS = {ANTHROPIC: ANTHROPIC_KEY, OPENAI: OPENAI_KEY}


@pytest.fixture
def provider(settings):
    state, url, server = start_fake_provider()
    settings.AI_ANTHROPIC_BASE_URL = url
    settings.AI_OPENAI_BASE_URL = url
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _member(kind, *, chat=True, background=False):
    user, person, household = make_member("byok")
    connect_api_key(person, kind=kind, key=KEYS[kind])
    set_api_defaults(
        person,
        kind=kind,
        chat_model="",
        background_model="",
        use_chat=chat,
        use_background=background,
    )
    return user, person, household


def _answer(conversation):
    return conversation.messages.filter(role__in=("assistant", "error")).last()


@pytest.mark.django_db
@pytest.mark.parametrize("kind", [ANTHROPIC, OPENAI])
def test_chat_runs_tools_and_logs_tokens(provider, kind):
    _user, person, household = _member(kind)
    account = checking(person, household, "Synthetic Checking")
    add_txn(account, person, date(2026, 1, 3), -500, "Synthetic dining")
    args = {"date_from": "2026-01-01", "date_to": "2026-01-31"}
    if kind == ANTHROPIC:
        provider.script = [(200, anthropic_tool("cash_flow_totals", args)), (200, anthropic_text("You spent five."))]
    else:
        provider.script = [(200, openai_tool("cash_flow_totals", args)), (200, openai_text("You spent five."))]

    conversation = ask(person, "How much did I spend?")

    reply = _answer(conversation)
    assert reply.status == AiConversationMessage.Status.DONE
    assert reply.content == "You spent five."
    assert reply.figures
    first, second = provider.requests
    if kind == ANTHROPIC:
        assert first["path"] == "/v1/messages"
        assert first["headers"]["x-api-key"] == ANTHROPIC_KEY
        assert first["body"]["model"] == "claude-sonnet-5-5"
        assert first["body"]["tools"][0]["input_schema"]
        result = second["body"]["messages"][-1]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    else:
        assert first["path"] == "/v1/responses"
        assert first["headers"]["authorization"] == f"Bearer {OPENAI_KEY}"
        assert first["body"]["store"] is False
        assert first["body"]["model"] == "gpt-6.1-sol"
        assert first["body"]["tools"][0]["type"] == "function"
        output = second["body"]["input"][-1]
        assert output["type"] == "function_call_output" and output["call_id"] == "call_1"
    event = AiUsageEvent.objects.get(member=person)
    assert event.provider == kind and event.outcome == "ok"
    assert event.prompt_tokens > 0 and event.completion_tokens > 0
    assert api_usage_summary(person)[kind] == event.prompt_tokens + event.completion_tokens


@pytest.mark.django_db
def test_chat_follow_up_resends_history(provider):
    _user, person, _household = _member(ANTHROPIC)
    conversation = ask(person, "First question")
    provider.answer = "Second answer."
    ask(person, "Second question", conversation_id=conversation.pk)
    messages = provider.requests[-1]["body"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "First question"
    assert messages[-1]["content"] == "Second question"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "status", "payload", "expected"),
    [
        (ANTHROPIC, 401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}, "authorization_required"),
        (ANTHROPIC, 429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}, "limit_reached"),
        (ANTHROPIC, 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}, "limit_reached"),
        (ANTHROPIC, 529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, "unavailable"),
        (OPENAI, 401, {"error": {"type": "invalid_request_error", "code": "invalid_api_key", "message": "bad"}}, "authorization_required"),
        (OPENAI, 429, {"error": {"type": "requests", "code": "rate_limit_exceeded", "message": "slow"}}, "limit_reached"),
        (OPENAI, 429, {"error": {"type": "insufficient_quota", "code": "insufficient_quota", "message": "no"}}, "limit_reached"),
        (OPENAI, 500, {"error": {"message": "oops"}}, "unavailable"),
    ],
)
def test_provider_failures_map_without_repeating_provider_text(provider, caplog, kind, status, payload, expected):
    _user, person, _household = _member(kind)
    provider.script = [(status, payload)]
    with caplog.at_level(logging.DEBUG):
        conversation = ask(person, "Hello?")
    reply = _answer(conversation)
    assert reply.status == AiConversationMessage.Status.FAILED
    assert AiUsageEvent.objects.get(member=person).outcome == expected
    shown = reply.content + caplog.text
    for secret in (ANTHROPIC_KEY, OPENAI_KEY, "bad key", "credit balance", "oops"):
        assert secret not in shown


@pytest.mark.django_db
def test_unreachable_provider_is_unavailable(provider, settings):
    _user, person, _household = _member(OPENAI, chat=False, background=True)
    settings.AI_OPENAI_BASE_URL = "http://127.0.0.1:1"
    result = run_structured(person, "Hi", feature="structured")
    assert not result.ok and result.failure_code == "unavailable"


@pytest.mark.django_db
def test_key_is_encrypted_and_never_shown(provider):
    user, person, _household = _member(ANTHROPIC)
    row = AiProviderConnection.objects.get(owner=person, kind=ANTHROPIC)
    assert ANTHROPIC_KEY.encode() not in bytes(row.encrypted_token)
    assert decrypt_secret(row.encrypted_token) == ANTHROPIC_KEY
    client = Client()
    client.force_login(user)
    page = client.get(reverse("settings-ai")).content.decode()
    assert ANTHROPIC_KEY not in page
    assert "Anthropic API" in page


@pytest.mark.django_db
def test_settings_views_require_reauth_and_hide_the_key(provider):
    user, person, _household = make_member("viewer")
    client = Client()
    client.force_login(user)
    body = {"provider": OPENAI, "key": OPENAI_KEY}
    assert client.post(reverse("ai-key-connect"), body).url.startswith(reverse("reauth"))
    stamp_recent_auth(client)
    assert client.post(reverse("ai-key-connect"), body).url == reverse("settings-ai")
    assert AiProviderConnection.objects.filter(owner=person, kind=OPENAI).exists()
    page = client.get(reverse("settings-ai"), follow=True).content.decode()
    assert OPENAI_KEY not in page
    saved = client.post(
        reverse("ai-key-defaults", args=[OPENAI]),
        {"chat_model": "gpt-6-astra", "background_model": "gpt-6-luna", "use_for_chat": "on"},
    )
    assert saved.url == reverse("settings-ai")
    row = AiProviderConnection.objects.get(owner=person, kind=OPENAI)
    assert row.chat_model == "gpt-6-astra" and row.use_for_chat and not row.use_for_background
    refused = client.post(reverse("ai-key-defaults", args=[OPENAI]), {"chat_model": "gpt-4", "background_model": "gpt-6-luna"})
    assert refused.url == reverse("settings-ai")
    assert AiProviderConnection.objects.get(pk=row.pk).chat_model == "gpt-6-astra"
    client.post(reverse("ai-key-disconnect", args=[OPENAI]))
    assert not AiProviderConnection.objects.filter(owner=person).exists()


@pytest.mark.django_db
def test_bad_keys_and_models_are_refused(provider):
    _user, person, _household = make_member("refuser")
    with pytest.raises(AiError):
        connect_api_key(person, kind=OPENAI, key="not a key")
    with pytest.raises(AiError):
        connect_api_key(person, kind=ANTHROPIC, key=OPENAI_KEY)
    with pytest.raises(AiError):
        connect_api_key(person, kind="other", key=OPENAI_KEY)
    connect_api_key(person, kind=OPENAI, key=OPENAI_KEY, chat_model="not-a-model")
    assert AiProviderConnection.objects.get(owner=person).chat_model == "gpt-6.1-sol"


@pytest.mark.django_db
def test_policy_acceptance_gates_the_key(provider):
    _user, person, _household = _member(ANTHROPIC)
    publish_policy(material=True, body="A newer synthetic policy that needs acceptance")
    person = Person.objects.get(pk=person.pk)
    assert not member_has_ai(person)
    result = run_structured(person, "Hi", feature="structured")
    assert not result.ok and result.failure_code == "authorization_required"
    assert provider.requests == []
    with pytest.raises(AiError):
        connect_api_key(person, kind=OPENAI, key=OPENAI_KEY)


@pytest.mark.django_db
def test_household_gating_applies_to_tools(provider):
    _user, person, household = _member(ANTHROPIC)
    _u, other, _h = make_member("slowpoke", household=household, policy=current_policy())
    account = checking(person, household, "Household Checking")
    add_txn(account, person, date(2026, 1, 3), -500, "Synthetic dining")
    newer = publish_policy(material=True, body="A newer synthetic policy that needs acceptance")
    accept_policy(person, newer)
    assert not may_use_ai(other)
    provider.script = [(200, anthropic_tool("list_accounts", {})), (200, anthropic_text("Done."))]
    ask(person, "List my accounts")
    result = provider.requests[1]["body"]["messages"][-1]["content"][0]["content"]
    assert "Household Checking" not in result


@pytest.mark.django_db
def test_background_job_uses_the_api_connection(provider):
    _user, person, _household = _member(OPENAI, chat=False, background=True)
    job = enqueue_job(person, feature="structured")
    assert job.backend == OPENAI
    assert process_due_jobs() == 1
    job.refresh_from_db()
    assert job.status == AiJob.Status.SUCCEEDED
    assert provider.requests[0]["body"]["model"] == "gpt-6-luna"
    assert AiUsageEvent.objects.get(member=person).provider == OPENAI


@pytest.mark.django_db
def test_a_member_can_mix_harness_and_api_connections(harness, provider):  # noqa: F811
    _state, url = harness
    _user, person, _household = make_member("mixer")
    connect_harness(person, base_url=url, token=TOKEN)
    connect_api_key(person, kind=ANTHROPIC, key=ANTHROPIC_KEY)
    assert AiProviderConnection.objects.filter(owner=person).count() == 2
    connection, backend = resolve_ai(person, use_chat=True)
    assert connection.kind == "agent_harness"
    set_api_defaults(person, kind=ANTHROPIC, chat_model="", background_model="", use_chat=True, use_background=False)
    connection, backend = resolve_ai(person, use_chat=True)
    assert (connection.kind, backend) == (ANTHROPIC, ANTHROPIC)
    connection, _backend = resolve_ai(person, use_chat=False)
    assert connection.kind == "agent_harness"
    # Picking a harness backend again takes the choice back from the API key.
    row = AiProviderConnection.objects.get(owner=person, kind="agent_harness")
    set_defaults(person, chat_backend=row.chat_backend or "claude", background_backend=row.background_backend or "local")
    connection, _backend = resolve_ai(person, use_chat=True)
    assert connection.kind == "agent_harness"
    # Disconnecting one kind leaves the other.
    disconnect_harness(person)
    assert AiProviderConnection.objects.filter(owner=person, kind=ANTHROPIC).exists()
    disconnect_api_key(person, ANTHROPIC)
    assert not AiProviderConnection.objects.filter(owner=person).exists()


@pytest.mark.django_db
def test_another_members_key_is_never_used(provider):
    _user, owner, household = _member(ANTHROPIC)
    _u, other, _h = make_member("other", household=household, policy=current_policy())
    assert resolve_ai(other, use_chat=True)[0] is None
    assert resolve_ai(other, use_chat=True, requested_backend=ANTHROPIC)[0] is None
    result = run_structured(other, "Hi", feature="structured", backend=ANTHROPIC)
    assert not result.ok
    assert provider.requests == []
    assert owner.pk != other.pk
