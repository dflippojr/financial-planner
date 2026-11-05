"""Chat charts (#646): make_chart draws only server-computed series from the report pages' own services."""

import json
from datetime import date, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.ai_services import connect_harness
from finance.ai_tools import WITHHELD_CATEGORY_NAME, cash_flow_totals, run_tool
from finance.budget_services import month_budget_cards, save_budget
from finance.cash_flow import spending_by_category_report
from finance.category_services import add_category, ensure_household_categories
from finance.chat_charts import MAX_CATEGORIES, MAX_CHARTS_PER_REPLY, OTHER_NAME, chart_tools, make_chart
from finance.chat_services import delete_conversation, purge_expired
from finance.lifecycle_services import unshare_account
from finance.export import collect_export_tables
from finance.policy_services import accept_policy, current_policy
from finance.models import Account, AiConversation, AiConversationMessage, BalanceSnapshot, Budget, Category
from tests.chat_helpers import ask
from tests.test_ai_household_labels import CUSTOM, _household_without_consent
from tests.test_chat import TOKEN, add_txn, checking, harness, make_member  # noqa: F401
from tests.test_security_headers import assert_page_is_csp_clean

pytestmark = pytest.mark.django_db

H1 = {"date_from": "2026-01-01", "date_to": "2026-06-30"}
SPENDING_BY_MONTH = {"kind": "over_time", "report": "spending", **H1}


def seed():
    """A household with full AI consent: shared and private spending over six months, a budget and balances."""
    user, alpha, household = make_member("alpha")
    make_member("beta", household=household, policy=current_policy())
    ensure_household_categories(household)
    groceries = Category.objects.get(household=household, name="Groceries")
    dining = Category.objects.get(household=household, name="Dining")
    shared = checking(alpha, household, "Synthetic Shared Checking")
    private = checking(alpha, household, "Synthetic Alpha Private", private=True)
    for month in range(1, 7):
        add_txn(shared, alpha, date(2026, month, 3), 500000, f"Synthetic payroll {month}")
        add_txn(shared, alpha, date(2026, month, 5), -(10000 + month * 137), f"SYN-MARKET weekly {month}", groceries)
        add_txn(private, alpha, date(2026, month, 9), -(2500 + month * 11), f"Synthetic cafe {month}", dining)
        add_txn(shared, alpha, date(2026, month, 12), -(3301 * month), f"SYN-MARKET deli {month}")
    save_budget(alpha, {"scope": Budget.Scope.HOUSEHOLD, "category": groceries, "amount_minor": 15000,
                        "effective_month": date(2026, 3, 1), "rollover_enabled": False})
    BalanceSnapshot.objects.create(account=shared, snapshot_date=date(2026, 2, 28), amount_minor=1234567,
                                   currency="USD", source=BalanceSnapshot.Source.MANUAL)
    BalanceSnapshot.objects.create(account=shared, snapshot_date=date(2026, 5, 31), amount_minor=2345678,
                                   currency="USD", source=BalanceSnapshot.Source.MANUAL)
    return user, alpha, household, shared, private, groceries


def _chart(result):
    assert result.ok, result.text
    (chart,) = result.charts
    return chart


def _values(chart, index=0):
    return chart["series"][index]["values"]


def _page_context(user, url):
    client = Client()
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200, url
    return response.context


def test_spending_by_month_chart_in_a_reply_equals_the_spending_report_to_the_cent(harness):  # noqa: F811
    state, url = harness
    user, alpha, _household, _shared, _private, _groceries = seed()
    connect_harness(alpha, base_url=url, token=TOKEN)
    state.need_tool = True
    state.pending_tool_calls = [[{"call_id": "c1", "name": "make_chart", "args": SPENDING_BY_MONTH}]]
    conversation = ask(alpha, "Chart my spending by month this year")
    reply = conversation.messages.get(role=AiConversationMessage.Role.ASSISTANT)
    (chart,) = reply.charts
    assert chart["v"] == 1
    assert chart["kind"] == "over_time" and chart["style"] == "bar"
    assert chart["spec"] == {**SPENDING_BY_MONTH, "style": "bar", "grouping": "month"}

    tool = json.loads(cash_flow_totals(alpha, {**H1, "grouping": "month"}).text)
    assert chart["labels"] == [period["label"] for period in tool["periods"]]
    assert _values(chart) == [period["spending_minor"] for period in tool["periods"]]
    assert sum(_values(chart)) == tool["spending_minor"]
    # The chart's link opens the Home cash flow report for the same range, which shows the same cents.
    page = _page_context(user, chart["page_url"])["chart_data"]["periods"]
    assert _values(chart) == [row["spending_minor"] for row in page if row["label"] in chart["labels"]]
    assert chart["series"][0]["displays"][0] == next(row["spending_display"] for row in page)
    # The chart's figures join the Sources list, and its accounts are recorded for purges.
    assert {"label": f"{chart['labels'][0]} Spending", "amount_minor": _values(chart)[0]}.items() <= reply.figures[0].items()
    conversation.refresh_from_db()
    assert set(conversation.used_account_ids) == set(Account.objects.values_list("pk", flat=True))


def test_income_net_and_weekly_charts_use_the_cash_flow_report():
    _user, alpha, *_rest = seed()
    report = json.loads(cash_flow_totals(alpha, {**H1, "grouping": "month"}).text)["periods"]
    income = _chart(make_chart(alpha, {"kind": "over_time", "report": "income", "style": "line", **H1}))
    net = _chart(make_chart(alpha, {"kind": "over_time", "report": "net", **H1}))
    assert income["style"] == "line"
    assert _values(income) == [period["income_minor"] for period in report]
    assert _values(net) == [period["net_minor"] for period in report]
    weekly = _chart(make_chart(alpha, {**SPENDING_BY_MONTH, "date_from": "2026-03-01", "date_to": "2026-04-30",
                                       "grouping": "week"}))
    weeks = json.loads(cash_flow_totals(alpha, {"date_from": "2026-03-01", "date_to": "2026-04-30",
                                                "grouping": "week"}).text)["periods"]
    assert _values(weekly) == [period["spending_minor"] for period in weeks]


def test_category_breakdown_matches_the_spending_by_category_page():
    user, alpha, *_rest = seed()
    chart = _chart(make_chart(alpha, {"kind": "category_breakdown", "report": "spending", **H1}))
    assert chart["style"] == "donut"
    rows = _page_context(user, chart["page_url"])["chart_data"]["rows"]
    expected = {row["name"]: row["spending_minor"] for row in rows if row["spending_minor"]}
    assert dict(zip(chart["labels"], _values(chart), strict=True)) == expected
    assert sum(_values(chart)) == spending_by_category_report(
        alpha, date_from=date(2026, 1, 1), date_to=date(2026, 6, 30)).total_spending_minor
    bars = _chart(make_chart(alpha, {"kind": "category_breakdown", "report": "spending", "style": "bar", **H1}))
    assert bars["style"] == "bar" and _values(bars) == _values(chart)


def test_breakdown_groups_categories_past_the_cap_into_other_and_keeps_the_total():
    _user, alpha, _household, shared, _private, _groceries = seed()
    for index in range(MAX_CATEGORIES + 3):
        category = add_category(alpha, f"Synthetic bucket {index:02d}")
        add_txn(shared, alpha, date(2026, 2, 14), -(100 + index), f"Synthetic bucket row {index}", category)
    chart = _chart(make_chart(alpha, {"kind": "category_breakdown", "report": "spending", **H1}))
    report = spending_by_category_report(alpha, date_from=date(2026, 1, 1), date_to=date(2026, 6, 30))
    assert len(chart["labels"]) == MAX_CATEGORIES
    assert chart["labels"][-1] == OTHER_NAME
    assert sum(_values(chart)) == report.total_spending_minor


def test_budget_vs_actual_matches_the_budget_cards():
    user, alpha, *_rest = seed()
    chart = _chart(make_chart(alpha, {"kind": "budget_vs_actual", "report": "budgets", "date_from": "2026-03-01"}))
    cards = month_budget_cards(alpha, date(2026, 3, 1))
    assert chart["labels"] == [card.name for card in cards] == ["Groceries"]
    assert _values(chart, 0) == [card.amount_minor for card in cards]
    assert _values(chart, 1) == [card.spent_minor for card in cards]
    assert chart["page_url"] == reverse("budgets") + "?month=2026-03"
    assert _page_context(user, chart["page_url"])["cards"][0].spent_minor == _values(chart, 1)[0]


def test_compare_periods_matches_two_spending_reports():
    _user, alpha, *_rest = seed()
    chart = _chart(make_chart(alpha, {"kind": "compare_periods", "report": "spending",
                                      "date_from": "2026-06-01", "date_to": "2026-06-30",
                                      "compare_from": "2026-01-01", "compare_to": "2026-01-31"}))
    for index, (start, end) in enumerate(((date(2026, 6, 1), date(2026, 6, 30)), (date(2026, 1, 1), date(2026, 1, 31)))):
        rows = {row.name: row.spending_minor for row in spending_by_category_report(alpha, date_from=start, date_to=end).rows}
        assert dict(zip(chart["labels"], _values(chart, index), strict=True)) == {
            name: rows.get(name, 0) for name in chart["labels"]
        }
    assert [series["name"] for series in chart["series"]] == ["2026-06-01 to 2026-06-30", "2026-01-01 to 2026-01-31"]


def test_category_merchant_and_net_worth_over_time_match_their_pages():
    user, alpha, _household, _shared, _private, groceries = seed()
    category = _chart(make_chart(alpha, {"kind": "over_time", "report": "category", "category": "groceries", **H1}))
    page = _page_context(user, category["page_url"])["chart_data"]["periods"]
    assert _values(category) == [row["spending_minor"] for row in page] == [10000 + m * 137 for m in range(1, 7)]
    by_id = _chart(make_chart(alpha, {"kind": "over_time", "report": "category", "category_id": groceries.pk, **H1}))
    assert _values(by_id) == _values(category)

    merchant = _chart(make_chart(alpha, {"kind": "over_time", "report": "merchant",
                                         "description_contains": "syn-market", **H1}))
    assert _values(merchant) == [10000 + m * 137 + 3301 * m for m in range(1, 7)]
    assert "q=syn-market" in merchant["page_url"]

    worth = _chart(make_chart(alpha, {"kind": "over_time", "report": "net_worth", **H1}))
    assert worth["style"] == "line"
    page = _page_context(user, worth["page_url"])["chart_data"]["periods"]
    assert _values(worth) == [row["net_minor"] for row in page]
    assert 2345678 in _values(worth)


@pytest.mark.parametrize("smuggled", [
    {"values": [100, 200, 300]},
    {"series": [{"name": "Spending", "values": [99999]}]},
    {"data": {"Jan": 1}},
    {"amount_minor": 123456},
])
def test_a_model_supplied_number_is_rejected_and_never_drawn(smuggled, harness):  # noqa: F811
    _user, alpha, *_rest = seed()
    result = make_chart(alpha, {**SPENDING_BY_MONTH, **smuggled})
    assert not result.ok and not result.charts and not result.figures
    assert "computes every value" in result.text

    state, url = harness
    connect_harness(alpha, base_url=url, token=TOKEN)
    state.need_tool = True
    state.pending_tool_calls = [[{"call_id": "c1", "name": "make_chart", "args": {**SPENDING_BY_MONTH, **smuggled}}]]
    reply = ask(alpha, "Chart this").messages.get(role=AiConversationMessage.Role.ASSISTANT)
    assert reply.charts == []


@pytest.mark.parametrize(("args", "message"), [
    ({"kind": "pie_of_everything", "report": "spending"}, "Unknown chart kind"),
    ({"kind": "over_time", "report": "transactions"}, "draw one of these reports"),
    ({"kind": "budget_vs_actual", "report": "spending"}, "draw one of these reports"),
    ({"kind": "category_breakdown", "report": "spending", "style": "line"}, "are drawn as"),
    ({"kind": "over_time", "report": "spending", "date_from": "2024-01-01", "date_to": "2026-06-30"}, "at most 24"),
    ({"kind": "over_time", "report": "net_worth", "grouping": "week"}, "by month"),
    ({"kind": "over_time", "report": "spending", "date_from": "not a date"}, "date like"),
    ({"kind": "over_time", "report": "spending", "date_from": "2026-06-01", "date_to": "2026-01-01"}, "on or before"),
    ({"kind": "category_breakdown", "report": "spending", "category_id": 1}, "do not take category_id"),
    ({"kind": "compare_periods", "report": "spending"}, "needs compare_from"),
    ({"kind": "over_time", "report": "merchant"}, "description_contains"),
    ({"kind": "over_time", "report": "spending", "scope": "everyone"}, "scope must be"),
])
def test_unknown_or_malformed_requests_get_a_recoverable_tool_error(args, message):
    _user, alpha, *_rest = seed()
    result = make_chart(alpha, args)
    assert not result.ok and not result.charts
    assert message in result.text


def test_an_inaccessible_account_gives_a_tool_error_without_leaking_it():
    _user, alpha, household, *_rest = seed()
    _beta_user, gamma, _ = make_member("gamma", household=household, policy=current_policy())
    secret = checking(gamma, household, "Synthetic Gamma Secret", private=True)
    add_txn(secret, gamma, date(2026, 2, 2), -987654, "Synthetic gamma secret buy")
    missing = make_chart(alpha, {**SPENDING_BY_MONTH, "account_id": 999999})
    hidden = make_chart(alpha, {**SPENDING_BY_MONTH, "account_id": secret.pk})
    assert not hidden.ok and not hidden.charts and not hidden.figures and not hidden.account_ids
    assert hidden.text == missing.text == "That account is not visible."
    # The member's own unfiltered chart never counts the other member's private spending.
    mine = _chart(make_chart(alpha, SPENDING_BY_MONTH))
    assert secret.pk not in make_chart(alpha, SPENDING_BY_MONTH).account_ids
    assert 987654 not in _values(mine)


def test_a_withheld_category_gives_the_missing_category_error_and_breakdowns_withhold_its_name():
    alpha, beta, _household, custom, _private = _household_without_consent()
    march = {"date_from": "2026-03-01", "date_to": "2026-03-31"}
    over_time = {"kind": "over_time", "report": "category", **march}
    withheld = make_chart(alpha, {**over_time, "category_id": custom.pk})
    by_name = make_chart(alpha, {**over_time, "category": CUSTOM})
    missing = make_chart(alpha, {**over_time, "category_id": 999999})
    for result in (withheld, by_name):
        assert not result.ok and not result.charts and not result.figures
        assert result.text == missing.text == "That category is not visible."
        assert CUSTOM not in result.text and "42.00" not in result.text

    breakdown = _chart(make_chart(alpha, {"kind": "category_breakdown", "report": "spending", **march}))
    assert CUSTOM not in json.dumps(breakdown)
    assert breakdown["labels"] == [WITHHELD_CATEGORY_NAME]
    budgets = _chart(make_chart(alpha, {"kind": "budget_vs_actual", "report": "budgets", **march}))
    assert CUSTOM not in json.dumps(budgets) and budgets["labels"] == [WITHHELD_CATEGORY_NAME]

    accept_policy(beta, current_policy())
    assert _chart(make_chart(alpha, {**over_time, "category_id": custom.pk}))["labels"] == ["March 2026"]


def test_a_reply_draws_at_most_three_charts():
    _user, alpha, *_rest = seed()
    (tool,) = chart_tools()
    results = [run_tool(alpha, (tool,), "make_chart", SPENDING_BY_MONTH) for _ in range(MAX_CHARTS_PER_REPLY + 1)]
    assert [result.ok for result in results] == [True] * MAX_CHARTS_PER_REPLY + [False]
    assert "At most 3 charts" in results[-1].text
    # A new turn gets a new tool with its own count.
    (fresh,) = chart_tools()
    assert run_tool(alpha, (fresh,), "make_chart", SPENDING_BY_MONTH).ok


def _charted_conversation(harness, alpha, args=SPENDING_BY_MONTH):  # noqa: F811
    state, url = harness
    connect_harness(alpha, base_url=url, token=TOKEN)
    state.need_tool = True
    state.tool_answered = False
    state.pending_tool_calls = [[{"call_id": "c1", "name": "make_chart", "args": args}]]
    conversation = ask(alpha, "Chart my spending")
    assert AiConversationMessage.objects.filter(conversation=conversation).exclude(charts=[]).count() == 1
    return conversation


def test_deleting_or_expiring_a_conversation_deletes_its_charts(harness):  # noqa: F811
    _user, alpha, *_rest = seed()
    conversation = _charted_conversation(harness, alpha)
    delete_conversation(alpha, conversation.pk)
    assert not AiConversationMessage.objects.exclude(charts=[]).exists()

    conversation = _charted_conversation(harness, alpha)
    AiConversation.objects.filter(pk=conversation.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    purge_expired(alpha)
    assert not AiConversationMessage.objects.exclude(charts=[]).exists()


def test_losing_sight_of_a_charted_account_deletes_the_chart(harness):  # noqa: F811
    _user, alpha, household, shared, *_rest = seed()
    _gamma_user, gamma, _ = make_member("gamma", household=household, policy=current_policy())
    own = checking(gamma, household, "Synthetic Gamma Shared")
    add_txn(own, gamma, date(2026, 2, 2), -4321, "Synthetic gamma shared buy")
    conversation = _charted_conversation(harness, alpha)
    assert own.pk in AiConversation.objects.get(pk=conversation.pk).used_account_ids
    unshare_account(gamma, own.pk)  # gamma takes their account back; alpha no longer sees it
    assert not AiConversation.objects.filter(pk=conversation.pk).exists()
    assert not AiConversationMessage.objects.exclude(charts=[]).exists()


def test_saved_and_live_turns_render_the_same_chart_markup_with_a_table_and_report_link(harness):  # noqa: F811
    user, alpha, *_rest = seed()
    conversation = _charted_conversation(harness, alpha)
    reply = conversation.messages.get(role=AiConversationMessage.Role.ASSISTANT)
    chart = reply.charts[0]
    client = Client()
    client.force_login(user)
    page = client.get(f"/chat/?c={conversation.pk}")
    assert_page_is_csp_clean(page)
    html = page.content.decode()
    live = client.get(reverse("chat-turn", args=[reply.pk])).json()
    assert live["charts_html"].strip() in html
    assert 'type="application/json"' in live["charts_html"]
    assert f'href="{chart["page_url"].replace("&", "&amp;")}"' in live["charts_html"]
    assert '<div class="sr-only"><table>' in live["charts_html"]
    for label, display in zip(chart["labels"], chart["series"][0]["displays"], strict=True):
        assert f"<tr><td>{label}</td><td>{display}</td></tr>" in live["charts_html"]
    assert f'aria-label="{chart["summary"]}"' in live["charts_html"]
    assert "highest" in chart["summary"]


def test_the_member_export_includes_charts(harness):  # noqa: F811
    _user, alpha, *_rest = seed()
    _charted_conversation(harness, alpha)
    rows = collect_export_tables(alpha)["ai_conversation_messages"]
    assert [row["charts"][0]["kind"] for row in rows if row["charts"]] == ["over_time"]


def test_a_budget_chart_covers_the_month_of_date_from_and_needs_no_date_to():
    _user, alpha, *_rest = seed()
    next_month = (timezone.localdate().replace(day=1) + timedelta(days=32)).replace(day=1)
    chart = _chart(make_chart(alpha, {"kind": "budget_vs_actual", "report": "budgets",
                                      "date_from": next_month.isoformat()}))
    assert chart["page_url"] == reverse("budgets") + f"?month={next_month.isoformat()[:7]}"
    assert chart["spec"]["date_from"] == next_month.isoformat()
    # A stray date_to does not move or break the month.
    again = _chart(make_chart(alpha, {"kind": "budget_vs_actual", "report": "budgets",
                                      "date_from": "2026-03-17", "date_to": "2026-01-01"}))
    assert again["page_url"].endswith("month=2026-03") and again["spec"]["date_to"] == "2026-03-31"


def test_charts_link_to_the_private_scope_they_draw_while_household_consent_is_incomplete():
    alpha, _beta, household, _custom, _private = _household_without_consent()
    shared = checking(alpha, household, "Synthetic Shared Checking")
    add_txn(shared, alpha, date(2026, 3, 6), -9000, "Synthetic shared groceries")
    march = {"date_from": "2026-03-01", "date_to": "2026-03-31"}
    client = Client()
    client.force_login(alpha.user)
    over_time = _chart(make_chart(alpha, {"kind": "over_time", "report": "spending", **march}))
    assert _values(over_time) == [4200]
    assert "scope=private" in over_time["page_url"]
    page = client.get(over_time["page_url"]).context["chart_data"]["periods"]
    assert [row["spending_minor"] for row in page if not row.get("projected")] == [4200]
    breakdown = _chart(make_chart(alpha, {"kind": "category_breakdown", "report": "spending", **march}))
    assert "scope=private" in breakdown["page_url"]
    rows = client.get(breakdown["page_url"]).context["chart_data"]["rows"]
    assert sum(row["spending_minor"] for row in rows) == sum(_values(breakdown)) == 4200
    worth = _chart(make_chart(alpha, {"kind": "over_time", "report": "net_worth", **march}))
    assert "scope=private" in worth["page_url"]
