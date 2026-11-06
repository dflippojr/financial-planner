"""The SimpleFIN Fix and Add pop-up states in Chromium, desktop and phone (#694).

Most tests replace window.open with a stub that records the pop-up. The
real-window tests open a real pop-up whose Bridge address is answered by a
local stub page. fetch_accounts is a synthetic feed, so nothing leaves the test.
"""

import json
from datetime import timedelta

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from finance.models import Person
from finance.simplefin_flow import BRIDGE_URL
from finance.simplefin_services import sync_connection
from tests.a11y import assert_accessible
from tests.browser_support import CSS, browser, page_session  # noqa: F401
from tests.test_simplefin import connect_owner
from tests.test_simplefin_health import Feed, feed

# No CSS-built skip: only the width check needs the stylesheet.
pytestmark = pytest.mark.django_db(transaction=True)

VIEWPORTS = {"desktop": (1280, 800), "phone": (375, 667)}

STUB_WINDOW = """
window.__popups = [];
window.__blockPopups = false;
window.open = function (url, name, features) {
  if (window.__blockPopups) return null;
  var popup = {url: url, name: name, features: features, closed: false, opener: window, location: {href: url},
               close: function () { this.closed = true; }};
  window.__popups.push(popup);
  return popup;
};
"""

CLOSED_POPUP = "() => window.__popups.length === 1 && window.__popups[0].closed"


@pytest.fixture
def flow_page(page_session, monkeypatch):  # noqa: F811 - the imported pytest fixture, injected by name
    def open_page(viewport, stub_window=True):
        person = Person.objects.get(user__username="synthetic_alex")
        now = timezone.now()
        connection = connect_owner(person, monkeypatch, feed(north_at=now - timedelta(hours=40)))
        stub = Feed(monkeypatch, feed(north_at=now - timedelta(hours=40)))
        sync_connection(person, connection.pk)
        context, page = page_session(VIEWPORTS[viewport], "light", "/settings/connections/")
        if stub_window:
            context.add_init_script(STUB_WINDOW)
        else:
            context.route(BRIDGE_URL + "**", lambda route: route.fulfill(
                status=200, content_type="text/html", body="<title>Synthetic Bridge</title><p>Synthetic Bridge</p>"))
        page.reload()
        page.wait_for_load_state("networkidle")
        return context, page, stub, now

    return open_page


def popup(page):
    return page.evaluate("() => window.__popups.map(p => ({href: p.location.href, closed: p.closed, "
                         "keepsOpener: p.opener === window, features: p.features}))")


def open_fix(page):
    page.get_by_role("link", name="Fix Synthetic North Bank").click()
    expect(page.locator("#simplefin-flow-panel")).to_be_visible()
    expect(page.locator("#simplefin-flow-text")).to_have_text("Waiting for SimpleFIN…")


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_fix_opens_a_pop_up_and_shows_the_waiting_state(flow_page, viewport):
    context, page, _stub, _now = flow_page(viewport)
    try:
        expect(page.get_by_text("Stale", exact=True)).to_be_visible()
        open_fix(page)

        [opened] = popup(page)
        assert opened["href"] == BRIDGE_URL
        assert opened["keepsOpener"] is True  # Kept so the page can close it (owner decision 4 on #694).
        assert "popup" in opened["features"]
        expect(page.get_by_role("button", name="Done")).to_be_visible()
        expect(page.get_by_role("button", name="Cancel")).to_be_visible()
        if CSS.exists():  # Width needs the stylesheet; CI always builds it before these tests.
            assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
        assert_accessible(page.content())
    finally:
        context.close()


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_cancel_closes_the_pop_up_and_syncs_once(flow_page, viewport):
    context, page, stub, _now = flow_page(viewport)
    try:
        open_fix(page)
        stub.calls.clear()
        page.get_by_role("button", name="Cancel").click()

        page.wait_for_function(CLOSED_POPUP)
        expect(page.get_by_text("Cancelled. Synced", exact=False).first).to_be_visible()
        assert len(stub.calls) == 1
    finally:
        context.close()


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_closing_the_pop_up_counts_as_cancel(flow_page, viewport):
    context, page, stub, _now = flow_page(viewport)
    try:
        open_fix(page)
        stub.calls.clear()
        # Even straight after opening: any close counts as Cancel (owner decision 5 on #694).
        page.evaluate("() => { window.__popups[0].closed = true; }")

        expect(page.get_by_text("Cancelled. Synced", exact=False).first).to_be_visible()
        assert len(stub.calls) == 1
    finally:
        context.close()


def test_cancel_before_the_start_request_finishes_waits_for_it(flow_page):
    context, page, stub, _now = flow_page("desktop")
    try:
        held = []
        page.route("**/settings/connections/flow/start/", lambda route: held.append(route))
        cancels = []
        page.on("request", lambda request: cancels.append(request) if request.url.endswith("/flow/cancel/") else None)
        with page.expect_request("**/settings/connections/flow/start/"):
            page.get_by_role("link", name="Fix Synthetic North Bank").click()
        page.get_by_role("button", name="Cancel").click()
        page.wait_for_timeout(300)
        assert cancels == []  # Cancel waits for the server to have the flow.

        stub.calls.clear()
        held[0].continue_()
        expect(page.get_by_text("Cancelled. Synced", exact=False).first).to_be_visible()
        assert len(stub.calls) == 1
    finally:
        context.close()


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_done_detects_success_closes_the_pop_up_and_reports_the_bank_up_to_date(flow_page, viewport):
    context, page, stub, now = flow_page(viewport)
    try:
        open_fix(page)
        stub.payload = feed(north_at=now - timedelta(minutes=5))
        page.get_by_role("button", name="Done").click()

        # Closed by the page itself, before the reload that follows.
        page.wait_for_function(CLOSED_POPUP)
        expect(page.get_by_text("Synthetic North Bank is up to date.").first).to_be_visible()
        # The page reloads with the fresh health: the bank no longer needs a fix.
        expect(page.get_by_role("link", name="Fix Synthetic North Bank")).to_have_count(0, timeout=15_000)
        page.wait_for_load_state("networkidle")
    finally:
        context.close()


def test_when_checks_run_out_the_pop_up_stays_watched_until_cancel_closes_it(flow_page):
    context, page, _stub, _now = flow_page("desktop")
    try:
        message = "SimpleFIN hasn't refreshed yet; we'll pick it up at the next sync."
        page.route("**/settings/connections/flow/check/", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"state": "exhausted", "message": message})))
        open_fix(page)
        page.get_by_role("button", name="Done").click()

        expect(page.locator("#simplefin-flow-text")).to_have_text(message)
        expect(page.get_by_role("button", name="Done")).to_be_hidden()
        assert popup(page)[0]["closed"] is False
        page.get_by_role("button", name="Cancel").click()
        page.wait_for_function(CLOSED_POPUP)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_a_blocked_pop_up_falls_back_to_a_normal_link(flow_page, viewport):
    context, page, _stub, _now = flow_page(viewport)
    try:
        page.evaluate("() => { window.__blockPopups = true; }")
        page.get_by_role("link", name="Fix Synthetic North Bank").click()

        blocked = page.locator("#simplefin-flow-blocked")
        expect(blocked).to_be_visible()
        expect(blocked).to_contain_text("Your browser blocked the pop-up.")
        link = blocked.get_by_role("link", name="Open SimpleFIN Bridge")
        expect(link).to_have_attribute("href", BRIDGE_URL)
        expect(link).to_have_attribute("rel", "noopener noreferrer")
        expect(page.get_by_role("button", name="Done")).to_be_visible()
        assert popup(page) == []
    finally:
        context.close()


def wait_closed(page, window):
    """The pop-up may close before a "close" listener could attach, so poll its state."""
    for _ in range(50):
        if window.is_closed():
            return
        page.wait_for_timeout(200)
    raise AssertionError("the app did not close the pop-up")


def open_real_fix(context, page):
    with context.expect_page() as opened:
        page.get_by_role("link", name="Fix Synthetic North Bank").click()
    window = opened.value
    window.wait_for_load_state()
    expect(page.locator("#simplefin-flow-text")).to_have_text("Waiting for SimpleFIN…")
    return window


def test_a_real_pop_up_opens_bridge_and_stays_watched(flow_page):
    context, page, _stub, _now = flow_page("desktop", stub_window=False)
    try:
        window = open_real_fix(context, page)

        assert window.url == BRIDGE_URL
        page.wait_for_timeout(1500)
        expect(page.locator("#simplefin-flow-panel")).to_be_visible()
        assert not window.is_closed()
    finally:
        context.close()


def test_cancel_closes_a_real_pop_up_itself(flow_page):
    context, page, stub, _now = flow_page("desktop", stub_window=False)
    try:
        window = open_real_fix(context, page)
        stub.calls.clear()
        page.get_by_role("button", name="Cancel").click()

        wait_closed(page, window)
        expect(page.get_by_text("Cancelled. Synced", exact=False).first).to_be_visible()
        assert len(stub.calls) == 1
        expect(page.get_by_text("Close the SimpleFIN window")).to_have_count(0)
    finally:
        context.close()


def test_closing_a_real_pop_up_cancels_and_syncs(flow_page):
    context, page, stub, _now = flow_page("desktop", stub_window=False)
    try:
        window = open_real_fix(context, page)
        stub.calls.clear()
        window.close()

        expect(page.get_by_text("Cancelled. Synced", exact=False).first).to_be_visible()
        assert len(stub.calls) == 1
    finally:
        context.close()


def test_success_closes_a_real_pop_up_itself_and_reloads(flow_page):
    context, page, stub, now = flow_page("desktop", stub_window=False)
    try:
        window = open_real_fix(context, page)
        stub.payload = feed(north_at=now - timedelta(minutes=5))
        page.get_by_role("button", name="Done").click()

        wait_closed(page, window)
        expect(page.get_by_text("Synthetic North Bank is up to date.", exact=False).first).to_be_visible()
        expect(page.get_by_text("Close the SimpleFIN window")).to_have_count(0)
        expect(page.get_by_role("link", name="Fix Synthetic North Bank")).to_have_count(0, timeout=15_000)
    finally:
        context.close()
