"""Changes the chat model can suggest. A tool call only stores a proposal; nothing is written
until the member presses Apply, and Apply runs the same service functions as the normal pages.

Proposals never touch accounts, sharing, connections, or deletion.
"""

from __future__ import annotations

import uuid
from datetime import date
from functools import partial

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Value
from django.db.models.functions import Concat
from django.urls import reverse
from django.utils import timezone

from .access import first_message as _first_message
from .ai_tools import ai_nameable_categories, household_consent_complete, visible_accounts
from .ai_types import ToolResult, ToolSpec
from .audit_services import origin, record
from .bulk_edit_services import _apply_tags
from .budget_services import amount_for, parse_month, save_budget, validate_budget_month
from .cash_flow import format_minor
from .category_services import (
    _DENIED,
    _person_for,
    assign_category,
    assignable_categories,
    current_household,
    exclusion_exists_for,
)
from .forms import MAX_SIGNED_BIGINT, PlannedItemForm
from .lifecycle_services import lock_actor_household
from .models import (
    AiProposal,
    Account,
    AuditEvent,
    Budget,
    PlannedItem,
    SavingsGoal,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
)
from .months import month_start
from .planning_services import save_planned_item
from .rule_services import apply_rule, preview_unsaved_rule, save_category_rule, unsaved_rule
from .savings_goal_services import save_savings_goal, validate_dependency
from .tag_services import add_tag

VIA_CHAT = " (via chat suggestion)"
MAX_PROPOSALS_PER_TURN = 5
MAX_TRANSACTIONS = 50
MAX_TAGS = 5
CARD_ROWS = 25
NEUTRAL_DROPPED = "Some of those could not be used and were left out."
NOTHING_APPLIED = "Nothing has changed. The member must press Apply on the proposal card in the app."

ALREADY_RESOLVED = "That suggestion was already handled."
NO_LONGER_APPLIES = "This suggestion no longer applies because the data changed. Ask again for a fresh one."
REFUSED = "That suggestion cannot be applied."

# Item cards (#659): one card, a checkbox per item, a result per item.
MAX_CARD_ITEMS = 25
MAX_PRIORITY = 1_000_000
MAX_FIELD_CHARS = 200
NOTHING_TICKED = "Tick at least one item to apply, or Dismiss the suggestion."
NONE_APPLIED = "None of the ticked items could be applied."
# Hidden and missing rows answer alike, so the model learns nothing from the difference.
ITEM_UNAVAILABLE = "That item is not available."
CATEGORY_PROBLEM = "Category: that category is not available."
DEPENDENCY_UNAVAILABLE = "Buy after: that goal is not available."
AMOUNT_PROBLEM = "Amount: enter a whole number of cents greater than zero."
ACTION_UNKNOWN = "Unknown action."
NOTHING_CHANGES = "Nothing would change."
SCOPE_UNKNOWN = "Scope must be private or household."
SCOPE_CHANGE = "Change whether an item is private or household on its own page."


class ProposalError(Exception):
    """A member-facing reason an Apply was refused."""


# ---------------------------------------------------------------------------
# Proposing: tool handlers that store a pending proposal and write nothing else
# ---------------------------------------------------------------------------


def proposal_tools(conversation, turn):
    """Tools bound to one chat turn. Their handlers only create AiProposal rows."""
    tools = (
        ("propose_set_category", "Suggest setting the category on transactions, by id. The member must confirm.", {
            "transaction_ids": {"type": "array", "items": {"type": "integer"}},
            "category": {"type": "string"},
        }, ["transaction_ids", "category"], _propose_set_category),
        ("propose_create_rule", "Suggest a categorization rule: description contains text, then category. The member must confirm.", {
            "description_contains": {"type": "string"},
            "category": {"type": "string"},
            "account_id": {"type": "integer"},
            "min_amount_minor": {"type": "integer"},
            "max_amount_minor": {"type": "integer"},
            "owner": {"type": "string", "enum": ["household", "personal"]},
        }, ["description_contains", "category"], _propose_create_rule),
        ("propose_set_budget", "Suggest creating or adjusting a category budget for a month. The member must confirm.", {
            "category": {"type": "string"},
            "month": {"type": "string"},
            "amount_minor": {"type": "integer"},
            "scope": {"type": "string", "enum": ["household", "private"]},
        }, ["category", "amount_minor"], _propose_set_budget),
        ("propose_add_tags", "Suggest adding tags to transactions, by id. The member must confirm.", {
            "transaction_ids": {"type": "array", "items": {"type": "integer"}},
            "tags": {"type": "array", "items": {"type": "string"}},
        }, ["transaction_ids", "tags"], _propose_add_tags),
        ("propose_planned_items", (
            "Suggest creating, editing or ending planned income and expense items, many in one card with a "
            "checkbox each. Amounts are positive cents, dates YYYY-MM-DD. Edit and end take planned_item_id "
            "from list_planned_items; end sets end_date (today when omitted). The member must confirm."
        ), {"items": _items_schema({
            "action": {"type": "string", "enum": ["create", "edit", "end"]},
            "planned_item_id": {"type": "integer"},
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": list(PlannedItem.Kind.values)},
            "amount_minor": {"type": "integer"},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "cadence": {"type": "string", "enum": list(PlannedItem.Cadence.values)},
            "category": {"type": "string"},
            "scope": {"type": "string", "enum": ["private", "household"]},
        })}, ["items"], _propose_planned_items),
        ("propose_savings_goals", (
            "Suggest creating or adjusting savings goals, many in one card with a checkbox each. Amounts are "
            "positive cents, dates YYYY-MM-DD, priority 1 is funded first. Adjusting takes goal_id. "
            "depends_on_goal_id is a goal to fund first. The member must confirm."
        ), {"items": _items_schema({
            "action": {"type": "string", "enum": ["create", "edit"]},
            "goal_id": {"type": "integer"},
            "name": {"type": "string"},
            "target_amount_minor": {"type": "integer"},
            "target_date": {"type": "string"},
            "priority": {"type": "integer"},
            "depends_on_goal_id": {"type": "integer"},
            "scope": {"type": "string", "enum": ["private", "household"]},
        })}, ["items"], _propose_savings_goals),
        ("propose_set_budgets", (
            "Suggest several category budgets for one month (YYYY-MM) in one card with a checkbox each. "
            "Amounts are positive cents. The member must confirm."
        ), {
            "month": {"type": "string"},
            "scope": {"type": "string", "enum": ["household", "private"]},
            "items": _items_schema({"category": {"type": "string"}, "amount_minor": {"type": "integer"}},
                                   required=["category", "amount_minor"]),
        }, ["items"], _propose_set_budgets),
    )
    return tuple(
        ToolSpec(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties, "required": required},
            handler=_bound(handler, conversation, turn),
        )
        for name, description, properties, required, handler in tools
    )


def _bound(handler, conversation, turn):
    def call(person, args):
        if AiProposal.objects.filter(message=turn).count() >= MAX_PROPOSALS_PER_TURN:
            return ToolResult(text="Too many suggestions in one answer.", ok=False)
        args = args or {}
        account_id = args.get("account_id")
        if account_id not in (None, ""):
            try:
                allowed = visible_accounts(person).filter(pk=int(account_id)).exists()
            except (TypeError, ValueError):
                allowed = False
            if not allowed:
                return _fail("Nothing could be proposed: that account is not available.")
        return handler(person, args, conversation, turn)

    return call


def _items_schema(properties, *, required=()):
    item = {"type": "object", "properties": properties}
    if required:
        item["required"] = list(required)
    return {"type": "array", "items": item, "maxItems": MAX_CARD_ITEMS}


def _store(conversation, turn, kind, payload, account_ids, *, budget_ids=()):
    proposal = AiProposal.objects.create(conversation=conversation, message=turn, kind=kind, payload=payload)
    text = f"Proposal {proposal.pk} is shown to the member as a card. {NOTHING_APPLIED}"
    return ToolResult(text=text, account_ids=tuple(sorted(set(account_ids))), budget_ids=tuple(budget_ids))


def _fail(text):
    return ToolResult(text=text, ok=False)


def _category_by_name(person, name):
    cleaned = _text(name)
    if not cleaned:
        return None
    # A household's own category name stays unknown to the model until
    # household consent is complete, so it answers like a missing name (#521).
    return (
        assignable_categories(person)
        .filter(name__iexact=cleaned, pk__in=ai_nameable_categories(person).values("pk"))
        .first()
    )


def _editable_transactions(person, ids):
    """Active visible transactions among ids, in the order given. Splits are left out."""
    wanted = []
    for item in ids if isinstance(ids, list) else []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in wanted:
            wanted.append(value)
    wanted = wanted[:MAX_TRANSACTIONS]
    rows = {
        txn.pk: txn
        for txn in Transaction.objects.visible_to(person)
        .filter(
            pk__in=wanted,
            status=Transaction.Status.ACTIVE,
            account_id__in=visible_accounts(person).values("pk"),
        )
        .exclude(category_source=Transaction.CategorySource.SPLIT)
        .select_related("account")
    }
    return [rows[pk] for pk in wanted if pk in rows], len(wanted)


def _propose_set_category(person, args, conversation, turn):
    category = _category_by_name(person, args.get("category"))
    txns, asked = _editable_transactions(person, args.get("transaction_ids"))
    if category is None or not txns:
        return _fail("Nothing could be proposed: that category or those transactions are not available.")
    payload = {
        "category_id": category.pk,
        "items": [
            {"id": txn.pk, "category_id": txn.category_id, "source": txn.category_source} for txn in txns
        ],
    }
    result = _store(conversation, turn, AiProposal.Kind.SET_CATEGORY, payload, [t.account_id for t in txns])
    return _with_dropped(result, asked, len(txns))


def _with_dropped(result, asked, kept):
    if kept >= asked:
        return result
    return ToolResult(text=f"{result.text} {NEUTRAL_DROPPED}", account_ids=result.account_ids)


def _optional_minor(value):
    if value is None or value == "":
        return None
    return int(value)


def _propose_create_rule(person, args, conversation, turn):
    category = _category_by_name(person, args.get("category"))
    owner = "personal" if args.get("owner") == "personal" else "household"
    if category is None:
        return _fail("Nothing could be proposed: that category is not available.")
    try:
        account_id = args.get("account_id")
        rule = unsaved_rule(
            person,
            owner_kind=owner,
            description_contains=args.get("description_contains"),
            account_id=int(account_id) if account_id not in (None, "") else None,
            min_amount_minor=_optional_minor(args.get("min_amount_minor")),
            max_amount_minor=_optional_minor(args.get("max_amount_minor")),
            category_id=category.pk,
        )
    except (ValidationError, PermissionDenied, TypeError, ValueError):
        return _fail("Nothing could be proposed: that rule is not valid for this member.")
    usable = set(visible_accounts(person).values_list("pk", flat=True))
    matches = [txn for txn in preview_unsaved_rule(person, rule) if txn.account_id in usable]
    payload = {
        "owner": owner,
        "description_contains": rule.description_contains,
        "category_id": category.pk,
        "account_id": rule.account_id,
        "min_amount_minor": rule.min_amount_minor,
        "max_amount_minor": rule.max_amount_minor,
        "match_ids": [txn.pk for txn in matches],
    }
    result = _store(conversation, turn, AiProposal.Kind.CREATE_RULE, payload, [t.account_id for t in matches])
    return ToolResult(
        text=f"{result.text} The rule would categorize {len(matches)} transactions now.",
        account_ids=result.account_ids,
    )


def _budget_for(person, scope, category, *, lock=False):
    query = Budget.objects.filter(status=Budget.Status.ACTIVE, scope=scope, category=category)
    if lock:
        query = query.select_for_update()
    if scope == Budget.Scope.PRIVATE:
        return query.filter(owner=person).first()
    household = current_household(person)
    return query.filter(household=household).first()


def _propose_set_budget(person, args, conversation, turn):
    category = _category_by_name(person, args.get("category"))
    scope = Budget.Scope.PRIVATE if args.get("scope") == "private" else Budget.Scope.HOUSEHOLD
    try:
        amount_minor = int(args.get("amount_minor"))
    except (TypeError, ValueError):
        amount_minor = 0
    if category is None or amount_minor <= 0 or (scope == Budget.Scope.HOUSEHOLD and current_household(person) is None):
        return _fail("Nothing could be proposed: that budget is not available.")
    month = parse_month(args.get("month"))
    existing = _budget_for(person, scope, category)
    payload = {
        "category_id": category.pk,
        "scope": scope,
        "month": month.isoformat(),
        "amount_minor": amount_minor,
        "budget_id": existing.pk if existing else None,
        "before_amount_minor": amount_for(existing, month) if existing else None,
    }
    # The card quotes the existing budget's amount.
    return _store(conversation, turn, AiProposal.Kind.SET_BUDGET, payload, [],
                  budget_ids=(existing.pk,) if existing else ())


def _clean_tag_names(raw):
    names = []
    for item in raw if isinstance(raw, list) else []:
        name = str(item or "").strip()
        if name and len(name) <= 80 and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names[:MAX_TAGS]


def _propose_add_tags(person, args, conversation, turn):
    names = _clean_tag_names(args.get("tags"))
    txns, asked = _editable_transactions(person, args.get("transaction_ids"))
    if not names or not txns or current_household(person) is None:
        return _fail("Nothing could be proposed: those tags or transactions are not available.")
    payload = {"tags": names, "items": [{"id": txn.pk} for txn in txns]}
    result = _store(conversation, turn, AiProposal.Kind.ADD_TAGS, payload, [t.account_id for t in txns])
    return _with_dropped(result, asked, len(txns))


# ---------------------------------------------------------------------------
# Item cards (#659): one card lists every proposed item with a checkbox. Each
# row is validated on its own; a row with a problem keeps its reason, shows it
# on the card, and can never be applied. Apply writes only the ticked rows.
# ---------------------------------------------------------------------------


def _raw_items(args):
    raw = args.get("items")
    raw = raw if isinstance(raw, list) else []
    items = [item for item in raw[:MAX_CARD_ITEMS] if isinstance(item, dict)]
    return items, len(raw) - len(items)


def _store_items(conversation, turn, kind, rows, dropped, *, extra=None, planned_item_ids=(), budget_ids=()):
    problems = "; ".join(f"item {number}: {row['error']}" for number, row in enumerate(rows, 1) if row["error"])
    if not any(row["error"] is None for row in rows):
        return _fail(f"Nothing could be proposed. {problems}." if problems else "Nothing could be proposed: no items.")
    payload = {**(extra or {}), "items": rows}
    proposal = AiProposal.objects.create(conversation=conversation, message=turn, kind=kind, payload=payload)
    text = (
        f"Proposal {proposal.pk} is shown to the member as one card listing {len(rows)} items, "
        f"each with a checkbox. {NOTHING_APPLIED}"
    )
    if problems:
        text += f" These items cannot be applied as written; the card shows why: {problems}."
    if dropped:
        text += f" {NEUTRAL_DROPPED} A card holds at most {MAX_CARD_ITEMS} items."
    return ToolResult(
        text=text,
        planned_item_ids=tuple(sorted(set(planned_item_ids))),
        budget_ids=tuple(sorted(set(budget_ids))),
    )


def _whole_minor(value):
    """A positive whole number of cents, or None. A fractional or boolean value is refused."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    try:
        minor = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return minor if 0 < minor <= MAX_SIGNED_BIGINT else None


def _whole_or_text(raw, key):
    """An integer field as given, or its capped text for validation to refuse."""
    value = raw.get(key)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    return _field(raw, key) or None


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dollars(minor):
    """Exact dollars text for a positive amount in cents, for the manual page's form."""
    return f"{minor // 100}.{minor % 100:02d}"


def _text(value):
    # PostgreSQL's jsonb refuses NUL, so model text never carries one into a stored payload.
    return "" if value is None else str(value).replace("\x00", "").strip()


def _field(raw, key):
    """A model-supplied text field, capped so an oversized value is refused, not stored whole."""
    return _text(raw.get(key))[:MAX_FIELD_CHARS]


def _iso(value):
    return value.isoformat() if value else None


def _form_problem(form):
    """The first form error, named by its field, the way the manual page shows it."""
    for name, errors in form.errors.items():
        message = str(errors[0])
        if name == "__all__" or name not in form.fields:
            return message
        return f"{form[name].label}: {message}"
    return REFUSED


def _item_scope(raw, current, error):
    """The scope a row asks for. A chat edit never changes an existing row's scope."""
    scope = raw.get("scope")
    if scope in (None, ""):
        return current, error
    if scope not in (PlannedItem.Scope.PRIVATE, PlannedItem.Scope.HOUSEHOLD):
        return current, error or SCOPE_UNKNOWN
    return scope, error


# Planned items --------------------------------------------------------------


def _visible_planned(person):
    return PlannedItem.objects.visible_to(person)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every PlannedItem.objects.visible_to in the baseline


def _visible_goals(person):
    return SavingsGoal.objects.visible_to(person)  # pyright: ignore[reportAttributeAccessIssue] - QuerySet.as_manager() is untyped, as for every SavingsGoal.objects.visible_to in the baseline


def _ai_planned_items(person):
    """Planned items the model may name: list_planned_items' rows, private only until consent (#521)."""
    query = _visible_planned(person)
    if not household_consent_complete(person):
        query = query.filter(scope=PlannedItem.Scope.PRIVATE, owner=person)
    return query


def _planned_values(item):
    return {
        "name": item.name,
        "kind": item.kind,
        "amount_minor": item.amount_minor,
        "start_date": item.start_date.isoformat(),
        "end_date": _iso(item.end_date),
        "cadence": item.cadence,
        "category_id": item.category_id,
        "scope": item.scope,
    }


def _planned_form(person, values, item):
    """The Planned items page's own form, so a row is valid exactly when the page would accept it."""
    amount = values.get("amount_minor")
    data = {
        "name": values.get("name") or "",
        "kind": values.get("kind") or "",
        "amount": _dollars(amount) if isinstance(amount, int) and amount > 0 else "",
        "start_date": values.get("start_date") or "",
        "end_date": values.get("end_date") or "",
        "cadence": values.get("cadence") or "",
        "scope": values.get("scope") or "",
        "category": values.get("category_id") or "",
    }
    return PlannedItemForm(
        data,
        principal=person,
        has_household=current_household(person) is not None,
        household_only=item is not None and item.owner_id != person.pk,
        current_series_id=item.replaces_series_id if item is not None else None,
    )


def _planned_overrides(person, raw, values, item):
    """Apply the model's fields to `values`; returns the first problem or None."""
    error = None
    for key in ("name", "kind", "cadence", "start_date"):
        if _field(raw, key):
            values[key] = _field(raw, key)
    if "end_date" in raw:
        values["end_date"] = _field(raw, "end_date") or None
    if "amount_minor" in raw:
        minor = _whole_minor(raw.get("amount_minor"))
        if minor is None:
            error = AMOUNT_PROBLEM
        else:
            values["amount_minor"] = minor
    if _text(raw.get("category")):
        category = _category_by_name(person, raw.get("category"))
        if category is None:
            error = error or CATEGORY_PROBLEM
        else:
            values["category_id"] = category.pk
    values["scope"], error = _item_scope(raw, values["scope"], error)
    if item is not None and values["scope"] != item.scope:
        error = error or SCOPE_CHANGE
    return error


def _planned_row(person, raw):
    target = _int_or_none(raw.get("planned_item_id"))
    action = raw.get("action") or ("edit" if target is not None else "create")
    row = {"action": action, "id": None, "values": {"name": _text(raw.get("name"))[:150]}, "before": None}
    if action not in ("create", "edit", "end"):
        return {**row, "action": "create", "error": ACTION_UNKNOWN}
    item = None
    if action == "create":
        values = {"name": "", "kind": "", "amount_minor": None, "start_date": None, "end_date": None,
                  "cadence": "", "category_id": None, "scope": PlannedItem.Scope.PRIVATE}
    else:
        item = _ai_planned_items(person).filter(pk=target).first() if target is not None else None
        if item is None:
            return {**row, "error": ITEM_UNAVAILABLE}
        values = _planned_values(item)
    if action == "end":
        values["end_date"] = _field(raw, "end_date") or timezone.localdate().isoformat()
        error = None
    else:
        error = _planned_overrides(person, raw, values, item)
    if error is None:
        form = _planned_form(person, values, item)
        if form.is_valid():
            cleaned = form.cleaned_data
            values.update(name=cleaned["name"], start_date=cleaned["start_date"].isoformat(),
                          end_date=_iso(cleaned.get("end_date")))
            if item is not None and values == _planned_values(item):
                error = NOTHING_CHANGES
        else:
            error = _form_problem(form)
    return {
        "action": action,
        "id": item.pk if item is not None else None,
        "values": values,
        "before": _planned_values(item) if item is not None else None,
        "error": error,
    }


def _propose_planned_items(person, args, conversation, turn):
    items, dropped = _raw_items(args)
    rows = [_planned_row(person, raw) for raw in items]
    return _store_items(
        conversation, turn, AiProposal.Kind.PLANNED_ITEMS, rows, dropped,
        planned_item_ids=[row["id"] for row in rows if row["id"] is not None],
    )


# Savings goals ---------------------------------------------------------------


def _ai_goals(person):
    """Goals the model may name: private only until household consent is complete (#521)."""
    query = _visible_goals(person)
    if not household_consent_complete(person):
        query = query.filter(scope=SavingsGoal.Scope.PRIVATE, owner=person)
    return query


def _goal_values(goal):
    return {
        "name": goal.name,
        "target_amount_minor": goal.target_amount_minor,
        "target_date": _iso(goal.target_date),
        "priority": goal.priority,
        "depends_on_id": goal.depends_on_id,
        "scope": goal.scope,
    }


class _RowProblem(Exception):
    """A member-facing reason one card row cannot be applied."""


def _goal_check(person, values, goal, goals):
    """(problem, None) or (None, save_savings_goal payload). `goals` are the ones a dependency may name."""
    try:
        return None, _goal_payload(person, values, goal, goals)
    except _RowProblem as exc:
        return str(exc), None


def _goal_payload(person, values, goal, goals):
    name = _text(values.get("name"))
    if not name or len(name) > 150:
        raise _RowProblem("Name: enter a name of up to 150 characters.")
    minor = values.get("target_amount_minor")
    if not isinstance(minor, int) or minor <= 0:
        raise _RowProblem("Target amount: enter an amount greater than zero.")
    scope = values.get("scope")
    household = current_household(person) if scope == SavingsGoal.Scope.HOUSEHOLD else None
    if scope == SavingsGoal.Scope.HOUSEHOLD and household is None:
        raise _RowProblem("Join a household before adding a household savings goal.")
    return {
        "scope": scope,
        "name": name,
        "target_amount_minor": minor,
        "target_date": _goal_target_date(values.get("target_date")),
        "priority": _goal_priority(values.get("priority")),
        "depends_on": _goal_dependency(person, values.get("depends_on_id"), goal, goals, scope, household),
    }


def _goal_target_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise _RowProblem("Target date: Enter a valid date.") from exc


def _goal_priority(value):
    if value in (None, ""):
        return None
    priority = _int_or_none(value)
    if priority is None or not 1 <= priority <= MAX_PRIORITY:
        raise _RowProblem("Priority: enter a whole number from 1 to 1,000,000.")
    return priority


def _goal_dependency(person, depends_on_id, goal, goals, scope, household):
    if depends_on_id in (None, ""):
        return None
    dependency = goals.filter(pk=_int_or_none(depends_on_id)).first()
    if dependency is None:
        raise _RowProblem(DEPENDENCY_UNAVAILABLE)
    try:
        validate_dependency(person, goal or SavingsGoal(owner=person), dependency, scope=scope, household=household)
    except PermissionDenied as exc:
        raise _RowProblem(DEPENDENCY_UNAVAILABLE) from exc
    except ValidationError as exc:
        raise _RowProblem(f"Buy after: {_first_message(exc, REFUSED, stringify=True)}") from exc
    return dependency


def _goal_overrides(raw, values):
    error = None
    if _field(raw, "name"):
        values["name"] = _field(raw, "name")
    if "target_amount_minor" in raw:
        minor = _whole_minor(raw.get("target_amount_minor"))
        if minor is None:
            error = AMOUNT_PROBLEM
        else:
            values["target_amount_minor"] = minor
    if "target_date" in raw:
        values["target_date"] = _field(raw, "target_date") or None
    if "priority" in raw:
        values["priority"] = _whole_or_text(raw, "priority")
    if "depends_on_goal_id" in raw:
        values["depends_on_id"] = _whole_or_text(raw, "depends_on_goal_id")
    return error


def _goal_row(person, raw):
    target = _int_or_none(raw.get("goal_id"))
    action = raw.get("action") or ("edit" if target is not None else "create")
    row = {"action": action, "id": None, "values": {"name": _text(raw.get("name"))[:150]}, "before": None}
    if action not in ("create", "edit"):
        return {**row, "action": "create", "error": ACTION_UNKNOWN}
    goal = None
    if action == "create":
        values = {"name": "", "target_amount_minor": None, "target_date": None, "priority": None,
                  "depends_on_id": None, "scope": SavingsGoal.Scope.PRIVATE}
    else:
        goal = _ai_goals(person).filter(pk=target, status=SavingsGoal.Status.ACTIVE).first() if target else None
        if goal is None:
            return {**row, "error": ITEM_UNAVAILABLE}
        values = _goal_values(goal)
    error = _goal_overrides(raw, values)
    values["scope"], error = _item_scope(raw, values["scope"], error)
    if goal is not None and values["scope"] != goal.scope:
        error = error or SCOPE_CHANGE
    if error is None:
        error, payload = _goal_check(person, values, goal, _ai_goals(person))
        if payload is not None:
            dependency = payload["depends_on"]
            values.update(name=payload["name"], target_date=_iso(payload["target_date"]),
                          priority=payload["priority"], depends_on_id=dependency.pk if dependency else None)
            if goal is not None and values == _goal_values(goal):
                error = NOTHING_CHANGES
    return {
        "action": action,
        "id": goal.pk if goal is not None else None,
        "values": values,
        "before": _goal_values(goal) if goal is not None else None,
        "error": error,
    }


def _propose_savings_goals(person, args, conversation, turn):
    items, dropped = _raw_items(args)
    rows = [_goal_row(person, raw) for raw in items]
    return _store_items(conversation, turn, AiProposal.Kind.SAVINGS_GOALS, rows, dropped)


# Several budgets for one month ----------------------------------------------


def _card_month(raw):
    """The month a budgets card is for: YYYY-MM inside the budgets page's range, or None."""
    if raw in (None, ""):
        return month_start(timezone.localdate())
    try:
        parts = str(raw).strip().split("-")
        return validate_budget_month(date(int(parts[0]), int(parts[1]), 1))
    except (TypeError, ValueError, IndexError, ValidationError):
        return None


def _budget_row(person, raw, month, scope, seen):
    category = _category_by_name(person, raw.get("category"))
    minor = _whole_minor(raw.get("amount_minor"))
    row = {"category_id": category.pk if category else None, "label": _text(raw.get("category"))[:80],
           "amount_minor": minor, "budget_id": None, "before_amount_minor": None, "error": None}
    if category is None:
        row["error"] = CATEGORY_PROBLEM
    elif category.pk in seen:
        row["error"] = "That category is already in this card."
    elif minor is None:
        row["error"] = AMOUNT_PROBLEM
    else:
        seen.add(category.pk)
        existing = _budget_for(person, scope, category)
        if existing is not None:
            row["budget_id"] = existing.pk
            row["before_amount_minor"] = amount_for(existing, month)
            # Before household consent (#521) a household budget's amount must not reach the
            # model, so a no-op row answers like any other; Apply then just keeps the amount.
            visible_to_model = scope == Budget.Scope.PRIVATE or household_consent_complete(person)
            if visible_to_model and row["before_amount_minor"] == minor:
                row["error"] = NOTHING_CHANGES
    return row


def _propose_set_budgets(person, args, conversation, turn):
    month = _card_month(args.get("month"))
    household = current_household(person)
    scope = args.get("scope") or (Budget.Scope.HOUSEHOLD if household is not None else Budget.Scope.PRIVATE)
    if month is None or scope not in Budget.Scope.values or (scope == Budget.Scope.HOUSEHOLD and household is None):
        return _fail("Nothing could be proposed: use a month like 2026-03 within the next 24 months, and a valid scope.")
    items, dropped = _raw_items(args)
    seen = set()
    rows = [_budget_row(person, raw, month, scope, seen) for raw in items]
    return _store_items(
        conversation, turn, AiProposal.Kind.SET_BUDGETS, rows, dropped,
        extra={"month": month.isoformat(), "scope": scope},
        budget_ids=[row["budget_id"] for row in rows if row["budget_id"] is not None],
    )


# ---------------------------------------------------------------------------
# Apply and dismiss: the only paths that write
# ---------------------------------------------------------------------------


def proposal_for(principal, proposal_id):
    person = _person_for(principal)
    return AiProposal.objects.visible_to(person).filter(pk=proposal_id).first()


def dismiss_proposal(principal, proposal_id):
    person = _person_for(principal)
    with transaction.atomic():
        proposal = _locked_pending(person, proposal_id)
        proposal.status = AiProposal.Status.DISMISSED
        proposal.resolved_at = timezone.now()
        proposal.save(update_fields=("status", "resolved_at"))
    return proposal


def apply_proposal(principal, proposal_id, *, selected=None):
    """Re-check the proposal as the member, then apply it through the normal services.

    `selected` holds the item indexes the member ticked on an item card; None means every
    item that can be applied. Raises PermissionDenied when the proposal is not the member's.
    Raises ProposalError with a member-facing reason when it is already handled, stale, or refused.
    """
    person = _person_for(principal)
    stale = None
    stale_items = None
    try:
        with transaction.atomic():
            # Memberships before the proposal, the order unsharing and leaving use:
            # their chat purge deletes this proposal while they hold the memberships.
            lock_actor_household(person)
            proposal = _locked_pending(person, proposal_id)
            before = TransactionCorrectionHistory.objects.aggregate(top=Max("pk"))["top"] or 0
            try:
                # The approving member is the actor; the proposal and a fresh operation ID
                # survive the conversation (and its proposals) being deleted.
                with origin(AuditEvent.Source.CHAT, correlation_id=uuid.uuid4(), proposal_id=proposal.pk):
                    if proposal.kind in _ITEM_APPLIERS:
                        result = _apply_items(person, proposal.payload, selected, _ITEM_APPLIERS[proposal.kind])
                    else:
                        result = _APPLIERS[proposal.kind](person, proposal.payload)
            except PermissionDenied as exc:
                raise ProposalError(REFUSED) from exc
            except ValidationError as exc:
                raise ProposalError(_first_message(exc, REFUSED, stringify=True)) from exc
            _label_history(person, before)
            proposal.status = AiProposal.Status.APPLIED
            proposal.result = result
            proposal.resolved_at = timezone.now()
            proposal.save(update_fields=("status", "result", "resolved_at"))
    except _Stale as exc:
        stale = str(exc)
        stale_items = exc.items
    if stale is not None:
        # The write rolled back; remember why so the card stops offering Apply.
        result = {"reason": stale} if stale_items is None else {"reason": stale, "items": stale_items, "count": 0}
        AiProposal.objects.filter(pk=proposal_id, status=AiProposal.Status.PENDING).update(
            status=AiProposal.Status.STALE, result=result, resolved_at=timezone.now()
        )
        raise ProposalError(stale)
    return proposal


class _Stale(ProposalError):
    def __init__(self, message, items=None):
        super().__init__(message)
        self.items = items


def _locked_pending(person, proposal_id):
    proposal = (
        AiProposal.objects.visible_to(person).select_for_update(of=("self",)).filter(pk=proposal_id).first()
    )
    if proposal is None:
        raise PermissionDenied(_DENIED)
    if proposal.status != AiProposal.Status.PENDING:
        raise ProposalError(ALREADY_RESOLVED)
    return proposal


def _label_history(person, since_pk):
    """Mark the correction-history rows this apply wrote."""
    TransactionCorrectionHistory.objects.filter(pk__gt=since_pk, actor=person).update(
        new_description=Concat(F("new_description"), Value(VIA_CHAT))
    )


def _apply_set_category(person, payload):
    ids = [item["id"] for item in payload["items"]]
    current = {
        txn.pk: txn
        for txn in Transaction.objects.visible_to(person).filter(
            pk__in=ids,
            status=Transaction.Status.ACTIVE,
            account_id__in=visible_accounts(person).values("pk"),
        )
    }
    for item in payload["items"]:
        txn = current.get(item["id"])
        if txn is None or txn.category_id != item["category_id"] or txn.category_source != item["source"]:
            raise _Stale(NO_LONGER_APPLIES)
    if not assignable_categories(person).filter(pk=payload["category_id"]).exists():
        raise _Stale(NO_LONGER_APPLIES)
    for pk in ids:
        assign_category(person, pk, payload["category_id"])
    return {"count": len(ids)}


def _apply_create_rule(person, payload):
    rule = unsaved_rule(
        person,
        owner_kind=payload["owner"],
        description_contains=payload["description_contains"],
        account_id=payload["account_id"],
        min_amount_minor=payload["min_amount_minor"],
        max_amount_minor=payload["max_amount_minor"],
        category_id=payload["category_id"],
    )
    now_matching = [txn.pk for txn in preview_unsaved_rule(person, rule)]
    if now_matching != payload["match_ids"]:
        raise _Stale(NO_LONGER_APPLIES)
    saved = save_category_rule(
        person,
        owner_kind=payload["owner"],
        description_contains=payload["description_contains"],
        account_id=payload["account_id"],
        min_amount_minor=payload["min_amount_minor"],
        max_amount_minor=payload["max_amount_minor"],
        category_id=payload["category_id"],
        priority=0,
        enabled=True,
    )
    application, _skipped = apply_rule(person, saved.pk)
    count = application.entries.count() if application is not None else 0
    return {"rule_id": saved.pk, "count": count}


def _change_budget(person, category_id, scope, month_text, amount_minor, budget_id, before_amount_minor):
    """Save one proposed budget amount after re-checking it, holding the budget's row lock throughout.

    Returns (budget, existing, month); raises _Stale when the budget moved since the suggestion.
    """
    category = assignable_categories(person).filter(pk=category_id).first()
    if category is None:
        raise _Stale(NO_LONGER_APPLIES)
    month = parse_month(month_text)
    existing = _budget_for(person, scope, category, lock=True)
    if (existing.pk if existing else None) != budget_id:
        raise _Stale(NO_LONGER_APPLIES)
    if existing is not None and amount_for(existing, month) != before_amount_minor:
        raise _Stale(NO_LONGER_APPLIES)
    budget = save_budget(
        person,
        {"scope": scope, "category": category, "effective_month": month, "amount_minor": amount_minor},
        budget=existing,
    )
    return budget, existing, month


def _apply_set_budget(person, payload):
    budget, _existing, month = _change_budget(
        person, payload["category_id"], payload["scope"], payload["month"], payload["amount_minor"],
        payload["budget_id"], payload["before_amount_minor"],
    )
    return {"budget_id": budget.pk, "month": month_start(month).isoformat()}


def _apply_add_tags(person, payload):
    ids = [item["id"] for item in payload["items"]]
    lock_actor_household(person)
    txns = list(
        Transaction.objects.visible_to(person)
        .select_for_update(of=("self",))
        .filter(
            pk__in=ids,
            status=Transaction.Status.ACTIVE,
            account_id__in=visible_accounts(person).values("pk"),
        )
        .order_by("pk")
    )
    if len(txns) != len(ids):
        raise _Stale(NO_LONGER_APPLIES)
    tags = []
    for name in payload["tags"]:
        found = Tag.objects.visible_to(person).active().filter(name__iexact=name).first()
        tags.append(found or add_tag(person, name))
    for txn in txns:
        before = set(txn.tags.values_list("pk", flat=True))
        _apply_tags(person, txn, tags, add=True)
        if before != set(txn.tags.values_list("pk", flat=True)):
            record(person, AuditEvent.Action.TAGS_CHANGED, AuditEvent.TargetType.TRANSACTION, txn.pk,
                   audience={"account": Account(pk=txn.account_id)}, fields=("tags",), verified=True)
    return {"count": len(txns)}


_APPLIERS = {
    AiProposal.Kind.SET_CATEGORY: _apply_set_category,
    AiProposal.Kind.CREATE_RULE: _apply_create_rule,
    AiProposal.Kind.SET_BUDGET: _apply_set_budget,
    AiProposal.Kind.ADD_TAGS: _apply_add_tags,
}


# Item cards ----------------------------------------------------------------

ITEM_CREATED = "created"
ITEM_UPDATED = "updated"
ITEM_ENDED = "ended"
ITEM_SKIPPED = "skipped"
ITEM_FAILED = "failed"
ITEM_STALE = "stale"
ITEM_INVALID = "invalid"


def _apply_items(person, payload, selected, apply_row):
    """Apply the ticked rows one by one; each row commits or rolls back alone.

    A row stored with a problem is never applied, even when its index is posted. Unticked
    rows are recorded as skipped. When no ticked row applies, the whole card goes stale.
    """
    rows = payload.get("items") or []
    usable = {index for index, row in enumerate(rows) if not row.get("error")}
    chosen = usable if selected is None else usable.intersection(selected)
    if not chosen:
        raise ProposalError(NOTHING_TICKED)
    results = []
    applied = 0
    for index, row in enumerate(rows):
        if row.get("error"):
            results.append({"status": ITEM_INVALID, "message": row["error"]})
            continue
        if index not in chosen:
            results.append({"status": ITEM_SKIPPED})
            continue
        try:
            with transaction.atomic():
                outcome = apply_row(person, payload, row)
        except _Stale as exc:
            results.append({"status": ITEM_STALE, "message": str(exc)})
        except PermissionDenied:
            results.append({"status": ITEM_FAILED, "message": ITEM_UNAVAILABLE})
        except ValidationError as exc:
            results.append({"status": ITEM_FAILED, "message": _first_message(exc, REFUSED, stringify=True)})
        except IntegrityError:
            results.append({"status": ITEM_FAILED, "message": REFUSED})
        else:
            applied += 1
            results.append(outcome)
    if not applied:
        raise _Stale(NONE_APPLIED, items=results)
    return {"items": results, "count": applied}


def _apply_planned_row(person, _payload, row):
    item = None
    if row["action"] != "create":
        item = _visible_planned(person).select_for_update().filter(pk=row["id"]).first()
        if item is None:
            raise PermissionDenied(_DENIED)
        if _planned_values(item) != row["before"]:
            raise _Stale(NO_LONGER_APPLIES)
    form = _planned_form(person, row["values"], item)
    if not form.is_valid():
        raise ValidationError(_form_problem(form))
    values = form.save_payload()
    # Keep what the card does not show: the replaced series and the enabled switch.
    values["replaces_series"] = item.replaces_series if item is not None else None
    saved = save_planned_item(person, values, item=item)
    status = {"create": ITEM_CREATED, "end": ITEM_ENDED}.get(row["action"], ITEM_UPDATED)
    return {"status": status, "id": saved.pk}


def _apply_goal_row(person, _payload, row):
    goal = None
    if row["action"] != "create":
        goal = (
            _visible_goals(person)
            .select_for_update()
            .filter(pk=row["id"], status=SavingsGoal.Status.ACTIVE)
            .first()
        )
        if goal is None:
            raise PermissionDenied(_DENIED)
        if _goal_values(goal) != row["before"]:
            raise _Stale(NO_LONGER_APPLIES)
    problem, values = _goal_check(person, row["values"], goal, _visible_goals(person))
    if problem is not None:
        raise ValidationError(problem)
    saved = save_savings_goal(person, values, goal=goal)
    return {"status": ITEM_CREATED if goal is None else ITEM_UPDATED, "id": saved.pk}


def _apply_budget_row(person, payload, row):
    if row["before_amount_minor"] is not None and row["before_amount_minor"] == row["amount_minor"]:
        # Proposed before household consent, so the model was not told; nothing to write or audit.
        raise ValidationError(NOTHING_CHANGES)
    budget, existing, _month = _change_budget(
        person, row["category_id"], payload["scope"], payload["month"], row["amount_minor"],
        row["budget_id"], row["before_amount_minor"],
    )
    return {"status": ITEM_CREATED if existing is None else ITEM_UPDATED, "id": budget.pk}


_ITEM_APPLIERS = {
    AiProposal.Kind.PLANNED_ITEMS: _apply_planned_row,
    AiProposal.Kind.SAVINGS_GOALS: _apply_goal_row,
    AiProposal.Kind.SET_BUDGETS: _apply_budget_row,
}
ITEM_KINDS = frozenset(_ITEM_APPLIERS)


# ---------------------------------------------------------------------------
# Cards: what each pending proposal will change, read fresh as the member
# ---------------------------------------------------------------------------


def card_for(person, proposal):
    """Template context for one proposal card. Only rows the member can still see appear."""
    payload = proposal.payload
    card = {"proposal": proposal, "kind": proposal.kind, "pending": proposal.status == AiProposal.Status.PENDING}
    if proposal.status == AiProposal.Status.STALE:
        card["stale_reason"] = (proposal.result or {}).get("reason") or NO_LONGER_APPLIES
    builder = _CARD_BUILDERS[proposal.kind]
    builder(person, proposal, payload, card)
    return card


def _category_name(person, category_id):
    from .models import Category

    row = Category.objects.visible_to(person).filter(pk=category_id).first()
    return row.name if row else "an unavailable category"


def _card_rows(person, items):
    ids = [item["id"] for item in items]
    rows = {
        txn.pk: txn
        for txn in Transaction.objects.visible_to(person)
        .filter(pk__in=ids, account_id__in=visible_accounts(person).values("pk"))
        .annotate(_excluded=exclusion_exists_for(person))
        .select_related("account", "category")
    }
    return [rows[pk] for pk in ids if pk in rows]


def _card_set_category(person, proposal, payload, card):
    rows = _card_rows(person, payload["items"])
    card["category_name"] = _category_name(person, payload["category_id"])
    card["transactions"] = rows[:CARD_ROWS]
    card["count"] = len(rows)
    card["more"] = max(0, len(rows) - CARD_ROWS)
    card["result_count"] = (proposal.result or {}).get("count")


def _card_add_tags(person, proposal, payload, card):
    rows = _card_rows(person, payload["items"])
    card["tag_names"] = payload["tags"]
    card["transactions"] = rows[:CARD_ROWS]
    card["count"] = len(rows)
    card["more"] = max(0, len(rows) - CARD_ROWS)
    card["result_count"] = (proposal.result or {}).get("count")


def _card_create_rule(person, proposal, payload, card):
    card["description_contains"] = payload["description_contains"]
    card["category_name"] = _category_name(person, payload["category_id"])
    card["owner_label"] = "Household rule" if payload["owner"] == "household" else "Personal rule"
    card["amount_range"] = _amount_range(payload)
    if proposal.status == AiProposal.Status.PENDING:
        try:
            rule = unsaved_rule(
                person,
                owner_kind=payload["owner"],
                description_contains=payload["description_contains"],
                account_id=payload["account_id"],
                min_amount_minor=payload["min_amount_minor"],
                max_amount_minor=payload["max_amount_minor"],
                category_id=payload["category_id"],
            )
            matches = preview_unsaved_rule(person, rule)
        except (ValidationError, PermissionDenied):
            matches = []
            card["unavailable"] = True
        card["matches"] = matches[:CARD_ROWS]
        card["count"] = len(matches)
        card["more"] = max(0, len(matches) - CARD_ROWS)
    else:
        card["result_count"] = (proposal.result or {}).get("count")
        rule_id = (proposal.result or {}).get("rule_id")
        if rule_id:
            card["rule_url"] = reverse("category-rule-detail", args=[rule_id])


def _amount_range(payload):
    low, high = payload.get("min_amount_minor"), payload.get("max_amount_minor")
    if low is None and high is None:
        return ""
    parts = []
    if low is not None:
        parts.append(f"at least {format_minor(low, 'USD')}")
    if high is not None:
        parts.append(f"at most {format_minor(high, 'USD')}")
    return " and ".join(parts)


def _card_set_budget(person, proposal, payload, card):
    month = parse_month(payload["month"])
    card["category_name"] = _category_name(person, payload["category_id"])
    card["month_label"] = month.strftime("%B %Y")
    card["new_amount"] = format_minor(payload["amount_minor"], "USD")
    before = payload["before_amount_minor"]
    card["before_amount"] = None if before is None else format_minor(before, "USD")
    card["scope_label"] = "Household" if payload["scope"] == Budget.Scope.HOUSEHOLD else "Private"
    card["budgets_url"] = f"{reverse('budgets')}?month={month.isoformat()[:7]}"


# Item cards: each row reads its target fresh as the member; payload values are what
# the model proposed, never another member's data.

_OUTCOME_TEXT = {
    ITEM_CREATED: "Created.",
    ITEM_UPDATED: "Updated.",
    ITEM_ENDED: "Ended.",
    ITEM_SKIPPED: "Skipped: not ticked.",
}


def _outcome_text(outcome):
    if not isinstance(outcome, dict):
        return ""
    status = outcome.get("status")
    if status in _OUTCOME_TEXT:
        return _OUTCOME_TEXT[status]
    return f"Not applied: {outcome.get('message') or REFUSED}"


def _card_items(person, proposal, payload, card, describe):
    outcomes = (proposal.result or {}).get("items") or []
    rows = []
    for index, item in enumerate(payload.get("items") or []):
        row = {"changes": [], "summary": "", "url": "", "unavailable": False, **describe(person, payload, item, card["pending"])}
        row["index"] = index
        row["error"] = item.get("error")
        row["outcome"] = _outcome_text(outcomes[index]) if index < len(outcomes) else ""
        row["applied"] = index < len(outcomes) and outcomes[index].get("status") in (ITEM_CREATED, ITEM_UPDATED, ITEM_ENDED)
        row["selectable"] = card["pending"] and not row["error"] and not row["unavailable"]
        rows.append(row)
    card["item_rows"] = rows
    card["count"] = len(rows)
    card["result_count"] = (proposal.result or {}).get("count")
    if card["pending"] and not any(row["selectable"] for row in rows):
        card["unavailable"] = True


def _change_rows(labels, before, after, show):
    return [
        {"label": label, "before": show(key, before.get(key)), "after": show(key, after.get(key))}
        for key, label in labels
        if before.get(key) != after.get(key)
    ]


def _choice_label(choices, value):
    try:
        return choices(value).label
    except ValueError:
        return _text(value) or "not set"


def _money(value):
    return format_minor(value, "USD") if isinstance(value, int) and not isinstance(value, bool) else "not set"


_PLANNED_LABELS = (
    ("name", "Name"), ("kind", "Kind"), ("amount_minor", "Amount"), ("cadence", "Cadence"),
    ("start_date", "Start"), ("end_date", "End"), ("category_id", "Category"), ("scope", "Scope"),
)


def _planned_text(person, key, value):
    if key == "amount_minor":
        return _money(value)
    if key == "kind":
        return _choice_label(PlannedItem.Kind, value)
    if key == "cadence":
        return _choice_label(PlannedItem.Cadence, value)
    if key == "scope":
        return _choice_label(PlannedItem.Scope, value)
    if key == "category_id":
        return _category_name(person, value) if value else "No category"
    if key == "end_date":
        return _text(value) or "no end date"
    return _text(value) or "not set"


def _describe_planned(person, _payload, item, pending):
    values = item.get("values") or {}
    name = _text(values.get("name"))[:150] or "Unnamed item"
    action = item.get("action")
    if action == "create":
        kind = values.get("kind") if values.get("kind") in PlannedItem.Kind.values else "item"
        parts = [
            f"{_planned_text(person, 'amount_minor', values.get('amount_minor'))} "
            f"{_planned_text(person, 'cadence', values.get('cadence')).lower()}",
            f"from {_planned_text(person, 'start_date', values.get('start_date'))}",
        ]
        if values.get("end_date"):
            parts.append(f"to {_text(values['end_date'])}")
        summary = " ".join(parts)
        summary += f" · {_planned_text(person, 'category_id', values.get('category_id'))}"
        summary += f" · {_planned_text(person, 'scope', values.get('scope'))}"
        return {"title": f"New {kind}: {name}", "summary": summary}
    verb = "End" if action == "end" else "Edit"
    current = _visible_planned(person).filter(pk=item.get("id")).first() if item.get("id") else None
    if current is None:
        # A row refused for an unknown target holds only the model's own text; a row that found
        # its target holds that target's snapshot, which is never shown once the target is hidden.
        if item.get("error") and item.get("id") is None:
            return {"title": f"{verb}: {name}", "unavailable": True}
        return {"title": f"{verb} a planned item", "unavailable": True,
                "summary": "This planned item is no longer available."}
    return _describe_edit(
        f"{verb}: {current.name}", item, values, _planned_values(current), pending,
        _PLANNED_LABELS, partial(_planned_text, person), reverse("planned-item-edit", args=[current.pk]),
    )


def _describe_edit(title, item, values, current_values, pending, labels, show, url):
    """Before → after from the snapshot the suggestion was made against.

    The target is visible to the member now (callers check). A pending row whose target
    changed since then is shown as such and cannot be ticked; Apply would refuse it anyway.
    """
    before = item.get("before") or {}
    row = {"title": title, "changes": _change_rows(labels, before, values, show), "url": url}
    if pending and current_values != before:
        row.update(unavailable=True, summary="This changed after the suggestion was made. Ask again for a fresh one.")
    return row


_GOAL_LABELS = (
    ("name", "Name"), ("target_amount_minor", "Target"), ("target_date", "Target date"),
    ("priority", "Priority"), ("depends_on_id", "Buy after"), ("scope", "Scope"),
)


def _goal_text(person, key, value):
    if key == "target_amount_minor":
        return _money(value)
    if key == "scope":
        return _choice_label(SavingsGoal.Scope, value)
    if key == "target_date":
        return _text(value) or "no target date"
    if key == "priority":
        return _text(value) or "unranked"
    if key == "depends_on_id":
        if value in (None, ""):
            return "nothing"
        goal = _visible_goals(person).filter(pk=_int_or_none(value)).first()
        return goal.name if goal else "an unavailable goal"
    return _text(value) or "not set"


def _describe_goal(person, _payload, item, pending):
    values = item.get("values") or {}
    name = _text(values.get("name"))[:150] or "Unnamed goal"
    show = partial(_goal_text, person)
    if item.get("action") == "create":
        parts = [f"Target {show('target_amount_minor', values.get('target_amount_minor'))}"]
        if values.get("target_date"):
            parts.append(f"by {_text(values['target_date'])}")
        if values.get("priority") not in (None, ""):
            parts.append(f"· priority {_text(values['priority'])}")
        if values.get("depends_on_id") not in (None, ""):
            parts.append(f"· after {show('depends_on_id', values['depends_on_id'])}")
        parts.append(f"· {show('scope', values.get('scope'))}")
        return {"title": f"New goal: {name}", "summary": " ".join(parts)}
    current = _visible_goals(person).filter(pk=item.get("id")).first() if item.get("id") else None
    if current is None:
        if item.get("error") and item.get("id") is None:
            return {"title": f"Adjust: {name}", "unavailable": True}
        return {"title": "Adjust a goal", "unavailable": True, "summary": "This goal is no longer available."}
    return _describe_edit(
        f"Adjust: {current.name}", item, values, _goal_values(current), pending,
        _GOAL_LABELS, show, reverse("savings-goal-edit", args=[current.pk]),
    )


def _describe_budget(person, payload, item, pending):
    category_id = item.get("category_id")
    title = _category_name(person, category_id) if category_id else (_text(item.get("label")) or "Unnamed category")
    amount = _money(item.get("amount_minor"))
    before = item.get("before_amount_minor")
    row = (
        {"title": title, "summary": f"New budget of {amount}"} if before is None
        else {"title": title, "changes": [{"label": "Amount", "before": _money(before), "after": amount}]}
    )
    if item.get("error") or not pending:
        return row
    if before == item.get("amount_minor"):
        return {"title": title, "unavailable": True, "summary": f"Already {amount}; nothing would change."}
    if _budget_snapshot(person, payload, category_id) != (item.get("budget_id"), before):
        row.update(unavailable=True, summary="This changed after the suggestion was made. Ask again for a fresh one.")
    return row


def _budget_snapshot(person, payload, category_id):
    """(budget id, amount for the card's month) as the member sees it now, or (None, None)."""
    category = assignable_categories(person).filter(pk=category_id).first()
    existing = _budget_for(person, payload.get("scope"), category) if category is not None else None
    if existing is None:
        return None, None
    return existing.pk, amount_for(existing, parse_month(payload.get("month")))


def _card_planned_items(person, proposal, payload, card):
    card["heading"] = "Planned items"
    card["page_url"] = reverse("planned-items")
    card["page_label"] = "Open planned items"
    _card_items(person, proposal, payload, card, _describe_planned)


def _card_savings_goals(person, proposal, payload, card):
    card["heading"] = "Savings goals"
    card["page_url"] = reverse("savings-goals")
    card["page_label"] = "Open savings goals"
    _card_items(person, proposal, payload, card, _describe_goal)


def _card_set_budgets(person, proposal, payload, card):
    month = parse_month(payload.get("month"))
    scope = "Household" if payload.get("scope") == Budget.Scope.HOUSEHOLD else "Private"
    card["heading"] = f"{scope} budgets for {month.strftime('%B %Y')}"
    card["page_url"] = f"{reverse('budgets')}?month={month.isoformat()[:7]}"
    card["page_label"] = "Open budgets"
    _card_items(person, proposal, payload, card, _describe_budget)


_CARD_BUILDERS = {
    AiProposal.Kind.SET_CATEGORY: _card_set_category,
    AiProposal.Kind.CREATE_RULE: _card_create_rule,
    AiProposal.Kind.SET_BUDGET: _card_set_budget,
    AiProposal.Kind.ADD_TAGS: _card_add_tags,
    AiProposal.Kind.PLANNED_ITEMS: _card_planned_items,
    AiProposal.Kind.SAVINGS_GOALS: _card_savings_goals,
    AiProposal.Kind.SET_BUDGETS: _card_set_budgets,
}
