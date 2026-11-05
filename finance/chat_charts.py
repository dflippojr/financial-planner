"""The chat's make_chart tool (#646): charts whose numbers the server computes.

The model names a chart kind, a report, dates and filters. Every value drawn
comes from the same service the matching report page uses, under the same
visibility and household-consent rules as the other chat tools, so a chart can
never show a number that is not in the member's data. The model gets back only
the figures the matching report tool would give it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from .ai_tools import (
    MAX_TOOL_ROWS,
    WITHHELD_CATEGORY_NAME,
    _account_ids,
    _ai_scope,
    _figure,
    _range_query,
    _transaction_scope,
    _visible_account,
    ai_nameable_categories,
    household_consent_complete,
    withheld_category_ids,
)
from .ai_types import ToolResult, ToolSpec
from .budget_services import month_budget_cards
from .cash_flow import (
    GROUPING_MONTH,
    GROUPING_WEEK,
    cash_flow_report,
    category_color_index,
    default_date_range,
    format_minor,
    iter_period_windows,
    period_count,
    period_label,
    selected_accounts,
    spending_by_category_report,
    spending_category_detail_url,
)
from .category_services import income_and_spending_by_window
from .models import Account, Category
from .net_worth import net_worth_report
from .spending_trends import category_spending_trend_report

CHART_VERSION = 1
MAX_CHARTS_PER_REPLY = 3
MAX_PERIODS = 24
MAX_CATEGORIES = 12
MAX_SEARCH_CHARS = 80
OTHER_NAME = "Other"

OVER_TIME = "over_time"
CATEGORY_BREAKDOWN = "category_breakdown"
BUDGET_VS_ACTUAL = "budget_vs_actual"
COMPARE_PERIODS = "compare_periods"

# The allow-list: each kind, the reports it draws, and the styles it takes (first is the default).
KINDS = {
    OVER_TIME: {
        "reports": ("income", "spending", "net", "net_worth", "category", "merchant"),
        "styles": ("bar", "line"),
    },
    CATEGORY_BREAKDOWN: {"reports": ("spending",), "styles": ("donut", "bar")},
    BUDGET_VS_ACTUAL: {"reports": ("budgets",), "styles": ("bar",)},
    COMPARE_PERIODS: {"reports": ("spending",), "styles": ("bar",)},
}
# Filters each report accepts besides kind, report, style and the date range.
FILTERS = {
    (OVER_TIME, "income"): {"grouping", "account_id", "scope"},
    (OVER_TIME, "spending"): {"grouping", "account_id", "scope"},
    (OVER_TIME, "net"): {"grouping", "account_id", "scope"},
    (OVER_TIME, "net_worth"): {"grouping", "scope"},
    (OVER_TIME, "category"): {"grouping", "account_id", "scope", "category_id", "category"},
    (OVER_TIME, "merchant"): {"grouping", "account_id", "scope", "description_contains"},
    (CATEGORY_BREAKDOWN, "spending"): {"account_id", "scope"},
    (BUDGET_VS_ACTUAL, "budgets"): {"scope"},
    (COMPARE_PERIODS, "spending"): {"account_id", "scope", "compare_from", "compare_to"},
}
COMMON = {"kind", "report", "style", "date_from", "date_to"}
ALLOWED_ARGS = COMMON | set().union(*FILTERS.values())
GROUPING_CHOICES = (GROUPING_MONTH, GROUPING_WEEK)

DESCRIPTION = (
    "Draw a chart of the member's data in your reply. You never supply numbers: the server computes "
    "every value from the same report the app shows and returns them to you. Kinds and reports: "
    "over_time with income, spending, net, net_worth, category (needs category or category_id) or "
    "merchant (needs description_contains), grouped by month or week (net_worth is monthly), "
    "style bar or line; category_breakdown with spending, style donut or bar; budget_vs_actual with "
    "budgets for the month holding date_from; compare_periods with spending, date_from..date_to "
    f"against compare_from..compare_to. Dates are YYYY-MM-DD. At most {MAX_PERIODS} periods and "
    f"{MAX_CHARTS_PER_REPLY} charts per reply. Describe a chart only with the figures it returns."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "report": {"type": "string", "enum": sorted({report for spec in KINDS.values() for report in spec["reports"]})},
        "style": {"type": "string", "enum": ["bar", "line", "donut"]},
        "date_from": {"type": "string"},
        "date_to": {"type": "string"},
        "grouping": {"type": "string", "enum": list(GROUPING_CHOICES)},
        "account_id": {"type": "integer"},
        "scope": {"type": "string", "enum": [Account.Scope.PRIVATE, Account.Scope.HOUSEHOLD]},
        "category_id": {"type": "integer"},
        "category": {"type": "string"},
        "description_contains": {"type": "string"},
        "compare_from": {"type": "string"},
        "compare_to": {"type": "string"},
    },
    "required": ["kind", "report"],
    "additionalProperties": False,
}
CATEGORY_NOT_VISIBLE = "That category is not visible."


class ChartError(Exception):
    """A reason the model can act on; never names or amounts the member cannot see."""


def chart_tools():
    """The make_chart tool for one chat turn; it draws at most MAX_CHARTS_PER_REPLY charts."""
    drawn = []

    def handler(person, args):
        if len(drawn) >= MAX_CHARTS_PER_REPLY:
            return ToolResult(text=f"At most {MAX_CHARTS_PER_REPLY} charts fit in one reply.", ok=False)
        result = make_chart(person, args)
        if result.ok:
            drawn.append(result)
        return result

    return (ToolSpec(name="make_chart", description=DESCRIPTION, parameters=PARAMETERS, handler=handler),)


def make_chart(person, args):
    try:
        spec = _validated(person, args if isinstance(args, dict) else {})
        built = BUILDERS[spec["kind"]](person, spec)
    except ChartError as exc:
        return ToolResult(text=str(exc), ok=False)
    if not built["labels"]:
        return ToolResult(text="Nothing to chart in that range. Try other dates or filters.", ok=False)
    chart = {
        "v": CHART_VERSION,
        "kind": spec["kind"],
        "report": spec["report"],
        "style": built.get("style", spec["style"]),
        "spec": _public_spec(spec),
        "title": built["title"],
        "labels": built["labels"],
        "series": built["series"],
        "page_url": built["page_url"],
        "currency": "USD",
    }
    chart["summary"] = _summary(chart)
    figures = tuple(
        _figure(f"{label} {series['name']}".strip(), value, chart["page_url"])
        for series in chart["series"]
        for label, value in zip(chart["labels"], series["values"], strict=True)
    )[:MAX_TOOL_ROWS]
    payload = {
        "chart": "shown to the member under your answer",
        "title": chart["title"],
        "page_url": chart["page_url"],
        "labels": chart["labels"],
        "series": [
            {"name": series["name"], "values_minor": series["values"], "displays": series["displays"]}
            for series in chart["series"]
        ],
    }
    return ToolResult(
        text=json.dumps(payload),
        figures=figures,
        account_ids=built.get("account_ids", ()),
        budget_ids=built.get("budget_ids", ()),
        charts=(chart,),
    )


# ---------------------------------------------------------------------------
# Validation: an allow-list of kinds, reports and filters; nothing else passes
# ---------------------------------------------------------------------------


def _validated(person, args):
    unknown = sorted(str(key) for key in args if key not in ALLOWED_ARGS)
    if unknown:
        raise ChartError(
            f"make_chart does not take {', '.join(unknown)[:120]}. The server computes every value; "
            "pass only kind, report, style, dates and filters."
        )
    kind = args.get("kind")
    if kind not in KINDS:
        raise ChartError(f"Unknown chart kind. Use one of: {', '.join(KINDS)}.")
    report = args.get("report")
    if report not in KINDS[kind]["reports"]:
        raise ChartError(f"Charts of kind {kind} draw one of these reports: {', '.join(KINDS[kind]['reports'])}.")
    allowed = FILTERS[(kind, report)]
    extra = sorted(key for key in args if key not in COMMON and key not in allowed and args[key] not in (None, ""))
    if extra:
        raise ChartError(f"Charts of kind {kind} and report {report} do not take {', '.join(extra)}.")
    style = args.get("style") or KINDS[kind]["styles"][0]
    if report == "net_worth" and not args.get("style"):
        style = "line"
    if style not in KINDS[kind]["styles"]:
        raise ChartError(f"Charts of kind {kind} are drawn as: {', '.join(KINDS[kind]['styles'])}.")
    today = timezone.localdate()
    default_from, default_to = default_date_range(today)
    if kind == BUDGET_VS_ACTUAL:
        # Budgets are monthly: the chart covers the whole month holding date_from; date_to is not used.
        date_from = _date(args, "date_from", today).replace(day=1)
        date_to = (date_from + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    else:
        date_from = _date(args, "date_from", default_from)
        date_to = _date(args, "date_to", default_to)
    if date_from > date_to:
        raise ChartError("date_from must be on or before date_to.")
    spec = {"kind": kind, "report": report, "style": style, "date_from": date_from, "date_to": date_to}
    spec["grouping"] = _grouping(args, report)
    account, error = _visible_account(person, args.get("account_id"), None)
    if error:
        raise ChartError(error)
    spec["account"] = account
    spec["scope"] = _scope(args.get("scope"))
    if not spec["scope"] and not household_consent_complete(person):
        # Until household consent is complete the tools see only private accounts, so the chart
        # and the report page it links to both say so (cash_flow_totals does the same).
        spec["scope"] = Account.Scope.PRIVATE
    if report == "category":
        spec["category"] = _category(person, args)
    if report == "merchant":
        spec["description_contains"] = _search_text(args.get("description_contains"))
    if kind == COMPARE_PERIODS:
        spec["compare_from"] = _date(args, "compare_from", None)
        spec["compare_to"] = _date(args, "compare_to", None)
        if spec["compare_from"] is None or spec["compare_to"] is None:
            raise ChartError("compare_periods needs compare_from and compare_to.")
        if spec["compare_from"] > spec["compare_to"]:
            raise ChartError("compare_from must be on or before compare_to.")
    if kind == OVER_TIME and period_count(date_from, date_to, spec["grouping"]) > MAX_PERIODS:
        raise ChartError(
            f"A chart shows at most {MAX_PERIODS} periods. Narrow the dates or group by month."
        )
    return spec


def _date(args, key, fallback):
    value = args.get(key)
    if value in (None, ""):
        return fallback
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ChartError(f"{key} must be a date like 2026-01-31.") from None


def _grouping(args, report):
    grouping = args.get("grouping") or GROUPING_MONTH
    if grouping not in GROUPING_CHOICES:
        raise ChartError("grouping must be month or week.")
    if report == "net_worth" and grouping != GROUPING_MONTH:
        raise ChartError("Net worth is charted by month.")
    return grouping


def _scope(value):
    text = (value or "").strip() if isinstance(value, str) else value
    if text in (None, ""):
        return ""
    if text not in (Account.Scope.PRIVATE, Account.Scope.HOUSEHOLD):
        raise ChartError("scope must be private or household.")
    return text


def _category(person, args):
    """A category the AI may name, chosen by id or name; a withheld one answers like a missing one (#521)."""
    nameable = ai_nameable_categories(person).exclude(code=Category.Code.TRANSFER)
    if args.get("category_id") not in (None, ""):
        try:
            match = nameable.filter(pk=int(args["category_id"])).first()
        except (TypeError, ValueError):
            match = None
    elif str(args.get("category") or "").strip():
        wanted = str(args["category"]).strip()
        if wanted.lower() == "uncategorized":
            match = nameable.filter(code=Category.Code.UNCATEGORIZED).first()
        else:
            match = nameable.filter(name__iexact=wanted).first()
    else:
        raise ChartError("A category chart needs category or category_id.")
    if match is None:
        raise ChartError(CATEGORY_NOT_VISIBLE)
    if match.code == Category.Code.UNCATEGORIZED:
        return {"key": "uncategorized", "name": "Uncategorized", "id": match.pk}
    return {"key": str(match.pk), "name": match.name, "id": match.pk}


def _search_text(value):
    text = " ".join(str(value or "").split())
    if len(text) < 2:
        raise ChartError("A merchant chart needs description_contains of at least 2 characters.")
    return text[:MAX_SEARCH_CHARS]


def _public_spec(spec):
    out = {
        "kind": spec["kind"],
        "report": spec["report"],
        "style": spec["style"],
        "date_from": spec["date_from"].isoformat(),
        "date_to": spec["date_to"].isoformat(),
    }
    if spec["kind"] == OVER_TIME and spec["report"] != "net_worth":
        out["grouping"] = spec["grouping"]
    if spec["account"] is not None:
        out["account_id"] = spec["account"].pk
    if spec["scope"]:
        out["scope"] = spec["scope"]
    if "category" in spec:
        out["category_id"] = spec["category"]["id"]
    if "description_contains" in spec:
        out["description_contains"] = spec["description_contains"]
    if spec["kind"] == COMPARE_PERIODS:
        out["compare_from"] = spec["compare_from"].isoformat()
        out["compare_to"] = spec["compare_to"].isoformat()
    return out


# ---------------------------------------------------------------------------
# Builders: each one calls the service behind the report page it links to
# ---------------------------------------------------------------------------


def _series(name, values, color, colors=None):
    values = [int(value) for value in values]
    series = {"name": name, "color": color, "values": values, "displays": [format_minor(value) for value in values]}
    if colors is not None:
        series["colors"] = list(colors)
    return series


def _range_text(date_from, date_to):
    return f"{date_from.isoformat()} to {date_to.isoformat()}"


CASH_FLOW_SERIES = {
    "income": ("Income", "income_minor", "income"),
    "spending": ("Spending", "spending_minor", "spending"),
    "net": ("Net cash flow", "net_minor", "primary"),
}


def _over_time(person, spec):
    builder = {"net_worth": _net_worth_over_time, "category": _category_over_time, "merchant": _merchant_over_time}
    return builder.get(spec["report"], _cash_flow_over_time)(person, spec)


def _cash_flow_over_time(person, spec):
    """The Home page's cash flow periods, as cash_flow_totals reads them."""
    accounts, scope = _transaction_scope(person, {"scope": spec["scope"]})
    account = spec["account"]
    report = cash_flow_report(
        person, date_from=spec["date_from"], date_to=spec["date_to"], grouping=spec["grouping"],
        account=account, scope=scope, accounts=accounts, actuals_only=True,
    )
    name, field, color = CASH_FLOW_SERIES[spec["report"]]
    query = _range_query(spec["date_from"], spec["date_to"], account=account, scope=scope)
    query["grouping"] = spec["grouping"]
    return {
        "title": f"{name} by {spec['grouping']}, {_range_text(spec['date_from'], spec['date_to'])}",
        "labels": [period.label for period in report.periods],
        "series": [_series(name, [getattr(period, field) for period in report.periods], color)],
        "page_url": f"{reverse('home')}?{urlencode(query)}",
        "account_ids": _account_ids(account, report.accounts),
    }


def _net_worth_over_time(person, spec):
    """The Net worth page's monthly totals, as net_worth_series reads them."""
    accounts, _include_household = _ai_scope(person)
    report = net_worth_report(
        person, date_from=spec["date_from"], date_to=spec["date_to"], scope=spec["scope"], accounts=accounts
    )
    query = _range_query(spec["date_from"], spec["date_to"], scope=spec["scope"])
    return {
        "title": f"Net worth by month, {_range_text(spec['date_from'], spec['date_to'])}",
        "labels": [period.label for period in report.periods],
        "series": [_series("Net worth", [period.net_minor for period in report.periods], "primary")],
        "page_url": f"{reverse('net-worth')}?{urlencode(query)}",
        "account_ids": _account_ids(None, report.accounts),
    }


def _category_over_time(person, spec):
    """One category's spending per period, from the category detail page's own report."""
    accounts, _include_household = _ai_scope(person)
    category = spec["category"]
    report = category_spending_trend_report(
        person, category_key=category["key"], category_name=category["name"],
        date_from=spec["date_from"], date_to=spec["date_to"], grouping=spec["grouping"],
        account=spec["account"], scope=spec["scope"], accounts=accounts,
    )
    color_key = "uncategorized" if category["key"] == "uncategorized" else int(category["key"])
    return {
        "title": f"{category['name']} spending by {spec['grouping']}, {_range_text(spec['date_from'], spec['date_to'])}",
        "labels": [period.label for period in report.periods],
        "series": [_series(
            category["name"], [period.spending_minor for period in report.periods],
            f"category-{category_color_index(color_key)}",
        )],
        "page_url": spending_category_detail_url(
            category["key"], spec["date_from"], spec["date_to"],
            account=spec["account"], scope=spec["scope"], grouping=spec["grouping"],
        ),
        "account_ids": _account_ids(spec["account"], report.accounts),
    }


def _merchant_over_time(person, spec):
    """Spending on transactions the transaction list's search matches, per period."""
    accounts, _include_household = _ai_scope(person)
    accounts = selected_accounts(
        person, account=spec["account"], scope=spec["scope"], cash_flow_only=True, accounts=accounts
    )
    today = timezone.localdate()
    windows = [
        window for window in iter_period_windows(spec["date_from"], spec["date_to"], spec["grouping"])
        if window.start <= today
    ]
    totals = income_and_spending_by_window(
        person, [(window.start, window.end) for window in windows], accounts=accounts,
        q=spec["description_contains"],
    )
    needle = spec["description_contains"]
    query = _range_query(spec["date_from"], spec["date_to"], account=spec["account"], scope=spec["scope"])
    query["q"] = needle
    return {
        "title": f"Spending matching “{needle}” by {spec['grouping']}, "
                 f"{_range_text(spec['date_from'], spec['date_to'])}",
        "labels": [period_label(window, today=today) for window in windows],
        "series": [_series("Spending", [spending for _income, spending in totals], "spending")],
        "page_url": f"{reverse('transaction-list')}?{urlencode(query)}",
        "account_ids": _account_ids(spec["account"], accounts),
    }


def _category_rows(person, report):
    """(key, name, amount, color_index) per category with spending, withheld names replaced (#521)."""
    withheld = withheld_category_ids(person)
    rows = []
    for row in report.rows:
        name = row.name
        if str(row.key).isdigit() and int(row.key) in withheld:
            name = WITHHELD_CATEGORY_NAME
        rows.append((row.key, name, row.spending_minor, row.color_index))
    return rows


def _top_with_other(rows, width):
    """Keep the first MAX_CATEGORIES - 1 rows and sum the rest into one "Other" row, value by value."""
    if len(rows) <= MAX_CATEGORIES:
        return rows
    kept = rows[: MAX_CATEGORIES - 1]
    rest = rows[MAX_CATEGORIES - 1 :]
    other = tuple(sum(row[2][index] for row in rest) for index in range(width))
    return [*kept, ("other", OTHER_NAME, other, category_color_index("other"))]


def _category_breakdown(person, spec):
    """The Spending by category page's rows for one range."""
    accounts, _include_household = _ai_scope(person)
    report = spending_by_category_report(
        person, date_from=spec["date_from"], date_to=spec["date_to"],
        account=spec["account"], scope=spec["scope"], accounts=accounts,
    )
    rows = [(key, name, (amount,), color) for key, name, amount, color in _category_rows(person, report) if amount]
    rows = _top_with_other(rows, 1)
    values = [row[2][0] for row in rows]
    style = spec["style"]
    if style == "donut" and any(value < 0 for value in values):
        # A net-refund category cannot be a slice; bars show it below zero instead.
        style = "bar"
    query = _range_query(spec["date_from"], spec["date_to"], account=spec["account"], scope=spec["scope"])
    return {
        "title": f"Spending by category, {_range_text(spec['date_from'], spec['date_to'])}",
        "style": style,
        "labels": [row[1] for row in rows],
        "series": [_series("Spending", values, "category", colors=[row[3] for row in rows])],
        "page_url": f"{reverse('spending-by-category')}?{urlencode(query)}",
        "account_ids": _account_ids(spec["account"], report.accounts),
    }


def _compare_periods(person, spec):
    """Two ranges of the Spending by category report, category by category."""
    accounts, _include_household = _ai_scope(person)
    reports = [
        spending_by_category_report(
            person, date_from=start, date_to=end, account=spec["account"], scope=spec["scope"], accounts=accounts,
        )
        for start, end in ((spec["date_from"], spec["date_to"]), (spec["compare_from"], spec["compare_to"]))
    ]
    first, second = ({key: (name, amount, color) for key, name, amount, color in _category_rows(person, report)}
                     for report in reports)
    keys = [key for key in first if first[key][1] or second.get(key, (None, 0))[1]]
    keys += [key for key in second if key not in first and second[key][1]]
    rows = [
        (key, (first.get(key) or second[key])[0],
         (first.get(key, (None, 0))[1], second.get(key, (None, 0))[1]), (first.get(key) or second[key])[2])
        for key in keys
    ]
    rows.sort(key=lambda row: (-row[2][0], -row[2][1], row[1]))
    rows = _top_with_other(rows, 2)
    labels = (_range_text(spec["date_from"], spec["date_to"]), _range_text(spec["compare_from"], spec["compare_to"]))
    query = _range_query(spec["date_from"], spec["date_to"], account=spec["account"], scope=spec["scope"])
    return {
        "title": f"Spending by category, {labels[0]} against {labels[1]}",
        "labels": [row[1] for row in rows],
        "series": [
            _series(labels[0], [row[2][0] for row in rows], "primary"),
            _series(labels[1], [row[2][1] for row in rows], "secondary"),
        ],
        "page_url": f"{reverse('spending-by-category')}?{urlencode(query)}",
        "account_ids": tuple(sorted({item.pk for report in reports for item in report.accounts}))
        if spec["account"] is None else (spec["account"].pk,),
    }


def _budget_vs_actual(person, spec):
    """The Budgets page's cards for the month holding date_from, as list_budgets reads them."""
    accounts, include_household = _ai_scope(person)
    month = spec["date_from"].replace(day=1)
    cards = month_budget_cards(person, month, accounts=accounts, include_household=include_household)
    if spec["scope"]:
        cards = [card for card in cards if card.budget.scope == spec["scope"]]
    withheld = withheld_category_ids(person)
    rows = [
        (card.budget.pk,
         WITHHELD_CATEGORY_NAME if card.budget.category_id in withheld else card.name,
         (card.amount_minor, card.spent_minor), None)
        for card in cards
    ]
    rows = _top_with_other(rows, 2)
    return {
        "title": f"Budget against spending, {month.strftime('%B %Y')}",
        "labels": [row[1] for row in rows],
        "series": [
            _series("Budget", [row[2][0] for row in rows], "primary"),
            _series("Spent", [row[2][1] for row in rows], "spending"),
        ],
        "page_url": f"{reverse('budgets')}?{urlencode({'month': month.isoformat()[:7]})}",
        "account_ids": _account_ids(None, accounts),
        "budget_ids": tuple(card.budget.pk for card in cards),
    }


BUILDERS = {
    OVER_TIME: _over_time,
    CATEGORY_BREAKDOWN: _category_breakdown,
    BUDGET_VS_ACTUAL: _budget_vs_actual,
    COMPARE_PERIODS: _compare_periods,
}


def _summary(chart):
    """A plain sentence for the canvas's accessible name; the table beside it holds every value."""
    if not chart["labels"]:
        return f"{chart['title']}: no data in this range."
    parts = []
    for series in chart["series"]:
        values = series["values"]
        high = max(range(len(values)), key=lambda index: values[index])
        low = min(range(len(values)), key=lambda index: values[index])
        parts.append(
            f"{series['name']}: highest {chart['labels'][high]} at {series['displays'][high]}, "
            f"lowest {chart['labels'][low]} at {series['displays'][low]}"
        )
    return f"{chart['title']}. " + "; ".join(parts) + "."
