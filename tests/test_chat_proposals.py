"""Chat proposals: the model can suggest changes, but only an Apply POST from the member writes."""

from datetime import date, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.ai_services import connect_harness
from finance.budget_services import amount_for, save_budget
from finance.category_services import ensure_household_categories
from finance.chat_proposals import (
    VIA_CHAT,
    ProposalError,
    apply_proposal,
    dismiss_proposal,
    proposal_tools,
)
from finance.chat_runner import process_pending_turns
from finance.chat_services import send_message
from finance.models import (
    AiConversation,
    AiProposal,
    Budget,
    Category,
    CategoryRule,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
)
from finance.policy_services import current_policy
from tests.chat_helpers import ask
from tests.test_chat import TOKEN, add_txn, checking, harness, make_member  # noqa: F401 - harness is a fixture
from tests.test_security_headers import assert_page_is_csp_clean


def no_sleep(_seconds):
    return None


def _setup(harness, username="owner"):
    state, url = harness
    user, person, household = make_member(username)
    connect_harness(person, base_url=url, token=TOKEN)
    client = Client()
    client.force_login(user)
    return state, client, person, household


def _propose(state, person, name, args, text="Here is a suggestion."):
    state.need_tool = True
    state.session_answer = text
    state.pending_tool_calls = [[{"call_id": "p1", "name": name, "args": args}]]
    return ask(person, "Please suggest a change.", sleep=no_sleep)


def _category(household, name="Dining"):
    ensure_household_categories(household)
    return Category.objects.get(household=household, name=name)


@pytest.mark.django_db
def test_a_proposal_alone_writes_nothing(harness):
    state, _client, person, household = _setup(harness)
    account = checking(person, household, "Shared Checking")
    txn = add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe")
    conversation = _propose(
        state, person, "propose_set_category", {"transaction_ids": [txn.pk], "category": "Dining"}
    )
    txn.refresh_from_db()
    assert txn.category_id is None
    proposal = AiProposal.objects.get()
    assert proposal.status == AiProposal.Status.PENDING
    assert proposal.conversation_id == conversation.pk
    assert not TransactionCorrectionHistory.objects.exists()


@pytest.mark.django_db
def test_set_category_applies_through_the_post_and_labels_history(harness):
    state, client, person, household = _setup(harness)
    account = checking(person, household, "Shared Checking")
    one = add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe")
    two = add_txn(account, person, date(2026, 1, 4), -800, "Synthetic bakery")
    _propose(state, person, "propose_set_category", {"transaction_ids": [one.pk, two.pk], "category": "dining"})
    proposal = AiProposal.objects.get()

    page = client.get(reverse("chat"))
    body = page.content.decode()
    assert "Synthetic cafe" in body and "Synthetic bakery" in body
    assert reverse("chat-proposal-apply", args=[proposal.pk]) in body
    assert_page_is_csp_clean(page)

    response = client.post(reverse("chat-proposal-apply", args=[proposal.pk]))
    assert response.status_code == 302
    for txn in (one, two):
        txn.refresh_from_db()
        assert txn.category_id == _category(household).pk
        assert txn.category_source == Transaction.CategorySource.MANUAL
    entry = TransactionCorrectionHistory.objects.filter(transaction=one).get()
    assert entry.new_description == f"Dining{VIA_CHAT}"
    proposal.refresh_from_db()
    assert proposal.status == AiProposal.Status.APPLIED
    again = client.post(reverse("chat-proposal-apply", args=[proposal.pk]))
    assert again.status_code == 302
    assert TransactionCorrectionHistory.objects.filter(transaction=one).count() == 1


@pytest.mark.django_db
def test_dismiss_writes_nothing_and_ends_the_card(harness):
    state, client, person, household = _setup(harness)
    account = checking(person, household, "Shared Checking")
    txn = add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe")
    _propose(state, person, "propose_set_category", {"transaction_ids": [txn.pk], "category": "Dining"})
    proposal = AiProposal.objects.get()
    client.post(reverse("chat-proposal-dismiss", args=[proposal.pk]))
    proposal.refresh_from_db()
    assert proposal.status == AiProposal.Status.DISMISSED
    client.post(reverse("chat-proposal-apply", args=[proposal.pk]))
    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_invalid_and_foreign_ids_are_dropped_with_a_neutral_message(harness):
    state, _client, person, household = _setup(harness)
    _u, other, _ = make_member("other", household=household, policy=current_policy())
    mine = add_txn(checking(person, household, "Shared Checking"), person, date(2026, 1, 3), -100, "Synthetic mine")
    theirs = add_txn(
        checking(other, household, "Other Private", private=True), other, date(2026, 1, 3), -100, "Other secret"
    )
    _propose(
        state,
        person,
        "propose_set_category",
        {"transaction_ids": [mine.pk, theirs.pk, 999999], "category": "Dining"},
    )
    proposal = AiProposal.objects.get()
    assert [item["id"] for item in proposal.payload["items"]] == [mine.pk]
    outputs = "\n".join(state.tool_outputs)
    assert "left out" in outputs
    assert str(theirs.pk) not in outputs and "Other secret" not in outputs

    AiProposal.objects.all().delete()
    _propose(state, person, "propose_set_category", {"transaction_ids": [theirs.pk], "category": "Dining"})
    assert not AiProposal.objects.exists()
    assert "Other secret" not in "\n".join(state.tool_outputs)


@pytest.mark.django_db
def test_a_forged_apply_on_another_members_proposal_is_refused(harness):
    state, _client, person, household = _setup(harness)
    user_b, _other, _ = make_member("intruder", household=household, policy=current_policy())
    txn = add_txn(checking(person, household, "Shared Checking"), person, date(2026, 1, 3), -100, "Synthetic cafe")
    _propose(state, person, "propose_set_category", {"transaction_ids": [txn.pk], "category": "Dining"})
    proposal = AiProposal.objects.get()
    intruder = Client()
    intruder.force_login(user_b)
    for name in ("chat-proposal-apply", "chat-proposal-dismiss"):
        response = intruder.post(reverse(name, args=[proposal.pk]), follow=True)
        assert "That suggestion is not available." in response.content.decode()
    proposal.refresh_from_db()
    txn.refresh_from_db()
    assert proposal.status == AiProposal.Status.PENDING
    assert txn.category_id is None
    assert "Synthetic cafe" not in intruder.get(reverse("chat")).content.decode()


@pytest.mark.django_db
def test_apply_is_refused_when_the_transaction_changed_since_the_proposal(harness):
    state, client, person, household = _setup(harness)
    txn = add_txn(checking(person, household, "Shared Checking"), person, date(2026, 1, 3), -100, "Synthetic cafe")
    _propose(state, person, "propose_set_category", {"transaction_ids": [txn.pk], "category": "Dining"})
    proposal = AiProposal.objects.get()
    txn.category = _category(household, "Groceries")
    txn.category_source = Transaction.CategorySource.MANUAL
    txn.save()
    page = client.post(reverse("chat-proposal-apply", args=[proposal.pk]), follow=True)
    assert "no longer applies" in page.content.decode()
    txn.refresh_from_db()
    assert txn.category.name == "Groceries"
    proposal.refresh_from_db()
    assert proposal.status == AiProposal.Status.STALE


@pytest.mark.django_db
def test_rule_proposal_shows_the_rules_page_preview_and_applies(harness):
    state, client, person, household = _setup(harness)
    account = checking(person, household, "Shared Checking")
    add_txn(account, person, date(2026, 1, 3), -1200, "SYNTHETIC CAFE 1")
    add_txn(account, person, date(2026, 1, 9), -900, "Synthetic Cafe 2")
    add_txn(account, person, date(2026, 1, 9), -900, "Unrelated grocer")
    _propose(
        state,
        person,
        "propose_create_rule",
        {"description_contains": "synthetic cafe", "category": "Dining"},
    )
    assert not CategoryRule.objects.exists()
    proposal = AiProposal.objects.get()
    page = client.get(reverse("chat")).content.decode()
    assert "SYNTHETIC CAFE 1" in page and "Synthetic Cafe 2" in page and "Unrelated grocer" not in page
    assert '<th scope="col">Category</th>' in page  # the rules page's own preview table

    client.post(reverse("chat-proposal-apply", args=[proposal.pk]))
    rule = CategoryRule.objects.get()
    assert rule.confirmed_at is not None
    assert Transaction.objects.filter(category=_category(household), category_source="rule").count() == 2
    history = TransactionCorrectionHistory.objects.filter(field_name="category")
    assert history.count() == 2 and all(row.new_description.endswith(VIA_CHAT) for row in history)
    assert "Open rule" in client.get(reverse("chat")).content.decode()


@pytest.mark.django_db
def test_rule_proposal_is_stale_when_the_matches_changed(harness):
    state, client, person, household = _setup(harness)
    account = checking(person, household, "Shared Checking")
    add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe")
    _propose(state, person, "propose_create_rule", {"description_contains": "cafe", "category": "Dining"})
    add_txn(account, person, date(2026, 1, 5), -300, "Another cafe")
    client.post(reverse("chat-proposal-apply", args=[AiProposal.objects.get().pk]))
    assert not CategoryRule.objects.exists()
    assert AiProposal.objects.get().status == AiProposal.Status.STALE


@pytest.mark.django_db
def test_budget_proposal_creates_and_adjusts(harness):
    state, client, person, _household = _setup(harness)
    args = {"category": "Dining", "month": "2026-03", "amount_minor": 45000}
    _propose(state, person, "propose_set_budget", args)
    assert not Budget.objects.exists()
    proposal = AiProposal.objects.get()
    assert "450.00" in client.get(reverse("chat")).content.decode()
    client.post(reverse("chat-proposal-apply", args=[proposal.pk]))
    budget = Budget.objects.get()
    assert amount_for(budget, date(2026, 3, 1)) == 45000

    AiProposal.objects.all().delete()
    _propose(state, person, "propose_set_budget", {**args, "amount_minor": 50000})
    adjust = AiProposal.objects.get()
    assert adjust.payload["before_amount_minor"] == 45000
    client.post(reverse("chat-proposal-apply", args=[adjust.pk]))
    assert Budget.objects.count() == 1
    assert amount_for(budget, date(2026, 3, 1)) == 50000


@pytest.mark.django_db
def test_budget_proposal_is_stale_when_the_budget_moved(harness):
    state, client, person, household = _setup(harness)
    dining = _category(household)
    budget = save_budget(
        person,
        {"scope": "household", "category": dining, "effective_month": date(2026, 3, 1), "amount_minor": 30000},
    )
    _propose(state, person, "propose_set_budget", {"category": "Dining", "month": "2026-03", "amount_minor": 40000})
    save_budget(
        person,
        {"scope": "household", "category": dining, "effective_month": date(2026, 3, 1), "amount_minor": 35000},
        budget=budget,
    )
    client.post(reverse("chat-proposal-apply", args=[AiProposal.objects.get().pk]))
    assert amount_for(budget, date(2026, 3, 1)) == 35000
    assert AiProposal.objects.get().status == AiProposal.Status.STALE


@pytest.mark.django_db
def test_tag_proposal_adds_tags_and_labels_history(harness):
    state, client, person, household = _setup(harness)
    account = checking(person, household, "Shared Checking")
    one = add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe")
    Tag.objects.create(household=household, name="Trip")
    _propose(state, person, "propose_add_tags", {"transaction_ids": [one.pk], "tags": ["trip", "Work lunch"]})
    assert not one.tags.exists()
    client.post(reverse("chat-proposal-apply", args=[AiProposal.objects.get().pk]))
    assert sorted(tag.name for tag in one.tags.all()) == ["Trip", "Work lunch"]
    assert Tag.objects.filter(name__iexact="trip").count() == 1
    entry = TransactionCorrectionHistory.objects.get(transaction=one, field_name="tags")
    assert entry.new_description.endswith(VIA_CHAT)


@pytest.mark.django_db
def test_proposals_expire_with_their_conversation(harness):
    state, _client, person, household = _setup(harness)
    txn = add_txn(checking(person, household, "Shared Checking"), person, date(2026, 1, 3), -100, "Synthetic cafe")
    conversation = _propose(state, person, "propose_set_category", {"transaction_ids": [txn.pk], "category": "Dining"})
    proposal = AiProposal.objects.get()
    AiConversation.objects.filter(pk=conversation.pk).update(expires_at=timezone.now() - timedelta(days=1))
    with pytest.raises(Exception):
        apply_proposal(person, proposal.pk)
    txn.refresh_from_db()
    assert txn.category_id is None


@pytest.mark.django_db
def test_chat_offers_only_propose_tools_and_resolved_proposals_stay_resolved(harness):
    _state, _client, person, _household = _setup(harness)
    conversation = send_message(person, "hello")
    turn = conversation.messages.get(status="pending")
    names = {tool.name for tool in proposal_tools(conversation, turn)}
    assert names == {"propose_set_category", "propose_create_rule", "propose_set_budget", "propose_add_tags"}
    process_pending_turns(sleep=no_sleep)
    assert not Transaction.objects.exists() and not CategoryRule.objects.exists()
    resolved = AiProposal.objects.create(
        conversation=conversation, message=turn, kind="set_budget", payload={}, status="applied"
    )
    with pytest.raises(ProposalError):
        dismiss_proposal(person, resolved.pk)


@pytest.mark.django_db
def test_confirmed_proposal_audit_event_keeps_approver_and_proposal_after_conversation_deletion(harness):
    from finance.models import AuditEvent

    state, client, person, household = _setup(harness)
    account = checking(person, household, "Shared Checking")
    txn = add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe")
    conversation = _propose(state, person, "propose_set_category", {"transaction_ids": [txn.pk], "category": "Dining"})
    proposal = AiProposal.objects.get()
    assert client.post(reverse("chat-proposal-apply", args=[proposal.pk])).status_code == 302
    event = AuditEvent.objects.get(action="transaction_corrected")
    assert (event.actor_id, event.source, event.metadata["proposal_id"]) == (person.pk, "chat_confirmation", proposal.pk)
    assert event.target_id == txn.pk and event.account_id == account.pk
    conversation.delete()
    assert not AiProposal.objects.exists()
    assert AuditEvent.objects.filter(pk=event.pk).exists()
