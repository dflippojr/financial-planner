"""A non-USD SimpleFIN account converts at the ECB rate, or is skipped alone (#441)."""

from datetime import date

import pytest

from finance.models import Account, Transaction
from finance.simplefin_services import save_account_links, sync_connection
from tests.test_simplefin import connect_owner, epoch, make_account, make_household, make_person, posted_txn


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
