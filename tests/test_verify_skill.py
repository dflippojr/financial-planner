"""The verify skill's doctor checks and its cleanup after a failed drive step."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from scripts import ui_instance

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "verify" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
import doctor  # noqa: E402
import verify  # noqa: E402


def health_server(status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok\n" if status == 200 else b"unavailable\n"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A synthetic instance directory and a loopback health endpoint, with no Django."""
    monkeypatch.setattr(ui_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    directory = tmp_path / (ui_instance.PREFIX + "doctor")
    directory.mkdir()
    database = sqlite3.connect(directory / "db.sqlite3")
    database.executescript("""
        CREATE TABLE auth_user (username TEXT);
        CREATE TABLE finance_account (name TEXT);
        CREATE TABLE finance_transaction (id INTEGER);
        CREATE TABLE finance_alert (id INTEGER);
        INSERT INTO auth_user VALUES ('synthetic_alex');
        INSERT INTO finance_account VALUES ('Synthetic Checking');
        INSERT INTO finance_transaction VALUES (1);
        INSERT INTO finance_alert VALUES (1);
    """)
    database.commit()
    database.close()
    server = health_server(200)
    (directory / "instance.json").write_text(json.dumps({"port": server.server_port, "token": "synthetic"}))
    yield {"url": f"http://127.0.0.1:{server.server_port}", "directory": directory, "server": server}
    server.shutdown()
    server.server_close()


def failures(results):
    return {name: detail for name, ok, detail in results if not ok}


def test_healthy_instance_passes_every_check(instance, capsys):
    results = doctor.run_checks(instance["url"], instance["directory"], {"PATH": "/usr/bin"})
    assert failures(results) == {}
    assert doctor.report(results)
    assert capsys.readouterr().out.count("PASS") == 5


def test_not_ready_instance_fails(instance):
    unavailable = health_server(503)
    try:
        url = f"http://127.0.0.1:{unavailable.server_port}"
        assert "503" in failures(doctor.run_checks(url, instance["directory"], {}))["ready"]
    finally:
        unavailable.shutdown()
        unavailable.server_close()
    instance["server"].shutdown()
    instance["server"].server_close()
    assert "unreachable" in failures(doctor.run_checks(instance["url"], instance["directory"], {}))["ready"]


def test_non_loopback_instance_fails(instance, monkeypatch):
    port = instance["server"].server_port
    named = failures(doctor.run_checks(f"http://localhost:{port}", instance["directory"], {}))
    assert "127.0.0.1" in named["loopback"]
    (instance["directory"] / "instance.json").write_text(json.dumps({"port": port + 1}))
    assert "does not match" in failures(doctor.run_checks(instance["url"], instance["directory"], {}))["loopback"]
    (instance["directory"] / "instance.json").write_text(json.dumps({"port": port}))
    # Stand in for a wildcard bind; binding one for real could raise a host firewall prompt.
    monkeypatch.setattr(doctor, "listener_addresses", lambda _port: {"127.0.0.1", "0.0.0.0"})
    assert "also listens on 0.0.0.0" in failures(doctor.run_checks(instance["url"], instance["directory"], {}))["loopback"]
    monkeypatch.setattr(doctor, "listener_addresses", lambda _port: set())
    assert "no TCP listener" in failures(doctor.run_checks(instance["url"], instance["directory"], {}))["loopback"]


def test_listener_table_addresses_decode():
    assert doctor.proc_address("0100007F:1F90") == ("127.0.0.1", 8080)
    assert doctor.proc_address("00000000:0050") == ("0.0.0.0", 80)
    assert doctor.proc_address("00000000000000000000000001000000:1F90") == ("::1", 8080)
    assert doctor.proc_address("00000000000000000000000000000000:0050") == ("::", 80)


def test_missing_seed_fails(instance):
    database = sqlite3.connect(instance["directory"] / "db.sqlite3")
    database.execute("DELETE FROM finance_alert")
    database.commit()
    database.close()
    assert failures(doctor.run_checks(instance["url"], instance["directory"], {}))["seed"].endswith("alerts")


def test_credentials_and_data_paths_fail_without_printing_values(instance, capsys):
    environ = {"OPENAI_API_KEY": "sk-synthetic-value", "DATABASE_URL": "postgresql://synthetic.invalid/x",
               "RECEIPTS_DIR": "/synthetic/receipts", "PATH": "/usr/bin"}
    results = doctor.run_checks(instance["url"], instance["directory"], environ)
    assert failures(results) == {"environment": "remove from the launch environment: "
                                                "DATABASE_URL, OPENAI_API_KEY, RECEIPTS_DIR"}
    assert not doctor.report(results)
    output = capsys.readouterr().out
    assert "sk-synthetic-value" not in output and "synthetic.invalid" not in output


def test_non_disposable_storage_fails(instance, tmp_path):
    elsewhere = tmp_path / "nested" / (ui_instance.PREFIX + "x")
    elsewhere.mkdir(parents=True)
    assert "not a disposable" in failures(doctor.run_checks(instance["url"], elsewhere, {}))["storage"]


def test_launch_environment_is_allowlisted():
    env = verify.scrubbed_environment({"PATH": "/usr/bin", "GH_TOKEN": "synthetic", "PGHOST": "db"})
    assert env == {"PATH": "/usr/bin"}




class FakeProcess:
    returncode = 0
    stdout = None

    def wait(self, timeout=None):
        return 0


class FakeTree:
    def __init__(self, pid, stop_error=None):
        self.pid, self.process, self.stop_error = pid, FakeProcess(), stop_error
        self.calls = []

    def running(self):
        return False

    def alive(self):
        self.calls.append("alive")
        return False

    def stop(self):
        self.calls.append("stop")
        if self.stop_error:
            raise self.stop_error

    def close(self):
        self.calls.append("close")


def test_failed_stop_does_not_abort_the_rest_of_cleanup(tmp_path):
    ledger = verify.Ledger(tmp_path)
    drive = FakeTree(2, subprocess.TimeoutExpired("taskkill", 10))
    launcher = FakeTree(1)
    ledger.own("launcher", launcher)
    ledger.own("drive", drive)
    checks = {name: ok for name, ok, _ in verify.cleanup(ledger)}
    assert checks["stop drive tree"] is False
    assert checks["launcher exited"] and checks["launcher process tree stopped"]
    assert checks["drive process tree stopped"]
    assert "close" in launcher.calls and "close" in drive.calls
    assert json.loads((tmp_path / "ledger.json").read_text())["cleanup"]


def pid_alive(pid):
    if sys.platform == "win32":
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in listing.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_relative_evidence_root_keeps_one_absolute_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def failing_launch(ledger, env):
        raise verify.StepFailed("deliberate launch failure")

    monkeypatch.setattr(verify, "launch", failing_launch)
    assert verify.run("alert", "relative/evidence") == 1
    evidence = next((tmp_path / "relative" / "evidence").iterdir())
    ledger = json.loads((evidence / "ledger.json").read_text())
    assert ledger["steps"] == {"launch": {"status": "FAIL", "detail": "deliberate launch failure"}}
    assert (evidence / "summary.md").exists()


def test_feature_choices_come_from_the_shared_registry():
    registry = json.loads((SKILL_SCRIPTS / "features.json").read_text())
    assert verify.FEATURES == tuple(registry)
    assert all(entry["page"].startswith("/") and entry["heading"] for entry in registry.values())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_CHILD_SUBREAPER is Linux-only; Windows owns descendants with a job")
def test_orphan_in_its_own_session_is_adopted_and_stopped(tmp_path):
    marker = tmp_path / "orphan.pid"
    # Like the launcher's CSS builder: a new session, then its parent dies without cleanup.
    spawn = ("import pathlib, subprocess, sys; "
             "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], start_new_session=True); "
             f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))")
    assert verify.set_subreaper(True)
    try:
        subprocess.run([sys.executable, "-c", spawn], check=True, timeout=30)
        orphan = int(marker.read_text())
        assert orphan in verify.adopted_orphans(set())
        assert orphan in verify.sweep_orphans(set())
        assert not pid_alive(orphan)
    finally:
        verify.set_subreaper(False)


# Launches a real disposable instance, which needs the built CSS that the
# browser CI job prepares; the drive step itself is replaced by a failure.
@pytest.mark.browser
def test_cleanup_after_failed_drive_leaves_nothing_running(tmp_path, monkeypatch):
    evidence_root = tmp_path / "evidence"
    marker = tmp_path / "grandchild.pid"
    orphans = []

    def failing_drive(ledger, env, feature):
        # The parent exits at once and leaves a "browser" behind, as a crashed runner would.
        spawn = ("import pathlib, subprocess, sys; "
                 "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
                 f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))")
        tree = verify.OwnedTree([sys.executable, "-c", spawn])
        ledger.own("drive", tree)
        tree.process.wait(timeout=30)
        orphans.append(int(marker.read_text()))
        raise verify.StepFailed("deliberate drive failure")

    monkeypatch.setattr(verify, "drive", failing_drive)
    assert verify.run("alert", evidence_root) == 1
    evidence = next(evidence_root.iterdir())
    ledger = json.loads((evidence / "ledger.json").read_text())
    assert ledger["steps"]["launch"]["status"] == "pass"
    assert ledger["steps"]["doctor"]["status"] == "pass"
    assert ledger["steps"]["drive"] == {"status": "FAIL", "detail": "deliberate drive failure"}
    assert ledger["cleanup"] and all(check["ok"] for check in ledger["cleanup"])
    assert orphans and not pid_alive(orphans[0])
    assert not Path(ledger["instance"]["directory"]).exists()
    assert verify.port_free(ledger["instance"]["port"])
    assert not (evidence / "startup-handle.json").exists()
    summary = (evidence / "summary.md").read_text()
    assert "| drive | FAIL | deliberate drive failure |" in summary
    assert "| drive process tree stopped | pass |" in summary
    assert "password" not in (evidence / "ledger.json").read_text().lower()
