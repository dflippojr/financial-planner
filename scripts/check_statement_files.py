#!/usr/bin/env python3
"""Fail if a tracked file looks like a bank or statement export.

.gitignore keeps exports out by accident, but `git add -f` or a renamed
directory bypasses it. This check lists tracked files with `git ls-files` and
rejects any with a statement extension outside the allow-list. It reads only
paths, never file contents.

    python scripts/check_statement_files.py
"""
import subprocess
import sys
from pathlib import PurePosixPath

STATEMENT_EXTENSIONS = frozenset({".csv", ".ofx", ".qfx", ".qif", ".xlsx", ".xls", ".pdf"})
# Directory prefixes (trailing slash) or exact paths that may hold these files.
ALLOWED = ("tests/fixtures/",)


def offending_paths(paths, allowed=ALLOWED):
    """Return the paths with a statement extension that are not allow-listed."""
    found = []
    for path in paths:
        if PurePosixPath(path).suffix.lower() not in STATEMENT_EXTENSIONS:
            continue
        if any(path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in allowed):
            continue
        found.append(path)
    return found


def tracked_paths():
    result = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    )
    return [path for path in result.stdout.split("\0") if path]


def main():
    found = offending_paths(tracked_paths())
    if not found:
        return 0
    print("Tracked files with a statement extension outside the allow-list:")
    for path in found:
        print(f"  {path}")
    print(
        "Remove them from git (git rm --cached). If a file is a synthetic fixture, "
        "move it under tests/fixtures/ or add it to ALLOWED in "
        "scripts/check_statement_files.py. See SECURITY.md."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
