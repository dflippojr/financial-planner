#!/usr/bin/env python3
"""Read-only Linux post-deploy checks; stdlib only, Python 3.10+.

Print states, counts, exception types and SHAs, never logs or exception messages.
See docs/deployment.md for recording events before deploying. The retained .ps1
implementation is retired only after the owner's first real Linux deploy.
"""
import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

SERVICES = ("db", "app", "backup", "background")
ONE_SHOT_SERVICES = ("config-check",)
BUILT_SERVICES = ("app", "backup", "background", "config-check")
REVISION_LABEL = "org.opencontainers.image.revision"
EVENT_FORMAT = "{{.TimeNano}} {{.Action}} {{.Actor.ID}}"
REPO_ROOT = Path(__file__).resolve().parents[1]
TIME_PATTERN = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)")

# Import by path so copied rollback tools work from any working directory too.
_spec = importlib.util.spec_from_file_location("prod_signals", REPO_ROOT / "scripts/prod_signals.py")
signals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(signals)


class CheckError(Exception):
    """A fixed, public diagnostic authored by this script."""


def native(*args):
    """Capture native output; never send stderr (which can carry data) to the terminal."""
    result = subprocess.run(  # noqa: S603 - fixed executables and argv, no shell
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=120, check=False)
    if result.returncode:
        raise CheckError(f"{args[0]} {args[1]} failed")
    return result.stdout


def parse_time(value):
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def unix_nanoseconds(value):
    """Avoid float timestamps and datetime's truncation past microseconds."""
    match = TIME_PATTERN.fullmatch(value or "")
    if not match:
        return None
    whole, fraction, zone = match.groups()
    parsed = parse_time(whole + zone)
    if parsed is None:
        return None
    delta = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + int((fraction or "").ljust(9, "0"))


def env_value(path, key):
    """Read only the requested key; later assignments override earlier ones."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    value = None
    for line in lines:
        match = re.match(r"\s*(?:export\s+)?" + re.escape(key) + r"\s*=(.*)$", line)
        if not match:
            continue
        raw = match[1].strip()
        single = re.match(r"'([^']*)'", raw)
        double = re.match(r'"((?:[^"\\]|\\.)*)"', raw)
        if single:
            value = single[1]
        elif double:
            value = re.sub(r'\\(["\\])', r'\1', double[1])
        else:
            value = re.sub(r"\s+#.*$", "", raw).strip()
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Checker:
    def __init__(self, args):
        self.args = args
        self.failures = []
        self.health_unreachable = False

    def fail(self, reason):
        self.failures.append(reason)
        print(f"FAIL {reason}")

    @staticmethod
    def ok(message):
        print(f"ok   {message}")

    def snapshot(self):
        snapshot = {}
        for service in SERVICES + ONE_SHOT_SERVICES:
            ids = native("docker", "ps", "-a", "-q", "--no-trunc",
                         "--filter", f"label=com.docker.compose.project={self.args.project_name}",
                         "--filter", f"label=com.docker.compose.service={service}",
                         "--filter", "label=com.docker.compose.oneoff=False").split()
            snapshot[service] = []
            for cid in ids:
                info = json.loads(native("docker", "inspect", cid))[0]
                state = info["State"]
                snapshot[service].append({
                    "service": service, "id": cid, "status": state["Status"],
                    "restart_count": int(info["RestartCount"]),
                    "started_at": state["StartedAt"],
                    "started": parse_time(state["StartedAt"]),
                    "started_ns": unix_nanoseconds(state["StartedAt"]),
                    "finished_at": state.get("FinishedAt"),
                    "created": parse_time(info["Created"]),
                    "revision": (info["Config"].get("Labels") or {}).get(REVISION_LABEL),
                })
        return snapshot

    def health(self, label="health"):
        request = Request(self.args.health_url, headers={"X-Forwarded-Proto": "https"})  # noqa: S310 - run() restricts to HTTP(S)
        try:
            try:
                response = build_opener(NoRedirect()).open(request, timeout=10)  # noqa: S310 - operator-supplied HTTP(S) URL
            except HTTPError as error:
                response = error
            with response:
                code = response.code
                revision = response.headers.get("X-App-Revision")
        except OSError as error:
            self.fail(f"{label}: unreachable ({type(error).__name__})")
            self.health_unreachable = True
            return None
        if code == 200:
            self.ok(f"{label}: 200")
        else:
            self.fail(f"{label}: returned {code}, expected 200")
        return revision

    def expected_revision(self):
        expected = self.args.expected_sha
        if expected is None:
            try:
                expected = native("git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "origin/main").strip()
            except (CheckError, OSError):
                raise CheckError("revision: cannot read origin/main; run git fetch origin or pass --expected-sha") from None
        expected = expected.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,40}", expected):
            raise CheckError("revision: --expected-sha must be 7 to 40 hex digits")
        return expected

    def health_revision(self, served, expected):
        if self.health_unreachable:
            return  # Already reported; still check image labels.
        if not served:
            self.fail("revision: /health/ sent no X-App-Revision header")
        elif served == "unknown":
            self.fail("revision: the app image has no GIT_SHA; rebuild with GIT_SHA set")
        elif not re.fullmatch(r"[0-9a-fA-F]{7,40}", served):
            self.fail("revision: /health/ sent an invalid X-App-Revision header")
        elif not (expected.startswith(served.lower()) or served.lower().startswith(expected)):
            self.fail(f"revision: /health/ serves {served}, expected {expected[:12]}")
        else:
            self.ok(f"revision: /health/ serves {served}")

    def revision(self, served, snapshot):
        try:
            expected = self.expected_revision()
        except CheckError as error:
            self.fail(str(error))
            return
        self.health_revision(served, expected)
        for service in BUILT_SERVICES:
            for container in snapshot[service]:
                label = (container["revision"] or "none").lower()
                if not label.startswith(expected):
                    shown = label[:12] if re.fullmatch(r"[0-9a-f]{7,40}", label) else "none"
                    self.fail(f"revision: {service} runs an image built from {shown}, expected {expected[:12]}; recreate it")

    def container_problem(self, now, then, deploy_start):
        if now["status"] != "running":
            return f"is {now['status']}, expected running"
        if then is None:
            return f"was replaced during the {self.args.stable_seconds}s window"
        if now["restart_count"] != then["restart_count"] or now["started_at"] != then["started_at"]:
            return f"restarted during the {self.args.stable_seconds}s window (RestartCount {now['restart_count']})"
        if now["created"] and now["created"] >= deploy_start and now["restart_count"] > 0:
            return f"restarted {now['restart_count']} time(s) since the deploy"
        if (now["created"] and now["created"] < deploy_start and now["started"]
                and now["started"] >= deploy_start
                and ((now["started"] - now["created"]).total_seconds() > 60 or now["restart_count"] > 0)):
            return "was kept by the deploy but started again since it began"
        return None

    def containers(self, before, after, deploy_start):
        for service in SERVICES:
            first = {c["id"]: c for c in before[service]}
            last = after[service]
            if not last:
                self.fail(f"containers: {service} has no container in project {self.args.project_name}")
                continue
            healthy = True
            for now in last:
                reason = self.container_problem(now, first.get(now["id"]), deploy_start)
                if reason:
                    self.fail(f"containers: {service} {reason}")
                    healthy = False
            if healthy:
                self.ok(f"containers: {service} running, RestartCount {','.join(str(c['restart_count']) for c in last)}")

    def check_finished(self, container):
        if container["status"] != "running":
            return False
        finished = container["finished_at"]
        service = container["service"]
        if finished is None:
            self.fail(f"containers: cannot read {service}'s FinishedAt to tell whether it stopped since the deploy")
            return True
        if not re.fullmatch(r"0001-01-01T00:00:00(?:\.0+)?Z", finished):
            self.fail(f"containers: {service} stopped and started again since it was created (FinishedAt is set)")
            return True
        return False

    def event_lines(self, new, until):
        if self.args.event_log:
            try:
                return Path(self.args.event_log).read_text(encoding="utf-8").splitlines()
            except OSError:
                raise CheckError("containers: no event log at --event-log to count starts since the deploy; record one from before up -d") from None
        since = min(c["created"] for c in new) - timedelta(seconds=1)
        try:
            return native("docker", "events", "--since", f"{since.timestamp():.3f}",
                          "--until", f"{until.timestamp():.3f}", "--filter", "type=container",
                          "--filter", f"label=com.docker.compose.project={self.args.project_name}",
                          "--filter", "event=create", "--filter", "event=start", "--format", EVENT_FORMAT).splitlines()
        except (CheckError, OSError):
            raise CheckError("containers: cannot read docker events to count starts since the deploy") from None

    @staticmethod
    def parse_events(lines, source):
        parsed = []
        for line in lines:
            if not line.strip():
                continue
            match = re.fullmatch(r"(\d{1,19}) (create|start) (\S+)", line.strip())
            if not match:
                raise CheckError(f"containers: {source} has a line that is not '<time in ns> <action> <container id>'; record it with the format in docs/deployment.md")
            parsed.append((int(match[1]), match[2], match[3]))
        return parsed

    def check_starts(self, container, mine, source, started_again):
        service = container["service"]
        if not any(action == "create" for _, action in mine):
            if self.args.event_log:
                self.fail(f"containers: the event log does not reach the {service} deploy; start the recorder before up -d")
            else:
                self.fail(f"containers: Docker's event history no longer reaches the {service} deploy; record the deploy's events and pass --event-log, as docs/deployment.md does")
            return
        starts = sorted(ns for ns, action in mine if action == "start")
        hint = "; keep the recorder running until the check ends" if self.args.event_log else ""
        if len(starts) > 1:
            self.fail(f"containers: {service} started {len(starts)} times between the deploy and the check")
        elif not starts:
            if container["status"] == "running":
                self.fail(f"containers: {source} has no start for {service}, which is running{hint}")
        elif container["started_ns"] is None or starts[-1] < container["started_ns"]:
            self.fail(f"containers: {source} missed a start of {service}: its last recorded start is older than StartedAt{hint}")
        elif container["id"] not in started_again:
            self.ok(f"containers: {service} has one start in {source} since the deploy")

    def start_counts(self, snapshot, deploy_start, until):
        new = [c for s in SERVICES for c in snapshot[s] if c["created"] and c["created"] >= deploy_start]
        if not new:
            return
        started_again = {c["id"] for c in new if self.check_finished(c)}
        source = "the event log" if self.args.event_log else "docker events"
        try:
            parsed = self.parse_events(self.event_lines(new, until), source)
        except CheckError as error:
            self.fail(str(error))
            return
        for container in new:
            mine = [(ns, action) for ns, action, cid in parsed if cid == container["id"]]
            self.check_starts(container, mine, source, started_again)

    def tracebacks(self, snapshot, deploy_start):
        since = deploy_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for service in SERVICES:
            for container in snapshot[service]:
                try:
                    counts = signals.count_tracebacks(signals.docker_log_lines(container["id"], since))
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                    self.fail(f"tracebacks: cannot count {service} logs ({type(error).__name__})")
                    continue
                if counts["tracebacks"] == 0:
                    self.ok(f"tracebacks: {service} has none since {since}")
                else:
                    types = ", ".join(f"{name} x{count}" for name, count in counts["types"].items())
                    self.fail(f"tracebacks: {service} logged {counts['tracebacks']} since {since} ({types})")

    def backup(self):
        status = signals.backup_state(self.args.status_path)
        state = status["state"]
        if state == "missing":
            self.fail("backup: no status file; run a backup before relying on this deploy")
        elif state == "error":
            self.fail(f"backup: status is error ({', '.join(status['errors'])})")
        elif state == "stale":
            print(f"warn backup: status is stale ({', '.join(status['stale'])}); last success {status['last_success_at']}")
        else:
            self.ok(f"backup: status ok, last success {status['last_success_at']}")

    def configure_paths(self):
        args = self.args
        if not args.health_url:
            origins = env_value(args.config_path, "DJANGO_CSRF_TRUSTED_ORIGINS")
            if not origins:
                raise CheckError("pass --health-url or a --config-path with DJANGO_CSRF_TRUSTED_ORIGINS")
            args.health_url = origins.split(",")[0].strip().rstrip("/") + "/health/"
        if not args.health_url.startswith(("http://", "https://")):
            raise CheckError("--health-url must use HTTP or HTTPS")
        if not args.status_path:
            backup_dir = env_value(args.config_path, "BACKUP_DIR")
            if not backup_dir:
                raise CheckError("pass --status-path or a --config-path with BACKUP_DIR")
            args.status_path = str(Path(backup_dir) / "health/status")

    def run(self):
        args = self.args
        # An explicitly empty SHA must never fall back to origin/main.
        if args.expected_sha is not None and not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.expected_sha.strip()):
            self.fail("revision: --expected-sha must be 7 to 40 hex digits (an empty value is rejected)")
            return 1
        try:
            self.configure_paths()
            before = self.snapshot()
            until = datetime.now(timezone.utc)
            deploy_start = args.since
            if deploy_start is None:
                created = [c["created"] for s in ("app", "background") for c in before[s] if c["created"]]
                if not created:
                    raise CheckError("no app or background container to date the deploy; pass --since")
                deploy_start = min(created) - timedelta(seconds=5)
            served = self.health()
            if args.skip_revision:
                print("skip revision: --skip-revision given")
            else:
                self.revision(served, before)
            if args.stable_seconds > 0:
                print(f"wait {args.stable_seconds} s for restarts", flush=True)
                time.sleep(args.stable_seconds)
                self.health("health after wait")
            after = self.snapshot()
            self.containers(before, after, deploy_start)
            self.start_counts(before, deploy_start, until)
            self.tracebacks(after, deploy_start)
            self.backup()
        except Exception as error:
            # Only fixed, script-authored diagnostics may expose their messages.
            self.fail(f"check: {error if isinstance(error, CheckError) else type(error).__name__}")
        if self.failures:
            print(f"post-deploy check FAILED: {len(self.failures)} problem(s). Roll back per docs/deployment.md.")
            return 1
        print("post-deploy check passed")
        return 0


def since_arg(value):
    parsed = parse_time(value)
    if parsed is None:
        raise argparse.ArgumentTypeError("use an RFC 3339 time with a timezone")
    return parsed


def stable_arg(value):
    seconds = int(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return seconds


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config-path", default="/path/to/financial-planner-config/production.env")
    result.add_argument("--project-name", default="financial-planner")
    result.add_argument("--health-url")
    result.add_argument("--expected-sha", help="7–40 hex digits; defaults to origin/main (fetch first)")
    result.add_argument("--since", type=since_arg, help="deploy start in RFC 3339, e.g. 2026-11-24T12:00:00Z")
    result.add_argument("--stable-seconds", type=stable_arg, default=120)
    result.add_argument("--status-path", help="defaults to BACKUP_DIR/health/status")
    result.add_argument("--event-log", help="docker events stream recorded before up -d")
    result.add_argument("--skip-revision", action="store_true", help="only for rollback to images older than #537")
    return result


def main(argv=None):
    return Checker(parser().parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
