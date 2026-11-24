"""A double click on the Add transaction form saves one row, in Chromium (#800).

The form opts in to the shared guard in static/js/page-actions.js through data-pending-label. The
guard only refuses a second send while the first is still waiting, so a deliberate second entry made
after the first save completed still saves, and a page restored from history is usable again.
"""

import threading

import pytest
from playwright.sync_api import expect

from finance import manual_entry_views
from finance.models import Account, Transaction
from tests.browser_support import browser, page_session  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

DESKTOP = (1280, 800)
DESCRIPTION = "SYNTHETIC DOUBLE CLICK CAFE"


@pytest.fixture
def add_page(page_session, live_requests_paused):  # noqa: F811 - the imported pytest fixture, injected by name
    def rows():
        with live_requests_paused():
            return Transaction.objects.filter(description=DESCRIPTION).count()

    def open_page():
        with live_requests_paused():
            account = Account.objects.get(name="Synthetic Checking")
        context, page = page_session(DESKTOP, "light", "/transactions/add/")
        fill(page, account)
        return context, page, account

    return open_page, rows


def fill(page, account):
    form = page.locator("main form[method=post]").filter(has=page.locator("[name=description]"))
    form.locator("[name=account]").select_option(str(account.pk))
    form.locator("[name=amount]").fill("4.25")
    form.locator("[name=description]").fill(DESCRIPTION)


@pytest.fixture
def held_save(monkeypatch):
    """Hold the first save on the server until the test releases it, so the second click lands while
    the first send is still waiting, as it does on a slow server."""
    release = threading.Event()
    real = manual_entry_views.add_manual_transaction
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            release.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(manual_entry_views, "add_manual_transaction", slow)
    return release


@pytest.mark.parametrize(
    ("button", "landing"),
    [("Add transaction", "/transactions/"), ("Add and enter another", "/transactions/add/")],
)
def test_double_click_saves_one_row(add_page, held_save, button, landing):
    open_page, rows = add_page
    context, page, _account = open_page()
    try:
        target = page.get_by_role("button", name=button)
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        assert box is not None
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        posts = []
        page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
        page.evaluate("() => { window.__beforeSave = true; }")
        # Raw mouse clicks: Playwright's click() would wait for the busy button to become enabled.
        with page.expect_request(lambda request: request.method == "POST"):
            page.mouse.click(x, y)
        page.mouse.click(x, y)
        page.wait_for_timeout(300)  # time for a second POST to leave, if the page sends one
        held_save.set()
        # A marker on the old document: it is gone once the browser shows the page after the save.
        page.wait_for_function("() => !window.__beforeSave", timeout=15000)
        page.wait_for_load_state("networkidle")
        assert page.url.endswith(landing)
        assert rows() == 1
        assert len(posts) == 1
    finally:
        held_save.set()
        context.close()


def test_same_entry_after_the_first_save_still_saves(add_page):
    open_page, rows = add_page
    context, page, account = open_page()
    try:
        with page.expect_navigation(url=lambda url: url.endswith("/transactions/add/")):
            page.get_by_role("button", name="Add and enter another").click()
        page.wait_for_load_state("networkidle")
        assert rows() == 1
        fill(page, account)
        with page.expect_navigation(url=lambda url: url.endswith("/transactions/")):
            page.get_by_role("button", name="Add transaction").click()
        page.wait_for_load_state("networkidle")
        assert rows() == 2
    finally:
        context.close()


def test_form_coming_back_from_history_is_usable(add_page):
    open_page, rows = add_page
    context, page, account = open_page()
    try:
        with page.expect_navigation(url=lambda url: url.endswith("/transactions/")):
            page.get_by_role("button", name="Add transaction").click()
        page.wait_for_load_state("networkidle")
        page.go_back(wait_until="networkidle")
        form = page.locator("form[data-pending-label]")
        expect(form).not_to_have_attribute("aria-busy", "true")
        add = page.get_by_role("button", name="Add transaction")
        expect(add).to_have_text("Add transaction")
        expect(add).not_to_have_attribute("aria-disabled", "true")
        fill(page, account)
        with page.expect_navigation(url=lambda url: url.endswith("/transactions/")):
            add.click()
        page.wait_for_load_state("networkidle")
        assert rows() == 2
    finally:
        context.close()
