"""Home dashboard, the More menu, and spending by category."""

from types import SimpleNamespace
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_not_required  # pyright: ignore[reportAttributeAccessIssue] - pyright's Django stubs lack login_not_required (passkey_views is baselined for the same)
from django.db import DatabaseError, connections
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .notice_dismissal import notice_context
from .access import visible_rows
from .forms import (
    CashFlowFilterForm,
    ScenarioChangeForm,
    SpendingFilterForm,
)
from .weekly_digest import WEEK_CURRENT, WEEK_LAST, WEEKS, build_weekly_digest
from .models import (
    Account,
    Category,
    PlannedItem,
    RecurringSeries,
)
from .cash_flow import (
    cash_flow_chart_data,
    date_range_presets,
    default_date_range,
    home_default_date_range,
    spending_by_category_report,
    spending_chart_data,
)
from .planning_services import cash_flow_with_projection, visible_projection_inputs
from .budget_services import dashboard_budget_summary
from .context_processors import navigation
from .alert_services import unread_alert_count
from .import_due import due_import_accounts, import_due_link, last_covered_display
from .projection import DEFAULT_HORIZON, SOURCE_PLANNED, SOURCE_SERIES
from .scenario import (
    MAX_CHANGES,
    describe_change,
    encode_change,
    encode_changes,
    make_this_real_url,
    parse_scenario_tokens,
    visible_scenario_changes,
)
from .simplefin_schedule import validate_sync_schedule
from .spending_trends import (
    category_spending_trend_report,
    category_trend_chart_data,
    spending_category_trend_report,
    spending_trend_chart_data,
)
from .tag_services import (
    selected_tag,
)


@login_not_required
@never_cache
@require_GET
def health(request):
    """Report process and database readiness without exposing application data.

    The X-App-Revision header carries the image's short git SHA, so the
    post-deploy check can confirm which commit is serving. A schedule that
    compose's config-check refuses adds a line naming it: app does not wait
    for that check, so it stays up and healthy while background is not started.
    """
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        status, body = 503, "unavailable\n"
    else:
        status, body = 200, "ok\n"
    try:
        validate_sync_schedule()
    except ValueError as error:
        body += f"background not started: {error} (docker compose logs config-check)\n"
    response = HttpResponse(body, status=status, content_type="text/plain")
    response["X-App-Revision"] = settings.GIT_REVISION
    return response



def _cash_flow_pairs(*, date_from, date_to, grouping, horizon, account, scope, tag, changes):
    pairs = [
        ("date_from", date_from.isoformat()),
        ("date_to", date_to.isoformat()),
        ("grouping", grouping),
        ("horizon", str(horizon)),
    ]
    if account is not None:
        pairs.append(("account", str(account.pk)))
    if scope:
        pairs.append(("scope", scope))
    if tag is not None:
        pairs.append(("tag", str(tag.pk)))
    pairs.extend(("sc", token) for token in encode_changes(changes))
    return pairs



def _scenario_choice_querysets(principal, inputs):
    planned_ids = [item.source_id for item in inputs if item.source == SOURCE_PLANNED]
    series_ids = [item.source_id for item in inputs if item.source == SOURCE_SERIES]
    planned = visible_rows(PlannedItem, principal).filter(pk__in=planned_ids).order_by("name", "pk")
    # The ids come from the authorized projection inputs, which may include a
    # household bill another member confirmed, so they are not limited to the
    # viewer's own series here.
    series = RecurringSeries.objects.filter(pk__in=series_ids).order_by("display_name", "pk")
    return planned, series



def _home_attention_items(principal, budget_summary, *, today):
    """Phone "Needs attention" rows: unread alerts, budgets over or near their limit, then imports due."""
    items = []
    unread = unread_alert_count(principal)
    if unread:
        items.append(
            SimpleNamespace(
                title=f"{unread} unread alert{'s' if unread != 1 else ''}",
                detail="Open your alerts",
                url=reverse("alert-list"),
                icon="unread",
            )
        )
    for card in budget_summary.attention_cards:
        if card.over_budget:
            title, detail = f"{card.name} is over budget", f"{card.spent_display} of {card.amount_display}"
        else:
            title, detail = f"{card.name} is close to its limit", f"{card.spent_display} of {card.amount_display}"
        items.append(SimpleNamespace(title=title, detail=detail, url=reverse("budgets"), icon="warning"))
    for account in due_import_accounts(visible_rows(Account, principal), today=today):
        items.append(
            SimpleNamespace(
                title=f"Import {account.name}",
                detail=f"Last covered {last_covered_display(account.last_covered)}",
                url=import_due_link(account),
                icon="warning",
            )
        )
    return items



def _home_digest(principal, query, week, *, today):
    """The "This week" card: the digest plus switch links that keep the other Home filters."""
    if week not in WEEKS:
        week = WEEK_CURRENT
    digest = build_weekly_digest(principal, today=today, week=week)
    options = []
    for label, key in (("This week", WEEK_CURRENT), ("Last week", WEEK_LAST)):
        params = query.copy()
        if key != WEEK_CURRENT:
            params["digest_week"] = key
        encoded = params.urlencode()
        url = f"?{encoded}#weekly-digest" if encoded else "?#weekly-digest"
        options.append(SimpleNamespace(label=label, url=url, current=key == week))
    return SimpleNamespace(
        digest=digest,
        label="This week" if week == WEEK_CURRENT else "Last week",
        options=options,
        groups=[
            SimpleNamespace(
                key="new-series",
                title="New recurring charges",
                items=digest.new_series,
                see_all_url=f"{reverse('recurring-review')}#recurring-suggestions",
            ),
            SimpleNamespace(
                key="price-changes",
                title="Price changes",
                items=digest.price_changes,
                see_all_url=f"{reverse('recurring-review')}#recurring-attention",
            ),
            SimpleNamespace(
                key="large-transactions",
                title="Large transactions",
                items=digest.large_transactions,
                # The transaction list cannot repeat the digest's transfer exclusions,
                # so the extra rows expand in place instead of linking there.
                see_all_url=None,
                settings_url=None if digest.large_threshold_display else reverse("settings-alerts"),
            ),
        ],
    )



HOME_RANGES = (("This month", "this-month"), ("3 months", "last-3-months"), ("12 months", "last-12-months"))



def _home_range_options(today, *, date_from, date_to, explicit, pair_kwargs):
    """Range control links for Home: each keeps the other filters and the scenario."""
    presets = {preset.key: preset for preset in date_range_presets(today)}
    options = []
    for label, key in HOME_RANGES:
        preset = presets[key]
        pairs = _cash_flow_pairs(date_from=preset.date_from, date_to=preset.date_to, **pair_kwargs)
        current = (preset.date_from, preset.date_to) == (date_from, date_to)
        options.append(SimpleNamespace(label=label, url=f"?{urlencode(pairs)}", current=current))
    custom = explicit and date_from is not None and not any(option.current for option in options)
    options.append(SimpleNamespace(label="Custom", url="#cash-flow-filters", current=custom, opens_filters=True))
    return options



def _home_filter_count(*, grouping, horizon, account, scope, tag):
    """Filters set away from their defaults; the range control shows the dates."""
    return sum(
        (
            account is not None,
            tag is not None,
            bool(scope),
            bool(grouping) and grouping != "month",
            horizon != DEFAULT_HORIZON,
        )
    )



def _show_effective_dates(form, date_from, date_to):
    """Fill blank date inputs of a bound form with the dates the report used.

    A link such as ?grouping=week binds the form without dates; its inputs then show
    This month instead of empty fields (#700). Submitted dates are never replaced.
    """
    data = form.data.copy()
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if not data.get(name):
            data[name] = value.isoformat()
    form.data = data



@require_GET
@never_cache
def home(request):
    today = timezone.localdate()
    # Home opens on This month, one of its range buttons (#700); other pages keep
    # default_date_range().
    default_from, default_to = home_default_date_range(today)
    # The digest week switch is not a cash-flow filter; keep it out of the filter form.
    query = request.GET.copy()
    digest_week = query.pop("digest_week", [WEEK_CURRENT])[-1]
    form = CashFlowFilterForm(query or None, principal=request.user)
    if form.is_bound and form.has_error("grouping"):
        # A missing or invalid grouping falls back to months; validate again with it so
        # the period-limit check still applies.
        data = query.copy()
        data["grouping"] = "month"
        form = CashFlowFilterForm(data, principal=request.user)
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
        tag = None
    elif form.is_valid():
        date_from = form.cleaned_data["date_from"] or default_from
        date_to = form.cleaned_data["date_to"] or default_to
        grouping = form.cleaned_data["grouping"]
        account = form.cleaned_data["account"]
        scope = form.cleaned_data["scope"]
        horizon = form.cleaned_data["horizon"] or DEFAULT_HORIZON
        tag = form.cleaned_data.get("tag")
    elif (
        form.non_field_errors()
        or form.has_error("date_from")
        or form.has_error("date_to")
    ):
        date_from = date_to = grouping = account = scope = None
        horizon = DEFAULT_HORIZON
        tag = None
    else:
        date_from = form.cleaned_data.get("date_from") or default_from
        date_to = form.cleaned_data.get("date_to") or default_to
        grouping = form.cleaned_data.get("grouping") or "month"
        account = form.cleaned_data.get("account")
        scope = form.cleaned_data.get("scope") or ""
        horizon = form.cleaned_data.get("horizon") or DEFAULT_HORIZON
        tag = form.cleaned_data.get("tag")
    if form.is_bound and date_from is not None:
        _show_effective_dates(form, date_from, date_to)
    changes = list(parse_scenario_tokens(request.GET))
    inputs_for_form = ()
    if date_from is not None:
        inputs_for_form = visible_projection_inputs(request.user, account=account, scope=scope)  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
        changes = list(visible_scenario_changes(changes, inputs_for_form))
    planned_qs, series_qs = _scenario_choice_querysets(request.user, inputs_for_form)
    scenario_form = ScenarioChangeForm(
        request.GET if request.GET.get("scenario_action") == "add" else None,
        planned_items=planned_qs,
        series=series_qs,
    )
    if date_from is not None and request.GET.get("scenario_action") == "remove":
        remove_token = request.GET.get("remove_sc", "")
        remaining = [change for change in changes if encode_change(change) != remove_token]
        return redirect(
            f"{reverse('home')}?{urlencode(_cash_flow_pairs(date_from=date_from, date_to=date_to, grouping=grouping, horizon=horizon, account=account, scope=scope, tag=tag, changes=remaining))}"
        )
    if date_from is not None and request.GET.get("scenario_action") == "add" and scenario_form.is_valid():
        changes.append(scenario_form.to_change())
        changes = changes[:MAX_CHANGES]
        return redirect(
            f"{reverse('home')}?{urlencode(_cash_flow_pairs(date_from=date_from, date_to=date_to, grouping=grouping, horizon=horizon, account=account, scope=scope, tag=tag, changes=changes))}"
        )
    report = None
    if date_from is not None:
        report = cash_flow_with_projection(
            request.user,
            date_from=date_from,
            date_to=date_to,
            grouping=grouping,
            account=account,
            scope=scope,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
            today=today,
            horizon=horizon,
            tag=tag,
            scenario_changes=changes,
        )
        inputs_for_form = report.projection_inputs
        planned_qs, series_qs = _scenario_choice_querysets(request.user, inputs_for_form)
        if request.GET.get("scenario_action") != "add":
            scenario_form = ScenarioChangeForm(planned_items=planned_qs, series=series_qs)
    planned_by_id = {item.source_id: item for item in inputs_for_form if item.source == SOURCE_PLANNED}
    series_by_id = {item.source_id: item for item in inputs_for_form if item.source == SOURCE_SERIES}
    listed_changes = []
    for change in changes:
        token = encode_change(change)
        remove_pairs = _cash_flow_pairs(
            date_from=date_from or default_from,
            date_to=date_to or default_to,
            grouping=grouping or "month",
            horizon=horizon,
            account=account,
            scope=scope or "",
            tag=tag,
            changes=changes,
        )
        remove_url = (
            f"{reverse('home')}?{urlencode(remove_pairs + [('scenario_action', 'remove'), ('remove_sc', token)])}"
        )
        listed_changes.append(
            SimpleNamespace(
                summary=describe_change(change, planned_by_id=planned_by_id, series_by_id=series_by_id),
                make_real_url=make_this_real_url(change),
                remove_url=remove_url,
                token=token,
            )
        )
    scenario_tokens = encode_changes(changes)
    budget_summary = dashboard_budget_summary(request.user, today=today)
    attention_items = _home_attention_items(request.user, budget_summary, today=today)
    filter_pairs = _cash_flow_pairs(
        date_from=date_from or default_from,
        date_to=date_to or default_to,
        grouping=grouping or "month",
        horizon=horizon,
        account=account,
        scope=scope or "",
        tag=tag,
        changes=(),
    )
    range_options = _home_range_options(
        today,
        date_from=date_from,
        date_to=date_to,
        explicit=bool(request.GET.get("date_from") or request.GET.get("date_to")),
        pair_kwargs={
            "grouping": grouping or "month",
            "horizon": horizon,
            "account": account,
            "scope": scope or "",
            "tag": tag,
            "changes": changes,
        },
    )
    return render(
        request,
        "finance/home.html",
        {
            "filter_form": form,
            "filter_pairs": filter_pairs,
            "range_options": range_options,
            "filter_count": _home_filter_count(
                grouping=grouping, horizon=horizon, account=account, scope=scope, tag=tag
            ),
            "scenario_form": scenario_form,
            "scenario_tokens": scenario_tokens,
            "scenario_listed_changes": listed_changes,
            "scenario_comparison": report.scenario_comparison if report is not None else None,
            "report": report,
            "chart_data": cash_flow_chart_data(report) if report is not None else None,
            **notice_context(request.user, report),
            "accounts": visible_rows(Account, request.user),
            "budget_summary": budget_summary,
            "attention_items": attention_items,
            "range_from": date_from,
            "range_to": date_to,
            "digest": _home_digest(request.user, query, digest_week, today=today),
        },
    )



@require_GET
def more(request):
    """Phone home for every page the bottom tabs do not cover."""
    groups = navigation(request)["nav_groups"][1:]
    return render(request, "finance/more.html", {"more_groups": groups})



def _preset_links(today, *, account=None, scope="", grouping=None, tab=None, extra_query=None, tag=None):
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
        if tag is not None:
            query["tag"] = str(tag.pk)
        if extra_query:
            query.update(extra_query)
        links.append(
            SimpleNamespace(
                label=preset.label,
                url=f"?{urlencode(query)}",
                date_from=preset.date_from,
                date_to=preset.date_to,
            )
        )
    return links



def _spending_query(*, date_from, date_to, grouping="month", account=None, scope="", tab="", tag=None):
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
    if tag is not None:
        query["tag"] = str(tag.pk)
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
    tag = selected_tag(form)
    report = None
    trend_report = None
    chart_data = None
    if date_from is not None:
        if tab == "trends":
            trend_report = spending_category_trend_report(
                request.user,
                date_from=date_from,
                date_to=date_to,
                grouping=grouping,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
                account=account,
                scope=scope,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
                today=today,
                tag=tag,
            )
            report = trend_report
            chart_data = spending_trend_chart_data(trend_report)
        else:
            report = spending_by_category_report(
                request.user,
                date_from=date_from,
                date_to=date_to,
                account=account,
                scope=scope,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
                grouping=grouping,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
                tag=tag,
            )
            chart_data = spending_chart_data(report)
    query = (
        _spending_query(
            date_from=date_from,
            date_to=date_to,
            grouping=grouping,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
            account=account,
            scope=scope,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
            tag=tag,
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
            **notice_context(request.user, report),
            "trend_report": trend_report,
            "chart_data": chart_data,
            "active_tab": tab,
            "range_from": date_from,
            "range_to": date_to,
            "overview_url": f"{reverse('spending-by-category')}?{urlencode(query)}" if query else reverse("spending-by-category"),
            "trends_url": (
                f"{reverse('spending-by-category')}?{urlencode({**query, 'tab': 'trends'})}"
                if query
                else f"{reverse('spending-by-category')}?tab=trends"
            ),
            "presets": (
                _preset_links(
                    today,
                    account=account,
                    scope=scope or "",
                    grouping=grouping,
                    tab=tab if tab == "trends" else None,
                    tag=tag,
                )
                if date_from is not None
                else ()
            ),
        },
    )



def _visible_spending_category(principal, category_id):
    if category_id is None:
        return SimpleNamespace(key="uncategorized", name="Uncategorized")
    category = (
        visible_rows(Category, principal)
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
    tag = selected_tag(form)
    report = None
    chart_data = None
    if date_from is not None:
        report = category_spending_trend_report(
            request.user,
            category_key=category.key,
            category_name=category.name,
            date_from=date_from,
            date_to=date_to,
            grouping=grouping,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
            account=account,
            scope=scope,  # pyright: ignore[reportArgumentType] - None when the filter form is invalid (see _spending_filter_state)
            today=today,
            tag=tag,
        )
        chart_data = category_trend_chart_data(report)
    return render(
        request,
        "finance/spending_category.html",
        {
            "filter_form": form,
            "report": report,
            **notice_context(request.user, report),
            "chart_data": chart_data,
            "category": category,
            "range_from": date_from,
            "range_to": date_to,
            "presets": (
                _preset_links(today, account=account, scope=scope or "", grouping=grouping, tag=tag)
                if date_from is not None
                else ()
            ),
        },
    )
