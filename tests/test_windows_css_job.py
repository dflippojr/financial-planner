"""Deterministic Windows tree failures, also runnable on Linux CI."""
import ctypes
import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import ui_instance as ui
from scripts import windows_css_job as windows


class Builder:
    pid = 123456789
    stdin = None
    returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        assert timeout == 5
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic builder", timeout)
        return self.returncode


class Job:
    name = "synthetic-css-job"
    remaining = 2
    terminated = False
    closed = False
    assigned = False

    def __init__(self, builder):
        self.builder = builder

    def assign(self, pid):
        assert pid == self.builder.pid
        self.assigned = True

    def active(self):
        return self.remaining

    def pids(self):
        return [self.builder.pid, self.builder.pid + 1]

    def terminate(self):
        self.terminated = True
        self.remaining = 0
        self.builder.returncode = 1

    def close(self):
        self.closed = True


@pytest.fixture
def owned(monkeypatch):
    builder = Builder()
    job = Job(builder)
    monkeypatch.setattr(ui, "os", SimpleNamespace(name="nt", environ={}))
    return builder, job


@pytest.mark.parametrize("result", ["timeout", "launch-error", "nonzero", "parent-only", "success"])
def test_taskkill_outcomes_verify_descendants_and_use_owned_fallback(monkeypatch, owned, result):
    builder, job = owned
    calls = []

    def taskkill(command, **options):
        calls.append(command)
        assert options["timeout"] == 5
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        if result == "launch-error":
            raise FileNotFoundError("synthetic missing taskkill")
        if result in {"parent-only", "success"}:
            builder.returncode = 1
        if result == "success":
            job.remaining = 0
        return SimpleNamespace(returncode=1 if result == "nonzero" else 0)

    monkeypatch.setattr(ui.subprocess, "run", taskkill)
    ui.stop_css_builder(builder, job)
    assert calls == [["taskkill", "/PID", str(builder.pid), "/T", "/F"]]
    assert job.terminated is (result != "success")
    assert job.remaining == 0


@pytest.mark.parametrize("remaining", [0, 1])
def test_exited_parent_never_targets_reused_pid_but_still_checks_job(monkeypatch, owned, remaining):
    builder, job = owned
    builder.returncode = 0
    job.remaining = remaining
    monkeypatch.setattr(ui.subprocess, "run", lambda *_a, **_k: pytest.fail("Dead parent PID"))
    ui.stop_css_builder(builder, job)
    assert job.terminated is bool(remaining)


@pytest.mark.parametrize("failure", ["terminate", "verification", "still-active", "parent-wait", "snapshot"])
def test_fallback_failure_is_bounded_and_retains_ownership(monkeypatch, owned, failure):
    builder, job = owned
    monkeypatch.setattr(ui.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    def fail():
        raise OSError("synthetic API failure")
    if failure == "terminate":
        monkeypatch.setattr(job, "terminate", fail)
    elif failure == "snapshot":
        monkeypatch.setattr(job, "pids", fail)
    elif failure == "verification":
        monkeypatch.setattr(job, "active", fail)
    elif failure == "still-active":
        monkeypatch.setattr(job, "terminate", lambda: None)
        times = iter([10, 15])
        monkeypatch.setattr(ui.time, "monotonic", lambda: next(times))
    else:
        job.remaining = 0
    with pytest.raises(ui.CssBuilderCleanupError, match="cleanup unverified") as raised:
        ui.stop_css_builder(builder, job)
    assert raised.value.recovery["builder_pid"] == builder.pid
    assert raised.value.recovery["job"] == job.name
    assert raised.value.recovery["last_known_owned_pids"] == ([] if failure == "snapshot" else job.pids())


def test_verification_waits_for_descendants_with_a_finite_deadline(monkeypatch, owned):
    builder, job = owned
    monkeypatch.setattr(ui.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    def terminate():
        builder.returncode = 1
    monkeypatch.setattr(job, "terminate", terminate)
    states = iter([2, 2, 1, 0])
    monkeypatch.setattr(job, "active", lambda: next(states))
    sleeps = []
    monkeypatch.setattr(ui.time, "sleep", sleeps.append)
    ui.stop_css_builder(builder, job)
    assert sleeps == [0.05]


def test_missing_job_cannot_claim_parent_cleanup_succeeded(owned):
    with pytest.raises(ui.CssBuilderCleanupError, match="Missing owned Windows job"):
        ui.stop_css_builder(owned[0])


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, RuntimeError])
def test_original_startup_error_survives_cleanup_failure(monkeypatch, owned, error_type, caplog):
    builder, job = owned
    builder.stdin = io.BytesIO()
    original = error_type("synthetic startup failure")
    def popen(command, **options):
        assert command[0] == sys._base_executable
        assert options["stdin"] == subprocess.PIPE
        return builder
    monkeypatch.setattr(windows, "WindowsCssJob", lambda: job)
    monkeypatch.setattr(ui.subprocess, "Popen", popen)
    monkeypatch.setattr(ui.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(ui.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    def sleep(_seconds):
        assert job.assigned
        assert builder.stdin.getvalue() == b"1"
        raise original
    monkeypatch.setattr(ui.time, "sleep", sleep)
    monkeypatch.setattr(ui.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    def fail():
        raise OSError("synthetic termination failure")
    monkeypatch.setattr(job, "terminate", fail)
    with pytest.raises(error_type) as raised:
        ui.run_css_builder(["synthetic-bash"])
    assert raised.value is original
    assert original.css_cleanup_error.recovery["last_known_owned_pids"] == job.pids()
    assert "cleanup unverified" in caplog.text
    assert builder.stdin.closed and job.closed


def test_assignment_failure_never_releases_builder_gate(monkeypatch, owned):
    builder, job = owned
    class Gate(io.BytesIO):
        released = False

        def write(self, value):
            self.released = True
            return super().write(value)

        def close(self):
            builder.returncode = 1  # EOF exits without spawning the command.
            super().close()

    builder.stdin = Gate()
    job.remaining = 0
    def deny(_pid):
        raise OSError("synthetic assignment denied")
    monkeypatch.setattr(job, "assign", deny)
    monkeypatch.setattr(windows, "WindowsCssJob", lambda: job)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *_a, **_k: builder)
    monkeypatch.setattr(ui.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(ui.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(ui.subprocess, "run", lambda *_a, **_k: pytest.fail("No released builder"))
    with pytest.raises(OSError, match="assignment denied"):
        ui.run_css_builder(["synthetic-bash"])
    assert not builder.stdin.released
    assert builder.stdin.closed and job.closed


def test_cleanup_failure_after_success_is_not_hidden(monkeypatch, owned):
    builder, job = owned
    builder.returncode = 0
    builder.stdin = io.BytesIO()
    monkeypatch.setattr(windows, "WindowsCssJob", lambda: job)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *_a, **_k: builder)
    monkeypatch.setattr(ui.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(ui.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    def fail():
        raise OSError("synthetic surviving descendant")
    monkeypatch.setattr(job, "terminate", fail)
    with pytest.raises(ui.CssBuilderCleanupError, match="surviving descendant"):
        ui.run_css_builder(["synthetic-bash"])
    assert job.closed


def test_close_failure_is_reported_after_verified_tree_exit(owned):
    builder, job = owned
    builder.returncode = 0
    job.remaining = 0
    def fail():
        raise OSError("synthetic close failure")
    job.close = fail
    with pytest.raises(ui.CssBuilderCleanupError, match="close failure"):
        ui.finish_css_builder(builder, job)


@pytest.mark.parametrize("phase", ["taskkill", "verification", "close"])
@pytest.mark.parametrize("initial_type", [None, KeyboardInterrupt, RuntimeError])
def test_cancellation_during_cleanup_retains_storage_and_initial_error(
    monkeypatch, owned, tmp_path, phase, initial_type
):
    builder, job = owned
    builder.stdin = io.BytesIO()
    initial = initial_type("synthetic initial error") if initial_type else None
    builder.returncode = None if initial else 0
    directory = tmp_path / (ui.PREFIX + "interrupted-cleanup")
    directory.mkdir()
    handle = tmp_path / "startup.json"
    monkeypatch.setattr(ui.tempfile, "mkdtemp", lambda **_k: str(directory))
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "--startup-handle", str(handle)])
    monkeypatch.setattr(windows, "WindowsCssJob", lambda: job)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *_a, **_k: builder)
    monkeypatch.setattr(ui.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(ui.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    def interrupt():
        raise KeyboardInterrupt()
    def taskkill(*_args, **_kwargs):
        if phase == "taskkill":
            interrupt()
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(ui.subprocess, "run", taskkill)
    if phase == "taskkill":
        builder.poll = lambda: None  # Interrupt before parent exit.
    def sleep(seconds):
        if seconds == 0.1 and initial:
            raise initial
        interrupt()
    if phase == "taskkill" and not initial:
        # Let the builder's ordinary polling finish, then interrupt taskkill.
        builder.poll = iter([0, None]).__next__
        builder.returncode = 0
    if phase == "verification":
        monkeypatch.setattr(job, "terminate", lambda: None)
    if phase == "close":
        monkeypatch.setattr(job, "close", interrupt)
    monkeypatch.setattr(ui.time, "sleep", sleep)
    monkeypatch.setattr(ui, "build_css", lambda: ui.run_css_builder(["synthetic-bash"]))
    try:
        with pytest.raises(initial_type or ui.CssBuilderCleanupError) as raised:
            ui.main()
        if initial:
            assert raised.value is initial
        assert json.loads(handle.read_text()) == {"directory": str(directory)}
        recovery = json.loads((directory / "css-cleanup-failed.json").read_text())
        assert recovery["reason"] == "KeyboardInterrupt"
        assert recovery["builder_pid"] == builder.pid and recovery["job"] == job.name
        assert builder.stdin.closed
        assert job.closed is (phase != "close")
    finally:
        ui.shutil.rmtree(directory)


@pytest.mark.parametrize("state", ["exited", "term", "missing", "kill"])
def test_posix_cleanup_retains_owned_group_behavior(monkeypatch, state):
    monkeypatch.setattr(ui.signal, "SIGKILL", 9, raising=False)
    builder = Builder()
    signals = []
    if state == "exited":
        builder.returncode = 0
    def killpg(pid, sig):
        assert pid == builder.pid
        signals.append(sig)
        if state == "missing":
            builder.returncode = 0
            raise ProcessLookupError()
        if state == "term" or sig == ui.signal.SIGKILL:
            builder.returncode = 0
    monkeypatch.setattr(ui, "os", SimpleNamespace(name="posix", killpg=killpg))
    ui.stop_css_builder(builder)
    assert signals == ([] if state == "exited" else
                       [ui.signal.SIGTERM, ui.signal.SIGKILL] if state == "kill" else [ui.signal.SIGTERM])


@pytest.mark.parametrize("initial", ["cleanup", "interrupt", "startup"])
def test_main_retains_storage_and_handle_when_tree_cleanup_is_unverified(monkeypatch, tmp_path, initial):
    directory = tmp_path / (ui.PREFIX + "synthetic-failure")
    directory.mkdir()
    handle = tmp_path / "startup.json"
    monkeypatch.setattr(ui.tempfile, "mkdtemp", lambda **_k: str(directory))
    monkeypatch.setattr(ui.os, "environ", {})
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "--startup-handle", str(handle)])
    error = ui.CssBuilderCleanupError(123, "synthetic-job", "synthetic failure", [123, 124])
    original = {"cleanup": error, "interrupt": KeyboardInterrupt(), "startup": RuntimeError()}[initial]
    if original is not error:
        original.css_cleanup_error = error
    def fail():
        raise original
    monkeypatch.setattr(ui, "build_css", fail)
    try:
        with pytest.raises(type(original)) as raised:
            ui.main()
        assert raised.value is original
        assert json.loads(handle.read_text()) == {"directory": str(directory)}
        assert json.loads((directory / "css-cleanup-failed.json").read_text()) == error.recovery
    finally:
        ui.shutil.rmtree(directory)


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.fixture
def native(monkeypatch):
    calls = []
    failure = set()
    def api_call(name, *args):
        calls.append((name, args))
        if name in failure:
            return 0
        if name == "SetInformationJobObject":
            assert args[1] == 9 and args[2]._obj.basic.flags == 0x2000
        if name == "QueryInformationJobObject":
            if args[1] == 1:
                args[2]._obj.active = 2
            else:
                args[2]._obj.count = 2
                args[2]._obj.ids[:2] = [123, 124]
        return 101 if name == "CreateJobObjectW" else 102 if name == "OpenProcess" else 1
    api = SimpleNamespace(**{name: Function(lambda *args, name=name: api_call(name, *args)) for name in (
        "CreateJobObjectW", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject",
        "TerminateJobObject", "QueryInformationJobObject", "CloseHandle"
    )})
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: api, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "synthetic native error"), raising=False)
    return calls, failure


def test_native_job_owns_process_and_queries_descendant_count(native):
    calls, _failure = native
    job = windows.WindowsCssJob()
    job.assign(123)
    assert job.active() == 2
    assert job.pids() == [123, 124]
    job.terminate()
    job.close()
    job.close()
    assert [(name, args) for name, args in calls if name == "OpenProcess"] == [("OpenProcess", (0x101, False, 123))]
    assert [(name, args) for name, args in calls if name == "AssignProcessToJobObject"] == [("AssignProcessToJobObject", (101, 102))]
    assert [args for name, args in calls if name == "CloseHandle"] == [(102,), (101,)]


@pytest.mark.parametrize("failure_name", ["CreateJobObjectW", "SetInformationJobObject", "OpenProcess",
                                        "AssignProcessToJobObject", "TerminateJobObject",
                                        "QueryInformationJobObject", "CloseHandle"])
def test_native_api_errors_are_reported_and_handles_closed(native, failure_name):
    calls, failure = native
    if failure_name in {"CreateJobObjectW", "SetInformationJobObject"}:
        failure.add(failure_name)
        with pytest.raises(OSError):
            windows.WindowsCssJob()
        if failure_name == "SetInformationJobObject":
            assert calls[-1] == ("CloseHandle", (101,))
        return
    job = windows.WindowsCssJob()
    failure.add(failure_name)
    action = {"OpenProcess": lambda: job.assign(123),
              "AssignProcessToJobObject": lambda: job.assign(123),
              "TerminateJobObject": job.terminate,
              "QueryInformationJobObject": job.active, "CloseHandle": job.close}[failure_name]
    try:
        with pytest.raises(OSError):
            action()
        if failure_name == "AssignProcessToJobObject":
            assert calls[-1] == ("CloseHandle", (102,))
    finally:
        failure.clear()
        job.close()
