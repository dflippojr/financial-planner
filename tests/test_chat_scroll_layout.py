"""Chat lands at the newest turn (#686) in Chromium, at phone width in light and desktop width in dark.

A long conversation loads at its latest exchange, sending a question lands on that question with
the pending reply below it, a reply with proposal cards reloads with the cards in view, and the
redirect's anchor does the same work when JavaScript is off.
"""

import pytest
from playwright.sync_api import expect

from finance.chat_proposals import proposal_tools
from finance.chat_services import start_conversation
from finance.models import AiConversationMessage
from tests.browser_support import browser, page_session  # noqa: F401
from tests.test_chat import harness  # noqa: F401
from tests.test_chat_markdown_layout import _member

pytestmark = pytest.mark.django_db(transaction=True)

VIEWPORTS = [((390, 844), "light"), ((1440, 900), "dark")]
FILLER = "SYN a synthetic line that wraps across the bubble so each turn takes real height. " * 4

# Where an element sits against the part of the viewport the composer does not cover. On a phone
# the composer is fixed above the dock; on a desktop it is sticky at the bottom of the thread.
IN_VIEW_JS = """(selector) => {
  const el = document.querySelector(selector);
  const composer = document.querySelector('main #chat-send-form').getBoundingClientRect();
  const box = el.getBoundingClientRect();
  const bottom = Math.min(window.innerHeight, composer.top);
  return {top: box.top, bottom: box.bottom, visibleBottom: bottom, scrollY: window.scrollY,
          inView: box.top >= -0.5 && box.top < bottom - 20};
}"""


def _long_conversation(person, pairs=12):
    conversation = start_conversation(person)
    for index in range(pairs):
        question = AiConversationMessage.objects.create(
            conversation=conversation, role=AiConversationMessage.Role.USER, content=f"Question {index}? {FILLER}",
        )
        AiConversationMessage.objects.create(
            conversation=conversation, role=AiConversationMessage.Role.ASSISTANT,
            content=f"Answer {index}. {FILLER}", reply_to=question,
        )
    return conversation


def _latest_question(conversation):
    return conversation.messages.filter(role=AiConversationMessage.Role.USER).order_by("-created_at", "-pk").first()


def _chat_url(page, conversation):
    return page.url.split("?")[0].split("#")[0].rstrip("/") + f"/chat/?c={conversation.pk}"


def _send(page, text):
    page.locator("#chat-send-form textarea[name=prompt]").fill(text)
    page.locator("#chat-send-form").get_by_role("button", name="Send").click()
    page.wait_for_url("**/chat/?c=*#turn-*")
    page.wait_for_load_state("load")


@pytest.mark.parametrize(("viewport", "scheme"), VIEWPORTS, ids=["phone", "desktop"])
def test_opening_a_long_conversation_lands_at_its_latest_exchange(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        conversation = _long_conversation(person)
        page.goto(_chat_url(page, conversation))
        page.wait_for_load_state("networkidle")
        latest = _latest_question(conversation)
        spot = page.evaluate(IN_VIEW_JS, f"main #turn-{latest.pk}")
        assert spot["scrollY"] > 0 and spot["inView"], spot
    finally:
        context.close()


@pytest.mark.parametrize(("viewport", "scheme"), VIEWPORTS, ids=["phone", "desktop"])
def test_sending_lands_at_the_new_question_with_its_reply_below(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        conversation = _long_conversation(person)
        page.goto(_chat_url(page, conversation))
        page.wait_for_load_state("networkidle")
        # The reader scrolls up into the history before asking.
        page.evaluate("() => window.scrollTo(0, 0)")
        _send(page, "How did dining go this month?")
        question = _latest_question(conversation)
        assert question.content == "How did dining go this month?"
        assert page.url.endswith(f"#turn-{question.pk}")
        expect(page.locator(f"main #turn-{question.pk}")).to_contain_text("How did dining go this month?")
        page.wait_for_load_state("networkidle")
        spot = page.evaluate(IN_VIEW_JS, f"main #turn-{question.pk}")
        assert spot["scrollY"] > 0 and spot["inView"], spot
        # The pending reply starts in view below the question.
        reply = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
        reply_spot = page.evaluate(IN_VIEW_JS, f"main #turn-{reply.pk}")
        assert reply_spot["inView"], reply_spot
        # Nothing took focus: the composer stays as the reader left it and the keyboard stays shut.
        assert page.evaluate("() => document.activeElement === document.body")
        # The anchor moved the sequential focus start: Tab goes on from the new turn, not the page top.
        page.keyboard.press("Tab")
        focus = page.evaluate(
            """(ids) => {
              const el = document.activeElement;
              const question = document.getElementById(ids.question);
              return {
                after: !!(question.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING),
                inReplyOrComposer: !!el.closest('#' + ids.reply + ', main #chat-send-form'),
              };
            }""",
            {"question": f"turn-{question.pk}", "reply": f"turn-{reply.pk}"},
        )
        assert focus == {"after": True, "inReplyOrComposer": True}, focus
    finally:
        context.close()


def _propose(person, conversation):
    turn = conversation.messages.get(status=AiConversationMessage.Status.PENDING)
    tool = next(spec for spec in proposal_tools(conversation, turn) if spec.name == "propose_planned_items")
    items = [{"name": "SYN gym", "kind": "expense", "amount_minor": 2680, "cadence": "monthly",
              "start_date": "2027-01-17"}]
    assert tool.handler(person, {"items": items}).ok
    turn.status = AiConversationMessage.Status.DONE
    turn.content = "Here is a planned item. Nothing changes until you apply."
    turn.save(update_fields=("status", "content"))
    return turn


@pytest.mark.parametrize(("viewport", "scheme"), VIEWPORTS, ids=["phone", "desktop"])
def test_proposal_reload_keeps_the_cards_in_view(page_session, harness, viewport, scheme):  # noqa: F811
    _state, url = harness
    context, page = page_session(viewport, scheme, "/")
    try:
        person = _member(url)
        conversation = _long_conversation(person)
        page.goto(_chat_url(page, conversation))
        page.wait_for_load_state("networkidle")
        _send(page, "Plan the gym.")
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
        turn = _propose(person, conversation)
        # The live turn reloads the page to show the server-rendered card, anchored to that turn.
        card = page.locator('main [data-chat-proposal="planned_items"]')
        expect(card).to_be_visible(timeout=15000)
        page.wait_for_url(f"**#turn-{turn.pk}")
        page.wait_for_load_state("networkidle")
        spot = page.evaluate(IN_VIEW_JS, 'main [data-chat-proposal="planned_items"]')
        assert spot["scrollY"] > 0 and spot["inView"], spot
    finally:
        context.close()


@pytest.mark.parametrize(("viewport", "scheme"), VIEWPORTS, ids=["phone", "desktop"])
def test_without_javascript_the_redirect_anchor_lands_at_the_new_question(
    browser, live_server, page_session, harness, viewport, scheme,  # noqa: F811
):
    _state, url = harness
    signed_in, _page = page_session(viewport, scheme, "/")
    cookies = signed_in.cookies()
    signed_in.close()
    width, height = viewport
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                  has_touch=True, java_script_enabled=False)
    try:
        context.add_cookies(cookies)
        page = context.new_page()
        person = _member(url)
        conversation = _long_conversation(person)
        page.goto(live_server.url + f"/chat/?c={conversation.pk}")
        _send(page, "Was spending steady?")
        question = _latest_question(conversation)
        assert page.url.endswith(f"#turn-{question.pk}")
        box = page.locator(f"main #turn-{question.pk}").bounding_box()
        composer = page.locator("main #chat-send-form").bounding_box()
        assert box is not None and composer is not None
        # The anchor alone scrolled the page: the question sits above the composer, not below the fold.
        assert 0 <= box["y"] < composer["y"], (box, composer)
    finally:
        context.close()
