"""Launch a disposable instance, check it, drive the main path, save evidence, clean up.

Usage (repo root, venv active):
    python .claude/skills/verify/scripts/verify.py [--feature NAME]

Evidence goes to .verify/<timestamp>-<feature>/ (git-ignored). Cleanup always
runs, stops only processes this script started (by recorded PID) and exits 1
when any step, or cleanup itself, could not be verified.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
for path in (ROOT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import doctor  # noqa: E402
from scripts.ui_instance import ENV_ALLOWLIST, disposable_directory  # noqa: E402

FEATURES = ("import", "cashflow", "recurring", "dashboard", "alert")
READY_TIMEOUT = 180  # A first run may build CSS before migrating and seeding.
DRIVE_TIMEOUT = 600
EXIT_TIMEOUT = 30
# Runtime essentials for Python, Node and Playwright browsers; nothing else is inherited.
EXTRA_ALLOWLIST = {"SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMDATA", "PATHEXT", "NUMBER_OF_PROCESSORS",
                   "PROCESSOR_ARCHITECTURE", "OS", "PLAYWRIGHT_BROWSERS_PATH", "CI", "XDG_CACHE_HOME",
                   "XDG_RUNTIME_DIR", "DISPLAY"}
LOG_SIGNAL = re.compile(r"Traceback|ERROR|CRITICAL|Exception|WARNING| 5\d\d ")
# Windows: a gate that cannot start the real command until it belongs to our job.
GATE = ("import subprocess,sys; token=sys.stdin.buffer.read(1); "
        "sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL) if token == b'1' else 1)")


class StepFailed(RuntimeError):
    pass


def scrubbed_environment(environ=None):
    environ = os.environ if environ is None else environ
    allowed = ENV_ALLOWLIST | EXTRA_ALLOWLIST
    return {key: value for key, value in environ.items() if key.upper() in allowed}


class OwnedTree:
    """A started process with every descendant: a process group on POSIX, a Job Object on Windows.

    Ownership outlives the parent, so a browser or worker left behind by an exited
    parent is still stopped and still counted when cleanup verifies.
    """

    def __init__(self, command, **options):
        self.job = None
        if os.name == "nt":
            from scripts.windows_css_job import WindowsCssJob
            self.job = WindowsCssJob()
            try:
                self.process = subprocess.Popen([sys._base_executable, "-c", GATE, *command], stdin=subprocess.PIPE,
                                                creationflags=subprocess.CREATE_NO_WINDOW, **options)
                self.job.assign(self.process.pid)
                self.process.stdin.write(b"1")
                self.process.stdin.close()
            except BaseException:
                self.job.terminate()
                self.job.close()
                raise
        else:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True, **options)
        self.pid = self.process.pid

    def running(self):
        return self.process.poll() is None

    def alive(self):
        self.process.poll()  # Reap our own child so it does not count as a group member.
        if self.job is not None:
            return self.job.active() > 0
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def stop(self):
        """Stop the whole tree, whether or not the parent has already exited."""
        if self.job is not None:
            if self.job.active():
                self.job.terminate()
        else:
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.alive() and self.job is None:
            os.killpg(self.pid, signal.SIGKILL)
        self.process.wait(timeout=10)

    def close(self):
        if self.job is not None:
            self.job.close()  # KILL_ON_JOB_CLOSE: nothing in the job survives this.
            self.job = None


class Ledger:
    """Everything this run started, written to ledger.json before it is used."""

    def __init__(self, evidence):
        self.evidence = evidence
        self.data = {"processes": {}, "instance": {}, "steps": {}}
        self.processes = {}
        self.save()

    def save(self):
        (self.evidence / "ledger.json").write_text(json.dumps(self.data, indent=2))

    def own(self, name, tree, **details):
        self.processes[name] = tree
        self.data["processes"][name] = {"pid": tree.pid, **details}
        self.save()

    def step(self, name, status, detail=""):
        self.data["steps"][name] = {"status": status, "detail": detail}
        self.save()


def launch(ledger, env):
    handle = ledger.evidence / "startup-handle.json"
    log = (ledger.evidence / "instance.log").open("w", encoding="utf-8")
    ledger.data["instance"]["handle"] = str(handle)
    try:
        tree = OwnedTree([sys.executable, "scripts/ui_instance.py", "--startup-handle", str(handle)],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=log)
    finally:
        log.close()  # The child holds its own handle.
    ledger.own("launcher", tree, log=str(ledger.evidence / "instance.log"))
    readiness = wait_for_readiness(tree.process)
    port = int(readiness["url"].rsplit(":", 1)[1].rstrip("/"))
    # The readiness line also carries the disposable sign-in; keep it out of evidence.
    ledger.data["instance"].update(url=readiness["url"], port=port, directory=readiness["directory"])
    ledger.save()
    return ledger.data["instance"]


def wait_for_readiness(process):
    lines = queue.Queue()

    def drain():
        # Keep reading after readiness so the launcher never blocks on stdout.
        for raw in process.stdout:
            lines.put(raw.decode("utf-8", errors="replace"))
        lines.put(None)

    threading.Thread(target=drain, daemon=True, name="verify-readiness").start()
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            continue
        if line is None:
            raise StepFailed("launcher exited before readiness; see instance.log")
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("url") and value.get("directory"):
            return value
    raise StepFailed(f"no readiness within {READY_TIMEOUT}s; see instance.log")


def run_doctor(ledger, env):
    instance = ledger.data["instance"]
    results = doctor.run_checks(instance["url"], instance["directory"], env)
    with (ledger.evidence / "doctor.txt").open("w", encoding="utf-8") as output:
        healthy = doctor.report(results, output)
    if not healthy:
        raise StepFailed("doctor reported failures; see doctor.txt")


def drive(ledger, env, feature):
    node = shutil.which("node", path=env.get("PATH"))
    cli = ROOT / "node_modules" / "@playwright" / "test" / "cli.js"
    if not node or not cli.exists():
        raise StepFailed("node and npm ci are required for the drive step (docs/ui-testing.md setup)")
    env = dict(env, VERIFY_URL=ledger.data["instance"]["url"], VERIFY_EVIDENCE=str(ledger.evidence),
               VERIFY_FEATURE=feature)
    with (ledger.evidence / "drive.log").open("w", encoding="utf-8") as log:
        tree = OwnedTree([node, str(cli), "test", "--config", str(HERE / "playwright.verify.config.ts")],
                         cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    ledger.own("drive", tree, log=str(ledger.evidence / "drive.log"))
    try:
        returncode = tree.process.wait(timeout=DRIVE_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise StepFailed(f"drive exceeded {DRIVE_TIMEOUT}s") from error
    if returncode:
        raise StepFailed(f"drive exited {returncode}; see drive.log and drive-results.json")


def port_free(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except OSError:
        return True


def instance_directory(ledger):
    directory = ledger.data["instance"].get("directory")
    handle = Path(ledger.data["instance"].get("handle", ledger.evidence / "startup-handle.json"))
    if not directory and handle.exists():
        directory = json.loads(handle.read_text())["directory"]
    return disposable_directory(directory) if directory else None


def cleanup(ledger):
    """Teardown per docs/ui-testing.md, stop owned trees and verify nothing remains.

    Each action is attempted independently: a failure is recorded as a FAIL check
    and the remaining teardown, stops and verification still run.
    """
    checks = []

    def attempt(name, action):
        try:
            return action()
        except Exception as error:
            checks.append((name, False, f"{type(error).__name__}: {error}"))
            return None

    for name, tree in ledger.processes.items():
        if name != "launcher":
            attempt(f"stop {name} tree", tree.stop)
    directory = attempt("instance directory lookup", lambda: instance_directory(ledger))
    launcher = ledger.processes.get("launcher")
    if directory and directory.exists() and launcher and launcher.running():
        attempt("teardown request", lambda: request_teardown(directory, checks))
    if launcher:
        attempt("launcher exit", lambda: await_launcher(launcher, checks))
    if directory:
        attempt("instance directory removal", lambda: await_directory(ledger, directory, checks))
    for name, tree in ledger.processes.items():
        alive = attempt(f"{name} tree query", tree.alive)
        checks.append((f"{name} process tree stopped", alive is False, f"PID {tree.pid} and descendants"))
        attempt(f"{name} job close", tree.close)
    port = ledger.data["instance"].get("port")
    if port:
        checks.append(("port free", port_free(port), f"127.0.0.1:{port}"))
    handle = ledger.evidence / "startup-handle.json"
    if handle.exists() and not (directory and directory.exists()):
        attempt("startup handle removal", handle.unlink)
    ledger.data["cleanup"] = [{"check": name, "ok": ok, "detail": detail} for name, ok, detail in checks]
    ledger.save()
    return checks


def request_teardown(directory, checks):
    result = subprocess.run([sys.executable, "scripts/ui_instance.py", "--teardown", str(directory)],
                            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    checks.append(("teardown request", result.returncode == 0,
                   "accepted" if result.returncode == 0 else f"exit {result.returncode}"))


def await_launcher(launcher, checks):
    try:
        launcher.process.wait(timeout=EXIT_TIMEOUT)
        forced = False
    except subprocess.TimeoutExpired:
        forced = True
    if forced or launcher.alive():
        launcher.stop()
    checks.append(("launcher exited", not forced,
                   f"exit {launcher.process.returncode}" + (" after forced stop" if forced else "")))
    if launcher.process.stdout:
        launcher.process.stdout.close()


def await_directory(ledger, directory, checks):
    deadline = time.monotonic() + 15
    while directory.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    retained = directory.exists() and (directory / "css-cleanup-failed.json").exists()
    if directory.exists() and not retained and not any(tree.alive() for tree in ledger.processes.values()):
        # Forced recovery: every owned tree is confirmed stopped, so remove the exact directory.
        shutil.rmtree(directory, ignore_errors=True)
        checks.append(("forced directory removal", False, "launcher had not removed its storage"))
    checks.append(("instance directory removed", not directory.exists(),
                   "gone" if not directory.exists() else "retained; see docs/ui-testing.md Teardown"))


def log_excerpts(evidence):
    log = evidence / "instance.log"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    flagged = [line for line in lines if LOG_SIGNAL.search(line)]
    excerpt = ["# Flagged lines", *flagged[-50:], "", "# Last 30 lines", *lines[-30:]]
    (evidence / "log-excerpts.txt").write_text("\n".join(excerpt) + "\n", encoding="utf-8")
    return len(flagged)


def drive_results(evidence):
    report = evidence / "drive-results.json"
    if not report.exists():
        return []
    results = []

    def walk(suite):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                status = test["results"][-1]["status"] if test.get("results") else "not run"
                results.append((test.get("projectName", ""), spec["title"], status))
        for child in suite.get("suites", []):
            walk(child)

    for suite in json.loads(report.read_text(encoding="utf-8")).get("suites", []):
        walk(suite)
    return results


def write_summary(ledger, feature, checks):
    evidence = ledger.evidence
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True, check=False).stdout.strip() or "unknown"
    flagged = log_excerpts(evidence)
    lines = [f"# Verify run {evidence.name}", "",
             f"- Commit: `{commit}`", f"- Feature: `{feature}`",
             f"- Instance: {ledger.data['instance'].get('url', 'not ready')} (disposable SQLite, synthetic seed)", "",
             "## Steps", "", "| Step | Status | Detail |", "| --- | --- | --- |"]
    lines += [f"| {name} | {step['status']} | {step['detail']} |" for name, step in ledger.data["steps"].items()]
    doctor_output = evidence / "doctor.txt"
    if doctor_output.exists():
        lines += ["", "## Doctor", "", "```", doctor_output.read_text(encoding="utf-8").strip(), "```"]
    results = drive_results(evidence)
    if results:
        lines += ["", "## Drive", "", "| Project | Check | Result |", "| --- | --- | --- |"]
        lines += [f"| {project} | {title} | {status} |" for project, title, status in results]
    screenshots = sorted((evidence / "screenshots").glob("*.png")) if (evidence / "screenshots").exists() else []
    if screenshots:
        lines += ["", "## Screenshots", ""] + [f"- `screenshots/{shot.name}`" for shot in screenshots]
    lines += ["", "## Cleanup", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    lines += [f"| {name} | {'pass' if ok else 'FAIL'} | {detail} |" for name, ok, detail in checks]
    lines += ["", f"Log: {flagged} flagged line(s) in `log-excerpts.txt`; full server log in `instance.log`.", ""]
    (evidence / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def run(feature="all", evidence_root=None):
    evidence_root = Path(evidence_root or ROOT / ".verify")
    evidence = evidence_root / f"{datetime.now():%Y%m%d-%H%M%S}-{feature}"
    evidence.mkdir(parents=True)
    ledger = Ledger(evidence)
    env = scrubbed_environment()
    current = "launch"
    try:
        for current, action in (("launch", lambda: launch(ledger, env)),
                                ("doctor", lambda: run_doctor(ledger, env)),
                                ("drive", lambda: drive(ledger, env, feature))):
            action()
            ledger.step(current, "pass")
    except Exception as error:  # Record any failure; cleanup must still run.
        ledger.step(current, "FAIL", str(error) or type(error).__name__)
    finally:
        checks = cleanup(ledger)
        write_summary(ledger, feature, checks)
    passed = all(step["status"] == "pass" for step in ledger.data["steps"].values())
    clean = all(ok for _, ok, _ in checks)
    print(f"Evidence: {evidence}")
    print(f"Steps {'passed' if passed else 'FAILED'}; cleanup {'verified' if clean else 'FAILED'}.")
    return 0 if passed and clean else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--feature", choices=("all", *FEATURES), default="all",
                        help="drive only one area of the feature map")
    parser.add_argument("--evidence-root", type=Path, help="default: .verify/ in the repo root")
    args = parser.parse_args(argv)
    return run(args.feature, args.evidence_root)


if __name__ == "__main__":
    sys.exit(main())
