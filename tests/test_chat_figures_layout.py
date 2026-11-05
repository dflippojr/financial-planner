"""Live and saved chat turns both start with Sources collapsed, and it opens on click (owner, 2026-11-04)."""

import pytest
from playwright.sync_api import expect

from finance.chat_services import send_message
from finance.models import AiConversationMessage
from tests.browser_support import browser, page_session  # noqa: F401
from tests.test_chat import harness  # noqa: F401
from tests.test_chat_markdown_layout import _member

pytestmark = pytest.mark.django_db(transaction=True)

FIGURES = [
    {"label": "Total spending", "amount_minor": 123456, "currency": "USD", "amount_display": "$1,234.56",
     "url": "/spending/"},
    {"label": "SYN-DINING spending", "amount_minor": 4200, "currency": "USD", "amount_display": "$42.00",
     "url": "/transactions/"},
]


def _answer(conversation):
    turn = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
    turn.status = AiConversationMessage.Status.DONE
    turn.content = "Spending was steady."
    turn.figures = FIGURES
    turn.save(update_fields=("status", "content", "figures"))


def _assert_collapsed_then_opens(page):
    sources = page.locator("main article:last-of-type details[data-chat-figures]")
    expect(sources).to_have_count(1)
    expect(sources.locator("summary")).to_have_text("Sources (2)")
    assert sources.evaluate("(el) => el.open") is False
    link = sources.get_by_role("link", name="Total spending: $1,234.56")
    expect(link).to_be_hidden()
    sources.locator("summary").click()
    expect(link).to_be_visible()
    assert link.get_attribute("href") == "/spending/"
    assert sources.locator("summary").evaluate("(el) => el.getBoundingClientRect().height") >= 44


@pytest.mark.parametrize(("viewport", "scheme"), [((390, 844), "light"), ((1440, 900), "dark")], ids=["phone", "desktop"])
def test_live_and_saved_sources_start_collapsed(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        conversation = send_message(person, "How did spending go?")
        page.goto(page.url.split("?")[0].rstrip("/") + f"/chat/?c={conversation.pk}")
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        _answer(conversation)
        expect(page.locator("main article:last-of-type .chat-md")).to_contain_text("Spending was steady.")
        _assert_collapsed_then_opens(page)
        page.reload()
        page.wait_for_load_state("networkidle")
        _assert_collapsed_then_opens(page)
    finally:
        context.close()
