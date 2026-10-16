#!/usr/bin/env python3
"""Read-only production signals for docs/production-signals.md and ops/post-deploy-check.ps1.

Both subcommands print one JSON object of counts, types, states and timestamps.
Log lines, error text, amounts and names never reach the output.

    python scripts/prod_signals.py tracebacks --container ID --since 24h
    python scripts/prod_signals.py tracebacks --file synthetic.log
    python scripts/prod_signals.py backup-status E:/financial-planner-backups/health/status

It needs only the standard library, so it runs on the Windows host without the
app's dependencies.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRACEBACK_HEADER = re.compile(r"Traceback \(most recent call last\):\s*$")
# Inside an exception group every line carries a "| " margin; drop it so the
# group's own exception line reads like a plain one.
GROUP_MARGIN = re.compile(r"^\s*\|\s?")
EXCEPTION_TYPE = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?::|$)")
MAX_TYPE_LENGTH = 120
UNKNOWN = "unknown"

# The keys ops/backup/backup.sh writes; finance/backup_health.py reads the same file.
STATUS_KEYS = (
    "last_success_at",
    "dump_name",
    "size_bytes",
    "table_count",
    "last_error",
    "offsite_success_at",
    "offsite_error",
    "offsite_configured",
    "restore_check_at",
    "restore_check_error",
    "restore_check_interval_days",
)
# The same limits as finance/backup_health.py, which raises the in-app alert.
STALE_AFTER = timedelta(hours=26)
RESTORE_CHECK_GRACE = timedelta(days=1)
DOCKER_LOGS_TIMEOUT_SECONDS = 120


def _exception_type(line):
    match = EXCEPTION_TYPE.match(line)
    if match is None or len(match.group(1)) > MAX_TYPE_LENGTH:
        return UNKNOWN
    return match.group(1)


def count_tracebacks(lines):
    """Count Python tracebacks by exception type.

    A traceback starts at its "Traceback (most recent call last):" line. Its
    frames are indented, and the first line that is not names the exception.
    A traceback cut off by the end of the log or by another header, or ended by
    a line that is not an exception, counts as "unknown".
    """
    types = {}
    open_traceback = False
    for raw in lines:
        line = GROUP_MARGIN.sub("", raw.rstrip("\r\n"))
        if TRACEBACK_HEADER.search(line):
            if open_traceback:
                types[UNKNOWN] = types.get(UNKNOWN, 0) + 1
            open_traceback = True
            continue
        if not open_traceback or not line.strip() or line[0].isspace():
            continue
        name = _exception_type(line)
        types[name] = types.get(name, 0) + 1
        open_traceback = False
    if open_traceback:
        types[UNKNOWN] = types.get(UNKNOWN, 0) + 1
    return {"tracebacks": sum(types.values()), "types": dict(sorted(types.items()))}


def docker_log_lines(container, since):
    """Both output streams of one container since a time docker accepts (24h or RFC 3339)."""
    result = subprocess.run(
        ["docker", "logs", "--since", since, container],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=DOCKER_LOGS_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        # Docker's message would echo what it read, so report only the failure.
        raise RuntimeError(f"docker logs exited {result.returncode}")
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def _parse_time(value):
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_status_text(text):
    data = dict.fromkeys(STATUS_KEYS, "")
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in data:
            data[key.strip()] = value.strip()
    return data


def _stale_reasons(data, now):
    reasons = []
    last_success = _parse_time(data["last_success_at"])
    if last_success is None or now - last_success > STALE_AFTER:
        reasons.append("last_success")
    if data["offsite_configured"] in {"1", "true", "yes"}:
        offsite = _parse_time(data["offsite_success_at"])
        if offsite is None or now - offsite > STALE_AFTER:
            reasons.append("offsite")
    interval = data["restore_check_interval_days"]
    restore_check = _parse_time(data["restore_check_at"])
    # Never checked is not overdue, as in finance/backup_health.py.
    if interval.isdigit() and int(interval) and restore_check is not None:
        if now - restore_check > timedelta(days=int(interval)) + RESTORE_CHECK_GRACE:
            reasons.append("restore_check")
    return reasons


def backup_state(path, *, now=None):
    """Summarize the backup status file without its error text.

    The state is "missing" when the file cannot be read, "error" when any run
    recorded an error, "stale" when a success is older than the in-app alert
    allows, and "ok" otherwise.
    """
    now = now or datetime.now(timezone.utc)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {"state": "missing"}
    data = parse_status_text(text)
    errors = [key for key in ("last_error", "offsite_error", "restore_check_error") if data[key]]
    stale = _stale_reasons(data, now)
    if errors:
        state = "error"
    elif stale:
        state = "stale"
    else:
        state = "ok"
    return {
        "state": state,
        "errors": errors,
        "stale": stale,
        "last_success_at": data["last_success_at"] or None,
        "restore_check_at": data["restore_check_at"] or None,
        "offsite_configured": data["offsite_configured"] in {"1", "true", "yes"},
    }


def _parser():
    parser = argparse.ArgumentParser(description="Read-only production signals as JSON.")
    commands = parser.add_subparsers(dest="command", required=True)
    tracebacks = commands.add_parser("tracebacks", help="count Python tracebacks by exception type")
    source = tracebacks.add_mutually_exclusive_group(required=True)
    source.add_argument("--container", help="container name or id to read with docker logs")
    source.add_argument("--file", help="a log file to read instead, or - for standard input")
    tracebacks.add_argument("--since", default="24h", help="docker logs --since value (default 24h)")
    status = commands.add_parser("backup-status", help="summarize the backup health status file")
    status.add_argument("path")
    status.add_argument("--now", help="evaluate at this ISO time instead of now")
    return parser


def _read_lines(args):
    if args.container:
        return docker_log_lines(args.container, args.since)
    if args.file == "-":
        return sys.stdin.read().splitlines()
    return Path(args.file).read_text(encoding="utf-8", errors="replace").splitlines()


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "tracebacks":
        try:
            lines = _read_lines(args)
        except RuntimeError as error:
            print(f"cannot read logs: {error}", file=sys.stderr)
            return 2
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"cannot read logs: {type(error).__name__}", file=sys.stderr)
            return 2
        result = count_tracebacks(lines)
    else:
        now = _parse_time(args.now) if args.now else None
        result = backup_state(args.path, now=now)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
