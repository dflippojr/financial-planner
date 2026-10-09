"""Per-member quotas, fairness and deadlines on the shared AI and chat lanes (#306)."""

from concurrent.futures import Future
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance import ai_harness, ai_jobs
from finance.ai_jobs import enqueue_review_phrasing
from finance.ai_services import AiError, connect_harness
from finance.ai_types import UNAVAILABLE
from finance.chat_runner import claim_next_turn, heartbeat, recover_stale_turns, turn_max_seconds
from finance.chat_services import CONVERSATION_LIMIT, PENDING_LIMIT, send_message, start_conversation
from finance.models import AiConversation, AiConversationMessage, AiJob, MonthlyReview
from finance.monthly_review import latest_closed_month, store_monthly_review
from finance.policy_services import current_policy
from finance.months import add_months
from tests.test_chat import TOKEN, harness as harness_fixture, make_member  # noqa: F401 - harness is a fixture
from tests.test_monthly_review_ai import connect_ai, make_account, make_household, make_person, make_transaction

PENDING = AiConversationMessage.Status.PENDING
ACTIVE = (AiJob.Status.QUEUED, AiJob.Status.WAITING_MODEL, AiJob.Status.RUNNING)


def _connected(harness, username):
    _state, url = harness
    # Every member accepts the one current policy; a second publish would void the first acceptance.
    user, person, _household = make_member(username, policy=current_policy(create_if_missing=False))
    connect_harness(person, base_url=url, token=TOKEN)
    return user, person


class _FakeSession:
    """A waiting_app session whose single pending batch holds `size` synthetic calls."""

    def __init__(self, size):
        self.calls = [{"call_id": f"call-{index}", "name": "synthetic_tool", "args": {}} for index in range(size)]
        self.answers = []

    def request(self, url, *, token=None, method="GET", body=None):
        if url.endswith("tool_calls?status=pending"):
            answered = {call_id for call_id, _body in self.answers}
            return [call for call in self.calls if call["call_id"] not in answered]
        if "/tool_calls/" in url:
            self.answers.append((url.rsplit("/", 1)[-1], body))
            return {}
        return {"id": "synthetic-session", "status": "waiting_app"}


def test_session_stops_tool_calls_once_the_deadline_passes(monkeypatch, settings):
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 60
    session = _FakeSession(25)
    monkeypatch.setattr(ai_harness, "json_request", session.request)
    clock = {"now": 0.0}
    ran = []

    def runner(name, _args):
        ran.append(name)
        clock["now"] = 61.0  # The first call outlasts the whole session budget.
        return "synthetic output", True

    result = ai_harness.wait_for_session(
        "http://harness.invalid", "token", "synthetic-session",
        {"id": "synthetic-session", "status": "waiting_app"},
        tool_runner=runner, sleep=lambda _s: None, monotonic=lambda: clock["now"],
    )

    assert len(ran) == 1
    assert len(session.answers) == 1
    assert not result.ok and result.failure_code == UNAVAILABLE and result.session_open


def test_a_batch_larger_than_the_tool_budget_runs_only_the_budget(monkeypatch):
    session = _FakeSession(10)
    monkeypatch.setattr(ai_harness, "json_request", session.request)
    ran = []

    def runner(name, _args):
        ran.append(name)
        return "synthetic output", True

    answered = ai_harness._answer_tool_calls(
        "http://harness.invalid", "token", "synthetic-session", runner, tool_budget=lambda: 3,
    )

    assert len(ran) == 3
    # Every call still gets an answer, so the session can finish instead of waiting.
    assert answered == 10
    refused = [body for _call, body in session.answers if not body["ok"]]
    assert len(refused) == 7
    assert all(body["output"] == ai_harness.TOOL_BUDGET_SPENT for body in refused)


@pytest.mark.django_db
def test_chat_turn_passes_the_conversations_remaining_tool_budget(harness, monkeypatch, settings):
    settings.AI_CHAT_MAX_TOOL_CALLS = 10
    _user, person = _connected(harness, "budget")
    conversation = send_message(person, "How much did I spend?")
    AiConversation.objects.filter(pk=conversation.pk).update(tool_call_count=8)
    seen = {}

    def fake_run(*_args, tool_budget=None, **_kwargs):
        from finance.ai_types import ProviderResult

        seen["remaining"] = tool_budget()
        return ProviderResult(ok=True, answer="Synthetic answer.")

    from finance import chat_services

    monkeypatch.setattr(chat_services, "run_conversation", fake_run)
    turn = conversation.messages.get(status=PENDING)
    chat_services.answer_turn(turn)
    assert seen["remaining"] == 2


@pytest.mark.django_db
def test_a_turn_past_its_deadline_fails_while_the_runner_heartbeat_is_fresh(harness):
    _user, person = _connected(harness, "deadline")
    conversation = send_message(person, "A very long question")
    start = timezone.now()
    pk = claim_next_turn("live-runner", now=start)
    turn = AiConversationMessage.objects.get(pk=pk)
    assert turn.deadline_at == start + timedelta(seconds=turn_max_seconds())

    later = start + timedelta(seconds=turn_max_seconds() + 1)
    heartbeat("live-runner", now=later)
    assert recover_stale_turns(now=later) == 1
    turn.refresh_from_db()
    assert turn.status == AiConversationMessage.Status.FAILED
    assert conversation.messages.filter(status=PENDING).count() == 0


@pytest.mark.django_db
def test_a_turn_inside_its_deadline_is_left_running(harness):
    _user, person = _connected(harness, "inside")
    send_message(person, "A question")
    start = timezone.now()
    pk = claim_next_turn("live-runner", now=start)
    later = start + timedelta(seconds=turn_max_seconds() - 1)
    heartbeat("live-runner", now=later)
    assert recover_stale_turns(now=later) == 0
    assert AiConversationMessage.objects.get(pk=pk).status == PENDING


@pytest.mark.django_db
def test_a_send_beyond_the_pending_cap_is_refused_without_a_row(harness, settings):
    settings.AI_CHAT_MAX_PENDING_PER_MEMBER = 2
    _user, person = _connected(harness, "pending")
    first = send_message(person, "First question")
    second = start_conversation(person)
    send_message(person, "Second question", conversation_id=second.pk)
    rows = AiConversationMessage.objects.filter(conversation__member=person).count()

    with pytest.raises(AiError) as refused:
        send_message(person, "Third question", conversation_id=first.pk)

    assert str(refused.value) == PENDING_LIMIT
    assert AiConversationMessage.objects.filter(conversation__member=person).count() == rows
    assert AiConversationMessage.objects.filter(conversation__member=person, status=PENDING).count() == 2


@pytest.mark.django_db
def test_another_members_pending_turns_do_not_count(harness, settings):
    settings.AI_CHAT_MAX_PENDING_PER_MEMBER = 1
    _user, person = _connected(harness, "busy")
    _other_user, other = _connected(harness, "quiet")
    send_message(person, "Busy question")
    send_message(other, "Quiet question")
    assert AiConversationMessage.objects.filter(status=PENDING).count() == 2


@pytest.mark.django_db
def test_a_conversation_beyond_the_cap_is_refused(harness, settings):
    settings.AI_CHAT_MAX_CONVERSATIONS = 2
    user, person = _connected(harness, "chatty")
    start_conversation(person)
    start_conversation(person)

    with pytest.raises(AiError) as refused:
        start_conversation(person)
    assert str(refused.value) == CONVERSATION_LIMIT

    client = Client()
    client.force_login(user)
    response = client.post(reverse("chat-new"), follow=True)
    assert CONVERSATION_LIMIT in response.content.decode()
    assert AiConversation.objects.filter(member=person).count() == 2


@pytest.mark.django_db
def test_expired_conversations_do_not_count_toward_the_cap(harness, settings):
    settings.AI_CHAT_MAX_CONVERSATIONS = 1
    _user, person = _connected(harness, "expired")
    old = start_conversation(person)
    AiConversation.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert start_conversation(person).pk != old.pk


@pytest.mark.django_db
def test_chat_claims_reach_the_quiet_member_within_the_worker_count(harness, settings):
    settings.AI_CHAT_MAX_PENDING_PER_MEMBER = 20
    settings.AI_CHAT_WORKERS = 4
    _user, busy = _connected(harness, "busy")
    _other_user, quiet = _connected(harness, "quiet")
    for index in range(8):
        conversation = start_conversation(busy)
        send_message(busy, f"Busy question {index}", conversation_id=conversation.pk)
    send_message(quiet, "Quiet question")
    quiet_turn = AiConversationMessage.objects.get(conversation__member=quiet, status=PENDING)

    claimed = [claim_next_turn("runner") for _ in range(settings.AI_CHAT_WORKERS)]

    assert quiet_turn.pk in claimed


@pytest.mark.django_db
def test_job_lane_reaches_the_quiet_member_within_the_worker_count(monkeypatch):
    _user, busy, _household = make_member("busy-jobs")
    _other_user, quiet, _other_household = make_member("quiet-jobs")
    for _ in range(10):
        AiJob.objects.create(member=busy, feature="structured")
    quiet_job = AiJob.objects.create(member=quiet, feature="structured")
    lane = ai_jobs.AiJobLane()
    submitted = []

    def submit(_run, pk, _moment):
        submitted.append(pk)
        return Future()

    try:
        monkeypatch.setattr(lane.executor, "submit", submit)
        lane.tick()
    finally:
        lane.close()

    assert len(submitted) == lane.workers
    assert quiet_job.pk in submitted


def _review_member(harness, username="owner"):
    _state, url = harness
    owner = make_person(username)
    make_household(owner)
    connect_ai(owner, url)
    checking = make_account(owner)
    make_transaction(owner, checking, transaction_date=latest_closed_month())
    store_monthly_review(owner, latest_closed_month())
    return owner


@pytest.fixture
def review_harness():
    from tests.fake_harness import start_fake_harness

    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.django_db
def test_repeated_regenerates_leave_one_active_phrasing_job(review_harness):
    owner = _review_member(review_harness)
    client = Client()
    client.force_login(owner.user)
    month = latest_closed_month().isoformat()[:7]
    for _ in range(4):
        assert client.post(reverse("monthly-review-regenerate"), {"month": month}).status_code == 302

    review = MonthlyReview.objects.get(person=owner, month=latest_closed_month())
    active = AiJob.objects.filter(member=owner, feature="monthly_review", status__in=ACTIVE)
    assert active.count() == 1
    # The surviving job phrases the newest generation, not the first one.
    assert active.get().input_refs["generated_at"] == review.generated_at.isoformat()


@pytest.mark.django_db
def test_unusual_phrasing_coalesces_per_review(review_harness):
    owner = _review_member(review_harness)
    review = MonthlyReview.objects.get(person=owner, month=latest_closed_month())
    first = enqueue_review_phrasing(owner, feature="unusual_spending", review=review)
    again = enqueue_review_phrasing(owner, feature="unusual_spending", review=review)
    assert first.pk == again.pk
    assert AiJob.objects.filter(member=owner, feature="unusual_spending", status__in=ACTIVE).count() == 1


@pytest.mark.django_db
def test_regenerate_while_phrasing_runs_queues_the_newest_generation_after(review_harness, monkeypatch):
    state, _url = review_harness
    owner = _review_member(review_harness)
    month = latest_closed_month()
    state.session_answer = "A synthetic summary."
    from finance import monthly_review_ai

    real_run = monthly_review_ai.run_structured

    def regenerate_mid_call(*args, **kwargs):
        result = real_run(*args, **kwargs)
        store_monthly_review(owner, month, force=True)
        # The running job absorbs the regenerate instead of a second job starting.
        assert AiJob.objects.filter(member=owner, feature="monthly_review", status__in=ACTIVE).count() == 1
        return result

    monkeypatch.setattr(monthly_review_ai, "run_structured", regenerate_mid_call)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    job = AiJob.objects.get(member=owner, feature="monthly_review")
    ai_jobs._process_one(job, timezone.now())

    review = MonthlyReview.objects.get(person=owner, month=month)
    follow_up = AiJob.objects.get(member=owner, feature="monthly_review", status=AiJob.Status.QUEUED)
    assert follow_up.pk != job.pk
    assert follow_up.input_refs["generated_at"] == review.generated_at.isoformat()


@pytest.mark.django_db
def test_member_phrasing_queue_is_bounded(review_harness, settings):
    settings.AI_JOB_MAX_QUEUED_PER_MEMBER = 2
    owner = _review_member(review_harness)
    closed = latest_closed_month()
    for back in range(1, 4):
        month = add_months(closed, -back)
        account = make_account(owner, name=f"Synthetic {back}")
        make_transaction(owner, account, amount_minor=-100 * back, description=f"Synthetic {back}", transaction_date=month)
        store_monthly_review(owner, month)
    phrasing = AiJob.objects.filter(member=owner, feature__in=("monthly_review", "unusual_spending"), status__in=ACTIVE)
    assert phrasing.count() == 2


@pytest.mark.django_db
def test_a_month_before_the_earliest_transaction_falls_back_to_the_default(review_harness):
    owner = _review_member(review_harness)
    closed = latest_closed_month()
    client = Client()
    client.force_login(owner.user)

    page = client.get(reverse("monthly-review") + "?month=2001-01")
    assert page.context["month"] == closed
    assert page.context["previous_url"] is None

    response = client.post(reverse("monthly-review-regenerate"), {"month": "2001-01"})
    assert response["Location"].endswith(f"month={closed.isoformat()[:7]}")
    assert not MonthlyReview.objects.filter(person=owner, month__lt=closed).exists()
