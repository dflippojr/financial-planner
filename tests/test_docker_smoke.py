"""The Docker smoke's checks, driven against stubs.

Most tests source scripts/docker-smoke.sh, which then only defines its
functions, and replace compose and container_state, so no Docker daemon is
reached. Each stub state is "<status> <restart count>", as docker inspect
prints it. The isolation tests run the whole script with a fake docker on PATH,
and the #560 tests run tests/docker_smoke_invalid_cron.py, the pytest verdict
the smoke runs inside the app image, on recorded outcomes.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

from tests.test_backup_scripts import POSIX_BASH, _unix_path

SMOKE_SCRIPT = Path(__file__).parents[1] / "scripts" / "docker-smoke.sh"
INVALID_CRON_VERDICT = Path(__file__).parent / "docker_smoke_invalid_cron.py"


def _run(tmp_path, body, **env):
    script = f'source "{_unix_path(SMOKE_SCRIPT)}"\nwork_dir="{_unix_path(tmp_path)}"\n{body}\n'
    # The expected-failure branch would otherwise write to the CI job summary.
    base = {key: value for key, value in os.environ.items() if key != "GITHUB_STEP_SUMMARY"}
    return subprocess.run(
        [POSIX_BASH or "bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**base, **env},
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
VERDICT_OUTPUT = "2 passed, 1 xfailed"


def _invalid_cron(tmp_path, state, *, logs=CRON_MESSAGE, verdict_code=0, verdict_output=VERDICT_OUTPUT):
    calls = tmp_path / "compose-calls"
    env_file = tmp_path / "smoke.env"
    env_file.write_text("POSTGRES_VOLUME_NAME=fp-smoke-postgres-data\nSIMPLEFIN_SYNC_CRON='30 6 * * 1-5'\n", encoding="utf-8")
    body = (
        f'env_file="{_unix_path(env_file)}"\n'
        f'compose() {{ echo "$(tr "\\n" " " < "$env_file")$*" >> "{_unix_path(calls)}"; '
        f'case $1 in logs) printf "%s\\n" "{logs}" ;; run) echo "{verdict_output}"; return {verdict_code} ;; esac; }}\n'
        f'container_state() {{ echo "{state}"; }}\n'
        "expect_invalid_cron_refused '30 6 * * MON-FRI' '60 6 * * *' && echo checked"
    )
    result = _run(tmp_path, body, REFUSE_SECONDS="1", SOAK_POLL_SECONDS="1")
    return result, calls.read_text(encoding="utf-8")


def test_invalid_cron_starts_the_normal_services_with_only_the_schedule_changed(tmp_path):
    _, calls = _invalid_cron(tmp_path, "restarting 3")

    ups = [line for line in calls.splitlines() if " up " in line]
    assert ups == [
        "POSTGRES_VOLUME_NAME=fp-smoke-postgres-data SIMPLEFIN_SYNC_CRON='30 6 * * MON-FRI' up -d db app backup background",
        "POSTGRES_VOLUME_NAME=fp-smoke-postgres-data SIMPLEFIN_SYNC_CRON='60 6 * * *' up -d db app backup background",
    ]


def test_invalid_cron_hands_each_outcome_to_the_pytest_verdict(tmp_path):
    result, calls = _invalid_cron(tmp_path, "restarting 3")

    assert result.returncode == 0, result.stderr
    cases = tmp_path / "invalid-cron"
    assert (cases / "1" / "schedule").read_text(encoding="utf-8") == "30 6 * * MON-FRI\n"
    assert (cases / "2" / "outcome").read_text(encoding="utf-8") == "crash-loop\n"
    assert CRON_MESSAGE in (cases / "2" / "log").read_text(encoding="utf-8")
    verdict = [line for line in calls.splitlines() if " run " in line]
    assert len(verdict) == 1
    assert f"{_unix_path(INVALID_CRON_VERDICT)}:/smoke/invalid_cron.py:ro" in verdict[0]
    assert f"{_unix_path(cases)}:/smoke/invalid-cron:ro" in verdict[0]
    assert verdict[0].endswith("app -B -m pytest -p no:django -p no:cacheprovider --noconftest -c /dev/null --rootdir /smoke -rxX /smoke/invalid_cron.py")


def test_the_expected_failure_is_a_warning_and_a_summary_line(tmp_path):
    result, _ = _invalid_cron(tmp_path, "restarting 3")

    assert "checked" in result.stdout
    assert "::warning::Expected failure (#560)" in result.stdout


def test_a_failing_verdict_fails_the_smoke(tmp_path):
    result, _ = _invalid_cron(tmp_path, "exited 0", verdict_code=1, verdict_output="FAILED [XPASS(strict)] #560")

    assert result.returncode == 1
    assert "checked" not in result.stdout
    assert "invalid SIMPLEFIN_SYNC_CRON check failed" in result.stderr


def _verdict(tmp_path, *cases):
    """Run the #560 pytest verdict on recorded (outcome, log) cases."""
    for number, (outcome, log) in enumerate(cases, start=1):
        case = tmp_path / "invalid-cron" / str(number)
        case.mkdir(parents=True)
        (case / "schedule").write_text("30 6 * * MON-FRI\n", encoding="utf-8")
        (case / "outcome").write_text(f"{outcome}\n", encoding="utf-8")
        (case / "log").write_text(log, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-p", "no:django", "-p", "no:cacheprovider", "-p", "no:xdist",
         "--noconftest", "-c", os.devnull, "--rootdir", str(tmp_path), "-rxX", str(INVALID_CRON_VERDICT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "SMOKE_INVALID_CRON_DIR": str(tmp_path / "invalid-cron")},
        check=False,
    )


def test_a_restart_loop_with_a_message_is_the_strict_expected_failure_for_560(tmp_path):
    result = _verdict(tmp_path, ("crash-loop", CRON_MESSAGE), ("crash-loop", CRON_MESSAGE))

    assert result.returncode == 0, result.stdout
    assert "2 xfailed" in result.stdout
    assert "XFAIL" in result.stdout
    assert "#560" in result.stdout


def test_a_refusal_without_a_restart_loop_is_a_strict_xpass_so_the_marker_must_go(tmp_path):
    result = _verdict(tmp_path, ("refused", CRON_MESSAGE), ("crash-loop", CRON_MESSAGE))

    assert result.returncode == 1
    assert "XPASS(strict)" in result.stdout
    assert "#560" in result.stdout


def test_a_restart_loop_without_a_message_naming_the_variable_fails(tmp_path):
    result = _verdict(tmp_path, ("crash-loop", "Traceback (most recent call last)"))

    assert result.returncode == 1
    assert "test_a_message_names_the_variable" in result.stdout


def test_background_that_keeps_running_on_an_invalid_schedule_fails(tmp_path):
    result = _verdict(tmp_path, ("started", CRON_MESSAGE))

    assert result.returncode == 1
    assert "test_background_does_not_keep_running" in result.stdout


def test_the_verdict_fails_when_the_smoke_recorded_no_case(tmp_path):
    (tmp_path / "invalid-cron").mkdir()
    result = _verdict(tmp_path)

    assert result.returncode == 1


# Exported in the shell of an operator who also runs the production stack.
PRODUCTION_ENV = {
    "POSTGRES_VOLUME_NAME": "financial-planner-postgres-data",
    "RECEIPTS_VOLUME_NAME": "financial-planner-receipts",
    "BACKUP_DIR": "/srv/financial-planner/backups",
    "OFFSITE_RCLONE_REMOTE": "offsite:financial-planner",
    "OFFSITE_RCLONE_CONFIG": "/srv/financial-planner/rclone.conf",
    "COMPOSE_FILE": "compose.yml:compose.override.yml",
}


def _fake_docker(tmp_path, config=None):
    """A docker that logs each call with the compose settings in its environment.

    `compose config` answers like Compose resolves volumes: an exported
    variable wins over the env file, unless config fixes the answer.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker-calls"
    fixed = tmp_path / "config.json"
    if config is not None:
        fixed.write_text(json.dumps(config), encoding="utf-8")
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'log="{_unix_path(log)}"\n'
        'echo "CALL $*" >> "$log"\n'
        "env | grep -E '^(POSTGRES_VOLUME_NAME|RECEIPTS_VOLUME_NAME|BACKUP_DIR|OFFSITE_[A-Z_]*|COMPOSE_[A-Z_]*)=' "
        '| sed "s/^/  ENV /" >> "$log"\n'
        'case "$*" in\n'
        '  *" config --format json")\n'
        f'    if [ -f "{_unix_path(fixed)}" ]; then cat "{_unix_path(fixed)}"; exit 0; fi\n'
        '    env_file=$(echo "$*" | sed "s/.*--env-file \\([^ ]*\\) .*/\\1/")\n'
        '    from_file() { grep "^$1=" "$env_file" | cut -d= -f2; }\n'
        '    pg=${POSTGRES_VOLUME_NAME:-$(from_file POSTGRES_VOLUME_NAME)}\n'
        '    rc=${RECEIPTS_VOLUME_NAME:-$(from_file RECEIPTS_VOLUME_NAME)}\n'
        '    bd=${BACKUP_DIR:-$(from_file BACKUP_DIR)}\n'
        '    printf \'{"volumes": {"postgres_data": {"name": "%s"}, "receipts": {"name": "%s"}}, '
        '"services": {"backup": {"volumes": [{"type": "bind", "source": "%s"}]}}}\\n\' "$pg" "$rc" "$bd" ;;\n'
        '  *" build "*) exit 1 ;;\n'
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)
    return bin_dir, log


def _run_smoke(tmp_path, bin_dir):
    return subprocess.run(
        [POSIX_BASH or "bash", _unix_path(SMOKE_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            **PRODUCTION_ENV,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "COMPOSE_PROJECT_NAME": "fp-smoke-isolation",
        },
        check=False,
    )


def test_exported_production_settings_never_reach_the_smoke_project(tmp_path):
    bin_dir, log = _fake_docker(tmp_path)

    result = _run_smoke(tmp_path, bin_dir)

    calls = log.read_text(encoding="utf-8")
    # The fake build fails, so the run stops there and cleans up.
    assert result.returncode == 1
    assert " build " in calls
    assert "down --volumes" in calls
    for name, value in PRODUCTION_ENV.items():
        assert f"ENV {name}=" not in calls, calls
        assert value not in calls, calls
    for line in calls.splitlines():
        if line.startswith("CALL compose"):
            assert line.startswith(
                f"CALL compose -p fp-smoke-isolation -f {_unix_path(SMOKE_SCRIPT.parents[1] / 'compose.yml')} --env-file "
            )


def test_storage_outside_the_smoke_project_stops_the_run_before_anything_starts(tmp_path):
    bin_dir, log = _fake_docker(
        tmp_path,
        config={
            "volumes": {
                "postgres_data": {"name": "fp-smoke-isolation-postgres-data"},
                "receipts": {"name": "financial-planner-receipts"},
            },
            "services": {"backup": {"volumes": [{"type": "bind", "source": "/srv/financial-planner/backups"}]}},
        },
    )

    result = _run_smoke(tmp_path, bin_dir)

    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 1
    assert "financial-planner-receipts" in result.stderr
    assert "/srv/financial-planner/backups" in result.stderr
    for command in (" build ", " up ", " down ", " run "):
        assert command not in calls, calls


def test_the_storage_check_accepts_project_volumes_and_read_only_repo_files(tmp_path):
    config = {
        "volumes": {"postgres_data": {"name": "fp-smoke-postgres-data"}, "receipts": {"name": "fp-smoke-receipts"}},
        "services": {
            "backup": {
                "volumes": [
                    {"type": "bind", "source": f"{_unix_path(tmp_path)}/backups"},
                    {"type": "bind", "source": "/repo/ops/backup/rclone.conf.example", "read_only": True},
                    {"type": "volume", "source": "receipts"},
                ]
            }
        },
    }
    result = _run(
        tmp_path,
        f"COMPOSE_PROJECT_NAME=fp-smoke\ncompose() {{ echo '{json.dumps(config)}'; }}\n"
        "assert_disposable_storage && echo disposable",
    )

    assert result.returncode == 0, result.stderr
    assert "disposable" in result.stdout
