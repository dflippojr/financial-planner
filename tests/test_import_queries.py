import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from finance.models import (
    AiJob, AiProviderConnection, Alert, AlertSettings,
    RuleApplication, RuleApplicationEntry, Transaction, TransactionCorrectionHistory,
)
from finance.category_suggestion_services import queue_category_suggestions_for
from finance.alert_services import raise_large_transaction_alerts
from finance.policy_services import accept_policy, publish_policy
from finance.rule_services import reverse_application
from tests.import_benchmark import import_content, seed


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("profile", ["huntington", "ofx"])
def test_import_request_query_ceiling_and_bulk_scaling(tmp_path, profile):
    person, account = seed(budgets=0)
    client = Client()
    client.force_login(person.user)
    url = reverse("csv-import-preview", args=[account.pk])
    counts = []
    for size in (500, 5000):
        # Distinct descriptions make the second file new rather than overlapping.
        content = import_content(size, profile).replace(b"MERCHANT", f"MERCHANT {size}".encode())
        with override_settings(CSV_IMPORT_STAGING_DIR=tmp_path):
            uploaded = client.post(url, {"action": "upload", "import_profile": profile,
                "csv_file": SimpleUploadedFile("synthetic.csv" if profile == "huntington" else "synthetic.ofx", content)})
            assert uploaded.status_code == 200
            form = uploaded.context["mapping_form"]
            with CaptureQueriesContext(connection) as queries:
                response = client.post(url, {"action": "commit", "token": form.data["token"],
                    "date_range_start": "2026-09-01", "date_range_end": "2026-09-30"})
        assert response.status_code == 302
        assert not list(tmp_path.iterdir())
        if size == 500:
            # Count each kind of bulk write once on SQLite, whose 999-parameter
            # limit splits these same PostgreSQL statements into several writes.
            writes = [q["sql"].split(" SET ")[0].split(" (")[0]
                      for q in queries if q["sql"].startswith(("INSERT", "UPDATE"))]
            extra_bulk = len(writes) - len(set(writes)) if connection.vendor == "sqlite" else 0
            assert len(queries) - extra_bulk <= 60
        # SQLite splits bulk writes at its parameter limit; PostgreSQL doesn't.
        # Every other query must have a fixed ceiling, even for the larger file.
        fixed = [q for q in queries if not q["sql"].startswith(("INSERT", "UPDATE"))]
        counts.append(len(fixed))
    assert counts[1] == counts[0]
    imported = Transaction.objects.filter(description__startswith="SYNTHETIC MERCHANT")
    assert imported.count() == 5500
    assert imported.filter(category_source=Transaction.CategorySource.RULE).count() == 5500
    assert RuleApplicationEntry.objects.count() == 5500
    assert TransactionCorrectionHistory.objects.count() == 5500
    # Automatic bulk applications retain the same undo snapshots as manual ones.
    reverse_application(person, RuleApplication.objects.order_by("pk").first().pk)
    assert imported.filter(category_source=Transaction.CategorySource.UNSET, category__isnull=True).count() == 500


@pytest.mark.django_db
def test_suggestion_queue_batches_existing_jobs_and_backend_resolution():
    person, account = seed(budgets=0)
    policy = publish_policy(material=True, body="Synthetic benchmark policy")
    from finance.models import Person
    for member in Person.objects.all():
        accept_policy(member, policy)
    AiProviderConnection.objects.create(owner=person, encrypted_token=b"synthetic-unused",
        base_url="http://synthetic.invalid", background_backend="local")
    rows = list(Transaction.objects.filter(account=account, category__isnull=True)[:500])
    # Add a partly filled job, many full jobs, a claimed job and a session retry.
    full = [AiJob(member=person, feature="category_suggestions", input_refs={"transaction_ids": list(range(-40, 0))}) for _ in range(100)]
    AiJob.objects.bulk_create(full)
    partial = AiJob.objects.create(member=person, feature="category_suggestions", input_refs={"transaction_ids": [-100]})
    claimed = AiJob.objects.create(member=person, feature="category_suggestions", status="running", input_refs={"transaction_ids": [rows[0].pk]})
    retry = AiJob.objects.create(member=person, feature="category_suggestions", harness_session_id="synthetic-session", input_refs={"transaction_ids": [rows[1].pk]})
    with CaptureQueriesContext(connection) as queries:
        jobs = queue_category_suggestions_for(person, rows)
    assert len(queries) <= 20
    assert all(len(job.input_refs["transaction_ids"]) <= 40 for job in jobs)
    partial.refresh_from_db()
    assert len(partial.input_refs["transaction_ids"]) == 40
    for job, expected in ((claimed, [rows[0].pk]), (retry, [rows[1].pk])):
        job.refresh_from_db()
        assert job.input_refs["transaction_ids"] == expected
    queued = [pk for job in jobs for pk in job.input_refs["transaction_ids"] if pk > 0]
    assert set(queued) == {row.pk for row in rows[2:]}
    assert len(queued) == len(set(queued))
    assert queue_category_suggestions_for(person, rows) == []


@pytest.mark.django_db
def test_large_transaction_alerts_batch_reads_and_writes_without_duplicates():
    person, account = seed(budgets=0)
    AlertSettings.objects.update(large_transaction_minor=100)
    rows = list(Transaction.objects.filter(account=account).select_related("account", "account__household")[:500])
    with CaptureQueriesContext(connection) as queries:
        alerts = raise_large_transaction_alerts(rows)
    assert len(queries) <= 15
    assert len(alerts) == 1000  # shared account, both current members
    assert all(alert.pk for alert in alerts)
    assert raise_large_transaction_alerts(rows) == []
    assert Alert.objects.count() == 1000
