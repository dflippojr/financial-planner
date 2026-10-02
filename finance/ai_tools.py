"""Read-only finance tools for AI sessions. Each call uses visible_to at run time."""

from __future__ import annotations

from .ai_types import ToolSpec
from .models import Account, Transaction
from .policy_services import household_ai_allowed
from .category_services import current_household

MAX_TOOL_ROWS = 50


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
    )


def run_tool(person, tools, name, args):
    spec = next((item for item in tools if item.name == name), None)
    if spec is None:
        return "Unknown tool.", False
    try:
        output = spec.handler(person, args or {})
        return output, True
    except Exception:
        return "That tool could not run.", False


def visible_accounts(person):
    query = Account.objects.visible_to(person).filter(status=Account.Status.ACTIVE)
    household = current_household(person)
    if household is not None and not household_ai_allowed(household):
        query = query.filter(scope=Account.Scope.PRIVATE, owner=person)
    return query


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
    return _format_rows(rows)


def list_transactions(person, args):
    limit = _limit(args.get("limit"))
    query = Transaction.objects.visible_to(person).filter(
        status=Transaction.Status.ACTIVE,
        account_id__in=visible_accounts(person).values("pk"),
    )
    account_id = args.get("account_id")
    if account_id is not None:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return "account_id must be a number."
        if not visible_accounts(person).filter(pk=account_id).exists():
            return "That account is not visible."
        query = query.filter(account_id=account_id)
    rows = []
    for txn in query.order_by("-transaction_date", "-pk")[:limit]:
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
    return _format_rows(rows)


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
