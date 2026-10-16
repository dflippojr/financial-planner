from datetime import date
from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError

from finance.csv_import.parser import Mapping, preview_csv, read_csv
from finance.csv_import.services import classify_overlap, commit_csv_import
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
@pytest.mark.parametrize("source", [
    ImportBatch.Source.VANGUARD, ImportBatch.Source.SIMPLEFIN, ImportBatch.Source.OFX,
    ImportBatch.Source.MANUAL, "unknown",
])
def test_a_mapped_csv_refuses_sources_that_do_not_fit_its_parser(source):
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)

    with pytest.raises(ValidationError, match="supported import source"):
        commit(user, account, CSV, source=source)

    assert not ImportBatch.objects.exists()
    assert not Transaction.objects.exists()


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


FORMULA_CSV = (
    b"When,Memo,Amount,Currency\n"
    b"09/27/2026,=SYNTHETIC FORMULA,-1.00,USD\n"
    b"09/28/2026,-SYNTHETIC ACH,-2.00,USD\n"
)


@pytest.mark.django_db
def test_formula_like_descriptions_are_stored_raw():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)

    commit(user, account, FORMULA_CSV)

    assert sorted(Transaction.objects.values_list("description", flat=True)) == [
        "-SYNTHETIC ACH", "=SYNTHETIC FORMULA",
    ]


@pytest.mark.django_db
def test_file_imported_before_raw_descriptions_reimports_as_duplicates():
    user, person = make_person("owner")
    account = Account.objects.create(name="Synthetic Checking", account_type="checking", owner=person)
    commit(user, account, FORMULA_CSV)
    # Recreate what imports stored before #503: an apostrophe on formula-like
    # text, hashed in that form. The upgrade then strips the apostrophe.
    for row in Transaction.objects.all():
        row.description = "'" + row.description
        row.save(update_fields=["description"])
    simplefin_batch = ImportBatch.objects.create(
        account=account, imported_by=person, source=ImportBatch.Source.SIMPLEFIN,
        source_file_sha256="c" * 64, date_range_start=date(2026, 9, 1), date_range_end=date(2026, 9, 30),
    )
    simplefin_row = Transaction.objects.create(
        account=account, import_batch=simplefin_batch, transaction_date=date(2026, 9, 29),
        amount_minor=-300, currency="USD", description="'=SYNTHETIC FEED TEXT", fingerprint="d" * 64,
        source_row_number=1, original_fields={},
    )
    literal = Transaction.objects.create(
        account=account, import_batch=Transaction.objects.first().import_batch, transaction_date=date(2026, 9, 29),
        amount_minor=-400, currency="USD", description="'-SYNTHETIC QUOTED", fingerprint="e" * 64,
        source_row_number=3, original_fields={"Memo": "'-SYNTHETIC QUOTED"},
    )
    joined = Transaction.objects.create(
        account=account, import_batch=literal.import_batch, transaction_date=date(2026, 9, 29),
        amount_minor=-500, currency="USD", description="'-SYNTHETIC PAYEE - Monthly memo", fingerprint="f" * 64,
        source_row_number=4, original_fields={"Payee": " '-SYNTHETIC PAYEE", "Memo": "Monthly memo "},
    )
    import_module("finance.migrations.0071_raw_import_descriptions").strip_import_formula_escape(django_apps, None)

    reimport = commit(user, account, FORMULA_CSV)

    assert (reimport.new_count, reimport.duplicate_count) == (0, 2)
    assert sorted(
        Transaction.objects.exclude(pk__in=(simplefin_row.pk, literal.pk, joined.pk)).values_list("description", flat=True)
    ) == ["-SYNTHETIC ACH", "=SYNTHETIC FORMULA"]
    simplefin_row.refresh_from_db()
    literal.refresh_from_db()
    joined.refresh_from_db()
    assert simplefin_row.description == "'=SYNTHETIC FEED TEXT"
    assert literal.description == "'-SYNTHETIC QUOTED"
    assert joined.description == "'-SYNTHETIC PAYEE - Monthly memo"
