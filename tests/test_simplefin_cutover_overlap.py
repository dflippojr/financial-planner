"""A SimpleFIN cut-over never overlaps rows the account already has (#671).

Saving a link refuses a cut-over on or before the latest active file-imported
row (compared by the date SimpleFIN would give it), or later than the earliest
active SimpleFIN row, so a CSV import and a sync can never both supply a day.
Synthetic data only.
"""

import ast
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import connection as db_connection
from django.test.utils import CaptureQueriesContext

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
def test_a_card_row_posted_before_its_transaction_date_bounds_by_the_posted_day(monkeypatch):
    """Each row counts by its own posted day, as classify_overlap does, not the later of its two dates."""
    owner = make_person("owner")
    make_household(owner)
    card = make_account(owner, name="Synthetic Card", account_type="credit_card")
    ledger.import_file(owner, card, (
        b"Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        b"2026-03-12,2026-03-10,0000,SYNTHETIC GROCER,Merchandise,25.00,\n"
    ), profile="capital_one")
    connection = connect_owner(owner, monkeypatch, _payload())
    with pytest.raises(SimpleFinError, match="after 2026-03-10"):
        _link(owner, connection, card, date(2026, 3, 10))
    _link(owner, connection, card, date(2026, 3, 11))
    assert _cutover(card) == date(2026, 3, 11)


@pytest.mark.django_db
def test_saving_a_cutover_locks_the_account_before_reading_its_rows(monkeypatch):
    """The CSV import holds this row lock, so an import cannot slip in between the check and the save."""
    if not db_connection.features.has_select_for_update:
        pytest.skip("Row locks need PostgreSQL")
    owner, checking = _owner_with_checking()
    connection = connect_owner(owner, monkeypatch, _payload())
    with CaptureQueriesContext(db_connection) as queries:
        _link(owner, connection, checking, date(2026, 3, 1))
    sql = [query["sql"] for query in queries.captured_queries]
    account_lock = next(i for i, q in enumerate(sql) if 'FROM "finance_account"' in q and "FOR UPDATE" in q
                        and q.lstrip().startswith("SELECT") and str(checking.pk) in q)
    first_row_read = next(i for i, q in enumerate(sql) if 'FROM "finance_transaction"' in q)
    assert account_lock < first_row_read


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


# Class-level check: no code path writes a cut-over without check_cutover.

FINANCE = Path(__file__).resolve().parent.parent / "finance"
WRITE_METHODS = {"create", "update", "update_or_create", "get_or_create", "bulk_create", "bulk_update", "save"}


def _writes_cutover(node):
    """True for a call or assignment in `node` that stores an AccountLink cut-over."""
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(isinstance(t, ast.Attribute) and t.attr == "cutover_date" for t in targets)
    if not isinstance(node, ast.Call):
        return False
    # AccountLink(cutover_date=...) builds a row for save() or bulk_create().
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    if name == "AccountLink" and any(keyword.arg == "cutover_date" for keyword in node.keywords):
        return True
    if not (isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS):
        return False
    # A field list naming it, positional or keyword: bulk_update(links, ["cutover_date"]), update_fields=.
    if any(isinstance(inner, ast.Constant) and inner.value == "cutover_date"
           for arg in [*node.args, *(keyword.value for keyword in node.keywords)] for inner in ast.walk(arg)):
        return True
    for keyword in node.keywords:
        if keyword.arg == "cutover_date":
            return True
        if isinstance(keyword.value, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value == "cutover_date" for key in keyword.value.keys
        ):
            return True
    return False


def _cutover_writers():
    found = set()
    for path in FINANCE.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(_writes_cutover(node) for node in ast.walk(function)):
                found.add((path.relative_to(FINANCE).as_posix(), function.name))
        module_level = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                                  ast.ClassDef))]
        if any(_writes_cutover(node) for top in module_level for node in ast.walk(top)):
            found.add((path.relative_to(FINANCE).as_posix(), "<module>"))
    return found


def test_every_cutover_write_goes_through_the_overlap_check():
    """A new way to set or move a cut-over must call check_cutover, through _save_link (#671)."""
    assert _cutover_writers() == {("simplefin_services.py", "_save_link")}
    source = (FINANCE / "simplefin_services.py").read_text(encoding="utf-8")
    save_link = next(node for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.FunctionDef) and node.name == "_save_link")
    assert "check_cutover(" in ast.unparse(save_link)


def test_the_writer_scan_sees_each_way_to_store_a_cutover():
    """Guard the guard: each write shape the scan claims to catch is caught."""
    for snippet in (
        "def f(): AccountLink.objects.create(cutover_date=day)",
        "def f(): AccountLink.objects.filter(pk=1).update(cutover_date=day)",
        "def f(): AccountLink.objects.update_or_create(pk=1, defaults={'cutover_date': day})",
        "def f(link): link.cutover_date = day",
        "def f(link): link.save(update_fields=['cutover_date'])",
        "def f(): AccountLink.objects.bulk_create([AccountLink(cutover_date=day)])",
        "def f(links): AccountLink.objects.bulk_update(links, ['cutover_date'])",
        "def f(links): AccountLink.objects.bulk_update(links, fields=('cutover_date', 'mode'))",
        "def f(): models.AccountLink(cutover_date=day).save()",
    ):
        tree = ast.parse(snippet)
        assert any(_writes_cutover(node) for node in ast.walk(tree)), snippet
