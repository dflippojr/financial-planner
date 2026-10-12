"""PostgreSQL lock compatibility for phrasing and harness connection changes."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections, transaction
from django.db.models.query import QuerySet
from django.utils import timezone

from finance import ai_jobs
from finance.ai_services import connect_harness, disconnect_harness
from finance.models import AiJob, AuditEvent, MonthlyReview, Person
from finance.monthly_review import latest_closed_month
from tests.test_ai_member_quotas import _review_member, review_harness  # noqa: F401 - fixture
from tests.test_monthly_review_ai import TOKEN


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["enqueue", "complete"])
@pytest.mark.parametrize("change", ["disconnect", "reconnect"])
def test_member_lock_allows_harness_change_and_its_audit_to_commit(review_harness, monkeypatch, operation, change):
    if connection.vendor != "postgresql":
        pytest.skip("foreign-key row lock compatibility requires PostgreSQL")
    _state, url = review_harness
    owner = _review_member(review_harness)
    job = AiJob.objects.get(member=owner, feature="monthly_review")
    AiJob.objects.filter(pk=job.pk).update(status=AiJob.Status.RUNNING, harness_session_id="synthetic-session")
    job.refresh_from_db()
    action = AuditEvent.Action.AI_HARNESS_DISCONNECTED if change == "disconnect" else AuditEvent.Action.AI_HARNESS_CONNECTED
    before = AuditEvent.objects.filter(action=action, actor=owner).count()
    changed = []
    real_first = QuerySet.first

    def change_connection():
        try:
            with transaction.atomic():
                # Bound a broken lock acquisition, rather than relying on sleeps
                # or a lucky schedule to expose incompatible FK checks.
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '2s'")
                if change == "disconnect":
                    disconnect_harness(owner)
                else:
                    connect_harness(owner, base_url=url, token=TOKEN)
            return AuditEvent.objects.filter(action=action, actor=owner).count()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as worker:
        def change_after_member_lock(qs):
            locked = real_first(qs)
            if qs.model is Person and qs.query.select_for_update and not changed:
                # The main transaction holds the member lock but has not yet
                # locked the job. Commit the real connection transaction first;
                # it clears the saved session and checks deferred audit FKs.
                assert locked.pk == owner.pk
                changed.append(worker.submit(change_connection).result(timeout=10))
            return locked

        monkeypatch.setattr(QuerySet, "first", change_after_member_lock)
        if operation == "enqueue":
            review = MonthlyReview.objects.get(person=owner, month=latest_closed_month())
            assert ai_jobs.enqueue_review_phrasing(owner, feature="monthly_review", review=review).pk == job.pk
        else:
            job.status = AiJob.Status.SUCCEEDED
            job.finished_at = timezone.now()
            assert ai_jobs._save_job_success(job)

    assert changed == [before + 1]
    job.refresh_from_db()
    assert job.status == (AiJob.Status.RUNNING if operation == "enqueue" else AiJob.Status.SUCCEEDED)
