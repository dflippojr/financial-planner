"""Desktop-width layout checks in a real browser (issue #340).

Every main page at 1440x900, 1280x800 and 1024x768, in light and dark, on synthetic seed data.
Needs Playwright with Chromium and a compiled static/dist/app.css (scripts/build_css.sh); skipped
otherwise. Set DESKTOP_SHOTS_DIR to also save a full-page screenshot of every page.
Sideways overflow on every page at these widths is owned by tests/ui/breakpoints.spec.ts.
"""

import os
from pathlib import Path

import pytest

from finance.context_processors import SETTINGS_TABS
from finance.policy_services import publish_policy
from tests.browser_support import CSS, browser, check_each_page, visit  # noqa: F401 - browser is a fixture
from tests.desktop_seed import seed_desktop_data

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
        visit(page, live_server.url, path)
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
def test_page_fits_desktop(desktop_session, live_server, size, scheme):
    # One seed and one browser context per size and theme; every page loads in it in turn.
    page = desktop_session(size, scheme, "/")

    def check(name, path):
        visit(page, live_server.url, path, wait_until="load")
        if SCREEN_DIR:
            Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SCREEN_DIR) / f"{name}-{size}-{scheme}.png"), full_page=True)
        layout = page.evaluate(LAYOUT_JS)
        assert layout["h1s"] == 1, f"has {layout['h1s']} h1 elements"
        assert len(layout["headerPrimary"]) <= 1, f"header has primary buttons {layout['headerPrimary']}"
        assert layout["fixed"] == [], f"has floating elements {layout['fixed']}"
        low = [amount for amount in page.evaluate(CONTRAST_JS) if amount["ratio"] < 4.5]
        assert low == [], f"has amounts under 4.5:1: {low}"
        if name.startswith("settings-"):
            sections = page.get_by_role("navigation", name="Settings sections")
            links = sections.get_by_role("link")
            assert links.count() == len(SETTINGS_TABS)
            for link in links.all():
                assert link.is_visible()
                box = link.bounding_box()
                assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= VIEWPORTS[size][0]
            active = sections.locator('[aria-current="page"]')
            assert active.count() == 1
            section_path = "/settings/categories/" if name == "settings-rules" else path
            assert active.get_attribute("href") == section_path
            assert not page.get_by_role("tablist").is_visible()

    check_each_page(PAGES, check)


def test_settings_ai_has_one_pending_privacy_prompt(desktop_session):
    # The prompt text and counts do not depend on theme or width. The narrowest desktop width is the
    # one where the extra prompt could add sideways scroll, so that is the one checked.
    publish_policy(material=True, body="Synthetic desktop privacy policy")
    page = desktop_session("1024x768", "light", "/settings/ai/")
    prompt = page.get_by_role("region", name="Privacy and data policy")
    assert prompt.is_visible()
    assert page.get_by_text("Privacy and data policy", exact=True).count() == 1
    assert page.get_by_role("button", name="Accept version", exact=False).count() == 1
    assert page.get_by_role("button", name="Not now", exact=True).count() == 1
    assert "needs a response before you can use AI" not in page.locator("main").inner_text()
    assert not page.evaluate(LAYOUT_JS)["scroll"]


def _bottom(locator):
    box = locator.bounding_box()
    assert box is not None, f"{locator} is not shown"
    return box["y"] + box["height"]


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
