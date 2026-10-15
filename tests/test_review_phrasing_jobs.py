"""One phrasing contract, run through the real job runner for both review features (#479)."""

import pytest
from django.test import Client
from django.urls import reverse

from finance import monthly_review_ai, unusual_spending_ai
from finance.ai_jobs import process_due_jobs
from finance.alert_services import settings_for
from finance.models import Account, AiJob, Household, Membership, MonthlyReview
from finance.monthly_review import latest_closed_month, store_monthly_review
from tests.test_ai_member_quotas import _review_member, review_harness  # noqa: F401 - fixture
from tests.test_monthly_review_ai import make_account, make_person, make_transaction

pytestmark = pytest.mark.django_db

GROUNDED = "Spending was 150.00 USD."
PARTNER_ITEM = ("Partner furniture", "654.32")

FEATURES = {
    "monthly_review": {
        "module": monthly_review_ai,
        "fields": ("ai_paragraph", "ai_backend"),
        "other": ("unusual_ai_paragraph", "unusual_ai_backend"),
        "switch": "monthly_review_ai_enabled",
    },
    "unusual_spending": {
        "module": unusual_spending_ai,
        "fields": ("unusual_ai_paragraph", "unusual_ai_backend"),
        "other": ("ai_paragraph", "ai_backend"),
        "switch": "unusual_spending_ai_enabled",
    },
}
OTHER_SUMMARY = ("Existing other summary.", "codex")


def _switch_off(owner, feature, shared):
    prefs = settings_for(owner)
    setattr(prefs, FEATURES[feature]["switch"], False)
    prefs.save(update_fields=[FEATURES[feature]["switch"]])


def _share_a_partner_account(owner):
    """A partner's shared account feeds the review; it is stored before the job runs."""
    household = Household.objects.get(memberships__person=owner)
    partner = make_person("partner")
    Membership.objects.create(person=partner, household=household)
    shared = make_account(partner, name="Partner shared", scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(partner, shared, amount_minor=-65432, description="Partner furniture",
                     transaction_date=latest_closed_month())
    store_monthly_review(owner, latest_closed_month(), force=True)
    return shared


def _make_the_account_private(owner, feature, shared):
    Account.objects.filter(pk=shared.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode="")


def _lose_membership(owner, feature, shared):
    Membership.objects.filter(person=owner).delete()


# answer: what the harness replies; failure: a harness session failure; written: the paragraph kept;
# skipped: the job must not reach the harness at all; regenerated: the review is replaced mid-call.
SCENARIOS = {
    "grounded": {"answer": GROUNDED, "written": GROUNDED},
    "fenced": {"answer": f"```text\n{GROUNDED}\n```", "written": GROUNDED},
    "invented": {"answer": "Invented spending of 99.99 USD.", "written": ""},
    "provider_failure": {"failure": "provider_error", "written": None},
    "switch_off": {"before": _switch_off, "skipped": True},
    "membership_lost": {"before": _lose_membership, "skipped": True},
    "account_made_private": {"setup": _share_a_partner_account, "before": _make_the_account_private,
                             "skipped": True, "hidden": PARTNER_ITEM},
    "regenerated_mid_call": {"answer": GROUNDED, "regenerated": True, "written": ""},
}


@pytest.mark.parametrize("feature", sorted(FEATURES))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_phrasing_job_contract(review_harness, monkeypatch, feature, scenario):  # noqa: F811
    spec, case = FEATURES[feature], SCENARIOS[scenario]
    state, _url = review_harness
    owner = _review_member(review_harness, unusual=True)
    month = latest_closed_month()
    shared = case["setup"](owner) if "setup" in case else None
    # Only this feature's job runs; the other feature keeps a summary the job must not touch.
    AiJob.objects.filter(member=owner).exclude(feature=feature).delete()
    MonthlyReview.objects.filter(person=owner, month=month).update(**dict(zip(spec["other"], OTHER_SUMMARY)))
    state.model_state = "ready"
    state.session_answer = case.get("answer")
    state.session_failure = case.get("failure")
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    if "before" in case:
        case["before"](owner, feature, shared)
    if case.get("regenerated"):
        real_run = spec["module"].run_structured

        def regenerate_mid_call(*args, **kwargs):
            result = real_run(*args, **kwargs)
            store_monthly_review(owner, month, force=True)
            return result

        monkeypatch.setattr(spec["module"], "run_structured", regenerate_mid_call)
    job = AiJob.objects.get(member=owner, feature=feature)

    process_due_jobs()

    review = MonthlyReview.objects.get(person=owner, month=month)
    written = case.get("written") or ""
    assert tuple(getattr(review, field) for field in spec["fields"]) == ((written, "local") if written else ("", ""))
    other = tuple(getattr(review, field) for field in spec["other"])
    assert other == (("", "") if case.get("regenerated") else OTHER_SUMMARY)
    prompts = "\n".join(state.session_prompts)
    assert bool(prompts) == (not case.get("skipped"))
    assert not any(item in prompts for item in case.get("hidden", ()))
    if case.get("regenerated"):
        follow_up = AiJob.objects.get(member=owner, feature=feature, status=AiJob.Status.QUEUED)
        assert follow_up.pk != job.pk
        assert follow_up.input_refs["generated_at"] == review.generated_at.isoformat()

    client = Client()
    client.force_login(owner.user)
    page = client.get(reverse("monthly-review") + f"?month={month.isoformat()[:7]}")
    assert page.status_code == 200
    assert (GROUNDED in page.content.decode()) == (written == GROUNDED)
