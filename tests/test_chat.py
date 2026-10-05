from datetime import date, timedelta
import hashlib
import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from tests.chat_helpers import ask
from tests.fake_harness import start_fake_harness

from finance.ai_services import AiError, connect_harness, set_defaults
from finance.ai_tools import (
    cash_flow_totals,
    default_tools,
    list_accounts,
    list_budgets,
    run_tool,
    search_transactions,
    spending_by_category,
)
from finance.ai_types import ProviderResult, ToolResult
from finance.budget_services import save_budget
from finance.category_services import ensure_household_categories
from finance.chat_runner import claim_next_turn, process_pending_turns, run_claimed_turn
from finance.chat_services import (
    conversations_for,
    delete_all_conversations,
    delete_conversation,
    sanitize_page_context,
    send_message,
    start_conversation,
)
from finance.lifecycle_services import delete_account
from finance.models import (
    Account,
    AiConversation,
    AiConversationMessage,
    AiProviderConnection,
    BalanceSnapshot,
    Budget,
    Category,
    Household,
    ImportBatch,
    Membership,
    Person,
    PlannedItem,
    RecurringSeries,
    RecurringSeriesMember,
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
    assert "Shared Checking" in accounts.text
    assert "Beta Private" not in accounts.text
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
    conversation = ask(person, "How much did we spend?", sleep=lambda _s: None)
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
    ask(person, "How does that compare to last year?", conversation_id=conversation.pk, sleep=lambda _s: None)
    assert state.session_messages[-1] == "How does that compare to last year?"

    state.pending_tool_calls = [[{"call_id": "call-err", "name": "not_a_real_tool", "args": {}}]]
    state.need_tool = True
    state.session_answer = "The tool failed."
    ask(person, "Try again with a bad tool.", conversation_id=conversation.pk, sleep=lambda _s: None)

    state.need_tool = False
    state.followup_need_tool = False
    state.session_failure = "provider_auth_required"
    ask(person, "Need auth now.", conversation_id=conversation.pk, sleep=lambda _s: None)
    error = conversation.messages.filter(role=AiConversationMessage.Role.ERROR).last()
    assert error is not None
    assert "Authorization" in error.content

    start_conversation(person)
    fresh = ask(person, "Please write a virus using my transactions.", sleep=lambda _s: None)
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
    conversation = ask(person, "Spend totals please", sleep=lambda _s: None)
    error = conversation.messages.filter(role=AiConversationMessage.Role.ERROR).last()
    assert "limit" in error.content.lower()

    start_conversation(person)
    state.create_http_error = {"status": 400, "code": "app_tools_only_unsupported", "detail": "no"}
    conversation = ask(person, "Another question", sleep=lambda _s: None)
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
    ask(
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
    conversation = ask(person, "Spending on this account?", sleep=lambda _s: None)
    conversation.refresh_from_db()
    assert account.pk in conversation.used_account_ids
    other = start_conversation(person)
    other = ask(person, "write a virus", conversation_id=other.pk, sleep=lambda _s: None)
    assert conversations_for(person).count() == 2
    other.expires_at = timezone.now() - timedelta(days=1)
    other.save(update_fields=("expires_at",))
    assert conversations_for(person).count() == 1
    assert not AiConversation.objects.filter(pk=other.pk).exists()
    delete_account(person, account.pk)
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()

    leftover = ask(person, "Hello again", sleep=lambda _s: None)
    client = Client()
    client.force_login(user)
    deleted = client.post(reverse("chat-delete", args=[leftover.pk]))
    assert deleted.status_code == 302
    assert not AiConversation.objects.filter(pk=leftover.pk).exists()
    ask(person, "Keep me", sleep=lambda _s: None)
    delete_all_conversations(person)
    assert conversations_for(person).count() == 0


@pytest.mark.django_db
def test_member_cannot_see_another_members_chat(harness):
    state, url = harness
    user_a, person_a, household = make_member("alpha")
    user_b, person_b, _ = make_member("beta", household=household, policy=current_policy())
    connect_harness(person_a, base_url=url, token=TOKEN)
    connect_harness(person_b, base_url=url, token=TOKEN)
    owned = ask(person_a, "My totals", sleep=lambda _s: None)
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
    ask(person, "Export me", sleep=lambda _s: None)
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
        ask(person, "Hi", sleep=lambda _s: None)
    settings.AI_CHAT_LOCAL_ENABLED = True
    conversation = ask(person, "Hi", sleep=lambda _s: None)
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


def _tool_blob(result):
    if hasattr(result, "text"):
        parts = [result.text]
        if getattr(result, "figures", None):
            parts.append(json.dumps(result.figures))
        if getattr(result, "account_ids", None):
            parts.append(str(result.account_ids))
        return "\n".join(parts)
    return str(result)


HOUSEHOLD_MARKERS = (
    "HH-SYN-SHARED-CHECKING",
    "HH-SYN-RENT-99421",
    "882233",
    "7654321",
    "HH-SYN-SERIES-NET",
    "HH-SYN-PLANNED-CAR",
)


def _household_tool_args(shared_id):
    return (
        ("list_accounts", {}),
        ("list_transactions", {"account_id": shared_id}),
        ("cash_flow_totals", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
        ("spending_by_category", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
        (
            "search_transactions",
            {"date_from": "2026-03-01", "date_to": "2026-03-31"},
        ),
        ("recurring_series", {}),
        ("net_worth_series", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
        ("list_budgets", {"month": "2026-03"}),
        ("projected_cash_flow", {"horizon": 3}),
    )


@pytest.mark.django_db
def test_tools_hide_household_data_until_every_member_accepts_policy():
    _user_a, person_a, household = make_member("alpha")
    user_b = get_user_model().objects.create_user(username="beta", password=PASSWORD)
    person_b = Person.objects.create(user=user_b, display_name="Beta Example")
    Membership.objects.create(person=person_b, household=household)
    ensure_household_categories(household)
    dining = household.categories.get(name="Dining")
    shared = checking(person_a, household, "HH-SYN-SHARED-CHECKING")
    private_a = checking(person_a, household, "Alpha Private", private=True)
    hh_txn = add_txn(shared, person_a, date(2026, 3, 4), -882233, "HH-SYN-RENT-99421", category=dining)
    add_txn(private_a, person_a, date(2026, 2, 5), -100, "A-PRIVATE-SYN-COFFEE")
    BalanceSnapshot.objects.create(
        account=shared,
        snapshot_date=date(2026, 3, 15),
        amount_minor=7654321,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    series = RecurringSeries.objects.create(
        person=person_a,
        merchant_key="hh syn rent",
        display_name="HH-SYN-SERIES-NET",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-882233,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="a" * 64,
    )
    RecurringSeriesMember.objects.create(series=series, transaction=hh_txn)
    PlannedItem.objects.create(
        owner=person_a,
        scope=PlannedItem.Scope.HOUSEHOLD,
        household=household,
        name="HH-SYN-PLANNED-CAR",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=444000,
        start_date=date(2026, 11, 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )
    save_budget(
        person_a,
        {
            "scope": Budget.Scope.HOUSEHOLD,
            "category": dining,
            "amount_minor": 10_000,
            "effective_month": date(2026, 3, 1),
            "rollover_enabled": False,
        },
    )
    tools = default_tools()
    for name, args in _household_tool_args(shared.pk):
        result = run_tool(person_a, tools, name, args)
        blob = _tool_blob(result)
        for marker in HOUSEHOLD_MARKERS:
            assert marker not in blob, f"{name} leaked {marker} while a housemate has not accepted"
        if name == "list_transactions":
            assert not result.ok
            assert "not visible" in result.text.lower()
    private_search = search_transactions(
        person_a, {"q": "A-PRIVATE-SYN-COFFEE", "date_from": "2026-02-01", "date_to": "2026-02-28"}
    )
    assert "A-PRIVATE-SYN-COFFEE" in private_search.text

    accept_policy(person_b, current_policy())
    for name, args in _household_tool_args(shared.pk):
        result = run_tool(person_a, tools, name, args)
        blob = _tool_blob(result)
        assert result.ok
        assert any(marker in blob for marker in HOUSEHOLD_MARKERS), f"{name} missing household data after acceptance"


MIXED_SERIES_NAME = "MIX-HH-SYN-SERIES"
MIXED_SERIES_AMOUNT = 777001


@pytest.mark.django_db
def test_mixed_private_household_series_hidden_until_household_ai_allowed():
    _user_a, person_a, household = make_member("alpha")
    user_b = get_user_model().objects.create_user(username="beta", password=PASSWORD)
    person_b = Person.objects.create(user=user_b, display_name="Beta Example")
    Membership.objects.create(person=person_b, household=household)
    shared = checking(person_a, household, "HH-SYN-SHARED-CHECKING")
    private_a = checking(person_a, household, "Alpha Private", private=True)
    household_txn = add_txn(shared, person_a, date(2026, 3, 4), -MIXED_SERIES_AMOUNT, MIXED_SERIES_NAME)
    private_txn = add_txn(private_a, person_a, date(2026, 4, 4), -1100, MIXED_SERIES_NAME)
    series = RecurringSeries.objects.create(
        person=person_a,
        merchant_key="mix hh syn series",
        display_name=MIXED_SERIES_NAME,
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-MIXED_SERIES_AMOUNT,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="b" * 64,
    )
    RecurringSeriesMember.objects.create(series=series, transaction=household_txn)
    RecurringSeriesMember.objects.create(series=series, transaction=private_txn)
    tools = default_tools()
    markers = (MIXED_SERIES_NAME, str(MIXED_SERIES_AMOUNT))
    for name, args in (("recurring_series", {}), ("projected_cash_flow", {"horizon": 3})):
        result = run_tool(person_a, tools, name, args)
        blob = _tool_blob(result)
        for marker in markers:
            assert marker not in blob, f"{name} leaked {marker} from a mixed series"
        if name == "recurring_series":
            assert shared.pk not in result.account_ids
    accept_policy(person_b, current_policy())
    for name, args in (("recurring_series", {}), ("projected_cash_flow", {"horizon": 3})):
        result = run_tool(person_a, tools, name, args)
        blob = _tool_blob(result)
        assert result.ok
        assert any(marker in blob for marker in markers), f"{name} missing mixed series after acceptance"


@pytest.mark.django_db
def test_turns_in_one_conversation_run_one_at_a_time_and_keep_both_queried_account_ids(harness):
    _state, url = harness
    _user, person, household = make_member("owner")
    first = checking(person, household, "First Checking")
    second = checking(person, household, "Second Checking")
    add_txn(first, person, date(2026, 1, 4), -800, "Synthetic coffee")
    add_txn(second, person, date(2026, 1, 5), -900, "Synthetic groceries")
    connect_harness(person, base_url=url, token=TOKEN)
    conversation = start_conversation(person)
    send_message(person, "Query-A first account", conversation_id=conversation.pk)
    send_message(person, "Query-B second account", conversation_id=conversation.pk)

    def fake_run(_person, prompt, **kwargs):
        account_id = first.pk if prompt.startswith("Query-A") else second.pk
        denied = kwargs["allow_tool"]("list_transactions", {"account_id": account_id})
        if denied is None:
            kwargs["on_tool"](ToolResult(text="{}", account_ids=(account_id,)))
        return ProviderResult(ok=True, answer="ok")

    first_turn = claim_next_turn("lane-a")
    # Both turns share one harness session, so the second waits for the first.
    assert claim_next_turn("lane-b") is None
    with patch("finance.chat_services.run_conversation", side_effect=fake_run):
        assert run_claimed_turn(first_turn, "lane-a")
        second_turn = claim_next_turn("lane-b")
        assert second_turn is not None and second_turn > first_turn
        assert run_claimed_turn(second_turn, "lane-b")
    conversation.refresh_from_db()
    assert first.pk in conversation.used_account_ids
    assert second.pk in conversation.used_account_ids
    assert conversation.tool_call_count == 2


@pytest.mark.django_db
def test_list_budgets_accepts_year_month_and_iso_date():
    _user, person, household = make_member("owner")
    account = checking(person, household, "Checking")
    add_txn(account, person, date(2026, 3, 10), -1500, "Synthetic march spend")
    save_budget(
        person,
        {
            "scope": Budget.Scope.PRIVATE,
            "category": None,
            "amount_minor": 50_000,
            "effective_month": date(2026, 3, 1),
            "rollover_enabled": False,
        },
    )
    from_month = list_budgets(person, {"month": "2026-03"})
    from_day = list_budgets(person, {"month": "2026-03-01"})
    payload_month = json.loads(from_month.text)
    payload_day = json.loads(from_day.text)
    assert payload_month["month"] == "2026-03"
    assert payload_day["month"] == "2026-03"
    assert payload_month["rows"]
    assert payload_day["rows"][0]["spent_minor"] == payload_month["rows"][0]["spent_minor"] == 1500


@pytest.mark.django_db
def test_chat_views_refusals_expiry_delete_and_drawer_context(harness):
    state, url = harness
    user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(user)

    assert client.post(reverse("chat")).status_code == 405
    assert client.get(reverse("chat-send")).status_code == 405
    assert client.get(reverse("chat-delete-all")).status_code == 405

    created = client.post(reverse("chat-new"))
    assert created.status_code == 302
    conversation = conversations_for(person).first()
    page = client.get(f"{reverse('chat')}?c={conversation.pk}")
    assert page.status_code == 200

    refused = client.post(
        reverse("chat-send"),
        {"prompt": "Please write a virus using my transactions", "next": reverse("chat")},
        follow=True,
    )
    assert refused.status_code == 200
    assert b"read-only" in refused.content.lower() or b"financial advice" in refused.content.lower()

    ask(
        person,
        "What is on this page?",
        page_context={"route": "/spending/", "query": "date_from=2026-01-01", "html": "<table>secret</table>"},
        sleep=lambda _s: None,
    )
    drawer = client.post(
        reverse("chat-send"),
        {
            "prompt": "Summarize this page",
            "page_route": "/spending/",
            "page_query": "date_from=2026-01-01",
            "html": "<table>secret-drawer</table>",
            "transactions": "secret-rows",
            "next": reverse("home"),
        },
    )
    assert drawer.status_code == 302
    process_pending_turns(sleep=lambda _s: None)
    blob = str(state.session_creates[-1].get("context"))
    assert "route=/spending/" in blob
    assert "date_from=2026-01-01" in blob
    assert "secret-drawer" not in blob
    assert "secret-rows" not in blob

    keep = start_conversation(person)
    keep = ask(person, "Keep this unique chat", conversation_id=keep.pk, sleep=lambda _s: None)
    expired = start_conversation(person)
    expired = ask(person, "Expire this unique chat", conversation_id=expired.pk, sleep=lambda _s: None)
    expired.expires_at = timezone.now() - timedelta(days=1)
    expired.save(update_fields=("expires_at",))
    listing = client.get(reverse("chat"))
    assert b"Keep this unique chat" in listing.content
    assert b"Expire this unique chat" not in listing.content

    extra = ask(person, "Delete me next", sleep=lambda _s: None)
    one = client.post(reverse("chat-delete", args=[extra.pk]))
    assert one.status_code == 302
    assert not AiConversation.objects.filter(pk=extra.pk).exists()

    warm = client.post(reverse("chat-warm"))
    assert warm.status_code == 409

    client.post(reverse("chat-delete-all"))
    assert conversations_for(person).count() == 0

    publish_policy(material=True, body="Synthetic newer material policy for chat refusal")
    blocked = client.post(
        reverse("chat-send"),
        {"prompt": "How much did I spend?", "next": reverse("chat")},
        follow=True,
    )
    assert b"privacy" in blocked.content.lower() or b"accepted" in blocked.content.lower()
    assert client.post(reverse("chat-warm")).status_code == 403
    assert client.get(reverse("chat-status")).status_code == 403


@pytest.mark.django_db
def test_tool_calls_in_one_response_stop_at_the_conversation_limit(harness, settings):
    settings.AI_CHAT_MAX_TOOL_CALLS = 40
    state, url = harness
    _user, person, household = make_member("owner")
    first = checking(person, household, "First Checking")
    second = checking(person, household, "Second Checking")
    add_txn(first, person, date(2026, 1, 4), -800, "Synthetic coffee")
    add_txn(second, person, date(2026, 1, 5), -900, "Synthetic groceries")
    connect_harness(person, base_url=url, token=TOKEN)
    conversation = ask(person, "Start the thread", sleep=lambda _s: None)
    conversation.tool_call_count = 39
    conversation.save(update_fields=("tool_call_count",))
    state.need_tool = True
    state.pending_tool_calls = [
        [
            {
                "call_id": "c1",
                "name": "cash_flow_totals",
                "args": {"date_from": "2026-01-01", "date_to": "2026-01-31", "account_id": first.pk},
            },
            {
                "call_id": "c2",
                "name": "cash_flow_totals",
                "args": {"date_from": "2026-01-01", "date_to": "2026-01-31", "account_id": second.pk},
            },
            {
                "call_id": "c3",
                "name": "list_accounts",
                "args": {},
            },
        ]
    ]
    conversation = ask(
        person,
        "Totals for both accounts?",
        conversation_id=conversation.pk,
        sleep=lambda _s: None,
    )
    conversation.refresh_from_db()
    assert conversation.tool_call_count == 40
    assert first.pk in conversation.used_account_ids
    assert second.pk not in conversation.used_account_ids


@pytest.mark.django_db
def test_list_transactions_conversation_is_removed_when_that_account_is_deleted(harness):
    state, url = harness
    _user, person, household = make_member("owner")
    account = checking(person, household, "Shared Checking")
    add_txn(account, person, date(2026, 1, 4), -800, "Synthetic coffee")
    connect_harness(person, base_url=url, token=TOKEN)
    state.need_tool = True
    state.pending_tool_calls = [
        [{"call_id": "c1", "name": "list_transactions", "args": {"account_id": account.pk}}]
    ]
    conversation = ask(person, "Show recent rows on this account", sleep=lambda _s: None)
    conversation.refresh_from_db()
    assert account.pk in conversation.used_account_ids
    delete_account(person, account.pk)
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()


@pytest.mark.django_db
def test_followup_starts_a_new_session_after_harness_reconnect(harness):
    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    conversation = ask(person, "First question", sleep=lambda _s: None)
    conversation.refresh_from_db()
    old_session = conversation.harness_session_id
    assert old_session
    creates_before = len(state.session_creates)
    connect_harness(person, base_url=url, token=TOKEN)
    state.need_tool = False
    ask(
        person,
        "Follow up after reconnect",
        conversation_id=conversation.pk,
        sleep=lambda _s: None,
    )
    assert ("POST", f"/api/v1/sessions/{old_session}/messages") not in state.requests
    assert len(state.session_creates) == creates_before + 1
    conversation.refresh_from_db()
    assert conversation.harness_session_id
    assert conversation.harness_session_id != old_session


@pytest.mark.django_db
def test_chat_send_and_delete_cover_remaining_error_paths(harness):
    from django.core.exceptions import PermissionDenied

    state, url = harness
    user, person, _household = make_member("owner")
    other_user, other, _ = make_member("intruder", policy=current_policy())
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(user)

    empty = client.post(reverse("chat-send"), {"prompt": "   ", "next": reverse("home")})
    assert empty.status_code == 302
    assert empty.url == reverse("home")

    created = ask(person, "A valid question", sleep=lambda _s: None)
    bad_id = client.post(
        reverse("chat-send"),
        {"prompt": "Hello", "conversation_id": "not-a-number", "next": reverse("chat")},
        follow=True,
    )
    assert bad_id.status_code == 200

    sent = client.post(
        reverse("chat-send"),
        {
            "prompt": "Drawer follow-up",
            "conversation_id": str(created.pk),
            "next": reverse("home"),
            "page_route": "not-a-path",
            "page_query": "?date_from=2026-01-01\nsecret",
        },
    )
    assert sent.status_code == 302
    assert sent.url.startswith(reverse("home")) or sent.url == reverse("home")

    client.force_login(other_user)
    denied = client.post(reverse("chat-delete", args=[created.pk]))
    assert denied.status_code == 302
    assert AiConversation.objects.filter(pk=created.pk).exists()
    with pytest.raises(PermissionDenied):
        delete_conversation(other, created.pk)

    cleaned = sanitize_page_context({"route": "https://evil.example/x", "query": "a=1"})
    assert "route" not in cleaned
    assert cleaned.get("query") == "a=1"
    query_only = sanitize_page_context({"path": "/spending/", "query_string": "?x=1"})
    assert query_only == {"route": "/spending/", "query": "x=1"}

    with pytest.raises(AiError):
        ask(person, "   ", sleep=lambda _s: None)
    state.session_failure = "provider_error"
    failed = ask(person, "A later question", conversation_id=created.pk, sleep=lambda _s: None)
    error = failed.messages.filter(role=AiConversationMessage.Role.ERROR).last()
    assert "could not complete" in error.content.lower()

    client.force_login(user)
    gone = client.post(
        reverse("chat-send"),
        {"prompt": "Talk about a missing thread", "conversation_id": "999999", "next": reverse("chat")},
        follow=True,
    )
    assert gone.status_code == 200
    created.turn_count = 99
    created.save(update_fields=("turn_count",))
    with pytest.raises(AiError) as turned:
        ask(person, "One more turn", conversation_id=created.pk, sleep=lambda _s: None)
    assert "turn limit" in str(turned.value).lower()
    created.turn_count = 1
    created.tool_call_count = 40
    created.save(update_fields=("turn_count", "tool_call_count"))
    with pytest.raises(AiError) as capped:
        ask(person, "One more tool", conversation_id=created.pk, sleep=lambda _s: None)
    assert "tool-call" in str(capped.value).lower()
    from finance.ai_services import disconnect_harness

    disconnect_harness(person)
    with pytest.raises(AiError):
        ask(person, "After disconnect", sleep=lambda _s: None)
    status = client.get(reverse("chat-status"))
    assert status.status_code == 403


@pytest.mark.django_db
def test_chat_warm_and_status_surface_provider_errors(harness, settings, monkeypatch):
    settings.AI_CHAT_LOCAL_ENABLED = True
    state, url = harness
    user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    client = Client()
    client.force_login(user)

    def boom(*_args, **_kwargs):
        raise AiError("synthetic provider failure")

    monkeypatch.setattr("finance.chat_views.warm_for_chat", boom)
    monkeypatch.setattr("finance.chat_views.local_status", boom)
    warm = client.post(reverse("chat-warm"))
    assert warm.status_code == 409
    assert b"synthetic provider failure" in warm.content
    status = client.get(reverse("chat-status"))
    assert status.status_code == 400
    assert b"synthetic provider failure" in status.content


@pytest.mark.django_db
def test_chat_send_rejects_external_next_url(harness):
    state, url = harness
    user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(user)
    response = client.post(
        reverse("chat-send"),
        {"prompt": "How much did I spend?", "next": "https://evil.example/"},
    )
    assert response.status_code == 302
    assert "evil.example" not in response.url
    assert response.url.startswith(reverse("chat"))


@pytest.mark.django_db
def test_cash_flow_totals_tracks_accounts_beyond_tool_row_limit(harness):
    state, url = harness
    _user, person, household = make_member("owner")
    accounts = []
    for index in range(51):
        account = checking(person, household, f"Shared Checking {index:02d}")
        add_txn(account, person, date(2026, 1, 4), -100 - index, f"Synthetic spend {index:02d}")
        accounts.append(account)
    extra = accounts[-1]
    connect_harness(person, base_url=url, token=TOKEN)
    state.need_tool = True
    state.pending_tool_calls = [
        [{"call_id": "c1", "name": "cash_flow_totals", "args": {"date_from": "2026-01-01", "date_to": "2026-01-31"}}]
    ]
    conversation = ask(person, "Spending across every account?", sleep=lambda _s: None)
    conversation.refresh_from_db()
    assert extra.pk in conversation.used_account_ids
    assert len(conversation.used_account_ids) >= 51
    delete_account(person, extra.pk)
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()


@pytest.mark.django_db
def test_followup_starts_a_new_session_after_chat_backend_switch(harness, settings):
    from finance.models import AiUsageEvent

    state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    conversation = ask(person, "First question on Claude", sleep=lambda _s: None)
    conversation.refresh_from_db()
    old_session = conversation.harness_session_id
    assert conversation.backend == "claude"
    assert old_session
    creates_before = len(state.session_creates)
    settings.AI_CHAT_LOCAL_ENABLED = True
    set_defaults(person, chat_backend="local", background_backend="local")
    state.need_tool = False
    ask(
        person,
        "Follow up after switching to local",
        conversation_id=conversation.pk,
        sleep=lambda _s: None,
    )
    assert ("POST", f"/api/v1/sessions/{old_session}/messages") not in state.requests
    assert len(state.session_creates) == creates_before + 1
    assert state.session_creates[-1].get("backend") == "local"
    conversation.refresh_from_db()
    assert conversation.backend == "local"
    assert conversation.harness_session_id
    assert conversation.harness_session_id != old_session
    latest = AiUsageEvent.objects.filter(member=person, feature="chat").order_by("-pk").first()
    assert latest is not None
    assert latest.backend == "local"



@pytest.mark.django_db
def test_recurring_tool_lists_only_confirmed_current_series():
    _user, person, household = make_member("owner")
    account = checking(person, household, "Owner Private", private=True)
    rows = [
        ("SYN-CONFIRMED-GYM", RecurringSeries.Status.CONFIRMED, None),
        ("SYN-SUGGESTED-STREAM", RecurringSeries.Status.SUGGESTED, None),
        ("SYN-DISMISSED-PAPER", RecurringSeries.Status.DISMISSED, None),
        ("SYN-CANCELLED-MUSIC", RecurringSeries.Status.CONFIRMED, timezone.now()),
    ]
    for index, (name, status, cancelled_at) in enumerate(rows):
        txn = add_txn(account, person, date(2026, 3, 4 + index), -1500 - index, name)
        series = RecurringSeries.objects.create(
            person=person,
            merchant_key=name.lower(),
            display_name=name,
            cadence=RecurringSeries.Cadence.MONTHLY,
            typical_amount_minor=-1500 - index,
            status=status,
            confidence=RecurringSeries.Confidence.HIGH,
            reasons=["synthetic"],
            fingerprint=str(index) * 64,
            cancelled_at=cancelled_at,
        )
        RecurringSeriesMember.objects.create(series=series, transaction=txn)

    result = run_tool(person, default_tools(), "recurring_series", {})

    names = [row["name"] for row in json.loads(result.text)["rows"]]
    assert names == ["SYN-CONFIRMED-GYM"]
    assert [figure["label"] for figure in result.figures] == ["SYN-CONFIRMED-GYM"]


@pytest.mark.django_db
def test_send_without_a_chat_backend_explains_what_to_do(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_harness(person, base_url=url, token=TOKEN)
    AiProviderConnection.objects.filter(owner=person).update(chat_backend="")

    with pytest.raises(AiError, match="No chat backend is available yet"):
        ask(person, "What did I spend last month?")
    assert not AiConversation.objects.filter(member=person).exists()
