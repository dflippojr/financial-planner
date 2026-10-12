"""Controlled PostgreSQL races against real account lifecycle operations."""

import threading
from time import monotonic
from unittest.mock import patch

from django.db import connection, connections

from finance import lifecycle_services


WAIT_SECONDS = 10
JOIN_SECONDS = 30


def _wait_for_membership_lock(first_pid, competing_pid, competing_done, errors):
    """Observe a real blocked query, not merely a thread about to execute it."""
    deadline = monotonic() + WAIT_SECONDS
    waiting = None
    with connection.cursor() as cursor:
        while monotonic() < deadline:
            assert not competing_done.is_set(), f"Competing operation finished before blocking: {errors}"
            cursor.execute(
                "SELECT query, wait_event, pg_blocking_pids(pid), "
                "EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
                "AND locktype = 'tuple' AND granted "
                "AND relation = 'finance_membership'::regclass) "
                "FROM pg_stat_activity WHERE pid = %s",
                [competing_pid, competing_pid],
            )
            waiting = cursor.fetchone()
            if waiting is not None and first_pid in waiting[2]:
                assert waiting[3], (
                    f"Competing operation blocked outside the membership lock boundary: {waiting!r}; errors={errors!r}"
                )
                return
            competing_done.wait(0.02)
    raise AssertionError(
        "Competing operation never blocked on the first worker's membership lock; "
        f"last observed row={waiting!r}; errors={errors!r}"
    )


def run_account_lock_race(first_action, competing_action, *, start_competitor_first=False):
    """Hold the first account lock until PostgreSQL proves actual contention.

    Both modes gate the competing service call until the first worker holds
    its locks. Starting the competitor first holds it at that gate while the
    first worker acquires its locks, simulating delayed scheduling without a
    timing sleep. Each worker has its own bounded database session.
    """
    assert connection.vendor == "postgresql" and not connection.in_atomic_block, (
        "run_account_lock_race requires PostgreSQL and django_db(transaction=True)"
    )
    first_lock_held = threading.Event()
    release_first = threading.Event()
    competing_ready = threading.Event()
    allow_competitor = threading.Event()
    competing_done = threading.Event()
    errors = []
    pids = {}
    started = []
    original_lock = lifecycle_services._visible_account_for_update

    def lock_then_pause(principal, account_id):
        locked = original_lock(principal, account_id)
        if threading.current_thread() is first:
            first_lock_held.set()
            if not release_first.wait(timeout=JOIN_SECONDS):
                raise TimeoutError("First worker was never released")
        return locked

    def run(action, role):
        try:
            # Session settings also cover service on_commit callbacks, which
            # may open another transaction after the original one commits.
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '15s'")
                cursor.execute("SET statement_timeout = '20s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[role] = cursor.fetchone()[0]
            if role == "competing":
                competing_ready.set()
                if not allow_competitor.wait(timeout=JOIN_SECONDS):
                    raise TimeoutError("Competing worker was never released")
            action()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            try:
                connections.close_all()
            finally:
                if role == "competing":
                    competing_done.set()

    # Database timeouts and finally cleanup bound normal failures. Daemon
    # threads are an additional safeguard against a broken worker trapping
    # the entire pytest process; an unfinished worker still fails the test.
    first = threading.Thread(target=run, args=(first_action, "first"), daemon=True)
    competing = threading.Thread(target=run, args=(competing_action, "competing"), daemon=True)

    def start(worker):
        worker.start()
        started.append(worker)

    with patch.object(lifecycle_services, "_visible_account_for_update", lock_then_pause):
        try:
            if start_competitor_first:
                start(competing)
                assert competing_ready.wait(timeout=WAIT_SECONDS), f"Competing worker did not connect: {errors}"
            start(first)
            assert first_lock_held.wait(timeout=WAIT_SECONDS), f"First worker did not acquire its locks: {errors}"
            if not start_competitor_first:
                start(competing)
                assert competing_ready.wait(timeout=WAIT_SECONDS), f"Competing worker did not connect: {errors}"
            assert not competing_done.is_set()
            allow_competitor.set()
            _wait_for_membership_lock(pids["first"], pids["competing"], competing_done, errors)
        finally:
            # Keep the patch installed until workers exit, even if observing
            # the lock boundary or either event assertion fails.
            allow_competitor.set()
            release_first.set()
            for worker in started:
                worker.join(timeout=JOIN_SECONDS)

    assert all(not worker.is_alive() for worker in started), "A race worker did not finish"
    assert errors == []
