"""Plan status reads reconcile only genuine link transitions (synthetic harness)."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.test import RequestFactory
from django.utils import timezone

from finance.ai_views import ai_plan_status
from finance.models import AiPlanLink, MemberSecurityEvent
from finance.policy_services import current_policy
from finance.security_services import EVENT_TYPES, record_security_event
from tests.test_chat import make_member


def _setup():
    user, person, household = make_member("poll-member", policy=current_policy())
    _host_user, host, _ = make_member("poll-host", household=household, policy=current_policy())
    from finance.ai_services import connect_harness

    selected = connect_harness(person, base_url="http://synthetic.invalid", token="synthetic-token")
    previous = connect_harness(host, base_url="http://previous.invalid", token="synthetic-token")
    return user, person, selected, previous


def _status(user, backend):
    request = RequestFactory().get("/synthetic-status/", REMOTE_ADDR="203.0.113.12")
    request.user = user
    response = ai_plan_status(request, backend)
    return response.status_code, json.loads(response.content)


def _events(person):
    return MemberSecurityEvent.objects.filter(member=person, event_type=EVENT_TYPES.AI_CONNECTION_CHANGED)


@pytest.mark.django_db
@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("initial", ["absent", "linked", "needs_login", "different_connection"])
def test_repeated_success_preserves_transition_timestamp_and_one_event(backend, initial):
    user, person, selected, previous = _setup()
    first = timezone.now() - timedelta(days=2)
    if initial != "absent":
        AiPlanLink.objects.create(
            person=person, backend=backend, linked_at=first,
            connection=previous if initial == "different_connection" else selected,
            needs_login=initial == "needs_login",
        )
    with patch("finance.ai_plan.end_user_login_state", return_value={"linked": True}):
        for offset in range(3):
            with patch("finance.ai_plan.timezone.now", return_value=first + timedelta(days=1, seconds=offset)):
                assert _status(user, backend) == (200, {"ok": True, "linked": True, "failed": False})
    link = AiPlanLink.objects.get(person=person, backend=backend)
    assert link.linked_at == (first if initial == "linked" else first + timedelta(days=1))
    assert link.connection_id == selected.pk and not link.needs_login
    assert _events(person).count() == (0 if initial == "linked" else 1)
    if initial != "linked":
        assert _events(person).get().ip_address == "203.0.113.12"
    assert not _events(previous.owner).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("initial", ["absent", "needs_login", "different_connection"])
def test_concurrent_success_records_one_transition(backend, initial):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock concurrency requires disposable PostgreSQL")
    user, person, selected, previous = _setup()
    if initial != "absent":
        AiPlanLink.objects.create(
            person=person, backend=backend,
            connection=previous if initial == "different_connection" else selected,
            needs_login=initial == "needs_login",
        )
    ready = Barrier(2)

    def harness_response(*args):
        ready.wait(timeout=15)
        return {"linked": True}

    def poll():
        close_old_connections()
        try:
            return _status(user, backend)
        finally:
            close_old_connections()

    with patch("finance.ai_plan.end_user_login_state", side_effect=harness_response):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: poll(), range(2)))
    assert results == [(200, {"ok": True, "linked": True, "failed": False})] * 2
    assert AiPlanLink.objects.filter(person=person, backend=backend).count() == 1
    assert _events(person).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("initial", ["absent", "needs_login", "different_connection"])
def test_link_and_event_roll_back_together(initial):
    user, person, selected, previous = _setup()
    if initial != "absent":
        AiPlanLink.objects.create(
            person=person, backend="claude",
            connection=previous if initial == "different_connection" else selected,
            needs_login=initial == "needs_login",
        )
    before = list(AiPlanLink.objects.values())

    def fail_after_event(*args, **kwargs):
        record_security_event(*args, **kwargs)
        raise RuntimeError("synthetic event failure")

    with patch("finance.ai_plan.end_user_login_state", return_value={"linked": True}):
        with patch("finance.ai_plan.record_security_event", side_effect=fail_after_event):
            with pytest.raises(RuntimeError, match="synthetic event failure"):
                _status(user, "claude")
    assert list(AiPlanLink.objects.values()) == before
    assert not _events(person).exists()
