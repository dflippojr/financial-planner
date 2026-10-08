"""Members link their own Claude or Codex plan through Agent Harness end-user logins (fake harness)."""

import json
import logging

import pytest
from django.test import Client
from django.urls import reverse
from tests.chat_helpers import ask
from tests.helpers import stamp_recent_auth
from tests.test_chat import TOKEN, harness as harness_fixture, make_member  # noqa: F401 - harness is a fixture
from tests.test_security_headers import assert_page_is_csp_clean

from finance.ai_jobs import enqueue_job, process_due_jobs
from finance.ai_plan import LINK_PROMPT, end_user_id, set_offer_plan_links
from finance.ai_services import connect_harness, run_structured
from finance.ai_types import AUTHORIZATION_REQUIRED, LOGIN_REQUIRED
from finance.models import AiJob, AiPlanLink
from finance.policy_services import current_policy

CANARY = "canary-code-7f3a9d"


def _household(harness):
    _state, url = harness
    host_user, host, household = make_member("host")
    guest_user, guest, _ = make_member("guest", household=household, policy=current_policy())
    connect_harness(host, base_url=url, token=TOKEN)
    set_offer_plan_links(host, True)
    client = Client()
    client.force_login(guest_user)
    stamp_recent_auth(client)
    return host, guest, client


def _post(client, name, backend, payload=None):
    return client.post(
        reverse(name, args=[backend]),
        data=json.dumps(payload or {}),
        content_type="application/json",
    )


def _link_claude(client):
    started = _post(client, "ai-plan-start", "claude").json()
    assert started["ok"] and started["needs_code"] and started["verification_url"].startswith("https://")
    assert not started["user_code"]
    code = _post(client, "ai-plan-code", "claude", {"attempt_id": started["attempt_id"], "code": CANARY})
    assert code.json() == {"ok": True}
    status = client.get(reverse("ai-plan-status", args=["claude"])).json()
    assert status["linked"] is True
    return started


@pytest.mark.django_db
def test_claude_link_url_code_then_linked(harness):
    state, _url = harness
    _host, guest, client = _household(harness)
    _link_claude(client)
    assert state.login_codes == [CANARY]
    assert AiPlanLink.objects.get(person=guest, backend="claude").connection.owner.display_name.startswith("Host")
    # The harness only ever sees the opaque id, never a username or name.
    (end_user, backend), = state.login_starts
    assert end_user == end_user_id(guest) and backend == "claude"
    assert "guest" not in end_user and "Guest" not in end_user


@pytest.mark.django_db
def test_codex_link_shows_user_code_and_polls_to_linked(harness):
    state, _url = harness
    _host, guest, client = _household(harness)
    started = _post(client, "ai-plan-start", "codex").json()
    assert started["user_code"] == "ABCD-1234" and not started["needs_code"]
    assert client.get(reverse("ai-plan-status", args=["codex"])).json()["linked"] is True
    assert AiPlanLink.objects.filter(person=guest, backend="codex").exists()


@pytest.mark.django_db
def test_pending_login_is_not_linked_and_ended_attempt_is_reported(harness):
    state, _url = harness
    _host, guest, client = _household(harness)
    state.codex_polls_to_link = 99
    _post(client, "ai-plan-start", "codex")
    assert client.get(reverse("ai-plan-status", args=["codex"])).json() == {"ok": True, "linked": False, "failed": False}
    state.end_user_logins[(end_user_id(guest), "codex")]["attempt"]["status"] = "expired"
    assert client.get(reverse("ai-plan-status", args=["codex"])).json()["failed"] is True
    assert not AiPlanLink.objects.exists()


@pytest.mark.django_db
def test_start_needs_a_fresh_sign_in_and_hands_back_the_reauth_address(harness):
    state, _url = harness
    _host, _guest, _client = _household(harness)
    stale = Client()
    stale.force_login(_guest.user)
    response = _post(stale, "ai-plan-start", "claude")
    assert response.status_code == 403
    assert "/reauth/" in response.json()["reauth"]
    assert state.login_starts == []
    assert _post(stale, "ai-plan-code", "claude", {"attempt_id": "x", "code": CANARY}).status_code == 403


@pytest.mark.django_db
def test_guest_without_host_offer_cannot_start(harness):
    state, url = harness
    _host_user, _host, household = make_member("host2")
    guest_user, _guest, _ = make_member("guest2", household=household, policy=current_policy())
    client = Client()
    client.force_login(guest_user)
    stamp_recent_auth(client)
    response = _post(client, "ai-plan-start", "claude")
    assert response.json()["ok"] is False
    assert state.login_starts == []


@pytest.mark.django_db
def test_pasted_code_never_stored_logged_or_echoed(harness, caplog):
    state, _url = harness
    _host, guest, client = _household(harness)
    caplog.set_level(logging.DEBUG)
    started = _post(client, "ai-plan-start", "claude").json()
    good = _post(client, "ai-plan-code", "claude", {"attempt_id": started["attempt_id"], "code": CANARY})
    bad_code = "bad-" + CANARY
    bad = _post(client, "ai-plan-code", "claude", {"attempt_id": started["attempt_id"], "code": bad_code})
    assert bad.status_code == 400
    for response in (good, bad, client.get(reverse("ai-plan-status", args=["claude"]))):
        assert CANARY not in response.content.decode()
    assert CANARY not in caplog.text
    ask(guest, "How am I doing?")
    from django.core import serializers
    from django.apps import apps

    dump = ""
    for model in apps.get_app_config("finance").get_models():
        dump += serializers.serialize("json", model.objects.all())
    assert CANARY not in dump
    assert CANARY not in " ".join(state.session_prompts)


@pytest.mark.django_db
def test_linked_member_chat_sends_end_user_and_never_the_owner_login(harness):
    state, _url = harness
    host, guest, client = _household(harness)
    _link_claude(client)
    conversation = ask(guest, "How am I doing?")
    reply = conversation.messages.filter(role="assistant").first()
    assert reply.status == "done" and reply.backend == "claude"
    create = state.session_creates[-1]
    assert create["end_user"] == end_user_id(guest)
    assert create["backend"] == "claude"
    assert end_user_id(guest) != end_user_id(host)
    # The owner's own use carries no end_user.
    connect_owner = ask(host, "Hello")
    assert connect_owner.messages.filter(role="assistant").exists()
    assert "end_user" not in state.session_creates[-1]


@pytest.mark.django_db
def test_login_required_shows_link_prompt_and_never_falls_back(harness):
    state, _url = harness
    host, guest, client = _household(harness)
    _link_claude(client)
    state.end_user_logins.clear()  # the harness lost the login
    conversation = ask(guest, "How am I doing?")
    reply = conversation.messages.filter(role="error").first()
    assert reply.status == "failed" and reply.content == LINK_PROMPT
    assert AiPlanLink.objects.get(person=guest, backend="claude").needs_login is True
    assert all(body.get("end_user") == end_user_id(guest) for body in state.session_creates)
    page = client.get(reverse("settings-ai"))
    assert "Link your plan to use this." in page.content.decode()


@pytest.mark.django_db
def test_unlink_removes_link_and_stops_hosted_use(harness):
    state, _url = harness
    _host, guest, client = _household(harness)
    _link_claude(client)
    response = client.post(reverse("ai-plan-unlink", args=["claude"]))
    assert response.status_code == 302
    assert not AiPlanLink.objects.exists()
    assert state.login_deletes == [(end_user_id(guest), "claude")]
    before = len(state.session_creates)
    result = run_structured(guest, "synthetic", feature="structured", backend="claude")
    assert not result.ok and result.failure_code == AUTHORIZATION_REQUIRED
    assert len(state.session_creates) == before


@pytest.mark.django_db
def test_plan_wins_over_api_key_and_settings_say_so(harness):
    _state, _url = harness
    from finance.ai_services import connect_api_key, set_api_defaults
    from finance.ai_types import ANTHROPIC_API, HOSTED_BACKENDS  # noqa: F401
    from finance.ai_services import resolve_ai

    _host, guest, client = _household(harness)
    connect_api_key(guest, kind=ANTHROPIC_API, key="sk-ant-synthetic-key-0000000000000000")
    set_api_defaults(
        guest, kind=ANTHROPIC_API, chat_model="", background_model="", use_chat=True, use_background=True
    )
    assert resolve_ai(guest, use_chat=True)[1] == ANTHROPIC_API
    _link_claude(client)
    connection, backend = resolve_ai(guest, use_chat=True)
    assert backend == "claude" and connection.owner_id != guest.pk
    assert "not your API key" in client.get(reverse("settings-ai")).content.decode()


@pytest.mark.django_db
def test_background_job_runs_on_member_plan(harness, settings):
    state, _url = harness
    settings.AGENT_HARNESS_HOSTED_SESSIONS = True
    _host, guest, client = _household(harness)
    _link_claude(client)
    job = enqueue_job(guest, feature="structured", backend="claude")
    assert process_due_jobs() == 1
    job.refresh_from_db()
    assert job.status == AiJob.Status.SUCCEEDED
    assert state.session_creates[-1]["end_user"] == end_user_id(guest)


@pytest.mark.django_db
def test_background_job_login_required_fails_without_retry(harness, settings):
    state, _url = harness
    settings.AGENT_HARNESS_HOSTED_SESSIONS = True
    _host, guest, client = _household(harness)
    _link_claude(client)
    state.end_user_logins.clear()
    job = enqueue_job(guest, feature="structured", backend="claude")
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == AiJob.Status.FAILED and job.failure_code == LOGIN_REQUIRED


@pytest.mark.django_db
def test_turning_off_the_offer_drops_member_links(harness):
    _state, _url = harness
    host, _guest, client = _household(harness)
    _link_claude(client)
    set_offer_plan_links(host, False)
    assert not AiPlanLink.objects.exists()


@pytest.mark.django_db
def test_settings_page_with_plan_popup_is_csp_clean(harness):
    _host, _guest, client = _household(harness)
    response = client.get(reverse("settings-ai"))
    assert_page_is_csp_clean(response)
    html = response.content.decode()
    assert 'src="/static/js/ai-plan-link.js"' in html
    assert "plan-link-dialog" in html and "Your Claude / Codex plan" in html
