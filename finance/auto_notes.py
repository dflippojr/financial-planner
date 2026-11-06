"""Note lines a categorization rule or a recurring series adds to transactions (#684).

A rule or a confirmed series may carry a one-line note. The line is appended
below whatever note a transaction already has, the way chat notes append
(#680), and is skipped when that exact line is already one of the note's lines
or when the combined note would pass the 2,000-character limit. Each added line
writes a correction-history row whose new value names its source, for example
"(rule: GOODYEAR)" or "(recurring: Synthetic Streaming)".

Adding or editing a rule's or series' note only reaches new transactions on its
own. Existing transactions get it only through the explicit preview-then-apply
step, which can be undone like a bulk edit.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from .bulk_edit_services import ACTION_APPEND_NOTE, UNDO_MINUTES
from .input_limits import AUTO_NOTE_MAX_CHARS, MAX_NOTE_CHARS
from .models import BulkEditUndo, Transaction, TransactionCorrectionHistory

NOTE_LINE_TOO_LONG = f"Notes must be {AUTO_NOTE_MAX_CHARS:,} characters or fewer."
NOTE_PREVIEW_CHANGED = "The matching transactions changed since the preview. Check the preview and apply again."
NO_NOTE_TO_ADD = "Add a note first."
NOTHING_TO_ADD = "No transactions need this note."


def clean_note_line(value):
    """One line: surrounding space trimmed and any run of whitespace (including line breaks) made one space."""
    return " ".join((value or "").split())


def validate_note_line(value):
    line = clean_note_line(value)
    if len(line) > AUTO_NOTE_MAX_CHARS:
        raise ValidationError(NOTE_LINE_TOO_LONG)
    return line


def has_line(note, line):
    return line in (note or "").splitlines()


def appended_note(note, line):
    """The note with the line added below it, or None when nothing should change."""
    if not line or has_line(note, line):
        return None
    combined = f"{note}\n{line}" if note else line
    if len(combined) > MAX_NOTE_CHARS:
        return None
    return combined


def rule_source(rule):
    return f" (rule: {rule.description_contains})"


def recurring_source(series):
    return f" (recurring: {series.display_name})"


def append_note_lines(actor, items, *, now=None):
    """Append each (transaction, line, source) in place and record its history; returns the changed rows.

    Callers pass rows they hold locked or just created. Writes are batched for
    imports: rows without a note take one UPDATE per line, the rest one bulk update.
    """
    now = now or timezone.now()
    changed, history, blank_by_line, with_note = [], [], {}, []
    for txn, line, source in items:
        previous = txn.note or ""
        combined = appended_note(previous, line)
        if combined is None:
            continue
        txn.note = combined
        txn.updated_at = now
        changed.append(txn)
        (with_note.append(txn) if previous else blank_by_line.setdefault(line, []).append(txn.pk))
        history.append(TransactionCorrectionHistory(
            transaction=txn, actor=actor, recorded_at=now, field_name=TransactionCorrectionHistory.Field.NOTE,
            previous_description=previous, new_description=f"{combined}{source}",
        ))
    for line, ids in blank_by_line.items():
        Transaction.objects.filter(pk__in=ids).update(note=line, updated_at=now)
    if with_note:
        Transaction.objects.bulk_update(with_note, ("note", "updated_at"), batch_size=500)
    TransactionCorrectionHistory.objects.bulk_create(history)
    return changed


def rows_needing_line(rows, line):
    """The rows the line would be added to: not already there and still within the length limit."""
    return [txn for txn in rows if appended_note(txn.note or "", line) is not None]


def preview_token(rows):
    """One bounded form value naming exactly the previewed rows, however many there are.

    The confirm form posts this instead of one field per row (a large rule
    would pass Django's 1,000-field limit); the apply recomputes it from the
    rows it reads under lock and refuses when they differ.
    """
    ids = ",".join(str(pk) for pk in sorted(txn.pk for txn in rows))
    return hashlib.sha256(ids.encode("ascii")).hexdigest()


def apply_line_to_existing(actor, locked, line, source, expected_token):
    """The confirm step: add the line to exactly the previewed rows, with a bulk-edit undo record.

    `locked` holds the rows re-read under lock. If the rows that need the line
    are not exactly the ones the member previewed (`expected_token`, from
    `preview_token`), nothing is written.
    """
    if not line:
        raise ValidationError(NO_NOTE_TO_ADD)
    targets = rows_needing_line(locked, line)
    if not targets:
        raise ValidationError(NOTHING_TO_ADD)
    if preview_token(targets) != expected_token:
        raise ValidationError(NOTE_PREVIEW_CHANGED)
    previous = {txn.pk: txn.note or "" for txn in targets}
    now = timezone.now()
    changed = append_note_lines(actor, [(txn, line, source) for txn in targets], now=now)
    # Bulk edit's snapshot shape and undo window, so `undo_bulk_edit` restores these rows too.
    undo = BulkEditUndo.objects.create(
        id=uuid.uuid4(),
        actor=actor,
        expires_at=now + timedelta(minutes=UNDO_MINUTES),
        snapshot={"action": ACTION_APPEND_NOTE, "rows": [
            {
                "id": txn.pk,
                "applied_updated_at": now.isoformat(),
                "action": ACTION_APPEND_NOTE,
                "previous_note": previous[txn.pk],
            }
            for txn in changed
        ]},
    )
    return changed, undo
