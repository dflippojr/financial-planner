"""A browser test's own database writes must not overlap a live-server request (#738).

pytest-django hands the connection it finds at start-up to every live-server request thread when
the test database is in-memory SQLite. A browser test's thread does not keep using that
connection: Playwright's sync API runs an asyncio event loop in the test thread, so asgiref's
thread-critical Local, which holds Django's connections, sees a running loop and switches to
context-variable storage. The test thread then opens a second connection to the same shared-cache
in-memory database. Shared-cache connections lock whole tables and fail at once with
SQLITE_LOCKED instead of waiting. A test-thread UPDATE that lands while a request (the Chat page
polling a pending turn) is reading the same table raised "database table is locked:
finance_aiconversationmessage" in test_cleanup_cards_layout_and_partial_apply (CI run
43289501721).

Each test here makes one request hold a read lock on auth_group inside a transaction, as a poll
does for the moment it reads, and writes that table from the test thread meanwhile.
"""

import threading
import urllib.error
import urllib.request

import pytest
from django.contrib.auth.models import Group
from django.db import OperationalError, connections, transaction

import finance.security_services as security_services
from tests.browser_support import browser  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

# How long the request keeps its read lock when nothing releases it sooner.
HOLD_SECONDS = 1


def _status(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - the local live server
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


class _ReadingRequest:
    """One live-server request that reads auth_group inside a transaction and holds it open."""

    def __init__(self, live_server, monkeypatch):
        self.reading = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.statuses = []
        original = security_services.enforce_session_validity

        def read_and_hold(request):
            with transaction.atomic():
                # A shared-cache read lock on auth_group, held until this transaction ends.
                list(Group.objects.values_list("pk", flat=True))
                self.reading.set()
                self.release.wait(HOLD_SECONDS)
            self.finished.set()
            return original(request)

        monkeypatch.setattr(security_services, "enforce_session_validity", read_and_hold)
        self.thread = threading.Thread(target=lambda: self.statuses.append(_status(live_server.url + "/sign-in/")))

    def __enter__(self):
        self.thread.start()
        assert self.reading.wait(timeout=30), "the request never read auth_group"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(timeout=60)


def _shared_connection(live_server):
    shared = live_server.thread.connections_override.get("default")
    if shared is None:
        pytest.skip("only in-memory SQLite shares a connection with the live server")
    return shared


def test_the_test_thread_has_its_own_connection_and_a_plain_write_hits_the_table_lock(
    live_server, browser, monkeypatch,  # noqa: F811
):
    # The cause: with Playwright's loop running, the test thread does not use the live server's
    # connection, and its write fails while a request reads the same table.
    shared = _shared_connection(live_server)
    group = Group.objects.create(name="SYN readers")
    assert connections["default"] is not shared
    with _ReadingRequest(live_server, monkeypatch) as request:
        with pytest.raises(OperationalError, match="database table is locked: auth_group"):
            Group.objects.filter(pk=group.pk).update(name="SYN writers")
    assert request.statuses == [200]


def test_a_write_while_live_requests_are_paused_waits_for_the_request_in_flight(
    live_server, browser, monkeypatch, live_requests_paused,  # noqa: F811
):
    _shared_connection(live_server)
    group = Group.objects.create(name="SYN readers")
    with _ReadingRequest(live_server, monkeypatch) as request:
        with live_requests_paused():
            # The pause returns only once the request has finished and released its lock.
            assert request.finished.is_set()
            assert Group.objects.filter(pk=group.pk).update(name="SYN writers") == 1
    assert request.statuses == [200]
    assert Group.objects.get(pk=group.pk).name == "SYN writers"
