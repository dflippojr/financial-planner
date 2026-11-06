"""The coverage shards in sonar.yml together run every non-browser test exactly once (issue #691)."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_coverage_shards.py"
spec = importlib.util.spec_from_file_location("check_coverage_shards", SCRIPT)
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)

# Synthetic JUnit ids of the tests -m "not browser" selects.
SELECTED = [
    ("tests.test_a", "test_a"),
    ("tests.test_b", "test_b[x]"),
    ("tests.properties.test_p.TestP", "test_p"),
]


def _report(tmp_path, name, *tests):
    cases = "".join(f'<testcase classname="{classname}" name="{test}" time="0.1"/>' for classname, test in tests)
    path = tmp_path / name
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>')
    return str(path)


@pytest.fixture
def check(tmp_path, monkeypatch):
    calls = []

    def collect(*extra):
        calls.append(extra)
        return list(SELECTED)

    monkeypatch.setattr(shards, "collect", collect)

    def run(*reports):
        return shards.main([_report(tmp_path, f"junit-{i}.xml", *tests) for i, tests in enumerate(reports)])

    run.calls = calls
    return run


@pytest.mark.parametrize(
    ("nodeid", "junit"),
    [
        ("tests/test_a.py::test_a", ("tests.test_a", "test_a")),
        ("tests/test_b.py::test_b[x::y/z.py]", ("tests.test_b", "test_b[x::y/z.py]")),
        ("tests/properties/test_p.py::TestP::test_p", ("tests.properties.test_p.TestP", "test_p")),
    ],
)
def test_node_ids_map_to_the_junit_ids_pytest_writes(nodeid, junit):
    assert shards.junit_id(nodeid) == junit


def test_shards_that_ran_every_test_once_pass(check, capsys):
    assert check(SELECTED[:2], SELECTED[2:]) == 0
    out = capsys.readouterr().out
    assert "3 tests in 2 shards: every test -m 'not browser' selects, each exactly once." in out
    assert check.calls == [()]


def test_a_test_no_shard_ran_fails(check, capsys):
    assert check(SELECTED[:1], SELECTED[2:]) == 1
    assert "::error::1 selected tests ran in no shard, for example [('tests.test_b', 'test_b[x]')]" in capsys.readouterr().out


def test_a_test_two_shards_ran_fails(check, capsys):
    assert check(SELECTED[:2], SELECTED[1:]) == 1
    assert "::error::1 tests ran more than once, for example [('tests.test_b', 'test_b[x]')]" in capsys.readouterr().out


def test_a_test_outside_the_selection_fails(check, capsys):
    assert check(SELECTED, [("tests.test_browser", "test_layout")]) == 1
    assert "::error::1 tests ran that -m 'not browser' does not select" in capsys.readouterr().out


def test_an_empty_shard_fails(check, capsys):
    assert check(SELECTED, []) == 1
    assert "has no tests" in capsys.readouterr().out


def test_collect_mode_collects_each_shard(monkeypatch, capsys):
    by_shard = {("--coverage-shard", "1/2"): SELECTED[:1], ("--coverage-shard", "2/2"): SELECTED[1:], (): SELECTED}
    monkeypatch.setattr(shards, "collect", lambda *extra: list(by_shard[extra]))
    assert shards.main(["--collect", "2"]) == 0
    assert "    2  --coverage-shard 2/2" in capsys.readouterr().out


def test_two_selected_tests_with_one_junit_id_fail(tmp_path, monkeypatch, capsys):
    # A JUnit report could not tell them apart, so the check could not prove both ran.
    monkeypatch.setattr(shards, "collect", lambda *extra: [*SELECTED, SELECTED[0]])
    assert shards.main([_report(tmp_path, "junit.xml", *SELECTED)]) == 1
    assert "::error::1 selected tests share a JUnit id" in capsys.readouterr().out


def test_an_empty_selection_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(shards, "collect", lambda *extra: [])
    with pytest.raises(SystemExit, match="selected no tests"):
        shards.main([_report(tmp_path, "junit.xml", *SELECTED)])


def test_collect_returns_the_junit_ids_of_the_real_selection():
    assert ("tests.test_check_coverage_shards", "test_an_empty_shard_fails") in shards.collect()


def test_a_collection_error_stops(monkeypatch):
    monkeypatch.setattr(shards, "SELECTION", "not (")
    with pytest.raises(SystemExit, match="failed with exit code"):
        shards.collect()


@pytest.mark.parametrize("args", [[], ["--collect"], ["--collect", "0"], ["--other"]])
def test_bad_arguments_stop_with_usage(args):
    with pytest.raises(SystemExit, match="usage"):
        shards.main(args)


def _collect(*args):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_check_coverage_shards.py", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT, capture_output=True, text=True,
    )
    return result.returncode, [line for line in result.stdout.splitlines() if "::" in line], result.stderr


def test_coverage_shard_option_splits_the_tests_disjointly_and_completely():
    _, everything, _ = _collect()
    parts = [_collect("--coverage-shard", f"{k}/3")[1] for k in (1, 2, 3)]
    assert all(parts)
    assert sorted(sum(parts, [])) == sorted(everything)
    assert max(map(len, parts)) - min(map(len, parts)) <= 1


@pytest.mark.parametrize("value", ["0/3", "4/3", "1", "a/b"])
def test_a_bad_coverage_shard_is_a_usage_error(value):
    code, _, stderr = _collect("--coverage-shard", value)
    assert code == 4
    assert "--coverage-shard" in stderr


def test_the_real_workflow_runs_every_shard_and_checks_and_combines_them():
    sonar = (ROOT / ".github/workflows/sonar.yml").read_text(encoding="utf-8")
    matrix = re.search(r"^        shard: \[([0-9, ]+)\]$", sonar, re.M)
    count = len(matrix.group(1).split(","))
    assert [int(k) for k in matrix.group(1).split(",")] == list(range(1, count + 1))
    assert f"--coverage-shard ${{{{ matrix.shard }}}}/{count}\n" in sonar
    assert f"name: coverage (${{{{ matrix.shard }}}}/{count})" in sonar
    assert 'python -m pytest tests -q -n 4 --dist worksteal -m "not browser"' in sonar
    assert "timeout-minutes: 20\n" in sonar
    assert "    needs: coverage\n" in sonar
    assert "run: python scripts/check_coverage_shards.py coverage-shards/junit-shard-*.xml\n" in sonar
    assert "python -m coverage report\n" in sonar
    assert sonar.index("check_coverage_shards.py") < sonar.index("coverage combine") < sonar.index("sonarqube-scan-action")
    # The local gate checks the same split by collection.
    assert f"scripts/check_coverage_shards.py --collect {count}\n" in (ROOT / "scripts/ci-local.sh").read_text(encoding="utf-8")
