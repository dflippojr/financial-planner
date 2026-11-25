"""The Linux checker runs directly: no shell, daemon, socket or optional runtime."""
import copy
import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, UTC
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("post_deploy", ROOT / "ops/post-deploy-check.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
SHA = "abc1234" + "0" * 33
WRONG = "def1234" + "0" * 33
NOW = datetime.now(UTC)
DEPLOY = NOW - timedelta(seconds=30)
ZERO = "0001-01-01T00:00:00Z"


def docker_time(value):
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    state = {}
    for service in check.SERVICES + check.ONE_SHOT_SERVICES:
        created = NOW - timedelta(days=1) if service == "db" else DEPLOY
        state[service] = {
            "Name": f"/{service}", "Created": docker_time(created), "RestartCount": 0,
            "Config": {"Labels": {check.REVISION_LABEL: SHA}},
            "State": {"Status": "exited" if service == "config-check" else "running",
                      "StartedAt": docker_time(created), "FinishedAt": ZERO},
        }
    status = tmp_path / "health/status"
    status.parent.mkdir()
    status.write_text(f"last_success_at={docker_time(NOW)}\n", encoding="utf-8")
    events = []
    for service in ("app", "backup", "background"):
        ns = check.unix_nanoseconds(state[service]["Created"])
        events += [f"{ns} create {service}-id", f"{ns} start {service}-id"]
    event_log = tmp_path / "events.log"
    event_log.write_text("\n".join(events), encoding="utf-8")
    args = check.parser().parse_args([
        "--project-name", "fp-post-deploy-test", "--health-url", "http://127.0.0.1:9999/health/",
        "--status-path", str(status), "--expected-sha", SHA, "--stable-seconds", "0",
        "--event-log", str(event_log),
    ])
    calls = []

    def native(*argv):
        calls.append(argv)
        if argv[0] == "git":
            return SHA + "\n"
        if argv[1] == "ps":
            service = next(a.split("service=")[1] for a in argv if "compose.service=" in a)
            assert "label=com.docker.compose.project=fp-post-deploy-test" in argv
            assert "label=com.docker.compose.oneoff=False" in argv
            return f"{service}-id\n" if service in state else ""
        if argv[1] == "inspect":
            return json.dumps([state[argv[2].removesuffix("-id")]])
        if argv[1] == "events":
            return "\n".join(events)
        raise AssertionError(argv)

    response = Mock()
    response.code = 200
    response.headers = {"X-App-Revision": SHA[:12]}
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(check, "native", native)
    monkeypatch.setattr(check, "build_opener", lambda *a: opener)
    monkeypatch.setattr(check.signals, "docker_log_lines", lambda *a: [])
    return SimpleNamespace(checker=check.Checker(args), state=state, events=events, log=event_log,
                           calls=calls, response=response, opener=opener, status=status, args=args)


def test_clean_deploy_runs_every_signal_and_returns_zero(rig, capsys):
    assert rig.checker.run() == 0
    output = capsys.readouterr().out
    for service in check.SERVICES:
        assert f"ok   containers: {service} running, RestartCount 0" in output
        assert f"ok   tracebacks: {service} has none since" in output
    assert "ok   backup: status ok" in output
    assert "ok   revision: /health/ serves" in output
    assert "post-deploy check passed" in output
    assert "FAIL" not in output
    request = rig.opener.open.call_args.args[0]
    assert request.get_header("X-forwarded-proto") == "https"
    assert rig.opener.open.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("value", ["", "   ", "main", "abc", "g" * 40])
def test_invalid_explicit_sha_stops_before_io(rig, value, capsys):
    rig.args.expected_sha = value
    assert rig.checker.run() == 1
    assert not rig.calls
    rig.opener.open.assert_not_called()
    assert "FAIL revision: --expected-sha must be 7 to 40 hex digits" in capsys.readouterr().out


@pytest.mark.parametrize("length", [7, 12, 40])
def test_short_and_full_sha_compare_health_and_every_built_service(rig, length):
    snapshot = rig.checker.snapshot()
    rig.args.expected_sha = SHA[:length]
    rig.checker.revision(SHA[:12], snapshot)
    assert not rig.checker.failures
    rig.args.expected_sha = WRONG[:length]
    rig.checker.revision(SHA[:12], snapshot)
    assert len(rig.checker.failures) == 5
    assert any("config-check runs an image" in f for f in rig.checker.failures)


@pytest.mark.parametrize("served,diagnostic", [
    (None, "no X-App-Revision"), ("unknown", "no GIT_SHA"),
    ("abc12", "invalid X-App-Revision"), ("synthetic-private-data", "invalid X-App-Revision"),
])
def test_missing_or_invalid_health_revision_fails_without_echoing_it(rig, served, diagnostic, capsys):
    rig.checker.revision(served, rig.checker.snapshot())
    assert diagnostic in rig.checker.failures[0]
    assert "synthetic-private-data" not in capsys.readouterr().out


def test_omitted_expected_sha_resolves_origin_main(rig):
    rig.args.expected_sha = None
    assert rig.checker.run() == 0
    assert any(c[-3:] == ("rev-parse", "--verify", "origin/main") for c in rig.calls)


def test_health_unreachable_still_checks_image_labels(rig, capsys):
    rig.opener.open.side_effect = URLError("synthetic private hostname")
    rig.state["backup"]["Config"]["Labels"][check.REVISION_LABEL] = WRONG
    assert rig.checker.run() == 1
    output = capsys.readouterr().out
    assert "FAIL health: unreachable (URLError)" in output
    assert "backup runs an image built from" in output
    assert "sent no X-App" not in output
    assert "synthetic private hostname" not in output


@pytest.mark.parametrize("code", [301, 503])
def test_health_rejects_redirects_and_unavailable(rig, code):
    rig.opener.open.side_effect = HTTPError(rig.args.health_url, code, "synthetic private detail", {}, None)
    rig.checker.health()
    assert rig.checker.failures == [f"health: returned {code}, expected 200"]
    assert check.NoRedirect().redirect_request(None, None, 301, "", {}, "https://example.invalid") is None


def test_health_is_rechecked_after_stability_wait(rig, monkeypatch, capsys):
    rig.args.stable_seconds = 2
    sleep = Mock(side_effect=lambda _: setattr(rig.response, "code", 503))
    monkeypatch.setattr(check.time, "sleep", sleep)
    assert rig.checker.run() == 1
    sleep.assert_called_once_with(2)
    assert "health after wait: returned 503" in capsys.readouterr().out


@pytest.mark.parametrize("mutation,diagnostic", [
    ("missing", "has no container"), ("exited", "is exited"),
    ("replaced", "was replaced"), ("count", "restarted during"),
    ("started", "restarted during"), ("already_restarted", "restarted 2 time(s) since"),
    ("kept", "was kept by the deploy but started again"),
])
def test_container_state_failures(rig, mutation, diagnostic):
    before = rig.checker.snapshot()
    after = copy.deepcopy(before)
    app = after["app"][0]
    if mutation == "missing":
        after["app"] = []
    elif mutation == "exited":
        app["status"] = "exited"
    elif mutation == "replaced":
        app["id"] = "new-id"
    elif mutation == "count":
        app["restart_count"] = 1
    elif mutation == "started":
        app["started_at"] = docker_time(NOW)
    elif mutation == "already_restarted":
        before["app"][0]["restart_count"] = app["restart_count"] = 2
    else:
        app["created"] = DEPLOY - timedelta(days=1)
        app["started"] = NOW
    rig.checker.containers(before, after, DEPLOY - timedelta(seconds=5))
    assert any(diagnostic in f for f in rig.checker.failures)


@pytest.mark.parametrize("mutation,diagnostic", [
    ("missing_log", "no event log"), ("no_create", "does not reach the app deploy"),
    ("no_start", "has no start for app"), ("old_start", "missed a start of app"),
    ("twice", "app started 2 times"), ("malformed", "has a line that is not"),
    ("finished", "FinishedAt is set"), ("missing_finished", "cannot read app's FinishedAt"),
])
def test_start_evidence_fails_closed(rig, mutation, diagnostic, capsys):
    if mutation == "missing_log":
        rig.log.unlink()
    elif mutation in {"finished", "missing_finished"}:
        rig.state["app"]["State"]["FinishedAt"] = docker_time(NOW) if mutation == "finished" else None
    elif mutation == "no_create":
        rig.events = [e for e in rig.events if "create app-id" not in e]
    elif mutation == "no_start":
        rig.events = [e for e in rig.events if "start app-id" not in e]
    elif mutation == "old_start":
        rig.state["app"]["State"]["StartedAt"] = docker_time(DEPLOY + timedelta(milliseconds=1))
    elif mutation == "twice":
        rig.events.append(f"{check.unix_nanoseconds(docker_time(NOW))} start app-id")
    else:
        rig.events = ["start app-id synthetic-private-data"]
    if mutation != "missing_log":
        rig.log.write_text("\n".join(rig.events), encoding="utf-8")
    rig.checker.start_counts(rig.checker.snapshot(), DEPLOY - timedelta(seconds=5), NOW)
    assert any(diagnostic in f for f in rig.checker.failures)
    assert "ok   containers: app has one start" not in capsys.readouterr().out


@pytest.mark.parametrize("offset", [-1, 0, 1, 3_000_000_000])
def test_events_compare_to_started_at_without_nanosecond_tolerance(rig, offset):
    rig.state["app"]["State"]["StartedAt"] = "2026-11-24T12:00:00.123456789Z"
    snapshot = rig.checker.snapshot()
    created = check.unix_nanoseconds(docker_time(DEPLOY))
    rig.log.write_text(f"{created} create app-id\n{1795521600123456789 + offset} start app-id\n", encoding="utf-8")
    # Limit this case to app so the log intentionally contains no unrelated services.
    for service in ("backup", "background"):
        snapshot[service] = []
    rig.checker.start_counts(snapshot, DEPLOY - timedelta(seconds=5), NOW)
    assert bool(rig.checker.failures) is (offset < 0)


def test_history_fallback_rejects_aged_out_history(rig):
    rig.args.event_log = None
    rig.events.clear()
    rig.checker.start_counts(rig.checker.snapshot(), DEPLOY - timedelta(seconds=5), NOW)
    assert any("Docker's event history no longer reaches" in f for f in rig.checker.failures)
    assert rig.calls[-1][-2:] == ("--format", check.EVENT_FORMAT)


def test_event_log_survives_aged_out_daemon_history(rig):
    rig.events.clear()  # Only the recorded log retains the create/start events.
    assert rig.checker.run() == 0
    assert not any(c[1] == "events" for c in rig.calls)


def test_automatic_deploy_date_includes_backup_created_before_app(rig, capsys):
    rig.state["backup"]["Created"] = docker_time(DEPLOY - timedelta(milliseconds=500))
    rig.state["backup"]["RestartCount"] = 1
    assert rig.checker.run() == 1
    assert "backup restarted 1 time(s) since the deploy" in capsys.readouterr().out


def test_traceback_output_contains_types_and_counts_only(rig, monkeypatch, capsys):
    monkeypatch.setattr(check.signals, "docker_log_lines", lambda *a: [
        "Traceback (most recent call last):", '  File "synthetic-private-path", line 1',
        "ValueError: synthetic private financial detail 123.45",
    ])
    rig.checker.tracebacks(rig.checker.snapshot(), DEPLOY)
    output = capsys.readouterr().out
    assert "(ValueError x1)" in output
    assert "synthetic" not in output
    assert "123.45" not in output
    assert len(rig.checker.failures) == 4


def test_unreadable_logs_fail_without_exposing_exception_message(rig, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("synthetic private detail")
    monkeypatch.setattr(check.signals, "docker_log_lines", fail)
    rig.checker.tracebacks(rig.checker.snapshot(), DEPLOY)
    assert len(rig.checker.failures) == 4
    assert "synthetic" not in capsys.readouterr().out


@pytest.mark.parametrize("state", ["missing", "error", "stale", "ok"])
def test_backup_states_and_error_privacy(rig, state, capsys):
    if state == "missing":
        rig.status.unlink()
    elif state == "error":
        rig.status.write_text("last_error=synthetic financial detail\n", encoding="utf-8")
    elif state == "stale":
        rig.status.write_text("last_success_at=2020-01-01T00:00:00Z\n", encoding="utf-8")
    rig.checker.backup()
    output = capsys.readouterr().out
    assert bool(rig.checker.failures) is (state in {"missing", "error"})
    assert "synthetic" not in output
    if state == "stale":
        assert "warn backup: status is stale" in output


def test_config_reads_quoted_values_and_latest_assignment(tmp_path):
    path = tmp_path / "production.env"
    path.write_text("OTHER=synthetic-secret\nBACKUP_DIR=/old\nexport BACKUP_DIR='/path with spaces' # comment\n"
                    'DJANGO_CSRF_TRUSTED_ORIGINS="https://planner.example.invalid" # comment\n', encoding="utf-8")
    assert check.env_value(path, "BACKUP_DIR") == "/path with spaces"
    assert check.env_value(path, "DJANGO_CSRF_TRUSTED_ORIGINS") == "https://planner.example.invalid"
    assert check.env_value(path, "MISSING") is None
    assert check.env_value(tmp_path / "missing", "BACKUP_DIR") is None


def test_config_defaults_derive_health_and_backup_paths(rig, tmp_path):
    config = tmp_path / "production.env"
    config.write_text(f"BACKUP_DIR={tmp_path}\nDJANGO_CSRF_TRUSTED_ORIGINS=https://planner.example.invalid/,https://other.invalid\n",
                      encoding="utf-8")
    rig.args.config_path = str(config)
    rig.args.status_path = rig.args.health_url = None
    assert rig.checker.run() == 0
    assert rig.args.health_url == "https://planner.example.invalid/health/"
    assert rig.args.status_path == str(rig.status)


def test_unexpected_errors_report_only_type(rig, monkeypatch, capsys):
    monkeypatch.setattr(rig.checker, "snapshot", Mock(side_effect=ValueError("synthetic private detail")))
    assert rig.checker.run() == 1
    output = capsys.readouterr().out
    assert "FAIL check: ValueError" in output
    assert "synthetic" not in output
    assert "post-deploy check FAILED: 1 problem(s)" in output


def test_native_captures_output_and_bounds_runtime(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="synthetic data"))
    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.native("docker", "inspect", "app-id") == "synthetic data"
    assert run.call_args.args == (("docker", "inspect", "app-id"),)
    assert run.call_args.kwargs["stderr"] == subprocess.DEVNULL
    assert run.call_args.kwargs["timeout"] == 120
    run.return_value = subprocess.CompletedProcess([], 1, stdout="synthetic private data")
    with pytest.raises(check.CheckError, match="^docker inspect failed$"):
        check.native("docker", "inspect", "app-id")


@pytest.mark.parametrize("text,ns", [
    ("2026-11-24T12:00:00.123456789Z", 1795521600123456789),
    ("2026-11-24T07:00:00.123456789-05:00", 1795521600123456789),
    ("1970-01-01T00:00:00Z", 0), ("invalid", None),
])
def test_exact_docker_time_parsing(text, ns):
    assert check.unix_nanoseconds(text) == ns


@pytest.mark.parametrize("argv", [["--since", "yesterday"], ["--stable-seconds", "-1"]])
def test_invalid_times_fail_argument_parsing(argv):
    with pytest.raises(SystemExit) as error:
        check.parser().parse_args(argv)
    assert error.value.code == 2


def test_cli_entry_function_runs_directly(rig):
    # Invoke the real parser with the same synthetic paths, not a shell runtime.
    assert check.main([
        "--project-name", "fp-post-deploy-test", "--health-url", rig.args.health_url,
        "--status-path", str(rig.status), "--expected-sha", SHA,
        "--event-log", str(rig.log), "--stable-seconds", "0",
        "--since", docker_time(DEPLOY - timedelta(seconds=5)),
    ]) == 0


def test_rollback_can_skip_revision_only_when_explicit(rig, capsys):
    rig.args.skip_revision = True
    rig.response.headers.clear()
    assert rig.checker.run() == 0
    assert "skip revision: --skip-revision given" in capsys.readouterr().out


def test_daemon_event_read_failure_fails_closed(rig, monkeypatch):
    rig.args.event_log = None
    snapshot = rig.checker.snapshot()
    monkeypatch.setattr(check, "native", Mock(side_effect=OSError("synthetic private detail")))
    rig.checker.start_counts(snapshot, DEPLOY - timedelta(seconds=5), NOW)
    assert rig.checker.failures == ["containers: cannot read docker events to count starts since the deploy"]


def test_missing_origin_main_does_not_default_to_checkout(rig, monkeypatch):
    rig.args.expected_sha = None
    snapshot = rig.checker.snapshot()
    monkeypatch.setattr(check, "native", Mock(side_effect=OSError("synthetic private detail")))
    rig.checker.revision(SHA[:12], snapshot)
    assert rig.checker.failures == ["revision: cannot read origin/main; run git fetch origin or pass --expected-sha"]


@pytest.mark.parametrize("missing", ["health", "backup"])
def test_missing_config_values_explain_required_override(rig, tmp_path, missing):
    rig.args.config_path = str(tmp_path / "absent.env")
    if missing == "health":
        rig.args.health_url = None
    else:
        rig.args.status_path = None
    assert rig.checker.run() == 1
    assert not rig.calls
    assert ("--health-url" if missing == "health" else "--status-path") in rig.checker.failures[0]


@pytest.fixture
def python310_datetime(monkeypatch):
    """Exercise the older fromisoformat contract on every CI interpreter."""
    class Python310Datetime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            match = check.TIME_PATTERN.fullmatch(value)
            fraction = match[2] if match else None
            if value.endswith("Z") or (fraction is not None and len(fraction) not in (3, 6)):
                raise ValueError("Python 3.10 cannot parse this ISO timestamp")
            return datetime.fromisoformat(value)

    monkeypatch.setattr(check, "datetime", Python310Datetime)


def nanosecond_inspect_times(rig):
    for info in rig.state.values():
        for mapping, key in ((info, "Created"), (info["State"], "StartedAt")):
            whole = mapping[key].split(".")[0].removesuffix("Z")
            mapping[key] = whole + ".123456789Z"
    rig.log.write_text("\n".join(
        f"{check.unix_nanoseconds(rig.state[service]['Created'])} {action} {service}-id"
        for service in ("app", "backup", "background") for action in ("create", "start")
    ), encoding="utf-8")


def test_python310_explicit_since_cannot_pass_a_restarted_nanosecond_container(
    rig, python310_datetime, capsys,
):
    nanosecond_inspect_times(rig)
    rig.args.since = DEPLOY - timedelta(seconds=5)
    rig.state["app"]["RestartCount"] = 2
    rig.state["app"]["State"]["FinishedAt"] = docker_time(NOW)
    rig.log.write_text("", encoding="utf-8")  # No evidence may prove one start.

    assert rig.checker.run() == 1
    output = capsys.readouterr().out
    assert "app restarted 2 time(s) since the deploy" in output
    assert "app stopped and started again since it was created" in output
    assert "the event log does not reach the app deploy" in output


@pytest.mark.parametrize("explicit_since", [False, True])
def test_python310_clean_nanosecond_deploy_passes(rig, python310_datetime, explicit_since):
    nanosecond_inspect_times(rig)
    if explicit_since:
        rig.args.since = DEPLOY - timedelta(seconds=5)
    assert rig.checker.run() == 0


@pytest.mark.parametrize("field", ["Created", "StartedAt"])
@pytest.mark.parametrize("value", ["not-a-date", "2026-02-30T12:00:00.123456789Z"])
def test_invalid_inspect_timestamp_fails_closed_with_explicit_since(rig, field, value):
    rig.args.since = DEPLOY - timedelta(seconds=5)
    target = rig.state["app"] if field == "Created" else rig.state["app"]["State"]
    target[field] = value
    assert rig.checker.run() == 1
    assert any(f"cannot parse app {field} timestamp" in failure for failure in rig.checker.failures)


@pytest.mark.parametrize("digits", range(1, 10))
@pytest.mark.parametrize("whole,zone", [("2026-11-24T12:00:00", "Z"), ("2026-11-24T07:00:00", "-05:00")])
def test_python310_normalizes_fraction_for_dates_but_keeps_exact_event_nanoseconds(
    python310_datetime, digits, whole, zone,
):
    fraction = "123456789"[:digits]
    text = f"{whole}.{fraction}{zone}"
    expected = datetime(2026, 11, 24, 12, 0, 0, int(fraction[:6].ljust(6, "0")), tzinfo=UTC)
    assert check.parse_time(text) == expected
    assert check.unix_nanoseconds(text) == 1795521600 * 10**9 + int(fraction.ljust(9, "0"))
