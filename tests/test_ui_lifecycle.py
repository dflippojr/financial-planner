"""Exercise the UI server lifecycle without requiring Chromium."""
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest
import requests

from scripts import ui_instance


def test_idle_browser_preconnection_does_not_block_shutdown():
    accepted = threading.Event()

    class ObservedServer(ui_instance.ThreadedServer):
        def get_request(self):
            result = super().get_request()
            accepted.set()
            return result

    server = ui_instance.make_server("127.0.0.1", 0, lambda _env, _start: [], server_class=ObservedServer)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    connection = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    try:
        assert accepted.wait(timeout=5)
        started = time.monotonic()
        server.shutdown()
        server.server_close()
        serving.join(timeout=1)
        assert not serving.is_alive()
        assert time.monotonic() - started < 7
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)


def test_cancel_before_readiness_removes_partial_storage_and_exits(tmp_path):
    handle = tmp_path / "startup.json"
    code = """
import sys, time
from scripts import ui_instance
ui_instance.build_css = lambda: None
def slow_start(directory):
    (directory / 'db.sqlite3').write_text('synthetic partial startup')
    while True:
        time.sleep(0.1)
ui_instance.serve = slow_start
sys.argv = ['ui_instance.py', '--startup-handle', sys.argv[1]]
ui_instance.main()
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(handle)], cwd=ui_instance.ROOT,
                             env=ui_instance.isolated_environment(tmp_path),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    directory = None
    try:
        deadline = time.monotonic() + 10
        while not handle.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert handle.exists(), "Cleanup handle missing before readiness"
        value = json.loads(handle.read_text())
        assert set(value) == {"directory"}  # Never publish credentials or tokens.
        directory = ui_instance.disposable_directory(value["directory"])
        assert not (directory / "instance.json").exists()
        ui_instance.teardown(directory)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr.decode()
        assert not stdout
        assert not directory.exists()
    finally:
        if child.poll() is None:
            if directory:
                ui_instance.teardown(directory)
            child.communicate(timeout=10)


@pytest.mark.django_db(transaction=True)
def test_http_lifecycle_seeds_sign_in_chat_and_authenticated_teardown(monkeypatch, settings):
    # Django is already initialized by pytest. Use its disposable test database;
    # the separate browser check verifies the CLI's fresh on-disk SQLite overlay.
    settings.DEBUG = True
    settings.ALLOWED_HOSTS = ["127.0.0.1"]
    errors = []
    with tempfile.TemporaryDirectory(prefix=ui_instance.PREFIX) as directory:
        directory = Path(directory)
        monkeypatch.setattr(os, "environ", dict(os.environ))

        def run_server():
            try:
                ui_instance.serve(directory)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        state_path = directory / "instance.json"
        deadline = time.monotonic() + 30
        while not state_path.exists() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert state_path.exists(), errors
        state = json.loads(state_path.read_text())
        base = f"http://127.0.0.1:{state['port']}"
        try:
            with requests.Session() as client:
                login = client.get(base + "/sign-in/", timeout=5)
                assert login.status_code == 200
                response = client.post(base + "/sign-in/", timeout=5, data={
                    "username": ui_instance.USERNAME, "password": ui_instance.PASSWORD,
                    "csrfmiddlewaretoken": client.cookies["csrftoken"],
                })
                assert "Cash flow" in response.text
                assert response.url == base + "/"
                for path in ["/transactions/", "/planning/budgets/", "/settings/security/"]:
                    assert client.get(base + path, timeout=5).status_code == 200
                response = client.post(base + "/chat/send/", timeout=5, data={
                    "prompt": "A synthetic lifecycle question",
                    "csrfmiddlewaretoken": client.cookies["csrftoken"],
                })
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    response = client.get(response.url, timeout=5)
                    if "Synthetic UI reply. No real provider was contacted." in response.text:
                        break
                    time.sleep(0.1)
                assert "Synthetic UI reply. No real provider was contacted." in response.text
                assert client.post(base + "/__ui_teardown__/", timeout=5).status_code == 403
                assert client.get(base + "/__ui_teardown__/", timeout=5,
                                  headers={"Authorization": "Bearer " + state["token"]}).status_code == 403
        finally:
            # A configured proxy must not receive or block the loopback teardown.
            monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
            monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv("no_proxy", "")
            ui_instance.teardown(directory)
            thread.join(timeout=15)
        assert not thread.is_alive(), "Server or synthetic chat worker did not stop"
        assert not errors


def test_instance_state_is_visible_only_after_the_write_finishes(monkeypatch, tmp_path):
    opened = threading.Event()
    finish = threading.Event()
    original_write = Path.write_text
    errors = []

    def paused_write(path, content):
        path.touch()
        opened.set()
        assert finish.wait(timeout=5)
        return original_write(path, content)

    def publish():
        try:
            ui_instance.publish_instance_state(tmp_path, {"port": 12345, "token": "synthetic"})
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(Path, "write_text", paused_write)
    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert opened.wait(timeout=5)
        assert not (tmp_path / "instance.json").exists()
    finally:
        finish.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert json.loads((tmp_path / "instance.json").read_text()) == {"port": 12345, "token": "synthetic"}


def test_ui_settings_force_temporary_sqlite_and_disable_email(monkeypatch, tmp_path):
    from financial_planner import settings as production_settings
    directory = tmp_path / (ui_instance.PREFIX + "settings")
    monkeypatch.setenv("FINANCIAL_PLANNER_UI_DIR", str(directory))
    values = runpy.run_module("financial_planner.ui_settings")
    assert values["DATABASES"]["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert values["DATABASES"]["default"]["NAME"] == directory / "db.sqlite3"
    assert values["EMAIL_BACKEND"] == "django.core.mail.backends.locmem.EmailBackend"
    assert values["EMAIL_HOST"] == ""
    assert values["LOGGING"]["handlers"]["console"]["stream"] == "ext://sys.stderr"
    assert values["LOGGING"] is not production_settings.LOGGING
    assert production_settings.LOGGING["handlers"]["console"]["stream"] == "ext://sys.stdout"
    monkeypatch.setenv("FINANCIAL_PLANNER_UI_DIR", "relative-directory")
    with pytest.raises(RuntimeError, match="disposable UI directory"):
        runpy.run_module("financial_planner.ui_settings")


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_main_removes_storage_on_interruption_and_startup_failure(monkeypatch, error):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "argv", ["ui_instance.py"])
    monkeypatch.setattr(ui_instance, "build_css", lambda: None)
    created = []

    def fail(directory):
        created.append(directory)
        (directory / "synthetic.txt").write_text("synthetic")
        raise error()

    monkeypatch.setattr(ui_instance, "serve", fail)
    if error is RuntimeError:
        with pytest.raises(RuntimeError):
            ui_instance.main()
    else:
        ui_instance.main()
    assert len(created) == 1
    assert not created[0].exists()


def test_teardown_cli_does_not_start_a_database(monkeypatch):
    monkeypatch.setattr(os, "environ", {"DATABASE_URL": "synthetic-configured-db"})
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "--teardown", "synthetic-directory"])
    stopped = []
    monkeypatch.setattr(ui_instance, "teardown", stopped.append)
    ui_instance.main()
    assert stopped == ["synthetic-directory"]


def test_missing_css_requires_bash_and_runs_the_pinned_builder(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_instance, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ui_instance.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Install Bash"):
        ui_instance.build_css()
    monkeypatch.setattr(ui_instance.shutil, "which", lambda _name: "/synthetic/bash")
    calls = []
    monkeypatch.setattr(ui_instance, "run_css_builder", calls.append)
    ui_instance.build_css()
    assert calls == [["/synthetic/bash", "scripts/build_css.sh"]]
    css = tmp_path / "static/dist/app.css"
    css.parent.mkdir(parents=True)
    css.write_text("synthetic CSS")
    ui_instance.build_css()
    assert len(calls) == 1


def test_chat_poll_survives_a_database_lock_and_closes_connections(monkeypatch):
    from django import db
    from django.db import OperationalError
    from finance import chat_runner

    stop = threading.Event()
    attempts = []
    recoveries = []
    closed = []
    monkeypatch.setattr(db, "close_old_connections", lambda: None)
    monkeypatch.setattr(db.connections, "close_all", lambda: closed.append(True))
    monkeypatch.setattr(chat_runner, "recover_stale_turns", lambda: recoveries.append(True))

    def poll(**_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OperationalError("database is locked")
        stop.set()

    monkeypatch.setattr(chat_runner, "process_pending_turns", poll)
    ui_instance.poll_chat(stop)
    assert len(attempts) == 2
    assert len(recoveries) == 2
    assert closed == [True]
