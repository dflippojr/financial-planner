"""Recurring and Transfers at desktop and phone widths in a real browser (issue #335, Desktop 9).

Needs Playwright with Chromium and a compiled static/dist/app.css; skipped otherwise.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from finance.models import Account, RecurringSeries, TransferPair
from tests.browser_support import CSS, SCREEN_DIR, browser, sync_api  # noqa: F401 - browser is a fixture
from tests.mobile_seed import seed_phone_data
from tests.test_recurring_review import add_member, make_account, make_series, make_transaction

expect = sync_api.expect

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

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


@pytest.mark.parametrize("state", ["confirmed", "suggested", "cancelled"])
@pytest.mark.parametrize("width", [390, 1024, 1280, 1440])
def test_unbroken_recurring_name_keeps_amounts_and_actions_in_view(open_page, state, width):
    page = open_page("/recurring/", width)
    series = RecurringSeries.objects.filter(display_name__startswith="Synthetic Streaming Service").get()
    name = "X" * 200
    series.display_name = name
    series.status = RecurringSeries.Status.SUGGESTED if state == "suggested" else RecurringSeries.Status.CONFIRMED
    series.cancelled_at = timezone.now() if state == "cancelled" else None
    series.save(update_fields=["display_name", "status", "cancelled_at"])
    # The transactions are unchanged, so detection preserves this valid stored name.
    page.reload()
    row = page.locator("main tbody tr").filter(has=page.get_by_text(name, exact=True)).first
    expect(row).to_be_visible()
    assert not page.evaluate(LAYOUT_JS)["scroll"]
    actions = row.locator("summary")
    expect(actions).to_be_visible()
    box = actions.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    if state == "confirmed":
        expect(row).to_contain_text("\N{MINUS SIGN}$17.99")
        actions.click()
        row.get_by_role("link", name="Show charges and reasons").click()
        expect(page.get_by_role("heading", name=f"Charges in {name}")).to_be_visible()
        assert not page.evaluate(LAYOUT_JS)["scroll"]


@pytest.mark.parametrize("width", [390, 1024, 1280, 1440])
def test_unbroken_transfer_account_names_keep_actions_in_view(open_page, width):
    Account.objects.filter(name__in=["Synthetic Checking", "Synthetic Savings"]).update(name="X" * 150)
    page = open_page("/transfers/", width)
    assert not page.evaluate(LAYOUT_JS)["scroll"]
    for name in ("Confirm", "Not a transfer"):
        button = page.get_by_role("button", name=name, exact=True)
        expect(button).to_be_visible()
        box = button.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
