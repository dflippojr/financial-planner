#!/usr/bin/env python3
"""Fail if two migrations share a number or an app's migrations have two leaves.

Parallel branches each add the next free number, so two PRs can both land a
0039 (PRs #185 and #189). makemigrations --check passes on each branch alone.
CI runs this on the pull request merge commit, where the collision with main
is visible. It reads the migration files with ast and never imports them, so
it needs no Django settings.

    python scripts/check_migration_numbers.py            # tracked migrations
    python scripts/check_migration_numbers.py DIR [DIR]  # every migrations/ under DIR
"""
import ast
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

NUMBERED = re.compile(r"^(\d+)_\w+\.py$")


def tracked_paths():
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*/migrations/*.py"], check=True, capture_output=True, text=True
    )
    return [Path(path) for path in result.stdout.split("\0") if path]


def walked_paths(roots):
    return [path for root in roots for path in Path(root).rglob("migrations/*.py")]


def dependencies(source):
    """The (app, name) pairs in a migration's dependencies list, skipping non-literal entries."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for statement in node.body:
                if (isinstance(statement, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "dependencies" for target in statement.targets)
                        and isinstance(statement.value, (ast.List, ast.Tuple))):
                    for item in statement.value.elts:
                        try:
                            value = ast.literal_eval(item)
                        except ValueError:
                            continue  # For example migrations.swappable_dependency(...).
                        if isinstance(value, tuple) and len(value) == 2:
                            found.append(value)
    return found


def problems(paths, read=lambda path: path.read_text(encoding="utf-8")):
    """Return a message for each shared number and each app with more than one leaf migration."""
    apps = defaultdict(dict)  # migrations directory -> {migration name: path}
    for path in paths:
        if NUMBERED.match(path.name):
            apps[path.parent][path.stem] = path
    found = []
    for directory in sorted(apps):
        migrations = apps[directory]
        numbers = defaultdict(list)
        for name in migrations:
            numbers[NUMBERED.match(f"{name}.py").group(1)].append(name)
        for number, names in sorted(numbers.items()):
            if len(names) > 1:
                found.append(f"{directory}: {len(names)} migrations numbered {number}: {', '.join(sorted(names))}")
        app = directory.parent.name
        depended_on = {name for path in migrations.values() for dep_app, name in dependencies(read(path))
                       if dep_app == app}
        leaves = sorted(set(migrations) - depended_on)
        if len(leaves) > 1:
            found.append(f"{directory}: {len(leaves)} leaf migrations, expected 1: {', '.join(leaves)}")
    return found


def main(argv):
    found = problems(walked_paths(argv) if argv else tracked_paths())
    if not found:
        return 0
    print("Migration numbering problems:")
    for message in found:
        print(f"  {message}")
    print(
        "Renumber the migration this branch adds to follow the latest one on main, and point its "
        "dependencies at that migration, then run python manage.py makemigrations --check."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
