from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.models import Account, BalanceSnapshot, Household, Membership, Person
from finance.net_worth import contribution_parts, net_worth_chart_data, net_worth_report
from tests.page_payload import json_script_payload


PASSWORD = "Synthetic-passphrase-42!"
SECRET_CHECKING = "Owner Secret Vault"


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


def add_snapshot(account, snapshot_date, amount_minor, source, note=""):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=source,
        note=note,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def missing_id(account):
    return account.pk + 999


def assert_same_404(response, missing_response):
    assert response.status_code == missing_response.status_code == 404
    assert response.content == missing_response.content


def _hand_checked_books(owner, member, household):
    """Synthetic ledger whose month-end math is checked by hand.

    January 31: checking 1000.00, SimpleFIN card 200.00 owed → NW 800.00
    February 28: checking carried 1000.00, card carried 200.00, invest 5000.00 → NW 5800.00
    March 31: plus owner's private manual card 500.00 owed → NW 5300.00
    """
    checking = make_account(owner, name="Synthetic Checking", household=household, scope=Account.Scope.HOUSEHOLD)
    sf_card = make_account(
        owner,
        name="Synthetic SimpleFIN Card",
        account_type=Account.Type.CREDIT_CARD,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    invest = make_account(
        owner,
        name="Synthetic Brokerage",
        account_type=Account.Type.INVESTMENT,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
    )
    private_card = make_account(
        owner,
        name=SECRET_CHECKING,
        account_type=Account.Type.CREDIT_CARD,
    )
    member_private = make_account(member, name="Member Private Cash")
    add_snapshot(checking, date(2026, 1, 15), 100_000, BalanceSnapshot.Source.MANUAL)
    add_snapshot(sf_card, date(2026, 1, 31), -20_000, BalanceSnapshot.Source.SIMPLEFIN)
    add_snapshot(invest, date(2026, 2, 28), 500_000, BalanceSnapshot.Source.MANUAL)
    add_snapshot(private_card, date(2026, 3, 15), 50_000, BalanceSnapshot.Source.MANUAL)
    add_snapshot(member_private, date(2026, 1, 10), 10_000, BalanceSnapshot.Source.MANUAL)
    return checking, sf_card, invest, private_card, member_private


@pytest.mark.django_db
def test_hand_checked_monthly_series_carry_forward_and_credit_cards():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    _hand_checked_books(owner, member, household)

    report = net_worth_report(
        owner.user,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 31),
        today=date(2026, 3, 31),
    )
    january, february, march = report.periods

    assert january.net_minor == 80_000
    assert january.assets_minor == 100_000
    assert january.liabilities_minor == 20_000
    assert january.omitted_untracked == 2
    jan_by_name = {row.account_name: row for row in january.accounts}
    assert jan_by_name["Synthetic Checking"].source_badge == "manual"
    assert jan_by_name["Synthetic SimpleFIN Card"].source_badge == "simplefin"
    assert "Synthetic Brokerage" not in jan_by_name
    assert SECRET_CHECKING not in jan_by_name

    assert february.net_minor == 580_000
    assert february.assets_minor == 600_000
    assert february.liabilities_minor == 20_000
    feb_by_name = {row.account_name: row for row in february.accounts}
    assert feb_by_name["Synthetic Checking"].source_badge == "carried forward"
    assert feb_by_name["Synthetic SimpleFIN Card"].source_badge == "carried forward"
    assert feb_by_name["Synthetic Brokerage"].source_badge == "manual"
    assert SECRET_CHECKING not in feb_by_name

    assert march.net_minor == 530_000
    assert march.assets_minor == 600_000
    assert march.liabilities_minor == 70_000
    mar_by_name = {row.account_name: row for row in march.accounts}
    assert mar_by_name[SECRET_CHECKING].source_badge == "manual"
    assert mar_by_name[SECRET_CHECKING].liabilities_minor == 50_000
    assert report.summary.net_minor == 530_000
    assert report.summary.month_change.minor == -50_000
    assert report.summary.range_change.minor == 450_000

    chart = net_worth_chart_data(report)
    for period, payload in zip(report.periods, chart["periods"], strict=True):
        assert payload["assets_minor"] == period.assets_minor
        assert payload["liabilities_minor"] == period.liabilities_minor
        assert payload["net_minor"] == period.net_minor
        assert [row["account_name"] for row in payload["accounts"]] == [row.account_name for row in period.accounts]
        assert [row["amount_minor"] for row in payload["accounts"]] == [row.amount_minor for row in period.accounts]


@pytest.mark.django_db
def test_simplefin_snapshot_wins_over_manual_on_the_same_date():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    add_snapshot(checking, date(2026, 1, 15), 1_000, BalanceSnapshot.Source.MANUAL)
    add_snapshot(checking, date(2026, 1, 15), 9_000, BalanceSnapshot.Source.SIMPLEFIN)
    report = net_worth_report(
        owner.user,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        today=date(2026, 1, 31),
    )
    assert report.periods[0].net_minor == 9_000
    assert report.periods[0].accounts[0].source == "simplefin"


@pytest.mark.django_db
def test_contribution_parts_match_documented_signs():
    assert contribution_parts(Account.Type.CHECKING, "manual", 100) == (100, 0)
    assert contribution_parts(Account.Type.CREDIT_CARD, "simplefin", -200) == (0, 200)
    assert contribution_parts(Account.Type.CREDIT_CARD, "manual", 200) == (0, 200)
    assert contribution_parts(Account.Type.CREDIT_CARD, "simplefin", 50) == (50, 0)
    assert contribution_parts(Account.Type.CREDIT_CARD, "manual", -50) == (50, 0)


@pytest.mark.django_db
def test_net_worth_page_hides_other_member_private_accounts():
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    make_household(outsider, name="Other Household")
    _hand_checked_books(owner, member, household)

    owner_page = signed_in(owner).get(
        reverse("net-worth"),
        {"date_from": "2026-01-01", "date_to": "2026-03-31"},
    )
    member_page = signed_in(member).get(
        reverse("net-worth"),
        {"date_from": "2026-01-01", "date_to": "2026-03-31"},
    )
    household_only = signed_in(owner).get(
        reverse("net-worth"),
        {"date_from": "2026-01-01", "date_to": "2026-03-31", "scope": "household"},
    )
    outsider_page = signed_in(outsider).get(
        reverse("net-worth"),
        {"date_from": "2026-01-01", "date_to": "2026-03-31"},
    )

    owner_html = owner_page.content.decode()
    member_html = member_page.content.decode()
    household_html = household_only.content.decode()
    outsider_html = outsider_page.content.decode()
    owner_chart = json_script_payload(owner_html, "net-worth-chart-data")
    member_chart = json_script_payload(member_html, "net-worth-chart-data")

    assert owner_page.status_code == member_page.status_code == 200
    assert SECRET_CHECKING in owner_html
    assert SECRET_CHECKING not in member_html
    assert SECRET_CHECKING not in household_html
    assert SECRET_CHECKING not in outsider_html
    assert "Member Private Cash" not in owner_html
    assert "Member Private Cash" in member_html
    assert any(SECRET_CHECKING == row["account_name"] for period in owner_chart["periods"] for row in period["accounts"])
    assert all(SECRET_CHECKING != row["account_name"] for period in member_chart["periods"] for row in period["accounts"])
    assert member_chart["summary"]["net_minor"] == 590_000
    assert household_only.context["report"].summary.net_minor == 580_000
    assert outsider_page.context["report"].summary.net_minor == 0
    assert owner_chart["periods"][-1]["net_minor"] == owner_page.context["report"].periods[-1].net_minor


@pytest.mark.django_db
def test_manual_snapshots_record_edit_delete_and_never_overwrite_simplefin():
    owner = make_person("owner")
    member = make_person("member")
    outsider = make_person("outsider")
    household = make_household(owner, member)
    make_household(outsider, name="Other Household")
    shared = make_account(owner, name="Shared Cash", scope=Account.Scope.HOUSEHOLD, household=household)
    private = make_account(owner, name=SECRET_CHECKING)
    simplefin_row = add_snapshot(shared, date(2026, 1, 20), 77_000, BalanceSnapshot.Source.SIMPLEFIN)

    owner_client = signed_in(owner)
    member_client = signed_in(member)
    outsider_client = signed_in(outsider)

    created = member_client.post(
        reverse("account-balances", args=[shared.pk]),
        {"snapshot_date": "2026-01-20", "amount": "12.34", "note": "synthetic note"},
    )
    assert created.status_code == 302
    manual = BalanceSnapshot.objects.get(account=shared, source=BalanceSnapshot.Source.MANUAL)
    simplefin_row.refresh_from_db()
    assert simplefin_row.amount_minor == 77_000
    assert manual.amount_minor == 1234
    assert manual.note == "synthetic note"

    edited = member_client.post(
        reverse("account-snapshot-edit", args=[shared.pk, manual.pk]),
        {"snapshot_date": "2026-01-21", "amount": "45.00", "note": "edited"},
    )
    assert edited.status_code == 302
    manual.refresh_from_db()
    assert manual.snapshot_date == date(2026, 1, 21)
    assert manual.amount_minor == 4500

    private_create = owner_client.post(
        reverse("account-balances", args=[private.pk]),
        {"snapshot_date": "2026-02-01", "amount": "9.00"},
    )
    assert private_create.status_code == 302
    private_snap = BalanceSnapshot.objects.get(account=private, source=BalanceSnapshot.Source.MANUAL)

    member_private = member_client.get(reverse("account-balances", args=[private.pk]))
    outsider_shared = outsider_client.get(reverse("account-balances", args=[shared.pk]))
    missing = member_client.get(reverse("account-balances", args=[missing_id(private)]))
    assert_same_404(member_private, missing)
    assert_same_404(outsider_shared, missing)

    member_edit_private = member_client.post(
        reverse("account-snapshot-edit", args=[private.pk, private_snap.pk]),
        {"snapshot_date": "2026-02-01", "amount": "1.00"},
    )
    outsider_delete = outsider_client.post(reverse("account-snapshot-delete", args=[shared.pk, manual.pk]))
    simplefin_edit = member_client.get(reverse("account-snapshot-edit", args=[shared.pk, simplefin_row.pk]))
    assert_same_404(member_edit_private, missing)
    assert_same_404(outsider_delete, missing)
    assert_same_404(simplefin_edit, missing)
    assert SECRET_CHECKING not in member_private.content.decode()
    assert SECRET_CHECKING not in outsider_shared.content.decode()

    deleted = member_client.post(reverse("account-snapshot-delete", args=[shared.pk, manual.pk]))
    assert deleted.status_code == 302
    assert not BalanceSnapshot.objects.filter(pk=manual.pk).exists()
    assert BalanceSnapshot.objects.filter(pk=simplefin_row.pk).exists()


@pytest.mark.django_db
def test_record_balance_rejects_future_dates(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    monkeypatch.setattr(timezone, "localdate", lambda: date(2026, 4, 1))
    response = signed_in(owner).post(
        reverse("account-balances", args=[account.pk]),
        {"snapshot_date": "2026-04-02", "amount": "1.00"},
    )
    assert response.status_code == 200
    assert "Balance date cannot be in the future." in response.content.decode()
    assert not BalanceSnapshot.objects.exists()


@pytest.mark.django_db
def test_accounts_page_links_to_record_balance():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_snapshot(account, date(2026, 1, 2), 250, BalanceSnapshot.Source.MANUAL)
    page = signed_in(owner).get(reverse("account-list"))
    content = page.content.decode()
    assert "Record balance" in content
    assert reverse("account-balances", args=[account.pk]) in content
    assert "+$2.50" in content
