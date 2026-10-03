from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .cash_flow import format_minor
from .category_services import current_household
from .forms import AccountDeleteForm, AccountRenameForm, AddAccountForm, ChangeShareModeForm, ShareAccountForm
from .lifecycle_services import (
    archive_account,
    change_account_share_mode,
    delete_account,
    lock_actor_household,
    rename_account,
    share_account,
    unshare_account,
)
from .reauth import requires_recent_auth
from .models import Account, BalanceSnapshot, ImportBatch, Person, Transaction


def _person(request):
    return get_object_or_404(Person, user=request.user)


def _active_visible_account(user, account_id):
    return get_object_or_404(
        Account.objects.visible_to(user).filter(status=Account.Status.ACTIVE, archived_at__isnull=True),
        pk=account_id,
    )


def _service_or_404(action):
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc


def _visible_accounts(user):
    latest = BalanceSnapshot.objects.filter(account_id=OuterRef("pk")).order_by(
        "-snapshot_date",
        "-source",
        "-pk",
    )
    return (
        Account.objects.visible_to(user)
        .annotate(
            last_import_at=Max(
                "import_batches__imported_at",
                filter=Q(import_batches__status=ImportBatch.Status.ACTIVE),
            ),
            transaction_count=Count(
                "transactions",
                distinct=True,
                filter=Q(transactions__status=Transaction.Status.ACTIVE),
            ),
            last_snapshot_date=Subquery(latest.values("snapshot_date")[:1]),
            last_snapshot_amount=Subquery(latest.values("amount_minor")[:1]),
            last_snapshot_source=Subquery(latest.values("source")[:1]),
        )
        .order_by("name", "pk")
    )


@require_http_methods(["GET", "POST"])
@never_cache
def account_list(request):
    person = _person(request)
    household = current_household(person)
    form = AddAccountForm(request.POST or None, has_household=household is not None)
    if request.method == "POST" and form.is_valid():
        account = _create_account(person, form)
        if account.accepts_csv_import():
            return redirect("csv-import-preview", account.pk)
        return redirect("account-balances", account.pk)
    accounts = list(_visible_accounts(request.user))
    for account in accounts:
        if account.last_snapshot_date is None:
            account.last_snapshot_display = ""
        else:
            account.last_snapshot_display = format_minor(account.last_snapshot_amount)
    return render(
        request,
        "finance/accounts.html",
        {
            "accounts": accounts,
            "add_form": form,
            "has_household": household is not None,
            "has_active_accounts": any(
                account.status == Account.Status.ACTIVE and account.archived_at is None for account in accounts
            ),
            "viewer_id": person.pk,
        },
    )


def _create_account(person, form):
    name = form.cleaned_data["name"]
    account_type = form.cleaned_data["account_type"]
    sharing = form.cleaned_data["sharing"]
    with transaction.atomic():
        membership, _memberships = lock_actor_household(person)
        if sharing in Account.ShareMode.values:
            if membership is None:
                raise Http404
            return Account.objects.create(
                name=name,
                account_type=account_type,
                owner=person,
                scope=Account.Scope.HOUSEHOLD,
                share_mode=sharing,
                household=membership.household,
                currency="USD",
            )
        return Account.objects.create(
            name=name,
            account_type=account_type,
            owner=person,
            scope=Account.Scope.PRIVATE,
            household=None,
            currency="USD",
        )


@require_POST
@never_cache
def account_rename(request, account_id):
    _active_visible_account(request.user, account_id)
    form = AccountRenameForm(request.POST)
    if form.is_valid():
        # The service rechecks visibility under the household and account
        # locks, so an account unshared mid-request cannot be renamed.
        _service_or_404(lambda: rename_account(request.user, account_id, form.cleaned_data["name"]))
    return redirect("account-list")


@require_POST
@never_cache
@requires_recent_auth("share-account", form_url_name="account-list")
def account_share(request, account_id):
    _active_visible_account(request.user, account_id)
    form = ShareAccountForm(request.POST)
    if not form.is_valid():
        return redirect("account-list")
    share_mode = form.cleaned_data["share_mode"]
    _service_or_404(lambda: share_account(request.user, account_id, share_mode))
    return redirect("account-list")


@require_POST
@never_cache
@requires_recent_auth("change-share-mode", form_url_name="account-list")
def account_change_share_mode(request, account_id):
    _active_visible_account(request.user, account_id)
    form = ChangeShareModeForm(request.POST)
    if not form.is_valid():
        return redirect("account-list")
    share_mode = form.cleaned_data["share_mode"]
    confirm = form.cleaned_data["confirm_give_up_ownership"]
    _service_or_404(
        lambda: change_account_share_mode(
            request.user,
            account_id,
            share_mode,
            confirm_give_up_ownership=confirm,
        )
    )
    return redirect("account-list")


@require_POST
@never_cache
@requires_recent_auth("unshare-account", form_url_name="account-list")
def account_unshare(request, account_id):
    _active_visible_account(request.user, account_id)
    _service_or_404(lambda: unshare_account(request.user, account_id))
    return redirect("account-list")


@require_POST
@never_cache
def account_archive(request, account_id):
    _active_visible_account(request.user, account_id)
    _service_or_404(lambda: archive_account(request.user, account_id))
    return redirect("account-list")


def _owned_visible_account(user, account_id):
    person = get_object_or_404(Person, user=user)
    return get_object_or_404(Account.objects.visible_to(user).filter(owner=person), pk=account_id)


def _account_delete_counts(account):
    return {
        "transaction_count": Transaction.objects.filter(account=account).count(),
        "import_count": ImportBatch.objects.filter(account=account).count(),
    }


def _render_account_delete(request, account, form):
    return render(
        request,
        "finance/account_delete.html",
        {"account": account, "form": form, **_account_delete_counts(account)},
    )


@require_http_methods(["GET", "POST"])
@never_cache
@requires_recent_auth("delete-account")
def account_delete(request, account_id):
    account = _owned_visible_account(request.user, account_id)
    form = AccountDeleteForm(request.POST or None, account_name=account.name)
    if request.method == "POST" and form.is_valid():
        account_name = _service_or_404(lambda: delete_account(request.user, account.pk))
        messages.success(request, f"Deleted {account_name}.")
        return redirect("account-list")
    return _render_account_delete(request, account, form)
