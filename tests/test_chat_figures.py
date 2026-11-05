"""Chat figures sit in a collapsed Sources block on saved turns (owner, 2026-11-04)."""

import json
import re

import pytest
from django.test import Client
from django.urls import reverse

from finance.ai_services import connect_harness
from finance.chat_runner import process_pending_turns
from finance.chat_services import send_message
from finance.models import AiConversationMessage
from tests.test_chat import TOKEN, harness, make_member  # noqa: F401 - harness is a fixture

FIGURES = [
    {"label": "Total spending", "amount_minor": 123456, "currency": "USD", "amount_display": "$1,234.56",
     "url": "/spending/?date_from=2026-01-01&date_to=2026-01-31"},
    {"label": "SYN-DINING spending", "amount_minor": 4200, "currency": "USD", "amount_display": "$42.00",
     "url": "/transactions/?category=1"},
]


def _turn_with_figures(harness, username, figures=FIGURES):  # noqa: F811 - pytest fixture value
    state, url = harness
    user, person, _household = make_member(username)
    connect_harness(person, base_url=url, token=TOKEN)
    state.session_answer = "Spending was steady."
    conversation = send_message(person, "How did January go?")
    assert process_pending_turns(sleep=lambda _s: None) == 1
    turn = conversation.messages.get(role=AiConversationMessage.Role.ASSISTANT)
    turn.figures = figures
    turn.save(update_fields=("figures",))
    client = Client()
    client.force_login(user)
    return client, conversation, turn


def _sources_block(html):
    match = re.search(r"<details[^>]*data-chat-figures[^>]*>.*?</details>", html, re.S)
    assert match, "the saved turn has no Sources block"
    return match.group(0)


@pytest.mark.django_db
def test_saved_turn_figures_are_collapsed_under_a_counted_sources_summary(harness):  # noqa: F811
    client, conversation, _turn = _turn_with_figures(harness, "figures-saved")
    block = _sources_block(client.get(reverse("chat") + f"?c={conversation.pk}").content.decode())
    assert " open" not in block.split(">", 1)[0]
    assert "<summary" in block and "Sources (2)" in block
    assert block.count("<li>") == 2
    assert 'href="/spending/?date_from=2026-01-01&amp;date_to=2026-01-31"' in block
    assert "Total spending: $1,234.56" in block


@pytest.mark.django_db
def test_popover_turn_figures_are_collapsed_too(harness):  # noqa: F811
    client, conversation, _turn = _turn_with_figures(harness, "figures-popover")
    popover = client.get(reverse("chat-popover") + f"?c={conversation.pk}", HTTP_ACCEPT="application/json").json()
    block = _sources_block(popover["html"])
    assert " open" not in block.split(">", 1)[0]
    assert "Sources (2)" in block


@pytest.mark.django_db
def test_turn_without_figures_has_no_sources_block(harness):  # noqa: F811
    client, conversation, _turn = _turn_with_figures(harness, "figures-none", figures=[])
    page = client.get(reverse("chat") + f"?c={conversation.pk}").content.decode()
    assert "data-chat-figures" not in page
    assert "Sources (" not in page


@pytest.mark.django_db
def test_live_turn_still_returns_the_figures_the_script_collapses(harness):  # noqa: F811
    client, _conversation, turn = _turn_with_figures(harness, "figures-live")
    live = json.loads(client.get(reverse("chat-turn", args=[turn.pk])).content)
    assert [item["label"] for item in live["figures"]] == ["Total spending", "SYN-DINING spending"]
