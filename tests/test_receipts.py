from datetime import date, timedelta
from io import BytesIO
import os
from pathlib import Path
import zipfile

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from finance.alert_services import run_daily_alert_pass
from finance.csv_import.services import undo_import_batch
from finance.export import write_export_zip
from finance.lifecycle_services import delete_account, delete_member_data
from finance.models import Account, Household, ImportBatch, Membership, Person, Receipt, Transaction, TransactionSplit
from finance.receipt_services import attach_receipt, remove_receipt, sniff_receipt_content_type


PASSWORD = "Synthetic-passphrase-42!"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 24
WEBP = b"RIFF" + (36).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 24
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00\x00\x00\x00mif1"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_transaction(owner, *, account=None, fingerprint="a" * 64):
    account = account or Account.objects.create(
        name="Synthetic Checking",
        account_type=Account.Type.CHECKING,
        owner=owner,
    )
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=-1234,
        description="Synthetic groceries",
        source_row_number=2,
        fingerprint=fingerprint,
        original_fields={"Synthetic Amount": "-12.34"},
    )


def upload_bytes(name, payload):
    return SimpleUploadedFile(name, payload)


@pytest.mark.django_db
def test_sniff_uses_content_not_the_filename():
    assert sniff_receipt_content_type(JPEG) == Receipt.ContentType.JPEG
    assert sniff_receipt_content_type(PNG) == Receipt.ContentType.PNG
    assert sniff_receipt_content_type(WEBP) == Receipt.ContentType.WEBP
    assert sniff_receipt_content_type(PDF) == Receipt.ContentType.PDF
    assert sniff_receipt_content_type(HEIC) == Receipt.ContentType.HEIC
    assert sniff_receipt_content_type(b"not a receipt") is None


@pytest.mark.django_db
def test_upload_view_download_and_delete(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    txn = make_transaction(owner)
    client = Client()
    client.force_login(owner.user)

    page = client.get(reverse("transaction-edit", args=(txn.pk,)))
    assert page.status_code == 200
    assert b'accept="' in page.content
    assert b'capture="environment"' in page.content

    uploaded = client.post(
        reverse("transaction-receipt-upload", args=(txn.pk,)),
        {"receipt": upload_bytes("synthetic-lunch.jpg", JPEG)},
    )
    assert uploaded.status_code == 302
    receipt = Receipt.objects.visible_to(owner).get()
    assert receipt.original_name == "synthetic-lunch.jpg"
    assert receipt.content_type == Receipt.ContentType.JPEG
    stored = Path(settings.RECEIPTS_DIR) / receipt.stored_name
    assert stored.is_file()
    assert receipt.stored_name != "synthetic-lunch.jpg"

    download = client.get(reverse("transaction-receipt-download", args=(txn.pk, receipt.pk)))
    assert download.status_code == 200
    assert download["Content-Type"] == "image/jpeg"
    assert download["X-Content-Type-Options"] == "nosniff"
    assert "synthetic-lunch.jpg" in download["Content-Disposition"]
    assert "inline" in download["Content-Disposition"].lower()
    assert download.getvalue() == JPEG

    pdf = attach_receipt(owner, txn.pk, upload_bytes("synthetic.pdf", PDF))
    pdf_response = client.get(reverse("transaction-receipt-download", args=(txn.pk, pdf.pk)))
    assert "attachment" in pdf_response["Content-Disposition"].lower()
    assert pdf_response["X-Content-Type-Options"] == "nosniff"
    assert pdf_response["Content-Type"] == "application/pdf"
    assert pdf_response.content == PDF

    deleted = client.post(reverse("transaction-receipt-delete", args=(txn.pk, receipt.pk)))
    assert deleted.status_code == 302
    assert not Receipt.objects.filter(pk=receipt.pk).exists()
    assert stored.is_file()


@pytest.mark.django_db
def test_forged_url_for_another_members_private_receipt_is_404(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    outsider = make_person("outsider")
    txn = make_transaction(owner)
    receipt = attach_receipt(owner, txn.pk, upload_bytes("private.jpg", JPEG))
    client = Client()
    client.force_login(outsider.user)

    download = client.get(reverse("transaction-receipt-download", args=(txn.pk, receipt.pk)))
    delete = client.post(reverse("transaction-receipt-delete", args=(txn.pk, receipt.pk)))
    upload = client.post(
        reverse("transaction-receipt-upload", args=(txn.pk,)),
        {"receipt": upload_bytes("intrusion.jpg", JPEG)},
    )

    assert download.status_code == 404
    assert b"private.jpg" not in download.content
    assert delete.status_code == 404
    assert upload.status_code == 404
    assert Receipt.objects.visible_to(outsider).count() == 0
    assert Receipt.objects.filter(pk=receipt.pk).exists()


@pytest.mark.django_db
def test_oversized_and_mislabeled_files_are_rejected(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    txn = make_transaction(owner)
    client = Client()
    client.force_login(owner.user)
    url = reverse("transaction-receipt-upload", args=(txn.pk,))

    oversized = client.post(url, {"receipt": upload_bytes("huge.jpg", JPEG + b"x" * (10 * 1024 * 1024))})
    mislabeled = client.post(url, {"receipt": upload_bytes("fake.pdf", b"not-a-pdf-or-image")})
    jpeg_named_pdf = client.post(url, {"receipt": upload_bytes("named.pdf", JPEG)})

    assert oversized.status_code == 200
    assert b"could not be attached" in oversized.content
    assert mislabeled.status_code == 200
    assert b"could not be attached" in mislabeled.content
    assert jpeg_named_pdf.status_code == 302
    stored = Receipt.objects.visible_to(owner).get()
    assert stored.content_type == Receipt.ContentType.JPEG
    assert Receipt.objects.count() == 1
    assert list(Path(settings.RECEIPTS_DIR).iterdir()) == [Path(settings.RECEIPTS_DIR) / stored.stored_name]


@pytest.mark.django_db
def test_files_are_removed_with_transaction_account_and_member(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    txn = make_transaction(owner)
    receipt = attach_receipt(owner, txn.pk, upload_bytes("keep-me.jpg", JPEG))
    path = Path(settings.RECEIPTS_DIR) / receipt.stored_name
    assert path.is_file()

    deleted = delete_account(owner, txn.account_id)
    assert deleted
    assert path.is_file()
    assert not Receipt.objects.filter(pk=receipt.pk).exists()

    leftover = make_person("leaving")
    leftover_txn = make_transaction(leftover)
    leftover_receipt = attach_receipt(leftover, leftover_txn.pk, upload_bytes("mine.jpg", PNG))
    leftover_path = Path(settings.RECEIPTS_DIR) / leftover_receipt.stored_name
    delete_member_data(leftover)
    assert leftover_path.is_file()
    assert not Receipt.objects.filter(pk=leftover_receipt.pk).exists()


@pytest.mark.django_db
def test_split_keeps_receipts_on_the_parent(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    from finance.category_services import ensure_household_categories, split_transaction

    ensure_household_categories(household)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    txn = make_transaction(owner)
    receipt = attach_receipt(owner, txn.pk, upload_bytes("parent.jpg", JPEG))
    split_transaction(
        owner,
        txn.pk,
        (
            {"category_id": groceries.pk, "amount_minor": -600},
            {"category_id": dining.pk, "amount_minor": -634},
        ),
    )
    txn.refresh_from_db()
    assert txn.category_source == Transaction.CategorySource.SPLIT
    assert Receipt.objects.visible_to(owner).get().pk == receipt.pk
    assert TransactionSplit.objects.filter(transaction=txn).count() == 2


@pytest.mark.django_db
def test_undo_import_deletes_receipts_on_removed_rows(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    txn = make_transaction(owner)
    receipt = attach_receipt(owner, txn.pk, upload_bytes("imported.jpg", JPEG))
    path = Path(settings.RECEIPTS_DIR) / receipt.stored_name

    undo_import_batch(owner, txn.account_id, txn.import_batch_id)

    assert not Receipt.objects.filter(pk=receipt.pk).exists()
    assert path.is_file()
    txn.refresh_from_db()
    assert txn.status == Transaction.Status.ARCHIVED


@pytest.mark.django_db
def test_export_includes_visible_receipts_only(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    member = make_person("member")
    household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=owner, household=household)
    Membership.objects.create(person=member, household=household)
    private = make_transaction(member, fingerprint="c" * 64)
    shared_account = Account.objects.create(
        name="Shared",
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=Account.Scope.HOUSEHOLD,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED,
    )
    shared = make_transaction(owner, account=shared_account, fingerprint="d" * 64)
    secret = attach_receipt(member, private.pk, upload_bytes("secret.jpg", JPEG))
    visible = attach_receipt(owner, shared.pk, upload_bytes("shared.jpg", PNG))

    payload = write_export_zip(owner)
    archive = zipfile.ZipFile(BytesIO(payload))
    names = set(archive.namelist())
    assert f"receipts/{visible.pk}/shared.jpg" in names
    assert f"receipts/{secret.pk}/secret.jpg" not in names
    assert b"secret.jpg" not in archive.read("receipts.csv")
    assert b"shared.jpg" in archive.read("receipts.csv")


@pytest.mark.django_db
def test_receipt_delete_inside_rolled_back_atomic_keeps_row_and_file(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    owner = make_person("owner")
    txn = make_transaction(owner)
    receipt = attach_receipt(owner, txn.pk, upload_bytes("keep.jpg", JPEG))
    path = Path(settings.RECEIPTS_DIR) / receipt.stored_name

    with pytest.raises(RuntimeError, match="roll back"):
        with transaction.atomic():
            remove_receipt(owner, txn.pk, receipt.pk)
            raise RuntimeError("roll back")

    assert Receipt.objects.filter(pk=receipt.pk).exists()
    assert path.is_file()


@pytest.mark.django_db
def test_sweep_removes_old_orphans_and_keeps_new_and_referenced(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    settings.RECEIPT_ORPHAN_GRACE_HOURS = 48
    owner = make_person("owner")
    txn = make_transaction(owner)
    kept = attach_receipt(owner, txn.pk, upload_bytes("keep.jpg", JPEG))
    referenced = Path(settings.RECEIPTS_DIR) / kept.stored_name
    old_orphan = Path(settings.RECEIPTS_DIR) / ("a" * 32)
    new_orphan = Path(settings.RECEIPTS_DIR) / ("b" * 32)
    junk = Path(settings.RECEIPTS_DIR) / "not-a-receipt-name"
    old_orphan.write_bytes(b"old-orphan")
    new_orphan.write_bytes(b"new-orphan")
    junk.write_bytes(b"junk")
    now = timezone.now()
    old_ts = (now - timedelta(hours=49)).timestamp()
    os.utime(old_orphan, (old_ts, old_ts))
    os.utime(referenced, (old_ts, old_ts))

    run_daily_alert_pass(now=now)

    assert not old_orphan.exists()
    assert new_orphan.is_file()
    assert referenced.is_file()
    assert junk.is_file()


@pytest.mark.django_db
def test_member_data_deletion_leaves_files_until_orphan_sweep(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    settings.RECEIPT_ORPHAN_GRACE_HOURS = 48
    leftover = make_person("leaving")
    leftover_txn = make_transaction(leftover)
    leftover_receipt = attach_receipt(leftover, leftover_txn.pk, upload_bytes("mine.jpg", PNG))
    leftover_path = Path(settings.RECEIPTS_DIR) / leftover_receipt.stored_name

    delete_member_data(leftover)

    assert not Receipt.objects.filter(pk=leftover_receipt.pk).exists()
    assert leftover_path.is_file()

    now = timezone.now()
    old_ts = (now - timedelta(hours=49)).timestamp()
    os.utime(leftover_path, (old_ts, old_ts))
    from finance.receipt_services import sweep_orphan_receipt_files

    sweep_orphan_receipt_files(now=now)
    assert not leftover_path.exists()


@pytest.mark.django_db
def test_the_grace_period_starts_when_an_old_receipt_is_deleted(tmp_path, settings):
    settings.RECEIPTS_DIR = str(tmp_path)
    settings.RECEIPT_ORPHAN_GRACE_HOURS = 48
    owner = make_person("owner")
    txn = make_transaction(owner)
    receipt = attach_receipt(owner, txn.pk, upload_bytes("old.jpg", JPEG))
    path = Path(settings.RECEIPTS_DIR) / receipt.stored_name
    uploaded_long_ago = (timezone.now() - timedelta(days=5)).timestamp()
    os.utime(path, (uploaded_long_ago, uploaded_long_ago))

    remove_receipt(owner, txn.pk, receipt.pk)
    run_daily_alert_pass(now=timezone.now())

    assert path.is_file()


def test_the_sync_container_can_sweep_receipts():
    compose = (Path(__file__).resolve().parent.parent / "compose.yml").read_text()
    scheduler = compose.split("  simplefin-sync:", 1)[1].split("\n  ai-jobs:", 1)[0]

    assert "RECEIPTS_DIR: /receipts" in scheduler
    assert "- receipts:/receipts" in scheduler
