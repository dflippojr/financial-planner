from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from finance.cash_flow import (
    GROUPING_MONTH,
    GROUPING_QUARTER,
    GROUPING_WEEK,
    GROUPING_YEAR,
    cash_flow_report,
    spending_by_category_report,
)
from finance.category_services import assign_category, link_refund, refresh_transfer_pairs
from finance.models import Account, Category
from finance.spending_trends import (
    OTHER_NAME,
    category_spending_trend_report,
    category_trend_chart_data,
    spending_category_trend_report,
    spending_trend_chart_data,
)
from tests.page_payload import json_script_payload
from tests.test_spending import make_account, make_household, make_person, make_transaction


def _spend(owner, account, category, amount_minor, transaction_date, mark):
    row = make_transaction(
        owner,
        account,
        amount_minor=amount_minor,
        transaction_date=transaction_date,
        fingerprint=mark * 64,
        description=f"Synthetic {mark}",
        range_start=date(transaction_date.year, 1, 1),
        range_end=date(transaction_date.year, 12, 31),
    )
    if category is not None:
        assign_category(owner, row.pk, category.pk)
    return row


@pytest.mark.django_db
@pytest.mark.parametrize(
    "grouping",
    [GROUPING_MONTH, GROUPING_WEEK, GROUPING_QUARTER, GROUPING_YEAR],
)
def test_period_category_totals_reconcile_with_spending_report(grouping):
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    _spend(owner, checking, groceries, -5000, date(2026, 1, 10), "a")
    _spend(owner, checking, groceries, -3000, date(2026, 2, 10), "b")
    original = _spend(owner, checking, dining, -1000, date(2026, 1, 12), "c")
    refund = make_transaction(
        owner,
        checking,
        amount_minor=1500,
        transaction_date=date(2026, 2, 5),
        fingerprint="d" * 64,
        description="Synthetic refund",
        range_start=date(2026, 1, 1),
        range_end=date(2026, 12, 31),
    )
    link_refund(owner, refund.pk, original.pk)
    make_transaction(
        owner,
        checking,
        amount_minor=-4000,
        transaction_date=date(2026, 1, 20),
        fingerprint="e" * 64,
        description="Synthetic transfer out",
        range_start=date(2026, 1, 1),
        range_end=date(2026, 12, 31),
    )
    make_transaction(
        owner,
        savings,
        amount_minor=4000,
        transaction_date=date(2026, 1, 20),
        fingerprint="f" * 64,
        description="Synthetic transfer in",
        range_start=date(2026, 1, 1),
        range_end=date(2026, 12, 31),
    )
    refresh_transfer_pairs(owner)
    date_from, date_to = date(2026, 1, 1), date(2026, 2, 28)
    trend = spending_category_trend_report(
        owner,
        date_from=date_from,
        date_to=date_to,
        grouping=grouping,
        today=date(2026, 3, 1),
    )

    for period in trend.periods:
        expected = spending_by_category_report(
            owner,
            date_from=period.start,
            date_to=period.end,
        )
        cash_flow = cash_flow_report(
            owner,
            date_from=period.start,
            date_to=period.end,
            grouping=grouping,
            today=date(2026, 3, 1),
        )
        assert sum(period.values) == expected.total_spending_minor == period.total_spending_minor
        assert period.total_spending_minor == cash_flow.periods[0].spending_minor


@pytest.mark.django_db
def test_trends_exclude_another_members_private_accounts():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(
        owner,
        name="Synthetic Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private = make_account(owner, name="SECRET PRIVATE LEDGER")
    groceries = household.categories.get(name="Groceries")
    _spend(owner, shared, groceries, -1100, date(2026, 1, 15), "g")
    _spend(owner, private, groceries, -8800, date(2026, 1, 16), "h")
    client = Client()
    client.force_login(member.user)
    page = client.get(
        reverse("spending-by-category"),
        {
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "scope": "household",
            "tab": "trends",
        },
    )
    html = page.content.decode()
    payload = json_script_payload(html, "spending-trend-chart-data")
    report = spending_category_trend_report(
        member,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        scope=Account.Scope.HOUSEHOLD,
        today=date(2026, 2, 1),
    )

    assert page.status_code == 200
    assert payload == spending_trend_chart_data(report)
    assert payload["total_spending_minor"] == 1100
    assert "SECRET PRIVATE LEDGER" not in html
    assert "88.00 USD" not in html
    assert all(value != 8800 for period in payload["periods"] for value in period["values"])


@pytest.mark.django_db
def test_trends_page_chart_payload_matches_accessible_table():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    _spend(owner, checking, groceries, -2500, date(2026, 1, 8), "i")
    _spend(owner, checking, None, -400, date(2026, 1, 9), "j")
    client = Client()
    client.force_login(owner.user)
    page = client.get(
        reverse("spending-by-category"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "tab": "trends"},
    )
    html = page.content.decode()
    payload = json_script_payload(html, "spending-trend-chart-data")

    assert "Trends" in html
    assert 'data-chart="spending-trend"' in html
    assert "Uncategorized" in html
    assert payload == spending_trend_chart_data(page.context["trend_report"])
    assert [item["name"] for item in payload["categories"]] == [item.name for item in page.context["trend_report"].categories]
    for period, row in zip(payload["periods"], page.context["trend_report"].periods, strict=True):
        assert period["values"] == row.values
        assert period["total_spending_minor"] == sum(row.values)


@pytest.mark.django_db
def test_trend_chart_groups_categories_beyond_top_eight_as_other():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    names = [
        "Groceries",
        "Dining",
        "Transportation",
        "Housing",
        "Utilities",
        "Health",
        "Insurance",
        "Shopping",
        "Entertainment",
    ]
    for index, name in enumerate(names, start=1):
        category = household.categories.get(name=name)
        _spend(owner, checking, category, -1000 * index, date(2026, 1, 5), chr(96 + index))
    trend = spending_category_trend_report(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        today=date(2026, 2, 1),
    )
    payload = spending_trend_chart_data(trend)
    ranked = [item.name for item in trend.categories if item.name != "Uncategorized"]

    assert ranked[:8] == list(reversed(names))[:8]
    chart_names = [item["name"] for item in payload["chart_series"]]
    assert chart_names[-1] == OTHER_NAME
    assert len(payload["chart_series"]) == 9
    assert "Uncategorized" in [item["name"] for item in payload["categories"]]
    assert payload["chart_series"][-1]["values"][0] == 1000


@pytest.mark.django_db
def test_category_detail_shows_average_change_and_transaction_link():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    _spend(owner, checking, groceries, -2000, date(2025, 12, 10), "k")
    _spend(owner, checking, groceries, -5000, date(2026, 1, 10), "l")
    _spend(owner, checking, groceries, -3000, date(2026, 2, 10), "m")
    report = category_spending_trend_report(
        owner,
        category_key=str(groceries.pk),
        category_name="Groceries",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 2, 28),
        grouping=GROUPING_MONTH,
        today=date(2026, 3, 1),
    )
    client = Client()
    client.force_login(owner.user)
    page = client.get(
        reverse("spending-category-detail", args=[groceries.pk]),
        {"date_from": "2026-01-01", "date_to": "2026-02-28", "grouping": "month"},
    )
    html = page.content.decode()
    payload = json_script_payload(html, "category-trend-chart-data")

    assert report.total_spending_minor == 8000
    assert report.average_minor == 4000
    assert report.previous_spending_minor == 2000
    assert report.spending_change.direction == "up"
    assert page.status_code == 200
    assert payload == category_trend_chart_data(page.context["report"])
    assert "Average per period" in html
    assert "View transactions" in html
    assert f"category={groceries.pk}" in html
    listed = client.get(page.context["report"].drilldown_url)
    assert listed.status_code == 200


@pytest.mark.django_db
def test_category_detail_and_uncategorized_hide_private_and_unknown_ids():
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    other_household = make_household(outsider, name="Other Synthetic Household")
    shared = make_account(
        owner,
        name="Synthetic Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private = make_account(owner, name="SECRET PRIVATE LEDGER")
    groceries = household.categories.get(name="Groceries")
    foreign = other_household.categories.get(name="Dining")
    transfer = household.categories.get(code=Category.Code.TRANSFER)
    _spend(owner, shared, groceries, -1100, date(2026, 1, 15), "n")
    _spend(owner, private, groceries, -8800, date(2026, 1, 16), "o")
    _spend(owner, shared, None, -300, date(2026, 1, 17), "p")
    client = Client()
    client.force_login(member.user)
    page = client.get(
        reverse("spending-category-detail", args=[groceries.pk]),
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "scope": "household"},
    )
    uncategorized = client.get(
        reverse("spending-category-uncategorized"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "scope": "household"},
    )
    html = page.content.decode()

    assert page.status_code == 200
    assert page.context["report"].total_spending_minor == 1100
    assert "88.00 USD" not in html
    assert uncategorized.context["report"].total_spending_minor == 300
    assert client.get(reverse("spending-category-detail", args=[foreign.pk])).status_code == 404
    assert client.get(reverse("spending-category-detail", args=[transfer.pk])).status_code == 404
    anonymous = Client().get(reverse("spending-category-uncategorized"))
    assert anonymous.status_code == 302


@pytest.mark.django_db
def test_overview_rows_link_to_category_detail():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    _spend(owner, checking, groceries, -1200, date(2026, 1, 4), "q")
    client = Client()
    client.force_login(owner.user)
    page = client.get(
        reverse("spending-by-category"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31"},
    )
    groceries_row = next(row for row in page.context["report"].rows if row.name == "Groceries")

    assert reverse("spending-category-detail", args=[groceries.pk]) in groceries_row.detail_url
    assert reverse("spending-category-detail", args=[groceries.pk]) in page.content.decode()
    assert reverse("spending-category-uncategorized") in page.content.decode()
