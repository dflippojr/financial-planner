from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connections
from django.db import transaction as database_transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .auth_services import (
    InvalidOneTimeCode,
    accept_invitation,
    clear_login_failures,
    create_invitation,
    login_is_blocked,
    normalize_username,
    recover_account,
    record_login_failure,
    throttle_key,
)
from .forms import JoinForm, LoginForm, RecoveryForm, TransactionCorrectionForm, TransactionFilterForm
from .lifecycle_services import lock_actor_household
from .models import Account, Transaction


@login_not_required
@never_cache
@require_GET
def health(request):
    """Report process and database readiness without exposing application data."""
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return HttpResponse("unavailable\n", status=503, content_type="text/plain")
    return HttpResponse("ok\n", content_type="text/plain")


@require_GET
def home(request):
    return render(request, "finance/home.html", {"accounts": Account.objects.visible_to(request.user)})


@require_GET
def transaction_list(request):
    transactions = (
        Transaction.objects.visible_to(request.user)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "import_batch")
        .order_by("-transaction_date", "-pk")
    )
    form = TransactionFilterForm(request.GET or None, principal=request.user)
    if form.is_valid():
        filters = form.cleaned_data
        if filters["date_from"]:
            transactions = transactions.filter(transaction_date__gte=filters["date_from"])
        if filters["date_to"]:
            transactions = transactions.filter(transaction_date__lte=filters["date_to"])
        if filters["account"]:
            transactions = transactions.filter(account=filters["account"])
        if filters["q"]:
            transactions = transactions.filter(description__icontains=filters["q"])
        # All transactions remain uncategorized until issue #8 introduces the
        # agreed category scheme, so its sole category choice needs no query.
    elif form.is_bound:
        transactions = transactions.none()
    return render(
        request,
        "finance/transaction_list.html",
        {"filter_form": form, "transactions": transactions},
    )


def _visible_active_transaction(principal, transaction_id):
    return get_object_or_404(
        Transaction.objects.visible_to(principal)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "import_batch"),
        pk=transaction_id,
    )


@require_http_methods(["GET", "POST"])
def transaction_edit(request, transaction_id):
    financial_transaction = _visible_active_transaction(request.user, transaction_id)
    if request.method == "POST":
        form = TransactionCorrectionForm(request.POST)
        if form.is_valid():
            with database_transaction.atomic():
                # Same lock order as the lifecycle services: the household's
                # memberships, then the account, then the transaction. The
                # membership lock is what stops a concurrent removal from
                # committing between the visibility check and the save.
                person = getattr(request.user, "person", None)
                if person is not None:
                    lock_actor_household(person)
                account = get_object_or_404(
                    Account.objects.visible_to(request.user).select_for_update(),
                    pk=financial_transaction.account_id,
                )
                financial_transaction = get_object_or_404(
                    Transaction.objects.select_for_update(),
                    pk=financial_transaction.pk,
                    account=account,
                    status=Transaction.Status.ACTIVE,
                )
                form.apply(financial_transaction)
            return redirect("transaction-list")
    else:
        form = TransactionCorrectionForm.for_transaction(financial_transaction)
    return render(
        request,
        "finance/transaction_edit.html",
        {"form": form, "transaction": financial_transaction},
    )


def _authenticate_member(request, username, password, key):
    """Resolve a signed-in member for (username, password), or None.

    Only a genuine failed authentication attempt counts toward the login
    throttle: recording one for a request that was already blocked would let
    a caller reset record_login_failure's window (and clear blocked_until)
    simply by retrying, before the block is meant to expire.
    """
    if login_is_blocked(key):
        return None
    user = authenticate(request, username=username, password=password)
    if user is not None and not hasattr(user, "person"):
        user = None
    if user is None:
        record_login_failure(key)
    return user


def _redirect_target(request):
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = reverse("home")
    return target


@login_not_required
@never_cache
def sign_in(request):
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = normalize_username(form.cleaned_data["username"])
        key = throttle_key(username, request.META.get("REMOTE_ADDR"))
        user = _authenticate_member(request, username, form.cleaned_data["password"], key)
        if user is None:
            form.add_error(None, "Sign-in failed. Check your credentials and try again later.")
        else:
            clear_login_failures(key)
            login(request, user)
            # Sessions last a fixed period from sign-in. Django's default expiry is
            # relative to the last time the session was saved, so any later write to
            # it (such as CSV staging metadata) would push the expiry out again, and
            # repeated activity would keep the session alive indefinitely. An
            # absolute expiry is stored once and never moves.
            request.session.set_expiry(timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE))
            return redirect(_redirect_target(request))
    return render(request, "finance/login.html", {"form": form, "next": request.GET.get("next", "")})


@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


@never_cache
def invite(request):
    code = None
    if request.method == "POST":
        code = create_invitation(request.user.person)
    return render(
        request,
        "finance/invite.html",
        {"invitation_code": code, "invitation_ttl_hours": settings.INVITATION_TTL_HOURS},
    )


@login_not_required
@never_cache
def join(request):
    form = JoinForm(request.POST or None)
    recovery_codes = None
    if request.method == "POST" and form.is_valid():
        try:
            _user, recovery_codes = accept_invitation(
                form.cleaned_data["invitation_code"],
                form.cleaned_data["username"],
                form.cleaned_data["display_name"],
                form.cleaned_data["password1"],
            )
        except InvalidOneTimeCode:
            form.add_error(None, "The invitation could not be used.")
    return render(request, "finance/join.html", {"form": form, "recovery_codes": recovery_codes})


@login_not_required
@never_cache
def recover(request):
    form = RecoveryForm(request.POST or None)
    recovered = False
    if request.method == "POST" and form.is_valid():
        try:
            recover_account(
                form.cleaned_data["username"],
                form.cleaned_data["recovery_code"],
                form.cleaned_data["password1"],
            )
            recovered = True
        except InvalidOneTimeCode:
            form.add_error(None, "Recovery failed. Check the supplied details.")
    return render(request, "finance/recover.html", {"form": form, "recovered": recovered})
