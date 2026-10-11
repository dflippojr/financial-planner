"""Server-owned tile identities and the small, typed context a bead may send."""

from dataclasses import dataclass
from datetime import date
import re

from .category_services import current_household
from .models import Account, Category
from .policy_services import household_ai_allowed


@dataclass(frozen=True)
class AiTile:
    label: str
    tools: tuple[str, ...]
    filters: tuple[str, ...]
    prompts: tuple[str, ...] = ("Explain this", "What changed?")


RANGE = ("date_from", "date_to", "account_id", "scope")
TILES = {
    "cash-flow-totals": AiTile("Key numbers", ("cash_flow_totals",), RANGE),
    "cash-flow-chart": AiTile("Cash flow by period", ("cash_flow_totals", "projected_cash_flow"), RANGE + ("grouping", "horizon")),
    "cash-flow-months": AiTile("By month", ("cash_flow_totals", "projected_cash_flow"), RANGE + ("grouping", "horizon")),
    "home-budgets": AiTile("Budgets", ("list_budgets", "propose_set_budget"), ("month",)),
    "transactions": AiTile("Transactions", ("search_transactions", "propose_set_category", "propose_add_tags"), RANGE + ("category_id", "all_dates")),
    "transaction-totals": AiTile("Cash flow totals", ("cash_flow_totals",), RANGE + ("all_dates",)),
    "budgets": AiTile("Category budgets", ("list_budgets", "propose_set_budget"), ("month",)),
    "budget-total": AiTile("Monthly total", ("list_budgets", "propose_set_budget"), ("month", "scope")),
    "spending-categories": AiTile("Categories", ("spending_by_category",), RANGE),
    "net-worth-totals": AiTile("Current net worth", ("net_worth_series",), ("date_from", "date_to", "scope")),
    "net-worth-months": AiTile("By month", ("net_worth_series",), ("date_from", "date_to", "scope")),
    "recurring": AiTile("Confirmed", ("recurring_series",), ()),
    "planned-items": AiTile("Your planned items", ("list_planned_items", "projected_cash_flow"), ()),
    "review-totals": AiTile("Income, spending, and net", ("cash_flow_totals",), ("month",)),
    "review-budgets": AiTile("Budgets", ("list_budgets", "propose_set_budget"), ("month",)),
    "review-categories": AiTile("Category changes", ("spending_by_category",), ("month",)),
}


def _iso(value, *, month=False):
    pattern = r"[0-9]{4}-[0-9]{2}" if month else r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        return None
    try:
        date.fromisoformat(value + "-01" if month else value)
    except ValueError:
        return None
    return value


def _visible_id(person, model, value):
    if person is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]{1,19}", str(value)):
        return None
    pk = int(value)
    if not 0 < pk <= 2**63 - 1:
        return None
    query = model.objects.visible_to(person)
    if model is Account:
        query = query.filter(status=Account.Status.ACTIVE, archived_at__isnull=True)
    return pk if query.filter(pk=pk).exists() else None


def _filter_value(person, key, value):
    if key == "all_dates":
        return True if value is True else None
    if key in {"date_from", "date_to", "month"}:
        return _iso(value, month=key == "month")
    if key in {"account_id", "category_id"}:
        return _visible_id(person, Account if key == "account_id" else Category, value)
    if key == "scope":
        return value if value in ("all", "private", "household") else None
    if key == "grouping":
        return value if value in ("week", "month", "quarter", "year") else None
    if key == "horizon":
        return int(value) if str(value) in ("3", "6", "12", "24") else None
    return None


def sanitize_tile(raw, person=None):
    if not isinstance(raw, dict):
        return {}
    kind = raw.get("kind")
    tile = TILES.get(kind) if isinstance(kind, str) else None
    if tile is None:
        return {}
    supplied = raw.get("filters")
    supplied = supplied if isinstance(supplied, dict) else {}
    filters = {}
    for key in tile.filters:
        value = _filter_value(person, key, supplied.get(key))
        if value is not None:
            filters[key] = value
    return {"kind": kind, "filters": filters}


def tile_data_shareable(person, filters):
    """A private-only tile remains usable while household consent is incomplete."""
    account_id = filters.get("account_id")
    if account_id:
        return Account.objects.visible_to(person).filter(pk=account_id, scope=Account.Scope.PRIVATE).exists() or _household_allowed(person)
    return filters.get("scope") == "private" or _household_allowed(person)


def _household_allowed(person):
    household = current_household(person)
    return household is None or household_ai_allowed(household)
