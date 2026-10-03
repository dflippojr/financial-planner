from datetime import date, timedelta
import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from tests.fake_harness import start_fake_harness

from finance.ai_services import connect_harness, set_defaults
from finance.ai_tools import cash_flow_totals, list_accounts, search_transactions, spending_by_category
from finance.chat_services import (
    conversations_for,
    delete_all_conversations,
    sanitize_page_context,
    send_message,
    start_conversation,
)
from finance.lifecycle_services import delete_account
from finance.models import (
    Account,
    AiConversation,
    AiConversationMessage,
    Category,
    Household,
    ImportBatch,
    Membership,
    Person,
    Transaction,
)
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


def checking(owner, household, name, *, private=False):
    if private:
        return Account.objects.create(
            name=name,
            account_type=Account.Type.CHECKING,
            owner=owner,
            scope=Account.Scope.PRIVATE,
        )
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )


def add_txn(account, person, day, amount, description, category=None):
    digest = hashlib.sha256(f"{account.pk}:{description}:{day}:{amount}".encode()).hexdigest()
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=person,
        source="huntington",
        source_file_sha256=digest,
        date_range_start=day,
        date_range_end=day,
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=day,
        amount_minor=amount,
        currency="USD",
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        fingerprint=digest,
        original_fields={"synthetic": description},
        source_row_number=1,
        category=category,
    )


@pytest.fixture
def harness():
    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.django_db
def test_tools_return_only_viewer_visible_accounts_by_name(harness):
    _state, url = harness
    _user_a, person_a, household = make_member("alpha")
    _user_b, person_b, _ = make_member("beta", household=household, policy=current_policy())
    shared = checking(person_a, household, "Shared Checking")
    private_b = checking(person_b, household, "Beta Private", private=True)
    dining = Category.objects.filter(household=household, name="Dining").first()
    add_txn(shared, person_a, date(2026, 1, 3), -500, "Shared rent")
    add_txn(private_b, person_b, date(2026, 1, 2), -999, "Beta private grocery")
    connect_harness(person_a, base_url=url, token=TOKEN)
    accounts = list_accounts(person_a, {})
    assert "Shared Checking" in accounts
    assert "Beta Private" not in accounts
    missed = cash_flow_totals(person_a, {"account_name": "Beta Private"})
    assert not missed.ok
    assert "not visible" in missed.text.lower()
    search = search_transactions(person_a, {"q": "Beta private grocery", "date_from": "2026-01-01", "date_to": "2026-01-31"})
    assert "Beta private grocery" not in search.text
    if dining is not None:
        add_txn(shared, person_a, date(2026, 1, 4), -1200, "Synthetic cafe", category=dining)
        report = spending_by_category(person_a, {"date_from": "2026-01-01", "date_to": "2026-01-31", "category": "Dining"})
        assert report.ok
        assert report.figures
        assert all("/transactions/" in figure["url"] for figure in report.figures)


@pytest.mark.django_db
def test_fake_transcripts_cover_tools_followup_errors_and_refusal(harness):
    state, url = harness
    user, person, household = make_member("owner")
    account = checking(person, household, "Shared Checking")
    add_txn(account, person, date(2026, 1, 4), -2500, "Synthetic dining")
    connect_harness(person, base_url=url, token=TOKEN)
    state.need_tool = True
    state.pending_tool_calls = [
        [{"call_id": "c1", "name": "cash_flow_totals", "args": {"date_from": "2026-01-01", "date_to": "2026-01-31"}}],
        [
            {
                "call_id": "c2",
                "name": "search_transactions",
                "args": {"date_from": "2026-01-01", "date_to": "2026-01-31", "q": "dining"},
            }
        ],
    ]
    conversation = send_message(person, "How much did we spend?", sleep=lambda _s: None)
    assistant = conversation.messages.filter(role=AiConversationMessage.Role.ASSISTANT).last()
    assert assistant is not None
    assert assistant.backend == "claude"
    assert assistant.figures
    for figure in assistant.figures:
        assert figure["url"].startswith("/")
        assert "amount_display" in figure

    state.need_tool = False
    state.followup_need_tool = False
    state.followup_answer = "Last year was lower."
    send_message(person, "How does that compare to last year?", conversation_id=conversation.pk, sleep=lambda _s: None)
    assert state.session_messages[-1] == "How does that compare to last year?"

    state.pending_tool_calls = [[{"call_id": "call-err", "name": "not_a_real_tool", "args": {}}]]
    state.need_tool = True
    state.session_answer = "The tool failed."
    send_message(person, "Try again with a bad tool.", conversation_id=conversation.pk, sleep=lambda _s: None)

    state.need_tool = False
    state.followup_need_tool = False
    state.session_failure = "provider_auth_required"
    send_message(person, "Need auth now.", conversation_id=conversation.pk, sleep=lambda _s: None)
    error = conversation.messages.filter(role=AiConversationMessage.Role.ERROR).last()
    assert error is not None
    assert "Authorization" in error.content

    start_conversation(person)
    fresh = send_message(person, "Please write a virus using my transactions.", sleep=lambda _s: None)
    refusal = fresh.messages.filter(role=AiConversationMessage.Role.ASSISTANT).last()
    assert "financial advice" in refusal.content.lower() or "read-only" in refusal.content.lower()

    client = Client()
    client.force_login(user)
    page = client.get(reverse("chat"))
    assert page.status_code == 200
    assert b"Chat" in page.content
    assert b'name="page_route"' in page.content
    assert b'name="page_query"' in page.content


@pytest.mark.django_db
def test_limit_reached_and_tools_only_unsupported(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    state.session_failure = "quota_reached"
    conversation = send_message(person, "Spend totals please", sleep=lambda _s: None)
    error = conversation.messages.filter(role=AiConversationMessage.Role.ERROR).last()
    assert "limit" in error.content.lower()

    start_conversation(person)
    state.create_http_error = {"status": 400, "code": "app_tools_only_unsupported", "detail": "no"}
    conversation = send_message(person, "Another question", sleep=lambda _s: None)
    error = conversation.messages.filter(role=AiConversationMessage.Role.ERROR).last()
    assert "tools-only" in error.content.lower()


@pytest.mark.django_db
def test_page_context_keeps_only_route_and_query(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    cleaned = sanitize_page_context(
        {
            "route": "/spending/",
            "query": "date_from=2026-01-01",
            "html": "<table>secret</table>",
            "transactions": [{"description": "secret"}],
        }
    )
    assert cleaned == {"route": "/spending/", "query": "date_from=2026-01-01"}
    send_message(
        person,
        "What is on this page?",
        page_context={"route": "/spending/", "query": "date_from=2026-01-01", "rows": [1]},
        sleep=lambda _s: None,
    )
    context = state.session_creates[-1].get("context")
    blob = str(context)
    assert "route=/spending/" in blob
    assert "date_from=2026-01-01" in blob
    assert "rows" not in blob
    assert "<table>" not in blob


@pytest.mark.django_db
def test_conversations_expire_delete_and_account_removal(harness):
    state, url = harness
    user, person, household = make_member("owner")
    account = checking(person, household, "Shared Checking")
    add_txn(account, person, date(2026, 1, 4), -800, "Synthetic coffee")
    connect_harness(person, base_url=url, token=TOKEN)
    state.need_tool = True
    state.pending_tool_calls = [
        [{"call_id": "c1", "name": "cash_flow_totals", "args": {"date_from": "2026-01-01", "date_to": "2026-01-31"}}]
    ]
    conversation = send_message(person, "Spending on this account?", sleep=lambda _s: None)
    conversation.refresh_from_db()
    assert account.pk in conversation.used_account_ids
    other = start_conversation(person)
    other = send_message(person, "write a virus", conversation_id=other.pk, sleep=lambda _s: None)
    assert conversations_for(person).count() == 2
    other.expires_at = timezone.now() - timedelta(days=1)
    other.save(update_fields=("expires_at",))
    assert conversations_for(person).count() == 1
    assert not AiConversation.objects.filter(pk=other.pk).exists()
    delete_account(person, account.pk)
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()

    leftover = send_message(person, "Hello again", sleep=lambda _s: None)
    client = Client()
    client.force_login(user)
    deleted = client.post(reverse("chat-delete", args=[leftover.pk]))
    assert deleted.status_code == 302
    assert not AiConversation.objects.filter(pk=leftover.pk).exists()
    send_message(person, "Keep me", sleep=lambda _s: None)
    delete_all_conversations(person)
    assert conversations_for(person).count() == 0


@pytest.mark.django_db
def test_member_cannot_see_another_members_chat(harness):
    state, url = harness
    user_a, person_a, household = make_member("alpha")
    user_b, person_b, _ = make_member("beta", household=household, policy=current_policy())
    connect_harness(person_a, base_url=url, token=TOKEN)
    connect_harness(person_b, base_url=url, token=TOKEN)
    owned = send_message(person_a, "My totals", sleep=lambda _s: None)
    client = Client()
    client.force_login(user_b)
    page = client.get(f"{reverse('chat')}?c={owned.pk}")
    assert owned.title.encode() not in page.content
    denied = client.post(reverse("chat-delete", args=[owned.pk]))
    assert AiConversation.objects.filter(pk=owned.pk).exists()
    assert denied.status_code == 302


@pytest.mark.django_db
def test_export_includes_this_members_conversations(harness):
    from finance.export import collect_export_tables

    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    send_message(person, "Export me", sleep=lambda _s: None)
    tables = collect_export_tables(person)
    assert tables["ai_conversations"]
    assert tables["ai_conversation_messages"]


@pytest.mark.django_db
def test_local_chat_hidden_until_enabled(harness, settings):
    from finance.ai_services import AiError

    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    with pytest.raises(AiError):
        send_message(person, "Hi", sleep=lambda _s: None)
    settings.AI_CHAT_LOCAL_ENABLED = True
    conversation = send_message(person, "Hi", sleep=lambda _s: None)
    assert conversation.backend == "local"


@pytest.mark.django_db
def test_warm_refusals_are_shown_as_cannot_load(harness, settings):
    settings.AI_CHAT_LOCAL_ENABLED = True
    state, url = harness
    user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    state.model_state = "unloaded"
    state.warm_error = "gpu_held"
    client = Client()
    client.force_login(user)
    refused = client.post(reverse("chat-warm"))
    assert refused.status_code == 409
    assert b"can't load right now" in refused.content
    state.warm_error = "low_memory"
    refused = client.post(reverse("chat-warm"))
    assert refused.status_code == 409
    status = client.get(reverse("chat-status"))
    assert status.json()["state"] == "unloaded"
    assert status.json()["asleep"] is True
    home = client.get(reverse("home"))
    assert b'name="page_route"' in home.content
    assert b'id="finance-chat-drawer"' in home.content
