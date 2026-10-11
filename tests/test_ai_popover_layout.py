"""The desktop chat window in Chromium, using only synthetic conversations."""
import pytest
from playwright.sync_api import expect

from finance.ai_services import connect_harness
from finance.models import AiConversationMessage, Person
from finance.policy_services import accept_policy, publish_policy
from tests.a11y import assert_accessible
from tests.browser_support import CSS, browser, page_session  # noqa: F401
from tests.test_chat import TOKEN, harness  # noqa: F401

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="CSS must be built"),
]

KEY = "finance.ai-popover"


@pytest.fixture
def popover_session(page_session, harness):
    _state, url = harness

    def open_page(width=1280, scheme="light"):
        context, page = page_session((width, 800), scheme, "/")
        person = Person.objects.get(user__username="synthetic_alex")
        accept_policy(person, publish_policy(material=True, body="Synthetic popover policy"))
        connect_harness(person, base_url=url, token=TOKEN)
        page.reload()
        page.wait_for_load_state("networkidle")
        return context, page, person

    return open_page


def open_popover(page):
    page.evaluate("() => window.AiPopover.open({origin: document.querySelector('main h1')})")
    expect(page.locator("#ai-popover")).to_be_visible()
    page.wait_for_timeout(260)


def stored(page):
    return page.evaluate("(key) => JSON.parse(sessionStorage.getItem(key))", KEY)


def assert_in_bounds(page):
    box = page.locator("#ai-popover").bounding_box()
    side = page.locator(".drawer-side > div").bounding_box()
    size = page.viewport_size
    assert box["x"] >= side["x"] + side["width"] + 7
    assert box["y"] >= 7
    assert box["x"] + box["width"] <= size["width"] - 7
    assert box["y"] + box["height"] <= size["height"] - 7


@pytest.mark.parametrize("width", [1024, 1280, 1440])
def test_drag_keyboard_move_pill_and_resize_stay_outside_sidebar(popover_session, width):
    context, page, _person = popover_session(width)
    try:
        open_popover(page)
        assert page.locator("#ai-popover").bounding_box()["width"] == 380
        move = page.get_by_role("button", name="Move AI chat")
        move.click()
        move.press("Shift+ArrowLeft")
        move.press("Shift+ArrowUp")
        move.press("Enter")
        assert_in_bounds(page)
        start = move.bounding_box()
        page.mouse.move(start["x"] + 10, start["y"] + 10)
        page.mouse.down()
        page.mouse.move(0, 0, steps=5)
        page.mouse.up()
        page.wait_for_timeout(250)
        assert_in_bounds(page)
        before = page.locator("#ai-popover").bounding_box()
        page.get_by_role("button", name="Collapse AI chat").click()
        page.wait_for_timeout(250)
        pill = page.locator("#ai-popover").bounding_box()
        assert (pill["width"], pill["height"]) == (320, 52)
        assert (pill["x"], pill["y"]) == (before["x"], before["y"])
        page.get_by_role("button", name="Expand AI chat").click()
        page.wait_for_timeout(250)
        page.set_viewport_size({"width": 1024, "height": 600})
        assert_in_bounds(page)
    finally:
        context.close()


def test_pin_idle_draft_activity_and_collapse_focus(popover_session):
    context, page, _person = popover_session()
    try:
        page.clock.install()
        open_popover(page)
        page.get_by_role("button", name="Pin AI chat").click()
        page.clock.fast_forward(6 * 60 * 1000)
        expect(page.get_by_role("button", name="Pin AI chat")).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#ai-popover-body")).to_be_visible()
        page.get_by_role("button", name="Pin AI chat").click()
        composer = page.locator("#ai-popover textarea")
        composer.fill("Unsent synthetic draft")
        page.clock.fast_forward(6 * 60 * 1000)
        expect(page.locator("#ai-popover-body")).to_be_visible()
        composer.fill("")
        page.clock.fast_forward(4 * 60 * 1000)
        composer.press("ArrowLeft")
        page.clock.fast_forward(4 * 60 * 1000)
        expect(page.locator("#ai-popover-body")).to_be_visible()
        page.clock.fast_forward(60 * 1000 + 1)
        expect(page.get_by_role("button", name="Expand AI chat")).to_be_focused()
        expect(page.locator("[data-pop-status]")).to_have_text("Chat collapsed after 5 minutes")
    finally:
        context.close()


def test_fetch_send_pending_navigation_answer_pill_badge_and_minimal_storage(popover_session):
    context, page, person = popover_session()
    try:
        page.clock.install()
        open_popover(page)
        page.locator("#ai-popover textarea").fill("Synthetic income question")
        page.locator("#ai-popover-form").get_by_role("button", name="Send").click()
        expect(page.locator("#ai-popover [data-chat-turn-url]")).to_have_count(1)
        saved = stored(page)
        assert set(saved) == {"open", "collapsed", "pinned", "conversationId", "x", "y", "dragged", "lastActivityAt"}
        assert "Synthetic" not in str(saved)
        page.clock.fast_forward(6 * 60 * 1000)
        expect(page.locator("#ai-popover-body")).to_be_visible()
        page.goto(page.url.split("?")[0] + "transactions/")
        expect(page.locator("#ai-popover")).to_be_visible()
        expect(page.locator("#ai-popover [data-chat-turn-url]")).to_have_count(1)
        expect(page.locator("[data-pop-subject]")).to_contain_text("Transactions")
        page.get_by_role("button", name="Collapse AI chat").click()
        turn = AiConversationMessage.objects.get(conversation_id=saved["conversationId"], status="pending")
        turn.status = AiConversationMessage.Status.DONE
        turn.content = "Synthetic answer ready"
        turn.save(update_fields=("status", "content"))
        page.clock.fast_forward(2000)
        expect(page.locator("[data-pill-status]")).to_contain_text("Answer ready")
        expect(page.locator("[data-pill-status] .badge")).to_have_text("New")
        page.get_by_role("button", name="Expand AI chat").click()
        expect(page.locator("[data-pop-thread]")).to_contain_text("Synthetic answer ready")
        assert stored(page)["conversationId"] == saved["conversationId"]
        other = context.new_page()
        other.goto(page.url)
        expect(other.locator("#ai-popover")).to_be_hidden()
        other.close()
    finally:
        context.close()


@pytest.mark.parametrize("collapsed,pinned", [(False, True), (True, False), (False, False)])
def test_navigation_restores_form_position_and_idle_state(popover_session, collapsed, pinned):
    context, page, _person = popover_session()
    try:
        open_popover(page)
        move = page.get_by_role("button", name="Move AI chat")
        move.click()
        move.press("Shift+ArrowRight")
        move.press("Enter")
        if collapsed:
            page.get_by_role("button", name="Collapse AI chat").click()
        if pinned:
            page.get_by_role("button", name="Pin AI chat").click()
        saved = stored(page)
        if not collapsed and not pinned:
            page.evaluate("(key) => { const s = JSON.parse(sessionStorage.getItem(key)); s.lastActivityAt -= 300001; sessionStorage.setItem(key, JSON.stringify(s)); }", KEY)
        page.goto(page.url + "transactions/")
        expect(page.locator("#ai-popover")).to_be_visible()
        expect(page.locator("#ai-popover-body")).to_be_visible() if pinned else expect(page.locator("#ai-popover-body")).to_be_hidden()
        assert stored(page)["x"] == saved["x"]
        assert stored(page)["y"] == saved["y"]
        assert stored(page)["pinned"] == pinned
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_keyboard_skip_escape_context_removal_a11y_and_reduced_motion(popover_session, scheme):
    context, page, _person = popover_session(scheme=scheme)
    try:
        page.emulate_media(reduced_motion="reduce")
        open_popover(page)
        assert page.locator("#ai-popover textarea").evaluate("(el) => el === document.activeElement")
        assert page.evaluate("() => document.getAnimations().every(a => a.effect.getKeyframes().every(f => !f.transform))")
        assert_accessible(page.content())
        page.get_by_role("button", name="Remove page context").click()
        assert page.locator('#ai-popover [name="page_route"]').input_value() == ""
        assert page.locator('#ai-popover [name="page_query"]').input_value() == ""
        page.get_by_role("button", name="Collapse AI chat").click()
        expect(page.get_by_role("region", name="Ask AI, collapsed")).to_be_visible()
        assert_accessible(page.content())
        page.locator("#skip-ai-chat").focus()
        page.locator("#skip-ai-chat").press("Enter")
        expect(page.get_by_role("button", name="Expand AI chat")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#ai-popover")).to_be_hidden()
        expect(page.locator("main h1")).to_be_focused()
        open_popover(page)
        page.get_by_role("button", name="Sign out", exact=True).click()
        assert page.evaluate("(key) => sessionStorage.getItem(key)", KEY) is None
    finally:
        context.close()


def test_popover_does_not_render_below_lg(popover_session):
    context, page, _person = popover_session(width=390)
    try:
        assert page.locator("#ai-popover").count() == 0
        expect(page.get_by_role("navigation", name="Main tabs")).to_be_visible()
    finally:
        context.close()
