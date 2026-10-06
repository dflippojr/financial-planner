"""The shared process keeps long-running work on independent, supervised lanes."""

import threading
import runpy
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.utils import timezone

from finance import background


def test_all_lanes_start_even_when_batch_job_blocks(monkeypatch):
    started = {name: threading.Event() for name in ("jobs", "daily", "chat")}
    stops = []

    def run(name):
        def wait(stop):
            stops.append(stop)
            started[name].set()
            stop.wait(5)
        return wait

    monkeypatch.setattr(background, "run_ai_jobs", run("jobs"))
    monkeypatch.setattr(background, "run_daily_pass", run("daily"))
    monkeypatch.setattr(background, "ChatLane", lambda: Mock(run_forever=run("chat")))

    def inspect(threads):
        try:
            assert all(event.wait(2) for event in started.values())
            assert all(thread.is_alive() for thread in threads)
        finally:
            for stop in stops:
                stop.set()
            for thread in threads:
                thread.join(2)

    monkeypatch.setattr(background, "supervise", inspect)
    background.main()


def test_invalid_schedule_fails_before_starting_lanes(settings, monkeypatch):
    settings.SIMPLEFIN_SYNC_CRON = "invalid"
    lane = Mock()
    monkeypatch.setattr(background, "ChatLane", lane)
    with pytest.raises(ValueError):
        background.main()
    lane.assert_not_called()


def test_dead_lane_exits_process_even_with_chat_pool_alive(monkeypatch):
    sleep = Mock()
    exit_process = Mock()
    monkeypatch.setattr(background.time, "sleep", sleep)
    monkeypatch.setattr(background.os, "_exit", exit_process)
    background.supervise([Mock(is_alive=Mock(side_effect=[True, False])), Mock(is_alive=lambda: True)])
    sleep.assert_called_once_with(1)
    exit_process.assert_called_once_with(1)


def test_job_failure_does_not_stop_polling_and_closes_connections(monkeypatch):
    stop = Mock(is_set=Mock(side_effect=[False, False, True]))
    jobs = Mock(side_effect=[RuntimeError("synthetic failure"), None])
    monkeypatch.setattr(background, "close_old_connections", Mock())
    close = Mock()
    monkeypatch.setattr(background, "process_due_jobs", jobs)
    monkeypatch.setattr(background.connections, "close_all", close)
    background.run_ai_jobs(stop)
    assert jobs.call_count == close.call_count == stop.wait.call_count == 2


def test_daily_pass_waits_runs_and_advances_schedule(monkeypatch):
    now = timezone.localtime()
    next_due = now + timedelta(days=1)
    stop = Mock(is_set=Mock(side_effect=[False, False, False, True]))
    command = Mock()
    monkeypatch.setattr(background, "close_old_connections", Mock())
    close = Mock()
    schedule = Mock(side_effect=[now, next_due])
    monkeypatch.setattr(background, "next_scheduled_sync", schedule)
    monkeypatch.setattr(background, "seconds_until", Mock(side_effect=[10, 0, 86400]))
    monkeypatch.setattr(background, "call_command", command)
    monkeypatch.setattr(background.connections, "close_all", close)
    background.run_daily_pass(stop)
    command.assert_called_once_with("sync_simplefin")
    close.assert_called_once()
    assert schedule.call_args.args[1] == now
    assert [call.args[0] for call in stop.wait.call_args_list] == [10, 86400]


def test_entrypoint_bootstraps_django_once_then_starts_lanes(monkeypatch):
    setup = Mock()
    main = Mock()
    monkeypatch.setattr("django.setup", setup)
    monkeypatch.setattr(background, "main", main)
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/background_loop.py"), run_name="__main__")
    setup.assert_called_once()
    main.assert_called_once()


def test_daily_failure_propagates_to_supervisor_and_closes_connections(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(background, "close_old_connections", Mock())
    close = Mock()
    monkeypatch.setattr(background, "seconds_until", lambda *_: 0)
    monkeypatch.setattr(background, "call_command", Mock(side_effect=RuntimeError("synthetic failure")))
    monkeypatch.setattr(background.connections, "close_all", close)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        background.run_daily_pass(stop)
    close.assert_called_once()
