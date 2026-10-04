"""Background AI phrasing of stored monthly review facts. Never sends raw transactions."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from django.urls import Resolver404, resolve

from .ai_jobs import enqueue_job
from .ai_services import member_has_ai, run_structured
from .ai_types import ProviderResult
from .alert_services import settings_for
from .category_services import current_household
from .models import Account, MonthlyReview, RecurringSeries, Transaction
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
        "item_id",
        "transaction_id",
        "account_id",
        "merchant_key",
        # Cache key for stored reviews, not a fact: its numbers must not ground AI text.
        "unusual_settings",
    }
)
# While a household member is not in acceptance, only these facts are sent.
# Recurring and large-transaction items stay only when every record behind them is
# the member's own private record. Unusual-spending flags are recomputed from the
# member's own private cash-flow accounts so baselines and medians cannot carry
# household amounts.
PRIVATE_SAFE_KEYS = ("month", "month_label")
RECURRING_LIST_KEYS = ("price_changes", "missed_charges", "cancellations", "new_recurring")
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
    household = current_household(person)
    if household is None or household_ai_allowed(household):
        return _drop_keys(facts)
    return _drop_keys(_own_private_facts(person, facts))


def queue_monthly_review_phrasing(person, review):
    if review is None or not monthly_review_ai_on(person):
        return None
    return enqueue_job(
        person,
        feature=FEATURE,
        input_refs={
            "monthly_review_id": review.pk,
            "generated_at": review.generated_at.isoformat(),
        },
    )


def visible_phrasing(person, review):
    if review is None or not monthly_review_ai_on(person):
        return "", ""
    paragraph = (review.ai_paragraph or "").strip()
    if not paragraph:
        return "", ""
    return paragraph, phrasing_label(review.ai_backend)


def run_monthly_review_job(person, job, *, backend, session_id="", on_session=None, connection=None):
    refs = job.input_refs or {}
    review = MonthlyReview.objects.filter(pk=refs.get("monthly_review_id"), person=person).first()
    if review is None:
        return ProviderResult(ok=True, answer="", session_id="skipped")
    if refs.get("generated_at") != review.generated_at.isoformat():
        return ProviderResult(ok=True, answer="", session_id="skipped")
    if not monthly_review_ai_on(person):
        return ProviderResult(ok=True, answer="", session_id="skipped")
    # Stored facts reflect the accounts visible when they were computed. If access has
    # changed since, send nothing; the next view regenerates the review and queues again.
    from .monthly_review import visibility_key

    if review.visibility_key != visibility_key(person):
        return ProviderResult(ok=True, answer="", session_id="skipped")
    # Filter at run time: household acceptance can change while the job waits.
    facts = facts_payload_for_ai(person, review.facts)
    prompt = PROMPT + json.dumps(facts, default=str)
    result = run_structured(
        person,
        prompt,
        feature=FEATURE,
        backend=backend,
        session_id=session_id,
        on_session=on_session,
        connection=connection,
    )
    if not result.ok:
        return result
    paragraph = _extract_paragraph(result.answer)
    grounded = bool(paragraph) and paragraph_is_grounded(paragraph, facts)
    # Write only if the review was not regenerated while the call ran.
    MonthlyReview.objects.filter(
        pk=review.pk,
        generated_at=review.generated_at,
        visibility_key=review.visibility_key,
    ).update(
        ai_paragraph=paragraph if grounded else "",
        ai_backend=(backend or "") if grounded else "",
    )
    return result


def _drop_keys(value):
    if isinstance(value, dict):
        return {key: _drop_keys(item) for key, item in value.items() if key not in DROP_KEYS}
    if isinstance(value, list):
        return [_drop_keys(item) for item in value]
    return value


def _own_private_facts(person, facts):
    filtered = {key: facts[key] for key in PRIVATE_SAFE_KEYS if key in facts}
    filtered["large_transactions"] = [
        item for item in facts.get("large_transactions") or [] if _own_private_transaction(person, item)
    ]
    filtered["unusual"] = _private_unusual_flags(person, facts)
    for key in RECURRING_LIST_KEYS:
        filtered[key] = [item for item in facts.get(key) or [] if _own_private_series(person, item)]
    return filtered


def _own_private_accounts(person):
    return Account.objects.filter(scope=Account.Scope.PRIVATE, owner=person)


def _private_unusual_flags(person, facts):
    own = list(_own_private_accounts(person).for_cash_flow())
    if not own:
        return []
    month = _month_from_facts(facts)
    if month is None:
        return []
    from .unusual_spending import compute_unusual_flags

    return compute_unusual_flags(person, month, accounts=own)


def _month_from_facts(facts):
    from datetime import date

    raw = facts.get("month")
    if not raw:
        return None
    try:
        year_s, month_s = str(raw).split("-", 1)
        return date(int(year_s), int(month_s), 1)
    except (TypeError, ValueError):
        return None


def _own_private_transaction(person, item):
    if not isinstance(item, dict):
        return False
    try:
        match = resolve(str(item.get("url") or "").split("?", 1)[0])
    except Resolver404:
        return False
    transaction_id = match.kwargs.get("transaction_id")
    if match.url_name != "transaction-edit" or transaction_id is None:
        return False
    return Transaction.objects.filter(pk=transaction_id, account__in=_own_private_accounts(person)).exists()


def _own_private_series(person, item):
    if not isinstance(item, dict) or not isinstance(item.get("series_id"), int):
        return False
    series = RecurringSeries.objects.visible_to(person).filter(pk=item["series_id"]).first()
    if series is None:
        return False
    account_ids = set(series.members.values_list("transaction__account_id", flat=True))
    if not account_ids:
        return False
    own = set(_own_private_accounts(person).filter(pk__in=account_ids).values_list("pk", flat=True))
    return own == account_ids


def _extract_paragraph(answer):
    text = (answer or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*", "", text).strip()
        text = text.removesuffix("```").strip()
    return text


def _allowed_numbers(facts):
    """Every number a fact states, as an absolute value; a paragraph may only repeat these."""
    values = set()

    def add(value):
        if value is not None:
            values.add(abs(value))

    def walk(node, key=""):
        if isinstance(node, dict):
            for child_key, item in node.items():
                walk(item, str(child_key))
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, bool):
            return
        elif isinstance(node, int):
            add(Decimal(node) / Decimal(100) if key.endswith("_minor") else Decimal(node))
        elif isinstance(node, float):
            add(Decimal(str(node)))
        elif isinstance(node, str):
            for token in NUMBER_RE.findall(node):
                add(_as_decimal(token))

    walk(facts)
    return values


def paragraph_is_grounded(paragraph, facts):
    allowed = _allowed_numbers(facts)
    for token in NUMBER_RE.findall(paragraph or ""):
        parsed = _as_decimal(token)
        if parsed is None or abs(parsed) not in allowed:
            return False
    return True


def _as_decimal(token):
    text = (token or "").replace(",", "").rstrip("%").strip()
    if not text or text in {".", "-", "+", "-.", "+."}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
