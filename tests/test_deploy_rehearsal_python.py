"""ops/deploy_rehearsal.py, the stdlib Python port of the deploy rehearsal (#789).

Each case in tests/test_deploy_rehearsal.py, which reaches the retained .ps1
by spawning its interpreter, has a counterpart here that imports the port and
calls its functions directly, so nothing here needs that interpreter or skips.
Whole runs call main() with tests/rehearsal_fake_docker.py first on PATH as
`docker`, so they prove which docker commands run before the rehearsal stops,
where each one runs, and that none reaches a real daemon. The end-to-end
rehearsal needs Docker and is an owner step recorded in the PR, not here.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ops" / "deploy_rehearsal.py"
SPEC = importlib.util.spec_from_file_location("deploy_rehearsal", SCRIPT)
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)

FAKE_DOCKER = Path(__file__).with_name("rehearsal_fake_docker.py")
# What the stand-in answers `docker create` with, and the prefix every command
# run inside the isolated container carries.
FAKE_ID = "f" * 64
IN_ISOLATED = f"exec -e DOCKER_HOST=unix:///home/rootless/docker.sock {FAKE_ID} docker "
PROJECT = "fp-rehearsal-t"
BENIGN = "services:\n  db:\n    image: postgres:18-alpine\n"
REAL_DOCKER = shutil.which("docker") or "docker"
_GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]


def _commit(repo, message):
    for command in (["add", "-A"], ["commit", "-q", "--allow-empty", "-m", message]):
        subprocess.run(["git", "-C", str(repo), *_GIT_IDENTITY, *command], check=True, capture_output=True)


def _candidate_repo(tmp_path, compose_text=BENIGN):
    """A git checkout whose HEAD is a candidate holding this compose.yml."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    (repo / "compose.yml").write_text(compose_text, encoding="utf-8")
    _commit(repo, "candidate")
    return repo


def _candidate_with(tmp_path, anchor, addition, files=None):
    """A candidate checkout of the checked-in compose.yml with addition after anchor."""
    source = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert source.count(anchor) == 1
    repo = _candidate_repo(tmp_path, source.replace(anchor, anchor + addition))
    in_tree = {"Dockerfile": "FROM scratch\n", "ops/backup/Dockerfile": "FROM scratch\n", **(files or {})}
    for name, content in in_tree.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            path.unlink(missing_ok=True)
            path.symlink_to(content)
        else:
            path.write_text(content, encoding="utf-8")
    _commit(repo, "candidate files")
    return repo


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """Puts the stand-in first on PATH as `docker`; returns a function that
    configures it and gives back its call log."""
    log = tmp_path / "docker-calls.log"
    config_path = tmp_path / "fake-docker.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_DOCKER}" "{config_path}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT"):
        monkeypatch.delenv(name, raising=False)

    def configure(**config):
        config_path.write_text(json.dumps({"log": str(log), "fake_id": FAKE_ID, **config}), encoding="utf-8")
        return log

    return configure


def _records(log):
    """Every docker call the stand-in received: its arguments and environment."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _calls(log):
    return [" ".join(record["args"]) for record in _records(log)]


@pytest.fixture
def rehearse(tmp_path, monkeypatch, capsys):
    """Runs main() against `repo`'s HEAD with a synthetic dump and the temporary
    directory inside tmp_path; returns the exit code and what it printed."""
    def run(*arguments, repo=None, candidate="HEAD", dump=True):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(rehearsal, "REPO_ROOT", repo or _candidate_repo(tmp_path))
        argv = [f"--candidate={candidate}", *arguments]
        if dump:
            path = tmp_path / "synthetic.dump"
            path.write_bytes(b"synthetic")
            argv += ["--dump-path", str(path)]
        if "--project-name" not in arguments:
            argv += ["--project-name", PROJECT]
        capsys.readouterr()
        code = rehearsal.main(argv)
        return code, capsys.readouterr().out

    return run


def _problems(config, work, existing=()):
    return rehearsal.disposable_storage_problems(config, PROJECT, str(work), existing)


def _compare(before, after, expected=()):
    return {row.table: (row.state, row.failed) for row in rehearsal.compare_row_counts(before, after, expected)}


@pytest.mark.parametrize(("name", "allowed"), [
    ("financial-planner", False),
    ("fp-smoke", False),
    ("rehearsal-1", False),
    ("fp-rehearsal-", False),
    ("fp-rehearsal-ABC", False),
    ("fp-rehearsal-../x", False),
    ("fp-rehearsal-x\n", False),
    ("", False),
    ("fp-rehearsal-1a2b", True),
    ("fp-rehearsal-pr-538_2", True),
])
def test_only_a_prefixed_throwaway_project_may_be_torn_down(name, allowed):
    reason = rehearsal.project_name_problem(name)

    assert (reason is None) is allowed
    if name == "financial-planner":
        assert reason == "refusing the production project financial-planner"
    if not allowed:
        with pytest.raises(rehearsal.RehearsalError, match="project"):
            rehearsal.assert_project_name(name)


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


@pytest.mark.parametrize("argv", [
    ["--expect-count-change", "finance_alert,finance_aiconversation"],
    ["--expect-count-change", "finance_alert", "finance_aiconversation"],
    ["--expect-count-change", "finance_alert, ", "--expect-count-change", "finance_aiconversation"],
])
def test_expected_tables_split_a_comma_list_from_the_command_line(argv):
    args = rehearsal.parse_arguments([*argv, "--project-name", "fp-rehearsal-x"])

    assert args.expect_count_change == ["finance_alert", "finance_aiconversation"]
    assert rehearsal.parse_arguments([]).expect_count_change == []


def test_count_rows_print_table_names_counts_and_states_only():
    rows = rehearsal.compare_row_counts(
        {"finance_transaction": 2720, "finance_alert": 84},
        {"finance_transaction": 2720, "finance_alert": 60, "finance_reconciliationrun": 0})
    lines = [rehearsal.format_count_row(row) for row in rows]

    assert [line.split() for line in lines] == [
        ["FAIL", "counts:", "finance_alert", "84", "->", "60", "changed"],
        ["ok", "counts:", "finance_reconciliationrun", "-", "->", "0", "new"],
        ["ok", "counts:", "finance_transaction", "2720", "->", "2720", "same"],
    ]
    # The .ps1's format string: '{0} counts: {1,-48} {2,9} -> {3,-9} {4}'.
    assert lines[0] == "FAIL counts: " + "finance_alert".ljust(48) + "        84 -> 60        changed"
    assert lines[1] == "ok   counts: " + "finance_reconciliationrun".ljust(48) + "         - -> 0         new"


def test_row_counts_reject_anything_but_table_and_count_lines():
    assert rehearsal.parse_row_counts(["auth_user\t4", "finance_transaction\t2720", ""]) == {
        "auth_user": 4, "finance_transaction": 2720,
    }
    with pytest.raises(rehearsal.RehearsalError) as refused:
        rehearsal.parse_row_counts(["auth_user\t4", "ERROR: something"])
    assert str(refused.value) == "row counts: unexpected output from row-counts.sh"


def test_verdict_names_the_candidate_and_counts_only():
    sha = "0123456789abcdef0123456789abcdef01234567"

    assert rehearsal.format_verdict(0, sha, 77, 16) == (
        "PASS deploy rehearsal of 0123456789ab: 77 tables compared, 16 migration(s) applied")
    assert rehearsal.format_verdict(2, sha, 77, 16) == (
        "FAIL deploy rehearsal of 0123456789ab: 2 problem(s); do not deploy")
    assert rehearsal.format_verdict(1, None, 0, 0) == (
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

    assert _problems(config, work) == [
        "volume receipts is named financial-planner-receipts",
        f"service backup volumes: bind {tmp_path / 'financial-planner-backups'} is outside the work directory",
        # Read-only is no exemption: a read-only bind of the Docker socket still drives the daemon.
        "service backup volumes: bind /etc/rclone.conf is outside the work directory",
    ]


def test_a_sibling_directory_sharing_the_prefix_is_outside_the_work_directory(tmp_path):
    work = tmp_path / "fp-rehearsal-t"
    config = {"services": {"backup": {"volumes": [
        {"type": "bind", "source": str(tmp_path / "fp-rehearsal-t-other"), "target": "/backups"},
    ]}}}

    assert _problems(config, work) == [
        f"service backup volumes: bind {tmp_path / 'fp-rehearsal-t-other'} is outside the work directory"]


def _nightly_names():
    names = []
    for stamp, signed in (("20261025T073000Z", True), ("20261026T073000Z", True), ("20261027T073000Z", False)):
        names.append(f"financial_planner_{stamp}.dump")
        if signed:
            names += [f"financial_planner_{stamp}.sha256", f"financial_planner_{stamp}.sha256.sig"]
    return names


def test_the_newest_signed_nightly_dump_is_chosen():
    # The names come from a root helper's listing: the nightly directory is root-only.
    assert rehearsal.newest_signed_dump(_nightly_names()) == "financial_planner_20261026T073000Z.dump"
    assert rehearsal.newest_signed_dump([]) is None
    assert rehearsal.newest_signed_dump(["financial_planner_x.dump\n", "other.dump", "other.sha256", "other.sha256.sig"]) is None


def test_the_throwaway_env_file_holds_dummy_values_and_its_own_volumes(tmp_path):
    env_file = tmp_path / "rehearsal.env"

    rehearsal.write_env_file(env_file, PROJECT, tmp_path / "backups", 41234, "abc123")

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


def test_the_port_writes_the_env_file_the_retained_script_writes(tmp_path):
    # The .ps1's New-RehearsalEnvFile lines, so the two give compose the same throwaway settings.
    source = (ROOT / "ops" / "deploy-rehearsal.ps1").read_text(encoding="utf-8")
    block = source[source.index("function New-RehearsalEnvFile"):source.index("| Set-Content -LiteralPath $Path")]
    names = [line.strip().strip('"').split("=", 1)[0] for line in block.splitlines() if line.strip().startswith('"')]
    assert len(names) == 15

    rehearsal.write_env_file(tmp_path / "rehearsal.env", PROJECT, "/b", 1, "abc")

    written = (tmp_path / "rehearsal.env").read_text(encoding="utf-8").splitlines()
    assert [line.split("=", 1)[0] for line in written] == names


def test_the_production_project_is_refused_before_any_docker_command(fake_docker, rehearse):
    log = fake_docker(plain=True, exit_code=0)

    code, out = rehearse("--project-name", "financial-planner")

    assert code == 1
    assert "FAIL rehearsal: refusing the production project financial-planner" in out
    assert out.rstrip().endswith("problem(s); do not deploy")
    assert _calls(log) == []


def test_unreachable_docker_fails_without_starting_or_tearing_down_anything(tmp_path, fake_docker, rehearse):
    log = fake_docker(plain=True, exit_code=1)

    # This checkout's own HEAD, as the operator runs it.
    code, out = rehearse("--project-name", "fp-rehearsal-test", repo=ROOT)

    assert code == 1
    assert "FAIL rehearsal: Docker is not reachable; start it yourself, this script never does" in out
    # Only the reachability probe ran: no compose command, so no up and no down.
    assert _calls(log) == ["info --format {{.ServerVersion}}"]
    assert not list(tmp_path.glob("fp-rehearsal-*"))


def test_a_missing_docker_cli_fails_like_an_unreachable_daemon(tmp_path, monkeypatch, rehearse):
    repo = _candidate_repo(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(tools))

    code, out = rehearse(repo=repo)

    assert code == 1
    assert "FAIL rehearsal: Docker is not reachable; start it yourself, this script never does" in out


@pytest.mark.parametrize("candidate", ["fp538-no-such-revision", "-x", "--output=/tmp/x"])
def test_a_candidate_that_is_not_a_commit_fails_before_docker(fake_docker, rehearse, candidate):
    log = fake_docker(plain=True, exit_code=0)

    code, out = rehearse(repo=ROOT, candidate=candidate)

    assert code == 1
    assert f"FAIL rehearsal: cannot resolve --candidate {candidate} to a commit in this checkout" in out
    assert _calls(log) == []


@pytest.mark.parametrize(("arguments", "problem"), [
    ((), "pass --config-path to rehearse the newest nightly dump, or --dump-path for a synthetic one"),
    (("--dump-path", "x", "--config-path", "y"), "pass --dump-path or --config-path, not both"),
    (("--dump-path", "no-such-file"), "--dump-path is not a file"),
    (("--config-path", "no-such-file"), "--config-path has no BACKUP_DIR"),
])
def test_the_dump_to_restore_must_be_named_one_way(fake_docker, rehearse, arguments, problem):
    log = fake_docker(plain=True, exit_code=0)

    code, out = rehearse(*arguments, dump=False)

    assert code == 1
    assert f"FAIL rehearsal: {problem}" in out
    assert _calls(log) == []


def test_a_missing_candidate_is_refused(fake_docker, capsys):
    log = fake_docker(plain=True, exit_code=0)

    assert rehearsal.main([]) == 1

    assert "FAIL rehearsal: pass --candidate, the git revision to rehearse" in capsys.readouterr().out
    assert _calls(log) == []


def test_a_planted_work_directory_is_never_mounted_or_emptied(tmp_path, fake_docker, rehearse):
    # Another local user pre-creates the directory a predictable work path
    # would use, with backups linking to real backups (#582 review).
    outside = tmp_path / "real-backups"
    (outside / "signing").mkdir(parents=True)
    (outside / "signing" / "backup-signing-key").write_text("synthetic sentinel", encoding="utf-8")
    planted = tmp_path / "fp-rehearsal-planted"
    planted.mkdir()
    (planted / "backups").symlink_to(outside, target_is_directory=True)
    log = fake_docker()

    code, _ = rehearse("--project-name", "fp-rehearsal-planted")

    assert code == 1
    assert (outside / "signing" / "backup-signing-key").read_text(encoding="utf-8") == "synthetic sentinel"
    assert (planted / "backups").is_symlink()
    calls = _calls(log)
    assert not [call for call in calls if str(planted) in call or str(outside) in call]
    # It got as far as emptying a backup tree: its own, in a fresh work directory it then removed.
    emptied = [call for call in calls if call.startswith(IN_ISOLATED + "run ") and "find /backups" in call]
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

    assert _problems(config, work) == [
        f"service backup volumes: bind {work / 'backups'} is outside the work directory",
        f"service backup volumes: bind {work / 'backups' / 'audit' / 'application'} is outside the work directory",
    ]


def test_each_work_directory_is_new_owner_only_and_not_named_after_the_project(tmp_path, monkeypatch):
    (tmp_path / "fp-rehearsal-x").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    first, second = Path(rehearsal.new_work_dir()), Path(rehearsal.new_work_dir())

    assert first != second
    for work in (first, second):
        assert work.parent == tmp_path
        assert work.name.startswith("fp-rehearsal-work-")
        assert not work.is_symlink()
        assert list(work.iterdir()) == []
        assert work.stat().st_mode & 0o777 == 0o700
    assert list((tmp_path / "fp-rehearsal-x").iterdir()) == []


def test_a_work_directory_is_identified_by_itself_not_by_where_a_link_points(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    identity = rehearsal.directory_identity(str(work))
    (tmp_path / "link").symlink_to(work, target_is_directory=True)
    (tmp_path / "file").write_text("x", encoding="utf-8")

    assert identity == f"{work.stat().st_dev}:{work.stat().st_ino}"
    assert rehearsal.directory_identity(str(tmp_path / "link")) is None
    assert rehearsal.directory_identity(str(tmp_path / "file")) is None
    assert rehearsal.directory_identity(str(tmp_path / "missing")) is None
    work.rename(tmp_path / "moved")
    work.mkdir()
    assert rehearsal.directory_identity(str(work)) != identity


def test_canonical_paths_follow_every_link_along_the_way(tmp_path):
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    (tmp_path / "relative").symlink_to("real", target_is_directory=True)
    (real / "up").symlink_to("..", target_is_directory=True)
    (tmp_path / "chain").symlink_to(tmp_path / "relative", target_is_directory=True)
    canonical = rehearsal.canonical_path

    assert canonical(str(tmp_path / "chain" / "inner")) == str(real / "inner")
    assert canonical(str(tmp_path / "relative" / "up" / "real" / "inner")) == str(real / "inner")
    # `..` after a link climbs from where the link lands, not from the link.
    assert canonical(f"{tmp_path / 'chain' / 'inner'}/../..") == str(tmp_path)
    # Parts compose will create are kept as written under the resolved parent.
    assert canonical(str(tmp_path / "chain" / "audit" / "application")) == str(real / "audit" / "application")
    assert canonical(f"{real}//inner/./") == str(real / "inner")
    assert canonical("/../..") == "/"


def test_a_path_that_cannot_be_resolved_is_refused(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    config = {"services": {"backup": {"volumes": [
        {"type": "bind", "source": str(tmp_path / "loop" / "backups"), "target": "/backups"},
        {"type": "bind", "source": "relative/backups", "target": "/backups"},
    ]}}}
    canonical = rehearsal.canonical_path

    assert canonical(str(tmp_path / "loop")) is None
    assert canonical("relative/backups") is None
    assert canonical(f"{tmp_path / 'missing'}/../x") is None
    assert canonical("") is None
    assert canonical(None) is None
    assert canonical(f"{tmp_path}/nul\0byte") is None
    assert _problems(config, tmp_path) == [
        f"service backup volumes: bind {tmp_path / 'loop' / 'backups'} is outside the work directory",
        "service backup volumes: bind relative/backups is outside the work directory"]
    assert rehearsal.under_directory(str(tmp_path / "x"), str(tmp_path / "loop")) is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read every directory, so no part of a path is unreadable")
def test_a_path_through_a_directory_that_cannot_be_read_is_refused(tmp_path):
    # What is behind it cannot be examined, and Docker, as root, would follow a link there.
    closed = tmp_path / "work" / "closed"
    closed.mkdir(parents=True)
    closed.chmod(0)
    try:
        assert rehearsal.canonical_path(str(closed / "backups")) is None
        assert rehearsal.under_directory(str(closed / "backups"), str(tmp_path / "work")) is False
    finally:
        closed.chmod(0o700)


def test_a_sibling_whose_name_ends_in_a_backslash_is_outside_the_work_directory(tmp_path):
    work = tmp_path / "work"
    work.mkdir()

    assert rehearsal.under_directory(str(work), str(work)) is True
    assert rehearsal.under_directory(str(work / "backups"), str(work)) is True
    assert rehearsal.under_directory(f"{work}\\", str(work)) is False
    assert rehearsal.under_directory(f"{work}\\/backups", str(work)) is False


def _torn_down(work):
    run = rehearsal.Rehearsal()
    run.project = PROJECT
    run.work_dir = str(work)
    run.work_dir_identity = rehearsal.directory_identity(str(work))
    run.built = True
    run.compose_file = "compose.yml"
    run.env_file = "rehearsal.env"
    run.isolated_id = FAKE_ID
    run.teardown()
    return run


def test_teardown_leaves_a_backup_directory_that_resolves_outside_the_work_directory(tmp_path, fake_docker):
    outside = tmp_path / "real-backups"
    outside.mkdir()
    (outside / "sentinel").write_text("synthetic sentinel", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "backups").symlink_to(outside, target_is_directory=True)
    log = fake_docker()

    run = _torn_down(work)

    assert (outside / "sentinel").read_text(encoding="utf-8") == "synthetic sentinel"
    assert "teardown: the backup directory no longer resolves inside the work directory; left it unemptied" in run.failures
    assert not any(" run " in f" {call} " for call in _calls(log))


def test_teardown_empties_its_own_backup_directory_inside_the_isolated_daemon(tmp_path, fake_docker):
    work = tmp_path / "work"
    (work / "backups" / "nightly").mkdir(parents=True)
    (work / "backups" / "nightly" / "synthetic.dump").write_text("synthetic", encoding="utf-8")
    log = fake_docker()

    run = _torn_down(work)

    assert run.failures == []
    assert list((work / "backups").iterdir()) == []
    assert _calls(log) == [
        IN_ISOLATED + "compose -p fp-rehearsal-t -f compose.yml --env-file rehearsal.env down --volumes --remove-orphans "
        "--timeout 10",
        IN_ISOLATED + "image inspect fp-rehearsal-t-backup",
        IN_ISOLATED + f"run --rm --network none -v {work / 'backups'}:/backups --entrypoint sh fp-rehearsal-t-backup "
        "-c find /backups -mindepth 1 -delete",
    ]


@pytest.mark.parametrize("project", ["financial-planner", "fp-smoke", ""])
def test_teardown_refuses_any_project_but_a_throwaway_one(tmp_path, fake_docker, project):
    log = fake_docker()
    run = rehearsal.Rehearsal()
    run.project = project
    run.isolated_id = FAKE_ID
    run.isolated_volume_owned = True

    for step in (run.teardown, run.stop_isolated_daemon, run.start_isolated_daemon):
        with pytest.raises(rehearsal.RehearsalError):
            step()

    assert _calls(log) == []


@pytest.mark.parametrize(("options", "problem"), [
    ({"external": True}, "volume receipts is external"),
    ({"external": {"name": "financial-planner-receipts"}}, "volume receipts is external"),
    ({"driver": "rclone"}, "volume receipts uses driver rclone"),
    ({"driver_opts": {"type": "none", "o": "bind", "device": "/srv/financial-planner/receipts"}},
     "volume receipts sets driver_opts"),
    ({"driver_opts": {"o": "rbind"}}, "volume receipts sets driver_opts"),
    ({"driver": "local", "driver_opts": {"device": "/dev/sdb1"}}, "volume receipts sets driver_opts"),
    ({"driver_opts": ["device=/dev/sdb1"]}, "volume receipts sets driver_opts"),
    ({"labels": {"keep": "yes"}}, "volume receipts sets labels"),
])
def test_a_prefixed_volume_whose_storage_is_not_a_plain_local_volume_is_reported(tmp_path, options, problem):
    # A project-prefixed name says nothing about where the volume's data lives (#582 review r2).
    config = {"volumes": {
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data", "driver": "local"},
        "receipts": {"name": "fp-rehearsal-t-receipts", **options},
    }}

    assert _problems(config, tmp_path) == [problem]


def test_a_volume_that_already_exists_is_reported(tmp_path):
    config = {"volumes": {
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data"},
        "receipts": {"name": "fp-rehearsal-t-receipts"},
    }}

    assert _problems(config, tmp_path, ["fp-rehearsal-t-receipts", "financial-planner-receipts"]) == [
        "volume receipts (fp-rehearsal-t-receipts) already exists"]


def _isolated_daemon_lifecycle(call):
    """The script's own start and removal of the isolated daemon."""
    return call in (f"start {FAKE_ID}", f"rm --force --volumes {FAKE_ID}") or call.startswith(
        "volume rm --force fp-rehearsal-")


def _started(log):
    """Docker calls that build, start, run or remove anything of the candidate's.

    A command inside the isolated container counts by what it runs there.
    """
    calls = [call.removeprefix(IN_ISOLATED) for call in _calls(log)]
    return [call for call in calls
            if not _isolated_daemon_lifecycle(call)
            and (call.startswith("run ") or any(f" {verb} " in f" {call} " for verb in ("build", "up", "down", "rm")))]


def test_a_prefixed_volume_bound_to_an_outside_directory_stops_before_build(tmp_path, fake_docker, rehearse):
    outside = tmp_path / "real-receipts"
    outside.mkdir()
    (outside / "receipt.pdf").write_text("synthetic sentinel", encoding="utf-8")
    log = fake_docker(volumes={
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data"},
        "receipts": {"name": "fp-rehearsal-t-receipts",
                     "driver_opts": {"type": "none", "o": "bind", "device": str(outside)}},
    })

    code, out = rehearse()

    assert code == 1
    assert "FAIL rehearsal: storage outside the throwaway project: volume receipts sets driver_opts" in out
    assert (outside / "receipt.pdf").read_text(encoding="utf-8") == "synthetic sentinel"
    assert _started(log) == []


def test_a_volume_left_from_before_is_refused_not_reused_or_removed(fake_docker, rehearse):
    log = fake_docker(volumes={
        "postgres_data": {"name": "fp-rehearsal-t-postgres-data"},
        "receipts": {"name": "fp-rehearsal-t-receipts"},
    }, existing_volumes=["fp-rehearsal-t-postgres-data"])

    code, out = rehearse()

    assert code == 1
    assert ("FAIL rehearsal: storage outside the throwaway project: "
            "volume postgres_data (fp-rehearsal-t-postgres-data) already exists") in out
    assert _started(log) == []


def test_a_work_directory_replaced_by_a_link_after_validation_is_not_emptied(tmp_path, fake_docker, rehearse):
    # Someone who can write the work directory's parent swaps it for a link to
    # real backups once the mounts have been checked (#582 review r2).
    outside = tmp_path / "real-parent"
    (outside / "backups" / "signing").mkdir(parents=True)
    (outside / "backups" / "signing" / "backup-signing-key").write_text("synthetic sentinel", encoding="utf-8")
    log = fake_docker(swap_work_dir_to=str(outside))

    code, out = rehearse()

    assert code == 1
    assert (outside / "backups" / "signing" / "backup-signing-key").read_text(encoding="utf-8") == "synthetic sentinel"
    assert "FAIL teardown: the work directory was replaced after it was created; left it unemptied" in out
    # The link is left where it is too: removing "the work directory" would follow nothing, but it is not this run's.
    assert "was replaced after it was created; left it in place" in out
    assert len(list(tmp_path.glob("fp-rehearsal-work-*"))) == 2
    assert not [call for call in _calls(log) if "find /backups" in call]


# The preflight is an allowlist (#582 review 6451190854). Each of these could
# reach production storage or the host; each must stop the rehearsal before it
# builds, starts, runs or tears down anything.
_PROD = "container:financial-planner-backup-1"
_REFUSED_SERVICES = [
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
    pytest.param({"security_opt": "seccomp=unconfined"}, "service sneaky security_opt: seccomp=unconfined is not allowed",
                 id="security_opt-string"),
    pytest.param({"userns_mode": "host"}, "service sneaky userns_mode: shares the host's user namespace", id="userns_mode"),
    pytest.param({"extends": {"service": "backup"}}, "service sneaky extends: pulls in a service the guard has not read",
                 id="extends"),
    pytest.param({"labels": {"com.docker.compose.project": "financial-planner"}},
                 "service sneaky labels: not a key the guard knows", id="unknown-labels"),
    pytest.param({"env_file": [{"path": "/srv/financial-planner/.env"}]},
                 "service sneaky env_file: reads a host file into the container's environment", id="env_file"),
    pytest.param({"volumes": [{"type": "npipe", "source": "/var/run/docker.sock", "target": "/sock"}]},
                 "service sneaky volumes: mount type 'npipe' is not bind, volume or tmpfs", id="mount-npipe"),
    pytest.param({"volumes": [{"type": "image", "source": "financial-planner-app", "target": "/img"}]},
                 "service sneaky volumes: mount type 'image' is not bind, volume or tmpfs", id="mount-image"),
    pytest.param({"volumes": ["/var/run/docker.sock:/sock"]},
                 "service sneaky volumes: mount type '' is not bind, volume or tmpfs", id="mount-short-form"),
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
    pytest.param({"volumes": [{"type": "bind", "source": "{BACKUP_DIR}", "target": "/b", "bind": "rshared"}]},
                 "service sneaky volumes: a bind mount sets bind to something other than a mapping", id="mount-bind-not-a-mapping"),
    pytest.param({"volumes": [{"type": "tmpfs", "source": "/srv", "target": "/t"}]},
                 "service sneaky volumes: a tmpfs mount sets source", id="mount-tmpfs-source"),
    pytest.param({"build": {"context": "/srv/financial-planner"}},
                 "service sneaky build: context /srv/financial-planner is outside the work directory", id="build-context"),
    pytest.param({"build": "/srv/financial-planner"}, "service sneaky build: context  is outside the work directory",
                 id="build-short-form"),
    pytest.param({"build": {"context": "{WORK}/source", "network": "host"}}, "service sneaky build: sets network",
                 id="build-option"),
    pytest.param({"build": {"context": "{WORK}/source"}, "image": "financial-planner-app"},
                 "service sneaky image: financial-planner-app is not a fp-rehearsal-t- image", id="build-retags-production"),
    pytest.param({"networks": {"production": None}}, "service sneaky networks: production is not a project network",
                 id="undeclared-network"),
    pytest.param({"networks": ["production"]}, "service sneaky networks: not a mapping the guard can read",
                 id="networks-not-a-mapping"),
]


@pytest.mark.parametrize(("spec", "problem"), _REFUSED_SERVICES)
def test_a_service_outside_the_allowlist_stops_before_anything_starts(fake_docker, rehearse, spec, problem):
    log = fake_docker(services={"sneaky": {"image": "alpine", **spec}})

    code, out = rehearse()

    assert code == 1
    assert "FAIL rehearsal: storage outside the throwaway project: " in out
    assert problem in out
    assert _started(log) == []


def test_every_service_refusal_of_the_retained_script_has_a_case_here():
    # Each refusal sentence the .ps1's guard can print about a service, by its fixed words.
    source = (ROOT / "ops" / "deploy-rehearsal.ps1").read_text(encoding="utf-8")
    cases = " ".join(case.values[1] for case in _REFUSED_SERVICES)
    for phrase in ("is not bind, volume or tmpfs", "is outside the work directory", "is not a declared project volume",
                   "shares a namespace outside the project", "is not allowed", "is not a project network",
                   "not a key the guard knows", "build: sets", "build: context", "image:"):
        assert phrase in source and phrase in cases
    for reason in rehearsal.REFUSED_SERVICE_KEYS.values():
        assert reason in source.replace("''", "'") and reason in cases


_REFUSED_TOP_LEVEL = [
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
    pytest.param({"networks": {"default": {"name": "fp-rehearsal-t_default", "ipam": {"driver": "x"}}}},
                 "network default sets ipam", id="network-ipam"),
    pytest.param({"networks": ["default"]}, "top-level networks: not a mapping the guard can read", id="networks-not-a-mapping"),
]


@pytest.mark.parametrize(("top", "problem"), _REFUSED_TOP_LEVEL)
def test_a_top_level_key_outside_the_allowlist_stops_before_anything_starts(fake_docker, rehearse, top, problem):
    log = fake_docker(top_level=top)

    code, out = rehearse()

    assert code == 1
    assert problem in out
    assert _started(log) == []


@pytest.mark.parametrize(("config", "problem"), [
    (["services"], "compose config: not a mapping the guard can read"),
    ({"services": ["app"]}, "top-level services: not a mapping the guard can read"),
    ({"volumes": ["data"]}, "top-level volumes: not a mapping the guard can read"),
    ({"services": {"app": "alpine"}}, "service app: not a mapping the guard can read"),
    ({"volumes": {"data": None}}, "volume data is named "),
])
def test_a_model_of_another_shape_is_refused_not_skipped(tmp_path, config, problem):
    assert _problems(config, tmp_path) == [problem]


def test_the_allowlists_are_the_retained_scripts():
    """The .ps1's key lists, read from its source, are the port's: nothing more is let through."""
    source = (ROOT / "ops" / "deploy-rehearsal.ps1").read_text(encoding="utf-8")

    def listed(name):
        start = source.index(f"${name} = @(") + len(f"${name} = @(")
        return tuple(item.strip().strip("'") for item in source[start:source.index(")", start)].split(","))

    def table(name):
        start = source.index(f"${name} = ")
        body = source[source.index("{", start) + 1:source.index("\n}", start)]
        rows = (line.split("=", 1) for line in body.splitlines() if "=" in line)
        return {key.strip(): value.strip() for key, value in rows}

    def items(value):
        return tuple(item.strip().strip("'") for item in value.removeprefix("@(").removesuffix(")").split(","))

    assert listed("KnownTopLevelKeys") == rehearsal.KNOWN_TOP_LEVEL_KEYS
    assert listed("KnownServiceKeys") == rehearsal.KNOWN_SERVICE_KEYS
    assert listed("KnownVolumeKeys") == rehearsal.KNOWN_VOLUME_KEYS
    assert listed("KnownNetworkKeys") == rehearsal.KNOWN_NETWORK_KEYS
    assert listed("BuiltServices") == rehearsal.BUILT_SERVICES
    assert listed("GrowOnlyTables") == rehearsal.GROW_ONLY_TABLES
    assert {key: items(value) for key, value in table("KnownMountKeys").items()} == rehearsal.KNOWN_MOUNT_KEYS
    assert {key: items(value) for key, value in table("KnownMountOptions").items()} == rehearsal.KNOWN_MOUNT_OPTIONS
    refused = {key: value.strip("'\"").replace("''", "'") for key, value in table("RefusedServiceKeys").items()}
    assert refused == rehearsal.REFUSED_SERVICE_KEYS
    assert "$key -cnotin @('context', 'dockerfile', 'args')" in source
    assert rehearsal.KNOWN_BUILD_KEYS == ("context", "dockerfile", "args")
    assert "-notin @('no-new-privileges', 'no-new-privileges:true')" in source
    assert rehearsal.ALLOWED_SECURITY_OPTIONS == ("no-new-privileges", "no-new-privileges:true")
    assert f"$IsolatedImage = '{rehearsal.ISOLATED_IMAGE}'" in source
    assert f"$IsolatedSocket = '{rehearsal.ISOLATED_SOCKET}'" in source
    # The port is Linux only, so docker gets the .ps1's names without the other host's.
    assert set(rehearsal.DOCKER_ENV_NAMES) < set(listed("DockerEnvNames"))
    assert listed("DockerEnvPrefixes") == rehearsal.DOCKER_ENV_PREFIXES


@pytest.mark.parametrize(("compose_text", "problem"), [
    pytest.param("include:\n  - /srv/financial-planner/compose.yml\n" + BENIGN,
                 "compose.yml include: pulls in compose files the guard has not read", id="include"),
    pytest.param(BENIGN + "  app:\n    Extends:\n      file: /srv/financial-planner/compose.yml\n      service: backup\n",
                 "compose.yml extends: pulls in compose files the guard has not read", id="extends"),
    pytest.param(BENIGN + '"\\x69nclude": [/srv/financial-planner/compose.yml]\n',
                 "compose.yml: a backslash escape could spell include, extends or env_file", id="escaped-key"),
    pytest.param(BENIGN + "? !!binary aW5jbHVkZQ==\n: [/srv/financial-planner/compose.yml]\n",
                 "compose.yml: a YAML tag could spell include, extends or env_file", id="binary-key"),
])
def test_a_compose_file_that_pulls_in_another_stops_before_anything_starts(tmp_path, fake_docker, rehearse, compose_text,
                                                                         problem):
    # Compose resolves include and extends before `config` prints the model,
    # so the guard reads the candidate's compose.yml itself.
    log = fake_docker()

    code, out = rehearse(repo=_candidate_repo(tmp_path, compose_text))

    assert code == 1
    assert f"FAIL rehearsal: compose file the guard cannot vouch for: {problem}" in out
    assert _started(log) == []
    assert not [call for call in _calls(log) if " config " in f" {call} "]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_a_compose_file_in_another_encoding_is_refused_not_read_past(tmp_path, encoding):
    # YAML may be UTF-16 or UTF-32, where "include" is other bytes than the guard looks for.
    path = tmp_path / "compose.yml"
    path.write_bytes(("include:\n  - /srv/financial-planner/compose.yml\n" + BENIGN).encode(encoding))

    assert rehearsal.compose_source_problems(path) == [
        "compose.yml: not UTF-8 text, where the guard could miss include, extends or env_file"]


def test_a_plain_compose_file_has_nothing_the_guard_refuses(tmp_path):
    path = tmp_path / "compose.yml"
    path.write_text(BENIGN, encoding="utf-8")

    assert rehearsal.compose_source_problems(path) == []
    assert rehearsal.compose_source_problems(ROOT / "compose.yml") == []


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
        "volumes": {"receipts": {"name": "fp-rehearsal-t-receipts", "driver": "local", "external": False}},
        "services": {"app": {
            "build": {"context": "{WORK}/source", "dockerfile": "Dockerfile", "args": {"GIT_SHA": "abc"}},
            "image": "fp-rehearsal-t-app",
            "networks": {"default": None},
            "tmpfs": ["/run/csv-staging:size=128m,mode=1777"],
            "security_opt": ["no-new-privileges:true"],
            "volumes": mounts,
        }},
    }).replace("{WORK}", str(work)))

    assert _problems(config, work) == []


def _resolved_model(work):
    """What the real `docker compose config` prints for work/source/compose.yml; it needs no daemon."""
    env_file = work / "rehearsal.env"
    rehearsal.write_env_file(env_file, PROJECT, work / "backups", 41234, "abc123")
    config = rehearsal.run_docker(["compose", "-p", PROJECT, "-f", str(work / "source" / "compose.yml"),
                                   "--env-file", str(env_file), "config", "--no-env-resolution", "--format", "json"])
    assert config.exit_code == 0
    return "\n".join(config.output)


def test_the_checked_in_compose_file_passes_the_allowlist(tmp_path):
    # The source sits in the work directory as the rehearsal extracts it.
    work = tmp_path / "work"
    (work / "source").mkdir(parents=True)
    shutil.copy(ROOT / "compose.yml", work / "source" / "compose.yml")

    model = json.loads(_resolved_model(work))

    assert rehearsal.compose_source_problems(work / "source" / "compose.yml") == []
    assert _problems(model, work) == []
    assert rehearsal.candidate_file_problem(work / "source" / "compose.yml", work / "source") is None


_DB_IMAGE = "    image: postgres:18-alpine\n"
_ENV_FILE_FORMS = [
    pytest.param("    env_file: {env}\n", id="string"),
    pytest.param("    env_file:\n      - {env}\n", id="list"),
    pytest.param("    env_file:\n      - path: {env}\n        required: false\n", id="long-form"),
]


def _outside_env(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    env = outside / "production.env"
    env.write_text("SYNTHETIC_SECRET=synthetic-sentinel-value\n", encoding="utf-8")
    return env


@pytest.mark.parametrize("form", _ENV_FILE_FORMS)
def test_an_env_file_from_outside_the_work_directory_stops_before_anything_starts(tmp_path, fake_docker, rehearse, form):
    # Resolving env files would fold the host file into `environment`, a key
    # the guard allows, and drop `env_file` from the model (#582 review r4 F1).
    # Compose before 5 does that even with --no-env-resolution, so the text is
    # refused before `config` reads it.
    repo = _candidate_with(tmp_path, _DB_IMAGE, form.format(env=_outside_env(tmp_path)))
    log = fake_docker(real_docker=REAL_DOCKER, build_exit=1)

    code, out = rehearse(repo=repo)

    assert code == 1
    assert ("FAIL rehearsal: compose file the guard cannot vouch for: "
            "compose.yml env_file: reads a host file into the container's environment") in out
    assert _started(log) == []
    calls = _calls(log)
    assert not [call for call in calls if " config " in f" {call} "]
    assert "synthetic-sentinel-value" not in out + "\n".join(calls)


@pytest.mark.parametrize("form", _ENV_FILE_FORMS)
def test_an_env_file_through_the_real_normalizer_is_refused_by_the_model_check(tmp_path, form):
    # The same candidates as above, past the text refusal: the real `docker
    # compose config --no-env-resolution` normalizes them (no daemon) and the
    # model check judges the result. Compose 5 keeps `env_file` and never reads
    # the file, so the model check refuses it. Compose before 5 reads the file
    # anyway and drops the key, which is why the text refusal comes first.
    source = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert source.count(_DB_IMAGE) == 1
    work = tmp_path / "work"
    (work / "source").mkdir(parents=True)
    (work / "source" / "compose.yml").write_text(
        source.replace(_DB_IMAGE, _DB_IMAGE + form.format(env=_outside_env(tmp_path))), encoding="utf-8")
    version = subprocess.run([REAL_DOCKER, "compose", "version", "--short"], capture_output=True, text=True,
                             check=True).stdout.strip().lstrip("v")

    resolved = _resolved_model(work)
    db = json.loads(resolved)["services"]["db"]

    if int(version.split(".")[0]) >= 5:
        assert "env_file" in db
        assert "synthetic-sentinel-value" not in resolved
        assert _problems(json.loads(resolved), work) == [
            "service db env_file: reads a host file into the container's environment"]
    else:
        assert "env_file" not in db
        assert db["environment"]["SYNTHETIC_SECRET"] == "synthetic-sentinel-value"


_BACKUP_CONTEXT = "      context: ops/backup\n"


@pytest.mark.parametrize(("dockerfile", "files", "shown"), [
    pytest.param("{outside}", None, "{outside}", id="absolute"),
    pytest.param("../../../../outside/Dockerfile", None, "../../../../outside/Dockerfile", id="dotdot"),
    pytest.param("Dockerfile.linked", {"ops/backup/Dockerfile.linked": "{outside}"}, "Dockerfile.linked",
                 id="in-tree-symlink"),
    pytest.param(None, {"ops/backup/Dockerfile": "{outside}"}, "Dockerfile", id="default-is-a-symlink"),
])
def test_a_dockerfile_outside_the_work_directory_stops_before_anything_starts(tmp_path, fake_docker, rehearse, dockerfile,
                                                                            files, shown):
    # Checking the build context alone left the Dockerfile path free (#582 review r4 F2).
    outside = tmp_path / "outside" / "Dockerfile"
    outside.parent.mkdir()
    outside.write_text("FROM scratch\n# synthetic sentinel\n", encoding="utf-8")
    addition = f"      dockerfile: {dockerfile.format(outside=outside)}\n" if dockerfile else ""
    links = {name: Path(target.format(outside=outside)) for name, target in (files or {}).items()}
    repo = _candidate_with(tmp_path, _BACKUP_CONTEXT, addition, links)
    log = fake_docker(real_docker=REAL_DOCKER, build_exit=1)

    code, out = rehearse(repo=repo)

    assert code == 1
    assert "FAIL rehearsal: storage outside the throwaway project: service backup build: dockerfile " in out
    assert f"dockerfile {shown.format(outside=outside)} is outside the work directory" in out
    assert _started(log) == []


def test_an_in_tree_dockerfile_passes_and_the_rehearsal_builds(tmp_path, fake_docker, rehearse):
    repo = _candidate_with(tmp_path, _BACKUP_CONTEXT, "      dockerfile: Dockerfile.rehearsal\n",
                           {"ops/backup/Dockerfile.rehearsal": "FROM scratch\n"})
    log = fake_docker(real_docker=REAL_DOCKER, build_exit=1)

    _, out = rehearse(repo=repo)

    assert "ok   storage: only new fp-rehearsal-t-* volumes" in out
    assert "FAIL rehearsal: building the candidate images failed" in out
    configs = [call for call in _calls(log) if " config " in f" {call} "]
    assert configs and all("--no-env-resolution" in call for call in configs)
    assert any(" build " in f" {call} " for call in _started(log))


# git archive keeps a committed symlink, so the candidate's compose.yml itself
# could be a link to a compose file outside the work directory whose inline
# settings pass the allowlist (#582 review 6457345701).
_OUTSIDE_COMPOSE = (
    "services:\n"
    "  db:\n    image: postgres:18-alpine\n    environment:\n      POSTGRES_PASSWORD: synthetic-database-only\n"
    "  app:\n    image: alpine:3.22\n"
    "  backup:\n    image: alpine:3.22\n    environment:\n      SYNTHETIC_SECRET: outside-compose-sentinel\n"
    "  background:\n    image: alpine:3.22\n"
)


@pytest.mark.parametrize("target", [
    pytest.param("{outside}", id="absolute-outside"),
    pytest.param("../../outside/production.yml", id="relative-outside"),
    pytest.param("ops/compose.linked.yml", id="in-tree-link"),
])
def test_a_compose_file_that_is_a_symlink_stops_before_it_is_read(tmp_path, fake_docker, rehearse, target):
    outside = tmp_path / "outside" / "production.yml"
    outside.parent.mkdir()
    outside.write_text(_OUTSIDE_COMPOSE, encoding="utf-8")
    repo = _candidate_repo(tmp_path)
    (repo / "ops").mkdir()
    (repo / "ops" / "compose.linked.yml").write_text(_OUTSIDE_COMPOSE, encoding="utf-8")
    (repo / "compose.yml").unlink()
    (repo / "compose.yml").symlink_to(target.format(outside=outside))
    _commit(repo, "linked compose.yml")
    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "HEAD", "compose.yml"], check=True, capture_output=True,
                            text=True)
    assert listed.stdout.startswith("120000 ")
    log = fake_docker(real_docker=REAL_DOCKER)

    code, out = rehearse(repo=repo)

    assert code == 1
    assert ("FAIL rehearsal: compose file the guard cannot vouch for: "
            "compose.yml is a symbolic link, which the guard does not follow") in out
    assert _started(log) == []
    calls = _calls(log)
    assert not [call for call in calls if " config " in f" {call} "]
    assert "outside-compose-sentinel" not in out + "\n".join(calls)


@pytest.mark.parametrize(("make", "problem"), [
    pytest.param(lambda source: (source / "ops").symlink_to(source.parent / "elsewhere"),
                 "ops/compose.yml: ops is a symbolic link, which the guard does not follow", id="linked-directory"),
    pytest.param(lambda source: (source / "ops").mkdir(),
                 "ops/compose.yml is not a regular file in the candidate source", id="missing"),
    pytest.param(lambda source: (source / "ops" / "compose.yml").mkdir(parents=True),
                 "ops/compose.yml is not a regular file in the candidate source", id="a-directory"),
])
def test_a_candidate_file_behind_a_link_or_not_a_file_is_refused(tmp_path, make, problem):
    source = tmp_path / "work" / "source"
    source.mkdir(parents=True)
    (tmp_path / "work" / "elsewhere").mkdir()
    (tmp_path / "work" / "elsewhere" / "compose.yml").write_text(BENIGN, encoding="utf-8")
    make(source)

    assert rehearsal.candidate_file_problem(source / "ops" / "compose.yml", source) == problem


def test_a_candidate_source_that_is_itself_a_link_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "compose.yml").write_text(BENIGN, encoding="utf-8")
    (tmp_path / "source").symlink_to(real, target_is_directory=True)

    assert rehearsal.candidate_file_problem(tmp_path / "source" / "compose.yml", tmp_path / "source") == (
        "compose.yml: . is a symbolic link, which the guard does not follow")
    assert rehearsal.candidate_file_problem(tmp_path / "compose.yml", real) == (
        "../compose.yml is not a regular file in the candidate source")


def test_a_plain_candidate_file_may_be_read(tmp_path):
    source = tmp_path / "work" / "source"
    (source / "ops").mkdir(parents=True)
    (source / "ops" / "compose.yml").write_text(BENIGN, encoding="utf-8")

    assert rehearsal.candidate_file_problem(source / "ops" / "compose.yml", source) is None


# Owner decision 2026-10-28 (#538, after review r6 of #582): the rehearsal runs
# in its own isolated Docker daemon. Review r6 hid `backup` in an inactive
# profile: `config` leaves it out, so the allowlist never sees its mounts, yet
# `build` and `run backup` name it and select it anyway. Whatever the allowlist
# misses, the candidate's compose project must reach only the isolated daemon.
_HIDDEN_BACKUP = "  backup:\n    profiles: [hidden]\n"
_BACKUP_RECEIPTS = '      - receipts:/receipts\n    command: ["/opt/financial-planner/run-backups.sh"]\n'
# What the host's Docker may hear from the rehearsal itself.
_HOST_VERBS = {"info", "compose ls", "container ls", "volume ls", "create", "start", "exec", "rm", "volume rm", "run"}


def _host_verb(call):
    words = call.split(" ")
    return " ".join(words[:2]) if words[0] in ("compose", "container", "volume") else words[0]


def _isolated_calls(log):
    """What ran inside the isolated container, as its own arguments."""
    return [call.removeprefix(IN_ISOLATED) for call in _calls(log) if call.startswith("exec ")]


def test_a_service_hidden_in_an_inactive_profile_never_reaches_the_host_daemon(tmp_path, fake_docker, rehearse):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("synthetic sentinel", encoding="utf-8")
    created = outside / "created-by-hidden-service"
    source = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert source.count("  backup:\n") == 1 and source.count(_BACKUP_RECEIPTS) == 1
    source = source.replace("  backup:\n", _HIDDEN_BACKUP).replace(_BACKUP_RECEIPTS, (
        "      - type: bind\n"
        f"        source: {created}\n"
        "        target: /receipts\n"
        "        bind:\n"
        "          create_host_path: true\n"
        '    command: ["/opt/financial-planner/run-backups.sh"]\n'))
    repo = _candidate_with(tmp_path, "name: financial-planner\n", "", None)
    (repo / "compose.yml").write_text(source, encoding="utf-8")
    _commit(repo, "hide backup in a profile")
    log = fake_docker(real_docker=REAL_DOCKER, create_host_binds=True)

    _, out = rehearse(repo=repo)

    host = _calls(log)
    isolated = _isolated_calls(log)
    # The hidden service's writable bind outside the work directory was never
    # created on the host, and nothing else there changed.
    assert not created.exists()
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    # The host's Docker ran no compose project but listing projects, so it
    # never got the candidate's compose file except as a command it passed,
    # unread, into the isolated container.
    assert [call for call in host if call.startswith("compose ") and not call.startswith("compose ls ")] == []
    assert not [call for call in host if ("compose.yml" in call or "rehearsal.env" in call) and not call.startswith("exec ")]
    # The host's Docker heard only the script's own commands, none carrying a
    # DOCKER_HOST a candidate could have redirected (#582 automated review of
    # 1884e63: a socket in the work directory can be swapped for a link).
    assert not [record for record in _records(log) if "DOCKER_HOST" in record["env"]]
    assert {_host_verb(call) for call in host} <= _HOST_VERBS - {"run"}
    # Every compose call, the hidden service's build and run included, ran
    # inside the isolated container this run created, so compose read its
    # build inputs there too.
    assert all(call.startswith(IN_ISOLATED) for call in host if call.startswith("exec "))
    compose_isolated = [call for call in isolated if call.startswith("compose ")]
    assert any(" build " in f" {call} " for call in compose_isolated)
    assert any(" run " in call and " backup " in call for call in compose_isolated)
    assert out.rstrip().endswith("problem(s); do not deploy")


# What the stand-in reports for a candidate that migrates and stays healthy.
_HEALTHY = {
    "app": "running 0 healthy",
    "counts_before": {"auth_user": 4, "django_migrations": 91, "finance_alert": 84, "finance_transaction": 2720},
    "counts_after": {"auth_user": 4, "django_migrations": 93, "finance_alert": 60, "finance_transaction": 2720,
                     "finance_refundlinkdrop": 0},
    "reconcile": json.dumps({"accounts": 3, "counts": {"ok": 11, "mismatch": 0, "unverifiable": 2}}),
}


def test_no_command_for_the_candidate_runs_against_a_host_socket(tmp_path, fake_docker, rehearse, monkeypatch):
    """A whole rehearsal, through the post-deploy check and teardown (#789).

    Every docker and compose command for the candidate is a `docker exec` into
    the isolated container with the daemon's socket inside it. The host's
    Docker gets none of them directly, by argument or by environment.
    """
    # What an operator's shell may hold; none of it may steer where the candidate runs.
    monkeypatch.setenv("COMPOSE_FILE", "/srv/financial-planner/compose.yml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "financial-planner")
    monkeypatch.setenv("POSTGRES_VOLUME_NAME", "financial-planner-postgres-data")
    monkeypatch.setenv("BACKUP_DIR", "/srv/financial-planner-backups")
    log = fake_docker(**_HEALTHY)

    code, out = rehearse("--stable-seconds", "0", "--expect-count-change", "finance_alert")

    records = _records(log)
    host = [" ".join(record["args"]) for record in records]
    isolated = _isolated_calls(log)
    project_arguments = f"compose -p {PROJECT} -f "
    # It ran the whole rehearsal: each step for the candidate is there, inside the isolated container.
    for step in ("config --no-env-resolution --format json", "build app backup background", "up -d --wait --wait-timeout 180 db",
                 "/opt/financial-planner/restore.sh /rehearsal/synthetic.dump", "/opt/financial-planner/row-counts.sh",
                 "up -d app", "ps --all -q app", "/opt/financial-planner/backup.sh",
                 "exec -T app python manage.py reconcile_balances --json", "down --volumes --remove-orphans --timeout 10"):
        assert [call for call in isolated if call.startswith(project_arguments) and call.endswith(step)], step
    assert any(call.startswith("inspect --format ") for call in isolated)
    assert f"image inspect {PROJECT}-backup" in isolated
    # The post-deploy check's own docker calls went the same way, through the shim on its PATH.
    assert any(call.startswith("ps -a -q --no-trunc --filter label=com.docker.compose.project=" + PROJECT) for call in isolated)
    # Nothing that names the candidate's project, compose file, env file or a compose
    # verb reached the host's Docker except wrapped, unread, in `docker exec`.
    direct = [call for call in host if not call.startswith("exec ")]
    assert {_host_verb(call) for call in direct} <= _HOST_VERBS - {"exec", "run"}
    assert not [call for call in direct if "compose.yml" in call or "rehearsal.env" in call or f"-p {PROJECT}" in call]
    assert [call for call in direct if call.startswith("compose ")] == ["compose ls --all --format json"] * 3
    # Every exec is into the container this run created, with the socket inside it.
    assert all(call.startswith(IN_ISOLATED) for call in host if call.startswith("exec "))
    # No call names another daemon: not by environment, not by a client flag, not by a socket path.
    for record in records:
        assert not {"DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "POSTGRES_VOLUME_NAME",
                    "BACKUP_DIR"} & set(record["env"]), record["args"]
        sent = record["args"]
        assert sent[0] != "exec" or sent[1:5] == ["-e", "DOCKER_HOST=unix:///home/rootless/docker.sock", FAKE_ID, "docker"]
        own = sent[5:] if sent[0] == "exec" else sent
        assert not {"-H", "--host", "--context"} & set(own), sent
        hosts = [argument for argument in own if argument.startswith(("--host=", "--context=", "DOCKER_HOST="))]
        assert hosts == (["--host=unix:///home/rootless/docker.sock"] if sent[0] == "create" else []), sent
        assert not [argument for argument in sent if "/var/run/docker.sock" in argument or "/run/docker.sock" in argument]
    # The isolated container itself holds one host directory, the work directory, and no host socket.
    create = next(record["args"] for record in records if record["args"][0] == "create")
    mounts = [create[index + 1] for index, argument in enumerate(create) if argument == "-v"]
    assert len(mounts) == 2
    assert mounts[0] == f"{PROJECT}-isolated-docker:/home/rootless/.local/share/docker"
    work = mounts[1].split(":")[0]
    assert mounts[1] == f"{work}:{work}" and Path(work).parent == tmp_path and Path(work).name.startswith("fp-rehearsal-work-")
    assert "--privileged" not in create
    assert code == 1 and "FAIL post-deploy: the check exited 1" in out
    assert not list(tmp_path.glob("fp-rehearsal-*"))


def test_the_post_deploy_check_is_the_python_one(tmp_path, fake_docker, rehearse, monkeypatch):
    # #613's ops/post-deploy-check.py, run by this interpreter: its own lines, and no other interpreter starts.
    for name in ("pwsh", "powershell"):
        other = tmp_path / "bin" / name
        other.write_text(f'#!/bin/sh\necho "$0 $@" >> "{tmp_path / "other-interpreter.log"}"\n', encoding="utf-8")
        other.chmod(0o755)
    fake_docker(**_HEALTHY)
    helpers = []
    run_helper = rehearsal.Rehearsal.run_helper
    monkeypatch.setattr(rehearsal.Rehearsal, "run_helper",
                        lambda self, arguments: helpers.append(arguments) or run_helper(self, arguments))

    code, out = rehearse("--stable-seconds", "0", "--app-port", "9", "--expect-count-change", "finance_alert")

    assert code == 1
    assert len(helpers) == 1
    script, *arguments = helpers[0]
    assert script == str(ROOT / "ops" / "post-deploy-check.py")
    options = dict(zip(arguments[::2], arguments[1::2], strict=True))
    assert options.pop("--since").endswith("Z")
    assert options.pop("--status-path").endswith("/backups/health/status")
    assert len(options.pop("--expected-sha")) == 40
    assert options == {"--project-name": PROJECT, "--health-url": "http://127.0.0.1:9/health/", "--stable-seconds": "0"}
    # The Python check's wording, each line shown under the rehearsal's prefix.
    assert f"     post-deploy FAIL containers: db has no container in project {PROJECT}" in out
    assert "     post-deploy post-deploy check FAILED: " in out
    assert "FAIL post-deploy: the check exited 1" in out
    assert not (tmp_path / "other-interpreter.log").exists()
    assert ".ps1" not in SCRIPT.read_text(encoding="utf-8").split('"""', 2)[2]


def test_a_healthy_candidate_passes_with_table_names_counts_and_states_only(tmp_path, fake_docker, rehearse, monkeypatch):
    log = fake_docker(**_HEALTHY)
    monkeypatch.setattr(rehearsal.Rehearsal, "run_helper",
                        lambda self, arguments: rehearsal.Result(0, ["ok   health: 200", "post-deploy check passed"]))

    code, out = rehearse("--expect-count-change", "finance_alert", "--app-port", "41234")

    sha = subprocess.run(["git", "-C", str(tmp_path / "repo"), "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    work = next(call for call in _calls(log) if call.startswith("create ")).split(" -v ")[2].split(":")[0]
    assert code == 0
    assert out.splitlines() == [
        "ok   production: project financial-planner is absent",
        f"ok   candidate: {sha[:12]} as project fp-rehearsal-t on 127.0.0.1:41234",
        "ok   isolated: rootless daemon fp-rehearsal-t-isolated; compose for the candidate runs inside it, "
        "never against the host's Docker",
        "ok   storage: only new fp-rehearsal-t-* volumes, bind mounts under the work directory and tmpfs",
        "ok   build: app, backup, background",
        "ok   restore: synthetic dump (no signed manifest), 4 tables",
        "ok   counts: " + "auth_user".ljust(48) + "         4 -> 4         same",
        "ok   counts: " + "django_migrations".ljust(48) + "        91 -> 93        grew",
        "ok   counts: " + "finance_alert".ljust(48) + "        84 -> 60        expected",
        "ok   counts: " + "finance_refundlinkdrop".ljust(48) + "         - -> 0         new",
        "ok   counts: " + "finance_transaction".ljust(48) + "      2720 -> 2720      same",
        "ok   migrate: app healthy, 2 migration(s) applied",
        "ok   backup: the migrated database dumps and restores",
        "ok   reconcile: 3 account(s): 11 ok, 0 mismatch, 2 unverifiable",
        "     post-deploy ok   health: 200",
        "     post-deploy post-deploy check passed",
        "ok   post-deploy: passed",
        "ok   teardown: the isolated daemon is gone, and every container, volume and image of fp-rehearsal-t with it",
        "ok   production: project financial-planner is still absent",
        f"PASS deploy rehearsal of {sha[:12]}: 5 tables compared, 2 migration(s) applied",
    ]
    assert not Path(work).exists()
    # The synthetic dump is restored unverified, from the copy in the work directory.
    restore = next(call for call in _isolated_calls(log) if call.endswith("/restore.sh /rehearsal/synthetic.dump"))
    assert f"-v {work}/input/synthetic.dump:/rehearsal/synthetic.dump:ro -e RESTORE_ALLOW_UNVERIFIED=1 backup" in restore


def test_the_output_lines_are_the_retained_scripts():
    """Each fixed ok and FAIL sentence of the .ps1 is one the port prints, flag names aside."""
    def sentences(source):
        found = set()
        for quoted in re.findall(r"""(?:Add-Failure|Write-Pass|throw|fail|ok|RehearsalError)\(?\s*\(?f?(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)')""",
                                 source):
            words = re.sub(r"\$\([^()]*(?:\([^()]*\)[^()]*)*\)|\$\{?[A-Za-z:.]+\}?|\{[^{}]*\}", " ", (quoted[0] or quoted[1]))
            words = re.sub(r"(?<![A-Za-z])--?[A-Za-z][A-Za-z-]*", " ", words.replace("''", "'").replace("\\'", "'"))
            found.add(" ".join(re.findall(r"[A-Za-z_/.]+", words)[:4]))
        # A lone word is a label before a value ("rehearsal: <the error>"), not a sentence.
        return {sentence for sentence in found if " " in sentence}

    retained = sentences((ROOT / "ops" / "deploy-rehearsal.ps1").read_text(encoding="utf-8"))
    ported = sentences(SCRIPT.read_text(encoding="utf-8"))

    assert len(retained) > 50
    assert retained - ported == set()


def test_a_count_change_nobody_declared_fails_the_rehearsal(fake_docker, rehearse, monkeypatch):
    fake_docker(**_HEALTHY)
    monkeypatch.setattr(rehearsal.Rehearsal, "run_helper", lambda self, arguments: rehearsal.Result(0, []))

    code, out = rehearse()

    assert code == 1
    assert "FAIL counts: " + "finance_alert".ljust(48) + "        84 -> 60        changed" in out
    assert out.rstrip().endswith(": 1 problem(s); do not deploy")


@pytest.mark.parametrize(("reconcile", "known", "line"), [
    ({"accounts": 3, "counts": {"ok": 9, "mismatch": 2}}, 0,
     "FAIL reconcile: 3 account(s): 9 ok, 2 mismatch; more than --known-mismatch-windows 0"),
    ({"accounts": 3, "counts": {"ok": 9, "mismatch": 2}}, 2, "ok   reconcile: 3 account(s): 9 ok, 2 mismatch"),
    ({"accounts": 0, "counts": {}}, 0, "ok   reconcile: 0 account(s): "),
    ({"accounts": 3}, 0, "FAIL reconcile: reconcile_balances --json exited 0 without a summary"),
    ({"accounts": 3, "counts": {"mismatch": "many"}}, 0, "FAIL reconcile: reconcile_balances --json exited 0 without a summary"),
    ("Traceback (most recent call last): synthetic", 0, "FAIL reconcile: reconcile_balances --json exited 0 without a summary"),
])
def test_reconciliation_fails_on_more_mismatches_than_production_shows(fake_docker, rehearse, monkeypatch, reconcile, known,
                                                                     line):
    fake_docker(**{**_HEALTHY, "reconcile": reconcile if isinstance(reconcile, str) else json.dumps(reconcile)})
    monkeypatch.setattr(rehearsal.Rehearsal, "run_helper", lambda self, arguments: rehearsal.Result(0, []))

    code, out = rehearse("--expect-count-change", "finance_alert", "--known-mismatch-windows", str(known))

    assert line in out.splitlines()
    assert code == (1 if line.startswith("FAIL") else 0)
    assert "synthetic" not in out.replace("synthetic dump (no signed manifest)", "")


def test_a_migration_that_stops_the_app_fails_with_exception_types_only(fake_docker, rehearse):
    log = fake_docker(**{**_HEALTHY, "app": "restarting 3 none", "app_log": (
        "Traceback (most recent call last):\n"
        '  File "/app/finance/migrations/0099_synthetic.py", line 12, in forwards\n'
        "django.db.utils.IntegrityError: synthetic-row-value 1234.56\n")})

    code, out = rehearse()

    assert code == 1
    assert "FAIL migrate: app is restarting, health none, RestartCount 3 after migrating" in out
    assert "     app logged 1 traceback(s): django.db.utils.IntegrityError x1" in out
    assert "synthetic-row-value" not in out
    isolated = _isolated_calls(log)
    # The log was read inside the isolated daemon, and nothing ran after the failed migration but teardown.
    assert any(call.startswith("logs --since ") and call.endswith(" -- " + "a" * 64) for call in isolated)
    assert not [call for call in isolated if call.endswith(("up -d", "backup.sh", "--json"))]
    assert any(call.endswith("down --volumes --remove-orphans --timeout 10") for call in isolated)


def test_an_app_container_whose_state_cannot_be_read_is_not_healthy(fake_docker, rehearse):
    fake_docker(**{**_HEALTHY, "app": "running"})

    code, out = rehearse()

    assert code == 1
    assert "FAIL migrate: app is unknown, health none, RestartCount 0 after migrating" in out


def test_an_app_that_never_reports_healthy_times_out(fake_docker, rehearse, monkeypatch):
    fake_docker(**{**_HEALTHY, "app": "running 0 starting"})
    monkeypatch.setattr(rehearsal.time, "sleep", lambda seconds: None)

    code, out = rehearse("--migrate-timeout-seconds", "0")

    assert code == 1
    assert "FAIL migrate: app is running, health starting, RestartCount 0 after migrating" in out


def test_ctrl_c_still_tears_the_project_and_the_isolated_daemon_down(tmp_path, fake_docker, rehearse, monkeypatch):
    log = fake_docker(**_HEALTHY)

    def interrupted(self, timeout_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(rehearsal.Rehearsal, "wait_app_healthy", interrupted)

    code, out = rehearse()

    assert code == 1
    assert "FAIL rehearsal: interrupted" in out
    calls = _calls(log)
    assert any(call.endswith("down --volumes --remove-orphans --timeout 10") for call in calls)
    assert f"rm --force --volumes {FAKE_ID}" in calls
    assert "volume rm --force fp-rehearsal-t-isolated-docker" in calls
    assert not list(tmp_path.glob("fp-rehearsal-*"))


def test_an_error_the_script_did_not_write_is_reported_by_type_only(fake_docker, rehearse, monkeypatch):
    fake_docker()

    def broken(*arguments):
        raise ValueError("synthetic-row-value 1234.56")

    monkeypatch.setattr(rehearsal, "write_env_file", broken)

    code, out = rehearse()

    assert code == 1
    assert "FAIL rehearsal: ValueError" in out.splitlines()
    assert "synthetic-row-value" not in out


def test_a_production_project_that_changes_during_the_rehearsal_fails(fake_docker, rehearse, monkeypatch):
    fake_docker(compose_ls=json.dumps([{"Name": "financial-planner", "Status": "running(5)"}]))
    statuses = iter(["running(5)", "absent", "running(4)"])
    monkeypatch.setattr(rehearsal, "project_status", lambda name: next(statuses))

    code, out = rehearse()

    assert code == 1
    assert "ok   production: project financial-planner is running(5)" in out
    assert "FAIL production: project financial-planner went from running(5) to running(4) during the rehearsal" in out


def test_project_status_reads_compose_ls(fake_docker):
    fake_docker(compose_ls=json.dumps([{"Name": "financial-planner", "Status": "running(5)"},
                                       {"Name": "fp-rehearsal-t", "Status": "exited(1)"}]))

    assert rehearsal.project_status("financial-planner") == "running(5)"
    assert rehearsal.project_status("fp-rehearsal-t") == "exited(1)"
    assert rehearsal.project_status("fp-rehearsal-other") == "absent"


@pytest.mark.parametrize("config", [{"plain": True, "exit_code": 1}, {"compose_ls": "not json"}])
def test_a_project_listing_that_cannot_be_read_is_an_error_not_absent(fake_docker, config):
    fake_docker(**config)

    with pytest.raises(rehearsal.RehearsalError, match="docker compose ls failed"):
        rehearsal.project_status("financial-planner")

    run = rehearsal.Rehearsal()
    run.production_before = "running(5)"
    run.check_production()
    assert run.failures == [
        "production: project financial-planner went from running(5) to unreadable during the rehearsal"]


def test_a_project_that_already_exists_is_refused(fake_docker, rehearse):
    log = fake_docker(compose_ls=json.dumps([{"Name": PROJECT, "Status": "running(1)"}]))

    code, out = rehearse()

    assert code == 1
    assert "FAIL rehearsal: project fp-rehearsal-t already exists; remove it or pass another --project-name" in out
    assert _started(log) == []
    assert not [call for call in _calls(log) if call.startswith("create ")]


# Teardown must not remove what another run under the same --project-name
# created, trust a failed removal or listing, or list a root-only directory as
# the operator (#582 automated review of 1884e63).
def test_an_isolated_daemon_left_by_another_run_is_refused_not_removed(fake_docker, rehearse):
    log = fake_docker(on_host=["fp-rehearsal-t-isolated"])

    code, out = rehearse()

    assert code == 1
    assert ("FAIL rehearsal: fp-rehearsal-t-isolated or its volume already exists; "
            "another rehearsal may be running as fp-rehearsal-t") in out
    assert not [call for call in _calls(log) if call.startswith(("create ", "run -d ", "rm ", "volume rm "))]
    assert _started(log) == []


def test_an_isolated_volume_left_by_another_run_is_refused_not_removed(fake_docker, rehearse):
    log = fake_docker(on_host=["fp-rehearsal-t-isolated-docker"])

    code, out = rehearse()

    assert code == 1
    assert "FAIL rehearsal: fp-rehearsal-t-isolated or its volume already exists" in out
    assert not [call for call in _calls(log) if call.startswith(("create ", "rm ", "volume rm "))]


def test_teardown_that_cannot_remove_or_confirm_the_isolated_daemon_fails(fake_docker, capsys):
    fake_docker(plain=True, exit_code=1)
    run = rehearsal.Rehearsal()
    run.project = PROJECT
    run.isolated_id = FAKE_ID
    run.isolated_volume_owned = True

    run.stop_isolated_daemon()

    assert "teardown: could not remove the isolated daemon fp-rehearsal-t-isolated" in run.failures
    assert "teardown: could not remove the isolated daemon's volume fp-rehearsal-t-isolated-docker" in run.failures
    assert "teardown: cannot confirm that the isolated daemon fp-rehearsal-t-isolated and its volume are gone" in run.failures
    assert "ok   teardown:" not in capsys.readouterr().out


def test_teardown_that_leaves_the_isolated_daemon_behind_fails(fake_docker, capsys):
    fake_docker(on_host=["fp-rehearsal-t-isolated-docker"])
    run = rehearsal.Rehearsal()
    run.project = PROJECT
    run.isolated_id = FAKE_ID
    run.isolated_volume_owned = True

    run.stop_isolated_daemon()

    assert run.failures == ["teardown: the isolated daemon fp-rehearsal-t-isolated or its volume remains"]
    assert "ok   teardown:" not in capsys.readouterr().out


@pytest.mark.parametrize(("config", "problem"), [
    ({"plain": True, "exit_code": 1}, "cannot list containers and volumes on the host's Docker"),
    ({"plain": True, "exit_code": 0}, "creating the isolated daemon failed"),
    ({"fake_id": "not an id"}, "creating the isolated daemon failed"),
])
def test_an_isolated_daemon_that_cannot_be_created_stops_the_rehearsal(tmp_path, fake_docker, config, problem):
    log = fake_docker(**config)
    run = rehearsal.Rehearsal()
    run.project = PROJECT
    run.work_dir = str(tmp_path)

    with pytest.raises(rehearsal.RehearsalError) as refused:
        run.start_isolated_daemon()

    assert str(refused.value) == problem
    assert run.isolated_id is None and run.isolated_volume_owned is False
    assert not [call for call in _calls(log) if call.startswith(("start ", "exec "))]


@pytest.mark.parametrize(("answers", "problem"), [
    ([rehearsal.Result(0, ["name=seccomp,profile=builtin"])], "the isolated daemon is not rootless"),
    ([rehearsal.Result(1, [])] * 3,
     "the isolated daemon did not answer within 120 seconds (it runs as uid 1000 and needs the work directory)"),
])
def test_an_isolated_daemon_must_answer_and_be_rootless(tmp_path, fake_docker, monkeypatch, answers, problem):
    fake_docker()
    run = rehearsal.Rehearsal()
    run.project = PROJECT
    run.work_dir = str(tmp_path)
    replies = iter(answers)
    clock = iter(range(0, 1000, 100))
    monkeypatch.setattr(rehearsal.Rehearsal, "isolated", lambda self, arguments: next(replies))
    monkeypatch.setattr(rehearsal.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(rehearsal.time, "sleep", lambda seconds: None)

    with pytest.raises(rehearsal.RehearsalError) as refused:
        run.start_isolated_daemon()

    assert str(refused.value) == problem
    # It is this run's to remove, whatever it turned out to be.
    assert run.isolated_id == FAKE_ID and run.isolated_volume_owned is True


def test_nothing_talks_to_the_candidate_before_the_isolated_daemon_exists(fake_docker):
    log = fake_docker()
    run = rehearsal.Rehearsal()
    run.project = PROJECT
    run.compose_file = "compose.yml"
    run.env_file = "rehearsal.env"
    refusal = "the isolated daemon is not running, so nothing may talk to the candidate's project"

    for step in (lambda: run.compose(["config"]), lambda: run.isolated(["volume", "ls", "-q"]),
                 lambda: run.run_helper(["post-deploy-check.py"])):
        with pytest.raises(rehearsal.RehearsalError) as refused:
            step()
        assert str(refused.value) == refusal

    assert _calls(log) == []


def test_helpers_get_a_docker_that_runs_inside_the_isolated_container(tmp_path, fake_docker, monkeypatch):
    log = fake_docker()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    work = tmp_path / "fp-rehearsal-work-x"
    work.mkdir()

    with rehearsal.isolated_docker_path(FAKE_ID) as path:
        bin_dir = Path(path.split(os.pathsep)[0])
        shim = bin_dir / "docker"
        # A new directory of its own, owner-only, never the work directory the candidate can write.
        assert bin_dir.parent == tmp_path and bin_dir.name.startswith("fp-rehearsal-bin-") and bin_dir != work
        assert bin_dir.stat().st_mode & 0o777 == 0o700
        assert shim.stat().st_mode & 0o777 == 0o700
        assert path.split(os.pathsep)[1:] == os.environ["PATH"].split(os.pathsep)
        subprocess.run(["docker", "logs", "--since", "2026-11-25T00:00:00Z", "--", "abc"], check=True,
                       env={**os.environ, "PATH": path})

    assert not bin_dir.exists()
    assert _calls(log) == [IN_ISOLATED + "logs --since 2026-11-25T00:00:00Z -- abc"]


def test_docker_gets_only_its_own_settings_from_the_environment(fake_docker, monkeypatch):
    # Compose lets the environment override --env-file, so an exported
    # production setting could point the throwaway project at real storage.
    log = fake_docker()
    for name in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "POSTGRES_VOLUME_NAME", "RECEIPTS_VOLUME_NAME", "BACKUP_DIR",
                 "OFFSITE_RCLONE_REMOTE", "DJANGO_SECRET_KEY"):
        monkeypatch.setenv(name, "synthetic-production-setting")
    monkeypatch.setenv("DOCKER_CONFIG", "/home/operator/.docker")
    monkeypatch.setenv("BUILDKIT_PROGRESS", "plain")

    result = rehearsal.run_docker(["compose", "ls", "--all", "--format", "json"])

    assert result == rehearsal.Result(0, ["[]"])
    env = _records(log)[0]["env"]
    assert "synthetic-production-setting" not in env.values()
    assert env["DOCKER_CONFIG"] == "/home/operator/.docker" and env["BUILDKIT_PROGRESS"] == "plain"
    assert set(env) - {"PATH", "HOME", "TMPDIR", "XDG_RUNTIME_DIR", "DOCKER_CONFIG", "BUILDKIT_PROGRESS",
                       # What the stand-in's own interpreter adds.
                       "LC_CTYPE", "PWD", "SHLVL", "_", "OLDPWD"} == set()


def test_docker_output_is_returned_by_line_and_its_errors_are_dropped(tmp_path, monkeypatch, capfd):
    shim = tmp_path / "docker"
    shim.write_text("#!/bin/sh\nprintf 'one\\r\\n\\ntwo\\n'\necho 'synthetic-row-value' >&2\nexit 3\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    assert rehearsal.run_docker(["restore"]) == rehearsal.Result(3, ["one", "two"])

    captured = capfd.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_root_only_nightly_directory_is_listed_and_copied_by_a_root_helper(tmp_path, fake_docker, rehearse):
    backups = tmp_path / "production-backups"
    nightly = backups / "nightly"
    nightly.mkdir(parents=True)
    for name in _nightly_names():
        (nightly / name).write_text("synthetic", encoding="utf-8")
    # As the backup scheduler's umask leaves it: the operator cannot list it.
    nightly.chmod(0)
    config = tmp_path / "production.env"
    config.write_text(f"DJANGO_SECRET_KEY=synthetic-production-setting\nBACKUP_DIR={backups}\n", encoding="utf-8")
    log = fake_docker(nightly=_nightly_names())
    try:
        _, out = rehearse("--config-path", str(config), dump=False)
    finally:
        nightly.chmod(0o700)

    calls = _calls(log)
    helpers = [call for call in calls if call.startswith("run --rm --network none --user 0:0 ")]
    assert len(helpers) == 2, out
    listing, copy = helpers
    assert f"--mount type=bind,source={nightly},target=/nightly,readonly" in listing
    assert f"--mount type=bind,source={nightly},target=/nightly,readonly" in copy
    assert f"--mount type=bind,source={backups}/signing/backup-signing-key.pub,target=/signing.pub,readonly" in copy
    assert f" copy financial_planner_20261026T073000Z {os.getuid()}:{os.getgid()}" in copy
    assert all(f" {rehearsal.ISOLATED_IMAGE} -c " in call for call in helpers)
    # The isolated daemon gets the copy in the work directory, not BACKUP_DIR.
    restore = next(call for call in calls if call.startswith(IN_ISOLATED) and "restore.sh" in call)
    assert str(backups) not in restore
    assert "/input:/rehearsal:ro" in restore
    assert "/input/backup-signing-key.pub:/rehearsal-signing.pub:ro -e BACKUP_SIGNING_PUBLIC_KEY=/rehearsal-signing.pub" in restore
    assert "RESTORE_ALLOW_UNVERIFIED" not in restore
    assert "restore.sh /rehearsal/financial_planner_20261026T073000Z.dump" in restore
    assert not [call for call in calls if call.startswith("create ") and str(backups) in call]
    # Only BACKUP_DIR was read from the production env file.
    assert "synthetic-production-setting" not in out + "\n".join(calls)


@pytest.mark.parametrize(("config", "problem"), [
    ({"nightly": ["financial_planner_20261027T073000Z.dump"]}, "no signed nightly dump in BACKUP_DIR/nightly; run a backup first"),
    ({"plain": True, "exit_code": 0}, "no signed nightly dump in BACKUP_DIR/nightly; run a backup first"),
])
def test_a_nightly_directory_without_a_signed_dump_stops_the_rehearsal(tmp_path, fake_docker, rehearse, config, problem):
    env = tmp_path / "production.env"
    env.write_text(f"BACKUP_DIR={tmp_path / 'production-backups'}\n", encoding="utf-8")
    log = fake_docker(**config)

    code, out = rehearse("--config-path", str(env), dump=False)

    assert code == 1
    assert f"FAIL rehearsal: {problem}" in out
    assert not [call for call in _calls(log) if call.startswith(("create ", "exec "))]


def test_only_the_named_key_is_read_from_an_env_file(tmp_path):
    env = tmp_path / "production.env"
    env.write_text(
        "# BACKUP_DIR=/commented\n"
        "BACKUP_DIR=/first\n"
        "DJANGO_SECRET_KEY=synthetic-production-setting\n"
        "  export BACKUP_DIR = '/srv/financial planner/backups'   # the tower\n"
        'QUOTED="a \\"b\\" \\\\ c" # trailing\n'
        "PLAIN=value with spaces # comment\n"
        "HASH=abc#def\n",
        encoding="utf-8")

    assert rehearsal.read_env_value(env, "BACKUP_DIR") == "/srv/financial planner/backups"
    assert rehearsal.read_env_value(env, "QUOTED") == 'a "b" \\ c'
    assert rehearsal.read_env_value(env, "PLAIN") == "value with spaces"
    assert rehearsal.read_env_value(env, "HASH") == "abc#def"
    assert rehearsal.read_env_value(env, "MISSING") is None
    assert rehearsal.read_env_value(tmp_path / "missing.env", "BACKUP_DIR") is None
    assert rehearsal.read_env_value(tmp_path, "BACKUP_DIR") is None


def test_a_free_loopback_port_is_picked():
    port = rehearsal.free_port()

    assert 0 < port < 65536
