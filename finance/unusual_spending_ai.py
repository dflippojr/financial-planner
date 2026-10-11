"""Background AI phrasing of unusual-spending flags. Never sends raw transactions."""

from __future__ import annotations

from .ai_jobs import enqueue_review_phrasing
from .ai_services import member_has_ai, run_structured
from .alert_services import settings_for
from .monthly_review_ai import (
    _run_review_phrasing_job,
    facts_payload_for_ai,
    phrasing_label,
)

FEATURE = "unusual_spending"
PROMPT = (
    "Phrase the following unusual-spending flags as two or three plain sentences. "
    "Stay neutral. Do not give advice. Use only numbers that appear in the facts. "
    "Do not invent amounts, percents, dates, or counts. Reply with the sentences only.\n\n"
    "Facts JSON:\n"
)


def unusual_spending_ai_on(person):
    if person is None or not member_has_ai(person):
        return False
    return bool(settings_for(person).unusual_spending_ai_enabled)


def unusual_facts_for_ai(person, facts):
    # Restricted household AI recomputes unusual flags from private accounts in facts_payload_for_ai.
    payload = facts_payload_for_ai(person, facts)
    return {
        "month": payload.get("month"),
        "month_label": payload.get("month_label"),
        "unusual": payload.get("unusual") or [],
        "unusual_omitted_count": payload.get("unusual_omitted_count", 0),
    }


def queue_unusual_phrasing(person, review, *, exclude_pk=None):
    if review is None or not unusual_spending_ai_on(person):
        return None
    flags = (review.facts or {}).get("unusual") or []
    if not flags:
        return None
    return enqueue_review_phrasing(person, feature=FEATURE, review=review, exclude_pk=exclude_pk)


def visible_unusual_phrasing(person, review):
    if review is None or not unusual_spending_ai_on(person):
        return "", ""
    paragraph = (review.unusual_ai_paragraph or "").strip()
    if not paragraph:
        return "", ""
    return paragraph, phrasing_label(review.unusual_ai_backend)


def run_unusual_spending_job(person, job, **provider_kwargs):
    return _run_review_phrasing_job(
        person,
        job,
        feature=FEATURE,
        prompt=PROMPT,
        enabled=unusual_spending_ai_on,
        payload=unusual_facts_for_ai,
        queue=queue_unusual_phrasing,
        fields=("unusual_ai_paragraph", "unusual_ai_backend"),
        provider=run_structured,
        skip_empty_unusual=True,
        **provider_kwargs,
    )
