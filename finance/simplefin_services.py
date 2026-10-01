from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from finance.csv_import.fingerprint import transaction_fingerprint
from finance.encryption import decrypt_access_url, encrypt_access_url
from finance.lifecycle_services import _DENIED, _person_for, lock_actor_household
from finance.models import (
    Account,
    AccountLink,
    BalanceSnapshot,
    ImportBatch,
    Membership,
    SimpleFinConnection,
    Transaction,
)
from finance.simplefin_client import claim_access_url, fetch_accounts
from finance.simplefin_errors import SimpleFinError, provider_errors

MAX_BIGINT = 2**63 - 1


def _owned_connection(person, connection_id):
    connection = SimpleFinConnection.objects.filter(pk=connection_id, owner=person).first()
    if connection is None:
        raise PermissionDenied(_DENIED)
    return connection


def default_cutover_date(account) -> date:
    latest = (
        Transaction.objects.filter(account=account, status=Transaction.Status.ACTIVE)
        .order_by("-transaction_date")
        .values_list("transaction_date", flat=True)
        .first()
    )
    if latest is None:
        return timezone.localdate()
    return latest + timedelta(days=1)


def decode_setup_token(token: str) -> str:
    compact = (token or "").strip()
    if not compact:
        raise SimpleFinError("Paste a SimpleFIN setup token.")
    padding = "=" * ((4 - len(compact) % 4) % 4)
    try:
        decoded = base64.b64decode(compact + padding, validate=False).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise SimpleFinError("That setup token is not a valid SimpleFIN token.") from None
    parts = urlsplit(decoded)
    if parts.scheme != "https" or not parts.netloc:
        raise SimpleFinError("That setup token is not a valid SimpleFIN token.")
    return decoded


def _mode_for_account_type(account_type: str) -> str:
    if account_type == Account.Type.INVESTMENT:
        return AccountLink.Mode.BALANCES_ONLY
    return AccountLink.Mode.TRANSACTIONS


def _iso4217_usd(value: str) -> str:
    code = (value or "").strip().upper()
    if "://" in (value or "") or len(code) != 3 or not code.isalpha():
        raise SimpleFinError("That SimpleFIN account uses a currency this app does not store.")
    if code != "USD":
        raise SimpleFinError("That SimpleFIN account uses a currency this app does not store.")
    return code


def decimal_to_minor(value: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise SimpleFinError("SimpleFIN sent an amount that could not be stored.") from exc
    if not amount.is_finite():
        raise SimpleFinError("SimpleFIN sent an amount that could not be stored.")
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise SimpleFinError("SimpleFIN sent an amount that could not be stored.")
    minor_int = int(minor)
    if abs(minor_int) > MAX_BIGINT:
        raise SimpleFinError("SimpleFIN sent an amount that could not be stored.")
    return minor_int


def posted_date(posted) -> date | None:
    try:
        stamp = int(posted)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    moment = datetime.fromtimestamp(stamp, tz=dt_timezone.utc)
    return timezone.localtime(moment).date()


def _connection_name_by_id(payload: dict) -> dict[str, str]:
    names = {}
    for item in payload.get("connections") or []:
        if isinstance(item, dict) and item.get("conn_id"):
            names[str(item["conn_id"])] = str(item.get("name") or "")
    return names


def listed_accounts(payload: dict) -> list[dict]:
    names = _connection_name_by_id(payload)
    rows = []
    for item in payload.get("accounts") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        conn_id = str(item.get("conn_id") or "")
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        reported_type = extra.get("type") or extra.get("account-type") or ""
        if not isinstance(reported_type, str):
            reported_type = ""
        rows.append(
            {
                "id": str(item["id"]),
                "name": str(item.get("name") or "Account"),
                "institution": names.get(conn_id) or str(item.get("conn_name") or ""),
                "type": reported_type[:80] or "Not provided by SimpleFIN",
                "currency": str(item.get("currency") or ""),
            }
        )
    return rows


@transaction.atomic
def claim_connection(principal, setup_token: str) -> SimpleFinConnection:
    person = _person_for(principal)
    lock_actor_household(person)
    if SimpleFinConnection.objects.filter(owner=person).exists():
        raise SimpleFinError("Disconnect the existing SimpleFIN connection before adding another.")
    claim_url = decode_setup_token(setup_token)
    access_url = claim_access_url(claim_url)
    return SimpleFinConnection.objects.create(
        owner=person,
        encrypted_access_url=encrypt_access_url(access_url),
    )


def load_remote_accounts(connection: SimpleFinConnection) -> tuple[list[dict], list[str]]:
    access_url = decrypt_access_url(connection.encrypted_access_url)
    payload = fetch_accounts(access_url, balances_only=True)
    return listed_accounts(payload), provider_errors(payload)


def _visible_linkable_account(person, account_id):
    account = (
        Account.objects.visible_to(person)
        .filter(pk=account_id, status=Account.Status.ACTIVE, archived_at__isnull=True)
        .first()
    )
    if account is None:
        raise PermissionDenied(_DENIED)
    return account


def _create_linked_account(person, *, name, account_type, sharing):
    membership = Membership.objects.filter(person=person, ended_at__isnull=True).first()
    if sharing == Account.Scope.HOUSEHOLD:
        if membership is None:
            raise PermissionDenied(_DENIED)
        return Account.objects.create(
            name=name,
            account_type=account_type,
            owner=person,
            scope=Account.Scope.HOUSEHOLD,
            household=membership.household,
            currency="USD",
        )
    return Account.objects.create(
        name=name,
        account_type=account_type,
        owner=person,
        scope=Account.Scope.PRIVATE,
        household=None,
        currency="USD",
    )


@transaction.atomic
def save_account_links(principal, connection_id, choices: list[dict]) -> list[AccountLink]:
    person = _person_for(principal)
    lock_actor_household(person)
    connection = (
        SimpleFinConnection.objects.select_for_update().filter(pk=connection_id, owner=person).first()
    )
    if connection is None:
        raise PermissionDenied(_DENIED)
    created = []
    for choice in choices:
        action = choice.get("action")
        simplefin_account_id = str(choice.get("simplefin_account_id") or "")
        if not simplefin_account_id or action == "ignore":
            continue
        if action == "link":
            if not choice.get("account_id"):
                raise SimpleFinError("Choose an account to link.")
            account = _visible_linkable_account(person, choice["account_id"])
            if AccountLink.objects.filter(account=account).exclude(connection=connection).exists():
                raise SimpleFinError("That account is already linked.")
            cutover = choice.get("cutover_date") or default_cutover_date(account)
            mode = _mode_for_account_type(account.account_type)
            link, _created = AccountLink.objects.update_or_create(
                connection=connection,
                simplefin_account_id=simplefin_account_id,
                defaults={"account": account, "cutover_date": cutover, "mode": mode},
            )
            created.append(link)
            continue
        if action == "create":
            account_type = choice.get("account_type")
            if account_type not in Account.Type.values:
                raise SimpleFinError("Choose a valid account type.")
            name = (choice.get("name") or "").strip()
            if not name:
                raise SimpleFinError("Name the new account.")
            sharing = choice.get("sharing") or Account.Scope.PRIVATE
            account = _create_linked_account(
                person, name=name, account_type=account_type, sharing=sharing
            )
            link = AccountLink.objects.create(
                connection=connection,
                account=account,
                simplefin_account_id=simplefin_account_id,
                cutover_date=choice.get("cutover_date") or timezone.localdate(),
                mode=_mode_for_account_type(account_type),
            )
            created.append(link)
            continue
        raise SimpleFinError("Choose how to use each SimpleFIN account.")
    return created


def _sync_interval():
    return timedelta(seconds=max(1, int(settings.SIMPLEFIN_SYNC_MIN_INTERVAL_SECONDS)))


def _rate_limited(connection: SimpleFinConnection, *, ignore_rate_limit: bool) -> bool:
    if ignore_rate_limit or connection.last_sync_at is None:
        return False
    return timezone.now() < connection.last_sync_at + _sync_interval()


def _batch_hash(connection_id, account_id, synced_at) -> str:
    payload = f"simplefin\n{connection_id}\n{account_id}\n{synced_at.isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_pending(item: dict) -> bool:
    if item.get("pending") is True:
        return True
    try:
        return int(item.get("posted") or 0) <= 0
    except (TypeError, ValueError):
        return True


def _accounts_by_simplefin_id(payload: dict) -> dict[str, dict]:
    found = {}
    for item in payload.get("accounts") or []:
        if isinstance(item, dict) and item.get("id"):
            found[str(item["id"])] = item
    return found


def _upsert_snapshot(account, *, snapshot_date, amount_minor, currency, batch):
    BalanceSnapshot.objects.update_or_create(
        account=account,
        snapshot_date=snapshot_date,
        source=BalanceSnapshot.Source.SIMPLEFIN,
        defaults={
            "amount_minor": amount_minor,
            "currency": currency,
            "import_batch": batch,
        },
    )


def _import_transactions(person, account, link, remote, batch) -> int:
    existing_ids = set(
        Transaction.objects.filter(
            account=account,
            status=Transaction.Status.ACTIVE,
        )
        .exclude(source_transaction_id="")
        .values_list("source_transaction_id", flat=True)
    )
    created = []
    row_number = 1
    for item in remote.get("transactions") or []:
        if not isinstance(item, dict):
            continue
        if _is_pending(item):
            continue
        source_id = str(item.get("id") or "")
        if not source_id or source_id in existing_ids:
            continue
        txn_date = posted_date(item.get("posted"))
        if txn_date is None or txn_date < link.cutover_date:
            continue
        amount_minor = decimal_to_minor(str(item.get("amount")))
        description = str(item.get("description") or "SimpleFIN transaction")
        original = {
            "id": source_id,
            "posted": item.get("posted"),
            "amount": str(item.get("amount")),
            "description": description,
            "pending": item.get("pending"),
        }
        created.append(
            Transaction(
                account=account,
                import_batch=batch,
                transaction_date=txn_date,
                amount_minor=amount_minor,
                currency="USD",
                description=description,
                kind=Transaction.Kind.CASH_FLOW,
                source_row_number=row_number,
                source_transaction_id=source_id,
                fingerprint=transaction_fingerprint(account.pk, txn_date, amount_minor, description),
                original_fields=original,
            )
        )
        existing_ids.add(source_id)
        row_number += 1
    if created:
        Transaction.objects.bulk_create(created)
    return len(created)


def _ensure_batch(person, account, connection, synced_at, start, end):
    return ImportBatch.objects.create(
        account=account,
        imported_by=person,
        source=ImportBatch.Source.SIMPLEFIN,
        source_file_sha256=_batch_hash(connection.pk, account.pk, synced_at),
        date_range_start=start,
        date_range_end=end,
    )


def _sync_one_link(person, connection, link, remote, synced_at) -> int:
    account = link.account
    currency = _iso4217_usd(str(remote.get("currency") or ""))
    if account.currency != currency:
        raise SimpleFinError("That SimpleFIN account uses a currency this app does not store.")
    balance_date = posted_date(remote.get("balance-date")) or timezone.localdate()
    amount_minor = decimal_to_minor(str(remote.get("balance")))
    start = link.cutover_date
    end = max(start, balance_date, timezone.localdate())
    batch = _ensure_batch(person, account, connection, synced_at, start, end)
    imported = 0
    if link.mode == AccountLink.Mode.TRANSACTIONS:
        imported = _import_transactions(person, account, link, remote, batch)
    _upsert_snapshot(
        account,
        snapshot_date=balance_date,
        amount_minor=amount_minor,
        currency=currency,
        batch=batch,
    )
    return imported


@transaction.atomic
def sync_connection(principal, connection_id, *, ignore_rate_limit=False) -> dict:
    person = _person_for(principal)
    lock_actor_household(person)
    connection = (
        SimpleFinConnection.objects.select_for_update().filter(pk=connection_id, owner=person).first()
    )
    if connection is None:
        raise PermissionDenied(_DENIED)
    if _rate_limited(connection, ignore_rate_limit=ignore_rate_limit):
        raise SimpleFinError("Wait 15 minutes between Sync now requests.")
    now = timezone.now()
    access_url = decrypt_access_url(connection.encrypted_access_url)
    links = list(AccountLink.objects.select_related("account").filter(connection=connection))
    start_epoch = None
    if links:
        earliest = min(link.cutover_date for link in links)
        start_local = timezone.make_aware(
            datetime.combine(earliest, datetime.min.time()),
            timezone.get_current_timezone(),
        )
        start_epoch = int(start_local.timestamp())
    try:
        payload = fetch_accounts(access_url, start_date=start_epoch, end_date=int(now.timestamp()) + 1)
    except SimpleFinError as exc:
        connection.last_sync_at = now
        connection.last_sync_result = str(exc)
        connection.disabled = True
        connection.save(update_fields=("last_sync_at", "last_sync_result", "disabled"))
        raise
    errors = provider_errors(payload)
    remote_accounts = _accounts_by_simplefin_id(payload)
    imported = 0
    try:
        for link in links:
            remote = remote_accounts.get(link.simplefin_account_id)
            if remote is None:
                continue
            imported += _sync_one_link(person, connection, link, remote, now)
    except SimpleFinError as exc:
        connection.last_sync_at = now
        connection.last_sync_result = str(exc)
        connection.save(update_fields=("last_sync_at", "last_sync_result"))
        raise
    from finance.category_services import refresh_transfer_pairs
    from finance.recurring_services import refresh_recurring_series

    refresh_transfer_pairs(person)
    refresh_recurring_series(person)
    summary = f"Synced {imported} new transaction(s)."
    if errors:
        summary = f"{summary} {errors[0]}"
    connection.last_sync_at = now
    connection.last_sync_result = summary[:500]
    connection.disabled = False
    connection.save(update_fields=("last_sync_at", "last_sync_result", "disabled"))
    return {"imported": imported, "errors": errors, "result": connection.last_sync_result}


@transaction.atomic
def disconnect_connection(principal, connection_id) -> None:
    person = _person_for(principal)
    lock_actor_household(person)
    connection = (
        SimpleFinConnection.objects.select_for_update().filter(pk=connection_id, owner=person).first()
    )
    if connection is None:
        raise PermissionDenied(_DENIED)
    connection.delete()


def sync_all_connections() -> int:
    """Daily job: sync every enabled connection. Returns the number attempted."""
    count = 0
    for connection in SimpleFinConnection.objects.filter(disabled=False).select_related("owner"):
        try:
            sync_connection(connection.owner, connection.pk, ignore_rate_limit=True)
        except SimpleFinError:
            pass
        count += 1
    return count
