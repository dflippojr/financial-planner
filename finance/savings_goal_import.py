"""Wishlist import: parse a small CSV or JSON file into savings goals.

`preview_goal_import` is read-only and `commit_goal_import` re-runs it on the
current data before writing, so a stale preview can never apply. Matching is by
name, case-insensitive, within the importer's own scope; goals missing from the
file are listed and never deleted.
"""

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .access import require_person as _person
from .cash_flow import format_minor
from .category_services import current_household
from .lifecycle_services import lock_actor_household
from .models import SavingsGoal
from .savings_goal_services import save_savings_goal

MAX_FILE_BYTES = 256 * 1024
MAX_ROWS = 300
MAX_NAME_LENGTH = 150
MAX_MINOR = 2**63 - 1
MAX_PRIORITY = 1_000_000
MAX_AMOUNT_TEXT = 40
REQUIRED_COLUMNS = ("name", "target_amount", "priority")
OPTIONAL_COLUMNS = ("depends_on", "time_sensitive", "target_date", "scope")
COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
TRUE_WORDS = frozenset({"true", "yes", "y", "1"})
FALSE_WORDS = frozenset({"", "false", "no", "n", "0"})

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_UNCHANGED = "unchanged"
ACTION_ERROR = "error"


class GoalFileError(ValueError):
    """The file as a whole cannot be read; the message is safe to show."""


@dataclass
class GoalRow:
    number: int
    name: str = ""
    target_amount_minor: int = 0
    priority: int = 0
    depends_on_name: str = ""
    time_sensitive: bool = False
    target_date: date | None = None
    scope: str = SavingsGoal.Scope.PRIVATE
    columns: frozenset = frozenset()
    errors: list = field(default_factory=list)
    action: str = ACTION_CREATE
    existing: SavingsGoal | None = None
    changes: list = field(default_factory=list)
    dependency: "GoalRow | None" = None

    @property
    def key(self):
        return (self.scope, self.name.casefold())

    @property
    def amount_display(self):
        return format_minor(self.target_amount_minor, "USD")


# -- parsing -----------------------------------------------------------------


def _decode(content):
    if len(content) > MAX_FILE_BYTES:
        raise GoalFileError("The file is larger than the 256 KB limit for a wishlist.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GoalFileError("The file must be UTF-8 text.") from exc


def _json_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, Decimal)):
        return str(value).strip()
    raise GoalFileError("Each JSON value must be text, a number, true/false or null.")


def _json_records(text):
    try:
        data = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise GoalFileError("The JSON could not be read.") from exc
    if isinstance(data, dict):
        data = data.get("goals")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise GoalFileError('JSON must be a list of goal objects, or an object with a "goals" list.')
    records = []
    for item in data:
        record = {str(key).strip().casefold(): _json_cell(value) for key, value in item.items()}
        if len(record) != len(item):
            raise GoalFileError("An object repeats a column name.")
        records.append(record)
    return records


def _reject_constant(_name):
    raise ValueError("Non-finite number.")


def _csv_records(text):
    reader = csv.DictReader(io.StringIO(text))
    try:
        records = []
        for record in reader:
            if None in record:
                raise GoalFileError("A row has more cells than the header has columns.")
            records.append({(key or "").strip().casefold(): (value or "").strip() for key, value in record.items()})
        return reader.fieldnames, records
    except csv.Error as exc:
        raise GoalFileError("The CSV could not be read.") from exc


def _records(content):
    text = _decode(content)
    if text.lstrip().startswith(("[", "{")):
        records = _json_records(text)
        headers = {key for record in records for key in record}
        return list(headers), records
    headers, records = _csv_records(text)
    if not headers:
        raise GoalFileError("The file is empty.")
    return [(header or "").strip().casefold() for header in headers], records


def _check_columns(headers):
    if len(set(headers)) != len(headers):
        raise GoalFileError("The file repeats a column name.")
    unknown = sorted(set(headers) - set(COLUMNS))
    if unknown:
        raise GoalFileError("Unknown column: use only " + ", ".join(COLUMNS) + ".")
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise GoalFileError("Missing required column: " + ", ".join(missing) + ".")


def _amount_to_minor(amount):
    """Exact minor units for a Decimal with at most two decimal places, else None."""
    sign, digits, exponent = amount.as_tuple()
    if sign or not any(digits) or amount.adjusted() > 17 or exponent < -(len(digits) + 2):
        return None
    coefficient = int("".join(map(str, digits)))
    if exponent >= -2:
        return coefficient * 10 ** (exponent + 2)
    divisor = 10 ** (-exponent - 2)
    return coefficient // divisor if coefficient % divisor == 0 else None


def _parse_amount(text):
    if len(text) > MAX_AMOUNT_TEXT:
        return None, "target_amount is outside the supported range."
    try:
        amount = Decimal(text.replace(",", "").lstrip("$").strip())
    except InvalidOperation:
        return None, "target_amount must be a number such as 1250.00."
    if not amount.is_finite():
        return None, "target_amount must be a number such as 1250.00."
    minor = _amount_to_minor(amount)
    if minor is None:
        return None, "target_amount must be greater than zero with at most two decimal places."
    if minor > MAX_MINOR:
        return None, "target_amount is outside the supported range."
    return minor, None


def _parse_priority(text):
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= MAX_PRIORITY:
        return None, f"priority must be a whole number from 1 to {MAX_PRIORITY}."
    return int(text), None


def _parse_bool(text):
    word = text.casefold()
    if word in TRUE_WORDS:
        return True, None
    if word in FALSE_WORDS:
        return False, None
    return False, "time_sensitive must be true or false."


def _parse_date(text):
    if not text:
        return None, None
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, "target_date must look like 2027-03-31."


def _parse_scope(text):
    word = text.casefold() or SavingsGoal.Scope.PRIVATE
    if word not in SavingsGoal.Scope.values:
        return SavingsGoal.Scope.PRIVATE, "scope must be private or household."
    return word, None


def _parse_row(number, record, columns):
    row = GoalRow(number=number, columns=columns)
    row.name = record.get("name", "")
    if not row.name:
        row.errors.append("name is required.")
    elif len(row.name) > MAX_NAME_LENGTH:
        row.errors.append(f"name can be at most {MAX_NAME_LENGTH} characters.")
    parsers = (
        ("target_amount", _parse_amount, "target_amount_minor"),
        ("priority", _parse_priority, "priority"),
        ("time_sensitive", _parse_bool, "time_sensitive"),
        ("target_date", _parse_date, "target_date"),
        ("scope", _parse_scope, "scope"),
    )
    for column, parser, attribute in parsers:
        if column not in columns:
            continue
        value, error = parser(record.get(column, ""))
        if error:
            row.errors.append(error)
        else:
            setattr(row, attribute, value)
    row.depends_on_name = record.get("depends_on", "")
    return row


def parse_goal_file(content):
    """Parse an uploaded CSV or JSON wishlist into rows; raises `GoalFileError` for file-level problems."""
    headers, records = _records(content)
    _check_columns(headers)
    if not records:
        raise GoalFileError("The file has no goals.")
    if len(records) > MAX_ROWS:
        raise GoalFileError(f"The file has more than {MAX_ROWS} goals.")
    columns = frozenset(headers)
    return [_parse_row(number, record, columns) for number, record in enumerate(records, start=1)]


# -- matching against saved goals ---------------------------------------------


def _own_goals(person, household):
    """Goals the importer can match: their private goals and their household's goals."""
    owned = Q(owner=person, scope=SavingsGoal.Scope.PRIVATE)
    if household is not None:
        owned |= Q(household=household, scope=SavingsGoal.Scope.HOUSEHOLD)
    return SavingsGoal.objects.filter(owned)


def _existing_by_key(person, household, scopes):
    """The importer's own goals per `(scope, folded name)`, with their dependency chains."""
    grouped = {}
    for goal in _own_goals(person, household).filter(scope__in=scopes).select_related("depends_on").order_by("pk"):
        grouped.setdefault((goal.scope, goal.name.casefold()), []).append(goal)
    return grouped


def _matchable(goals):
    """Saved goals a row may update: the active ones, or archived ones when none is active."""
    active = [goal for goal in goals if goal.status == SavingsGoal.Status.ACTIVE]
    return active or goals


def _flag_duplicates_and_matches(rows, existing, household):
    seen = {}
    for row in rows:
        if row.errors:
            continue
        if row.scope == SavingsGoal.Scope.HOUSEHOLD and household is None:
            row.errors.append("Join a household before importing household goals.")
        elif row.key in seen:
            row.errors.append(f"Same goal name as row {seen[row.key]} (names are not case-sensitive).")
        else:
            seen[row.key] = row.number
        matches = _matchable(existing.get(row.key, []))
        if len(matches) > 1:
            row.errors.append("More than one saved goal has this name; rename them before importing.")
        elif matches:
            row.existing = matches[0]


def _resolve_dependency(row, by_name):
    """A dependency is another row of this file in the same scope (private or household)."""
    if not row.depends_on_name:
        return
    target = by_name.get(row.depends_on_name.casefold(), {}).get(row.scope)
    if target is None:
        row.errors.append("depends_on must be the name of another goal in this file with the same scope.")
    elif target is row:
        row.errors.append("A goal cannot depend on itself.")
    elif target.errors:
        row.errors.append(f"depends_on points at row {target.number}, which has errors.")
    else:
        row.dependency = target


def _final_dependency(row):
    """The row or saved goal this row will depend on once the import is applied."""
    if "depends_on" in row.columns:
        return row.dependency
    return row.existing.depends_on if row.existing is not None else None


def _check_cycles(rows, saved_dependency):
    """Reject a dependency that would loop, counting saved links of goals the file does not touch."""
    by_goal = {row.existing.pk: row for row in rows if row.existing is not None}

    def row_of(item):
        return item if isinstance(item, GoalRow) else by_goal.get(item.pk)

    def node(item):
        row = row_of(item)
        return ("row", row.number) if row is not None else ("goal", item.pk)

    def following(item):
        row = row_of(item)
        return _final_dependency(row) if row is not None else saved_dependency.get(item.pk)

    for row in rows:
        if row.errors:
            continue
        seen = {node(row)}
        current = following(row)
        while current is not None:
            if node(current) in seen:
                row.errors.append("This dependency would make goals wait on each other.")
                break
            seen.add(node(current))
            current = following(current)


def _saved_dependencies(person, household):
    return {goal.pk: goal.depends_on for goal in _own_goals(person, household).select_related("depends_on")}


def _row_changes(row):
    goal = row.existing
    if goal is None:
        return []
    checks = [
        ("name", goal.name != row.name),
        ("amount", goal.target_amount_minor != row.target_amount_minor),
        ("priority", "priority" in row.columns and goal.priority != row.priority),
        ("time_sensitive", "time_sensitive" in row.columns and goal.time_sensitive != row.time_sensitive),
        ("date", "target_date" in row.columns and goal.target_date != row.target_date),
        ("dependency", "depends_on" in row.columns and _dependency_changed(row)),
    ]
    return [name for name, changed in checks if changed]


def _dependency_changed(row):
    saved = row.existing.depends_on_id
    wanted = row.dependency
    if wanted is None:
        return saved is not None
    return wanted.existing is None or wanted.existing.pk != saved


def _classify(rows):
    for row in rows:
        if row.errors:
            row.action = ACTION_ERROR
        elif row.existing is None:
            row.action = ACTION_CREATE
        else:
            row.changes = _row_changes(row)
            row.action = ACTION_UPDATE if row.changes else ACTION_UNCHANGED


def preview_goal_import(principal, content):
    """Read-only report of what the file would create, change or leave alone."""
    rows = parse_goal_file(content)
    person = _person(principal, check_authenticated=False)
    household = current_household(person)
    scopes = {row.scope for row in rows}
    existing = _existing_by_key(person, household, scopes)
    _flag_duplicates_and_matches(rows, existing, household)
    by_name = {}
    for row in rows:
        by_name.setdefault(row.name.casefold(), {}).setdefault(row.scope, row)
    for row in rows:
        if not row.errors:
            _resolve_dependency(row, by_name)
    _check_cycles(rows, _saved_dependencies(person, household))
    _classify(rows)
    matched = {row.existing.pk for row in rows if row.existing is not None}
    missing = [
        goal
        for goals in existing.values()
        for goal in goals
        if goal.pk not in matched and goal.status == SavingsGoal.Status.ACTIVE
    ]
    return SimpleNamespace(
        rows=rows,
        errors=[row for row in rows if row.errors],
        missing=sorted(missing, key=lambda goal: (goal.scope, goal.name.casefold(), goal.pk)),
        counts=SimpleNamespace(
            create=sum(row.action == ACTION_CREATE for row in rows),
            update=sum(row.action == ACTION_UPDATE for row in rows),
            unchanged=sum(row.action == ACTION_UNCHANGED for row in rows),
            error=sum(row.action == ACTION_ERROR for row in rows),
        ),
    )


# -- commit --------------------------------------------------------------------


def _in_dependency_order(rows):
    ordered = []
    placed = set()

    def visit(row):
        if row.number in placed:
            return
        placed.add(row.number)
        if row.dependency is not None:
            visit(row.dependency)
        ordered.append(row)

    for row in rows:
        visit(row)
    return ordered


def _row_payload(row, saved):
    payload = {"scope": row.scope, "name": row.name, "target_amount_minor": row.target_amount_minor}
    if "priority" in row.columns:
        payload["priority"] = row.priority
    if "time_sensitive" in row.columns:
        payload["time_sensitive"] = row.time_sensitive
    if "target_date" in row.columns:
        payload["target_date"] = row.target_date
    if "depends_on" in row.columns:
        payload["depends_on"] = saved[row.dependency.number] if row.dependency is not None else None
    return payload


def commit_goal_import(principal, content):
    """Apply the file in one transaction; refuses a file with any error. Returns the preview."""
    person = _person(principal, check_authenticated=False)
    saved = {}
    with transaction.atomic():
        # Two members confirming the same household file must not both create its goals.
        lock_actor_household(person)
        preview = preview_goal_import(principal, content)
        if preview.errors:
            raise ValidationError("Fix the rows marked with errors, then import again.")
        for row in _in_dependency_order(preview.rows):
            if row.action == ACTION_UNCHANGED:
                saved[row.number] = row.existing
                continue
            saved[row.number] = save_savings_goal(principal, _row_payload(row, saved), goal=row.existing)
    return preview
