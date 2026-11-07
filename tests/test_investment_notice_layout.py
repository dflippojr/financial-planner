"""The investment-activity notice's dismiss button in a real browser (#699).

Needs Playwright with Chromium and a compiled static/dist/app.css; skipped otherwise. The
synthetic demo household has an investment account, so the notice shows on Home, Spending and
the spending-category page.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings

from finance.models import Person
from tests.mobile_seed import seed_phone_data

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

VIEWPORTS = {"phone-320": (320, 700), "phone": (390, 844), "desktop": (1440, 900)}
PAGES = ("/", "/spending/", "/spending/category/uncategorized/")
NOTICE = "[data-investment-notice]"
BUTTON = "Dismiss the investment activity notice"

LAYOUT_JS = """() => {
  const note = document.querySelector('[data-investment-notice]');
  const button = note.querySelector('button[type=submit]');
  const r = button.getBoundingClientRect();
  const n = note.getBoundingClientRect();
  return {
    button: [r.width, r.height, r.right, r.top, r.bottom],
    note: [n.right, n.top, n.bottom],
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    width: document.documentElement.clientWidth,
  };
}"""


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
def open_page(live_server, client, browser):
    person = seed_phone_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    contexts = []

    def opener(size, path, javascript=True):
        width, height = VIEWPORTS[size]
        context = browser.new_context(
            viewport={"width": width, "height": height},
            java_script_enabled=javascript,
            has_touch=size != "desktop",
        )
        contexts.append(context)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + path)
        page.wait_for_load_state("networkidle")
        return page

    opener.person = person
    yield opener
    for context in contexts:
        context.close()


@pytest.mark.parametrize("size", VIEWPORTS)
@pytest.mark.parametrize("path", PAGES)
def test_dismiss_button_fits_with_a_touch_sized_target(open_page, size, path):
    page = open_page(size, path)
    layout = page.evaluate(LAYOUT_JS)
    assert not layout["scroll"], layout
    width, height, right, top, bottom = layout["button"]
    note_right, note_top, note_bottom = layout["note"]
    if size != "desktop":
        assert width >= 44 and height >= 44, layout
    assert right <= note_right <= layout["width"], layout
    assert note_top <= top and bottom <= note_bottom, layout


def test_dismissing_with_the_keyboard_removes_it_in_place_and_it_stays_hidden(open_page):
    page = open_page("phone", "/spending/?tab=trends")
    page.evaluate("() => { window.__stillHere = true; }")
    button = page.get_by_role("button", name=BUTTON)
    button.focus()
    expect(button).to_be_focused()
    with page.expect_response(lambda response: "/notices/investment/dismiss/" in response.url) as reply:
        page.keyboard.press("Enter")
    assert reply.value.status == 200
    expect(page.locator(NOTICE)).to_have_count(0)
    # No full reload: the page's own state survives, and focus stays in the page.
    assert page.evaluate("() => window.__stillHere === true")
    assert page.evaluate("() => document.activeElement.id") == "main-content"
    assert page.url.endswith("/spending/?tab=trends")

    for path in PAGES:
        page.goto(page.url.split("/spending/")[0] + path)
        expect(page.locator(NOTICE)).to_have_count(0)
    assert Person.objects.get(pk=open_page.person.pk).investment_notice_dismissed_hash != ""


@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_dismissing_without_javascript_returns_to_the_same_page(open_page, size):
    path = "/spending/category/uncategorized/?grouping=week"
    page = open_page(size, path, javascript=False)
    with page.expect_navigation():
        page.get_by_role("button", name=BUTTON).click()
    assert page.url.endswith(path)
    expect(page.locator(NOTICE)).to_have_count(0)
    page.locator("details.page-header-more summary").first.click()
    expect(page.locator("details.page-header-more").first).to_contain_text("Investment activity is left out")
