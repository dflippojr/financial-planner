"""Recurring series: create and review."""


from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access import service_or_404 as _service_or_404
from .access import visible_rows
from .models import (
    Person,
    RecurringSeries,
)
from .recurring_services import (
    ManualSeriesError,
    add_recurring_members,
    create_manual_series,
    list_manual_series_candidates,
    preview_manual_series,
    cancel_recurring_series,
    confirm_resume_recurring_series,
    confirm_recurring_series,
    confirmed_totals_once_per_bill,
    dismiss_price_change,
    dismiss_recurring_series,
    keep_cancelled_recurring_series,
    list_addable_transactions,
    merge_recurring_series,
    refresh_recurring_series,
    remove_recurring_member,
    household_review,
    undo_cancel_recurring_series,
)
from .recurring_review import build_recurring_review, price_change_percent_for
from .cash_flow import (
    format_minor,
)


def _money_display(minor, currency):
    return format_minor(minor, currency)



def _recurring_chart_data(confirmed, monthly_minor, annual_minor, monthly_display, annual_display):
    return {
        "monthly_minor": monthly_minor,
        "annual_minor": annual_minor,
        "monthly_display": monthly_display,
        "annual_display": annual_display,
        "series": [
            {
                "name": series.display_name,
                "monthly_minor": series.monthly_minor,
                "annual_minor": series.annual_minor,
                "monthly_display": series.monthly_display,
                "annual_display": series.annual_display,
            }
            for series in confirmed
        ],
    }



def _handle_recurring_post(request):
    try:
        series_id = int(request.POST.get("series_id", "0"))
    except (TypeError, ValueError) as exc:
        raise Http404 from exc
    action = request.POST.get("action")
    if action == "confirm":
        _service_or_404(lambda: confirm_recurring_series(request.user, series_id))
    elif action == "dismiss":
        _service_or_404(lambda: dismiss_recurring_series(request.user, series_id))
    elif action == "merge":
        try:
            target_id = int(request.POST.get("target_id", "0"))
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
        _service_or_404(lambda: merge_recurring_series(request.user, series_id, target_id))
    elif action == "remove":
        try:
            transaction_id = int(request.POST.get("transaction_id", "0"))
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
        _service_or_404(lambda: remove_recurring_member(request.user, series_id, transaction_id))
    elif action == "add":
        transaction_ids = []
        for raw in request.POST.getlist("transaction_id"):
            try:
                transaction_ids.append(int(raw))
            except (TypeError, ValueError) as exc:
                raise Http404 from exc
        _service_or_404(lambda: add_recurring_members(request.user, series_id, transaction_ids))
    elif action == "cancel":
        _service_or_404(lambda: cancel_recurring_series(request.user, series_id))
    elif action == "undo_cancel":
        _service_or_404(lambda: undo_cancel_recurring_series(request.user, series_id))
    elif action == "confirm_resume":
        _service_or_404(lambda: confirm_resume_recurring_series(request.user, series_id))
    elif action == "keep_cancelled":
        _service_or_404(lambda: keep_cancelled_recurring_series(request.user, series_id))
    elif action == "dismiss_price":
        _service_or_404(lambda: dismiss_price_change(request.user, series_id))
    else:
        raise Http404
    _service_or_404(lambda: refresh_recurring_series(request.user))
    return redirect("recurring-review")



def _posted_ids(request):
    ids = []
    for raw in request.POST.getlist("transaction_id"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
    return ids



@require_http_methods(["GET", "POST"])
@never_cache
def recurring_create(request):
    """Create a confirmed series by hand: pick charges, preview, then confirm."""
    context = {"cadences": RecurringSeries.Cadence.choices, "error": None, "preview": None}
    if request.method == "POST":
        step = request.POST.get("step")
        name = request.POST.get("name", "")
        cadence = request.POST.get("cadence", "")
        ids = _posted_ids(request)
        if step not in ("preview", "confirm"):
            raise Http404
        try:
            if step == "confirm":
                _service_or_404(lambda: create_manual_series(request.user, name, cadence, ids))
                return redirect("recurring-review")
            preview = _service_or_404(lambda: preview_manual_series(request.user, name, cadence, ids))
            preview["amount_display"] = _money_display(preview["typical_amount_minor"], preview["currency"])
            context["preview"] = preview
        except ManualSeriesError as exc:
            context["error"] = str(exc)
        context.update(name=name, cadence=cadence)
    if context["preview"] is None:
        try:
            page = int(request.GET.get("page", "1"))
        except (TypeError, ValueError):
            page = 1
        query = request.GET.get("q", "")
        candidates, has_next = _service_or_404(lambda: list_manual_series_candidates(request.user, query, page))
        context.update(
            candidates=candidates,
            query=query,
            page=max(page, 1),
            next_page=max(page, 1) + 1 if has_next else None,
            previous_page=page - 1 if page > 1 else None,
        )
        context.setdefault("name", "")
        context.setdefault("cadence", "")
    return render(request, "finance/recurring_create.html", context)



def _recurring_merge_selection(raw_merge, grouping_series):
    if raw_merge is None:
        return None, []
    try:
        series_id = int(raw_merge)
    except (TypeError, ValueError) as exc:
        raise Http404 from exc
    if not any(series.pk == series_id for series in grouping_series):
        raise Http404
    return series_id, [target for target in grouping_series if target.pk != series_id]



@require_http_methods(["GET", "POST"])
@never_cache
def recurring_review(request):
    if request.method == "POST":
        return _handle_recurring_post(request)
    _service_or_404(lambda: refresh_recurring_series(request.user, only_if_changed=True))
    own = list(
        visible_rows(RecurringSeries, request.user)
        .prefetch_related("members__transaction__account")
        .order_by("display_name", "pk")
    )
    # Household bills another member confirmed can be cancelled or resumed
    # here too; they stay out of suggestions, merge and charge editing.
    review = household_review(request.user, own)
    visible = sorted([*own, *review.shared_rows], key=lambda series: (series.display_name, series.pk))
    confirmed = [
        series
        for series in visible
        if series.status == RecurringSeries.Status.CONFIRMED and series.is_active and series.cancelled_at is None
    ]
    suggestions = [
        series
        for series in visible
        if series.status in (RecurringSeries.Status.POSSIBLE, RecurringSeries.Status.SUGGESTED)
    ]
    monthly_minor, annual_minor = confirmed_totals_once_per_bill(confirmed, review)
    confirmed.sort(key=lambda series: (-series.monthly_minor, series.display_name, series.pk))
    currency = confirmed[0].currency if confirmed else "USD"
    monthly_display = _money_display(monthly_minor, currency)
    annual_display = _money_display(annual_minor, currency)
    grouping_series = [
        series
        for series in own
        if series.is_active
        and series.status != RecurringSeries.Status.DISMISSED
        and series.cancelled_at is None
    ]
    merge_series_id, merge_targets = _recurring_merge_selection(request.GET.get("merge_series"), grouping_series)
    add_series_id = None
    add_query = request.GET.get("q", "")
    add_candidates = []
    raw_add = request.GET.get("add_series")
    if raw_add:
        try:
            add_series_id = int(raw_add)
        except (TypeError, ValueError):
            add_series_id = None
        add_row = next((series for series in grouping_series if series.pk == add_series_id), None)
        if add_row is not None:
            add_candidates = list_addable_transactions(request.user, add_row, add_query)
        else:
            add_series_id = None
    today = timezone.localdate()
    upcoming, price_change_rows, missed, cancelled = build_recurring_review(
        request.user, visible, today=today
    )
    return render(
        request,
        "finance/recurring_review.html",
        {
            "suggestions": suggestions,
            "skipped_merchants": Person.objects.values_list("recurring_skipped_merchants", flat=True).get(
                pk=request.user.person.pk
            ),
            "confirmed": confirmed,
            "upcoming": upcoming,
            "price_changes": price_change_rows,
            "price_change_percent": price_change_percent_for(request.user),
            "missed_charges": missed,
            "cancelled_series": cancelled,
            "monthly_display": monthly_display,
            "annual_display": annual_display,
            "monthly_minor": monthly_minor,
            "annual_minor": annual_minor,
            "add_series_id": add_series_id,
            "add_query": add_query,
            "add_candidates": add_candidates,
            "can_merge": len(grouping_series) > 1,
            "merge_series_id": merge_series_id,
            "merge_targets": merge_targets,
            "chart_data": _recurring_chart_data(
                confirmed,
                monthly_minor,
                annual_minor,
                monthly_display,
                annual_display,
            ),
        },
    )
