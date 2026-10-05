from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .forms import ManualTransactionForm
from .manual_entry_services import add_manual_transaction, delete_manual_transaction


def _service_or_404(action):
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc


def _save_manual_entry(request, form):
    """Save a valid form. Returns False and adds a form error if the save fails."""
    data = form.cleaned_data
    category = data.get("category")
    try:
        _service_or_404(
            lambda: add_manual_transaction(
                request.user,
                data["account"].pk,
                transaction_date=data["transaction_date"],
                amount_minor=form.amount_minor(),
                description=data["description"],
                category_id=category.pk if category else None,
                note=data.get("note") or "",
                tag_ids=[tag.pk for tag in data.get("tags") or ()],
            )
        )
    except ValidationError as exc:
        form.add_error(None, exc.messages[0] if exc.messages else "The transaction could not be saved.")
        return False
    return True


@require_http_methods(["GET", "POST"])
@never_cache
def manual_transaction_add(request):
    if request.method != "POST":
        form = ManualTransactionForm(principal=request.user)
    else:
        form = ManualTransactionForm(request.POST, principal=request.user)
        if form.is_valid() and _save_manual_entry(request, form):
            messages.success(request, "Transaction added.")
            return redirect("transaction-add" if "add_another" in request.POST else "transaction-list")
    return render(
        request,
        "finance/transaction_add.html",
        {"form": form, "has_accounts": form.fields["account"].queryset.exists()},
    )


@require_POST
@never_cache
def manual_transaction_delete(request, transaction_id):
    _service_or_404(lambda: delete_manual_transaction(request.user, transaction_id))
    messages.success(request, "Manual transaction deleted.")
    return redirect("transaction-list")
