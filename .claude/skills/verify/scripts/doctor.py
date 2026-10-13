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


def non_loopback_addresses():
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    addresses = {str(info[4][0]) for info in infos}
    return sorted(address for address in addresses if not address.startswith("127."))


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
    exposed = []
    for address in non_loopback_addresses():
        try:
            with socket.create_connection((address, parts.port), timeout=1):
                exposed.append(address)
        except OSError:
            pass
    if exposed:
        return False, f"port {parts.port} also answers on {', '.join(exposed)}"
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
