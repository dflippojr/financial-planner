"""The Docker smoke's soak and invalid-schedule checks, driven against stubs.

The test sources scripts/docker-smoke.sh, which then only defines its
functions, and replaces compose and container_state, so no Docker daemon is
reached. Each stub state is "<status> <restart count>", as docker inspect
prints it.
"""

from pathlib import Path
import shutil
import subprocess

import pytest

SMOKE_SCRIPT = Path(__file__).parents[1] / "scripts" / "docker-smoke.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(tmp_path, body, **env):
    script = f'source "{SMOKE_SCRIPT.as_posix()}"\nwork_dir="{tmp_path.as_posix()}"\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", **env},
        check=False,
    )


def test_soak_passes_when_every_service_stays_up(tmp_path):
    result = _run(
        tmp_path,
        'container_state() { echo "running 0"; }\nsoak app backup background && echo soaked',
        SOAK_SECONDS="1",
        SOAK_POLL_SECONDS="1",
    )

    assert result.returncode == 0, result.stderr
    assert "soaked" in result.stdout


def test_soak_fails_when_a_service_restarts_in_the_last_interval(tmp_path):
    # Up at the first check, restarted before the deadline, and the next
    # check would come only after the deadline.
    result = _run(
        tmp_path,
        'container_state() { if [ "$SECONDS" -ge 2 ] && [ "$1" = background ]; then echo "running 1"; '
        'else echo "running 0"; fi; }\n'
        "soak app backup background && echo soaked",
        SOAK_SECONDS="2",
        SOAK_POLL_SECONDS="3",
    )

    assert result.returncode == 1
    assert "soaked" not in result.stdout
    assert "background is 'running 1'" in result.stderr


CRON_MESSAGE = "ValueError: SIMPLEFIN_SYNC_CRON supports numbers only (for example 1-5 for Monday to Friday)"


def _invalid_cron(tmp_path, state, *, up_output="", logs=CRON_MESSAGE):
    calls = tmp_path / "compose-calls"
    body = (
        f'compose() {{ echo "SIMPLEFIN_SYNC_CRON=${{SIMPLEFIN_SYNC_CRON:-}} $*" >> "{calls.as_posix()}"; '
        f'case $1 in up) printf "%s\\n" "{up_output}" ;; logs) printf "%s\\n" "{logs}" ;; esac; }}\n'
        f'container_state() {{ echo "{state}"; }}\n'
        "expect_invalid_cron_refused '30 6 * * MON-FRI' && echo checked"
    )
    result = _run(tmp_path, body, REFUSE_SECONDS="1", SOAK_POLL_SECONDS="1")
    return result, calls.read_text(encoding="utf-8")


def test_invalid_cron_starts_the_normal_services_with_the_bad_value(tmp_path):
    _, calls = _invalid_cron(tmp_path, "restarting 3")

    assert calls.splitlines()[0] == "SIMPLEFIN_SYNC_CRON=30 6 * * MON-FRI up -d db app backup background"
    assert " run " not in calls


@pytest.mark.parametrize("state", ["restarting 3", "running 2", "exited 4"])
def test_a_restart_loop_is_the_expected_failure_for_now(tmp_path, state):
    result, _ = _invalid_cron(tmp_path, state)

    assert result.returncode == 0, result.stderr
    assert "checked" in result.stdout
    assert "::warning::Expected failure (#560)" in result.stdout


def test_a_restart_loop_without_a_message_naming_the_variable_fails(tmp_path):
    result, _ = _invalid_cron(tmp_path, "restarting 3", logs="Traceback (most recent call last)")

    assert result.returncode == 1
    assert "no message names SIMPLEFIN_SYNC_CRON" in result.stderr


@pytest.mark.parametrize("state", ["exited 0", "created 0", "absent 0"])
def test_a_refusal_without_restarts_fails_so_the_expected_failure_is_removed(tmp_path, state):
    result, _ = _invalid_cron(
        tmp_path,
        state,
        up_output="service config-check didn't complete successfully: SIMPLEFIN_SYNC_CRON has a minute outside 0-59",
        logs="",
    )

    assert result.returncode == 1
    assert "#560 looks fixed" in result.stderr


def test_background_that_keeps_running_on_an_invalid_schedule_fails(tmp_path):
    result, _ = _invalid_cron(tmp_path, "running 0")

    assert result.returncode == 1
    assert "background started" in result.stderr
