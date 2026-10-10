"""Shared Chromium and authenticated page fixtures for responsive layout tests."""

import os
from pathlib import Path

import pytest
from django.conf import settings

from tests.mobile_seed import seed_phone_data

# Playwright runs an event loop in the ORM thread during these synchronous tests.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
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
def page_session(live_server, client, browser):
    person = seed_phone_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value

    def open_page(viewport, scheme, path):
        width, height = viewport
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
