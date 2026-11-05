"""Chat lookups for savings goals, debt payoff and what-ifs, rules, and balance history (#662)."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from tests.chat_helpers import ask
from tests.test_chat import TOKEN, add_txn, checking, harness, make_member  # noqa: F401 - harness is a fixture

from finance.ai_lookup_tools import lookup_tools
from finance.ai_services import connect_harness
from finance.ai_tools import MAX_TOOL_ROWS, WITHHELD_CATEGORY_NAME, default_tools, run_tool
from finance.category_services import ensure_household_categories
from finance.debt_planner_services import build_debt_plan
from finance.lifecycle_services import update_debt_terms
from finance.models import (
    Account,
    AiConversationMessage,
    BalanceSnapshot,
    Category,
    CategoryRule,
    PlannedItem,
    RecurringSeries,
    RecurringSeriesMember,
    SavingsGoal,
)
from finance.monthly_review_ai import paragraph_is_grounded
from finance.money_display import format_minor
from finance.months import add_months
from finance.policy_services import accept_policy, current_policy

LOOKUPS = ("savings_goals", "debt_payoff", "cash_flow_what_if", "list_rules", "balance_history")
SHARED_BALANCE = 7_654_321
LOAN_OWED = 3_333_300
BETA_CARD_OWED = 9_988_776
HOUSEHOLD_MARKERS = (
    "HH-SYN-SHARED",
    "HH-SYN-GOAL",
    "HH-SYN-RULE",
    "HH-SYN-LOAN",
    "HH-SYN-PLANNED",
    "HH-SYN-CATEGORY",
    str(SHARED_BALANCE),
    str(LOAN_OWED),
)
BETA_MARKERS = ("B-PRIV-CARD", "B-PRIV-GOAL", "B-PRIV-RULE", "B-PRIV-PLANNED", str(BETA_CARD_OWED))


def _snapshot(account, day, amount, source=BalanceSnapshot.Source.MANUAL, note=""):
    return BalanceSnapshot.objects.create(
        account=account, snapshot_date=day, amount_minor=amount, currency="USD", source=source, note=note
    )


def _card(owner, household, name, owed, *, account_type=Account.Type.CREDIT_CARD, shared=False):
    account = Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD if shared else Account.Scope.PRIVATE,
        household=household if shared else None,
        share_mode=Account.ShareMode.CO_OWNED if shared else "",
    )
    _snapshot(account, timezone.localdate(), owed)
    update_debt_terms(owner.user, account.pk, apr_percent=Decimal("18.000"), minimum_payment_minor=25_000,
                      payment_day=None)
    return account


def _planned(owner, name, amount, *, kind=PlannedItem.Kind.INCOME, household=None):
    return PlannedItem.objects.create(
        owner=owner,
        scope=PlannedItem.Scope.HOUSEHOLD if household else PlannedItem.Scope.PRIVATE,
        household=household,
        name=name,
        kind=kind,
        amount_minor=amount,
        start_date=add_months(timezone.localdate().replace(day=1), 1),
        cadence=PlannedItem.Cadence.MONTHLY,
    )


@pytest.fixture(name="world")
def world_fixture():
    """Alpha and beta share a household; each also keeps private rows the other must never see."""
    user_a, alpha, household = make_member("alpha")
    _user_b, beta, _ = make_member("beta", household=household, policy=current_policy())
    ensure_household_categories(household)
    dining = household.categories.get(name="Dining")
    custom = Category.objects.create(household=household, name="HH-SYN-CATEGORY")
    today = timezone.localdate()
    shared = checking(alpha, household, "HH-SYN-SHARED")
    _snapshot(shared, today - timedelta(days=3), SHARED_BALANCE, BalanceSnapshot.Source.SIMPLEFIN)
    private = checking(alpha, household, "A-SYN-CHECKING", private=True)
    _snapshot(private, today - timedelta(days=40), 120_000, BalanceSnapshot.Source.CALCULATED)
    _snapshot(private, today - timedelta(days=2), 150_000, note="A-SYN-NOTE estimate from statement")
    card = _card(alpha, household, "A-SYN-CARD", 400_000)
    loan = _card(alpha, household, "HH-SYN-LOAN", LOAN_OWED, account_type=Account.Type.LOAN, shared=True)
    beta_card = _card(beta, None, "B-PRIV-CARD", BETA_CARD_OWED)
    goal = SavingsGoal.objects.create(owner=alpha, name="A-SYN-GOAL", target_amount_minor=500_000,
                                      manual_amount_minor=100_000, manual_amount_date=today, priority=1)
    linked_goal = SavingsGoal.objects.create(owner=alpha, name="A-SYN-LINKED-GOAL", target_amount_minor=9_000_000,
                                             linked_account=shared)
    hh_goal = SavingsGoal.objects.create(owner=alpha, scope=SavingsGoal.Scope.HOUSEHOLD, household=household,
                                         name="HH-SYN-GOAL", target_amount_minor=200_000)
    SavingsGoal.objects.create(owner=beta, name="B-PRIV-GOAL", target_amount_minor=123_000)
    own_rule = CategoryRule.objects.create(owner_person=alpha, description_contains="A-SYN-RULE coffee",
                                           category=custom, min_amount_minor=-5_000, max_amount_minor=-100,
                                           account=shared)
    hh_rule = CategoryRule.objects.create(owner_household=household, description_contains="HH-SYN-RULE rent",
                                          category=dining, confirmed_at=timezone.now())
    CategoryRule.objects.create(owner_person=beta, description_contains="B-PRIV-RULE pharmacy", category=dining)
    income = _planned(alpha, "A-SYN-PLANNED salary", 900_000)
    hh_planned = _planned(alpha, "HH-SYN-PLANNED daycare", 70_000, kind=PlannedItem.Kind.EXPENSE, household=household)
    beta_planned = _planned(beta, "B-PRIV-PLANNED bonus", 55_000)
    return {
        "user_a": user_a, "alpha": alpha, "beta": beta, "household": household, "shared": shared,
        "private": private, "card": card, "loan": loan, "beta_card": beta_card, "goal": goal,
        "linked_goal": linked_goal, "hh_goal": hh_goal, "own_rule": own_rule, "hh_rule": hh_rule,
        "income": income, "hh_planned": hh_planned, "beta_planned": beta_planned,
    }


def _withhold_consent(world):
    world["beta"].privacy_policy_acceptances.all().delete()


def _client(world):
    client = Client()
    client.force_login(world["user_a"])
    return client


def _args(world):
    first = add_months(timezone.localdate().replace(day=1), 1)
    return {
        "savings_goals": {},
        "debt_payoff": {"strategy": "avalanche", "extra_minor": 10_000},
        "cash_flow_what_if": {"horizon": 3, "changes": [
            {"type": "add", "kind": "expense", "amount_minor": 12_345, "cadence": "monthly",
             "start_date": first.isoformat(), "name": "SYN what-if gym"},
        ]},
        "list_rules": {},
        "balance_history": {"account_id": world["private"].pk},
    }


def _blob(result):
    return "\n".join((result.text, json.dumps(result.figures), str(result.account_ids)))


def _money_values(node, key=""):
    if isinstance(node, dict):
        for child, value in node.items():
            yield from _money_values(value, str(child))
    elif isinstance(node, list):
        for item in node:
            yield from _money_values(item, key)
    elif isinstance(node, int) and not isinstance(node, bool) and key.endswith("_minor"):
        yield node


def _rate_values(node, key=""):
    if isinstance(node, dict):
        for child, value in node.items():
            yield from _rate_values(value, str(child))
    elif isinstance(node, list):
        for item in node:
            yield from _rate_values(item, key)
    elif key in {"percent", "apr_percent"} and node is not None:
        yield node


def assert_grounded(result):
    """Every amount and rate a tool returns can be cited back to a figure link (the monthly review's grounding rule)."""
    assert result.ok, result.text
    payload = json.loads(result.text)
    amounts = list(_money_values(payload))
    stated = " ".join(f"{amount / 100:.2f}" for amount in amounts)
    assert paragraph_is_grounded(stated, list(result.figures)), stated
    rates = " ".join(f"{rate}%" for rate in _rate_values(payload))
    assert paragraph_is_grounded(rates, list(result.figures)), rates
    cited = {figure["amount_minor"] for figure in result.figures}
    assert set(amounts) <= cited
    for figure in result.figures:
        assert figure["url"].startswith("/") and not figure["url"].startswith("//")
        assert figure["amount_display"] == format_minor(figure["amount_minor"], figure["currency"])
    return payload


@pytest.mark.django_db
def test_lookup_tools_are_registered_for_chat():
    names = [tool.name for tool in default_tools()]
    assert set(LOOKUPS) <= set(names)
    assert len(names) == len(set(names))
    assert [tool.name for tool in lookup_tools()] == list(LOOKUPS)


@pytest.mark.django_db
@pytest.mark.parametrize("name", LOOKUPS)
def test_each_lookup_is_grounded_read_only_and_never_shows_another_members_private_rows(world, name):
    with CaptureQueriesContext(connection) as queries:
        result = run_tool(world["alpha"], default_tools(), name, _args(world)[name])
    assert_grounded(result)
    writes = [q["sql"] for q in queries.captured_queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes == []
    blob = _blob(result)
    for marker in BETA_MARKERS:
        assert marker not in blob, f"{name} leaked {marker}"
    assert world["beta_card"].pk not in result.account_ids


@pytest.mark.django_db
@pytest.mark.parametrize("name", LOOKUPS)
def test_each_lookup_holds_household_rows_until_every_member_accepts(world, name):
    _withhold_consent(world)
    args = _args(world)[name]
    held = run_tool(world["alpha"], default_tools(), name, args)
    assert_grounded(held)
    blob = _blob(held)
    for marker in HOUSEHOLD_MARKERS:
        assert marker not in blob, f"{name} leaked {marker} while a housemate has not accepted"
    assert world["shared"].pk not in held.account_ids
    assert world["loan"].pk not in held.account_ids
    assert world["hh_planned"].pk not in held.planned_item_ids
    if name == "balance_history":
        refused = run_tool(world["alpha"], default_tools(), name, {"account_name": "HH-SYN-SHARED"})
        assert not refused.ok and str(SHARED_BALANCE) not in refused.text

    accept_policy(world["beta"], current_policy())
    if name == "balance_history":
        args = {"account_name": "HH-SYN-SHARED"}
    released = run_tool(world["alpha"], default_tools(), name, args)
    assert_grounded(released)
    if name == "cash_flow_what_if":
        # The household planned item now shapes the projection, though only its effect is quoted.
        assert world["hh_planned"].pk in released.planned_item_ids
        assert held.text != released.text
    else:
        assert any(marker in _blob(released) for marker in HOUSEHOLD_MARKERS), f"{name} missing household data"


@pytest.mark.django_db
def test_savings_goals_match_the_goals_and_plan_pages(world):
    result = run_tool(world["alpha"], default_tools(), "savings_goals", {})
    payload = assert_grounded(result)
    rows = {row["name"]: row for row in payload["rows"]}
    assert list(rows)[0] == "A-SYN-GOAL"  # ranked first, as on the page
    goal = rows["A-SYN-GOAL"]
    assert (goal["target_amount_minor"], goal["saved_minor"], goal["remaining_minor"], goal["percent"]) == (
        500_000, 100_000, 400_000, 20)
    assert goal["saved_source"] == "manual"
    linked = rows["A-SYN-LINKED-GOAL"]
    assert linked["saved_minor"] == SHARED_BALANCE and linked["linked_account"]["name"] == "HH-SYN-SHARED"
    assert world["shared"].pk in result.account_ids
    client = _client(world)
    page = client.get(payload["page_url"])
    assert page.status_code == 200
    cards = {card.goal.pk: card for card in page.context["cards"]}
    for row in payload["rows"]:
        assert cards[row["id"]].current_amount_minor == row["saved_minor"]
        assert cards[row["id"]].remaining_minor == row["remaining_minor"]
    plan = client.get(payload["plan_url"])
    assert plan.status_code == 200
    funded = {row.goal.pk: row for row in plan.context["plan"].rows}
    for row in payload["rows"]:
        expected = funded[row["id"]]
        completion = row["projected_completion"]["funded_month"]
        # The month is cited, linked to the plan page that shows it.
        assert paragraph_is_grounded(f"{row['name']} is funded by {completion}.", list(result.figures))
        cite = next(f for f in result.figures if f["label"] == f"{row['name']} projected funding: {completion}")
        assert cite["url"] == payload["plan_url"] and cite["amount_minor"] == expected.remaining_minor
        if expected.funded_month is not None:
            assert completion == expected.funded_month.isoformat()[:7]
        else:
            assert not completion[:4].isdigit()


@pytest.mark.django_db
def test_private_goal_linked_to_a_shared_account_withholds_progress_without_consent(world):
    _withhold_consent(world)
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "savings_goals", {}))
    rows = {row["name"]: row for row in payload["rows"]}
    assert set(rows) == {"A-SYN-GOAL", "A-SYN-LINKED-GOAL"}
    linked = rows["A-SYN-LINKED-GOAL"]
    assert "saved_minor" not in linked and "projected_completion" not in linked
    assert linked["linked_account"] is None
    assert "withheld" in linked["progress"]
    # Goals fill from one surplus, so the shared balance behind one goal would move the others' months.
    assert "withheld" in payload["plan_note"]
    assert all("projected_completion" not in row for row in payload["rows"])

    world["linked_goal"].delete()
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "savings_goals", {}))
    assert "plan_note" not in payload
    goal = payload["rows"][0]
    assert goal["projected_completion"]["url"].endswith("scope=private")
    plan = _client(world).get(goal["projected_completion"]["url"])
    funded = plan.context["plan"].rows[0]
    assert funded.goal.pk == goal["id"]
    assert goal["projected_completion"]["funded_month"] == funded.funded_month.isoformat()[:7]


@pytest.mark.django_db
def test_savings_goals_record_what_the_funding_plan_read(world):
    result = run_tool(world["alpha"], default_tools(), "savings_goals", {})
    assert {world["income"].pk, world["hh_planned"].pk} <= set(result.planned_item_ids)
    assert world["beta_planned"].pk not in result.planned_item_ids
    assert {world["card"].pk, world["loan"].pk, world["private"].pk} <= set(result.account_ids)

    world["linked_goal"].delete()
    _withhold_consent(world)
    held = run_tool(world["alpha"], default_tools(), "savings_goals", {})
    assert held.planned_item_ids == (world["income"].pk,)
    assert world["shared"].pk not in held.account_ids and world["card"].pk in held.account_ids


@pytest.mark.django_db
def test_goal_linked_to_an_account_the_member_cannot_see_falls_back_like_the_page(world):
    # Beta can see the household goal but not alpha's private account it is linked to.
    hh_goal = world["hh_goal"]
    hh_goal.linked_account = world["private"]
    hh_goal.manual_amount_minor = 31_400
    hh_goal.manual_amount_date = timezone.localdate()
    hh_goal.save(update_fields=["linked_account", "manual_amount_minor", "manual_amount_date"])
    result = run_tool(world["beta"], default_tools(), "savings_goals", {})
    payload = assert_grounded(result)
    row = next(row for row in payload["rows"] if row["id"] == hh_goal.pk)
    assert row["saved_minor"] == 31_400 and row["saved_source"] == "manual"
    assert row["linked_account"] is None and "progress" not in row
    assert world["private"].pk not in result.account_ids
    assert "150000" not in json.dumps(payload)
    client = Client()
    client.force_login(world["beta"].user)
    cards = {card.goal.pk: card for card in client.get(payload["page_url"]).context["cards"]}
    assert cards[hh_goal.pk].current_amount_minor == row["saved_minor"]


@pytest.mark.django_db
def test_savings_goals_caps_rows(world):
    SavingsGoal.objects.bulk_create(
        SavingsGoal(owner=world["alpha"], name=f"SYN cap goal {index:03}", target_amount_minor=1_000 + index)
        for index in range(MAX_TOOL_ROWS + 5)
    )
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "savings_goals", {}))
    assert len(payload["rows"]) == MAX_TOOL_ROWS and payload["truncated"] is True
    assert payload["rows"][0]["name"] == "A-SYN-GOAL"


@pytest.mark.django_db
def test_goal_plan_is_withheld_when_a_household_buffer_shapes_it_without_consent(world):
    household = world["household"]
    household.savings_buffer_minor = 4_242
    household.save(update_fields=["savings_buffer_minor"])
    _withhold_consent(world)
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "savings_goals", {}))
    assert "withheld" in payload["plan_note"]
    assert all("projected_completion" not in row for row in payload["rows"])
    assert "4242" not in json.dumps(payload)


@pytest.mark.django_db
def test_debt_payoff_matches_the_debt_page_and_validates_choices(world):
    result = run_tool(world["alpha"], default_tools(), "debt_payoff", _args(world)["debt_payoff"])
    payload = assert_grounded(result)
    names = {row["name"] for row in payload["debts"]}
    assert names == {"A-SYN-CARD", "HH-SYN-LOAN"}
    assert set(result.account_ids) == {world["card"].pk, world["loan"].pk}
    assert all(row["included"] and row["apr_percent"] == "18.000" for row in payload["debts"])
    page = _client(world).get(payload["page_url"])
    assert page.status_code == 200
    comparison = page.context["plan"].comparison
    assert comparison.chosen.total_interest_minor == payload["plan"]["total_interest_minor"]
    assert comparison.baseline.total_interest_minor == payload["plan"]["minimums_only_interest_minor"]
    assert comparison.interest_saved_minor == payload["plan"]["interest_saved_minor"]

    only = run_tool(world["alpha"], default_tools(), "debt_payoff",
                    {"account_ids": [world["card"].pk, world["beta_card"].pk], "strategy": "custom",
                     "custom_order": [world["card"].pk]})
    only_payload = assert_grounded(only)
    included = {row["name"] for row in only_payload["debts"] if row["included"]}
    assert included == {"A-SYN-CARD"}
    assert f"rank_{world['card'].pk}=1" in only_payload["page_url"]
    assert not run_tool(world["alpha"], default_tools(), "debt_payoff", {"account_ids": [world["beta_card"].pk]}).ok
    assert not run_tool(world["alpha"], default_tools(), "debt_payoff", {"strategy": "lottery"}).ok
    assert not run_tool(world["alpha"], default_tools(), "debt_payoff", {"extra_minor": -1}).ok


@pytest.mark.django_db
def test_debt_payoff_simulates_only_debts_in_the_consent_scope(world):
    _withhold_consent(world)
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "debt_payoff", {}))
    alone = build_debt_plan(world["alpha"], include_ids=[world["card"].pk], extra_minor=0, strategy="minimums",
                            custom_order=[], today=timezone.localdate())
    assert payload["plan"]["total_interest_minor"] == alone.comparison.chosen.total_interest_minor
    assert f"include={world['loan'].pk}" not in payload["page_url"]
    assert not run_tool(world["alpha"], default_tools(), "debt_payoff", {"account_ids": [world["loan"].pk]}).ok


@pytest.mark.django_db
def test_debt_payoff_flags_a_capped_catalog_and_records_every_simulated_debt(world):
    today = timezone.localdate()
    extra = Account.objects.bulk_create(
        Account(name=f"SYN cap card {index:03}", account_type=Account.Type.CREDIT_CARD, owner=world["alpha"],
                scope=Account.Scope.PRIVATE, apr_percent=Decimal("5.000"), minimum_payment_minor=1_000)
        for index in range(MAX_TOOL_ROWS + 2)
    )
    BalanceSnapshot.objects.bulk_create(
        BalanceSnapshot(account=account, snapshot_date=today, amount_minor=500, currency="USD",
                        source=BalanceSnapshot.Source.MANUAL)
        for account in extra
    )
    result = run_tool(world["alpha"], default_tools(), "debt_payoff", {})
    payload = assert_grounded(result)
    assert len(payload["debts"]) == MAX_TOOL_ROWS and payload["debts_truncated"] is True
    assert {account.pk for account in extra} <= set(result.account_ids)


@pytest.mark.django_db
def test_debt_apr_is_cited_without_a_balance_and_withheld_without_any_amount(world):
    no_balance = Account.objects.create(name="A-SYN-NO-BALANCE", account_type=Account.Type.CREDIT_CARD,
                                        owner=world["alpha"], apr_percent=Decimal("18.375"),
                                        minimum_payment_minor=4_000)
    bare = Account.objects.create(name="A-SYN-BARE", account_type=Account.Type.CREDIT_CARD, owner=world["alpha"],
                                  apr_percent=Decimal("21.125"))
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "debt_payoff", {}))
    rows = {row["account_id"]: row for row in payload["debts"]}
    assert rows[no_balance.pk]["apr_percent"] == "18.375" and not rows[no_balance.pk]["ready"]
    assert rows[bare.pk]["apr_percent"] is None and "21.125" not in json.dumps(payload)


@pytest.mark.django_db
def test_debt_payoff_caps_its_month_rows(world):
    # Minimums barely above the interest stretch the plan far past MAX_TOOL_ROWS months.
    update_debt_terms(world["user_a"], world["card"].pk, apr_percent=Decimal("1.000"), minimum_payment_minor=2_000,
                      payment_day=None)
    result = run_tool(world["alpha"], default_tools(), "debt_payoff", {"account_ids": [world["card"].pk]})
    payload = assert_grounded(result)
    assert payload["plan"]["months_in_plan"] > MAX_TOOL_ROWS
    assert payload["plan"]["months_truncated"] is True
    assert len(payload["plan"]["months"]) == MAX_TOOL_ROWS


@pytest.mark.django_db
def test_what_if_matches_the_home_scenario_and_ignores_rows_outside_scope(world):
    args = _args(world)["cash_flow_what_if"]
    args["changes"] = [
        *args["changes"],
        {"type": "amount", "planned_item_id": world["income"].pk, "amount_minor": 800_000},
        {"type": "amount", "planned_item_id": world["beta_planned"].pk, "amount_minor": 1},
        {"type": "add", "kind": "expense", "amount_minor": -5, "cadence": "monthly", "start_date": "bad", "name": "x"},
        "not a change",
        {"type": "pause", "series_id": 999_999, "pause_from": timezone.localdate().isoformat()},
    ]
    result = run_tool(world["alpha"], default_tools(), "cash_flow_what_if", args)
    payload = assert_grounded(result)
    assert payload["ignored_changes"] == [2, 3, 4, 5]
    assert len(payload["changes"]) == 2
    assert world["income"].pk in result.planned_item_ids
    assert world["beta_planned"].pk not in result.planned_item_ids
    # Each month: 12,345 more spending and 100,000 less income.
    assert [month["difference_minor"] for month in payload["months"]] == [-112_345] * 3
    assert payload["total_difference_minor"] == -112_345 * 3
    page = _client(world).get(payload["page_url"])
    assert page.status_code == 200
    rows = page.context["scenario_comparison"]
    assert [(row.label, row.baseline.net_minor, row.scenario.net_minor, row.difference_minor) for row in rows] == [
        (m["label"], m["baseline_net_minor"], m["scenario_net_minor"], m["difference_minor"]) for m in payload["months"]
    ]


@pytest.mark.django_db
def test_what_if_pauses_a_recurring_series_like_home(world):
    today = timezone.localdate()
    series = RecurringSeries.objects.create(
        person=world["alpha"], merchant_key="syn streaming", display_name="SYN-STREAMING",
        cadence=RecurringSeries.Cadence.MONTHLY, typical_amount_minor=-1_599,
        status=RecurringSeries.Status.CONFIRMED, confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"], fingerprint="c" * 64,
    )
    for offset in (3, 2, 1):
        day = add_months(today.replace(day=5), -offset)
        txn = add_txn(world["private"], world["alpha"], day, -1_599, "SYN-STREAMING")
        RecurringSeriesMember.objects.create(series=series, transaction=txn)
    first = add_months(today.replace(day=1), 1)
    result = run_tool(world["alpha"], default_tools(), "cash_flow_what_if", {"horizon": 3, "changes": [
        {"type": "pause", "series_id": series.pk, "pause_from": first.isoformat()}]})
    payload = assert_grounded(result)
    assert payload["ignored_changes"] == []
    assert "SYN-STREAMING" in payload["changes"][0]["summary"]
    assert [month["difference_minor"] for month in payload["months"]] == [1_599] * 3
    page = _client(world).get(payload["page_url"])
    assert [(row.baseline.net_minor, row.scenario.net_minor) for row in page.context["scenario_comparison"]] == [
        (m["baseline_net_minor"], m["scenario_net_minor"]) for m in payload["months"]]


@pytest.mark.django_db
def test_what_if_links_the_private_projection_while_consent_is_incomplete(world):
    _withhold_consent(world)
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "cash_flow_what_if",
                                       _args(world)["cash_flow_what_if"]))
    assert "scope=private" in payload["page_url"]
    assert "can differ" in payload["page_note"]
    page = _client(world).get(payload["page_url"])
    assert page.status_code == 200
    assert [row.scenario.net_minor for row in page.context["scenario_comparison"]] == [
        month["scenario_net_minor"] for month in payload["months"]]


@pytest.mark.django_db
def test_what_if_refuses_the_household_projection_while_consent_is_incomplete(world):
    _withhold_consent(world)
    refused = run_tool(world["alpha"], default_tools(), "cash_flow_what_if", {"scope": "household"})
    assert not refused.ok and "withheld" in refused.text
    accept_policy(world["beta"], current_policy())
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "cash_flow_what_if", {"scope": "household"}))
    assert "scope=household" in payload["page_url"]
    assert "page_note" not in payload


@pytest.mark.django_db
def test_what_if_caps_changes_and_rejects_a_non_list(world):
    first = add_months(timezone.localdate().replace(day=1), 1).isoformat()
    many = [{"type": "oneoff", "kind": "income", "amount_minor": 100 + index, "start_date": first,
             "name": f"SYN one-off {index}"} for index in range(30)]
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "cash_flow_what_if", {"changes": many}))
    assert len(payload["changes"]) == 24 and payload["changes_truncated"] is True
    assert not run_tool(world["alpha"], default_tools(), "cash_flow_what_if", {"changes": "a|b"}).ok


@pytest.mark.django_db
def test_list_rules_shows_what_each_rule_matches(world):
    result = run_tool(world["alpha"], default_tools(), "list_rules", {})
    payload = assert_grounded(result)
    rows = {row["description_contains"]: row for row in payload["rows"]}
    assert set(rows) == {"A-SYN-RULE coffee", "HH-SYN-RULE rent"}
    own = rows["A-SYN-RULE coffee"]
    assert own == {**own, "owner": "personal", "account": {"id": world["shared"].pk, "name": "HH-SYN-SHARED"},
                   "min_amount_minor": -5_000, "max_amount_minor": -100, "action": "categorize",
                   "category": "HH-SYN-CATEGORY", "enabled": True, "confirmed": False}
    assert rows["HH-SYN-RULE rent"]["confirmed"] is True and rows["HH-SYN-RULE rent"]["owner"] == "household"
    assert world["shared"].pk in result.account_ids
    page = _client(world).get(payload["page_url"])
    assert page.status_code == 200
    assert {rule.pk for rule in page.context["rules"]} == {row["id"] for row in payload["rows"]}
    assert _client(world).get(own["url"]).status_code == 200
    narrowed = assert_grounded(run_tool(world["alpha"], default_tools(), "list_rules", {"q": "coffee"}))
    assert [row["id"] for row in narrowed["rows"]] == [world["own_rule"].pk]


@pytest.mark.django_db
def test_list_rules_masks_household_names_on_own_rules_without_consent(world):
    _withhold_consent(world)
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "list_rules", {}))
    assert [row["id"] for row in payload["rows"]] == [world["own_rule"].pk]
    own = payload["rows"][0]
    assert own["category"] == WITHHELD_CATEGORY_NAME
    assert own["account"]["id"] is None


@pytest.mark.django_db
def test_list_rules_caps_rows(world):
    dining = world["household"].categories.get(name="Dining")
    CategoryRule.objects.bulk_create(
        CategoryRule(owner_person=world["alpha"], description_contains=f"SYN cap {index:03}", category=dining)
        for index in range(MAX_TOOL_ROWS + 5)
    )
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "list_rules", {}))
    assert len(payload["rows"]) == MAX_TOOL_ROWS and payload["truncated"] is True


@pytest.mark.django_db
def test_balance_history_lists_sources_and_notes_like_the_balances_page(world):
    result = run_tool(world["alpha"], default_tools(), "balance_history", {"account_name": "A-SYN-CHECKING"})
    payload = assert_grounded(result)
    assert result.account_ids == (world["private"].pk,)
    assert [(row["balance_minor"], row["source"]) for row in payload["rows"]] == [
        (150_000, "manual"), (120_000, "calculated")]
    assert payload["rows"][0]["note"] == "A-SYN-NOTE estimate from statement"
    assert payload["is_estimate"] is False
    page = _client(world).get(payload["page_url"])
    assert page.status_code == 200
    assert [s.amount_display for s in page.context["snapshots"]] == [
        format_minor(row["balance_minor"]) for row in payload["rows"]]
    today = timezone.localdate()
    ranged = assert_grounded(run_tool(world["alpha"], default_tools(), "balance_history", {
        "account_id": world["private"].pk, "date_from": (today - timedelta(days=10)).isoformat(),
        "date_to": today.isoformat()}))
    assert [row["balance_minor"] for row in ranged["rows"]] == [150_000]


@pytest.mark.django_db
def test_balance_history_signs_debts_caps_rows_and_refuses_archived_history(world):
    card = world["card"]
    start = timezone.localdate() - timedelta(days=200)
    BalanceSnapshot.objects.bulk_create(
        BalanceSnapshot(account=card, snapshot_date=start + timedelta(days=index), amount_minor=1_000 + index,
                        currency="USD", source=BalanceSnapshot.Source.MANUAL)
        for index in range(MAX_TOOL_ROWS + 5)
    )
    payload = assert_grounded(run_tool(world["alpha"], default_tools(), "balance_history", {"account_id": card.pk}))
    assert len(payload["rows"]) == MAX_TOOL_ROWS and payload["truncated"] is True
    assert payload["rows"][0]["balance_minor"] == -400_000  # an amount owed reads negative, as on Net worth

    card.status = Account.Status.ARCHIVED
    card.archived_at = timezone.now()
    card.save(update_fields=["status", "archived_at"])
    # No page shows every balance of an archived account, so none is quoted; Net worth's month ends remain.
    archived = run_tool(world["alpha"], default_tools(), "balance_history", {"account_id": card.pk})
    assert not archived.ok and "net_worth_series" in archived.text and "400000" not in archived.text
    assert _client(world).get(reverse("account-balances", args=[card.pk])).status_code == 404


@pytest.mark.django_db
def test_archived_refusal_keeps_the_consent_scope(world):
    for account in (world["shared"], world["beta_card"]):
        account.status = Account.Status.ARCHIVED
        account.archived_at = timezone.now()
        account.save(update_fields=["status", "archived_at"])
    _withhold_consent(world)
    # Out of scope, an archived account answers like a missing one: no hint that it exists.
    for args in ({"account_id": world["shared"].pk}, {"account_name": "HH-SYN-SHARED"},
                 {"account_id": world["beta_card"].pk}):
        result = run_tool(world["alpha"], default_tools(), "balance_history", args)
        assert not result.ok and result.text == "That account is not visible."
    accept_policy(world["beta"], current_policy())
    shared = run_tool(world["alpha"], default_tools(), "balance_history", {"account_id": world["shared"].pk})
    assert not shared.ok and "archived" in shared.text
    beta = run_tool(world["alpha"], default_tools(), "balance_history", {"account_id": world["beta_card"].pk})
    assert beta.text == "That account is not visible."


@pytest.mark.django_db
def test_balance_history_refuses_accounts_the_member_cannot_see(world):
    for args in ({"account_id": world["beta_card"].pk}, {"account_name": "B-PRIV-CARD"}, {"account_id": "x"}, {}):
        result = run_tool(world["alpha"], default_tools(), "balance_history", args)
        assert not result.ok
        assert "B-PRIV" not in result.text and str(BETA_CARD_OWED) not in result.text


@pytest.mark.django_db
@pytest.mark.parametrize("name", LOOKUPS)
def test_a_chat_turn_uses_each_lookup_end_to_end(harness, world, name):  # noqa: F811 - pytest fixture value
    state, url = harness
    connect_harness(world["alpha"], base_url=url, token=TOKEN)
    args = _args(world)[name]
    if name == "balance_history":
        args = {"account_name": "HH-SYN-SHARED"}
    state.need_tool = True
    state.pending_tool_calls = [[{"call_id": "c1", "name": name, "args": args}]]
    conversation = ask(world["alpha"], f"Synthetic question for {name}")
    reply = conversation.messages.get(role=AiConversationMessage.Role.ASSISTANT)
    assert reply.status == AiConversationMessage.Status.DONE
    output = state.tool_outputs[-1]
    assert "could not run" not in output
    assert not any(marker in output for marker in BETA_MARKERS)
    assert reply.figures, f"{name} gave the reply no figure links"
    assert all(figure["url"].startswith("/") for figure in reply.figures)
    assert paragraph_is_grounded(reply.figures[0]["amount_display"], reply.figures)
    expected = set(run_tool(world["alpha"], default_tools(), name, args).account_ids)
    assert expected and expected <= set(conversation.used_account_ids)
    if name in {"savings_goals", "list_rules"}:
        # A household goal or rule is purged with the household's accounts when the member leaves;
        # no goal or rule names the loan, so only that link records it.
        assert world["loan"].pk in conversation.used_account_ids
    if name == "cash_flow_what_if":
        assert world["income"].pk in conversation.used_planned_item_ids
