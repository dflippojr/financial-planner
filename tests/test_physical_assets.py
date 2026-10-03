from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from finance.cash_flow import cash_flow_report, spending_by_category_report
from finance.export import collect_export_tables
from finance.lifecycle_services import archive_account, delete_account, share_account
from finance.models import Account, BalanceSnapshot, Household, ImportBatch, Membership, Person, Transaction, loan_asset_pairing_allowed
from finance.net_worth import contribution_parts, net_worth_report
from finance.pairing_services import PairingError, set_loan_secured_asset
from tests.page_payload import json_script_payload


PASSWORD = "Synthetic-passphrase-42!"
SECRET_HOUSE = "Owner Secret House"
MEMBER_GOLD = "Member Private Gold"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
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


def add_snapshot(account, snapshot_date, amount_minor, note=""):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
        note=note,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_loan_contribution_parts_match_credit_cards():
    assert contribution_parts(Account.Type.LOAN, "simplefin", -250_000) == (0, 250_000)
    assert contribution_parts(Account.Type.LOAN, "manual", 250_000) == (0, 250_000)
    assert contribution_parts(Account.Type.REAL_ESTATE, "manual", 400_000) == (400_000, 0)


@pytest.mark.django_db
def test_create_asset_records_valuations_and_archives():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)
    created = client.post(
        reverse("account-list"),
        {
            "name": "Synthetic House",
            "account_type": Account.Type.REAL_ESTATE,
            "sharing": Account.Scope.PRIVATE,
        },
    )
    house = Account.objects.get(name="Synthetic House")
    assert created.status_code == 302
    assert created.url == reverse("account-balances", args=[house.pk])
    assert house.account_type == Account.Type.REAL_ESTATE

    recorded = client.post(
        reverse("account-balances", args=[house.pk]),
        {"snapshot_date": "2026-03-01", "amount": "350000.00", "note": "county assessment"},
    )
    assert recorded.status_code == 302
    snap = BalanceSnapshot.objects.get(account=house)
    assert snap.amount_minor == 35_000_000
    assert snap.note == "county assessment"
    assert snap.currency == "USD"

    page = client.get(reverse("account-balances", args=[house.pk]))
    html = page.content.decode()
    assert "estimate" in html
    assert "not an appraisal" in html.lower() or "not appraisals" in html.lower()
    assert "county assessment" in html
    accounts_page = client.get(reverse("account-list")).content.decode()
    assert reverse("csv-import-preview", args=[house.pk]) not in accounts_page

    import_redirect = client.get(reverse("csv-import-preview", args=[house.pk]))
    assert import_redirect.status_code == 302
    assert import_redirect.url == reverse("account-balances", args=[house.pk])

    edited = client.post(
        reverse("account-snapshot-edit", args=[house.pk, snap.pk]),
        {"snapshot_date": "2026-03-15", "amount": "355000.00", "note": "online estimate"},
    )
    assert edited.status_code == 302
    snap.refresh_from_db()
    assert snap.amount_minor == 35_500_000
    assert snap.note == "online estimate"

    archive_account(owner.user, house.pk)
    house.refresh_from_db()
    assert house.status == Account.Status.ARCHIVED
    delete_account(owner, house.pk)
    assert not Account.objects.filter(pk=house.pk).exists()


@pytest.mark.django_db
def test_net_worth_includes_assets_and_subtracts_loans_with_equity_line():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    checking = make_account(owner, name="Synthetic Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    house = make_account(
        owner,
        name="Synthetic House",
        account_type=Account.Type.REAL_ESTATE,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    mortgage = make_account(
        owner,
        name="Synthetic Mortgage",
        account_type=Account.Type.LOAN,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private_house = make_account(owner, name=SECRET_HOUSE, account_type=Account.Type.REAL_ESTATE)
    member_gold = make_account(member, name=MEMBER_GOLD, account_type=Account.Type.PRECIOUS_METALS)
    add_snapshot(checking, date(2026, 1, 31), 10_000)
    add_snapshot(house, date(2026, 1, 15), 40_000_000, note="synthetic county assessment")
    add_snapshot(mortgage, date(2026, 1, 31), 30_000_000)
    add_snapshot(private_house, date(2026, 1, 20), 5_000_000)
    add_snapshot(member_gold, date(2026, 1, 10), 200_000)
    mortgage.secured_asset = house
    mortgage.save()

    report = net_worth_report(
        owner.user,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        today=date(2026, 1, 31),
    )
    january = report.periods[0]
    assert january.assets_minor == 10_000 + 40_000_000 + 5_000_000
    assert january.liabilities_minor == 30_000_000
    assert january.net_minor == 15_010_000
    assert january.net_display.endswith("USD")
    assert len(january.equity_lines) == 1
    assert january.equity_lines[0].equity_minor == 10_000_000
    assert january.equity_lines[0].is_estimate
    unpaired_net = january.assets_minor - january.liabilities_minor
    assert unpaired_net == january.net_minor

    owner_page = signed_in(owner).get(
        reverse("net-worth"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31"},
    )
    member_page = signed_in(member).get(
        reverse("net-worth"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31"},
    )
    household_only = signed_in(owner).get(
        reverse("net-worth"),
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "scope": "household"},
    )
    owner_html = owner_page.content.decode()
    member_html = member_page.content.decode()
    household_html = household_only.content.decode()
    owner_chart = json_script_payload(owner_html, "net-worth-chart-data")

    assert SECRET_HOUSE in owner_html
    assert SECRET_HOUSE not in member_html
    assert SECRET_HOUSE not in household_html
    assert MEMBER_GOLD not in owner_html
    assert MEMBER_GOLD in member_html
    assert "estimate" in owner_html
    assert "not appraisals" in owner_html
    assert "Synthetic House equity" in owner_html
    assert household_only.context["report"].summary.net_minor == 10_010_000
    assert member_page.context["report"].summary.net_minor == 10_210_000
    assert owner_chart["periods"][0]["net_minor"] == january.net_minor
    assert owner_chart["periods"][0]["equity_lines"][0]["equity_minor"] == 10_000_000


@pytest.mark.django_db
def test_pairing_rejects_cross_scope_and_other_owner():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private_house = make_account(owner, name="Private House", account_type=Account.Type.REAL_ESTATE)
    household_house = make_account(
        owner,
        name="Household House",
        account_type=Account.Type.REAL_ESTATE,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    member_house = make_account(member, name="Member House", account_type=Account.Type.REAL_ESTATE)
    private_loan = make_account(owner, name="Private Mortgage", account_type=Account.Type.LOAN)
    household_loan = make_account(
        owner,
        name="Household Mortgage",
        account_type=Account.Type.LOAN,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )

    assert loan_asset_pairing_allowed(private_loan, private_house)
    assert loan_asset_pairing_allowed(household_loan, household_house)
    assert not loan_asset_pairing_allowed(private_loan, household_house)
    assert not loan_asset_pairing_allowed(household_loan, private_house)
    assert not loan_asset_pairing_allowed(private_loan, member_house)

    with pytest.raises(PairingError, match="same scope"):
        set_loan_secured_asset(owner.user, private_loan.pk, household_house.pk)
    with pytest.raises(PairingError, match="same scope"):
        set_loan_secured_asset(owner.user, household_loan.pk, private_house.pk)

    household_loan.secured_asset = member_house
    with pytest.raises(ValidationError, match="same scope"):
        household_loan.save()

    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        set_loan_secured_asset(owner.user, private_loan.pk, member_house.pk)

    set_loan_secured_asset(owner.user, private_loan.pk, private_house.pk)
    share_account(owner.user, private_loan.pk, Account.ShareMode.CO_OWNED)
    private_loan.refresh_from_db()
    assert private_loan.secured_asset_id is None


@pytest.mark.django_db
def test_cash_flow_and_spending_ignore_asset_and_loan_accounts():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    house = make_account(owner, name="Synthetic House", account_type=Account.Type.REAL_ESTATE)
    loan = make_account(owner, name="Synthetic Mortgage", account_type=Account.Type.LOAN)
    batch = ImportBatch.objects.create(
        account=checking,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="a" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    Transaction.objects.create(
        account=checking,
        import_batch=batch,
        transaction_date=date(2026, 1, 10),
        amount_minor=-5_000,
        currency="USD",
        description="Synthetic groceries",
        source_row_number=2,
        fingerprint="a" * 64,
        original_fields={"synthetic": "row"},
    )
    add_snapshot(house, date(2026, 1, 15), 40_000_000)
    add_snapshot(loan, date(2026, 1, 15), 10_000_000)
    loan.secured_asset = house
    loan.save()

    cash = cash_flow_report(
        owner.user,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        today=date(2026, 1, 31),
    )
    spending = spending_by_category_report(
        owner.user,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
    )
    assert cash.periods[0].spending_minor == 5_000
    assert cash.periods[0].income_minor == 0
    assert not cash.periods[0].missing_import
    account_ids = {item.pk for item in cash.accounts}
    spending_ids = {item.pk for item in spending.accounts}
    assert house.pk not in account_ids
    assert loan.pk not in account_ids
    assert spending.total_spending_minor == 5_000
    assert house.pk not in spending_ids
    assert loan.pk not in spending_ids


@pytest.mark.django_db
def test_export_includes_visible_assets_and_omits_other_private():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    house = make_account(
        owner,
        name="Household House",
        account_type=Account.Type.REAL_ESTATE,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    loan = make_account(
        owner,
        name="Household Mortgage",
        account_type=Account.Type.LOAN,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    secret = make_account(owner, name=SECRET_HOUSE, account_type=Account.Type.VEHICLE)
    member_gold = make_account(member, name=MEMBER_GOLD, account_type=Account.Type.PRECIOUS_METALS)
    add_snapshot(secret, date(2026, 2, 1), 1_200_000, note="online estimate")
    loan.secured_asset = house
    loan.save()

    owner_tables = collect_export_tables(owner)
    member_tables = collect_export_tables(member)
    owner_names = {row["name"] for row in owner_tables["accounts"]}
    member_names = {row["name"] for row in member_tables["accounts"]}
    owner_loan = next(row for row in owner_tables["accounts"] if row["name"] == "Household Mortgage")

    assert SECRET_HOUSE in owner_names
    assert SECRET_HOUSE not in member_names
    assert MEMBER_GOLD not in owner_names
    assert MEMBER_GOLD in member_names
    assert owner_loan["secured_asset_id"] == house.pk
    owner_notes = {row["note"] for row in owner_tables["balance_snapshots"]}
    member_notes = {row["note"] for row in member_tables["balance_snapshots"]}
    assert "online estimate" in owner_notes
    assert "online estimate" not in member_notes


@pytest.mark.django_db
def test_loan_pairing_form_only_lists_same_scope_assets():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private_house = make_account(owner, name="Private House", account_type=Account.Type.REAL_ESTATE)
    household_house = make_account(
        owner,
        name="Household House",
        account_type=Account.Type.REAL_ESTATE,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    loan = make_account(
        owner,
        name="Household Mortgage",
        account_type=Account.Type.LOAN,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    page = signed_in(owner).get(reverse("account-balances", args=[loan.pk]))
    html = page.content.decode()
    assert "Household House" in html
    assert "Private House" not in html
    paired = signed_in(owner).post(
        reverse("account-pair-loan", args=[loan.pk]),
        {"secured_asset": household_house.pk},
    )
    assert paired.status_code == 302
    loan.refresh_from_db()
    assert loan.secured_asset_id == household_house.pk
    rejected = signed_in(owner).post(
        reverse("account-pair-loan", args=[loan.pk]),
        {"secured_asset": private_house.pk},
    )
    assert rejected.status_code == 302
    loan.refresh_from_db()
    assert loan.secured_asset_id == household_house.pk
