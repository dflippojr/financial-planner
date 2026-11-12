"""A blank, null or missing SimpleFIN balance skips the snapshot, never the sync (#672).

Synthetic data only.
"""

from datetime import date
from unittest.mock import patch

import pytest

from finance.models import Account, AccountLink, BalanceSnapshot, Transaction
from finance.simplefin_services import save_account_links, sync_connection
from tests.test_simplefin import (
    account_payload,
    connect_owner,
    make_account,
    make_household,
    make_person,
    posted_txn,
)

ABSENT = "absent"
BLANK_BALANCES = ["", None, "   ", ABSENT]


def _set_balance(remote, balance):
    if balance == ABSENT:
        del remote["balance"]
    else:
        remote["balance"] = balance


@pytest.mark.django_db
@pytest.mark.parametrize("balance", BLANK_BALANCES)
def test_a_blank_balance_does_not_fail_the_sync(monkeypatch, balance):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload(transactions=[posted_txn(txn_id="sf-1", day=12, amount="-25.00")])
    _set_balance(payload["accounts"][0], balance)
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(owner, connection.pk, [{
        "simplefin_account_id": "CON-1:sf-checking", "action": "link",
        "account_id": checking.pk, "cutover_date": date(2026, 3, 1),
    }])

    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert result["imported"] == 1
    assert Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE).count() == 1
    assert not BalanceSnapshot.objects.filter(account=checking).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("balance", BLANK_BALANCES)
def test_a_blank_balances_only_account_does_not_block_the_other_accounts(monkeypatch, balance):
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    brokerage = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    payload = account_payload(transactions=[posted_txn(txn_id="sf-1", day=12, amount="-25.00")])
    # Vanguard-shaped: Bridge has sent "" for currency (#627), and may for balance.
    vanguard = {
        "id": "sf-invest", "name": "Synthetic Brokerage", "conn_id": "CON-1",
        "currency": "", "balance": "", "balance-date": payload["accounts"][0]["balance-date"],
        "transactions": [], "extra": {},
    }
    _set_balance(vanguard, balance)
    payload["accounts"].insert(0, vanguard)
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(owner, connection.pk, [
        {"simplefin_account_id": "CON-1:sf-invest", "action": "link", "account_id": brokerage.pk},
        {"simplefin_account_id": "CON-1:sf-checking", "action": "link",
         "account_id": checking.pk, "cutover_date": date(2026, 3, 1)},
    ])
    assert AccountLink.objects.get(account=brokerage).mode == AccountLink.Mode.BALANCES_ONLY

    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        result = sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert result["imported"] == 1
    assert Transaction.objects.filter(account=checking, status=Transaction.Status.ACTIVE).count() == 1
    assert BalanceSnapshot.objects.get(account=checking).amount_minor == 10023
    assert not BalanceSnapshot.objects.filter(account=brokerage).exists()


@pytest.mark.django_db
def test_a_blank_balance_keeps_the_earlier_snapshot(monkeypatch):
    """A later sync with no balance leaves the day's recorded balance alone rather than zeroing it."""
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    payload = account_payload()
    connection = connect_owner(owner, monkeypatch, payload)
    save_account_links(owner, connection.pk, [{
        "simplefin_account_id": "CON-1:sf-checking", "action": "link",
        "account_id": checking.pk, "cutover_date": date(2026, 3, 1),
    }])
    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)
    payload["accounts"][0]["balance"] = ""

    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        sync_connection(owner, connection.pk, ignore_rate_limit=True)

    assert BalanceSnapshot.objects.get(account=checking).amount_minor == 10023
