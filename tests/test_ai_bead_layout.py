"""Real border tracking, sticky reach, keyboard, touch and reduced-motion checks."""
import pytest
from playwright.sync_api import expect

from finance.ai_services import connect_harness
from finance.models import Person
from finance.policy_services import accept_policy, publish_policy
from tests.browser_support import CSS, browser, page_session  # noqa: F401
from tests.test_chat import TOKEN, harness  # noqa: F401

pytestmark = [pytest.mark.django_db(transaction=True),
              pytest.mark.skipif(not CSS.exists(), reason="CSS must be built")]


@pytest.fixture
def bead_session(page_session, harness, browser):
    def open_page(path="/", *, touch=False, reduced=False, width=1280, clock=False):
        initial, initial_page = page_session((width, 800), "light", "/")
        _state, url = harness
        person = Person.objects.get(user__username="synthetic_alex")
        policy = publish_policy(material=True, body="Synthetic bead policy")
        for member in Person.objects.all():
            accept_policy(member, policy)
        connect_harness(person, base_url=url, token=TOKEN)
        base_url = initial_page.url.rsplit("/", 1)[0]
        storage = initial.storage_state()
        initial.close()
        context = browser.new_context(viewport={"width": width, "height": 800}, has_touch=touch,
                                      reduced_motion="reduce" if reduced else "no-preference",
                                      storage_state=storage)
        page = context.new_page()
        if clock:
            page.clock.install(time="2026-01-01T00:00:00Z")
        page.goto(base_url + path)
        page.wait_for_load_state("networkidle")
        if clock:
            page.clock.pause_at("2026-01-01T00:01:00Z")
        return context, page
    return open_page


def center(locator):
    box = locator.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def wait_for_center_x(page, bead, expected):
    """Wait for the bead's 80 ms transform transition to land within 2 px of expected."""
    page.wait_for_function(
        "([el, x]) => { const r = el.getBoundingClientRect(); return Math.abs(r.left + r.width / 2 - x) <= 2; }",
        arg=[bead.element_handle(), expected],
        timeout=5000,
    )


def bead_x(tile):
    return tile.evaluate("el => el.style.getPropertyValue('--ai-bead-x')")


def settle_frames(page):
    """Let two real frames run so style and the bead's script have had their turn (no fake clock)."""
    page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def test_pointer_tracks_border_clamps_and_freezes_on_bead(bead_session):
    # The clamp is relative to the tile, so one desktop width is enough.
    context, page = bead_session(clock=True)
    try:
        bead = page.get_by_role("button", name="Ask AI about Key numbers")
        tile = bead.locator("xpath=ancestor::section[1]")
        box = tile.bounding_box()
        assert bead.evaluate("el => getComputedStyle(el).opacity") == "0"
        for offset in (4, 100, box["width"] / 2, box["width"] - 4):
            x = box["x"] + offset
            page.mouse.move(x, box["y"] + 65)
            page.clock.run_for(16)
            expected = max(box["x"] + 36, min(x, box["x"] + box["width"] - 36))
            wait_for_center_x(page, bead, expected)
            assert abs(center(bead)[1] - box["y"]) <= 2
        x, y = center(bead)
        page.mouse.move(x, y)
        expect(bead).to_have_css("width", "40px")
        frozen = bead_x(tile)
        # The bead sits at the right clamp, so only a move left would shift it if it tracked.
        page.mouse.move(x - 6, y + 3)
        # The frame the move scheduled runs here, and must leave the bead where it was.
        page.clock.run_for(16)
        assert bead_x(tile) == frozen
        wait_for_center_x(page, bead, x)
        # Assert the delay at its boundary without host scheduling or IPC jitter.
        page.mouse.move(1, 1)
        page.clock.run_for(299)
        assert bead.evaluate("el => getComputedStyle(el).opacity") == "1"
        assert tile.evaluate("el => el.classList.contains('ai-bead-visible')")
        page.clock.run_for(1)
        assert not tile.evaluate("el => el.classList.contains('ai-bead-visible')")
        expect(bead).to_have_css("opacity", "0")
    finally:
        context.close()


def test_keyboard_home_enter_escape_and_pin_context_switch(bead_session):
    context, page = bead_session()
    try:
        bead = page.get_by_role("button", name="Ask AI about Key numbers")
        tile = bead.locator("xpath=ancestor::section[1]").bounding_box()
        # Tab through the real document so the opener's tab-order contract is exercised.
        for _ in range(60):
            page.keyboard.press("Tab")
            if bead.evaluate("el => el === document.activeElement"):
                break
        expect(bead).to_be_focused()
        assert abs(center(bead)[0] - (tile["x"] + tile["width"] - 24)) <= 2
        bead.press("Enter")
        expect(page.locator("#ai-popover")).to_be_visible()
        expect(bead).to_have_attribute("aria-expanded", "true")
        expect(page.locator("[data-pop-subject]")).to_have_text("Key numbers")
        assert '"kind":"cash-flow-totals"' in page.locator('[name="tile_context"]').input_value()
        page.get_by_role("button", name="Pin AI chat").click()
        before = page.locator("#ai-popover").bounding_box()
        second = page.get_by_role("button", name="Ask AI about Budgets")
        second.focus()
        second.press("Enter")
        after = page.locator("#ai-popover").bounding_box()
        assert (after["x"], after["y"]) == (before["x"], before["y"])
        expect(page.locator("[data-pop-subject]")).to_have_text("Budgets")
        page.locator("#ai-popover textarea").press("Escape")
        expect(page.locator("#ai-popover")).to_be_hidden()
        expect(second).to_be_focused()
        expect(second).to_have_attribute("aria-expanded", "false")
    finally:
        context.close()


def test_touch_on_hover_capable_device_stays_reachable_then_resumes_mouse(bead_session):
    context, page = bead_session(clock=True)
    try:
        assert page.evaluate("matchMedia('(hover: hover)').matches")
        bead = page.get_by_role("button", name="Ask AI about Key numbers")
        tile = bead.locator("xpath=ancestor::section[1]")
        box = tile.bounding_box()
        # Native touch events without replacing the device's hover-capable pointer.
        session = context.new_cdp_session(page)

        def tap(x, y):
            session.send("Input.dispatchTouchEvent", {
                "type": "touchStart", "touchPoints": [{"x": x, "y": y}],
            })
            session.send("Input.dispatchTouchEvent", {
                "type": "touchEnd", "touchPoints": [],
            })

        tap(box["x"] + 80, box["y"] + 70)
        page.clock.run_for(400)
        expect(bead).to_have_css("opacity", "1")
        assert abs(center(bead)[0] - (box["x"] + box["width"] - 24)) <= 2
        assert bead.bounding_box()["width"] == 30
        page.mouse.move(box["x"] + 100, box["y"] + 70)
        page.clock.run_for(16)
        wait_for_center_x(page, bead, box["x"] + 100)
        page.mouse.move(1, 1)
        page.clock.run_for(300)
        expect(bead).to_have_css("opacity", "0")
        tap(box["x"] + 80, box["y"] + 70)
        page.clock.run_for(400)
        expect(bead).to_have_css("opacity", "1")
        assert abs(center(bead)[0] - (box["x"] + box["width"] - 24)) <= 2
        tap(*center(bead))
        expect(page.locator("#ai-popover")).to_be_visible()
        expect(bead).to_have_css("width", "30px")
    finally:
        context.close()


@pytest.mark.parametrize("touch,reduced", [(True, False), (False, True)])
def test_fixed_home_for_touch_and_reduced_motion(bead_session, touch, reduced):
    context, page = bead_session(touch=touch, reduced=reduced)
    try:
        bead = page.get_by_role("button", name="Ask AI about Key numbers")
        tile = bead.locator("xpath=ancestor::section[1]").bounding_box()
        page.mouse.move(tile["x"] + 80, tile["y"] + 70)
        # Opacity fades in over 120 ms, longer than the 80 ms transform a tracking bead would run.
        expect(bead).to_have_css("opacity", "1")
        assert abs(center(bead)[0] - (tile["x"] + tile["width"] - 24)) <= 2
        if reduced:
            page.mouse.move(*center(bead))
            settle_frames(page)
            expect(bead).to_have_css("width", "30px")
        else:
            bead.tap()
            expect(page.locator("#ai-popover")).to_be_visible()
    finally:
        context.close()


def test_narrow_load_restores_openers_when_resized_to_desktop(bead_session):
    # 1023 is the widest width without openers; the 390 reload is covered by the sticky-track test.
    context, page = bead_session(width=1023)
    try:
        bead = page.get_by_role("button", name="Ask AI about Key numbers")
        assert bead.count() == 0
        page.set_viewport_size({"width": 1024, "height": 800})
        expect(bead).to_be_visible()
        bead.focus()
        expect(bead).to_be_focused()
        bead.press("Enter")
        expect(page.locator("#ai-popover")).to_be_visible()
        page.locator("#ai-popover textarea").press("Escape")
        page.set_viewport_size({"width": 390, "height": 844})
        assert bead.count() == 0
        page.keyboard.press("Tab")
        assert not page.evaluate("document.activeElement.matches('[data-ai-bead]')")
        page.set_viewport_size({"width": 1280, "height": 800})
        bead.click()
        expect(page.locator("#ai-popover")).to_be_visible()
    finally:
        context.close()


def test_tall_transactions_sticky_track_and_mobile_absence(bead_session):
    context, page = bead_session("/transactions/")
    try:
        bead = page.get_by_role("button", name="Ask AI about Transactions", exact=True)
        tile = bead.locator("xpath=ancestor::section[1]")
        box = tile.bounding_box()
        page.evaluate("y => window.scrollTo(0, y)", box["y"] + 150)
        page.mouse.move(box["x"] + 100, 100)
        wait_for_center_x(page, bead, box["x"] + 100)
        assert 7 <= bead.bounding_box()["y"] <= 10
        page.set_viewport_size({"width": 390, "height": 844})
        expect(bead).to_be_hidden()
        page.reload()
        assert page.get_by_role("button", name="Ask AI about Transactions", exact=True).count() == 0
    finally:
        context.close()
