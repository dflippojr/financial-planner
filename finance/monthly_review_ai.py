"""Background AI phrasing of stored monthly review facts. Never sends raw transactions."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from .ai_jobs import enqueue_job
from .ai_services import connection_for, member_has_ai, run_structured
from .ai_types import ProviderResult
from .alert_services import settings_for
from .category_services import current_household
from .models import Account, Budget, MonthlyReview, RecurringSeries, SavingsGoal, Transaction
from .policy_services import household_ai_allowed

FEATURE = "monthly_review"
BACKEND_LABELS = {
    "local": "Local model",
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
}
DROP_KEYS = frozenset(
    {
        "url",
        "cash_flow_url",
        "spending_url",
        "missing_import_url",
        "recurring_url",
        "budgets_url",
        "goals_url",
        "net_worth_url",
        "series_id",
        "key",
    }
)
MIXED_ACCOUNT_KEYS = frozenset(
    {
        "income_minor",
        "spending_minor",
        "net_minor",
        "income_display",
        "spending_display",
        "net_display",
        "prior",
        "year_ago",
        "missing_import",
        "category_increases",
        "category_decreases",
        "net_worth_minor",
        "net_worth_display",
        "net_worth_prior_minor",
        "net_worth_prior_display",
        "net_worth_change_minor",
        "net_worth_change_display",
    }
)
NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
PROMPT = (
    "Phrase the following monthly review facts as a short paragraph of three to five "
    "plain sentences. Stay neutral. Do not give advice. Use only numbers that appear in "
    "the facts. Do not invent amounts, percents, dates, or counts. Reply with the "
    "paragraph only.\n\nFacts JSON:\n"
)


def backend_label(backend):
    return BACKEND_LABELS.get(backend, backend or "Unknown backend")


def phrasing_label(backend):
    return f"AI-generated · {backend_label(backend)}"


def monthly_review_ai_on(person):
    if person is None or not member_has_ai(person):
        return False
    return bool(settings_for(person).monthly_review_ai_enabled)


def facts_payload_for_ai(person, facts):
    payload = _drop_keys(facts)
    household = current_household(person)
    if household is None or household_ai_allowed(household):
        return payload
    return _omit_household_derived(person, payload)


def queue_monthly_review_phrasing(person, review):
    if review is None or not monthly_review_ai_on(person):
        return None
    payload = facts_payload_for_ai(person, review.facts)
    return enqueue_job(
        person,
        feature=FEATURE,
        input_refs={
            "monthly_review_id": review.pk,
            "generated_at": review.generated_at.isoformat(),
            "facts": payload,
        },
    )


def visible_phrasing(person, review):
    if review is None or not monthly_review_ai_on(person):
        return "", ""
    paragraph = (review.ai_paragraph or "").strip()
    if not paragraph:
        return "", ""
    return paragraph, phrasing_label(review.ai_backend)


def paragraph_is_grounded(paragraph, facts):
    allowed_values, blob = _allowed_numbers(facts)
    for token in NUMBER_RE.findall(paragraph or ""):
        if _token_allowed(token, allowed_values, blob):
            continue
        return False
    return True


def run_monthly_review_job(person, job, *, backend, session_id="", on_session=None):
    refs = job.input_refs or {}
    review = MonthlyReview.objects.filter(pk=refs.get("monthly_review_id"), person=person).first()
    if review is None:
        return ProviderResult(ok=True, answer="", session_id="skipped")
    if refs.get("generated_at") != review.generated_at.isoformat():
        return ProviderResult(ok=True, answer="", session_id="skipped")
    facts = refs.get("facts")
    if not isinstance(facts, dict):
        facts = facts_payload_for_ai(person, review.facts)
    prompt = PROMPT + json.dumps(facts, default=str)
    result = run_structured(
        person,
        prompt,
        feature=FEATURE,
        backend=backend,
        session_id=session_id,
        on_session=on_session,
    )
    if not result.ok:
        return result
    paragraph = _extract_paragraph(result.answer)
    if paragraph and paragraph_is_grounded(paragraph, facts):
        review.ai_paragraph = paragraph
        review.ai_backend = backend or ""
        review.save(update_fields=("ai_paragraph", "ai_backend"))
    else:
        review.ai_paragraph = ""
        review.ai_backend = ""
        review.save(update_fields=("ai_paragraph", "ai_backend"))
    return result


def _drop_keys(value):
    if isinstance(value, dict):
        return {key: _drop_keys(item) for key, item in value.items() if key not in DROP_KEYS}
    if isinstance(value, list):
        return [_drop_keys(item) for item in value]
    return value


def _omit_household_derived(person, payload):
    filtered = {key: value for key, value in payload.items() if key not in MIXED_ACCOUNT_KEYS}
    filtered["large_transactions"] = [
        item for item in filtered.get("large_transactions") or [] if _private_transaction_item(person, item)
    ]
    filtered["price_changes"] = [
        item for item in filtered.get("price_changes") or [] if _private_series_item(person, item)
    ]
    filtered["missed_charges"] = [
        item for item in filtered.get("missed_charges") or [] if _private_series_item(person, item)
    ]
    filtered["cancellations"] = [
        item for item in filtered.get("cancellations") or [] if _private_series_item(person, item)
    ]
    filtered["new_recurring"] = [
        item for item in filtered.get("new_recurring") or [] if _private_series_item(person, item)
    ]
    filtered["budgets_over"] = [
        item for item in filtered.get("budgets_over") or [] if _private_budget_item(person, item)
    ]
    remaining = filtered.get("largest_remaining")
    if remaining is not None and not _private_budget_item(person, remaining):
        filtered["largest_remaining"] = None
    filtered["savings_goals"] = [
        item for item in filtered.get("savings_goals") or [] if _private_goal_item(person, item)
    ]
    return filtered


def _private_transaction_item(person, item):
    if not isinstance(item, dict):
        return False
    rows = Transaction.objects.visible_to(person).filter(
        description=item.get("description") or "",
        transaction_date=item.get("date") or None,
        account__scope=Account.Scope.PRIVATE,
        account__owner=person,
    )
    return rows.exists()


def _private_series_item(person, item):
    if not isinstance(item, dict):
        return False
    series = (
        RecurringSeries.objects.visible_to(person)
        .filter(display_name=item.get("name") or "")
        .prefetch_related("members__transaction__account")
        .first()
    )
    if series is None:
        return False
    accounts = [member.transaction.account for member in series.members.all()]
    if not accounts:
        return False
    return all(account.scope == Account.Scope.PRIVATE and account.owner_id == person.pk for account in accounts)


def _private_budget_item(person, item):
    if not isinstance(item, dict):
        return False
    wanted = item.get("name") or ""
    budgets = Budget.objects.visible_to(person).filter(scope=Budget.Scope.PRIVATE, owner=person).select_related(
        "category"
    )
    for budget in budgets:
        label = "Overall spending" if budget.category_id is None else budget.category.name
        if label == wanted:
            return True
    return False


def _private_goal_item(person, item):
    if not isinstance(item, dict):
        return False
    return SavingsGoal.objects.visible_to(person).filter(
        name=item.get("name") or "",
        scope=SavingsGoal.Scope.PRIVATE,
        owner=person,
    ).exists()


def _extract_paragraph(answer):
    text = (answer or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:\w+)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


def _allowed_numbers(facts):
    values = set()
    blob = json.dumps(facts, default=str)

    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                walk(item)
                if isinstance(item, int) and str(key).endswith("_minor"):
                    values.add(Decimal(item) / Decimal(100))
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, bool):
            return
        elif isinstance(node, int):
            values.add(Decimal(node))
        elif isinstance(node, float):
            values.add(Decimal(str(node)))
        elif isinstance(node, str):
            for token in NUMBER_RE.findall(node):
                parsed = _as_decimal(token)
                if parsed is not None:
                    values.add(parsed)

    walk(facts)
    return values, blob


def _token_allowed(token, allowed_values, blob):
    stripped = token.replace(",", "").rstrip("%")
    if stripped and stripped in blob:
        return True
    if token in blob:
        return True
    parsed = _as_decimal(token)
    if parsed is None:
        return False
    return parsed in allowed_values


def _as_decimal(token):
    text = (token or "").replace(",", "").rstrip("%").strip()
    if not text or text in {".", "-", "+", "-.", "+."}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
