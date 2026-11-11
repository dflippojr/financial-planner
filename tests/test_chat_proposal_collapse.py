"""A chat "Suggested change" card that is no longer pending collapses to its one-line status (#721).

Every card kind, after Apply and after Dismiss, and a stale card, in the chat page and the
#ai-popover: no item rows, transaction list or match table, only the summary and the status line.
A pending card still shows its rows.
"""

import re
from datetime import date

import pytest
from django.urls import reverse

from finance.budget_services import save_budget
from finance.models import AiProposal
from tests.test_chat import add_txn, checking
from tests.test_chat import harness as harness_fixture  # noqa: F401 - registers the harness fixture
from tests.test_chat_proposals import _category, _propose, _setup

# Markup that belongs only to an expanded card: the transaction list, the rules page's match
# table, the item rows, and the Apply / Dismiss forms.
EXPANDED_MARKERS = ("<ul", "<table", "data-proposal-items", "data-proposal-item=", 'name="item"', "<form")


def _card_set_category(state, person, household):
    account = checking(person, household, "Shared Checking")
    ids = [add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe").pk,
           add_txn(account, person, date(2026, 1, 4), -800, "Synthetic bakery").pk]
    _propose(state, person, "propose_set_category", {"transaction_ids": ids, "category": "Dining"})
    return ["Synthetic cafe", "Synthetic bakery"]


def _card_add_tags(state, person, household):
    account = checking(person, household, "Shared Checking")
    txn = add_txn(account, person, date(2026, 1, 3), -1200, "Synthetic cafe")
    _propose(state, person, "propose_add_tags", {"transaction_ids": [txn.pk], "tags": ["Trip"]})
    return ["Synthetic cafe"]


def _card_create_rule(state, person, household):
    account = checking(person, household, "Shared Checking")
    add_txn(account, person, date(2026, 1, 3), -1200, "SYNTHETIC CAFE 1")
    add_txn(account, person, date(2026, 1, 9), -900, "Synthetic Cafe 2")
    _propose(state, person, "propose_create_rule", {"description_contains": "synthetic cafe", "category": "Dining"})
    return ["SYNTHETIC CAFE 1", "Synthetic Cafe 2"]


def _card_set_budget(state, person, household):
    _propose(state, person, "propose_set_budget", {"category": "Dining", "month": "2026-03", "amount_minor": 45000})
    return []


def _card_planned_items(state, person, household):
    _propose(state, person, "propose_planned_items", {"items": [
        {"name": "Synthetic rent", "kind": "expense", "amount_minor": 74000, "cadence": "monthly",
         "start_date": "2026-12-01"},
        {"name": "Synthetic gym", "kind": "expense", "amount_minor": 2680, "cadence": "monthly",
         "start_date": "2027-01-17"},
    ]})
    return ["Synthetic rent", "Synthetic gym"]


CARDS = {
    "set_category": _card_set_category,
    "add_tags": _card_add_tags,
    "create_rule": _card_create_rule,
    "set_budget": _card_set_budget,
    "planned_items": _card_planned_items,
}

STATUS_LINES = {
    "applied": {
        "set_category": "Applied to 2.",
        "add_tags": "Applied to 1.",
        "create_rule": "Applied to 2.",
        "set_budget": "Applied.",
        "planned_items": "Applied 2 of 2 items.",
    },
}


def _card_html(client, proposal, conversation_id, place):
    if place == "chat":
        html = client.get(reverse("chat")).content.decode()
        section_id = f"proposal-{proposal.pk}"
    else:
        response = client.get(reverse("chat-popover") + f"?c={conversation_id}", HTTP_ACCEPT="application/json")
        html = response.json()["html"]
        section_id = f"popover-proposal-{proposal.pk}"
    match = re.search(rf'<section id="{section_id}"[^>]*>.*?</section>', html, re.DOTALL)
    assert match, f"no card {section_id} in the {place} html"
    return match.group(0)


def _assert_collapsed(card, rows, status_line):
    for marker in EXPANDED_MARKERS:
        assert marker not in card, f"collapsed card still renders {marker!r}:\n{card}"
    for row in rows:
        assert row not in card, f"collapsed card still lists {row!r}:\n{card}"
    assert card.count('role="status"') + card.count("Dismissed.") == 1, card
    assert status_line in card, card


def _resolve(client, proposal, outcome):
    if outcome == "applied":
        data = {}
        if proposal.kind == AiProposal.Kind.PLANNED_ITEMS:
            data = {"item_selection": "1", "item": ["0", "1"]}
        client.post(reverse("chat-proposal-apply", args=[proposal.pk]), data)
    else:
        client.post(reverse("chat-proposal-dismiss", args=[proposal.pk]))
    proposal.refresh_from_db()
    assert proposal.status == outcome


@pytest.mark.django_db
@pytest.mark.parametrize("place", ["chat", "popover"])
@pytest.mark.parametrize("outcome", ["applied", "dismissed"])
@pytest.mark.parametrize("kind", list(CARDS))
def test_a_resolved_card_collapses_to_its_status_line(harness, kind, outcome, place):
    state, client, person, household = _setup(harness)
    rows = CARDS[kind](state, person, household)
    proposal = AiProposal.objects.get()
    assert proposal.kind == kind

    pending = _card_html(client, proposal, proposal.conversation_id, place)
    assert "<form" in pending and all(row in pending for row in rows)  # a pending card is unchanged

    _resolve(client, proposal, outcome)
    card = _card_html(client, proposal, proposal.conversation_id, place)
    status_line = "Dismissed." if outcome == "dismissed" else STATUS_LINES["applied"][kind]
    _assert_collapsed(card, rows, status_line)


@pytest.mark.django_db
@pytest.mark.parametrize("place", ["chat", "popover"])
def test_a_stale_card_collapses_to_its_reason(harness, place):
    state, client, person, household = _setup(harness)
    dining = _category(household)
    budget = save_budget(
        person,
        {"scope": "household", "category": dining, "effective_month": date(2026, 3, 1), "amount_minor": 30000},
    )
    rows = _card_planned_items(state, person, household)
    planned = AiProposal.objects.get()
    _propose(state, person, "propose_set_budget", {"category": "Dining", "month": "2026-03", "amount_minor": 40000})
    save_budget(
        person,
        {"scope": "household", "category": dining, "effective_month": date(2026, 3, 1), "amount_minor": 35000},
        budget=budget,
    )
    stale_budget = AiProposal.objects.exclude(pk=planned.pk).get()
    client.post(reverse("chat-proposal-apply", args=[stale_budget.pk]))
    stale_budget.refresh_from_db()
    assert stale_budget.status == AiProposal.Status.STALE

    # An item card marked stale keeps its rows on main; it must collapse like the rest.
    AiProposal.objects.filter(pk=planned.pk).update(status=AiProposal.Status.STALE, result={"reason": "Synthetic reason."})
    planned.refresh_from_db()
    _assert_collapsed(_card_html(client, planned, planned.conversation_id, place), rows, "Synthetic reason.")
    _assert_collapsed(_card_html(client, stale_budget, stale_budget.conversation_id, place), [], 'role="status"')


def test_the_apply_message_carries_the_reasons_the_collapsed_card_no_longer_shows():
    from types import SimpleNamespace

    from finance.chat_views import _applied_message

    proposal = SimpleNamespace(result={"count": 1, "items": [
        {"status": "created"},
        {"status": "failed", "message": "Synthetic refusal."},
        {"status": "stale", "message": "Synthetic change."},
        {"status": "skipped"},
    ]})
    text = _applied_message(proposal)
    assert text == "Applied 1 item. 2 ticked items could not be applied. Synthetic refusal. Synthetic change."
    assert "the card shows why" not in text
