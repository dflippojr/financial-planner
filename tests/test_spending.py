from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.cash_flow import (
    cash_flow_report,
    date_range_presets,
    default_date_range,
    spending_by_category_report,
    spending_chart_data,
)
from tests.page_payload import json_script_payload
from finance.category_services import (
    assign_category,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
)
from finance.models import Account, Category, Household, ImportBatch, Membership, Person, Transaction


PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    from finance.category_services import ensure_household_categories

    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 15),
    amount_minor=-1000,
    description="Synthetic row",
    fingerprint=None,
    kind=Transaction.Kind.CASH_FLOW,
    range_start=None,
    range_end=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=range_start or date(2026, 1, 1),
        date_range_end=range_end or date(2026, 1, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=kind,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def test_date_range_presets_match_cash_flow_calendar():
    today = date(2026, 9, 15)
    presets = {item.key: item for item in date_range_presets(today)}

    assert default_date_range(today) == (date(2025, 9, 1), today)
    assert (presets["this-month"].date_from, presets["this-month"].date_to) == (date(2026, 9, 1), today)
    assert (presets["last-month"].date_from, presets["last-month"].date_to) == (date(2026, 8, 1), date(2026, 8, 31))
    assert (presets["last-3-months"].date_from, presets["last-3-months"].date_to) == (date(2026, 7, 1), today)
    assert (presets["last-12-months"].date_from, presets["last-12-months"].date_to) == (date(2025, 10, 1), today)
    assert (presets["year-to-date"].date_from, presets["year-to-date"].date_to) == (date(2026, 1, 1), today)


@pytest.mark.django_db
def test_category_rows_sum_to_cash_flow_spending_and_keep_helper_totals():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    grocery_tx = make_transaction(owner, checking, amount_minor=-5000, description="Synthetic groceries")
    dining_tx = make_transaction(
        owner,
        checking,
        amount_minor=-2500,
        description="Synthetic dining",
        fingerprint="c" * 64,
    )
    uncategorized_tx = make_transaction(
        owner,
        checking,
        amount_minor=-1500,
        description="Synthetic uncategorized",
        fingerprint="d" * 64,
    )
    assign_category(owner, grocery_tx.pk, groceries.pk)
    assign_category(owner, dining_tx.pk, dining.pk)
    date_from, date_to = date(2026, 1, 1), date(2026, 1, 31)
    expected = income_and_spending_totals(owner, date_from=date_from, date_to=date_to)
    cash_flow = cash_flow_report(owner, date_from=date_from, date_to=date_to, today=date(2026, 2, 1))
    report = spending_by_category_report(owner, date_from=date_from, date_to=date_to)

    by_name = {row.name: row for row in report.rows}
    assert [row.name for row in report.rows] == ["Groceries", "Dining", "Uncategorized"]
    assert by_name["Groceries"].spending_minor == expected.spending_by_category_id[groceries.pk]
    assert by_name["Dining"].spending_minor == expected.spending_by_category_id[dining.pk]
    assert by_name["Uncategorized"].spending_minor == expected.spending_by_category_id[None]
    assert sum(row.spending_minor for row in report.rows) == expected.spending_minor == cash_flow.periods[0].spending_minor
    assert by_name["Groceries"].percent_display == "55.6%"
    assert by_name["Dining"].percent_display == "27.8%"
    assert by_name["Uncategorized"].percent_display == "16.7%"
    assert uncategorized_tx.category_id is None


@pytest.mark.django_db
def test_transfers_are_excluded_and_net_refund_is_marked():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    groceries = household.categories.get(name="Groceries")
    make_transaction(owner, checking, amount_minor=-4000, description="Synthetic transfer out")
    make_transaction(
        owner,
        savings,
        amount_minor=4000,
        description="Synthetic transfer in",
        fingerprint="e" * 64,
    )
    original = make_transaction(
        owner,
        checking,
        amount_minor=-1000,
        description="Synthetic store",
        fingerprint="f" * 64,
    )
    refund = make_transaction(
        owner,
        checking,
        amount_minor=1500,
        description="Synthetic store refund",
        fingerprint="g" * 64,
    )
    assign_category(owner, original.pk, groceries.pk)
    refresh_transfer_pairs(owner)
    link_refund(owner, refund.pk, original.pk)
    date_from, date_to = date(2026, 1, 1), date(2026, 1, 31)
    expected = income_and_spending_totals(owner, date_from=date_from, date_to=date_to)
    report = spending_by_category_report(owner, date_from=date_from, date_to=date_to)
    groceries_row = next(row for row in report.rows if row.name == "Groceries")

    assert expected.spending_minor == -500
    assert groceries_row.spending_minor == -500
    assert groceries_row.is_net_refund is True
    assert groceries_row.spending_display == "-5.00 USD"
    assert sum(row.spending_minor for row in report.rows) == expected.spending_minor
    assert all(row.name != "Transfer" for row in report.rows)


@pytest.mark.django_db
def test_uncategorized_is_listed_when_empty_and_drilldown_matches_filter():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    grocery_tx = make_transaction(owner, checking, amount_minor=-2200, description="Synthetic groceries")
    assign_category(owner, grocery_tx.pk, groceries.pk)
    date_from, date_to = date(2026, 1, 1), date(2026, 1, 31)
    report = spending_by_category_report(owner, date_from=date_from, date_to=date_to)
    uncategorized = next(row for row in report.rows if row.name == "Uncategorized")
    groceries_row = next(row for row in report.rows if row.name == "Groceries")
    client = Client()
    client.force_login(owner.user)
    listed = client.get(groceries_row.drilldown_url)
    uncategorized_listed = client.get(uncategorized.drilldown_url)
    parsed = parse_qs(urlparse(groceries_row.drilldown_url).query)

    assert uncategorized.spending_minor == 0
    assert parsed["category"] == [str(groceries.pk)]
    assert parsed["date_from"] == ["2026-01-01"]
    assert parsed["date_to"] == ["2026-01-31"]
    assert list(listed.context["transactions"]) == [grocery_tx]
    assert list(uncategorized_listed.context["transactions"]) == []


@pytest.mark.django_db
def test_member_does_not_see_private_account_in_spending_or_drilldown():
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
    shared_tx = make_transaction(owner, shared, amount_minor=-1100, description="Synthetic shared spend")
    private_tx = make_transaction(
        owner,
        private,
        amount_minor=-8800,
        fingerprint="i" * 64,
        description="Synthetic secret spend",
    )
    assign_category(owner, shared_tx.pk, groceries.pk)
    assign_category(owner, private_tx.pk, groceries.pk)
    report = spending_by_category_report(
        member,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        scope=Account.Scope.HOUSEHOLD,
    )
    groceries_row = next(row for row in report.rows if row.name == "Groceries")
    client = Client()
    client.force_login(member.user)
    page = client.get(
        reverse("spending-by-category"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "scope": "household"},
    )
    listed = client.get(groceries_row.drilldown_url)

    assert groceries_row.spending_minor == 1100
    assert "SECRET PRIVATE LEDGER" not in page.content.decode()
    assert "8800" not in page.content.decode()
    assert "88.00 USD" not in page.content.decode()
    html = page.content.decode()
    payload = json_script_payload(html, "spending-chart-data")
    assert payload == spending_chart_data(report)
    assert payload["rows"][0]["spending_minor"] == 1100
    assert payload["total_spending_minor"] == 1100
    assert all(row["spending_minor"] != 8800 for row in payload["rows"])
    assert "/static/vendor/chart.umd.min.js" in html
    assert "cdn." not in html.lower()
    assert list(listed.context["transactions"]) == [shared_tx]
    assert private_tx not in listed.context["transactions"]


@pytest.mark.django_db
@patch("finance.views.timezone.localdate", return_value=date(2026, 9, 15))
@patch("finance.cash_flow.timezone.localdate", return_value=date(2026, 9, 15))
def test_spending_page_default_range_presets_and_nav(_cash_today, _view_today):
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)
    client = Client()
    client.force_login(owner.user)

    response = client.get(reverse("spending-by-category"))
    content = response.content.decode()
    home = client.get(reverse("home"))

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert ">Spending by category</h1>" in content
    assert reverse("spending-by-category") in home.content.decode()
    assert "This month" in content
    assert "Last month" in content
    assert "Last 3 months" in content
    assert "Last 12 months" in content
    assert "Year to date" in content
    payload = json_script_payload(content, "spending-chart-data")
    assert payload == spending_chart_data(response.context["report"])
    assert 'data-chart="spending"' in content
    assert "/static/vendor/chart.umd.min.js" in content
    assert "cdn." not in content.lower()
    assert response.context["filter_form"]["date_from"].value() == date(2025, 9, 1)
    assert response.context["filter_form"]["date_to"].value() == date(2026, 9, 15)
    assert "Uncategorized" in content


@pytest.mark.django_db
def test_spending_page_empty_state_and_anonymous_redirect():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    client = Client()
    client.force_login(owner.user)
    response = client.get(
        reverse("spending-by-category"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31"},
    )

    assert "No visible transactions yet." in response.content.decode()
    assert reverse("csv-import-preview", args=(account.pk,)) in response.content.decode()
    anonymous = Client().get(reverse("spending-by-category"))
    assert anonymous.status_code == 302
    assert anonymous.url.startswith(reverse("login"))


@pytest.mark.django_db
def test_uncategorized_drilldown_lists_charges_whose_category_is_no_longer_visible():
    from finance.lifecycle_services import leave_household

    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    checking = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    grocery_tx = make_transaction(owner, checking, amount_minor=-2200, description="Synthetic groceries")
    assign_category(owner, grocery_tx.pk, groceries.pk)
    leave_household(owner)

    report = spending_by_category_report(owner, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    uncategorized = next(row for row in report.rows if row.name == "Uncategorized")
    client = Client()
    client.force_login(owner.user)
    listed = client.get(uncategorized.drilldown_url)

    assert uncategorized.spending_minor == 2200
    assert list(listed.context["transactions"]) == [grocery_tx]
