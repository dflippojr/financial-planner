"""The live server must not run two requests at once on one shared database connection (#653).

With the default in-memory SQLite test database, pytest-django hands the test thread's connection
to every live-server request thread. Two requests that overlap inside atomic() then corrupt that
one connection's transaction bookkeeping. Locally at -n 4, the Chat page's two pending-turn polls
in test_the_provider_switch_warning_shows_before_the_new_provider_reads ran recover_stale_turns
together and Atomic.__exit__ raised IndexError: pop from empty list. Django never closes an
in-memory connection, so every later live-server test on that xdist worker failed with
TransactionManagementError, as in CI run 42033147346.

Marked browser so the Playwright job, the one that runs live-server tests on in-memory SQLite,
selects it.
"""

import threading
import urllib.error
import urllib.request

import pytest
from django.db import connections, transaction

import finance.security_services as security_services

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.browser]

REQUESTS = 2
# Long enough for every request to reach the barrier when the server runs them together.
OVERLAP_WAIT_SECONDS = 2


def _status(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - the local live server
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_requests_sharing_one_connection_never_overlap_inside_atomic(live_server, monkeypatch):
    shared = live_server.thread.connections_override.get("default")
    mine = connections["default"]
    # pytest-django shares the connection exactly when the database is in-memory SQLite.
    assert (shared is not None) == (mine.vendor == "sqlite" and mine.is_in_memory_db())
    original = security_services.enforce_session_validity
    barrier = threading.Barrier(REQUESTS)
    lock = threading.Lock()
    inside = {"now": 0, "most": 0}

    def held_open(request):
        # Each request opens a transaction and waits for the other one inside it.
        with transaction.atomic():
            with lock:
                inside["now"] += 1
                inside["most"] = max(inside["most"], inside["now"])
            try:
                barrier.wait(timeout=OVERLAP_WAIT_SECONDS)
            except threading.BrokenBarrierError:
                pass
            finally:
                with lock:
                    inside["now"] -= 1
            original(request)

    monkeypatch.setattr(security_services, "enforce_session_validity", held_open)
    statuses = []
    threads = [
        threading.Thread(target=lambda: statuses.append(_status(live_server.url + "/sign-in/")))
        for _ in range(REQUESTS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    monkeypatch.setattr(security_services, "enforce_session_validity", original)

    assert statuses == [200] * REQUESTS
    if shared is not None:
        assert inside["most"] == 1, "two live-server requests ran inside atomic() on one shared connection"
        assert (shared.in_atomic_block, shared.needs_rollback, shared.atomic_blocks, shared.savepoint_ids) == (
            False, False, [], [],
        )
    # The next request still reads the database.
    assert _status(live_server.url + "/sign-in/") == 200
    assert not connections["default"].needs_rollback
