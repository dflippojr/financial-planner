"""Transaction-list actions for AI category suggestions."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .category_suggestion_services import (
    accept_suggestion,
    accept_suggestions,
    queue_remaining_uncategorized,
    reject_suggestion,
)
from .forms import TransactionFilterForm
from .models import CategorySuggestion, Person


def _person(request):
    return Person.objects.filter(user=request.user).first()


def _safe_next(request):
    next_url = request.POST.get("next") or reverse("transaction-list")
    if next_url.startswith("/transactions"):
        return next_url
    return reverse("transaction-list")


def _service_or_404(action):
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc


@require_POST
@never_cache
def suggest_categories(request):
    person = _person(request)
    if person is None:
        raise Http404
    form = TransactionFilterForm(request.POST, principal=request.user)
    if not form.is_valid() or form.cleaned_data.get("category") != "uncategorized":
        return redirect(_safe_next(request))
    queue_remaining_uncategorized(person)
    return redirect(_safe_next(request))


@require_POST
@never_cache
def suggestion_accept(request, suggestion_id):
    _service_or_404(lambda: accept_suggestion(request.user, suggestion_id))
    return redirect(_safe_next(request))


@require_POST
@never_cache
def suggestion_reject(request, suggestion_id):
    _service_or_404(lambda: reject_suggestion(request.user, suggestion_id))
    return redirect(_safe_next(request))


@require_POST
@never_cache
def suggestion_accept_all(request):
    raw_ids = request.POST.getlist("suggestion_id")
    ids = []
    for item in raw_ids:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    shown = set(
        CategorySuggestion.objects.visible_to(request.user)
        .filter(pk__in=ids, status=CategorySuggestion.Status.PENDING)
        .values_list("pk", flat=True)
    )
    _service_or_404(lambda: accept_suggestions(request.user, [pk for pk in ids if pk in shown]))
    return redirect(_safe_next(request))
