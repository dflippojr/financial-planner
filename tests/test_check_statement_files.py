import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_statement_files.py"
spec = importlib.util.spec_from_file_location("check_statement_files", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_blocks_statement_extensions_outside_allow_list():
    paths = ["data/statement.csv", "docs/x.PDF", "a/b.QFX", "c.xlsx", "d.xls", "e.ofx", "f.qif"]
    assert guard.offending_paths(paths) == paths


def test_allows_fixtures_and_ordinary_files():
    paths = ["tests/fixtures/synthetic_card.qfx", "tests/fixtures/sub/x.csv", "finance/models.py", "README.md"]
    assert guard.offending_paths(paths) == []


def test_allow_list_does_not_match_lookalike_directories():
    assert guard.offending_paths(["tests/fixtures_old/a.csv", "x/tests/fixtures/a.csv"]) == [
        "tests/fixtures_old/a.csv",
        "x/tests/fixtures/a.csv",
    ]


def test_exact_path_allow_entry():
    assert guard.offending_paths(["docs/img/a.pdf", "docs/b.pdf"], allowed=("docs/img/a.pdf",)) == ["docs/b.pdf"]


def test_current_repository_passes():
    assert guard.offending_paths(guard.tracked_paths()) == []


def test_main_reports_offending_paths(monkeypatch, capsys):
    monkeypatch.setattr(guard, "tracked_paths", lambda: ["imports/synthetic.csv", "README.md"])
    assert guard.main() == 1
    output = capsys.readouterr().out
    assert "imports/synthetic.csv" in output
    assert "README.md" not in output


def test_main_passes_on_clean_tree(monkeypatch, capsys):
    monkeypatch.setattr(guard, "tracked_paths", lambda: ["tests/fixtures/synthetic_card.qfx"])
    assert guard.main() == 0
    assert capsys.readouterr().out == ""
