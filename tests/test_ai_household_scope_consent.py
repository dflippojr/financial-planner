"""Household scope before AI consent (#677): every chat tool that takes a scope refuses it.

Until every household member has accepted the AI policy the chat tools see only
the member's private accounts. A tool asked for scope=household then used to
intersect that private set with the household scope and answer with zeros (or
"nothing to chart"), which the model repeated as fact. The class-level check
below walks the chat's own tool registry, so a tool added later that takes a
scope is held to the same rule without anyone remembering to list it here.
"""

import json
from datetime import date

import pytest
from django.test import Client

from finance.ai_tools import HOUSEHOLD_SCOPE_WITHHELD, default_tools, run_tool
from finance.budget_services import save_budget
from finance.chat_charts import FILTERS, chart_tools, make_chart
from finance.models import BalanceSnapshot, Budget, Category
from finance.policy_services import accept_policy, current_policy
from tests.test_ai_household_labels import _household_without_consent
from tests.test_chat import add_txn, checking

pytestmark = pytest.mark.django_db

MARCH = {"date_from": "2026-03-01", "date_to": "2026-03-31"}
SHARED_SPENDING = 9000


def _seed_household_activity():
    """Alpha's household is waiting on beta's consent; its shared account has March activity."""
    alpha, beta, household, _custom, _private = _household_without_consent()
    groceries = Category.objects.get(household=household, name="Groceries")
    shared = checking(alpha, household, "Synthetic Shared Checking")
    add_txn(shared, alpha, date(2026, 3, 6), -SHARED_SPENDING, "SYN-MARKET shared groceries", groceries)
    add_txn(shared, alpha, date(2026, 2, 6), -3100, "SYN-MARKET shared groceries", groceries)
    save_budget(alpha, {"scope": Budget.Scope.HOUSEHOLD, "category": groceries, "amount_minor": 15000,
                        "effective_month": date(2026, 3, 1), "rollover_enabled": False})
    BalanceSnapshot.objects.create(account=shared, snapshot_date=date(2026, 3, 31), amount_minor=1234567,
                                   currency="USD", source=BalanceSnapshot.Source.MANUAL)
    return alpha, beta


def _chart_args():
    """Every chart kind and report that takes a scope, with the filters it needs to draw."""
    needs = {
        "category": {"category": "Groceries"},
        "merchant": {"description_contains": "SYN-MARKET"},
    }
    calls = []
    for (kind, report), allowed in sorted(FILTERS.items()):
        if "scope" not in allowed:
            continue
        args = {"kind": kind, "report": report, "scope": "household", **MARCH, **needs.get(report, {})}
        if kind == "compare_periods":
            args.update(compare_from="2026-02-01", compare_to="2026-02-28")
        calls.append((f"make_chart:{kind}:{report}", "make_chart", args))
    return calls


def _scoped_tool_calls():
    """(id, tool name, args) for every chat tool whose parameters take a scope."""
    calls = []
    for spec in default_tools() + chart_tools():
        if "scope" not in spec.parameters.get("properties", {}):
            continue
        if spec.name == "make_chart":
            calls.extend(_chart_args())
        else:
            calls.append((spec.name, spec.name, {"scope": "household", **MARCH}))
    return calls


SCOPED_CALLS = _scoped_tool_calls()


def test_the_registry_walk_finds_the_scoped_tools_this_issue_names():
    names = {name for _id, name, _args in SCOPED_CALLS}
    assert {"cash_flow_totals", "make_chart", "spending_by_category", "search_transactions"} <= names
    chart_kinds = {args["kind"] for _id, name, args in SCOPED_CALLS if name == "make_chart"}
    assert chart_kinds == {"over_time", "category_breakdown", "budget_vs_actual", "compare_periods"}


@pytest.mark.parametrize(("name", "args"), [call[1:] for call in SCOPED_CALLS], ids=[call[0] for call in SCOPED_CALLS])
def test_every_scoped_chat_tool_refuses_household_scope_until_consent_is_complete(name, args):
    alpha, beta = _seed_household_activity()
    tools = default_tools() + chart_tools()

    refused = run_tool(alpha, tools, name, args)
    assert not refused.ok, refused.text
    assert "AI policy" in refused.text
    # No household figure reaches the model: no numbers, no figures, no chart, no rows quoted.
    assert not refused.figures and not refused.charts
    assert not refused.account_ids and not refused.budget_ids and not refused.planned_item_ids
    assert str(SHARED_SPENDING) not in refused.text and "90.00" not in refused.text

    # The same call answers once consent is complete, so the refusal above is about consent alone.
    accept_policy(beta, current_policy())
    assert run_tool(alpha, default_tools() + chart_tools(), name, args).ok


def test_household_chart_without_consent_is_refused_not_drawn_as_zero():
    """The reproduction from #677, as filed: a drawn chart must equal the page it links to."""
    alpha, _beta, household, _custom, _private = _household_without_consent()
    shared = checking(alpha, household, "Synthetic Shared Checking")
    add_txn(shared, alpha, date(2026, 3, 6), -9000, "Synthetic shared groceries")
    result = make_chart(alpha, {"kind": "over_time", "report": "spending", "scope": "household", **MARCH})
    if result.ok:
        (chart,) = result.charts
        client = Client()
        client.force_login(alpha.user)
        rows = client.get(chart["page_url"]).context["chart_data"]["periods"]
        page_total = sum(row["spending_minor"] for row in rows if not row.get("projected"))
        drawn_total = sum(sum(series["values"]) for series in chart["series"])
        assert drawn_total == page_total
    assert not result.ok and result.text == HOUSEHOLD_SCOPE_WITHHELD


def test_private_and_unscoped_calls_still_answer_from_private_accounts_before_consent():
    alpha, _beta = _seed_household_activity()
    for scope in ("private", None):
        args = {**MARCH, **({"scope": scope} if scope else {})}
        totals = run_tool(alpha, default_tools(), "cash_flow_totals", args)
        assert totals.ok and json.loads(totals.text)["spending_minor"] == 4200
        chart = make_chart(alpha, {"kind": "over_time", "report": "spending", **args})
        assert chart.ok and chart.charts[0]["series"][0]["values"] == [4200]
