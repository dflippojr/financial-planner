"""Cleanup cards (#661) in Chromium, at phone width in light and desktop width in dark.

A notes card with a long unbroken note and a splits card with one split whose parts don't add up
stay inside the reply without sideways scrolling; tickable rows are labels at least 44 px tall, the
refused split shows its parts and reason with no checkbox, and applying writes only the ticked note.
"""

from datetime import date

import pytest
from playwright.sync_api import expect

from finance.category_services import current_household, ensure_household_categories
from finance.chat_proposals import proposal_tools
from finance.chat_services import send_message
from finance.models import AiConversationMessage, Transaction
from tests.browser_support import browser, page_session  # noqa: F401
from tests.test_chat import add_txn, checking
from tests.test_chat import harness  # noqa: F401
from tests.test_chat_markdown_layout import _member

pytestmark = pytest.mark.django_db(transaction=True)

LONG_NOTE = "SYN-" + "reimbursable" * 12

LAYOUT_JS = """() => {
  const root = document.documentElement;
  return Array.from(document.querySelectorAll('main [data-chat-proposal]'), (card) => {
    const article = card.closest('article').getBoundingClientRect();
    const box = card.getBoundingClientRect();
    return {
      kind: card.dataset.chatProposal,
      inside: box.left >= article.left - 0.5 && box.right <= article.right + 0.5,
      rows: Array.from(card.querySelectorAll('[data-proposal-item]'), (row) => ({
        label: row.querySelector('label') ? row.querySelector('label').getBoundingClientRect().height : 0,
        problem: !!row.querySelector('[data-proposal-item-problem]'),
        inside: row.getBoundingClientRect().right <= box.right + 0.5,
      })),
      pageScroll: root.scrollWidth > root.clientWidth,
    };
  });
}"""


def _rows(person):
    ensure_household_categories(current_household(person))
    account = checking(person, None, "SYN Cleanup Checking", private=True)
    cafe = add_txn(account, person, date(2026, 3, 3), -1200, "SYN cafe with a long synthetic description")
    kiosk = add_txn(account, person, date(2026, 3, 4), -300, "SYN kiosk")
    store = add_txn(account, person, date(2026, 3, 5), -6000, "SYN big box store")
    shop = add_txn(account, person, date(2026, 3, 6), -900, "SYN corner shop")
    return cafe, kiosk, store, shop


def _propose_and_answer(person, conversation, cafe, kiosk, store, shop):
    turn = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
    tools = {spec.name: spec for spec in proposal_tools(conversation, turn)}
    assert tools["propose_notes"].handler(person, {"items": [
        {"transaction_id": cafe.pk, "note": LONG_NOTE}, {"transaction_id": kiosk.pk, "note": "Snack"},
    ]}).ok
    assert tools["propose_splits"].handler(person, {"items": [{"transaction_id": store.pk, "parts": [
        {"category": "Groceries", "amount_minor": 4000}, {"category": "Shopping", "amount_minor": 2500},
    ]}, {"transaction_id": shop.pk, "parts": [
        {"category": "Groceries", "amount_minor": 450}, {"category": "Shopping", "amount_minor": 450},
    ]}]}).ok
    turn.status = AiConversationMessage.Status.DONE
    turn.content = "Here are the notes and the split. Nothing changes until you apply."
    turn.save(update_fields=("status", "content"))


@pytest.mark.parametrize(("viewport", "scheme"), [((390, 844), "light"), ((1440, 900), "dark")], ids=["phone", "desktop"])
def test_cleanup_cards_layout_and_partial_apply(page_session, harness, viewport, scheme, live_requests_paused):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        # The test thread has its own SQLite connection, so its database work waits for the page's
        # requests rather than overlapping them (#738); the pending turn is answered mid-poll.
        with live_requests_paused():
            person = _member(url)
            cafe, kiosk, store, shop = _rows(person)
            conversation = send_message(person, "Add notes and split the store run.")
        page.goto(page.url.split("?")[0].rstrip("/") + f"/chat/?c={conversation.pk}")
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        with live_requests_paused():
            _propose_and_answer(person, conversation, cafe, kiosk, store, shop)
        notes = page.locator('main [data-chat-proposal="notes"]')
        expect(notes).to_be_visible(timeout=15000)
        page.wait_for_load_state("networkidle")

        layout = {card["kind"]: card for card in page.evaluate(LAYOUT_JS)}
        assert set(layout) == {"notes", "splits"}
        for card in layout.values():
            assert card["inside"] and not card["pageScroll"] and all(row["inside"] for row in card["rows"])
        assert all(row["label"] >= 44 and not row["problem"] for row in layout["notes"]["rows"])
        refused, valid = layout["splits"]["rows"]
        assert refused == {"label": 0, "problem": True, "inside": True}
        assert valid["label"] >= 44 and not valid["problem"]
        splits = page.locator('main [data-chat-proposal="splits"]')
        expect(splits).to_contain_text("Groceries 40.00 USD · Shopping 25.00 USD")
        expect(splits).to_contain_text("Parts must add up exactly to the transaction amount.")

        notes.locator('[data-proposal-item="1"] label').click()
        expect(notes.locator('[data-proposal-item="1"] input')).not_to_be_checked()
        notes.get_by_role("button", name="Apply ticked items").click()
        page.wait_for_load_state("networkidle")
        expect(page.locator('main [data-chat-proposal="notes"] [role="status"]')).to_have_text("Applied 1 of 2 items.")
        with live_requests_paused():
            assert Transaction.objects.get(pk=cafe.pk).note == LONG_NOTE
            assert Transaction.objects.get(pk=kiosk.pk).note == ""
        assert not page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
    finally:
        context.close()
