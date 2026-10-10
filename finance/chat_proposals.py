"""Changes the chat model can suggest. A tool call only stores a proposal; nothing is written
until the member presses Apply, and Apply runs the same service functions as the normal pages.

Proposals never touch accounts, sharing, connections, or deletion.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Max, Value
from django.db.models.functions import Concat
from django.urls import reverse
from django.utils import timezone

from .access import first_message as _first_message
from .ai_tools import visible_accounts
from .ai_types import ToolResult, ToolSpec
from .audit_services import origin, record
from .bulk_edit_services import _apply_tags
from .budget_services import amount_for, parse_month, save_budget
from .cash_flow import format_minor
from .category_services import (
    _DENIED,
    _person_for,
    assign_category,
    assignable_categories,
    current_household,
    exclusion_exists_for,
)
from .lifecycle_services import lock_actor_household
from .models import (
    AiProposal,
    Account,
    AuditEvent,
    Budget,
    Tag,
    Transaction,
    TransactionCorrectionHistory,
)
from .months import month_start
from .rule_services import apply_rule, preview_unsaved_rule, save_category_rule, unsaved_rule
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


def _store(conversation, turn, kind, payload, account_ids):
    proposal = AiProposal.objects.create(conversation=conversation, message=turn, kind=kind, payload=payload)
    text = f"Proposal {proposal.pk} is shown to the member as a card. {NOTHING_APPLIED}"
    return ToolResult(text=text, account_ids=tuple(sorted(set(account_ids))))


def _fail(text):
    return ToolResult(text=text, ok=False)


def _category_by_name(person, name):
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    return assignable_categories(person).filter(name__iexact=cleaned).first()


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


def _budget_for(person, scope, category):
    query = Budget.objects.filter(status=Budget.Status.ACTIVE, scope=scope, category=category)
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
    return _store(conversation, turn, AiProposal.Kind.SET_BUDGET, payload, [])


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


def apply_proposal(principal, proposal_id):
    """Re-check the proposal as the member, then apply it through the normal services.

    Raises PermissionDenied when the proposal is not the member's. Raises ProposalError
    with a member-facing reason when it is already handled, stale, or refused.
    """
    person = _person_for(principal)
    stale = None
    try:
        with transaction.atomic():
            proposal = _locked_pending(person, proposal_id)
            before = TransactionCorrectionHistory.objects.aggregate(top=Max("pk"))["top"] or 0
            try:
                # The approving member is the actor; the proposal and a fresh operation ID
                # survive the conversation (and its proposals) being deleted.
                with origin(AuditEvent.Source.CHAT, correlation_id=uuid.uuid4(), proposal_id=proposal.pk):
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
    if stale is not None:
        # The write rolled back; remember why so the card stops offering Apply.
        AiProposal.objects.filter(pk=proposal_id, status=AiProposal.Status.PENDING).update(
            status=AiProposal.Status.STALE, result={"reason": stale}, resolved_at=timezone.now()
        )
        raise ProposalError(stale)
    return proposal


class _Stale(ProposalError):
    pass


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


def _apply_set_budget(person, payload):
    category = assignable_categories(person).filter(pk=payload["category_id"]).first()
    if category is None:
        raise _Stale(NO_LONGER_APPLIES)
    month = parse_month(payload["month"])
    existing = _budget_for(person, payload["scope"], category)
    if (existing.pk if existing else None) != payload["budget_id"]:
        raise _Stale(NO_LONGER_APPLIES)
    if existing is not None and amount_for(existing, month) != payload["before_amount_minor"]:
        raise _Stale(NO_LONGER_APPLIES)
    budget = save_budget(
        person,
        {
            "scope": payload["scope"],
            "category": category,
            "effective_month": month,
            "amount_minor": payload["amount_minor"],
        },
        budget=existing,
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


_CARD_BUILDERS = {
    AiProposal.Kind.SET_CATEGORY: _card_set_category,
    AiProposal.Kind.CREATE_RULE: _card_create_rule,
    AiProposal.Kind.SET_BUDGET: _card_set_budget,
    AiProposal.Kind.ADD_TAGS: _card_add_tags,
}
