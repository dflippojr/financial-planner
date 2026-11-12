"""A SimpleFIN cut-over never overlaps rows the account already has (#671).

Saving a link refuses a cut-over on or before the latest active file-imported
row (compared by the date SimpleFIN would give it), or later than the earliest
active SimpleFIN row, so a CSV import and a sync can never both supply a day.
Synthetic data only.
"""

from datetime import date
from unittest.mock import patch

import pytest

from finance.csv_import.services import undo_import_batch
from finance.models import AccountLink, Transaction
from finance.simplefin_services import SimpleFinError, save_account_links, sync_connection
from tests.properties import harness as ledger
from tests.test_simplefin import account_payload, connect_owner, make_account, make_household, make_person, posted_txn

HUNTINGTON_CSV = (
    b"Date,Reference Number,Payee Name,Memo,Amount,Category Name,Transaction Number\n"
    b"03/12/2026,R1,SYNTHETIC GROCER,Purchase,-25.00,Food,T-1\n"
)
# Transaction Date 03/10, posted 03/12: SimpleFIN dates it by the posted day.
CAPITAL_ONE_CSV = (
    b"Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
    b"2026-03-10,2026-03-12,0000,SYNTHETIC GROCER,Merchandise,25.00,\n"
)


def _link(owner, connection, account, cutover):
    save_account_links(owner, connection.pk, [{
        "simplefin_account_id": "CON-1:sf-checking", "action": "link",
        "account_id": account.pk, "cutover_date": cutover,
    }])


def _link_if_accepted(owner, connection, account, cutover):
    """Save the link the way a member would; a refusal leaves the account as it was."""
    try:
        _link(owner, connection, account, cutover)
    except SimpleFinError:
        return False
    return True


def _sync(owner, connection, payload):
    with patch("finance.simplefin_services.fetch_accounts", return_value=payload):
        return sync_connection(owner, connection.pk, ignore_rate_limit=True)


def _active(account):
    return Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE)


def _cutover(account):
    return AccountLink.objects.filter(account=account).values_list("cutover_date", flat=True).first()


def _owner_with_checking():
    owner = make_person("owner")
    make_household(owner)
    return owner, make_account(owner)


def _payload():
    return account_payload(transactions=[posted_txn(txn_id="sf-1", day=12, amount="-25.00")])


# The two reproductions from the issue: each purchase counts once.

@pytest.mark.django_db
def test_a_cutover_before_existing_csv_rows_does_not_double_count_them(monkeypatch):
    owner, checking = _owner_with_checking()
    ledger.import_file(owner, checking, HUNTINGTON_CSV, profile="huntington")
    payload = _payload()
    connection = connect_owner(owner, monkeypatch, payload)
    _link_if_accepted(owner, connection, checking, date(2026, 3, 1))
    _sync(owner, connection, payload)
    assert _active(checking).count() == 1


@pytest.mark.django_db
def test_moving_the_cutover_later_does_not_let_a_csv_double_count_synced_days(monkeypatch):
    owner, checking = _owner_with_checking()
    payload = _payload()
    connection = connect_owner(owner, monkeypatch, payload)
    _link(owner, connection, checking, date(2026, 3, 1))
    _sync(owner, connection, payload)
    _link_if_accepted(owner, connection, checking, date(2026, 4, 1))
    ledger.import_file(owner, checking, HUNTINGTON_CSV, profile="huntington")
    assert _active(checking).count() == 1


# What the member sees: the save is refused with the date that would be safe.

@pytest.mark.django_db
def test_a_cutover_on_or_before_the_latest_file_row_is_refused(monkeypatch):
    owner, checking = _owner_with_checking()
    ledger.import_file(owner, checking, HUNTINGTON_CSV, profile="huntington")
    connection = connect_owner(owner, monkeypatch, _payload())
    for cutover in (date(2026, 3, 1), date(2026, 3, 12)):
        with pytest.raises(SimpleFinError, match="after 2026-03-12"):
            _link(owner, connection, checking, cutover)
    assert not AccountLink.objects.filter(account=checking).exists()
    _link(owner, connection, checking, date(2026, 3, 13))
    assert _cutover(checking) == date(2026, 3, 13)


@pytest.mark.django_db
def test_a_card_rows_posted_day_bounds_the_cutover(monkeypatch):
    owner = make_person("owner")
    make_household(owner)
    card = make_account(owner, name="Synthetic Card", account_type="credit_card")
    ledger.import_file(owner, card, CAPITAL_ONE_CSV, profile="capital_one")
    connection = connect_owner(owner, monkeypatch, _payload())
    with pytest.raises(SimpleFinError, match="after 2026-03-12"):
        _link(owner, connection, card, date(2026, 3, 11))
    _link(owner, connection, card, date(2026, 3, 13))


@pytest.mark.django_db
def test_moving_the_cutover_past_a_synced_row_is_refused(monkeypatch):
    owner, checking = _owner_with_checking()
    payload = _payload()
    connection = connect_owner(owner, monkeypatch, payload)
    _link(owner, connection, checking, date(2026, 3, 1))
    _sync(owner, connection, payload)
    with pytest.raises(SimpleFinError, match="on or before 2026-03-12"):
        _link(owner, connection, checking, date(2026, 3, 13))
    assert _cutover(checking) == date(2026, 3, 1)
    # Up to the earliest synced day is still safe, and so is moving it earlier.
    _link(owner, connection, checking, date(2026, 3, 12))
    _link(owner, connection, checking, date(2026, 2, 1))
    assert _cutover(checking) == date(2026, 2, 1)


@pytest.mark.django_db
def test_re_pointing_a_link_checks_the_newly_chosen_account(monkeypatch):
    owner, checking = _owner_with_checking()
    other = make_account(owner, name="Synthetic Other")
    ledger.import_file(owner, other, HUNTINGTON_CSV, profile="huntington")
    connection = connect_owner(owner, monkeypatch, _payload())
    _link(owner, connection, checking, date(2026, 3, 1))
    with pytest.raises(SimpleFinError, match="after 2026-03-12"):
        _link(owner, connection, other, date(2026, 3, 1))
    assert AccountLink.objects.get(connection=connection).account_id == checking.pk


@pytest.mark.django_db
def test_an_unchanged_link_saves_even_when_its_account_already_overlaps(monkeypatch):
    """Re-saving the form must not trap a link created before this check existed."""
    owner, checking = _owner_with_checking()
    payload = _payload()
    connection = connect_owner(owner, monkeypatch, payload)
    _link(owner, connection, checking, date(2026, 3, 1))
    ledger.import_file(owner, checking, b"Date,Reference Number,Payee Name,Memo,Amount,Category Name,"
                       b"Transaction Number\n02/20/2026,R2,SYNTHETIC SHOP,Purchase,-9.00,Food,T-2\n",
                       profile="huntington")
    # Simulate a pre-#671 overlap: a file row after the cut-over.
    Transaction.objects.filter(account=checking).update(transaction_date=date(2026, 3, 5))
    _link(owner, connection, checking, date(2026, 3, 1))
    assert _cutover(checking) == date(2026, 3, 1)


@pytest.mark.django_db
def test_a_blank_cutover_on_an_existing_link_keeps_its_date(monkeypatch):
    owner, checking = _owner_with_checking()
    payload = _payload()
    connection = connect_owner(owner, monkeypatch, payload)
    _link(owner, connection, checking, date(2026, 3, 1))
    _sync(owner, connection, payload)
    _link(owner, connection, checking, None)
    assert _cutover(checking) == date(2026, 3, 1)


@pytest.mark.django_db
def test_undone_rows_do_not_bound_the_cutover(monkeypatch):
    owner, checking = _owner_with_checking()
    result = ledger.import_file(owner, checking, HUNTINGTON_CSV, profile="huntington")
    undo_import_batch(owner, checking.pk, result.batch.pk)
    connection = connect_owner(owner, monkeypatch, _payload())
    _link(owner, connection, checking, date(2026, 3, 1))
    assert _cutover(checking) == date(2026, 3, 1)
