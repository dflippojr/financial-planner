from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access import request_person as _person
from .access import service_or_404 as _service_or_404
from .access import visible_rows
from .category_services import current_household
from .forms import SavingsGoalForm
from .fund_services import monthly_set_asides, visible_fund_accounts
from .models import PlannedItem, SavingsGoal
from .post_only import post_only
from .savings_goal_services import (
    goal_progress,
    save_savings_goal,
    set_savings_goal_archived,
    set_savings_goal_completed,
)


def _visible_goal(user, goal_id):
    return get_object_or_404(SavingsGoal.objects.visible_to(user), pk=goal_id)


def _fund_prefill(request):
    """Start the add form as a fund for a planned item (?fund_item=<id>), when the viewer can see it."""
    raw = request.GET.get("fund_item", "")
    if not raw.isdigit():
        return None
    item = visible_rows(PlannedItem, request.user).filter(pk=int(raw), kind=PlannedItem.Kind.EXPENSE).first()
    if item is None:
        return None
    return {"name": item.name, "scope": item.scope, "share_of_account": True, "funds_planned_item": item.pk}


def _form(request, person, data=None, instance=None, initial=None):
    household = current_household(person)
    if instance is not None and data is None:
        initial = {
            "name": instance.name,
            "target_amount": Decimal(instance.target_amount_minor) / Decimal(100),
            "target_date": instance.target_date,
            "priority": instance.priority,
            "depends_on": instance.depends_on_id,
            "time_sensitive": instance.time_sensitive,
            "scope": instance.scope,
            "linked_account": instance.linked_account,
            "manual_amount": (
                Decimal(instance.manual_amount_minor) / Decimal(100)
                if instance.manual_amount_minor is not None
                else None
            ),
            "manual_amount_date": instance.manual_amount_date,
            "share_of_account": instance.share_of_account,
            "funds_planned_item": instance.funds_planned_item_id,
        }
    return SavingsGoalForm(
        data,
        principal=request.user,
        has_household=household is not None,
        household_only=instance is not None and instance.owner_id != person.pk,
        instance=instance,
        initial=initial,
    )


@require_http_methods(["GET", "POST"])
@never_cache
def savings_goal_list(request):
    person = _person(request)
    prefill = _fund_prefill(request) if request.method == "GET" else None
    form = _form(request, person, request.POST if request.method == "POST" else None, initial=prefill)
    if request.method == "POST" and form.is_valid():
        _service_or_404(lambda: save_savings_goal(request.user, form.save_payload()), also=(ValidationError,))
        return redirect("savings-goals")
    today = timezone.localdate()
    goals = SavingsGoal.objects.visible_to(request.user).select_related(
        "depends_on", "funds_planned_item", "linked_account"
    ).order_by(F("priority").asc(nulls_last=True), F("target_date").asc(nulls_last=True), "name", "pk")
    cards = [goal_progress(request.user, goal, today=today) for goal in goals]
    fund_positions = visible_fund_accounts(request.user, today=today)
    set_asides = monthly_set_asides(request.user, today=today) if fund_positions else None
    return render(
        request,
        "finance/savings_goals.html",
        {
            "cards": cards,
            "add_form": form,
            "fund_positions": fund_positions,
            "set_asides": set_asides,
            "open_add_form": prefill is not None,
        },
    )


@require_http_methods(["GET", "POST"])
@never_cache
def savings_goal_edit(request, goal_id):
    person = _person(request)
    goal = _visible_goal(request.user, goal_id)
    form = _form(
        request,
        person,
        request.POST if request.method == "POST" else None,
        instance=goal,
    )
    if request.method == "POST" and form.is_valid():
        _service_or_404(lambda: save_savings_goal(request.user, form.save_payload(), goal=goal), also=(ValidationError,))
        return redirect("savings-goals")
    return render(
        request,
        "finance/savings_goal_edit.html",
        {"goal": goal, "form": form},
    )


@post_only("savings-goals")
@never_cache
def savings_goal_complete(request, goal_id):
    goal = _visible_goal(request.user, goal_id)
    _service_or_404(lambda: set_savings_goal_completed(request.user, goal, True), also=(ValidationError,))
    return redirect("savings-goals")


@post_only("savings-goals")
@never_cache
def savings_goal_reopen(request, goal_id):
    goal = _visible_goal(request.user, goal_id)
    _service_or_404(lambda: set_savings_goal_completed(request.user, goal, False), also=(ValidationError,))
    return redirect("savings-goals")


@post_only("savings-goals")
@never_cache
def savings_goal_archive(request, goal_id):
    goal = _visible_goal(request.user, goal_id)
    _service_or_404(lambda: set_savings_goal_archived(request.user, goal, True), also=(ValidationError,))
    return redirect("savings-goals")


@post_only("savings-goals")
@never_cache
def savings_goal_unarchive(request, goal_id):
    goal = _visible_goal(request.user, goal_id)
    _service_or_404(lambda: set_savings_goal_archived(request.user, goal, False), also=(ValidationError,))
    return redirect("savings-goals")
