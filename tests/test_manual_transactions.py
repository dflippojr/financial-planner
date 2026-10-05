import csv
import io
import zipfile
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.budget_services import progress_snapshot, save_budget
from finance.cash_flow import cash_flow_report, spending_by_category_report
from finance.category_services import ensure_household_categories, income_and_spending_totals
from finance.csv_import.parser import Mapping, read_csv
from finance.csv_import.services import categorize_imported_batch, commit_csv_import, undo_import_batch
from finance.export import write_export_zip
from finance.manual_entry_services import add_manual_transaction, delete_manual_transaction
from finance.models import (
    Account,
    Budget,
    Category,
    Household,
    ImportBatch,
    Membership,
    Person,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
)
from finance.rule_services import apply_rule, save_category_rule


PASSWORD = "Synthetic-passphrase-42!"
ENTRY_DATE = date(2026, 9, 27)
CSV = b"When,Memo,Amount,Currency\n09/27/2026,SYNTHETIC GROCER,-12.34,USD\n"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Cash", account_type=Account.Type.CHECKING, household=None):
    scope = Account.Scope.HOUSEHOLD if household else Account.Scope.PRIVATE
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if household else "",
    )


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


def category(household, name):
    return Category.objects.get(household=household, name=name)


def add_payload(account, **overrides):
    payload = {
        "account": account if isinstance(account, int) else account.pk,
        "transaction_date": ENTRY_DATE.isoformat(),
        "direction": "out",
        "amount": "12.34",
        "description": "SYNTHETIC GROCER",
        "category": "",
        "note": "",
    }
    payload.update(overrides)
    return payload


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


def import_csv(person, account):
    return commit_csv_import(
        person.user,
        account.pk,
        content=CSV,
        document=read_csv(CSV),
        mapping=mapping(),
        source=ImportBatch.Source.HUNTINGTON,
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 30),
    )


def september_totals(person, account):
    return income_and_spending_totals(
        person, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), accounts=[account]
    )


@pytest.mark.django_db
def test_member_adds_to_private_and_household_accounts_and_reports_include_them():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner)
    shared = make_account(owner, name="Synthetic Joint", household=household)
    groceries = category(household, "Groceries")
    budget = save_budget(
        owner.user,
        {
            "scope": Budget.Scope.PRIVATE,
            "category": groceries,
            "amount_minor": 10_000,
            "effective_month": date(2026, 9, 1),
            "rollover_enabled": False,
        },
    )
    client = signed_in(owner)

    response = client.post(
        reverse("transaction-add"), add_payload(private, category=groceries.pk, note="Synthetic receipt lost")
    )
    assert response.status_code == 302
    response = client.post(
        reverse("transaction-add"),
        add_payload(shared, direction="in", amount="50.00", description="SYNTHETIC CASH GIFT"),
    )
    assert response.status_code == 302

    spent = Transaction.objects.get(account=private)
    received = Transaction.objects.get(account=shared)
    assert spent.amount_minor == -1234
    assert spent.category_id == groceries.pk
    assert spent.category_source == Transaction.CategorySource.MANUAL
    assert spent.note == "Synthetic receipt lost"
    assert spent.original_fields == {"entry": "manual"}
    assert spent.source_row_number == 1
    assert spent.import_batch.source == ImportBatch.Source.MANUAL
    assert spent.import_batch.imported_by == owner
    assert spent.import_batch.date_range_start == spent.import_batch.date_range_end == ENTRY_DATE
    assert received.amount_minor == 5000

    listing = client.get(reverse("transaction-list"))
    assert "SYNTHETIC GROCER" in listing.content.decode()
    assert "SYNTHETIC CASH GIFT" in listing.content.decode()
    assert "Manual" in listing.content.decode()

    report = cash_flow_report(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    assert report.periods[0].income_minor == 5000
    assert report.periods[0].spending_minor == 1234
    by_category = spending_by_category_report(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    assert ("Groceries", 1234) in [(row.name, row.spending_minor) for row in by_category.rows]
    assert progress_snapshot(budget, date(2026, 9, 1), owner).spent_minor == 1234

    # The household member sees the shared entry, but not the private one.
    member_list = signed_in(member).get(reverse("transaction-list")).content.decode()
    assert "SYNTHETIC CASH GIFT" in member_list
    assert "SYNTHETIC GROCER" not in member_list


@pytest.mark.django_db
def test_household_member_may_add_to_a_shared_account():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    shared = make_account(owner, name="Synthetic Joint", household=household)

    entry = add_manual_transaction(
        member, shared.pk, transaction_date=ENTRY_DATE, amount_minor=-500, description="SYNTHETIC CHECK 101"
    )

    assert entry.account_id == shared.pk
    assert entry.import_batch.imported_by == member


@pytest.mark.django_db
def test_other_member_cannot_add_see_or_delete_on_a_private_account():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    private = make_account(owner)
    other_tag = Tag.objects.create(household=Household.objects.create(name="Other"), name="Synthetic other")
    entry = add_manual_transaction(
        owner, private.pk, transaction_date=ENTRY_DATE, amount_minor=-1234, description="SYNTHETIC GROCER"
    )
    client = signed_in(member)

    form_page = client.get(reverse("transaction-add")).content.decode()
    assert "Synthetic Cash" not in form_page

    forged = client.post(reverse("transaction-add"), add_payload(private))
    missing = client.post(reverse("transaction-add"), add_payload(private.pk + 999))
    assert forged.status_code == missing.status_code == 200
    assert forged.context["form"].errors == missing.context["form"].errors
    assert Transaction.objects.filter(account=private).count() == 1

    forged_delete = client.post(reverse("transaction-delete", args=[entry.pk]))
    missing_delete = client.post(reverse("transaction-delete", args=[entry.pk + 999]))
    assert forged_delete.status_code == missing_delete.status_code == 404
    entry.refresh_from_db()
    assert entry.status == Transaction.Status.ACTIVE
    assert client.get(reverse("transaction-edit", args=[entry.pk])).status_code == 404
    assert "SYNTHETIC GROCER" not in client.get(reverse("transaction-list")).content.decode()

    with pytest.raises(PermissionDenied):
        add_manual_transaction(
            member, private.pk, transaction_date=ENTRY_DATE, amount_minor=-1, description="SYNTHETIC FORGED"
        )
    with pytest.raises(PermissionDenied):
        delete_manual_transaction(member, entry.pk)
    own = make_account(member, name="Synthetic Member Cash")
    with pytest.raises(PermissionDenied):
        add_manual_transaction(
            member,
            own.pk,
            transaction_date=ENTRY_DATE,
            amount_minor=-1,
            description="SYNTHETIC TAG",
            tag_ids=[other_tag.pk],
        )


@pytest.mark.django_db
def test_only_active_checking_savings_and_card_accounts_accept_entries():
    owner = make_person("owner")
    make_household(owner)
    investment = make_account(owner, name="Synthetic Brokerage", account_type=Account.Type.INVESTMENT)
    loan = make_account(owner, name="Synthetic Loan", account_type=Account.Type.LOAN)
    card = make_account(owner, name="Synthetic Card", account_type=Account.Type.CREDIT_CARD)
    archived = make_account(owner, name="Synthetic Closed")
    archived.status = Account.Status.ARCHIVED
    archived.archived_at = timezone.now()
    archived.save()

    for account in (investment, loan, archived):
        with pytest.raises(PermissionDenied):
            add_manual_transaction(
                owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1, description="SYNTHETIC"
            )
    assert add_manual_transaction(
        owner, card.pk, transaction_date=ENTRY_DATE, amount_minor=-1, description="SYNTHETIC CARD"
    ).account_id == card.pk

    choices = signed_in(owner).get(reverse("transaction-add")).context["form"].fields["account"].queryset
    assert list(choices) == [card]


@pytest.mark.django_db
def test_form_rejects_future_dates_and_zero_amounts():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    client = signed_in(owner)
    tomorrow = timezone.localdate() + timedelta(days=1)

    future = client.post(reverse("transaction-add"), add_payload(account, transaction_date=tomorrow.isoformat()))
    zero = client.post(reverse("transaction-add"), add_payload(account, amount="0"))
    negative = client.post(reverse("transaction-add"), add_payload(account, amount="-5"))

    assert "transaction_date" in future.context["form"].errors
    assert "amount" in zero.context["form"].errors
    assert "amount" in negative.context["form"].errors
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_rules_apply_like_an_import_and_a_form_category_wins():
    owner = make_person("owner")
    household = make_household(owner)
    account = make_account(owner)
    rule = save_category_rule(
        owner,
        owner_kind="personal",
        description_contains="grocer",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=category(household, "Groceries").pk,
        priority=0,
    )
    apply_rule(owner, rule.pk)

    ruled = add_manual_transaction(
        owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1234, description="SYNTHETIC GROCER"
    )
    chosen = add_manual_transaction(
        owner,
        account.pk,
        transaction_date=ENTRY_DATE,
        amount_minor=-1234,
        description="SYNTHETIC GROCER",
        category_id=category(household, "Dining").pk,
    )
    imported = import_csv(owner, make_account(owner, name="Synthetic Imported"))
    categorize_imported_batch(owner, imported.batch)

    ruled.refresh_from_db()
    chosen.refresh_from_db()
    imported_row = Transaction.objects.get(import_batch=imported.batch)
    assert ruled.category_id == imported_row.category_id == category(household, "Groceries").pk
    assert ruled.category_source == imported_row.category_source == Transaction.CategorySource.RULE
    assert chosen.category_id == category(household, "Dining").pk
    assert chosen.category_source == Transaction.CategorySource.MANUAL


@pytest.mark.django_db
def test_matching_import_neither_drops_nor_merges_the_manual_entry():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    before = add_manual_transaction(
        owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1234, description="SYNTHETIC GROCER"
    )

    result = import_csv(owner, account)

    assert result.new_count == 1
    assert result.duplicate_count == 0
    assert Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE).count() == 2
    # A manual entry added after the import does not match it either, and a
    # reimport still sees only the imported row as a duplicate.
    after = add_manual_transaction(
        owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1234, description="SYNTHETIC GROCER"
    )
    assert before.fingerprint != after.fingerprint
    again = import_csv(owner, account)
    assert again.new_count == 0
    assert again.duplicate_count == 1
    assert Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE).count() == 3


@pytest.mark.django_db
def test_deleting_a_manual_entry_removes_it_from_totals_and_records_history():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    entry = add_manual_transaction(
        owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1234, description="SYNTHETIC GROCER"
    )
    assert september_totals(owner, account).spending_minor == 1234
    client = signed_in(owner)
    edit_page = client.get(reverse("transaction-edit", args=[entry.pk])).content.decode()
    assert "Delete manual transaction" in edit_page

    response = client.post(reverse("transaction-delete", args=[entry.pk]))

    assert response.status_code == 302
    entry.refresh_from_db()
    assert entry.status == Transaction.Status.ARCHIVED
    assert entry.import_batch.status == ImportBatch.Status.ARCHIVED
    assert september_totals(owner, account).spending_minor == 0
    history = TransactionCorrectionHistory.objects.get(transaction=entry)
    assert history.field_name == TransactionCorrectionHistory.Field.DELETED
    assert history.actor == owner
    assert client.post(reverse("transaction-delete", args=[entry.pk])).status_code == 404


@pytest.mark.django_db
def test_imported_rows_keep_their_rules_and_manual_batches_skip_import_undo():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    imported = import_csv(owner, account)
    imported_row = Transaction.objects.get(import_batch=imported.batch)
    entry = add_manual_transaction(
        owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-500, description="SYNTHETIC CASH"
    )
    client = signed_in(owner)

    assert client.post(reverse("transaction-delete", args=[imported_row.pk])).status_code == 404
    assert "Delete manual transaction" not in client.get(
        reverse("transaction-edit", args=[imported_row.pk])
    ).content.decode()
    with pytest.raises(PermissionDenied):
        undo_import_batch(owner, account.pk, entry.import_batch_id)
    imported_row.refresh_from_db()
    entry.refresh_from_db()
    assert imported_row.status == entry.status == Transaction.Status.ACTIVE
    # Manual batches are not listed as imports on the import page.
    preview = client.get(reverse("csv-import-preview", args=[account.pk]))
    assert list(preview.context["import_batches"]) == [imported.batch]


@pytest.mark.django_db
def test_export_includes_manual_entries_with_their_source():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    entry = add_manual_transaction(
        owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1234, description="SYNTHETIC GROCER"
    )
    payload = write_export_zip(owner)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        transactions_name = next(name for name in names if name.endswith("transactions.csv"))
        batches_name = next(name for name in names if name.endswith("import_batches.csv"))
        rows = list(csv.DictReader(io.StringIO(archive.read(transactions_name).decode("utf-8"))))
        batches = list(csv.DictReader(io.StringIO(archive.read(batches_name).decode("utf-8"))))
    assert [(row["id"], row["import_source"]) for row in rows] == [(str(entry.pk), "manual")]
    assert [row["source"] for row in batches] == ["manual"]


@pytest.mark.django_db
def test_file_imports_cannot_claim_the_manual_source():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    document = read_csv(CSV)
    csv_mapping = mapping()

    with pytest.raises(ValidationError):
        commit_csv_import(
            owner.user,
            account.pk,
            content=CSV,
            document=document,
            mapping=csv_mapping,
            source=ImportBatch.Source.MANUAL,
            date_range_start=date(2026, 9, 1),
            date_range_end=date(2026, 9, 30),
        )
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_a_manual_entry_does_not_count_as_an_imported_statement():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    add_manual_transaction(
        owner, account.pk, transaction_date=ENTRY_DATE, amount_minor=-1234, description="SYNTHETIC GROCER"
    )

    report = cash_flow_report(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))

    assert report.periods[0].spending_minor == 1234
    assert report.periods[0].missing_import is True
    import_csv(owner, account)
    report = cash_flow_report(owner, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    assert report.periods[0].missing_import is False


@pytest.mark.django_db
def test_entry_takes_household_tags_and_refuses_bad_input():
    owner = make_person("owner")
    outsider = make_person("outsider")
    household = make_household(owner)
    other_household = make_household(outsider)
    account = make_account(owner)
    tag = Tag.objects.create(household=household, name="Synthetic trip")
    client = signed_in(owner)

    response = client.post(reverse("transaction-add"), add_payload(account, tags=[tag.pk], add_another="1"))

    assert response.status_code == 302
    assert response.url == reverse("transaction-add")
    assert list(Transaction.objects.get(account=account).tags.all()) == [tag]

    def add(**overrides):
        fields = {"transaction_date": ENTRY_DATE, "amount_minor": -100, "description": "SYNTHETIC"}
        fields.update(overrides)
        return add_manual_transaction(owner, account.pk, **fields)

    foreign_category_id = category(other_household, "Groceries").pk
    with pytest.raises(PermissionDenied):
        add(category_id=foreign_category_id)
    with pytest.raises(ValidationError):
        add(description="   ")
    with pytest.raises(ValidationError):
        add(note="x" * 2001)
    assert Transaction.objects.filter(account=account).count() == 1
