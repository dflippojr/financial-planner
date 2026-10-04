from datetime import date
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from finance.cash_flow import GROUPING_MONTH, cash_flow_report
from finance.models import (
    Account,
    Household,
    ImportBatch,
    Membership,
    Person,
    SheetComparisonSettings,
    SheetMonthTotal,
    Transaction,
)
from finance.sheet_comparison import (
    comparison_rows,
    mapping_matches_headers,
    parse_sheet_month,
    parsed_month_totals,
    read_sheet_csv,
    save_mapping,
    spending_minor_from_cell,
    store_month_totals,
)


PASSWORD = "Synthetic-passphrase-42!"
UNSIGNED_CSV = (
    b"Month,Income,Spending\n"
    b"2026-01,1000.00,400.00\n"
    b"2026-02,1100.00,500.00\n"
)
SIGNED_CSV = (
    b"Month,Income,Spending\n"
    b"2026-01,1000.00,-400.00\n"
)


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


def make_account(owner, *, name="Synthetic Checking", scope=Account.Scope.PRIVATE, household=None, share_mode=None):
    if share_mode is None:
        share_mode = Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else ""
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=share_mode,
    )


def make_transaction(owner, account, *, transaction_date, amount_minor, description):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="a" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 12, 31),
    )
    digest = f"{account.pk}-{amount_minor}-{transaction_date}".encode().hex().ljust(64, "a")[:64]
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def signed_in(person):
    client = Client()
    assert client.login(username=person.user.username, password=PASSWORD)
    return client


def test_parse_sheet_month_formats():
    assert parse_sheet_month("2026-01") == date(2026, 1, 1)
    assert parse_sheet_month("January 2026") == date(2026, 1, 1)
    assert parse_sheet_month("01/2026") == date(2026, 1, 1)


def test_unsigned_and_signed_spending_map_to_the_same_magnitude():
    unsigned = spending_minor_from_cell("400.00", SheetComparisonSettings.SpendingSign.UNSIGNED)
    signed = spending_minor_from_cell("-400.00", SheetComparisonSettings.SpendingSign.SIGNED)
    assert unsigned == signed == 40000


def test_unsigned_spending_rejects_a_negative_cell():
    with pytest.raises(ValueError):
        spending_minor_from_cell("-400.00", SheetComparisonSettings.SpendingSign.UNSIGNED)


def test_csv_errors_do_not_include_cell_values():
    headers, rows = read_sheet_csv(b"Month,Income,Spending\nnot-a-month,secret-payee,1.00\n")
    mapping = type("Mapping", (), {"month_column": "Month", "income_column": "Income", "spending_column": "Spending", "spending_sign": "unsigned"})()
    with pytest.raises(Exception) as exc:
        parsed_month_totals(rows, mapping)
    assert "secret-payee" not in str(exc.value)
    assert "not-a-month" not in str(exc.value)


@pytest.mark.django_db
def test_mapping_handles_signed_and_unsigned_spending_columns():
    owner = make_person("owner")
    make_household(owner)
    headers, unsigned_rows = read_sheet_csv(UNSIGNED_CSV)
    _headers, signed_rows = read_sheet_csv(SIGNED_CSV)
    unsigned_mapping = save_mapping(
        owner,
        month_column="Month",
        income_column="Income",
        spending_column="Spending",
        spending_sign=SheetComparisonSettings.SpendingSign.UNSIGNED,
        headers=headers,
    )
    signed_mapping = save_mapping(
        owner,
        month_column="Month",
        income_column="Income",
        spending_column="Spending",
        spending_sign=SheetComparisonSettings.SpendingSign.SIGNED,
        headers=headers,
    )
    unsigned_totals = parsed_month_totals(unsigned_rows, unsigned_mapping)
    signed_totals = parsed_month_totals(signed_rows, signed_mapping)
    assert unsigned_totals[date(2026, 1, 1)] == signed_totals[date(2026, 1, 1)] == (100000, 40000)


@pytest.mark.django_db
@patch("finance.sheet_comparison.timezone.localdate", return_value=date(2026, 2, 10))
@patch("finance.cash_flow.timezone.localdate", return_value=date(2026, 2, 10))
def test_month_totals_reconcile_with_the_cash_flow_page(_cash_flow_today, _sheet_today):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, transaction_date=date(2026, 1, 5), amount_minor=100000, description="Synthetic pay")
    make_transaction(owner, account, transaction_date=date(2026, 1, 12), amount_minor=-40000, description="Synthetic grocer")
    headers, rows = read_sheet_csv(UNSIGNED_CSV)
    mapping = save_mapping(
        owner,
        month_column="Month",
        income_column="Income",
        spending_column="Spending",
        spending_sign=SheetComparisonSettings.SpendingSign.UNSIGNED,
        headers=headers,
    )
    store_month_totals(owner, parsed_month_totals(rows, mapping), "synthetic-sheet.csv")
    report = comparison_rows(owner, today=date(2026, 2, 10))
    january = next(row for row in report.rows if row.month == date(2026, 1, 1))
    cash_flow = cash_flow_report(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        grouping=GROUPING_MONTH,
        today=date(2026, 2, 10),
    )
    period = cash_flow.periods[0]
    assert january.app_income_minor == period.income_minor == 100000
    assert january.app_spending_minor == period.spending_minor == 40000
    assert january.app_net_minor == period.net_minor == 60000
    assert january.sheet_net_minor == 60000
    assert january.matches
    assert "date_from=2026-01-01" in january.drilldown_url
    assert reverse("transaction-list") in january.drilldown_url


@pytest.mark.django_db
def test_stored_sheet_totals_are_private_to_the_uploader():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    headers, rows = read_sheet_csv(UNSIGNED_CSV)
    mapping = save_mapping(
        owner,
        month_column="Month",
        income_column="Income",
        spending_column="Spending",
        spending_sign=SheetComparisonSettings.SpendingSign.UNSIGNED,
        headers=headers,
    )
    store_month_totals(owner, parsed_month_totals(rows, mapping), "synthetic-sheet.csv")
    assert SheetMonthTotal.objects.visible_to(owner).count() == 2
    assert SheetMonthTotal.objects.visible_to(member).count() == 0
    assert SheetComparisonSettings.objects.visible_to(member).count() == 0
    assert not SheetMonthTotal.objects.visible_to(None).exists()
    owner_page = signed_in(owner).get(reverse("sheet-comparison"))
    member_page = signed_in(member).get(reverse("sheet-comparison"))
    assert owner_page.status_code == 200
    assert "1,000.00 USD" in owner_page.content.decode()
    assert "synthetic-sheet.csv" not in member_page.content.decode()
    assert "1,000.00 USD" not in member_page.content.decode()
    assert "0 of 0 recent months" in member_page.content.decode() or "No stored sheet totals" in member_page.content.decode()


@pytest.mark.django_db
def test_remembered_mapping_applies_on_later_upload_and_delete_clears_private_rows():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)
    first = client.post(
        reverse("sheet-comparison"),
        {
            "action": "upload",
            "csv_file": SimpleUploadedFile("synthetic-sheet.csv", UNSIGNED_CSV, "text/csv"),
        },
    )
    assert first.status_code == 302
    mapping_page = client.get(reverse("sheet-comparison"))
    assert b"Map columns" in mapping_page.content
    mapped = client.post(
        reverse("sheet-comparison"),
        {
            "action": "map",
            "month_column": "Month",
            "income_column": "Income",
            "spending_column": "Spending",
            "spending_sign": "unsigned",
        },
    )
    assert mapped.status_code == 302
    page = client.get(reverse("sheet-comparison"))
    assert b"January 2026" in page.content
    assert mapping_matches_headers(SheetComparisonSettings.objects.visible_to(owner).get(), ["Month", "Income", "Spending"])
    second = client.post(
        reverse("sheet-comparison"),
        {
            "action": "upload",
            "csv_file": SimpleUploadedFile("synthetic-sheet.csv", UNSIGNED_CSV, "text/csv"),
        },
    )
    assert second.status_code == 302
    assert SheetMonthTotal.objects.visible_to(owner).count() == 2
    note = client.post(
        reverse("sheet-comparison"),
        {"action": "note", "month": "2026-01-01", "note": "Synthetic timing difference"},
    )
    assert note.status_code == 302
    assert SheetMonthTotal.objects.visible_to(owner).get(month=date(2026, 1, 1)).note == "Synthetic timing difference"
    client.post(reverse("sheet-comparison"), {"action": "tolerance", "tolerance": "2.00"})
    assert SheetComparisonSettings.objects.visible_to(owner).get().tolerance_minor == 200
    deleted = client.post(reverse("sheet-comparison-delete"))
    assert deleted.status_code == 302
    assert SheetMonthTotal.objects.visible_to(owner).count() == 0
    assert SheetComparisonSettings.objects.visible_to(owner).count() == 0


@pytest.mark.django_db
def test_app_totals_ignore_another_members_private_account():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Joint", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(member, name="Synthetic Secret")
    make_transaction(owner, shared, transaction_date=date(2026, 1, 4), amount_minor=100000, description="Synthetic shared pay")
    make_transaction(member, private, transaction_date=date(2026, 1, 4), amount_minor=999999, description="Synthetic secret pay")
    headers, rows = read_sheet_csv(UNSIGNED_CSV)
    mapping = save_mapping(
        owner,
        month_column="Month",
        income_column="Income",
        spending_column="Spending",
        spending_sign=SheetComparisonSettings.SpendingSign.UNSIGNED,
        headers=headers,
    )
    store_month_totals(owner, parsed_month_totals(rows, mapping), "synthetic-sheet.csv")
    january = next(row for row in comparison_rows(owner).rows if row.month == date(2026, 1, 1))
    assert january.app_income_minor == 100000
    assert 999999 not in (january.app_income_minor, january.app_net_minor)


@pytest.mark.django_db
def test_nav_includes_sheet_comparison():
    owner = make_person("owner")
    make_household(owner)
    home = signed_in(owner).get(reverse("home"))
    item = next(row for row in home.context["nav_items"] if row["key"] == "sheet-comparison")
    assert item["label"] == "Sheet comparison"
    assert item["url"] == reverse("sheet-comparison")


@pytest.mark.django_db
def test_member_cannot_save_a_note_on_another_members_month():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    headers, rows = read_sheet_csv(UNSIGNED_CSV)
    mapping = save_mapping(
        owner,
        month_column="Month",
        income_column="Income",
        spending_column="Spending",
        spending_sign=SheetComparisonSettings.SpendingSign.UNSIGNED,
        headers=headers,
    )
    store_month_totals(owner, parsed_month_totals(rows, mapping), "synthetic-sheet.csv")
    response = signed_in(member).post(
        reverse("sheet-comparison"),
        {"action": "note", "month": "2026-01-01", "note": "should not stick"},
    )
    assert response.status_code in (302, 400)
    assert SheetMonthTotal.objects.visible_to(owner).get(month=date(2026, 1, 1)).note == ""
