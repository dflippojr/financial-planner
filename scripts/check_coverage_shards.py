#!/usr/bin/env python3
"""Fail unless sonar.yml's coverage shards together ran every non-browser test exactly once.

Issue #691 split the coverage run in sonar.yml into shards (pytest
--coverage-shard K/N, see tests/conftest.py) whose coverage data the scan job
combines. This collects the tests the single run selected (-m "not browser",
collection only, so it needs no database) and compares them with the test
cases in the shards' JUnit reports. It requires that

- every selected test appears in some shard's report,
- no report has a test outside that selection, and
- no test appears in two reports, or twice in one.

The scan job runs it before combining coverage:

    python scripts/check_coverage_shards.py coverage-shards/junit-*.xml

To check the split locally without running the suite, collect the shards
instead of reading reports (a few seconds):

    python scripts/check_coverage_shards.py --collect 3
"""
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The selection of the coverage run in sonar.yml.
SELECTION = "not browser"


def junit_id(nodeid):
    """The (classname, name) pytest's JUnit report gives a node id, as _pytest.junitxml.mangle_test_address does."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def collect(*extra):
    """The JUnit ids of the tests -m SELECTION selects, collection only."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", SELECTION, *extra],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f"collecting -m {SELECTION!r} {' '.join(extra)} failed with exit code {result.returncode}")
    return [junit_id(line) for line in result.stdout.splitlines() if "::" in line]


def reported_tests(path):
    root = ET.parse(path).getroot()  # noqa: S314 - pytest wrote these reports in this workflow run
    return [(case.get("classname"), case.get("name")) for case in root.iter("testcase")]


def shard_reports(args):
    """(label, JUnit ids) per shard: from JUnit reports, or with --collect N by collecting each shard."""
    if args[:1] == ["--collect"] and len(args) == 2 and args[1].isdigit() and int(args[1]) > 0:
        count = int(args[1])
        return [(f"--coverage-shard {k}/{count}", collect("--coverage-shard", f"{k}/{count}")) for k in range(1, count + 1)]
    if not args or args[0].startswith("-"):
        raise SystemExit("usage: check_coverage_shards.py JUNIT_XML [JUNIT_XML ...] | --collect N")
    return [(path, reported_tests(path)) for path in args]


def main(args):
    shards = shard_reports(args)
    selected = Counter(collect())
    if not selected:
        raise SystemExit(f"-m {SELECTION!r} selected no tests")

    failures = []
    duplicate_ids = [test for test, count in selected.items() if count > 1]
    if duplicate_ids:
        failures.append(f"{len(duplicate_ids)} selected tests share a JUnit id, for example {duplicate_ids[:5]}")
    ran = Counter()
    for label, tests in shards:
        print(f"{len(tests):5d}  {label}")
        if not tests:
            failures.append(f"{label} has no tests")
        ran.update(tests)

    twice = sorted(test for test, count in ran.items() if count > 1)
    missing = sorted(set(selected) - set(ran))
    extra = sorted(set(ran) - set(selected))
    if twice:
        failures.append(f"{len(twice)} tests ran more than once, for example {twice[:5]}")
    if missing:
        failures.append(f"{len(missing)} selected tests ran in no shard, for example {missing[:5]}")
    if extra:
        failures.append(f"{len(extra)} tests ran that -m {SELECTION!r} does not select, for example {extra[:5]}")

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        return 1
    print(f"{len(ran)} tests in {len(shards)} shards: every test -m {SELECTION!r} selects, each exactly once.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
