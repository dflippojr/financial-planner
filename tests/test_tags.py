from datetime import date
from urllib.parse import urlencode
import io
import json
import zipfile

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from finance.category_services import (
    ensure_household_categories,
    income_and_spending_totals,
    link_refund,
    refresh_transfer_pairs,
    split_transaction,
)
from finance.csv_import.parser import Mapping, read_csv
from finance.csv_import.services import commit_csv_import
from finance.export import collect_export_tables, write_export_zip
from finance.lifecycle_services import delete_account
from finance.models import (
    Account,
    Household,
    ImportBatch,
    Membership,
    Person,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
    TransactionTag,
)
from finance.tag_services import add_tag, archive_tag, set_transaction_note_and_tags


PASSWORD = "Synthetic-passphrase-42!"
RANGE = {"date_from": "2026-01-01", "date_to": "2026-01-31"}


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", account_type=Account.Type.CHECKING, scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    transaction_date=date(2026, 1, 10),
    amount_minor=-1000,
    description="Synthetic row",
    fingerprint=None,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}-{description}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        currency="USD",
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def signed_client(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_tag_names_are_unique_per_household_ignoring_case():
    owner = make_person("owner")
    household = make_household(owner)
    Tag.objects.create(household=household, name="Vacation 2026")
    with pytest.raises(IntegrityError), transaction.atomic():
        Tag.objects.create(household=household, name="vacation 2026")
    other = make_household(make_person("other"), name="Other Household")
    Tag.objects.create(household=other, name="Vacation 2026")


@pytest.mark.django_db
def test_note_and_tags_are_not_recorded_in_correction_history():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account)
    tag = add_tag(owner, "vacation 2026")
    set_transaction_note_and_tags(owner, txn.pk, note="Hotel deposit", tag_ids=[tag.pk])
    txn.refresh_from_db()
    assert txn.note == "Hotel deposit"
    assert list(txn.tags.values_list("name", flat=True)) == ["vacation 2026"]
    assert not TransactionCorrectionHistory.objects.filter(transaction=txn).exists()


@pytest.mark.django_db
def test_archived_tag_stays_applied_but_cannot_be_newly_applied():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    tagged = make_transaction(owner, account, description="Tagged trip")
    other = make_transaction(owner, account, description="Later purchase", fingerprint="c" * 64)
    tag = add_tag(owner, "vacation 2026")
    set_transaction_note_and_tags(owner, tagged.pk, note="", tag_ids=[tag.pk])
    archive_tag(owner, tag.pk)
    set_transaction_note_and_tags(owner, tagged.pk, note="still tagged", tag_ids=[])
    tagged.refresh_from_db()
    assert tagged.note == "still tagged"
    assert list(tagged.tags.values_list("pk", flat=True)) == [tag.pk]
    with pytest.raises(PermissionDenied):
        set_transaction_note_and_tags(owner, other.pk, note="", tag_ids=[tag.pk])


@pytest.mark.django_db
def test_tag_filter_totals_reconcile_across_list_spending_and_cash_flow():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    checking = make_account(owner, name="Synthetic Checking")
    savings = make_account(owner, name="Synthetic Savings", account_type=Account.Type.SAVINGS)
    tag = add_tag(owner, "vacation 2026")
    purchase = make_transaction(owner, checking, amount_minor=-8000, description="Synthetic trip card")
    split_transaction(
        owner,
        purchase.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -5000},
            {"category_id": dining.pk, "amount_minor": -3000},
        ),
    )
    refund = make_transaction(owner, checking, amount_minor=1500, description="Synthetic trip refund", fingerprint="d" * 64)
    link_refund(owner, refund.pk, purchase.pk, original_part_id=purchase.splits.get(category=dining).pk)
    transfer_out = make_transaction(owner, checking, amount_minor=-2000, description="Synthetic transfer out", fingerprint="e" * 64)
    transfer_in = make_transaction(owner, savings, amount_minor=2000, description="Synthetic transfer in", fingerprint="f" * 64)
    refresh_transfer_pairs(owner)
    untagged = make_transaction(owner, checking, amount_minor=-9999, description="Synthetic untagged", fingerprint="g" * 64)
    for txn in (purchase, refund, transfer_out, transfer_in):
        set_transaction_note_and_tags(owner, txn.pk, note="trip", tag_ids=[tag.pk])
    expected = income_and_spending_totals(
        owner,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        tag=tag,
    )
    client = signed_client(owner)
    query = urlencode({**RANGE, "tag": tag.pk})
    listing = client.get(f"{reverse('transaction-list')}?{query}")
    spending = client.get(f"{reverse('spending-by-category')}?{query}")
    cash = client.get(f"{reverse('home')}?{query}&grouping=month")
    assert listing.status_code == spending.status_code == cash.status_code == 200
    assert listing.context["list_totals"].spending_minor == expected.spending_minor
    assert listing.context["list_totals"].income_minor == expected.income_minor
    assert spending.context["report"].total_spending_minor == expected.spending_minor
    assert cash.context["report"].summary.spending_minor == expected.spending_minor
    assert cash.context["report"].summary.income_minor == expected.income_minor
    assert expected.spending_minor == 6500
    assert expected.income_minor == 0
    listed = listing.content.decode()
    assert "Synthetic trip card" in listed
    assert "Synthetic untagged" not in listed
    assert "tag=" in cash.context["report"].periods[0].drilldown_url
    assert untagged.pk


@pytest.mark.django_db
def test_tag_filter_and_notes_never_leak_another_members_private_transactions():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    tag = add_tag(owner, "vacation 2026")
    owner_private = make_account(owner, name="Owner Private")
    member_private = make_account(member, name="Member Private")
    shared = make_account(owner, name="Shared", scope=Account.Scope.HOUSEHOLD, household=household)
    secret = make_transaction(member, member_private, amount_minor=-7777, description="Secret synthetic purchase")
    set_transaction_note_and_tags(member, secret.pk, note="Do not leak this note", tag_ids=[tag.pk])
    visible = make_transaction(owner, shared, amount_minor=-1200, description="Shared synthetic trip")
    set_transaction_note_and_tags(owner, visible.pk, note="Shared hotel", tag_ids=[tag.pk])
    owner_only = make_transaction(owner, owner_private, amount_minor=-400, description="Owner private trip", fingerprint="c" * 64)
    set_transaction_note_and_tags(owner, owner_only.pk, note="Owner private note", tag_ids=[tag.pk])
    owner_client = signed_client(owner)
    member_client = signed_client(member)
    query = urlencode({**RANGE, "tag": tag.pk})
    owner_page = owner_client.get(f"{reverse('transaction-list')}?{query}").content.decode()
    member_page = member_client.get(f"{reverse('transaction-list')}?{query}").content.decode()
    assert "Secret synthetic purchase" not in owner_page
    assert "Do not leak this note" not in owner_page
    assert "Owner private trip" in owner_page
    assert "Owner private note" not in member_page
    assert "Owner private trip" not in member_page
    assert "Shared synthetic trip" in member_page
    assert "Shared hotel" in member_page
    member_totals = member_client.get(f"{reverse('home')}?{query}&grouping=month").context["report"].summary
    owner_totals = owner_client.get(f"{reverse('home')}?{query}&grouping=month").context["report"].summary
    assert member_totals.spending_minor == 8977
    assert owner_totals.spending_minor == 1600
    export_tables = collect_export_tables(owner)
    notes = {row["id"]: row["note"] for row in export_tables["transactions"]}
    assert secret.pk not in notes
    assert notes[visible.pk] == "Shared hotel"
    assert notes[owner_only.pk] == "Owner private note"


@pytest.mark.django_db
def test_reimport_leaves_notes_and_tags_unchanged():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    csv_bytes = b"When,Memo,Amount,Currency\n01/10/2026,SYNTHETIC GROCER,-12.34,USD\n"
    mapping = Mapping(
        date_column="When",
        description_column="Memo",
        date_format="mdy_slash_4",
        number_format="dot_comma",
        amount_mode="signed",
        amount_column="Amount",
        currency_column="Currency",
    )
    first = commit_csv_import(
        owner.user,
        account.pk,
        content=csv_bytes,
        document=read_csv(csv_bytes),
        mapping=mapping,
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    txn = Transaction.objects.get(import_batch=first.batch)
    tag = add_tag(owner, "vacation 2026")
    set_transaction_note_and_tags(owner, txn.pk, note="Keep this note", tag_ids=[tag.pk])
    second = commit_csv_import(
        owner.user,
        account.pk,
        content=csv_bytes,
        document=read_csv(csv_bytes),
        mapping=mapping,
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    txn.refresh_from_db()
    assert second.new_count == 0
    assert txn.note == "Keep this note"
    assert list(txn.tags.values_list("name", flat=True)) == ["vacation 2026"]


@pytest.mark.django_db
def test_export_includes_notes_and_tags_and_account_deletion_removes_them():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, amount_minor=-2500, description="Synthetic tagged spend")
    tag = add_tag(owner, "vacation 2026")
    set_transaction_note_and_tags(owner, txn.pk, note="Airport parking", tag_ids=[tag.pk])
    tables = collect_export_tables(owner)
    txn_row = next(row for row in tables["transactions"] if row["id"] == txn.pk)
    assert txn_row["note"] == "Airport parking"
    assert any(row["name"] == "vacation 2026" for row in tables["tags"])
    assert any(row["transaction_id"] == txn.pk and row["tag_name"] == "vacation 2026" for row in tables["transaction_tags"])
    payload = write_export_zip(owner)
    archive = zipfile.ZipFile(io.BytesIO(payload))
    txn_json = json.loads(archive.read("transactions.json"))
    match = next(row for row in txn_json if row["id"] == txn.pk)
    assert match["note"] == "Airport parking"
    tags_json = json.loads(archive.read("tags.json"))
    assert any(row["name"] == "vacation 2026" for row in tags_json)
    account_id = account.pk
    delete_account(owner, account_id)
    assert not Transaction.objects.filter(pk=txn.pk).exists()
    assert not TransactionTag.objects.filter(transaction_id=txn.pk).exists()
    assert Tag.objects.filter(pk=tag.pk).exists()


@pytest.mark.django_db
def test_tags_page_and_edit_page_let_members_manage_tags():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    account = make_account(owner, scope=Account.Scope.HOUSEHOLD, household=household)
    txn = make_transaction(owner, account)
    client = signed_client(member)
    listed = client.get(reverse("tag-list"))
    assert listed.status_code == 200
    added = client.post(reverse("tag-list"), {"action": "add", "name": "vacation 2026"})
    assert added.status_code == 302
    tag = Tag.objects.get(household=household, name="vacation 2026")
    saved = client.post(
        reverse("transaction-note-tags", args=(txn.pk,)),
        {"note": "Cabin rental", "tags": [str(tag.pk)], "new_tag": "souvenirs"},
    )
    assert saved.status_code == 302
    txn.refresh_from_db()
    assert txn.note == "Cabin rental"
    assert set(txn.tags.values_list("name", flat=True)) == {"vacation 2026", "souvenirs"}
    page = client.get(reverse("transaction-list"))
    body = page.content.decode()
    assert "vacation 2026" in body
    assert "Cabin rental" in body
    archived = client.post(reverse("tag-list"), {"action": "archive", "tag_id": str(tag.pk)})
    assert archived.status_code == 302
    tag.refresh_from_db()
    assert tag.is_archived


@pytest.mark.django_db
def test_tags_page_adds_renames_and_archives_and_refuses_duplicates():
    from finance.tag_services import TAG_EXISTS

    owner = make_person("owner")
    household = make_household(owner)
    client = signed_client(owner)
    url = reverse("tag-list")

    assert client.post(url, {"action": "add", "name": "Vacation 2026"}).status_code == 302
    vacation = Tag.objects.get(household=household, name="Vacation 2026")
    client.post(url, {"action": "add", "name": "Reimbursable"})

    duplicate_add = client.post(url, {"action": "add", "name": "vacation 2026"})
    assert duplicate_add.status_code == 200
    assert TAG_EXISTS.encode() in duplicate_add.content
    assert Tag.objects.filter(household=household).count() == 2

    renamed = client.post(url, {"action": "rename", "tag_id": str(vacation.pk), "name": "Trip 2026"})
    assert renamed.status_code == 302
    vacation.refresh_from_db()
    assert vacation.name == "Trip 2026"

    clash = client.post(url, {"action": "rename", "tag_id": str(vacation.pk), "name": "REIMBURSABLE"})
    assert clash.status_code == 200
    assert TAG_EXISTS.encode() in clash.content
    vacation.refresh_from_db()
    assert vacation.name == "Trip 2026"

    assert client.post(url, {"action": "archive", "tag_id": str(vacation.pk)}).status_code == 302
    vacation.refresh_from_db()
    assert vacation.is_archived


@pytest.mark.django_db
def test_tag_names_are_validated_and_other_households_tags_are_out_of_reach():
    from finance.tag_services import TAG_NAME_ERROR, rename_tag

    owner = make_person("owner")
    make_household(owner)
    outsider = make_person("outsider")
    other_household = make_household(outsider, name="Other Household")
    theirs = Tag.objects.create(household=other_household, name="Theirs")

    with pytest.raises(ValidationError, match=TAG_NAME_ERROR):
        add_tag(owner, "   ")
    with pytest.raises(ValidationError):
        add_tag(owner, "x" * 81)
    with pytest.raises(PermissionDenied):
        rename_tag(owner, theirs.pk, "Mine now")
    with pytest.raises(PermissionDenied):
        archive_tag(owner, theirs.pk)
    theirs.refresh_from_db()
    assert theirs.name == "Theirs"
    assert not theirs.is_archived

    response = signed_client(owner).post(
        reverse("tag-list"), {"action": "rename", "tag_id": str(theirs.pk), "name": "Mine now"}
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_note_page_creates_a_tag_inline_and_reports_a_duplicate_new_tag():
    from finance.tag_services import TAG_EXISTS

    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account)
    Tag.objects.create(household=household, name="Vacation 2026")
    client = signed_client(owner)
    url = reverse("transaction-note-tags", args=(txn.pk,))

    saved = client.post(url, {"note": "Synthetic note", "new_tag": "Reimbursable"})
    assert saved.status_code == 302
    txn.refresh_from_db()
    assert txn.note == "Synthetic note"
    assert list(txn.tags.values_list("name", flat=True)) == ["Reimbursable"]

    duplicate = client.post(url, {"note": "Changed", "new_tag": "vacation 2026"})
    assert duplicate.status_code == 200
    assert TAG_EXISTS.encode() in duplicate.content
    txn.refresh_from_db()
    assert txn.note == "Synthetic note"
