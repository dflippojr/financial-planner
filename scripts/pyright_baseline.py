#!/usr/bin/env python3
"""Run pyright and fail only on errors that are not in the committed baseline.

pyright found about 650 errors when it was added (#530), nearly all of them
Django's dynamic attributes (custom queryset methods on managers, reverse
relations, `_id` fields). pyright-baseline.json records them so that only a
new error fails. An entry is (file, rule, message) with a count rather than a
line number, so editing a file does not invalidate it; a key whose count grows
fails and lists every occurrence of that key.

    python scripts/pyright_baseline.py            # check
    python scripts/pyright_baseline.py --update   # rewrite the baseline

Rewrite the baseline after fixing baselined errors, never to accept a new one.
"""
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "pyright-baseline.json"


def run_pyright():
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", "--pythonpath", sys.executable],
        cwd=ROOT, capture_output=True, text=True,
    )
    # 0: no errors, 1: errors were reported. Anything else means pyright itself failed.
    if result.returncode not in (0, 1):
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"pyright failed with exit code {result.returncode}")
    return json.loads(result.stdout)["generalDiagnostics"]


def key(diagnostic):
    path = Path(diagnostic["file"]).resolve().relative_to(ROOT).as_posix()
    return path, diagnostic.get("rule", ""), diagnostic["message"].splitlines()[0]


def errors_by_key(diagnostics):
    found = {}
    for diagnostic in diagnostics:
        if diagnostic["severity"] == "error":
            found.setdefault(key(diagnostic), []).append(diagnostic)
    return found


def load_baseline():
    if not BASELINE.exists():
        return Counter()
    return Counter({(e["file"], e["rule"], e["message"]): e["count"] for e in json.loads(BASELINE.read_text())})


def write_baseline(found):
    entries = [
        {"file": file, "rule": rule, "message": message, "count": len(found[(file, rule, message)])}
        for file, rule, message in sorted(found)
    ]
    BASELINE.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def compare(found, baseline):
    """Return (new, fixed): keys over their baseline count, and keys under it."""
    new = {k: found[k] for k in found if len(found[k]) > baseline.get(k, 0)}
    fixed = {k: baseline[k] - len(found.get(k, ())) for k in baseline if len(found.get(k, ())) < baseline[k]}
    return new, fixed


def main(argv):
    found = errors_by_key(run_pyright())
    total = sum(len(v) for v in found.values())
    if argv[1:] == ["--update"]:
        write_baseline(found)
        print(f"Wrote {total} pyright errors to {BASELINE.name}.")
        return 0
    new, fixed = compare(found, load_baseline())
    for (file, rule, message), diagnostics in sorted(new.items()):
        print(f"{file}: {rule}: {message}")
        for diagnostic in diagnostics:
            start = diagnostic["range"]["start"]
            print(f"  {file}:{start['line'] + 1}:{start['character'] + 1}")
    if fixed:
        print(f"{sum(fixed.values())} baselined pyright errors are gone; "
              "run python scripts/pyright_baseline.py --update to lock that in.")
    if new:
        print(f"pyright: new errors above (each key lists all of its occurrences); {total} errors in all.")
        return 1
    print(f"pyright: no new errors ({total} baselined).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
