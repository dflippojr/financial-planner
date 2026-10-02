import json
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from tests.fake_harness import start_fake_harness
from tests.helpers import stamp_recent_auth

from finance.ai_jobs import process_due_jobs
from finance.ai_services import connect_harness, set_defaults
from finance.category_services import assign_category, ensure_household_categories
from finance.category_suggestion_services import (
    BATCH_SIZE,
    FEATURE,
    accept_suggestion,
    queue_category_suggestions_for,
    queue_remaining_uncategorized,
    snapshot_hash,
)
from finance.csv_import.services import categorize_imported_batch
from finance.models import (
    Account,
    AiJob,
    Category,
    CategorySuggestion,
    Household,
    ImportBatch,
    Membership,
    Person,
    Transaction,
)
from finance.policy_services import accept_policy, current_policy, publish_policy
from finance.rule_services import apply_rule, save_category_rule
from finance.simplefin_services import save_account_links, sync_connection
from tests.test_simplefin import account_payload, connect_owner, posted_txn


PASSWORD = "Synthetic-passphrase-42!"
TOKEN = "ha-synthetic-app-token"


def make_member(username, household=None):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    person = Person.objects.create(user=user, display_name=f"{username.title()} Example")
    if household is None:
        household = Household.objects.create(name="Synthetic Household")
    Membership.objects.create(person=person, household=household)
    ensure_household_categories(household)
    policy = current_policy(create_if_missing=False)
    if policy is None:
        policy = publish_policy(material=True, body="Synthetic privacy policy for AI tests")
    accept_policy(person, policy)
    return user, person, household


def make_account(owner, *, name="Synthetic Checking", scope=Account.Scope.PRIVATE, household=None):
    return Account.objects.create(
        name=name,
        account_type=Account.Type.CHECKING,
        owner=owner,
        scope=scope,
        household=household,
        share_mode=Account.ShareMode.CO_OWNED if scope == Account.Scope.HOUSEHOLD else "",
    )


def make_transaction(
    owner,
    account,
    *,
    description="Synthetic coffee",
    amount_minor=-500,
    fingerprint=None,
    transaction_date=date(2026, 1, 2),
):
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=owner,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="b" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    digest = fingerprint or (f"{account.pk}-{amount_minor}-{description}".encode().hex().ljust(64, "a")[:64])
    return Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=transaction_date,
        amount_minor=amount_minor,
        currency="USD",
        description=description,
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint=digest,
        original_fields={"synthetic": description},
    )


def groceries(household):
    return Category.objects.get(household=household, name="Groceries")


@pytest.fixture
def harness():
    state, url, server = start_fake_harness()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


def connect_ai(person, url):
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")


def suggestion_json(mapping):
    return json.dumps(
        {
            "suggestions": [
                {"transaction_id": txn_id, "category_id": category_id} for txn_id, category_id in mapping.items()
            ]
        }
    )


@pytest.mark.django_db
def test_import_queues_uncategorized_and_skips_rule_rows(harness):
    state, url = harness
    _user, person, household = make_member("owner")
    connect_ai(person, url)
    account = make_account(person)
    groceries_cat = groceries(household)
    rule = save_category_rule(
        person,
        owner_kind="personal",
        description_contains="KROGER",
        account_id=None,
        min_amount_minor=None,
        max_amount_minor=None,
        category_id=groceries_cat.pk,
        priority=0,
    )
    apply_rule(person, rule.pk)
    batch = ImportBatch.objects.create(
        account=account,
        imported_by=person,
        source=ImportBatch.Source.HUNTINGTON,
        source_file_sha256="c" * 64,
        date_range_start=date(2026, 1, 1),
        date_range_end=date(2026, 1, 31),
    )
    ruled = Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=date(2026, 1, 2),
        amount_minor=-1200,
        currency="USD",
        description="SYNTHETIC KROGER",
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=1,
        fingerprint="d" * 64,
        original_fields={"synthetic": "kroger"},
    )
    leftover = Transaction.objects.create(
        account=account,
        import_batch=batch,
        transaction_date=date(2026, 1, 3),
        amount_minor=-400,
        currency="USD",
        description="Synthetic unknown cafe",
        kind=Transaction.Kind.CASH_FLOW,
        source_row_number=2,
        fingerprint="e" * 64,
        original_fields={"synthetic": "cafe"},
    )
    categorize_imported_batch(person, batch)
    ruled.refresh_from_db()
    leftover.refresh_from_db()
    assert ruled.category_source == Transaction.CategorySource.RULE
    job = AiJob.objects.get(member=person, feature=FEATURE)
    assert leftover.pk in job.input_refs["transaction_ids"]
    assert ruled.pk not in job.input_refs["transaction_ids"]
    assert job.status == AiJob.Status.QUEUED
    assert state.session_prompts == []


@pytest.mark.django_db
def test_sync_queues_suggestions(harness, monkeypatch):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_ai(person, url)
    checking = make_account(person)
    payload = account_payload(transactions=[posted_txn(txn_id="new-1", day=16, amount="-3.00")])
    connection = connect_owner(person, monkeypatch, payload)
    save_account_links(
        person,
        connection.pk,
        [
            {
                "simplefin_account_id": "CON-1:sf-checking",
                "action": "link",
                "account_id": checking.pk,
                "cutover_date": date(2026, 3, 11),
            }
        ],
    )
    sync_connection(person, connection.pk, ignore_rate_limit=True)
    job = AiJob.objects.get(member=person, feature=FEATURE)
    assert job.input_refs["transaction_ids"]
    assert Transaction.objects.get(pk=job.input_refs["transaction_ids"][0]).description == "Synthetic Stream"


@pytest.mark.django_db
def test_job_waits_while_local_model_sleeps(harness, monkeypatch):
    state, url = harness
    _user, person, household = make_member("owner")
    connect_ai(person, url)
    account = make_account(person)
    txn = make_transaction(person, account)
    queue_category_suggestions_for(person, [txn])
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: False)
    process_due_jobs()
    job = AiJob.objects.get(member=person, feature=FEATURE)
    assert job.status == AiJob.Status.WAITING_MODEL
    assert CategorySuggestion.objects.count() == 0
    state.model_state = "ready"
    state.session_answer = suggestion_json({txn.pk: groceries(household).pk})
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == AiJob.Status.SUCCEEDED
    assert CategorySuggestion.objects.filter(transaction=txn, status=CategorySuggestion.Status.PENDING).exists()


@pytest.mark.django_db
def test_fake_provider_valid_unknown_and_malformed(harness, monkeypatch):
    state, url = harness
    _user, person, household = make_member("owner")
    connect_ai(person, url)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    account = make_account(person)
    valid = make_transaction(person, account, description="Synthetic grocer A", fingerprint="1" * 64)
    unknown = make_transaction(person, account, description="Synthetic grocer B", fingerprint="2" * 64)
    malformed = make_transaction(person, account, description="Synthetic grocer C", fingerprint="3" * 64)
    groceries_cat = groceries(household)

    state.session_answer = suggestion_json({valid.pk: groceries_cat.pk})
    queue_category_suggestions_for(person, [valid])
    process_due_jobs()
    stored = CategorySuggestion.objects.get(transaction=valid)
    assert stored.category_id == groceries_cat.pk
    assert stored.status == CategorySuggestion.Status.PENDING

    CategorySuggestion.objects.all().delete()
    AiJob.objects.all().delete()
    state.session_answer = suggestion_json({unknown.pk: 999999})
    queue_category_suggestions_for(person, [unknown])
    process_due_jobs()
    assert CategorySuggestion.objects.filter(transaction=unknown).count() == 0

    AiJob.objects.all().delete()
    state.session_answer = "not-json {please suggest groceries}"
    queue_category_suggestions_for(person, [malformed])
    process_due_jobs()
    assert CategorySuggestion.objects.filter(transaction=malformed).count() == 0


@pytest.mark.django_db
def test_accept_never_overwrites_hand_or_rule_category(harness, monkeypatch):
    state, url = harness
    _user, person, household = make_member("owner")
    connect_ai(person, url)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    account = make_account(person)
    groceries_cat = groceries(household)
    dining = Category.objects.get(household=household, name="Dining")
    hand = make_transaction(person, account, description="Synthetic hand", fingerprint="4" * 64)
    ruled = make_transaction(person, account, description="Synthetic rule", fingerprint="5" * 64)
    state.session_answer = suggestion_json({hand.pk: groceries_cat.pk, ruled.pk: groceries_cat.pk})
    queue_category_suggestions_for(person, [hand, ruled])
    process_due_jobs()
    assign_category(person, hand.pk, dining.pk)
    ruled.category = dining
    ruled.category_source = Transaction.CategorySource.RULE
    ruled.save(update_fields=("category", "category_source", "updated_at"))
    hand_sug = CategorySuggestion.objects.get(transaction=hand)
    rule_sug = CategorySuggestion.objects.get(transaction=ruled)
    accept_suggestion(person, hand_sug.pk)
    accept_suggestion(person, rule_sug.pk)
    hand.refresh_from_db()
    ruled.refresh_from_db()
    assert hand.category_id == dining.pk
    assert hand.category_source == Transaction.CategorySource.MANUAL
    assert ruled.category_id == dining.pk
    assert ruled.category_source == Transaction.CategorySource.RULE


@pytest.mark.django_db
def test_private_transactions_are_never_sent_or_shown(harness, monkeypatch):
    state, url = harness
    user_a, person_a, household = make_member("alpha")
    user_b, person_b, _ = make_member("beta", household=household)
    connect_ai(person_a, url)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    shared = make_account(person_a, name="Shared Checking", scope=Account.Scope.HOUSEHOLD, household=household)
    private_b = make_account(person_b, name="Beta Private")
    shared_txn = make_transaction(person_a, shared, description="Shared rent", fingerprint="6" * 64)
    private_txn = make_transaction(person_b, private_b, description="Beta private grocery", fingerprint="7" * 64)
    groceries_cat = groceries(household)
    state.session_answer = suggestion_json({shared_txn.pk: groceries_cat.pk, private_txn.pk: groceries_cat.pk})
    queue_remaining_uncategorized(person_a)
    process_due_jobs()
    prompt = "\n".join(state.session_prompts)
    assert "Shared rent" in prompt
    assert "Beta private grocery" not in prompt
    assert not CategorySuggestion.objects.filter(transaction=private_txn).exists()
    assert CategorySuggestion.objects.filter(member=person_a, transaction=shared_txn).exists()
    client_a = Client()
    client_a.force_login(user_a)
    page = client_a.get(reverse("transaction-list") + "?category=uncategorized")
    body = page.content.decode()
    assert "Shared rent" in body
    assert "Beta private grocery" not in body
    assert "AI · Agent Harness · Local model" in body
    client_b = Client()
    client_b.force_login(user_b)
    other = client_b.get(reverse("transaction-list") + "?category=uncategorized")
    other_body = other.content.decode()
    assert "Beta private grocery" in other_body
    assert "AI · Agent Harness" not in other_body


@pytest.mark.django_db
def test_ai_off_changes_nothing(harness):
    _state, url = harness
    user, person, _household = make_member("solo")
    account = make_account(person)
    txn = make_transaction(person, account)
    categorize_imported_batch(person, txn.import_batch)
    assert AiJob.objects.count() == 0
    assert CategorySuggestion.objects.count() == 0
    client = Client()
    client.force_login(user)
    page = client.get(reverse("transaction-list") + "?category=uncategorized")
    assert b"Suggest categories" not in page.content
    assert b"suggestions pending" not in page.content.lower()
    queued = client.post(reverse("suggest-categories"), {"category": "uncategorized", "next": "/transactions/?category=uncategorized"})
    assert queued.status_code == 302
    assert AiJob.objects.count() == 0


@pytest.mark.django_db
def test_reload_does_not_rerun_the_model(harness, monkeypatch):
    state, url = harness
    user, person, household = make_member("owner")
    connect_ai(person, url)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    account = make_account(person)
    txn = make_transaction(person, account)
    groceries_cat = groceries(household)
    state.session_answer = suggestion_json({txn.pk: groceries_cat.pk})
    queue_category_suggestions_for(person, [txn])
    process_due_jobs()
    assert len(state.session_prompts) == 1
    client = Client()
    client.force_login(user)
    first = client.get(reverse("transaction-list") + "?category=uncategorized")
    second = client.get(reverse("transaction-list") + "?category=uncategorized")
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(state.session_prompts) == 1
    txn.description = "Synthetic coffee changed"
    txn.save(update_fields=("description", "updated_at"))
    page = client.get(reverse("transaction-list") + "?category=uncategorized")
    assert b"Accept" not in page.content


@pytest.mark.django_db
def test_accept_sets_category_and_offers_rule(harness, monkeypatch):
    state, url = harness
    user, person, household = make_member("owner")
    connect_ai(person, url)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    account = make_account(person)
    groceries_cat = groceries(household)
    rows = [
        make_transaction(person, account, description="SYNTHETIC KROGER 1", fingerprint="a" * 64, amount_minor=-110),
        make_transaction(person, account, description="SYNTHETIC KROGER 2", fingerprint="b" * 64, amount_minor=-120),
        make_transaction(person, account, description="SYNTHETIC KROGER 3", fingerprint="c" * 64, amount_minor=-130),
    ]
    state.session_answer = suggestion_json({row.pk: groceries_cat.pk for row in rows})
    queue_category_suggestions_for(person, rows)
    process_due_jobs()
    client = Client()
    client.force_login(user)
    stamp_recent_auth(client)
    suggestion = CategorySuggestion.objects.get(transaction=rows[0])
    accept = client.post(
        reverse("suggestion-accept", args=[suggestion.pk]),
        {"next": "/transactions/?category=uncategorized"},
    )
    assert accept.status_code == 302
    rows[0].refresh_from_db()
    assert rows[0].category_id == groceries_cat.pk
    assert rows[0].category_source == Transaction.CategorySource.MANUAL
    shown = CategorySuggestion.objects.filter(status=CategorySuggestion.Status.PENDING)
    accept_all = client.post(
        reverse("suggestion-accept-all"),
        {
            "next": "/transactions/?category=uncategorized",
            "suggestion_id": [str(item.pk) for item in shown],
        },
    )
    assert accept_all.status_code == 302
    page = client.get(reverse("transaction-list") + "?category=uncategorized")
    assert b"SYNTHETIC KROGER" in page.content
    assert b"Preview rule" in page.content
    rules = client.get(reverse("category-rule-list") + "?description_contains=SYNTHETIC+KROGER&category=" + str(groceries_cat.pk))
    assert rules.status_code == 200
    assert b"SYNTHETIC KROGER" in rules.content


@pytest.mark.django_db
def test_reject_leaves_uncategorized(harness, monkeypatch):
    state, url = harness
    user, person, household = make_member("owner")
    connect_ai(person, url)
    monkeypatch.setattr("finance.ai_jobs.in_quiet_window", lambda moment=None: True)
    account = make_account(person)
    txn = make_transaction(person, account)
    groceries_cat = groceries(household)
    state.session_answer = suggestion_json({txn.pk: groceries_cat.pk})
    queue_category_suggestions_for(person, [txn])
    process_due_jobs()
    suggestion = CategorySuggestion.objects.get(transaction=txn)
    client = Client()
    client.force_login(user)
    client.post(reverse("suggestion-reject", args=[suggestion.pk]), {"next": "/transactions/?category=uncategorized"})
    txn.refresh_from_db()
    suggestion.refresh_from_db()
    assert txn.category_id is None
    assert suggestion.status == CategorySuggestion.Status.REJECTED


@pytest.mark.django_db
def test_snapshot_changes_with_description():
    owner_user = get_user_model().objects.create_user(username="snap", password=PASSWORD)
    owner = Person.objects.create(user=owner_user, display_name="Snap")
    account = make_account(owner)
    txn = make_transaction(owner, account, description="One")
    first = snapshot_hash(txn)
    txn.description = "Two"
    assert snapshot_hash(txn) != first


def _uncategorized_rows(owner, account, count, start=0):
    return [
        make_transaction(
            owner,
            account,
            description=f"Synthetic uncategorized {index}",
            amount_minor=-(100 + index),
            fingerprint=f"{index:064d}",
        )
        for index in range(start, start + count)
    ]


def _job_id_lists(person):
    jobs = list(AiJob.objects.filter(member=person, feature=FEATURE).order_by("pk"))
    return jobs, [job.input_refs["transaction_ids"] for job in jobs]


@pytest.mark.django_db
def test_eighty_five_uncategorized_rows_split_into_three_jobs(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_ai(person, url)
    account = make_account(person)
    _uncategorized_rows(person, account, 85)
    queue_remaining_uncategorized(person)
    jobs, id_lists = _job_id_lists(person)
    assert [len(ids) for ids in id_lists] == [BATCH_SIZE, BATCH_SIZE, 5]
    queued_ids = [pk for ids in id_lists for pk in ids]
    assert len(queued_ids) == 85
    assert len(set(queued_ids)) == 85
    queue_remaining_uncategorized(person)
    jobs_again, id_lists_again = _job_id_lists(person)
    assert len(jobs_again) == 3
    assert id_lists_again == id_lists
    assert jobs_again == jobs


@pytest.mark.django_db
def test_partly_filled_queued_job_tops_up_only_to_batch_size(harness):
    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_ai(person, url)
    account = make_account(person)
    _uncategorized_rows(person, account, BATCH_SIZE - 2)
    queue_remaining_uncategorized(person)
    jobs, id_lists = _job_id_lists(person)
    assert len(jobs) == 1
    assert len(id_lists[0]) == BATCH_SIZE - 2
    _uncategorized_rows(person, account, 10, start=BATCH_SIZE - 2)
    queue_remaining_uncategorized(person)
    jobs, id_lists = _job_id_lists(person)
    assert [len(ids) for ids in id_lists] == [BATCH_SIZE, 8]
    assert jobs[0].status == AiJob.Status.QUEUED
    queued_ids = [pk for ids in id_lists for pk in ids]
    assert len(queued_ids) == BATCH_SIZE + 8
    assert len(set(queued_ids)) == len(queued_ids)


@pytest.mark.django_db
def test_top_up_skips_a_job_the_runner_already_claimed(harness):
    from finance.category_suggestion_services import _top_up_queued_job

    _state, url = harness
    _user, person, _household = make_member("owner")
    connect_ai(person, url)
    account = make_account(person)
    _uncategorized_rows(person, account, 3)
    queue_remaining_uncategorized(person)
    jobs, id_lists = _job_id_lists(person)
    claimed = jobs[0]
    claimed.status = AiJob.Status.RUNNING
    claimed.save(update_fields=("status", "updated_at"))

    rest, topped = _top_up_queued_job(claimed.pk, [999001, 999002])

    assert topped is None
    assert rest == [999001, 999002]
    claimed.refresh_from_db()
    assert claimed.input_refs["transaction_ids"] == id_lists[0]
