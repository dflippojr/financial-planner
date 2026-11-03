"""Markdown replies in Chromium (#634): live and saved turns match, and a wide table scrolls in its bubble."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from finance.ai_services import connect_harness
from finance.chat_services import send_message
from finance.models import AiConversation, AiConversationMessage, Person
from finance.policy_services import accept_policy, publish_policy
from tests.browser_support import CSS, SCREEN_DIR, browser, page_session  # noqa: F401
from tests.test_chat import TOKEN, harness  # noqa: F401

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="CSS must be built"),
]

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October"]
COLUMNS = ["Category", *MONTHS, "Synthetic note"]
REPLY = "\n".join([
    "## Spending by month",
    "",
    "Spending was **steady**; see [transactions](/transactions/).",
    "",
    "- Rent stayed flat",
    "- Dining rose",
    "",
    "| " + " | ".join(COLUMNS) + " |",
    "|" + "---|" * len(COLUMNS),
    "| Dining | " + " | ".join("$1,234.56" for _month in MONTHS) + " | A long synthetic remark |",
    "",
    '<img src=x onerror="window.__chatXss = 1">',
])

LAYOUT_JS = """(scope) => {
  const root = document.documentElement;
  const article = document.querySelector(scope + ' article:last-of-type');
  const table = article.querySelector('.chat-md-table');
  const a = article.getBoundingClientRect();
  const t = table.getBoundingClientRect();
  return {
    pageScroll: root.scrollWidth > root.clientWidth,
    tableScrolls: table.scrollWidth > table.clientWidth + 1,
    inside: t.left >= a.left - 0.5 && t.right <= a.right + 0.5,
    xss: window.__chatXss === 1 || !!article.querySelector('img'),
    slot: !!article.querySelector('[data-chat-turn-slot]'),
  };
}"""


def _member(url):
    person = Person.objects.get(user__username="synthetic_alex")
    accept_policy(person, publish_policy(material=True, body="Synthetic markdown policy"))
    connect_harness(person, base_url=url, token=TOKEN)
    return person


def _answer(conversation):
    turn = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
    turn.status = AiConversationMessage.Status.DONE
    turn.content = REPLY
    turn.save(update_fields=("status", "content"))


def _check(page, scope):
    layout = page.evaluate(LAYOUT_JS, scope)
    assert layout == {"pageScroll": False, "tableScrolls": True, "inside": True, "xss": False, "slot": False}
    return page.locator(f"{scope} article:last-of-type .chat-md").evaluate("(el) => el.outerHTML")


@pytest.mark.parametrize(("viewport", "scheme"), [((390, 844), "light"), ((1440, 900), "dark")], ids=["phone", "desktop"])
def test_chat_page_live_reply_matches_the_saved_one(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        # No chat lane runs beside the live server, so the reply stays pending until the test answers it.
        conversation = send_message(person, "How did spending go?")
        page.goto(page.url.split("?")[0].rstrip("/") + f"/chat/?c={conversation.pk}")
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        _answer(conversation)
        expect(page.locator("main article:last-of-type .chat-md h4")).to_have_text("Spending by month")
        live = _check(page, "main")
        if SCREEN_DIR:
            page.locator("main article:last-of-type").screenshot(path=str(Path(SCREEN_DIR) / f"chat-markdown-{scheme}.png"))
        page.reload()
        page.wait_for_load_state("networkidle")
        saved = _check(page, "main")
        assert live == saved
        assert page.locator("main .chat-md a").get_attribute("href") == "/transactions/"
    finally:
        context.close()


def test_popover_live_reply_matches_the_saved_one(page_session, harness):  # noqa: F811
    _state, url = harness
    context, page = page_session((1280, 800), "light", "/")
    try:
        _member(url)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.evaluate("() => window.AiPopover.open({origin: document.querySelector('main h1')})")
        expect(page.locator("#ai-popover")).to_be_visible()
        page.locator("#ai-popover textarea").fill("How did spending go?")
        page.locator("#ai-popover-form").get_by_role("button", name="Send").click()
        expect(page.locator("#ai-popover [data-chat-turn-url]")).to_have_count(1)
        conversation_id = page.evaluate("() => JSON.parse(sessionStorage.getItem('finance.ai-popover')).conversationId")
        _answer(AiConversation.objects.get(pk=conversation_id))
        expect(page.locator("#ai-popover article:last-of-type .chat-md h4")).to_have_text("Spending by month")
        live = _check(page, "#ai-popover")
        thread = page.locator("#ai-popover [data-pop-scroll]").evaluate("(el) => el.scrollWidth > el.clientWidth + 1")
        assert thread is False
        page.reload()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#ai-popover article:last-of-type .chat-md h4")).to_have_text("Spending by month")
        assert _check(page, "#ai-popover") == live
    finally:
        context.close()
