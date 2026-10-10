"""Net worth and Spending at phone and desktop widths (issue #334), in a real browser.

Uses the shared browser fixtures in browser_support.py, so it skips the same way when Playwright,
Chromium or static/dist/app.css is missing. Set MOBILE_SHOTS_DIR to save screenshots.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from finance.models import Account, BalanceSnapshot
from tests.browser_support import CSS, SCREEN_DIR, browser, page_session  # noqa: F401

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

LAYOUT_JS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width]; };
  const columns = document.querySelector('main .page-columns');
  const side = document.querySelector('main .page-side');
  return {
    h1s: document.querySelectorAll('h1').length,
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    main: box(columns.firstElementChild),
    side: box(side),
  };
}"""


@pytest.fixture
def open_report(page_session):
    def open_page(path, width, scheme="light"):
        return page_session((width, 900), scheme, path)

    return open_page


def _add_balances():
    today = timezone.localdate()
    for index, account in enumerate(Account.objects.order_by("pk")):
        for months_ago in (0, 2):
            BalanceSnapshot.objects.create(
                account=account,
                snapshot_date=today.replace(day=1) - timedelta(days=31 * months_ago),
                amount_minor=125_000 + index * 10_000,
                currency="USD",
                source=BalanceSnapshot.Source.MANUAL,
            )


def _check_columns(layout, width):
    assert layout["h1s"] == 1
    assert not layout["scroll"]
    main_left, main_top, main_width = layout["main"]
    side_left, side_top, side_width = layout["side"]
    if width >= 1280:
        assert side_width == 340
        assert side_left >= main_left + main_width
        assert abs(side_top - main_top) <= 1
    else:
        assert side_top > main_top
        assert abs(side_left - main_left) <= 1


@pytest.mark.parametrize("width", [390, 1024, 1440])
def test_net_worth_by_month_table_and_side_column(open_report, width):
    _add_balances()
    context, page = open_report("/net-worth/", width)
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"net-worth-{width}.png"), full_page=True)
        _check_columns(page.evaluate(LAYOUT_JS), width)
        months = page.get_by_role("table").filter(has=page.get_by_role("columnheader", name="Net worth"))
        periods = page.evaluate("JSON.parse(document.getElementById('net-worth-chart-data').textContent).periods.length")
        assert months.locator("tbody tr").count() == periods
        assert "carried forward" in months.inner_text()
        assert page.get_by_role("heading", name="By account", exact=False).is_visible()
        page.get_by_text("Filters", exact=True).click()
        assert page.get_by_role("button", name="Update").is_visible()
        assert not page.evaluate(LAYOUT_JS)["scroll"]
    finally:
        context.close()


@pytest.mark.parametrize("width", [390, 1024, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_spending_ranked_table_and_side_column(open_report, width, scheme):
    context, page = open_report("/spending/", width, scheme)
    try:
        if SCREEN_DIR:
            page.screenshot(path=str(Path(SCREEN_DIR) / f"spending-{width}-{scheme}.png"), full_page=True)
        _check_columns(page.evaluate(LAYOUT_JS), width)
        assert page.evaluate("[...document.querySelectorAll('main .overflow-x-auto')].every((box) => box.scrollWidth <= box.clientWidth)")
        table = page.get_by_role("table").filter(has=page.get_by_role("columnheader", name="Share"))
        names = [text.strip() for text in table.locator("tbody tr td:first-child a").all_inner_texts()]
        assert {"Uncategorized", "Groceries", "Dining"} <= set(names)
        assert "%" in table.locator("tbody tr").first.inner_text()
        assert page.locator("main .page-side").get_by_text("of spending has no category").is_visible()
        ranges = page.get_by_role("navigation", name="Date range")
        assert ranges.locator("[aria-current]").count() == 0
        ranges.get_by_role("link", name="Last 3 months").click()
        page.wait_for_load_state("networkidle")
        assert ranges.locator("[aria-current=true]").inner_text().strip() == "Last 3 months"
        assert not page.evaluate(LAYOUT_JS)["scroll"]
    finally:
        context.close()
