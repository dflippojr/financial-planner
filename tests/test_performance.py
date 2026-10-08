from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from finance.models import Account, BalanceSnapshot, Household, Membership, Person
from finance.performance import account_performance

PASSWORD = "Synthetic-passphrase-42!"
SECRET_INVESTMENT = "Owner Secret Brokerage"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def make_account(owner, *, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def statement(account, snapshot_date, amount_minor, net_contribution_minor):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
        net_contribution_minor=net_contribution_minor,
    )


def manual_balance(account, snapshot_date, amount_minor):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
    )


def simplefin_balance(account, snapshot_date, amount_minor):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=BalanceSnapshot.Source.SIMPLEFIN,
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_database_rejects_net_contribution_on_simplefin_snapshot():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)

    snapshot = BalanceSnapshot(
        account=account,
        snapshot_date=date(2026, 1, 31),
        amount_minor=10_000,
        currency="USD",
        source=BalanceSnapshot.Source.SIMPLEFIN,
        net_contribution_minor=0,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        snapshot.save()


@pytest.mark.django_db
def test_single_period_with_a_contribution():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 1, 31), 10_000, 0)
    statement(account, date(2026, 2, 28), 11_000, 500)

    result = account_performance(account, date(2026, 2, 28))

    assert len(result.periods) == 1
    period = result.periods[0]
    assert period.growth_minor == 500
    assert period.return_display == "4.9%"


@pytest.mark.django_db
def test_negative_contribution_is_a_withdrawal():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 1, 31), 20_000, 0)
    statement(account, date(2026, 2, 28), 18_000, -3_000)

    result = account_performance(account, date(2026, 2, 28))

    period = result.periods[0]
    assert period.contribution_minor == -3_000
    assert period.growth_minor == 1_000
    assert period.return_display == "5.4%"


@pytest.mark.django_db
def test_two_linked_periods_compound_their_returns():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 1, 31), 10_000, 0)
    statement(account, date(2026, 2, 28), 11_000, 500)
    statement(account, date(2026, 3, 31), 12_100, 0)

    result = account_performance(account, date(2026, 3, 31))

    assert len(result.periods) == 2
    all_time = result.summaries.all_time
    assert all_time.contributions_minor == 500
    assert all_time.growth_minor == 1_600
    assert all_time.return_display == "15.4%"


@pytest.mark.django_db
def test_ytd_starting_from_an_entry_before_january_first():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2025, 11, 30), 10_000, 0)
    statement(account, date(2026, 1, 31), 10_500, 200)

    result = account_performance(account, date(2026, 6, 30))

    ytd = result.summaries.ytd
    assert ytd.partial is False
    assert ytd.start_date == date(2025, 11, 30)
    assert ytd.contributions_minor == 200
    assert ytd.growth_minor == 300
    assert ytd.return_display == "3.0%"


@pytest.mark.django_db
def test_ytd_is_partial_when_no_entry_precedes_the_year():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 2, 15), 5_000, 0)
    statement(account, date(2026, 3, 15), 5_300, 200)

    result = account_performance(account, date(2026, 6, 30))

    ytd = result.summaries.ytd
    assert ytd.partial is True
    assert ytd.start_date == date(2026, 2, 15)
    assert ytd.growth_minor == 100
    assert ytd.return_display == "2.0%"


@pytest.mark.django_db
def test_period_with_non_positive_average_balance_has_no_return():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 1, 31), 1_000, 0)
    statement(account, date(2026, 2, 28), 200, -3_000)

    result = account_performance(account, date(2026, 2, 28))

    period = result.periods[0]
    assert period.return_fraction is None
    assert period.return_display is None
    assert period.growth_minor == 2_200


@pytest.mark.django_db
def test_account_with_fewer_than_two_statement_entries_shows_value_change_only():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 1, 31), 10_000, 0)

    result = account_performance(account, date(2026, 2, 28))

    assert result.periods == []
    for summary in (result.summaries.ytd, result.summaries.one_year, result.summaries.all_time):
        assert summary.has_data is False
        assert summary.return_display is None


@pytest.mark.django_db
def test_missing_return_in_the_chain_forces_value_change_only_for_the_range():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 1, 31), 1_000, 0)
    statement(account, date(2026, 2, 28), 200, -3_000)  # non-positive average -> no return
    statement(account, date(2026, 3, 31), 400, 0)

    result = account_performance(account, date(2026, 3, 31))

    all_time = result.summaries.all_time
    assert all_time.has_data is True
    assert all_time.return_display is None
    assert all_time.growth_minor == 2_200 + 200
    assert "value change only" in all_time.note


@pytest.mark.django_db
def test_simplefin_snapshots_between_statements_do_not_split_periods():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    statement(account, date(2026, 1, 31), 10_000, 0)
    simplefin_balance(account, date(2026, 2, 10), 10_200)
    simplefin_balance(account, date(2026, 2, 20), 10_300)
    manual_balance(account, date(2026, 2, 25), 10_400)  # manual but no contribution recorded
    statement(account, date(2026, 2, 28), 11_000, 500)

    result = account_performance(account, date(2026, 2, 28))

    assert len(result.periods) == 1
    period = result.periods[0]
    assert period.start_value_minor == 10_000
    assert period.end_value_minor == 11_000


@pytest.mark.django_db
def test_manual_balance_form_shows_net_contribution_only_for_investment_accounts():
    owner = make_person("owner")
    make_household(owner)
    investment = make_account(owner, name="Brokerage", account_type=Account.Type.INVESTMENT)
    checking = make_account(owner, name="Checking", account_type=Account.Type.CHECKING)
    client = signed_in(owner)

    investment_page = client.get(reverse("account-balances", args=[investment.pk]))
    checking_page = client.get(reverse("account-balances", args=[checking.pk]))

    assert "Net contributions this period" in investment_page.content.decode()
    assert "Net contributions this period" not in checking_page.content.decode()


@pytest.mark.django_db
def test_recording_a_statement_entry_parses_net_contribution():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    client = signed_in(owner)

    response = client.post(
        reverse("account-balances", args=[account.pk]),
        {"snapshot_date": "2026-01-31", "amount": "100.00", "net_contribution": "5.00"},
    )

    assert response.status_code == 302
    snapshot = BalanceSnapshot.objects.get(account=account)
    assert snapshot.net_contribution_minor == 500


@pytest.mark.django_db
def test_private_investment_account_performance_is_never_visible_to_another_member():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = make_account(owner, name=SECRET_INVESTMENT, scope=Account.Scope.PRIVATE)
    statement(private, date(2026, 1, 31), 10_000, 0)
    statement(private, date(2026, 2, 28), 11_000, 500)

    member_client = signed_in(member)
    missing_id = private.pk + 999

    account_page = member_client.get(reverse("account-balances", args=[private.pk]))
    missing_page = member_client.get(reverse("account-balances", args=[missing_id]))
    assert account_page.status_code == missing_page.status_code == 404
    assert account_page.content == missing_page.content

    net_worth_page = member_client.get(reverse("net-worth"))
    assert SECRET_INVESTMENT not in net_worth_page.content.decode()
