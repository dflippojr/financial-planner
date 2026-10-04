from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, QueryDict
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .bulk_edit_services import (
    BULK_EDIT_CAP,
    SKIP_LABELS,
    UNDO_UNAVAILABLE,
    apply_bulk_edit,
    preview_bulk_edit,
    undo_bulk_edit,
)
from .category_services import exclusion_exists_for
from .forms import BulkTransactionEditForm, TransactionFilterForm
from .models import BulkEditUndo, Person, Transaction


def _first_message(exc, fallback):
    items = getattr(exc, "messages", None)
    return items[0] if items else fallback


def _matching_transactions(request):
    from .views import _apply_transaction_filters

    transactions = (
        Transaction.objects.visible_to(request.user)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "import_batch", "category")
        .prefetch_related("splits__category", "tags")
        .annotate(_excluded=exclusion_exists_for(request.user))
        .order_by("-transaction_date", "-pk")
    )
    raw = request.POST.get("filter_query", "")
    form = TransactionFilterForm(QueryDict(raw) if raw else None, principal=request.user)
    if form.is_valid():
        return _apply_transaction_filters(transactions, form.cleaned_data, request.user)
    if form.is_bound:
        return transactions.none()
    return transactions


def _bulk_kwargs(form):
    category = form.cleaned_data.get("category")
    tags = form.cleaned_data.get("tags") or []
    return {
        "select_matching": bool(form.cleaned_data.get("select_matching")),
        "action": form.cleaned_data["action"],
        "category_id": None if category is None else category.pk,
        "tag_ids": [tag.pk for tag in tags],
        "note_line": form.cleaned_data.get("note_line") or "",
        "transaction_ids": form.data.getlist("transaction_id"),
    }


def _service_or_404(action):
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc


@require_POST
@never_cache
def bulk_edit_preview(request):
    form = BulkTransactionEditForm(request.POST, principal=request.user)
    matching = _matching_transactions(request)
    if not form.is_valid():
        messages.error(request, "Choose a bulk edit action and try again.")
        return redirect("transaction-list")
    kwargs = _bulk_kwargs(form)
    try:
        preview = preview_bulk_edit(request.user, matching=matching, **kwargs)
    except PermissionDenied as exc:
        raise Http404 from exc
    except ValidationError as exc:
        messages.error(request, _first_message(exc, "The bulk edit could not be previewed."))
        return redirect("transaction-list")
    skips = [
        {"reason": reason, "label": SKIP_LABELS.get(reason, reason), "count": count}
        for reason, count in preview.skip_counts.items()
    ]
    return render(
        request,
        "finance/transaction_bulk_preview.html",
        {
            "form": form,
            "preview": preview,
            "skips": skips,
            "cap": BULK_EDIT_CAP,
            "filter_query": request.POST.get("filter_query", ""),
            "transaction_ids": preview.selected_ids,
            "eligible_ids": preview.eligible_ids,
        },
    )


@require_POST
@never_cache
def bulk_edit_apply(request):
    form = BulkTransactionEditForm(request.POST, principal=request.user)
    matching = _matching_transactions(request)
    if not form.is_valid():
        messages.error(request, "Choose a bulk edit action and try again.")
        return redirect("transaction-list")
    kwargs = _bulk_kwargs(form)
    kwargs["select_matching"] = False
    posted_eligible = form.data.getlist("eligible_id")
    kwargs["expected_eligible_ids"] = posted_eligible or None
    try:
        preview, undo = _service_or_404(
            lambda: apply_bulk_edit(request.user, matching=matching, **kwargs)
        )
    except ValidationError as exc:
        messages.error(request, _first_message(exc, "The bulk edit could not be applied."))
        return redirect("transaction-list")
    request.session["bulk_edit_undo_id"] = str(undo.pk)
    count = len(preview.eligible_ids)
    messages.success(request, f"Updated {count} transaction{'s' if count != 1 else ''}.")
    return redirect("transaction-list")


@require_POST
@never_cache
def bulk_edit_undo(request, undo_id):
    try:
        _service_or_404(lambda: undo_bulk_edit(request.user, undo_id))
    except ValidationError as exc:
        messages.error(request, _first_message(exc, UNDO_UNAVAILABLE))
        return redirect("transaction-list")
    request.session.pop("bulk_edit_undo_id", None)
    messages.success(request, "Bulk edit undone.")
    return redirect("transaction-list")


def active_bulk_undo(request):
    person = Person.objects.filter(user=request.user).first()
    if person is None:
        return None
    undo_id = request.session.get("bulk_edit_undo_id")
    if not undo_id:
        return None
    return (
        BulkEditUndo.objects.visible_to(person)
        .filter(pk=undo_id, undone_at__isnull=True, expires_at__gt=timezone.now())
        .first()
    )
