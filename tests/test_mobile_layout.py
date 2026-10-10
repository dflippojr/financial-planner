"""Phone-width layout checks in a real browser (issue #221).

Needs Playwright with Chromium and a compiled static/dist/app.css
(scripts/build_css.sh); skipped otherwise. Set MOBILE_SHOTS_DIR to also save a
full-page screenshot of every page, in light and dark, at both phone sizes.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings
from django.template import engines

from tests.mobile_seed import seed_phone_data

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


HEADER_JS = """() => {
  const header = document.querySelector('main .page-header');
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const helper = header.querySelector('.page-header-helper');
  const side = document.querySelector('main .page-side');
  const columns = document.querySelector('main .page-columns');
  return {
    h1s: document.querySelectorAll('h1').length,
    header: box(header),
    helper: helper && helper.offsetParent !== null ? box(helper) : null,
    main: box(columns.firstElementChild),
    side: side.offsetParent !== null ? box(side) : null,
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
}"""


@pytest.mark.parametrize("width", [390, 1024, 1440])
def test_home_page_header_and_side_column(phone_session, width):
    """Desktop layout skeleton (Desktop 14): the shared header and side column on Home."""
    size = f"home-{width}"
    VIEWPORTS[size] = (width, 900)
    try:
        context, page = phone_session(size, "light", "/")
    finally:
        del VIEWPORTS[size]
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"home-header-{width}.png"), full_page=True)
        layout = page.evaluate(HEADER_JS)
        assert layout["h1s"] == 1
        assert not layout["scroll"]
        if width < 768:
            assert layout["helper"] is None and layout["side"] is None
            return
        assert layout["helper"] is not None
        assert "amounts in usd" in page.locator("main .page-header-helper").inner_text().lower()
        main_left, main_top, main_width, _ = layout["main"]
        side_left, side_top, side_width, _ = layout["side"]
        if width >= 1280:
            assert side_width == 340
            assert side_left >= main_left + main_width
            assert abs(side_top - main_top) <= 1
        else:
            assert side_top > main_top
            assert abs(side_left - main_left) <= 1
    finally:
        context.close()


CASH_FLOW_FOLD_JS = """() => {
  const visible = (el) => el && el.offsetParent !== null;
  const bottom = (selector, text) => {
    const el = [...document.querySelectorAll(selector)].find((node) => visible(node) && node.textContent.trim() === text);
    return el ? el.getBoundingClientRect().bottom : null;
  };
  return {
    net: bottom('main h2', 'Net cash flow'),
    income: bottom('main h2', 'Income'),
    spending: bottom('main h2', 'Spending'),
    attention: bottom('main h2', 'Needs attention'),
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
}"""


@pytest.mark.parametrize(("width", "height", "attention_on_screen"), [(1440, 900, True), (1024, 768, False)])
def test_cash_flow_key_numbers_and_attention_are_above_the_fold(phone_session, width, height, attention_on_screen):
    """Desktop 3 (#329): key numbers first; Needs attention shows from lg, in the side column at xl."""
    size = f"cash-flow-{width}"
    VIEWPORTS[size] = (width, height)
    try:
        context, page = phone_session(size, "light", "/")
    finally:
        del VIEWPORTS[size]
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"cash-flow-{width}.png"), full_page=True)
        fold = page.evaluate(CASH_FLOW_FOLD_JS)
        assert not fold["scroll"]
        for name in ("net", "income", "spending"):
            assert fold[name] is not None and fold[name] <= height, name
        assert fold["attention"] is not None
        if attention_on_screen:
            assert fold["attention"] <= height
    finally:
        context.close()


def test_cash_flow_filters_keep_every_field(phone_session):
    VIEWPORTS["cash-flow-filters"] = (1280, 800)
    try:
        context, page = phone_session("cash-flow-filters", "light", "/")
    finally:
        del VIEWPORTS["cash-flow-filters"]
    try:
        panel = page.locator("#cash-flow-filters form")
        assert not panel.is_visible()
        page.get_by_role("link", name="Custom").click()
        assert panel.is_visible()
        for name in ("date_from", "date_to", "grouping", "horizon", "account", "tag", "scope"):
            assert panel.locator(f"[name={name}]").count() == 1, name
        assert page.evaluate("document.activeElement.name") == "date_from"
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    finally:
        context.close()


ROW_MENU_ITEMS = """<li><a href="#edit">Edit</a></li>
<li><form method="post" data-confirm="Archive Dining?"><button type="submit" class="row-menu-danger">Archive</button></form></li>"""


def test_row_menu_opens_and_closes_from_the_keyboard(browser):
    django_engine = engines["django"]
    markup = django_engine.from_string(
        '{% include "finance/_row_menu.html" with label="Actions for Dining" items=items %}'
    ).render({"items": django_engine.from_string(ROW_MENU_ITEMS)})
    page = browser.new_page()
    try:
        page.set_content(f"<main><button>Before</button><table><tr><td>{markup}</td></tr></table></main>")
        page.add_style_tag(path=str(CSS))
        page.add_script_tag(path=str(Path(settings.BASE_DIR) / "static" / "js" / "row-menu.js"))
        toggle = page.get_by_label("Actions for Dining")
        menu = page.locator("details[data-row-menu]")
        edit = page.get_by_role("link", name="Edit")

        toggle.focus()
        page.keyboard.press("Enter")
        assert menu.evaluate("el => el.open") and edit.is_visible()
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.textContent.trim()") == "Edit"
        page.keyboard.press("Escape")
        assert not menu.evaluate("el => el.open")
        assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Actions for Dining"

        page.keyboard.press(" ")
        assert menu.evaluate("el => el.open")
        assert page.locator(".row-menu-danger").evaluate("el => el.closest('form').dataset.confirm") == "Archive Dining?"
        page.get_by_role("button", name="Before").click()
        assert not menu.evaluate("el => el.open")
    finally:
        page.close()


TRANSACTIONS_JS = """() => {
  const visible = (el) => el && el.offsetParent !== null;
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const rows = [...document.querySelectorAll('main tbody tr')].filter(visible);
  const side = document.querySelector('main .page-side');
  const table = document.querySelector('main section[aria-label=Transactions]');
  return {
    h1s: document.querySelectorAll('h1').length,
    firstRow: rows.length ? box(rows[0]) : null,
    headings: [...document.querySelectorAll('main thead th')].filter(visible).map((th) => th.textContent.trim()),
    boxes: [...document.querySelectorAll('.js-bulk-row')].filter(visible).length,
    bulk: visible(document.getElementById('bulk-edit-form')),
    side: visible(side) ? box(side) : null,
    table: box(table),
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
}"""


def open_transactions(browser, live_server, client, width, height, query="", javascript=True):
    person = seed_phone_data()
    client.force_login(person.user)
    context = browser.new_context(viewport={"width": width, "height": height}, java_script_enabled=javascript)
    context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value, "url": live_server.url}])
    page = context.new_page()
    page.goto(f"{live_server.url}/transactions/{query}")
    page.wait_for_load_state("networkidle")
    return context, page


@pytest.mark.parametrize(("width", "height"), [(1440, 900), (1024, 768)])
def test_transactions_desktop_layout(browser, live_server, client, width, height):
    """Desktop 4 (issue #330): rows above the fold, four columns, totals beside the list at xl."""
    context, page = open_transactions(browser, live_server, client, width, height, "?date_from=2000-01-01")
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"transactions-{width}.png"), full_page=True)
        layout = page.evaluate(TRANSACTIONS_JS)
        assert layout["h1s"] == 1
        assert not layout["scroll"]
        assert layout["headings"] == ["Date", "Description", "Category", "Amount"]
        assert layout["boxes"] == 0 and not layout["bulk"]
        assert layout["firstRow"] is not None and layout["firstRow"][1] < height
        table_left, table_top, table_width, _ = layout["table"]
        side_left, side_top, side_width, _ = layout["side"]
        if width >= 1280:
            assert side_width == 340
            assert side_left >= table_left + table_width
        else:
            assert side_top < table_top
        more = page.locator("details[data-open-below]")
        assert not more.evaluate("el => el.open")
        more.locator("summary").click()
        for label in ("Tag", "Scope", "Amount min", "Amount max", "Amount mode", "Has note", "Is split", "Set by"):
            assert page.get_by_label(label, exact=True).is_visible(), label
    finally:
        context.close()


def test_transactions_select_mode_and_escape(browser, live_server, client):
    context, page = open_transactions(browser, live_server, client, 1440, 900)
    try:
        select = page.locator("main [data-select-toggle]:visible")
        select.click()
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / "transactions-1440-select.png"))
        layout = page.evaluate(TRANSACTIONS_JS)
        assert layout["boxes"] > 0 and layout["bulk"]
        assert layout["headings"][1:] == ["Date", "Description", "Category", "Amount"]
        assert select.get_attribute("aria-pressed") == "true"
        page.locator(".js-bulk-row").first.focus()
        page.keyboard.press("Escape")
        layout = page.evaluate(TRANSACTIONS_JS)
        assert layout["boxes"] == 0 and not layout["bulk"]
        assert page.evaluate("document.activeElement.hasAttribute('data-select-toggle')")
        assert page.evaluate("document.activeElement.getAttribute('aria-pressed')") == "false"
    finally:
        context.close()


def test_transactions_without_javascript_keep_checkboxes_visible(browser, live_server, client):
    context, page = open_transactions(browser, live_server, client, 1440, 900, javascript=False)
    try:
        assert page.locator(".js-bulk-row").first.is_visible()
        assert page.locator("#bulk-edit-form").is_visible()
        assert page.locator("main [data-select-toggle]:visible").count() == 0
    finally:
        context.close()
