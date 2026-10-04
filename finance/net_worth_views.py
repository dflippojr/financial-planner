from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .account_views import _active_visible_account, _service_or_404
from .cash_flow import default_date_range, format_minor
from .forms import DebtTermsForm, ManualBalanceForm, NetWorthFilterForm, PairLoanForm
from .lifecycle_services import update_debt_terms
from .models import Account, BalanceSnapshot
from .net_worth import net_worth_chart_data, net_worth_preset_links, net_worth_report
from .performance import account_performance
from .pairing_services import PairingError, set_loan_secured_asset
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


def _debt_terms_form(account, data=None):
    if account.account_type not in Account.LIABILITY_TYPES:
        return None
    initial = None
    if data is None:
        initial = {
            "apr_percent": account.apr_percent,
            "minimum_payment": (
                Decimal(account.minimum_payment_minor) / Decimal(100)
                if account.minimum_payment_minor is not None
                else None
            ),
            "payment_day": account.payment_day,
        }
    return DebtTermsForm(data, account=account, initial=initial)


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
            messages.success(
                request,
                "Recorded estimate." if account.is_physical_asset() else "Recorded balance.",
            )
            return redirect("account-balances", account.pk)
    snapshots = list(account.balance_snapshots.order_by("-snapshot_date", "-source", "-pk"))
    for snapshot in snapshots:
        snapshot.amount_display = format_minor(snapshot.amount_minor, snapshot.currency)
        if snapshot.net_contribution_minor is not None:
            snapshot.net_contribution_display = format_minor(snapshot.net_contribution_minor)
    performance = None
    if account.account_type == Account.Type.INVESTMENT:
        performance = account_performance(account, timezone.localdate())
    pair_form = None
    if account.account_type == Account.Type.LOAN:
        pair_form = PairLoanForm(loan=account, principal=request.user)
    debt_terms_form = _debt_terms_form(account)
    return render(
        request,
        "finance/account_balances.html",
        {
            "account": account,
            "form": form,
            "snapshots": snapshots,
            "performance": performance,
            "pair_form": pair_form,
            "debt_terms_form": debt_terms_form,
        },
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
            messages.success(
                request,
                "Updated estimate." if account.is_physical_asset() else "Updated balance.",
            )
            return redirect("account-balances", account.pk)
    return render(
        request,
        "finance/account_snapshot_edit.html",
        {"account": account, "snapshot": snapshot, "form": form},
    )


@require_POST
@never_cache
def account_snapshot_delete(request, account_id, snapshot_id):
    account, _snapshot = _visible_manual_snapshot(request.user, account_id, snapshot_id)
    _service_or_404(lambda: delete_manual_snapshot(request.user, account_id, snapshot_id))
    messages.success(request, "Deleted estimate." if account.is_physical_asset() else "Deleted balance.")
    return redirect("account-balances", account_id)


@require_POST
@never_cache
def account_pair_loan(request, account_id):
    account = _active_visible_account(request.user, account_id)
    form = PairLoanForm(request.POST, loan=account, principal=request.user)
    if form.is_valid():
        asset = form.cleaned_data["secured_asset"]
        try:
            set_loan_secured_asset(request.user, account.pk, asset.pk if asset is not None else None)
            messages.success(request, "Updated loan pairing.")
        except PairingError as exc:
            messages.error(request, str(exc))
        except PermissionDenied as exc:
            raise Http404 from exc
    return redirect("account-balances", account.pk)


@require_POST
@never_cache
def account_debt_terms(request, account_id):
    account = _active_visible_account(request.user, account_id)
    form = _debt_terms_form(account, request.POST)
    if form is None:
        raise Http404
    if form.is_valid():
        try:
            update_debt_terms(
                request.user,
                account.pk,
                apr_percent=form.cleaned_data["apr_percent"],
                minimum_payment_minor=form.minimum_payment_minor(),
                payment_day=form.cleaned_data.get("payment_day"),
            )
            messages.success(request, "Updated debt details.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if getattr(exc, "messages", None) else str(exc))
        except PermissionDenied as exc:
            raise Http404 from exc
    else:
        messages.error(request, form.errors.as_text())
    return redirect("account-balances", account.pk)
