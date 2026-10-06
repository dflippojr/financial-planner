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
    person, account = seed()
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
            assert len(queries) - extra_bulk <= 60, "\n".join(q["sql"][:160] for q in queries)
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


@pytest.mark.django_db(transaction=True)
def test_suggestion_queue_batches_existing_jobs_and_backend_resolution():
    person, account = seed(budgets=0)
    policy = publish_policy(material=True, body="Synthetic benchmark policy")
    from finance.models import Person
    for member in Person.objects.all():
        accept_policy(member, policy)
    AiProviderConnection.objects.create(owner=person, encrypted_token=b"synthetic-unused",
        base_url="http://synthetic.invalid", background_backend="local")
    Transaction.objects.filter(account=account).update(category=None, category_source=Transaction.CategorySource.UNSET)
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


@pytest.mark.django_db(transaction=True)
def test_large_transaction_alerts_batch_reads_and_writes_without_duplicates():
    person, account = seed(budgets=0)
    AlertSettings.objects.update(large_transaction_minor=100)
    rows = list(Transaction.objects.filter(account=account).select_related("account", "account__household")[:500])
    with CaptureQueriesContext(connection) as queries:
        alerts = raise_large_transaction_alerts(rows)
    # SQLite splits the 1,000-row bulk insert at its parameter limit; count that once.
    writes = [q["sql"].split("(")[0] for q in queries if q["sql"].startswith("INSERT")]
    extra_bulk = len(writes) - len(set(writes)) if connection.vendor == "sqlite" else 0
    assert len(queries) - extra_bulk <= 15, "\n".join(q["sql"][:140] for q in queries)
    assert len(alerts) == 1000  # shared account, both current members
    assert all(alert.pk for alert in alerts)
    assert raise_large_transaction_alerts(rows) == []
    assert Alert.objects.count() == 1000
    Alert.objects.exclude(recipient=person).delete()
    topped_up = raise_large_transaction_alerts(rows)
    assert len(topped_up) == 500
    assert all(alert.recipient_id != person.pk for alert in topped_up)
    assert Alert.objects.count() == 1000


@pytest.mark.django_db(transaction=True)
def test_concurrent_reimports_serialize_on_the_account():
    if connection.vendor != "postgresql":
        pytest.skip("concurrent imports require PostgreSQL row locks")
    import threading
    from datetime import date
    from django.db import connections
    from finance.csv_import.parser import read_csv
    from finance.csv_import.profiles import HUNTINGTON_MAPPING
    from finance.csv_import.services import commit_csv_import
    from tests.test_csv_import_views import make_person
    from finance.models import Account

    _user, person = make_person("synthetic-import-owner")
    account = Account.objects.create(name="Synthetic account", account_type="checking", owner=person)
    content = import_content(500, "huntington")
    document = read_csv(content)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def commit():
        try:
            barrier.wait(timeout=10)
            result = commit_csv_import(person, account.pk, content=content, document=document,
                mapping=HUNTINGTON_MAPPING, source="huntington",
                date_range_start=date(2026, 9, 1), date_range_end=date(2026, 9, 30))
            results.append(result.new_count)
        except Exception as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    workers = [threading.Thread(target=commit) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert sorted(results) == [0, 500]
    assert Transaction.objects.filter(account=account).count() == 500


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("reset_in_current_period", [True, False])
def test_prefetched_budget_progress_matches_amount_changes_and_rollover_resets(reset_in_current_period):
    from datetime import date, timedelta
    from django.utils import timezone
    from finance.budget_services import progress_snapshot, progress_snapshots
    from finance.models import Budget, BudgetAmount, BudgetRolloverReset

    person, _account = seed()
    budget = Budget.objects.select_related("category").order_by("pk").first()
    budget.rollover_enabled = True
    budget.rollover_started_month = date(2026, 8, 1)
    budget.rollover_enabled_at = timezone.now() - timedelta(days=1)
    budget.save()
    BudgetAmount.objects.create(budget=budget, effective_month=date(2026, 9, 1), amount_minor=5000)
    reset = BudgetRolloverReset.objects.create(budget=budget, month=date(2026, 9, 1), actor=person)
    if not reset_in_current_period:
        BudgetRolloverReset.objects.filter(pk=reset.pk).update(created_at=timezone.now() - timedelta(days=2))
    expected = progress_snapshot(budget, date(2026, 10, 1), person)
    cached = Budget.objects.select_related("category").prefetch_related("amounts", "rollover_resets").get(pk=budget.pk)
    actual = progress_snapshots([cached], date(2026, 10, 1), person)[budget.pk]
    assert actual.amount_minor == expected.amount_minor == 5000
    assert actual.carry_minor == expected.carry_minor
    assert actual.spent_minor == expected.spent_minor
    assert actual.available_minor == expected.available_minor
