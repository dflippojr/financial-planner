from dataclasses import dataclass, field
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .category_services import (
    _history_label,
    _record_text_history,
    apply_inherited_category,
    apply_manual_category,
    assignable_categories,
    exclusion_exists_for,
    linked_refunds_for_originals,
    transaction_is_linked_refund,
)
from .lifecycle_services import lock_actor_household
from .models import (
    Account,
    BulkEditUndo,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
    _person_for,
)
from .tag_services import NOTE_TOO_LONG


_DENIED = "Operation is not permitted."
BULK_EDIT_CAP = 500
UNDO_MINUTES = 10
ACTION_CATEGORY = "set_category"
ACTION_ADD_TAGS = "add_tags"
ACTION_REMOVE_TAGS = "remove_tags"
ACTION_APPEND_NOTE = "append_note"
ACTIONS = (ACTION_CATEGORY, ACTION_ADD_TAGS, ACTION_REMOVE_TAGS, ACTION_APPEND_NOTE)
SKIP_INVESTMENT = "investment_activity"
SKIP_SPLIT = "split_parent"
SKIP_TRANSFER = "transfer_or_card_payment"
SKIP_LINKED_REFUND = "linked_refund"
SKIP_NOTE_TOO_LONG = "note_too_long"
SKIP_LABELS = {
    SKIP_INVESTMENT: "Investment activity",
    SKIP_SPLIT: "Split parents (edit parts on the split page)",
    SKIP_TRANSFER: "Transfers and card payments",
    SKIP_LINKED_REFUND: "Linked refunds (follow their purchase)",
    SKIP_NOTE_TOO_LONG: "Note would exceed 2,000 characters",
}
NO_TAGS = "No tags"
OVER_CAP = "Narrow the filters; bulk edit can select at most 500 matching rows."
UNDO_UNAVAILABLE = "Undo is not available."
CHANGED_SINCE = "Undo is not available because a row changed since the bulk edit."
NO_ELIGIBLE = "No eligible transactions to update."
EMPTY_NOTE = "Enter a note line to append."
EMPTY_TAGS = "Choose at least one tag."
UNKNOWN_ACTION = "Choose a bulk edit action."


@dataclass
class BulkEditPreview:
    matching_count: int
    over_cap: bool
    selected_count: int
    selected_ids: list
    eligible_ids: list
    skip_counts: dict = field(default_factory=dict)
    action: str = ""


def _visible_active(person):
    return Transaction.objects.visible_to(person).filter(status=Transaction.Status.ACTIVE)


def _parse_ids(raw_ids):
    ids = []
    seen = set()
    for item in raw_ids or []:
        try:
            pk = int(item)
        except (TypeError, ValueError):
            continue
        if pk not in seen:
            seen.add(pk)
            ids.append(pk)
    return ids


def _tag_label(tags):
    names = sorted(tag.name for tag in tags)
    return ", ".join(names) if names else NO_TAGS


def _category_for(person, category_id):
    if not category_id:
        return None
    category = assignable_categories(person).filter(pk=category_id).first()
    if category is None:
        raise PermissionDenied(_DENIED)
    return category


def _tags_for(person, tag_ids):
    requested = _parse_ids(tag_ids)
    if not requested:
        raise ValidationError(EMPTY_TAGS)
    tags = list(Tag.objects.visible_to(person).active().filter(pk__in=requested))
    if {tag.pk for tag in tags} != set(requested):
        raise PermissionDenied(_DENIED)
    return tags


def _candidate_rows(person, matching, transaction_ids, select_matching):
    matching_count = matching.count()
    if select_matching:
        if matching_count > BULK_EDIT_CAP:
            return matching_count, True, []
        rows = list(
            matching.select_related("account", "category", "refund_link").prefetch_related("tags")[
                :BULK_EDIT_CAP
            ]
        )
        return matching_count, False, rows
    ids = _parse_ids(transaction_ids)
    if not ids:
        return matching_count, False, []
    if len(ids) > BULK_EDIT_CAP:
        return matching_count, True, []
    rows = list(
        _visible_active(person)
        .filter(pk__in=ids)
        .annotate(_excluded=exclusion_exists_for(person))
        .select_related("account", "category", "refund_link")
        .prefetch_related("tags")
        .order_by("pk")
    )
    return matching_count, False, rows


def _skip_reason(txn, action, note_line=""):
    if txn.kind == Transaction.Kind.INVESTMENT_ACTIVITY:
        return SKIP_INVESTMENT
    if action == ACTION_CATEGORY:
        if transaction_is_linked_refund(txn):
            return SKIP_LINKED_REFUND
        if txn.category_source == Transaction.CategorySource.SPLIT:
            return SKIP_SPLIT
        if txn.is_excluded_transfer:
            return SKIP_TRANSFER
    if action == ACTION_APPEND_NOTE:
        current = txn.note or ""
        addition = note_line or ""
        combined = f"{current}\n{addition}" if current else addition
        if len(combined) > 2000:
            return SKIP_NOTE_TOO_LONG
    return None


def _will_change(txn, action, *, category=None, tags=None, note_line=""):
    if action == ACTION_CATEGORY:
        target_id = None if category is None else category.pk
        return txn.category_id != target_id or txn.category_source != Transaction.CategorySource.MANUAL
    if action == ACTION_ADD_TAGS:
        have = {tag.pk for tag in txn.tags.all()}
        return any(tag.pk not in have for tag in tags)
    if action == ACTION_REMOVE_TAGS:
        have = {tag.pk for tag in txn.tags.all()}
        return any(tag.pk in have for tag in tags)
    if action == ACTION_APPEND_NOTE:
        return bool(note_line)
    return False


def preview_bulk_edit(
    principal,
    *,
    matching,
    transaction_ids,
    select_matching,
    action,
    category_id=None,
    tag_ids=None,
    note_line="",
):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    if action not in ACTIONS:
        raise ValidationError(UNKNOWN_ACTION)
    if action == ACTION_CATEGORY:
        _category_for(person, category_id)
    if action in (ACTION_ADD_TAGS, ACTION_REMOVE_TAGS):
        _tags_for(person, tag_ids)
    if action == ACTION_APPEND_NOTE:
        cleaned = (note_line or "").strip()
        if not cleaned:
            raise ValidationError(EMPTY_NOTE)
        note_line = cleaned
        if len(note_line) > 2000:
            raise ValidationError(NOTE_TOO_LONG)
    matching_count, over_cap, rows = _candidate_rows(person, matching, transaction_ids, select_matching)
    if over_cap:
        return BulkEditPreview(
            matching_count=matching_count,
            over_cap=True,
            selected_count=0,
            selected_ids=[],
            eligible_ids=[],
            skip_counts={},
            action=action,
        )
    skip_counts = {}
    eligible_ids = []
    selected_ids = sorted(txn.pk for txn in rows)
    category = _category_for(person, category_id) if action == ACTION_CATEGORY else None
    tags = _tags_for(person, tag_ids) if action in (ACTION_ADD_TAGS, ACTION_REMOVE_TAGS) else []
    for txn in rows:
        reason = _skip_reason(txn, action, note_line=note_line)
        if reason:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
            continue
        if _will_change(txn, action, category=category, tags=tags, note_line=note_line):
            eligible_ids.append(txn.pk)
    eligible_ids.sort()
    return BulkEditPreview(
        matching_count=matching_count,
        over_cap=False,
        selected_count=len(rows),
        selected_ids=selected_ids,
        eligible_ids=eligible_ids,
        skip_counts=skip_counts,
        action=action,
    )


def _lock_rows(person, transactions, extra=()):
    lock_actor_household(person)
    required_ids = sorted({item.pk for item in transactions})
    all_items = list(transactions) + [item for item in extra if item.pk not in set(required_ids)]
    account_ids = sorted({item.account_id for item in all_items})
    if account_ids:
        list(Account.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
    ids = sorted({item.pk for item in all_items})
    locked = list(
        Transaction.objects.select_for_update(of=("self",))
        .select_related("account", "category", "refund_link")
        .prefetch_related("tags")
        .filter(pk__in=ids, status=Transaction.Status.ACTIVE)
        .order_by("pk")
    )
    if len(locked) != len(ids):
        raise PermissionDenied(_DENIED)
    visible = set(_visible_active(person).filter(pk__in=required_ids).values_list("pk", flat=True))
    if visible != set(required_ids):
        raise PermissionDenied(_DENIED)
    excluded = {
        pk
        for pk, is_excluded in _visible_active(person)
        .filter(pk__in=required_ids)
        .annotate(_excluded=exclusion_exists_for(person))
        .values_list("pk", "_excluded")
        if is_excluded
    }
    for txn in locked:
        txn._excluded = txn.pk in excluded
    return locked


def _append_note(txn, note_line):
    current = txn.note or ""
    return f"{current}\n{note_line}" if current else note_line


def _refunds_by_original(by_id, refunds):
    grouped = {}
    for refund in refunds:
        locked_refund = by_id[refund.pk]
        grouped.setdefault(locked_refund.refund_link.original_id, []).append(locked_refund)
    return grouped


def _apply_category(person, txn, category, refunds):
    apply_manual_category(person, txn, category)
    for refund in refunds:
        apply_inherited_category(person, refund, category)


def _apply_tags(person, txn, tags, *, add):
    previous = list(txn.tags.all())
    have = {tag.pk: tag for tag in previous}
    if add:
        for tag in tags:
            have[tag.pk] = tag
        next_tags = list(have.values())
    else:
        drop = {tag.pk for tag in tags}
        next_tags = [tag for tag in previous if tag.pk not in drop]
    txn.tags.set(next_tags)
    txn.save(update_fields=("updated_at",))
    _record_text_history(
        txn,
        person,
        TransactionCorrectionHistory.Field.TAGS,
        _tag_label(previous),
        _tag_label(next_tags),
    )


def _apply_note(person, txn, note_line):
    previous = txn.note or ""
    txn.note = _append_note(txn, note_line)
    txn.save(update_fields=("note", "updated_at"))
    _record_text_history(
        txn,
        person,
        TransactionCorrectionHistory.Field.NOTE,
        previous,
        txn.note,
    )


def _row_snapshot(txn, action):
    payload = {
        "id": txn.pk,
        "applied_updated_at": txn.updated_at.isoformat(),
        "action": action,
        "previous_category_id": txn.category_id,
        "previous_category_source": txn.category_source,
        "previous_note": txn.note or "",
        "previous_tag_ids": sorted(tag.pk for tag in txn.tags.all()),
    }
    return payload


@transaction.atomic
def apply_bulk_edit(
    principal,
    *,
    matching,
    transaction_ids,
    select_matching,
    action,
    category_id=None,
    tag_ids=None,
    note_line="",
    expected_eligible_ids=None,
):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    preview = preview_bulk_edit(
        person,
        matching=matching,
        transaction_ids=transaction_ids,
        select_matching=False,
        action=action,
        category_id=category_id,
        tag_ids=tag_ids,
        note_line=note_line,
    )
    if preview.over_cap:
        raise ValidationError(OVER_CAP)
    if not preview.eligible_ids:
        raise ValidationError(NO_ELIGIBLE)
    if expected_eligible_ids is not None and set(preview.eligible_ids) != set(_parse_ids(expected_eligible_ids)):
        raise ValidationError(NO_ELIGIBLE)
    if action == ACTION_APPEND_NOTE:
        note_line = (note_line or "").strip()
    category = _category_for(person, category_id) if action == ACTION_CATEGORY else None
    tags = _tags_for(person, tag_ids) if action in (ACTION_ADD_TAGS, ACTION_REMOVE_TAGS) else []
    unlocked = list(_visible_active(person).filter(pk__in=preview.eligible_ids).order_by("pk"))
    refunds = linked_refunds_for_originals(unlocked) if action == ACTION_CATEGORY else []
    locked = _lock_rows(person, unlocked, extra=refunds)
    by_id = {txn.pk: txn for txn in locked}
    eligible = []
    for pk in preview.eligible_ids:
        txn = by_id.get(pk)
        if txn is None:
            continue
        reason = _skip_reason(txn, action, note_line=note_line)
        if reason:
            continue
        if _will_change(txn, action, category=category, tags=tags, note_line=note_line):
            eligible.append(txn)
    if {txn.pk for txn in eligible} != set(preview.eligible_ids):
        # Eligibility moved after lock; refuse rather than write a different set.
        raise ValidationError(NO_ELIGIBLE)
    refunds_by_original = _refunds_by_original(by_id, refunds)
    snapshots = []
    for txn in eligible:
        related = refunds_by_original.get(txn.pk, [])
        before = _row_snapshot(txn, action)
        if action == ACTION_CATEGORY:
            _apply_category(person, txn, category, related)
        elif action == ACTION_ADD_TAGS:
            _apply_tags(person, txn, tags, add=True)
        elif action == ACTION_REMOVE_TAGS:
            _apply_tags(person, txn, tags, add=False)
        else:
            _apply_note(person, txn, note_line)
        txn.refresh_from_db()
        before["applied_updated_at"] = txn.updated_at.isoformat()
        snapshots.append(before)
    undo = BulkEditUndo.objects.create(
        actor=person,
        expires_at=timezone.now() + timedelta(minutes=UNDO_MINUTES),
        snapshot={"action": action, "rows": snapshots},
    )
    if action == ACTION_CATEGORY:
        from finance.alert_services import schedule_after_category_change

        schedule_after_category_change()
    return preview, undo


@transaction.atomic
def undo_bulk_edit(principal, undo_id):
    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    record = BulkEditUndo.objects.visible_to(person).filter(pk=undo_id).first()
    if record is None:
        raise PermissionDenied(_DENIED)
    if record.undone_at is not None or record.expires_at <= timezone.now():
        raise ValidationError(UNDO_UNAVAILABLE)
    rows = record.snapshot.get("rows") or []
    ids = [item["id"] for item in rows]
    if not ids:
        raise ValidationError(UNDO_UNAVAILABLE)
    action = record.snapshot.get("action")
    unlocked = list(_visible_active(person).filter(pk__in=ids))
    if len(unlocked) != len(ids):
        raise ValidationError(CHANGED_SINCE)
    refunds = linked_refunds_for_originals(unlocked) if action == ACTION_CATEGORY else []
    locked = _lock_rows(person, unlocked, extra=refunds)
    by_id = {txn.pk: txn for txn in locked}
    for item in rows:
        txn = by_id.get(item["id"])
        if txn is None:
            raise ValidationError(CHANGED_SINCE)
        if txn.updated_at.isoformat() != item["applied_updated_at"]:
            raise ValidationError(CHANGED_SINCE)
    refunds_by_original = _refunds_by_original(by_id, refunds)
    for item in rows:
        txn = by_id[item["id"]]
        if action == ACTION_CATEGORY:
            previous_label = _history_label(txn.category)
            txn.category_id = item["previous_category_id"]
            txn.category_source = item["previous_category_source"]
            txn.save(update_fields=("category", "category_source", "updated_at"))
            txn.refresh_from_db()
            _record_text_history(
                txn,
                person,
                TransactionCorrectionHistory.Field.CATEGORY,
                previous_label,
                _history_label(txn.category),
            )
            for refund in refunds_by_original.get(txn.pk, []):
                apply_inherited_category(person, refund, txn.category)
        elif action in (ACTION_ADD_TAGS, ACTION_REMOVE_TAGS):
            previous = list(txn.tags.all())
            restored = list(Tag.objects.visible_to(person).filter(pk__in=item["previous_tag_ids"]))
            if {tag.pk for tag in restored} != set(item["previous_tag_ids"]):
                raise PermissionDenied(_DENIED)
            txn.tags.set(restored)
            txn.save(update_fields=("updated_at",))
            _record_text_history(
                txn,
                person,
                TransactionCorrectionHistory.Field.TAGS,
                _tag_label(previous),
                _tag_label(restored),
            )
        elif action == ACTION_APPEND_NOTE:
            previous = txn.note or ""
            txn.note = item["previous_note"]
            txn.save(update_fields=("note", "updated_at"))
            _record_text_history(
                txn,
                person,
                TransactionCorrectionHistory.Field.NOTE,
                previous,
                txn.note,
            )
    record.undone_at = timezone.now()
    record.save(update_fields=("undone_at",))
    if action == ACTION_CATEGORY:
        from finance.alert_services import schedule_after_category_change

        schedule_after_category_change()
    return record
