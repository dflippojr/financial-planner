"""Read-only chat lookups: savings goals, debt payoff, what-ifs, rules, balance history (#662).

Every handler runs as the member at call time, with the same visibility and
household-consent scope as the tools in ai_tools: while a household member has
not accepted the AI policy, household rows stay out and a private row never
reaches a household balance or category name through them.
"""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from .ai_tools import (
    MAX_TOOL_ROWS,
    WITHHELD_CATEGORY_NAME,
    _account_ids,
    _ai_scope,
    _figure,
    _parse_date,
    _scope,
    _visible_account,
    ai_nameable_categories,
    household_consent_complete,
    visible_accounts,
)
from .ai_types import ToolResult, ToolSpec
from .category_services import current_household
from .debt_planner import STRATEGIES, STRATEGY_CUSTOM, STRATEGY_MINIMUMS
from .debt_planner_services import build_debt_plan, list_visible_debts
from .models import Account, BalanceSnapshot, PlannedItem, SavingsGoal
from .net_worth import signed_balance_minor
from .planning_services import visible_projection_inputs
from .projection import DEFAULT_HORIZON, HORIZONS, SOURCE_PLANNED, SOURCE_SERIES, project_cash_flow
from .rule_services import list_visible_rules
from .savings_goal_plan import SCOPE_ALL, build_funding_plan, plan_account_ids
from .savings_goal_services import goal_progress
from .scenario import (
    CHANGE_ADD,
    CHANGE_AMOUNT,
    CHANGE_ONEOFF,
    CHANGE_PAUSE,
    MAX_CHANGES,
    QUERY_KEY,
    apply_scenario,
    compare_projected_months,
    describe_change,
    encode_change,
    parse_scenario_tokens,
    visible_scenario_changes,
)

MAX_EXTRA_MINOR = 2**63 - 1
ARCHIVED_HISTORY = (
    "That account is archived, so it has no Balances page to cite; use net_worth_series for the "
    "month-end balances Net worth still shows for it."
)
CONSENT_WITHHELD = "withheld until every household member accepts the AI policy"


def lookup_tools():
    return (
        ToolSpec(
            name="savings_goals",
            description=(
                "Savings goals the member can see: target, saved so far, progress, and the month the "
                "funding plan projects each open goal to be funded."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=savings_goals,
        ),
        ToolSpec(
            name="debt_payoff",
            description=(
                "Loan and credit card payoff plan from the debt planner: balances, APR, minimums, "
                "payoff month per debt and total interest against paying minimums only. Optional "
                "strategy, extra monthly payment in minor units, and the debt account ids to include "
                "(custom_order ranks them for the custom strategy)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "strategy": {"type": "string", "enum": list(STRATEGIES)},
                    "extra_minor": {"type": "integer"},
                    "account_ids": {"type": "array", "items": {"type": "integer"}},
                    "custom_order": {"type": "array", "items": {"type": "integer"}},
                },
                "required": [],
            },
            handler=debt_payoff,
        ),
        ToolSpec(
            name="cash_flow_what_if",
            description=(
                "Projected monthly net cash flow with temporary what-if changes against the baseline "
                "projection. Nothing is saved. Each change is one of: add (new recurring income or "
                "expense: kind, amount_minor, cadence, start_date, optional end_date, name), oneoff "
                "(kind, amount_minor, start_date, name), amount (planned_item_id, new amount_minor), "
                "pause (series_id, pause_from). Amounts are positive minor units."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "horizon": {"type": "integer", "enum": list(HORIZONS)},
                    "scope": {"type": "string"},
                    "changes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": [CHANGE_ADD, CHANGE_ONEOFF, CHANGE_AMOUNT, CHANGE_PAUSE]},
                                "kind": {"type": "string", "enum": ["income", "expense"]},
                                "amount_minor": {"type": "integer"},
                                "cadence": {"type": "string"},
                                "start_date": {"type": "string"},
                                "end_date": {"type": "string"},
                                "name": {"type": "string"},
                                "planned_item_id": {"type": "integer"},
                                "series_id": {"type": "integer"},
                                "pause_from": {"type": "string"},
                            },
                        },
                    },
                },
                "required": [],
            },
            handler=cash_flow_what_if,
        ),
        ToolSpec(
            name="list_rules",
            description=(
                "Categorization rules the member can see: the description text, account and amount "
                "range they match, their action and category, and whether each is enabled and confirmed. "
                "Optional q filters by the description text."
            ),
            parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": []},
            handler=list_rules,
        ),
        ToolSpec(
            name="balance_history",
            description=(
                "One visible account's recorded balances, newest first, with each balance's source "
                "(simplefin, calculated, manual) and note. Active accounts only. Give account_id or account_name; optional "
                "date_from and date_to."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "account_name": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                },
                "required": [],
            },
            handler=balance_history,
        ),
    )


def _allowed_account_ids(person):
    """Accounts whose balances and names may reach the AI backend now, archived ones included."""
    return set(visible_accounts(person, include_archived=True).values_list("pk", flat=True))


def _percent(value):
    return None if value is None else str(value)


# Savings goals


def savings_goals(person, _args):
    today = timezone.localdate()
    consent = household_consent_complete(person)
    allowed = _allowed_account_ids(person)
    seen = set(Account.objects.visible_to(person).values_list("pk", flat=True))  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped
    goals = SavingsGoal.objects.visible_to(person)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as in balance_history.py
    if not consent:
        goals = goals.filter(scope=SavingsGoal.Scope.PRIVATE, owner=person)
    # The Savings goals page's order.
    goals = list(goals.select_related("depends_on").order_by(
        F("priority").asc(nulls_last=True), F("target_date").asc(nulls_last=True), "name", "pk"
    ))
    plan_rows, plan_url, plan_note, plan_accounts = _funding_plan(person, today, goals, consent=consent,
                                                                  allowed=allowed)
    planned_item_ids = ()
    list_url = reverse("savings-goals")
    rows = []
    figures = []
    account_ids = set()
    shown = goals[:MAX_TOOL_ROWS]
    shown_ids = {goal.pk for goal in shown}
    for goal in shown:
        # A private goal linked to a shared account outside the consent scope
        # would quote that account's balance, so its progress is withheld. An
        # account the member cannot see at all falls back to the manual amount
        # inside goal_progress, as on the Savings goals page.
        withheld = goal.linked_account_id in seen and goal.linked_account_id not in allowed
        progress = goal_progress(person, goal, today=today)
        linked = progress.linked_account if not withheld else None
        if linked is not None:
            account_ids.add(linked.pk)
        row = {
            "id": goal.pk,
            "name": goal.name,
            "scope": goal.scope,
            "status": "completed" if progress.completed else ("archived" if progress.archived else "active"),
            "target_amount_minor": goal.target_amount_minor,
            "target_amount_display": progress.target_amount_display,
            "target_date": goal.target_date.isoformat() if goal.target_date else None,
            "priority": goal.priority,
            "depends_on": goal.depends_on.name if goal.depends_on_id in shown_ids else None,
            "linked_account": {"id": linked.pk, "name": linked.name} if linked is not None else None,
            "url": list_url,
        }
        # Dates, rank and percent ride in the labels so every number the row states is cited.
        target_label = f"{goal.name} target"
        if goal.target_date:
            target_label += f" by {goal.target_date.isoformat()}"
        if goal.priority is not None:
            target_label += f", rank {goal.priority}"
        figures.append(_figure(target_label, goal.target_amount_minor, list_url, goal.currency))
        if withheld:
            row["progress"] = CONSENT_WITHHELD
        else:
            row.update({
                "saved_minor": progress.current_amount_minor,
                "saved_display": progress.current_amount_display,
                "saved_source": progress.source,
                "saved_as_of": progress.as_of.isoformat() if progress.as_of else None,
                "remaining_minor": progress.remaining_minor,
                "remaining_display": progress.remaining_display,
                "percent": progress.percent,
                "reached": progress.reached,
                "past_due": progress.past_due,
                "monthly_needed_minor": progress.monthly_needed_minor,
            })
            as_of = f" as of {progress.as_of.isoformat()}" if progress.as_of else ""
            figures.append(_figure(f"{goal.name} saved{as_of} ({progress.percent}% of target)", progress.current_amount_minor,
                                   list_url, goal.currency))
            figures.append(_figure(f"{goal.name} remaining", progress.remaining_minor, list_url, goal.currency))
            if progress.monthly_needed_minor is not None:
                figures.append(
                    _figure(f"{goal.name} needed per month", progress.monthly_needed_minor, list_url, goal.currency)
                )
        plan_row = None if withheld else plan_rows.get(goal.pk)
        if plan_row is not None:
            row["projected_completion"] = plan_row
            # The month is cited from the Savings plan page, which shows it beside what is left to fund.
            figures.append(_figure(f"{goal.name} projected funding: {plan_row['funded_month']}",
                                   progress.remaining_minor, plan_row["url"], goal.currency))
        rows.append(row)
    if plan_rows:
        # Funding months come from the whole projection: the accounts it read and every planned item in its scope.
        account_ids |= plan_accounts & allowed
        planned_item_ids = _plan_planned_item_ids(person, consent)
    payload = {"page_url": list_url, "plan_url": plan_url, "rows": rows, "truncated": len(goals) > MAX_TOOL_ROWS}
    if plan_note:
        payload["plan_note"] = plan_note
    return ToolResult(text=json.dumps(payload), figures=tuple(figures), account_ids=tuple(sorted(account_ids)),
                      planned_item_ids=planned_item_ids,
                      # Every goal listed, and every goal the funding plan filled before them.
                      goal_ids=tuple(sorted(shown_ids | set(plan_rows))))


def _plan_planned_item_ids(person, consent):
    """The enabled planned items the Savings plan's projection reads for this scope."""
    planned = PlannedItem.objects.visible_to(person).filter(enabled=True)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped
    if not consent:
        planned = planned.filter(scope=PlannedItem.Scope.PRIVATE, owner=person)
    return tuple(sorted(planned.values_list("pk", flat=True)))


def _funding_plan(person, today, goals, *, consent, allowed):
    """Each open goal's projected funding month from the Savings plan page.

    Without consent the private plan is used. Goals fill in rank order from one
    surplus, so a household balance behind any goal, or the household's safety
    buffer, would shape every funding month; then the whole plan is withheld.
    """
    scope = SCOPE_ALL if consent else SavingsGoal.Scope.PRIVATE
    query = {"horizon": DEFAULT_HORIZON}
    if scope:
        query["scope"] = scope
    url = f"{reverse('savings-goal-plan')}?{urlencode(query)}"
    if not consent:
        household = current_household(person)
        buffer_set = household is not None and household.savings_buffer_minor is not None
        linked_outside = any(
            goal.linked_account_id is not None and goal.linked_account_id not in allowed for goal in goals
        )
        if buffer_set or linked_outside:
            return {}, url, f"Projected completion is {CONSENT_WITHHELD}.", set()
    plan = build_funding_plan(person, today=today, scope=scope, horizon=DEFAULT_HORIZON)
    rows = {}
    for row in plan.rows:
        if row.already_funded:
            when = "already funded"
        elif row.funded_month is not None:
            when = row.funded_month.isoformat()[:7]
        else:
            when = f"not within {DEFAULT_HORIZON} months"
        rows[row.goal.pk] = {"funded_month": when, "target_check": row.check, "url": url}
    return rows, url, "", plan_account_ids(person, plan)


# Debt payoff


def _id_list(values):
    ids = []
    for raw in values or ():
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _extra_minor(value):
    try:
        extra = int(value or 0)
    except (TypeError, ValueError):
        return None
    if not 0 <= extra <= MAX_EXTRA_MINOR:
        return None
    return extra


def debt_payoff(person, args):
    today = timezone.localdate()
    strategy = args.get("strategy") or STRATEGY_MINIMUMS
    if strategy not in STRATEGIES:
        return ToolResult(text=f"strategy must be one of {', '.join(STRATEGIES)}.", ok=False)
    extra_minor = _extra_minor(args.get("extra_minor"))
    if extra_minor is None:
        return ToolResult(text="extra_minor must be zero or a positive whole number of cents.", ok=False)
    allowed = _allowed_account_ids(person)
    # The planner itself reads every visible debt; the consent scope narrows both
    # what is printed and what is simulated.
    catalog = [row for row in list_visible_debts(person, today=today) if row.account_id in allowed]
    catalog_ids = {row.account_id for row in catalog}
    if args.get("account_ids"):
        include_ids = [account_id for account_id in _id_list(args["account_ids"]) if account_id in catalog_ids]
        if not include_ids:
            return ToolResult(text="None of those debts is visible.", ok=False)
    else:
        include_ids = [row.account_id for row in catalog if row.ready]
    custom_order = [account_id for account_id in _id_list(args.get("custom_order")) if account_id in include_ids]
    custom_order += [account_id for account_id in include_ids if account_id not in custom_order]
    plan = build_debt_plan(
        person, include_ids=include_ids, extra_minor=extra_minor, strategy=strategy,
        custom_order=custom_order, today=today,
    )
    url = _debt_url(include_ids, strategy, extra_minor, custom_order)
    rows = []
    figures = []
    payoff_by_id = {}
    comparison = plan.comparison
    if comparison is not None:
        payoff_by_id = {debt.account_id: debt for debt in comparison.chosen.debts}
    in_scope = [row for row in plan.catalog if row.account_id in allowed]
    for row in in_scope[:MAX_TOOL_ROWS]:
        # Owed and payoff are cited from Debt payoff, which shows them; the APR and
        # minimum from the account's Balances page, which shows its debt terms and
        # balances. With no balance and no minimum nothing carries the APR.
        apr = f" at {row.apr_percent}% APR" if row.apr_percent is not None else ""
        terms_url = reverse("account-balances", args=[row.account_id])
        as_of = f" on {row.snapshot_date.isoformat()}" if row.snapshot_date else ""
        citable_apr = row.apr_percent is not None and (row.owed_minor is not None or row.minimum_payment_minor is not None)
        rows.append({
            "account_id": row.account_id,
            "name": row.name,
            "account_type": row.account_type,
            "owed_minor": row.owed_minor,
            "owed_display": row.owed_display,
            "balance_as_of": row.snapshot_date.isoformat() if row.snapshot_date else None,
            "apr_percent": _percent(row.apr_percent) if citable_apr else None,
            "minimum_payment_minor": row.minimum_payment_minor,
            "ready": row.ready,
            "missing": list(row.missing),
            "included": row.included,
            "payoff": row.payoff_label,
        })
        if row.owed_minor is not None:
            figures.append(_figure(f"{row.name} owed{as_of}, payoff {row.payoff_label}", row.owed_minor, url))
        if row.minimum_payment_minor is not None:
            figures.append(_figure(f"{row.name} minimum payment{apr}", row.minimum_payment_minor, terms_url))
        elif apr and row.owed_minor is not None:
            figures.append(_figure(f"{row.name} balance{as_of}{apr}", row.owed_minor, terms_url))
        if row.account_id in payoff_by_id and payoff_by_id[row.account_id].remaining_minor > 0:
            remaining = payoff_by_id[row.account_id].remaining_minor
            rows[-1]["remaining_at_end_minor"] = remaining
            figures.append(_figure(f"{row.name} still owed at the end of the plan", remaining, url))
    payload = {
        "page_url": url,
        "strategy": strategy,
        "extra_minor": extra_minor,
        "debts": rows,
        "debts_truncated": len(in_scope) > MAX_TOOL_ROWS,
        "plan": None,
    }
    figures.append(_figure("Extra monthly payment", extra_minor, url))
    if comparison is not None:
        chosen = comparison.chosen
        months = chosen.months[:MAX_TOOL_ROWS]
        payload["plan"] = {
            "first_month": plan.start.isoformat()[:7],
            "months_in_plan": len(chosen.months),
            "months_truncated": len(chosen.months) > MAX_TOOL_ROWS,
            "total_interest_minor": chosen.total_interest_minor,
            "minimums_only_interest_minor": comparison.baseline.total_interest_minor,
            "interest_saved_minor": comparison.interest_saved_minor,
            "never_pays_off": chosen.never_pays_off,
            "beyond_limit": chosen.beyond_limit,
            "months": [
                {"label": month.label, "interest_minor": month.interest_minor, "paid_minor": month.paid_minor,
                 "remaining_minor": month.remaining_minor}
                for month in months
            ],
        }
        figures.append(_figure(
            f"Total interest over {len(chosen.months)} months from {plan.start.isoformat()[:7]}",
            chosen.total_interest_minor, url,
        ))
        figures.append(_figure("Interest paying minimums only", comparison.baseline.total_interest_minor, url))
        if comparison.interest_saved_minor is not None:
            figures.append(_figure("Interest saved", comparison.interest_saved_minor, url))
        for month in months:
            figures.extend((
                _figure(f"{month.label} interest", month.interest_minor, url),
                _figure(f"{month.label} paid", month.paid_minor, url),
                _figure(f"{month.label} still owed", month.remaining_minor, url),
            ))
    return ToolResult(
        text=json.dumps(payload),
        figures=tuple(figures),
        # Every in-scope debt, listed or simulated, not only the rows shown.
        account_ids=tuple(sorted(row.account_id for row in in_scope)),
    )


def _debt_url(include_ids, strategy, extra_minor, custom_order):
    """The Debt payoff page with the same choices, so its numbers match."""
    pairs = [("strategy", strategy), ("extra", f"{Decimal(extra_minor) / Decimal(100):.2f}")]
    pairs += [("include", str(account_id)) for account_id in include_ids]
    if strategy == STRATEGY_CUSTOM:
        pairs += [(f"rank_{account_id}", str(rank)) for rank, account_id in enumerate(custom_order, start=1)]
    return f"{reverse('debt-payoff')}?{urlencode(pairs)}"


# What-if scenarios


def _clean(value):
    return str(value or "").replace("|", " ").strip()


def _change_token(change):
    """A change in the Home page's own query-string format, parsed and validated by scenario.py."""
    if not isinstance(change, dict):
        return ""
    kind = _clean(change.get("type"))
    if kind == CHANGE_ADD:
        return "|".join(("a", _clean(change.get("kind")), _clean(change.get("amount_minor")),
                         _clean(change.get("cadence")), _clean(change.get("start_date"))[:10],
                         _clean(change.get("end_date"))[:10], _clean(change.get("name"))))
    if kind == CHANGE_ONEOFF:
        return "|".join(("o", _clean(change.get("kind")), _clean(change.get("amount_minor")),
                         _clean(change.get("start_date"))[:10], _clean(change.get("name"))))
    if kind == CHANGE_AMOUNT:
        return "|".join(("m", _clean(change.get("planned_item_id")), _clean(change.get("amount_minor"))))
    if kind == CHANGE_PAUSE:
        return "|".join(("p", _clean(change.get("series_id")), _clean(change.get("pause_from"))[:10]))
    return ""


def cash_flow_what_if(person, args):
    today = timezone.localdate()
    try:
        horizon = int(args.get("horizon") or DEFAULT_HORIZON)
    except (TypeError, ValueError):
        horizon = DEFAULT_HORIZON
    if horizon not in HORIZONS:
        horizon = DEFAULT_HORIZON
    raw = args.get("changes") or []
    if not isinstance(raw, list):
        return ToolResult(text="changes must be a list.", ok=False)
    accounts, include_household = _ai_scope(person)
    scope = _scope(args.get("scope"))
    if scope == Account.Scope.HOUSEHOLD and not include_household:
        return ToolResult(text=f"The household projection is {CONSENT_WITHHELD}.", ok=False)
    if not scope and not include_household:
        # As the other report links do while consent is incomplete, so Home shows the same projection.
        scope = Account.Scope.PRIVATE
    inputs = visible_projection_inputs(person, scope=scope, accounts=accounts, include_household=include_household)
    if not include_household and _input_keys(inputs) != _input_keys(visible_projection_inputs(person, scope=scope)):
        # Home's private view would count a recurring series that also charges a
        # shared account; its totals could not be cited, and quoting them would
        # reach household charges, so the what-if waits for consent.
        return ToolResult(text=f"This projection includes a recurring series that also charges a shared account, "
                               f"so it is {CONSENT_WITHHELD}.", ok=False)
    requested = raw[:MAX_CHANGES]
    parsed = []
    ignored = []
    for index, change in enumerate(requested):
        found = parse_scenario_tokens({QUERY_KEY: [_change_token(change)]})
        # An amount or pause change only reaches a planned item or series this member's projection holds.
        applied = visible_scenario_changes(found, inputs)
        if applied:
            parsed.append(applied[0])
        else:
            ignored.append(index)
    baseline = project_cash_flow(inputs, today=today, horizon=horizon)
    scenario = project_cash_flow(apply_scenario(inputs, parsed), today=today, horizon=horizon)
    comparison = compare_projected_months(baseline, scenario)
    query = [("horizon", str(horizon))]
    if scope:
        query.append(("scope", scope))
    query += [(QUERY_KEY, encode_change(change)) for change in parsed]
    url = f"{reverse('home')}?{urlencode(query)}"
    planned_by_id = {item.source_id: item for item in inputs if item.source == SOURCE_PLANNED}
    series_by_id = {item.source_id: item for item in inputs if item.source == SOURCE_SERIES}
    changes = []
    figures = []
    for change in parsed:
        summary = describe_change(change, planned_by_id=planned_by_id, series_by_id=series_by_id)
        entry = {"type": change.type, "summary": summary}
        if change.type != CHANGE_PAUSE:
            entry["amount_minor"] = change.amount_minor
            figures.append(_figure(summary[:80], change.amount_minor, url))
        changes.append(entry)
    months = []
    for row in comparison[:MAX_TOOL_ROWS]:
        months.append({
            "label": row.label,
            "baseline_net_minor": row.baseline.net_minor,
            "scenario_net_minor": row.scenario.net_minor,
            "difference_minor": row.difference_minor,
        })
        figures.extend((
            _figure(f"Projected {row.label} net", row.baseline.net_minor, url),
            _figure(f"What-if {row.label} net", row.scenario.net_minor, url),
            _figure(f"What-if {row.label} difference", row.difference_minor, url),
        ))
    total_difference = sum(row.difference_minor for row in comparison)
    figures.append(_figure(f"What-if difference over {horizon} months", total_difference, url))
    payload = {
        "horizon": horizon,
        "page_url": url,
        "changes": changes,
        "ignored_changes": ignored,
        "total_difference_minor": total_difference,
        "months": months,
    }
    if len(raw) > MAX_CHANGES:
        payload["changes_truncated"] = True
    return ToolResult(
        text=json.dumps(payload),
        figures=tuple(figures),
        account_ids=_account_ids(None, accounts),
        planned_item_ids=tuple(sorted(source_id for source_id in planned_by_id if source_id > 0)),
    )


def _input_keys(inputs):
    return sorted((item.source, item.source_id, item.amount_minor, item.cadence, item.start, item.end or item.start)
                  for item in inputs)


# Rules


def list_rules(person, args):
    consent = household_consent_complete(person)
    allowed = _allowed_account_ids(person)
    # Only names the member can see and an AI backend may read now: a household
    # category under incomplete consent, or one from a household the member
    # left (an inactive personal rule), is withheld.
    nameable = set(ai_nameable_categories(person).values_list("pk", flat=True))
    needle = str(args.get("q") or "").strip().lower()
    list_url = reverse("category-rule-list")
    # The Rules page lists nothing for a member outside a household.
    rules = list_visible_rules(person) if current_household(person) is not None else []
    if not consent:
        rules = [rule for rule in rules if rule.owner_household_id is None]
    if needle:
        rules = [rule for rule in rules if needle in rule.description_contains.lower()]
    rows = []
    figures = []
    account_ids = set()
    shown = rules[:MAX_TOOL_ROWS]
    for rule in shown:
        url = reverse("category-rule-detail", args=[rule.pk])
        account = None
        if rule.account_id is not None:
            if rule.account_id in allowed:
                account = {"id": rule.account_id, "name": rule.account.name}
                account_ids.add(rule.account_id)
            else:
                account = {"id": None, "name": "Account not visible to the assistant"}
        category = None
        if rule.category_id is not None:
            category = rule.category.name if rule.category_id in nameable else WITHHELD_CATEGORY_NAME
        rows.append({
            "id": rule.pk,
            "owner": "household" if rule.owner_household_id else "personal",
            "description_contains": rule.description_contains,
            "account": account,
            "min_amount_minor": rule.min_amount_minor,
            "max_amount_minor": rule.max_amount_minor,
            "action": rule.action,
            "category": category,
            "enabled": rule.enabled,
            "confirmed": rule.confirmed_at is not None,
            "inactive": bool(getattr(rule, "inactive", False)),
            "url": url,
        })
        if rule.min_amount_minor is not None:
            figures.append(_figure(f"Rule '{rule.description_contains[:60]}' minimum amount", rule.min_amount_minor, url))
        if rule.max_amount_minor is not None:
            figures.append(_figure(f"Rule '{rule.description_contains[:60]}' maximum amount", rule.max_amount_minor, url))
    # Rows keep the Rules page's order: the order in which rules are tried.
    payload = {"page_url": list_url, "rows": rows, "truncated": len(rules) > MAX_TOOL_ROWS}
    return ToolResult(text=json.dumps(payload), figures=tuple(figures), account_ids=tuple(sorted(account_ids)),
                      rule_ids=tuple(rule.pk for rule in shown))


# Balance history


def balance_history(person, args):
    if args.get("account_id") is None and not args.get("account_name"):
        return ToolResult(text="Give account_id or account_name.", ok=False)
    account, error = _visible_account(person, args.get("account_id"), args.get("account_name"))
    if account is None:
        if not error.startswith("account_id") and _archived_account(
            person, args.get("account_id"), args.get("account_name")
        ) is not None:
            # Only an active account has a Balances page that shows every recorded balance.
            return ToolResult(text=ARCHIVED_HISTORY, ok=False)
        return ToolResult(text=error, ok=False)
    date_from = _parse_date(args.get("date_from"), None)
    date_to = _parse_date(args.get("date_to"), None)
    query = BalanceSnapshot.objects.filter(account=account)
    if date_from:
        query = query.filter(snapshot_date__gte=date_from)
    if date_to:
        query = query.filter(snapshot_date__lte=date_to)
    # The account's Balances page order.
    snapshots = list(query.order_by("-snapshot_date", "-source", "-pk")[: MAX_TOOL_ROWS + 1])
    url = reverse("account-balances", args=[account.pk])
    rows = []
    figures = []
    for snapshot in snapshots[:MAX_TOOL_ROWS]:
        balance = signed_balance_minor(account.account_type, snapshot.source, snapshot.amount_minor)
        rows.append({
            "date": snapshot.snapshot_date.isoformat(),
            "balance_minor": balance,
            "currency": snapshot.currency,
            "source": snapshot.source,
            "note": snapshot.note,
        })
        figures.append(_figure(f"{account.name} {snapshot.snapshot_date.isoformat()} ({snapshot.source})",
                               balance, url, snapshot.currency))
    payload = {
        "account": {"id": account.pk, "name": account.name, "account_type": account.account_type,
                    "status": account.status, "scope": account.scope},
        "is_estimate": account.is_physical_asset(),
        "page_url": url,
        "rows": rows,
        "truncated": len(snapshots) > MAX_TOOL_ROWS,
    }
    return ToolResult(text=json.dumps(payload), figures=tuple(figures), account_ids=(account.pk,))


def _archived_account(person, account_id, name):
    query = visible_accounts(person, include_archived=True).exclude(status=Account.Status.ACTIVE)
    if account_id is not None:
        return query.filter(pk=int(account_id)).first()
    return query.filter(name__iexact=str(name).strip()).first()
