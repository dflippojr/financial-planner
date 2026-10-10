from bisect import bisect_right
from types import SimpleNamespace
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from .cash_flow import (
    GROUPING_MONTH,
    MAX_REPORT_PERIODS,
    date_range_presets,
    format_minor,
    iter_period_windows,
    period_count,
    period_label,
    selected_accounts,
)
from .models import Account, BalanceSnapshot

CARRIED_FORWARD = "carried forward"


def contribution_parts(account_type, source, amount_minor):
    """Split a snapshot into asset and liability minor units.

    Credit-card and loan SimpleFIN rows keep the protocol sign (owed is negative).
    Manual rows for those types store the amount owed as a positive number.
    """
    if account_type not in Account.LIABILITY_TYPES:
        return amount_minor, 0
    owed = -amount_minor if source == BalanceSnapshot.Source.SIMPLEFIN else amount_minor
    if owed >= 0:
        return 0, owed
    return -owed, 0


def signed_balance_minor(account_type, source, amount_minor):
    """A snapshot as its net-worth contribution: an amount owed is negative whatever its source."""
    assets, liabilities = contribution_parts(account_type, source, amount_minor)
    return assets - liabilities


def _source_rank(source):
    return 1 if source == BalanceSnapshot.Source.SIMPLEFIN else 0


def _ordered_snapshots(rows):
    ordered = sorted(rows, key=lambda row: (row.snapshot_date, _source_rank(row.source), row.pk))
    return ordered, [row.snapshot_date for row in ordered]


def last_on_or_before(ordered, dates, as_of):
    index = bisect_right(dates, as_of) - 1
    if index < 0:
        return None
    return ordered[index]


def _load_snapshots(principal, accounts, date_to):
    grouped = {account.pk: [] for account in accounts}
    if not accounts:
        return grouped
    rows = BalanceSnapshot.objects.visible_to(principal).filter(
        account_id__in=grouped,
        snapshot_date__lte=date_to,
    )
    for row in rows:
        grouped[row.account_id].append(row)
    return grouped


def _indexed_snapshots(principal, accounts, date_to):
    raw = _load_snapshots(principal, accounts, date_to)
    return {account.pk: _ordered_snapshots(raw[account.pk]) for account in accounts}


def _account_month_row(account, snapshot, window):
    assets, liabilities = contribution_parts(account.account_type, snapshot.source, snapshot.amount_minor)
    carried = snapshot.snapshot_date < window.calendar_start
    return SimpleNamespace(
        account_id=account.pk,
        account_name=account.name,
        account_type=account.account_type,
        amount_minor=snapshot.amount_minor,
        amount_display=format_minor(assets - liabilities, snapshot.currency),
        assets_minor=assets,
        liabilities_minor=liabilities,
        snapshot_date=snapshot.snapshot_date,
        source=snapshot.source,
        source_badge=CARRIED_FORWARD if carried else snapshot.source,
        carried_forward=carried,
        is_estimate=account.is_physical_asset(),
    )


def _month_period(window, accounts, indexed, today):
    assets = 0
    liabilities = 0
    rows = []
    omitted = 0
    for account in accounts:
        ordered, dates = indexed[account.pk]
        snapshot = last_on_or_before(ordered, dates, window.end)
        if snapshot is None:
            omitted += 1
            continue
        row = _account_month_row(account, snapshot, window)
        assets += row.assets_minor
        liabilities += row.liabilities_minor
        rows.append(row)
    net = assets - liabilities
    return SimpleNamespace(
        start=window.start,
        end=window.end,
        as_of=window.end,
        label=period_label(window, today=today),
        assets_minor=assets,
        liabilities_minor=liabilities,
        net_minor=net,
        assets_display=format_minor(assets),
        liabilities_display=format_minor(liabilities),
        net_display=format_minor(net),
        omitted_untracked=omitted,
        carried_forward=sum(1 for row in rows if row.carried_forward),
        accounts=rows,
        equity_lines=_equity_lines(accounts, rows),
    )


def _equity_lines(accounts, rows):
    visible = {account.pk: account for account in accounts}
    by_id = {row.account_id: row for row in rows}
    lines = []
    for account in accounts:
        if account.account_type != Account.Type.LOAN or not account.secured_asset_id:
            continue
        asset = visible.get(account.secured_asset_id)
        if asset is None:
            continue
        asset_row = by_id.get(asset.pk)
        loan_row = by_id.get(account.pk)
        if asset_row is None or loan_row is None:
            continue
        equity = asset_row.assets_minor - loan_row.liabilities_minor
        lines.append(
            SimpleNamespace(
                asset_id=asset.pk,
                loan_id=account.pk,
                label=f"{asset.name} equity",
                asset_name=asset.name,
                loan_name=account.name,
                equity_minor=equity,
                equity_display=format_minor(equity),
                is_estimate=True,
            )
        )
    return lines


def _delta(current_minor, previous_minor, versus):
    if previous_minor is None:
        return SimpleNamespace(
            minor=None,
            display="—",
            direction="none",
            label=f"No earlier {versus} to compare",
        )
    delta = current_minor - previous_minor
    if delta > 0:
        direction = "up"
        label = f"up {format_minor(delta)} vs {versus}"
    elif delta < 0:
        direction = "down"
        label = f"down {format_minor(-delta)} vs {versus}"
    else:
        direction = "flat"
        label = f"no change vs {versus}"
    return SimpleNamespace(
        minor=delta,
        display=format_minor(delta),
        direction=direction,
        label=label,
    )


def _summary(periods):
    current_net = periods[-1].net_minor if periods else 0
    previous_net = periods[-2].net_minor if len(periods) > 1 else None
    first_net = periods[0].net_minor if periods else None
    return SimpleNamespace(
        net_minor=current_net,
        net_display=format_minor(current_net),
        month_change=_delta(current_net, previous_net, "previous month"),
        range_change=_delta(current_net, first_net, "selected range"),
    )


def net_worth_report(principal, *, date_from, date_to, scope="", today=None, accounts=None):
    if period_count(date_from, date_to, GROUPING_MONTH) > MAX_REPORT_PERIODS:
        raise ValueError("Too many periods for one report.")
    today = today or timezone.localdate()
    accounts = selected_accounts(principal, scope=scope, accounts=accounts)
    indexed = _indexed_snapshots(principal, accounts, date_to)
    periods = [
        _month_period(window, accounts, indexed, today)
        for window in iter_period_windows(date_from, date_to, GROUPING_MONTH)
    ]
    return SimpleNamespace(
        accounts=accounts,
        periods=periods,
        has_snapshots=any(indexed[account.pk][0] for account in accounts),
        summary=_summary(periods),
    )


def _account_chart_row(row):
    return {
        "account_id": row.account_id,
        "account_name": row.account_name,
        "amount_minor": row.amount_minor,
        "amount_display": row.amount_display,
        "assets_minor": row.assets_minor,
        "liabilities_minor": row.liabilities_minor,
        "snapshot_date": row.snapshot_date.isoformat(),
        "source": row.source,
        "source_badge": row.source_badge,
        "carried_forward": row.carried_forward,
        "is_estimate": row.is_estimate,
    }


def _period_chart_row(period):
    return {
        "label": period.label,
        "assets_minor": period.assets_minor,
        "liabilities_minor": period.liabilities_minor,
        "net_minor": period.net_minor,
        "assets_display": period.assets_display,
        "liabilities_display": period.liabilities_display,
        "net_display": period.net_display,
        "omitted_untracked": period.omitted_untracked,
        "accounts": [_account_chart_row(row) for row in period.accounts],
        "equity_lines": [
            {
                "label": line.label,
                "asset_name": line.asset_name,
                "loan_name": line.loan_name,
                "equity_minor": line.equity_minor,
                "equity_display": line.equity_display,
                "is_estimate": line.is_estimate,
            }
            for line in period.equity_lines
        ],
    }


def _change_chart_row(change):
    return {
        "minor": change.minor,
        "display": change.display,
        "direction": change.direction,
        "label": change.label,
    }


def net_worth_chart_data(report):
    summary = report.summary
    return {
        "periods": [_period_chart_row(period) for period in report.periods],
        "summary": {
            "net_minor": summary.net_minor,
            "net_display": summary.net_display,
            "month_change": _change_chart_row(summary.month_change),
            "range_change": _change_chart_row(summary.range_change),
        },
    }


def net_worth_preset_links(today, *, scope=""):
    links = []
    for preset in date_range_presets(today):
        query = {"date_from": preset.date_from.isoformat(), "date_to": preset.date_to.isoformat()}
        if scope:
            query["scope"] = scope
        links.append(
            SimpleNamespace(
                label=preset.label,
                url=f"{reverse('net-worth')}?{urlencode(query)}",
                date_from=preset.date_from,
                date_to=preset.date_to,
            )
        )
    return links
