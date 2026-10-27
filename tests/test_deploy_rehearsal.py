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


def _simulating_docker(tmp_path, volumes=None, existing_volumes=(), swap_work_dir_to=None, services=None, top_level=None):
    """A docker stand-in that acts like the commands that touch the host.

    `compose config` resolves the backup bind mount from the env file's
    BACKUP_DIR, as compose does, plus the top-level `volumes`, and `run -v
    SRC:/backups ... find /backups -mindepth 1 -delete` empties SRC, following
    a symlink as Docker's mount does. restore.sh empties the directory a
    volume's driver_opts device names, as replacing /receipts does on such a
    volume. `volume ls` lists `existing_volumes`. With `swap_work_dir_to`,
    `build` moves the work directory aside and puts a link to that directory
    in its place. `services` and `top_level` add to the resolved config, with
    {BACKUP_DIR} and {WORK} standing for the backup and work directories.
    Everything else succeeds silently.
    """
    log = tmp_path / "docker-calls.log"
    stub = tmp_path / "fake_docker.py"
    stub.write_text(
        "import json, os, shutil, sys\n"
        "args = sys.argv[1:]\n"
        f"volumes = json.loads({json.dumps(volumes or {})!r})\n"
        f"extra = {json.dumps({'services': services or {}, 'top': top_level or {}})!r}\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(' '.join(args) + '\\n')\n"
        "def empty(source):\n"
        "    for name in os.listdir(source):\n"
        "        path = os.path.join(source, name)\n"
        "        shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.unlink(path)\n"
        "if args[:1] == ['compose'] and 'ls' in args:\n"
        "    print('[]')\n"
        "elif args[:1] == ['compose'] and 'config' in args:\n"
        "    env_file = args[args.index('--env-file') + 1]\n"
        "    values = dict(line.split('=', 1) for line in open(env_file, encoding='utf-8').read().splitlines())\n"
        "    extra = json.loads(extra.replace('{BACKUP_DIR}', values['BACKUP_DIR'])\n"
        "                       .replace('{WORK}', os.path.dirname(env_file)))\n"
        "    print(json.dumps({'volumes': volumes, 'services': {'backup': {'volumes': [\n"
        "        {'type': 'bind', 'source': values['BACKUP_DIR'], 'target': '/backups'}]}, **extra['services']},\n"
        "        **extra['top']}))\n"
        "elif args[:1] == ['compose'] and 'build' in args and " + repr(swap_work_dir_to is not None) + ":\n"
        "    work = os.path.dirname(args[args.index('--env-file') + 1])\n"
        "    os.rename(work, work + '-moved')\n"
        "    os.symlink(" + repr(str(swap_work_dir_to)) + ", work, target_is_directory=True)\n"
        "elif args[:1] == ['compose'] and any(a.endswith('/restore.sh') for a in args):\n"
        "    for volume in volumes.values():\n"
        "        device = (volume.get('driver_opts') or {}).get('device')\n"
        "        if device and os.path.isdir(device):\n"
        "            empty(device)\n"
        "elif args[:1] == ['run'] and 'find /backups -mindepth 1 -delete' in args:\n"
        "    empty(next(v.rsplit(':', 1)[0] for v in args if v.endswith(':/backups')))\n"
        "elif args[:2] == ['volume', 'ls']:\n"
        f"    print('\\n'.join({list(existing_volumes)!r}))\n"
        "elif args[:2] == ['volume', 'inspect']:\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir, log


def test_a_planted_work_directory_is_never_mounted_or_emptied(tmp_path):
    # Another local user pre-creates the directory a predictable work path
    # would use, with backups linking to real backups (#582 review).
    outside = tmp_path / "real-backups"
    (outside / "signing").mkdir(parents=True)
    (outside / "signing" / "backup-signing-key").write_text("synthetic sentinel", encoding="utf-8")
    planted = tmp_path / "fp-rehearsal-planted"
    planted.mkdir()
    (planted / "backups").symlink_to(outside, target_is_directory=True)
    bin_dir, log = _simulating_docker(tmp_path)
    dump = tmp_path / "synthetic.dump"
    dump.write_bytes(b"synthetic")
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], TMPDIR=str(tmp_path))

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
         "-Candidate", "HEAD", "-DumpPath", str(dump), "-ProjectName", "fp-rehearsal-planted"],
        capture_output=True, text=True, env=env, timeout=300, check=False, cwd=ROOT,
    )

    assert result.returncode == 1
    assert (outside / "signing" / "backup-signing-key").read_text(encoding="utf-8") == "synthetic sentinel"
    assert (planted / "backups").is_symlink()
    calls = log.read_text(encoding="utf-8")
    assert str(planted) not in calls
    assert str(outside) not in calls
    # It got as far as emptying a backup tree: its own, in a fresh work directory it then removed.
    emptied = [call for call in calls.splitlines() if call.startswith("run ") and "find /backups" in call]
    assert len(emptied) == 1
    assert f"-v {tmp_path}{os.sep}fp-rehearsal-work-" in emptied[0]
    assert not list(tmp_path.glob("fp-rehearsal-work-*"))


def test_a_bind_source_that_links_outside_the_work_directory_is_reported(tmp_path):
    outside = tmp_path / "real-backups"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "backups").symlink_to(outside, target_is_directory=True)
    config = {"services": {"backup": {"volumes": [
        {"type": "bind", "source": str(work / "backups"), "target": "/backups"},
        {"type": "bind", "source": str(work / "backups" / "audit" / "application"), "target": "/operator-audit"},
    ]}}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    problems = _ps(
        f"ConvertTo-Json -Compress -InputObject @(Test-DisposableStorage (Get-Content -Raw "
        f"{_ps_string(tmp_path / 'config.json')} | ConvertFrom-Json) 'fp-rehearsal-t' {_ps_string(work)})"
    )

    assert problems == [
        f"backup mounts {work / 'backups'} writable",
        f"backup mounts {work / 'backups' / 'audit' / 'application'} writable",
    ]


def test_each_work_directory_is_new_owner_only_and_not_named_after_the_project(tmp_path):
    (tmp_path / "fp-rehearsal-x").mkdir()
    env = dict(os.environ, TMPDIR=str(tmp_path))
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         f". '{SCRIPT}' -ProjectName fp-rehearsal-x; ConvertTo-Json -Compress @(New-RehearsalWorkDir; New-RehearsalWorkDir)"],
        capture_output=True, text=True, env=env, timeout=120, check=False, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    first, second = (Path(path) for path in json.loads(result.stdout))

    assert first != second
    for work in (first, second):
        assert work.parent == tmp_path
        assert work.name.startswith("fp-rehearsal-work-")
        assert not work.is_symlink()
        assert list(work.iterdir()) == []
        if os.name != "nt":
            assert work.stat().st_mode & 0o777 == 0o700
    assert list((tmp_path / "fp-rehearsal-x").iterdir()) == []


def _canonical(path):
    return _ps(f"Resolve-CanonicalPath {_ps_string(path)} | ConvertTo-Json")


def test_canonical_paths_follow_every_link_along_the_way(tmp_path):
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    (tmp_path / "relative").symlink_to("real", target_is_directory=True)
    (real / "up").symlink_to("..", target_is_directory=True)
    (tmp_path / "chain").symlink_to(tmp_path / "relative", target_is_directory=True)

    assert _canonical(tmp_path / "chain" / "inner") == str(real / "inner")
    assert _canonical(tmp_path / "relative" / "up" / "real" / "inner") == str(real / "inner")
    # `..` after a link climbs from where the link lands, not from the link.
    assert _canonical(f"{tmp_path / 'chain' / 'inner'}/../..") == str(tmp_path)
    # Parts compose will create are kept as written under the resolved parent.
    assert _canonical(tmp_path / "chain" / "audit" / "application") == str(real / "audit" / "application")


def test_a_path_that_cannot_be_resolved_is_refused(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    config = {"services": {"backup": {"volumes": [
        {"type": "bind", "source": str(tmp_path / "loop" / "backups"), "target": "/backups"},
        {"type": "bind", "source": "relative/backups", "target": "/backups"},
    ]}}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    assert _canonical(tmp_path / "loop") is None
    assert _canonical("relative/backups") is None
    assert _canonical(f"{tmp_path / 'missing'}/../x") is None
    assert _ps(
        f"ConvertTo-Json -Compress -InputObject @(Test-DisposableStorage (Get-Content -Raw "
        f"{_ps_string(tmp_path / 'config.json')} | ConvertFrom-Json) 'fp-rehearsal-t' {_ps_string(tmp_path)})"
    ) == [f"backup mounts {tmp_path / 'loop' / 'backups'} writable", "backup mounts relative/backups writable"]
    assert _ps(f"Test-UnderDirectory {_ps_string(tmp_path / 'x')} {_ps_string(tmp_path / 'loop')} | ConvertTo-Json") is False


def test_teardown_leaves_a_backup_directory_that_resolves_outside_the_work_directory(tmp_path):
    outside = tmp_path / "real-backups"
    outside.mkdir()
    (outside / "sentinel").write_text("synthetic sentinel", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "backups").symlink_to(outside, target_is_directory=True)
    bin_dir, log = _simulating_docker(tmp_path)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         f". '{SCRIPT}'; $script:Project = 'fp-rehearsal-t'; $script:WorkDir = {_ps_string(work)}; "
         "$script:WorkDirIdentity = Get-DirectoryIdentity $script:WorkDir; $script:Built = $true; $script:ComposeFile = 'compose.yml'; $script:EnvFile = 'rehearsal.env'; "
         "Invoke-Teardown; ConvertTo-Json -Compress -InputObject @($script:Failures)"],
        capture_output=True, text=True, env=env, timeout=120, check=False, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr

    assert (outside / "sentinel").read_text(encoding="utf-8") == "synthetic sentinel"
    assert "teardown: the backup directory no longer resolves inside the work directory; left it unemptied" in (
        json.loads(result.stdout.splitlines()[-1]))
    assert not any(call.startswith("run ") for call in log.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize(("options", "problem"), [
    ({"external": True}, "volume receipts is external"),
    ({"driver": "rclone"}, "volume receipts uses driver rclone"),
    ({"driver_opts": {"type": "none", "o": "bind", "device": "/srv/financial-planner/receipts"}},
     "volume receipts sets driver_opts"),
    ({"driver_opts": {"o": "rbind"}}, "volume receipts sets driver_opts"),
    ({"driver": "local", "driver_opts": {"device": "/dev/sdb1"}}, "volume receipts sets driver_opts"),
])
def test_a_prefixed_volume_whose_storage_is_not_a_plain_local_volume_is_reported(tmp_path, options, problem):
    # A project-prefixed name says nothing about where the volume's data lives (#582 review r2).
    config = {"volumes": {
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data", "driver": "local"},
        "receipts": {"name": "fp-rehearsal-t-receipts", **options},
    }}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    problems = _ps(
        f"ConvertTo-Json -Compress -InputObject @(Test-DisposableStorage (Get-Content -Raw "
        f"{_ps_string(tmp_path / 'config.json')} | ConvertFrom-Json) 'fp-rehearsal-t' {_ps_string(tmp_path)})"
    )

    assert problems == [problem]


def test_a_volume_that_already_exists_is_reported(tmp_path):
    config = {"volumes": {
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data"},
        "receipts": {"name": "fp-rehearsal-t-receipts"},
    }}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    problems = _ps(
        f"ConvertTo-Json -Compress -InputObject @(Test-DisposableStorage (Get-Content -Raw "
        f"{_ps_string(tmp_path / 'config.json')} | ConvertFrom-Json) 'fp-rehearsal-t' {_ps_string(tmp_path)} "
        "@('fp-rehearsal-t-receipts', 'financial-planner-receipts'))"
    )

    assert problems == ["volume receipts (fp-rehearsal-t-receipts) already exists"]


def _run_simulated(tmp_path, bin_dir, *arguments):
    dump = tmp_path / "synthetic.dump"
    dump.write_bytes(b"synthetic")
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], TMPDIR=str(tmp_path))
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
         "-Candidate", "HEAD", "-DumpPath", str(dump), "-ProjectName", "fp-rehearsal-t", *arguments],
        capture_output=True, text=True, env=env, timeout=300, check=False, cwd=ROOT,
    )


def _started(log):
    """Docker calls that build, start, run or remove anything."""
    return [call for call in log.read_text(encoding="utf-8").splitlines()
            if call.startswith("run ") or any(f" {verb} " in f" {call} " for verb in ("build", "up", "down", "rm"))]


def test_a_prefixed_volume_bound_to_an_outside_directory_stops_before_build(tmp_path):
    outside = tmp_path / "real-receipts"
    outside.mkdir()
    (outside / "receipt.pdf").write_text("synthetic sentinel", encoding="utf-8")
    bin_dir, log = _simulating_docker(tmp_path, volumes={
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data"},
        "receipts": {"name": "fp-rehearsal-t-receipts",
                     "driver_opts": {"type": "none", "o": "bind", "device": str(outside)}},
    })

    result = _run_simulated(tmp_path, bin_dir)

    assert result.returncode == 1
    assert "FAIL rehearsal: storage outside the throwaway project: volume receipts sets driver_opts" in result.stdout
    assert (outside / "receipt.pdf").read_text(encoding="utf-8") == "synthetic sentinel"
    assert _started(log) == []


def test_a_volume_left_from_before_is_refused_not_reused_or_removed(tmp_path):
    bin_dir, log = _simulating_docker(tmp_path, volumes={
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data"},
        "receipts": {"name": "fp-rehearsal-t-receipts"},
    }, existing_volumes=["fp-rehearsal-t-postgres-data"])

    result = _run_simulated(tmp_path, bin_dir)

    assert result.returncode == 1
    assert ("FAIL rehearsal: storage outside the throwaway project: "
            "volume postgres_data (fp-rehearsal-t-postgres-data) already exists") in result.stdout
    assert _started(log) == []


def test_a_work_directory_replaced_by_a_link_after_validation_is_not_emptied(tmp_path):
    # Someone who can write the work directory's parent swaps it for a link to
    # real backups once the mounts have been checked (#582 review r2).
    outside = tmp_path / "real-parent"
    (outside / "backups" / "signing").mkdir(parents=True)
    (outside / "backups" / "signing" / "backup-signing-key").write_text("synthetic sentinel", encoding="utf-8")
    bin_dir, log = _simulating_docker(tmp_path, swap_work_dir_to=outside)

    result = _run_simulated(tmp_path, bin_dir)

    assert result.returncode == 1
    assert (outside / "backups" / "signing" / "backup-signing-key").read_text(encoding="utf-8") == "synthetic sentinel"
    assert "FAIL teardown: the work directory was replaced after it was created; left it unemptied" in result.stdout
    assert not [call for call in log.read_text(encoding="utf-8").splitlines() if "find /backups" in call]


# Round 4 (#582 review 6451190854): the preflight is an allowlist. Each of these
# could reach production storage or the host; each must stop the rehearsal
# before it builds, starts, runs or tears down anything.
_PROD = "container:financial-planner-backup-1"


@pytest.mark.parametrize(("spec", "problem"), [
    pytest.param({"volumes_from": [f"{_PROD}:rw"]}, "service sneaky volumes_from: inherits another container's mounts",
                 id="volumes_from"),
    pytest.param({"devices": [{"source": "/dev/sdb", "target": "/dev/sdb", "permissions": "rwm"}]},
                 "service sneaky devices: maps host devices into the container", id="devices"),
    pytest.param({"privileged": True}, "service sneaky privileged: gives the container the host's devices", id="privileged"),
    pytest.param({"cap_add": ["SYS_ADMIN"]}, "service sneaky cap_add: adds kernel capabilities", id="cap_add"),
    pytest.param({"pid": "host"}, "service sneaky pid: host shares a namespace outside the project", id="pid-host"),
    pytest.param({"pid": _PROD}, f"service sneaky pid: {_PROD} shares a namespace outside the project", id="pid-container"),
    pytest.param({"ipc": "host"}, "service sneaky ipc: host shares a namespace outside the project", id="ipc-host"),
    pytest.param({"ipc": _PROD}, f"service sneaky ipc: {_PROD} shares a namespace outside the project", id="ipc-container"),
    pytest.param({"network_mode": "host"}, "service sneaky network_mode: host shares a namespace outside the project",
                 id="network_mode-host"),
    pytest.param({"network_mode": _PROD}, f"service sneaky network_mode: {_PROD} shares a namespace outside the project",
                 id="network_mode-container"),
    pytest.param({"security_opt": ["apparmor=unconfined"]}, "service sneaky security_opt: apparmor=unconfined is not allowed",
                 id="security_opt-apparmor"),
    pytest.param({"security_opt": ["seccomp=unconfined"]}, "service sneaky security_opt: seccomp=unconfined is not allowed",
                 id="security_opt-seccomp"),
    pytest.param({"userns_mode": "host"}, "service sneaky userns_mode: shares the host's user namespace", id="userns_mode"),
    pytest.param({"extends": {"service": "backup"}}, "service sneaky extends: pulls in a service the guard has not read",
                 id="extends"),
    pytest.param({"labels": {"com.docker.compose.project": "financial-planner"}},
                 "service sneaky labels: not a key the guard knows", id="unknown-labels"),
    pytest.param({"env_file": [{"path": "/srv/financial-planner/.env"}]},
                 "service sneaky env_file: not a key the guard knows", id="unknown-env_file"),
    pytest.param({"volumes": [{"type": "npipe", "source": "/var/run/docker.sock", "target": "/sock"}]},
                 "service sneaky volumes: mount type 'npipe' is not bind, volume or tmpfs", id="mount-npipe"),
    pytest.param({"volumes": [{"type": "image", "source": "financial-planner-app", "target": "/img"}]},
                 "service sneaky volumes: mount type 'image' is not bind, volume or tmpfs", id="mount-image"),
    pytest.param({"volumes": [{"type": "volume", "target": "/data"}]},
                 "service sneaky volumes: volume '' is not a declared project volume", id="mount-anonymous-volume"),
    pytest.param({"volumes": [{"type": "volume", "source": "financial-planner-receipts", "target": "/receipts"}]},
                 "service sneaky volumes: volume 'financial-planner-receipts' is not a declared project volume",
                 id="mount-undeclared-volume"),
    pytest.param({"volumes": [{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock",
                               "read_only": True}]},
                 "service sneaky volumes: bind /var/run/docker.sock is outside the work directory", id="mount-read-only-socket"),
    pytest.param({"volumes": [{"type": "bind", "source": "{BACKUP_DIR}", "target": "/b", "bind": {"propagation": "rshared"}}]},
                 "service sneaky volumes: a bind mount sets bind.propagation", id="mount-bind-option"),
    pytest.param({"volumes": [{"type": "tmpfs", "source": "/srv", "target": "/t"}]},
                 "service sneaky volumes: a tmpfs mount sets source", id="mount-tmpfs-source"),
    pytest.param({"build": {"context": "/srv/financial-planner"}},
                 "service sneaky build: context /srv/financial-planner is outside the work directory", id="build-context"),
    pytest.param({"build": {"context": "{WORK}/source", "network": "host"}}, "service sneaky build: sets network",
                 id="build-option"),
    pytest.param({"build": {"context": "{WORK}/source"}, "image": "financial-planner-app"},
                 "service sneaky image: financial-planner-app is not a fp-rehearsal-t- image", id="build-retags-production"),
    pytest.param({"networks": {"production": None}}, "service sneaky networks: production is not a project network",
                 id="undeclared-network"),
])
def test_a_service_outside_the_allowlist_stops_before_anything_starts(tmp_path, spec, problem):
    bin_dir, log = _simulating_docker(tmp_path, services={"sneaky": {"image": "alpine", **spec}})

    result = _run_simulated(tmp_path, bin_dir)

    assert result.returncode == 1
    assert "FAIL rehearsal: storage outside the throwaway project: " in result.stdout
    assert problem in result.stdout
    assert _started(log) == []


@pytest.mark.parametrize(("top", "problem"), [
    pytest.param({"include": [{"path": ["/srv/financial-planner/compose.yml"]}]},
                 "top-level include: not a key the guard knows", id="include"),
    pytest.param({"secrets": {"key": {"file": "/srv/financial-planner/.env"}}},
                 "top-level secrets: not a key the guard knows", id="secrets"),
    pytest.param({"x-anything": {}}, "top-level x-anything: not a key the guard knows", id="extension"),
    pytest.param({"networks": {"default": {"name": "financial-planner_default", "external": True}}},
                 "network default is named financial-planner_default", id="network-name"),
    pytest.param({"networks": {"default": {"name": "fp-rehearsal-t_default", "external": True}}},
                 "network default sets external", id="network-external"),
    pytest.param({"networks": {"default": {"name": "fp-rehearsal-t_default", "driver": "macvlan"}}},
                 "network default uses driver macvlan", id="network-driver"),
])
def test_a_top_level_key_outside_the_allowlist_stops_before_anything_starts(tmp_path, top, problem):
    bin_dir, log = _simulating_docker(tmp_path, top_level=top)

    result = _run_simulated(tmp_path, bin_dir)

    assert result.returncode == 1
    assert problem in result.stdout
    assert _started(log) == []


def _candidate_repo(tmp_path, compose_text):
    """A git checkout holding the script and a candidate compose.yml."""
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "ops" / SCRIPT.name)
    (repo / "compose.yml").write_text(compose_text, encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]
    for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "candidate"]):
        subprocess.run(git + command, check=True, capture_output=True)
    return repo


_BENIGN = "services:\n  db:\n    image: postgres:18-alpine\n"


@pytest.mark.parametrize(("compose_text", "problem"), [
    pytest.param("include:\n  - /srv/financial-planner/compose.yml\n" + _BENIGN,
                 "compose.yml include: pulls in compose files the guard has not read", id="include"),
    pytest.param(_BENIGN + "  app:\n    Extends:\n      file: /srv/financial-planner/compose.yml\n      service: backup\n",
                 "compose.yml extends: pulls in compose files the guard has not read", id="extends"),
    pytest.param(_BENIGN + '"\\x69nclude": [/srv/financial-planner/compose.yml]\n',
                 "compose.yml: a backslash escape could spell include or extends", id="escaped-key"),
    pytest.param(_BENIGN + "? !!binary aW5jbHVkZQ==\n: [/srv/financial-planner/compose.yml]\n",
                 "compose.yml: a YAML tag could spell include or extends", id="binary-key"),
])
def test_a_compose_file_that_pulls_in_another_stops_before_anything_starts(tmp_path, compose_text, problem):
    # Compose resolves include and extends before `config` prints the model,
    # so the guard reads the candidate's compose.yml itself.
    repo = _candidate_repo(tmp_path, compose_text)
    bin_dir, log = _simulating_docker(tmp_path)
    dump = tmp_path / "synthetic.dump"
    dump.write_bytes(b"synthetic")
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], TMPDIR=str(tmp_path))

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(repo / "ops" / SCRIPT.name),
         "-Candidate", "HEAD", "-DumpPath", str(dump), "-ProjectName", "fp-rehearsal-t"],
        capture_output=True, text=True, env=env, timeout=300, check=False, cwd=repo,
    )

    assert result.returncode == 1
    assert problem in result.stdout
    assert _started(log) == []
    assert not [call for call in log.read_text(encoding="utf-8").splitlines() if " config " in f" {call} "]


def _storage_problems(tmp_path, config, work):
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return _ps(
        f"ConvertTo-Json -Compress -InputObject @(Test-DisposableStorage (Get-Content -Raw "
        f"{_ps_string(tmp_path / 'config.json')} | ConvertFrom-Json) 'fp-rehearsal-t' {_ps_string(work)})"
    )


@pytest.mark.parametrize("mounts", [
    pytest.param([{"type": "bind", "source": "{WORK}/backups", "target": "/backups"},
                  {"type": "bind", "source": "{WORK}/backups/health", "target": "/h", "read_only": True,
                   "bind": {"create_host_path": True}},
                  {"type": "bind", "source": "{WORK}/source/ops/backup/rclone.conf.example", "target": "/c",
                   "read_only": True}], id="bind-under-work-directory"),
    pytest.param([{"type": "volume", "source": "receipts", "target": "/receipts", "volume": {"nocopy": True}}],
                 id="new-local-project-volume"),
    pytest.param([{"type": "tmpfs", "target": "/run/x", "tmpfs": {"size": 1048576, "mode": 1777}}], id="tmpfs"),
])
def test_each_allowed_mount_type_passes(tmp_path, mounts):
    work = tmp_path / "work"
    (work / "backups").mkdir(parents=True)
    config = json.loads(json.dumps({
        "name": "fp-rehearsal-t",
        "networks": {"default": {"name": "fp-rehearsal-t_default", "ipam": {}}},
        "volumes": {"receipts": {"name": "fp-rehearsal-t-receipts", "driver": "local"}},
        "services": {"app": {
            "build": {"context": "{WORK}/source", "dockerfile": "Dockerfile", "args": {"GIT_SHA": "abc"}},
            "image": "fp-rehearsal-t-app",
            "networks": {"default": None},
            "tmpfs": ["/run/csv-staging:size=128m,mode=1777"],
            "security_opt": ["no-new-privileges:true"],
            "volumes": mounts,
        }},
    }).replace("{WORK}", str(work)))

    assert _storage_problems(tmp_path, config, work) == []


def test_the_checked_in_compose_file_passes_the_allowlist(tmp_path):
    # `docker compose config` resolves the file without a daemon; the source
    # sits in the work directory as the rehearsal extracts it.
    work = tmp_path / "work"
    source = work / "source"
    source.mkdir(parents=True)
    shutil.copy(ROOT / "compose.yml", source / "compose.yml")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         f". '{SCRIPT}'; $script:Project = 'fp-rehearsal-t'; $work = {_ps_string(work)}; "
         "$script:ComposeFile = Join-Path $work 'source/compose.yml'; $script:EnvFile = Join-Path $work 'rehearsal.env'; "
         "New-RehearsalEnvFile $script:EnvFile (Join-Path $work 'backups') 41234 'abc123'; "
         "$config = Invoke-Compose @('config', '--format', 'json'); if ($config.ExitCode -ne 0) { throw 'compose config failed' }; "
         "ConvertTo-Json -Compress -InputObject @(@(Test-ComposeSource $script:ComposeFile) + "
         "@(Test-DisposableStorage (($config.Output -join \"`n\") | ConvertFrom-Json) $script:Project $work))"],
        capture_output=True, text=True, timeout=120, check=False, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr

    assert json.loads(result.stdout) == []
