"""Chat history is derived data: it goes when the member loses access to a row it used."""

from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone
from tests.chat_helpers import ask
from tests.fake_provider import ANTHROPIC_KEY
from tests.test_ai_api_key import provider  # noqa: F401 - provider is a fixture
from tests.test_chat import TOKEN, add_txn, checking, harness, make_member  # noqa: F401 - harness is a fixture
from tests.test_monthly_review import signed_in

from finance.ai_services import connect_api_key, connect_harness, set_api_defaults
from finance.chat_runner import process_pending_turns
from finance.chat_services import send_message
from finance.export import collect_export_tables
from finance.lifecycle_services import leave_household, unshare_account
from finance.models import Account, AiConversation, AiConversationMessage
from finance.policy_services import current_policy


def _conversation(member, account_ids, marker):
    conversation = AiConversation.objects.create(
        member=member, expires_at=timezone.now() + timedelta(days=30), used_account_ids=list(account_ids)
    )
    question = AiConversationMessage.objects.create(
        conversation=conversation, role=AiConversationMessage.Role.USER, content="What did we spend?"
    )
    AiConversationMessage.objects.create(
        conversation=conversation,
        role=AiConversationMessage.Role.ASSISTANT,
        reply_to=question,
        content=f"{marker} therapy cost 4,321.09 USD",
        figures=[{"label": f"{marker} therapy", "amount_minor": -432_109, "currency": "USD",
                  "amount_display": "-4,321.09 USD", "url": "/transactions/"}],
    )
    return conversation


def _make_private_behind_the_purge(account):
    """An unshare that lands while a turn is in flight, after the purge has already run."""
    Account.objects.filter(pk=account.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode="")


def _ask_with_tool(state, person, account, text):
    state.need_tool = True
    state.pending_tool_calls = [[{"call_id": "c1", "name": "list_transactions", "args": {"account_id": account.pk}}]]
    conversation = ask(person, text)
    state.need_tool = False
    return conversation


@pytest.mark.django_db
def test_unshare_purges_chat_history_built_from_the_now_private_account(harness):
    _state, url = harness
    _a, owner, household = make_member("owner")
    _b, member, _ = make_member("member", household=household, policy=current_policy())
    connect_harness(member, base_url=url, token=TOKEN)
    shared = checking(owner, household, "ZQUNSHAREDACCT")
    still_shared = checking(owner, household, "ZQSTILLSHARED")
    conv = _conversation(member, [shared.pk], "ZQFORMERSHARED")
    kept = _conversation(member, [still_shared.pk], "ZQSTILLVISIBLE")
    owners = _conversation(owner, [shared.pk], "ZQOWNERSOWN")
    assert "ZQFORMERSHARED" in signed_in(member).get(reverse("chat") + f"?c={conv.pk}").content.decode()

    unshare_account(owner, shared.pk)

    assert not Account.objects.visible_to(member).filter(pk=shared.pk).exists()
    body = signed_in(member).get(reverse("chat") + f"?c={conv.pk}").content.decode()
    assert "ZQFORMERSHARED" not in body
    assert "ZQFORMERSHARED" not in str(collect_export_tables(member))
    assert not AiConversation.objects.filter(pk=conv.pk).exists()
    # Conversations over accounts their member can still see are untouched.
    assert AiConversation.objects.filter(pk__in=(kept.pk, owners.pk)).count() == 2


@pytest.mark.django_db
def test_leaving_purges_conversations_over_accounts_either_side_can_no_longer_see(harness):
    state, url = harness
    _a, owner, household = make_member("owner")
    _b, member, _ = make_member("member", household=household, policy=current_policy())
    connect_harness(member, base_url=url, token=TOKEN)
    shared = checking(owner, household, "Synthetic shared checking")
    add_txn(shared, owner, date(2026, 1, 4), -800, "Synthetic shared coffee")
    lent = checking(member, household, "Synthetic lent checking")
    lent.share_mode = Account.ShareMode.LENT
    lent.save(update_fields=("share_mode",))
    owner_private = checking(owner, household, "Synthetic owner private", private=True)

    left_behind = _ask_with_tool(state, member, shared, "Show the shared rows")
    assert shared.pk in left_behind.used_account_ids
    old_session = left_behind.harness_session_id
    assert old_session
    members_own = _conversation(member, [lent.pk], "ZQLENTBYLEAVER")
    owner_on_lent = _conversation(owner, [lent.pk], "ZQOWNERONLENT")
    owner_on_shared = _conversation(owner, [shared.pk, owner_private.pk], "ZQOWNERSHARED")

    leave_household(member)

    assert not AiConversation.objects.filter(pk__in=(left_behind.pk, owner_on_lent.pk)).exists()
    assert AiConversation.objects.filter(pk__in=(members_own.pk, owner_on_shared.pk)).count() == 2
    with pytest.raises(PermissionDenied):
        ask(member, "Follow up", conversation_id=left_behind.pk)
    creates_before = len(state.session_creates)
    fresh = ask(member, "A new question", conversation_id=members_own.pk)
    assert ("POST", f"/api/v1/sessions/{old_session}/messages") not in state.requests
    assert len(state.session_creates) == creates_before + 1
    assert fresh.harness_session_id != old_session


@pytest.mark.django_db
def test_harness_session_is_not_resumed_once_a_used_account_is_out_of_sight(harness):
    state, url = harness
    _a, owner, household = make_member("owner")
    _b, member, _ = make_member("member", household=household, policy=current_policy())
    connect_harness(member, base_url=url, token=TOKEN)
    shared = checking(owner, household, "Synthetic shared checking")
    conversation = _ask_with_tool(state, member, shared, "Show the shared rows")
    old_session = conversation.harness_session_id
    assert old_session and shared.pk in conversation.used_account_ids

    _make_private_behind_the_purge(shared)
    creates_before = len(state.session_creates)
    send_message(member, "Follow up", conversation_id=conversation.pk)
    process_pending_turns(sleep=lambda _s: None)

    assert ("POST", f"/api/v1/sessions/{old_session}/messages") not in state.requests
    assert len(state.session_creates) == creates_before + 1
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()


@pytest.mark.django_db
def test_reply_is_dropped_when_access_is_revoked_after_the_turn_recorded_its_account(harness, monkeypatch):
    import finance.chat_runner as chat_runner

    state, url = harness
    _a, owner, household = make_member("owner")
    _b, member, _ = make_member("member", household=household, policy=current_policy())
    connect_harness(member, base_url=url, token=TOKEN)
    shared = checking(owner, household, "Synthetic shared checking")
    add_txn(shared, owner, date(2026, 1, 4), -800, "Synthetic shared coffee")
    real_answer_turn = chat_runner.answer_turn

    def answer_then_unshare(turn, **kwargs):
        reply = real_answer_turn(turn, **kwargs)
        _make_private_behind_the_purge(shared)
        return reply

    monkeypatch.setattr(chat_runner, "answer_turn", answer_then_unshare)
    state.need_tool = True
    state.pending_tool_calls = [[{"call_id": "c1", "name": "list_transactions", "args": {"account_id": shared.pk}}]]
    conversation = send_message(member, "Show the shared rows")
    process_pending_turns(sleep=lambda _s: None)

    assert not AiConversation.objects.filter(pk=conversation.pk).exists()
    assert not AiConversationMessage.objects.filter(conversation_id=conversation.pk).exists()


def _api_member(household):
    _b, member, _ = make_member("byok", household=household, policy=current_policy())
    connect_api_key(member, kind="anthropic_api", key=ANTHROPIC_KEY)
    set_api_defaults(member, kind="anthropic_api", chat_model="", background_model="",
                     use_chat=True, use_background=False)
    return member


@pytest.mark.django_db
def test_api_history_is_not_resent_once_a_used_account_is_out_of_sight(provider):  # noqa: F811
    _a, owner, household = make_member("owner")
    member = _api_member(household)
    shared = checking(owner, household, "Synthetic shared checking")
    marker = "ZQFORMERSHARED-HISTORY"
    conversation = ask(member, f"First question {marker}")
    AiConversation.objects.filter(pk=conversation.pk).update(used_account_ids=[shared.pk])

    _make_private_behind_the_purge(shared)
    send_message(member, "Second question", conversation_id=conversation.pk)
    process_pending_turns(sleep=lambda _s: None)

    messages = provider.requests[-1]["body"]["messages"]
    assert marker not in str(messages)
    assert [m["content"] for m in messages] == ["Second question"]
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()


@pytest.mark.django_db
def test_next_api_turn_after_unshare_starts_without_earlier_history(provider):  # noqa: F811
    _a, owner, household = make_member("owner")
    member = _api_member(household)
    shared = checking(owner, household, "Synthetic shared checking")
    marker = "ZQFORMERSHARED-HISTORY"
    conversation = ask(member, f"First question {marker}")
    AiConversation.objects.filter(pk=conversation.pk).update(used_account_ids=[shared.pk])

    unshare_account(owner, shared.pk)
    with pytest.raises(PermissionDenied):
        ask(member, "Second question", conversation_id=conversation.pk)
    fresh = ask(member, "Second question")

    assert fresh.pk != conversation.pk
    messages = provider.requests[-1]["body"]["messages"]
    assert [m["content"] for m in messages] == ["Second question"]


@pytest.mark.django_db(transaction=True)
def test_applying_a_proposal_while_its_account_is_unshared_does_not_deadlock():
    # Unsharing locks memberships and then purges the conversation, deleting its
    # proposals; Apply must take the memberships before the proposal, not after.
    from django.db import connection

    import finance.chat_proposals as chat_proposals
    from finance.models import AiProposal
    from tests.lock_races import run_account_lock_race

    if connection.vendor != "postgresql":
        pytest.skip("row-lock ordering can only be exercised on PostgreSQL")
    _a, owner, household = make_member("owner")
    _b, member, _ = make_member("member", household=household, policy=current_policy())
    shared = checking(owner, household, "Synthetic shared checking")
    txn = add_txn(shared, owner, date(2026, 1, 4), -800, "Synthetic shared coffee")
    conversation = _conversation(member, [shared.pk], "ZQPROPOSAL")
    proposal = AiProposal.objects.create(
        conversation=conversation, message=conversation.messages.last(), kind="add_tags",
        payload={"items": [{"id": txn.pk}], "tags": ["Synthetic"]},
    )

    run_account_lock_race(
        lambda: chat_proposals.apply_proposal(member, proposal.pk),
        lambda: unshare_account(owner, shared.pk),
        pause_after=(chat_proposals, "_locked_pending"),
    )

    assert txn.tags.filter(name="Synthetic").exists()
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()


def _plan(owner, household, name, *, private=False):
    from finance.models import PlannedItem

    return PlannedItem.objects.create(
        owner=owner, scope="private" if private else "household", household=None if private else household,
        name=name, kind="expense", amount_minor=432_109, cadence="monthly", start_date=timezone.localdate(),
    )


def _plan_payload(item, scope):
    return {"scope": scope, "name": item.name, "kind": item.kind, "amount_minor": item.amount_minor,
            "start_date": item.start_date, "end_date": item.end_date, "cadence": item.cadence}


def _budget(owner, household, name):
    from finance.budget_services import save_budget
    from finance.models import Category

    category = Category.objects.create(household=household, name=name)
    return save_budget(owner, {"scope": "household", "category": category, "amount_minor": 432_109,
                               "effective_month": _MONTH, "rollover_enabled": False})


def _budget_payload(budget, scope):
    return {"scope": scope, "category": budget.category, "amount_minor": 432_109,
            "effective_month": _MONTH, "rollover_enabled": False}


_MONTH = date(2026, 3, 1)
_TOOL_ARGS = {"list_planned_items": {}, "projected_cash_flow": {}, "list_budgets": {"month": "2026-03"}}


def _ask_quoting(state, person, tool, text="What is planned?"):
    state.need_tool = True
    state.pending_tool_calls = [[{"call_id": "c1", "name": tool, "args": _TOOL_ARGS[tool]}]]
    conversation = ask(person, text)
    state.need_tool = False
    return conversation


def _three_members(harness):
    _state, url = harness
    _a, owner, household = make_member("owner")
    _b, member, _ = make_member("member", household=household, policy=current_policy())
    _c, third, _ = make_member("third", household=household, policy=current_policy())
    for person in (owner, member, third):
        connect_harness(person, base_url=url, token=TOKEN)
    return owner, member, third, household


def _make_private(owner, row):
    from finance.budget_services import save_budget
    from finance.models import Budget
    from finance.planning_services import save_planned_item

    if isinstance(row, Budget):
        save_budget(owner, _budget_payload(row, "private"), budget=row)
    else:
        save_planned_item(owner, _plan_payload(row, "private"), item=row)


@pytest.mark.django_db
@pytest.mark.parametrize("tool", ["list_planned_items", "projected_cash_flow", "list_budgets"])
def test_making_a_quoted_household_row_private_purges_other_members_chat(harness, tool):
    state, _url = harness
    owner, member, _third, household = _three_members(harness)
    marker = "ZQHOUSEHOLDROW"
    row = _budget(owner, household, marker) if tool == "list_budgets" else _plan(owner, household, marker)
    conversation = _ask_quoting(state, member, tool)
    old_session = conversation.harness_session_id
    assert old_session
    if tool != "projected_cash_flow":
        assert marker in signed_in(member).get(reverse("chat") + f"?c={conversation.pk}").content.decode()
    owners = _ask_quoting(state, owner, tool)

    _make_private(owner, row)

    assert not AiConversation.objects.filter(pk=conversation.pk).exists()
    assert marker not in signed_in(member).get(reverse("chat") + f"?c={conversation.pk}").content.decode()
    with pytest.raises(PermissionDenied):
        ask(member, "Follow up", conversation_id=conversation.pk)
    assert ("POST", f"/api/v1/sessions/{old_session}/messages") not in state.requests
    # The owner still sees the row, so the owner's own conversation is kept.
    assert AiConversation.objects.filter(pk=owners.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("tool", ["list_planned_items", "list_budgets"])
def test_members_who_still_see_a_quoted_row_keep_their_chat(harness, tool):
    state, _url = harness
    owner, member, third, household = _three_members(harness)
    if tool == "list_budgets":
        _budget(owner, household, "ZQSTAYSSHARED")
    else:
        _plan(owner, household, "ZQSTAYSSHARED")
    conversation = _ask_quoting(state, member, tool)
    other = _budget(third, household, "ZQOTHER") if tool == "list_budgets" else _plan(third, household, "ZQOTHER")

    _make_private(third, other)

    # The conversation never quoted the row made private; what it quoted is still visible.
    assert AiConversation.objects.filter(pk=conversation.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("tool", ["list_planned_items", "projected_cash_flow", "list_budgets"])
def test_leaving_purges_chat_that_quoted_a_household_row_without_any_account(harness, tool):
    state, _url = harness
    owner, member, third, household = _three_members(harness)
    if tool == "list_budgets":
        _budget(owner, household, "ZQLEFTROW")
    else:
        _plan(owner, household, "ZQLEFTROW")
    left_behind = _ask_quoting(state, member, tool)
    assert not left_behind.used_account_ids
    stays = _ask_quoting(state, third, tool)
    owners = _ask_quoting(state, owner, tool)

    leave_household(member)

    assert not AiConversation.objects.filter(pk=left_behind.pk).exists()
    assert AiConversation.objects.filter(pk__in=(stays.pk, owners.pk)).count() == 2


@pytest.mark.django_db
def test_follow_up_does_not_resume_a_session_once_a_quoted_plan_is_out_of_sight(harness):
    from finance.models import PlannedItem

    state, _url = harness
    owner, member, _third, household = _three_members(harness)
    item = _plan(owner, household, "ZQPLANBEHINDPURGE")
    conversation = _ask_quoting(state, member, "list_planned_items")
    old_session = conversation.harness_session_id
    assert old_session

    PlannedItem.objects.filter(pk=item.pk).update(scope="private", household=None)
    creates_before = len(state.session_creates)
    send_message(member, "Follow up", conversation_id=conversation.pk)
    process_pending_turns(sleep=lambda _s: None)

    assert ("POST", f"/api/v1/sessions/{old_session}/messages") not in state.requests
    assert len(state.session_creates) == creates_before + 1
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()
