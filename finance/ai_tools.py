"""Read-only finance tools for AI sessions. Each call uses visible_to at run time."""

from __future__ import annotations

import json
from datetime import date, datetime
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from .ai_types import ToolResult, ToolSpec
from .budget_services import month_budget_cards, parse_month
from .cash_flow import (
    GROUPING_MONTH,
    cash_flow_report,
    default_date_range,
    format_minor,
    spending_by_category_report,
)
from .category_services import current_household
from .models import Account, Category, PlannedItem, RecurringSeries, RecurringSeriesMember, Transaction
from .net_worth import net_worth_report
from .planning_services import confine_recurring_series_to_accounts, projected_months_for
from .policy_services import household_ai_allowed, may_use_ai

MAX_TOOL_ROWS = 50
INSTRUCTION_CONTEXT = (
    "Answer only from tool results for this member. Every figure must come from a tool. "
    "Link numbers to the page URLs the tools return. Label suggestions as opinion, never "
    "as financial advice, and never present them as verified facts. You cannot change data: "
    "to suggest a change use a propose_* tool, which only shows the member a card they may "
    "Apply or Dismiss; never say a change was made. Refuse requests to run SQL or talk about "
    "anyone else's private accounts."
)


def default_tools():
    return (
        ToolSpec(
            name="list_accounts",
            description="List accounts the member can already see in the app.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=list_accounts,
        ),
        ToolSpec(
            name="list_transactions",
            description="List recent visible transactions. Optional account_id limits to one visible account.",
            parameters={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            handler=list_transactions,
        ),
        ToolSpec(
            name="cash_flow_totals",
            description="Income, spending, and net cash flow for a date range the member can see.",
            parameters={
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "account_id": {"type": "integer"},
                    "account_name": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": [],
            },
            handler=cash_flow_totals,
        ),
        ToolSpec(
            name="spending_by_category",
            description="Spending totals by category for a date range.",
            parameters={
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "category": {"type": "string"},
                    "account_id": {"type": "integer"},
                    "scope": {"type": "string"},
                },
                "required": [],
            },
            handler=spending_by_category,
        ),
        ToolSpec(
            name="search_transactions",
            description="Search visible transactions by date range, description, category, or account.",
            parameters={
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "q": {"type": "string"},
                    "category": {"type": "string"},
                    "account_id": {"type": "integer"},
                    "account_name": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            handler=search_transactions,
        ),
        ToolSpec(
            name="recurring_series",
            description="Confirmed recurring charges visible to this member.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=recurring_series,
        ),
        ToolSpec(
            name="net_worth_series",
            description="Monthly net worth totals for a date range.",
            parameters={
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": [],
            },
            handler=net_worth_series,
        ),
        ToolSpec(
            name="list_budgets",
            description="Budget amounts and spending for a calendar month.",
            parameters={
                "type": "object",
                "properties": {"month": {"type": "string"}},
                "required": [],
            },
            handler=list_budgets,
        ),
        ToolSpec(
            name="projected_cash_flow",
            description="Planned items and projected months after today.",
            parameters={
                "type": "object",
                "properties": {"horizon": {"type": "integer"}},
                "required": [],
            },
            handler=projected_cash_flow_tool,
        ),
    )


def run_tool(person, tools, name, args):
    # Re-check on every call: a material policy version can be published mid-conversation.
    if not may_use_ai(person):
        return ToolResult(text="AI is off until the current privacy and data policy is accepted.", ok=False)
    spec = next((item for item in tools if item.name == name), None)
    if spec is None:
        return ToolResult(text="Unknown tool.", ok=False)
    try:
        output = spec.handler(person, args or {})
        if isinstance(output, ToolResult):
            return output
        return ToolResult(text=str(output), ok=True)
    except Exception:
        return ToolResult(text="That tool could not run.", ok=False)


def visible_accounts(person):
    query = Account.objects.visible_to(person).filter(status=Account.Status.ACTIVE)
    household = current_household(person)
    if household is not None and not household_ai_allowed(household):
        query = query.filter(scope=Account.Scope.PRIVATE, owner=person)
    return query


def _ai_scope(person):
    household = current_household(person)
    include_household = household is None or household_ai_allowed(household)
    return visible_accounts(person), include_household


def list_accounts(person, args):
    rows = []
    for account in visible_accounts(person).order_by("name", "pk")[:MAX_TOOL_ROWS]:
        rows.append(
            {
                "id": account.pk,
                "name": account.name,
                "account_type": account.account_type,
                "scope": account.scope,
            }
        )
    return ToolResult(
        text=_format_rows(rows),
        account_ids=tuple(row["id"] for row in rows),
    )


def list_transactions(person, args):
    limit = _limit(args.get("limit"))
    query = Transaction.objects.visible_to(person).filter(
        status=Transaction.Status.ACTIVE,
        account_id__in=visible_accounts(person).values("pk"),
    )
    account_ids = set()
    account_id = args.get("account_id")
    if account_id is not None:
        account, error = _visible_account(person, account_id, name=None)
        if error:
            return ToolResult(text=error, ok=False)
        query = query.filter(account_id=account.pk)
        account_ids.add(account.pk)
    rows = []
    for txn in query.order_by("-transaction_date", "-pk")[:limit]:
        account_ids.add(txn.account_id)
        rows.append(
            {
                "id": txn.pk,
                "account_id": txn.account_id,
                "date": str(txn.transaction_date),
                "amount_minor": txn.amount_minor,
                "currency": txn.currency,
                "description": txn.description,
            }
        )
    return ToolResult(text=_format_rows(rows), account_ids=tuple(sorted(account_ids)))


def cash_flow_totals(person, args):
    date_from, date_to = _dates(args)
    account, error = _visible_account(person, args.get("account_id"), args.get("account_name"))
    if error:
        return ToolResult(text=error, ok=False)
    scope = _scope(args.get("scope"))
    accounts, _include_household = _ai_scope(person)
    report = cash_flow_report(
        person,
        date_from=date_from,
        date_to=date_to,
        grouping=GROUPING_MONTH,
        account=account,
        scope=scope,
        accounts=accounts,
    )
    query = _range_query(date_from, date_to, account=account, scope=scope)
    page_url = f"{reverse('home')}?{urlencode(query)}"
    txn_url = f"{reverse('transaction-list')}?{urlencode(query)}"
    summary = report.summary
    payload = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "income_minor": summary.income_minor,
        "spending_minor": summary.spending_minor,
        "net_minor": summary.net_minor,
        "income_display": summary.income_display,
        "spending_display": summary.spending_display,
        "net_display": summary.net_display,
        "page_url": page_url,
        "transactions_url": txn_url,
    }
    figures = (
        _figure("Income", summary.income_minor, page_url),
        _figure("Spending", summary.spending_minor, txn_url),
        _figure("Net cash flow", summary.net_minor, page_url),
    )
    return ToolResult(
        text=json.dumps(payload),
        figures=figures,
        account_ids=_account_ids(account, report.accounts),
    )


def spending_by_category(person, args):
    date_from, date_to = _dates(args)
    account, error = _visible_account(person, args.get("account_id"), args.get("account_name"))
    if error:
        return ToolResult(text=error, ok=False)
    scope = _scope(args.get("scope"))
    accounts, _include_household = _ai_scope(person)
    report = spending_by_category_report(
        person,
        date_from=date_from,
        date_to=date_to,
        account=account,
        scope=scope,
        accounts=accounts,
    )
    wanted = (args.get("category") or "").strip()
    rows = []
    figures = []
    for row in report.rows:
        if wanted and wanted.lower() not in {row.name.lower(), str(row.key).lower()}:
            continue
        query = _range_query(date_from, date_to, account=account, scope=scope)
        if row.key == "uncategorized":
            query["category"] = "uncategorized"
        elif str(row.key).isdigit():
            query["category"] = str(row.key)
        url = f"{reverse('transaction-list')}?{urlencode(query)}"
        rows.append(
            {
                "name": row.name,
                "spending_minor": row.spending_minor,
                "spending_display": row.spending_display,
                "url": url,
            }
        )
        figures.append(_figure(f"{row.name} spending", row.spending_minor, url))
    page_query = _range_query(date_from, date_to, account=account, scope=scope)
    payload = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "total_spending_minor": report.total_spending_minor,
        "total_spending_display": report.total_spending_display,
        "page_url": f"{reverse('spending-by-category')}?{urlencode(page_query)}",
        "rows": rows[:MAX_TOOL_ROWS],
    }
    figures.insert(0, _figure("Total spending", report.total_spending_minor, payload["page_url"]))
    return ToolResult(
        text=json.dumps(payload),
        figures=tuple(figures[: MAX_TOOL_ROWS + 1]),
        account_ids=_account_ids(account, report.accounts),
    )


def search_transactions(person, args):
    date_from, date_to = _dates(args)
    account, error = _visible_account(person, args.get("account_id"), args.get("account_name"))
    if error:
        return ToolResult(text=error, ok=False)
    query = Transaction.objects.visible_to(person).filter(
        status=Transaction.Status.ACTIVE,
        account_id__in=visible_accounts(person).values("pk"),
        transaction_date__gte=date_from,
        transaction_date__lte=date_to,
    )
    if account is not None:
        query = query.filter(account_id=account.pk)
    needle = (args.get("q") or "").strip()
    if needle:
        query = query.filter(description__icontains=needle)
    category = (args.get("category") or "").strip()
    if category:
        match = Category.objects.visible_to(person).filter(name__iexact=category).first()
        if match is None and category.lower() == "uncategorized":
            query = query.filter(category__isnull=True)
        elif match is None:
            return ToolResult(text="That category is not visible.", ok=False)
        else:
            query = query.filter(category_id=match.pk)
    limit = _limit(args.get("limit"))
    rows = []
    account_ids = set()
    if account is not None:
        account_ids.add(account.pk)
    for txn in query.select_related("account").order_by("-transaction_date", "-pk")[:limit]:
        account_ids.add(txn.account_id)
        rows.append(
            {
                "id": txn.pk,
                "account_id": txn.account_id,
                "date": str(txn.transaction_date),
                "amount_minor": txn.amount_minor,
                "currency": txn.currency,
                "description": txn.description,
            }
        )
    list_query = _range_query(date_from, date_to, account=account)
    if needle:
        list_query["q"] = needle
    if category:
        if category.lower() == "uncategorized":
            list_query["category"] = "uncategorized"
        else:
            match = Category.objects.visible_to(person).filter(name__iexact=category).first()
            if match is not None:
                list_query["category"] = str(match.pk)
    url = f"{reverse('transaction-list')}?{urlencode(list_query)}"
    figures = tuple(
        _figure(row["description"][:80], row["amount_minor"], url, row["currency"]) for row in rows
    )
    payload = {"url": url, "rows": rows}
    return ToolResult(text=json.dumps(payload), figures=figures, account_ids=tuple(sorted(account_ids)))


def recurring_series(person, args):
    accounts, _include_household = _ai_scope(person)
    url = reverse("recurring-review")
    rows = []
    figures = []
    series_query = confine_recurring_series_to_accounts(
        RecurringSeries.objects.visible_to(person).filter(
            status=RecurringSeries.Status.CONFIRMED,
            is_active=True,
            cancelled_at__isnull=True,
        ),
        accounts,
    ).order_by("display_name", "pk")
    shown = list(series_query[:MAX_TOOL_ROWS])
    for series in shown:
        rows.append(
            {
                "id": series.pk,
                "name": series.display_name,
                "cadence": series.cadence,
                "typical_amount_minor": series.typical_amount_minor,
                "typical_amount_display": format_minor(series.typical_amount_minor, series.currency),
                "status": series.status,
                "url": url,
            }
        )
        figures.append(_figure(series.display_name, series.typical_amount_minor, url, series.currency))
    member_ids = tuple(
        RecurringSeriesMember.objects.filter(series__in=shown)
        .values_list("transaction__account_id", flat=True)
        .distinct()
    )
    return ToolResult(
        text=json.dumps({"url": url, "rows": rows}),
        figures=tuple(figures),
        account_ids=member_ids,
    )


def net_worth_series(person, args):
    date_from, date_to = _dates(args)
    scope = _scope(args.get("scope"))
    accounts, _include_household = _ai_scope(person)
    report = net_worth_report(
        person, date_from=date_from, date_to=date_to, scope=scope, accounts=accounts
    )
    query = _range_query(date_from, date_to, scope=scope)
    url = f"{reverse('net-worth')}?{urlencode(query)}"
    periods = []
    figures = [_figure("Net worth", report.summary.net_minor, url)]
    for period in report.periods[-MAX_TOOL_ROWS :]:
        periods.append(
            {
                "label": period.label,
                "net_minor": period.net_minor,
                "net_display": period.net_display,
                "url": url,
            }
        )
    payload = {"page_url": url, "net_minor": report.summary.net_minor, "net_display": report.summary.net_display, "periods": periods}
    return ToolResult(text=json.dumps(payload), figures=tuple(figures), account_ids=_account_ids(None, report.accounts))


def list_budgets(person, args):
    accounts, include_household = _ai_scope(person)
    month = parse_month(args.get("month"))
    cards = month_budget_cards(
        person, month, accounts=accounts, include_household=include_household
    )
    url = f"{reverse('budgets')}?{urlencode({'month': month.isoformat()[:7]})}"
    rows = []
    figures = []
    for card in cards[:MAX_TOOL_ROWS]:
        rows.append(
            {
                "name": card.name,
                "amount_minor": card.amount_minor,
                "spent_minor": card.spent_minor,
                "remaining_minor": card.remaining_minor,
                "amount_display": card.amount_display,
                "spent_display": card.spent_display,
                "url": card.drilldown_url,
            }
        )
        figures.append(_figure(f"{card.name} spent", card.spent_minor, card.drilldown_url))
    return ToolResult(
        text=json.dumps({"month": month.isoformat()[:7], "page_url": url, "rows": rows}),
        figures=tuple(figures),
        account_ids=_account_ids(None, accounts),
    )


def projected_cash_flow_tool(person, args):
    today = timezone.localdate()
    try:
        horizon = int(args.get("horizon") or 12)
    except (TypeError, ValueError):
        horizon = 12
    if horizon not in {3, 6, 12, 24}:
        horizon = 12
    accounts, include_household = _ai_scope(person)
    months = projected_months_for(
        person,
        today=today,
        horizon=horizon,
        accounts=accounts,
        include_household=include_household,
    )
    url = f"{reverse('home')}?{urlencode({'horizon': horizon})}"
    rows = []
    figures = []
    for month in months[:MAX_TOOL_ROWS]:
        rows.append(
            {
                "label": month.label,
                "income_minor": month.income_minor,
                "spending_minor": month.spending_minor,
                "net_minor": month.net_minor,
                "income_display": month.income_display,
                "spending_display": month.spending_display,
                "net_display": month.net_display,
            }
        )
        figures.append(_figure(f"Projected {month.label} net", month.net_minor, url))
    planned_query = PlannedItem.objects.visible_to(person).filter(enabled=True)
    if not include_household:
        planned_query = planned_query.filter(scope=PlannedItem.Scope.PRIVATE, owner=person)
    planned = list(planned_query.order_by("start_date", "pk")[:MAX_TOOL_ROWS])
    planned_rows = [
        {
            "id": item.pk,
            "name": item.name,
            "kind": item.kind,
            "amount_minor": item.amount_minor,
            "url": reverse("planned-items"),
        }
        for item in planned
    ]
    payload = {"horizon": horizon, "page_url": url, "months": rows, "planned_items": planned_rows}
    return ToolResult(
        text=json.dumps(payload),
        figures=tuple(figures),
        account_ids=_account_ids(None, accounts),
    )


def _visible_account(person, account_id, name):
    if account_id is None and not name:
        return None, ""
    query = visible_accounts(person)
    if account_id is not None:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return None, "account_id must be a number."
        account = query.filter(pk=account_id).first()
        if account is None:
            return None, "That account is not visible."
        return account, ""
    account = query.filter(name__iexact=str(name).strip()).first()
    if account is None:
        return None, "That account is not visible."
    return account, ""


def _dates(args):
    today = timezone.localdate()
    default_from, default_to = default_date_range(today)
    return _parse_date(args.get("date_from"), default_from), _parse_date(args.get("date_to"), default_to)


def _parse_date(value, fallback):
    if not value:
        return fallback
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return fallback


def _scope(value):
    text = (value or "").strip()
    if text in {Account.Scope.PRIVATE, Account.Scope.HOUSEHOLD}:
        return text
    return ""


def _range_query(date_from, date_to, account=None, scope=""):
    query = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    if account is not None:
        query["account"] = str(account.pk)
    if scope:
        query["scope"] = scope
    return query


def _figure(label, amount_minor, url, currency="USD"):
    return {
        "label": label,
        "amount_minor": int(amount_minor),
        "currency": currency,
        "amount_display": format_minor(int(amount_minor), currency),
        "url": url,
    }


def _account_ids(account, accounts):
    if account is not None:
        return (account.pk,)
    return tuple(item.pk for item in accounts)


def _limit(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 20
    return max(1, min(parsed, MAX_TOOL_ROWS))


def _format_rows(rows):
    if not rows:
        return "(none)"
    lines = []
    for row in rows:
        parts = [f"{key}={row[key]}" for key in row]
        lines.append("; ".join(parts))
    return "\n".join(lines)
