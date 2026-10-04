from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness
from tests.helpers import stamp_recent_auth

from finance.ai_jobs import enqueue_job, process_due_jobs
from finance.ai_services import (
    AiError,
    connect_harness,
    member_has_ai,
    run_conversation,
    run_structured,
    set_offer_local_to_household,
    set_shared_local_use,
)
from finance.ai_tools import default_tools, list_accounts
from finance.ai_types import AUTHORIZATION_REQUIRED, LIMIT_REACHED, LOCAL_BACKEND, UNAVAILABLE
from finance.chat_services import send_message
from finance.models import Account, AiJob, AiUsageEvent, Household, ImportBatch, Membership, Person, Transaction
from finance.policy_services import accept_policy, current_policy, publish_policy


PASSWORD = "Synthetic-passphrase-42!"
TOKEN = "ha-synthetic-app-token"


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


def _household_pair(harness):
    _state, url = harness
    host_user, host, household = make_member("host")
    guest_user, guest, _ = make_member("guest", household=household, policy=current_policy())
    connect_harness(host, base_url=url, token=TOKEN)
    return host_user, host, guest_user, guest, household


@pytest.mark.django_db
def test_guest_chat_allowed_only_when_host_offers_local(harness):
    state, _url = harness
    _host_user, host, _guest_user, guest, _household = _household_pair(harness)
    with pytest.raises(AiError) as hidden:
        send_message(guest, "What can I see?", sleep=lambda _s: None)
    assert hidden.value.failure_code == AUTHORIZATION_REQUIRED
    before = list(state.requests)
    result = run_conversation(guest, "Hi", feature="chat", backend=LOCAL_BACKEND, tools=default_tools())
    assert not result.ok
    assert result.failure_code == AUTHORIZATION_REQUIRED
    assert state.requests == before
    with pytest.raises(AiError):
        set_shared_local_use(guest, chat=True, background=True)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=True, background=True)
    conversation = send_message(guest, "What can I see?", sleep=lambda _s: None)
    assert conversation.member_id == guest.pk
    assert conversation.backend == LOCAL_BACKEND
    assert conversation.messages.filter(role="assistant").exists()
    assert member_has_ai(guest)
    set_offer_local_to_household(host, False)
    with pytest.raises(AiError) as stopped:
        send_message(guest, "Still there?", sleep=lambda _s: None)
    assert stopped.value.failure_code in {AUTHORIZATION_REQUIRED, UNAVAILABLE}


@pytest.mark.django_db
def test_guest_cannot_use_hosted_backend_on_host_connection(harness):
    _state, _url = harness
    _host_user, host, _guest_user, guest, _household = _household_pair(harness)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=True, background=True)
    result = run_conversation(guest, "Hi", feature="chat", backend="claude", tools=default_tools())
    assert not result.ok
    assert result.failure_code == AUTHORIZATION_REQUIRED
    structured = run_structured(guest, "synthetic", feature="structured", backend="claude")
    assert not structured.ok
    assert structured.failure_code == AUTHORIZATION_REQUIRED


@pytest.mark.django_db
def test_guest_tools_hide_host_private_data(harness):
    _state, _url = harness
    _host_user, host, _guest_user, guest, household = _household_pair(harness)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=True, background=True)
    Account.objects.create(
        name="Host Private",
        account_type=Account.Type.CHECKING,
        owner=host,
        scope=Account.Scope.PRIVATE,
    )
    shared = Account.objects.create(
        name="Shared Checking",
        account_type=Account.Type.CHECKING,
        owner=host,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    batch = ImportBatch.objects.create(
        account=shared,
        imported_by=host,
        source="huntington",
        source_file_sha256="e" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    Transaction.objects.create(
        account=shared,
        import_batch=batch,
        transaction_date=date(2026, 1, 4),
        amount_minor=-400,
        currency="USD",
        description="Shared groceries",
        kind=Transaction.Kind.CASH_FLOW,
        fingerprint="e" * 64,
        original_fields={"synthetic": "shared"},
        source_row_number=1,
    )
    listed = list_accounts(guest, {})
    assert "Shared Checking" in listed.text
    assert "Host Private" not in listed.text
    conversation = send_message(guest, "List my accounts", sleep=lambda _s: None)
    text = " ".join(conversation.messages.values_list("content", flat=True))
    assert "Host Private" not in text
    usage = AiUsageEvent.objects.visible_to(guest)
    assert usage.exists()
    assert not AiUsageEvent.objects.visible_to(host).filter(member=guest).exists()
    assert usage.get().member_id == guest.pk


@pytest.mark.django_db
def test_shared_local_daily_cap(harness, settings):
    settings.AI_SHARED_LOCAL_DAILY_CAP = 2
    _state, _url = harness
    _host_user, host, _guest_user, guest, _household = _household_pair(harness)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=True, background=True)
    assert run_structured(guest, "one", feature="structured").ok
    assert run_structured(guest, "two", feature="structured").ok
    blocked = run_structured(guest, "three", feature="structured")
    assert not blocked.ok
    assert blocked.failure_code == LIMIT_REACHED


@pytest.mark.django_db
def test_turning_off_or_disconnecting_fails_guest_jobs(harness, monkeypatch):
    state, _url = harness
    _host_user, host, _guest_user, guest, _household = _household_pair(harness)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=True, background=True)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: False)
    state.model_state = "unloaded"
    job = enqueue_job(guest, feature="structured")
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == AiJob.Status.WAITING_MODEL
    assert job.member_id == guest.pk
    set_offer_local_to_household(host, False)
    job.refresh_from_db()
    assert job.status == AiJob.Status.FAILED
    assert job.failure_code == UNAVAILABLE
    set_offer_local_to_household(host, True)
    waiting = enqueue_job(guest, feature="structured")
    process_due_jobs()
    waiting.refresh_from_db()
    assert waiting.status == AiJob.Status.WAITING_MODEL
    from finance.ai_services import disconnect_harness

    disconnect_harness(host)
    waiting.refresh_from_db()
    assert waiting.status == AiJob.Status.FAILED
    assert waiting.failure_code == UNAVAILABLE


@pytest.mark.django_db
def test_only_connection_owner_can_offer_local(harness):
    _state, _url = harness
    host_user, host, guest_user, guest, _household = _household_pair(harness)
    with pytest.raises(PermissionDenied):
        set_offer_local_to_household(guest, True)
    client = Client()
    client.force_login(guest_user)
    stamp_recent_auth(client)
    guest_post = client.post(reverse("ai-offer-local"), {"offer_local_to_household": "on"})
    assert guest_post.url == reverse("settings-ai")
    host.refresh_from_db()
    from finance.ai_services import connection_for

    assert not connection_for(host).offer_local_to_household
    client.force_login(host_user)
    stamp_recent_auth(client)
    saved = client.post(reverse("ai-offer-local"), {"offer_local_to_household": "on"})
    assert saved.url == reverse("settings-ai")
    assert connection_for(host).offer_local_to_household
    guest_page = Client()
    guest_page.force_login(guest_user)
    page = guest_page.get(reverse("settings-ai"))
    assert b"Local model (shared)" in page.content
    stamp_recent_auth(guest_page)
    chosen = guest_page.post(
        reverse("ai-shared-local"),
        {"use_shared_local_chat": "on", "use_shared_local_background": "on"},
    )
    assert chosen.url == reverse("settings-ai")
    guest.refresh_from_db()
    assert guest.use_shared_local_chat
    assert guest.use_shared_local_background


@pytest.mark.django_db
def test_shared_job_never_resumes_on_the_members_own_harness(harness):
    from finance.ai_jobs import SESSION_CONNECTION_KEY, _connection_marker
    from finance.ai_services import offered_local_connection

    host_state, _host_url = harness
    own_state, own_url, own_server = start_fake_harness()
    try:
        _host_user, host, _guest_user, guest, _household = _household_pair(harness)
        set_offer_local_to_household(host, True)
        connect_harness(guest, base_url=own_url, token=TOKEN)
        set_shared_local_use(guest, chat=False, background=True)
        job = enqueue_job(guest, feature="structured")
        shared = offered_local_connection(guest)
        host_state.sessions["sess-shared-1"] = {
            "id": "sess-shared-1",
            "status": "done",
            "answer": "synthetic-ok",
            "prompt_tokens": 3,
            "completion_tokens": 4,
        }
        job.harness_session_id = "sess-shared-1"
        job.input_refs = {**job.input_refs, SESSION_CONNECTION_KEY: _connection_marker(shared)}
        job.save(update_fields=("harness_session_id", "input_refs", "updated_at"))
        # The member stops using the shared model while the job waits to resume.
        set_shared_local_use(guest, chat=False, background=False)

        process_due_jobs()

        job.refresh_from_db()
        # The host's session never reaches the member's own harness; the shared job ends instead.
        assert not any("sess-shared-1" in path for _method, path in own_state.requests)
        assert job.status == AiJob.Status.FAILED
        assert job.failure_code == AUTHORIZATION_REQUIRED
    finally:
        own_server.shutdown()
        own_server.server_close()


@pytest.mark.django_db
def test_daily_cap_does_not_block_resuming_a_started_session(harness, settings):
    settings.AI_SHARED_LOCAL_DAILY_CAP = 1
    state, _url = harness
    _host_user, host, _guest_user, guest, _household = _household_pair(harness)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=False, background=True)
    assert run_structured(guest, "one", feature="structured").ok
    state.sessions["sess-cap-1"] = {
        "id": "sess-cap-1",
        "status": "done",
        "answer": "synthetic-ok",
        "prompt_tokens": 3,
        "completion_tokens": 4,
    }

    resumed = run_structured(guest, "one", feature="structured", session_id="sess-cap-1")
    fresh = run_structured(guest, "two", feature="structured")

    assert resumed.ok
    assert fresh.failure_code == LIMIT_REACHED


@pytest.mark.django_db
def test_daily_cap_still_blocks_chat_follow_ups(harness, settings):
    settings.AI_SHARED_LOCAL_DAILY_CAP = 1
    _host_user, host, _guest_user, guest, _household = _household_pair(harness)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=True, background=True)
    assert run_structured(guest, "one", feature="structured").ok

    follow_up = run_conversation(
        guest,
        "another question",
        feature="chat",
        session_id="sess-chat-1",
        follow_up=True,
        tools=default_tools(),
    )

    assert follow_up.failure_code == LIMIT_REACHED


@pytest.mark.django_db
def test_a_resumed_poll_does_not_count_as_another_request(harness, settings):
    settings.AI_SHARED_LOCAL_DAILY_CAP = 2
    state, _url = harness
    _host_user, host, _guest_user, guest, _household = _household_pair(harness)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=False, background=True)
    assert run_structured(guest, "one", feature="structured").ok
    state.sessions["sess-poll-1"] = {
        "id": "sess-poll-1",
        "status": "done",
        "answer": "synthetic-ok",
        "prompt_tokens": 3,
        "completion_tokens": 4,
    }
    assert run_structured(guest, "one", feature="structured", session_id="sess-poll-1").ok

    second = run_structured(guest, "two", feature="structured")

    assert second.ok
