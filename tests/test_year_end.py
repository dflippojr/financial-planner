import csv
import io
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from finance.cash_flow import cash_flow_report, spending_by_category_report
from finance.category_services import assign_category, ensure_household_categories
from finance.export import minor_from_decimal_string
from finance.models import (
    Account,
    BalanceSnapshot,
    Household,
    ImportBatch,
    Membership,
    Person,
    RecurringSeries,
    RecurringSeriesMember,
    Transaction,
)
from finance.net_worth import net_worth_report
from finance.tag_services import add_tag, set_transaction_note_and_tags
from finance.year_end import last_full_year, year_end_report


PASSWORD = "Synthetic-passphrase-42!"
SECRET_ACCOUNT = "Owner Secret Vault"
TODAY = date(2026, 10, 4)


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(
    owner,
    *,
    name="Synthetic Checking",
    account_type=Account.Type.CHECKING,
    scope=Account.Scope.PRIVATE,
    household=None,
):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2025, 3, 15),
    amount_minor=-1000,
    description="Synthetic row",
    fingerprint=None,
    range_start=None,
    range_end=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=range_start or date(2025, 1, 1),
        date_range_end=range_end or date(2025, 12, 31),
    )
    digest = fingerprint or (
        f"{account.pk}-{amount_minor}-{transaction_date}-{description}".encode().hex().ljust(64, "a")[:64]
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        currency="USD",
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def signed_client(person):
    client = Client()
    client.force_login(person.user)
    return client


def read_csv(response):
    body = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.DictReader(io.StringIO(body)))


@pytest.mark.django_db
def test_year_end_totals_match_cash_flow_and_spending_pages():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    income_cat = household.categories.get(name="Income")
    spend = make_transaction(owner, account, amount_minor=-4321, description="Synthetic groceries")
    payday = make_transaction(
        owner,
        account,
        amount_minor=120000,
        description="Synthetic paycheck",
        transaction_date=date(2025, 6, 1),
    )
    assign_category(owner, spend.pk, groceries.pk)
    assign_category(owner, payday.pk, income_cat.pk)

    date_from, date_to = date(2025, 1, 1), date(2025, 12, 31)
    cash_flow = cash_flow_report(owner, date_from=date_from, date_to=date_to, grouping="month", today=TODAY)
    spending = spending_by_category_report(owner, date_from=date_from, date_to=date_to)
    report = year_end_report(owner, year=2025, today=TODAY)

    assert report.cash_flow.summary.income_minor == cash_flow.summary.income_minor == 120000
    assert report.cash_flow.summary.spending_minor == cash_flow.summary.spending_minor == 4321
    assert report.cash_flow.summary.net_minor == cash_flow.summary.net_minor
    assert [period.income_minor for period in report.cash_flow.periods] == [
        period.income_minor for period in cash_flow.periods
    ]
    assert report.spending.total_spending_minor == spending.total_spending_minor == 4321
    by_name = {row.name: row.spending_minor for row in report.spending.rows}
    expected = {row.name: row.spending_minor for row in spending.rows}
    assert by_name == expected
    income_by_name = {row.name: row.income_minor for row in report.income.rows}
    assert income_by_name["Income"] == 120000
    assert report.accounts[0].spending_minor == 4321
    assert report.accounts[0].income_minor == 120000


@pytest.mark.django_db
def test_year_end_hides_another_members_private_account():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(
        owner,
        name="Synthetic Shared",
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private = make_account(owner, name=SECRET_ACCOUNT)
    make_transaction(owner, shared, amount_minor=-1100, description="Synthetic shared spend")
    make_transaction(
        owner,
        private,
        amount_minor=-8800,
        description="Synthetic secret spend",
        fingerprint="c" * 64,
    )
    owner_series = RecurringSeries.objects.create(
        person=owner,
        merchant_key="secret stream",
        display_name="Secret Stream Charge",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-1599,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="d" * 64,
    )
    RecurringSeriesMember.objects.create(
        series=owner_series,
        transaction=Transaction.objects.get(account=private),
    )

    client = signed_client(member)
    page = client.get(reverse("year-end"), {"year": "2025"})
    content = page.content.decode()
    assert page.status_code == 200
    assert SECRET_ACCOUNT not in content
    assert "Secret Stream Charge" not in content
    assert "Synthetic Shared" in content
    assert page.context["report"].cash_flow.summary.spending_minor == 1100
    assert SECRET_ACCOUNT not in [row.name for row in page.context["report"].accounts]

    csv_page = client.get(reverse("year-end-csv", args=["accounts"]), {"year": "2025"})
    rows = read_csv(csv_page)
    names = {row["account"] for row in rows}
    assert SECRET_ACCOUNT not in names
    assert "Synthetic Shared" in names


@pytest.mark.django_db
@patch("finance.year_end_views.timezone.localdate", return_value=TODAY)
@patch("finance.year_end.timezone.localdate", return_value=TODAY)
def test_year_end_defaults_to_last_full_year(_report_today, _view_today):
    owner = make_person("owner")
    make_household(owner)
    make_account(owner)
    client = signed_client(owner)
    page = client.get(reverse("year-end"))
    assert last_full_year(TODAY) == 2025
    assert page.context["report"].year == 2025
    assert "Year-end" in client.get(reverse("home")).content.decode()


@pytest.mark.django_db
def test_year_end_lists_missing_import_months():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(
        owner,
        account,
        transaction_date=date(2025, 1, 10),
        range_start=date(2025, 1, 1),
        range_end=date(2025, 1, 31),
    )
    report = year_end_report(owner, year=2025, today=TODAY)
    labels = [period.label for period in report.missing_months]
    assert "January 2025" not in labels
    assert "February 2025" in labels
    assert "December 2025" in labels


@pytest.mark.django_db
def test_year_end_csv_amounts_round_trip_and_streams():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    groceries = household.categories.get(name="Groceries")
    spend = make_transaction(owner, account, amount_minor=-4321, description="Synthetic groceries")
    assign_category(owner, spend.pk, groceries.pk)
    tag = add_tag(owner, "synthetic-tax")
    set_transaction_note_and_tags(owner, spend.pk, note="", tag_ids=[tag.pk])
    RecurringSeries.objects.create(
        person=owner,
        merchant_key="synthetic stream",
        display_name="Synthetic Stream",
        cadence=RecurringSeries.Cadence.MONTHLY,
        typical_amount_minor=-1599,
        status=RecurringSeries.Status.CONFIRMED,
        confidence=RecurringSeries.Confidence.HIGH,
        reasons=["synthetic"],
        fingerprint="e" * 64,
    )
    BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=date(2024, 12, 31),
        amount_minor=50000,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )
    BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=date(2025, 12, 31),
        amount_minor=45679,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )

    client = signed_client(owner)
    spending_csv = client.get(reverse("year-end-csv", args=["spending"]), {"year": "2025"})
    assert spending_csv.streaming
    assert spending_csv["Content-Type"].startswith("text/csv")
    assert 'filename="year-end-2025-spending.csv"' in spending_csv["Content-Disposition"]
    grocery_row = next(row for row in read_csv(spending_csv) if row["category"] == "Groceries")
    assert grocery_row["currency"] == "USD"
    assert minor_from_decimal_string(grocery_row["spending"]) == 4321

    cash_rows = read_csv(client.get(reverse("year-end-csv", args=["cash-flow"]), {"year": "2025"}))
    march = next(row for row in cash_rows if row["month"] == "2025-03")
    assert minor_from_decimal_string(march["spending"]) == 4321
    assert minor_from_decimal_string(march["income"]) == 0

    tag_rows = read_csv(client.get(reverse("year-end-csv", args=["tags"]), {"year": "2025"}))
    assert minor_from_decimal_string(tag_rows[0]["spending"]) == 4321

    recurring_rows = read_csv(client.get(reverse("year-end-csv", args=["recurring"]), {"year": "2025"}))
    assert minor_from_decimal_string(recurring_rows[0]["annual_cost"]) == 19188

    worth = read_csv(client.get(reverse("year-end-csv", args=["net-worth"]), {"year": "2025"}))
    by_point = {row["point"]: row for row in worth}
    assert minor_from_decimal_string(by_point["start"]["net"]) == 50000
    assert minor_from_decimal_string(by_point["end"]["net"]) == 45679
    start_nw = net_worth_report(
        owner, date_from=date(2024, 12, 1), date_to=date(2024, 12, 31), today=date(2024, 12, 31)
    )
    end_nw = net_worth_report(
        owner, date_from=date(2025, 12, 1), date_to=date(2025, 12, 31), today=date(2025, 12, 31)
    )
    assert start_nw.periods[-1].net_minor == 50000
    assert end_nw.periods[-1].net_minor == 45679

    unknown = client.get(reverse("year-end-csv", args=["not-a-section"]), {"year": "2025"})
    assert unknown.status_code == 404


def test_print_stylesheet_hides_sidebar_and_breaks_sections():
    css = (Path(__file__).resolve().parents[1] / "static" / "src" / "app.css").read_text(encoding="utf-8")
    assert "@media print" in css
    assert ".drawer-side" in css
    assert "break-after: page" in css
    assert ".year-end-print-header" in css
    assert ".no-print" in css
