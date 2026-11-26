"""Tags, categories and the transfer review."""


from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
    RECENTLY_UNDONE_DAYS,
    RECENTLY_UNDONE_LIMIT,
    confirm_transfer_pair,
    dismiss_transfer_pair,
    recently_undone_pairs,
    refresh_transfer_pairs,
    restore_transfer_pair,
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



_TRANSFER_ACTIONS = {
    "confirm": confirm_transfer_pair,
    "dismiss": dismiss_transfer_pair,
    "undo": undo_transfer_pair,
    "restore": restore_transfer_pair,
}


def _kept_undone_ids(request):
    """Pair ids the page keeps in place as Undone, from `?undone=1,2` (#635).

    Only ids: the page shows a kept pair only if the viewer can see it and a
    member undid it, so a forged id shows nothing. Garbage is ignored.
    """
    kept = []
    for part in request.GET.get("undone", "").split(","):
        if part.strip().isdigit() and int(part) not in kept:
            kept.append(int(part))
    return kept[-RECENTLY_UNDONE_LIMIT:]


def _transfer_review_url(kept):
    url = reverse("transfer-review")
    return f"{url}?{urlencode({'undone': ','.join(map(str, kept))})}" if kept else url


def _kept_after(action, pair_id, kept):
    # An undone pair stays where it was until the page is left; restoring it
    # puts it back to an ordinary excluded row in the same place.
    if action == "undo":
        return [*kept, pair_id][-RECENTLY_UNDONE_LIMIT:]
    if action == "restore":
        return [item for item in kept if item != pair_id]
    return kept


@require_http_methods(["GET", "POST"])
@never_cache
def transfer_review(request):
    kept = _kept_undone_ids(request)
    if request.method == "POST":
        try:
            pair_id = int(request.POST.get("pair_id", "0"))
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
        action = request.POST.get("action")
        handler = _TRANSFER_ACTIONS.get(action or "")
        if handler is None:
            raise Http404
        pair = service_or_404(lambda: handler(request.user, pair_id))
        service_or_404(lambda: refresh_transfer_pairs(
            request.user, transaction_ids=[pair.leg_a_id, pair.leg_b_id]  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute, as in models.py
        ))
        return redirect(_transfer_review_url(_kept_after(action, pair_id, kept)))
    visible = visible_rows(TransferPair, request.user).select_related(
        "leg_a",
        "leg_b",
        "leg_a__account",
        "leg_b__account",
    )
    marked = Q(status__in=(TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED))
    kept_in_place = Q(pk__in=kept, status=TransferPair.Status.UNDONE, undone_at__isnull=False)
    # Ordered by a key no action changes, so a row never moves under the cursor (#635).
    exclusions = list(visible.filter(marked | kept_in_place).order_by("-created_at", "-pk"))
    suggestions = visible.filter(status=TransferPair.Status.SUGGESTED).order_by("-created_at", "-pk")
    return render(
        request,
        "finance/transfer_review.html",
        {
            "exclusions": exclusions,
            "excluded_count": sum(1 for pair in exclusions if pair.is_active_exclusion),
            "suggestions": suggestions,
            "recently_undone": list(recently_undone_pairs(request.user)),
            "recently_undone_days": RECENTLY_UNDONE_DAYS,
        },
    )
