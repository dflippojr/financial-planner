"""The repo's Python version is declared once in .python-version and matches every workflow setup-python step.

Tools that build an environment from the repo itself (agent-ops' verifier among them) read
.python-version or pyproject.toml, not the workflows, so the declaration must live in the repo.
This keeps it from drifting away from the versions CI installs (#741).
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
# "uses:" with or without its list-item dash, so "- name:" then "uses:" at the same indent still matches.
SETUP_PYTHON = re.compile(r"^\s*(?:-\s+)?uses:\s*[\"']?actions/setup-python@")
PYTHON_VERSION = re.compile(r"^\s*python-version:(.*)$")


def _declared():
    return (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def _workflow_files():
    # GitHub runs both extensions, so a renamed workflow must still be checked.
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _indent(line):
    return len(line) - len(line.lstrip())


def _scalar(raw):
    """The whole value of a python-version key: no comment, no quotes, nothing trimmed out of the middle."""
    value = re.sub(r"\s+#.*$", "", raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _step_indent(lines, number):
    """Indent of the list-item dash that opens the step whose uses key is on line number."""
    key = _indent(lines[number])
    for above in range(number, -1, -1):
        stripped = lines[above].lstrip()
        if stripped.startswith("-") and (above == number or _indent(lines[above]) < key):
            return _indent(lines[above])
    return key


def _setup_python_versions(text):
    """The python-version of each setup-python step in one workflow file, as (line number, value or None)."""
    lines = text.splitlines()
    live = [(number, line) for number, line in enumerate(lines) if not line.lstrip().startswith("#")]
    # A setup-python line this reader does not recognise must fail, not be skipped.
    unrecognised = [number + 1 for number, line in live if "setup-python" in line and not SETUP_PYTHON.match(line)]
    assert not unrecognised, f"setup-python at lines {unrecognised} is not a 'uses:' step this test can read"

    found = []
    for number, line in live:
        if not SETUP_PYTHON.match(line):
            continue
        # A step's keys sit deeper than its dash; the step ends at the next non-blank line at or above that indent.
        step_indent = _step_indent(lines, number)
        versions = []
        for step_line in lines[number + 1:]:
            if step_line.strip() and _indent(step_line) <= step_indent:
                break
            if step_line.lstrip().startswith("#"):
                continue
            version = PYTHON_VERSION.match(step_line)
            if version:
                versions.append(_scalar(version.group(1)))
        found.append((number + 1, versions[0] if len(versions) == 1 else None))
    return found


def test_python_version_file_declares_major_minor():
    assert re.fullmatch(r"\d+\.\d+", _declared()), ".python-version must hold one major.minor version, such as 3.15"


def test_every_setup_python_step_matches_python_version_file():
    declared = _declared()
    steps = []
    for workflow in _workflow_files():
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
        "      - uses: \"actions/setup-python@v7\"\n"
        "        # python-version: \"3.13\"\n"
        "      - name: later\n"
        "        with:\n"
        "          python-version: \"3.12\"\n"
        "      - uses: 'actions/setup-python@v7'\n"
        "        with:\n"
        "          python-version: 3.15  # pinned\n"
        "      - name: Set up Python\n"
        "        uses: actions/setup-python@v7\n"
        "        with:\n"
        "          python-version: \"3.15 || 3.14\"\n"
    )
    assert _setup_python_versions(text) == [(4, "3.15"), (8, None), (13, "3.15"), (17, "3.15 || 3.14")]


def test_setup_python_detection_fails_on_a_form_it_cannot_read():
    text = (
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - uses: actions/setup-python@v7   # ok\n"
        "        with:\n"
        "          python-version: \"3.15\"\n"
        "      - name: setup-python via ${{ env.ACTION }}\n"
    )
    try:
        _setup_python_versions(text)
    except AssertionError as error:
        assert "[7]" in str(error)
    else:
        raise AssertionError("an unreadable setup-python line was skipped")
