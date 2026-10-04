"""Store and authorize receipt files. Do not log names, paths, or contents."""

from __future__ import annotations

import secrets
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .lifecycle_services import lock_actor_household
from .models import Account, Person, Receipt, Transaction, _person_for

_DENIED = "Operation is not permitted."
_REJECTED = "That file could not be attached."
_TOO_MANY = "A transaction can have at most 5 receipts."
_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"
_PDF = b"%PDF"
_HEIC_BRANDS = frozenset(
    {
        b"heic",
        b"heif",
        b"heix",
        b"hevc",
        b"hevx",
        b"heim",
        b"heis",
        b"hevm",
        b"hevs",
        b"mif1",
        b"msf1",
    }
)


def receipts_root() -> Path:
    return Path(settings.RECEIPTS_DIR)


def stored_receipt_path(stored_name: str) -> Path:
    if len(stored_name) != 32 or any(ch not in "0123456789abcdef" for ch in stored_name):
        raise PermissionDenied(_DENIED)
    return receipts_root() / stored_name


def sniff_receipt_content_type(data: bytes) -> str | None:
    if data.startswith(_JPEG):
        return Receipt.ContentType.JPEG
    if data.startswith(_PNG):
        return Receipt.ContentType.PNG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return Receipt.ContentType.WEBP
    if data.startswith(_PDF):
        return Receipt.ContentType.PDF
    if _looks_like_heic(data):
        return Receipt.ContentType.HEIC
    return None


def _looks_like_heic(data: bytes) -> bool:
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    size = int.from_bytes(data[:4], "big")
    if size < 16 or size > len(data):
        size = min(len(data), 256)
    brands = {data[8:12]}
    offset = 16
    while offset + 4 <= size:
        brands.add(data[offset : offset + 4])
        offset += 4
    return bool(brands & _HEIC_BRANDS)


def unlink_receipt_files(stored_names):
    for name in stored_names:
        try:
            stored_receipt_path(name).unlink(missing_ok=True)
        except (OSError, PermissionDenied):
            continue


def schedule_receipt_file_removal(stored_names):
    unlink_receipt_files(stored_names)


def delete_receipts_for_transactions(transaction_ids):
    ids = list(transaction_ids)
    if not ids:
        return
    names = list(
        Receipt.objects.filter(transaction_id__in=ids).values_list("stored_name", flat=True)
    )
    Receipt.objects.filter(transaction_id__in=ids).delete()
    schedule_receipt_file_removal(names)


@transaction.atomic
def attach_receipt(principal, transaction_id, uploaded_file) -> Receipt:
    person = _require_person(principal)
    lock_actor_household(person)
    financial_transaction = _lock_editable_transaction(person, transaction_id)
    existing = list(
        Receipt.objects.select_for_update().filter(transaction=financial_transaction).order_by("pk")
    )
    if len(existing) >= settings.RECEIPT_MAX_PER_TRANSACTION:
        raise ValidationError(_TOO_MANY)
    payload = _read_upload(uploaded_file)
    content_type = sniff_receipt_content_type(payload)
    if content_type is None:
        raise ValidationError(_REJECTED)
    original_name = _safe_original_name(getattr(uploaded_file, "name", "") or "")
    stored_name = _unique_stored_name()
    destination = stored_receipt_path(stored_name)
    receipts_root().mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    try:
        receipt = Receipt.objects.create(
            transaction=financial_transaction,
            original_name=original_name,
            stored_name=stored_name,
            content_type=content_type,
            size_bytes=len(payload),
            uploaded_by=person,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return receipt


@transaction.atomic
def remove_receipt(principal, transaction_id, receipt_id):
    person = _require_person(principal)
    lock_actor_household(person)
    financial_transaction = _lock_editable_transaction(person, transaction_id)
    receipt = (
        Receipt.objects.select_for_update()
        .filter(pk=receipt_id, transaction=financial_transaction)
        .first()
    )
    if receipt is None:
        raise PermissionDenied(_DENIED)
    stored_name = receipt.stored_name
    receipt.delete()
    schedule_receipt_file_removal([stored_name])


def visible_receipt_or_none(principal, transaction_id, receipt_id) -> Receipt | None:
    return (
        Receipt.objects.visible_to(principal)
        .filter(pk=receipt_id, transaction_id=transaction_id)
        .select_related("transaction")
        .first()
    )


def _require_person(principal) -> Person:
    person = principal if isinstance(principal, Person) else _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    return person


def _lock_editable_transaction(person, transaction_id) -> Transaction:
    if not Transaction.objects.visible_to(person).filter(
        pk=transaction_id, status=Transaction.Status.ACTIVE
    ).exists():
        raise PermissionDenied(_DENIED)
    seed = Transaction.objects.filter(pk=transaction_id).values("account_id").first()
    if seed is None:
        raise PermissionDenied(_DENIED)
    account = (
        Account.objects.visible_to(person)
        .select_for_update()
        .filter(pk=seed["account_id"])
        .first()
    )
    if account is None:
        raise PermissionDenied(_DENIED)
    financial_transaction = (
        Transaction.objects.select_for_update()
        .filter(pk=transaction_id, account=account, status=Transaction.Status.ACTIVE)
        .first()
    )
    if financial_transaction is None:
        raise PermissionDenied(_DENIED)
    return financial_transaction


def _read_upload(uploaded_file) -> bytes:
    size = getattr(uploaded_file, "size", None)
    if size is None or size < 1 or size > settings.RECEIPT_MAX_BYTES:
        raise ValidationError(_REJECTED)
    uploaded_file.seek(0)
    payload = uploaded_file.read()
    if not payload or len(payload) > settings.RECEIPT_MAX_BYTES:
        raise ValidationError(_REJECTED)
    return payload


def _safe_original_name(name: str) -> str:
    cleaned = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = cleaned.replace("\x00", "")
    if not cleaned or cleaned in {".", ".."}:
        return "receipt"
    return cleaned[:255]


def _unique_stored_name() -> str:
    for _ in range(8):
        candidate = secrets.token_hex(16)
        if not Receipt.objects.filter(stored_name=candidate).exists():
            return candidate
    raise ValidationError(_REJECTED)
