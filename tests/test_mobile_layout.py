"""Phone-width layout checks in a real browser (issue #221).

Needs Playwright with Chromium and a compiled static/dist/app.css
(scripts/build_css.sh); skipped otherwise. Set MOBILE_SHOTS_DIR to also save a
full-page screenshot of every page, in light and dark, at both phone sizes.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings

from finance.ai_services import connect_harness
from finance.chat_services import send_message, start_conversation
from finance.models import Person
from finance.policy_services import accept_policy, publish_policy
from tests.chat_helpers import ask
from tests.mobile_seed import seed_phone_data
from tests.test_chat import TOKEN, harness  # noqa: F401 - harness is a fixture

# Playwright's sync API keeps an event loop running in this thread, which Django's ORM guard rejects.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

VIEWPORTS = {"390x844": (390, 844), "360x740": (360, 740)}
PAGES = (
    ("home", "/"),
    ("transactions", "/transactions/"),
    ("budgets", "/planning/budgets/"),
    ("chat", "/chat/"),
    ("more", "/more/"),
    ("accounts", "/accounts/"),
    ("alerts", "/alerts/"),
    ("recurring", "/recurring/"),
    ("imports", "/imports/"),
    ("settings", "/settings/security/"),
)
SCREEN_DIR = os.environ.get("MOBILE_SHOTS_DIR")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception as error:  # noqa: BLE001 - browser binaries are optional locally
            pytest.skip(f"Chromium is not installed: {error}")
        yield instance
        instance.close()


@pytest.fixture
def phone_session(live_server, client, browser):
    person = seed_phone_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value

    def open_page(size, scheme, path):
        width, height = VIEWPORTS[size]
        context = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme=scheme,
            device_scale_factor=2 if SCREEN_DIR else 1,
            has_touch=True,
        )
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + path)
        page.wait_for_load_state("networkidle")
        return context, page

    return open_page


OVERFLOW_JS = """() => {
  const root = document.documentElement;
  const clipped = [...document.querySelectorAll('.btn, .badge, [role=button], button')]
    .filter((el) => el.offsetParent !== null && el.scrollWidth > el.clientWidth + 1)
    .map((el) => (el.textContent || el.getAttribute('aria-label') || el.className).trim().slice(0, 40));
  return { scroll: root.scrollWidth, width: root.clientWidth, clipped };
}"""

TAP_JS = """() => [...document.querySelectorAll('.dock a, .dock button, main .btn-primary')]
  .filter((el) => el.offsetParent !== null || getComputedStyle(el).position === 'fixed')
  .map((el) => { const r = el.getBoundingClientRect(); return [(el.textContent || el.getAttribute('aria-label')).trim(), r.width, r.height]; })
  .filter(([, w, h]) => w > 0 && (w < 44 || h < 44))"""


@pytest.mark.parametrize("size", VIEWPORTS)
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("name", "path"), PAGES)
def test_page_fits_phone(phone_session, size, scheme, name, path):
    context, page = phone_session(size, scheme, path)
    try:
        if SCREEN_DIR:
            Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SCREEN_DIR) / f"{name}-{size}-{scheme}.png"), full_page=True)
        fit = page.evaluate(OVERFLOW_JS)
        assert fit["scroll"] <= fit["width"], f"{name} scrolls sideways: {fit}"
        assert not fit["clipped"], f"{name} has clipped button text: {fit['clipped']}"
        assert page.evaluate(TAP_JS) == [], f"{name} has small tap targets"
    finally:
        context.close()


@pytest.mark.parametrize("size", VIEWPORTS)
def test_home_totals_are_on_the_first_screen(phone_session, size):
    context, page = phone_session(size, "light", "/")
    try:
        height = VIEWPORTS[size][1]
        for label in ("Income", "Spending", "Net cash flow"):
            box = page.get_by_text(label, exact=True).first.bounding_box()
            assert box is not None and box["y"] + box["height"] <= height, label
    finally:
        context.close()


def test_bottom_tabs_replace_the_drawer_on_phones(phone_session):
    context, page = phone_session("390x844", "light", "/transactions/")
    try:
        tabs = page.get_by_role("navigation", name="Main tabs")
        assert tabs.is_visible()
        labels = [text.strip() for text in tabs.get_by_role("link").all_inner_texts()]
        assert labels == ["Home", "Activity", "Budgets", "Chat", "More"]
        assert tabs.locator("[aria-current=page]").inner_text().strip() == "Activity"
    finally:
        context.close()


DESKTOP_VIEWPORTS = {"1440": (1440, 900), "1280": (1280, 800), "1024": (1024, 768)}

SHELL_JS = """() => {
  const side = document.querySelector('.drawer-side > div').getBoundingClientRect();
  const content = document.querySelector('#main-content > div').getBoundingClientRect();
  const groups = [...document.querySelectorAll('[data-nav-group]')].map((list) => ({
    key: list.dataset.navGroup,
    labels: [...list.querySelectorAll('a')].map((a) => a.textContent.trim()),
    icons: [...list.querySelectorAll('a')].every((a) => a.querySelector('svg path')),
  }));
  return {
    side: side.width,
    content: [content.left, content.width],
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    groups,
    dock: getComputedStyle(document.querySelector('.dock')).display,
  };
}"""


@pytest.mark.parametrize("size", DESKTOP_VIEWPORTS)
def test_desktop_sidebar_groups_and_content_width(phone_session, browser, live_server, size):
    width, height = DESKTOP_VIEWPORTS[size]
    VIEWPORTS[size] = (width, height)
    try:
        context, page = phone_session(size, "light", "/transactions/")
    finally:
        del VIEWPORTS[size]
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"desktop-{size}.png"))
        shell = page.evaluate(SHELL_JS)
        assert not shell["scroll"]
        assert shell["dock"] == "none"
        assert shell["side"] == (216 if width < 1280 else 240)
        assert [group["key"] for group in shell["groups"]] == ["main", "money", "planning"]
        assert shell["groups"][0]["labels"] == ["Home", "Activity", "Budgets", "Chat"]
        assert all(group["icons"] for group in shell["groups"])
        assert shell["content"][1] <= 1180
        if width == 1440:
            left, content_width = shell["content"]
            assert abs((left - 240) - (width - left - content_width)) <= 1
        sidebar = page.get_by_role("navigation", name="Main")
        assert sidebar.locator("[aria-current=page]").inner_text().strip() == "Activity"
        assert page.locator("label[for=finance-chat-drawer]").count() == 0
    finally:
        context.close()


def test_ask_about_this_page_opens_the_chat_drawer_from_the_keyboard(phone_session):
    VIEWPORTS["1280"] = (1280, 800)
    try:
        context, page = phone_session("1280", "light", "/planning/budgets/")
    finally:
        del VIEWPORTS["1280"]
    try:
        ask = page.get_by_role("button", name="Ask about this page")
        ask.focus()
        page.keyboard.press("Enter")
        panel = page.locator("#finance-chat-panel")
        assert panel.is_visible()
        assert ask.get_attribute("aria-expanded") == "true"
        page.keyboard.press("Escape")
        assert not panel.is_visible()
        assert page.evaluate("document.activeElement.textContent.trim()") == "Ask about this page"
        switch = page.get_by_role("button", name="Dark theme")
        before = switch.get_attribute("aria-pressed")
        switch.click()
        assert switch.get_attribute("aria-pressed") != before
    finally:
        context.close()


CHAT_JS = """() => {
  const box = (selector) => {
    const el = document.querySelector(selector);
    if (!el || el.offsetParent === null) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, width: r.width };
  };
  return {
    list: box('main nav[aria-label=Conversations]'),
    thread: box('main section'),
    messages: box('main section [aria-live]'),
    composer: box("main #chat-send-form > div"),
    summary: box('main nav[aria-label=Conversations] summary'),
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
}"""


@pytest.fixture
def chat_session(phone_session, harness):  # noqa: F811 - pytest fixture injection
    _state, url = harness

    def open_chat(size, scheme="light"):
        context, page = phone_session(size, scheme, "/")
        context.close()
        person = Person.objects.get(user__username="synthetic_alex")
        accept_policy(person, publish_policy(material=True, body="Synthetic privacy policy for layout tests"))
        connect_harness(person, base_url=url, token=TOKEN)
        ask(person, "Any new subscriptions?")
        answered = ask(person, "How much went to dining?", conversation_id=start_conversation(person).pk)
        # No chat lane runs beside the live server, so this reply stays pending.
        send_message(person, "And groceries?", conversation_id=answered.pk)
        return phone_session(size, scheme, f"/chat/?c={answered.pk}")

    return open_chat


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_chat_list_sits_beside_the_thread_at_desktop_width(chat_session, scheme):
    VIEWPORTS["1440"] = (1440, 900)
    try:
        context, page = chat_session("1440", scheme)
    finally:
        del VIEWPORTS["1440"]
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"chat-1440-{scheme}.png"))
        layout = page.evaluate(CHAT_JS)
        assert not layout["scroll"]
        assert layout["summary"] is None
        assert layout["list"]["right"] < layout["thread"]["left"]
        assert abs(layout["list"]["top"] - layout["thread"]["top"]) <= 1
        assert layout["list"]["width"] == 260
        assert layout["messages"]["width"] <= 760
        assert layout["composer"]["width"] <= 760
        conversations = page.get_by_role("navigation", name="Conversations")
        assert conversations.get_by_role("link").count() == 2
        assert conversations.locator("[aria-current=page]").inner_text().strip() == "How much went to dining?"
        assert conversations.get_by_role("button", name="New conversation").is_visible()
        thinking = page.locator("main [data-chat-turn-url]")
        thinking.locator("[data-chat-thinking]").wait_for(state="visible")
        assert thinking.get_attribute("aria-busy") == "true"
        assert "Working on it." in thinking.inner_text()
        assert page.locator("h1").count() == 1
    finally:
        context.close()


def test_chat_at_phone_width_keeps_the_picker_and_pinned_composer(chat_session):
    context, page = chat_session("390x844")
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / "chat-390-ready.png"), full_page=True)
        layout = page.evaluate(CHAT_JS)
        assert not layout["scroll"]
        assert layout["summary"] is not None
        assert page.get_by_role("link", name="How much went to dining?").is_hidden()
        assert page.locator("main #chat-send-form").evaluate("(el) => getComputedStyle(el).position") == "fixed"
        assert page.evaluate(TAP_JS) == []
        page.locator("main [data-chat-turn-url] [data-chat-thinking]").wait_for(state="visible")
    finally:
        context.close()


def test_thinking_dots_hold_still_when_motion_is_reduced(chat_session):
    context, page = chat_session("390x844")
    try:
        page.emulate_media(reduced_motion="reduce")
        dot = page.locator("main [data-chat-thinking] > span").first
        dot.wait_for(state="visible")
        assert dot.evaluate("(el) => getComputedStyle(el).animationName") == "none"
        page.emulate_media(reduced_motion="no-preference")
        assert dot.evaluate("(el) => getComputedStyle(el).animationName") != "none"
    finally:
        context.close()
