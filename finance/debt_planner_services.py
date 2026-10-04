from types import SimpleNamespace

from django.utils import timezone

from .cash_flow import format_minor
from .debt_planner import NEEDS_DETAILS, DebtInput, compare_to_minimums
from .models import Account, BalanceSnapshot
from .net_worth import contribution_parts, last_on_or_before


def _snapshot_rank(source):
    return 1 if source == BalanceSnapshot.Source.SIMPLEFIN else 0


def _ordered_snapshots(rows):
    ordered = sorted(rows, key=lambda row: (row.snapshot_date, _snapshot_rank(row.source), row.pk))
    return ordered, [row.snapshot_date for row in ordered]


def _owed_minor(account, snapshot):
    if snapshot is None:
        return None
    _assets, liabilities = contribution_parts(account.account_type, snapshot.source, snapshot.amount_minor)
    return liabilities


def _load_owed(principal, accounts, today):
    grouped = {account.pk: [] for account in accounts}
    owed = {}
    if not accounts:
        return owed
    rows = BalanceSnapshot.objects.visible_to(principal).filter(
        account_id__in=grouped,
        snapshot_date__lte=today,
    )
    for row in rows:
        grouped[row.account_id].append(row)
    for account in accounts:
        ordered, dates = _ordered_snapshots(grouped[account.pk])
        owed[account.pk] = _owed_minor(account, last_on_or_before(ordered, dates, today))
    return owed


def _catalog_row(account, owed_minor):
    missing = []
    if account.apr_percent is None:
        missing.append("APR")
    if owed_minor is None or owed_minor <= 0:
        missing.append("balance")
    if account.minimum_payment_minor is None:
        missing.append("minimum payment")
    ready = not missing
    return SimpleNamespace(
        account_id=account.pk,
        account=account,
        name=account.name,
        account_type=account.account_type,
        payment_day=account.payment_day if account.account_type == Account.Type.LOAN else None,
        apr_percent=account.apr_percent,
        minimum_payment_minor=account.minimum_payment_minor,
        owed_minor=owed_minor,
        owed_display=format_minor(owed_minor) if owed_minor is not None else "",
        ready=ready,
        status="" if ready else NEEDS_DETAILS,
        missing=tuple(missing),
    )


def list_visible_debts(principal, today=None):
    today = today or timezone.localdate()
    accounts = list(
        Account.objects.visible_to(principal)
        .filter(
            status=Account.Status.ACTIVE,
            archived_at__isnull=True,
            account_type__in=Account.LIABILITY_TYPES,
        )
        .order_by("name", "pk")
    )
    owed = _load_owed(principal, accounts, today)
    return [_catalog_row(account, owed[account.pk]) for account in accounts]


def _inputs_for(rows, include_ids):
    selected = set(include_ids)
    included = []
    for row in rows:
        if row.account_id not in selected:
            continue
        if not row.ready:
            continue
        included.append(
            DebtInput(
                account_id=row.account_id,
                name=row.name,
                balance_minor=row.owed_minor,
                apr_percent=row.apr_percent,
                minimum_payment_minor=row.minimum_payment_minor,
            )
        )
    return included


def planner_chart_data(plan):
    if plan is None:
        return None
    return {
        "labels": [month.label for month in plan.months],
        "interest_minor": [month.interest_minor for month in plan.months],
        "interest_display": [format_minor(month.interest_minor) for month in plan.months],
        "remaining_minor": [month.remaining_minor for month in plan.months],
        "remaining_display": [format_minor(month.remaining_minor) for month in plan.months],
    }


def _decorate_comparison(comparison):
    for month in comparison.chosen.months:
        month.interest_display = format_minor(month.interest_minor)
        month.paid_display = format_minor(month.paid_minor)
        month.remaining_display = format_minor(month.remaining_minor)
    comparison.chosen.total_interest_display = format_minor(comparison.chosen.total_interest_minor)
    comparison.baseline.total_interest_display = format_minor(comparison.baseline.total_interest_minor)
    if comparison.interest_saved_minor is None:
        comparison.interest_saved_display = ""
    else:
        comparison.interest_saved_display = format_minor(comparison.interest_saved_minor)


def build_debt_plan(principal, *, include_ids, extra_minor, strategy, custom_order, today=None):
    today = today or timezone.localdate()
    catalog = list_visible_debts(principal, today=today)
    inputs = _inputs_for(catalog, include_ids)
    start = today.replace(day=1)
    comparison = None
    if inputs:
        comparison = compare_to_minimums(
            inputs,
            extra_minor=extra_minor or 0,
            strategy=strategy,
            custom_order=custom_order,
            start=start,
        )
    results_by_id = {}
    if comparison is not None:
        results_by_id = {row.account_id: row for row in comparison.chosen.debts}
    custom_rank = {account_id: index for index, account_id in enumerate(custom_order or (), start=1)}
    display_rows = []
    for row in catalog:
        result = results_by_id.get(row.account_id)
        if not row.ready:
            payoff_label = NEEDS_DETAILS
        elif result is None:
            payoff_label = "not included"
        else:
            payoff_label = result.payoff_label
        display_rows.append(
            SimpleNamespace(
                **row.__dict__,
                included=result is not None,
                payoff_label=payoff_label,
                rank=custom_rank.get(row.account_id),
            )
        )
    if comparison is not None:
        _decorate_comparison(comparison)
    return SimpleNamespace(
        catalog=display_rows,
        comparison=comparison,
        chart_data=planner_chart_data(None if comparison is None else comparison.chosen),
        start=start,
    )
