"""The verify skill's doctor checks and its cleanup after a failed drive step."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A synthetic instance directory and a loopback health endpoint, with no Django."""
    monkeypatch.setattr(ui_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(doctor, "non_loopback_addresses", lambda: [])
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
    # Stand in for a listener that also answers on a LAN address.
    monkeypatch.setattr(doctor, "non_loopback_addresses", lambda: ["127.0.0.1"])
    assert "also answers" in failures(doctor.run_checks(instance["url"], instance["directory"], {}))["loopback"]


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


# Launches a real disposable instance, which needs the built CSS that the
# browser CI job prepares; the drive step itself is replaced by a failure.
@pytest.mark.browser
def test_cleanup_after_failed_drive_leaves_nothing_running(tmp_path, monkeypatch):
    started = []

    def failing_drive(ledger, env, feature):
        # Stands in for a browser or worker the drive step started and left behind.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], **verify.GROUP)
        started.append(child)
        ledger.own("drive", child)
        raise verify.StepFailed("deliberate drive failure")

    monkeypatch.setattr(verify, "drive", failing_drive)
    try:
        assert verify.run("alert", tmp_path) == 1
    finally:
        for child in started:
            if child.poll() is None:
                child.kill()
    evidence = next(tmp_path.iterdir())
    ledger = json.loads((evidence / "ledger.json").read_text())
    assert ledger["steps"]["launch"]["status"] == "pass"
    assert ledger["steps"]["doctor"]["status"] == "pass"
    assert ledger["steps"]["drive"] == {"status": "FAIL", "detail": "deliberate drive failure"}
    assert ledger["cleanup"] and all(check["ok"] for check in ledger["cleanup"])
    assert started[0].returncode is not None
    assert not Path(ledger["instance"]["directory"]).exists()
    assert verify.port_free(ledger["instance"]["port"])
    assert not (evidence / "startup-handle.json").exists()
    summary = (evidence / "summary.md").read_text()
    assert "| drive | FAIL | deliberate drive failure |" in summary
    assert "password" not in (evidence / "ledger.json").read_text().lower()
