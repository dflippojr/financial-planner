"""Accounts page at desktop widths in a real browser (issue #332, Desktop 6).

Needs Playwright with Chromium and a compiled static/dist/app.css; skipped otherwise. The list, add
form and header link are checked with the Goals and Planned items pages in
test_goals_import_alerts_layout.py.
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


def test_row_menu_reaches_rename_and_archive(accounts_page):
    page = accounts_page(1440)
    page.get_by_label("More actions for Synthetic Checking").click()
    menu = page.locator("details[data-row-menu][open]")
    menu.locator("summary", has_text="Rename").click()
    expect(page.get_by_label("New name for Synthetic Checking")).to_be_visible()

    menu.get_by_role("button", name="Archive").click()
    expect(page.get_by_role("dialog")).to_contain_text("Archive Synthetic Checking?")
