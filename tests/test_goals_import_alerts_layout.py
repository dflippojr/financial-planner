"""Goals, Planned items, Import and Alerts at desktop widths in a real browser (issue #337, Desktop 11).

Needs Playwright with Chromium and a compiled static/dist/app.css; skipped otherwise.
"""

import os
from datetime import date
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from finance.alert_services import raise_alert
from finance.models import PlannedItem, SavingsGoal
from tests.mobile_seed import seed_phone_data

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

BOX_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [r.left, r.top, r.width, r.height];
}"""
SCROLLS_JS = "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"


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
    for index, name in enumerate(("Synthetic emergency fund", "Synthetic laptop", "Synthetic trip", "Synthetic repairs")):
        SavingsGoal.objects.create(owner=person, name=name, target_amount_minor=150_000, priority=index + 1)
    PlannedItem.objects.create(
        owner=person,
        name="Synthetic bonus",
        kind=PlannedItem.Kind.INCOME,
        amount_minor=300_000,
        start_date=date(2026, 12, 15),
        cadence=PlannedItem.Cadence.ONE_TIME,
    )
    PlannedItem.objects.create(
        owner=person,
        name="Synthetic daycare",
        kind=PlannedItem.Kind.EXPENSE,
        amount_minor=115_000,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 8, 31),
        cadence=PlannedItem.Cadence.MONTHLY,
        enabled=False,
    )
    raise_alert([person], "monthly_review", "Synthetic review is ready", "/planning/review/", "layout-seed-1")
    raise_alert([person], "backup", "Synthetic backup completed", "/settings/data/", "layout-seed-2")
    person.alerts.filter(dedupe_key="layout-seed-2").update(read_at=timezone.now())
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    contexts = []

    def opener(path, width, height=900, scheme="light"):
        context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
        contexts.append(context)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + path)
        page.wait_for_load_state("networkidle")
        return page

    yield opener
    for context in contexts:
        context.close()


@pytest.mark.parametrize("width", [1024, 1280, 1440])
@pytest.mark.parametrize(
    ("path", "listing", "form"),
    [
        ("/planning/goals/", "section[aria-labelledby=goals-list-heading]", "#add-goal"),
        ("/planning/items/", "section[aria-labelledby=planned-list-heading]", "#add-item"),
    ],
)
def test_list_first_with_the_add_form_beside_or_under_it(open_page, path, listing, form, width):
    page = open_page(path, width)

    assert not page.evaluate(SCROLLS_JS)
    list_left, list_top, list_width, list_height = page.evaluate(BOX_JS, listing)
    form_left, form_top, _, _ = page.evaluate(BOX_JS, form)
    if width >= 1280:
        assert form_left >= list_left + list_width
        assert abs(form_top - list_top) <= 1
    else:
        assert form_top >= list_top + list_height
        assert abs(form_left - list_left) <= 1


@pytest.mark.parametrize(
    ("path", "first_row"),
    [
        ("/planning/goals/", "section[aria-labelledby=goals-list-heading] li"),
        ("/planning/items/", "section[aria-labelledby=planned-list-heading] tbody tr"),
    ],
)
def test_first_goal_and_planned_item_are_above_the_fold(open_page, path, first_row):
    page = open_page(path, 1440)
    _, top, _, height = page.evaluate(BOX_JS, first_row)

    assert top + height <= 900


@pytest.mark.parametrize(
    ("path", "link"),
    [("/planning/goals/", "Add goal"), ("/planning/items/", "Add planned item")],
)
def test_header_link_moves_focus_to_the_add_form(open_page, path, link):
    page = open_page(path, 1440)
    page.get_by_role("link", name=link).focus()
    page.keyboard.press("Enter")

    assert page.evaluate("document.activeElement.id") == "id_name"


def test_planned_amounts_carry_their_sign(open_page):
    page = open_page("/planning/items/", 1440)
    table = page.locator("section[aria-labelledby=planned-list-heading] table")

    expect(table).to_contain_text("+$3,000.00")
    expect(table).to_contain_text("\N{MINUS SIGN}$1,150.00")
    expect(table).to_contain_text("Disabled")


@pytest.mark.parametrize("width", [390, 1024, 1280, 1440])
def test_import_form_and_recent_imports_side_by_side_at_xl(open_page, width):
    page = open_page("/imports/", width)

    assert not page.evaluate(SCROLLS_JS)
    form_left, form_top, form_width, form_height = page.evaluate(BOX_JS, "section[aria-labelledby=import-form-heading]")
    recent_left, recent_top, _, _ = page.evaluate(BOX_JS, "section[aria-labelledby=recent-imports-heading]")
    if width >= 1280:
        assert recent_left >= form_left + form_width
        assert abs(recent_top - form_top) <= 1
        expect(page.get_by_role("button", name="Undo this import").first).to_be_in_viewport()
    else:
        assert recent_top >= form_top + form_height


def test_undo_keeps_its_confirmation(open_page):
    page = open_page("/imports/", 1440)
    messages = []
    page.on("dialog", lambda dialog: (messages.append(dialog.message), dialog.dismiss()))
    page.get_by_role("button", name="Undo this import").first.click()

    assert messages == ["Undoing this import deletes receipts on the rows it removes."]
    expect(page.get_by_role("button", name="Undo this import").first).to_be_visible()


@pytest.mark.parametrize("width", [1024, 1440])
def test_alerts_list_is_capped_with_unread_as_a_word(open_page, width):
    page = open_page("/alerts/", width)

    assert not page.evaluate(SCROLLS_JS)
    _, _, list_width, _ = page.evaluate(BOX_JS, "main ul[aria-label=Alerts]")
    assert list_width <= 880
    unread = page.locator("main ul[aria-label=Alerts] > li", has_text="Synthetic review is ready")
    read = page.locator("main ul[aria-label=Alerts] > li", has_text="Synthetic backup completed")
    expect(unread).to_contain_text("Unread")
    expect(read).not_to_contain_text("Unread")
    expect(page.get_by_role("button", name="Mark all as read")).to_be_visible()
