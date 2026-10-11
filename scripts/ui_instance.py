"""Run a loopback-only, synthetic UI instance; Ctrl-C or --teardown removes it."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
from socketserver import ThreadingMixIn
import subprocess
import sys
import tempfile
import threading
from urllib.request import ProxyHandler, Request, build_opener
from wsgiref.simple_server import WSGIServer, make_server

ROOT = Path(__file__).resolve().parents[1]
USERNAME = "synthetic_alex"
PASSWORD = "Synthetic-passphrase-42!"
PREFIX = "financial-planner-ui-"
logger = logging.getLogger(__name__)


def refuse_database_environment(environ):
    """Fail closed before imports, migrations, or CSS builds can touch anything."""
    unsafe = [key for key, value in environ.items() if value and (
        key.upper().startswith(("DATABASE", "POSTGRES_", "PG"))
        or key.upper() in {"DB_HOST", "DB_NAME", "DB_URL", "DB_PASSWORD"}
    )]
    if unsafe:
        raise RuntimeError("Refusing database environment variables: " + ", ".join(sorted(unsafe)))


def isolated_environment(directory):
    # Retain OS/runtime essentials only. Operator AI, SMTP, receipt, policy, and
    # audit settings cannot point this process at live services or storage.
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
            "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update({
        "DJANGO_SETTINGS_MODULE": "financial_planner.ui_settings",
        "FINANCIAL_PLANNER_UI_DIR": str(directory),
        "FINANCIAL_PLANNER_TEST_SQLITE": "1",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "DJANGO_DEBUG": "true", "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "DJANGO_SECURE_COOKIES": "false", "DJANGO_SECURE_SSL_REDIRECT": "false",
        "DJANGO_SECURE_HSTS_SECONDS": "0",
        "FIELD_ENCRYPTION_KEY": "dGVzdC1maWVsZC1lbmNyeXB0aW9uLWtleS0zMmJ5dGU=",
        "RECEIPTS_DIR": str(directory / "receipts"),
        "CSV_IMPORT_STAGING_DIR": str(directory / "csv-staging"),
        "OPERATOR_AUDIT_DIR": str(directory / "operator-audit"),
        "AI_LOCAL_QUIET_WINDOW": "", "AGENT_HARNESS_HOSTED_SESSIONS": "true",
    })
    return env


def build_css():
    if (ROOT / "static/dist/app.css").exists():
        return
    bash = shutil.which("bash")
    if sys.platform == "win32":
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if git_bash.exists():
            bash = str(git_bash)
    if not bash:
        raise RuntimeError("Install Bash (Git Bash on Windows) to build CSS.")
    subprocess.run([bash, "scripts/build_css.sh"], cwd=ROOT, check=True, stdout=sys.stderr)


def teardown(directory):
    directory = Path(directory).resolve()
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith(PREFIX):
        raise RuntimeError("Teardown requires the printed disposable directory under the system temp folder.")
    state = json.loads((directory / "instance.json").read_text())
    # State controls only a loopback request, never a PID kill or deletion.
    port = int(state["port"])
    if not 1 <= port <= 65535:
        raise RuntimeError("Invalid disposable port.")
    request = Request(f"http://127.0.0.1:{port}/__ui_teardown__/", method="POST",
                      headers={"Authorization": "Bearer " + state["token"]})
    # Teardown must stay on loopback even in an operator shell with HTTP_PROXY.
    with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("Disposable server refused teardown.")


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = False
    block_on_close = True


def serve(directory):
    env = isolated_environment(directory)
    os.environ.clear()
    os.environ.update(env)
    sys.path.insert(0, str(ROOT))
    import django
    django.setup()
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from django.db import connections
    from finance.ai_services import connect_harness, set_defaults, set_offer_local_chat
    from finance.policy_services import accept_policy, publish_policy
    from tests.fake_harness import start_fake_harness
    from tests.mobile_seed import seed_phone_data

    call_command("migrate", interactive=False, verbosity=0)
    person = seed_phone_data()
    person.user.set_password(PASSWORD)
    person.user.save(update_fields=["password"])
    accept_policy(person, publish_policy(material=True, body="Synthetic UI testing policy."))
    harness_state, fake_url, harness_server = start_fake_harness()
    harness_state.session_answer = "Synthetic UI reply. No real provider was contacted."
    stop = threading.Event()
    worker = None
    server = None
    try:
        connect_harness(person, base_url=fake_url, token=harness_state.token)
        set_offer_local_chat(person, True)
        set_defaults(person, chat_backend="local", background_backend="local")
        app = StaticFilesHandler(get_wsgi_application())
        token = secrets.token_urlsafe(32)

        def application(environ, start_response):
            if environ.get("PATH_INFO") != "/__ui_teardown__/":
                return app(environ, start_response)
            authorized = secrets.compare_digest(environ.get("HTTP_AUTHORIZATION", ""), "Bearer " + token)
            if environ.get("REQUEST_METHOD") != "POST" or not authorized:
                start_response("403 Forbidden", [("Content-Type", "text/plain")])
                return [b"Forbidden"]
            start_response("200 OK", [("Content-Type", "text/plain")])
            threading.Thread(target=server.shutdown, daemon=True).start()
            return [b"Stopping disposable UI instance"]

        server = make_server("127.0.0.1", 0, application, server_class=ThreadedServer)
        port = server.server_port
        (directory / "instance.json").write_text(json.dumps({"port": port, "token": token}))

        worker = threading.Thread(target=poll_chat, args=(stop,), name="synthetic-chat")
        worker.start()
        print(json.dumps({"url": f"http://127.0.0.1:{port}", "directory": str(directory),
                          "username": USERNAME, "password": PASSWORD}), flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        stop.set()
        if server:
            server.server_close()
        if worker:
            worker.join()
        harness_server.shutdown()
        harness_server.server_close()
        connections.close_all()


def poll_chat(stop):
    from django.db import close_old_connections, connections
    from finance.chat_runner import process_pending_turns, recover_stale_turns

    try:
        while not stop.wait(0.2):
            try:
                close_old_connections()
                recover_stale_turns()
                process_pending_turns(sleep=lambda _seconds: None)
            except Exception:
                # Like ChatLane.run_forever: a transient SQLite lock must not
                # permanently disable chat for this disposable instance.
                logger.exception("Synthetic chat poll failed; continuing")
    finally:
        connections.close_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teardown", metavar="DIRECTORY", help="Stop the instance printed at startup")
    args = parser.parse_args()
    if args.teardown:
        teardown(args.teardown)
        return
    refuse_database_environment(os.environ)
    build_css()
    with tempfile.TemporaryDirectory(prefix=PREFIX) as directory:
        try:
            serve(Path(directory))
        except KeyboardInterrupt:
            pass
        finally:
            # Also close connections if seeding failed before serve's cleanup block.
            if "django.db" in sys.modules:
                from django.db import connections
                connections.close_all()


if __name__ == "__main__":
    main()
