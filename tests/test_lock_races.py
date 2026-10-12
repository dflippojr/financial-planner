"""Failure contracts for the PostgreSQL race helper, without starting workers."""

import re
import threading
from unittest.mock import Mock, patch

import pytest

from django.db import connection

from tests import lock_races


@pytest.mark.parametrize("vendor, in_atomic_block", [("sqlite", False), ("postgresql", True)])
def test_race_rejects_unsupported_connection_before_starting_workers(vendor, in_atomic_block):
    action = Mock()
    with (
        patch.object(lock_races, "connection", vendor=vendor, in_atomic_block=in_atomic_block),
        patch.object(threading.Thread, "start") as start,
        pytest.raises(AssertionError, match=re.escape("PostgreSQL and django_db(transaction=True)")),
    ):
        lock_races.run_account_lock_race(action, action)
    start.assert_not_called()
    action.assert_not_called()


@pytest.mark.django_db
def test_race_rejects_real_non_transactional_test():
    assert connection.in_atomic_block
    with pytest.raises(AssertionError, match=re.escape("django_db(transaction=True)")):
        lock_races.run_account_lock_race(Mock(), Mock())


def test_poll_timeout_reports_last_activity_and_worker_errors():
    row = ("SELECT synthetic", "transactionid", [999], False)
    errors = [RuntimeError("synthetic worker failure")]
    cursor = Mock()
    cursor.fetchone.return_value = row
    with (
        patch.object(lock_races, "connection") as database,
        patch.object(lock_races, "monotonic", side_effect=[0, 0, lock_races.WAIT_SECONDS]),
        pytest.raises(AssertionError) as failure,
    ):
        database.cursor.return_value.__enter__.return_value = cursor
        lock_races._wait_for_membership_lock(1, 2, threading.Event(), errors)
    assert repr(row) in str(failure.value)
    assert repr(errors) in str(failure.value)


def test_membership_subquery_does_not_disguise_account_lock():
    row = (
        'SELECT (SELECT id FROM "finance_membership") FROM "finance_account" WHERE id = 1 FOR UPDATE',
        "transactionid",
        [1],
        False,
    )
    cursor = Mock()
    cursor.fetchone.return_value = row
    with (
        patch.object(lock_races, "connection") as database,
        pytest.raises(AssertionError, match="outside the membership lock boundary"),
    ):
        database.cursor.return_value.__enter__.return_value = cursor
        lock_races._wait_for_membership_lock(1, 2, threading.Event(), [])
