import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
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


@contextmanager
def _health_url(revision):
    if revision is None:
        # Nothing listens on the discard port, so the health check fails without a request.
        yield "http://127.0.0.1:9/health/"
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("X-App-Revision", revision)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/health/"
    finally:
        server.shutdown()
        server.server_close()


def _run_check(tmp_path, *, restart_on_inspect=None, expected_sha=None, image_sha=None, backup_created_early=0,
               health_revision=None, full_result=False):
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
    with _health_url(health_revision) as health_url:
        result = subprocess.run(
            [
                POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                "-ProjectName", "fp-post-deploy-test",
                "-HealthUrl", health_url,
                # No helper runs, so the traceback and backup checks fail without touching Docker.
                "-StatusPath", str(tmp_path / "missing-status"),
                "-Python", "fp-no-such-python",
                "-StableSeconds", "0",
                *(["-ExpectedSha", expected_sha] if expected_sha is not None else ["-SkipRevision"]),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
    return result if full_result else result.stdout


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


@pytest.mark.parametrize("expected_sha", ["", "   "])
def test_empty_expected_sha_is_rejected_before_any_check(tmp_path, expected_sha):
    # An unset variable passed as -ExpectedSha must not fall back to origin/main,
    # which could pass a deploy of some other revision.
    result = _run_check(tmp_path, expected_sha=expected_sha, image_sha=FULL_SHA, health_revision=SERVED_REVISION,
                        full_result=True)

    assert result.returncode == 1
    assert "FAIL revision: -ExpectedSha is empty" in result.stdout
    assert "ok   " not in result.stdout
    assert "health" not in result.stdout


def test_backup_created_just_before_app_is_counted_as_new(tmp_path):
    # Compose creates backup and app together, so backup can be a fraction of a
    # second older than app. Without -Since it must not be taken for a kept
    # container, whose restarts the check does not count.
    output = _run_check(tmp_path, restart_on_inspect="backup", backup_created_early=0.5)

    assert "FAIL containers: backup started 2 times between the deploy and the check" in output


WRONG_SHA = "abc1235" + "0" * 33
# /health/ always serves the first 12 digits of the deployed commit.
SERVED_REVISION = FULL_SHA[:12]


@pytest.mark.parametrize("length", [7, 12, 40])
def test_right_expected_sha_passes_both_checks_at_any_length(tmp_path, length):
    output = _run_check(tmp_path, expected_sha=FULL_SHA[:length], image_sha=FULL_SHA, health_revision=SERVED_REVISION)

    assert f"ok   revision: /health/ serves {SERVED_REVISION}" in output
    assert "FAIL revision" not in output


@pytest.mark.parametrize("length", [7, 12, 40])
def test_wrong_expected_sha_fails_both_checks_at_any_length(tmp_path, length):
    expected = WRONG_SHA[:length]
    output = _run_check(tmp_path, expected_sha=expected, image_sha=FULL_SHA, health_revision=SERVED_REVISION)

    assert f"FAIL revision: /health/ serves {SERVED_REVISION}, expected {expected[:12]}" in output
    assert f"FAIL revision: app runs an image built from {SERVED_REVISION}, expected {expected[:12]}" in output


def test_served_revision_shorter_than_seven_digits_is_not_a_match(tmp_path):
    output = _run_check(tmp_path, expected_sha=FULL_SHA, image_sha=FULL_SHA, health_revision="abc12")

    assert f"FAIL revision: /health/ serves abc12, expected {FULL_SHA[:12]}" in output


def test_unknown_served_revision_still_reports_the_missing_git_sha(tmp_path):
    output = _run_check(tmp_path, expected_sha=FULL_SHA, image_sha=FULL_SHA, health_revision="unknown")

    assert "FAIL revision: the app image has no GIT_SHA" in output
