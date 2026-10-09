from __future__ import annotations

import base64
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from finance.alert_email import notify_after_alert_run
from finance.csv_import.fingerprint import transaction_fingerprint
from finance.date_bounds import activity_date_error
from finance.encryption import decrypt_access_url, encrypt_access_url
from finance.audit_services import append_event
from finance.audit_operations import execution, operation, outcome, member_operation
from finance.lifecycle_services import _DENIED, _person_for, create_account, lock_actor_household
from finance.models import (
    Account,
    AuditEvent,
    AccountLink,
    BalanceSnapshot,
    ImportBatch,
    Membership,
    SimpleFinConnection,
    Transaction,
)
from finance.simplefin_client import claim_access_url, fetch_accounts
from finance.simplefin_errors import SimpleFinError, SimpleFinRateLimited, clean_text, provider_errors

logger = logging.getLogger(__name__)

MAX_BIGINT = 2**63 - 1
# Ids are stored in 255-character columns; descriptions are capped so one
# provider row cannot bloat every page that lists it.
MAX_ID_CHARS = 255
MAX_DESCRIPTION_CHARS = 1000
# Minor units of a signed 64-bit column have at most 19 digits, so an amount
# with a larger exponent is rejected before any arithmetic.
MAX_AMOUNT_ADJUSTED_EXPONENT = 18
UNSUPPORTED_CURRENCY = "That SimpleFIN account uses a currency this app does not store."
UNSTORABLE_AMOUNT = "SimpleFIN sent an amount that could not be stored."
INVALID_TOKEN = "That setup token is not a valid SimpleFIN token."
UNSTORABLE_ID = "SimpleFIN sent a transaction id that could not be stored."
ALREADY_CONNECTED = "Disconnect the existing SimpleFIN connection before adding another."
RATE_LIMITED = "Wait 15 minutes between Sync now requests."
LINKS_CHANGED = "Account links changed while SimpleFIN was syncing. Sync again."
UNEXPECTED_FAILURE = "The sync could not be completed. It will be retried on the next scheduled run."


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
    except ValueError:
        raise SimpleFinError(INVALID_TOKEN) from None
    parts = urlsplit(decoded)
    if parts.scheme != "https" or not parts.netloc:
        raise SimpleFinError(INVALID_TOKEN)
    return decoded


def _mode_for_account_type(account_type: str) -> str:
    if account_type in (Account.Type.INVESTMENT, Account.Type.LOAN):
        return AccountLink.Mode.BALANCES_ONLY
    return AccountLink.Mode.TRANSACTIONS


def _iso4217_usd(value: str) -> str:
    code = (value or "").strip().upper()
    if "://" in (value or "") or len(code) != 3 or not code.isalpha():
        raise SimpleFinError(UNSUPPORTED_CURRENCY)
    if code != "USD":
        raise SimpleFinError(UNSUPPORTED_CURRENCY)
    return code


def decimal_to_minor(value: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise SimpleFinError(UNSTORABLE_AMOUNT) from exc
    if not amount.is_finite() or amount.adjusted() > MAX_AMOUNT_ADJUSTED_EXPONENT:
        raise SimpleFinError(UNSTORABLE_AMOUNT)
    with localcontext() as context:
        context.traps[Inexact] = True
        try:
            minor = amount.scaleb(2)
        except Inexact:
            raise SimpleFinError(UNSTORABLE_AMOUNT) from None
    if minor != minor.to_integral_value():
        raise SimpleFinError(UNSTORABLE_AMOUNT)
    minor_int = int(minor)
    if abs(minor_int) > MAX_BIGINT:
        raise SimpleFinError(UNSTORABLE_AMOUNT)
    return minor_int


def _posted_stamp(posted) -> int | None:
    """A positive epoch-seconds value, or None when missing or malformed."""
    if isinstance(posted, bool):
        return None
    try:
        stamp = int(posted)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp if stamp > 0 else None


def posted_date(posted) -> date | None:
    """The local date of a SimpleFIN timestamp, or None when it is unusable or implausible."""
    stamp = _posted_stamp(posted)
    if stamp is None:
        return None
    try:
        day = timezone.localtime(datetime.fromtimestamp(stamp, tz=dt_timezone.utc)).date()
    except (OverflowError, OSError, ValueError):
        return None
    return None if activity_date_error(day) else day


UNREADABLE_CONNECTION = (
    "This connection can no longer be read with the current encryption key. "
    "Disconnect it and connect SimpleFIN again."
)


def _readable_access_url(connection) -> str:
    """Decrypt the access URL, turning a key mismatch into a safe, actionable error."""
    try:
        return decrypt_access_url(connection.encrypted_access_url)
    except ImproperlyConfigured:
        raise SimpleFinError(UNREADABLE_CONNECTION) from None


def _connection_name_by_id(payload: dict) -> dict[str, str]:
    names = {}
    for item in payload.get("connections") or []:
        if isinstance(item, dict) and item.get("conn_id"):
            names[clean_text(item["conn_id"])] = clean_text(item.get("name") or "")[:200]
    return names


def remote_account_key(item: dict) -> str:
    """Stable key for a SimpleFIN account.

    Account ids are unique only within one provider connection, so the key
    includes the connection id when SimpleFIN reports one.
    """
    # Percent-encode each part so a ':' inside an id can never make two
    # different accounts share one key.
    account_id = quote(clean_text(item["id"]), safe="")
    conn_id = quote(clean_text(item.get("conn_id") or ""), safe="")
    return f"{conn_id}:{account_id}" if conn_id else account_id


def _remote_accounts(payload: dict):
    """(key, item) for each usable account; one whose key cannot be stored is left out."""
    for item in payload.get("accounts") or []:
        if isinstance(item, dict) and item.get("id"):
            key = remote_account_key(item)
            if len(key) <= MAX_ID_CHARS:
                yield key, item


def _account_row(key: str, item: dict, names: dict[str, str]) -> dict:
    conn_id = clean_text(item.get("conn_id") or "")
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    reported_type = extra.get("type") or extra.get("account-type") or ""
    if not isinstance(reported_type, str):
        reported_type = ""
    return {
        "id": key,
        "name": clean_text(item.get("name") or "Account")[:200],
        "institution": names.get(conn_id) or clean_text(item.get("conn_name") or "")[:200],
        "type": clean_text(reported_type)[:80] or "Not provided by SimpleFIN",
        "currency": clean_text(item.get("currency") or "")[:10],
    }


def listed_accounts(payload: dict) -> list[dict]:
    names = _connection_name_by_id(payload)
    return [_account_row(key, item, names) for key, item in _remote_accounts(payload)]


def claim_connection(principal, setup_token: str) -> SimpleFinConnection:
    """Claim a setup token and store the encrypted Access URL.

    The outbound claim runs before any transaction or row lock, so a slow
    provider never holds up other household members' writes.
    """
    person = _person_for(principal)
    if SimpleFinConnection.objects.filter(owner=person).exists():
        raise SimpleFinError(ALREADY_CONNECTED)
    claim_url = decode_setup_token(setup_token)
    access_url = claim_access_url(claim_url)
    return _store_claimed_connection(person, access_url)


@transaction.atomic
def _store_claimed_connection(person, access_url: str) -> SimpleFinConnection:
    lock_actor_household(person)
    if SimpleFinConnection.objects.filter(owner=person).exists():
        raise SimpleFinError(ALREADY_CONNECTED)
    connection = SimpleFinConnection.objects.create(
        owner=person,
        encrypted_access_url=encrypt_access_url(access_url),
    )
    append_event(action=AuditEvent.Action.SIMPLEFIN_CONNECTED, actor=person, target_id=connection.pk)
    return connection


def load_remote_accounts(connection: SimpleFinConnection) -> tuple[list[dict], list[str]]:
    access_url = _readable_access_url(connection)
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
        # Shared accounts need a mode (#30); co-owned matches how sharing
        # behaved before modes existed.
        return create_account(person, name=name, account_type=account_type,
                              household=membership.household, share_mode=Account.ShareMode.CO_OWNED)
    return create_account(person, name=name, account_type=account_type)


def _link_existing(person, connection, choice, simplefin_account_id):
    if not choice.get("account_id"):
        raise SimpleFinError("Choose an account to link.")
    account = _visible_linkable_account(person, choice["account_id"])
    if not account.accepts_simplefin():
        raise SimpleFinError("Physical asset accounts are valued manually, not through SimpleFIN.")
    if (
        AccountLink.objects.filter(account=account)
        .exclude(connection=connection, simplefin_account_id=simplefin_account_id)
        .exists()
    ):
        raise SimpleFinError("That account is already linked.")
    cutover = choice.get("cutover_date") or default_cutover_date(account)
    mode = _mode_for_account_type(account.account_type)
    link, _created = AccountLink.objects.update_or_create(
        connection=connection,
        simplefin_account_id=simplefin_account_id,
        defaults={"account": account, "cutover_date": cutover, "mode": mode},
    )
    return link


def _create_and_link(person, connection, choice, simplefin_account_id):
    account_type = choice.get("account_type")
    if account_type not in Account.SIMPLEFIN_TYPES:
        raise SimpleFinError("Choose a valid account type.")
    name = (choice.get("name") or "").strip()
    if not name:
        raise SimpleFinError("Name the new account.")
    sharing = choice.get("sharing") or Account.Scope.PRIVATE
    account = _create_linked_account(person, name=name, account_type=account_type, sharing=sharing)
    # A row that is already linked is re-pointed at the new account. The
    # previously linked account keeps the data it already imported.
    link, _created = AccountLink.objects.update_or_create(
        connection=connection,
        simplefin_account_id=simplefin_account_id,
        defaults={
            "account": account,
            "cutover_date": choice.get("cutover_date") or timezone.localdate(),
            "mode": _mode_for_account_type(account_type),
        },
    )
    return link


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
    link_state = _link_state(connection)
    for choice in choices:
        action = choice.get("action")
        simplefin_account_id = str(choice.get("simplefin_account_id") or "")
        if not simplefin_account_id:
            continue
        if action == "keep":
            # The linked account is archived or no longer visible to this
            # person, so the form cannot show it; leave the link as it is.
            continue
        if action == "ignore":
            # Ignoring an account that is already linked unlinks it, so syncs
            # stop. Data already imported into the app account stays.
            AccountLink.objects.filter(connection=connection, simplefin_account_id=simplefin_account_id).delete()
            continue
        if action == "link":
            created.append(_link_existing(person, connection, choice, simplefin_account_id))
            continue
        if action == "create":
            created.append(_create_and_link(person, connection, choice, simplefin_account_id))
            continue
        raise SimpleFinError("Choose how to use each SimpleFIN account.")
    after = _link_state(connection)
    changed = []
    if {key: value[0] for key, value in link_state.items()} != {key: value[0] for key, value in after.items()}:
        changed.append("account_links")
    if any(key in link_state and link_state[key][1] != value[1] for key, value in after.items()):
        changed.append("cutover")
    if changed:
        append_event(action=AuditEvent.Action.SIMPLEFIN_LINKS_CHANGED, actor=person, target_id=connection.pk,
                     changed_fields=changed)
    return created


def _link_state(connection):
    return {
        row["simplefin_account_id"]: (row["account_id"], row["cutover_date"])
        for row in AccountLink.objects.filter(connection=connection).values(
            "simplefin_account_id", "account_id", "cutover_date")
    }


def _sync_interval():
    return timedelta(seconds=max(1, int(settings.SIMPLEFIN_SYNC_MIN_INTERVAL_SECONDS)))


def _rate_limited(connection: SimpleFinConnection, *, ignore_rate_limit: bool) -> bool:
    if ignore_rate_limit or connection.last_sync_at is None:
        return False
    return timezone.now() < connection.last_sync_at + _sync_interval()


def _batch_hash(connection_id, account_id, synced_at, start) -> str:
    payload = f"simplefin\n{connection_id}\n{account_id}\n{synced_at.isoformat()}\n{start.isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_pending(item: dict) -> bool:
    return item.get("pending") is True or _posted_stamp(item.get("posted")) is None


def _accounts_by_simplefin_id(payload: dict) -> dict[str, dict]:
    return dict(_remote_accounts(payload))


def _upsert_snapshot(account, *, snapshot_date, amount_minor, currency, batch):
    # A snapshot stays tied to the sync that first recorded its date, so
    # undoing a later batch never deletes an earlier day's balance.
    BalanceSnapshot.objects.update_or_create(
        account=account,
        snapshot_date=snapshot_date,
        source=BalanceSnapshot.Source.SIMPLEFIN,
        defaults={"amount_minor": amount_minor, "currency": currency},
        create_defaults={"amount_minor": amount_minor, "currency": currency, "import_batch": batch},
    )


def _posted_source_id(item) -> str:
    """The id of a posted (not pending) transaction item, or "" to skip it."""
    if not isinstance(item, dict) or _is_pending(item):
        return ""
    source_id = clean_text(item.get("id") or "")
    if len(source_id) > MAX_ID_CHARS:
        raise SimpleFinError(UNSTORABLE_ID)
    return source_id


def _new_transaction(account, link, item, source_id, *, row_number):
    txn_date = posted_date(item.get("posted"))
    if txn_date is None or txn_date < link.cutover_date:
        return None
    amount_minor = decimal_to_minor(str(item.get("amount")))
    description = clean_text(item.get("description") or "")[:MAX_DESCRIPTION_CHARS] or "SimpleFIN transaction"
    pending = item.get("pending")
    original = {
        "id": source_id,
        "posted": _posted_stamp(item.get("posted")),
        "amount": clean_text(item.get("amount"))[:100],
        "description": description,
        "pending": pending if isinstance(pending, bool) else None,
    }
    return Transaction(
        account=account,
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


def _import_transactions(account, link, remote, batches) -> int:
    """Import new posted rows, each into the batch whose range covers its date."""
    existing_ids = set(
        Transaction.objects.filter(
            account=account,
            status=Transaction.Status.ACTIVE,
            import_batch__source=ImportBatch.Source.SIMPLEFIN,
            import_batch__simplefin_account_id=link.simplefin_account_id,
        )
        .exclude(source_transaction_id="")
        .values_list("source_transaction_id", flat=True)
    )
    created = []
    next_row = {}
    for item in remote.get("transactions") or []:
        source_id = _posted_source_id(item)
        if not source_id or source_id in existing_ids:
            continue
        txn = _new_transaction(account, link, item, source_id, row_number=1)
        if txn is None:
            continue
        batch = _batch_covering(batches, txn.transaction_date)
        if batch.pk not in next_row:
            last = batch.transactions.aggregate(Max("source_row_number"))["source_row_number__max"] or 0
            next_row[batch.pk] = last + 1
        txn.import_batch = batch
        txn.source_row_number = next_row[batch.pk]
        next_row[batch.pk] += 1
        created.append(txn)
        existing_ids.add(source_id)
    if created:
        Transaction.objects.bulk_create(created)
    return len(created)


def _batch_covering(batches, day):
    """The batch whose range holds day; a later day falls to the newest range."""
    for batch in batches:
        if batch.date_range_start <= day <= batch.date_range_end:
            return batch
    return max(batches, key=lambda batch: (batch.date_range_end, batch.pk))


def _active_link_batches(account, link):
    return ImportBatch.objects.filter(
        account=account,
        source=ImportBatch.Source.SIMPLEFIN,
        simplefin_account_id=link.simplefin_account_id,
        status=ImportBatch.Status.ACTIVE,
        archived_at__isnull=True,
    )


def _uncovered_ranges(account, link, end):
    """Date ranges from the cut-over through end that no active batch covers.

    A sync creates one batch per gap, so batches never overlap: undoing any
    one of them uncovers exactly its own dates and brings back the
    missing-import warning there.
    """
    gaps = []
    cursor = link.cutover_date
    ranges = (
        _active_link_batches(account, link)
        .filter(date_range_end__gte=cursor, date_range_start__lte=end)
        .order_by("date_range_start")
        .values_list("date_range_start", "date_range_end")
    )
    for range_start, range_end in ranges:
        if range_start > cursor:
            gaps.append((cursor, range_start - timedelta(days=1)))
        cursor = max(cursor, range_end + timedelta(days=1))
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def _ensure_batch(person, account, connection, link, synced_at, start, end):
    return ImportBatch.objects.create(
        account=account,
        imported_by=person,
        source=ImportBatch.Source.SIMPLEFIN,
        source_file_sha256=_batch_hash(connection.pk, account.pk, synced_at, start),
        simplefin_account_id=link.simplefin_account_id,
        date_range_start=start,
        date_range_end=end,
    )


def _error_applies_to_remote(item: dict, remote: dict) -> bool:
    account_id = str(remote.get("id") or "")
    keyed = remote_account_key(remote)
    err_acct = str(item.get("account_id") or "")
    err_conn = str(item.get("conn_id") or "")
    conn_id = str(remote.get("conn_id") or "")
    if err_acct:
        if err_acct == keyed:
            return True
        # A bare account id is only unique within its connection.
        return err_acct == account_id and (not err_conn or err_conn == conn_id)
    return bool(err_conn and conn_id and err_conn == conn_id)


def _transactions_unreliable(remote: dict, payload: dict) -> bool:
    if "transactions" not in remote or remote.get("transactions") is None:
        return True
    for item in payload.get("errlist") or []:
        if not isinstance(item, dict) or not (item.get("account_id") or item.get("conn_id")):
            # An error naming no account or connection may affect any of them.
            return True
        if _error_applies_to_remote(item, remote):
            return True
    return False


def _sync_one_link(person, connection, link, remote, synced_at, payload) -> int:
    account = link.account
    currency = _iso4217_usd(str(remote.get("currency") or ""))
    if account.currency != currency:
        raise SimpleFinError("That SimpleFIN account uses a currency this app does not store.")
    balance_date = posted_date(remote.get("balance-date")) or timezone.localdate()
    if link.mode == AccountLink.Mode.TRANSACTIONS and _transactions_unreliable(remote, payload):
        if remote.get("balance") not in (None, ""):
            _upsert_snapshot(
                account,
                snapshot_date=balance_date,
                amount_minor=decimal_to_minor(str(remote.get("balance"))),
                currency=currency,
                batch=None,
            )
        return 0
    end = max(link.cutover_date, balance_date, timezone.localdate())
    new_batches = [
        _ensure_batch(person, account, connection, link, synced_at, gap_start, gap_end)
        for gap_start, gap_end in _uncovered_ranges(account, link, end)
    ]
    new_ids = [batch.pk for batch in new_batches]
    batches = new_batches + list(_active_link_batches(account, link).exclude(pk__in=new_ids))
    imported = 0
    if link.mode == AccountLink.Mode.TRANSACTIONS:
        imported = _import_transactions(account, link, remote, batches)
    batch = _batch_covering(batches, balance_date) if batches else None
    _upsert_snapshot(
        account,
        snapshot_date=balance_date,
        amount_minor=decimal_to_minor(str(remote.get("balance"))),
        currency=currency,
        batch=batch,
    )
    return imported


@member_operation
@notify_after_alert_run
def sync_connection(principal, connection_id, *, ignore_rate_limit=False) -> dict:
    """Sync one connection. A fetch or import failure is recorded, then raised.

    The fetch runs before the transaction and household lock, so a slow
    provider never holds up other members' writes. The results are then
    applied under the lock after re-checking the connection and its links.
    A failure record commits on its own, and the error is raised after it.
    """
    person = _person_for(principal)
    plan = _plan_sync(person, connection_id, ignore_rate_limit=ignore_rate_limit)
    try:
        payload = fetch_accounts(plan.access_url, start_date=plan.start_epoch, end_date=int(plan.now.timestamp()) + 1)
    except SimpleFinError as exc:
        failure = "access_denied" if exc.access_denied else "provider_error"
        # Only revoked access stops scheduled syncs; a transient failure is
        # retried on the next run.
        _record_failure(person, connection_id, plan.now, str(exc), failure=failure, disabled=exc.access_denied)
        raise
    result, failure = _apply_sync(person, connection_id, plan, payload, ignore_rate_limit=ignore_rate_limit)
    if failure is not None:
        raise failure
    return result


def _start_epoch(cutover_dates):
    if not cutover_dates:
        return None
    start_local = timezone.make_aware(
        datetime.combine(min(cutover_dates), datetime.min.time()),
        timezone.get_current_timezone(),
    )
    return int(start_local.timestamp())


def _plan_sync(person, connection_id, *, ignore_rate_limit):
    """Everything the outbound fetch needs, read without taking locks."""
    connection = SimpleFinConnection.objects.filter(pk=connection_id, owner=person).first()
    if connection is None:
        raise PermissionDenied(_DENIED)
    if _rate_limited(connection, ignore_rate_limit=ignore_rate_limit):
        raise SimpleFinRateLimited(RATE_LIMITED)
    cutovers = list(AccountLink.objects.filter(connection=connection).values_list("cutover_date", flat=True))
    return SimpleNamespace(
        now=timezone.now(),
        access_url=_readable_access_url(connection),
        start_epoch=_start_epoch(cutovers),
    )


@transaction.atomic
def _record_failure(person, connection_id, now, message, *, failure, disabled=None):
    lock_actor_household(person)
    connection = SimpleFinConnection.objects.select_for_update().filter(pk=connection_id, owner=person).first()
    if connection is None:
        return
    _save_failure(person, connection, now, message, failure=failure, disabled=disabled)


def _save_failure(person, connection, now, message, *, failure, disabled=None):
    from finance.alert_services import raise_sync_alert

    connection.last_sync_at = now
    connection.last_sync_result = message[:500]
    fields = ["last_sync_at", "last_sync_result"]
    if disabled is not None:
        connection.disabled = disabled
        fields.append("disabled")
    connection.save(update_fields=fields)
    raise_sync_alert(connection)
    outcome(person, "simplefin_sync", connection.pk, phase="failed",
            metadata={"connection_id": connection.pk, "failure": failure})


def _locked_connection(person, connection_id, plan, *, ignore_rate_limit):
    """Lock the household and connection, then re-check what the fetch assumed."""
    lock_actor_household(person)
    connection = (
        SimpleFinConnection.objects.select_for_update().filter(pk=connection_id, owner=person).first()
    )
    if connection is None:
        raise PermissionDenied(_DENIED)
    if _rate_limited(connection, ignore_rate_limit=ignore_rate_limit):
        # Another sync finished while this one was fetching.
        raise SimpleFinRateLimited(RATE_LIMITED)
    links = list(AccountLink.objects.select_related("account").filter(connection=connection))
    start_epoch = _start_epoch([link.cutover_date for link in links])
    if start_epoch is not None and (plan.start_epoch is None or start_epoch < plan.start_epoch):
        # A link with an earlier cut-over was saved after the fetch began, so
        # the fetched history may not cover it.
        raise SimpleFinError(LINKS_CHANGED)
    return connection, links


def _import_links(person, connection, links, syncable_ids, payload, now):
    """Apply each linked account. Returns (imported, skipped)."""
    remote_accounts = _accounts_by_simplefin_id(payload)
    imported = 0
    skipped = 0
    for link in links:
        if link.account_id not in syncable_ids:
            # Archived, or no longer visible to the connection owner
            # (for example a shared account made private by its owner).
            skipped += 1
            continue
        remote = remote_accounts.get(link.simplefin_account_id)
        if remote is None:
            continue
        imported += _sync_one_link(person, connection, link, remote, now, payload)
    return imported, skipped


@transaction.atomic
def _apply_sync(person, connection_id, plan, payload, *, ignore_rate_limit):
    connection, links = _locked_connection(person, connection_id, plan, ignore_rate_limit=ignore_rate_limit)
    now = plan.now
    errors = provider_errors(payload)
    syncable_ids = set(
        Account.objects.visible_to(person)
        .filter(pk__in=[link.account_id for link in links], status=Account.Status.ACTIVE, archived_at__isnull=True)
        .values_list("pk", flat=True)
    )
    try:
        # A savepoint: a failure part-way undoes this run's imports while the
        # failure record below still commits.
        with transaction.atomic():
            imported, skipped = _import_links(person, connection, links, syncable_ids, payload, now)
    except SimpleFinError as exc:
        _save_failure(person, connection, now, str(exc), failure="import_failed")
        return None, exc
    synced = _refresh_after_sync(person, syncable_ids, now)
    summary = f"Synced {imported} new transaction(s)."
    if skipped:
        summary = f"{summary} Skipped {skipped} linked account(s) that are archived or no longer available to you."
    if errors:
        summary = f"{summary} {errors[0]}"
    connection.last_sync_at = now
    connection.last_sync_result = summary[:500]
    connection.disabled = False
    connection.save(update_fields=("last_sync_at", "last_sync_result", "disabled"))
    from finance.alert_services import schedule_after_new_transactions

    schedule_after_new_transactions(synced)
    details = {"connection_id": connection.pk, "new_count": imported}
    if errors:
        details["failure"] = "provider_error"
    outcome(person, "simplefin_sync", connection.pk, phase="failed" if errors else "succeeded", metadata=details)
    return {"imported": imported, "errors": errors, "result": connection.last_sync_result}, None


def _refresh_after_sync(person, syncable_ids, now):
    from finance.category_services import refresh_transfer_pairs
    from finance.category_suggestion_services import queue_category_suggestions_for
    from finance.recurring_services import refresh_recurring_series
    from finance.rule_services import apply_enabled_rules_to_transactions

    synced = list(
        Transaction.objects.filter(
            account_id__in=syncable_ids,
            import_batch__source=ImportBatch.Source.SIMPLEFIN,
            created_at__gte=now,
        )
    )
    refresh_transfer_pairs(person, transaction_ids=[row.pk for row in synced])
    apply_enabled_rules_to_transactions(person, synced)
    queue_category_suggestions_for(person, synced)
    refresh_recurring_series(person)
    return synced


@transaction.atomic
def disconnect_connection(principal, connection_id) -> None:
    person = _person_for(principal)
    lock_actor_household(person)
    connection = (
        SimpleFinConnection.objects.select_for_update().filter(pk=connection_id, owner=person).first()
    )
    if connection is None:
        raise PermissionDenied(_DENIED)
    connection_id = connection.pk
    connection.delete()
    append_event(action=AuditEvent.Action.SIMPLEFIN_DISCONNECTED, actor=person, target_id=connection_id)


def _sync_scheduled(connection):
    if execution.get() is None:
        with operation():
            sync_connection(connection.owner, connection.pk, ignore_rate_limit=True)
    else:
        sync_connection(connection.owner, connection.pk, ignore_rate_limit=True)


def _record_unexpected_failure(connection):
    try:
        _record_failure(connection.owner, connection.pk, timezone.now(), UNEXPECTED_FAILURE, failure="import_failed")
    except Exception:  # The daily pass must reach every connection and the alert pass.
        logger.error("Could not record the failed SimpleFIN sync for connection %s.", connection.pk)


def sync_all_connections() -> int:
    """Daily job: sync every enabled connection. Returns the number attempted.

    One connection's failure, expected or not, is recorded and the pass moves
    on, so later connections still sync. Logs name the error type only: an
    exception message could quote provider data.
    """
    count = 0
    for connection in SimpleFinConnection.objects.filter(disabled=False).select_related("owner"):
        try:
            _sync_scheduled(connection)
        except SimpleFinError:
            pass
        except Exception as exc:  # The daily pass must reach every connection and the alert pass.
            logger.error(
                "Scheduled SimpleFIN sync for connection %s failed unexpectedly (%s).",
                connection.pk,
                type(exc).__name__,
            )
            _record_unexpected_failure(connection)
        count += 1
    return count
