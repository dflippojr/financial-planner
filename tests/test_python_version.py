"""The repo's Python version is declared once in .python-version and matches every workflow setup-python step.

Tools that build an environment from the repo itself (agent-ops' verifier among them) read
.python-version or pyproject.toml, not the workflows, so the declaration must live in the repo.
This keeps it from drifting away from the versions CI installs (#741).

The workflows are read line by line, not parsed as YAML: the project adds no YAML dependency. So the reader
accepts only the layout these workflows use (a block-style "with:" mapping under a step's uses key) and fails
on anything else, rather than guessing.
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


def _is_content(line):
    return bool(line.strip()) and not line.lstrip().startswith("#")


def _scalar(raw):
    """The whole value of a python-version key: no comment, no quotes, nothing trimmed out of the middle."""
    value = re.sub(r"\s+#.*$", "", raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _step_dash_indent(lines, number):
    """Indent of the list-item dash that opens the step whose uses key is on line number."""
    key = _indent(lines[number])
    for above in range(number, -1, -1):
        stripped = lines[above].lstrip()
        if stripped.startswith("-") and (above == number or _indent(lines[above]) < key):
            return _indent(lines[above])
    return key


def _step_python_versions(lines, number):
    """The python-version values under the with: mapping of the step whose uses key is on line number."""
    dash = _step_dash_indent(lines, number)
    key_col = lines[number].index("uses:")
    # The step's body: content lines below its uses key, until one at or above its dash.
    body = []
    for index in range(number + 1, len(lines)):
        if _is_content(lines[index]) and _indent(lines[index]) <= dash:
            break
        body.append(index)

    versions = []
    for index in body:
        line = lines[index]
        if not _is_content(line) or _indent(line) != key_col:
            continue
        stripped = line.strip()
        if stripped.startswith("with:") and stripped != "with:" and not stripped.startswith("with: #"):
            raise AssertionError(f"line {index + 1}: an inline 'with:' value is not read; use a block mapping")
        if stripped != "with:":
            continue
        # The with: mapping's keys: content below it, indented deeper than its key, until one back at its key.
        children = []
        for child_index in range(index + 1, len(lines)):
            if _is_content(lines[child_index]) and _indent(lines[child_index]) <= key_col:
                break
            if _is_content(lines[child_index]):
                children.append(child_index)
        if not children:
            continue
        child_indent = min(_indent(lines[child]) for child in children)
        for child in children:
            if _indent(lines[child]) != child_indent:
                continue
            version = PYTHON_VERSION.match(lines[child])
            if version:
                versions.append(_scalar(version.group(1)))
    return versions


def _setup_python_versions(text):
    """The python-version of each setup-python step in one workflow file, as (line number, value or None)."""
    lines = text.splitlines()
    # An action reference this reader does not recognise must fail, not be skipped.
    unrecognised = [
        number + 1
        for number, line in enumerate(lines)
        if _is_content(line) and "uses:" in line and "actions/setup-python" in line and not SETUP_PYTHON.match(line)
    ]
    assert not unrecognised, f"setup-python action reference at lines {unrecognised} is not one this test can read"

    found = []
    for number, line in enumerate(lines):
        if _is_content(line) and SETUP_PYTHON.match(line):
            versions = _step_python_versions(lines, number)
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
    text = "\n".join([
        "jobs:",                                       # 1
        "  a:",                                        # 2
        "    steps:",                                  # 3
        "      - uses: actions/setup-python@v7",        # 4
        "        with:",                                # 5
        "          python-version: \"3.15\"",           # 6
        "      - run: echo 3.14",                       # 7
        "      - uses: \"actions/setup-python@v7\"",    # 8
        "        # python-version: \"3.13\"",           # 9
        "      - name: later",                          # 10
        "        with:",                                # 11
        "          python-version: \"3.12\"",           # 12
        "      - uses: 'actions/setup-python@v7'",      # 13
        "        with:",                                # 14
        "          python-version: 3.15  # pinned",     # 15
        "      - name: |",                              # 16
        "          python-version: \"3.15 || 3.14\"",   # 17
        "        uses: actions/setup-python@v7",        # 18
        "        with:",                                # 19
        "          python-version: \"3.14\"",           # 20
        "      - uses: actions/setup-python@v7",        # 21
        "        with:",                                # 22
        "          python-version: \"3.14\"",           # 23
        "        name: |",                              # 24
        "          python-version: \"3.15\"",           # 25
        "      - name: Install Python with setup-python", # 26
        "        uses: actions/checkout@v7",            # 27
    ]) + "\n"
    assert _setup_python_versions(text) == [
        (4, "3.15"), (8, None), (13, "3.15"), (18, "3.14"), (21, "3.14"),
    ]


def test_setup_python_detection_fails_on_a_form_it_cannot_read():
    inline = (
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - uses: actions/setup-python@v7\n"
        "        with: {python-version: \"3.15\"}\n"
    )
    try:
        _setup_python_versions(inline)
    except AssertionError as error:
        assert "line 5" in str(error)
    else:
        raise AssertionError("an inline with: mapping was read as if it were block style")

    quoted_action_in_a_name = (
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - name: \"actions/setup-python@v7 example\"\n"
        "        uses: actions/setup-python@v7 # ok\n"
        "        with:\n"
        "          python-version: \"3.15\"\n"
    )
    assert _setup_python_versions(quoted_action_in_a_name) == [(5, "3.15")]
