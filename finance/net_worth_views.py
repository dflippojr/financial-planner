from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .account_views import _active_visible_account, _service_or_404
from .cash_flow import default_date_range, format_minor
from .forms import ManualBalanceForm, NetWorthFilterForm
from .models import Account, BalanceSnapshot
from .net_worth import net_worth_chart_data, net_worth_preset_links, net_worth_report
from .performance import account_performance
from .snapshot_services import SnapshotError, delete_manual_snapshot, record_manual_snapshot, update_manual_snapshot


def _service_or_form_error(form, action):
    try:
        return action()
    except SnapshotError as exc:
        form.add_error(None, str(exc))
        return None
    except PermissionDenied as exc:
        raise Http404 from exc


@require_GET
@never_cache
def net_worth(request):
    today = timezone.localdate()
    default_from, default_to = default_date_range(today)
    form = NetWorthFilterForm(request.GET or None)
    if not form.is_bound:
        form = NetWorthFilterForm(initial={"date_from": default_from, "date_to": default_to})
        date_from, date_to, scope = default_from, default_to, ""
    elif form.is_valid():
        date_from = form.cleaned_data["date_from"] or default_from
        date_to = form.cleaned_data["date_to"] or default_to
        scope = form.cleaned_data["scope"]
    else:
        date_from = date_to = scope = None
    report = None
    performance_rows = []
    if date_from is not None:
        report = net_worth_report(
            request.user,
            date_from=date_from,
            date_to=date_to,
            scope=scope,
            today=today,
        )
        performance_rows = [
            account_performance(account, today)
            for account in report.accounts
            if account.account_type == Account.Type.INVESTMENT
        ]
    return render(
        request,
        "finance/net_worth.html",
        {
            "filter_form": form,
            "report": report,
            "chart_data": net_worth_chart_data(report) if report is not None else None,
            "presets": net_worth_preset_links(today, scope=scope or "") if date_from is not None else (),
            "performance_rows": performance_rows,
        },
    )


def _manual_form(account, data=None, snapshot=None):
    initial = None
    if snapshot is not None:
        initial = {
            "snapshot_date": snapshot.snapshot_date,
            "amount": Decimal(snapshot.amount_minor) / Decimal(100),
            "note": snapshot.note,
        }
        if snapshot.net_contribution_minor is not None:
            initial["net_contribution"] = Decimal(snapshot.net_contribution_minor) / Decimal(100)
    return ManualBalanceForm(data, account=account, initial=initial)


@require_http_methods(["GET", "POST"])
@never_cache
def account_balances(request, account_id):
    account = _active_visible_account(request.user, account_id)
    form = _manual_form(account, request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        saved = _service_or_form_error(
            form,
            lambda: record_manual_snapshot(
                request.user,
                account.pk,
                snapshot_date=form.cleaned_data["snapshot_date"],
                amount_minor=form.amount_minor(),
                note=form.cleaned_data["note"],
                net_contribution_minor=form.net_contribution_minor(),
            ),
        )
        if saved is not None:
            messages.success(request, "Recorded balance.")
            return redirect("account-balances", account.pk)
    snapshots = list(account.balance_snapshots.order_by("-snapshot_date", "-source", "-pk"))
    for snapshot in snapshots:
        snapshot.amount_display = format_minor(snapshot.amount_minor, snapshot.currency)
        if snapshot.net_contribution_minor is not None:
            snapshot.net_contribution_display = format_minor(snapshot.net_contribution_minor)
    performance = None
    if account.account_type == Account.Type.INVESTMENT:
        performance = account_performance(account, timezone.localdate())
    return render(
        request,
        "finance/account_balances.html",
        {"account": account, "form": form, "snapshots": snapshots, "performance": performance},
    )


def _visible_manual_snapshot(user, account_id, snapshot_id):
    account = _active_visible_account(user, account_id)
    snapshot = get_object_or_404(
        BalanceSnapshot.objects.visible_to(user).filter(
            pk=snapshot_id,
            account=account,
            source=BalanceSnapshot.Source.MANUAL,
        )
    )
    return account, snapshot


@require_http_methods(["GET", "POST"])
@never_cache
def account_snapshot_edit(request, account_id, snapshot_id):
    account, snapshot = _visible_manual_snapshot(request.user, account_id, snapshot_id)
    form = _manual_form(account, request.POST if request.method == "POST" else None, snapshot)
    if request.method == "POST" and form.is_valid():
        saved = _service_or_form_error(
            form,
            lambda: update_manual_snapshot(
                request.user,
                account.pk,
                snapshot.pk,
                snapshot_date=form.cleaned_data["snapshot_date"],
                amount_minor=form.amount_minor(),
                note=form.cleaned_data["note"],
                net_contribution_minor=form.net_contribution_minor(),
            ),
        )
        if saved is not None:
            messages.success(request, "Updated balance.")
            return redirect("account-balances", account.pk)
    return render(
        request,
        "finance/account_snapshot_edit.html",
        {"account": account, "snapshot": snapshot, "form": form},
    )


@require_POST
@never_cache
def account_snapshot_delete(request, account_id, snapshot_id):
    _visible_manual_snapshot(request.user, account_id, snapshot_id)
    _service_or_404(lambda: delete_manual_snapshot(request.user, account_id, snapshot_id))
    messages.success(request, "Deleted balance.")
    return redirect("account-balances", account_id)
