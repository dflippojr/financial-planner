"""The repo's Python version is declared once in .python-version and matches every workflow setup-python step.

Tools that build an environment from the repo itself (agent-ops' verifier among them) read
.python-version or pyproject.toml, not the workflows, so the declaration must live in the repo.
This keeps it from drifting away from the versions CI installs (#741).
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
SETUP_PYTHON = re.compile(r"^(\s*)-?\s*uses: actions/setup-python@")
PYTHON_VERSION = re.compile(r"^\s*python-version:\s*[\"']?([^\"'\s#]+)")


def _declared():
    return (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def _setup_python_versions(text):
    """The python-version of each setup-python step in one workflow file, as (line number, version or None)."""
    lines = text.splitlines()
    found = []
    for number, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = SETUP_PYTHON.match(line)
        if not match:
            continue
        # The step's keys sit deeper than its "- uses:" dash; the step ends at the next line at or above that indent.
        step_indent = len(match.group(1))
        versions = []
        for step_line in lines[number + 1:]:
            if step_line.strip() and len(step_line) - len(step_line.lstrip()) <= step_indent:
                break
            if step_line.lstrip().startswith("#"):
                continue
            version = PYTHON_VERSION.match(step_line)
            if version:
                versions.append(version.group(1))
        found.append((number + 1, versions[0] if len(versions) == 1 else None))
    return found


def test_python_version_file_declares_major_minor():
    assert re.fullmatch(r"\d+\.\d+", _declared()), ".python-version must hold one major.minor version, such as 3.15"


def test_every_setup_python_step_matches_python_version_file():
    declared = _declared()
    steps = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, version in _setup_python_versions(workflow.read_text(encoding="utf-8")):
            steps.append((workflow.name, line_number, version))

    assert steps, "no setup-python steps found in .github/workflows; the check would pass vacuously"
    mismatched = [
        f"{name}:{line} python-version {version!r}"
        for name, line, version in steps
        if version != declared
    ]
    assert not mismatched, (
        f".python-version declares {declared!r}; these setup-python steps disagree or set no single python-version: "
        + "; ".join(mismatched)
    )


def test_setup_python_detection_reads_the_version_under_each_step():
    text = (
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - uses: actions/setup-python@v7\n"
        "        with:\n"
        "          python-version: \"3.15\"\n"
        "      - run: echo 3.14\n"
        "      - uses: actions/setup-python@v7\n"
        "        # python-version: \"3.13\"\n"
        "      - name: later\n"
        "        with:\n"
        "          python-version: \"3.12\"\n"
    )
    assert _setup_python_versions(text) == [(4, "3.15"), (8, None)]
