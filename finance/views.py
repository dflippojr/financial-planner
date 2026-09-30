from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connections
from django.db import transaction as database_transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
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
from .forms import (
    CategoryNameForm,
    JoinForm,
    LoginForm,
    RecoveryForm,
    RefundLinkForm,
    TransactionCategoryForm,
    TransactionCorrectionForm,
    TransactionFilterForm,
    TransferWindowForm,
)
from .lifecycle_services import lock_actor_household
from .models import Account, Category, Person, RefundLink, Transaction, TransactionCorrectionHistory, TransferPair
from .category_services import (
    add_category,
    assign_category,
    confirm_transfer_pair,
    current_household,
    dismiss_transfer_pair,
    ensure_household_categories,
    exclusion_exists_for,
    link_refund,
    refresh_transfer_pairs,
    rename_category,
    set_transfer_window_days,
    undo_transfer_pair,
)


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
@never_cache
def home(request):
    return render(request, "finance/home.html", {"accounts": Account.objects.visible_to(request.user)})


@require_GET
@never_cache
def transaction_list(request):
    transactions = (
        Transaction.objects.visible_to(request.user)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "import_batch", "category")
        .annotate(_excluded=exclusion_exists_for(request.user))
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
        category = filters["category"]
        if category == "uncategorized":
            transactions = transactions.filter(
                Q(category__isnull=True) | Q(category__code=Category.Code.UNCATEGORIZED)
            ).exclude(_excluded=True)
        elif category == "transfer":
            transactions = transactions.filter(_excluded=True)
        elif category:
            transactions = transactions.filter(category_id=category)
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
@never_cache
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
                # History rows are inserted after the transaction lock, in
                # this same database transaction.
                person = get_object_or_404(Person, user=request.user)
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
                form.apply(financial_transaction, actor=person)
            return redirect("transaction-list")
    else:
        form = TransactionCorrectionForm.for_transaction(financial_transaction)
    correction_history = (
        TransactionCorrectionHistory.objects.visible_to(request.user)
        .filter(transaction=financial_transaction)
        .select_related("actor")
        .order_by("-recorded_at", "-pk")
    )
    return render(
        request,
        "finance/transaction_edit.html",
        {
            "form": form,
            "category_form": TransactionCategoryForm(
                principal=request.user,
                initial={"category": financial_transaction.category_id},
            ),
            "refund_form": RefundLinkForm(principal=request.user, refund=financial_transaction),
            "refund_link": RefundLink.objects.visible_to(request.user)
            .select_related("original")
            .filter(refund=financial_transaction)
            .first(),
            "transaction": financial_transaction,
            "correction_history": correction_history,
        },
    )


def _service_or_404(action):
    try:
        return action()
    except PermissionDenied as exc:
        raise Http404 from exc


@require_POST
@never_cache
def transaction_categorize(request, transaction_id):
    _visible_active_transaction(request.user, transaction_id)
    form = TransactionCategoryForm(request.POST, principal=request.user)
    if form.is_valid():
        category = form.cleaned_data["category"]
        _service_or_404(
            lambda: assign_category(request.user, transaction_id, None if category is None else category.pk)
        )
    return redirect("transaction-edit", transaction_id=transaction_id)


@require_POST
@never_cache
def transaction_link_refund(request, transaction_id):
    _visible_active_transaction(request.user, transaction_id)
    form = RefundLinkForm(request.POST, principal=request.user, refund=_visible_active_transaction(request.user, transaction_id))
    if form.is_valid():
        _service_or_404(lambda: link_refund(request.user, transaction_id, form.cleaned_data["original"].pk))
    return redirect("transaction-edit", transaction_id=transaction_id)


@require_http_methods(["GET", "POST"])
@never_cache
def category_list(request):
    person = get_object_or_404(Person, user=request.user)
    household = current_household(person)
    if household is None:
        return render(
            request,
            "finance/category_list.html",
            {"household": None, "categories": [], "add_form": CategoryNameForm(), "window_form": None},
        )
    ensure_household_categories(household)
    add_form = CategoryNameForm(request.POST if request.POST.get("action") == "add" else None)
    window_form = TransferWindowForm(
        request.POST if request.POST.get("action") == "window" else None,
        initial={"transfer_match_window_days": household.transfer_match_window_days},
    )
    if request.method == "POST" and request.POST.get("action") == "add" and add_form.is_valid():
        try:
            _service_or_404(lambda: add_category(request.user, add_form.cleaned_data["name"]))
        except ValidationError as exc:
            add_form.add_error("name", exc.messages[0] if exc.messages else "The category could not be saved.")
        else:
            return redirect("category-list")
    if request.method == "POST" and request.POST.get("action") == "window" and window_form.is_valid():
        try:
            _service_or_404(
                lambda: set_transfer_window_days(request.user, window_form.cleaned_data["transfer_match_window_days"])
            )
        except ValidationError as exc:
            window_form.add_error(
                "transfer_match_window_days",
                exc.messages[0] if exc.messages else "The match window could not be saved.",
            )
        else:
            return redirect("category-list")
    if request.method == "POST" and request.POST.get("action") == "rename":
        rename_form = CategoryNameForm(request.POST)
        if rename_form.is_valid():
            try:
                _service_or_404(
                    lambda: rename_category(
                        request.user,
                        int(request.POST.get("category_id", "0")),
                        rename_form.cleaned_data["name"],
                    )
                )
            except (ValidationError, ValueError) as exc:
                messages = getattr(exc, "messages", None)
                add_form.add_error(None, messages[0] if messages else "The category could not be renamed.")
            else:
                return redirect("category-list")
    categories = Category.objects.visible_to(request.user).order_by("name", "pk")
    return render(
        request,
        "finance/category_list.html",
        {
            "household": household,
            "categories": categories,
            "add_form": add_form if add_form is not None else CategoryNameForm(),
            "window_form": window_form,
        },
    )


@require_http_methods(["GET", "POST"])
@never_cache
def transfer_review(request):
    _service_or_404(lambda: refresh_transfer_pairs(request.user))
    if request.method == "POST":
        try:
            pair_id = int(request.POST.get("pair_id", "0"))
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
        action = request.POST.get("action")
        actions = {
            "confirm": confirm_transfer_pair,
            "dismiss": dismiss_transfer_pair,
            "undo": undo_transfer_pair,
        }
        handler = actions.get(action)
        if handler is None:
            raise Http404
        _service_or_404(lambda: handler(request.user, pair_id))
        return redirect("transfer-review")
    visible = TransferPair.objects.visible_to(request.user).select_related(
        "leg_a",
        "leg_b",
        "leg_a__account",
        "leg_b__account",
    )
    exclusions = visible.filter(status__in=(TransferPair.Status.AUTO_MARKED, TransferPair.Status.CONFIRMED)).order_by(
        "-updated_at", "-pk"
    )
    suggestions = visible.filter(status=TransferPair.Status.SUGGESTED).order_by("-updated_at", "-pk")
    return render(
        request,
        "finance/transfer_review.html",
        {"exclusions": exclusions, "suggestions": suggestions},
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
