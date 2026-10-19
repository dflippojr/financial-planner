import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, UTC
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ops" / "post-deploy-check.ps1"
FAKE_DOCKER = Path(__file__).with_name("fake_docker.py")
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")


def _fake_docker_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "docker.cmd").write_text(f'@"{sys.executable}" "{FAKE_DOCKER}" %*\r\n', encoding="utf-8")
    else:
        shim = bin_dir / "docker"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_DOCKER}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    return bin_dir


def _run_check(tmp_path, *, restart_on_inspect=None, expected_sha=None, image_sha=None, backup_created_early=0):
    now = datetime.now(UTC).timestamp()
    deployed = now - 30
    containers = {"db-id": {"id": "db-id", "service": "db", "created": now - 86400, "started": now - 86400}}
    events = []
    for service in ("app", "backup", "background"):
        cid = f"{service}-id"
        created = deployed - (backup_created_early if service == "backup" else 0)
        containers[cid] = {"id": cid, "service": service, "created": created, "started": created}
        if image_sha and service != "db":
            containers[cid]["labels"] = {"org.opencontainers.image.revision": image_sha}
        events += [
            {"time": created, "action": "create", "id": cid},
            {"time": created, "action": "start", "id": cid},
        ]
    if restart_on_inspect:
        containers[f"{restart_on_inspect}-id"]["restart_on_inspect"] = True
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps({"containers": containers, "events": events}), encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = str(_fake_docker_bin(tmp_path)) + os.pathsep + env["PATH"]
    env["FAKE_DOCKER_STATE"] = str(state)
    result = subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            "-ProjectName", "fp-post-deploy-test",
            # Nothing listens on the discard port, and no helper runs: the
            # health, traceback and backup checks fail without touching Docker.
            "-HealthUrl", "http://127.0.0.1:9/health/",
            "-StatusPath", str(tmp_path / "missing-status"),
            "-Python", "fp-no-such-python",
            "-StableSeconds", "0",
            *(["-ExpectedSha", expected_sha] if expected_sha else ["-SkipRevision"]),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    return result.stdout


def test_clean_deploy_counts_one_start_per_new_container(tmp_path):
    output = _run_check(tmp_path)

    assert "ok   containers: app running" in output
    assert "started" not in output
    assert "event history" not in output


def test_restart_between_check_start_and_first_snapshot_fails(tmp_path):
    # The restart lands after the check began but before the first snapshot
    # read the container, so both snapshots agree; only the events show it.
    output = _run_check(tmp_path, restart_on_inspect="app")

    assert "ok   containers: app running" in output
    assert "FAIL containers: app started 2 times between the deploy and the check" in output
    assert "backup started" not in output


FULL_SHA = "abc1234" + "0" * 33


def test_short_expected_sha_matches_the_full_image_label(tmp_path):
    output = _run_check(tmp_path, expected_sha="abc1234", image_sha=FULL_SHA)

    assert "FAIL revision" not in output


def test_expected_sha_that_is_not_a_prefix_of_the_image_label_fails(tmp_path):
    output = _run_check(tmp_path, expected_sha="abc1235", image_sha=FULL_SHA)

    assert "FAIL revision: app runs an image built from abc123400000, expected abc1235" in output


def test_expected_sha_that_is_not_hex_is_rejected(tmp_path):
    output = _run_check(tmp_path, expected_sha="main", image_sha=FULL_SHA)

    assert "FAIL revision: -ExpectedSha must be 7 to 40 hex digits" in output


def test_backup_created_just_before_app_is_counted_as_new(tmp_path):
    # Compose creates backup and app together, so backup can be a fraction of a
    # second older than app. Without -Since it must not be taken for a kept
    # container, whose restarts the check does not count.
    output = _run_check(tmp_path, restart_on_inspect="backup", backup_created_early=0.5)

    assert "FAIL containers: backup started 2 times between the deploy and the check" in output
