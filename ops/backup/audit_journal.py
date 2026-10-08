"""Protected, rotating metadata journal; executable without Django.

The operator chooses a directory outside database volumes. No free-form values,
paths, command arguments or exception messages are persisted.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


OPERATIONS = frozenset({
    "backup", "offsite_upload", "offsite_prune", "restore", "restore_check", "retention_cleanup",
    "daily_alerts", "seed_first_user", "evict_household_member", "delete_member_data",
    "publish_privacy_policy", "rebuild_transfer_pairs", "sync_simplefin", "generate_monthly_reviews",
})
PHASES = frozenset({"started", "succeeded", "failed"})
ACTORS = frozenset({"operator", "scheduler"})
GAP = "Audit write gap: operator journal could not be recorded"


def checksum(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def locked(directory):
    lock = directory / ".writer-lock"
    deadline = time.monotonic() + 2
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError("Journal lock unavailable") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.rmdir()


def append(operation, phase, run_id, *, actor="operator", directory=None, now=None):
    if operation not in OPERATIONS or phase not in PHASES or actor not in ACTORS:
        raise ValueError("Unsupported journal metadata")
    run_id = str(uuid.UUID(str(run_id)))
    now = now or datetime.now(timezone.utc)
    row = {"operation": operation, "outcome": phase, "correlation_id": run_id,
           "actor_kind": actor, "source": "job" if actor == "scheduler" else "cli",
           "occurred_at": now.isoformat()}
    row["checksum"] = checksum(row)
    # Stdout has no retention promise; persisted files are the review contract.
    try:
        directory = Path(directory or os.environ.get("OPERATOR_AUDIT_DIR", "/operator-audit"))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        with locked(directory):
            cutoff = (now - timedelta(days=90)).date().isoformat()
            for old in directory.glob("????-??-??.jsonl"):
                if old.stem < cutoff and not old.is_symlink():
                    old.unlink()
            path = directory / f"{now.date().isoformat()}.jsonl"
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags, 0o600)
            try:
                os.chmod(path, 0o600)
                line = (json.dumps(row, sort_keys=True) + "\n").encode()
                if os.write(fd, line) != len(line):
                    raise OSError("Incomplete journal append")
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError:
        print(GAP, file=sys.stderr)
        return False
    return True


def query(directory):
    """Read without rotation or repair; reject unexpected keys before displaying."""
    allowed = {"operation", "outcome", "correlation_id", "actor_kind", "source", "occurred_at", "checksum"}
    for path in sorted(Path(directory).glob("????-??-??.jsonl")):
        if path.is_symlink():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if set(row) != allowed or row["operation"] not in OPERATIONS or row["outcome"] not in PHASES:
                    raise ValueError
                if row["actor_kind"] not in ACTORS or row["source"] not in {"cli", "job"}:
                    raise ValueError
                uuid.UUID(row["correlation_id"])
                datetime.fromisoformat(row["occurred_at"])
                saved = row.pop("checksum")
                valid = saved == checksum(row)
                # Never echo malformed/unverified input from a damaged journal.
                if not valid:
                    raise ValueError
                yield {**row, "checksum_valid": valid}
            except (ValueError, TypeError, KeyError):
                yield {"warning": "Invalid journal record"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=sorted(OPERATIONS), nargs="?")
    parser.add_argument("phase", choices=sorted(PHASES), nargs="?")
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--actor", choices=sorted(ACTORS), default="operator")
    parser.add_argument("--directory", default=os.environ.get("OPERATOR_AUDIT_DIR", "/operator-audit"))
    parser.add_argument("--query", action="store_true")
    options = parser.parse_args()
    if options.query:
        for row in query(options.directory):
            print(json.dumps(row, sort_keys=True))
    elif options.operation and options.phase and options.run_id:
        append(options.operation, options.phase, options.run_id, actor=options.actor, directory=options.directory)
    else:
        parser.error("An operation, phase and UUID are required")


if __name__ == "__main__":
    main()
