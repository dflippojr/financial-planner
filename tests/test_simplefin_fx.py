"""A non-USD SimpleFIN account converts at the ECB rate, or is skipped alone (#441)."""

from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from finance.fx_rates import RateUnavailable, to_usd_minor
from finance.models import Account, BalanceSnapshot, Transaction
from finance.reconciliation import eligible_accounts
from finance.simplefin_services import save_account_links, sync_connection
from tests.test_simplefin import (
    connect_owner,
    epoch,
    make_account,
    make_household,
    make_person,
    posted_txn,
    signed_in,
)


def two_account_payload(*, cad_currency="CAD"):
    return {
        "errlist": [],
        "connections": [{"conn_id": "CON-1", "name": "Synthetic Bank - Pat", "org_id": "ORG-1"}],
        "accounts": [
            {
                "id": "sf-checking",
                "name": "Synthetic Checking",
                "conn_id": "CON-1",
                "currency": "USD",
                "balance": "100.00",
                "balance-date": epoch(2026, 3, 20),
                "transactions": [posted_txn(txn_id="usd-1", day=16, amount="-3.00")],
            },
            {
                "id": "sf-savings",
                "name": "Synthetic Canadian Savings",
                "conn_id": "CON-1",
                "currency": cad_currency,
                "balance": "200.00",
                "balance-date": epoch(2026, 3, 20),
                "transactions": [posted_txn(txn_id="cad-1", day=16, amount="-10.00")],
            },
        ],
    }


def link_both(monkeypatch, payload):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    savings = make_account(owner, name="Synthetic Canadian Savings", account_type=Account.Type.SAVINGS)
    connection = connect_owner(owner, monkeypatch, payload)
    cutover = date(2026, 3, 1)
    save_account_links(
        owner,
        connection.pk,
        [
            {"simplefin_account_id": "CON-1:sf-checking", "action": "link", "account_id": checking.pk,
             "cutover_date": cutover},
            {"simplefin_account_id": "CON-1:sf-savings", "action": "link", "account_id": savings.pk,
             "cutover_date": cutover},
        ],
    )
    return owner, checking, savings, connection


@pytest.mark.django_db
def test_a_cad_account_does_not_block_the_usd_account_on_its_connection(monkeypatch):
    owner, checking, _savings, connection = link_both(monkeypatch, two_account_payload())

    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert Transaction.objects.filter(account=checking, source_transaction_id="usd-1").exists()


def patch_rates(monkeypatch, table, *, calls=None):
    def fake(currency, start, end):
        if calls is not None:
            calls.append((currency, start, end))
        if isinstance(table, Exception):
            raise table
        return dict(table)

    monkeypatch.setattr("finance.fx_rates.fetch_rates", fake)


# 2026-03-16 is the CAD transaction's date and 2026-03-20 its balance date; the
# sync runs on a later day, so a rate taken at sync time would differ.
RATES = {date(2026, 3, 16): Decimal("0.75"), date(2026, 3, 20): Decimal("0.9"), date(2026, 3, 27): Decimal("2")}


@pytest.mark.django_db
def test_a_cad_transaction_is_stored_in_usd_at_its_own_dates_rate_and_keeps_its_original(monkeypatch):
    calls = []
    patch_rates(monkeypatch, RATES, calls=calls)
    owner, checking, savings, connection = link_both(monkeypatch, two_account_payload())

    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    txn = Transaction.objects.get(account=savings, source_transaction_id="cad-1")
    assert (txn.amount_minor, txn.currency) == (-750, "USD")
    assert txn.original_fields["amount"] == "-10.00"
    assert txn.original_fields["fx"] == {"currency": "CAD", "rate": "0.75", "rate_date": "2026-03-16"}
    snapshot = BalanceSnapshot.objects.get(account=savings)
    assert (snapshot.amount_minor, snapshot.currency) == (18000, "USD")
    assert (snapshot.original_amount, snapshot.original_currency) == (Decimal("200"), "CAD")
    assert (snapshot.fx_rate, snapshot.fx_rate_date) == (Decimal("0.9"), date(2026, 3, 20))
    assert Transaction.objects.get(account=checking, source_transaction_id="usd-1").original_fields.get("fx") is None
    assert BalanceSnapshot.objects.get(account=checking).original_currency == ""
    # One request for the whole currency: the dates this sync converts, from a
    # week earlier so a weekend start still finds its Friday.
    assert calls == [("CAD", date(2026, 3, 9), date(2026, 3, 20))]


@pytest.mark.django_db
def test_a_resync_neither_duplicates_nor_reconverts(monkeypatch):
    patch_rates(monkeypatch, RATES)
    owner, _checking, savings, connection = link_both(monkeypatch, two_account_payload())
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    patch_rates(monkeypatch, {day: rate * 2 for day, rate in RATES.items()})
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert list(Transaction.objects.filter(account=savings).values_list("amount_minor", flat=True)) == [-750]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "table",
    [RateUnavailable("The exchange-rate service could not be reached."), {}, {date(2026, 1, 5): Decimal("0.7")}],
    ids=["client error", "no rates", "no rate near the date"],
)
def test_without_a_rate_only_the_cad_account_is_skipped_with_a_message(monkeypatch, table):
    patch_rates(monkeypatch, table)
    owner, checking, savings, connection = link_both(monkeypatch, two_account_payload())

    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert Transaction.objects.filter(account=checking, source_transaction_id="usd-1").exists()
    assert not Transaction.objects.filter(account=savings).exists()
    assert not BalanceSnapshot.objects.filter(account=savings).exists()
    assert len(result["unconverted"]) == 1
    connection.refresh_from_db()
    assert "Skipped Synthetic Canadian Savings (CAD)" in connection.last_sync_result
    assert "retried on the next sync" in connection.last_sync_result
    assert not connection.disabled


@pytest.mark.django_db
def test_a_later_sync_converts_an_account_that_was_skipped(monkeypatch):
    patch_rates(monkeypatch, RateUnavailable("The exchange-rate service could not be reached."))
    owner, _checking, savings, connection = link_both(monkeypatch, two_account_payload())
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    patch_rates(monkeypatch, RATES)
    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert result["unconverted"] == []
    assert Transaction.objects.get(account=savings).amount_minor == -750


@pytest.mark.django_db
@pytest.mark.parametrize("currency", ["", "https://example.test/points", "C4D"])
def test_an_unusable_currency_is_skipped_without_asking_for_a_rate(monkeypatch, currency):
    calls = []
    patch_rates(monkeypatch, RATES, calls=calls)
    owner, checking, savings, connection = link_both(monkeypatch, two_account_payload(cad_currency=currency))

    result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert calls == []
    assert "usable currency" in result["unconverted"][0]
    assert Transaction.objects.filter(account=checking).count() == 1
    assert not Transaction.objects.filter(account=savings).exists()


@pytest.mark.django_db
def test_a_cad_account_that_reports_no_transactions_still_converts_its_balance(monkeypatch):
    patch_rates(monkeypatch, RATES)
    payload = two_account_payload()
    del payload["accounts"][1]["transactions"]
    owner, _checking, savings, connection = link_both(monkeypatch, payload)

    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert BalanceSnapshot.objects.get(account=savings).amount_minor == 18000


@pytest.mark.django_db
def test_reconciliation_leaves_a_converted_account_out(monkeypatch):
    patch_rates(monkeypatch, RATES)
    owner, checking, savings, connection = link_both(monkeypatch, two_account_payload())
    sync_connection(owner, connection.pk, ignore_rate_limit=True)

    eligible = set(eligible_accounts().values_list("pk", flat=True))

    assert checking.pk in eligible
    assert savings.pk not in eligible
    out = StringIO()
    call_command("reconcile_balances", stdout=out, stderr=StringIO())


@pytest.mark.django_db
def test_the_connections_page_shows_each_remote_accounts_currency(monkeypatch):
    owner, _checking, _savings, _connection = link_both(monkeypatch, two_account_payload())

    page = signed_in(owner).get(reverse("simplefin-connections")).content.decode()

    assert "<th scope=\"col\">Currency</th>" in page
    assert "CAD" in page
    assert "Converted to USD at the ECB rate" in page


@pytest.mark.parametrize(
    ("amount", "rate", "cents"),
    [
        ("0.05", "0.5", 2),  # 2.5 cents rounds to the even 2
        ("0.15", "0.5", 8),  # 7.5 cents rounds to the even 8
        ("-0.15", "0.5", -8),
        ("10.00", "0.72975", 730),  # 729.75 cents
        ("1234.567", "0.73", 90123),  # a three-decimal currency is not truncated first
        ("0", "0.73", 0),
    ],
)
def test_conversion_rounds_half_even_once_to_the_cent(amount, rate, cents):
    assert to_usd_minor(Decimal(amount), Decimal(rate)) == cents
