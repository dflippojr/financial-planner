from calendar import month_name
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .budget_services import (
    NEAR_LIMIT_PERCENT,
    amount_for,
    month_budget_cards,
    parse_month,
    reset_budget_rollover,
    save_budget,
    set_budget_archived,
    set_budget_rollover,
)
from .category_services import current_household
from .forms import BudgetForm
from .models import Budget, Person
from .months import add_months


def _person(request):
    return get_object_or_404(Person, user=request.user)


def _service_or_404(action):
    try:
        return action()
    except (PermissionDenied, ValidationError) as exc:
        raise Http404 from exc


def _visible_budget(user, budget_id):
    return get_object_or_404(Budget.objects.visible_to(user), pk=budget_id)


def _list_url(month):
    return f"{reverse('budgets')}?month={month.isoformat()[:7]}"


def _form(request, person, data=None, instance=None, *, month):
    household = current_household(person)
    initial = {"effective_month": month.isoformat()[:7], "scope": Budget.Scope.PRIVATE}
    if instance is not None and data is None:
        amount_minor = amount_for(instance, month)
        initial = {
            "scope": instance.scope,
            "category": instance.category,
            "amount": Decimal(amount_minor) / Decimal(100) if amount_minor else None,
            "effective_month": month.isoformat()[:7],
            "rollover_enabled": instance.rollover_enabled,
        }
    return BudgetForm(
        data,
        principal=request.user,
        has_household=household is not None,
        edit=instance is not None,
        initial=initial,
    )


@require_http_methods(["GET", "POST"])
@never_cache
def budget_list(request):
    person = _person(request)
    month = parse_month(request.GET.get("month") or request.POST.get("month"))
    form = _form(request, person, request.POST if request.method == "POST" else None, month=month)
    if request.method == "POST" and form.is_valid():
        try:
            save_budget(request.user, form.save_payload())
            return redirect(_list_url(month))
        except ValidationError as exc:
            form.add_error(None, exc)
        except PermissionDenied as exc:
            raise Http404 from exc
    cards = month_budget_cards(request.user, month)
    # Phone cards: the overall total first, then categories over their limit, then near it.
    overall_cards = [card for card in cards if card.budget.category_id is None]
    category_cards = sorted(
        (card for card in cards if card.budget.category_id is not None),
        key=lambda card: (not card.over_budget, card.percent < NEAR_LIMIT_PERCENT, -card.percent, card.name.lower()),
    )
    previous_month = add_months(month, -1)
    next_month = add_months(month, 1)
    return render(
        request,
        "finance/budgets.html",
        {
            "cards": cards,
            "overall_cards": overall_cards,
            "category_cards": category_cards,
            "add_form": form,
            "month": month,
            "month_label": f"{month_name[month.month]} {month.year}",
            "previous_url": _list_url(previous_month),
            "next_url": _list_url(next_month),
        },
    )


@require_http_methods(["GET", "POST"])
@never_cache
def budget_edit(request, budget_id):
    person = _person(request)
    budget = _visible_budget(request.user, budget_id)
    month = parse_month(request.GET.get("month") or request.POST.get("month"))
    form = _form(
        request,
        person,
        request.POST if request.method == "POST" else None,
        instance=budget,
        month=month,
    )
    if request.method == "POST" and form.is_valid():
        payload = form.save_payload()
        payload["scope"] = budget.scope
        payload["category"] = budget.category
        try:
            save_budget(request.user, payload, budget=budget)
            return redirect(_list_url(month))
        except ValidationError as exc:
            form.add_error(None, exc)
        except PermissionDenied as exc:
            raise Http404 from exc
    return render(
        request,
        "finance/budget_edit.html",
        {"budget": budget, "form": form, "month": month, "cancel_url": _list_url(month)},
    )


@require_POST
@never_cache
def budget_archive(request, budget_id):
    budget = _visible_budget(request.user, budget_id)
    month = parse_month(request.POST.get("month"))
    _service_or_404(lambda: set_budget_archived(request.user, budget, True))
    return redirect(_list_url(month))


@require_POST
@never_cache
def budget_rollover_toggle(request, budget_id):
    budget = _visible_budget(request.user, budget_id)
    month = parse_month(request.POST.get("month"))
    enabled = request.POST.get("enabled") == "1"
    _service_or_404(lambda: set_budget_rollover(request.user, budget, enabled, month=month))
    return redirect(_list_url(month))


@require_http_methods(["GET", "POST"])
@never_cache
def budget_rollover_reset(request, budget_id):
    budget = _visible_budget(request.user, budget_id)
    month = parse_month(request.GET.get("month") or request.POST.get("month"))
    if request.method == "POST":
        _service_or_404(lambda: reset_budget_rollover(request.user, budget, month=month))
        return redirect(_list_url(month))
    return render(
        request,
        "finance/budget_rollover_reset.html",
        {
            "budget": budget,
            "month": month,
            "month_label": f"{month_name[month.month]} {month.year}",
            "cancel_url": _list_url(month),
        },
    )
