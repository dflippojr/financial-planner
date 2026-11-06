"""Per-bank health from SimpleFIN reads, and the ntfy pushes when it changes (#694).

A SimpleFIN "connection" in the protocol is one bank login on Bridge
(`connections[]`, joined to accounts by `conn_id`). This app's
SimpleFinConnection is the member's Access Token, which covers every bank on
their Bridge account. Each read stores, per bank, the newest `balance-date`
and the errors SimpleFIN attached to it, so the Connections page never needs
a live request. A bank is stale when its newest balance-date is more than
STALE_AFTER old; staleness is judged against the current time, so a bank that
stops refreshing between reads still turns stale.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.html import strip_tags

from finance.simplefin_errors import clean_text, is_api_usage_notice, remote_account_key, sanitize_provider_message

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=36)
OK, STALE, ERROR = "ok", "stale", "error"
MAX_BANKS = 50
MAX_ERRORS_PER_BANK = 5
MAX_GENERAL_MESSAGES = 5


def _text(value, limit) -> str:
    return " ".join(strip_tags(clean_text(value or "")).split())[:limit]


def _epoch_moment(value) -> datetime | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        stamp = int(value)
        return datetime.fromtimestamp(stamp, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bank_key(account: dict) -> str:
    return clean_text(account.get("conn_id") or "")[:255]


def _fallback_bank_name(account: dict) -> str:
    org = account.get("org")
    if not isinstance(org, dict):
        org = {}
    return _text(account.get("conn_name") or org.get("name") or "SimpleFIN", 200)


def _collect_banks(payload: dict) -> dict[str, dict]:
    banks: dict[str, dict] = {}
    for item in payload.get("connections") or []:
        if isinstance(item, dict) and item.get("conn_id"):
            key = clean_text(item["conn_id"])[:255]
            banks[key] = {"conn_id": key, "name": _text(item.get("name"), 200) or "Bank", "newest": None,
                          "errors": [], "accounts": 0}
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict) or not account.get("id"):
            continue
        key = _bank_key(account)
        bank = banks.setdefault(key, {"conn_id": key, "name": _fallback_bank_name(account), "newest": None,
                                      "errors": [], "accounts": 0})
        bank["accounts"] += 1
        moment = _epoch_moment(account.get("balance-date"))
        if moment is not None and (bank["newest"] is None or moment > bank["newest"]):
            bank["newest"] = moment
    return banks


def _account_banks(payload: dict) -> dict[str, set[str]]:
    """Every name an error may use for an account -> the banks (conn_ids) it could mean.

    Account ids are unique only within one bank. An error may name the raw id
    or the app's percent-encoded "conn_id:account_id" key (the import path
    accepts both). Raw ids and keys are collected side by side, never one
    overwriting the other, so an error is placed only when its name points
    to exactly one bank.
    """
    owners: dict[str, set[str]] = {}
    for account in payload.get("accounts") or []:
        if isinstance(account, dict) and account.get("id"):
            bank = _bank_key(account)
            owners.setdefault(clean_text(account["id"]), set()).add(bank)
            owners.setdefault(remote_account_key(account), set()).add(bank)
    return owners


def _add_error(bank: dict, message: str) -> None:
    if message and message not in bank["errors"] and len(bank["errors"]) < MAX_ERRORS_PER_BANK:
        bank["errors"].append(message)


def _names(name: str, message: str) -> bool:
    """Whether the message names the bank as a whole word ("Chase" is not in "Purchase")."""
    pattern = r"(?<![^\W_])" + re.escape(name) + r"(?![^\W_])"
    return re.search(pattern, message, re.IGNORECASE) is not None


def _named_bank(banks: dict[str, dict], message: str) -> dict | None:
    """A general message that names exactly one bank belongs to that bank."""
    named = [bank for bank in banks.values() if len(bank["name"]) >= 3 and _names(bank["name"], message)]
    return named[0] if len(named) == 1 else None


def _place_general(banks, general, message) -> None:
    bank = _named_bank(banks, message)
    if bank is not None:
        _add_error(bank, message)
    elif message not in general and len(general) < MAX_GENERAL_MESSAGES:
        general.append(message)


def _attach_errors(payload: dict, banks: dict[str, dict]) -> list[str]:
    """Attach errlist items to their bank; return the messages that name no bank."""
    owners = _account_banks(payload)
    general: list[str] = []
    for item in payload.get("errlist") or []:
        if isinstance(item, str):
            _place_general(banks, general, sanitize_provider_message(item))
            continue
        if not isinstance(item, dict) or is_api_usage_notice(item):
            continue
        message = sanitize_provider_message(item.get("msg") or "") or "SimpleFIN reported an error."
        conn_id = clean_text(item.get("conn_id") or "")
        account_id = clean_text(item.get("account_id") or "")
        if not conn_id and account_id:
            candidates = owners.get(account_id, set())
            conn_id = next(iter(candidates)) if len(candidates) == 1 else ""
        if conn_id and conn_id in banks:
            _add_error(banks[conn_id], message)
        else:
            _place_general(banks, general, message)
    for item in payload.get("errors") or []:  # Deprecated plain strings.
        if isinstance(item, str):
            message = sanitize_provider_message(item)
            if message:
                _place_general(banks, general, message)
    return general


def parse_health(payload: dict) -> tuple[list[dict], list[str]]:
    """(banks, general messages) from one Account Set; JSON-ready."""
    banks = _collect_banks(payload)
    general = _attach_errors(payload, banks)
    rows = []
    for bank in sorted(banks.values(), key=lambda row: (row["name"].lower(), row["conn_id"]))[:MAX_BANKS]:
        newest = bank["newest"]
        rows.append({
            "conn_id": bank["conn_id"],
            "name": bank["name"],
            "balance_date": newest.isoformat() if newest else None,
            "errors": bank["errors"],
            "accounts": bank["accounts"],
        })
    return rows, general


def balance_moment(bank: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(bank["balance_date"]) if bank.get("balance_date") else None
    except (TypeError, ValueError):
        return None


def bank_state(bank: dict, now=None) -> str:
    if bank.get("errors"):
        return ERROR
    moment = balance_moment(bank)
    if moment is not None and (now or timezone.now()) - moment > STALE_AFTER:
        return STALE
    return OK


def stored_read(connection) -> dict:
    """The connection's newest stored /accounts read, or {} before the first one."""
    return connection.last_read if isinstance(connection.last_read, dict) else {}


def has_stored_read(connection) -> bool:
    return isinstance(stored_read(connection).get("banks"), list)


def stored_banks(connection) -> list[dict]:
    read = stored_read(connection)
    banks = read.get("banks")
    return [bank for bank in banks if isinstance(bank, dict)] if isinstance(banks, list) else []


def bank_rows(connection, now=None) -> list[dict]:
    """Stored banks with their current state, for the page."""
    now = now or timezone.now()
    rows = []
    for bank in stored_banks(connection):
        state = bank_state(bank, now)
        rows.append({**bank, "state": state, "newest": balance_moment(bank), "needs_fix": state != OK})
    return rows


def find_bank(connection, conn_id) -> dict | None:
    return next((bank for bank in stored_banks(connection) if bank.get("conn_id") == conn_id), None)


def read_record(payload: dict, listing: list[dict], now) -> dict:
    banks, general = parse_health(payload)
    return {"at": now.isoformat(), "accounts": listing, "banks": banks, "messages": general}


# ntfy pushes ------------------------------------------------------------------


def _push_text(bank: dict, state: str) -> tuple[str, str, str]:
    name = bank["name"]
    if state == OK:
        return f"{name} is up to date", f"SimpleFIN is refreshing {name} again.", "white_check_mark"
    if state == ERROR:
        return f"{name} needs attention", f"SimpleFIN reports a problem with {name}. Open Connections to fix it.", "warning"
    moment = balance_moment(bank)
    local = timezone.localtime(moment) if moment else None
    since = f" since {local:%b} {local.day} {local:%H:%M}" if local else ""
    return (f"{name} needs attention", f"SimpleFIN has not refreshed {name}{since}. Open Connections to fix it.",
            "warning")


def _transition(previous: str | None, state: str) -> bool:
    """Whether a change from the last pushed state to this one is worth a push."""
    previous = previous or OK
    if state == ERROR:
        return previous != ERROR
    if state == STALE:
        return previous == OK
    return previous != OK


def _deliver(person, bank, state) -> bool:
    from finance.alert_ntfy import send_connection_notice
    from finance.models import AlertSettings

    prefs = AlertSettings.objects.filter(person=person).select_related("person").first()
    if prefs is None or not prefs.sync_enabled:
        return False
    return send_connection_notice(prefs, *_push_text(bank, state))


def push_health_changes(connection_id) -> int:
    """Push one ntfy notice per bank whose state changed since the last push.

    Locks the connection so overlapping calls never push the same change
    twice. A state is recorded only once its push was delivered, so a failed
    or switched-off push is tried again at the next read or alert pass, and
    the same state is never pushed twice.
    """
    from finance.models import SimpleFinConnection

    sent = 0
    with transaction.atomic():
        connection = (
            SimpleFinConnection.objects.select_for_update(of=("self",))
            .select_related("owner").filter(pk=connection_id).first()
        )
        if connection is None:
            return 0
        now = timezone.now()
        banks = stored_banks(connection)
        pushed = dict(connection.health_alerts) if isinstance(connection.health_alerts, dict) else {}
        current = {bank["conn_id"]: bank for bank in banks if "conn_id" in bank}
        updated = {key: value for key, value in pushed.items() if key in current}
        for key, bank in current.items():
            state = bank_state(bank, now)
            if not _transition(pushed.get(key), state):
                # A bank first seen healthy is recorded; an error that cleared
                # while the bank is still behind is not a recovery yet.
                if key not in pushed and state == OK:
                    updated[key] = OK
                continue
            if _deliver(connection.owner, bank, state):
                updated[key] = state
                sent += 1
        if updated != pushed:
            SimpleFinConnection.objects.filter(pk=connection.pk).update(health_alerts=updated)
    return sent


def schedule_health_push(connection_id) -> None:
    def _run():
        try:
            push_health_changes(connection_id)
        except Exception:  # A failed push must never fail the sync that stored the read.
            logger.error("SimpleFIN health push failed.")

    transaction.on_commit(_run)


def evaluate_health_alerts() -> list:
    """Daily alert pass: banks can turn stale with no new read. Returns no Alert rows."""
    from finance.models import SimpleFinConnection

    for connection_id in SimpleFinConnection.objects.values_list("pk", flat=True):
        try:
            push_health_changes(connection_id)
        except Exception:
            logger.error("SimpleFIN health push failed.")
    return []
