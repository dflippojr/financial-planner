"""Extreme but storable values must not break shared pages or background refreshes."""

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from finance.csv_import.parser import preview_csv, read_csv
from finance.date_bounds import TOO_EARLY_ERROR, TOO_LATE_ERROR, activity_date_error
from finance.models import Account, BalanceSnapshot, RecurringSeries
from finance.performance import _pct_display, account_performance
from finance.recurring_services import add_cadence, confirm_recurring_series, refresh_recurring_series
from tests.test_csv_parser import mapping
from tests.test_recurring import add_monthly_charges, make_account, make_household, make_person, make_transaction

MAX_BIGINT = 2**63 - 1


def statement(account, snapshot_date, amount_minor, net_contribution_minor=0):
    return BalanceSnapshot.objects.create(
        account=account,
        snapshot_date=snapshot_date,
        amount_minor=amount_minor,
        currency="USD",
        source=BalanceSnapshot.Source.MANUAL,
        net_contribution_minor=net_contribution_minor,
    )


def extreme_statements(account):
    """Form-valid entries whose linked return exceeds decimal precision."""
    return [
        statement(account, date(2026, 1, 1), 1),
        statement(account, date(2026, 2, 1), MAX_BIGINT),
        statement(account, date(2026, 3, 1), 1, 1 - MAX_BIGINT),
        statement(account, date(2026, 4, 1), MAX_BIGINT),
    ]


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_extreme_statement_entries_keep_shared_pages_working_and_deletable():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    account = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT,
                           scope=Account.Scope.HOUSEHOLD, household=household)
    entries = extreme_statements(account)

    for person in (owner, member):
        client = signed_in(person)
        assert client.get(reverse("net-worth")).status_code == 200
        assert client.get(reverse("account-balances", args=[account.pk])).status_code == 200

    performance = account_performance(account, date(2026, 10, 1))
    assert performance.summaries.all_time.return_display == "Over 1,000,000%"

    response = signed_in(member).post(reverse("account-snapshot-delete", args=[account.pk, entries[-1].pk]))
    assert response.status_code == 302
    assert not BalanceSnapshot.objects.filter(pk=entries[-1].pk).exists()


def test_percent_display_is_bounded_and_total():
    assert _pct_display(Decimal("0.1234")) == "12.3%"
    assert _pct_display(Decimal("-1e40")) == "Below -1,000,000%"
    assert _pct_display(Decimal("Infinity")) == "Over 1,000,000%"
    assert _pct_display(Decimal("NaN")) is None
    assert _pct_display(Decimal("9e999999")) == "Over 1,000,000%"


@pytest.mark.django_db
def test_balance_form_rejects_implausible_statement_entries():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    client = signed_in(owner)
    from tests.helpers import stamp_recent_auth

    stamp_recent_auth(client)
    response = client.post(
        reverse("account-balances", args=[account.pk]),
        {"snapshot_date": "2026-01-01", "amount": "92233720368547758.07", "net_contribution": "-1"},
    )

    assert response.status_code == 200
    assert b"between -$1,000,000,000,000 and $1,000,000,000,000" in response.content
    assert not BalanceSnapshot.objects.filter(account=account).exists()


def test_activity_date_window():
    today = date(2026, 10, 9)
    assert activity_date_error(date(1899, 12, 31), today) == TOO_EARLY_ERROR
    assert activity_date_error(date(1900, 1, 1), today) is None
    assert activity_date_error(date(2027, 10, 10), today) is None
    assert activity_date_error(date(2027, 10, 11), today) == TOO_LATE_ERROR


def test_csv_row_with_a_far_future_date_gets_a_row_error():
    document = read_csv(b"When,Memo,Amount\n06/15/9999,SYNTHETIC ITEM,1.00\n06/15/2026,SYNTHETIC ITEM,1.00\n")

    result = preview_csv(document, mapping())

    assert result.rows[0].errors == (TOO_LATE_ERROR,)
    assert result.rows[0].transaction_date is None
    assert result.rows[1].errors == ()


@pytest.mark.django_db
def test_edit_form_rejects_a_far_future_date():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    row = make_transaction(owner, account)

    response = signed_in(owner).post(
        reverse("transaction-edit", args=(row.pk,)),
        {"transaction_date": "9999-06-15", "description": "Synthetic row", "amount": "-10.00"},
    )

    assert response.status_code == 200
    row.refresh_from_db()
    assert row.transaction_date == date(2026, 1, 2)


def test_cadence_arithmetic_returns_none_near_the_end_of_the_calendar():
    for cadence in RecurringSeries.Cadence.values:
        assert add_cadence(date(9999, 12, 30), cadence) is None
    assert add_cadence(date(2026, 1, 31), RecurringSeries.Cadence.MONTHLY) == date(2026, 2, 28)


@pytest.mark.django_db
def test_far_future_rows_do_not_break_recurring_detection_or_the_page():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_monthly_charges(owner, account)
    for cadence_day in (date(9999, 6, 15), date(9999, 9, 15), date(9999, 12, 15), date(9999, 12, 31)):
        make_transaction(owner, account, transaction_date=cadence_day, amount_minor=-1599,
                         description="Synthetic Stream")

    refresh_recurring_series(owner)
    for series in RecurringSeries.objects.filter(person=owner):
        confirm_recurring_series(owner, series.pk)
    refresh_recurring_series(owner)

    client = signed_in(owner)
    for name in ("recurring-review", "home", "transaction-list", "planned-items", "bills-calendar",
                 "spending-by-category", "monthly-review", "budgets", "transfer-review"):
        assert client.get(reverse(name)).status_code == 200, name


@pytest.mark.django_db
def test_activity_date_check_lists_out_of_range_rows_without_details():
    from io import StringIO

    from django.core.management import call_command

    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    clean = StringIO()
    call_command("check_activity_dates", stdout=clean)
    assert "All stored dates are within the plausible window." in clean.getvalue()

    far = make_transaction(owner, account, transaction_date=date(9999, 6, 15), description="Synthetic Secret Payee")
    make_transaction(owner, account, transaction_date=date(2026, 1, 2))
    old = statement(make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT),
                    date(1800, 1, 1), 1234)
    out = StringIO()
    call_command("check_activity_dates", stdout=out)
    text = out.getvalue()

    assert "1 transaction row(s)" in text
    assert f"IDs: {far.pk}" in text
    assert "1 balance entry row(s)" in text
    assert f"IDs: {old.pk}" in text
    assert "Synthetic Secret Payee" not in text
    assert "12.34" not in text
