"""Shared behavior contracts exercised through both feature entry points."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils import timezone

from finance import monthly_review_ai, unusual_spending_ai
from finance.ai_types import ProviderResult, PROVIDER_ERROR
from finance.models import MonthlyReview
from finance.monthly_review import visibility_key
from tests.test_monthly_review_ai import make_person


pytestmark = pytest.mark.django_db


@pytest.fixture(params=["monthly_review", "unusual_spending"])
def phrasing(request, monkeypatch):
    if request.param == "monthly_review":
        module = monthly_review_ai
        run_job = module.run_monthly_review_job
        enabled_name = "monthly_review_ai_on"
        queue_name = "queue_monthly_review_phrasing"
        fields = ("ai_paragraph", "ai_backend")
    else:
        module = unusual_spending_ai
        run_job = module.run_unusual_spending_job
        enabled_name = "unusual_spending_ai_on"
        queue_name = "queue_unusual_phrasing"
        fields = ("unusual_ai_paragraph", "unusual_ai_backend")
    person = make_person("synthetic-review-owner")
    review = MonthlyReview.objects.create(
        person=person,
        month=date(2026, 9, 1),
        generated_at=timezone.now(),
        visibility_key=visibility_key(person),
        facts={"month": "2026-09", "unusual": [{"name": "Synthetic groceries", "month_minor": 15000}]},
        ai_paragraph="Existing monthly summary.",
        ai_backend="local",
        unusual_ai_paragraph="Existing unusual summary.",
        unusual_ai_backend="local",
    )
    job = SimpleNamespace(
        pk=42,
        input_refs={"monthly_review_id": review.pk, "generated_at": review.generated_at.isoformat()},
    )
    provider = Mock(return_value=ProviderResult(ok=True, answer="Spending was 150.00 USD."))
    queue = Mock()
    monkeypatch.setattr(module, "run_structured", provider)
    monkeypatch.setattr(module, enabled_name, lambda person: True)
    monkeypatch.setattr(module, queue_name, queue)
    return SimpleNamespace(
        module=module, run=run_job, enabled_name=enabled_name, person=person,
        review=review, job=job, provider=provider, queue=queue, fields=fields,
    )


def summaries(review):
    return review.ai_paragraph, review.ai_backend, review.unusual_ai_paragraph, review.unusual_ai_backend


@pytest.mark.parametrize("blocked", ["missing", "other_member", "disabled", "visibility"])
def test_unavailable_or_inaccessible_reviews_skip_without_mutating_summaries(phrasing, monkeypatch, blocked):
    original = summaries(phrasing.review)
    person = phrasing.person
    if blocked == "missing":
        phrasing.job.input_refs["monthly_review_id"] = None
    elif blocked == "other_member":
        person = make_person("synthetic-other-member")
    elif blocked == "disabled":
        monkeypatch.setattr(phrasing.module, phrasing.enabled_name, lambda person: False)
    else:
        MonthlyReview.objects.filter(pk=phrasing.review.pk).update(visibility_key="0" * 64)

    result = phrasing.run(person, phrasing.job, backend="codex")

    assert result == ProviderResult(ok=True, answer="", session_id="skipped")
    phrasing.provider.assert_not_called()
    phrasing.queue.assert_not_called()
    phrasing.review.refresh_from_db()
    assert summaries(phrasing.review) == original


@pytest.mark.parametrize("answer", ["```text\nSpending was 150.00 USD.\n```", "Invented spending of 99.99 USD."])
def test_provider_options_and_feature_fields_remain_independent(phrasing, answer):
    original = summaries(phrasing.review)
    phrasing.provider.return_value = ProviderResult(ok=True, answer=answer)
    validated = object()
    on_session = Mock()

    result = phrasing.run(
        phrasing.person, phrasing.job, backend="codex", session_id="synthetic-session",
        on_session=on_session, connection=validated,
    )

    assert result is phrasing.provider.return_value
    args, kwargs = phrasing.provider.call_args
    assert args[0] is phrasing.person
    assert args[1].startswith(phrasing.module.PROMPT)
    assert kwargs == {
        "feature": phrasing.module.FEATURE, "backend": "codex", "session_id": "synthetic-session",
        "on_session": on_session, "connection": validated,
    }
    phrasing.review.refresh_from_db()
    grounded = answer.startswith("```")
    assert getattr(phrasing.review, phrasing.fields[0]) == ("Spending was 150.00 USD." if grounded else "")
    assert getattr(phrasing.review, phrasing.fields[1]) == ("codex" if grounded else "")
    other = ("unusual_ai_paragraph", "unusual_ai_backend") if phrasing.module is monthly_review_ai else ("ai_paragraph", "ai_backend")
    offset = 2 if phrasing.module is monthly_review_ai else 0
    assert tuple(getattr(phrasing.review, field) for field in other) == original[offset:offset + 2]
    phrasing.queue.assert_not_called()


def test_provider_failure_returns_unchanged_and_keeps_both_summaries(phrasing):
    original = summaries(phrasing.review)
    failure = ProviderResult(ok=False, failure_code=PROVIDER_ERROR, session_id="synthetic-failure")
    phrasing.provider.return_value = failure

    assert phrasing.run(phrasing.person, phrasing.job, backend="local") is failure

    phrasing.review.refresh_from_db()
    assert summaries(phrasing.review) == original
    phrasing.queue.assert_not_called()


@pytest.mark.parametrize("when", ["before", "during"])
def test_regeneration_requeues_this_feature_without_overwriting_new_summaries(phrasing, when):
    newer_generation = phrasing.review.generated_at + timedelta(seconds=1)

    def regenerate():
        MonthlyReview.objects.filter(pk=phrasing.review.pk).update(
            generated_at=newer_generation, ai_paragraph="New monthly summary.",
            unusual_ai_paragraph="New unusual summary.",
        )

    if when == "before":
        regenerate()
    else:
        def provider(*args, **kwargs):
            regenerate()
            return ProviderResult(ok=True, answer="Spending was 150.00 USD.")

        phrasing.provider.side_effect = provider

    result = phrasing.run(phrasing.person, phrasing.job, backend="codex")

    assert result.ok
    assert phrasing.provider.call_count == (0 if when == "before" else 1)
    phrasing.review.refresh_from_db()
    assert phrasing.review.ai_paragraph == "New monthly summary."
    assert phrasing.review.unusual_ai_paragraph == "New unusual summary."
    assert phrasing.review.ai_backend == phrasing.review.unusual_ai_backend == "local"
    phrasing.queue.assert_called_once_with(phrasing.person, phrasing.review, exclude_pk=phrasing.job.pk)
    assert phrasing.queue.call_args.args[1].generated_at == newer_generation


def test_only_unusual_phrasing_skips_an_empty_flag_payload(phrasing):
    MonthlyReview.objects.filter(pk=phrasing.review.pk).update(facts={"month": "2026-09", "unusual": []})
    phrasing.provider.return_value = ProviderResult(ok=True, answer="A synthetic summary.")

    result = phrasing.run(phrasing.person, phrasing.job, backend="local")

    assert result.ok
    if phrasing.module is unusual_spending_ai:
        assert result.session_id == "skipped"
        phrasing.provider.assert_not_called()
    else:
        phrasing.provider.assert_called_once()
        phrasing.review.refresh_from_db()
        assert phrasing.review.ai_paragraph == "A synthetic summary."


def test_visibility_change_during_call_discards_reply_without_requeueing_same_generation(phrasing):
    original = summaries(phrasing.review)

    def lose_visibility(*args, **kwargs):
        MonthlyReview.objects.filter(pk=phrasing.review.pk).update(visibility_key="0" * 64)
        return ProviderResult(ok=True, answer="Spending was 150.00 USD.")

    phrasing.provider.side_effect = lose_visibility

    assert phrasing.run(phrasing.person, phrasing.job, backend="codex").ok

    phrasing.review.refresh_from_db()
    assert summaries(phrasing.review) == original
    phrasing.queue.assert_not_called()
