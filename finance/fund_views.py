"""Sinking fund pages (#670): a fund's history, the monthly set-asides, and a balance shortfall."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access import visible_rows
from .forms import FundMoveForm, FundSetAsideForm, FundShortfallForm
from .fund_services import (
    account_position,
    assign_to_fund,
    confirm_set_asides,
    fund_history,
    monthly_set_asides,
    release_from_fund,
    resolve_shortfall,
    undo_draw_down,
)
from .funds import fund_matches_account
from .models import Account, FundEntry, SavingsGoal
from .post_only import post_only
from .savings_goal_services import goal_progress


def _form_error(form, action):
    """Run a service call; a refusal the member can fix becomes a form error, a denial a 404."""
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc
    except ValidationError as exc:
        form.add_error(None, exc.messages[0])
        return None


def _visible_fund(user, goal_id):
    goal = get_object_or_404(
        visible_rows(SavingsGoal, user).select_related("linked_account", "funds_planned_item"), pk=goal_id
    )
    if not goal.share_of_account:
        raise Http404
    return goal


@require_http_methods(["GET", "POST"])
@never_cache
def fund_detail(request, goal_id):
    goal = _visible_fund(request.user, goal_id)
    today = timezone.localdate()
    form = FundMoveForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        mover = assign_to_fund if form.cleaned_data["action"] == FundMoveForm.ASSIGN else release_from_fund
        saved = _form_error(form, lambda: mover(request.user, goal, form.amount_minor(), today=today))
        if saved is not None:
            messages.success(request, "Fund updated.")
            return redirect("savings-goal-fund", goal.pk)
    card = goal_progress(request.user, goal, today=today)
    position = None
    account = card.linked_account
    if account is not None and fund_matches_account(goal, account):
        position = account_position(account, today=today)
    return render(
        request,
        "finance/savings_goal_fund.html",
        {
            "goal": goal,
            "card": card,
            "position": position,
            "account_mismatch": goal.linked_account_id is not None and position is None and account is not None,
            "entries": fund_history(request.user, goal),
            "form": form,
        },
    )


@post_only("savings-goal-fund", "goal_id")
@never_cache
def fund_undo(request, goal_id, entry_id):
    goal = _visible_fund(request.user, goal_id)
    entry = get_object_or_404(FundEntry.objects.filter(goal=goal), pk=entry_id)
    try:
        undo_draw_down(request.user, entry)
    except PermissionDenied as exc:
        raise Http404 from exc
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, "Draw-down undone; the money is back in the fund.")
    return redirect("savings-goal-fund", goal.pk)


@require_http_methods(["GET", "POST"])
@never_cache
def fund_set_asides(request):
    today = timezone.localdate()
    plan = monthly_set_asides(request.user, today=today)
    form = FundSetAsideForm(request.POST if request.method == "POST" else None, rows=plan.pending)
    if request.method == "POST" and form.is_valid():
        saved = _form_error(form, lambda: confirm_set_asides(request.user, form.picks(), today=today))
        if saved is not None:
            messages.success(request, f"Set aside money for {len(saved)} fund{'s' if len(saved) != 1 else ''}.")
            return redirect("fund-set-asides")
    return render(
        request,
        "finance/fund_set_asides.html",
        {
            "plan": plan,
            "form": form,
            "confirmed_rows": [row for row in plan.rows if row.confirmed is not None],
            "idle_rows": [row for row in plan.rows if row.confirmed is None and not row.suggested_minor],
        },
    )


@require_http_methods(["GET", "POST"])
@never_cache
def fund_shortfall(request, account_id):
    account = get_object_or_404(
        visible_rows(Account, request.user).filter(status=Account.Status.ACTIVE, archived_at__isnull=True),
        pk=account_id,
    )
    today = timezone.localdate()
    position = account_position(account, today=today)
    if not position.funds:
        raise Http404
    funds = [row for row in position.funds if row.assigned_minor > 0]
    form = FundShortfallForm(request.POST if request.method == "POST" else None, funds=funds)
    if request.method == "POST" and form.is_valid():
        saved = _form_error(form, lambda: resolve_shortfall(request.user, account, form.reductions(), today=today))
        if saved is not None:
            messages.success(request, "Funds reduced.")
            return redirect("fund-shortfall", account.pk)
    return render(
        request,
        "finance/fund_shortfall.html",
        {"account": account, "position": position, "form": form},
    )
