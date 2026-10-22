"""The Docker smoke's verdict on an invalid SIMPLEFIN_SYNC_CRON (#531, #560).

scripts/docker-smoke.sh starts the stack once per invalid schedule, records
each case under SMOKE_INVALID_CRON_DIR and runs this file with the app image's
own pytest. The name keeps it out of the normal suite, which tests it against
recorded outcomes in tests/test_docker_smoke.py. Each case directory holds:

- schedule: the SIMPLEFIN_SYNC_CRON value;
- outcome: what Docker did with background (crash-loop, started or refused);
- up-exit: the exit code of `docker compose up -d`;
- log: the output of that `up` and every service's logs;
- config-check-log: `docker compose logs config-check`;
- app-health: app's health status once `up --wait` returned;
- health: the body of GET /health/.

The stack must refuse an invalid schedule in the one-shot config-check service,
so `up` fails naming it and background is never started into a restart loop,
while app stays healthy and its health page names the variable.
"""

import os
from pathlib import Path
import re

import pytest

CASES_DIR = Path(os.environ.get("SMOKE_INVALID_CRON_DIR", "/smoke/invalid-cron"))
CASES = sorted(path for path in CASES_DIR.iterdir() if path.is_dir()) if CASES_DIR.is_dir() else []
# The error itself must name the variable, as in "ValueError: SIMPLEFIN_SYNC_CRON
# has a minute outside 0-59": a traceback whose source lines quote it does not.
VALIDATION_ERROR = re.compile(r"\b\w*(?:Error|Exception): [^\n]*\bSIMPLEFIN_SYNC_CRON\b")


def _read(case, name):
    return case.joinpath(name).read_text(encoding="utf-8")


def test_the_smoke_recorded_its_cases():
    # An empty parameter set would only skip the checks below.
    assert CASES, f"no invalid-schedule case under {CASES_DIR}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_message_names_the_variable(case):
    assert VALIDATION_ERROR.search(_read(case, "config-check-log")), _read(case, "schedule")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_background_does_not_keep_running(case):
    assert _read(case, "outcome").strip() != "started", _read(case, "schedule")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_background_is_refused_without_a_restart_loop(case):
    assert _read(case, "outcome").strip() == "refused", _read(case, "schedule")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_up_fails_naming_config_check(case):
    assert _read(case, "up-exit").strip() != "0", _read(case, "schedule")
    assert 'service "config-check" didn\'t complete successfully' in _read(case, "log"), _read(case, "schedule")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_app_stays_healthy_and_its_health_page_names_the_variable(case):
    assert _read(case, "app-health").strip() == "healthy", _read(case, "schedule")
    health = _read(case, "health")
    assert health.startswith("ok\n"), health
    assert "SIMPLEFIN_SYNC_CRON" in health, health
