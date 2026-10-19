"""Run a loopback-only, synthetic UI instance; Ctrl-C or --teardown removes it."""
from __future__ import annotations

import argparse
import _thread
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import shutil
from socketserver import ThreadingMixIn
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener
from wsgiref.simple_server import WSGIServer, make_server

ROOT = Path(__file__).resolve().parents[1]
USERNAME = "synthetic_alex"
PASSWORD = "Synthetic-passphrase-42!"  # noqa: S105 - synthetic login for a disposable instance
PREFIX = "financial-planner-ui-"
ENV_ALLOWLIST = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
                 "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL"}
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
    env = {key: value for key, value in os.environ.items() if key.upper() in ENV_ALLOWLIST}
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
    run_css_builder([bash, "scripts/build_css.sh"])


def run_css_builder(command):
    # Poll instead of Popen.wait: its KeyboardInterrupt handler can pass an
    # expired timeout to the Windows wait API, delaying cancellation indefinitely.
    # Own the process group so cancellation also stops the tools it started.
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    tree = None
    process = None
    initial_error = None
    try:
        if os.name == "nt":
            # This gate cannot spawn Bash until it belongs to our job. Use the
            # base interpreter, avoiding the Windows venv launcher's own child.
            if __package__:
                from .windows_css_job import WindowsCssJob
            else:
                from windows_css_job import WindowsCssJob
            tree = WindowsCssJob()
            gate = ("import subprocess,sys; "
                    "token=sys.stdin.buffer.read(1); "
                    "sys.exit(subprocess.call(sys.argv[1:]) if token == b'1' else 1)")
            process = subprocess.Popen([sys._base_executable, "-c", gate, *command],
                                       cwd=ROOT, stdout=sys.stderr, stdin=subprocess.PIPE, **options)
            tree.assign(process.pid)
            process.stdin.write(b"1")
            process.stdin.flush()
        else:
            process = subprocess.Popen(command, cwd=ROOT, stdout=sys.stderr, **options)
        while process.poll() is None:
            time.sleep(0.1)
        returncode = process.returncode
        if returncode:
            raise subprocess.CalledProcessError(returncode, command)
    except BaseException as error:
        initial_error = error
        raise
    finally:
        try:
            finish_css_builder(process, tree)
        except CssBuilderCleanupError as error:
            if initial_error is None:
                raise
            initial_error.css_cleanup_error = error
            try:
                initial_error.add_note(str(error))
                logger.error("%s", error)
            except BaseException:
                raise initial_error  # noqa: B904 - re-raises the primary failure; the later one is attached as a note


def finish_css_builder(process, tree):
    cleanup_error = None
    try:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            stop_css_builder(process, tree)
    except (OSError, CssBuilderCleanupError, KeyboardInterrupt) as error:
        cleanup_error = error
    finally:
        if tree is not None:
            try:
                tree.close()
            except (OSError, KeyboardInterrupt) as error:
                cleanup_error = cleanup_error or error
    if cleanup_error:
        if isinstance(cleanup_error, CssBuilderCleanupError):
            raise cleanup_error
        raise CssBuilderCleanupError(process.pid if process else None,
                                     tree.name if tree else None, cleanup_error) from cleanup_error


class CssBuilderCleanupError(RuntimeError):
    def __init__(self, pid, job, reason, owned_pids=()):
        reason = str(reason) or type(reason).__name__
        owned_pids = list(owned_pids)
        self.recovery = {"builder_pid": pid, "job": job, "reason": reason,
                         "last_known_owned_pids": owned_pids}
        super().__init__(f"CSS builder cleanup unverified (PID {pid}, job {job}, "
                         f"last known owned PIDs {owned_pids}): {reason}. "
                         "Retain the startup handle; verify the owned tree has stopped before removing storage.")


def css_cleanup_failure(error):
    return error if isinstance(error, CssBuilderCleanupError) else getattr(error, "css_cleanup_error", None)


def secondary_failure_note(prefix, error, cleanup_error=None):
    reason = str(error) or type(error).__name__
    return prefix + ": " + reason + ("; " + str(cleanup_error) if cleanup_error else "")


def stop_css_builder(process, tree=None):
    # This is only the builder just spawned here, never a UI server.
    if os.name == "nt":
        stop_windows_css_builder(process, tree)
        return
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def stop_windows_css_builder(process, tree):
    if tree is None:
        raise CssBuilderCleanupError(process.pid, None, "Missing owned Windows job")
    owned_pids = ()
    try:
        owned_pids = tree.pids()
        if tree.active():
            # A dead parent PID may already have been reused. Only taskkill a
            # live parent; the job remains authoritative even after parent exit.
            if process.poll() is None:
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   check=False, timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass  # The job fallback below does not depend on taskkill.
            if tree.active():
                tree.terminate()
            deadline = time.monotonic() + 5
            while tree.active():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Owned Windows job still has active descendants after 5 seconds")
                time.sleep(0.05)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        raise CssBuilderCleanupError(process.pid, tree.name, error, owned_pids) from error


def disposable_directory(directory):
    directory = Path(directory).resolve()
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith(PREFIX):
        raise RuntimeError("Teardown requires the printed disposable directory under the system temp folder.")
    return directory


def teardown(directory):
    directory = disposable_directory(directory)
    # Publish cancellation even before the HTTP listener exists. The launcher's
    # watcher interrupts startup in-process, so its finally blocks remove storage.
    if not (directory / "instance.json").exists():
        (directory / "cancel-startup").touch()
        return
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

    def get_request(self):
        connection, address = super().get_request()
        # Browsers preconnect without sending a request. Closing the listener
        # must also finish those request threads before deleting their database.
        connection.settimeout(5)
        return connection, address


def publish_instance_state(directory, state):
    pending = directory / "instance.json.tmp"
    pending.write_text(json.dumps(state))
    pending.replace(directory / "instance.json")


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
    stop = threading.Event()
    worker = None
    server = None
    harness_server = None
    try:
        harness_state, fake_url, harness_server = start_fake_harness()
        harness_state.session_answer = "Synthetic UI reply. No real provider was contacted."
        harness_state.followup_answer = harness_state.session_answer
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
        publish_instance_state(directory, {"port": port, "token": token})

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
        if harness_server:
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
    parser.add_argument("--startup-handle", type=Path, help="Publish a token-free cleanup handle before startup")
    args = parser.parse_args()
    if args.teardown:
        teardown(args.teardown)
        return
    refuse_database_environment(os.environ)
    directory = Path(tempfile.mkdtemp(prefix=PREFIX))
    initial_error = None
    startup_failures = []
    try:
        if args.startup_handle:
            args.startup_handle.parent.mkdir(parents=True, exist_ok=True)
            pending = args.startup_handle.with_suffix(".tmp")
            pending.write_text(json.dumps({"directory": str(directory)}))
            pending.replace(args.startup_handle)
        run_ui_startup(directory, startup_failures)
    except BaseException as error:
        initial_error = startup_failures[0] if startup_failures else error
        if initial_error is not error:
            initial_error.add_note(secondary_failure_note("UI finalization also failed", error))
            raise initial_error  # noqa: B904 - re-raises the primary failure; the later one is attached as a note
        raise
    finally:
        # The primary failure is captured before watcher finalization. Even an
        # interruption entering that block cannot clear the retention decision.
        if startup_failures:
            initial_error = startup_failures[0]
        try:
            finish_ui_storage(directory, initial_error)
        except BaseException as error:
            if initial_error is None:
                raise
            cleanup_error = css_cleanup_failure(initial_error)
            initial_error.add_note(secondary_failure_note("UI storage cleanup also failed", error, cleanup_error))
            raise initial_error  # noqa: B904 - re-raises the primary failure; the later one is attached as a note


def finish_ui_storage(directory, initial_error):
    cleanup_error = css_cleanup_failure(initial_error)
    if cleanup_error:
        try:
            (directory / "css-cleanup-failed.json").write_text(json.dumps(cleanup_error.recovery))
        except BaseException as error:
            initial_error.add_note(secondary_failure_note("CSS recovery record could not be written", error, cleanup_error))
        logger.error("CSS cleanup storage retained in %s", directory)
    else:
        shutil.rmtree(directory)


def run_ui_startup(directory, startup_failures):
    stopped = threading.Event()
    watcher = threading.Thread(target=watch_startup_cancellation, args=(directory, stopped),
                               name="synthetic-startup-cancellation")
    initial_error = None
    try:
        watcher.start()
        build_css()
        serve(directory)
    except KeyboardInterrupt as error:
        if getattr(error, "css_cleanup_error", None):
            startup_failures.append(error)
            initial_error = error
            raise
    except BaseException as error:
        startup_failures.append(error)
        initial_error = error
        raise
    finally:
        finalize_ui_startup(stopped, watcher, initial_error)


def finalize_ui_startup(stopped, watcher, initial_error):
    try:
        stopped.set()
        watcher.join()
        # Also close connections if seeding failed before serve's cleanup block.
        if "django.db" in sys.modules:
            from django.db import connections
            connections.close_all()
    except BaseException as error:
        if initial_error is None:
            raise
        note = secondary_failure_note("UI finalization also failed", error)
        initial_error.add_note(note)
        logger.error("%s", note)


def watch_startup_cancellation(directory, stopped):
    while not stopped.wait(0.1):
        if (directory / "cancel-startup").exists():
            _thread.interrupt_main()
            return


if __name__ == "__main__":
    main()
