"""The Docker smoke's verdict on an invalid SIMPLEFIN_SYNC_CRON (#531, #560).

scripts/docker-smoke.sh starts the stack once per invalid schedule, records
each case under SMOKE_INVALID_CRON_DIR (schedule, outcome, log) and runs this
file with the app image's own pytest. The name keeps it out of the normal
suite, which tests it against recorded outcomes in tests/test_docker_smoke.py.

The stack must refuse an invalid schedule with a message rather than
crash-loop. Today restart: unless-stopped restarts the refusing background
container, so that assertion is a strict expected failure for #560: once
background refuses without restarting, it passes, strict turns that into a
failure, and the marker has to go.
"""

import os
from pathlib import Path

import pytest

CASES_DIR = Path(os.environ.get("SMOKE_INVALID_CRON_DIR", "/smoke/invalid-cron"))
CASES = sorted(path for path in CASES_DIR.iterdir() if path.is_dir()) if CASES_DIR.is_dir() else []


def _read(case, name):
    return case.joinpath(name).read_text(encoding="utf-8")


def test_the_smoke_recorded_its_cases():
    # An empty parameter set would only skip the checks below.
    assert CASES, f"no invalid-schedule case under {CASES_DIR}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_message_names_the_variable(case):
    assert "SIMPLEFIN_SYNC_CRON" in _read(case, "log"), _read(case, "schedule")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_background_does_not_keep_running(case):
    assert _read(case, "outcome").strip() != "started", _read(case, "schedule")


@pytest.mark.xfail(strict=True, reason="#560")
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_background_is_refused_without_a_restart_loop(case):
    assert _read(case, "outcome").strip() == "refused", _read(case, "schedule")
