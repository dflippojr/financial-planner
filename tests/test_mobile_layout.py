"""Phone-width layout checks in a real browser (issue #221).

Needs Playwright with Chromium and a compiled static/dist/app.css
(scripts/build_css.sh); skipped otherwise. Set MOBILE_SHOTS_DIR to also save
screenshots from these checks. Sideways overflow, clipped button text and 44px tap
targets on every primary page are owned by tests/ui/breakpoints.spec.ts.
"""

from pathlib import Path

import pytest
from django.conf import settings
from django.template import engines
from django.utils import timezone

from finance.ai_services import connect_harness
from finance.budget_services import save_budget
from finance.chat_services import send_message, start_conversation
from finance.models import Budget, Person, Household
from finance.policy_services import accept_policy, publish_policy
from tests.chat_helpers import ask
from tests.browser_support import CSS, SCREEN_DIR, browser, page_session  # noqa: F401
from tests.mobile_seed import seed_phone_data
from tests.test_chat import TOKEN, harness  # noqa: F401 - harness is a fixture

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

VIEWPORTS = {"390x844": (390, 844), "360x740": (360, 740)}


@pytest.fixture
def phone_session(page_session):
    def open_page(size, scheme, path):
        return page_session(VIEWPORTS[size], scheme, path)

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
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_enable_ai_dialog_keyboard_escape_and_close(page_session, scheme):
    from finance.models import PrivacyPolicyAcceptance
    from tests.a11y import assert_accessible
    context, page = page_session((1280, 800), scheme, "/planning/budgets/")
    try:
        trigger = page.get_by_role("link", name="Enable AI Features", exact=True)
        trigger.focus()
        page.keyboard.press("Enter")
        dialog = page.get_by_role("dialog", name="Enable AI features?")
        assert dialog.is_visible()
        assert page.evaluate("document.activeElement.id") == "ai-consent-heading"
        assert_accessible(page.content())
        from tests.test_desktop_layout import CONTRAST_JS
        contrast = CONTRAST_JS.replace(".amount-in, .amount-out", "#ai-consent h2, #ai-consent p, #ai-consent a, #ai-consent button")
        low_contrast = [row for row in page.evaluate(contrast) if row["ratio"] < 4.5]
        assert not low_contrast, low_contrast
        if SCREEN_DIR:
            Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SCREEN_DIR) / f"ai-consent-{scheme}.png"))
        assert dialog.bounding_box()["width"] == 360
        assert dialog.bounding_box()["height"] < 600
        page.keyboard.press("Escape")
        assert not dialog.is_visible()
        assert trigger.evaluate("el => el === document.activeElement")
        trigger.click()
        dialog.get_by_role("link", name="Close AI dialog").click()
        assert not dialog.is_visible()
        assert trigger.evaluate("el => el === document.activeElement")
        assert not PrivacyPolicyAcceptance.objects.exists()
        person = Person.objects.get(user__username="synthetic_alex")
        assert person.privacy_policy_declined_version_id is None
        assert person.privacy_policy_dismissed_version_id is None
        switch = page.get_by_role("button", name="Dark theme")
        before = switch.get_attribute("aria-pressed")
        switch.click()
        assert switch.get_attribute("aria-pressed") != before
    finally:
        context.close()


@pytest.mark.parametrize("reduced", [False, True])
def test_dialog_accept_shows_members_and_reopens_from_slot(page_session, reduced):
    context, page = page_session((1280, 800), "light", "/transactions/?date_from=2000-01-01")
    from tests.test_privacy_policy import make_member
    make_member("pending-synthetic", Household.objects.get())
    page.reload()
    try:
        page.emulate_media(reduced_motion="reduce" if reduced else "no-preference")
        page.get_by_role("link", name="Enable AI Features", exact=True).click()
        with page.expect_response("**/privacy-policy/respond/") as response:
            page.get_by_role("dialog").get_by_role("button", name="Accept", exact=True).click()
        assert response.value.status == 200, response.value.text()
        heading = page.get_by_role("heading", name="AI is on for you")
        heading.wait_for()
        assert heading.evaluate("el => el === document.activeElement")
        dialog = page.get_by_role("dialog")
        assert "1 of 2 members still to accept" in dialog.inner_text()
        members = dialog.get_by_role("list", name="Household policy status").get_by_role("listitem")
        assert "(you)" in members.nth(0).inner_text()
        assert "Accepted" in members.nth(0).inner_text()
        assert "Not answered yet" in members.nth(1).inner_text()
        assert page.locator("[data-ai-consent-announcement]").inner_text().strip() == "Household AI: 1 of 2 members still to accept"
        assert page.locator("[data-policy-prompt]").count() == 0
        dialog.get_by_role("link", name="Done", exact=True).click()
        setup = page.get_by_role("link", name="Finish AI setup", exact=True)
        assert setup.evaluate("el => el === document.activeElement")
        pending = page.locator("[data-ai-slot]").get_by_role("link", name="Household AI:")
        pending.click()
        assert page.get_by_role("dialog", name="AI is on for you").is_visible()
        page.keyboard.press("Escape")
        assert pending.evaluate("el => el === document.activeElement")
        assert "date_from=2000-01-01" in pending.get_attribute("href")
    finally:
        context.close()


def test_dialog_deny_removes_slot_and_focuses_settings(page_session):
    context, page = page_session((1280, 800), "light", "/")
    try:
        page.get_by_role("link", name="Enable AI Features", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Deny", exact=True).click()
        page.locator("#ai-consent").wait_for(state="detached")
        assert page.locator("[data-ai-slot]").count() == 0
        assert page.evaluate("document.activeElement.id") == "sidebar-settings"
        assert "AI is off" in page.locator("#ai-consent-announcement").inner_text()
        person = Person.objects.get(user__username="synthetic_alex")
        assert person.privacy_policy_declined_version_id is not None
    finally:
        context.close()


@pytest.mark.parametrize("action", ["Accept", "Deny"])
def test_consent_dialog_plain_forms_without_javascript(page_session, browser, action):
    context, page = page_session((1280, 800), "dark", "/")
    from tests.test_privacy_policy import make_member
    make_member("pending-synthetic", Household.objects.get())
    cookies = context.cookies()
    base = page.url
    context.close()
    context = browser.new_context(viewport={"width": 1280, "height": 800}, java_script_enabled=False)
    context.add_cookies(cookies)
    page = context.new_page()
    try:
        page.goto(base)
        page.get_by_role("link", name="Enable AI Features", exact=True).click()
        dialog = page.get_by_role("dialog", name="Enable AI features?")
        assert dialog.is_visible()
        dialog.get_by_role("button", name=action, exact=True).click()
        page.wait_for_load_state("networkidle")
        if action == "Accept":
            assert page.get_by_role("dialog", name="AI is on for you").is_visible()
            assert page.get_by_role("list", name="Household policy status").is_visible()
            page.get_by_role("dialog").get_by_role("link", name="Done", exact=True).click()
            pending = page.locator("[data-ai-slot]").get_by_role("link", name="Household AI:")
            pending.click()
            assert page.get_by_role("dialog", name="AI is on for you").is_visible()
        else:
            assert page.locator("[data-ai-slot]").count() == 0
            assert page.locator("#ai-consent").count() == 0
    finally:
        context.close()


HEADER_JS = """() => {
  const header = document.querySelector('main .page-header');
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const helper = header.querySelector('.page-header-helper');
  const side = document.querySelector('main .page-side');
  const columns = document.querySelector('main .page-columns');
  return {
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


BUDGETS_JS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const visible = (el) => el.checkVisibility();
  const table = document.querySelector('main section[aria-label="Category budgets"]');
  const side = document.querySelector('main aside.page-side');
  const attention = document.querySelector('#budgets-attention-heading');
  return {
    table: box(table),
    side: box(side),
    attention: attention !== null && visible(attention),
    rowButtons: [...table.querySelectorAll('tbody tr')].map(
      (row) => [...row.querySelectorAll('a.btn, button, summary')].filter(visible).length),
  };
}"""


@pytest.mark.parametrize("width", [1024, 1440])
def test_budgets_rows_fit_with_one_menu_and_a_summary_column(phone_session, width):
    """Desktop 5 (issue #331): no clipped row buttons at 1024; the summary sits beside the table at xl."""
    size = f"budgets-{width}"
    VIEWPORTS[size] = (width, 900)
    try:
        context, page = phone_session(size, "light", "/planning/budgets/")
        person = Person.objects.get(user__username="synthetic_alex")
        save_budget(person.user, {
            "scope": Budget.Scope.PRIVATE,
            "category": None,
            "amount_minor": 300_000,
            "effective_month": timezone.localdate().replace(day=1),
            "rollover_enabled": False,
        })
        page.reload()
    finally:
        del VIEWPORTS[size]
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"budgets-{width}.png"), full_page=True)
        fit = page.evaluate(OVERFLOW_JS)
        assert fit["scroll"] <= fit["width"], fit
        assert not fit["clipped"], fit["clipped"]
        assert page.get_by_role("link", name="Previous month", exact=True).is_visible()
        assert page.get_by_role("link", name="Next month", exact=True).is_visible()
        layout = page.evaluate(BUDGETS_JS)
        assert layout["rowButtons"] and all(count == 1 for count in layout["rowButtons"])
        table_left, table_top, table_width, _ = layout["table"]
        side_left, side_top, side_width, _ = layout["side"]
        if width >= 1280:
            assert side_width == 340 and side_left >= table_left + table_width
            assert abs(side_top - table_top) <= 1
            assert layout["attention"]
        else:
            assert side_top < table_top and abs(side_left - table_left) <= 1
            assert not layout["attention"]
        menu = page.locator("main table details[data-row-menu]").last
        menu.locator("summary").click()
        archive = menu.get_by_role("button", name="Archive")
        assert archive.is_visible()
        # Nothing clips or covers the open menu: the point under its last entry is that entry.
        assert archive.evaluate(
            "el => { const r = el.getBoundingClientRect(); "
            "return el.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); }"
        )
        right = archive.bounding_box()
        assert right["x"] >= 0 and right["x"] + right["width"] <= width
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


def test_chat_list_sits_beside_the_thread_at_desktop_width(chat_session):
    VIEWPORTS["1440"] = (1440, 900)
    try:
        context, page = chat_session("1440", "light")
    finally:
        del VIEWPORTS["1440"]
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / "chat-1440.png"))
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
        helper = page.locator("main .page-header-helper")
        assert helper.count() == 1
        assert "Opinion, not financial advice." in helper.inner_text()
        assert page.locator("main").inner_text().count("Opinion, not financial advice.") == 1
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


BILLS_JS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width }; };
  const options = [...document.querySelectorAll('main .bills-account-option')].map((label) => ({
    box: box(label.querySelector('input[type=checkbox]')),
    text: box(label.querySelector('span')),
  }));
  const columns = document.querySelector('main .page-columns');
  const side = document.querySelector('main .page-side');
  return {
    options,
    main: box(columns.firstElementChild),
    side: box(side),
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
}"""


@pytest.mark.parametrize("width", [390, 1024, 1440])
def test_bills_calendar_checkboxes_and_side_column(phone_session, width):
    """Desktop 10: account checkboxes never overlap their labels, and labels toggle them."""
    size = f"bills-{width}"
    VIEWPORTS[size] = (width, 900)
    try:
        context, page = phone_session(size, "light", "/planning/calendar/")
    finally:
        del VIEWPORTS[size]
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"bills-{width}.png"), full_page=True)
        layout = page.evaluate(BILLS_JS)
        assert not layout["scroll"]
        assert layout["options"], "no expected-balance account options"
        for option in layout["options"]:
            checkbox, text = option["box"], option["text"]
            assert checkbox["width"] > 0 and text["width"] > 0
            overlaps = not (
                checkbox["right"] <= text["left"]
                or text["right"] <= checkbox["left"]
                or checkbox["bottom"] <= text["top"]
                or text["bottom"] <= checkbox["top"]
            )
            assert not overlaps, option
        main, side = layout["main"], layout["side"]
        if width >= 1280:
            assert side["width"] == 340
            assert side["left"] >= main["right"]
        elif width >= 768:
            assert side["top"] > main["top"]
        else:
            assert side["top"] < main["top"]
        label = page.locator("main .bills-account-option").first
        checkbox = label.locator("input[type=checkbox]")
        before = checkbox.is_checked()
        label.locator("span").click()
        assert checkbox.is_checked() != before
    finally:
        context.close()
