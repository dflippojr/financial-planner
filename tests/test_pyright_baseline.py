import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "pyright_baseline.py"
spec = importlib.util.spec_from_file_location("pyright_baseline", SCRIPT)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def error(file, line, message="Cannot access attribute \"visible_to\"\n  detail", rule="reportAttributeAccessIssue"):
    return {
        "file": str(baseline.ROOT / file),
        "severity": "error",
        "rule": rule,
        "message": message,
        "range": {"start": {"line": line - 1, "character": 4}},
    }


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(baseline, "BASELINE", tmp_path / "pyright-baseline.json")
    diagnostics = []
    monkeypatch.setattr(baseline, "run_pyright", lambda: diagnostics)
    return diagnostics


def test_key_ignores_line_numbers_and_message_detail():
    assert baseline.key(error("finance/views.py", 3)) == baseline.key(error("finance/views.py", 90))
    assert baseline.key(error("finance/views.py", 3)) == (
        "finance/views.py", "reportAttributeAccessIssue", "Cannot access attribute \"visible_to\""
    )


def test_warnings_are_not_counted():
    warning = {**error("finance/views.py", 3), "severity": "warning"}
    assert baseline.errors_by_key([warning]) == {}


def test_baselined_errors_pass_after_their_lines_move(isolated, capsys):
    isolated += [error("finance/views.py", 3), error("finance/views.py", 8)]
    assert baseline.main(["x", "--update"]) == 0
    isolated[:] = [error("finance/views.py", 40), error("finance/views.py", 41)]
    assert baseline.main(["x"]) == 0
    assert "no new errors (2 baselined)" in capsys.readouterr().out


def test_one_more_error_of_a_baselined_kind_fails_and_lists_its_occurrences(isolated, capsys):
    isolated.append(error("finance/views.py", 3))
    baseline.main(["x", "--update"])
    isolated.append(error("finance/views.py", 9))
    assert baseline.main(["x"]) == 1
    output = capsys.readouterr().out
    assert "finance/views.py:3:5" in output and "finance/views.py:9:5" in output


def test_new_kind_of_error_fails(isolated, capsys):
    baseline.main(["x", "--update"])
    isolated.append(error("finance/plan.py", 2, '"date" is not defined', rule="reportUndefinedVariable"))
    assert baseline.main(["x"]) == 1
    assert 'finance/plan.py: reportUndefinedVariable: "date" is not defined' in capsys.readouterr().out


def test_fixed_errors_pass_and_ask_for_an_update(isolated, capsys):
    isolated += [error("finance/views.py", 3), error("finance/views.py", 4)]
    baseline.main(["x", "--update"])
    isolated.pop()
    assert baseline.main(["x"]) == 0
    assert "1 baselined pyright errors are gone" in capsys.readouterr().out


def test_baseline_file_is_sorted_and_counted(isolated):
    isolated += [error("finance/b.py", 1), error("finance/a.py", 1), error("finance/a.py", 2)]
    baseline.main(["x", "--update"])
    entries = json.loads(baseline.BASELINE.read_text())
    assert [(e["file"], e["count"]) for e in entries] == [("finance/a.py", 2), ("finance/b.py", 1)]


def test_missing_baseline_means_every_error_is_new(isolated):
    isolated.append(error("finance/views.py", 3))
    assert baseline.main(["x"]) == 1


def test_pyright_crash_fails_instead_of_passing(monkeypatch):
    crashed = subprocess.CompletedProcess([], 3, stdout="", stderr="config error")
    monkeypatch.setattr(baseline.subprocess, "run", lambda *_args, **_kwargs: crashed)
    with pytest.raises(SystemExit, match="exit code 3"):
        baseline.run_pyright()


def test_reads_pyright_json(monkeypatch):
    payload = json.dumps({"generalDiagnostics": [error("finance/views.py", 3)]})
    ran = subprocess.CompletedProcess([], 1, stdout=payload, stderr="")
    monkeypatch.setattr(baseline.subprocess, "run", lambda *_args, **_kwargs: ran)
    assert baseline.run_pyright() == [error("finance/views.py", 3)]
