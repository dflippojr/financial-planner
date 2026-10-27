"""The parts of ops/deploy-rehearsal.ps1 that need no Docker (#538).

The functions are loaded by dot-sourcing the script, which returns before the
rehearsal runs. Whole-script runs put a recording stand-in for docker on PATH,
so they prove which docker commands run before the script stops, and that none
reaches a real daemon. The end-to-end rehearsal needs Docker and is recorded in
the PR, not here.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ops" / "deploy-rehearsal.ps1"
# The script targets PowerShell 7 on Linux, which the CI runners have; without
# it these tests fail rather than skip.
POWERSHELL = shutil.which("pwsh") or "pwsh"


def _ps(body):
    """Dot-source the script, run body and return its JSON output."""
    command = f". '{SCRIPT}'; {body}"
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=120, check=False, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _counts(table):
    pairs = "; ".join(f"{_ps_string(name)} = {count}" for name, count in table.items())
    return f"([ordered]@{{ {pairs} }})"


def _compare(before, after, expected=()):
    listed = "@(" + ", ".join(_ps_string(name) for name in expected) + ")"
    rows = _ps(f"@(Compare-RowCounts {_counts(before)} {_counts(after)} {listed}) | ConvertTo-Json -Compress -AsArray")
    return {row["Table"]: (row["State"], row["Failed"]) for row in rows}


@pytest.mark.parametrize(("name", "allowed"), [
    ("financial-planner", False),
    ("fp-smoke", False),
    ("rehearsal-1", False),
    ("fp-rehearsal-", False),
    ("fp-rehearsal-ABC", False),
    ("fp-rehearsal-../x", False),
    ("", False),
    ("fp-rehearsal-1a2b", True),
    ("fp-rehearsal-pr-538_2", True),
])
def test_only_a_prefixed_throwaway_project_may_be_torn_down(name, allowed):
    reason = _ps(f"Test-RehearsalProjectName {_ps_string(name)} | ConvertTo-Json")

    assert (reason is None) is allowed
    if name == "financial-planner":
        assert reason == "refusing the production project financial-planner"


def test_unchanged_and_new_tables_pass():
    rows = _compare({"finance_transaction": 2720, "auth_user": 4},
                    {"finance_transaction": 2720, "auth_user": 4, "finance_refundlinkdrop": 0})

    assert rows == {"finance_transaction": ("same", False), "auth_user": ("same", False),
                    "finance_refundlinkdrop": ("new", False)}


def test_a_changed_or_dropped_table_fails_unless_expected():
    before = {"finance_transaction": 2720, "finance_alert": 84, "finance_legacy": 3}
    after = {"finance_transaction": 2719, "finance_alert": 60}

    assert _compare(before, after) == {
        "finance_transaction": ("changed", True),
        "finance_alert": ("changed", True),
        "finance_legacy": ("dropped", True),
    }
    assert _compare(before, after, ["finance_alert", "finance_legacy"]) == {
        "finance_transaction": ("changed", True),
        "finance_alert": ("expected", False),
        "finance_legacy": ("expected", False),
    }


def test_a_gained_row_fails_like_a_lost_one():
    # A migration that duplicates rows double counts money as surely as one that drops them.
    assert _compare({"finance_transaction": 10}, {"finance_transaction": 11}) == {"finance_transaction": ("changed", True)}


def test_django_bookkeeping_may_only_grow():
    before = {"django_migrations": 91, "django_content_type": 64, "auth_permission": 256}

    assert _compare(before, {"django_migrations": 107, "django_content_type": 69, "auth_permission": 276}) == {
        "django_migrations": ("grew", False), "django_content_type": ("grew", False), "auth_permission": ("grew", False),
    }
    assert _compare(before, {"django_migrations": 90, "django_content_type": 64, "auth_permission": 256})[
        "django_migrations"] == ("changed", True)


def test_expected_tables_split_a_comma_list_from_the_command_line():
    # `pwsh -File script.ps1 -ExpectCountChange a,b` binds one string "a,b".
    listed = _ps(f". '{SCRIPT}' -ExpectCountChange 'finance_alert,finance_aiconversation' "
                 "-ProjectName fp-rehearsal-x; $ExpectCountChange | ConvertTo-Json -Compress -AsArray")

    assert listed == ["finance_alert", "finance_aiconversation"]


def test_count_rows_print_table_names_counts_and_states_only():
    lines = _ps(
        "@(Compare-RowCounts ([ordered]@{ finance_transaction = 2720; finance_alert = 84 }) "
        "([ordered]@{ finance_transaction = 2720; finance_alert = 60; finance_reconciliationrun = 0 }) "
        "| ForEach-Object { Format-CountRow $_ }) | ConvertTo-Json -Compress -AsArray"
    )

    assert [line.split() for line in lines] == [
        ["FAIL", "counts:", "finance_alert", "84", "->", "60", "changed"],
        ["ok", "counts:", "finance_reconciliationrun", "-", "->", "0", "new"],
        ["ok", "counts:", "finance_transaction", "2720", "->", "2720", "same"],
    ]


def test_row_counts_reject_anything_but_table_and_count_lines():
    assert _ps("ConvertFrom-RowCounts @(\"auth_user`t4\", \"finance_transaction`t2720\", '') | ConvertTo-Json -Compress") == {
        "auth_user": 4, "finance_transaction": 2720,
    }
    message = _ps("try { ConvertFrom-RowCounts @(\"auth_user`t4\", 'ERROR: something') } "
                  "catch { $_.Exception.Message | ConvertTo-Json }")
    assert message == "row counts: unexpected output from row-counts.sh"


def test_verdict_names_the_candidate_and_counts_only():
    sha = "0123456789abcdef0123456789abcdef01234567"

    assert _ps(f"Format-Verdict 0 '{sha}' 77 16 | ConvertTo-Json") == (
        "PASS deploy rehearsal of 0123456789ab: 77 tables compared, 16 migration(s) applied")
    assert _ps(f"Format-Verdict 2 '{sha}' 77 16 | ConvertTo-Json") == (
        "FAIL deploy rehearsal of 0123456789ab: 2 problem(s); do not deploy")
    assert _ps("Format-Verdict 1 $null 0 0 | ConvertTo-Json") == (
        "FAIL deploy rehearsal of unresolved candidate: 1 problem(s); do not deploy")


def test_storage_outside_the_throwaway_project_is_reported(tmp_path):
    work = tmp_path / "fp-rehearsal-t"
    config = {
        "volumes": {
            "postgres_data": {"name": "fp-rehearsal-t-postgres-data"},
            "receipts": {"name": "financial-planner-receipts"},
        },
        "services": {
            "app": {"volumes": [
                {"type": "bind", "source": str(work / "backups" / "health"), "target": "/backup-health"},
                {"type": "volume", "source": "receipts", "target": "/receipts"},
            ]},
            "backup": {"volumes": [
                {"type": "bind", "source": str(tmp_path / "financial-planner-backups"), "target": "/backups"},
                {"type": "bind", "source": "/etc/rclone.conf", "target": "/config/rclone.conf", "read_only": True},
            ]},
            "background": {},
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    problems = _ps(
        f"@(Test-DisposableStorage (Get-Content -Raw {_ps_string(tmp_path / 'config.json')} | ConvertFrom-Json) "
        f"'fp-rehearsal-t' {_ps_string(work)}) | ConvertTo-Json -Compress -AsArray"
    )

    assert problems == [
        "volume receipts is named financial-planner-receipts",
        f"backup mounts {tmp_path / 'financial-planner-backups'} writable",
    ]


def test_a_sibling_directory_sharing_the_prefix_is_outside_the_work_directory(tmp_path):
    work = tmp_path / "fp-rehearsal-t"
    config = {"services": {"backup": {"volumes": [
        {"type": "bind", "source": str(tmp_path / "fp-rehearsal-t-other"), "target": "/backups"},
    ]}}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    problems = _ps(
        f"@(Test-DisposableStorage (Get-Content -Raw {_ps_string(tmp_path / 'config.json')} | ConvertFrom-Json) "
        f"'fp-rehearsal-t' {_ps_string(work)}) | ConvertTo-Json -Compress -AsArray"
    )

    assert problems == [f"backup mounts {tmp_path / 'fp-rehearsal-t-other'} writable"]


def test_the_newest_signed_nightly_dump_is_chosen(tmp_path):
    nightly = tmp_path / "nightly"
    nightly.mkdir()
    for stamp, signed in (("20261025T073000Z", True), ("20261026T073000Z", True), ("20261027T073000Z", False)):
        (nightly / f"financial_planner_{stamp}.dump").write_bytes(b"synthetic")
        if signed:
            (nightly / f"financial_planner_{stamp}.sha256").write_text("synthetic", encoding="utf-8")
            (nightly / f"financial_planner_{stamp}.sha256.sig").write_text("synthetic", encoding="utf-8")

    chosen = _ps(f"Find-NewestSignedDump {_ps_string(nightly)} | ConvertTo-Json")

    assert Path(chosen).name == "financial_planner_20261026T073000Z.dump"
    assert _ps(f"Find-NewestSignedDump {_ps_string(tmp_path / 'missing')} | ConvertTo-Json") is None


def test_the_throwaway_env_file_holds_dummy_values_and_its_own_volumes(tmp_path):
    env_file = tmp_path / "rehearsal.env"

    _ps(f"$script:Project = 'fp-rehearsal-t'; New-RehearsalEnvFile {_ps_string(env_file)} "
        f"{_ps_string(tmp_path / 'backups')} 41234 'abc123'; 'null'")

    values = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines())
    assert values["POSTGRES_VOLUME_NAME"] == "fp-rehearsal-t-postgres-data"
    assert values["RECEIPTS_VOLUME_NAME"] == "fp-rehearsal-t-receipts"
    assert values["BACKUP_DIR"] == str(tmp_path / "backups")
    assert values["APP_PORT"] == "41234"
    assert values["GIT_SHA"] == "abc123"
    # Nothing in the restored copy may call the harness, chat or notify anyone.
    assert values["AGENT_HARNESS_ALLOWED_ORIGINS"] == ""
    assert values["AI_CHAT_LOCAL_ENABLED"] == "false"
    assert all("not-a-secret" in values[key] for key in ("POSTGRES_PASSWORD", "DJANGO_SECRET_KEY"))
    assert not {"SMTP_HOST", "NTFY_URL", "OFFSITE_RCLONE_REMOTE", "OFFSITE_AGE_RECIPIENT"} & set(values)


def _recording_docker(tmp_path, exit_code):
    """A docker stand-in that logs each call's arguments and exits exit_code."""
    log = tmp_path / "docker-calls.log"
    stub = tmp_path / "fake_docker.py"
    stub.write_text(
        "import sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "docker.cmd").write_text(f'@"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8")
    else:
        shim = bin_dir / "docker"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    return bin_dir, log


def _run_rehearsal(tmp_path, *arguments, docker_exit=1):
    bin_dir, log = _recording_docker(tmp_path, docker_exit)
    dump = tmp_path / "synthetic.dump"
    dump.write_bytes(b"synthetic")
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], TMPDIR=str(tmp_path))
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
         "-Candidate", "HEAD", "-DumpPath", str(dump), *arguments],
        capture_output=True, text=True, env=env, timeout=120, check=False, cwd=ROOT,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_the_production_project_is_refused_before_any_docker_command(tmp_path):
    result, calls = _run_rehearsal(tmp_path, "-ProjectName", "financial-planner", docker_exit=0)

    assert result.returncode == 1
    assert "FAIL rehearsal: refusing the production project financial-planner" in result.stdout
    assert result.stdout.rstrip().endswith("problem(s); do not deploy")
    assert calls == []


def test_unreachable_docker_fails_without_starting_or_tearing_down_anything(tmp_path):
    result, calls = _run_rehearsal(tmp_path, "-ProjectName", "fp-rehearsal-test")

    assert result.returncode == 1
    assert "FAIL rehearsal: Docker is not reachable; start it yourself, this script never does" in result.stdout
    # Only the reachability probe ran: no compose command, so no up and no down.
    assert calls == ["info --format {{.ServerVersion}}"]
    assert not (tmp_path / "fp-rehearsal-test").exists()


def test_a_candidate_that_is_not_a_commit_fails_before_docker(tmp_path):
    bin_dir, log = _recording_docker(tmp_path, 0)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
         "-Candidate", "fp538-no-such-revision", "-DumpPath", str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=120, check=False, cwd=ROOT,
    )

    assert result.returncode == 1
    assert "FAIL rehearsal: cannot resolve -Candidate fp538-no-such-revision" in result.stdout
    assert not log.exists()
