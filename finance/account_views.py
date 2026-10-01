from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .category_services import current_household
from .forms import AccountRenameForm, AddAccountForm
from .lifecycle_services import archive_account, lock_actor_household, share_account, unshare_account
from .models import Account, ImportBatch, Person, Transaction


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
        )
        .order_by("name", "pk")
    )


@require_http_methods(["GET", "POST"])
@never_cache
def account_list(request):
    person = _person(request)
    household = current_household(person)
    form = AddAccountForm(request.POST or None, has_household=household is not None)
    if request.method == "POST":
        if form.is_valid():
            account = _create_account(person, form)
            return redirect("csv-import-preview", account.pk)
    accounts = list(_visible_accounts(request.user))
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
        },
    )


def _create_account(person, form):
    name = form.cleaned_data["name"]
    account_type = form.cleaned_data["account_type"]
    sharing = form.cleaned_data["sharing"]
    with transaction.atomic():
        membership, _memberships = lock_actor_household(person)
        if sharing == Account.Scope.HOUSEHOLD:
            if membership is None:
                raise Http404
            return Account.objects.create(
                name=name,
                account_type=account_type,
                owner=person,
                scope=Account.Scope.HOUSEHOLD,
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
    account = _active_visible_account(request.user, account_id)
    form = AccountRenameForm(request.POST)
    if form.is_valid():
        account.name = form.cleaned_data["name"]
        account.save(update_fields=("name", "updated_at"))
    return redirect("account-list")


@require_POST
@never_cache
def account_share(request, account_id):
    _active_visible_account(request.user, account_id)
    _service_or_404(lambda: share_account(request.user, account_id))
    return redirect("account-list")


@require_POST
@never_cache
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
