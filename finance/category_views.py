"""Tags, categories and the transfer review."""


from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
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


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _microseconds(moment):
    return (moment - _EPOCH) // _MICROSECOND


def _visit_since(request):
    """When this visit to the Transfers page took its first action, from `?since=<epoch microseconds>` (#635).

    Every pair undone at or after it keeps its row, so nothing moves under the
    cursor, however many pairs the member undoes. It only widens which of the
    pairs the viewer can already see are listed, so a forged value shows nothing
    new; garbage is ignored and the value is kept within the Recently undone window.
    """
    raw = request.GET.get("since", "")
    # ASCII digits only ("\u00b2" is a digit to str.isdigit), and short enough to be a date.
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
        return None
    now = timezone.now()
    earliest = now - timedelta(days=RECENTLY_UNDONE_DAYS)
    # Clamped as integers, so a huge value never reaches datetime and overflows.
    micros = min(max(int(raw), _microseconds(earliest)), _microseconds(now))
    return _EPOCH + micros * _MICROSECOND


def _transfer_review_url(since):
    # Exact to the microsecond, as undone_at is stored: rounding down would pull
    # in a pair undone earlier in the same second, before this visit (#820 review r3).
    return f"{reverse('transfer-review')}?{urlencode({'since': _microseconds(since)})}"


@require_http_methods(["GET", "POST"])
@never_cache
def transfer_review(request):
    since = _visit_since(request)
    if request.method == "POST":
        since = since or timezone.now()
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
        return redirect(_transfer_review_url(since))
    visible = visible_rows(TransferPair, request.user).select_related(
        "leg_a",
        "leg_b",
        "leg_a__account",
        "leg_b__account",
    )
    listed = Q(status__in=(TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED))
    if since is not None:
        # Undone during this visit: the row stays where it was, with Restore (#635).
        listed |= Q(status=TransferPair.Status.UNDONE, undone_at__gte=since)
    # Ordered by a key no action changes, so a row never moves under the cursor (#635).
    exclusions = list(visible.filter(listed).order_by("-created_at", "-pk"))
    # A suggestion on a leg of a pair kept in place came from that Undo's refresh.
    # Listing it above would push the Restore button down under the cursor, and
    # Restore deletes it anyway, so it waits for the next visit to the page.
    kept_legs = {leg for pair in exclusions if not pair.is_active_exclusion for leg in (pair.leg_a_id, pair.leg_b_id)}  # pyright: ignore[reportAttributeAccessIssue] - runtime FK id attribute
    suggestions = list(
        visible.filter(status=TransferPair.Status.SUGGESTED)
        .exclude(Q(leg_a_id__in=kept_legs) | Q(leg_b_id__in=kept_legs))
        .order_by("-created_at", "-pk")
    )
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
