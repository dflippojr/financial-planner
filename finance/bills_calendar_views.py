from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .bills_calendar import (
    build_month,
    calendar_settings_for,
    deposit_accounts,
    evaluate_expected_balance_alert,
    save_calendar_settings,
)
from .forms import BillsCalendarForm
from .models import Person


def _person(request):
    return get_object_or_404(Person, user=request.user)


def _service_or_404(action):
    try:
        return action()
    except (PermissionDenied, ValidationError) as exc:
        raise Http404 from exc


def _month_from_params(params, today):
    try:
        year = int(params.get("year", today.year))
        month = int(params.get("month", today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if month < 1 or month > 12 or year < 1 or year > 9998:
        year, month = today.year, today.month
    return year, month


def _scope_from_params(params):
    scope = params.get("scope", "")
    if scope not in ("", "private", "household"):
        return ""
    return scope


def _settings_form(request, person, data=None):
    prefs = calendar_settings_for(person)
    amount = None
    if prefs.threshold_minor is not None:
        amount = Decimal(prefs.threshold_minor) / Decimal(100)
    selected = list(prefs.accounts.filter(pk__in=deposit_accounts(person).values("pk")))
    return BillsCalendarForm(
        data,
        principal=request.user,
        initial={"accounts": selected, "threshold_amount": amount},
    )


@require_http_methods(["GET", "POST"])
@never_cache
def bills_calendar(request):
    person = _person(request)
    today = timezone.localdate()
    params = request.POST if request.method == "POST" else request.GET
    year, month = _month_from_params(params, today)
    scope = _scope_from_params(params)
    form = _settings_form(request, person)
    if request.method == "POST":
        form = _settings_form(request, person, request.POST)
        if form.is_valid():
            _service_or_404(lambda: save_calendar_settings(request.user, **form.save_payload()))
            evaluate_expected_balance_alert(person, today=today)
            url = reverse("bills-calendar")
            params = f"?year={year}&month={month}"
            if scope:
                params += f"&scope={scope}"
            return redirect(url + params)
    prefs = calendar_settings_for(person)
    selected_ids = list(prefs.accounts.values_list("pk", flat=True))
    calendar = build_month(
        request.user,
        year=year,
        month=month,
        today=today,
        scope=scope,
        account_ids=selected_ids,
        threshold_minor=prefs.threshold_minor,
    )
    if request.method == "GET":
        evaluate_expected_balance_alert(person, today=today)
    return render(
        request,
        "finance/bills_calendar.html",
        {
            "calendar": calendar,
            "settings_form": form,
            "scope": scope,
        },
    )
