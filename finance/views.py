from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connections
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

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
from .forms import JoinForm, LoginForm, RecoveryForm
from .models import Account


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
