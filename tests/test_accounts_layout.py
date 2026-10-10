"""Accounts page at desktop widths in a real browser (issue #332, Desktop 6).

Needs Playwright with Chromium and a compiled static/dist/app.css; skipped otherwise.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings

from tests.mobile_seed import seed_phone_data

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

LAYOUT_JS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const rows = [...document.querySelectorAll('main section[aria-labelledby=accounts-list-heading] tbody tr')]
    .filter((row) => row.querySelector('details[data-row-menu]'));
  return {
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    list: box(document.querySelector('main section[aria-labelledby=accounts-list-heading]')),
    form: box(document.getElementById('add-account')),
    rowHeights: rows.map((row) => row.getBoundingClientRect().height),
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
def accounts_page(live_server, client, browser):
    person = seed_phone_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    contexts = []

    def open_page(width, scheme="light"):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
        contexts.append(context)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + "/accounts/")
        page.wait_for_load_state("networkidle")
        return page

    yield open_page
    for context in contexts:
        context.close()


@pytest.mark.parametrize("width", [1024, 1280, 1440])
def test_accounts_list_first_with_the_add_form_beside_or_under_it(accounts_page, width):
    page = accounts_page(width)
    layout = page.evaluate(LAYOUT_JS)

    assert not layout["scroll"]
    list_left, list_top, list_width, list_height = layout["list"]
    form_left, form_top, _, _ = layout["form"]
    if width >= 1280:
        assert form_left >= list_left + list_width
        assert abs(form_top - list_top) <= 1
    else:
        assert form_top >= list_top + list_height
        assert abs(form_left - list_left) <= 1
    if width == 1440:
        # One line for the name plus a second line for scope, not a stack of controls.
        assert layout["rowHeights"] and max(layout["rowHeights"]) < 90


def test_header_link_moves_focus_to_the_add_form(accounts_page):
    page = accounts_page(1440)
    page.get_by_role("link", name="Add account").focus()
    page.keyboard.press("Enter")

    assert page.evaluate("document.activeElement.id") == "id_name"


def test_row_menu_reaches_rename_and_archive(accounts_page):
    page = accounts_page(1440)
    page.get_by_label("More actions for Synthetic Checking").click()
    menu = page.locator("details[data-row-menu][open]")
    menu.locator("summary", has_text="Rename").click()
    expect(page.get_by_label("New name for Synthetic Checking")).to_be_visible()

    menu.get_by_role("button", name="Archive").click()
    expect(page.get_by_role("dialog")).to_contain_text("Archive Synthetic Checking?")
