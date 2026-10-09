"""Synthetic regression checks for bounded member input and query work (#307)."""
from datetime import date
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch
import errno
import json

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from finance import category_services
from finance.ai_services import AiError
from finance.alert_services import raise_large_transaction_alerts, unread_alert_count, purge_old_read_alerts
from finance.budget_services import carry_start_month, parse_month, save_budget
from finance.category_suggestion_services import _build_prompt, shared_description_contains
from finance.chat_services import sanitize_page_context, send_message
from finance.chat_views import _chat_context
from finance.csv_import import staging
from finance.csv_import.ofx import read_ofx, MAX_CURRENCY_ELEMENTS
from finance.csv_import.parser import CsvInputError, Mapping, MAX_COLUMNS, read_csv
from finance.csv_import.saved_mappings import save_csv_mapping
from finance.csv_import.services import commit_csv_import
from finance.forms import TransactionCorrectionForm
from finance.input_limits import (
    MAX_CHAT_MESSAGES, MAX_CHAT_PROMPT_CHARS, MAX_PAGE_CONTEXT_CHARS,
    MAX_PROMPT_CHARS, MAX_CARRY_MONTHS, BUDGET_MONTH_FLOOR,
    MAX_LARGE_ALERTS, MAX_UNUSUAL_FLAGS, MAX_UNUSUAL_ALERTS, MAX_AI_FACTS_CHARS,
)
from finance.models import AiConversation, AiConversationMessage, Alert, AlertSettings, Budget, Transaction
from finance.monthly_review import store_monthly_review
from finance.monthly_review_ai import bound_ai_facts
from finance.rule_services import apply_rule, preview_rule, save_category_rule
from tests.test_alerts import make_person, make_household, make_account, make_transaction
from tests.test_ofx_import import fixture as ofx_fixture
from tests.test_csv_import_views import upload


@pytest.mark.django_db
def test_edit_rejects_oversized_description():
    person = make_person("edit-limit")
    account = make_account(person)
    txn = make_transaction(person, account)
    form = TransactionCorrectionForm.for_transaction(txn,
        {"transaction_date": "2026-10-01", "description": "x" * 501, "amount": "-500.00"},
    )
    assert not form.is_valid()
    assert "description" in form.errors


def test_description_inference_and_prompt_are_bounded():
    start = perf_counter()
    assert shared_description_contains(["SYNTHETIC SHOP " + "x" * 10_000] * 3)
    assert perf_counter() - start < 1
    txns = [SimpleNamespace(pk=i, transaction_date=date(2026, 1, 1), amount_minor=-100,
                            currency="USD", description="Synthetic\nmerchant\r\n" * 10_000) for i in range(40)]
    prompt = _build_prompt(txns, [SimpleNamespace(pk=1, name="Groceries")])
    assert len(prompt) <= MAX_PROMPT_CHARS
    lines = prompt.split("Transactions:\n")[1].splitlines()
    assert len(lines) == 40
    assert all(line.startswith("- id=") for line in lines)


@pytest.mark.django_db
def test_oversized_chat_rejected_before_any_storage_and_page_context_dropped():
    person = make_person("chat-limit")
    with pytest.raises(AiError, match="at most"):
        send_message(person, "x" * (MAX_CHAT_PROMPT_CHARS + 1))
    assert not AiConversation.objects.exists()
    assert not AiConversationMessage.objects.exists()
    assert sanitize_page_context({"route": "/transactions/", "query": "q=" + "x" * MAX_PAGE_CONTEXT_CHARS}) == {}
    client = Client()
    client.force_login(person.user)
    response = client.post(reverse("chat-send"), {"prompt": "x" * (MAX_CHAT_PROMPT_CHARS + 1)})
    assert response.status_code == 302
    assert not AiConversationMessage.objects.exists()


@pytest.mark.django_db
def test_chat_context_keeps_newest_messages_in_order():
    person = make_person("chat-history")
    conversation = AiConversation.objects.create(member=person, backend="local", expires_at=timezone.now() + timezone.timedelta(days=1))
    AiConversationMessage.objects.bulk_create([
        AiConversationMessage(conversation=conversation, role="user", content=f"Synthetic {i}")
        for i in range(MAX_CHAT_MESSAGES + 20)
    ])
    request = RequestFactory().get("/chat/")
    context = _chat_context(person, conversation, request)
    assert len(context["chat_messages"]) == MAX_CHAT_MESSAGES
    assert context["chat_messages"][0].content == "Synthetic 20"
    assert context["chat_messages"][-1].content == f"Synthetic {MAX_CHAT_MESSAGES + 19}"


def wide_csv():
    return (",".join(f"Synthetic{i}" for i in range(MAX_COLUMNS + 1)) + "\n").encode()


def test_csv_and_ofx_element_caps():
    with pytest.raises(CsvInputError, match="column limit"):
        read_csv(wide_csv())
    content = ofx_fixture().replace(b"<CURDEF>USD", b"<CURDEF>USD" + b"<CURRENCY><CURSYM>USD</CURRENCY>" * (MAX_CURRENCY_ELEMENTS + 1))
    with pytest.raises(CsvInputError, match="currency elements"):
        read_ofx(content)


@pytest.mark.django_db
def test_wide_csv_is_rejected_without_kept_stage(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    person = make_person("wide-csv")
    make_household(person)
    account = make_account(person)
    client = Client()
    client.force_login(person.user)
    response = upload(client, account, wide_csv())
    assert response.status_code == 200
    assert b"column limit" in response.content
    assert not list(tmp_path.glob("*.csvstage"))
    with pytest.raises(ValidationError, match="columns"):
        save_csv_mapping(person, name="Synthetic wide", headers=[f"x{i}" for i in range(MAX_COLUMNS+1)],
                         mapping=Mapping("Date", "Memo", "iso", "dot_none", "signed", "Amount"))


def stage_request(user_id):
    return SimpleNamespace(user=SimpleNamespace(pk=user_id), session={})


def stage_upload():
    return SimpleUploadedFile("synthetic.csv", b"Date,Memo,Amount\n2026-01-01,Synthetic,-1.00\n")


def test_stage_caps_across_sessions_and_other_user_can_stage(settings, tmp_path, monkeypatch):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    monkeypatch.setattr(staging, "MAX_USER_STAGES", 2)
    staging.create_stage(stage_request(101), 1, stage_upload())
    staging.create_stage(stage_request(101), 2, stage_upload())
    with pytest.raises(CsvInputError, match="too many staged"):
        staging.create_stage(stage_request(101), 3, stage_upload())
    staging.create_stage(stage_request(202), 3, stage_upload())
    assert len(list(tmp_path.glob("*.csvstage"))) == 3
    monkeypatch.setattr(staging, "MAX_TOTAL_STAGES", 3)
    with pytest.raises(CsvInputError, match="storage"):
        staging.create_stage(stage_request(303), 4, stage_upload())


def test_stage_enospc_cleans_partial_file(settings, tmp_path, monkeypatch):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    original_open = Path.open
    class BrokenWriter:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def write(self, content):
            self.handle.write(content[:5])
            raise OSError(errno.ENOSPC, "Synthetic storage full")
    def failing_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return BrokenWriter(handle) if mode == "xb" else handle
    monkeypatch.setattr(Path, "open", failing_open)
    request = stage_request(101)
    with pytest.raises(CsvInputError, match="storage"):
        staging.create_stage(request, 1, stage_upload())
    assert not list(tmp_path.glob("*.csvstage"))
    assert not request.session.get(staging.SESSION_KEY)


@pytest.mark.django_db
def test_stage_quota_and_enospc_are_form_errors(settings, tmp_path, monkeypatch):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    person = make_person("quota-form")
    account = make_account(person)
    client = Client()
    client.force_login(person.user)
    monkeypatch.setattr(staging, "MAX_USER_STAGES", 0)
    response = upload(client, account)
    assert response.status_code == 200 and b"too many staged" in response.content
    monkeypatch.setattr(staging, "MAX_USER_STAGES", 5)
    with patch.object(staging.os, "chmod", side_effect=OSError(errno.ENOSPC, "Synthetic full")):
        response = upload(client, account)
    assert response.status_code == 200 and b"storage is unavailable" in response.content
    assert not list(tmp_path.glob("*.csvstage"))


@pytest.mark.django_db
def test_import_description_truncation_keeps_reimport_fingerprint():
    person = make_person("long-import")
    account = make_account(person)
    description = "Synthetic " + "x" * 900
    content = f"Date,Memo,Amount\n2026-01-01,{description},-1.00\n".encode()
    mapping = Mapping("Date", "Memo", "iso", "dot_none", "signed", "Amount")
    payload = dict(principal=person, account_id=account.pk, content=content, document=read_csv(content), mapping=mapping,
                   source="huntington", date_range_start=date(2026,1,1), date_range_end=date(2026,1,31))
    commit_csv_import(**payload)
    txn = Transaction.objects.get(account=account)
    assert len(txn.description) == 500
    assert txn.original_fields["Memo"] == description
    result = commit_csv_import(**payload)
    assert result.new_count == 0 and result.duplicate_count == 1


@pytest.mark.django_db
def test_unread_count_one_sql_query_and_large_alert_limit():
    person = make_person("alert-limit")
    make_household(person)
    account = make_account(person)
    AlertSettings.objects.create(person=person, large_transaction_minor=1)
    rows = [make_transaction(person, account, description=f"Synthetic {i}") for i in range(MAX_LARGE_ALERTS + 20)]
    created = raise_large_transaction_alerts(rows)
    assert len(created) == MAX_LARGE_ALERTS + 1
    assert not raise_large_transaction_alerts(rows)
    for count in (10, 100):
        Alert.objects.bulk_create([Alert(recipient=person, kind="sync", title="Synthetic", link="/", dedupe_key=f"synthetic:{count}:{i}") for i in range(count)])
        with CaptureQueriesContext(connection) as captured:
            unread_alert_count(person)
        assert len(captured) == 1
    Alert.objects.update(created_at=timezone.now() - timezone.timedelta(days=181))
    assert purge_old_read_alerts() >= MAX_LARGE_ALERTS + 1
    assert not Alert.objects.exists()


@pytest.mark.django_db
def test_budget_month_floor_constraint_and_clamped_legacy_span():
    person = make_person("budget-limit")
    assert parse_month("0001-01", today=date(2026,1,1)) == BUDGET_MONTH_FLOOR
    assert parse_month("9999-12", today=date(2026,1,1)) == date(2028,1,1)
    with pytest.raises(ValidationError, match="Budget month"):
        save_budget(person, {"scope":"private", "category":None, "amount_minor":100,
                             "effective_month":date(1,1,1), "rollover_enabled":True})
    budget = Budget(owner=person, rollover_enabled=True, rollover_started_month=date(1,1,1))
    with patch("finance.budget_services.last_reset_month", return_value=None):
        assert carry_start_month(budget, date(2026,1,1)) == date(2024,1,1)
    assert MAX_CARRY_MONTHS == 24
    with pytest.raises(IntegrityError), transaction.atomic():
        budget.save()


@pytest.mark.django_db
def test_transfer_scoring_query_count_and_work_cap(monkeypatch):
    person = make_person("transfer-limit")
    household = make_household(person)
    private = make_account(person)
    shared = make_account(person, scope="household", household=household)
    queries = []
    for size in (10, 40):
        rows = [make_transaction(person, private if i % 2 else shared, amount_minor=-100 if i%2 else 100,
                                 description="Synthetic payment") for i in range(size)]
        # The cap makes this adversarial equal-amount group bounded and uncertain.
        monkeypatch.setattr(category_services, "MAX_TRANSFER_CANDIDATES", 50)
        with CaptureQueriesContext(connection) as captured:
            category_services.refresh_transfer_pairs(person, transaction_ids=[row.pk for row in rows])
        queries.append(len(captured))
    assert queries[1] <= queries[0] + 10
    from finance.models import TransferPair
    assert not TransferPair.objects.filter(status="auto_marked").exists()


@pytest.mark.django_db
def test_unusual_review_top_flags_omitted_count_and_alert_cap():
    person = make_person("unusual-limit")
    make_household(person)
    account = make_account(person)
    AlertSettings.objects.create(person=person, large_transaction_minor=1)
    for i in range(500):
        make_transaction(person, account, transaction_date=date(2026,9,1),
                         description=f"Synthetic merchant {chr(97+i//26)}{chr(97+i%26)}", amount_minor=-100-i)
    review, _created = store_monthly_review(person, date(2026,9,1), today=date(2026,10,9))
    assert len(review.facts["unusual"]) == MAX_UNUSUAL_FLAGS
    assert review.facts["unusual_omitted_count"] >= 450
    assert Alert.objects.filter(recipient=person, kind="unusual_spending").count() <= MAX_UNUSUAL_ALERTS


def test_serialized_ai_facts_bound_preserves_json():
    facts = {"month":"2026-09", "unusual":[{"name":"x" * 10_000, "amount_minor":100} for _ in range(500)]}
    bounded = bound_ai_facts(facts)
    assert len(json.dumps(bounded)) <= MAX_AI_FACTS_CHARS
    assert bounded["unusual_omitted_count"] >= 450
    assert len(bounded["unusual"]) <= MAX_UNUSUAL_FLAGS


@pytest.mark.django_db
def test_rule_preview_and_apply_queries_do_not_scale_per_row():
    person = make_person("rule-limit")
    household = make_household(person)
    account = make_account(person)
    rule = save_category_rule(person, owner_kind="personal", description_contains="Synthetic",
                              account_id=None, min_amount_minor=None, max_amount_minor=None,
                              category_id=household.categories.get(name="Groceries").pk, priority=0)
    counts = []
    for size in (10, 100):
        for i in range(size):
            make_transaction(person, account, description=f"Synthetic shop {size}-{i}")
        with CaptureQueriesContext(connection) as preview_queries:
            _rule, matches = preview_rule(person, rule.pk)
        assert len(matches) >= size
        with CaptureQueriesContext(connection) as apply_queries:
            application, _skipped = apply_rule(person, rule.pk)
        assert application is not None
        reads = sum(query["sql"].lstrip().upper().startswith("SELECT") for query in apply_queries)
        counts.append((len(preview_queries), reads, len(apply_queries)))
        Transaction.objects.filter(account=account).update(category_source="manual")
    assert counts[0][:2] == counts[1][:2]
    # SQLite splits the 100-row history INSERT at its 999-parameter limit;
    # PostgreSQL writes it in one statement. Neither backend reloads per row.
    if connection.vendor == "sqlite":
        assert counts[1][2] <= counts[0][2] + 1
    else:
        assert counts[0][2] == counts[1][2]
