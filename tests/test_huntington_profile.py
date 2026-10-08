from datetime import date

import pytest
from django.contrib.auth import get_user_model

from finance.csv_import.parser import preview_csv, read_csv
from finance.csv_import.profiles import (
    HUNTINGTON_HEADER_ERROR,
    HUNTINGTON_HEADERS,
    HUNTINGTON_MAPPING,
    require_headers,
)
from finance.csv_import.services import commit_csv_import
from finance.models import Account, ImportBatch, Person, Transaction
from tests.huntington_fixtures import NATIVE_CSV, huntington_csv

PASSWORD = "Synthetic-passphrase-42!"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def commit(user, account, content):
    document = read_csv(content)
    require_headers(document.headers, HUNTINGTON_HEADERS, HUNTINGTON_HEADER_ERROR)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=document,
        mapping=HUNTINGTON_MAPPING,
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )


def test_native_fixture_uses_crlf_and_expected_headers():
    assert NATIVE_CSV.startswith(
        b"Date,Reference Number,Payee Name,Memo,Amount,Category Name,Transaction Number\r\n"
    )


def test_huntington_preview_joins_description_and_keeps_signed_amounts():
    preview = preview_csv(read_csv(NATIVE_CSV), HUNTINGTON_MAPPING)
    rows = preview.rows

    assert preview.valid_count == 3
    assert [row.transaction_date.isoformat() for row in rows] == ["2026-09-15", "2026-09-16", "2026-09-17"]
    assert [row.amount_minor for row in rows] == [-4215, -300, 150000]
    assert [row.description for row in rows] == [
        "SYNTHETIC GROCER - WEEKLY FOOD",
        "ATM WITHDRAWAL FEE",
        "SYNTHETIC PAYROLL - DIRECT DEPOSIT",
    ]
    assert [row.source_transaction_id for row in rows] == [
        "100000000000000001",
        "100000000000000002",
        "100000000000000003",
    ]


@pytest.mark.django_db
def test_huntington_import_stores_source_id_and_does_not_match_on_it():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)

    first = commit(user, account, NATIVE_CSV)
    stored = list(Transaction.objects.filter(account=account).order_by("transaction_date", "pk"))
    relabeled = huntington_csv(
        (
            "09/15/2026,9,SYNTHETIC GROCER,WEEKLY FOOD,-42.15,,999999999999999999",
            "09/16/2026,2,,ATM WITHDRAWAL FEE,-3.00,,100000000000000002",
            "09/17/2026,1,SYNTHETIC PAYROLL,DIRECT DEPOSIT,1500.00,,100000000000000003",
        )
    )
    second = commit(user, account, relabeled)

    assert first.new_count == 3
    assert second.new_count == 0
    assert second.duplicate_count == 3
    assert [row.source_transaction_id for row in stored] == [
        "100000000000000001",
        "100000000000000002",
        "100000000000000003",
    ]
    assert Transaction.objects.filter(account=account, status="active").count() == 3
