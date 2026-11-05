"""The checkbox item card (#659) in Chromium, at phone width in light and desktop width in dark.

Each tickable row is a label at least 44 px tall, rows with a problem have no checkbox, nothing
scrolls sideways, the card a live turn loads matches the saved turn, and unticking a row leaves it out.
"""

import pytest
from playwright.sync_api import expect

from finance.chat_proposals import proposal_tools
from finance.chat_services import send_message
from finance.models import AiConversationMessage, PlannedItem
from tests.browser_support import browser, page_session  # noqa: F401
from tests.test_chat import harness  # noqa: F401
from tests.test_chat_markdown_layout import _member

pytestmark = pytest.mark.django_db(transaction=True)

ITEMS = [
    {"name": "SYN rent for the apartment on a long synthetic street name", "kind": "expense",
     "amount_minor": 74000, "cadence": "monthly", "start_date": "2026-12-01", "end_date": "2027-06-30"},
    {"name": "SYN car insurance", "kind": "expense", "amount_minor": 13400, "cadence": "monthly",
     "start_date": "2026-12-01"},
    {"name": "SYN gym", "kind": "expense", "amount_minor": 2680, "cadence": "monthly", "start_date": "2027-01-17"},
    {"name": "SYN broken", "kind": "expense", "amount_minor": 100, "cadence": "monthly", "start_date": "2026-13-45"},
]

LAYOUT_JS = """() => {
  const root = document.documentElement;
  const card = document.querySelector('main [data-chat-proposal="planned_items"]');
  const article = card.closest('article').getBoundingClientRect();
  const box = card.getBoundingClientRect();
  const rows = Array.from(card.querySelectorAll('[data-proposal-item]'), (row) => {
    const label = row.querySelector('label');
    const input = row.querySelector('input[type=checkbox]');
    return {
      label: label ? label.getBoundingClientRect().height : 0,
      labelWidth: label ? label.getBoundingClientRect().width : 0,
      checked: input ? input.checked : null,
      problem: !!row.querySelector('[data-proposal-item-problem]'),
      inside: row.getBoundingClientRect().right <= box.right + 0.5,
    };
  });
  const buttons = Array.from(card.querySelectorAll('button'), (b) => b.getBoundingClientRect().height);
  const style = getComputedStyle(card);
  return {
    rows, buttons,
    pageScroll: root.scrollWidth > root.clientWidth,
    inside: box.left >= article.left - 0.5 && box.right <= article.right + 0.5,
    colored: style.color !== style.backgroundColor,
  };
}"""


def _propose_and_answer(person, conversation):
    turn = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
    tool = next(spec for spec in proposal_tools(conversation, turn) if spec.name == "propose_planned_items")
    assert tool.handler(person, {"items": ITEMS}).ok
    turn.status = AiConversationMessage.Status.DONE
    turn.content = "Here are the planned items. Nothing changes until you apply."
    turn.save(update_fields=("status", "content"))


def _card_text(page):
    return page.locator('main [data-chat-proposal="planned_items"]').inner_text()


@pytest.mark.parametrize(("viewport", "scheme"), [((390, 844), "light"), ((1440, 900), "dark")], ids=["phone", "desktop"])
def test_checkbox_card_layout_and_partial_apply(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        conversation = send_message(person, "Plan rent, car insurance and the gym.")
        page.goto(page.url.split("?")[0].rstrip("/") + f"/chat/?c={conversation.pk}")
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        _propose_and_answer(person, conversation)
        # The live turn loads the page to show the server-rendered card.
        card = page.locator('main [data-chat-proposal="planned_items"]')
        expect(card).to_be_visible(timeout=15000)
        page.wait_for_load_state("networkidle")
        live = _card_text(page)
        page.reload()
        page.wait_for_load_state("networkidle")
        assert _card_text(page) == live

        layout = page.evaluate(LAYOUT_JS)
        assert not layout["pageScroll"] and layout["inside"] and layout["colored"]
        assert [row["checked"] for row in layout["rows"]] == [True, True, True, None]
        assert [row["problem"] for row in layout["rows"]] == [False, False, False, True]
        assert all(row["label"] >= 44 and row["inside"] for row in layout["rows"][:3])
        assert all(height >= 44 for height in layout["buttons"])

        # Tap the second row's label to untick it, then apply.
        card.locator('[data-proposal-item="1"] label').click()
        expect(card.locator('[data-proposal-item="1"] input')).not_to_be_checked()
        card.get_by_role("button", name="Apply ticked items").click()
        page.wait_for_load_state("networkidle")
        expect(page.locator('main [data-chat-proposal="planned_items"] [role="status"]')).to_have_text(
            "Applied 2 of 4 items."
        )
        assert sorted(PlannedItem.objects.values_list("name", flat=True)) == sorted([ITEMS[0]["name"], ITEMS[2]["name"]])
        expect(page.locator('main [data-proposal-item="1"] [data-proposal-item-outcome]')).to_have_text(
            "Skipped: not ticked."
        )
        assert not page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
    finally:
        context.close()
