"""Recurring and Transfers at desktop and phone widths in a real browser (issue #335, Desktop 9).

Needs Playwright with Chromium and a compiled static/dist/app.css; skipped otherwise.
"""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from finance.models import Account, RecurringSeries, TransferPair
from tests.mobile_seed import seed_phone_data
from tests.test_recurring_review import add_member, make_account, make_series, make_transaction

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]
SCREEN_DIR = os.environ.get("MOBILE_SHOTS_DIR")

LAYOUT_JS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const side = document.querySelector('main .page-side');
  const columns = document.querySelector('main .page-columns');
  return {
    h1s: document.querySelectorAll('h1').length,
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    main: box(columns.querySelector(':scope > :not(.page-side)')),
    side: box(side),
    emptyTables: [...document.querySelectorAll('main table')].filter((table) => !table.querySelector('tbody tr')).length,
  };
}"""


def _seed_review_data():
    person = seed_phone_data()
    account = Account.objects.get(name="Synthetic Checking")
    today = timezone.localdate()
    for name, cadence, minor in (
        ("Synthetic Streaming Service", RecurringSeries.Cadence.MONTHLY, -1799),
        ("Synthetic Car Insurance With A Long Name", RecurringSeries.Cadence.QUARTERLY, -64000),
    ):
        series = make_series(person, name=name, cadence=cadence, typical_minor=minor)
        for months_ago in (1, 2):
            add_member(
                series,
                make_transaction(
                    person,
                    account,
                    transaction_date=today - timedelta(days=30 * months_ago),
                    amount_minor=minor,
                    description=f"{name} {months_ago}",
                ),
            )
    savings = make_account(person, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    out_leg = make_transaction(person, account, transaction_date=today, amount_minor=-25000, description="Synthetic out")
    in_leg = make_transaction(person, savings, transaction_date=today, amount_minor=25000, description="Synthetic in")
    TransferPair.objects.create(
        leg_a=out_leg,
        leg_b=in_leg,
        status=TransferPair.Status.SUGGESTED,
        kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.LOW,
        reasons=["same amount", "same day"],
    )
    return person


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
    person = _seed_review_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    contexts = []

    def go(path, width, scheme="light"):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
        contexts.append(context)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        page.goto(live_server.url + path)
        page.wait_for_load_state("networkidle")
        return page

    yield go
    for context in contexts:
        context.close()


@pytest.mark.parametrize("path", ["/recurring/", "/transfers/"])
@pytest.mark.parametrize("width", [390, 1024, 1280, 1440])
def test_side_column_beside_lists_from_xl_and_no_sideways_scroll(open_page, path, width):
    page = open_page(path, width)
    if SCREEN_DIR:
        Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SCREEN_DIR) / f"{path.strip('/')}-{width}.png"), full_page=True)
    layout = page.evaluate(LAYOUT_JS)
    assert layout["h1s"] == 1
    assert not layout["scroll"], layout
    assert layout["emptyTables"] == 0
    main_left, main_top, main_width, _ = layout["main"]
    side_left, side_top, side_width, _ = layout["side"]
    if width >= 1280:
        assert side_width == 340
        assert side_left >= main_left + main_width
        assert abs(side_top - main_top) <= 1
    else:
        assert abs(side_left - main_left) <= 1


def test_recurring_row_menu_reaches_charges_and_grouping(open_page):
    page = open_page("/recurring/", 1440)
    # Detection may rename a seeded series ("... 2"), so match on the prefix.
    name = "Synthetic Streaming Service"
    charges = page.locator("tr[id$='-charges']").filter(has_text=f"Charges in {name}")
    expect(charges).to_be_hidden()
    toggle = page.locator(f"summary[aria-label^='Actions for {name}']")
    toggle.click()
    menu = toggle.locator("xpath=..")
    expect(menu.get_by_role("button", name="Mark cancelled")).to_be_visible()
    menu.get_by_role("link", name="Show charges and reasons").click()
    expect(charges).to_be_visible()
    expect(charges.get_by_role("button", name="Remove from series").first).to_be_visible()

    if not menu.evaluate("el => el.open"):
        toggle.click()
    menu.get_by_role("link", name="Merge into another series…").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("select[name=target_id]")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth") is False


def test_transfer_suggestion_shows_both_legs_and_its_actions(open_page):
    page = open_page("/transfers/", 1440)
    row = page.locator("main tbody tr").filter(has_text="Synthetic Checking")
    expect(row).to_contain_text("Synthetic Savings")
    expect(row).to_contain_text("same amount; same day")
    expect(row.get_by_role("button", name="Confirm")).to_be_visible()
    expect(row.get_by_role("button", name="Not a transfer")).to_be_visible()
    expect(page.get_by_text("No automatic or confirmed exclusions.")).to_be_visible()
