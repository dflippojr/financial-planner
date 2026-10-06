"""Phone-width layout checks in a real browser (issue #221).

Needs Playwright with Chromium and a compiled static/dist/app.css
(scripts/build_css.sh); skipped otherwise. Set MOBILE_SHOTS_DIR to also save a
full-page screenshot of every page, in light and dark, at both phone sizes.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings

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
