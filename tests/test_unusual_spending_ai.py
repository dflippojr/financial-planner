from datetime import date
from hashlib import sha256

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness

from finance.ai_jobs import process_due_jobs
from finance.ai_services import connect_harness, set_defaults
from finance.alert_services import save_alert_settings, settings_for
from finance.category_services import assign_category, ensure_household_categories
from finance.models import Account, AiJob, Household, ImportBatch, Membership, MonthlyReview, Person, Transaction
from finance.monthly_review import store_monthly_review
from finance.monthly_review_ai import facts_payload_for_ai, phrasing_label
from finance.policy_services import accept_policy, current_policy, publish_policy
from finance.unusual_spending_ai import unusual_facts_for_ai, queue_unusual_phrasing


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


def make_transaction(owner, account, *, amount_minor, description, transaction_date):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256=sha256(f"{account.pk}-{amount_minor}-{description}-{transaction_date}".encode()).hexdigest(),
        date_range_start=date(transaction_date.year, transaction_date.month, 1),
        date_range_end=date(transaction_date.year, transaction_date.month, 28),
    )
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=sha256(f"txn-{account.pk}-{amount_minor}-{description}-{transaction_date}".encode()).hexdigest(),
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


def _grocery_spike(owner, household, checking):
    groceries = household.categories.get(name="Groceries")
    for when in (
        date(2026, 3, 10),
        date(2026, 4, 10),
        date(2026, 5, 10),
        date(2026, 6, 10),
        date(2026, 7, 10),
        date(2026, 8, 10),
    ):
        row = make_transaction(
            owner, checking, amount_minor=-10_000, description=f"Synthetic grocer {when}", transaction_date=when
        )
        assign_category(owner, row.pk, groceries.pk)
    spike = make_transaction(
        owner, checking, amount_minor=-15_000, description="Synthetic grocer spike", transaction_date=date(2026, 9, 12)
    )
    assign_category(owner, spike.pk, groceries.pk)
    return groceries


def _disable_monthly_review_ai(owner):
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
        unusual_spending_enabled=True,
        unusual_spending_ai_enabled=True,
    )


@pytest.mark.django_db
def test_no_ai_job_when_ai_off():
    owner = make_person("owner")
    household = make_household(owner)
    checking = make_account(owner)
    _grocery_spike(owner, household, checking)
    store_monthly_review(owner, SEP, today=TODAY)
    assert AiJob.objects.count() == 0


@pytest.mark.django_db
def test_no_ai_job_when_unusual_ai_disabled(harness):
    _state, url = harness
    owner = make_person("owner")
    household = make_household(owner)
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
        unusual_spending_ai_enabled=False,
    )
    checking = make_account(owner)
    _grocery_spike(owner, household, checking)
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    assert queue_unusual_phrasing(owner, review) is None
    assert AiJob.objects.count() == 0


@pytest.mark.django_db
def test_unusual_ai_phrases_and_household_private_stays_out(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    outsider = make_person("member", accept=False)
    household = make_household(owner, outsider)
    connect_ai(owner, url)
    _disable_monthly_review_ai(owner)
    private = make_account(owner, name="Private checking")
    shared = make_account(owner, name="Shared checking", scope=Account.Scope.HOUSEHOLD, household=household)
    _grocery_spike(owner, household, private)
    make_transaction(
        owner,
        shared,
        amount_minor=-88_888,
        description="Household rent",
        transaction_date=date(2026, 9, 15),
    )
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    job = AiJob.objects.get(member=owner, feature="unusual_spending")
    payload = unusual_facts_for_ai(owner, review.facts)
    blob = str(payload)
    assert "Household rent" not in blob
    assert "888.88" not in blob
    assert any(item["name"] == "Groceries" for item in payload["unusual"])
    state.session_answer = (
        "Groceries spent 150.00 USD versus a median of 100.00 USD. "
        "No other flags are listed."
    )
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    review.refresh_from_db()
    job.refresh_from_db()
    assert job.status == AiJob.Status.SUCCEEDED
    assert "150.00 USD" in review.unusual_ai_paragraph
    assert "888.88" not in (state.session_prompts[0] if state.session_prompts else "")
    page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    body = page.content.decode()
    assert phrasing_label("local") in body
    assert "150.00 USD" in body
    assert "Unusual this month" in body


def _prompts(state):
    return "\n".join(state.session_prompts)


def _category_shared_baseline_private_spike(owner, household):
    groceries = household.categories.get(name="Groceries")
    private = make_account(owner, name="Private checking")
    shared = make_account(owner, name="Shared checking", scope=Account.Scope.HOUSEHOLD, household=household)
    for when in (
        date(2026, 3, 10),
        date(2026, 4, 10),
        date(2026, 5, 10),
        date(2026, 6, 10),
        date(2026, 7, 10),
        date(2026, 8, 10),
    ):
        row = make_transaction(
            owner, shared, amount_minor=-10_000, description=f"Synthetic grocer {when}", transaction_date=when
        )
        assign_category(owner, row.pk, groceries.pk)
    spike = make_transaction(
        owner, private, amount_minor=-15_000, description="Synthetic grocer spike", transaction_date=date(2026, 9, 12)
    )
    assign_category(owner, spike.pk, groceries.pk)
    return private, groceries


def _merchant_shared_median_private_charge(owner, household):
    private = make_account(owner, name="Private checking")
    shared = make_account(owner, name="Shared checking", scope=Account.Scope.HOUSEHOLD, household=household)
    for when in (date(2026, 6, 2), date(2026, 7, 2), date(2026, 8, 2)):
        make_transaction(owner, shared, amount_minor=-1_000, description="SYNTHETIC-CAFE", transaction_date=when)
    make_transaction(
        owner, private, amount_minor=-2_500, description="SYNTHETIC-CAFE", transaction_date=date(2026, 9, 4)
    )
    return private


@pytest.mark.django_db
def test_restricted_ai_does_not_send_shared_category_baseline(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    household = make_household(owner, make_person("member", accept=False))
    connect_ai(owner, url)
    _category_shared_baseline_private_spike(owner, household)
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    grocery = next(item for item in review.facts["unusual"] if item["kind"] == "category" and item["name"] == "Groceries")
    assert grocery["baseline_minor"] == 10000
    payload = unusual_facts_for_ai(owner, review.facts)
    blob = str(payload) + str(facts_payload_for_ai(owner, review.facts))
    assert "100.00" not in blob
    assert "10000" not in blob
    state.session_answer = "A short summary with no extra numbers."
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    prompts = _prompts(state)
    assert prompts
    assert "100.00" not in prompts
    assert "10000" not in prompts
    page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    body = page.content.decode()
    assert "Unusual this month" in body
    assert "100.00 USD" in body


@pytest.mark.django_db
def test_restricted_ai_does_not_send_shared_merchant_median(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    household = make_household(owner, make_person("member", accept=False))
    connect_ai(owner, url)
    _merchant_shared_median_private_charge(owner, household)
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    merchant = next(item for item in review.facts["unusual"] if item["kind"] == "merchant")
    assert merchant["median_minor"] == 1000
    payload = unusual_facts_for_ai(owner, review.facts)
    blob = str(payload) + str(facts_payload_for_ai(owner, review.facts))
    assert "10.00" not in blob
    assert "1000" not in blob
    state.session_answer = "A short summary with no extra numbers."
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    prompts = _prompts(state)
    assert prompts
    assert "10.00" not in prompts
    assert "1000" not in prompts
    page = signed_in(owner).get(reverse("monthly-review") + "?month=2026-09")
    assert "10.00 USD" in page.content.decode()


@pytest.mark.django_db
def test_allowed_household_ai_still_sends_full_unusual_flags(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    household = make_household(owner, make_person("member"))
    connect_ai(owner, url)
    _category_shared_baseline_private_spike(owner, household)
    _merchant_shared_median_private_charge(owner, household)
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    payload = unusual_facts_for_ai(owner, review.facts)
    blob = str(payload)
    assert "100.00" in blob
    assert "10000" in blob
    assert "10.00" in blob
    assert "1000" in blob
    state.session_answer = "A short summary with no extra numbers."
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    prompts = _prompts(state)
    assert "100.00" in prompts
    assert "10000" in prompts
    assert "10.00" in prompts
    assert "1000" in prompts


@pytest.mark.django_db
def test_restricted_ai_sends_no_unusual_facts_without_private_accounts(harness, monkeypatch):
    state, url = harness
    owner = make_person("owner")
    household = make_household(owner, make_person("member", accept=False))
    connect_ai(owner, url)
    shared = make_account(owner, name="Shared checking", scope=Account.Scope.HOUSEHOLD, household=household)
    groceries = household.categories.get(name="Groceries")
    for when in (
        date(2026, 3, 10),
        date(2026, 4, 10),
        date(2026, 5, 10),
        date(2026, 6, 10),
        date(2026, 7, 10),
        date(2026, 8, 10),
    ):
        row = make_transaction(
            owner, shared, amount_minor=-10_000, description=f"Synthetic grocer {when}", transaction_date=when
        )
        assign_category(owner, row.pk, groceries.pk)
    spike = make_transaction(
        owner, shared, amount_minor=-15_000, description="Synthetic grocer spike", transaction_date=date(2026, 9, 12)
    )
    assign_category(owner, spike.pk, groceries.pk)
    review, _wrote = store_monthly_review(owner, SEP, today=TODAY)
    assert any(item["kind"] == "category" for item in review.facts["unusual"])
    payload = unusual_facts_for_ai(owner, review.facts)
    assert payload["unusual"] == []
    state.session_answer = "A short summary with no extra numbers."
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    process_due_jobs()
    prompts = _prompts(state)
    assert "100.00" not in prompts
    assert "10000" not in prompts
    assert "150.00" not in prompts
    assert "15000" not in prompts


def test_baseline_and_median_are_stored_as_integer_cents():
    from finance.unusual_spending import _whole_minor

    assert _whole_minor(10000) == 10000
    assert _whole_minor("2500.5") == 2500
    assert _whole_minor("2501.5") == 2502


def test_a_cents_figure_read_as_dollars_is_not_grounded():
    from finance.monthly_review_ai import paragraph_is_grounded

    facts = {"unusual": [{"kind": "category", "name": "Groceries", "month_minor": 15000, "baseline_minor": 10000}]}

    assert paragraph_is_grounded("Groceries were 150.00 USD against a usual 100.00 USD.", facts)
    assert not paragraph_is_grounded("Groceries had a median of 10,000 USD.", facts)
