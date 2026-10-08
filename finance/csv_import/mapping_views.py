from dataclasses import replace

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from finance.access import request_person as _person
from finance.category_services import current_household
from finance.models import Account, SavedCsvMapping

from .forms import SavedCsvMappingEditForm
from .saved_mappings import (
    LOCKED_PARSING_MESSAGE,
    delete_or_archive_csv_mapping,
    mapping_from_saved,
    mapping_has_batches,
    update_csv_mapping,
    visible_saved_mapping,
)

LIST_TEMPLATE = "finance/csv_import/mapping_list.html"
EDIT_TEMPLATE = "finance/csv_import/mapping_edit.html"


def _account_choices(request):
    return [
        (str(account.pk), account.name)
        for account in Account.objects.visible_to(request.user)
        .filter(status=Account.Status.ACTIVE, archived_at__isnull=True)
        .order_by("name", "pk")
    ]


def _edit_initial(saved, request):
    default_ids = [
        str(pk)
        for pk in Account.objects.visible_to(request.user)
        .filter(default_saved_csv_mapping=saved)
        .values_list("pk", flat=True)
    ]
    return {
        "name": saved.name,
        "date_column": saved.date_column,
        "description_column": saved.description_column,
        "date_format": saved.date_format,
        "number_format": saved.number_format,
        "amount_mode": saved.amount_mode,
        "amount_column": saved.amount_column,
        "debit_column": saved.debit_column,
        "credit_column": saved.credit_column,
        "currency_column": saved.currency_column,
        "invert_sign": saved.invert_sign,
        "default_accounts": default_ids,
    }


@never_cache
@require_http_methods(["GET", "POST"])
def csv_mapping_list(request):
    person = _person(request)
    household = current_household(person)
    if household is None:
        return render(request, LIST_TEMPLATE, {"household": None, "mappings": []})
    if request.method == "POST" and request.POST.get("action") == "delete":
        try:
            mapping_id = int(request.POST.get("mapping_id", "0"))
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
        try:
            delete_or_archive_csv_mapping(request.user, mapping_id)
        except PermissionDenied as exc:
            raise Http404 from exc
        return redirect("csv-mapping-list")
    mappings = SavedCsvMapping.objects.visible_to(request.user).order_by("status", "name", "pk")
    used_ids = set(
        SavedCsvMapping.objects.visible_to(request.user)
        .filter(import_batches__isnull=False)
        .values_list("pk", flat=True)
    )
    return render(
        request,
        LIST_TEMPLATE,
        {"household": household, "mappings": mappings, "used_ids": used_ids},
    )


@never_cache
@require_http_methods(["GET", "POST"])
def csv_mapping_edit(request, mapping_id):
    saved = visible_saved_mapping(request.user, mapping_id)
    if saved is None:
        raise Http404
    locked = saved.locked_at is not None
    account_choices = _account_choices(request)
    form = SavedCsvMappingEditForm(
        request.POST or None,
        headers=saved.headers,
        account_choices=account_choices,
        locked=locked,
        initial=_edit_initial(saved, request),
    )
    if request.method == "POST" and form.is_valid():
        mapping = None
        if not locked:
            form_mapping = form.mapping()
            mapping = replace(
                mapping_from_saved(saved),
                date_column=form_mapping.date_column,
                description_column=form_mapping.description_column,
                date_format=form_mapping.date_format,
                number_format=form_mapping.number_format,
                amount_mode=form_mapping.amount_mode,
                amount_column=form_mapping.amount_column,
                debit_column=form_mapping.debit_column,
                credit_column=form_mapping.credit_column,
                currency_column=form_mapping.currency_column,
                invert_sign=form_mapping.invert_sign,
            )
        default_ids = [int(pk) for pk in form.cleaned_data.get("default_accounts", [])]
        try:
            update_csv_mapping(
                request.user,
                saved.pk,
                name=form.cleaned_data["name"],
                mapping=mapping,
                default_account_ids=default_ids,
            )
        except PermissionDenied as exc:
            raise Http404 from exc
        except ValidationError as exc:
            form.add_error(None, exc.messages[0] if exc.messages else LOCKED_PARSING_MESSAGE)
        else:
            return redirect("csv-mapping-list")
    return render(
        request,
        EDIT_TEMPLATE,
        {
            "saved_mapping": saved,
            "form": form,
            "locked": locked,
            "used": mapping_has_batches(saved),
            "parser_mapping": mapping_from_saved(saved),
        },
    )
