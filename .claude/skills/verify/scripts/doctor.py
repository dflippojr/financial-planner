"""Check that a disposable UI instance is ready, loopback-only, seeded and synthetic.

Usage (repo root, venv active):
    python .claude/skills/verify/scripts/doctor.py --ledger .verify/<run>/ledger.json
    python .claude/skills/verify/scripts/doctor.py --url http://127.0.0.1:PORT --directory <readiness directory>

Prints one PASS/FAIL line per check and exits 1 when any check fails. Output
names environment variables but never prints their values.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ui_instance import disposable_directory  # noqa: E402

# Names that carry credentials or point at live services. The launcher already
# drops them from the server environment; the skill launches from a scrubbed
# environment, so their presence means the run is not isolated as documented.
CREDENTIAL_NAME = re.compile(
    r"SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|"
    r"^(DATABASE|POSTGRES_|PG|DB_|SMTP_|EMAIL_HOST|GOOGLE_|SIMPLEFIN|PLAID|ANTHROPIC|OPENAI|AWS_|AGENT_HARNESS)",
    re.IGNORECASE,
)
# Storage settings the launcher sets itself; an inherited value could name real data.
DATA_PATH_NAMES = {"RECEIPTS_DIR", "CSV_IMPORT_STAGING_DIR", "OPERATOR_AUDIT_DIR", "PRIVACY_POLICY_PATH",
                   "BACKUP_DIR", "FINANCIAL_PLANNER_UI_DIR", "DJANGO_SETTINGS_MODULE"}
SEED_QUERIES = {
    "synthetic_alex user": "SELECT COUNT(*) FROM auth_user WHERE username = 'synthetic_alex'",
    "Synthetic Checking account": "SELECT COUNT(*) FROM finance_account WHERE name = 'Synthetic Checking'",
    "transactions": "SELECT COUNT(*) FROM finance_transaction",
    "alerts": "SELECT COUNT(*) FROM finance_alert",
}


def check_ready(url):
    try:
        with build_opener(ProxyHandler({})).open(url.rstrip("/") + "/health/", timeout=5) as response:
            body = response.read(64).decode(errors="replace").strip()
            return response.status == 200 and body == "ok", f"/health/ returned {response.status} {body!r}"
    except HTTPError as error:
        return False, f"/health/ returned {error.code}"
    except (URLError, OSError) as error:
        return False, f"/health/ unreachable ({type(error).__name__})"


def proc_address(field):
    host, port = field.split(":")
    raw = bytes.fromhex(host)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1]), int(port, 16)
    # /proc stores IPv6 as four host-order (little-endian) 32-bit words.
    words = b"".join(raw[index:index + 4][::-1] for index in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, words), int(port, 16)


def listener_addresses(port):
    """Local addresses with a TCP listener on port, from the OS socket table.

    Reading listeners rather than probing interfaces also catches a wildcard
    bind (0.0.0.0 or ::) that no hostname lookup would reveal.
    """
    found = set()
    tables = [Path("/proc/net/tcp"), Path("/proc/net/tcp6")]
    if tables[0].exists():
        for table in tables:
            if not table.exists():
                continue
            for line in table.read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[3] == "0A":  # TCP_LISTEN
                    address, listening = proc_address(fields[1])
                    if listening == port:
                        found.add(address)
        return found
    output = subprocess.run(["netstat", "-an"], capture_output=True, text=True, timeout=15, check=False).stdout
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[0].upper().startswith("TCP") and fields[-1].startswith("LISTEN"):
            address, _, listening = fields[1].rpartition(":")
            if listening.isdigit() and int(listening) == port:
                found.add(address.strip("[]"))
    return found


def check_loopback(url, directory):
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port:
        return False, f"URL host must be http://127.0.0.1:<port>, got {parts.hostname}"
    try:
        state = json.loads((Path(directory) / "instance.json").read_text())
    except (OSError, ValueError) as error:
        return False, f"instance.json unreadable ({type(error).__name__})"
    if int(state.get("port", 0)) != parts.port:
        return False, f"URL port {parts.port} does not match the instance port"
    listeners = listener_addresses(parts.port)
    if not listeners:
        return False, f"no TCP listener found on port {parts.port}"
    exposed = sorted(listeners - {"127.0.0.1"})
    if exposed:
        return False, f"port {parts.port} also listens on {', '.join(exposed)}"
    return True, f"127.0.0.1:{parts.port} only"


def check_storage(directory):
    try:
        resolved = disposable_directory(directory)
    except RuntimeError:
        return False, "instance directory is not a disposable system-temp directory"
    if not (resolved / "db.sqlite3").is_file():
        return False, "db.sqlite3 missing from the instance directory"
    return True, "SQLite inside the disposable directory"


def check_seed(directory):
    database = Path(directory) / "db.sqlite3"
    if not database.is_file():
        return False, "db.sqlite3 missing"
    missing = []
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        for label, query in SEED_QUERIES.items():
            try:
                if not connection.execute(query).fetchone()[0]:
                    missing.append(label)
            except sqlite3.Error:
                missing.append(label)
    finally:
        connection.close()
    if missing:
        return False, "missing synthetic seed: " + ", ".join(missing)
    return True, "synthetic user, account, transactions and alert present"


def check_environment(environ):
    flagged = sorted(key for key in environ if CREDENTIAL_NAME.search(key) or key.upper() in DATA_PATH_NAMES)
    if flagged:
        return False, "remove from the launch environment: " + ", ".join(flagged)
    return True, "no credential or data-path variables"


def run_checks(url, directory, environ):
    return [
        ("ready", *check_ready(url)),
        ("loopback", *check_loopback(url, directory)),
        ("storage", *check_storage(directory)),
        ("seed", *check_seed(directory)),
        ("environment", *check_environment(environ)),
    ]


def report(results, stream=None):
    stream = stream or sys.stdout
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", file=stream)
    return all(ok for _, ok, _ in results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, help="ledger.json written by verify.py")
    parser.add_argument("--url")
    parser.add_argument("--directory")
    args = parser.parse_args(argv)
    if args.ledger:
        instance = json.loads(args.ledger.read_text())["instance"]
        args.url, args.directory = instance["url"], instance["directory"]
    if not (args.url and args.directory):
        parser.error("pass --ledger, or both --url and --directory")
    return 0 if report(run_checks(args.url, args.directory, os.environ)) else 1


if __name__ == "__main__":
    sys.exit(main())
