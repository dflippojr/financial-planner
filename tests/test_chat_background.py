"""Chat turns run in the background: the request stores a pending turn, the chat lane answers it."""

import json
import time
from datetime import date, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.ai_services import connect_harness
from finance.chat_runner import (
    ChatLane,
    claim_next_turn,
    heartbeat,
    process_pending_turns,
    recover_stale_turns,
    run_claimed_turn,
    stale_seconds,
    unclaimed_max_age_seconds,
)
from finance.chat_services import answer_turn, send_message
from finance.models import AiConversationMessage
from finance.policy_services import current_policy
from tests.test_chat import TOKEN, add_txn, checking, harness as harness_fixture, make_member  # noqa: F401 - harness is a fixture
from tests.test_security_headers import assert_page_is_csp_clean

PENDING = AiConversationMessage.Status.PENDING
NO_SLEEP = {"sleep": lambda _s: None}


def _pending_turn(conversation):
    return conversation.messages.get(status=PENDING)


def _connected_client(harness, username="owner"):
    state, url = harness
    user, person, household = make_member(username)
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(user)
    return state, client, person, household


@pytest.mark.django_db
def test_send_returns_at_once_while_the_harness_takes_a_minute(harness):
    state, client, person, _household = _connected_client(harness)
    state.session_create_delay = 60

    started = time.monotonic()
    response = client.post(reverse("chat-send"), {"prompt": "How much did I spend?", "next": reverse("chat")})
    elapsed = time.monotonic() - started

    assert response.status_code == 302
    assert elapsed < 1
    assert not [path for _method, path in state.requests if path.startswith("/api/v1/sessions")]
    turn = AiConversationMessage.objects.get(conversation__member=person, status=PENDING)
    assert turn.role == AiConversationMessage.Role.ASSISTANT
    assert turn.reply_to.content == "How much did I spend?"


@pytest.mark.django_db
def test_pending_turn_shows_thinking_and_the_reply_arrives_by_poll_and_reload(harness):
    state, client, person, _household = _connected_client(harness)
    state.session_answer = "Synthetic spending was steady."
    client.post(reverse("chat-send"), {"prompt": "Was spending steady?", "next": reverse("chat")})
    turn = AiConversationMessage.objects.get(conversation__member=person, status=PENDING)
    status_url = reverse("chat-turn", args=[turn.pk])

    page = client.get(reverse("chat"))
    body = page.content.decode()
    assert_page_is_csp_clean(page)
    assert "Thinking… refresh to see the answer." in body
    assert f'data-chat-turn-url="{status_url}"' in body
    assert "js/chat-turn.js" in body
    assert json.loads(client.get(status_url).content) == {"ok": True, "status": "pending"}

    assert process_pending_turns(**NO_SLEEP) == 1

    payload = json.loads(client.get(status_url).content)
    assert payload["status"] == "done"
    assert payload["role"] == "assistant"
    assert payload["content"] == "Synthetic spending was steady."
    reloaded = client.get(reverse("chat")).content.decode()
    assert "Synthetic spending was steady." in reloaded
    assert "data-chat-turn-url" not in reloaded


@pytest.mark.django_db
def test_turn_status_is_only_visible_to_the_member_who_asked(harness):
    state, client, person, household = _connected_client(harness)
    client.post(reverse("chat-send"), {"prompt": "Private question", "next": reverse("chat")})
    turn = AiConversationMessage.objects.get(conversation__member=person, status=PENDING)
    other_user, _other, _ = make_member("other", household=household, policy=current_policy())
    other_client = Client()
    other_client.force_login(other_user)

    assert other_client.get(reverse("chat-turn", args=[turn.pk])).status_code == 404
    assert other_client.get(reverse("chat-turn", args=[turn.reply_to_id])).status_code == 404


@pytest.mark.django_db
def test_background_tool_calls_see_only_the_asking_members_records(harness):
    state, url = harness
    _user_a, alpha, household = make_member("alpha")
    _user_b, beta, _ = make_member("beta", household=household, policy=current_policy())
    shared = checking(alpha, household, "Shared Checking")
    private_beta = checking(beta, household, "Beta Private Savings", private=True)
    add_txn(shared, alpha, date(2026, 1, 3), -500, "Synthetic shared rent")
    add_txn(private_beta, beta, date(2026, 1, 2), -999, "Beta private pharmacy")
    connect_harness(alpha, base_url=url, token=TOKEN)
    state.need_tool = True
    state.session_answer = "Done."
    state.pending_tool_calls = [
        [{"call_id": "c1", "name": "list_accounts", "args": {}}],
        [
            {
                "call_id": "c2",
                "name": "search_transactions",
                "args": {"q": "pharmacy", "date_from": "2026-01-01", "date_to": "2026-01-31"},
            }
        ],
        [{"call_id": "c3", "name": "cash_flow_totals", "args": {"account_name": "Beta Private Savings"}}],
    ]

    conversation = send_message(alpha, "What accounts and pharmacy spending do we have?")
    process_pending_turns(**NO_SLEEP)

    outputs = "\n".join(state.tool_outputs)
    assert len(state.tool_outputs) == 3
    assert "Shared Checking" in outputs
    assert "Beta Private Savings" not in outputs
    assert "Beta private pharmacy" not in outputs
    conversation.refresh_from_db()
    assert private_beta.pk not in conversation.used_account_ids
    reply = conversation.messages.filter(role=AiConversationMessage.Role.ASSISTANT).last()
    assert reply.status == AiConversationMessage.Status.DONE


@pytest.mark.django_db
def test_a_runner_killed_mid_turn_leaves_no_turn_pending_after_the_cutoff(harness):
    state, client, person, _household = _connected_client(harness)
    conversation = send_message(person, "Will this finish?")
    turn = _pending_turn(conversation)
    claimed_at = timezone.now()
    assert claim_next_turn("dead-runner", now=claimed_at) == turn.pk
    # The runner dies here: no more heartbeats and no reply.

    assert recover_stale_turns(now=claimed_at + timedelta(seconds=stale_seconds() - 1)) == 0
    assert recover_stale_turns(now=claimed_at + timedelta(seconds=stale_seconds() + 1)) == 1

    turn.refresh_from_db()
    assert turn.status == AiConversationMessage.Status.FAILED
    assert turn.role == AiConversationMessage.Role.ERROR
    assert turn.content == "The AI backend is unavailable."
    assert not AiConversationMessage.objects.filter(status=PENDING).exists()
    # A runner that comes back late cannot overwrite the failure.
    assert run_claimed_turn(turn.pk, "dead-runner", **NO_SLEEP) is False
    assert state.session_creates == []
    payload = json.loads(client.get(reverse("chat-turn", args=[turn.pk])).content)
    assert payload["status"] == "failed"
    assert payload["role"] == "error"


@pytest.mark.django_db
def test_live_runner_heartbeats_keep_a_long_turn_from_going_stale(harness):
    _state, _client, person, _household = _connected_client(harness)
    turn = _pending_turn(send_message(person, "A long answer"))
    start = timezone.now()
    claim_next_turn("live-runner", now=start)
    later = start + timedelta(seconds=stale_seconds() * 5)
    assert heartbeat("live-runner", now=later - timedelta(seconds=1)) == 1
    assert recover_stale_turns(now=later) == 0
    turn.refresh_from_db()
    assert turn.status == PENDING


@pytest.mark.django_db
def test_turn_no_runner_picks_up_fails_when_the_page_polls_after_the_cutoff(harness):
    _state, client, person, _household = _connected_client(harness)
    turn = _pending_turn(send_message(person, "Is anyone there?"))
    AiConversationMessage.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - timedelta(seconds=unclaimed_max_age_seconds() + 5)
    )

    payload = json.loads(client.get(reverse("chat-turn", args=[turn.pk])).content)

    assert payload["status"] == "failed"
    assert payload["content"] == "The AI backend is unavailable."


@pytest.mark.django_db
def test_harness_failure_ends_the_turn_failed_with_todays_message(harness):
    state, _client, person, _household = _connected_client(harness)
    state.session_failure = "provider_error"
    turn = _pending_turn(send_message(person, "Fail please"))
    process_pending_turns(**NO_SLEEP)
    turn.refresh_from_db()
    assert turn.status == AiConversationMessage.Status.FAILED
    assert turn.role == AiConversationMessage.Role.ERROR
    assert turn.content == "The AI backend could not complete that question."


@pytest.mark.django_db
def test_turn_fails_when_the_member_disconnects_before_the_runner_starts(harness):
    from finance.ai_services import disconnect_harness

    state, _client, person, _household = _connected_client(harness)
    turn = _pending_turn(send_message(person, "Still connected?"))
    disconnect_harness(person)
    process_pending_turns(**NO_SLEEP)
    turn.refresh_from_db()
    assert turn.status == AiConversationMessage.Status.FAILED
    assert turn.content == "Connect an AI backend first."
    assert state.session_creates == []


@pytest.mark.django_db
def test_unexpected_runner_error_fails_the_turn_instead_of_leaving_it_pending(harness, monkeypatch):
    _state, _client, person, _household = _connected_client(harness)
    turn = _pending_turn(send_message(person, "Break it"))

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("finance.chat_runner.answer_turn", boom)
    process_pending_turns(**NO_SLEEP)
    turn.refresh_from_db()
    assert turn.status == AiConversationMessage.Status.FAILED
    assert turn.content == "The AI backend could not complete that question."


@pytest.mark.django_db
def test_deleting_the_conversation_mid_turn_drops_the_reply(harness, monkeypatch):
    _state, _client, person, _household = _connected_client(harness)
    conversation = send_message(person, "Delete me while you think")
    turn = _pending_turn(conversation)
    pk = claim_next_turn("runner")

    def delete_then_answer(item, **kwargs):
        conversation.delete()
        return answer_turn(item, **kwargs)

    monkeypatch.setattr("finance.chat_runner.answer_turn", delete_then_answer)
    assert run_claimed_turn(pk, "runner", **NO_SLEEP) is False
    assert not AiConversationMessage.objects.filter(pk=turn.pk).exists()


class _InlineExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append(args)
        fn(*args)


@pytest.mark.django_db
def test_chat_lane_tick_claims_runs_and_heartbeats(harness, settings):
    state, _client, person, _household = _connected_client(harness)
    settings.AI_CHAT_WORKERS = 2
    state.session_answer = "Lane answer."
    first = send_message(person, "First lane question")
    executor = _InlineExecutor()
    lane = ChatLane(executor=executor)

    lane.tick()

    assert len(executor.submitted) == 1
    reply = first.messages.filter(role=AiConversationMessage.Role.ASSISTANT).last()
    assert reply.status == AiConversationMessage.Status.DONE
    assert reply.content == "Lane answer."
    lane.tick()
    assert len(executor.submitted) == 1


class _StopAfter:
    """A stop event that lets run_forever loop a set number of times."""

    def __init__(self, loops):
        self.loops = loops
        self.waits = []

    def is_set(self):
        return self.loops <= 0

    def wait(self, seconds):
        self.waits.append(seconds)
        self.loops -= 1


@pytest.mark.django_db
def test_chat_lane_keeps_going_after_a_failed_turn_or_poll(harness, monkeypatch, settings):
    _state, _client, person, _household = _connected_client(harness)
    settings.AI_CHAT_POLL_SECONDS = 1
    send_message(person, "This one breaks the runner")
    lane = ChatLane(executor=_InlineExecutor(), workers=1)

    def broken_run(*_args, **_kwargs):
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr("finance.chat_runner.run_claimed_turn", broken_run)
    lane.tick()
    assert lane._busy() == 0

    def broken_tick(**_kwargs):
        raise RuntimeError("synthetic poll failure")

    monkeypatch.setattr(lane, "tick", broken_tick)
    stop = _StopAfter(2)
    lane.run_forever(stop)
    assert stop.waits == [1.0, 1.0]


def test_chat_lane_defaults_to_its_own_worker_pool(settings):
    settings.AI_CHAT_WORKERS = 3
    lane = ChatLane()
    try:
        assert lane.workers == 3
        assert lane._pool_threads is True
    finally:
        lane.executor.shutdown(wait=False)


@pytest.mark.django_db
def test_turn_status_returns_figures_with_local_links_only_and_notices(harness):
    _state, client, person, _household = _connected_client(harness)
    turn = _pending_turn(send_message(person, "Show figures"))
    AiConversationMessage.objects.filter(pk=turn.pk).update(
        status=AiConversationMessage.Status.DONE,
        content="Here you go.",
        figures=[
            {"label": "Total spending", "amount_display": "$12.00", "url": "/spending/?date_from=2026-01-01"},
            {"label": "Elsewhere", "amount_display": "$1.00", "url": "https://evil.example/"},
            {"label": "Protocol relative", "amount_display": "$2.00", "url": "//evil.example/"},
            "not a figure",
        ],
        notices=["Synthetic usage notice"],
    )

    payload = json.loads(client.get(reverse("chat-turn", args=[turn.pk])).content)

    assert [item["url"] for item in payload["figures"]] == ["/spending/?date_from=2026-01-01", "", ""]
    assert payload["figures"][0] == {
        "url": "/spending/?date_from=2026-01-01",
        "label": "Total spending",
        "amount_display": "$12.00",
    }
    assert payload["notices"] == ["Synthetic usage notice"]


@pytest.mark.django_db
def test_a_follow_up_queued_behind_a_long_live_turn_is_not_failed(harness):
    _state, client, person, _household = _connected_client(harness)
    conversation = send_message(person, "A long first question")
    send_message(person, "A quick follow-up", conversation_id=conversation.pk)
    start = timezone.now()
    first = claim_next_turn("live-runner", now=start)
    waiting = conversation.messages.filter(status=PENDING).exclude(pk=first).get()
    later = start + timedelta(seconds=unclaimed_max_age_seconds() + 30)
    heartbeat("live-runner", now=later)

    assert recover_stale_turns(now=later) == 0
    waiting.refresh_from_db()
    assert waiting.status == PENDING

    # Once that runner is gone too, both turns end instead of waiting forever.
    gone = later + timedelta(seconds=stale_seconds() + 1)
    assert recover_stale_turns(now=gone) == 2
