"""Background AI phrasing of unusual-spending flags. Never sends raw transactions."""

from __future__ import annotations

import json

from .ai_jobs import enqueue_review_phrasing
from .ai_services import member_has_ai, run_structured
from .ai_types import ProviderResult
from .alert_services import settings_for
from .models import MonthlyReview
from .monthly_review_ai import (
    facts_payload_for_ai,
    paragraph_is_grounded,
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


def _requeue_if_regenerated(person, job, generated_at):
    """The review was regenerated after this job read it: phrase the newest generation instead."""
    current = MonthlyReview.objects.filter(pk=(job.input_refs or {}).get("monthly_review_id"), person=person).first()
    if current is not None and current.generated_at.isoformat() != generated_at:
        queue_unusual_phrasing(person, current, exclude_pk=job.pk)


def visible_unusual_phrasing(person, review):
    if review is None or not unusual_spending_ai_on(person):
        return "", ""
    paragraph = (review.unusual_ai_paragraph or "").strip()
    if not paragraph:
        return "", ""
    return paragraph, phrasing_label(review.unusual_ai_backend)


def run_unusual_spending_job(person, job, *, backend, session_id="", on_session=None, connection=None):
    from .monthly_review_ai import _extract_paragraph

    refs = job.input_refs or {}
    review = MonthlyReview.objects.filter(pk=refs.get("monthly_review_id"), person=person).first()
    if review is None:
        return ProviderResult(ok=True, answer="", session_id="skipped")
    if refs.get("generated_at") != review.generated_at.isoformat():
        _requeue_if_regenerated(person, job, refs.get("generated_at"))
        return ProviderResult(ok=True, answer="", session_id="skipped")
    if not unusual_spending_ai_on(person):
        return ProviderResult(ok=True, answer="", session_id="skipped")
    from .monthly_review import visibility_key

    if review.visibility_key != visibility_key(person):
        return ProviderResult(ok=True, answer="", session_id="skipped")
    facts = unusual_facts_for_ai(person, review.facts)
    if not facts.get("unusual"):
        return ProviderResult(ok=True, answer="", session_id="skipped")
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
    written = MonthlyReview.objects.filter(
        pk=review.pk,
        generated_at=review.generated_at,
        visibility_key=review.visibility_key,
    ).update(
        unusual_ai_paragraph=paragraph if grounded else "",
        unusual_ai_backend=(backend or "") if grounded else "",
    )
    if not written:
        _requeue_if_regenerated(person, job, review.generated_at.isoformat())
    return result
