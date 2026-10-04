"""Temporary cash-flow scenario changes stored only in the query string."""

from copy import copy
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.urls import reverse

from .cash_flow import format_minor
from .projection import (
    CADENCE_ONE_TIME,
    KIND_EXPENSE,
    KIND_INCOME,
    SOURCE_PLANNED,
    SOURCE_SERIES,
    project_cash_flow,
)

CHANGE_ADD = "add"
CHANGE_AMOUNT = "amount"
CHANGE_PAUSE = "pause"
CHANGE_ONEOFF = "oneoff"
QUERY_KEY = "sc"
MAX_CHANGES = 24
_CADENCES = frozenset(
    ("one_time", "weekly", "biweekly", "monthly", "quarterly", "annual")
)
_KINDS = frozenset((KIND_INCOME, KIND_EXPENSE))


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_amount(value):
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return amount


def _parse_id(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def parse_scenario_tokens(query):
    """Return validated change rows from repeated `sc` query values."""
    raw = query.getlist(QUERY_KEY) if hasattr(query, "getlist") else list(query.get(QUERY_KEY) or [])
    changes = []
    for token in raw:
        change = _parse_token(token)
        if change is None:
            continue
        changes.append(change)
        if len(changes) >= MAX_CHANGES:
            break
    return tuple(changes)


def encode_change(change):
    if change.type == CHANGE_ADD:
        end = change.end.isoformat() if change.end else ""
        name = change.name.replace("|", " ")
        return f"a|{change.kind}|{change.amount_minor}|{change.cadence}|{change.start.isoformat()}|{end}|{name}"
    if change.type == CHANGE_AMOUNT:
        return f"m|{change.source_id}|{change.amount_minor}"
    if change.type == CHANGE_PAUSE:
        return f"p|{change.source_id}|{change.pause_from.isoformat()}"
    name = change.name.replace("|", " ")
    return f"o|{change.kind}|{change.amount_minor}|{change.start.isoformat()}|{name}"


def encode_changes(changes):
    return [encode_change(change) for change in changes]


def _parse_token(token):
    if not token or not isinstance(token, str):
        return None
    parts = token.split("|")
    if not parts:
        return None
    prefix = parts[0]
    if prefix == "a" and len(parts) >= 7:
        kind, amount, cadence, start, end = parts[1], parts[2], parts[3], parts[4], parts[5]
        name = "|".join(parts[6:]).strip()
        amount_minor = _parse_amount(amount)
        start_date = _parse_date(start)
        end_date = _parse_date(end) if end else None
        if (
            kind not in _KINDS
            or cadence not in _CADENCES
            or amount_minor is None
            or start_date is None
            or not name
            or len(name) > 150
            or (end_date is not None and end_date < start_date)
        ):
            return None
        return SimpleNamespace(
            type=CHANGE_ADD,
            name=name,
            kind=kind,
            amount_minor=amount_minor,
            cadence=cadence,
            start=start_date,
            end=end_date,
        )
    if prefix == "m" and len(parts) == 3:
        source_id = _parse_id(parts[1])
        amount_minor = _parse_amount(parts[2])
        if source_id is None or amount_minor is None:
            return None
        return SimpleNamespace(type=CHANGE_AMOUNT, source_id=source_id, amount_minor=amount_minor)
    if prefix == "p" and len(parts) == 3:
        source_id = _parse_id(parts[1])
        pause_from = _parse_date(parts[2])
        if source_id is None or pause_from is None:
            return None
        return SimpleNamespace(type=CHANGE_PAUSE, source_id=source_id, pause_from=pause_from)
    if prefix == "o" and len(parts) >= 5:
        kind, amount, start = parts[1], parts[2], parts[3]
        name = "|".join(parts[4:]).strip()
        amount_minor = _parse_amount(amount)
        start_date = _parse_date(start)
        if (
            kind not in _KINDS
            or amount_minor is None
            or start_date is None
            or not name
            or len(name) > 150
        ):
            return None
        return SimpleNamespace(
            type=CHANGE_ONEOFF,
            name=name,
            kind=kind,
            amount_minor=amount_minor,
            cadence=CADENCE_ONE_TIME,
            start=start_date,
            end=None,
        )
    return None


def _copy_inputs(items):
    return [copy(item) for item in items]


def apply_scenario(items, changes):
    """Return a new input list with query-string changes applied in memory."""
    result = _copy_inputs(items)
    next_id = -1
    for change in changes:
        if change.type == CHANGE_ADD:
            result.append(
                SimpleNamespace(
                    name=change.name,
                    kind=change.kind,
                    amount_minor=change.amount_minor,
                    currency="USD",
                    start=change.start,
                    end=change.end,
                    cadence=change.cadence,
                    source=SOURCE_PLANNED,
                    source_id=next_id,
                )
            )
            next_id -= 1
        elif change.type == CHANGE_ONEOFF:
            result.append(
                SimpleNamespace(
                    name=change.name,
                    kind=change.kind,
                    amount_minor=change.amount_minor,
                    currency="USD",
                    start=change.start,
                    end=None,
                    cadence=CADENCE_ONE_TIME,
                    source=SOURCE_PLANNED,
                    source_id=next_id,
                )
            )
            next_id -= 1
        elif change.type == CHANGE_AMOUNT:
            for item in result:
                if item.source == SOURCE_PLANNED and item.source_id == change.source_id:
                    item.amount_minor = change.amount_minor
                    break
        elif change.type == CHANGE_PAUSE:
            for item in result:
                if item.source == SOURCE_SERIES and item.source_id == change.source_id:
                    # Pausing from the first representable day pauses the whole series.
                    item.end = change.pause_from - timedelta(days=1) if change.pause_from > date.min else date.min
                    break
    return result


def scenario_projected_months(items, changes, *, today, horizon):
    return project_cash_flow(apply_scenario(items, changes), today=today, horizon=horizon)


def compare_projected_months(baseline, scenario):
    rows = []
    for left, right in zip(baseline, scenario, strict=True):
        difference = right.net_minor - left.net_minor
        rows.append(
            SimpleNamespace(
                label=left.label,
                start=left.start,
                end=left.end,
                baseline=left,
                scenario=right,
                difference_minor=difference,
                difference_display=format_minor(difference),
            )
        )
    return tuple(rows)


def dollars_from_minor(amount_minor):
    return format(Decimal(amount_minor) / Decimal(100), ".2f")


def make_this_real_url(change):
    """Link to the planned-item form with the scenario change as GET initials."""
    if change.type in (CHANGE_ADD, CHANGE_ONEOFF):
        query = {
            "name": change.name,
            "kind": change.kind,
            "amount": dollars_from_minor(change.amount_minor),
            "start_date": change.start.isoformat(),
            "cadence": change.cadence if change.type == CHANGE_ADD else CADENCE_ONE_TIME,
        }
        if change.type == CHANGE_ADD and change.end:
            query["end_date"] = change.end.isoformat()
        return f"{reverse('planned-items')}?{urlencode(query)}"
    if change.type == CHANGE_AMOUNT:
        query = {"amount": dollars_from_minor(change.amount_minor)}
        return f"{reverse('planned-item-edit', args=[change.source_id])}?{urlencode(query)}"
    return reverse("recurring-review")


def describe_change(change, *, planned_by_id, series_by_id):
    if change.type == CHANGE_ADD:
        end = f" through {change.end.isoformat()}" if change.end else ""
        return (
            f"Add {change.kind} {format_minor(change.amount_minor)} "
            f"{change.cadence} from {change.start.isoformat()}{end}: {change.name}"
        )
    if change.type == CHANGE_ONEOFF:
        return (
            f"One-off {change.kind} {format_minor(change.amount_minor)} "
            f"on {change.start.isoformat()}: {change.name}"
        )
    if change.type == CHANGE_AMOUNT:
        item = planned_by_id.get(change.source_id)
        label = item.name if item is not None else "planned item"
        return f"Change {label} amount to {format_minor(change.amount_minor)}"
    series = series_by_id.get(change.source_id)
    label = series.name if series is not None else "recurring series"
    return f"Pause {label} from {change.pause_from.isoformat()}"


def change_applies(change, items):
    if change.type in (CHANGE_ADD, CHANGE_ONEOFF):
        return True
    if change.type == CHANGE_AMOUNT:
        return any(item.source == SOURCE_PLANNED and item.source_id == change.source_id for item in items)
    if change.type == CHANGE_PAUSE:
        return any(item.source == SOURCE_SERIES and item.source_id == change.source_id for item in items)
    return False


def visible_scenario_changes(changes, items):
    return tuple(change for change in changes if change_applies(change, items))


def scenario_query_pairs(changes):
    return [(QUERY_KEY, encode_change(change)) for change in changes]
