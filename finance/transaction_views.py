"""Transaction list, saved filters, edits, categories, splits and refund links."""

from types import SimpleNamespace
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction as database_transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .access import first_message as _first_message
from .access import service_or_404 as _service_or_404
from .access import visible_rows
from .post_only import post_only
from .transaction_services import correct_transaction
from .forms import (
    ReceiptUploadForm,
    RefundLinkForm,
    RefundSearchForm,
    SplitPartCategoryForm,
    SplitTransactionForm,
    TransactionCategoryForm,
    TransactionCorrectionForm,
    TransactionFilterForm,
    TransactionNoteTagsForm,
    UnsplitTransactionForm,
    SavedTransactionFilterNameForm,
)
from .lifecycle_services import (
    lock_actor_household,
)
from .models import (
    Account,
    Person,
    Receipt,
    RefundLink,
    RefundLinkDrop,
    SavedTransactionFilter,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
)
from .cash_flow import (
    format_minor,
    selected_accounts,
)
from .tag_services import (
    set_transaction_note_and_tags,
)
from .transaction_filters import (
    apply_saved_filter_url,
    apply_transaction_filters,
    delete_saved_transaction_filter,
    filter_hidden_pairs,
    paginate_transactions,
    query_params_from_cleaned,
    save_transaction_filter,
    visible_transaction_queryset,
)
from .category_services import assign_category
from .ledger_counting import exclusion_exists_for, income_and_spending_totals, with_transfer_state
from .refund_links import link_refund, REFUND_LINK_STANDS
from .splits import assign_split_part_category, split_transaction, unsplit_transaction
from .transfer_pairing import (
    refresh_transfer_pairs,
    transfer_matching_key,
)
from .rule_services import manual_rule_offer
from .untracked_transfers import mark_refusal, mark_untracked_transfer, unmark_untracked_transfer


def _query_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()



@require_GET
@never_cache
def transaction_list(request):
    from .ai_services import member_has_ai
    from .category_suggestion_services import (
        pending_suggestions_for,
        proposed_rule_from_accepts,
        suggestions_pending,
    )

    person = Person.objects.filter(user=request.user).first()
    transactions = visible_transaction_queryset(request.user)
    form = TransactionFilterForm(request.GET or None, principal=request.user)
    list_totals = None
    if form.is_valid():
        transactions = apply_transaction_filters(transactions, form.cleaned_data, request.user)
        list_totals = income_and_spending_totals(
            request.user,
            date_from=form.cleaned_data.get("date_from"),
            date_to=form.cleaned_data.get("date_to"),
            accounts=selected_accounts(
                request.user,
                account=form.cleaned_data.get("account"),
                scope=form.cleaned_data.get("scope") or "",
            ),
            tag=form.cleaned_data.get("tag"),
        )
        list_totals = SimpleNamespace(
            income_minor=list_totals.income_minor,
            spending_minor=list_totals.spending_minor,
            net_minor=list_totals.net_minor,
            income_display=format_minor(list_totals.income_minor),
            spending_display=format_minor(list_totals.spending_minor),
            net_display=format_minor(list_totals.net_minor),
        )
    elif form.is_bound:
        transactions = transactions.none()
    matching_count = transactions.count()
    page = paginate_transactions(transactions, request.GET.get("page"))
    show_ai = bool(person and member_has_ai(person))
    uncategorized_filter = form.is_valid() and form.cleaned_data.get("category") == "uncategorized"
    suggestions = pending_suggestions_for(person, page.object_list) if show_ai else {}
    filter_hidden = filter_hidden_pairs(form.cleaned_data) if form.is_valid() else []
    more_filter_count = sum(
        1
        for name, _value in filter_hidden
        if name not in TransactionFilterForm.TOOLBAR_FIELD_NAMES and name != "amount_mode"
    )
    if filter_hidden:
        verb = "matches" if matching_count == 1 else "match"
        list_helper = f"{matching_count:,} {verb} these filters"
    else:
        list_helper = f"{matching_count:,} transaction{'' if matching_count == 1 else 's'}"
    saved_filters = (
        visible_rows(SavedTransactionFilter, request.user).order_by("name", "pk")
        if person
        else SavedTransactionFilter.objects.none()
    )
    from .bulk_edit_services import BULK_EDIT_CAP
    from .bulk_edit_views import active_bulk_undo
    from .forms import BulkTransactionEditForm

    return render(
        request,
        "finance/transaction_list.html",
        {
            "filter_form": form,
            "save_filter_form": SavedTransactionFilterNameForm(),
            "saved_filters": saved_filters,
            "transactions": page.object_list,
            "page": page,
            "list_totals": list_totals,
            "show_ai_suggestions": show_ai,
            "uncategorized_filter": uncategorized_filter,
            "suggestions_pending": show_ai and suggestions_pending(person),
            "shown_suggestions": list(suggestions.values()),
            "suggestions": suggestions,
            "proposed_rule": proposed_rule_from_accepts(person) if show_ai else None,
            "list_query": request.get_full_path(),
            "filter_hidden": filter_hidden,
            "more_filter_count": more_filter_count,
            "more_filters_have_errors": any(field.errors for field in form.more_fields()),
            "list_helper": f"{list_helper} · amounts in USD",
            "export_filters_valid": not form.is_bound or form.is_valid(),
            "filter_query": _query_without_page(request),
            "matching_count": matching_count,
            "matching_over_cap": matching_count > BULK_EDIT_CAP,
            "bulk_select_cap": BULK_EDIT_CAP,
            "bulk_form": BulkTransactionEditForm(principal=request.user, auto_id="bulk_%s"),
            "bulk_undo": active_bulk_undo(request),
        },
    )



@post_only("transaction-list")
@never_cache
def transaction_saved_filter_create(request):
    form = TransactionFilterForm(request.POST or None, principal=request.user)
    name_form = SavedTransactionFilterNameForm(request.POST)
    if not name_form.is_valid():
        return redirect("transaction-list")
    query = query_params_from_cleaned(form.cleaned_data) if form.is_valid() else {}
    try:
        save_transaction_filter(request.user, name=name_form.cleaned_data["name"], query=query)
    except PermissionDenied as exc:
        raise Http404 from exc
    except ValidationError:
        params = urlencode(query)
        path = reverse("transaction-list")
        return redirect(f"{path}?{params}" if params else path)
    params = urlencode(query)
    path = reverse("transaction-list")
    return redirect(f"{path}?{params}" if params else path)



@post_only("transaction-list")
@never_cache
def transaction_saved_filter_delete(request, filter_id):
    _service_or_404(lambda: delete_saved_transaction_filter(request.user, filter_id))
    return redirect("transaction-list")



@require_GET
@never_cache
def transaction_saved_filter_apply(request, filter_id):
    url = _service_or_404(lambda: apply_saved_filter_url(request.user, filter_id))
    return redirect(url)



def visible_active_transaction(principal, transaction_id):
    return get_object_or_404(
        visible_rows(Transaction, principal)
        .filter(status=Transaction.Status.ACTIVE)
        .annotate(_excluded=exclusion_exists_for(principal))
        .select_related("account", "import_batch"),
        pk=transaction_id,
    )



@require_http_methods(["GET", "POST"])
@never_cache
def transaction_edit(request, transaction_id):
    financial_transaction = visible_active_transaction(request.user, transaction_id)
    if request.method == "POST":
        form = TransactionCorrectionForm(request.POST)
        if form.is_valid():
            with database_transaction.atomic():
                # Same lock order as the lifecycle services: the household's
                # memberships, then the account, then the transaction. The
                # membership lock is what stops a concurrent removal from
                # committing between the visibility check and the save.
                # History rows are inserted after the transaction lock, in
                # this same database transaction.
                person = get_object_or_404(Person, user=request.user)
                lock_actor_household(person)
                account = get_object_or_404(
                    visible_rows(Account, request.user).select_for_update(),
                    pk=financial_transaction.account_id,
                )
                financial_transaction = get_object_or_404(
                    Transaction.objects.select_for_update(),
                    pk=financial_transaction.pk,
                    account=account,
                    status=Transaction.Status.ACTIVE,
                )
                previous_key = transfer_matching_key(financial_transaction)
                try:
                    correct_transaction(person, financial_transaction, form.cleaned_data)
                except ValidationError as exc:
                    form.add_error("amount", _first_message(exc, "The amount could not be saved."))
                    return render_transaction_edit(request, financial_transaction, form=form)
            _service_or_404(lambda: refresh_transfer_pairs(
                request.user, transaction_ids=[financial_transaction.pk], previous_keys=[previous_key]
            ))
            return redirect("transaction-list")
    else:
        form = TransactionCorrectionForm.for_transaction(financial_transaction)
    return render_transaction_edit(request, financial_transaction, form=form)



def render_transaction_edit(
    request, financial_transaction, *, form=None, refund_form=None, split_form=None, note_form=None, receipt_form=None
):
    if getattr(financial_transaction, "_excluded", None) is None:
        # A locked reload after a failed POST drops the queryset annotation.
        with_transfer_state(request.user, financial_transaction)
    if form is None:
        form = TransactionCorrectionForm.for_transaction(financial_transaction)
    if note_form is None:
        note_form = TransactionNoteTagsForm.for_transaction(financial_transaction, request.user)
    if receipt_form is None:
        receipt_form = ReceiptUploadForm()
    refund_link = (
        visible_rows(RefundLink, request.user).select_related("original")
        .filter(refund=financial_transaction).first()
    )
    can_link_refund = (
        financial_transaction.amount_minor > 0 and refund_link is None
        and not financial_transaction.is_excluded_transfer
        # A visible refund may still have a link whose original is now private.
        # Check only this transaction's link existence; never expose that purchase.
        and not RefundLink.objects.filter(refund=financial_transaction).exists()
    )
    # A link dropped for this member alone still stands for another (#570, option c).
    refund_link_stands = refund_link is None and RefundLinkDrop.objects.filter(
        link__refund=financial_transaction, person__user=request.user
    ).exists()
    refund_search_form = None
    refund_results = []
    selected_original = None
    if can_link_refund:
        refund_search_form = RefundSearchForm(request.GET)
        if "refund_search" in request.GET:
            refund_results = refund_search_form.candidates(request.user, financial_transaction)
        if refund_form is None:
            refund_form = RefundLinkForm(
                principal=request.user, refund=financial_transaction,
                initial={"original": request.GET.get("original")},
            )
        selected_original = refund_form.fields["original"].queryset.select_related("account").first()  # pyright: ignore[reportAttributeAccessIssue] - a ModelChoiceField's queryset; pyright types form fields as the base Field
    correction_history = (
        visible_rows(TransactionCorrectionHistory, request.user)
        .filter(transaction=financial_transaction)
        .select_related("actor")
        .order_by("-recorded_at", "-pk")
    )
    splits = list(financial_transaction.splits.select_related("category").order_by("position"))
    part_forms = []
    for part in splits:
        part_forms.append(
            (
                part,
                SplitPartCategoryForm(
                    principal=request.user,
                    prefix=f"part{part.pk}",
                    initial={"category": part.category_id},
                ),
            )
        )
    return render(
        request,
        "finance/transaction_edit.html",
        {
            "form": form,
            "note_form": note_form,
            "category_form": TransactionCategoryForm(
                principal=request.user,
                initial={"category": financial_transaction.category_id},
            ),
            "refund_form": refund_form,
            "refund_link": refund_link,
            "can_link_refund": can_link_refund,
            "refund_link_stands": REFUND_LINK_STANDS if refund_link_stands else "",
            "refund_search_form": refund_search_form,
            "refund_results": refund_results,
            "selected_original": selected_original,
            "split_form": split_form
            or SplitTransactionForm(principal=request.user, transaction=financial_transaction),
            "unsplit_form": UnsplitTransactionForm(principal=request.user),
            "part_forms": part_forms,
            "is_split": financial_transaction.category_source == Transaction.CategorySource.SPLIT,
            "can_mark_untracked": (
                not financial_transaction.untracked_transfer and mark_refusal(financial_transaction) is None
            ),
            "rule_offer": manual_rule_offer(request.user, financial_transaction),
            "transaction": financial_transaction,
            "correction_history": correction_history,
            "receipt_form": receipt_form,
            "receipts": list(
                visible_rows(Receipt, request.user)
                .filter(transaction=financial_transaction)
                .order_by("pk")
            ),
        },
    )



@post_only("transaction-edit", "transaction_id")
@never_cache
def transaction_note_tags(request, transaction_id):
    financial_transaction = visible_active_transaction(request.user, transaction_id)
    form = TransactionNoteTagsForm(request.POST, principal=request.user)
    if form.is_valid():
        try:
            _service_or_404(
                lambda: set_transaction_note_and_tags(
                    request.user,
                    transaction_id,
                    note=form.cleaned_data.get("note") or "",
                    tag_ids=[tag.pk for tag in form.cleaned_data.get("tags") or []],
                    new_tag_name=form.cleaned_data.get("new_tag") or "",
                )
            )
        except ValidationError as exc:
            form.add_error(None, _first_message(exc, "The note and tags could not be saved."))
            return render_transaction_edit(request, financial_transaction, note_form=form)
        return redirect("transaction-edit", transaction_id=transaction_id)
    return render_transaction_edit(request, financial_transaction, note_form=form)



@post_only("transaction-edit", "transaction_id")
@never_cache
def transaction_categorize(request, transaction_id):
    visible_active_transaction(request.user, transaction_id)
    form = TransactionCategoryForm(request.POST, principal=request.user)
    if form.is_valid():
        category = form.cleaned_data["category"]
        try:
            _service_or_404(
                lambda: assign_category(request.user, transaction_id, None if category is None else category.pk)
            )
        except ValidationError:
            pass
    return redirect("transaction-edit", transaction_id=transaction_id)



@post_only("transaction-edit", "transaction_id")
@never_cache
def transaction_untracked_transfer(request, transaction_id):
    """Mark or unmark a row as a transfer to or from an account the planner doesn't track (#642)."""
    visible_active_transaction(request.user, transaction_id)
    action = request.POST.get("action")
    if action not in ("mark", "unmark"):
        messages.error(request, "Choose whether to mark or unmark the transfer.")
        return redirect("transaction-edit", transaction_id=transaction_id)
    marked = action == "mark"
    service = mark_untracked_transfer if marked else unmark_untracked_transfer
    try:
        _service_or_404(lambda: service(request.user, transaction_id))
    except ValidationError as exc:
        messages.error(request, _first_message(exc, "The transfer mark could not be changed."))
    else:
        messages.success(
            request,
            "Marked as a transfer to or from an account you don't track. It no longer counts as income or spending."
            if marked
            else "Transfer mark removed. The transaction counts as income or spending again.",
        )
    return redirect("transaction-edit", transaction_id=transaction_id)



@post_only("transaction-edit", "transaction_id")
@never_cache
def transaction_link_refund(request, transaction_id):
    financial_transaction = visible_active_transaction(request.user, transaction_id)
    form = RefundLinkForm(request.POST, principal=request.user, refund=financial_transaction)
    if form.is_valid():
        try:
            _service_or_404(
                lambda: link_refund(
                    request.user,
                    transaction_id,
                    form.cleaned_data["original"].pk,
                    None if form.cleaned_data.get("original_part") is None else form.cleaned_data["original_part"].pk,
                )
            )
        except ValidationError as exc:
            form.add_error(None, _first_message(exc, "The refund could not be linked."))
        else:
            return redirect("transaction-edit", transaction_id=transaction_id)
    return render_transaction_edit(request, financial_transaction, refund_form=form)



@post_only("transaction-edit", "transaction_id")
@never_cache
def transaction_split(request, transaction_id):
    financial_transaction = visible_active_transaction(request.user, transaction_id)
    form = SplitTransactionForm(request.POST, principal=request.user, transaction=financial_transaction)
    if form.is_valid():
        try:
            _service_or_404(
                lambda: split_transaction(
                    request.user,
                    transaction_id,
                    form.parts_payload(),
                    form.refund_assignments(),
                )
            )
        except ValidationError as exc:
            form.add_error(None, _first_message(exc, "The transaction could not be split."))
        else:
            return redirect("transaction-edit", transaction_id=transaction_id)
    return render_transaction_edit(request, financial_transaction, split_form=form)



@post_only("transaction-edit", "transaction_id")
@never_cache
def transaction_unsplit(request, transaction_id):
    visible_active_transaction(request.user, transaction_id)
    form = UnsplitTransactionForm(request.POST, principal=request.user)
    if form.is_valid():
        category = form.cleaned_data["category"]
        _service_or_404(
            lambda: unsplit_transaction(
                request.user,
                transaction_id,
                None if category is None else category.pk,
            )
        )
    return redirect("transaction-edit", transaction_id=transaction_id)



@post_only("transaction-edit", "transaction_id")
@never_cache
def transaction_split_part_category(request, transaction_id, part_id):
    financial_transaction = visible_active_transaction(request.user, transaction_id)
    part = get_object_or_404(TransactionSplit, pk=part_id, transaction=financial_transaction)
    form = SplitPartCategoryForm(request.POST, principal=request.user, prefix=f"part{part.pk}")
    if form.is_valid():
        _service_or_404(
            lambda: assign_split_part_category(request.user, part.pk, form.cleaned_data["category"].pk)
        )
    return redirect("transaction-edit", transaction_id=transaction_id)
