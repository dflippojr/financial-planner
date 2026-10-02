from collections import namedtuple

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from finance.models import Account, ImportBatch

from .forms import AppleCardImportForm, CapitalOneImportForm, CsvMappingForm, CsvUploadForm, HuntingtonImportForm
from .parser import CsvInputError, preview_csv, read_csv
from .profiles import (
    APPLE_CARD,
    APPLE_CARD_MAPPING,
    CAPITAL_ONE,
    CAPITAL_ONE_MAPPING,
    HUNTINGTON,
    HUNTINGTON_MAPPING,
    require_apple_card_headers,
    require_capital_one_headers,
    require_huntington_headers,
)
from .services import categorize_imported_batch, classify_overlap, commit_csv_import, undo_import_batch
from .staging import StageUnavailable, create_stage, delete_stage, find_live_stage, load_stage, stage_profile


PREVIEW_TEMPLATE = "finance/csv_import/preview.html"
RESULT_SESSION_KEY = "csv_import_result"

FixedProfile = namedtuple("FixedProfile", ("require_headers", "mapping", "source", "form_class"))

FIXED_PROFILES = {
    HUNTINGTON: FixedProfile(require_huntington_headers, HUNTINGTON_MAPPING, ImportBatch.Source.HUNTINGTON, HuntingtonImportForm),
    CAPITAL_ONE: FixedProfile(require_capital_one_headers, CAPITAL_ONE_MAPPING, ImportBatch.Source.CAPITAL_ONE, CapitalOneImportForm),
    APPLE_CARD: FixedProfile(require_apple_card_headers, APPLE_CARD_MAPPING, ImportBatch.Source.APPLE_CARD, AppleCardImportForm),
}


def _visible_account(request, account_id):
    return get_object_or_404(
        Account.objects.visible_to(request.user).filter(
            status=Account.Status.ACTIVE,
            archived_at__isnull=True,
        ),
        pk=account_id,
    )


def _mapping_context(context, token, document, profile):
    context["import_profile"] = profile
    context["headers"] = document.headers
    context["mapping_form"] = CsvMappingForm(
        headers=document.headers,
        initial={"token": token, "amount_mode": "signed"},
    )
    return context


def _restore_live_stage(request, account, context):
    token = find_live_stage(request, account.pk)
    if not token:
        return
    try:
        document = read_csv(load_stage(request, token, account.pk))
    except (CsvInputError, StageUnavailable):
        return
    profile = stage_profile(request, token, account.pk)
    if profile in FIXED_PROFILES:
        try:
            _fixed_profile_preview(
                context,
                document,
                account,
                {"token": token, "source": FIXED_PROFILES[profile].source},
                profile,
            )
        except CsvInputError:
            return
        return
    _mapping_context(context, token, document, profile)


def _import_batches(request, account):
    return (
        ImportBatch.objects.visible_to(request.user)
        .filter(account=account, status=ImportBatch.Status.ACTIVE)
        .order_by("-imported_at", "-pk")
    )


def _render_preview(request, account, context):
    context.setdefault("upload_form", CsvUploadForm())
    context["account"] = account
    context["import_batches"] = _import_batches(request, account)
    if request.method == "GET":
        result = request.session.pop(RESULT_SESSION_KEY, None)
        if result and result.get("account_id") == account.pk:
            context["import_result"] = result
    return render(request, PREVIEW_TEMPLATE, context)


def _prefill_date_range(post_data, preview):
    dates = [row.transaction_date for row in preview.rows if row.is_valid]
    if not dates:
        return post_data
    data = post_data.copy()
    if not data.get("date_range_start"):
        data["date_range_start"] = min(dates).isoformat()
    if not data.get("date_range_end"):
        data["date_range_end"] = max(dates).isoformat()
    return data


def _store_result(request, account_id, *, new_count=0, duplicate_count=0, invalid_count=0, undone=False):
    request.session[RESULT_SESSION_KEY] = {
        "account_id": account_id,
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "invalid_count": invalid_count,
        "undone": undone,
    }


def _fixed_profile_preview(context, document, account, post_data, profile):
    spec = FIXED_PROFILES[profile]
    spec.require_headers(document.headers)
    preview = classify_overlap(account, preview_csv(document, spec.mapping))
    filled = _prefill_date_range(post_data, preview)
    mapping_form = spec.form_class(filled)
    mapping_form.is_valid()
    context.update(
        {
            "mapping_form": mapping_form,
            "headers": document.headers,
            "import_profile": profile,
            "preview": preview,
            "commit_available": True,
        }
    )
    return preview, mapping_form


def _require_import_range(mapping_form, *, source_required):
    source = mapping_form.cleaned_data.get("source")
    start = mapping_form.cleaned_data.get("date_range_start")
    end = mapping_form.cleaned_data.get("date_range_end")
    if source_required and not source:
        mapping_form.add_error("source", "Choose the source of this export.")
    if not start:
        mapping_form.add_error("date_range_start", "Choose the start of the import date range.")
    if not end:
        mapping_form.add_error("date_range_end", "Choose the end of the import date range.")
    elif start and end < start:
        mapping_form.add_error("date_range_end", "The date range must end on or after it starts.")
    return source, start, end


def _handle_upload(request, account, context):
    upload_form = CsvUploadForm(request.POST, request.FILES)
    context["upload_form"] = upload_form
    if not upload_form.is_valid():
        return _render_preview(request, account, context)
    token = None
    try:
        profile = upload_form.cleaned_data["import_profile"]
        token, content = create_stage(
            request,
            account.pk,
            upload_form.cleaned_data["csv_file"],
            import_profile=profile,
        )
        document = read_csv(content)
        if profile in FIXED_PROFILES:
            _fixed_profile_preview(
                context,
                document,
                account,
                {"token": token, "source": FIXED_PROFILES[profile].source},
                profile,
            )
        else:
            _mapping_context(context, token, document, profile)
    except CsvInputError as exc:
        if token:
            delete_stage(request, token)
        upload_form.add_error("csv_file", str(exc))
    return _render_preview(request, account, context)


def _prepare_staged_preview(request, account, document, profile, context):
    if profile in FIXED_PROFILES:
        spec = FIXED_PROFILES[profile]
        mapping_form = spec.form_class(request.POST)
        context.update(
            {"mapping_form": mapping_form, "headers": document.headers, "import_profile": profile}
        )
        if not mapping_form.is_valid():
            return None
        try:
            preview, mapping_form = _fixed_profile_preview(context, document, account, request.POST, profile)
        except CsvInputError:
            raise Http404 from None
        return preview, mapping_form, spec.mapping, False
    mapping_form = CsvMappingForm(request.POST, headers=document.headers)
    context.update({"mapping_form": mapping_form, "headers": document.headers, "import_profile": profile})
    if not mapping_form.is_valid():
        return None
    mapping = mapping_form.mapping()
    preview = classify_overlap(account, preview_csv(document, mapping))
    return preview, mapping_form, mapping, True


def _render_generic_preview(request, account, context, document, preview):
    filled = _prefill_date_range(request.POST, preview)
    mapping_form = CsvMappingForm(filled, headers=document.headers)
    mapping_form.is_valid()
    context["mapping_form"] = mapping_form
    context["preview"] = preview
    context["commit_available"] = True
    return _render_preview(request, account, context)


def _commit_staged_import(request, account, context, *, token, content, document, mapping, mapping_form, preview, source_required):
    source, start, end = _require_import_range(mapping_form, source_required=source_required)
    if mapping_form.errors:
        context["preview"] = preview
        context["commit_available"] = True
        return _render_preview(request, account, context)
    try:
        result = commit_csv_import(
            request.user,
            account.pk,
            content=content,
            document=document,
            mapping=mapping,
            source=source,
            date_range_start=start,
            date_range_end=end,
        )
    except PermissionDenied as exc:
        raise Http404 from exc
    except ValidationError as exc:
        mapping_form.add_error(None, exc.messages[0] if exc.messages else "The import could not be saved.")
        context["preview"] = preview
        context["commit_available"] = True
        return _render_preview(request, account, context)

    categorize_imported_batch(request.user, result.batch)

    delete_stage(request, token)
    _store_result(
        request,
        account.pk,
        new_count=result.new_count,
        duplicate_count=result.duplicate_count,
        invalid_count=result.invalid_count,
    )
    return redirect("csv-import-preview", account_id=account.pk)


@never_cache
@require_http_methods(["GET", "POST"])
def csv_preview(request, account_id):
    account = _visible_account(request, account_id)
    context = {}
    if request.method == "GET":
        _restore_live_stage(request, account, context)
        return _render_preview(request, account, context)

    action = request.POST.get("action", "upload")
    if action == "cancel":
        delete_stage(request, request.POST.get("token", ""))
        return redirect("home")
    if action == "upload":
        return _handle_upload(request, account, context)

    token = request.POST.get("token", "")
    try:
        content = load_stage(request, token, account.pk)
        document = read_csv(content)
        profile = stage_profile(request, token, account.pk)
    except (CsvInputError, StageUnavailable):
        # Missing, expired, cross-user, and cross-account stages are deliberately
        # indistinguishable and never expose metadata about the staged upload.
        raise Http404 from None

    prepared = _prepare_staged_preview(request, account, document, profile, context)
    if prepared is None:
        return _render_preview(request, account, context)
    preview, mapping_form, mapping, source_required = prepared

    if action == "preview":
        if profile not in FIXED_PROFILES:
            return _render_generic_preview(request, account, context, document, preview)
        return _render_preview(request, account, context)
    if action != "commit":
        return _render_preview(request, account, context)
    return _commit_staged_import(
        request,
        account,
        context,
        token=token,
        content=content,
        document=document,
        mapping=mapping,
        mapping_form=mapping_form,
        preview=preview,
        source_required=source_required,
    )


@never_cache
@require_POST
def csv_undo_import(request, account_id, batch_id):
    account = _visible_account(request, account_id)
    try:
        undo_import_batch(request.user, account.pk, batch_id)
    except PermissionDenied as exc:
        raise Http404 from exc
    _store_result(request, account.pk, undone=True)
    return redirect("csv-import-preview", account_id=account.pk)
