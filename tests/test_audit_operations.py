"""Synthetic delegated work, rollback, privacy and operator-journal contracts."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone as utc_timezone
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone

from finance.audit_operations import execution, operation, outcome
from finance.audit_services import events_for
from finance.models import AuditEvent, AiJob, ImportBatch, Membership
from finance.simplefin_errors import SimpleFinError
from finance.simplefin_services import sync_connection, sync_all_connections
from ops.backup.audit_journal import append, query
from tests.test_ai_provider import TOKEN, make_member, harness  # noqa: F401
from tests.test_simplefin import connect_owner, account_payload, make_account


pytestmark = pytest.mark.django_db
CANARY = "synthetic-secret@example.test/provider-body/path/key"


def operational_rows(person, name):
    return list(events_for(person).filter(action="operation_outcome", metadata__operation=name).order_by("occurred_at"))


@pytest.mark.parametrize("kind,source", [("member", "ui"), ("scheduler", "job"), ("operator", "cli")])
def test_sync_actor_and_effective_owner(monkeypatch, kind, source):
    _user, person, _household = make_member("sync")
    connection = connect_owner(person, monkeypatch)
    run = uuid.uuid4()
    with operation(actor_kind=kind, source=source, run_id=run):
        sync_connection(person, connection.pk, ignore_rate_limit=True)
    row, = operational_rows(person, "simplefin_sync")
    assert (row.actor_kind, row.source, row.effective_member_id) == (kind, source, person.pk)
    assert row.actor_id == (person.pk if kind == "member" else None)
    assert row.correlation_id == run
    assert row.metadata == {"operation": "simplefin_sync", "connection_id": connection.pk, "new_count": 0}
    assert row.outcome == "succeeded"


def test_daily_sync_has_scheduler_and_no_other_member_audience(monkeypatch):
    _user, person, household = make_member("sync")
    _other_user, other, _ = make_member("other", household=household)
    connect_owner(person, monkeypatch)
    sync_all_connections()
    row, = operational_rows(person, "simplefin_sync")
    assert row.actor_kind == "scheduler" and row.actor_id is None
    assert not operational_rows(other, "simplefin_sync")


def test_sync_provider_failure_is_sanitized_and_persists(monkeypatch):
    _user, person, _household = make_member("sync")
    connection = connect_owner(person, monkeypatch)
    monkeypatch.setattr("finance.simplefin_services.fetch_accounts", lambda *a, **k: (_ for _ in ()).throw(SimpleFinError(CANARY)))
    with pytest.raises(SimpleFinError):
        sync_connection(person, connection.pk)
    row, = operational_rows(person, "simplefin_sync")
    assert row.outcome == "failed" and row.metadata["failure"] == "provider_error"
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_inner_import_rollback_keeps_only_failure(monkeypatch):
    from finance.models import AccountLink

    _user, person, _household = make_member("sync")
    connection = connect_owner(person, monkeypatch, account_payload())
    account = make_account(person)
    AccountLink.objects.create(connection=connection, account=account, simplefin_account_id="CON-1:sf-checking", cutover_date=date(2026, 1, 1), mode="transactions")

    def fail(*args):
        outcome(person, "monthly_review")
        raise SimpleFinError(CANARY)

    monkeypatch.setattr("finance.simplefin_services._sync_one_link", fail)
    with pytest.raises(SimpleFinError):
        sync_connection(person, connection.pk)
    row, = operational_rows(person, "simplefin_sync")
    assert row.outcome == "failed" and row.metadata["failure"] == "import_failed"
    assert not operational_rows(person, "monthly_review")
    assert not ImportBatch.objects.exists()


def test_operator_eviction_correlates_existing_domain_events_and_declares_identity():
    _user, person, household = make_member("target")
    _user2, operator, _ = make_member("operator", household=household)
    account = make_account(person, scope="household", household=household)
    call_command("evict_household_member", username=person.user.username, operator_member=operator.pk, stdout=StringIO())
    rows = list(events_for(operator).filter(source="cli"))
    assert {row.action for row in rows} == {"member_left", "account_owner_changed"}
    assert len({row.correlation_id for row in rows}) == 1
    assert all(row.actor_id is None and row.actor_kind == "operator" and row.declared_operator_id == operator.pk for row in rows)
    assert all(row.effective_member_id == person.pk for row in rows)
    assert not events_for(person).filter(source="cli").exists()
    account.refresh_from_db()
    assert account.owner == operator


def test_invalid_operator_is_rejected_before_action():
    _user, person, _ = make_member("target")
    with pytest.raises(CommandError):
        call_command("evict_household_member", username=person.user.username, operator_member=999999, stdout=StringIO())
    assert Membership.objects.filter(person=person, ended_at__isnull=True).exists()


def test_bootstrap_reuses_domain_event_without_recovery_material():
    from finance.models import Person

    with patch("getpass.getpass", return_value="Synthetic-passphrase-42!"):
        call_command("seed_first_user", username="seed", display_name=CANARY, household=CANARY, stdout=StringIO())
    person = Person.objects.get(user__username="seed")
    row, = events_for(person)
    assert row.action == "household_created" and row.actor_kind == "operator"
    assert row.actor_id is None and row.effective_member_id == person.pk
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_command_summaries_never_include_targets_or_cross_member_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    _user, person, _ = make_member("target")
    call_command("rebuild_transfer_pairs", username=person.user.username, stdout=StringIO())
    rows = list(query(tmp_path))
    assert [row["outcome"] for row in rows] == ["started", "succeeded"]
    assert all(row["actor_kind"] == "operator" and row["operation"] == "rebuild_transfer_pairs" for row in rows)
    assert all("target_id" not in row and "row_count" not in row for row in rows)
    event, = operational_rows(person, "transfer_rebuild")
    assert str(event.correlation_id) == rows[0]["correlation_id"]


def test_audit_gap_does_not_retry_sync(monkeypatch, django_capture_on_commit_callbacks, caplog):
    _user, person, _ = make_member("sync")
    connection = connect_owner(person, monkeypatch)
    with patch.object(AuditEvent, "save", side_effect=DatabaseError(CANARY)):
        with django_capture_on_commit_callbacks(execute=True):
            result = sync_connection(person, connection.pk)
    assert result["imported"] == 0
    connection.refresh_from_db()
    assert connection.last_sync_at is not None
    assert "Audit write gap" in caplog.text and CANARY not in caplog.text


def test_ai_claim_inference_usage_and_final_outcome_share_attempt(harness):
    from finance.ai_jobs import enqueue_job, process_due_jobs
    from finance.ai_services import connect_harness, set_defaults

    state, url = harness
    state.model_state = "ready"
    state.session_answer = CANARY
    _user, person, _ = make_member("ai")
    connection = connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    process_due_jobs()
    job.refresh_from_db()
    assert job.status == "succeeded"
    rows = list(events_for(person).filter(action="operation_outcome"))
    assert len(rows) == 4
    assert {row.correlation_id for row in rows} == {job.audit_run_id}
    assert all(row.metadata["job_id"] == job.pk and row.metadata["attempt"] == 1 for row in rows)
    assert all(row.actor_kind == "scheduler" and row.actor_id is None for row in rows)
    inference = [row for row in rows if row.metadata["operation"] == "ai_inference"]
    assert {row.outcome for row in inference} == {"started", "succeeded"}
    assert all(row.metadata["connection_id"] == connection.pk for row in inference)
    assert any("usage_id" in row.metadata for row in inference)
    before = len(rows)
    process_due_jobs()
    assert events_for(person).filter(action="operation_outcome").count() == before
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_ai_retry_has_new_attempt_id(harness):
    from finance.ai_jobs import enqueue_job, process_due_jobs
    from finance.ai_services import connect_harness, set_defaults

    state, url = harness
    state.model_state = "ready"
    _user, person, _ = make_member("ai")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    state.session_failure = "provider_unavailable"
    process_due_jobs()
    job.refresh_from_db()
    first = job.audit_run_id
    AiJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
    process_due_jobs()
    job.refresh_from_db()
    assert job.audit_run_id != first and job.attempts == 2
    assert len(operational_rows(person, "ai_job")) == 1
    assert len(operational_rows(person, "ai_claim")) == 2


def test_journal_rotation_read_only_query_and_tamper_detection(tmp_path):
    now = datetime(2026, 10, 8, tzinfo=utc_timezone.utc)
    run = uuid.uuid4()
    append("restore", "started", run, directory=tmp_path, now=now - timedelta(days=91))
    old = tmp_path / "2026-07-09.jsonl"
    assert old.exists()
    append("restore", "succeeded", run, directory=tmp_path, now=now)
    assert not old.exists()
    current = tmp_path / "2026-10-08.jsonl"
    before = current.read_bytes()
    row, = query(tmp_path)
    assert row["checksum_valid"]
    assert current.read_bytes() == before
    current.write_text(before.decode().replace('"restore"', '"backup"'))
    assert list(query(tmp_path)) == [{"warning": "Invalid journal record"}]
    current.write_text(CANARY)
    assert CANARY not in json.dumps(list(query(tmp_path)))


def test_journal_failure_warns_without_exception_or_side_effect_retry(tmp_path, capsys):
    path = tmp_path / "not-a-directory"
    path.write_text(CANARY)
    assert not append("backup", "started", uuid.uuid4(), directory=path)
    stderr = capsys.readouterr().err
    assert "Audit write gap" in stderr and CANARY not in stderr


def test_context_resets_after_failure_and_rollbacks_leave_no_outcome():
    _user, person, _ = make_member("member")
    with pytest.raises(RuntimeError):
        with operation():
            with transaction.atomic():
                outcome(person, "transfer_rebuild")
                raise RuntimeError(CANARY)
    assert execution.get() is None
    assert not operational_rows(person, "transfer_rebuild")


def test_operator_deletion_removes_private_outcomes_and_anonymizes_shared_declarations(monkeypatch, tmp_path):
    from finance.lifecycle_services import rename_account
    from finance.models import Person

    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    _user, person, household = make_member("delete")
    _other_user, other, _ = make_member("other", household=household)
    account = make_account(other, scope="household", household=household)
    with operation(actor_kind="operator", source="cli", declared_operator=person):
        rename_account(other, account.pk, "Synthetic renamed")
        outcome(person, "transfer_rebuild")
    pk = person.pk
    with patch("builtins.input", return_value=person.user.username):
        call_command("delete_member_data", username=person.user.username, stdout=StringIO())
    assert not Person.objects.filter(pk=pk).exists()
    assert not AuditEvent.objects.filter(private_owner_id=pk).exists()
    assert not AuditEvent.objects.filter(declared_operator_id=pk).exists()
    retained = events_for(other).get(action="account_renamed")
    assert retained.actor_kind == "operator" and retained.declared_operator_id is None
    assert [row["outcome"] for row in query(tmp_path) if row["operation"] == "delete_member_data"] == ["started", "succeeded"]


def test_publish_and_reviews_and_sync_commands_record_operator_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_AUDIT_DIR", str(tmp_path))
    _user, person, _ = make_member("member")
    with patch("finance.policy_services.load_policy_source", return_value=CANARY):
        call_command("publish_privacy_policy", material=True, stdout=StringIO())
    call_command("generate_monthly_reviews", stdout=StringIO())
    call_command("sync_simplefin", stdout=StringIO())
    rows = list(query(tmp_path))
    for command in ("publish_privacy_policy", "generate_monthly_reviews", "sync_simplefin"):
        selected = [row for row in rows if row["operation"] == command]
        assert [row["outcome"] for row in selected] == ["started", "succeeded"]
        assert len({row["correlation_id"] for row in selected}) == 1
    assert CANARY not in json.dumps(rows)
    reviews = operational_rows(person, "monthly_review")
    assert reviews and reviews[0].actor_kind == "operator" and reviews[0].actor_id is None


def test_ai_resuming_saved_session_adds_no_new_inference_or_claim(harness, settings):
    from finance.ai_jobs import enqueue_job, process_due_jobs
    from finance.ai_services import connect_harness, set_defaults

    state, url = harness
    state.model_state = "ready"
    state.running_polls = 1000
    settings.AGENT_HARNESS_SESSION_TIMEOUT_SECONDS = 0
    _user, person, _ = make_member("ai")
    connect_harness(person, base_url=url, token=TOKEN)
    set_defaults(person, chat_backend="local", background_backend="local")
    job = enqueue_job(person, feature="structured")
    for _ in range(3):
        AiJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
        process_due_jobs()
    job.refresh_from_db()
    assert job.attempts == 1
    assert len(operational_rows(person, "ai_claim")) == 1
    assert len(operational_rows(person, "ai_inference")) == 2
    assert not operational_rows(person, "ai_job")
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1


def test_chat_claim_and_conditional_finish_and_stale_recovery(harness):
    from finance.chat_runner import claim_next_turn, run_claimed_turn, recover_stale_turns
    from finance.chat_services import send_message
    from finance.ai_services import connect_harness
    from finance.models import AiConversationMessage

    _state, url = harness
    _user, person, _ = make_member("chat")
    connect_harness(person, base_url=url, token=TOKEN)
    send_message(person, CANARY)
    pk = claim_next_turn("synthetic-worker")
    assert pk is not None
    assert run_claimed_turn(pk, "synthetic-worker", sleep=lambda _s: None)
    assert not run_claimed_turn(pk, "synthetic-worker", sleep=lambda _s: None)
    rows = operational_rows(person, "chat_turn")
    assert [row.outcome for row in rows] == ["started", "succeeded"]
    assert len({row.correlation_id for row in rows}) == 1
    send_message(person, CANARY)
    stale_pk = claim_next_turn("dead-worker")
    AiConversationMessage.objects.filter(pk=stale_pk).update(heartbeat_at=timezone.now() - timedelta(hours=1))
    assert recover_stale_turns() == 1
    assert recover_stale_turns() == 0
    assert not run_claimed_turn(stale_pk, "dead-worker")
    assert operational_rows(person, "chat_turn")[-1].metadata["failure"] == "stale"
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_shared_inference_names_credential_owner_but_stays_requester_only(harness):
    from finance.ai_services import connect_harness, set_offer_local_to_household, set_shared_local_use, run_structured
    from finance.policy_services import current_policy

    state, url = harness
    state.model_state = "ready"
    _host_user, host, household = make_member("host")
    _guest_user, guest, _ = make_member("guest", household=household, policy=current_policy())
    connection = connect_harness(host, base_url=url, token=TOKEN)
    set_offer_local_to_household(host, True)
    set_shared_local_use(guest, chat=False, background=True)
    with operation():
        result = run_structured(guest, CANARY, feature="structured", backend="local", sleep=lambda _s: None)
    assert result.ok
    rows = operational_rows(guest, "ai_inference")
    assert len(rows) == 2
    assert all(row.effective_member_id == host.pk and row.affected_member_id == guest.pk for row in rows)
    assert all(row.metadata["connection_id"] == connection.pk and row.actor_id is None for row in rows)
    assert not operational_rows(host, "ai_inference")


def test_provider_error_list_does_not_claim_a_clean_sync(monkeypatch):
    _user, person, _household = make_member("sync")
    payload = account_payload()
    payload["errlist"] = [CANARY]
    connection = connect_owner(person, monkeypatch, payload)
    sync_connection(person, connection.pk)
    row, = operational_rows(person, "simplefin_sync")
    assert row.outcome == "failed" and row.metadata["failure"] == "provider_error"
    assert CANARY not in str(list(AuditEvent.objects.values()))


def test_usage_write_failure_keeps_successful_inference_without_retry(harness, caplog):
    from finance.ai_services import connect_harness, run_structured
    from finance.models import AiUsageEvent

    state, url = harness
    _user, person, _ = make_member("ai")
    connect_harness(person, base_url=url, token=TOKEN)
    with patch.object(AiUsageEvent.objects, "create", side_effect=DatabaseError(CANARY)):
        result = run_structured(person, CANARY, feature="structured", backend="local", sleep=lambda _s: None)
    assert result.ok
    assert state.requests.count(("POST", "/api/v1/sessions")) == 1
    assert len(operational_rows(person, "ai_inference")) == 2
    assert "Audit write gap" in caplog.text and CANARY not in caplog.text


def test_late_worker_cannot_duplicate_final_outcomes_or_change_newer_attempt():
    from finance.ai_jobs import _save_job_success, _fail, _retry_or_fail

    _user, person, _ = make_member("ai")
    job = AiJob.objects.create(member=person, feature="structured", status="running", attempts=1)
    job.status = "succeeded"
    job.finished_at = timezone.now()
    assert _save_job_success(job)
    assert not _save_job_success(job)
    _fail(job, "provider_error")
    _retry_or_fail(job, timezone.now(), "provider_error")
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert len(operational_rows(person, "ai_job")) == 1
    old_run = job.audit_run_id
    AiJob.objects.filter(pk=job.pk).update(status="running", audit_run_id=uuid.uuid4())
    job.audit_run_id = old_run
    assert not _save_job_success(job)
    _fail(job, "provider_error")
    job.refresh_from_db()
    assert job.status == "running"


def test_stale_recovery_has_distinct_claim_id_without_spending_inference_attempt():
    from finance.ai_jobs import _claim_for_run

    _user, person, _ = make_member("ai")
    moment = timezone.now()
    job = AiJob.objects.create(member=person, feature="structured", status="running", attempts=1,
                               harness_session_id="synthetic-saved-session")
    AiJob.objects.filter(pk=job.pk).update(updated_at=moment - timedelta(hours=1))
    claimed = _claim_for_run(job, moment, moment - timedelta(minutes=10))
    assert claimed is not None and claimed.attempts == 1 and claimed.audit_run_id == job.audit_run_id
    row, = operational_rows(person, "ai_recovery")
    assert row.correlation_id != job.audit_run_id
    assert row.metadata["operation_id"] == str(job.audit_run_id)
    assert _claim_for_run(job, moment, moment - timedelta(minutes=10)) is None
    assert len(operational_rows(person, "ai_recovery")) == 1


def test_ui_triggered_alert_retains_requester_instead_of_impersonating_recipient():
    from finance.alert_services import raise_alert
    from finance.models import Alert

    _user, requester, household = make_member("requester")
    _other_user, recipient, _ = make_member("recipient", household=household)
    with operation(actor_kind="member", source="ui", initiator=requester):
        raise_alert([recipient], Alert.Kind.MONTHLY_REVIEW, CANARY, "/alerts/", "synthetic-audit-alert")
    row, = operational_rows(recipient, "alert_delivery")
    assert row.actor_id == requester.pk and row.effective_member_id == recipient.pk
    assert row.affected_member_id == recipient.pk and row.actor_id != recipient.pk
    assert not operational_rows(requester, "alert_delivery")


def test_delegated_domain_event_keeps_explicit_rule_source_and_run():
    from finance.audit_services import record

    _user, person, _ = make_member("member")
    with operation(actor_kind="operator", source="cli") as run:
        with transaction.atomic():
            record(person, AuditEvent.Action.RULE_APPLIED, AuditEvent.TargetType.RULE, 1,
                   audience={"private_owner": person}, source=AuditEvent.Source.RULE)
    row = events_for(person).get(action="rule_applied")
    assert row.source == "rule" and row.actor_kind == "operator"
    assert row.correlation_id == run.run_id and row.effective_member_id == person.pk


def test_inference_outcome_requires_result_and_start_does_not(harness):
    from finance.ai_services import _inference_event, connect_harness

    _state, url = harness
    _user, person, _ = make_member("member")
    connection = connect_harness(person, base_url=url, token=TOKEN)
    with pytest.raises(ValueError, match="requires a result"):
        _inference_event(person, connection, "local", "structured")
    assert not operational_rows(person, "ai_inference")
    _inference_event(person, connection, "local", "structured", phase="started")
    row, = operational_rows(person, "ai_inference")
    assert row.outcome == "started"
