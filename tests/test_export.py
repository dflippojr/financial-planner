import csv
import io
import json
import logging
import zipfile
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from tests.helpers import stamp_recent_auth

from finance.category_services import (
    assign_category,
    ensure_household_categories,
    refresh_transfer_pairs,
)
from finance.export import (
    ENTITY_FILES,
    _balance_snapshot_model,
    _snapshot_rows,
    collect_export_tables,
    minor_from_decimal_string,
    money_decimal,
    write_export_zip,
)
from finance.models import (
    Account,
    Household,
    ImportBatch,
    Membership,
    Person,
    RecurringSeries,
    RecurringSeriesMember,
    RefundLink,
    Transaction,
    TransferPair,
)
from finance.recurring_services import refresh_recurring_series


PASSWORD = "Synthetic-passphrase-42!"


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
    transaction_date=date(2026, 1, 2),
    amount_minor=-1000,
    description="Synthetic row",
    fingerprint=None,
    source_transaction_id="",
    kind=Transaction.Kind.CASH_FLOW,
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=f"{account.pk:064x}"[-64:].rjust(64, "e"),
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{transaction_date}-{description}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=kind,
        source_row_number=2,
        source_transaction_id=source_transaction_id,
        fingerprint=digest,
        original_fields={"Synthetic Amount": str(amount_minor)},
    )


def _zip_from_bytes(payload):
    return zipfile.ZipFile(io.BytesIO(payload))


def _json_rows(archive, name):
    return json.loads(archive.read(f"{name}.json").decode())


def _csv_rows(archive, name):
    text = archive.read(f"{name}.csv").decode()
    return list(csv.DictReader(io.StringIO(text)))


@pytest.mark.django_db
def test_export_zip_contains_csv_json_and_readme_matching_visible_rows():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    private = make_account(owner, name="Owner Private")
    shared = make_account(owner, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    groceries = household.categories.get(name="Groceries")
    visible = make_transaction(
        owner,
        private,
        amount_minor=-1234,
        description="Synthetic groceries",
        source_transaction_id="synth-txn-19",
    )
    assign_category(owner, visible.pk, groceries.pk)
    make_transaction(owner, shared, amount_minor=5000, description="Synthetic paycheck")
    savings = make_account(owner, name="Owner Savings", account_type=Account.Type.SAVINGS)
    make_transaction(owner, private, amount_minor=-2000, description="Synthetic transfer out")
    make_transaction(owner, savings, amount_minor=2000, description="Synthetic transfer in")
    refresh_transfer_pairs(owner)
    refresh_recurring_series(owner)

    tables = collect_export_tables(owner)
    payload = write_export_zip(owner)
    archive = _zip_from_bytes(payload)
    names = set(archive.namelist())

    assert "README.txt" in names
    assert b"negative" in archive.read("README.txt").lower()
    for entity in ENTITY_FILES:
        assert f"{entity}.csv" in names
        assert f"{entity}.json" in names
        json_rows = _json_rows(archive, entity)
        csv_rows = _csv_rows(archive, entity)
        assert len(json_rows) == len(csv_rows) == len(tables[entity])
    txn_json = _json_rows(archive, "transactions")
    match = next(row for row in txn_json if row["id"] == visible.pk)
    assert match["amount_minor"] == -1234
    assert match["amount_decimal"] == "-12.34"
    assert match["currency"] == "USD"
    assert match["source_transaction_id"] == "synth-txn-19"
    assert match["category_name"] == "Groceries"
    csv_match = next(row for row in _csv_rows(archive, "transactions") if int(row["id"]) == visible.pk)
    assert int(csv_match["amount_minor"]) == -1234
    assert csv_match["amount_decimal"] == "-12.34"
    assert TransferPair.objects.visible_to(owner).count() == len(_json_rows(archive, "transfer_pairs"))


@pytest.mark.django_db
def test_export_never_includes_another_members_private_data():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    make_account(owner, name="Owner Private Ledger")
    member_private = make_account(member, name="Member Secret Ledger")
    shared = make_account(owner, name="Shared Ledger", scope=Account.Scope.HOUSEHOLD, household=household)
    secret_txn = make_transaction(member, member_private, amount_minor=-7777, description="Secret synthetic purchase")
    secret_hash = secret_txn.import_batch.source_file_sha256
    visible_out = make_transaction(owner, shared, amount_minor=-1500, description="Synthetic shared out")
    hidden_in = make_transaction(member, member_private, amount_minor=1500, description="Synthetic private in")
    first, second = (visible_out, hidden_in) if visible_out.pk < hidden_in.pk else (hidden_in, visible_out)
    TransferPair.objects.create(
        leg_a=first,
        leg_b=second,
        status=TransferPair.Status.CONFIRMED,
        kind=TransferPair.Kind.TRANSFER,
        confidence=TransferPair.Confidence.HIGH,
        reasons=["synthetic pair"],
    )
    original = make_transaction(member, member_private, amount_minor=-4000, description="Secret original")
    refund = make_transaction(owner, shared, amount_minor=4000, description="Shared refund")
    RefundLink.objects.create(refund=refund, original=original)
    RecurringSeriesMember.objects.create(
        series=RecurringSeries.objects.create(
            person=member,
            merchant_key="secret merchant",
            display_name="Secret series",
            cadence=RecurringSeries.Cadence.MONTHLY,
            typical_amount_minor=-1599,
            status=RecurringSeries.Status.CONFIRMED,
            confidence=RecurringSeries.Confidence.HIGH,
            reasons=["synthetic"],
            fingerprint="d" * 64,
        ),
        transaction=secret_txn,
    )

    payload = write_export_zip(owner)
    text = payload.decode("latin-1")
    archive = _zip_from_bytes(payload)
    txn_ids = {row["id"] for row in _json_rows(archive, "transactions")}
    account_names = {row["name"] for row in _json_rows(archive, "accounts")}
    pair_ids = {row["id"] for row in _json_rows(archive, "transfer_pairs")}
    series_keys = {row["merchant_key"] for row in _json_rows(archive, "recurring_series")}
    refund_originals = {row["refund_original_id"] for row in _json_rows(archive, "transactions")}

    assert "Member Secret Ledger" not in account_names
    assert "Owner Private Ledger" in account_names
    assert "Shared Ledger" in account_names
    assert secret_txn.pk not in txn_ids
    assert original.pk not in txn_ids
    assert pair_ids == set()
    assert "secret merchant" not in series_keys
    assert original.pk not in refund_originals
    assert "Member Secret Ledger" not in text
    assert secret_hash not in text
    refund_row = next(row for row in _json_rows(archive, "transactions") if row["id"] == refund.pk)
    assert refund_row["refund_original_id"] is None


@pytest.mark.django_db
def test_archived_transactions_are_exported_and_flagged():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    txn = make_transaction(owner, account, amount_minor=-250)
    txn.status = Transaction.Status.ARCHIVED
    txn.archived_at = timezone.now()
    txn.save(update_fields=("status", "archived_at", "updated_at"))
    txn.import_batch.status = ImportBatch.Status.ARCHIVED
    txn.import_batch.archived_at = timezone.now()
    txn.import_batch.save(update_fields=("status", "archived_at"))

    rows = {row["id"]: row for row in collect_export_tables(owner)["transactions"]}
    batches = {row["id"]: row for row in collect_export_tables(owner)["import_batches"]}

    assert rows[txn.pk]["status"] == "archived"
    assert rows[txn.pk]["archived_at"] is not None
    assert batches[txn.import_batch_id]["status"] == "archived"


@pytest.mark.django_db
def test_amounts_round_trip_through_csv_and_json():
    owner = make_person("owner")
    make_household(owner)
    account = make_account(owner)
    make_transaction(owner, account, amount_minor=-1, description="one cent out")
    make_transaction(owner, account, amount_minor=999999, description="large in")
    archive = _zip_from_bytes(write_export_zip(owner))
    for row in _json_rows(archive, "transactions"):
        assert minor_from_decimal_string(row["amount_decimal"]) == row["amount_minor"]
        assert money_decimal(row["amount_minor"]) == row["amount_decimal"]
    for row in _csv_rows(archive, "transactions"):
        minor = int(row["amount_minor"])
        assert minor_from_decimal_string(row["amount_decimal"]) == minor
        assert money_decimal(minor) == row["amount_decimal"]


@pytest.mark.django_db
def test_export_view_streams_zip_without_storing_cache_or_logging_content(caplog):
    owner = make_person("owner")
    make_household(owner)
    make_transaction(owner, make_account(owner), amount_minor=-4321, description="Logged-secret-should-not-appear")
    client = Client()
    client.force_login(owner.user)
    stamp_recent_auth(client)
    caplog.set_level(logging.DEBUG)
    with patch("finance.views.timezone.localdate", return_value=date(2026, 10, 1)):
        response = client.post(reverse("account-export"))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    assert "no-store" in response["Cache-Control"]
    assert response["Content-Disposition"] == 'attachment; filename="financial-planner-export-20261001.zip"'
    archive = _zip_from_bytes(response.content)
    assert "transactions.json" in archive.namelist()
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Logged-secret-should-not-appear" not in logged
    assert "-4321" not in logged

    page = client.get(reverse("account-settings"))
    assert b"Export data" in page.content
    from_settings = client.post(reverse("account-settings"), {"action": "export"})
    assert from_settings.status_code == 200
    assert from_settings["Content-Type"] == "application/zip"


@pytest.mark.django_db
def test_export_requires_auth_post_and_csrf():
    owner = make_person("owner")
    make_household(owner)
    anonymous = Client().post(reverse("account-export"))
    logged = Client()
    logged.force_login(owner.user)
    stamp_recent_auth(logged)
    get_denied = logged.get(reverse("account-export"))
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner.user)
    stamp_recent_auth(csrf_client)
    csrf_client.get(reverse("account-settings"))
    token = csrf_client.cookies["csrftoken"].value
    denied = csrf_client.post(reverse("account-export"))
    allowed = csrf_client.post(reverse("account-export"), {"csrfmiddlewaretoken": token})

    assert anonymous.status_code == 302
    assert get_denied.status_code == 405
    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_money_decimal_helpers():
    assert money_decimal(-1234) == "-12.34"
    assert money_decimal(50) == "0.50"
    assert minor_from_decimal_string("-12.34") == -1234
    assert _balance_snapshot_model() is None


@pytest.mark.django_db
def test_snapshot_rows_keep_only_visible_accounts():
    owner = make_person("owner")
    other = make_person("other")
    make_household(owner, other)
    visible = make_account(owner, name="Visible Snap")
    hidden = make_account(other, name="Hidden Snap")
    snaps = [
        SimpleNamespace(pk=1, account_id=visible.pk, captured_at=date(2026, 2, 1), amount_minor=250, currency="USD"),
        SimpleNamespace(pk=2, account_id=hidden.pk, captured_at=date(2026, 2, 1), amount_minor=999, currency="USD"),
    ]

    class Query(list):
        def order_by(self, *_args):
            return self

    class Model:
        class objects:
            @staticmethod
            def filter(account_id__in):
                allowed = {row["pk"] for row in account_id__in}
                return Query(item for item in snaps if item.account_id in allowed)

    rows = _snapshot_rows(owner, Model)
    assert len(rows) == 1
    assert rows[0]["amount_minor"] == 250
    assert rows[0]["amount_decimal"] == "2.50"
    assert rows[0]["account_id"] == visible.pk

    with patch("finance.export._balance_snapshot_model", return_value=Model):
        tables = collect_export_tables(owner)
    assert "balance_snapshots" in tables
    assert tables["balance_snapshots"][0]["id"] == 1
