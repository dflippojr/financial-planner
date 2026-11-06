"""The recurring series note page and the undo link after adding a note to existing rows (#684)."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access import first_message as _first_message
from .access import service_or_404 as _service_or_404
from .auto_notes import preview_token
from .bulk_edit_views import active_bulk_undo
from .forms import AutoNoteForm
from .recurring_services import (
    apply_recurring_note_to_existing,
    note_version,
    recurring_note_preview,
    set_recurring_series_note,
)

_UNDO_TARGET_KEY = "auto_note_undo_target"


def remember_note_undo(request, target, undo, count):
    """Offer undo for this apply on its own page, and on Transactions like any bulk edit."""
    request.session["bulk_edit_undo_id"] = str(undo.pk)
    request.session[_UNDO_TARGET_KEY] = f"{target}:{undo.pk}"
    messages.success(request, f"Added the note to {count} transaction{'s' if count != 1 else ''}.")


def note_undo_for(request, target):
    """The still-open undo of a note this page added, or None (never another page's bulk edit)."""
    undo = active_bulk_undo(request)
    if undo is None or request.session.get(_UNDO_TARGET_KEY) != f"{target}:{undo.pk}":
        return None
    return undo


@require_http_methods(["GET", "POST"])
@never_cache
def recurring_note(request, series_id):
    series, rows = _service_or_404(lambda: recurring_note_preview(request.user, series_id))
    form = AutoNoteForm(initial={"note": series.note})
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            form = AutoNoteForm(request.POST)
            if form.is_valid():
                try:
                    _service_or_404(lambda: set_recurring_series_note(request.user, series_id, form.cleaned_data["note"]))
                except ValidationError as exc:
                    form.add_error("note", _first_message(exc, "The note could not be saved."))
                else:
                    messages.success(request, "Note saved. New charges in this series will get it.")
                    return redirect("recurring-note", series_id=series_id)
        elif action == "apply":
            try:
                changed, undo = _service_or_404(lambda: apply_recurring_note_to_existing(
                    request.user, series_id, previewed_version=request.POST.get("note_version") or "",
                    expected_token=request.POST.get("preview_token") or "",
                ))
            except ValidationError as exc:
                error = _first_message(exc, "The note could not be added.")
                series, rows = _service_or_404(lambda: recurring_note_preview(request.user, series_id))
            else:
                remember_note_undo(request, f"recurring:{series_id}", undo, len(changed))
                return redirect("recurring-note", series_id=series_id)
        else:
            raise Http404
    return render(request, "finance/recurring_note.html", {
        "series": series,
        "form": form,
        "rows": rows,
        "preview_token": preview_token(rows),
        "note_version": note_version(series),
        "apply_error": error,
        "note_undo": note_undo_for(request, f"recurring:{series_id}"),
    })
