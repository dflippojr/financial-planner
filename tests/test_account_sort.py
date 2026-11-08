"""Sorting the balance-by-account lists on Net worth and Accounts (#655)."""
import re
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.account_sort import sort_rows
from finance.lifecycle_services import delete_member_data
from finance.models import Account, AccountSortSettings, BalanceSnapshot
from finance.net_worth import net_worth_report
from tests.test_net_worth import add_snapshot, make_account, make_household, make_person, signed_in

MANUAL = BalanceSnapshot.Source.MANUAL
SIMPLEFIN = BalanceSnapshot.Source.SIMPLEFIN
ORDERS = ("name_asc", "name_desc", "balance_desc", "balance_asc")

# Checking: two equal balances (beta, Delta), one smaller, one with no balance.
# Credit cards: one owed amount recorded by hand (positive) and one from SimpleFIN
# (negative); both count as debts. Card B owes the most, so it is "lowest".
CHECKING = {"beta": 30_000, "Alpha": 10_000, "gamma": None, "Delta": 30_000}
CARDS = {"Card B": (50_000, MANUAL), "card a": (-20_000, SIMPLEFIN), "Card C": None}

ACCOUNTS_EXPECTED = {
    "name_asc": (["Alpha", "beta", "Delta", "gamma"], ["card a", "Card B", "Card C"]),
    "name_desc": (["gamma", "Delta", "beta", "Alpha"], ["Card C", "Card B", "card a"]),
    "balance_desc": (["beta", "Delta", "Alpha", "gamma"], ["card a", "Card B", "Card C"]),
    "balance_asc": (["Alpha", "beta", "Delta", "gamma"], ["Card B", "card a", "Card C"]),
}
# Accounts with no balance are not on the Net worth panel at all.
NET_WORTH_EXPECTED = {
    "name_asc": ["Alpha", "beta", "card a", "Card B", "Delta"],
    "name_desc": ["Delta", "Card B", "card a", "beta", "Alpha"],
    "balance_desc": ["beta", "Delta", "Alpha", "card a", "Card B"],
    "balance_asc": ["Card B", "card a", "Alpha", "beta", "Delta"],
}


def _books(owner, *, scope=Account.Scope.PRIVATE, household=None):
    today = timezone.localdate()
    for name, amount in CHECKING.items():
        account = make_account(owner, name=name, scope=scope, household=household)
        if amount is not None:
            add_snapshot(account, today, amount, MANUAL)
    for name, balance in CARDS.items():
        account = make_account(
            owner, name=name, account_type=Account.Type.CREDIT_CARD, scope=scope, household=household
        )
        if balance is not None:
            add_snapshot(account, today, balance[0], balance[1])


def _owner_with_books():
    owner = make_person("owner")
    make_household(owner)
    _books(owner)
    return owner


def _account_groups(client):
    """The Accounts page as [(type heading, [account names])] in page order."""
    html = client.get(reverse("account-list")).content.decode()
    groups = []
    for body in re.findall(r"<tbody>(.*?)</tbody>", html, re.S):
        heading = re.search(r'uppercase tracking-wide">([^<]+)</td>', body)
        if heading is None:
            continue
        names = re.findall(r'<span class="font-semibold break-words">([^<]+)</span>', body)
        groups.append((heading.group(1), names))
    return groups


def _panel_names(client, query=""):
    html = client.get(reverse("net-worth") + query).content.decode()
    panel = html.split('id="net-worth-accounts"', 1)[1].split("</section>", 1)[0]
    return re.findall(r"<li [^>]*>\s*<span [^>]*>\s*<span>([^<]+)", panel)


def _save(client, page, sort, **extra):
    return client.post(reverse("account-sort"), {"page": page, "sort": sort, **extra})


def _row(name, balance=None):
    return SimpleNamespace(name=name, balance=balance)


def _sorted_names(rows, order):
    return [row.name for row in sort_rows(rows, order, name=lambda row: row.name, balance=lambda row: row.balance)]


def test_name_sort_ignores_case_and_keeps_equal_names_in_their_given_order():
    rows = [_row("b", 1), _row("A", 2), _row("a", 3), _row("B", 4)]
    ascending = sort_rows(rows, "name_asc", name=lambda row: row.name, balance=lambda row: row.balance)
    descending = sort_rows(rows, "name_desc", name=lambda row: row.name, balance=lambda row: row.balance)
    assert [(row.name, row.balance) for row in ascending] == [("A", 2), ("a", 3), ("b", 1), ("B", 4)]
    assert [(row.name, row.balance) for row in descending] == [("b", 1), ("B", 4), ("A", 2), ("a", 3)]


def test_balance_ties_fall_back_to_name_and_missing_balances_go_last_both_ways():
    rows = [_row("zed", 5), _row("none-b"), _row("Mid", 5), _row("none-A"), _row("low", -7), _row("top", 9)]
    assert _sorted_names(rows, "balance_desc") == ["top", "Mid", "zed", "low", "none-A", "none-b"]
    assert _sorted_names(rows, "balance_asc") == ["low", "Mid", "zed", "top", "none-A", "none-b"]


def test_blank_order_keeps_the_given_order():
    rows = [_row("b", 1), _row("a"), _row("C", -1)]
    assert _sorted_names(rows, "") == ["b", "a", "C"]


@pytest.mark.django_db
def test_accounts_page_defaults_to_todays_order_with_no_saved_choice():
    owner = _owner_with_books()
    client = signed_in(owner)
    groups = _account_groups(client)
    assert [label for label, _names in groups] == ["Checking", "Credit card"]
    # Today's order is the database's name order; collation differs between SQLite and PostgreSQL.
    expected = [
        list(Account.objects.filter(account_type=kind).order_by("name", "pk").values_list("name", flat=True))
        for kind in (Account.Type.CHECKING, Account.Type.CREDIT_CARD)
    ]
    assert [names for _label, names in groups] == expected
    assert not AccountSortSettings.objects.exists()
    html = client.get(reverse("account-list")).content.decode()
    assert '<option value="" selected>Default order</option>' in html


@pytest.mark.django_db
@pytest.mark.parametrize("order", ORDERS)
def test_accounts_page_sorts_inside_each_type_group(order):
    owner = _owner_with_books()
    client = signed_in(owner)
    response = _save(client, "accounts", order)
    assert response.status_code == 302
    assert response["Location"] == reverse("account-list")
    checking, cards = ACCOUNTS_EXPECTED[order]
    assert _account_groups(client) == [("Checking", checking), ("Credit card", cards)]
    html = client.get(reverse("account-list")).content.decode()
    assert f'<option value="{order}" selected>' in html


@pytest.mark.django_db
def test_net_worth_panel_defaults_to_the_report_order():
    owner = _owner_with_books()
    client = signed_in(owner)
    today = timezone.localdate()
    report = net_worth_report(owner.user, date_from=today.replace(day=1), date_to=today)
    assert _panel_names(client) == [row.account_name for row in report.periods[-1].accounts]


@pytest.mark.django_db
@pytest.mark.parametrize("order", ORDERS)
def test_net_worth_panel_sorts_by_name_or_signed_balance(order):
    owner = _owner_with_books()
    client = signed_in(owner)
    response = _save(client, "net_worth", order)
    assert response.status_code == 302
    assert response["Location"] == reverse("net-worth") + "#net-worth-accounts"
    assert _panel_names(client) == NET_WORTH_EXPECTED[order]


@pytest.mark.django_db
def test_balance_sort_counts_debts_as_negative_not_by_size():
    owner = _owner_with_books()
    client = signed_in(owner)
    _save(client, "net_worth", "balance_desc")
    names = _panel_names(client)
    # Card B owes $500, more than Alpha's $100 asset, yet sorts last: the panel's signed value.
    assert names[-1] == "Card B"
    assert names.index("Alpha") < names.index("card a")
    html = client.get(reverse("net-worth")).content.decode()
    assert "\N{MINUS SIGN}$500.00" in html


@pytest.mark.django_db
def test_saved_sort_survives_reload_and_another_device_and_pages_stay_separate():
    owner = _owner_with_books()
    phone = signed_in(owner)
    _save(phone, "accounts", "name_desc")
    laptop = Client()
    laptop.force_login(owner.user)
    expected = [("Checking", ACCOUNTS_EXPECTED["name_desc"][0]), ("Credit card", ACCOUNTS_EXPECTED["name_desc"][1])]
    assert _account_groups(phone) == expected
    assert _account_groups(laptop) == expected
    # The Net worth page keeps its own (default) order.
    assert AccountSortSettings.objects.get(person=owner).net_worth_sort == ""
    default_panel = _panel_names(laptop)
    _save(laptop, "net_worth", "balance_asc")
    assert _panel_names(phone) == NET_WORTH_EXPECTED["balance_asc"] != default_panel
    assert _account_groups(phone) == expected
    settings = AccountSortSettings.objects.get(person=owner)
    assert (settings.accounts_sort, settings.net_worth_sort) == ("name_desc", "balance_asc")


@pytest.mark.django_db
def test_choosing_default_order_clears_the_saved_choice():
    owner = _owner_with_books()
    client = signed_in(owner)
    _save(client, "accounts", "name_desc")
    _save(client, "accounts", "")
    assert AccountSortSettings.objects.get(person=owner).accounts_sort == ""
    html = client.get(reverse("account-list")).content.decode()
    assert '<option value="" selected>Default order</option>' in html


@pytest.mark.django_db
def test_another_members_saved_sort_never_changes_this_members_view():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    _books(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    owner_client = signed_in(owner)
    member_client = signed_in(member)
    member_accounts = _account_groups(member_client)
    member_panel = _panel_names(member_client)
    _save(owner_client, "accounts", "name_desc")
    _save(owner_client, "net_worth", "balance_asc")
    assert _account_groups(owner_client) != member_accounts
    assert _panel_names(owner_client) == NET_WORTH_EXPECTED["balance_asc"]
    assert _account_groups(member_client) == member_accounts
    assert _panel_names(member_client) == member_panel
    assert not AccountSortSettings.objects.filter(person=member).exists()
    _save(member_client, "accounts", "balance_desc")
    assert AccountSortSettings.objects.get(person=owner).accounts_sort == "name_desc"


@pytest.mark.django_db
def test_net_worth_sort_returns_to_the_same_range_and_drops_filters_that_do_not_validate():
    owner = _owner_with_books()
    client = signed_in(owner)
    kept = _save(client, "net_worth", "name_asc", date_from="2026-01-01", date_to="2026-03-31", scope="household")
    assert kept["Location"] == reverse("net-worth") + "?date_from=2026-01-01&date_to=2026-03-31&scope=household#net-worth-accounts"
    dropped = _save(client, "net_worth", "name_asc", date_from="not-a-date", scope="household&evil=1")
    assert dropped["Location"] == reverse("net-worth") + "#net-worth-accounts"
    accounts = _save(client, "accounts", "name_asc", date_from="2026-01-01")
    assert accounts["Location"] == reverse("account-list")


@pytest.mark.django_db
def test_net_worth_sort_form_carries_the_current_range():
    owner = _owner_with_books()
    today = timezone.localdate()
    query = f"?date_from={today.replace(day=1).isoformat()}&date_to={today.isoformat()}&scope="
    html = signed_in(owner).get(reverse("net-worth") + query).content.decode()
    form = html.split('data-account-sort="net_worth"', 1)[1].split("</form>", 1)[0]
    assert f'name="date_from" value="{today.replace(day=1).isoformat()}"' in form
    assert f'name="date_to" value="{today.isoformat()}"' in form
    assert 'name="scope"' not in form


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data",
    [{"page": "accounts", "sort": "balance"}, {"page": "transactions", "sort": "name_asc"}, {"sort": "name_asc"}],
)
def test_unknown_page_or_order_is_refused_and_saves_nothing(data):
    owner = _owner_with_books()
    response = signed_in(owner).post(reverse("account-sort"), data)
    assert response.status_code == 400
    assert not AccountSortSettings.objects.exists()


@pytest.mark.django_db
def test_sort_save_takes_only_a_csrf_checked_post_from_a_signed_in_member():
    owner = _owner_with_books()
    # A GET only returns to the accounts page (#705).
    got = signed_in(owner).get(reverse("account-sort"), {"page": "accounts", "sort": "name_desc"})
    assert got.status_code == 302 and got["Location"] == reverse("account-list")
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(owner.user)
    assert strict.post(reverse("account-sort"), {"page": "accounts", "sort": "name_desc"}).status_code == 403
    anonymous = Client().post(reverse("account-sort"), {"page": "accounts", "sort": "name_desc"})
    assert anonymous.status_code == 302 and anonymous["Location"].startswith(reverse("login"))
    assert not AccountSortSettings.objects.exists()


@pytest.mark.django_db
def test_deleting_member_data_removes_their_sort_choices():
    owner = _owner_with_books()
    _save(signed_in(owner), "accounts", "name_desc")
    delete_member_data(owner)
    assert not AccountSortSettings.objects.exists()
