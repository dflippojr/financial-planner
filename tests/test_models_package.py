"""finance.models is a package (#759); these names must keep importing from it.

Migrations record callables by their import path. Seven historical migrations
import validators and an on_delete handler as `finance.models.<name>`, and
Django imports every migration file whenever it builds the migration graph
(migrate, makemigrations, tests). Migrations are never edited after they ship,
so if one of these names stopped importing from finance.models, every database
command would fail. The finance/models/__init__.py re-exports are therefore
permanent, unlike the convenience re-exports a later stage may remove.
"""

import ast
import importlib
from pathlib import Path

import finance.models

MIGRATIONS = Path(__file__).resolve().parent.parent / "finance" / "migrations"

MIGRATION_REFERENCED_NAMES = (
    "validate_json_object",
    "validate_bulk_edit_snapshot",
    "validate_saved_filter_query",
    "validate_reason_list",
    "validate_header_name_list",
    "validate_stored_csv_headers",
    "_detach_draw_downs",
)


def test_migration_referenced_names_import_from_finance_models():
    for name in MIGRATION_REFERENCED_NAMES:
        assert callable(getattr(finance.models, name, None)), f"finance.models.{name} must stay importable"


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def test_every_finance_models_path_in_a_migration_resolves():
    """Catches a migration added later that names something the package does not export."""
    referenced = set()
    for path in sorted(MIGRATIONS.glob("[0-9]*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            dotted = _dotted(node) if isinstance(node, ast.Attribute) else None
            if dotted and dotted.startswith("finance.models."):
                referenced.add((path.name, dotted))
            if isinstance(node, ast.ImportFrom) and node.module == "finance.models":
                referenced.update((path.name, f"finance.models.{alias.name}") for alias in node.names)
    assert {dotted.rsplit(".", 1)[1] for _, dotted in referenced} >= set(MIGRATION_REFERENCED_NAMES)
    module = importlib.import_module("finance.models")
    missing = sorted((name, dotted) for name, dotted in referenced
                     if not hasattr(module, dotted.removeprefix("finance.models.")))
    assert not missing
