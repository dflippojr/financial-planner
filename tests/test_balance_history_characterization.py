"""Pin snapshot selection in net worth and debt planning before they share one history index (#586).

Every account and balance is synthetic. Expected amounts are written out by hand.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from finance.balance_history import last_on_or_before, ordered_history
from finance.debt_planner_services import list_visible_debts
from finance.models import Account, BalanceSnapshot, Household, Membership, Person
from finance.net_worth import net_worth_report

PASSWORD = "Synthetic-passphrase-42!"
MANUAL = BalanceSnapshot.Source.MANUAL
SIMPLEFIN = BalanceSnapshot.Source.SIMPLEFIN
CARD = Account.Type.CREDIT_CARD
LOAN = Account.Type.LOAN


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def make_account(owner, name, account_type=CARD, *, household=None):
    scope = Account.Scope.HOUSEHOLD if household else Account.Scope.PRIVATE
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if household else "",
    )


def add_snapshot(account, snapshot_date, amount_minor, source=MANUAL):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=source,
    )


def report(person, date_from, date_to, **kwargs):
    return net_worth_report(person.user, date_from=date_from, date_to=date_to, today=date_to, **kwargs)


def known(period):
    return {row.account_name: (row.snapshot_date, row.source, row.liabilities_minor) for row in period.accounts}


def debts(person, today):
    return {row.name: (row.snapshot_date, row.owed_minor) for row in list_visible_debts(person.user, today=today)}


def snapshot_queries(captured):
    table = f'"{BalanceSnapshot._meta.db_table}"'
    return [query["sql"] for query in captured.captured_queries if f"FROM {table}" in query["sql"]]


@pytest.mark.django_db
def test_later_date_wins_and_simplefin_wins_on_the_same_date():
    owner = make_person("owner")
    make_household(owner)
    card = make_account(owner, "Synthetic Card")
    add_snapshot(card, date(2026, 1, 5), -30_000, SIMPLEFIN)
    add_snapshot(card, date(2026, 1, 20), 12_000, MANUAL)
    add_snapshot(card, date(2026, 2, 10), -6_500, SIMPLEFIN)
    add_snapshot(card, date(2026, 2, 10), 7_000, MANUAL)  # inserted later, so a higher pk

    january, february = report(owner, date(2026, 1, 1), date(2026, 2, 28)).periods

    assert known(january) == {"Synthetic Card": (date(2026, 1, 20), "manual", 12_000)}
    assert known(february) == {"Synthetic Card": (date(2026, 2, 10), "simplefin", 6_500)}
    assert (january.net_minor, february.net_minor) == (-12_000, -6_500)
    assert debts(owner, date(2026, 1, 25)) == {"Synthetic Card": (date(2026, 1, 20), 12_000)}
    assert debts(owner, date(2026, 2, 15)) == {"Synthetic Card": (date(2026, 2, 10), 6_500)}


@pytest.mark.django_db
def test_cutoff_day_counts_future_is_ignored_and_carried_snapshot_is_selected():
    owner = make_person("owner")
    make_household(owner)
    loan = make_account(owner, "Synthetic Loan", LOAN)
    card = make_account(owner, "Synthetic Card")
    add_snapshot(loan, date(2026, 1, 15), 80_000)
    add_snapshot(card, date(2026, 3, 31), -20_000, SIMPLEFIN)
    add_snapshot(card, date(2026, 4, 1), -99_999, SIMPLEFIN)

    march = report(owner, date(2026, 1, 1), date(2026, 3, 31)).periods[-1]

    assert known(march) == {
        "Synthetic Card": (date(2026, 3, 31), "simplefin", 20_000),
        "Synthetic Loan": (date(2026, 1, 15), "manual", 80_000),
    }
    assert [(row.account_name, row.carried_forward) for row in march.accounts] == [
        ("Synthetic Card", False),
        ("Synthetic Loan", True),
    ]
    assert (march.liabilities_minor, march.net_minor, march.carried_forward) == (100_000, -100_000, 1)
    assert debts(owner, date(2026, 3, 31)) == {
        "Synthetic Card": (date(2026, 3, 31), 20_000),
        "Synthetic Loan": (date(2026, 1, 15), 80_000),
    }


@pytest.mark.django_db
def test_account_without_an_eligible_snapshot_stays_unknown_not_zero():
    owner = make_person("owner")
    make_household(owner)
    make_account(owner, "Synthetic Empty Card")
    future = make_account(owner, "Synthetic Future Loan", LOAN)
    add_snapshot(future, date(2026, 4, 1), 50_000)

    result = report(owner, date(2026, 3, 1), date(2026, 3, 31))
    march = result.periods[0]

    assert result.has_snapshots is False
    assert march.accounts == []
    assert (march.omitted_untracked, march.liabilities_minor, march.net_minor) == (2, 0, 0)
    rows = {row.name: row for row in list_visible_debts(owner.user, today=date(2026, 3, 31))}
    assert set(rows) == {"Synthetic Empty Card", "Synthetic Future Loan"}
    for row in rows.values():
        assert (row.owed_minor, row.owed_display, row.snapshot_date, row.ready) == (None, "", None, False)
        assert "balance" in row.missing


@pytest.mark.django_db
def test_private_snapshots_of_another_person_never_count_and_ended_membership_hides_household():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    own_card = make_account(owner, "Synthetic Own Card")
    member_card = make_account(member, "Synthetic Member Card")
    shared_loan = make_account(member, "Synthetic Shared Loan", LOAN, household=household)
    add_snapshot(own_card, date(2026, 3, 10), 1_000)
    add_snapshot(member_card, date(2026, 3, 10), 777_700)
    add_snapshot(shared_loan, date(2026, 3, 12), 40_000)

    march = report(owner, date(2026, 3, 1), date(2026, 3, 31)).periods[0]
    assert known(march) == {
        "Synthetic Own Card": (date(2026, 3, 10), "manual", 1_000),
        "Synthetic Shared Loan": (date(2026, 3, 12), "manual", 40_000),
    }
    assert march.liabilities_minor == 41_000
    assert debts(owner, date(2026, 3, 31)) == {
        "Synthetic Own Card": (date(2026, 3, 10), 1_000),
        "Synthetic Shared Loan": (date(2026, 3, 12), 40_000),
    }

    # Even when a caller names the other person's account, its snapshots stay hidden.
    named = report(owner, date(2026, 3, 1), date(2026, 3, 31), accounts=[own_card, member_card]).periods[0]
    assert known(named) == {"Synthetic Own Card": (date(2026, 3, 10), "manual", 1_000)}
    assert (named.omitted_untracked, named.liabilities_minor) == (1, 1_000)

    Membership.objects.filter(person=owner, household=household).update(ended_at=timezone.now())
    march = report(owner, date(2026, 3, 1), date(2026, 3, 31)).periods[0]
    assert known(march) == {"Synthetic Own Card": (date(2026, 3, 10), "manual", 1_000)}
    assert debts(owner, date(2026, 3, 31)) == {"Synthetic Own Card": (date(2026, 3, 10), 1_000)}


@pytest.mark.django_db
def test_manual_and_simplefin_liability_signs_give_the_same_owed_amounts():
    owner = make_person("owner")
    make_household(owner)
    manual_card = make_account(owner, "Synthetic Manual Card")
    simplefin_loan = make_account(owner, "Synthetic SimpleFIN Loan", LOAN)
    credit_card = make_account(owner, "Synthetic Credit Balance Card")
    add_snapshot(manual_card, date(2026, 3, 5), 25_000, MANUAL)
    add_snapshot(simplefin_loan, date(2026, 3, 5), -40_000, SIMPLEFIN)
    add_snapshot(credit_card, date(2026, 3, 5), 5_000, SIMPLEFIN)

    march = report(owner, date(2026, 3, 1), date(2026, 3, 31)).periods[0]

    assert (march.assets_minor, march.liabilities_minor, march.net_minor) == (5_000, 65_000, -60_000)
    rows = {row.name: row for row in list_visible_debts(owner.user, today=date(2026, 3, 31))}
    assert {name: row.owed_minor for name, row in rows.items()} == {
        "Synthetic Credit Balance Card": 0,
        "Synthetic Manual Card": 25_000,
        "Synthetic SimpleFIN Loan": 40_000,
    }
    assert "balance" in rows["Synthetic Credit Balance Card"].missing


@pytest.mark.django_db
def test_no_selected_account_runs_no_snapshot_query():
    owner = make_person("owner")
    make_household(owner)

    with CaptureQueriesContext(connection) as captured:
        result = report(owner, date(2026, 3, 1), date(2026, 3, 31))
        rows = list_visible_debts(owner.user, today=date(2026, 3, 31))

    assert (result.accounts, rows) == ([], [])
    assert snapshot_queries(captured) == []


@pytest.mark.django_db
def test_several_accounts_load_snapshots_in_one_query_per_entry_point():
    owner = make_person("owner")
    make_household(owner)
    for index in range(4):
        account = make_account(owner, f"Synthetic Card {index}")
        add_snapshot(account, date(2026, 3, 1 + index), 1_000 * (index + 1))
        add_snapshot(account, date(2026, 2, 1 + index), 9_000)

    with CaptureQueriesContext(connection) as captured:
        march = report(owner, date(2026, 3, 1), date(2026, 3, 31)).periods[0]
    assert len(snapshot_queries(captured)) == 1
    assert march.liabilities_minor == 10_000

    with CaptureQueriesContext(connection) as captured:
        owed = debts(owner, date(2026, 3, 31))
    assert len(snapshot_queries(captured)) == 1
    assert sum(amount for _day, amount in owed.values()) == 10_000


def test_history_breaks_a_same_date_same_source_tie_by_the_higher_pk():
    # The unique constraint on account, date and source forbids these rows in the database.
    rows = [
        SimpleNamespace(pk=7, snapshot_date=date(2026, 3, 9), source=SIMPLEFIN),
        SimpleNamespace(pk=9, snapshot_date=date(2026, 3, 9), source=MANUAL),
        SimpleNamespace(pk=3, snapshot_date=date(2026, 3, 9), source=SIMPLEFIN),
        SimpleNamespace(pk=5, snapshot_date=date(2026, 3, 2), source=SIMPLEFIN),
    ]

    ordered, dates = ordered_history(rows)

    assert [row.pk for row in ordered] == [5, 9, 3, 7]
    assert dates == [date(2026, 3, 2), date(2026, 3, 9), date(2026, 3, 9), date(2026, 3, 9)]
    assert last_on_or_before(ordered, dates, date(2026, 3, 9)).pk == 7
    assert last_on_or_before(ordered, dates, date(2026, 3, 8)).pk == 5
    assert last_on_or_before(ordered, dates, date(2026, 3, 1)) is None
    assert ordered_history([]) == ([], [])
