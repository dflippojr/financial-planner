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
GROUP: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
         if os.name == "nt" else {"start_new_session": True})


class StepFailed(RuntimeError):
    pass


def scrubbed_environment(environ=None):
    environ = os.environ if environ is None else environ
    allowed = ENV_ALLOWLIST | EXTRA_ALLOWLIST
    return {key: value for key, value in environ.items() if key.upper() in allowed}


class Ledger:
    """Everything this run started, written to ledger.json before it is used."""

    def __init__(self, evidence):
        self.evidence = evidence
        self.data = {"processes": {}, "instance": {}, "steps": {}}
        self.processes = {}
        self.save()

    def save(self):
        (self.evidence / "ledger.json").write_text(json.dumps(self.data, indent=2))

    def own(self, name, process, **details):
        self.processes[name] = process
        self.data["processes"][name] = {"pid": process.pid, **details}
        self.save()

    def step(self, name, status, detail=""):
        self.data["steps"][name] = {"status": status, "detail": detail}
        self.save()


def launch(ledger, env):
    handle = ledger.evidence / "startup-handle.json"
    log = (ledger.evidence / "instance.log").open("w", encoding="utf-8")
    ledger.data["instance"]["handle"] = str(handle)
    try:
        process = subprocess.Popen(
            [sys.executable, "scripts/ui_instance.py", "--startup-handle", str(handle)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
            text=True, encoding="utf-8", **GROUP)
    finally:
        log.close()  # The child holds its own handle.
    ledger.own("launcher", process, log=str(ledger.evidence / "instance.log"))
    readiness = wait_for_readiness(process)
    port = int(readiness["url"].rsplit(":", 1)[1].rstrip("/"))
    # The readiness line also carries the disposable sign-in; keep it out of evidence.
    ledger.data["instance"].update(url=readiness["url"], port=port, directory=readiness["directory"])
    ledger.save()
    return ledger.data["instance"]


def wait_for_readiness(process):
    lines = queue.Queue()

    def drain():
        # Keep reading after readiness so the launcher never blocks on stdout.
        for line in process.stdout:
            lines.put(line)
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
        process = subprocess.Popen([node, str(cli), "test", "--config", str(HERE / "playwright.verify.config.ts")],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, **GROUP)
    ledger.own("drive", process, log=str(ledger.evidence / "drive.log"))
    try:
        returncode = process.wait(timeout=DRIVE_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise StepFailed(f"drive exceeded {DRIVE_TIMEOUT}s") from error
    if returncode:
        raise StepFailed(f"drive exited {returncode}; see drive.log and drive-results.json")


def stop_tree(process):
    """Stop a process this run started, with its descendants, by its recorded PID."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


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
    """Teardown per docs/ui-testing.md, stop owned processes and verify nothing remains."""
    checks = []
    for name, process in ledger.processes.items():
        if name != "launcher" and process.poll() is None:
            stop_tree(process)
    directory = instance_directory(ledger)
    launcher = ledger.processes.get("launcher")
    if directory and directory.exists() and launcher and launcher.poll() is None:
        result = subprocess.run([sys.executable, "scripts/ui_instance.py", "--teardown", str(directory)],
                                cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
        checks.append(("teardown request", result.returncode == 0,
                       "accepted" if result.returncode == 0 else f"exit {result.returncode}"))
    if launcher:
        try:
            launcher.wait(timeout=EXIT_TIMEOUT)
            forced = False
        except subprocess.TimeoutExpired:
            stop_tree(launcher)
            forced = True
        checks.append(("launcher exited", not forced,
                       f"exit {launcher.returncode}" + (" after forced stop" if forced else "")))
        if launcher.stdout:
            launcher.stdout.close()
    if directory:
        deadline = time.monotonic() + 15
        while directory.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        retained = directory.exists() and (directory / "css-cleanup-failed.json").exists()
        if directory.exists() and not retained and all(p.poll() is not None for p in ledger.processes.values()):
            # Forced recovery: the owned launcher is confirmed stopped, so remove the exact directory.
            shutil.rmtree(directory, ignore_errors=True)
            checks.append(("forced directory removal", False, "launcher had not removed its storage"))
        checks.append(("instance directory removed", not directory.exists(),
                       "gone" if not directory.exists() else "retained; see docs/ui-testing.md Teardown"))
    for name, process in ledger.processes.items():
        checks.append((f"{name} process stopped", process.poll() is not None, f"PID {process.pid}"))
    port = ledger.data["instance"].get("port")
    if port:
        checks.append(("port free", port_free(port), f"127.0.0.1:{port}"))
    handle = ledger.evidence / "startup-handle.json"
    if handle.exists() and not (directory and directory.exists()):
        handle.unlink()
    ledger.data["cleanup"] = [{"check": name, "ok": ok, "detail": detail} for name, ok, detail in checks]
    ledger.save()
    return checks


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
