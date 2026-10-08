from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .audit_services import record_download
from .forms import ReceiptUploadForm
from .models import Receipt
from .receipt_services import (
    attach_receipt,
    remove_receipt,
    stored_receipt_path,
    visible_receipt_or_none,
)
from .views import _render_transaction_edit, _visible_active_transaction


def _service_or_404(action):
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc


@require_POST
@never_cache
def receipt_upload(request, transaction_id):
    financial_transaction = _visible_active_transaction(request.user, transaction_id)
    form = ReceiptUploadForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            _service_or_404(lambda: attach_receipt(request.user, transaction_id, form.cleaned_data["receipt"]))
        except ValidationError as exc:
            form.add_error("receipt", exc.messages[0] if exc.messages else "That file could not be attached.")
            return _render_transaction_edit(request, financial_transaction, receipt_form=form)
        return redirect("transaction-edit", transaction_id=transaction_id)
    return _render_transaction_edit(request, financial_transaction, receipt_form=form)


@require_POST
@never_cache
def receipt_delete(request, transaction_id, receipt_id):
    _visible_active_transaction(request.user, transaction_id)
    _service_or_404(lambda: remove_receipt(request.user, transaction_id, receipt_id))
    return redirect("transaction-edit", transaction_id=transaction_id)


@require_GET
@never_cache
def receipt_download(request, transaction_id, receipt_id):
    receipt = visible_receipt_or_none(request.user, transaction_id, receipt_id)
    if receipt is None:
        raise Http404()
    try:
        path = stored_receipt_path(receipt.stored_name)
    except PermissionDenied as exc:
        raise Http404 from exc
    if not path.is_file():
        raise Http404()
    record_download(request.user, export_kind="receipt", receipt=receipt)
    response = FileResponse(
        path.open("rb"),
        content_type=receipt.content_type,
        as_attachment=receipt.content_type == Receipt.ContentType.PDF,
        filename=receipt.original_name,
    )
    response["X-Content-Type-Options"] = "nosniff"
    # Uploaded bytes are never allowed to run script or load anything in this origin.
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
    return response
