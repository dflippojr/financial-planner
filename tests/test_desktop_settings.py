"""Settings at desktop widths in a real browser (issue #339).

Needs Playwright with Chromium and a compiled static/dist/app.css
(scripts/build_css.sh); skipped otherwise. The settings pages' fit at every desktop width is
checked with the other pages in test_desktop_layout.py::test_page_fits_desktop.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings

from finance.models import Category
from tests.mobile_seed import seed_phone_data

# Playwright's sync API keeps an event loop running in this thread, which Django's ORM guard rejects.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")

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
def desktop(live_server, client, browser):
    person = seed_phone_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    context = browser.new_context(viewport={"width": 1024, "height": 768})
    context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
    page = context.new_page()

    def open_page(path):
        page.goto(live_server.url + path)
        page.wait_for_load_state("networkidle")
        return page

    yield open_page
    context.close()


def test_category_rename_works_by_keyboard(desktop):
    page = desktop("/settings/categories/")
    category = Category.objects.exclude(code="transfer").order_by("name").first()
    page.get_by_text(f"Rename {category.name}", exact=True).focus()
    page.keyboard.press("Enter")
    field = page.get_by_label(f"New name for {category.name}")
    assert field.is_visible()
    field.focus()
    field.fill("Synthetic renamed")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    category.refresh_from_db()
    assert category.name == "Synthetic renamed"
