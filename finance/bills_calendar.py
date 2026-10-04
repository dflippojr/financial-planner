"""Month calendar of expected charges and a running expected deposit balance."""

from calendar import monthrange
from datetime import date, timedelta
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Case, Value, When
from django.urls import reverse
from django.utils import timezone

from .cash_flow import format_minor, selected_accounts
from .models import (
    Account,
    Alert,
    AlertSettings,
    BalanceSnapshot,
    BillsCalendarSettings,
    Transaction,
)
from .planning_services import visible_projection_inputs
from .projection import (
    KIND_INCOME,
    SOURCE_PLANNED,
    SOURCE_SERIES,
    month_end,
    occurrence_dates,
)

_DENIED = "Operation is not permitted."
ALERT_TITLE = "Expected balance below threshold in the next 7 days"
DEPOSIT_TYPES = (Account.Type.CHECKING, Account.Type.SAVINGS)


def _person(principal):
    from .models import _person_for

    person = _person_for(principal)
    if person is None:
        raise PermissionDenied(_DENIED)
    return person


def deposit_accounts(principal, *, scope=""):
    query = (
        Account.objects.visible_to(principal)
        .filter(
            account_type__in=DEPOSIT_TYPES,
            status=Account.Status.ACTIVE,
            archived_at__isnull=True,
        )
        .order_by("name", "pk")
    )
    if scope:
        query = query.filter(scope=scope)
    return query


def calendar_settings_for(principal):
    person = _person(principal)
    prefs, _created = BillsCalendarSettings.objects.get_or_create(person=person)
    return prefs


def save_calendar_settings(principal, *, account_ids, threshold_minor):
    person = _person(principal)
    if threshold_minor is not None and threshold_minor < 0:
        raise ValidationError("Threshold must be zero or greater.")
    allowed = deposit_accounts(person)
    allowed_ids = set(allowed.values_list("pk", flat=True))
    chosen = [pk for pk in account_ids if pk in allowed_ids]
    prefs = calendar_settings_for(person)
    prefs.threshold_minor = threshold_minor
    prefs.save(update_fields=("threshold_minor",))
    prefs.accounts.set(chosen)
    return prefs


def _last_charge_account_ids(series_ids):
    if not series_ids:
        return {}
    from .models import RecurringSeriesMember

    best = {}
    rows = RecurringSeriesMember.objects.filter(series_id__in=series_ids).values(
        "series_id",
        "transaction__account_id",
        "transaction__transaction_date",
        "transaction_id",
    )
    for row in rows:
        key = (row["transaction__transaction_date"], row["transaction_id"])
        current = best.get(row["series_id"])
        if current is None or key > current[0]:
            best[row["series_id"]] = (key, row["transaction__account_id"])
    return {series_id: account_id for series_id, (_key, account_id) in best.items()}


def calendar_inputs(principal, *, scope=""):
    rows = []
    sources = visible_projection_inputs(principal, scope=scope)
    series_ids = [item.source_id for item in sources if item.source == SOURCE_SERIES]
    last_accounts = _last_charge_account_ids(series_ids)
    for item in sources:
        account_id = last_accounts.get(item.source_id) if item.source == SOURCE_SERIES else None
        rows.append(
            SimpleNamespace(
                name=item.name,
                kind=item.kind,
                amount_minor=item.amount_minor,
                currency=item.currency,
                start=item.start,
                end=item.end,
                cadence=item.cadence,
                source=item.source,
                source_id=item.source_id,
                account_id=account_id,
            )
        )
    return rows


def _signed_amount(item):
    if item.kind == KIND_INCOME:
        return item.amount_minor
    return -item.amount_minor


def _item_url(item):
    if item.source == SOURCE_PLANNED:
        return reverse("planned-item-edit", args=[item.source_id])
    return f"{reverse('recurring-review')}#series-{item.source_id}"


def _expected_on_day(sources, on_date):
    hits = []
    for item in sources:
        if occurrence_dates(item.start, item.end, item.cadence, on_date, on_date):
            delta = _signed_amount(item)
            hits.append(
                SimpleNamespace(
                    name=item.name,
                    kind=item.kind,
                    source=item.source,
                    source_id=item.source_id,
                    account_id=item.account_id,
                    amount_minor=delta,
                    amount_display=format_minor(delta, item.currency),
                    url=_item_url(item),
                    expected=True,
                )
            )
    hits.sort(key=lambda row: (row.name.lower(), row.source, row.source_id))
    return hits


def _actuals_on_day(principal, on_date, *, scope=""):
    query = (
        Transaction.objects.visible_to(principal)
        .filter(
            status=Transaction.Status.ACTIVE,
            kind=Transaction.Kind.CASH_FLOW,
            transaction_date=on_date,
            account__status=Account.Status.ACTIVE,
            account__archived_at__isnull=True,
        )
        .select_related("account")
        .order_by("pk")
    )
    if scope:
        accounts = selected_accounts(principal, scope=scope, cash_flow_only=True)
        query = query.filter(account__in=accounts)
    hits = []
    for txn in query:
        hits.append(
            SimpleNamespace(
                name=txn.description,
                kind="actual",
                source="actual",
                source_id=txn.pk,
                account_id=txn.account_id,
                amount_minor=txn.amount_minor,
                amount_display=format_minor(txn.amount_minor, txn.currency),
                url=reverse("transaction-edit", args=[txn.pk]),
                expected=False,
            )
        )
    return hits


def _latest_snapshots(accounts, *, as_of):
    if not accounts:
        return {}
    account_ids = [account.pk for account in accounts]
    rows = (
        BalanceSnapshot.objects.filter(account_id__in=account_ids, snapshot_date__lte=as_of)
        .annotate(
            _source_rank=Case(
                When(source=BalanceSnapshot.Source.SIMPLEFIN, then=Value(1)),
                default=Value(0),
            )
        )
        .order_by("account_id", "-snapshot_date", "-_source_rank", "-pk")
    )
    latest = {}
    for snapshot in rows:
        if snapshot.account_id not in latest:
            latest[snapshot.account_id] = snapshot
    return latest


def starting_balance_minor(accounts, *, as_of):
    snapshots = _latest_snapshots(accounts, as_of=as_of)
    return sum(snapshot.amount_minor for snapshot in snapshots.values())


def _balance_delta_on_day(sources, on_date, selected_ids):
    total = 0
    for item in _expected_on_day(sources, on_date):
        if item.source == SOURCE_SERIES and item.account_id not in selected_ids:
            continue
        if item.source == SOURCE_PLANNED or (item.source == SOURCE_SERIES and item.account_id in selected_ids):
            total += item.amount_minor
    return total


def _shift_month(year, month, delta):
    month += delta
    year += (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return year, month


def month_weeks(year, month):
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    cursor = start - timedelta(days=start.weekday())
    weeks = []
    while True:
        week = []
        for _ in range(7):
            week.append(cursor)
            cursor += timedelta(days=1)
        weeks.append(week)
        if cursor > end and cursor.weekday() == 0:
            break
    return weeks


def expected_balances_by_day(sources, *, start_balance, selected_ids, from_date, through_date):
    """Inclusive running expected balances from from_date through through_date."""
    running = start_balance
    balances = {}
    day = from_date
    while day <= through_date:
        running += _balance_delta_on_day(sources, day, selected_ids)
        balances[day] = running
        day += timedelta(days=1)
    return balances


def build_month(
    principal,
    *,
    year,
    month,
    today=None,
    scope="",
    account_ids=None,
    threshold_minor=None,
):
    today = today or timezone.localdate()
    month_start = date(year, month, 1)
    last = month_end(month_start)
    sources = calendar_inputs(principal, scope=scope)
    selected = list(deposit_accounts(principal, scope=scope))
    wanted = set(account_ids or [])
    selected = [account for account in selected if account.pk in wanted]
    selected_ids = {account.pk for account in selected}
    start_balance = starting_balance_minor(selected, as_of=today) if selected else None
    through = last if last >= today else today
    balances = {}
    if start_balance is not None and selected:
        balances = expected_balances_by_day(
            sources,
            start_balance=start_balance,
            selected_ids=selected_ids,
            from_date=today,
            through_date=max(through, today + timedelta(days=7)),
        )
    days = []
    for day in (month_start + timedelta(days=offset) for offset in range((last - month_start).days + 1)):
        if day < today:
            items = _actuals_on_day(principal, day, scope=scope)
        else:
            items = _expected_on_day(sources, day)
        balance = balances.get(day) if day >= today else None
        below = (
            balance is not None
            and threshold_minor is not None
            and balance < threshold_minor
        )
        days.append(
            SimpleNamespace(
                date=day,
                in_month=True,
                past=day < today,
                items=items,
                balance_minor=balance,
                balance_display=format_minor(balance) if balance is not None else None,
                below_threshold=below,
            )
        )
    day_map = {row.date: row for row in days}
    weeks = []
    for week_dates in month_weeks(year, month):
        cells = []
        for cell_date in week_dates:
            if cell_date in day_map:
                cells.append(day_map[cell_date])
            else:
                cells.append(
                    SimpleNamespace(
                        date=cell_date,
                        in_month=False,
                        past=cell_date < today,
                        items=(),
                        balance_minor=None,
                        balance_display=None,
                        below_threshold=False,
                    )
                )
        weeks.append(cells)
    prev_year, prev_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    return SimpleNamespace(
        year=year,
        month=month,
        label=month_start.strftime("%B %Y"),
        today=today,
        days=days,
        weeks=weeks,
        selected_accounts=selected,
        start_balance_minor=start_balance,
        start_balance_display=format_minor(start_balance) if start_balance is not None else None,
        threshold_minor=threshold_minor,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
    )


def first_below_in_next_days(principal, *, today=None, days=7):
    today = today or timezone.localdate()
    prefs = calendar_settings_for(principal)
    if prefs.threshold_minor is None:
        return None
    selected_ids = list(prefs.accounts.values_list("pk", flat=True))
    selected = list(deposit_accounts(principal).filter(pk__in=selected_ids))
    if not selected:
        return None
    sources = calendar_inputs(principal)
    start_balance = starting_balance_minor(selected, as_of=today)
    through = today + timedelta(days=days - 1)
    balances = expected_balances_by_day(
        sources,
        start_balance=start_balance,
        selected_ids={account.pk for account in selected},
        from_date=today,
        through_date=through,
    )
    for offset in range(days):
        day = today + timedelta(days=offset)
        if balances[day] < prefs.threshold_minor:
            return day
    return None


def evaluate_expected_balance_alert(principal, *, today=None):
    from .alert_services import raise_alert

    today = today or timezone.localdate()
    person = _person(principal)
    first = first_below_in_next_days(person, today=today)
    if first is None:
        return []
    return raise_alert(
        [person],
        Alert.Kind.EXPECTED_BALANCE,
        ALERT_TITLE,
        reverse("bills-calendar"),
        f"expected_balance:{first.isoformat()}",
    )


def evaluate_expected_balance_alerts(*, today=None):
    today = today or timezone.localdate()
    created = []
    for prefs in AlertSettings.objects.filter(expected_balance_enabled=True).select_related("person"):
        created.extend(evaluate_expected_balance_alert(prefs.person, today=today))
    return created
