"""Characterize the Chart.js options static/js/charts.js builds (issue #584).

The real script runs in Chromium against a capturing Chart stub and synthetic inline payloads, so
the shared tooltip callback and axis options can be consolidated without changing what any chart
receives. Expected values are literal; nothing here rebuilds the options the way the script does.
"""

import json
from pathlib import Path

from django.conf import settings

from tests.browser_support import browser  # noqa: F401 - fixture

SCRIPT = (Path(settings.BASE_DIR) / "static" / "js" / "charts.js").read_text(encoding="utf-8")
ORIGIN = "http://charts.test"
T, G, DARK = "rgb(10, 10, 10)", "rgb(20, 20, 20)", "rgb(210, 210, 210)"
COLORS = ";".join(f"--chart-category-{i}: rgb(10{i}, 0, 0)" for i in range(8))
PERIODS = [
    {"label": "Jan 2026", "income_minor": 100000, "spending_minor": 50000, "net_minor": 50000,
     "income_display": "$1,000.00", "spending_display": "$500.00", "net_display": "$500.00",
     "assets_minor": 900000, "liabilities_minor": 1000, "assets_display": "$9,000.00",
     "liabilities_display": "$10.00", "missing_import": False, "drilldown_url": "/drill/jan/"},
    {"label": "Feb 2026", "income_minor": 0, "spending_minor": 0, "net_minor": 0, "projected": True,
     "scenario_net_minor": 25, "drilldown_url": "/drill/feb/"},
]
PAYLOADS = {
    "cash-flow": {"periods": PERIODS},
    "debt-payoff": {"labels": ["Jan 2026"], "interest_minor": [120], "remaining_minor": [5000]},
    "net-worth": {"periods": PERIODS},
    "spending": {"chart_rows": [{"name": "Groceries", "spending_minor": 300, "color_index": 1,
                                 "spending_display": "$3.00", "share_display": "100%",
                                 "drilldown_url": "/drill/groceries/"}]},
    "spending-trend": {"periods": PERIODS, "chart_series": [
        {"name": "Rent", "values": [1, 2], "color_index": 2, "displays": ["$0.01", "$0.02"],
         "detail_url": "/drill/rent/"}]},
    "category-trend": {"name": "Rent", "color_index": 3, "periods": PERIODS},
}
CARTESIAN = ["cash-flow", "debt-payoff", "net-worth", "spending-trend", "category-trend"]
STUB = """window.__charts = [];
window.Chart = function (canvas, config) { this.canvas = canvas; this.config = config; window.__charts.push(this); };
window.Chart.prototype.destroy = function () { this.destroyed = true; };
window.Chart.defaults = { animation: {} };"""
SNAPSHOT_JS = """() => window.__charts.map((chart) => {
  const o = chart.config.options;
  const cb = (o.plugins.tooltip || {}).callbacks || {};
  const ctx = (displays, dataIndex) => ({dataset: {label: 'Rent', displays}, dataIndex, label: 'Groceries', formattedValue: '7'});
  return {
    kind: chart.canvas.dataset.chart, destroyed: !!chart.destroyed, sizing: [o.responsive, o.maintainAspectRatio],
    scales: JSON.parse(JSON.stringify(o.scales || null, (k, v) => typeof v === 'function' ? v(123456) : v)),
    labels: [ctx(['$1.00', ''], 0), ctx(['$1.00', ''], 1), ctx(['$1.00'], 4), ctx(undefined, 0)].map(cb.label),
    titles: cb.title ? [[{dataIndex: 0}], [{dataIndex: 1}], []].map(cb.title) : null,
    legend: o.plugins.legend, plugins: (chart.config.plugins || []).map((p) => p.id), clicks: !!o.onClick,
  };
})"""


def _page_html():
    canvases = "".join(
        f'<script type="application/json" id="p-{kind}">{json.dumps(data)}</script>'
        f'<canvas data-chart="{kind}" data-chart-payload="p-{kind}"></canvas>'
        for kind, data in PAYLOADS.items()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
:root {{ --color-base-content: {T}; --color-base-300: {G}; --color-success: rgb(30, 0, 0);
  --color-error: rgb(40, 0, 0); --color-primary: rgb(50, 0, 0); --chart-income: rgb(60, 0, 0);
  --chart-spending: rgb(70, 0, 0); --color-warning: rgb(80, 0, 0); {COLORS} }}
:root[data-theme=dark] {{ --color-base-content: {DARK}; }}
</style><script>{STUB}</script><script>{SCRIPT}</script></head><body>{canvases}</body></html>"""


def _open(browser):  # noqa: F811 - the fixture's value
    page = browser.new_context(locale="en-US").new_page()
    html = _page_html()
    page.route(f"{ORIGIN}/**", lambda route: route.fulfill(
        content_type="text/html", body=html if route.request.url == f"{ORIGIN}/" else "<p>drilldown</p>"))
    page.goto(f"{ORIGIN}/")
    return page


def _scales(text, grid, stacked=False):
    x = {"ticks": {"color": text, "maxRotation": 45, "minRotation": 0}, "grid": {"color": grid}}
    y = {"ticks": {"color": text, "callback": "1,234.56"}, "title": {"display": True, "text": "USD", "color": text},
         "grid": {"color": grid}}
    if stacked:
        x["stacked"] = y["stacked"] = True
    return {"x": x, "y": y}


def test_cartesian_charts_share_axes_tooltips_and_keep_their_own_options(browser):  # noqa: F811
    page = _open(browser)
    charts = {chart["kind"]: chart for chart in page.evaluate(SNAPSHOT_JS)}
    page.context.close()

    assert list(charts) == list(PAYLOADS)
    for kind in CARTESIAN:
        assert charts[kind]["sizing"] == [True, False], kind
        assert charts[kind]["scales"] == _scales(T, G, stacked=kind == "spending-trend"), kind
        assert charts[kind]["labels"] == ["Rent: $1.00", "Rent: 7", "Rent: 7", "Rent: 7"], kind
    assert charts["spending"]["scales"] is None
    assert charts["spending"]["labels"] == ["Groceries: $1.00", "Groceries: 7", "Groceries: 7", "Groceries: 7"]
    assert charts["cash-flow"]["titles"] == ["Jan 2026", "Feb 2026 · Projected (estimate)", ""]
    assert [kind for kind, chart in charts.items() if chart["titles"] is not None] == ["cash-flow"]
    assert {kind: chart["legend"] for kind, chart in charts.items()} == {
        "cash-flow": {"display": False},
        "debt-payoff": {"labels": {"color": T}},
        "net-worth": {"labels": {"color": T}},
        "spending": {"position": "bottom", "labels": {"color": T}},
        "spending-trend": {"position": "bottom", "labels": {"color": T}},
        "category-trend": {"display": False},
    }
    assert charts["cash-flow"]["plugins"] == ["missingImportMarker", "projectedBarOutline"]
    assert not any(chart["plugins"] for kind, chart in charts.items() if kind != "cash-flow")
    assert [kind for kind, chart in charts.items() if chart["clicks"]] == ["cash-flow", "spending", "spending-trend"]


def test_charts_get_independent_scales_and_rebuild_on_theme_change(browser):  # noqa: F811
    page = _open(browser)
    distinct = page.evaluate("""() => {
      const scales = window.__charts.map((c) => c.config.options.scales).filter(Boolean);
      const parts = scales.flatMap((s) => [s, s.x, s.y, s.x.ticks, s.y.ticks, s.y.title, s.x.grid, s.y.grid]);
      scales[0].x.ticks.color = 'mutated';
      return new Set(parts).size === parts.length && scales.slice(1).every((s) => s.x.ticks.color !== 'mutated');
    }""")
    assert distinct

    page.evaluate("""() => { document.documentElement.dataset.theme = 'dark';
      window.dispatchEvent(new Event('financial-planner:themechange')); }""")
    charts = page.evaluate(SNAPSHOT_JS)

    assert [chart["destroyed"] for chart in charts] == [True] * 6 + [False] * 6
    rebuilt = {chart["kind"]: chart for chart in charts[6:]}
    for kind in CARTESIAN:
        assert rebuilt[kind]["scales"] == _scales(DARK, G, stacked=kind == "spending-trend"), kind
    assert page.evaluate("window.Chart.defaults.color") == DARK
    page.context.close()


def test_chart_clicks_open_their_drilldown(browser):  # noqa: F811
    clicks = [
        ("cash-flow", "[{index: 1}]", "/drill/feb/"),
        ("spending", "[{index: 0}]", "/drill/groceries/"),
        ("spending-trend", "[{datasetIndex: 0, index: 1}]", "/drill/rent/"),
    ]
    for kind, elements, path in clicks:
        page = _open(browser)
        page.evaluate(f"""() => {{ const chart = window.__charts.find((c) => c.canvas.dataset.chart === '{kind}');
          chart.config.options.onClick({{}}, []); chart.config.options.onClick({{}}, {elements}); }}""")
        page.wait_for_url(f"{ORIGIN}{path}")
        page.context.close()
