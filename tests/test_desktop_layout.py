"""Desktop-width layout checks in a real browser (issue #340).

Every main page at 1440x900, 1280x800 and 1024x768, in light and dark, on synthetic seed data.
Needs Playwright with Chromium and a compiled static/dist/app.css (scripts/build_css.sh); skipped
otherwise. Set DESKTOP_SHOTS_DIR to also save a full-page screenshot of every page.
"""

import os
from pathlib import Path

import pytest
from django.conf import settings

from tests.desktop_seed import seed_desktop_data

# Playwright's sync API keeps an event loop running in this thread, which Django's ORM guard rejects.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

sync_api = pytest.importorskip("playwright.sync_api")

CSS = Path(settings.BASE_DIR) / "static" / "dist" / "app.css"
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not CSS.exists(), reason="static/dist/app.css is not built"),
]

VIEWPORTS = {"1440x900": (1440, 900), "1280x800": (1280, 800), "1024x768": (1024, 768)}
PAGES = (
    ("home", "/"),
    ("transactions", "/transactions/"),
    ("budgets", "/planning/budgets/"),
    ("chat", "/chat/"),
    ("accounts", "/accounts/"),
    ("net-worth", "/net-worth/"),
    ("spending", "/spending/"),
    ("recurring", "/recurring/"),
    ("transfers", "/transfers/"),
    ("bills", "/planning/calendar/"),
    ("goals", "/planning/goals/"),
    ("planned-items", "/planning/items/"),
    ("import", "/imports/"),
    ("alerts", "/alerts/"),
    ("monthly-review", "/planning/review/"),
    ("year-end", "/planning/year-end/"),
    ("settings-security", "/settings/security/"),
    ("settings-connections", "/settings/connections/"),
    ("settings-household", "/settings/household/"),
    ("settings-categories", "/settings/categories/"),
    ("settings-rules", "/settings/categories/rules/"),
    ("settings-tags", "/settings/tags/"),
    ("settings-csv-mappings", "/settings/csv-mappings/"),
    ("settings-alerts", "/settings/alerts/"),
    ("settings-data", "/settings/data/"),
    ("settings-ai", "/settings/ai/"),
    ("settings-audit", "/settings/audit/"),
)
SCREEN_DIR = os.environ.get("DESKTOP_SHOTS_DIR")


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
def desktop_session(live_server, client, browser):
    person = seed_desktop_data()
    client.force_login(person.user)
    cookie = client.cookies["sessionid"].value
    contexts = []

    def open_page(size, scheme, path):
        width, height = VIEWPORTS[size]
        context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
        contexts.append(context)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": live_server.url}])
        page = context.new_page()
        response = page.goto(live_server.url + path)
        assert response is not None and response.ok, f"{path} answered {response and response.status}"
        page.wait_for_load_state("networkidle")
        return page

    yield open_page
    for context in contexts:
        context.close()


LAYOUT_JS = """() => {
  const shown = (el) => el.checkVisibility({ opacityProperty: true, visibilityProperty: true }) && el.getBoundingClientRect().width > 0;
  const label = (el) => (el.getAttribute('aria-label') || el.textContent || el.className).trim().slice(0, 40);
  const fixed = [...document.querySelectorAll('body *')].filter((el) => {
    if (getComputedStyle(el).position !== 'fixed' || !shown(el)) return false;
    const r = el.getBoundingClientRect();
    return r.right > 0 && r.left < innerWidth && r.bottom > 0 && r.top < innerHeight;
  });
  return {
    scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    h1s: document.querySelectorAll('h1').length,
    headerPrimary: [...document.querySelectorAll('main .page-header .btn-primary')].filter(shown).map(label),
    fixed: fixed.map(label),
  };
}"""

# Paints each background layer from the root down on a 1x1 canvas, so translucent fills and
# oklch colours resolve the way the page draws them, then the text over it at its net opacity.
CONTRAST_JS = """() => {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const pixel = () => [...ctx.getImageData(0, 0, 1, 1).data.slice(0, 3)];
  const luminance = ([r, g, b]) => {
    const channel = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  };
  const seen = new Map();
  for (const el of document.querySelectorAll('.amount-in, .amount-out')) {
    if (!el.checkVisibility() || el.getBoundingClientRect().width === 0) continue;
    const chain = [];
    let opacity = 1;
    for (let node = el; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      chain.unshift(style.backgroundColor);
      opacity *= Number(style.opacity);
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, 1, 1);
    for (const colour of chain) { ctx.fillStyle = colour; ctx.fillRect(0, 0, 1, 1); }
    const background = pixel();
    ctx.globalAlpha = opacity;
    ctx.fillStyle = getComputedStyle(el).color;
    ctx.fillRect(0, 0, 1, 1);
    const text = pixel();
    const [light, dark] = [luminance(text), luminance(background)].sort((a, b) => b - a);
    const ratio = (light + 0.05) / (dark + 0.05);
    const tone = el.classList.contains('amount-in') ? 'amount-in' : 'amount-out';
    const key = `${tone} ${text} on ${background}`;
    if (!seen.has(key)) seen.set(key, { tone, text, background, ratio: Math.round(ratio * 100) / 100, sample: el.textContent.trim() });
  }
  return [...seen.values()];
}"""


@pytest.mark.parametrize("size", VIEWPORTS)
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("name", "path"), PAGES)
def test_page_fits_desktop(desktop_session, size, scheme, name, path):
    page = desktop_session(size, scheme, path)
    if SCREEN_DIR:
        Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SCREEN_DIR) / f"{name}-{size}-{scheme}.png"), full_page=True)
    layout = page.evaluate(LAYOUT_JS)
    assert not layout["scroll"], f"{name} scrolls sideways"
    assert layout["h1s"] == 1, f"{name} has {layout['h1s']} h1 elements"
    assert len(layout["headerPrimary"]) <= 1, f"{name} header has primary buttons {layout['headerPrimary']}"
    assert layout["fixed"] == [], f"{name} has floating elements {layout['fixed']}"
    low = [amount for amount in page.evaluate(CONTRAST_JS) if amount["ratio"] < 4.5]
    assert low == [], f"{name} has amounts under 4.5:1: {low}"


def _bottom(locator):
    box = locator.bounding_box()
    assert box is not None, f"{locator} is not shown"
    return box["y"] + box["height"]


def test_home_totals_and_needs_attention_are_above_the_fold(desktop_session):
    page = desktop_session("1440x900", "light", "/")
    numbers = page.get_by_role("region", name="Key numbers")
    for label in ("Net cash flow", "Income", "Spending"):
        heading = numbers.get_by_role("heading", name=label, exact=True)
        assert _bottom(heading.locator("xpath=following-sibling::p[1]")) <= 900, label
    attention = page.locator("#attention-heading-side")
    assert attention.is_visible()
    assert _bottom(attention) <= 900
    assert _bottom(page.locator("main .page-side li").first) <= 900


@pytest.mark.parametrize("size", VIEWPORTS)
@pytest.mark.parametrize(
    ("path", "selector"),
    [
        ("/transactions/", "main tbody tr"),
        ("/planning/goals/", "main section[aria-labelledby=goals-list-heading] li"),
        ("/planning/items/", "main tbody tr"),
    ],
)
def test_first_row_is_above_the_fold(desktop_session, size, path, selector):
    page = desktop_session(size, "light", path)
    first = page.locator(selector).locator("visible=true").first
    assert _bottom(first) <= VIEWPORTS[size][1], f"first row of {path} is below the fold at {size}"
