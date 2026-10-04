from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .forms import (
    SheetColumnMappingForm,
    SheetComparisonFilterForm,
    SheetCsvUploadForm,
    SheetMonthNoteForm,
    SheetToleranceForm,
)
from .csv_import.parser import CsvInputError
from .csv_import.staging import (
    KIND_SHEET_COMPARISON,
    StageUnavailable,
    create_stage,
    delete_stage,
    load_stage,
)
from .models import Person
from .sheet_comparison import (
    DEFAULT_TOLERANCE_MINOR,
    SESSION_KEY,
    SheetCsvError,
    comparison_rows,
    delete_comparison_data,
    mapping_matches_headers,
    parsed_month_totals,
    read_sheet_csv,
    save_mapping,
    save_month_note,
    set_tolerance,
    settings_for,
    store_month_totals,
)


def _person(request):
    return get_object_or_404(Person, user=request.user)


def _pending(request):
    pending = request.session.get(SESSION_KEY)
    if not isinstance(pending, dict):
        return None
    headers = pending.get("headers")
    token = pending.get("token")
    source = pending.get("source")
    if not isinstance(headers, list) or not token or not source:
        return None
    try:
        load_stage(request, token, None, kind=KIND_SHEET_COMPARISON)
    except StageUnavailable:
        request.session.pop(SESSION_KEY, None)
        return None
    return pending


def _clear_pending(request):
    pending = request.session.get(SESSION_KEY)
    token = pending.get("token") if isinstance(pending, dict) else None
    if token:
        delete_stage(request, token)
    request.session.pop(SESSION_KEY, None)


def _store_pending(request, headers, token, source):
    request.session[SESSION_KEY] = {
        "headers": list(headers),
        "token": token,
        "source": source,
    }


def _staged_rows(request, pending):
    raw = load_stage(request, pending["token"], None, kind=KIND_SHEET_COMPARISON)
    return read_sheet_csv(raw)


def _filter_kwargs(form):
    if not form.is_valid():
        return {"account": None, "scope": "", "tag": None}
    return {
        "account": form.cleaned_data.get("account"),
        "scope": form.cleaned_data.get("scope") or "",
        "tag": form.cleaned_data.get("tag"),
    }


def _commit_rows(request, person, headers, rows, source, token, mapping_data=None):
    settings_row = settings_for(person)
    # One transaction: a new mapping is kept only if its rows also parse and store.
    with transaction.atomic():
        if mapping_data is not None:
            settings_row = save_mapping(person, headers=headers, **mapping_data)
        elif not mapping_matches_headers(settings_row, headers):
            _store_pending(request, headers, token, source)
            return False
        by_month = parsed_month_totals(rows, settings_row)
        store_month_totals(person, by_month, source)
    delete_stage(request, token)
    request.session.pop(SESSION_KEY, None)
    messages.success(request, "Stored month totals from the CSV. The file was not kept.")
    return True


@require_http_methods(["GET", "POST"])
@never_cache
def sheet_comparison(request):
    person = _person(request)
    filter_form = SheetComparisonFilterForm(request.GET or None, principal=person)
    upload_form = SheetCsvUploadForm()
    pending = _pending(request)
    settings_row = settings_for(person)
    mapping_form = None
    if pending:
        mapping_form = SheetColumnMappingForm(
            headers=pending["headers"],
            initial={
                "month_column": getattr(settings_row, "month_column", ""),
                "income_column": getattr(settings_row, "income_column", ""),
                "spending_column": getattr(settings_row, "spending_column", ""),
                "spending_sign": getattr(
                    settings_row,
                    "spending_sign",
                    "unsigned",
                ),
            },
        )
    if request.method == "POST":
        return _handle_post(request, person, pending, mapping_form)
    report = comparison_rows(person, **_filter_kwargs(filter_form))
    tolerance_initial = Decimal(report.tolerance_minor) / Decimal(100)
    return render(
        request,
        "finance/sheet_comparison.html",
        {
            "filter_form": filter_form,
            "upload_form": upload_form,
            "mapping_form": mapping_form,
            "pending": pending,
            "tolerance_form": SheetToleranceForm(initial={"tolerance": tolerance_initial}),
            "report": report,
            "default_tolerance": Decimal(DEFAULT_TOLERANCE_MINOR) / Decimal(100),
        },
    )


def _handle_post(request, person, pending, mapping_form):
    action = request.POST.get("action")
    try:
        if action == "upload":
            upload_form = SheetCsvUploadForm(request.POST, request.FILES)
            if not upload_form.is_valid():
                return _rerender(request, person, upload_form=upload_form, mapping_form=mapping_form, pending=pending)
            uploaded = upload_form.cleaned_data["csv_file"]
            token = None
            try:
                token, raw = create_stage(
                    request,
                    None,
                    uploaded,
                    kind=KIND_SHEET_COMPARISON,
                )
                headers, rows = read_sheet_csv(raw)
            except CsvInputError as exc:
                if token:
                    delete_stage(request, token)
                raise SheetCsvError("Choose a CSV file of at most 5 MB.") from exc
            except (SheetCsvError, StageUnavailable):
                if token:
                    delete_stage(request, token)
                raise
            _commit_rows(request, person, headers, rows, uploaded.name, token)
            return redirect("sheet-comparison")
        if action == "map":
            if pending is None:
                raise SheetCsvError("Upload a CSV to map its columns.")
            mapping_form = SheetColumnMappingForm(request.POST, headers=pending["headers"])
            if not mapping_form.is_valid():
                return _rerender(request, person, mapping_form=mapping_form, pending=pending)
            try:
                headers, rows = _staged_rows(request, pending)
            except StageUnavailable as exc:
                raise SheetCsvError("Upload a CSV to map its columns.") from exc
            _commit_rows(
                request,
                person,
                headers,
                rows,
                pending["source"],
                pending["token"],
                mapping_data=mapping_form.cleaned_data,
            )
            return redirect("sheet-comparison")
        if action == "tolerance":
            form = SheetToleranceForm(request.POST)
            if not form.is_valid():
                return _rerender(request, person, tolerance_form=form, mapping_form=mapping_form, pending=pending)
            set_tolerance(person, form.tolerance_minor())
            messages.success(request, "Saved the match tolerance.")
            return redirect("sheet-comparison")
        if action == "note":
            form = SheetMonthNoteForm(request.POST)
            if not form.is_valid():
                raise SheetCsvError("Choose a month to annotate.")
            save_month_note(person, form.cleaned_data["month"], form.cleaned_data.get("note") or "")
            messages.success(request, "Saved the month note.")
            return redirect("sheet-comparison")
        if action == "cancel-map":
            _clear_pending(request)
            return redirect("sheet-comparison")
    except SheetCsvError as exc:
        messages.error(request, exc.messages[0] if getattr(exc, "messages", None) else str(exc))
        return redirect("sheet-comparison")
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if getattr(exc, "messages", None) else str(exc))
        return redirect("sheet-comparison")
    except PermissionDenied:
        messages.error(request, "That comparison data is not available.")
        return redirect("sheet-comparison")
    messages.error(request, "Choose a comparison action.")
    return redirect("sheet-comparison")


@require_POST
@never_cache
def sheet_comparison_delete(request):
    person = _person(request)
    delete_comparison_data(person)
    _clear_pending(request)
    messages.success(request, "Deleted this member's sheet comparison data.")
    return redirect("sheet-comparison")


def _rerender(request, person, *, upload_form=None, mapping_form=None, tolerance_form=None, pending=None):
    filter_form = SheetComparisonFilterForm(request.GET or None, principal=person)
    report = comparison_rows(person, **_filter_kwargs(filter_form))
    if tolerance_form is None:
        tolerance_form = SheetToleranceForm(initial={"tolerance": Decimal(report.tolerance_minor) / Decimal(100)})
    return render(
        request,
        "finance/sheet_comparison.html",
        {
            "filter_form": filter_form,
            "upload_form": upload_form or SheetCsvUploadForm(),
            "mapping_form": mapping_form,
            "pending": pending,
            "tolerance_form": tolerance_form,
            "report": report,
            "default_tolerance": Decimal(DEFAULT_TOLERANCE_MINOR) / Decimal(100),
        },
        status=400,
    )
