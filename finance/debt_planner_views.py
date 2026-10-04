from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .debt_planner import STRATEGY_MINIMUMS
from .debt_planner_services import build_debt_plan, list_visible_debts
from .forms import DebtPlannerForm


def _id_list(values):
    ids = []
    for raw in values:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _custom_order(query, include_ids):
    ranked = []
    for account_id in include_ids:
        raw = query.get(f"rank_{account_id}")
        try:
            rank = int(raw)
        except (TypeError, ValueError):
            rank = 10**9
        ranked.append((rank, account_id))
    ranked.sort()
    return [account_id for _rank, account_id in ranked]


@require_GET
@never_cache
def debt_payoff(request):
    today = timezone.localdate()
    bound = bool(request.GET)
    form = DebtPlannerForm(request.GET or None)
    extra_minor = 0
    strategy = STRATEGY_MINIMUMS
    if not bound:
        form = DebtPlannerForm()
    elif form.is_valid():
        extra_minor = form.extra_minor()
        strategy = form.cleaned_data["strategy"]
    include_ids = _id_list(request.GET.getlist("include"))
    if not bound:
        include_ids = [row.account_id for row in list_visible_debts(request.user, today=today) if row.ready]
    custom_order = _custom_order(request.GET, include_ids)
    plan = build_debt_plan(
        request.user,
        include_ids=include_ids,
        extra_minor=extra_minor,
        strategy=strategy,
        custom_order=custom_order,
        today=today,
    )
    comparison = plan.comparison
    interest_saved_display = ""
    if comparison is not None:
        interest_saved_display = comparison.interest_saved_display
    return render(
        request,
        "finance/debt_payoff.html",
        {
            "form": form,
            "plan": plan,
            "today": today,
            "interest_saved_display": interest_saved_display,
        },
    )
