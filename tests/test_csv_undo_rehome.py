"""Undoing an import keeps the rows a later overlapping import skipped as duplicates (#796).

Every file here is synthetic.
"""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from finance.csv_import.parser import Mapping, read_csv
from finance.csv_import.services import commit_csv_import, undo_import_batch
from finance.models import (
    Account,
    AuditEvent,
    ImportBatch,
    Person,
    Transaction,
    TransactionCorrectionHistory,
)

pytestmark = pytest.mark.django_db

HEADER = b"When,Memo,Amount,Currency\n"
GROCER = b"09/27/2026,SYNTHETIC GROCER,-12.34,USD\n"
COFFEE = b"09/28/2026,SYNTHETIC COFFEE,-4.50,USD\n"
BAKERY = b"09/29/2026,SYNTHETIC BAKERY,-7.00,USD\n"
FUEL = b"10/02/2026,SYNTHETIC FUEL,-30.00,USD\n"


def mapping():
    return Mapping(
        date_column="When",
        description_column="Memo",
        date_format="mdy_slash_4",
        number_format="dot_comma",
        amount_mode="signed",
        amount_column="Amount",
        currency_column="Currency",
    )


def make_owner(username="owner"):
    user = get_user_model().objects.create_user(username=username, password="Synthetic-passphrase-42!")
    return user, Person.objects.create(user=user, display_name="Owner Example")


def make_account(person, name="Synthetic Checking"):
    return Account.objects.create(name=name, account_type="checking", owner=person)


def commit(user, account, *rows):
    content = HEADER + b"".join(rows)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=read_csv(content),
        mapping=mapping(),
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 10, 31),
    )


def rows_by_memo(account):
    return {
        (txn.description, txn.status): txn.import_batch_id
        for txn in Transaction.objects.filter(account=account)
    }


def test_undo_keeps_rows_a_later_overlapping_batch_skipped_as_duplicates():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    later = commit(user, account, COFFEE, FUEL)
    assert (later.new_count, later.duplicate_count) == (1, 1)

    undo_import_batch(user, account.pk, first.batch.pk)

    # The later file also contains the coffee row, so it stays, now on that batch.
    # Only the later file's absence explains the grocer row, which is archived.
    assert rows_by_memo(account) == {
        ("SYNTHETIC GROCER", "archived"): first.batch.pk,
        ("SYNTHETIC COFFEE", "active"): later.batch.pk,
        ("SYNTHETIC FUEL", "active"): later.batch.pk,
    }
    first.batch.refresh_from_db()
    later.batch.refresh_from_db()
    assert first.batch.status == ImportBatch.Status.ARCHIVED
    assert later.batch.status == ImportBatch.Status.ACTIVE


def test_undo_archives_rows_no_later_batch_contains():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    later = commit(user, account, BAKERY, FUEL)
    assert later.duplicate_count == 0

    undo_import_batch(user, account.pk, first.batch.pk)

    assert rows_by_memo(account) == {
        ("SYNTHETIC GROCER", "archived"): first.batch.pk,
        ("SYNTHETIC COFFEE", "archived"): first.batch.pk,
        ("SYNTHETIC BAKERY", "active"): later.batch.pk,
        ("SYNTHETIC FUEL", "active"): later.batch.pk,
    }


def test_undo_ignores_a_later_batch_with_the_same_row_on_another_account():
    user, person = make_owner()
    account = make_account(person)
    other = make_account(person, "Synthetic Savings")
    first = commit(user, account, GROCER, COFFEE)
    # The same file rows on another account are new there, not duplicates, and
    # a duplicate skipped there says nothing about this account.
    commit(user, other, GROCER, COFFEE)
    other_later = commit(user, other, GROCER, COFFEE)
    assert other_later.duplicate_count == 2

    undo_import_batch(user, account.pk, first.batch.pk)

    assert not Transaction.objects.filter(account=account, status="active").exists()
    assert Transaction.objects.filter(account=account, import_batch=first.batch, status="archived").count() == 2
    assert Transaction.objects.filter(account=other, status="active").count() == 2


def test_undo_does_not_rehome_to_an_earlier_or_an_undone_batch():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER)
    second = commit(user, account, GROCER, COFFEE)
    third = commit(user, account, COFFEE)
    assert (second.duplicate_count, third.duplicate_count) == (1, 1)
    undo_import_batch(user, account.pk, third.batch.pk)

    # The only batch that skipped the coffee row is undone, and the first batch
    # is earlier than the second, so nothing covers the second batch's coffee row.
    undo_import_batch(user, account.pk, second.batch.pk)

    assert rows_by_memo(account) == {
        ("SYNTHETIC GROCER", "active"): first.batch.pk,
        ("SYNTHETIC COFFEE", "archived"): second.batch.pk,
    }


def test_undo_rehomes_only_as_many_repeated_rows_as_the_later_file_holds():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, COFFEE, COFFEE, COFFEE)
    later = commit(user, account, COFFEE, COFFEE)
    assert (later.new_count, later.duplicate_count) == (0, 2)

    undo_import_batch(user, account.pk, first.batch.pk)

    coffee = Transaction.objects.filter(account=account, description="SYNTHETIC COFFEE")
    assert coffee.filter(status="active", import_batch=later.batch).count() == 2
    assert coffee.filter(status="archived", import_batch=first.batch).count() == 1


def test_a_row_two_later_batches_skipped_survives_each_undo_until_the_last():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, COFFEE)
    second = commit(user, account, COFFEE)
    third = commit(user, account, COFFEE)

    undo_import_batch(user, account.pk, first.batch.pk)
    assert rows_by_memo(account) == {("SYNTHETIC COFFEE", "active"): second.batch.pk}
    undo_import_batch(user, account.pk, second.batch.pk)
    assert rows_by_memo(account) == {("SYNTHETIC COFFEE", "active"): third.batch.pk}
    undo_import_batch(user, account.pk, third.batch.pk)
    assert rows_by_memo(account) == {("SYNTHETIC COFFEE", "archived"): third.batch.pk}


def test_a_duplicate_another_active_row_still_stands_in_for_is_not_rehomed():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, COFFEE)
    second = commit(user, account, COFFEE, COFFEE)
    third = commit(user, account, COFFEE)
    assert (second.new_count, second.duplicate_count) == (1, 1)
    assert third.duplicate_count == 1

    # The third file holds one coffee row; the second batch's own row still
    # stands in for it, so the first batch's row moves only to the second batch.
    undo_import_batch(user, account.pk, first.batch.pk)

    coffee = Transaction.objects.filter(account=account, status="active")
    assert coffee.filter(import_batch=second.batch).count() == 2
    assert not coffee.filter(import_batch=third.batch).exists()


def test_reimporting_an_undone_file_counts_the_rehomed_rows_as_duplicates():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    commit(user, account, COFFEE, FUEL)
    undo_import_batch(user, account.pk, first.batch.pk)

    again = commit(user, account, GROCER, COFFEE)

    assert (again.new_count, again.duplicate_count) == (1, 1)
    assert Transaction.objects.filter(account=account, status="active").count() == 3


def test_undo_records_what_was_rehomed_in_the_audit_event_and_row_history():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    later = commit(user, account, COFFEE, FUEL)

    undo_import_batch(user, account.pk, first.batch.pk)

    event = AuditEvent.objects.get(action=AuditEvent.Action.IMPORT_UNDONE)
    assert event.metadata == {
        "batch_id": first.batch.pk,
        "row_count": 1,
        "rehomed_count": 1,
        "format": "huntington",
    }
    coffee = Transaction.objects.get(account=account, description="SYNTHETIC COFFEE")
    history = TransactionCorrectionHistory.objects.get(transaction=coffee)
    assert history.field_name == "import_batch"
    assert history.actor == person
    assert (history.previous_display, history.new_display) == (
        f"Huntington Bank import {first.batch.pk}",
        f"Huntington Bank import {later.batch.pk}",
    )
    grocer = Transaction.objects.get(account=account, description="SYNTHETIC GROCER")
    assert not TransactionCorrectionHistory.objects.filter(transaction=grocer).exists()


def test_undo_page_says_how_many_rows_a_later_import_kept(client):
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    commit(user, account, COFFEE, FUEL)
    client.force_login(user)

    response = client.post(
        reverse("csv-import-undo", kwargs={"account_id": account.pk, "batch_id": first.batch.pk}), follow=True
    )

    assert response.status_code == 200
    assert "1 row stayed because a later import also contains it." in response.content.decode()
