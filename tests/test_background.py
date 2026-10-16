"""The shared process keeps long-running work on independent, supervised lanes."""

import io
import logging
import threading
import runpy
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.utils import timezone

from finance import background
from finance.log_redaction import RedactingFormatter


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
    # main() replaces the process-wide hook; put pytest's back afterwards.
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)

    def inspect(threads):
        try:
            assert threading.excepthook is background.log_uncaught_lane_error
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


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("30 6 * * MON-FRI", "numbers only"),
        ("0 2 * * sun", "numbers only"),
        ("30 6 * JAN *", "numbers only"),
        ("@daily x x x x", "numbers only"),
        ("60 6 * * *", "minute outside 0-59"),
        ("30 24 * * *", "hour outside 0-23"),
        ("30 6 0 * *", "day of month outside 1-31"),
        ("30 6 * 13 *", "month outside 1-12"),
        ("30 6 * * 8", "day of week outside 0-7"),
        ("*/0 6 * * *", "minute step below 1"),
        ("30 6-4 * * *", "hour range that runs backwards"),
        ("30 6,,7 * * *", "invalid hour field"),
        ("30 6 31 2 *", "does not match a time"),
        ("30 6 * *", "five-field"),
    ],
)
def test_unusable_schedule_fails_with_a_clear_message_before_starting_lanes(expression, message, settings, monkeypatch):
    settings.SIMPLEFIN_SYNC_CRON = expression
    lane = Mock()
    monkeypatch.setattr(background, "ChatLane", lane)
    with pytest.raises(ValueError, match=message):
        background.main()
    lane.assert_not_called()


@pytest.mark.parametrize("expression", ["30 6 * * *", "30 6 * * 1-5", "*/15 * * * *", "0 6-18/6 1,15 * 0,7", "5/20 3 * * *"])
def test_valid_schedule_starts_the_lanes(expression, settings, monkeypatch):
    settings.SIMPLEFIN_SYNC_CRON = expression
    started = Mock()
    monkeypatch.setattr(background, "ChatLane", Mock())
    monkeypatch.setattr(background.threading.Thread, "start", started)
    monkeypatch.setattr(background, "supervise", Mock())
    # main() replaces the process-wide hook; put pytest's back afterwards.
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    background.main()
    assert started.call_count == 3


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
    lane = Mock(tick=jobs)
    monkeypatch.setattr(background, "close_old_connections", Mock())
    close = Mock()
    monkeypatch.setattr(background, "AiJobLane", lambda: lane)
    monkeypatch.setattr(background.connections, "close_all", close)
    background.run_ai_jobs(stop)
    assert jobs.call_count == close.call_count == stop.wait.call_count == 2
    lane.close.assert_called_once()


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


def test_an_uncaught_lane_exception_is_logged_with_secrets_redacted(monkeypatch):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(RedactingFormatter("{levelname} {name}: {message}", style="{"))
    monkeypatch.setattr(background.logger, "handlers", [handler])
    monkeypatch.setattr(background.logger, "propagate", False)
    monkeypatch.setattr(threading, "excepthook", background.log_uncaught_lane_error)

    def crash():
        raise RuntimeError("x-api-key: sk-ant-api03-SYNTHETIC0123456789")

    thread = threading.Thread(target=crash, name="synthetic-lane")
    thread.start()
    thread.join(2)

    logged = output.getvalue()
    assert "Background lane synthetic-lane raised an uncaught exception" in logged
    assert "RuntimeError" in logged
    assert "SYNTHETIC0123456789" not in logged


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
