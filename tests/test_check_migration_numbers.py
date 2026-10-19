import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_migration_numbers.py"
spec = importlib.util.spec_from_file_location("check_migration_numbers", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def _migration(directory, name, *depends_on, app="finance"):
    deps = ", ".join(f'("{app}", "{dep}")' for dep in depends_on)
    (directory / f"{name}.py").write_text(
        "from django.conf import settings\nfrom django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        f"    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL), {deps}]\n"
        "    operations = []\n",
        encoding="utf-8",
    )


def _app(tmp_path):
    directory = tmp_path / "finance" / "migrations"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    _migration(directory, "0001_initial")
    _migration(directory, "0038_shared_local_model", "0001_initial")
    return directory


def test_linear_history_passes(tmp_path):
    directory = _app(tmp_path)
    _migration(directory, "0039_sheet_comparison", "0038_shared_local_model")
    assert guard.main([str(tmp_path)]) == 0


def test_two_0039_files_fail_on_number_and_on_leaves(tmp_path, capsys):
    # The PR #185 / #189 shape: both branches added a 0039 after 0038.
    directory = _app(tmp_path)
    _migration(directory, "0039_sheet_comparison", "0038_shared_local_model")
    _migration(directory, "0039_saved_transaction_filter", "0038_shared_local_model")
    assert guard.main([str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "2 migrations numbered 0039: 0039_saved_transaction_filter, 0039_sheet_comparison" in output
    assert "2 leaf migrations, expected 1" in output


def test_shared_number_fails_even_when_dependencies_are_linear(tmp_path):
    directory = _app(tmp_path)
    _migration(directory, "0039_sheet_comparison", "0038_shared_local_model")
    _migration(directory, "0039_saved_transaction_filter", "0039_sheet_comparison")
    assert guard.problems(guard.walked_paths([tmp_path])) == [
        f"{directory}: 2 migrations numbered 0039: 0039_saved_transaction_filter, 0039_sheet_comparison",
    ]


def test_two_leaves_fail_without_a_shared_number(tmp_path):
    directory = _app(tmp_path)
    _migration(directory, "0039_sheet_comparison", "0038_shared_local_model")
    _migration(directory, "0040_saved_transaction_filter", "0038_shared_local_model")
    assert guard.problems(guard.walked_paths([tmp_path])) == [
        f"{directory}: 2 leaf migrations, expected 1: 0039_sheet_comparison, 0040_saved_transaction_filter",
    ]


def test_dependencies_on_other_apps_do_not_count(tmp_path):
    directory = _app(tmp_path)
    _migration(directory, "0039_sheet_comparison", "0038_shared_local_model")
    _migration(directory, "0040_other", "0039_sheet_comparison")
    _migration(directory, "0041_auth_only", "0040_other")
    (directory / "0042_external.py").write_text(
        "from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n"
        '    dependencies = [("auth", "0041_auth_only")]\n',
        encoding="utf-8",
    )
    assert guard.problems(guard.walked_paths([tmp_path])) == [
        f"{directory}: 2 leaf migrations, expected 1: 0041_auth_only, 0042_external",
    ]


def test_current_repository_passes():
    assert guard.problems(guard.tracked_paths()) == []
