from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import authenticate, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connections
from django.db import transaction as database_transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST, require_safe

from .auth_services import (
    InvalidOneTimeCode,
    SETUP_THROTTLE_USERNAME,
    accept_invitation,
    clear_login_failures,
    complete_member_session,
    create_invitation,
    first_member_exists,
    invitation_is_usable,
    login_is_blocked,
    normalize_username,
    recover_account,
    record_login_failure,
    seed_first_household,
    setup_code_configured,
    setup_code_matches,
    throttle_key,
    validated_username,
)
from .forms import (
    CashFlowFilterForm,
    CategoryNameForm,
    JoinForm,
    JoinGoogleForm,
    LoginForm,
    PasswordPairForm,
    ReauthPasswordForm,
    RecoveryForm,
    RefundLinkForm,
    SetupForm,
    SetupGoogleForm,
    SpendingFilterForm,
    SplitPartCategoryForm,
    SplitTransactionForm,
    TransactionCategoryForm,
    TransactionCorrectionForm,
    TransactionFilterForm,
    TransferWindowForm,
    UnsplitTransactionForm,
)
from .google_auth import (
    disconnect_google_account,
    google_signin_enabled,
    google_throttle_key,
    has_google_account,
    has_usable_google_sign_in,
    remove_member_password,
    store_google_pending,
    username_is_taken,
)
from .lifecycle_services import leave_household, lock_actor_household
from .reauth import (
    ACCOUNT_SETTINGS_ACTIONS,
    action_label,
    reauth_redirect,
    recent_auth_is_fresh,
    requires_recent_auth,
    safe_next_url,
    stamp_recent_auth,
)
from .export import export_filename, write_export_zip
from .models import (
    Account,
    Category,
    Membership,
    Person,
    PrivacyPolicyVersion,
    RecurringSeries,
    RefundLink,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
    TransferPair,
)
from .ai_views import ai_settings_context
from .policy_services import (
    accept_shown_version,
    current_policy,
    in_acceptance,
    latest_acceptance,
    decline_shown_version,
    record_onboarding_acceptance,
)
from .recurring_services import confirm_recurring_series, confirmed_totals, dismiss_recurring_series, refresh_recurring_series
from .cash_flow import (
    cash_flow_chart_data,
    date_range_presets,
    default_date_range,
    spending_by_category_report,
    spending_chart_data,
)
from .planning_services import cash_flow_with_projection
from .budget_services import dashboard_budget_summary
from .projection import DEFAULT_HORIZON
from .spending_trends import (
    category_spending_trend_report,
    category_trend_chart_data,
    spending_category_trend_report,
    spending_trend_chart_data,
)
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
    split_transaction,
    assign_split_part_category,
    unsplit_transaction,
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
    today = timezone.localdate()
    default_from, default_to = default_date_range(today)
    form = CashFlowFilterForm(request.GET or None, principal=request.user)
    if not form.is_bound:
        form = CashFlowFilterForm(
            principal=request.user,
            initial={
                "date_from": default_from,
                "date_to": default_to,
                "grouping": "month",
                "horizon": DEFAULT_HORIZON,
            },
        )
        date_from, date_to, grouping, account, scope = default_from, default_to, "month", None, ""
        horizon = DEFAULT_HORIZON
    elif form.is_valid():
        date_from = form.cleaned_data["date_from"] or default_from
        date_to = form.cleaned_data["date_to"] or default_to
        grouping = form.cleaned_data["grouping"]
        account = form.cleaned_data["account"]
        scope = form.cleaned_data["scope"]
        horizon = form.cleaned_data["horizon"] or DEFAULT_HORIZON
    else:
        date_from = date_to = grouping = account = scope = None
        horizon = DEFAULT_HORIZON
    report = None
    if date_from is not None:
        report = cash_flow_with_projection(
            request.user,
            date_from=date_from,
            date_to=date_to,
            grouping=grouping,
            account=account,
            scope=scope,
            today=today,
            horizon=horizon,
        )
    return render(
        request,
        "finance/home.html",
        {
            "filter_form": form,
            "report": report,
            "chart_data": cash_flow_chart_data(report) if report is not None else None,
            "accounts": Account.objects.visible_to(request.user),
            "budget_summary": dashboard_budget_summary(request.user, today=today),
        },
    )


def _preset_links(today, *, account=None, scope="", grouping=None, tab=None, extra_query=None):
    links = []
    for preset in date_range_presets(today):
        query = {"date_from": preset.date_from.isoformat(), "date_to": preset.date_to.isoformat()}
        if account is not None:
            query["account"] = str(account.pk)
        if scope:
            query["scope"] = scope
        if grouping:
            query["grouping"] = grouping
        if tab:
            query["tab"] = tab
        if extra_query:
            query.update(extra_query)
        links.append(SimpleNamespace(label=preset.label, url=f"?{urlencode(query)}"))
    return links


def _spending_query(*, date_from, date_to, grouping="month", account=None, scope="", tab=""):
    query = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "grouping": grouping,
    }
    if account is not None:
        query["account"] = str(account.pk)
    if scope:
        query["scope"] = scope
    if tab:
        query["tab"] = tab
    return query


def _spending_filter_state(request):
    today = timezone.localdate()
    default_from, default_to = default_date_range(today)
    form = SpendingFilterForm(request.GET or None, principal=request.user)
    tab = "trends" if request.GET.get("tab") == "trends" else "overview"
    if not form.is_bound:
        form = SpendingFilterForm(
            principal=request.user,
            initial={
                "date_from": default_from,
                "date_to": default_to,
                "grouping": "month",
            },
        )
        return form, default_from, default_to, "month", None, "", tab, today
    if form.is_valid():
        return (
            form,
            form.cleaned_data["date_from"] or default_from,
            form.cleaned_data["date_to"] or default_to,
            form.cleaned_data.get("grouping") or "month",
            form.cleaned_data["account"],
            form.cleaned_data["scope"],
            tab,
            today,
        )
    return form, None, None, None, None, None, tab, today


@require_GET
@never_cache
def spending_by_category(request):
    form, date_from, date_to, grouping, account, scope, tab, today = _spending_filter_state(request)
    report = None
    trend_report = None
    chart_data = None
    if date_from is not None:
        if tab == "trends":
            trend_report = spending_category_trend_report(
                request.user,
                date_from=date_from,
                date_to=date_to,
                grouping=grouping,
                account=account,
                scope=scope,
                today=today,
            )
            report = trend_report
            chart_data = spending_trend_chart_data(trend_report)
        else:
            report = spending_by_category_report(
                request.user,
                date_from=date_from,
                date_to=date_to,
                account=account,
                scope=scope,
                grouping=grouping,
            )
            chart_data = spending_chart_data(report)
    query = (
        _spending_query(
            date_from=date_from,
            date_to=date_to,
            grouping=grouping,
            account=account,
            scope=scope,
        )
        if date_from is not None
        else {}
    )
    return render(
        request,
        "finance/spending.html",
        {
            "filter_form": form,
            "report": report,
            "trend_report": trend_report,
            "chart_data": chart_data,
            "active_tab": tab,
            "overview_url": f"{reverse('spending-by-category')}?{urlencode(query)}" if query else reverse("spending-by-category"),
            "trends_url": (
                f"{reverse('spending-by-category')}?{urlencode({**query, 'tab': 'trends'})}"
                if query
                else f"{reverse('spending-by-category')}?tab=trends"
            ),
            "presets": (
                _preset_links(today, account=account, scope=scope, grouping=grouping, tab=tab if tab == "trends" else None)
                if date_from is not None
                else ()
            ),
        },
    )


def _visible_spending_category(principal, category_id):
    if category_id is None:
        return SimpleNamespace(key="uncategorized", name="Uncategorized")
    category = (
        Category.objects.visible_to(principal)
        .exclude(code=Category.Code.TRANSFER)
        .filter(pk=category_id)
        .first()
    )
    if category is None:
        raise Http404()
    if category.code == Category.Code.UNCATEGORIZED:
        return SimpleNamespace(key="uncategorized", name="Uncategorized")
    return SimpleNamespace(key=str(category.pk), name=category.name)


@require_GET
@never_cache
def spending_category_detail(request, category_id=None):
    category = _visible_spending_category(request.user, category_id)
    form, date_from, date_to, grouping, account, scope, _tab, today = _spending_filter_state(request)
    report = None
    chart_data = None
    if date_from is not None:
        report = category_spending_trend_report(
            request.user,
            category_key=category.key,
            category_name=category.name,
            date_from=date_from,
            date_to=date_to,
            grouping=grouping,
            account=account,
            scope=scope,
            today=today,
        )
        chart_data = category_trend_chart_data(report)
    return render(
        request,
        "finance/spending_category.html",
        {
            "filter_form": form,
            "report": report,
            "chart_data": chart_data,
            "category": category,
            "presets": (
                _preset_links(today, account=account, scope=scope, grouping=grouping)
                if date_from is not None
                else ()
            ),
        },
    )


def _apply_transaction_filters(transactions, filters, principal):
    if filters["date_from"]:
        transactions = transactions.filter(transaction_date__gte=filters["date_from"])
    if filters["date_to"]:
        transactions = transactions.filter(transaction_date__lte=filters["date_to"])
    if filters["account"]:
        transactions = transactions.filter(account=filters["account"])
    if filters["scope"]:
        transactions = transactions.filter(account__scope=filters["scope"])
    if filters["q"]:
        transactions = transactions.filter(description__icontains=filters["q"])
    category = filters["category"]
    if category == "uncategorized":
        # Match the spending view: a category the viewer can no longer see
        # (for example after leaving a household) counts as uncategorized.
        return transactions.filter(
            Q(category__isnull=True)
            | Q(category__code=Category.Code.UNCATEGORIZED)
            | ~Q(category__in=Category.objects.visible_to(principal))
        ).exclude(_excluded=True).exclude(category_source=Transaction.CategorySource.SPLIT)
    if category == "transfer":
        return transactions.filter(_excluded=True)
    if category:
        return transactions.filter(
            Q(category_id=category) | Q(splits__category_id=category)
        ).exclude(_excluded=True).distinct()
    return transactions


@require_GET
@never_cache
def transaction_list(request):
    from .ai_services import member_has_ai
    from .category_suggestion_services import (
        pending_suggestions_for,
        proposed_rule_from_accepts,
        suggestions_pending,
    )

    person = Person.objects.filter(user=request.user).first()
    transactions = (
        Transaction.objects.visible_to(request.user)
        .filter(status=Transaction.Status.ACTIVE)
        .select_related("account", "import_batch", "category")
        .prefetch_related("splits__category")
        .annotate(_excluded=exclusion_exists_for(request.user))
        .order_by("-transaction_date", "-pk")
    )
    form = TransactionFilterForm(request.GET or None, principal=request.user)
    if form.is_valid():
        transactions = _apply_transaction_filters(transactions, form.cleaned_data, request.user)
    elif form.is_bound:
        transactions = transactions.none()
    show_ai = bool(person and member_has_ai(person))
    uncategorized_filter = form.is_valid() and form.cleaned_data.get("category") == "uncategorized"
    suggestions = pending_suggestions_for(person, transactions) if show_ai else {}
    filter_hidden = []
    if form.is_valid():
        for name, value in form.cleaned_data.items():
            if value in (None, ""):
                continue
            if name == "account":
                filter_hidden.append((name, str(value.pk)))
            else:
                filter_hidden.append((name, str(value)))
    return render(
        request,
        "finance/transaction_list.html",
        {
            "filter_form": form,
            "transactions": transactions,
            "show_ai_suggestions": show_ai,
            "uncategorized_filter": uncategorized_filter,
            "suggestions_pending": show_ai and suggestions_pending(person),
            "shown_suggestions": list(suggestions.values()),
            "suggestions": suggestions,
            "proposed_rule": proposed_rule_from_accepts(person) if show_ai else None,
            "list_query": request.get_full_path(),
            "filter_hidden": filter_hidden,
        },
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
                try:
                    form.apply(financial_transaction, actor=person)
                except ValidationError as exc:
                    form.add_error("amount", _first_message(exc, "The amount could not be saved."))
                    return _render_transaction_edit(request, financial_transaction, form=form)
            _service_or_404(lambda: refresh_transfer_pairs(request.user))
            return redirect("transaction-list")
    else:
        form = TransactionCorrectionForm.for_transaction(financial_transaction)
    return _render_transaction_edit(request, financial_transaction, form=form)


def _render_transaction_edit(request, financial_transaction, *, form=None, refund_form=None, split_form=None):
    if form is None:
        form = TransactionCorrectionForm.for_transaction(financial_transaction)
    correction_history = (
        TransactionCorrectionHistory.objects.visible_to(request.user)
        .filter(transaction=financial_transaction)
        .select_related("actor")
        .order_by("-recorded_at", "-pk")
    )
    splits = list(financial_transaction.splits.select_related("category").order_by("position"))
    part_forms = []
    for part in splits:
        part_forms.append(
            (
                part,
                SplitPartCategoryForm(
                    principal=request.user,
                    prefix=f"part{part.pk}",
                    initial={"category": part.category_id},
                ),
            )
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
            "refund_form": refund_form
            or RefundLinkForm(principal=request.user, refund=financial_transaction),
            "refund_link": RefundLink.objects.visible_to(request.user)
            .select_related("original")
            .filter(refund=financial_transaction)
            .first(),
            "split_form": split_form
            or SplitTransactionForm(principal=request.user, transaction=financial_transaction),
            "unsplit_form": UnsplitTransactionForm(principal=request.user),
            "part_forms": part_forms,
            "is_split": financial_transaction.category_source == Transaction.CategorySource.SPLIT,
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
        try:
            _service_or_404(
                lambda: assign_category(request.user, transaction_id, None if category is None else category.pk)
            )
        except ValidationError:
            pass
    return redirect("transaction-edit", transaction_id=transaction_id)


@require_POST
@never_cache
def transaction_link_refund(request, transaction_id):
    financial_transaction = _visible_active_transaction(request.user, transaction_id)
    form = RefundLinkForm(request.POST, principal=request.user, refund=financial_transaction)
    if form.is_valid():
        try:
            _service_or_404(
                lambda: link_refund(
                    request.user,
                    transaction_id,
                    form.cleaned_data["original"].pk,
                    None if form.cleaned_data.get("original_part") is None else form.cleaned_data["original_part"].pk,
                )
            )
        except ValidationError as exc:
            form.add_error(None, _first_message(exc, "The refund could not be linked."))
        else:
            return redirect("transaction-edit", transaction_id=transaction_id)
    return _render_transaction_edit(request, financial_transaction, refund_form=form)


@require_POST
@never_cache
def transaction_split(request, transaction_id):
    financial_transaction = _visible_active_transaction(request.user, transaction_id)
    form = SplitTransactionForm(request.POST, principal=request.user, transaction=financial_transaction)
    if form.is_valid():
        try:
            _service_or_404(
                lambda: split_transaction(
                    request.user,
                    transaction_id,
                    form.parts_payload(),
                    form.refund_assignments(),
                )
            )
        except ValidationError as exc:
            form.add_error(None, _first_message(exc, "The transaction could not be split."))
        else:
            return redirect("transaction-edit", transaction_id=transaction_id)
    return _render_transaction_edit(request, financial_transaction, split_form=form)


@require_POST
@never_cache
def transaction_unsplit(request, transaction_id):
    _visible_active_transaction(request.user, transaction_id)
    form = UnsplitTransactionForm(request.POST, principal=request.user)
    if form.is_valid():
        category = form.cleaned_data["category"]
        _service_or_404(
            lambda: unsplit_transaction(
                request.user,
                transaction_id,
                None if category is None else category.pk,
            )
        )
    return redirect("transaction-edit", transaction_id=transaction_id)


@require_POST
@never_cache
def transaction_split_part_category(request, transaction_id, part_id):
    financial_transaction = _visible_active_transaction(request.user, transaction_id)
    part = get_object_or_404(TransactionSplit, pk=part_id, transaction=financial_transaction)
    form = SplitPartCategoryForm(request.POST, principal=request.user, prefix=f"part{part.pk}")
    if form.is_valid():
        _service_or_404(
            lambda: assign_split_part_category(request.user, part.pk, form.cleaned_data["category"].pk)
        )
    return redirect("transaction-edit", transaction_id=transaction_id)


def _first_message(exc, fallback):
    messages = getattr(exc, "messages", None)
    return messages[0] if messages else fallback


def _handle_add_category(request, forms):
    add_form = forms["add_form"]
    if not add_form.is_valid():
        return False
    try:
        _service_or_404(lambda: add_category(request.user, add_form.cleaned_data["name"]))
    except ValidationError as exc:
        add_form.add_error("name", _first_message(exc, "The category could not be saved."))
        return False
    return True


def _handle_transfer_window(request, forms):
    window_form = forms["window_form"]
    if not window_form.is_valid():
        return False
    days = window_form.cleaned_data["transfer_match_window_days"]
    try:
        _service_or_404(lambda: set_transfer_window_days(request.user, days))
    except ValidationError as exc:
        window_form.add_error("transfer_match_window_days", _first_message(exc, "The match window could not be saved."))
        return False
    return True


def _handle_rename_category(request, forms):
    rename_form = CategoryNameForm(request.POST)
    if not rename_form.is_valid():
        return False
    try:
        _service_or_404(
            lambda: rename_category(
                request.user,
                int(request.POST.get("category_id", "0")),
                rename_form.cleaned_data["name"],
            )
        )
    except (ValidationError, ValueError) as exc:
        forms["rename_error"] = _first_message(exc, "The category could not be renamed.")
        return False
    return True


_CATEGORY_ACTIONS = {
    "add": _handle_add_category,
    "window": _handle_transfer_window,
    "rename": _handle_rename_category,
}


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
    action = request.POST.get("action") if request.method == "POST" else None
    add_form = CategoryNameForm(request.POST if action == "add" else None)
    window_form = TransferWindowForm(
        request.POST if action == "window" else None,
        initial={"transfer_match_window_days": household.transfer_match_window_days},
    )
    forms = {"add_form": add_form, "window_form": window_form, "rename_error": None}
    handler = _CATEGORY_ACTIONS.get(action)
    if handler is not None and handler(request, forms):
        return redirect("category-list")
    categories = Category.objects.visible_to(request.user).order_by("name", "pk")
    return render(
        request,
        "finance/category_list.html",
        {
            "household": household,
            "categories": categories,
            "add_form": add_form,
            "window_form": window_form,
            "rename_error": forms["rename_error"],
        },
    )


@require_http_methods(["GET", "POST"])
@never_cache
def transfer_review(request):
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
        _service_or_404(lambda: refresh_transfer_pairs(request.user))
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


def _money_display(minor, currency):
    return f"{Decimal(minor) / Decimal(100):,.2f} {currency}"


def _recurring_chart_data(confirmed, monthly_minor, annual_minor, monthly_display, annual_display):
    return {
        "monthly_minor": monthly_minor,
        "annual_minor": annual_minor,
        "monthly_display": monthly_display,
        "annual_display": annual_display,
        "series": [
            {
                "name": series.display_name,
                "monthly_minor": series.monthly_minor,
                "annual_minor": series.annual_minor,
                "monthly_display": series.monthly_display,
                "annual_display": series.annual_display,
            }
            for series in confirmed
        ],
    }


def _handle_recurring_post(request):
    try:
        series_id = int(request.POST.get("series_id", "0"))
    except (TypeError, ValueError) as exc:
        raise Http404 from exc
    actions = {
        "confirm": confirm_recurring_series,
        "dismiss": dismiss_recurring_series,
    }
    handler = actions.get(request.POST.get("action"))
    if handler is None:
        raise Http404
    _service_or_404(lambda: handler(request.user, series_id))
    _service_or_404(lambda: refresh_recurring_series(request.user))
    return redirect("recurring-review")


@require_http_methods(["GET", "POST"])
@never_cache
def recurring_review(request):
    if request.method == "POST":
        return _handle_recurring_post(request)
    _service_or_404(lambda: refresh_recurring_series(request.user))
    visible = (
        RecurringSeries.objects.visible_to(request.user)
        .prefetch_related("members__transaction")
        .order_by("display_name", "pk")
    )
    confirmed = [
        series
        for series in visible
        if series.status == RecurringSeries.Status.CONFIRMED and series.is_active
    ]
    suggestions = [
        series
        for series in visible
        if series.status in (RecurringSeries.Status.POSSIBLE, RecurringSeries.Status.SUGGESTED)
    ]
    monthly_minor, annual_minor = confirmed_totals(confirmed)
    confirmed.sort(key=lambda series: (-series.monthly_minor, series.display_name, series.pk))
    currency = confirmed[0].currency if confirmed else "USD"
    monthly_display = _money_display(monthly_minor, currency)
    annual_display = _money_display(annual_minor, currency)
    return render(
        request,
        "finance/recurring_review.html",
        {
            "suggestions": suggestions,
            "confirmed": confirmed,
            "monthly_display": monthly_display,
            "annual_display": annual_display,
            "monthly_minor": monthly_minor,
            "annual_minor": annual_minor,
            "chart_data": _recurring_chart_data(
                confirmed,
                monthly_minor,
                annual_minor,
                monthly_display,
                annual_display,
            ),
        },
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
    return safe_next_url(request, request.POST.get("next", ""))


def _complete_member_session(request, user):
    complete_member_session(request, user)


SETUP_FAILED = "Setup could not be completed. Check the setup code and try again later."
LAST_SIGN_IN_METHOD = "Keep at least one sign-in method."


def _privacy_policy_page_context(*, wide=True):
    return {"privacy_policy": current_policy(), "wide_card": wide}


@login_not_required
@never_cache
def sign_in(request):
    if not first_member_exists():
        return redirect("setup")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = normalize_username(form.cleaned_data["username"])
        key = throttle_key(username, request.META.get("REMOTE_ADDR"))
        user = _authenticate_member(request, username, form.cleaned_data["password"], key)
        if user is None:
            form.add_error(None, "Sign-in failed. Check your credentials and try again later.")
        else:
            clear_login_failures(key)
            _complete_member_session(request, user)
            return redirect(_redirect_target(request))
    return render(
        request,
        "finance/login.html",
        {"form": form, "next": request.GET.get("next", ""), **_privacy_policy_page_context(wide=False)},
    )


def _google_oauth_login(request):
    from allauth.socialaccount.providers.google.views import oauth2_login

    return oauth2_login(request)


def _start_google_join(request, google_form):
    if not google_signin_enabled():
        raise Http404()
    if not google_form.is_valid():
        return None
    invitation_code = google_form.cleaned_data["invitation_code"]
    username = validated_username(google_form.cleaned_data["username"])
    if not invitation_is_usable(invitation_code) or username_is_taken(username):
        google_form.add_error(None, "The invitation could not be used.")
        return None
    store_google_pending(
        request,
        {
            "intent": "join",
            "invitation_code": invitation_code,
            "username": google_form.cleaned_data["username"],
            "display_name": google_form.cleaned_data["display_name"],
            "accept_privacy_policy": google_form.cleaned_data.get("accept_privacy_policy", False),
            "privacy_policy_version": google_form.cleaned_data.get("privacy_policy_version"),
        },
    )
    return _google_oauth_login(request)


def _start_google_setup(request, google_form):
    if not google_signin_enabled():
        raise Http404()
    key = throttle_key(SETUP_THROTTLE_USERNAME, request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        google_form.add_error(None, SETUP_FAILED)
        return None
    if not setup_code_matches(request.POST.get("setup_code", "")):
        record_login_failure(key)
        google_form.add_error(None, SETUP_FAILED)
        return None
    if not google_form.is_valid():
        return None
    username = validated_username(google_form.cleaned_data["username"])
    if username_is_taken(username):
        google_form.add_error(None, SETUP_FAILED)
        return None
    store_google_pending(
        request,
        {
            "intent": "setup",
            "username": google_form.cleaned_data["username"],
            "display_name": google_form.cleaned_data["display_name"],
            "household_name": google_form.cleaned_data["household_name"],
            "accept_privacy_policy": google_form.cleaned_data.get("accept_privacy_policy", False),
            "privacy_policy_version": google_form.cleaned_data.get("privacy_policy_version"),
        },
    )
    return _google_oauth_login(request)


def _complete_password_setup(request, form):
    key = throttle_key(SETUP_THROTTLE_USERNAME, request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        form.add_error(None, SETUP_FAILED)
        return None
    if not setup_code_matches(request.POST.get("setup_code", "")):
        record_login_failure(key)
        form.add_error(None, SETUP_FAILED)
        return None
    if not form.is_valid():
        return None
    try:
        user, recovery_codes = seed_first_household(
            form.cleaned_data["username"],
            form.cleaned_data["display_name"],
            form.cleaned_data["household_name"],
            form.cleaned_data["password1"],
        )
    except ValueError:
        raise Http404() from None
    clear_login_failures(key)
    _complete_member_session(request, user)
    record_onboarding_acceptance(
        user.person,
        form.cleaned_data.get("accept_privacy_policy", False),
        form.cleaned_data.get("privacy_policy_version"),
    )
    return recovery_codes


def _account_connect_google(request):
    if not google_signin_enabled():
        raise Http404()
    post = request.POST.copy()
    post["process"] = "connect"
    request.POST = post
    return _google_oauth_login(request)


def _account_disconnect_google(user):
    if not disconnect_google_account(user):
        return LAST_SIGN_IN_METHOD
    return None


def _account_add_password(request, password_form):
    if not password_form.is_valid():
        return password_form, None
    request.user.set_password(password_form.cleaned_data["password1"])
    request.user.save(update_fields=("password",))
    update_session_auth_hash(request, request.user)
    password_form = PasswordPairForm()
    password_form.existing_user = request.user
    return password_form, None


def _account_remove_password(request):
    if not remove_member_password(request.user):
        return LAST_SIGN_IN_METHOD
    request.user.refresh_from_db()
    update_session_auth_hash(request, request.user)
    return None


@login_not_required
@never_cache
@require_POST
def start_google_sign_in(request):
    if not google_signin_enabled():
        raise Http404()
    if not first_member_exists():
        return redirect("setup")
    key = google_throttle_key(request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        return render(
            request,
            "finance/login.html",
            {
                "form": LoginForm(),
                "next": request.POST.get("next", ""),
                "auth_error": "Sign-in failed. Check your credentials and try again later.",
                **_privacy_policy_page_context(wide=False),
            },
        )
    store_google_pending(request, {"intent": "login"})
    return _google_oauth_login(request)


@login_not_required
@never_cache
@require_http_methods(["GET", "POST"])
def google_oauth_login(request):
    if not google_signin_enabled():
        raise Http404()
    if (
        request.method == "POST"
        and request.POST.get("process") == "connect"
        and request.user.is_authenticated
        and not recent_auth_is_fresh(request)
    ):
        return reauth_redirect(request, "connect-google", reverse("account-settings"))
    return _google_oauth_login(request)


@login_not_required
@never_cache
@require_GET
def google_oauth_callback(request):
    if not google_signin_enabled():
        raise Http404()
    from allauth.socialaccount.providers.google.views import oauth2_callback

    return oauth2_callback(request)


@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


@never_cache
@requires_recent_auth("invite")
def invite(request):
    person = get_object_or_404(Person, user=request.user)
    household = current_household(person)
    code = None
    if request.method == "POST":
        code = create_invitation(person)
    members = []
    if household is not None:
        members = list(
            Membership.objects.filter(household=household, ended_at__isnull=True)
            .select_related("person")
            .order_by("person__display_name", "pk")
        )
    return render(
        request,
        "finance/invite.html",
        {
            "invitation_code": code,
            "invitation_ttl_hours": settings.INVITATION_TTL_HOURS,
            "household": household,
            "members": members,
        },
    )


@login_not_required
@never_cache
def join(request):
    form = JoinForm()
    google_form = JoinGoogleForm()
    recovery_codes = None
    if request.method == "POST" and request.POST.get("intent") == "google":
        google_form = JoinGoogleForm(request.POST)
        started = _start_google_join(request, google_form)
        if started is not None:
            return started
    elif request.method == "POST":
        form = JoinForm(request.POST)
        if form.is_valid():
            try:
                _user, recovery_codes = accept_invitation(
                    form.cleaned_data["invitation_code"],
                    form.cleaned_data["username"],
                    form.cleaned_data["display_name"],
                    form.cleaned_data["password1"],
                )
            except InvalidOneTimeCode:
                form.add_error(None, "The invitation could not be used.")
            else:
                record_onboarding_acceptance(
                    _user.person,
                    form.cleaned_data.get("accept_privacy_policy", False),
                    form.cleaned_data.get("privacy_policy_version"),
                )
    return render(
        request,
        "finance/join.html",
        {
            "form": form,
            "google_form": google_form,
            "recovery_codes": recovery_codes,
            **_privacy_policy_page_context(),
        },
    )


@login_not_required
@never_cache
@require_http_methods(["GET", "POST"])
def setup(request):
    if first_member_exists():
        raise Http404()
    configured = setup_code_configured()
    form = SetupForm()
    google_form = SetupGoogleForm()
    recovery_codes = None
    if not configured:
        return render(
            request,
            "finance/setup.html",
            {
                "form": None,
                "google_form": None,
                "recovery_codes": None,
                "setup_configured": False,
                **_privacy_policy_page_context(),
            },
        )
    if request.method == "POST" and request.POST.get("intent") == "google":
        google_form = SetupGoogleForm(request.POST)
        started = _start_google_setup(request, google_form)
        if started is not None:
            return started
    elif request.method == "POST":
        form = SetupForm(request.POST)
        created_codes = _complete_password_setup(request, form)
        if created_codes is not None:
            recovery_codes = created_codes
    return render(
        request,
        "finance/setup.html",
        {
            "form": form,
            "google_form": google_form,
            "recovery_codes": recovery_codes,
            "setup_configured": True,
            **_privacy_policy_page_context(),
        },
    )


@never_cache
@requires_recent_auth("account-settings", action_from_post=ACCOUNT_SETTINGS_ACTIONS, form_url_name="account-settings")
def account_settings(request):
    password_form = PasswordPairForm()
    password_form.existing_user = request.user
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "connect-google":
            return _account_connect_google(request)
        if action == "disconnect-google":
            error = _account_disconnect_google(request.user)
        elif action == "add-password":
            password_form = PasswordPairForm(request.POST)
            password_form.existing_user = request.user
            password_form, error = _account_add_password(request, password_form)
        elif action == "remove-password":
            error = _account_remove_password(request)
    return render(
        request,
        "finance/account_settings.html",
        {
            "password_form": password_form,
            "has_google": has_google_account(request.user),
            "has_password": request.user.has_usable_password(),
            "error": error,
        },
    )


@never_cache
def settings_data(request):
    return render(request, "finance/settings_data.html")


@never_cache
def settings_ai(request):
    person = getattr(request.user, "person", None)
    if request.method == "POST" and request.POST.get("action") == "accept-privacy-policy":
        if person is not None:
            accept_shown_version(person, request.POST.get("version"))
    policy = current_policy()
    acceptance = latest_acceptance(person) if person is not None else None
    return render(
        request,
        "finance/settings_ai.html",
        {
            "privacy_policy": policy,
            "privacy_in_acceptance": in_acceptance(person) if person is not None else False,
            "privacy_acceptance": acceptance,
            **ai_settings_context(person),
        },
    )


def _account_export_zip(request):
    person = get_object_or_404(Person, user=request.user)
    payload = write_export_zip(person)
    filename = export_filename(timezone.localdate())
    response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@require_POST
@never_cache
@requires_recent_auth("export-data", form_url_name="settings-data")
def account_export(request):
    return _account_export_zip(request)


REAUTH_FAILED = "Confirmation failed. Try again later."
# Google accepts only none, consent, and select_account for prompt. It sends
# auth_time only to published, verified apps that ask for it through claims;
# without it the callback checks that the ID token was just issued (iat).
GOOGLE_REAUTH_AUTH_PARAMS = urlencode(
    {
        "prompt": "select_account",
        "max_age": "0",
        "claims": '{"id_token":{"auth_time":{"essential":true}}}',
    }
)


def _reauth_context(request, form, auth_error=None):
    next_url = safe_next_url(request, request.POST.get("next") or request.GET.get("next", ""))
    action = request.POST.get("action") or request.GET.get("action", "")
    return {
        "form": form,
        "next": next_url,
        "action": action,
        "action_label": action_label(action),
        "show_password": request.user.has_usable_password(),
        "show_google": has_usable_google_sign_in(request.user),
        "auth_error": auth_error,
        "auth_card_layout": True,
    }


def _render_reauth(request, form=None, auth_error=None):
    if form is None:
        form = ReauthPasswordForm()
    return render(request, "finance/reauth.html", _reauth_context(request, form, auth_error))


@never_cache
@require_http_methods(["GET", "POST"])
def reauth(request):
    form = ReauthPasswordForm(request.POST or None)
    if request.method != "POST":
        return _render_reauth(request, form)
    if not request.user.has_usable_password():
        return _render_reauth(request, ReauthPasswordForm(), REAUTH_FAILED)
    key = throttle_key(request.user.username, request.META.get("REMOTE_ADDR"))
    user = _authenticate_member(request, request.user.username, request.POST.get("password", ""), key)
    if user is None or user.pk != request.user.pk:
        form.add_error(None, REAUTH_FAILED)
        return _render_reauth(request, form)
    clear_login_failures(key)
    stamp_recent_auth(request)
    return redirect(safe_next_url(request, request.POST.get("next", "")))


@require_POST
@never_cache
def start_google_reauth(request):
    if not has_usable_google_sign_in(request.user):
        raise Http404()
    key = google_throttle_key(request.META.get("REMOTE_ADDR"))
    if login_is_blocked(key):
        return _render_reauth(request, auth_error=REAUTH_FAILED)
    next_url = safe_next_url(request, request.POST.get("next", ""))
    store_google_pending(request, {"intent": "reauth", "next": next_url})
    request.session["reauth_next"] = next_url
    query = request.GET.copy()
    query["auth_params"] = GOOGLE_REAUTH_AUTH_PARAMS
    request.GET = query
    return _google_oauth_login(request)


@require_POST
@never_cache
@requires_recent_auth("leave-household", form_url_name="invite")
def leave_household_view(request):
    try:
        leave_household(request.user)
    except PermissionDenied:
        raise Http404() from None
    return redirect("home")


@login_not_required
@require_safe
@never_cache
def privacy_policy(request, version=None):
    if version is None:
        policy = current_policy()
    else:
        policy = get_object_or_404(PrivacyPolicyVersion, version=version)
    current = current_policy()
    return render(
        request,
        "finance/privacy_policy.html",
        {
            "privacy_policy": policy,
            "wide_card": True,
            "is_current_policy": policy.pk == current.pk,
        },
    )


@require_POST
@never_cache
def privacy_policy_respond(request):
    person = get_object_or_404(Person, user=request.user)
    action = request.POST.get("action")
    if action == "accept":
        if not accept_shown_version(person, request.POST.get("version")):
            return redirect("privacy-policy")
    elif action == "decline":
        decline_shown_version(person, request.POST.get("version"))
    return redirect(safe_next_url(request, request.POST.get("next", "")))


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
