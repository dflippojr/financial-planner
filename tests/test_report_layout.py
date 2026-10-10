"""Report page layout in a real browser (issue #338, Desktop 12).

Monthly review, Year-end, Sheet comparison and Debt payoff share the page header and the
two-across report cards. Skipped like tests/test_mobile_layout.py without Playwright or a
compiled static/dist/app.css.
"""

from pathlib import Path

import pytest

from tests.browser_support import CSS, SCREEN_DIR, browser, page_session  # noqa: F401 - fixtures

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

REPORT_PAGES = (
    ("monthly-review", "/planning/review/"),
    ("year-end", "/planning/year-end/"),
    ("sheet-comparison", "/planning/sheet-comparison/"),
    ("debt-payoff", "/planning/debts/"),
)

LAYOUT_JS = """() => {
  const box = (el) => { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.width)]; };
  const grid = document.querySelector('main .xl\\\\:grid-cols-2');
  return {
    h1s: document.querySelectorAll('h1').length,
    header: !!document.querySelector('main .page-header'),
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    cards: grid ? [...grid.children].slice(0, 2).map(box) : [],
  };
}"""


def _open(page_session, width, path):  # noqa: F811 - the fixture is passed in
    return page_session((width, 900), "light", path)


@pytest.mark.parametrize("width", [390, 1024, 1440])
@pytest.mark.parametrize(("name", "path"), REPORT_PAGES)
def test_report_pages_share_the_header_and_fit(page_session, width, name, path):  # noqa: F811
    context, page = _open(page_session, width, path)
    try:
        if SCREEN_DIR:
            Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SCREEN_DIR) / f"{name}-{width}.png"), full_page=True)
        layout = page.evaluate(LAYOUT_JS)
        assert layout["h1s"] == 1
        assert layout["header"]
        assert not layout["scroll"], f"{name} scrolls sideways at {width}"
        if name == "monthly-review":
            first, second = layout["cards"]
            if width >= 1280:
                assert first[1] == second[1] and second[0] > first[0] + first[2]
            else:
                assert second[1] > first[1] and first[0] == second[0]
    finally:
        context.close()


def test_monthly_review_comparisons_are_signed_and_aligned(page_session):  # noqa: F811
    context, page = _open(page_session, 1440, "/planning/review/")
    try:
        cells = page.locator("main dl dd")
        assert cells.count() == 6
        for index in range(cells.count()):
            cell = cells.nth(index)
            assert cell.evaluate("el => getComputedStyle(el).fontVariantNumeric") == "tabular-nums"
            assert cell.evaluate("el => getComputedStyle(el).textAlign") == "right"
            text = cell.inner_text().strip()
            assert text in ("—", "$0.00") or text[0] in "+\N{MINUS SIGN}", text
    finally:
        context.close()


def test_year_end_exports_are_reachable_from_the_keyboard(page_session):  # noqa: F811
    context, page = _open(page_session, 1280, "/planning/year-end/")
    try:
        toggle = page.locator("main summary", has_text="Export")
        menu = page.locator("main details[data-row-menu]")
        toggle.focus()
        page.keyboard.press("Enter")
        assert menu.evaluate("el => el.open")
        names = []
        for _ in range(8):
            page.keyboard.press("Tab")
            names.append(page.evaluate("document.activeElement.textContent.trim()"))
        assert names == [
            "Cash-flow CSV",
            "Spending CSV",
            "Income CSV",
            "Accounts CSV",
            "Tags CSV",
            "Recurring CSV",
            "Net-worth CSV",
            "Print or save as PDF",
        ]
        hrefs = menu.locator("a").evaluate_all("links => links.map((a) => a.getAttribute('href'))")
        assert all(href.startswith("/planning/year-end/csv/") and "year=" in href for href in hrefs), hrefs
        page.evaluate("window.print = () => { window.__printed = true; }")
        page.keyboard.press("Enter")
        assert page.evaluate("window.__printed === true")
        assert not menu.evaluate("el => el.open")
        page.keyboard.press("Shift+Tab")
        toggle.focus()
        page.keyboard.press("Enter")
        page.keyboard.press("Escape")
        assert not menu.evaluate("el => el.open")
    finally:
        context.close()
