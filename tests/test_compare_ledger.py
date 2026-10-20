"""compare_ledger: monthly cash-flow totals against an exported sheet, counts and months only (#536).

Every name, amount and file here is synthetic.
"""

import json
from datetime import date
from io import BytesIO, StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from finance.models import SheetComparisonSettings
from tests.test_sheet_comparison import make_account, make_household, make_person, make_transaction

SHEET = "Month,Income,Spending\n2026-01,1000.00,400.00\n2026-02,1100.00,500.00\n"


def write_sheet(tmp_path, text=SHEET):
    path = tmp_path / "synthetic-sheet.csv"
    path.write_text(text, encoding="utf-8")
    return str(path)


def run(*args):
    out = StringIO()
    try:
        call_command("compare_ledger", *args, stdout=out, stderr=StringIO())
    except CommandError as exc:
        return out.getvalue(), exc
    return out.getvalue(), None


@pytest.fixture
def ledger():
    """An owner whose January matches SHEET exactly and whose February is a cent short on income."""
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    for when, amount, text in (
        (date(2026, 1, 5), 100000, "Synthetic pay"),
        (date(2026, 1, 12), -40000, "Synthetic grocer"),
        (date(2026, 2, 5), 109999, "Synthetic pay"),
        (date(2026, 2, 12), -50000, "Synthetic grocer"),
    ):
        make_transaction(owner, account, transaction_date=when, amount_minor=amount, description=text)
    return owner, account


@pytest.mark.django_db
def test_exact_months_match_and_one_cent_differs(ledger, tmp_path):
    owner, _account = ledger
    out, error = run("--sheet", write_sheet(tmp_path), "--username", owner.user.username)
    assert out.splitlines() == ["2026-02 differs", "2 month(s) compared: 1 match, 1 differs, 0 excepted, 0 open."]
    assert error is not None and error.returncode == 1


@pytest.mark.django_db
def test_all_months_matching_exits_clean(ledger, tmp_path):
    owner, account = ledger
    make_transaction(owner, account, transaction_date=date(2026, 2, 20), amount_minor=1, description="Synthetic interest")
    out, error = run("--sheet", write_sheet(tmp_path), "--username", owner.user.username)
    assert error is None
    assert "2 month(s) compared: 2 match, 0 differs, 0 excepted, 0 open." in out


@pytest.mark.django_db
def test_income_and_spending_are_judged_separately(ledger, tmp_path):
    """Offsetting errors in income and spending must not cancel into a match."""
    owner, account = ledger
    make_transaction(owner, account, transaction_date=date(2026, 2, 20), amount_minor=1, description="Synthetic interest")
    make_transaction(owner, account, transaction_date=date(2026, 2, 21), amount_minor=-100, description="Synthetic fee")
    make_transaction(owner, account, transaction_date=date(2026, 2, 22), amount_minor=100, description="Synthetic credit")
    sheet = write_sheet(tmp_path, "Month,Income,Spending\n2026-02,1100.00,500.00\n")
    out, error = run("--sheet", sheet, "--username", owner.user.username)
    assert error is not None and "2026-02 differs" in out


@pytest.mark.django_db
def test_known_timing_month_is_excepted_only_when_it_differs(ledger, tmp_path):
    owner, _account = ledger
    out, error = run(
        "--sheet", write_sheet(tmp_path), "--username", owner.user.username,
        "--known-timing", "2026-02", "--known-timing", "2026-01",
    )
    assert error is None
    assert out.splitlines() == ["2026-02 excepted", "2 month(s) compared: 1 match, 0 differs, 1 excepted, 0 open."]


@pytest.mark.django_db
def test_month_that_has_not_ended_is_open_not_differs(ledger, tmp_path):
    owner, _account = ledger
    this_month = timezone.localdate().strftime("%Y-%m")
    sheet = write_sheet(tmp_path, f"Month,Income,Spending\n{this_month},5.00,5.00\n")
    out, error = run("--sheet", sheet, "--username", owner.user.username)
    assert error is None
    assert f"{this_month} open" in out


@pytest.mark.django_db
def test_json_has_counts_and_months_only(ledger, tmp_path):
    owner, _account = ledger
    out, _error = run("--sheet", write_sheet(tmp_path), "--username", owner.user.username, "--json")
    assert json.loads(out) == {
        "version": 1,
        "counts": {"match": 1, "differs": 1, "excepted": 0, "open": 0},
        "months": [{"month": "2026-01", "state": "match"}, {"month": "2026-02", "state": "differs"}],
    }


@pytest.mark.django_db
def test_output_never_carries_amounts_names_or_sheet_cells(ledger, tmp_path):
    owner, account = ledger
    out, error = run("--sheet", write_sheet(tmp_path), "--username", owner.user.username)
    for text in (out, str(error)):
        for secret in ("1000", "1100", "109999", "1099.99", "400", "Synthetic", account.name):
            assert secret not in text


@pytest.mark.django_db
def test_other_members_private_accounts_are_not_counted(ledger, tmp_path):
    owner, _account = ledger
    other = make_person("other")
    make_household(other, name="Other Household")
    make_transaction(
        other,
        make_account(other, name="Other Checking"),
        transaction_date=date(2026, 2, 6),
        amount_minor=1,
        description="Synthetic private pay",
    )
    out, _error = run("--sheet", write_sheet(tmp_path), "--username", owner.user.username)
    assert "2026-02 differs" in out


@pytest.mark.django_db
def test_account_option_limits_the_comparison(ledger, tmp_path):
    owner, first = ledger
    second = make_account(owner, name="Synthetic Second")
    make_transaction(owner, second, transaction_date=date(2026, 1, 9), amount_minor=1, description="Synthetic pay")
    sheet = write_sheet(tmp_path, "Month,Income,Spending\n2026-01,1000.00,400.00\n")
    both, error = run("--sheet", sheet, "--username", owner.user.username)
    assert error is not None and "2026-01 differs" in both
    only_first, error = run("--sheet", sheet, "--username", owner.user.username, "--account", str(first.pk))
    assert error is None and "1 match" in only_first
    _out, error = run("--sheet", sheet, "--username", owner.user.username, "--account", "999999")
    assert error is not None and "not a cash-flow account" in str(error)


@pytest.mark.django_db
def test_column_flags_and_signed_spending(ledger, tmp_path):
    owner, _account = ledger
    sheet = write_sheet(tmp_path, "When,In,Out\n2026-01,1000.00,-400.00\n")
    out, error = run(
        "--sheet", sheet, "--username", owner.user.username,
        "--month-column", "When", "--income-column", "In", "--spending-column", "Out", "--spending-sign", "signed",
    )
    assert error is None and "1 month(s) compared: 1 match" in out


@pytest.mark.django_db
def test_saved_mapping_is_the_default_and_flags_override_it(ledger, tmp_path):
    owner, _account = ledger
    SheetComparisonSettings.objects.create(
        member=owner, month_column="When", income_column="In", spending_column="Out", spending_sign="signed"
    )
    sheet = write_sheet(tmp_path, "When,In,Out\n2026-01,1000.00,-400.00\n")
    _out, error = run("--sheet", sheet, "--username", owner.user.username)
    assert error is None
    _out, error = run("--sheet", sheet, "--username", owner.user.username, "--spending-sign", "unsigned")
    assert error is not None and "could not be read" in str(error)


@pytest.mark.parametrize(
    ("args", "text", "message"),
    [
        ((), "Month,Income\n2026-01,1.00\n", "columns must all be headers"),
        ((), "Month,Income,Spending\nnot-a-month,secret-payee,1.00\n", "could not be read"),
        ((), "Month,Income,Spending\n2026-01,1.00,1.00\n2026-01,1.00,1.00\n", "only once"),
        (("--known-timing", "March"), SHEET, "YYYY-MM"),
        (("--known-timing", "2026-13"), SHEET, "YYYY-MM"),
    ],
)
@pytest.mark.django_db
def test_bad_input_is_refused_without_echoing_cells(ledger, tmp_path, args, text, message):
    owner, _account = ledger
    out, error = run("--sheet", write_sheet(tmp_path, text), "--username", owner.user.username, *args)
    assert message in str(error)
    assert "secret-payee" not in str(error) + out
    assert "not-a-month" not in str(error) + out


@pytest.mark.django_db
def test_year_zero_known_timing_returns_validation_error(ledger, tmp_path):
    owner, _ = ledger
    with pytest.raises(CommandError, match='YYYY-MM'):
        call_command('compare_ledger', '--sheet', write_sheet(tmp_path),
                     '--username', owner.user.username, '--known-timing', '0000-01')


@pytest.mark.django_db
def test_missing_file_and_unknown_member(ledger, tmp_path):
    owner, _account = ledger
    _out, error = run("--sheet", str(tmp_path / "absent.csv"), "--username", owner.user.username)
    assert "could not be read" in str(error)
    _out, error = run("--sheet", write_sheet(tmp_path), "--username", "nobody")
    assert "Member not found" in str(error)


@pytest.mark.django_db
def test_sheet_can_come_from_standard_input(ledger, monkeypatch):
    owner, _account = ledger
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=BytesIO(SHEET.encode())))
    out, error = run("--sheet", "-", "--username", owner.user.username)
    assert error is not None and "2026-02 differs" in out


@pytest.mark.django_db
def test_oversized_sheet_is_refused(ledger, tmp_path):
    owner, _account = ledger
    big = write_sheet(tmp_path, "Month,Income,Spending\n" + "#" * (5 * 1024 * 1024))
    _out, error = run("--sheet", big, "--username", owner.user.username)
    assert "at most 5 MB" in str(error)


@pytest.mark.django_db
def test_command_changes_nothing(ledger, tmp_path):
    from finance.models import SheetMonthTotal, Transaction

    owner, _account = ledger
    before = (Transaction.objects.count(), SheetMonthTotal.objects.count(), SheetComparisonSettings.objects.count())
    run("--sheet", write_sheet(tmp_path), "--username", owner.user.username)
    assert before == (Transaction.objects.count(), SheetMonthTotal.objects.count(), SheetComparisonSettings.objects.count())
