"""Tags, categories and the transfer review."""


from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access import first_message
from .access import service_or_404
from .access import visible_rows
from .forms import (
    CategoryNameForm,
    TagNameForm,
    TransferWindowForm,
)
from .models import (
    Category,
    Person,
    Tag,
    TransferPair,
)
from .tag_services import (
    add_tag,
    archive_tag,
    rename_tag,
)
from .category_services import add_category, ensure_household_categories, rename_category
from .households import current_household
from .transfer_pairing import (
    confirm_transfer_pair,
    dismiss_transfer_pair,
    refresh_transfer_pairs,
    set_transfer_window_days,
    undo_transfer_pair,
)


def _handle_add_category(request, forms):
    add_form = forms["add_form"]
    if not add_form.is_valid():
        return False
    try:
        service_or_404(lambda: add_category(request.user, add_form.cleaned_data["name"]))
    except ValidationError as exc:
        add_form.add_error("name", first_message(exc, "The category could not be saved."))
        return False
    return True



def _handle_transfer_window(request, forms):
    window_form = forms["window_form"]
    if not window_form.is_valid():
        return False
    days = window_form.cleaned_data["transfer_match_window_days"]
    try:
        service_or_404(lambda: set_transfer_window_days(request.user, days))
    except ValidationError as exc:
        window_form.add_error("transfer_match_window_days", first_message(exc, "The match window could not be saved."))
        return False
    return True



def _handle_rename_category(request, forms):
    rename_form = CategoryNameForm(request.POST)
    if not rename_form.is_valid():
        return False
    try:
        service_or_404(
            lambda: rename_category(
                request.user,
                int(request.POST.get("category_id", "0")),
                rename_form.cleaned_data["name"],
            )
        )
    except (ValidationError, ValueError) as exc:
        forms["rename_error"] = first_message(exc, "The category could not be renamed.")
        return False
    return True



_CATEGORY_ACTIONS = {
    "add": _handle_add_category,
    "window": _handle_transfer_window,
    "rename": _handle_rename_category,
}



def _handle_add_tag(request, forms):
    add_form = forms["add_form"]
    if not add_form.is_valid():
        return False
    try:
        service_or_404(lambda: add_tag(request.user, add_form.cleaned_data["name"]))
    except ValidationError as exc:
        add_form.add_error("name", first_message(exc, "The tag could not be added."))
        return False
    return True



def _handle_rename_tag(request, forms):
    rename_form = TagNameForm(request.POST)
    if not rename_form.is_valid():
        return False
    try:
        service_or_404(
            lambda: rename_tag(
                request.user,
                int(request.POST.get("tag_id", "0")),
                rename_form.cleaned_data["name"],
            )
        )
    except (ValidationError, ValueError) as exc:
        forms["rename_error"] = first_message(exc, "The tag could not be renamed.")
        return False
    return True



def _handle_archive_tag(request, forms):
    try:
        service_or_404(lambda: archive_tag(request.user, int(request.POST.get("tag_id", "0"))))
    except (ValidationError, ValueError) as exc:
        forms["rename_error"] = first_message(exc, "The tag could not be archived.")
        return False
    return True



_TAG_ACTIONS = {
    "add": _handle_add_tag,
    "rename": _handle_rename_tag,
    "archive": _handle_archive_tag,
}



@require_http_methods(["GET", "POST"])
@never_cache
def tag_list(request):
    person = get_object_or_404(Person, user=request.user)
    household = current_household(person)
    if household is None:
        return render(
            request,
            "finance/tag_list.html",
            {"household": None, "tags": [], "add_form": TagNameForm(), "rename_error": None},
        )
    action = request.POST.get("action") if request.method == "POST" else None
    add_form = TagNameForm(request.POST if action == "add" else None)
    forms = {"add_form": add_form, "rename_error": None}
    handler = _TAG_ACTIONS.get(action or "")
    if handler is not None and handler(request, forms):
        return redirect("tag-list")
    tags = visible_rows(Tag, request.user).order_by("is_archived", "name", "pk")
    return render(
        request,
        "finance/tag_list.html",
        {
            "household": household,
            "tags": tags,
            "add_form": add_form,
            "rename_error": forms["rename_error"],
        },
    )



@require_http_methods(["GET", "POST"])
@never_cache
def category_list(request):
    person = get_object_or_404(Person, user=request.user)
    household = current_household(person)
    if household is None:
        return render(
            request,
            "finance/category_list.html",
            {"household": None, "categories": [], "add_form": CategoryNameForm(), "window_form": None},
        )
    ensure_household_categories(household)
    action = request.POST.get("action") if request.method == "POST" else None
    add_form = CategoryNameForm(request.POST if action == "add" else None)
    window_form = TransferWindowForm(
        request.POST if action == "window" else None,
        initial={"transfer_match_window_days": household.transfer_match_window_days},
    )
    forms = {"add_form": add_form, "window_form": window_form, "rename_error": None}
    handler = _CATEGORY_ACTIONS.get(action or "")
    if handler is not None and handler(request, forms):
        return redirect("category-list")
    categories = visible_rows(Category, request.user).order_by("name", "pk")
    return render(
        request,
        "finance/category_list.html",
        {
            "household": household,
            "categories": categories,
            "add_form": add_form,
            "window_form": window_form,
            "rename_error": forms["rename_error"],
        },
    )



@require_http_methods(["GET", "POST"])
@never_cache
def transfer_review(request):
    if request.method == "POST":
        try:
            pair_id = int(request.POST.get("pair_id", "0"))
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
        action = request.POST.get("action")
        actions = {
            "confirm": confirm_transfer_pair,
            "dismiss": dismiss_transfer_pair,
            "undo": undo_transfer_pair,
        }
        handler = actions.get(action)
        if handler is None:
            raise Http404
        pair = service_or_404(lambda: handler(request.user, pair_id))
        service_or_404(lambda: refresh_transfer_pairs(
            request.user, transaction_ids=[pair.leg_a_id, pair.leg_b_id]  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute, as in models.py
        ))
        return redirect("transfer-review")
    visible = visible_rows(TransferPair, request.user).select_related(
        "leg_a",
        "leg_b",
        "leg_a__account",
        "leg_b__account",
    )
    exclusions = visible.filter(status__in=(TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED)).order_by(
        "-updated_at", "-pk"
    )
    suggestions = visible.filter(status=TransferPair.Status.SUGGESTED).order_by("-updated_at", "-pk")
    return render(
        request,
        "finance/transfer_review.html",
        {"exclusions": exclusions, "suggestions": suggestions},
    )
