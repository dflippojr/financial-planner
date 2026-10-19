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


def _run_check(tmp_path, *, restart_on_inspect=None):
    now = datetime.now(UTC).timestamp()
    deployed = now - 30
    containers = {"db-id": {"id": "db-id", "service": "db", "created": now - 86400, "started": now - 86400}}
    events = []
    for service in ("app", "backup", "background"):
        cid = f"{service}-id"
        containers[cid] = {"id": cid, "service": service, "created": deployed, "started": deployed}
        events += [
            {"time": deployed, "action": "create", "id": cid},
            {"time": deployed, "action": "start", "id": cid},
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
            "-SkipRevision",
            "-StableSeconds", "0",
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
