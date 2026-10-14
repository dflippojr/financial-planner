"""An import that adds no rows still records its date range as coverage (#437)."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from finance.cash_flow import _batches_by_account, _period_missing_accounts
from finance.csv_import.parser import read_csv
from finance.csv_import.profiles import APPLE_CARD_MAPPING
from finance.csv_import.services import commit_csv_import, undo_import_batch
from finance.import_due import due_import_accounts
from finance.models import Account, ImportBatch, Person, Transaction

pytestmark = pytest.mark.django_db

HEADER = "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password="Synthetic-passphrase-42!")
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def import_csv(user, account, content, start, end):
    return commit_csv_import(
        user, account.pk, content=content, document=read_csv(content), mapping=APPLE_CARD_MAPPING,
        source="apple_card", date_range_start=start, date_range_end=end,
    )


def due_ids(account, today):
    return [a.pk for a in due_import_accounts(Account.objects.filter(pk=account.pk), today=today)]


def test_all_duplicate_import_counts_as_coverage_for_import_due():
    user, person = make_person("due-owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    today = timezone.localdate()
    old = today - timedelta(days=60)
    content = (HEADER + f"{old:%m/%d/%Y},{old:%m/%d/%Y},RAW,SYNTHETIC SHOP,Other,Purchase,12.34,Synthetic\n").encode()
    import_csv(user, account, content, old, old)
    assert due_ids(account, today) == [account.pk]
    result = import_csv(user, account, content, old, today)
    assert result.new_count == 0 and result.duplicate_count == 1
    assert not result.batch.transactions.exists()
    assert due_ids(account, today) == []
    assert Transaction.objects.count() == 1


def test_header_only_export_clears_missing_import_on_cash_flow():
    user, person = make_person("quiet-owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    september = SimpleNamespace(start=date(2026, 9, 1), end=date(2026, 9, 30))
    assert _period_missing_accounts(september, [account], _batches_by_account(user, [account])) == [account.name]
    header = HEADER.encode()
    import_csv(user, account, header, date(2026, 9, 1), date(2026, 9, 30))
    assert _period_missing_accounts(september, [account], _batches_by_account(user, [account])) == []


def test_undoing_an_empty_batch_removes_its_coverage_and_keeps_earlier_rows():
    user, person = make_person("undo-owner")
    account = Account.objects.create(name="Synthetic Card", account_type="credit_card", owner=person)
    today = timezone.localdate()
    old = today - timedelta(days=60)
    content = (HEADER + f"{old:%m/%d/%Y},{old:%m/%d/%Y},RAW,SYNTHETIC SHOP,Other,Purchase,12.34,Synthetic\n").encode()
    import_csv(user, account, content, old, old)
    empty = import_csv(user, account, content, old, today).batch
    assert due_ids(account, today) == []
    undo_import_batch(user, account.pk, empty.pk)
    assert due_ids(account, today) == [account.pk]
    assert Transaction.objects.filter(status=Transaction.Status.ACTIVE).count() == 1
    assert ImportBatch.objects.get(pk=empty.pk).status == ImportBatch.Status.ARCHIVED
