"""scripts/check_postgres_split.py: the split PostgreSQL jobs run exactly the old job's tests (issue #685)."""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_postgres_split.py"
spec = importlib.util.spec_from_file_location("check_postgres_split", SCRIPT)
split = importlib.util.module_from_spec(spec)
spec.loader.exec_module(split)

SUITE = "not browser and not migration and not property"
PROPERTY = "property and not browser and not migration"

# Synthetic node ids: the old job ran a migration test and four others, one of them a property test.
COLLECTED = {
    "migration": {"tests/m.py::test_m"},
    "not browser and not migration": {"tests/a.py::test_a", "tests/b.py::test_b[x]", "tests/p.py::test_p"},
    SUITE: {"tests/a.py::test_a", "tests/b.py::test_b[x]"},
    PROPERTY: {"tests/p.py::test_p"},
    "not browser and not migration and not property and not slow": {"tests/a.py::test_a"},
}


def _workflow(tmp_path, *markers, extra=""):
    matrix = "".join(f'          - group: g{i}\n            markers: "{m}"\n' for i, m in enumerate(markers))
    text = (
        "jobs:\n  postgres:\n    strategy:\n      matrix:\n        include:\n" + matrix + "    steps:\n"
        "      # A comment naming python -m pytest tests -m nothing is ignored.\n"
        "      - run: python -m pytest tests -q -p no:cacheprovider -m migration -rP\n"
        '      - run: python -m pytest tests -q -n 4 -m "${{ matrix.markers }}" --durations=15\n' + extra
    )
    path = tmp_path / "postgres.yml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def check(tmp_path, monkeypatch):
    def run(*markers, extra=""):
        monkeypatch.setattr(split, "WORKFLOW", _workflow(tmp_path, *markers, extra=extra))
        return split.main()

    monkeypatch.setattr(split, "collect", lambda expression: COLLECTED[expression])
    return run


def test_the_real_workflow_runs_property_tests_in_their_own_job_and_gate_needs_both():
    # gate needs the caller job, which fails when either matrix job fails; fail-fast off lets both report.
    postgres = (ROOT / ".github/workflows/postgres.yml").read_text(encoding="utf-8")
    assert split.workflow_selections(postgres) == ["migration", SUITE, PROPERTY]
    assert "      fail-fast: false\n" in postgres
    assert "        run: python scripts/check_postgres_split.py\n" in postgres
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^  gate:\n(?:    .*\n|\n)*?    needs: \[[^\]]*\bpostgres\b", ci, re.M)


def test_selections_that_cover_the_old_job_once_pass(check, capsys):
    assert check(SUITE, PROPERTY) == 0
    out = capsys.readouterr().out
    assert "4 tests in 3 selections, the same set the single job ran, none twice." in out
    assert "    1  -m 'property and not browser and not migration'" in out


def test_a_dropped_test_fails(check, capsys):
    assert check("not browser and not migration and not property and not slow", PROPERTY) == 1
    assert "1 tests the old job ran are dropped, for example ['tests/b.py::test_b[x]']" in capsys.readouterr().out


def test_a_test_in_two_jobs_fails(check, capsys):
    assert check(SUITE, "not browser and not migration") == 1
    out = capsys.readouterr().out
    assert "2 tests run in both" in out
    assert "::error::" in out


def test_the_same_selection_twice_fails(check, capsys):
    assert check(SUITE, PROPERTY, PROPERTY) == 1
    assert "postgres.yml runs the same selection twice" in capsys.readouterr().out


def test_a_test_the_old_job_did_not_run_fails(check, capsys, monkeypatch):
    monkeypatch.setitem(COLLECTED, "browser", {"tests/ui.py::test_ui"})
    assert check(SUITE, PROPERTY, "browser") == 1
    assert "1 tests the old job did not run, for example ['tests/ui.py::test_ui']" in capsys.readouterr().out


def test_a_pytest_run_without_a_selection_is_refused():
    with pytest.raises(SystemExit, match="without a -m selection"):
        split.workflow_selections("      - run: python -m pytest tests -q\n")


def test_matrix_markers_without_a_matrix_is_refused():
    with pytest.raises(SystemExit, match="no quoted markers"):
        split.workflow_selections('      - run: python -m pytest tests -m "${{ matrix.markers }}"\n')


def test_a_workflow_with_no_pytest_run_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "postgres.yml"
    path.write_text("jobs: {}\n", encoding="utf-8")
    monkeypatch.setattr(split, "WORKFLOW", path)
    with pytest.raises(SystemExit, match="found no pytest selections"):
        split.main()


def test_collect_keeps_node_ids_and_refuses_an_empty_or_broken_selection(monkeypatch, capsys):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == "property":
            return subprocess.CompletedProcess(command, 0, "tests/p.py::test_p\ntests/p.py::test_q\n\n2/9 tests collected\n", "")
        return subprocess.CompletedProcess(command, 5, "no tests collected\n", "")

    monkeypatch.setattr(split.subprocess, "run", fake_run)
    assert split.collect("property") == {"tests/p.py::test_p", "tests/p.py::test_q"}
    assert calls[0][-3:] == ["no:cacheprovider", "-m", "property"]
    with pytest.raises(SystemExit, match="exit code 5"):
        split.collect("nothing")
    assert "no tests collected" in capsys.readouterr().out
