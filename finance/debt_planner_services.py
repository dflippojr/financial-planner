from dataclasses import replace
from types import SimpleNamespace

from django.utils import timezone

from .balance_history import indexed_histories, last_on_or_before
from .cash_flow import format_minor
from .debt_planner import NEEDS_DETAILS, DebtInput, compare_to_minimums
from .models import Account
from .months import add_months, month_end
from .net_worth import contribution_parts


def _owed_minor(account, snapshot):
    if snapshot is None:
        return None
    _assets, liabilities = contribution_parts(account.account_type, snapshot.source, snapshot.amount_minor)
    return liabilities


def _latest_snapshots(principal, accounts, today):
    """Each account's latest snapshot on or before today, or None."""
    histories = indexed_histories(principal, accounts, today)
    return {account_id: last_on_or_before(ordered, dates, today) for account_id, (ordered, dates) in histories.items()}


def _catalog_row(account, snapshot):
    owed_minor = _owed_minor(account, snapshot)
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
        snapshot_date=snapshot.snapshot_date if snapshot is not None else None,
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
    snapshots = _latest_snapshots(principal, accounts, today)
    return [_catalog_row(account, snapshots[account.pk]) for account in accounts]


def _payment_in_snapshot(row, today):
    """Whether the balance already reflects this month's payment (#511).

    With a known payment day, a snapshot on or after this month's payment date
    reflects it. Without one, any snapshot taken this month is assumed to.
    """
    if row.payment_day:
        due = today.replace(day=min(row.payment_day, month_end(today).day))
        return row.snapshot_date >= due
    return row.snapshot_date >= today.replace(day=1)


def _inputs_for(rows, include_ids, today):
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
                first_month_paid=_payment_in_snapshot(row, today),
            )
        )
    return included


def _first_month(inputs, today):
    """This month, unless every included debt has already paid it."""
    start = today.replace(day=1)
    if inputs and all(debt.first_month_paid for debt in inputs):
        return add_months(start, 1), [replace(debt, first_month_paid=False) for debt in inputs]
    return start, inputs


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
    start, inputs = _first_month(_inputs_for(catalog, include_ids, today), today)
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
