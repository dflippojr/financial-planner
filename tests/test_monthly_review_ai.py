from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness

from finance.ai_jobs import process_due_jobs
from finance.ai_services import connect_harness, set_defaults
from finance.alert_services import save_alert_settings, settings_for
from finance.category_services import ensure_household_categories
from finance.models import Account, AiJob, Household, ImportBatch, Membership, MonthlyReview, Person, Transaction
from finance.monthly_review import store_monthly_review
from finance.monthly_review_ai import (
    facts_payload_for_ai,
    paragraph_is_grounded,
    phrasing_label,
    queue_monthly_review_phrasing,
)
from finance.policy_services import accept_policy, current_policy, publish_policy


PASSWORD = "Synthetic-passphrase-42!"
TOKEN = "ha-synthetic-app-token"
TODAY = date(2026, 10, 3)
SEP = date(2026, 9, 1)


def make_person(username, *, accept=True):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    if accept:
        policy = current_policy(create_if_missing=False)
        if policy is None:
            policy = publish_policy(material=True, body="Synthetic privacy policy for AI tests")
        accept_policy(person, policy)
    return person


def make_household(*people, name="Synthetic Household"):
    household = Household.objects.create(name=name)
    for person in people:
        Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    return household


def make_account(owner, *, name="Synthetic Checking", scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(owner, account, *, amount_minor=-1234, description="Synthetic coffee", transaction_date=date(2026, 9, 12)):
    from hashlib import sha256

    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=sha256(f"{account.pk}-{amount_minor}-{description}".encode()).hexdigest(),
        date_range_start=date(2026, 9, 1),
        date_range_end=date(2026, 9, 28),
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=sha256(f"txn-{account.pk}-{amount_minor}-{description}".encode()).hexdigest(),
        original_fields={"synthetic": "1"},
    )


def connect_ai(person, url):
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")


@pytest.fixture
def harness():
    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


def signed_in(person):
    client = Client()
    client.force_login(person.user)
    return client


@pytest.mark.django_db
def test_grounding_rejects_numbers_missing_from_facts():
    facts = {"income_display": "12.34 USD", "income_minor": 1234, "month": "2026-09"}
    ok = "Income was 12.34 USD in 2026."
    bad = "Income was 12.34 USD, plus an invented 99.00 USD."
    assert paragraph_is_grounded(ok, facts)
    assert not paragraph_is_grounded(bad, facts)


@pytest.mark.django_db
def test_ai_off_does_not_queue_and_page_is_unchanged():
    owner = make_person("owner")
    make_household(owner)
    checking = make_account(owner)
    make_transaction(owner, checking)
    review, wrote = store_monthly_review(owner, SEP, today=TODAY)
    assert wrote
    assert AiJob.objects.count() == 0
    page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    assert page.status_code == 200
    body = page.content.decode()
    assert "AI-generated" not in body
    assert "Income, spending, and net" in body


@pytest.mark.django_db
def test_switch_off_does_not_queue(harness):
    _state, url = harness
    owner = make_person("owner")
    make_household(owner)
    connect_ai(owner, url)
    prefs = settings_for(owner)
    save_alert_settings(
        owner,
        sync_enabled=True,
        recurring_price_enabled=True,
        recurring_missed_enabled=True,
        budget_enabled=True,
        large_transaction_enabled=True,
        monthly_review_enabled=True,
        monthly_review_ai_enabled=False,
        large_transaction_minor=prefs.large_transaction_minor,
    )
    checking = make_account(owner)
    make_transaction(owner, checking)
    store_monthly_review(owner, SEP, today=TODAY)
    assert AiJob.objects.count() == 0


@pytest.mark.django_db
def test_ai_on_phrases_in_background_and_shows_label(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    make_household(owner)
    connect_ai(owner, url)
    checking = make_account(owner)
    make_transaction(owner, checking, amount_minor=-1234, description="Synthetic coffee")
    review, wrote = store_monthly_review(owner, SEP, today=TODAY)
    assert wrote
    job = AiJob.objects.get(member=owner, feature="monthly_review")
    assert job.status == AiJob.Status.QUEUED
    assert set(job.input_refs) == {"monthly_review_id", "generated_at"}
    state.session_answer = (
        "Spending was 12.34 USD. The largest listed purchase was Synthetic coffee. "
        "This is a closed-month summary. Totals come from the stored facts. "
        "No other amounts are included."
    )
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: False)
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == AiJob.Status.WAITING_MODEL
    state.model_state = "ready"
    process_due_jobs()
    job.refresh_from_db()
    review.refresh_from_db()
    assert job.status == AiJob.Status.SUCCEEDED
    assert "12.34 USD" in review.ai_paragraph
    assert review.facts["spending_minor"] == 1234
    page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    body = page.content.decode()
    assert phrasing_label("local") in body
    assert "12.34 USD" in body
    assert "Income, spending, and net" in body


@pytest.mark.django_db
def test_ungrounded_reply_is_discarded(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    make_household(owner)
    connect_ai(owner, url)
    checking = make_account(owner)
    make_transaction(owner, checking, amount_minor=-1234)
    store_monthly_review(owner, SEP, today=TODAY)
    state.session_answer = "Spending was 12.34 USD and an invented 50.00 USD appeared."
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    review = MonthlyReview.objects.get(person=owner, month=SEP)
    assert review.ai_paragraph == ""
    page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    assert "AI-generated" not in page.content.decode()


@pytest.mark.django_db
def test_household_facts_omitted_when_member_not_in_acceptance(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    outsider = make_person("member", accept=False)
    household = make_household(owner, outsider)
    connect_ai(owner, url)
    private = make_account(owner, name="Private checking")
    shared = make_account(owner, name="Shared checking", scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(owner, private, amount_minor=-1200, description="Private coffee")
    make_transaction(owner, shared, amount_minor=-88888, description="Household rent")
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    assert review.facts["spending_minor"] == 90_088
    assert "Household rent" in [item["description"] for item in review.facts["large_transactions"]]
    payload = facts_payload_for_ai(owner, review.facts)
    blob = str(payload)
    assert "888.88" not in blob
    assert "Household rent" not in blob
    assert "90.88" not in blob
    assert [item["description"] for item in payload["large_transactions"]] == ["Private coffee"]
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    assert state.session_prompts
    assert "888.88" not in state.session_prompts[0]
    assert "Household rent" not in state.session_prompts[0]


@pytest.mark.django_db
def test_regenerate_clears_paragraph_and_queues_again(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    make_household(owner)
    connect_ai(owner, url)
    checking = make_account(owner)
    make_transaction(owner, checking, amount_minor=-1234)
    store_monthly_review(owner, SEP, today=TODAY)
    state.session_answer = (
        "Spending was 12.34 USD. This summary uses stored facts. "
        "The tone stays neutral. Amounts are unchanged. The facts remain listed below."
    )
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    review = MonthlyReview.objects.get(person=owner, month=SEP)
    assert review.ai_paragraph
    response = signed_in(owner).post(reverse("monthly-review-regenerate"), {"month": "2026-09"})
    assert response.status_code == 302
    review.refresh_from_db()
    assert review.ai_paragraph == ""
    assert AiJob.objects.filter(member=owner, feature="monthly_review").count() == 2


@pytest.mark.django_db
def test_alert_settings_include_ai_review_switch():
    owner = make_person("owner")
    make_household(owner)
    client = signed_in(owner)
    page = client.get(reverse("settings-alerts"))
    assert b"AI monthly review summary" in page.content
    response = client.post(
        reverse("settings-alerts"),
        {
            "sync_enabled": "on",
            "monthly_review_enabled": "on",
        },
    )
    assert response.status_code == 302
    prefs = settings_for(owner)
    assert prefs.monthly_review_ai_enabled is False
    assert prefs.monthly_review_enabled is True


@pytest.mark.django_db
def test_queue_helper_skips_without_backend():
    owner = make_person("owner")
    make_household(owner)
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    assert queue_monthly_review_phrasing(owner, review) is None
    assert AiJob.objects.count() == 0


def test_grounding_needs_a_whole_fact_number_not_a_fragment():
    facts = {"month": "2026-09", "spending_minor": 1200, "spending_display": "$12.00", "net_minor": -4321}
    assert paragraph_is_grounded("Spending was $12.00 and net was $43.21 in September 2026.", facts)
    assert not paragraph_is_grounded("Spending was 120 dollars.", facts)
    assert not paragraph_is_grounded("Spending rose 20 percent.", facts)
    assert not paragraph_is_grounded("Net was $4.32.", facts)


@pytest.mark.django_db
def test_household_filter_applies_when_the_job_runs(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    household = make_household(owner)
    connect_ai(owner, url)
    shared = make_account(owner, name="Shared checking", scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(owner, shared, amount_minor=-77777, description="Household boiler")
    store_monthly_review(owner, SEP, today=TODAY)
    late_joiner = make_person("joiner", accept=False)
    Membership.objects.create(person=late_joiner, household=household)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    assert state.session_prompts
    assert "777.77" not in state.session_prompts[0]
    assert "Household boiler" not in state.session_prompts[0]


@pytest.mark.django_db
def test_switch_turned_off_before_the_job_runs_sends_nothing(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    make_household(owner)
    connect_ai(owner, url)
    checking = make_account(owner)
    make_transaction(owner, checking, amount_minor=-1234)
    store_monthly_review(owner, SEP, today=TODAY)
    prefs = settings_for(owner)
    prefs.monthly_review_ai_enabled = False
    prefs.save(update_fields=["monthly_review_ai_enabled"])
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    assert not state.session_prompts
    assert MonthlyReview.objects.get(person=owner, month=SEP).ai_paragraph == ""


@pytest.mark.django_db
def test_job_sends_nothing_once_an_account_is_no_longer_visible(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    partner = make_person("partner")
    household = make_household(owner, partner)
    connect_ai(owner, url)
    shared = make_account(partner, name="Partner shared", scope=Account.Scope.HOUSEHOLD, household=household)
    make_transaction(partner, shared, amount_minor=-65432, description="Partner furniture")
    store_monthly_review(owner, SEP, today=TODAY)
    Account.objects.filter(pk=shared.pk).update(scope=Account.Scope.PRIVATE, household=None, share_mode="")
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    assert not any("Partner furniture" in prompt or "654.32" in prompt for prompt in state.session_prompts)
    assert MonthlyReview.objects.get(person=owner, month=SEP).ai_paragraph == ""


@pytest.mark.django_db
def test_regenerate_during_the_call_keeps_the_cleared_summary(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    make_household(owner)
    connect_ai(owner, url)
    checking = make_account(owner)
    make_transaction(owner, checking, amount_minor=-1234)
    store_monthly_review(owner, SEP, today=TODAY)
    state.session_answer = "Spending was 12.34 USD."
    from finance import monthly_review_ai

    real_run = monthly_review_ai.run_structured

    def regenerate_mid_call(*args, **kwargs):
        result = real_run(*args, **kwargs)
        store_monthly_review(owner, SEP, today=TODAY, force=True)
        return result

    monkeypatch.setattr(monthly_review_ai, "run_structured", regenerate_mid_call)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    old_job = AiJob.objects.get(member=owner, feature="monthly_review")
    from finance.ai_jobs import _process_one
    from django.utils import timezone

    _process_one(old_job, timezone.now())
    review = MonthlyReview.objects.get(person=owner, month=SEP)
    assert review.ai_paragraph == ""
