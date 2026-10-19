import importlib.util
import io
import json
import subprocess
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import patch

import pytest

from finance import backup_health

SCRIPT = Path(__file__).parents[1] / "scripts" / "prod_signals.py"
spec = importlib.util.spec_from_file_location("prod_signals", SCRIPT)
signals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signals)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

# Synthetic container output: Gunicorn and Django lines around tracebacks whose
# messages stand in for the private detail that must never be reported.
SYNTHETIC_LOG = """\
[2026-10-16 08:00:01 -0400] [7] [INFO] Booting worker with pid: 7
[2026-10-16 08:01:12 -0400] [7] [ERROR] Error handling request /dashboard/
Traceback (most recent call last):
  File "/app/finance/views.py", line 10, in dashboard
    total = summarize(rows)
            ^^^^^^^^^^^^^^^
ValueError: synthetic private detail 123.45
Traceback (most recent call last):
  File "/app/finance/services.py", line 20, in sync
    cursor.execute(query)
psycopg.OperationalError: synthetic connection detail

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/finance/services.py", line 22, in sync
    raise SyncFailed() from None
finance.simplefin_services.SyncFailed
GET /health/ 200
"""


def write(tmp_path, case, text):
    """A status file in its own folder; the reader opens only files named status."""
    folder = tmp_path / case
    folder.mkdir()
    path = folder / "status"
    path.write_text(text, encoding="utf-8")
    return path


def test_counts_tracebacks_by_exception_type_only():
    result = signals.count_tracebacks(SYNTHETIC_LOG.splitlines())

    assert result["tracebacks"] == 3
    assert result["types"] == {
        "ValueError": 1,
        "finance.simplefin_services.SyncFailed": 1,
        "psycopg.OperationalError": 1,
    }
    assert "synthetic" not in json.dumps(result)


def test_a_log_with_no_tracebacks_counts_zero():
    lines = ["[INFO] Booting worker", "ERROR: a plain log line, not a traceback"]

    assert signals.count_tracebacks(lines) == {"tracebacks": 0, "types": {}}


def test_cut_off_tracebacks_count_as_unknown():
    lines = [
        "Traceback (most recent call last):",
        '  File "/app/a.py", line 1, in f',
        "Traceback (most recent call last):",
        '  File "/app/b.py", line 2, in g',
        "[2026-10-16 08:00:01] [ERROR] interleaved line where the exception should be",
        "Traceback (most recent call last):",
        '  File "/app/c.py", line 3, in h',
    ]

    assert signals.count_tracebacks(lines) == {"tracebacks": 3, "types": {"unknown": 3}}


def test_exception_groups_count_the_group_and_each_member():
    lines = [
        "  + Exception Group Traceback (most recent call last):",
        '  |   File "/app/a.py", line 1, in f',
        "  | ExceptionGroup: synthetic detail (2 sub-exceptions)",
        "  +-+---------------- 1 ----------------",
        "    | Traceback (most recent call last):",
        '    |   File "/app/a.py", line 2, in g',
        "    | KeyError: 'synthetic'",
        "    +---------------- 2 ----------------",
        "    | TimeoutError",
        "    +------------------------------------",
    ]

    assert signals.count_tracebacks(lines) == {
        "tracebacks": 2,
        "types": {"ExceptionGroup": 1, "KeyError": 1},
    }


def test_an_implausibly_long_type_is_reported_as_unknown():
    lines = ["Traceback (most recent call last):", "A" * 200 + ": detail"]

    assert signals.count_tracebacks(lines)["types"] == {"unknown": 1}


def test_tracebacks_command_reads_standard_input(monkeypatch, capsys):
    monkeypatch.setattr(signals.sys, "stdin", io.StringIO(SYNTHETIC_LOG))

    assert signals.main(["tracebacks", "--stdin"]) == 0
    assert json.loads(capsys.readouterr().out)["types"]["ValueError"] == 1


def test_tracebacks_command_reads_docker_logs_with_both_streams(capsys):
    completed = subprocess.CompletedProcess([], 0, stdout=SYNTHETIC_LOG.encode())
    with patch.object(signals.subprocess, "run", return_value=completed) as run:
        assert signals.main(["tracebacks", "--container", "fp-app-1", "--since", "2026-10-16T12:00:00Z"]) == 0

    assert run.call_args.args[0] == ["docker", "logs", "--since", "2026-10-16T12:00:00Z", "--", "fp-app-1"]
    assert run.call_args.kwargs["stderr"] == subprocess.STDOUT
    assert json.loads(capsys.readouterr().out)["tracebacks"] == 3


def test_a_docker_logs_failure_reports_no_docker_output(capsys):
    failed = subprocess.CompletedProcess([], 1, stdout=b"Error: synthetic detail")
    with patch.object(signals.subprocess, "run", return_value=failed):
        assert signals.main(["tracebacks", "--container", "missing"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "cannot read logs: docker logs exited 1"


def test_a_docker_logs_timeout_exits_2(capsys):
    timeout = subprocess.TimeoutExpired(["docker"], 120)
    with patch.object(signals.subprocess, "run", side_effect=timeout):
        assert signals.main(["tracebacks", "--container", "fp-app-1"]) == 2

    assert capsys.readouterr().err.strip() == "cannot read logs: TimeoutExpired"


@pytest.mark.parametrize(
    ("container", "since"),
    [("--help", "24h"), ("fp app", "24h"), ("fp-app-1", "1d"), ("fp-app-1", "--follow"), ("fp-app-1", "2026-10-16")],
)
def test_only_a_plain_container_and_time_reach_docker(capsys, container, since):
    with patch.object(signals.subprocess, "run") as run:
        assert signals.main(["tracebacks", f"--container={container}", f"--since={since}"]) == 2

    run.assert_not_called()
    assert capsys.readouterr().err.startswith("cannot read logs: give a container id")


def status_text(**overrides):
    values = {
        "last_success_at": "2026-10-16T07:30:05Z",
        "dump_name": "financial_planner_20261016T073000Z.dump",
        "size_bytes": "1048576",
        "table_count": "70",
        "last_error": "",
        "offsite_success_at": "",
        "offsite_error": "",
        "offsite_configured": "0",
        "restore_check_at": "2026-10-12T07:31:00Z",
        "restore_check_error": "",
        "restore_check_interval_days": "7",
    }
    values.update(overrides)
    return "".join(f"{key}={value}\n" for key, value in values.items())


def test_a_healthy_status_file_is_ok(tmp_path):
    path = write(tmp_path, "status", status_text())

    assert signals.backup_state(path, now=NOW) == {
        "state": "ok",
        "errors": [],
        "stale": [],
        "last_success_at": "2026-10-16T07:30:05Z",
        "restore_check_at": "2026-10-12T07:31:00Z",
        "offsite_configured": False,
    }


@pytest.mark.parametrize("key", ["last_error", "offsite_error", "restore_check_error"])
def test_any_recorded_error_is_an_error_state_without_its_text(tmp_path, key):
    path = write(tmp_path, "status", status_text(**{key: "pg_dump failed: synthetic detail"}))

    result = signals.backup_state(path, now=NOW)

    assert result["state"] == "error"
    assert result["errors"] == [key]
    assert "synthetic" not in json.dumps(result)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"last_success_at": "2026-10-15T09:00:00Z"}, "last_success"),
        ({"last_success_at": ""}, "last_success"),
        ({"offsite_configured": "1", "offsite_success_at": ""}, "offsite"),
        ({"restore_check_at": "2026-10-07T11:00:00Z"}, "restore_check"),
    ],
)
def test_old_successes_are_stale(tmp_path, overrides, reason):
    path = write(tmp_path, "status", status_text(**overrides))

    result = signals.backup_state(path, now=NOW)

    assert result["state"] == "stale"
    assert result["stale"] == [reason]


def test_a_restore_check_turned_off_or_never_run_is_not_stale(tmp_path):
    off = write(tmp_path, "off", status_text(restore_check_interval_days="0", restore_check_at="2026-01-01T00:00:00Z"))
    never = write(tmp_path, "never", status_text(restore_check_at=""))

    assert signals.backup_state(off, now=NOW)["state"] == "ok"
    assert signals.backup_state(never, now=NOW)["state"] == "ok"


def test_a_missing_status_file_is_missing(tmp_path):
    assert signals.backup_state(tmp_path / "status", now=NOW) == {"state": "missing"}


def test_naive_and_offset_timestamps_parse(tmp_path):
    naive = write(tmp_path, "naive", status_text(last_success_at="2026-10-16T07:30:05"))
    offset = write(tmp_path, "offset", status_text(last_success_at="2026-10-16T03:30:05-04:00"))
    garbage = write(tmp_path, "garbage", status_text(last_success_at="last night"))

    assert signals.backup_state(naive, now=NOW)["state"] == "ok"
    assert signals.backup_state(offset, now=NOW)["state"] == "ok"
    assert signals.backup_state(garbage, now=NOW)["stale"] == ["last_success"]


def test_backup_status_reads_only_a_file_named_status(tmp_path, capsys):
    other = tmp_path / "production.env"
    other.write_text("last_error=synthetic\n", encoding="utf-8")

    assert signals.main(["backup-status", str(other)]) == 2
    assert "BACKUP_DIR/health/status" in capsys.readouterr().err


def test_backup_status_command_prints_json(tmp_path, capsys):
    path = write(tmp_path, "status", status_text(last_error="synthetic detail"))

    assert signals.main(["backup-status", str(path), "--now", "2026-10-16T12:00:00Z"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "error"


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"last_error": "synthetic"},
        {"offsite_error": "synthetic"},
        {"restore_check_error": "synthetic"},
        {"last_success_at": "2026-10-15T09:00:00Z"},
        {"last_success_at": ""},
        {"offsite_configured": "1", "offsite_success_at": ""},
        {"offsite_configured": "1", "offsite_success_at": "2026-10-16T07:40:00Z"},
        {"restore_check_at": "2026-10-07T11:00:00Z"},
        {"restore_check_at": "2026-10-07T11:00:00Z", "restore_check_interval_days": "0"},
        {"restore_check_at": "", "restore_check_interval_days": ""},
    ],
)
def test_flags_a_status_file_exactly_when_the_in_app_alert_does(tmp_path, overrides):
    path = write(tmp_path, "status", status_text(**overrides))

    flagged = signals.backup_state(path, now=NOW)["state"] != "ok"

    assert flagged == backup_health.backup_is_unhealthy(backup_health.read_backup_status(path), now=NOW)


def test_a_missing_file_is_flagged_by_both(tmp_path):
    path = tmp_path / "status"

    assert signals.backup_state(path, now=NOW)["state"] == "missing"
    assert backup_health.backup_is_unhealthy(backup_health.read_backup_status(path), now=NOW)


def test_reads_the_keys_the_app_reads():
    # Both parse the file ops/backup/backup.sh writes; a key added to one must reach the other.
    assert signals.STATUS_KEYS == backup_health.STATUS_KEYS
    assert signals.STALE_AFTER == backup_health.STALE_AFTER
    assert signals.RESTORE_CHECK_GRACE == backup_health.RESTORE_CHECK_GRACE
