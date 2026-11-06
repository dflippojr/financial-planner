"""Chat cleanup cards (#661): recurring charges, transfers, AI category suggestions, notes and splits.

The same rules as the other item cards (#659): a propose tool only stores an AiProposal with one
row per item. Apply re-reads each ticked row as the member, refuses a row whose target changed
since the suggestion (stale), and runs the service the manual page runs. Three read-only tools
list the open suggestions the model may name, within the other tools' account and household
consent scope (#521).
"""

from __future__ import annotations

import hashlib
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from .access import DENIED as _DENIED
from .ai_tools import (
    MAX_TOOL_ROWS,
    WITHHELD_CATEGORY_NAME,
    _figure,
    ai_nameable_categories,
    visible_accounts,
)
from .ai_types import ToolResult, ToolSpec
from .bulk_edit_services import _apply_note
from .cash_flow import format_minor
from .category_services import (
    SPLIT_COUNT_ERROR,
    SPLIT_REFUND_ERROR,
    SPLIT_SIGN_ERROR,
    SPLIT_SUM_ERROR,
    SPLIT_TRANSFER_ERROR,
    _lock_accounts_and_transactions,
    confirm_transfer_pair,
    dismiss_transfer_pair,
    split_transaction,
    transaction_is_linked_refund,
    with_transfer_state,
)
from .category_suggestion_services import accept_suggestion, reject_suggestion, suggestion_is_current
from .chat_proposals import (
    ACTION_UNKNOWN,
    CATEGORY_PROBLEM,
    MAX_CARD_ITEMS,
    ITEM_ACCEPTED,
    ITEM_CONFIRMED,
    ITEM_DISMISSED,
    ITEM_MARKED,
    ITEM_NOTED,
    ITEM_REJECTED,
    ITEM_SPLIT,
    ITEM_UNAVAILABLE,
    ITEM_UNMARKED,
    NO_LONGER_APPLIES,
    _card_items,
    _category_by_name,
    _category_name,
    _int_or_none,
    _raw_items,
    _Stale,
    _store_items,
    _text,
)
from .models import (
    AiProposal,
    Category,
    CategorySuggestion,
    RecurringSeries,
    RecurringSeriesMember,
    RefundLink,
    Transaction,
    TransferPair,
)
from .planning_services import confine_recurring_series_to_accounts
from .recurring_services import confirm_recurring_series, dismiss_recurring_series
from .tag_services import NOTE_TOO_LONG
from .untracked_transfers import NOT_MARKED, mark_refusal, mark_untracked_transfer, unmark_untracked_transfer

MAX_NOTE_CHARS = 2000
MAX_SPLIT_PARTS = 20
DUPLICATE = "That item is already in this card."
NOTE_EMPTY = "Note: enter a note."
NOTE_WOULD_BE_TOO_LONG = "Note: the note would be longer than 2,000 characters."
ALREADY_SPLIT = "Already split: change its parts on the transaction's page."
REFUND_PART_NEEDED = "It has a linked refund: split it on the transaction's page to choose each refund's part."
SPLIT_TOO_MANY = f"A split has at most {MAX_SPLIT_PARTS} parts."
CHANGED = "This changed after the suggestion was made. Ask again for a fresh one."
OPEN_SERIES = (RecurringSeries.Status.POSSIBLE, RecurringSeries.Status.SUGGESTED)
_ALREADY = {
    "confirmed": "Already confirmed.",
    "auto_marked": "Already marked as a transfer.",
    "dismissed": "Already dismissed.",
    "undone": "Already undone.",
    "accepted": "Already accepted.",
    "rejected": "Already rejected.",
}


def _already(status):
    return _ALREADY.get(status, NO_LONGER_APPLIES)


def _action(raw, allowed):
    action = _text(raw.get("action")).lower()
    return action if action in allowed else None


def _row(action, error=None, **fields):
    return {"action": action, "id": None, "before": None, "error": error, **fields}


def _ai_transactions(person):
    """Active rows the model may name: visible, in an account inside the AI scope (#521)."""
    return Transaction.objects.visible_to(person).filter(  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every Transaction.objects.visible_to in the baseline
        status=Transaction.Status.ACTIVE,
        account_id__in=visible_accounts(person).values("pk"),
    ).select_related("account", "category")


def _transaction_line(txn):
    return f"{txn.transaction_date} · {txn.account.name} · {txn.amount_display}"


def _transaction_url(txn):
    return reverse("transaction-edit", args=[txn.pk])


def _read_tool(name, description, handler):
    return ToolSpec(name=name, description=description,
                    parameters={"type": "object", "properties": {}, "required": []}, handler=handler)


# ---------------------------------------------------------------------------
# Read-only lookups: the ids the propose tools take
# ---------------------------------------------------------------------------


def cleanup_lookup_tools():
    return (
        _read_tool("recurring_suggestions", (
            "Detected recurring charges waiting for review (not yet confirmed or dismissed), with series_id "
            "for propose_recurring_charges."
        ), recurring_suggestions),
        _read_tool("transfer_suggestions", (
            "Suggested transfer pairs waiting for review, with pair_id for propose_transfers."
        ), transfer_suggestions),
        _read_tool("category_suggestions", (
            "Pending AI category suggestions for uncategorized transactions, with suggestion_id for "
            "propose_ai_suggestions."
        ), category_suggestions),
    )


def _ai_series(person):
    """Series the model may name: the member's own, every charge inside the AI scope (#521)."""
    return confine_recurring_series_to_accounts(_visible_series(person), visible_accounts(person))


def _visible_series(person):
    return RecurringSeries.objects.visible_to(person)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every RecurringSeries.objects.visible_to in the baseline


def _series_account_ids(series_ids):
    return tuple(
        RecurringSeriesMember.objects.filter(series_id__in=series_ids)
        .values_list("transaction__account_id", flat=True)
        .distinct()
    )


def recurring_suggestions(person, _args):
    url = reverse("recurring-review")
    shown = list(_ai_series(person).filter(status__in=OPEN_SERIES).order_by("display_name", "pk")[:MAX_TOOL_ROWS])
    rows = [{
        "series_id": series.pk, "name": series.display_name, "cadence": series.cadence,
        "typical_amount_minor": series.typical_amount_minor,
        "typical_amount_display": format_minor(series.typical_amount_minor, series.currency),
        "confidence": series.confidence, "status": series.status,
    } for series in shown]
    return ToolResult(
        text=json.dumps({"url": url, "rows": rows}),
        figures=tuple(_figure(s.display_name, s.typical_amount_minor, url, s.currency) for s in shown),
        account_ids=_series_account_ids([series.pk for series in shown]),
    )


def _ai_pairs(person):
    """Pairs the model may name: both legs active and inside the AI scope."""
    accounts = visible_accounts(person).values("pk")
    return TransferPair.objects.visible_to(person).filter(  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every TransferPair.objects.visible_to in the baseline
        leg_a__status=Transaction.Status.ACTIVE, leg_b__status=Transaction.Status.ACTIVE,
        leg_a__account_id__in=accounts, leg_b__account_id__in=accounts,
    ).select_related("leg_a", "leg_b", "leg_a__account", "leg_b__account")


def _leg(txn):
    return {"transaction_id": txn.pk, "date": str(txn.transaction_date), "account_id": txn.account_id,
            "amount_minor": txn.amount_minor, "description": txn.description}


def transfer_suggestions(person, _args):
    url = reverse("transfer-review")
    shown = list(_ai_pairs(person).filter(status=TransferPair.Status.SUGGESTED).order_by("-updated_at", "-pk")
                 [:MAX_TOOL_ROWS])
    rows = [{"pair_id": pair.pk, "kind": pair.kind, "confidence": pair.confidence,
             "from": _leg(pair.leg_a), "to": _leg(pair.leg_b)} for pair in shown]
    accounts = {pair.leg_a.account_id for pair in shown} | {pair.leg_b.account_id for pair in shown}
    return ToolResult(text=json.dumps({"url": url, "rows": rows}), account_ids=tuple(sorted(accounts)))


def _ai_suggestions(person):
    """Pending suggestions the model may name: current, on a still uncategorized row inside the AI scope."""
    return CategorySuggestion.objects.visible_to(person).filter(  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every CategorySuggestion.objects.visible_to in the baseline
        transaction__in=_ai_transactions(person),
        category__in=Category.objects.visible_to(person),  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every Category.objects.visible_to in the baseline
    ).select_related("transaction", "transaction__account", "category")


def _suggestion_open(suggestion):
    txn = suggestion.transaction
    return (
        suggestion.status == CategorySuggestion.Status.PENDING
        and suggestion_is_current(suggestion, txn)
        and txn.category_id is None
        and txn.category_source == Transaction.CategorySource.UNSET
    )


def category_suggestions(person, _args):
    nameable = set(ai_nameable_categories(person).values_list("pk", flat=True))
    candidates = _ai_suggestions(person).filter(status=CategorySuggestion.Status.PENDING).order_by("-pk")
    shown = [row for row in candidates[: MAX_TOOL_ROWS * 2] if _suggestion_open(row)][:MAX_TOOL_ROWS]
    rows = [{
        "suggestion_id": row.pk, "transaction_id": row.transaction_id, "date": str(row.transaction.transaction_date),
        "amount_minor": row.transaction.amount_minor, "description": row.transaction.description,
        "suggested_category": row.category.name if row.category_id in nameable else WITHHELD_CATEGORY_NAME,
    } for row in shown]
    return ToolResult(text=json.dumps({"url": reverse("transaction-list"), "rows": rows}),
                      account_ids=tuple(sorted({row.transaction.account_id for row in shown})))


# ---------------------------------------------------------------------------
# Proposing: one row per item, each checked on its own
# ---------------------------------------------------------------------------


def _items_schema(properties, required):
    return {"type": "array", "maxItems": MAX_CARD_ITEMS,
            "items": {"type": "object", "properties": properties, "required": list(required)}}


def cleanup_tool_rows():
    """(name, description, properties, required, handler) for chat_proposals.proposal_tools."""
    return (
        ("propose_recurring_charges", (
            "Suggest confirming or dismissing detected recurring charges, many in one card with a checkbox "
            "each. series_id comes from recurring_suggestions. The member must confirm."
        ), {"items": _items_schema({
            "series_id": {"type": "integer"},
            "action": {"type": "string", "enum": ["confirm", "dismiss"]},
        }, ["series_id", "action"])}, ["items"], _propose_recurring),
        ("propose_transfers", (
            "Suggest transfer cleanup, many in one card with a checkbox each: confirm or dismiss a suggested "
            "pair (pair_id from transfer_suggestions), or mark or unmark one transaction as a transfer to or "
            "from an account the planner doesn't track (transaction_id). The member must confirm."
        ), {"items": _items_schema({
            "action": {"type": "string", "enum": ["confirm", "dismiss", "mark", "unmark"]},
            "pair_id": {"type": "integer"},
            "transaction_id": {"type": "integer"},
        }, ["action"])}, ["items"], _propose_transfers),
        ("propose_ai_suggestions", (
            "Suggest accepting or rejecting pending AI category suggestions in bulk, one card with a checkbox "
            "each. suggestion_id comes from category_suggestions. The member must confirm."
        ), {"items": _items_schema({
            "suggestion_id": {"type": "integer"},
            "action": {"type": "string", "enum": ["accept", "reject"]},
        }, ["suggestion_id", "action"])}, ["items"], _propose_ai_suggestions),
        ("propose_notes", (
            "Suggest adding a note line to transactions, by id, one card with a checkbox each. The line is "
            "added below any existing note. The member must confirm."
        ), {"items": _items_schema({
            "transaction_id": {"type": "integer"},
            "note": {"type": "string"},
        }, ["transaction_id", "note"])}, ["items"], _propose_notes),
        ("propose_splits", (
            "Suggest splitting transactions across categories, one card row per transaction. Each part is a "
            "category and positive cents; the parts take the transaction's sign and must add up exactly to "
            "its amount, with 2 to 20 parts. The member must confirm."
        ), {"items": _items_schema({
            "transaction_id": {"type": "integer"},
            "parts": {"type": "array", "maxItems": MAX_SPLIT_PARTS, "items": {
                "type": "object",
                "properties": {"category": {"type": "string"}, "amount_minor": {"type": "integer"}},
                "required": ["category", "amount_minor"],
            }},
        }, ["transaction_id", "parts"])}, ["items"], _propose_splits),
    )


def _claim(row, key, seen):
    """Mark a row's target as used in this card; a second row for it gets a problem."""
    if key in seen:
        row["error"] = DUPLICATE
        return False
    seen.add(key)
    return True


# Recurring charges -----------------------------------------------------------


def _series_snapshot(series):
    return {"status": series.status, "fingerprint": series.fingerprint}


def _recurring_row(person, raw, seen):
    action = _action(raw, ("confirm", "dismiss"))
    if action is None:
        return _row("confirm", ACTION_UNKNOWN)
    target = _int_or_none(raw.get("series_id"))
    series = _ai_series(person).filter(pk=target, status__in=OPEN_SERIES).first() if target is not None else None
    row = _row(action)
    if series is None:
        row["error"] = ITEM_UNAVAILABLE
    elif _claim(row, series.pk, seen):
        row.update(id=series.pk, before=_series_snapshot(series), accounts=list(_series_account_ids([series.pk])))
    return row


# Transfers --------------------------------------------------------------------

PAIR_ACTIONS = ("confirm", "dismiss")
MARK_ACTIONS = ("mark", "unmark")


def _mark_problem(txn, action):
    if action == "mark":
        return mark_refusal(txn)
    return None if txn.untracked_transfer else NOT_MARKED


def _transfer_row(person, raw, seen):
    action = _action(raw, PAIR_ACTIONS + MARK_ACTIONS)
    if action is None:
        return _row("confirm", ACTION_UNKNOWN, target="pair")
    if action in PAIR_ACTIONS:
        row = _row(action, target="pair")
        target = _int_or_none(raw.get("pair_id"))
        pair = (_ai_pairs(person).filter(pk=target, status=TransferPair.Status.SUGGESTED).first()
                if target is not None else None)
        if pair is None:
            row["error"] = ITEM_UNAVAILABLE
        elif _claim(row, ("pair", pair.pk), seen):
            row.update(id=pair.pk, before={"status": pair.status},
                       accounts=[pair.leg_a.account_id, pair.leg_b.account_id])
        return row
    row = _row(action, target="transaction")
    target = _int_or_none(raw.get("transaction_id"))
    txn = _ai_transactions(person).filter(pk=target).first() if target is not None else None
    if txn is None:
        row["error"] = ITEM_UNAVAILABLE
    elif _claim(row, ("transaction", txn.pk), seen):
        row.update(id=txn.pk, before={"untracked": txn.untracked_transfer}, accounts=[txn.account_id],
                   error=_mark_problem(with_transfer_state(person, txn), action))
    return row


# AI category suggestions -------------------------------------------------------


def _suggestion_snapshot(suggestion):
    return {"status": suggestion.status, "hash": suggestion.snapshot_hash, "category_id": suggestion.category_id}


def _suggestion_row(person, raw, seen):
    action = _action(raw, ("accept", "reject"))
    if action is None:
        return _row("accept", ACTION_UNKNOWN)
    target = _int_or_none(raw.get("suggestion_id"))
    suggestion = _ai_suggestions(person).filter(pk=target).first() if target is not None else None
    row = _row(action)
    if suggestion is None or not _suggestion_open(suggestion):
        row["error"] = ITEM_UNAVAILABLE
    elif _claim(row, suggestion.pk, seen):
        row.update(id=suggestion.pk, before=_suggestion_snapshot(suggestion),
                   accounts=[suggestion.transaction.account_id])
    return row


# Notes ------------------------------------------------------------------------


def _note_hash(note):
    return hashlib.sha256((note or "").encode("utf-8")).hexdigest()


def _with_note(current, line):
    return f"{current}\n{line}" if current else line


def _note_row(person, raw, seen):
    # The card shows the model's line; the stored snapshot of the current note is a hash only.
    line = _text(raw.get("note"))
    row = _row("add", note=line[:MAX_NOTE_CHARS])
    target = _int_or_none(raw.get("transaction_id"))
    txn = _ai_transactions(person).filter(pk=target).first() if target is not None else None
    if txn is None:
        row["error"] = ITEM_UNAVAILABLE
    elif _claim(row, txn.pk, seen):
        row.update(id=txn.pk, before={"note_hash": _note_hash(txn.note)}, accounts=[txn.account_id])
        if not line:
            row["error"] = NOTE_EMPTY
        elif len(_with_note(txn.note or "", line)) > MAX_NOTE_CHARS:
            row["error"] = NOTE_WOULD_BE_TOO_LONG
    return row


# Splits -----------------------------------------------------------------------


def _split_snapshot(txn):
    return {"amount_minor": txn.amount_minor, "category_id": txn.category_id, "category_source": txn.category_source}


def _part_amount(value):
    """A non-zero whole number of cents as given, or None. Fractions and booleans are refused."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    try:
        minor = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return minor or None


def _split_parts(person, raw_parts):
    """(parts as the card shows them, first problem or None). Amounts are stored as given."""
    raw_parts = raw_parts if isinstance(raw_parts, list) else []
    parts = []
    problem = None
    for number, raw in enumerate(raw_parts[:MAX_SPLIT_PARTS], 1):
        raw = raw if isinstance(raw, dict) else {}
        category = _category_by_name(person, raw.get("category"))
        amount = _part_amount(raw.get("amount_minor"))
        parts.append({"category_id": category.pk if category else None, "label": _text(raw.get("category"))[:80],
                      "amount_minor": amount})
        if problem is None and category is None:
            problem = f"Part {number} {CATEGORY_PROBLEM[0].lower()}{CATEGORY_PROBLEM[1:]}"
        if problem is None and amount is None:
            problem = f"Part {number} amount: enter a whole number of cents that is not zero."
    if len(raw_parts) > MAX_SPLIT_PARTS:
        problem = SPLIT_TOO_MANY
    elif len(raw_parts) < 2:
        problem = SPLIT_COUNT_ERROR
    return parts, problem


def _signed_parts(parts, txn_amount):
    """The parts with the transaction's sign, or a problem: all positive, or all already with its sign."""
    amounts = [part["amount_minor"] for part in parts]
    if txn_amount == 0 or not (all(a > 0 for a in amounts) or all((a > 0) == (txn_amount > 0) for a in amounts)):
        return None, SPLIT_SIGN_ERROR
    sign = 1 if txn_amount > 0 else -1
    signed = [abs(amount) * sign for amount in amounts]
    if sum(signed) != txn_amount:
        total = format_minor(abs(sum(signed)), "USD")
        return None, f"{SPLIT_SUM_ERROR} The parts add up to {total}; the transaction is {format_minor(abs(txn_amount), 'USD')}."
    return signed, None


def _split_target_problem(person, txn):
    if txn.category_source == Transaction.CategorySource.SPLIT:
        return ALREADY_SPLIT
    if with_transfer_state(person, txn).is_excluded_transfer:
        return SPLIT_TRANSFER_ERROR
    if transaction_is_linked_refund(txn):
        return SPLIT_REFUND_ERROR
    if RefundLink.objects.visible_to(person).filter(original=txn, refund__status=Transaction.Status.ACTIVE).exists():  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every RefundLink.objects.visible_to in the baseline
        return REFUND_PART_NEEDED
    return None


def _split_row(person, raw, seen):
    parts, problem = _split_parts(person, raw.get("parts"))
    row = _row("split", parts=parts)
    target = _int_or_none(raw.get("transaction_id"))
    txn = _ai_transactions(person).filter(pk=target).first() if target is not None else None
    if txn is None:
        row["error"] = ITEM_UNAVAILABLE
        return row
    if not _claim(row, txn.pk, seen):
        return row
    row.update(id=txn.pk, before=_split_snapshot(txn), accounts=[txn.account_id])
    problem = _split_target_problem(person, txn) or problem
    if problem is None:
        signed, problem = _signed_parts(parts, txn.amount_minor)
        if signed is not None:
            for part, amount in zip(parts, signed, strict=True):
                part["amount_minor"] = amount
    row["error"] = problem
    return row


def _proposer(kind, build_row):
    """A propose handler: one row per item, each checked alone; touched accounts are recorded as used."""
    def propose(person, args, conversation, turn):
        items, dropped = _raw_items(args)
        seen = set()
        rows = [build_row(person, raw, seen) for raw in items]
        accounts = [account for row in rows for account in row.pop("accounts", [])]
        return _store_items(conversation, turn, kind, rows, dropped, account_ids=accounts)

    return propose


_propose_recurring = _proposer(AiProposal.Kind.RECURRING, _recurring_row)
_propose_transfers = _proposer(AiProposal.Kind.TRANSFERS, _transfer_row)
_propose_ai_suggestions = _proposer(AiProposal.Kind.AI_SUGGESTIONS, _suggestion_row)
_propose_notes = _proposer(AiProposal.Kind.NOTES, _note_row)
_propose_splits = _proposer(AiProposal.Kind.SPLITS, _split_row)


# ---------------------------------------------------------------------------
# Apply: one ticked row, re-checked as the member, through the manual page's service
# ---------------------------------------------------------------------------


def _visible_or_denied(query, pk):
    found = query.filter(pk=pk).first()
    if found is None:
        raise PermissionDenied(_DENIED)
    return found


def _locked_transaction(person, pk):
    """The row locked the way the manual services lock it (household, accounts, rows), or denied."""
    return _lock_accounts_and_transactions(person, [_visible_or_denied(_ai_transactions(person), pk)])[0]


def _locked(model, pk):
    """Re-read a target under its row lock, so the snapshot check and the service see the same row."""
    found = model.objects.select_for_update(of=("self",)).filter(pk=pk).first()
    if found is None:
        raise PermissionDenied(_DENIED)
    return found


def _apply_recurring_row(person, _payload, row):
    # apply_proposal already holds the household lock, the service's first lock.
    series = _locked(RecurringSeries, _visible_or_denied(_ai_series(person), row["id"]).pk)
    if _series_snapshot(series) != row["before"]:
        raise _Stale(_already(series.status))
    if row["action"] == "confirm":
        confirm_recurring_series(person, series.pk)
        return {"status": ITEM_CONFIRMED, "id": series.pk}
    dismiss_recurring_series(person, series.pk)
    return {"status": ITEM_DISMISSED, "id": series.pk}


def _apply_transfer_row(person, _payload, row):
    if row.get("target") == "pair":
        pair = _visible_or_denied(_ai_pairs(person), row["id"])
        _lock_accounts_and_transactions(person, [pair.leg_a, pair.leg_b])
        pair = _locked(TransferPair, pair.pk)
        if pair.status != row["before"]["status"]:
            raise _Stale(_already(pair.status))
        if row["action"] == "confirm":
            confirm_transfer_pair(person, pair.pk)
            return {"status": ITEM_CONFIRMED, "id": pair.pk}
        dismiss_transfer_pair(person, pair.pk)
        return {"status": ITEM_DISMISSED, "id": pair.pk}
    txn = _locked_transaction(person, row["id"])
    if txn.untracked_transfer != row["before"]["untracked"]:
        raise _Stale(NO_LONGER_APPLIES)
    if row["action"] == "mark":
        mark_untracked_transfer(person, txn.pk)
        return {"status": ITEM_MARKED, "id": txn.pk}
    unmark_untracked_transfer(person, txn.pk)
    return {"status": ITEM_UNMARKED, "id": txn.pk}


def _apply_suggestion_row(person, _payload, row):
    found = _visible_or_denied(_ai_suggestions(person), row["id"])
    txn = _locked_transaction(person, found.transaction_id)
    suggestion = _locked(CategorySuggestion, found.pk)
    suggestion.transaction = txn
    if suggestion.status != CategorySuggestion.Status.PENDING:
        raise _Stale(_already(suggestion.status))
    if _suggestion_snapshot(suggestion) != row["before"] or not _suggestion_open(suggestion):
        raise _Stale(NO_LONGER_APPLIES)
    if row["action"] == "reject":
        reject_suggestion(person, suggestion.pk)
        return {"status": ITEM_REJECTED, "id": txn.pk}
    accepted = accept_suggestion(person, suggestion.pk)
    if accepted.status != CategorySuggestion.Status.ACCEPTED:
        # The service rejects a suggestion it may no longer apply; the row's savepoint undoes that.
        raise _Stale(NO_LONGER_APPLIES)
    return {"status": ITEM_ACCEPTED, "id": txn.pk}


def _apply_note_row(person, _payload, row):
    txn = _locked_transaction(person, row["id"])
    if _note_hash(txn.note) != row["before"]["note_hash"]:
        raise _Stale(NO_LONGER_APPLIES)
    if len(_with_note(txn.note or "", row["note"])) > MAX_NOTE_CHARS:
        raise ValidationError(NOTE_TOO_LONG)
    # Bulk edit's append-note step: the note line plus a correction-history row.
    _apply_note(person, txn, row["note"])
    return {"status": ITEM_NOTED, "id": txn.pk}


def _apply_split_row(person, _payload, row):
    txn = _locked_transaction(person, row["id"])
    if _split_snapshot(txn) != row["before"]:
        raise _Stale(NO_LONGER_APPLIES)
    parts = [{"category_id": part["category_id"], "amount_minor": part["amount_minor"]} for part in row["parts"]]
    split_transaction(person, txn.pk, parts, None)
    return {"status": ITEM_SPLIT, "id": txn.pk}


CLEANUP_APPLIERS = {
    AiProposal.Kind.RECURRING: _apply_recurring_row,
    AiProposal.Kind.TRANSFERS: _apply_transfer_row,
    AiProposal.Kind.AI_SUGGESTIONS: _apply_suggestion_row,
    AiProposal.Kind.NOTES: _apply_note_row,
    AiProposal.Kind.SPLITS: _apply_split_row,
}


# ---------------------------------------------------------------------------
# Cards: each row reads its target fresh as the member. A target the member can no
# longer see shows a generic line, never the stored snapshot or the model's text about it.
# ---------------------------------------------------------------------------

_VERBS = {
    "confirm": "Confirm", "dismiss": "Dismiss", "mark": "Mark as untracked transfer",
    "unmark": "Count again", "accept": "Accept", "reject": "Reject",
}


def _gone(title, summary):
    return {"title": title, "summary": summary, "unavailable": True}


def _blocked(row, summary):
    """A pending row that can no longer be ticked, with the reason as its summary."""
    return {**row, "summary": summary, "unavailable": True}


def _describe_recurring(person, _payload, item, pending):
    verb = _VERBS.get(item.get("action"), "Review")
    series = _visible_series(person).filter(pk=item.get("id")).first() if item.get("id") else None
    if series is None:
        return _gone(f"{verb} a recurring charge", "This recurring charge is not available.")
    row = {
        "title": f"{verb}: {series.display_name}",
        "summary": f"{series.amount_display} {series.get_cadence_display().lower()} · {series.get_confidence_display()} confidence",
        "url": reverse("recurring-review"),
    }
    if pending and not item.get("error") and _series_snapshot(series) != item.get("before"):
        row = _blocked(row, _already(series.status) if series.status not in OPEN_SERIES else CHANGED)
    return row


def _describe_transfer(person, _payload, item, pending):
    verb = _VERBS.get(item.get("action"), "Review")
    if item.get("target") == "pair":
        pair = (TransferPair.objects.visible_to(person).select_related("leg_a", "leg_b", "leg_a__account", "leg_b__account")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every TransferPair.objects.visible_to in the baseline
                .filter(pk=item.get("id")).first() if item.get("id") else None)
        if pair is None:
            return _gone(f"{verb} a transfer pair", "This transfer pair is not available.")
        row = {
            "title": f"{verb} transfer: {pair.leg_a.description} → {pair.leg_b.description}",
            "summary": f"{_transaction_line(pair.leg_a)} → {_transaction_line(pair.leg_b)}",
            "url": reverse("transfer-review"),
        }
        if pending and not item.get("error") and pair.status != (item.get("before") or {}).get("status"):
            row = _blocked(row, _already(pair.status))
        return row
    txn = _visible_transaction(person, item)
    if txn is None:
        return _gone(f"{verb}: a transaction", "This transaction is not available.")
    summary = _transaction_line(txn)
    if item.get("action") == "unmark":
        summary += " · counts in income and spending again"
    row = {"title": f"{verb}: {txn.description}", "summary": summary, "url": _transaction_url(txn)}
    if pending and not item.get("error") and txn.untracked_transfer != (item.get("before") or {}).get("untracked"):
        row = _blocked(row, CHANGED)
    return row


def _visible_transaction(person, item):
    if not item.get("id"):
        return None
    return (Transaction.objects.visible_to(person).filter(pk=item["id"], status=Transaction.Status.ACTIVE)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every Transaction.objects.visible_to in the baseline
            .select_related("account", "category").first())


def _describe_suggestion(person, _payload, item, pending):
    verb = _VERBS.get(item.get("action"), "Review")
    suggestion = (
        CategorySuggestion.objects.visible_to(person).select_related("transaction", "transaction__account")  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every CategorySuggestion.objects.visible_to in the baseline
        .filter(pk=item.get("id")).first() if item.get("id") else None
    )
    if suggestion is None:
        return _gone(f"{verb} a category suggestion", "This suggestion is not available.")
    txn = suggestion.transaction
    row = {
        "title": f"{verb}: {txn.description}",
        "summary": f"{_transaction_line(txn)} · suggested {_category_name(person, suggestion.category_id)}",
        "url": _transaction_url(txn),
    }
    if pending and not item.get("error"):
        if suggestion.status != CategorySuggestion.Status.PENDING:
            row = _blocked(row, _already(suggestion.status))
        elif _suggestion_snapshot(suggestion) != item.get("before") or not _suggestion_open(suggestion):
            row = _blocked(row, CHANGED)
    return row


def _describe_note(person, _payload, item, pending):
    txn = _visible_transaction(person, item)
    if txn is None:
        return _gone("Add a note to a transaction", "This transaction is not available.")
    placement = "added below the current note" if txn.note else "the transaction has no note yet"
    row = {"title": f"Add a note: {txn.description}", "url": _transaction_url(txn),
           "summary": f"{_transaction_line(txn)} · {placement}", "note": item.get("note") or ""}
    if pending and not item.get("error") and _note_hash(txn.note) != (item.get("before") or {}).get("note_hash"):
        row = _blocked(row, CHANGED)
    return row


def _part_text(person, part):
    category = _category_name(person, part["category_id"]) if part.get("category_id") else (
        _text(part.get("label")) or "no category")
    amount = part.get("amount_minor")
    shown = format_minor(abs(amount), "USD") if isinstance(amount, int) and not isinstance(amount, bool) else "not set"
    return f"{category} {shown}"


def _describe_split(person, _payload, item, pending):
    parts = " · ".join(_part_text(person, part) for part in item.get("parts") or []) or "no parts"
    txn = _visible_transaction(person, item)
    if txn is None:
        return _gone("Split a transaction", "This transaction is not available.")
    row = {"title": f"Split: {txn.description}", "url": _transaction_url(txn),
           "summary": f"{_transaction_line(txn)} into {parts}"}
    if pending and not item.get("error") and _split_snapshot(txn) != item.get("before"):
        row = _blocked(row, CHANGED)
    return row


def _card(heading, url_name, label, describe):
    def build(person, proposal, payload, card):
        card["heading"] = heading
        card["page_url"] = reverse(url_name)
        card["page_label"] = label
        _card_items(person, proposal, payload, card, describe)

    return build


CLEANUP_CARD_BUILDERS = {
    AiProposal.Kind.RECURRING: _card("Recurring charges", "recurring-review", "Open recurring charges",
                                     _describe_recurring),
    AiProposal.Kind.TRANSFERS: _card("Transfers", "transfer-review", "Open transfers", _describe_transfer),
    AiProposal.Kind.AI_SUGGESTIONS: _card("AI category suggestions", "transaction-list", "Open transactions",
                                          _describe_suggestion),
    AiProposal.Kind.NOTES: _card("Notes", "transaction-list", "Open transactions", _describe_note),
    AiProposal.Kind.SPLITS: _card("Splits", "transaction-list", "Open transactions", _describe_split),
}
