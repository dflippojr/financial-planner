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


def commit(user, account, *rows, source=ImportBatch.Source.HUNTINGTON):
    content = HEADER + b"".join(rows)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=read_csv(content),
        mapping=mapping(),
        source=source,
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


def test_an_earlier_batch_takes_the_row_when_the_later_batch_it_relied_on_is_undone():
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, COFFEE)
    second = commit(user, account, COFFEE)
    third = commit(user, account, COFFEE, COFFEE)
    assert (third.new_count, third.duplicate_count) == (1, 1)

    # The third batch's own row stands in for the second file's copy, so the
    # first batch's row goes to the third, whose file holds two.
    undo_import_batch(user, account.pk, first.batch.pk)
    coffee = Transaction.objects.filter(account=account, status="active")
    assert coffee.filter(import_batch=third.batch).count() == 2

    # The second batch is earlier than the third and still holds the row: it keeps one.
    result = undo_import_batch(user, account.pk, third.batch.pk)
    assert list(coffee.values_list("import_batch_id", flat=True)) == [second.batch.pk]
    assert (result.rehomed_count, result.holes) == (1, ())


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


def test_undo_page_says_how_many_rows_another_import_kept(client):
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    commit(user, account, COFFEE, FUEL)
    client.force_login(user)

    response = client.post(
        reverse("csv-import-undo", kwargs={"account_id": account.pk, "batch_id": first.batch.pk}), follow=True
    )

    assert response.status_code == 200
    assert "1 row stayed because another active import also contains it." in response.content.decode()


def undo_page(client, user, account, batch):
    client.force_login(user)
    response = client.post(
        reverse("csv-import-undo", kwargs={"account_id": account.pk, "batch_id": batch.pk}), follow=True
    )
    assert response.status_code == 200
    return response.content.decode()


def test_undo_warns_with_the_days_when_a_later_batch_from_before_the_record_covers_them(client):
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE, FUEL)
    later = commit(user, account, COFFEE, BAKERY)
    # A batch committed before migration 0093 recorded nothing about what it skipped.
    ImportBatch.objects.filter(pk=later.batch.pk).update(
        skipped_duplicates=None, date_range_start=date(2026, 9, 28), date_range_end=date(2026, 9, 30)
    )

    page = undo_page(client, user, account, first.batch)

    # Nothing says which rows the later file held, so they are archived as before,
    # and the warning names the removed rows' days that the later batch still covers.
    assert rows_by_memo(account) == {
        ("SYNTHETIC GROCER", "archived"): first.batch.pk,
        ("SYNTHETIC COFFEE", "archived"): first.batch.pk,
        ("SYNTHETIC FUEL", "archived"): first.batch.pk,
        ("SYNTHETIC BAKERY", "active"): later.batch.pk,
    }
    assert (
        "1 removed row dated 2026-09-28 is still marked as covered by the Huntington Bank import "
        "for 2026-09-28 to 2026-09-30. Import that file again to restore it."
    ) in page
    assert "2026-09-27" not in page and "2026-10-02" not in page


def test_undo_warns_instead_of_rehoming_to_a_batch_from_another_source(client):
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    later = commit(user, account, GROCER, COFFEE, FUEL, source=ImportBatch.Source.CAPITAL_ONE)
    assert later.duplicate_count == 2

    page = undo_page(client, user, account, first.batch)

    # The stored original fields are read by the batch's source, so a row never
    # moves to a batch of another source; the gap is named instead.
    assert rows_by_memo(account) == {
        ("SYNTHETIC GROCER", "archived"): first.batch.pk,
        ("SYNTHETIC COFFEE", "archived"): first.batch.pk,
        ("SYNTHETIC FUEL", "active"): later.batch.pk,
    }
    assert (
        "2 removed rows dated 2026-09-27 and 2026-09-28 are still marked as covered by the Capital One import "
        "for 2026-09-01 to 2026-10-31. Import that file again to restore them."
    ) in page
    assert not TransactionCorrectionHistory.objects.exists()


def test_undo_does_not_warn_when_nothing_later_covers_the_removed_rows(client):
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, GROCER, COFFEE)
    commit(user, account, BAKERY, FUEL)

    page = undo_page(client, user, account, first.batch)

    assert "still marked as covered" not in page


def test_the_warning_lists_at_most_ten_days(client):
    user, person = make_owner()
    account = make_account(person)
    rows = [f"09/{day:02d}/2026,SYNTHETIC DAY {day},-1.00,USD\n".encode() for day in range(1, 13)]
    first = commit(user, account, *rows)
    commit(user, account, *rows, source=ImportBatch.Source.CAPITAL_ONE)

    page = undo_page(client, user, account, first.batch)

    assert "12 removed rows dated 2026-09-01, 2026-09-02," in page
    assert "2026-09-10 and 2 more days are still marked as covered by the Capital One import" in page
    assert "2026-09-11" not in page


def test_undoing_the_batch_a_row_moved_to_warns_for_an_earlier_batch_of_another_source(client):
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, COFFEE)
    card = commit(user, account, COFFEE, source=ImportBatch.Source.CAPITAL_ONE)
    third = commit(user, account, COFFEE)
    assert (card.duplicate_count, third.duplicate_count) == (1, 1)

    # The row moves past the other-source batch to the third; that batch's copy still has a row.
    page = undo_page(client, user, account, first.batch)
    assert rows_by_memo(account) == {("SYNTHETIC COFFEE", "active"): third.batch.pk}
    assert "still marked as covered" not in page

    # Undoing the third removes the last row the earlier other-source batch relied on.
    page = undo_page(client, user, account, third.batch)

    assert rows_by_memo(account) == {("SYNTHETIC COFFEE", "archived"): third.batch.pk}
    assert (
        "1 removed row dated 2026-09-28 is still marked as covered by the "
        "Capital One import for 2026-09-01 to 2026-10-31."
    ) in page


def test_undoing_the_batch_a_row_moved_to_warns_for_an_earlier_batch_with_no_record(client):
    user, person = make_owner()
    account = make_account(person)
    first = commit(user, account, COFFEE)
    legacy = commit(user, account, COFFEE)
    third = commit(user, account, COFFEE)
    # A batch committed before migration 0093 recorded nothing about what it skipped.
    ImportBatch.objects.filter(pk=legacy.batch.pk).update(skipped_duplicates=None)

    page = undo_page(client, user, account, first.batch)
    assert rows_by_memo(account) == {("SYNTHETIC COFFEE", "active"): third.batch.pk}
    assert "still marked as covered" not in page

    page = undo_page(client, user, account, third.batch)

    assert rows_by_memo(account) == {("SYNTHETIC COFFEE", "archived"): third.batch.pk}
    assert (
        "1 removed row dated 2026-09-28 is still marked as covered by the "
        "Huntington Bank import for 2026-09-01 to 2026-10-31."
    ) in page

