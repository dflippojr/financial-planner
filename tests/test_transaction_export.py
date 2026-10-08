import csv
import io
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
from django.test import Client
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from finance.category_services import split_transaction
from finance.models import CategorySuggestion, MemberSecurityEvent, Membership, RefundLink, SavedTransactionFilter, Tag, Transaction, TransactionSplit, TransferPair
from finance.reauth import RECENT_AUTH_SESSION_KEY
from finance.security_services import record_security_event
from finance.transaction_export import CSV_COLUMNS
from tests.test_transaction_search import make_account, make_household, make_person, make_transaction, PASSWORD


pytestmark = pytest.mark.django_db


def fresh_client(person):
    client = Client()
    client.force_login(person.user)
    session = client.session
    session[RECENT_AUTH_SESSION_KEY] = timezone.now().timestamp()
    session.save()
    return client


def download(client, filters=None):
    response = client.post(reverse("transaction-export"), filters or {})
    assert response.status_code == 200
    payload = b"".join(response.streaming_content).decode("utf-8")
    return response, list(csv.DictReader(io.StringIO(payload))), payload


def test_all_pages_stream_once_in_list_order_with_bounded_queries():
    owner = make_person("owner")
    account = make_account(owner)
    template = make_transaction(owner, account)
    Transaction.objects.bulk_create([
        Transaction(account=account, import_batch=template.import_batch,
                    transaction_date=template.transaction_date, amount_minor=-1234,
                    description="Synthetic row", original_fields={}, source_row_number=i + 3, fingerprint=f"{i:064x}")
        for i in range(405)
    ])
    archived = make_transaction(owner, account, description="Archived")
    archived.status = Transaction.Status.ARCHIVED
    archived.archived_at = timezone.now()
    archived.save()
    client = fresh_client(owner)
    with CaptureQueriesContext(connection) as queries, patch("finance.transaction_export.CHUNK_SIZE", 100), patch(
        "finance.transaction_export.record_security_event", wraps=record_security_event,
    ) as event:
        response, rows, _ = download(client, {"page": "2", "transaction_id": archived.pk, "select_matching": "1"})
    expected = list(Transaction.objects.filter(status="active").order_by("-transaction_date", "-pk").values_list("pk", flat=True))
    assert [int(row["transaction_id"]) for row in rows] == expected
    assert len(rows) == 406
    assert len(queries) < 45  # Related rows are fetched per chunk, never per transaction.
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == f'attachment; filename="financial-planner-transactions-{timezone.localdate():%Y%m%d}.csv"'
    assert "no-store" in response["Cache-Control"]
    assert event.call_count == 1
    assert set(event.call_args.kwargs) == {"request"}
    assert MemberSecurityEvent.objects.filter(event_type="member_data_export").count() == 1


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r", ""])
def test_text_escaping_json_and_exact_bigint_money(prefix):
    owner = make_person("owner")
    household = make_household(owner)
    category = household.categories.get(name="Groceries")
    category.name = prefix + 'Synthetic, "café"\ncategory'
    category.save()
    account = make_account(owner, name=prefix + "Synthetic account")
    tag = Tag.objects.create(household=household, name=prefix + "Synthetic tag")
    transactions = []
    for amount in (-9223372036854775808, 9223372036854775807):
        row = make_transaction(owner, account, description=prefix + 'Synthetic, "雪"\ntext', note=prefix + "Synthetic note", amount_minor=amount, category=category)
        row.tags.add(tag)
        transactions.append(row)
    _, rows, payload = download(fresh_client(owner))
    for row, original in zip(rows, reversed(transactions)):
        escape = "'" if prefix else ""
        assert row["description"] == escape + original.description
        assert row["note"] == escape + original.note
        assert row["account_name"] == escape + account.name
        assert row["category_name"] == escape + category.name
        assert int(row["amount_minor"]) == original.amount_minor
        assert Decimal(row["amount_decimal"]) * 100 == original.amount_minor
        assert row["currency"] == "USD"
        assert json.loads(row["tags"]) == [tag.name]
        assert json.loads(row["splits"]) == []
    assert tuple(rows[0]) == CSV_COLUMNS
    assert "original_fields" not in payload
    assert "fingerprint" not in payload


def test_filters_match_list_including_split_parents_and_saved_filters():
    owner = make_person("owner")
    household = make_household(owner)
    groceries = household.categories.get(name="Groceries")
    dining = household.categories.get(name="Dining")
    account = make_account(owner)
    tag = Tag.objects.create(household=household, name="Synthetic trip")
    hand = make_transaction(owner, account, description="Synthetic coffee", note="Trip note", category=groceries, category_source="manual", amount_minor=-2500)
    hand.tags.add(tag)
    make_transaction(owner, account, description="Synthetic rule", category=dining, category_source="rule", amount_minor=2500)
    ai = make_transaction(owner, account, description="Synthetic AI", category=dining, category_source="manual")
    CategorySuggestion.objects.create(member=owner, transaction=ai, category=dining, provider="agent_harness", backend="local", status="accepted", snapshot_hash="a" * 64)
    split = make_transaction(owner, account, description="Synthetic split", amount_minor=-10000)
    split_transaction(owner, split.pk, ({"category_id": groceries.pk, "amount_minor": -6000}, {"category_id": groceries.pk, "amount_minor": -4000}))
    make_transaction(owner, make_account(owner), description="Other account")
    client = fresh_client(owner)
    selections = [
        {"date_from": "2026-01-02", "date_to": "2026-01-02"}, {"account": account.pk},
        {"category": groceries.pk}, {"category": "uncategorized"}, {"q": "coffee"},
        {"q": "Trip note"}, {"tag": tag.pk}, {"scope": "private"}, {"scope": "household"},
        {"amount_min": "-25.00", "amount_max": "-12.00", "amount_mode": "signed"},
        {"amount_min": "25.00", "amount_max": "100.00", "amount_mode": "absolute"},
        {"has_note": "1"}, {"is_split": "1"}, {"set_by": "hand"}, {"set_by": "rule"}, {"set_by": "suggestion"},
        {"account": account.pk, "q": "coffee", "has_note": "1", "tag": tag.pk, "category": groceries.pk, "amount_max": "-25.00"},
    ]
    for filters in selections:
        listed = client.get(reverse("transaction-list"), filters)
        _, rows, _ = download(client, filters)
        assert [int(row["transaction_id"]) for row in rows] == [txn.pk for txn in listed.context["transactions"]]
    _, rows, _ = download(client, {"category": groceries.pk, "is_split": "1"})
    assert len(rows) == 1
    assert int(rows[0]["amount_minor"]) == -10000
    assert [part["amount_minor"] for part in json.loads(rows[0]["splits"])] == [-6000, -4000]
    saved = SavedTransactionFilter.objects.create(member=owner, name="Synthetic saved", query={"tag": str(tag.pk), "q": "coffee"})
    applied = client.get(reverse("transaction-saved-filter-apply", args=(saved.pk,)))
    filters = {key: values[0] for key, values in parse_qs(urlparse(applied.url).query).items()}
    _, rows, _ = download(client, filters)
    assert [int(row["transaction_id"]) for row in rows] == [hand.pk]


@pytest.mark.parametrize("filters", [{"date_from": "bad"}, {"date_from": "2026-02-01", "date_to": "2026-01-01"}, {"scope": "bad"}, {"amount_mode": "bad"}, {"amount_min": "2", "amount_max": "1"}, {"set_by": "bad"}])
def test_invalid_filters_have_no_rows_or_events(filters):
    owner = make_person("owner")
    make_transaction(owner, make_account(owner), description="Synthetic never disclosed")
    response = fresh_client(owner).post(reverse("transaction-export"), filters)
    assert response.status_code == 400
    assert response.content == b"Invalid transaction filters."
    assert not MemberSecurityEvent.objects.filter(event_type="member_data_export").exists()


def test_auth_csrf_reauth_resubmission_and_empty_header():
    owner = make_person("owner")
    account = make_account(owner)
    make_transaction(owner, account)
    client = Client()
    assert client.post(reverse("transaction-export")).url.startswith(reverse("login"))
    client.force_login(owner.user)
    assert client.get(reverse("transaction-export")).status_code == 405
    filters = {"account": str(account.pk), "q": "Synthetic", "page": "4"}
    response = client.post(reverse("transaction-export"), filters)
    next_url = parse_qs(urlparse(response.url).query)["next"][0]
    assert parse_qs(urlparse(next_url).query) == {"account": [str(account.pk)], "q": ["Synthetic"]}
    assert not MemberSecurityEvent.objects.filter(event_type="member_data_export").exists()
    confirmed = client.post(reverse("reauth"), {"password": PASSWORD, "next": next_url, "action": "export-data"})
    assert confirmed.url == next_url
    assert client.get(confirmed.url).status_code == 200
    assert not MemberSecurityEvent.objects.filter(event_type="member_data_export").exists()
    _, rows, _ = download(client, filters)
    assert len(rows) == 1
    _, rows, payload = download(client, {"q": "No synthetic match"})
    assert rows == []
    assert next(csv.reader(io.StringIO(payload))) == list(CSV_COLUMNS)
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner.user)
    assert csrf_client.post(reverse("transaction-export")).status_code == 403
    page = client.get(reverse("transaction-list"), filters).content.decode()
    assert 'action="/transactions/export/"' in page
    assert 'id="transaction-export-help"' in page


def test_private_metadata_and_revoked_household_access_are_hidden():
    owner = make_person("owner")
    other = make_person("other")
    household = make_household(owner)
    hidden_household = make_household(other)
    hidden_category = hidden_household.categories.get(name="Groceries")
    hidden_category.name = "Synthetic SECRET category"
    hidden_category.save()
    hidden_tag = Tag.objects.create(household=hidden_household, name="Synthetic SECRET tag")
    own = make_transaction(owner, make_account(owner), category=hidden_category)
    own.tags.add(hidden_tag)
    # Model foreign keys may retain a category after household access is lost.
    TransactionSplit.objects.create(transaction=own, category=hidden_category, amount_minor=own.amount_minor, position=0)
    secret = make_transaction(other, make_account(other, name="Synthetic SECRET account"), description="Synthetic SECRET description", note="Synthetic SECRET note")
    shared = make_transaction(other, make_account(other, name="Synthetic shared", scope="household", household=household))
    client = fresh_client(owner)
    _, rows, payload = download(client)
    assert "SECRET" not in payload
    assert {int(row["transaction_id"]) for row in rows} == {own.pk, shared.pk}
    own_row = next(row for row in rows if int(row["transaction_id"]) == own.pk)
    assert own_row["category_name"] == ""
    assert json.loads(own_row["tags"]) == []
    assert json.loads(own_row["splits"])[0]["category_name"] == ""
    for filters in ({"account": secret.account_id}, {"tag": hidden_tag.pk}, {"category": hidden_category.pk}):
        response = client.post(reverse("transaction-export"), filters)
        assert response.status_code == 400
        assert b"SECRET" not in response.content
    membership = Membership.objects.get(person=owner, household=household)
    membership.ended_at = timezone.now()
    membership.save()
    _, rows, _ = download(client)
    assert [int(row["transaction_id"]) for row in rows] == [own.pk]


def test_transfer_flags_follow_visibility_and_omit_relationship_ids():
    owner = make_person("owner")
    other = make_person("other")
    household = make_household(owner, other)
    outflow = make_transaction(owner, make_account(owner), description="Synthetic transfer", amount_minor=-5000)
    inflow = make_transaction(other, make_account(other, scope="household", household=household), amount_minor=5000)
    TransferPair.objects.create(leg_a=outflow, leg_b=inflow, status="confirmed", kind="transfer", confidence="high", reasons=[])
    purchase = make_transaction(other, make_account(other), description="Synthetic SECRET original")
    refund = make_transaction(owner, outflow.account, amount_minor=1234)
    RefundLink.objects.create(refund=refund, original=purchase)
    client = fresh_client(owner)
    _, rows, payload = download(client, {"category": "transfer"})
    assert {int(row["transaction_id"]) for row in rows} == {outflow.pk, inflow.pk}
    assert all(row["excluded_from_income_and_spending"] == "true" for row in rows)
    _, rows, payload = download(client)
    assert len(rows) == 3
    assert "SECRET" not in payload
    assert "original" not in payload
    assert "leg_a" not in payload and "leg_b" not in payload
    inflow.account.scope = "private"
    inflow.account.household = None
    inflow.account.share_mode = ""
    inflow.account.save()
    _, rows, _ = download(client, {"category": "transfer"})
    assert rows == []
    _, rows, _ = download(client)
    assert {int(row["transaction_id"]) for row in rows} == {outflow.pk, refund.pk}
    assert all(row["excluded_from_income_and_spending"] == "false" for row in rows)
