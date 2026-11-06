#!/usr/bin/env python3
"""Fail unless postgres.yml's jobs together run exactly the tests the single job ran.

Issue #685 split the PostgreSQL job by the property marker. Before the split
one job ran two selections: the populated migration test (-m migration) and
everything else that is not a browser test. This reads every pytest selection
postgres.yml runs now, the matrix jobs' markers included, collects each one
(collection only, so it needs no database) and requires that

- together they select the same node ids as the old two selections,
- no test is selected twice, and
- no selection is empty.

CI runs it in the property job; locally it takes a few seconds:

    python scripts/check_postgres_split.py
"""
import re
import subprocess
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/postgres.yml"

# The single job's selections before #685, in its two pytest steps.
OLD_SELECTIONS = ("migration", "not browser and not migration")

MATRIX_MARKERS = "${{ matrix.markers }}"


def workflow_selections(text):
    """The -m expression of every `python -m pytest tests` line, one per matrix job for ${{ matrix.markers }}."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    matrix = re.findall(r'^\s+markers: "([^"]+)"$', code, re.M)
    selections = []
    for line in code.splitlines():
        if "python -m pytest tests" not in line:
            continue
        match = re.search(r' -m (?:"([^"]+)"|([^\s"]+))', line.split("python -m pytest", 1)[1])
        if not match:
            raise SystemExit(f"postgres.yml runs pytest without a -m selection: {line.strip()}")
        expression = match.group(1) or match.group(2)
        if expression == MATRIX_MARKERS:
            if not matrix:
                raise SystemExit("postgres.yml uses ${{ matrix.markers }} but has no quoted markers: in its matrix")
            selections.extend(matrix)
        else:
            selections.append(expression)
    return selections


def collect(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", expression],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        # 5 is "no tests collected", which is a broken selection here too.
        sys.stdout.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f"collecting -m {expression!r} failed with exit code {result.returncode}")
    return {line for line in result.stdout.splitlines() if "::" in line}


def main():
    new = workflow_selections(WORKFLOW.read_text(encoding="utf-8"))
    if not new:
        raise SystemExit("found no pytest selections in postgres.yml")
    collected = {expression: collect(expression) for expression in dict.fromkeys(new + list(OLD_SELECTIONS))}
    for expression in new:
        print(f"{len(collected[expression]):5d}  -m {expression!r}")

    failures = []
    if len(set(new)) != len(new):
        failures.append("postgres.yml runs the same selection twice")
    for a, b in combinations(dict.fromkeys(new), 2):
        both = collected[a] & collected[b]
        if both:
            failures.append(f"{len(both)} tests run in both -m {a!r} and -m {b!r}, for example {sorted(both)[:5]}")
    old = set().union(*(collected[expression] for expression in OLD_SELECTIONS))
    now = set().union(*(collected[expression] for expression in new))
    if now - old:
        failures.append(f"{len(now - old)} tests the old job did not run, for example {sorted(now - old)[:5]}")
    if old - now:
        failures.append(f"{len(old - now)} tests the old job ran are dropped, for example {sorted(old - now)[:5]}")

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        return 1
    print(f"{len(now)} tests in {len(new)} selections, the same set the single job ran, none twice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
