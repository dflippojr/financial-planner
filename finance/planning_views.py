from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .category_services import current_household
from .forms import PlannedItemForm
from .models import Person, PlannedItem
from .planning_services import save_planned_item, set_planned_item_enabled


def _person(request):
    return get_object_or_404(Person, user=request.user)


def _service_or_404(action):
    try:
        return action()
    except (PermissionDenied, ValidationError) as exc:
        raise Http404 from exc


def _visible_item(user, item_id):
    return get_object_or_404(PlannedItem.objects.visible_to(user), pk=item_id)


def _prefill_from_query(query):
    initial = {}
    for key in ("name", "kind", "amount", "start_date", "end_date", "cadence", "scope"):
        value = query.get(key)
        if value:
            initial[key] = value
    return initial


def _form(request, person, data=None, instance=None, extra_initial=None):
    household = current_household(person)
    initial = None
    if instance is not None and data is None:
        initial = {
            "name": instance.name,
            "kind": instance.kind,
            "amount": Decimal(instance.amount_minor) / Decimal(100),
            "start_date": instance.start_date,
            "end_date": instance.end_date,
            "cadence": instance.cadence,
            "scope": instance.scope,
            "category": instance.category,
            "replaces_series": instance.replaces_series,
        }
    if extra_initial:
        initial = {**(initial or {}), **extra_initial}
    return PlannedItemForm(
        data,
        principal=request.user,
        has_household=household is not None,
        household_only=instance is not None and instance.owner_id != person.pk,
        current_series_id=instance.replaces_series_id if instance is not None else None,
        initial=initial,
    )


@require_http_methods(["GET", "POST"])
@never_cache
def planned_item_list(request):
    person = _person(request)
    extra = _prefill_from_query(request.GET) if request.method == "GET" else None
    form = _form(request, person, request.POST if request.method == "POST" else None, extra_initial=extra)
    if request.method == "POST" and form.is_valid():
        _service_or_404(lambda: save_planned_item(request.user, form.save_payload()))
        return redirect("planned-items")
    items = PlannedItem.objects.visible_to(request.user).order_by("start_date", "name", "pk")
    return render(
        request,
        "finance/planned_items.html",
        {"items": items, "add_form": form},
    )


@require_http_methods(["GET", "POST"])
@never_cache
def planned_item_edit(request, item_id):
    person = _person(request)
    item = _visible_item(request.user, item_id)
    extra = _prefill_from_query(request.GET) if request.method == "GET" else None
    form = _form(
        request,
        person,
        request.POST if request.method == "POST" else None,
        instance=item,
        extra_initial=extra,
    )
    if request.method == "POST" and form.is_valid():
        _service_or_404(lambda: save_planned_item(request.user, form.save_payload(), item=item))
        return redirect("planned-items")
    return render(
        request,
        "finance/planned_item_edit.html",
        {"item": item, "form": form},
    )


@require_POST
@never_cache
def planned_item_disable(request, item_id):
    item = _visible_item(request.user, item_id)
    _service_or_404(lambda: set_planned_item_enabled(request.user, item, False))
    return redirect("planned-items")


@require_POST
@never_cache
def planned_item_enable(request, item_id):
    item = _visible_item(request.user, item_id)
    _service_or_404(lambda: set_planned_item_enabled(request.user, item, True))
    return redirect("planned-items")
