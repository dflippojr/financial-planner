from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from finance.models import Account

from .forms import CsvMappingForm, CsvUploadForm
from .parser import CsvInputError, preview_csv, read_csv
from .staging import StageUnavailable, create_stage, delete_stage, load_stage


def _visible_account(request, account_id):
    return get_object_or_404(Account.objects.visible_to(request.user), pk=account_id)


@never_cache
@require_http_methods(["GET", "POST"])
def csv_preview(request, account_id):
    account = _visible_account(request, account_id)
    context = {"account": account, "upload_form": CsvUploadForm()}
    if request.method == "GET":
        return render(request, "finance/csv_import/preview.html", context)

    action = request.POST.get("action", "upload")
    if action == "cancel":
        delete_stage(request, request.POST.get("token", ""))
        return redirect("home")
    if action == "upload":
        upload_form = CsvUploadForm(request.POST, request.FILES)
        context["upload_form"] = upload_form
        if upload_form.is_valid():
            token = None
            try:
                token, content = create_stage(request, account.pk, upload_form.cleaned_data["csv_file"])
                document = read_csv(content)
            except CsvInputError as exc:
                if token:
                    delete_stage(request, token)
                upload_form.add_error("csv_file", str(exc))
            else:
                context["mapping_form"] = CsvMappingForm(
                    headers=document.headers,
                    initial={"token": token, "amount_mode": "signed"},
                )
                context["headers"] = document.headers
        return render(request, "finance/csv_import/preview.html", context)

    token = request.POST.get("token", "")
    try:
        document = read_csv(load_stage(request, token, account.pk))
    except (CsvInputError, StageUnavailable):
        # Missing, expired, cross-user, and cross-account stages are deliberately
        # indistinguishable and never expose metadata about the staged upload.
        raise Http404 from None
    mapping_form = CsvMappingForm(request.POST, headers=document.headers)
    context.update({"mapping_form": mapping_form, "headers": document.headers})
    if mapping_form.is_valid():
        context["preview"] = preview_csv(document, mapping_form.mapping())
        # Guard the out-of-scope boundary: previewing must never persist imports.
        context["commit_available"] = False
    return render(request, "finance/csv_import/preview.html", context)
