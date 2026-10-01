from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from finance.csv_import.parser import Mapping, preview_csv, read_csv
from finance.csv_import.services import classify_overlap, commit_csv_import, undo_import_batch
from finance.models import Account, Household, ImportBatch, Membership, Person, Transaction


CSV = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC GROCER,-12.34,USD\n"
REPEAT_CSV = (
    b"When,Memo,Amount,Currency\n"
    b"09/27/2026,SYNTHETIC COFFEE,-4.50,USD\n"
    b"09/27/2026,SYNTHETIC COFFEE,-4.50,USD\n"
)
INVALID_CSV = b"When,Memo,Amount,Currency\nnot-a-date,SYNTHETIC SKIP,-1.00,USD\n09/28/2026,SYNTHETIC KEEP,-2.00,USD\n"


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


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password="Synthetic-passphrase-42!")
    return user, Person.objects.create(user=user, display_name=f"{username.title()} Example")


def commit(user, account, content, **kwargs):
    document = read_csv(content)
    return commit_csv_import(
        user,
        account.pk,
        content=content,
        document=document,
        mapping=mapping(),
        source=kwargs.get("source", ImportBatch.Source.HUNTINGTON),
        date_range_start=kwargs.get("date_range_start", date(2026, 9, 1)),
        date_range_end=kwargs.get("date_range_end", date(2026, 9, 30)),
    )


@pytest.mark.django_db
def test_repeated_purchases_import_once_then_overlap_is_duplicate():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    other = Account.objects.create(name="Synthetic Savings", account_type="savings", owner=person)

    first = commit(user, account, REPEAT_CSV)
    classified = classify_overlap(account, preview_csv(read_csv(REPEAT_CSV), mapping()))
    other_classified = classify_overlap(other, preview_csv(read_csv(REPEAT_CSV), mapping()))

    assert first.new_count == 2
    assert first.duplicate_count == 0
    assert Transaction.objects.filter(account=account, status="active").count() == 2
    assert classified.new_count == 0
    assert classified.duplicate_count == 2
    assert other_classified.new_count == 2
    assert commit(user, account, REPEAT_CSV).new_count == 0
    assert Transaction.objects.filter(account=account, status="active").count() == 2


@pytest.mark.django_db
def test_partial_overlap_keeps_the_unmatched_repeat():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    commit(user, account, CSV.replace(b"SYNTHETIC GROCER", b"SYNTHETIC COFFEE").replace(b"-12.34", b"-4.50"))

    result = commit(user, account, REPEAT_CSV)

    assert result.new_count == 1
    assert result.duplicate_count == 1
    assert Transaction.objects.filter(account=account, status="active").count() == 2


@pytest.mark.django_db
def test_invalid_rows_are_counted_and_never_imported():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)

    result = commit(user, account, INVALID_CSV)

    assert result.invalid_count == 1
    assert result.new_count == 1
    txn = Transaction.objects.get()
    assert txn.description == "SYNTHETIC KEEP"
    assert txn.source_row_number == 3
    assert txn.original_fields["Memo"] == "SYNTHETIC KEEP"


@pytest.mark.django_db
def test_vanguard_rows_are_neutral_investment_activity():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Brokerage", account_type="investment", owner=person)

    commit(user, account, CSV, source=ImportBatch.Source.VANGUARD)

    assert Transaction.objects.get().kind == Transaction.Kind.INVESTMENT_ACTIVITY


@pytest.mark.django_db
def test_household_member_sees_shared_overlap_and_cannot_touch_private():
    owner_user, owner = make_person("owner")
    member_user, member = make_person("member")
    stranger_user, _stranger = make_person("stranger")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    shared = Account.objects.create(
        name="Shared Card",
        account_type="credit_card",
        owner=owner,
        scope="household",
        household=household,
        share_mode="co_owned",
    )
    private = Account.objects.create(name="Owner Private", account_type="checking", owner=owner)

    commit(owner_user, shared, CSV)
    overlap = commit(member_user, shared, CSV)
    with pytest.raises(PermissionDenied):
        commit(member_user, private, CSV)
    with pytest.raises(PermissionDenied):
        commit(stranger_user, shared, CSV)

    assert overlap.new_count == 0
    assert Transaction.objects.filter(account=shared, status="active").count() == 1


@pytest.mark.django_db
def test_undo_archives_only_that_batch_and_allows_reimport():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    earlier = commit(user, account, CSV)
    later_csv = b"When,Memo,Amount,Currency\n09/28/2026,SYNTHETIC CAFE,-5.00,USD\n"
    later = commit(user, account, later_csv)

    undo_import_batch(user, account.pk, later.batch.pk)
    later.batch.refresh_from_db()

    assert later.batch.status == ImportBatch.Status.ARCHIVED
    assert Transaction.objects.filter(import_batch=later.batch).get().status == Transaction.Status.ARCHIVED
    earlier.batch.refresh_from_db()
    assert earlier.batch.status == ImportBatch.Status.ACTIVE
    assert Transaction.objects.filter(import_batch=earlier.batch).get().status == Transaction.Status.ACTIVE
    replay = commit(user, account, later_csv)
    assert replay.new_count == 1
    assert Transaction.objects.filter(account=account, status="active").count() == 2
