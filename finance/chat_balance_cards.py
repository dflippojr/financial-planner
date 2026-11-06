"""Chat balance cards (#660): manual account balances, for example month ends read from a statement.

The same rules as the other item cards (#659): the propose tool only stores an AiProposal with one
row per balance. Each row is checked the way the account's Balances page checks its form. A manual
balance already recorded for that date is kept as the row's snapshot and shown before → after;
Apply re-reads it under its lock, refuses the row when it moved (stale), and records the balance
through `record_manual_snapshot`, the Balances page's own service.

Manual balances use the manual sign: a credit card or loan stores the amount owed as a positive
number, unlike SimpleFIN and calculated rows (see net_worth.contribution_parts).
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from .access import DENIED as _DENIED
from .ai_tools import visible_accounts
from .cash_flow import format_minor
from .chat_proposals import (
    ITEM_RECORDED,
    ITEM_UNAVAILABLE,
    ITEM_UPDATED,
    NOTHING_CHANGES,
    _card_items,
    _form_problem,
    _int_or_none,
    _items_schema,
    _raw_items,
    _Stale,
    _store_items,
    _text,
)
from .forms import BALANCE_RANGE_ERROR, MAX_BALANCE_MINOR, ManualBalanceForm
from .models import Account, AiProposal, BalanceSnapshot
from .snapshot_services import SnapshotError, _lock_editable_account, record_manual_snapshot

MAX_NOTE_CHARS = 200
ACCOUNT_ARCHIVED = "This account is archived, so its balances can't change. Restore it on its page first."
SIMPLEFIN_DATE = (
    "Your bank connection (SimpleFIN) already recorded this account's balance for that date. A synced "
    "balance counts ahead of a manual one on the same day, so a manual balance would change nothing. "
    "Pick another date."
)
AMOUNT_NEEDED = "Amount: enter a whole number of cents."
CONTRIBUTION_NEEDED = "Net contributions: enter a whole number of cents, or leave it out."
CONTRIBUTION_NOT_INVESTMENT = "Net contributions: only investment accounts record them."
DUPLICATE_DATE = "That account and date are already in this card."
CHANGED = "This changed after the suggestion was made. Ask again for a fresh one."
LIABILITY_SIGN = "The amount owed is entered as a positive number; a negative amount means an overpayment."
ESTIMATE = "A personal estimate, not an appraisal."
REPLACES = "Replaces the manual balance already recorded for this date."


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------


def balance_tool_rows():
    """(name, description, properties, required, handler) for chat_proposals.proposal_tools."""
    return (
        ("propose_balances", (
            "Suggest manual account balances, for example month-end balances read from a statement the member "
            "attached, many in one card with a checkbox each. account_id comes from list_accounts. date is the "
            "balance's date, YYYY-MM-DD, never in the future. amount_minor is whole cents: for a credit card or "
            "loan, the amount owed as a positive number (an overpayment is negative), even though balance_history "
            "shows owed amounts as negative; for any other account, the balance. net_contribution_minor is for "
            "investment accounts only: contributions minus withdrawals since the previous statement. A manual "
            "balance already recorded on that date is replaced, and the card shows it before and after; an "
            "omitted note or net_contribution_minor keeps the existing one. The member must confirm."
        ), {"items": _items_schema({
            "account_id": {"type": "integer"},
            "date": {"type": "string"},
            "amount_minor": {"type": "integer"},
            "net_contribution_minor": {"type": "integer"},
            "note": {"type": "string"},
        }, required=["account_id", "date", "amount_minor"])}, ["items"], _propose_balances),
    )


def _signed_minor(value):
    """A whole number of cents of either sign, zero included, or None. Fractions and booleans are refused."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _minor_text(minor):
    """Exact dollars text for cents of either sign, as the Balances page's form takes it."""
    return format(Decimal(minor).scaleb(-2), "f")


def _note_hash(note):
    return hashlib.sha256((note or "").encode("utf-8")).hexdigest()


def _manual_state(snapshot):
    """What a row's snapshot holds of an existing manual balance: never its note, only a hash."""
    if snapshot is None:
        return None
    return {"id": snapshot.pk, "amount_minor": snapshot.amount_minor,
            "net_contribution_minor": snapshot.net_contribution_minor, "note_hash": _note_hash(snapshot.note)}


def _manual_on(account, snapshot_date, *, lock=False):
    query = BalanceSnapshot.objects.filter(account=account, snapshot_date=snapshot_date,
                                           source=BalanceSnapshot.Source.MANUAL)
    if lock:
        query = query.select_for_update(of=("self",))
    return query.first()


def _synced_on(account, snapshot_date):
    return BalanceSnapshot.objects.filter(account=account, snapshot_date=snapshot_date,
                                          source=BalanceSnapshot.Source.SIMPLEFIN).exists()


def _balance_form(account, snapshot_date, amount_minor, contribution_minor, note):
    """The Balances page's own form, so a row is valid exactly when the page would accept it."""
    data = {"snapshot_date": snapshot_date or "", "amount": _minor_text(amount_minor), "note": note or ""}
    if contribution_minor is not None:
        data["net_contribution"] = _minor_text(contribution_minor)
    return ManualBalanceForm(data, account=account)


def _in_range(minor):
    return abs(minor) <= MAX_BALANCE_MINOR


def _parsed_fields(raw, account, values):
    """Read the model's amount, net contribution and note into `values`; returns the first problem or None.

    `values["note"]` stays None when the model left the note out, which keeps an existing note.
    """
    amount = _signed_minor(raw.get("amount_minor"))
    if amount is None:
        return AMOUNT_NEEDED
    if not _in_range(amount):
        return f"Amount: {BALANCE_RANGE_ERROR}"
    values["amount_minor"] = amount
    if "net_contribution_minor" in raw:
        values["contribution_given"] = True
        given = raw.get("net_contribution_minor")
        if given not in (None, ""):
            contribution = _signed_minor(given)
            if contribution is None:
                return CONTRIBUTION_NEEDED
            if not _in_range(contribution):
                return f"Net contributions: {BALANCE_RANGE_ERROR}"
            if account.account_type != Account.Type.INVESTMENT:
                return CONTRIBUTION_NOT_INVESTMENT
            values["net_contribution_minor"] = contribution
    if "note" in raw:
        values["note"] = _text(raw.get("note"))
    return None


def _balance_row(person, raw, seen):
    values = {"date": _text(raw.get("date"))[:40], "amount_minor": _signed_minor(raw.get("amount_minor")),
              "net_contribution_minor": None, "contribution_given": False, "note": None}
    row = {"action": "record", "id": None, "before": None, "error": None, "values": values}
    target = _int_or_none(raw.get("account_id"))
    account = (visible_accounts(person, include_archived=True).filter(pk=target).first()
               if target is not None else None)
    if account is None:
        # Hidden, missing and not-yet-consented household accounts answer alike (#521).
        row["error"] = ITEM_UNAVAILABLE
        return row
    row.update(id=account.pk, account_type=account.account_type, accounts=[account.pk])
    if account.status != Account.Status.ACTIVE:
        row["error"] = ACCOUNT_ARCHIVED
        return row
    problem = _parsed_fields(raw, account, values)
    note = values["note"]
    if note is not None and len(note) > MAX_NOTE_CHARS:
        values["note"] = note[:MAX_NOTE_CHARS]
    if problem is None:
        form = _balance_form(account, values["date"], values["amount_minor"], values["net_contribution_minor"], note)
        if form.is_valid():
            values["date"] = form.cleaned_data["snapshot_date"].isoformat()
        else:
            problem = _form_problem(form)
    if problem is None:
        problem = _against_stored(account, values, row, seen)
    row["error"] = problem
    return row


def _against_stored(account, values, row, seen):
    """Checks against what is already recorded for the row's account and date; fills the row's snapshot."""
    key = (account.pk, values["date"])
    if key in seen:
        return DUPLICATE_DATE
    seen.add(key)
    snapshot_date = date.fromisoformat(values["date"])
    if _synced_on(account, snapshot_date):
        return SIMPLEFIN_DATE
    existing = _manual_on(account, snapshot_date)
    row["before"] = _manual_state(existing)
    if existing is None:
        return None
    if not values["contribution_given"]:
        values["net_contribution_minor"] = existing.net_contribution_minor
    same_note = values["note"] is None or values["note"] == existing.note
    if (values["amount_minor"] == existing.amount_minor
            and values["net_contribution_minor"] == existing.net_contribution_minor and same_note):
        return NOTHING_CHANGES
    return None


def _propose_balances(person, args, conversation, turn):
    items, dropped = _raw_items(args)
    seen = set()
    rows = [_balance_row(person, raw, seen) for raw in items]
    accounts = [account for row in rows for account in row.pop("accounts", [])]
    # A card where a row found its account is kept even when every row is refused, so the member
    # sees each refused balance and why (a SimpleFIN date, a future date) as well as the model.
    keep = any(row["id"] is not None for row in rows)
    return _store_items(conversation, turn, AiProposal.Kind.BALANCES, rows, dropped, account_ids=accounts,
                        keep_refused=keep)


# ---------------------------------------------------------------------------
# Apply: one ticked row, re-checked as the member, through the Balances page's service
# ---------------------------------------------------------------------------


def _apply_balance_row(person, _payload, row):
    found = visible_accounts(person, include_archived=True).filter(pk=row["id"]).first()
    if found is None:
        raise PermissionDenied(_DENIED)
    if found.status != Account.Status.ACTIVE:
        raise ValidationError(ACCOUNT_ARCHIVED)
    # apply_proposal already holds the household lock; this takes the account's, as the service does.
    account = _lock_editable_account(person, row["id"])
    if account.account_type != row["account_type"]:
        raise _Stale(CHANGED)
    values = row["values"]
    snapshot_date = date.fromisoformat(values["date"])
    existing = _manual_on(account, snapshot_date, lock=True)
    if _manual_state(existing) != row["before"]:
        raise _Stale(CHANGED)
    if _synced_on(account, snapshot_date):
        raise _Stale(SIMPLEFIN_DATE)
    note = values["note"] if values["note"] is not None else (existing.note if existing is not None else "")
    form = _balance_form(account, values["date"], values["amount_minor"], values["net_contribution_minor"], note)
    if not form.is_valid():
        raise ValidationError(_form_problem(form))
    contribution = form.net_contribution_minor() if "net_contribution" in form.fields else None
    try:
        saved = record_manual_snapshot(
            person, account.pk, snapshot_date=form.cleaned_data["snapshot_date"], amount_minor=form.amount_minor(),
            note=form.cleaned_data["note"], net_contribution_minor=contribution,
        )
    except SnapshotError as exc:
        raise ValidationError(str(exc)) from exc
    return {"status": ITEM_RECORDED if existing is None else ITEM_UPDATED, "id": saved.pk}


BALANCE_APPLIERS = {AiProposal.Kind.BALANCES: _apply_balance_row}


# ---------------------------------------------------------------------------
# Card: each row reads its account fresh as the member. An account the member can no
# longer see shows a generic line, never the stored snapshot or the model's values.
# ---------------------------------------------------------------------------


def _amount_label(account_type):
    if account_type in Account.LIABILITY_TYPES:
        return "Amount owed"
    if account_type in Account.PHYSICAL_ASSET_TYPES:
        return "Estimated value"
    return "Balance"


def _money(value):
    return format_minor(value, "USD") if isinstance(value, int) and not isinstance(value, bool) else "not set"


def _snapshot_date(values):
    try:
        return date.fromisoformat(_text(values.get("date")))
    except ValueError:
        return None


def _new_summary(label, values, account):
    parts = [f"{label} {_money(values.get('amount_minor'))}"]
    if values.get("net_contribution_minor") is not None:
        parts.append(f"net contributions {_money(values['net_contribution_minor'])}")
    if account.is_physical_asset():
        parts.append(ESTIMATE)
    return " · ".join(parts)


def _overwrite_changes(label, before, values, existing, pending):
    changes = []
    if before.get("amount_minor") != values.get("amount_minor"):
        changes.append({"label": label, "before": _money(before.get("amount_minor")),
                        "after": _money(values.get("amount_minor"))})
    if before.get("net_contribution_minor") != values.get("net_contribution_minor"):
        changes.append({"label": "Net contributions", "before": _money(before.get("net_contribution_minor")),
                        "after": _money(values.get("net_contribution_minor"))})
    note = values.get("note")
    if note is not None and pending and existing is not None and existing.note != note:
        changes.append({"label": "Note", "before": existing.note or "no note", "after": note or "no note"})
    return changes


def _pending_problem(account, item, snapshot_date):
    """Why a pending row can no longer be ticked, or None. Apply would refuse it for the same reason."""
    if account.status != Account.Status.ACTIVE:
        return ACCOUNT_ARCHIVED
    if account.account_type != item.get("account_type") or snapshot_date is None:
        return CHANGED
    if _synced_on(account, snapshot_date):
        return SIMPLEFIN_DATE
    if _manual_state(_manual_on(account, snapshot_date)) != item.get("before"):
        return CHANGED
    return None


def _describe_balance(person, _payload, item, pending):
    account = (Account.objects.visible_to(person).filter(pk=item.get("id")).first()  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every Account.objects.visible_to in the baseline
               if item.get("id") else None)
    if account is None:
        return {"title": "Record a balance", "summary": "This account is not available.", "unavailable": True}
    values = item.get("values") or {}
    snapshot_date = _snapshot_date(values)
    label = _amount_label(item.get("account_type") or account.account_type)
    row = {"title": f"{account.name} · {_text(values.get('date')) or 'no date'}", "summary": "", "changes": [],
           "url": reverse("account-balances", args=[account.pk]) if account.status == Account.Status.ACTIVE else ""}
    before = item.get("before")
    existing = _manual_on(account, snapshot_date) if pending and snapshot_date is not None else None
    if before is None:
        row["summary"] = _new_summary(label, values, account)
    else:
        row["summary"] = REPLACES
        row["changes"] = _overwrite_changes(label, before, values, existing, pending)
    if values.get("note") and not any(change["label"] == "Note" for change in row["changes"]):
        row["note"] = values["note"]
    if label == "Amount owed":
        row["summary"] = f"{row['summary']} {LIABILITY_SIGN}"
    if pending and not item.get("error"):
        problem = _pending_problem(account, item, snapshot_date)
        if problem is not None:
            row.update(summary=problem, unavailable=True)
    return row


def _card_balances(person, proposal, payload, card):
    card["heading"] = "Balances"
    card["page_url"] = reverse("net-worth")
    card["page_label"] = "Open net worth"
    _card_items(person, proposal, payload, card, _describe_balance)


BALANCE_CARD_BUILDERS = {AiProposal.Kind.BALANCES: _card_balances}
