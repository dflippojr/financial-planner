import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, models, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.audit_services import append_event, events_for, purge_old_events, GAP_WARNING
from finance.lifecycle_services import (
    archive_account, change_account_share_mode, delete_account, delete_member_data,
    leave_household, share_account, unshare_account,
)
from finance.models import Account, AuditEvent, Membership
from tests.test_auth_flows import make_member

pytestmark = pytest.mark.django_db


def account_for(person, household=None):
    return Account.objects.create(
        owner=person, name="Synthetic secret account", account_type="checking", currency="USD",
        scope="household" if household else "private", household=household,
        share_mode="co_owned" if household else "",
    )


def append(account, actor, **kwargs):
    with transaction.atomic():
        return append_event(account=account, actor=actor, action=AuditEvent.Action.ACCOUNT_ARCHIVED,
                            changed_fields=("status",), **kwargs)


def shared_members():
    user, person, household = make_member("owner")
    other_user, other, _ = make_member("borrower")
    Membership.objects.filter(person=other).update(ended_at=timezone.now())
    Membership.objects.create(person=other, household=household)
    return user, person, household, other_user, other


def test_current_account_access_drives_rows_counts_filters_and_cli():
    user, person, household, other_user, other = shared_members()
    _outsider_user, outsider, _ = make_member("outsider")
    private = account_for(person)
    shared = account_for(person, household)
    append(private, person)
    event = append(shared, other)
    assert list(events_for(other).values_list("pk", flat=True)) == [event.pk]
    assert events_for(outsider).count() == 0
    assert events_for(None).count() == 0
    assert events_for(other, actor=str(person.pk)).count() == 0
    assert events_for(other, action="unknown").count() == 0
    assert events_for(other, source="unknown").count() == 0
    assert events_for(other, actor="unknown").count() == 0
    assert events_for(other, source="ui", actor=str(other.pk), date_from=timezone.now().date(),
                      date_to=timezone.now().date()).count() == 1
    out = StringIO()
    call_command("query_audit", member=other.pk, stdout=out)
    data = json.loads(out.getvalue())
    assert data["count"] == 1
    assert data["events"][0]["checksum_valid"] is True
    assert "Synthetic secret account" not in out.getvalue()
    unshare_account(person, shared.pk)
    assert events_for(other).count() == 0  # actor does not retain private access
    assert events_for(person).count() == 3
    call_command("query_audit", member=other.pk, stdout=StringIO())
    client = Client()
    client.force_login(other_user)
    response = client.get(reverse("settings-audit"))
    assert response.context["page"].paginator.count == 0
    assert str(event.pk) not in response.content.decode()
    assert "no-store" in response["Cache-Control"]
    assert client.post(reverse("settings-audit")).status_code == 405
    assert Client().get(reverse("settings-audit")).status_code == 302


def test_archived_former_member_and_lent_account_access():
    _user, person, household, _other_user, other = shared_members()
    shared = account_for(person, household)
    archive_account(person, shared.pk)
    assert events_for(other).count() == 1
    shared.share_mode = "lent"
    shared.save(update_fields=("share_mode",))
    leave_household(person)
    assert events_for(other).count() == 0
    assert events_for(person).count() == 1


def test_co_owned_exit_and_deleted_shared_actor_revoke_former_member():
    _user, person, household, _other_user, other = shared_members()
    shared = account_for(person, household)
    append(shared, person)
    leave_household(person)
    assert events_for(person).count() == 0
    assert events_for(other).count() == 1
    delete_member_data(person)
    assert events_for(other).get().actor_id is None


def test_deleted_shared_event_is_anonymized_while_household_remains():
    _user, person, household, _other_user, other = shared_members()
    shared = account_for(person, household)
    delete_account(person, shared.pk)
    delete_member_data(person)
    event = events_for(other).get()
    assert event.action == "account_deleted"
    assert event.actor_id is None


def test_deleted_shared_target_keeps_only_deletion_for_current_household():
    _user, person, household, _other_user, other = shared_members()
    account = account_for(person, household)
    old_id = account.pk
    append(account, person)
    delete_account(person, account.pk)
    event = AuditEvent.objects.get()
    assert event.action == "account_deleted"
    assert event.account_id is None
    assert event.target_id == old_id
    assert event.changed_fields == []
    assert events_for(other).count() == 1
    leave_household(other)
    assert events_for(other).count() == 0
    delete_member_data(person)
    assert AuditEvent.objects.count() == 0  # no audience after last household removed


def test_member_deletion_removes_private_and_anonymizes_shared_references():
    _user, person, household, _other_user, other = shared_members()
    append(account_for(person), person)
    append(account_for(person, household), person, effective_member=person)
    delete_member_data(person)
    event = AuditEvent.objects.get()
    assert event.actor_id is None
    assert event.effective_member_id is None
    assert event.checksum == event.calculated_checksum()
    assert events_for(other).count() == 1


def test_private_deletion_metadata_is_not_visible_to_household_and_goes_with_member():
    _user, person, _household, _other_user, other = shared_members()
    account = account_for(person)
    delete_account(person, account.pk)
    assert events_for(person).count() == 1
    assert events_for(other).count() == 0
    delete_member_data(person)
    assert AuditEvent.objects.count() == 0


def test_atomic_rollback_no_op_and_hidden_target_failures():
    _user, person, household = make_member()
    account = account_for(person)
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            share_account(person, account.pk, "co_owned")
            raise RuntimeError("Synthetic rollback")
    account.refresh_from_db()
    assert account.scope == "private"
    assert AuditEvent.objects.count() == 0
    share_account(person, account.pk, "co_owned")
    change_account_share_mode(person, account.pk, "co_owned")
    assert AuditEvent.objects.count() == 1
    change_account_share_mode(person, account.pk, "lent")
    archive_account(person, account.pk)
    archive_account(person, account.pk)
    assert AuditEvent.objects.count() == 3
    _other_user, outsider, _h = make_member("outsider")
    for pk in (account.pk, 987654):
        with pytest.raises(PermissionDenied, match="^Operation is not permitted.$"):
            delete_account(outsider, pk)
    assert AuditEvent.objects.count() == 3


@pytest.mark.parametrize("canary", [
    "password-SYNTHETIC", "recovery-code-SYNTHETIC", "token-SYNTHETIC",
    "https://user:secret@example.invalid", "Private merchant description", "123.45",
    "Private note", "synthetic@example.invalid", "date,amount,raw CSV", "<OFX>raw</OFX>",
    "receipt-private.png", "Private AI text",
])
def test_sensitive_metadata_is_rejected_without_echo(canary, caplog):
    _user, person, _household = make_member()
    account = account_for(person)
    with pytest.raises(ValidationError) as error:
        with transaction.atomic():
            append_event(account=account, actor=person, action="account_archived", changed_fields=(canary,))
    assert canary not in str(error.value)
    assert canary not in caplog.text
    assert AuditEvent.objects.count() == 0
    with pytest.raises(TypeError):
        append_event(account=account, actor=person, action="account_archived", payload={"secret": canary})


def test_append_only_validation_and_typed_metadata():
    _user, person, _household = make_member()
    account = account_for(person)
    event = append(account, person)
    for operation in (event.save, event.delete, lambda: AuditEvent.objects.all().delete(),
                      lambda: AuditEvent.objects.all().update(source="cli"),
                      lambda: AuditEvent.objects.bulk_create([event])):
        with pytest.raises(ValidationError):
            operation()
    for kwargs in ({"source": "https://secret.invalid"}, {"outcome": "secret"},
                   {"correlation_id": "secret"}, {"actor_kind": "operator"}):
        with pytest.raises((ValidationError, PermissionDenied)):
            append(account, person, **kwargs)
    assert AuditEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_requires_transaction_and_scheduler_identity():
    _user, person, _household = make_member()
    account = account_for(person)
    with pytest.raises(RuntimeError, match="action transaction"):
        append_event(account=account, actor=person, action="account_archived")
    with transaction.atomic():
        event = append_event(account=account, action="account_archived", actor_kind="scheduler",
                             effective_member=person, source="job")
    assert event.actor_id is None
    assert event.effective_member_id == person.pk


def test_audit_write_failure_preserves_action_and_reports_redacted_gap(caplog):
    user, person, household = make_member()
    account = account_for(person)
    client = Client()
    client.force_login(user)
    from tests.helpers import stamp_recent_auth
    stamp_recent_auth(client)
    with patch.object(AuditEvent, "save", side_effect=DatabaseError("token-SYNTHETIC")):
        # Exercise middleware through the actual share UI.
        response = client.post(reverse("account-share", args=(account.pk,)), {"share_mode": "co_owned"}, follow=True)
    account.refresh_from_db()
    assert account.scope == "household"
    assert AuditEvent.objects.count() == 0
    assert GAP_WARNING in response.content.decode()
    assert "Audit write gap" in caplog.text
    assert "token-SYNTHETIC" not in caplog.text


def test_database_error_rolls_back_only_audit_savepoint(caplog):
    from django.db import connection

    _user, person, household = make_member()
    account = account_for(person)
    original_save = AuditEvent.save

    def failing_save(event, *args, **kwargs):
        original_save(event, *args, **kwargs)
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM synthetic_missing_audit_table")

    with patch.object(AuditEvent, "save", failing_save):
        share_account(person, account.pk, "co_owned")
    account.refresh_from_db()
    assert account.scope == "household"
    assert AuditEvent.objects.count() == 0
    assert "Audit write gap" in caplog.text
    assert "synthetic_missing_audit_table" not in caplog.text


def test_reader_omits_arbitrary_query_canaries_and_request_actor_headers():
    user, person, household = make_member()
    account = account_for(person)
    client = Client()
    client.force_login(user)
    from tests.helpers import stamp_recent_auth
    stamp_recent_auth(client)
    response = client.post(reverse("account-share", args=(account.pk,)),
                           {"share_mode": "co_owned", "actor": "forged", "source": "forged"},
                           HTTP_X_ACTOR="forged")
    assert response.status_code == 302
    event = AuditEvent.objects.get()
    assert event.actor_id == person.pk and event.source == "ui"
    canary = "token-SYNTHETIC@example.invalid"
    for query in ({"actor": canary}, {"date_from": canary}, {"q": canary},
                  {"actor": "999999999999999999999999999999"}, {"date_to": "9999-12-31"}):
        response = client.get(reverse("settings-audit"), query)
        assert response.status_code == 200
        assert canary not in response.content.decode()


def test_pagination_ordering_filters_and_read_only_cli():
    user, person, household = make_member()
    account = account_for(person)
    for _ in range(51):
        append(account, person)
    stamp = timezone.now()
    models.QuerySet(model=AuditEvent).update(occurred_at=stamp)
    expected = list(events_for(person).values_list("pk", flat=True))
    client = Client()
    client.force_login(user)
    page = client.get(reverse("settings-audit"), {"page": 2})
    assert [row.pk for row in page.context["page"]] == expected[50:]
    for query in ({"actor": "invalid"}, {"date_from": "invalid"}):
        assert client.get(reverse("settings-audit"), query).context["page"].paginator.count == 0
    out = StringIO()
    call_command("query_audit", member=person.pk, page=2, stdout=out)
    assert len(json.loads(out.getvalue())["events"]) == 1
    with pytest.raises(CommandError):
        call_command("query_audit", member=987654)
    with pytest.raises(CommandError):
        call_command("query_audit", member=person.pk, source="invalid")


@override_settings(AUDIT_RETENTION_DAYS=10, AUDIT_PURGE_BATCH_SIZE=2)
def test_retention_is_bounded_configurable_and_exact_at_boundary():
    _user, person, _household = make_member()
    account = account_for(person)
    old = [append(account, person).pk for _ in range(3)]
    boundary = append(account, person)
    now = timezone.now()
    models.QuerySet(model=AuditEvent).filter(pk__in=old).update(occurred_at=now - timedelta(days=11))
    models.QuerySet(model=AuditEvent).filter(pk=boundary.pk).update(occurred_at=now - timedelta(days=10))
    assert purge_old_events(now=now) == 2
    assert purge_old_events(now=now) == 1
    assert purge_old_events(now=now) == 0
    assert AuditEvent.objects.get().pk == boundary.pk
