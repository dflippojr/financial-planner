import os
from pathlib import Path
import shutil
import subprocess

import pytest


BACKUP_SCRIPT = Path(__file__).parents[1] / "ops" / "backup" / "backup.sh"
RESTORE_SCRIPT = Path(__file__).parents[1] / "ops" / "backup" / "restore.sh"


def _posix_bash():
    if os.name != "nt":
        return shutil.which("bash")
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if git_bash.is_file():
        return str(git_bash)
    which = shutil.which("bash")
    if which and "Git" in which:
        return which
    return None


POSIX_BASH = _posix_bash()
NEEDS_BASH = POSIX_BASH is None


def _unix_path(path):
    posix = Path(path).resolve().as_posix()
    if os.name == "nt" and len(posix) >= 2 and posix[1] == ":":
        return "/" + posix[0].lower() + posix[2:]
    return posix


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _fake_date(fake_bin):
    _write_executable(
        fake_bin / "date",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = '-u' ]; then\n"
        "  if [ \"$2\" = '+%Y-%m-%dT%H:%M:%SZ' ]; then echo 2026-09-27T06:00:00Z; else echo 20260927T060000Z; fi\n"
        "else echo 7; fi\n",
    )


def _fake_pg(fake_bin, *, dump_fail=False, restore_fail=False):
    if dump_fail:
        _write_executable(fake_bin / "pg_dump", "#!/bin/sh\necho dump refused >&2\nexit 1\n")
    else:
        _write_executable(
            fake_bin / "pg_dump",
            "#!/bin/sh\nwhile [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = '--file' ]; then shift; printf 'synthetic dump' > \"$1\"; exit 0; fi\n"
            "  shift\n"
            "done\nexit 2\n",
        )
    if restore_fail:
        _write_executable(fake_bin / "pg_restore", "#!/bin/sh\necho list refused >&2\nexit 1\n")
    else:
        _write_executable(
            fake_bin / "pg_restore",
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = '--list' ]; then\n"
            "  [ -s \"$2\" ] || exit 1\n"
            "  echo '; 1 TABLE DATA public finance_account'\n"
            "  echo '; 2 TABLE DATA public finance_transaction'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )


def _fake_age(fake_bin, *, fail=False):
    if fail:
        _write_executable(fake_bin / "age", "#!/bin/sh\necho age refused >&2\nexit 1\n")
        return
    _write_executable(
        fake_bin / "age",
        "#!/bin/sh\n"
        "out=\"\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = '-o' ]; then shift; out=$1; fi\n"
        "  src=$1\n"
        "  shift\n"
        "done\n"
        "printf 'age-encrypted:%s' \"$(cat \"$src\")\" > \"$out\"\n",
    )


def _fake_rclone(fake_bin, remote_root, *, fail=False, delete_fail=False, missing_dir_code=0):
    if fail:
        _write_executable(fake_bin / "rclone", "#!/bin/sh\necho rclone refused >&2\nexit 1\n")
        return
    _write_executable(
        fake_bin / "rclone",
        "#!/bin/sh\n"
        f"root='{_unix_path(remote_root)}'\n"
        "cmd=$1; shift\n"
        "[ \"$1\" = '--config' ] && shift 2\n"
        "[ \"$1\" = '--files-only' ] && shift\n"
        "to_path() { printf '%s/%s' \"$root\" \"$(printf '%s' \"$1\" | sed 's/^[^:]*://')\"; }\n"
        "case \"$cmd\" in\n"
        "  copyto)\n"
        "    src=$1; dest=$(to_path \"$2\")\n"
        "    mkdir -p \"$(dirname \"$dest\")\"\n"
        "    cp \"$src\" \"$dest\"\n"
        "    ;;\n"
        "  lsf)\n"
        "    dir=$(to_path \"$1\")\n"
        f"    [ -d \"$dir\" ] || exit {missing_dir_code}\n"
        "    ls -1 \"$dir\"\n"
        "    ;;\n"
        "  deletefile)\n"
        + ("    exit 1\n" if delete_fail else "")
        + "    rm -f \"$(to_path \"$1\")\"\n"
        "    ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
    )


def _run_backup(fake_bin, backup_root, extra_env=None):
    env = os.environ.copy()
    env.update(
        {
            "BACKUP_ROOT": _unix_path(backup_root),
            "POSTGRES_DB": "financial_planner",
            "POSTGRES_USER": "financial_planner",
            "POSTGRES_PASSWORD": "synthetic-test-password",
            "POSTGRES_HOST": "db",
            "NIGHTLY_RETENTION": "14",
            "WEEKLY_RETENTION": "8",
            "RECEIPTS_DIR": _unix_path(backup_root / "receipts-live"),
        }
    )
    (backup_root / "receipts-live").mkdir(parents=True, exist_ok=True)
    if extra_env:
        converted = {}
        for key, value in extra_env.items():
            if key in {"RCLONE_CONFIG"}:
                converted[key] = _unix_path(value) if value else value
            else:
                converted[key] = value
        env.update(converted)
    fake_unix = _unix_path(fake_bin)
    script_unix = _unix_path(BACKUP_SCRIPT)
    return subprocess.run(
        [
            POSIX_BASH,
            "-c",
            f'export PATH="{fake_unix}:$PATH"; exec sh "{script_unix}"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_restore(fake_bin, dump_path, extra_env=None):
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_DB": "financial_planner",
            "POSTGRES_USER": "financial_planner",
            "POSTGRES_PASSWORD": "synthetic-test-password",
            "POSTGRES_HOST": "db",
        }
    )
    if extra_env:
        converted = {}
        for key, value in extra_env.items():
            if key in {"RECEIPTS_DIR"}:
                converted[key] = _unix_path(value) if value else value
            else:
                converted[key] = value
        env.update(converted)
    fake_unix = _unix_path(fake_bin)
    script_unix = _unix_path(RESTORE_SCRIPT)
    dump_unix = _unix_path(dump_path)
    return subprocess.run(
        [
            POSIX_BASH,
            "-c",
            f'export PATH="{fake_unix}:$PATH"; exec sh "{script_unix}" "{dump_unix}"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _read_status(backup_root):
    text = (backup_root / "health" / "status").read_text(encoding="utf-8")
    data = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key] = value
    return data


def _posix_mode(path):
    result = subprocess.run(
        [POSIX_BASH, "-c", f'stat -c %a "{_unix_path(path)}"'],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_backup_is_verified_and_retains_latest_nightly_and_weekly_files(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    nightly = backup_root / "nightly"
    weekly = backup_root / "weekly"
    fake_bin.mkdir()
    nightly.mkdir(parents=True)
    weekly.mkdir()
    _fake_date(fake_bin)
    _fake_pg(fake_bin)

    for index in range(1, 16):
        (nightly / f"financial_planner_202609{index:02d}T060000Z.dump").write_text("old")
    for index in range(1, 10):
        (weekly / f"financial_planner_202608{index:02d}T060000Z.dump").write_text("old")

    result = _run_backup(fake_bin, backup_root)

    assert result.returncode == 0, result.stderr
    assert len(list(nightly.glob("*.dump"))) == 14
    assert len(list(weekly.glob("*.dump"))) == 8
    dump = nightly / "financial_planner_20260927T060000Z.dump"
    assert dump.read_text() == "synthetic dump"
    assert (weekly / "financial_planner_20260927T060000Z.dump").read_text() == "synthetic dump"
    assert not list(nightly.glob("*.partial"))
    status = _read_status(backup_root)
    assert status["last_success_at"] == "2026-09-27T06:00:00Z"
    assert status["dump_name"] == "financial_planner_20260927T060000Z.dump"
    assert status["size_bytes"] == str(dump.stat().st_size)
    assert status["table_count"] == "2"
    assert status["last_error"] == ""
    assert status.get("offsite_configured", "0") == "0"
    assert (backup_root / "health" / "status").is_file()
    assert not (backup_root / "status").exists()


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_backup_status_records_dump_failure_without_publishing(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    _fake_date(fake_bin)
    _fake_pg(fake_bin, dump_fail=True)

    result = _run_backup(fake_bin, backup_root)

    assert result.returncode == 1
    assert list((backup_root / "nightly").glob("*.dump")) == []
    status = _read_status(backup_root)
    assert status["last_success_at"] == ""
    assert "dump refused" in status["last_error"]


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_offsite_encrypts_uploads_and_prunes_by_name(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    remote = tmp_path / "remote"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    (backup_root / "weekly").mkdir()
    nightly_remote = remote / "offsite" / "nightly"
    weekly_remote = remote / "offsite" / "weekly"
    nightly_remote.mkdir(parents=True)
    weekly_remote.mkdir()
    _fake_date(fake_bin)
    _fake_pg(fake_bin)
    _fake_age(fake_bin)
    _fake_rclone(fake_bin, remote)

    for index in range(1, 16):
        (nightly_remote / f"financial_planner_202609{index:02d}T060000Z.dump.age").write_text("old")
    for index in range(1, 10):
        (weekly_remote / f"financial_planner_202608{index:02d}T060000Z.dump.age").write_text("old")

    result = _run_backup(
        fake_bin,
        backup_root,
        {
            "OFFSITE_RCLONE_REMOTE": "fake:offsite",
            "OFFSITE_AGE_RECIPIENT": "age1syntheticrecipient",
            "RCLONE_CONFIG": (tmp_path / "rclone.conf").as_posix(),
        },
    )

    assert result.returncode == 0, result.stderr
    local = backup_root / "nightly" / "financial_planner_20260927T060000Z.dump"
    uploaded = nightly_remote / "financial_planner_20260927T060000Z.dump.age"
    assert local.read_text() == "synthetic dump"
    assert uploaded.read_text() == "age-encrypted:synthetic dump"
    assert (weekly_remote / "financial_planner_20260927T060000Z.dump.age").is_file()
    assert len(list(nightly_remote.glob("*.dump.age"))) == 14
    assert len(list(weekly_remote.glob("*.dump.age"))) == 8
    status = _read_status(backup_root)
    assert status["offsite_success_at"] == "2026-09-27T06:00:00Z"
    assert status["offsite_error"] == ""
    assert status["last_error"] == ""
    assert status["offsite_configured"] == "1"


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_offsite_upload_failure_keeps_local_dump_and_records_error(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    _fake_date(fake_bin)
    _fake_pg(fake_bin)
    _fake_age(fake_bin)
    _fake_rclone(fake_bin, tmp_path / "remote", fail=True)

    result = _run_backup(
        fake_bin,
        backup_root,
        {
            "OFFSITE_RCLONE_REMOTE": "fake:offsite",
            "OFFSITE_AGE_RECIPIENT": "age1syntheticrecipient",
            "RCLONE_CONFIG": (tmp_path / "rclone.conf").as_posix(),
        },
    )

    assert result.returncode == 1
    dump = backup_root / "nightly" / "financial_planner_20260927T060000Z.dump"
    assert dump.read_text() == "synthetic dump"
    assert list(dump.parent.glob("*.age*")) == []
    status = _read_status(backup_root)
    assert status["last_success_at"] == "2026-09-27T06:00:00Z"
    assert status["dump_name"] == "financial_planner_20260927T060000Z.dump"
    assert "Off-site upload failed" in status["offsite_error"] or "rclone refused" in status["offsite_error"]
    assert status["last_error"] == ""


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_age_failure_keeps_local_dump(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    _fake_date(fake_bin)
    _fake_pg(fake_bin)
    _fake_age(fake_bin, fail=True)
    _fake_rclone(fake_bin, tmp_path / "remote")

    result = _run_backup(
        fake_bin,
        backup_root,
        {
            "OFFSITE_RCLONE_REMOTE": "fake:offsite",
            "OFFSITE_AGE_RECIPIENT": "age1syntheticrecipient",
            "RCLONE_CONFIG": (tmp_path / "rclone.conf").as_posix(),
        },
    )

    assert result.returncode == 1
    assert (backup_root / "nightly" / "financial_planner_20260927T060000Z.dump").read_text() == "synthetic dump"
    status = _read_status(backup_root)
    assert status["last_success_at"] == "2026-09-27T06:00:00Z"
    assert "age encryption failed" in status["offsite_error"]
    assert status["last_error"] == ""


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_backup_status_is_readable_by_the_app_user(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    _fake_date(fake_bin)
    _fake_pg(fake_bin)

    result = _run_backup(fake_bin, backup_root)

    assert result.returncode == 0, result.stderr
    script = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert 'chmod 644 "$status_file"' in script
    assert 'chmod 755 "$health_dir"' in script
    dump = backup_root / "nightly" / "financial_planner_20260927T060000Z.dump"
    assert _posix_mode(backup_root / "health") == "755"
    assert _posix_mode(backup_root / "health" / "status") == "644"
    if os.name != "nt":
        assert _posix_mode(dump) == "600"


def _offsite_run(tmp_path, **rclone_options):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    remote = tmp_path / "remote"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    _fake_date(fake_bin)
    _fake_pg(fake_bin)
    _fake_age(fake_bin)
    _fake_rclone(fake_bin, remote, **rclone_options)
    return fake_bin, backup_root, remote


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_offsite_prune_failure_is_recorded_after_a_successful_upload(tmp_path):
    fake_bin, backup_root, remote = _offsite_run(tmp_path, delete_fail=True)
    nightly_remote = remote / "offsite" / "nightly"
    nightly_remote.mkdir(parents=True)
    for index in range(1, 16):
        (nightly_remote / f"financial_planner_202609{index:02d}T060000Z.dump.age").write_text("old")

    result = _run_backup(
        fake_bin,
        backup_root,
        {
            "OFFSITE_RCLONE_REMOTE": "fake:offsite",
            "OFFSITE_AGE_RECIPIENT": "age1syntheticrecipient",
            "RCLONE_CONFIG": (tmp_path / "rclone.conf").as_posix(),
        },
    )

    assert result.returncode == 1
    assert (nightly_remote / "financial_planner_20260927T060000Z.dump.age").is_file()
    status = _read_status(backup_root)
    assert status["offsite_success_at"] == "2026-09-27T06:00:00Z"
    assert status["offsite_error"] == "Off-site retention pruning failed"
    assert status["last_error"] == ""


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_offsite_missing_remote_directory_is_not_a_prune_failure(tmp_path):
    fake_bin, backup_root, _remote = _offsite_run(tmp_path, missing_dir_code=3)

    result = _run_backup(
        fake_bin,
        backup_root,
        {
            "OFFSITE_RCLONE_REMOTE": "fake:offsite",
            "OFFSITE_AGE_RECIPIENT": "age1syntheticrecipient",
            "RCLONE_CONFIG": (tmp_path / "rclone.conf").as_posix(),
        },
    )

    assert result.returncode == 0, result.stderr
    assert _read_status(backup_root)["offsite_error"] == ""


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_backup_archives_receipts_next_to_the_dump(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    (backup_root / "weekly").mkdir()
    live = backup_root / "receipts-live"
    live.mkdir(parents=True)
    (live / "synthetic-receipt.bin").write_bytes(b"synthetic-receipt-bytes")
    _fake_date(fake_bin)
    _fake_pg(fake_bin)

    result = _run_backup(fake_bin, backup_root)

    assert result.returncode == 0, result.stderr
    archive = backup_root / "nightly" / "financial_planner_20260927T060000Z.receipts.tar.gz"
    weekly = backup_root / "weekly" / "financial_planner_20260927T060000Z.receipts.tar.gz"
    assert archive.is_file()
    assert weekly.is_file()
    listing = subprocess.run(
        [POSIX_BASH, "-c", f'tar -tzf "{_unix_path(archive)}"'],
        check=False,
        capture_output=True,
        text=True,
    )
    assert listing.returncode == 0, listing.stderr
    assert "synthetic-receipt.bin" in listing.stdout


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_restore_replaces_receipts_from_the_sibling_archive(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    fake_bin.mkdir()
    (backup_root / "nightly").mkdir(parents=True)
    (backup_root / "weekly").mkdir()
    live = backup_root / "receipts-live"
    live.mkdir(parents=True)
    (live / "synthetic-receipt.bin").write_bytes(b"synthetic-receipt-bytes")
    _fake_date(fake_bin)
    _fake_pg(fake_bin)

    backed = _run_backup(fake_bin, backup_root)
    assert backed.returncode == 0, backed.stderr
    (live / "synthetic-receipt.bin").unlink()
    (live / "stale.bin").write_bytes(b"should-be-removed")

    restored = _run_restore(
        fake_bin,
        backup_root / "nightly" / "financial_planner_20260927T060000Z.dump",
        {"RECEIPTS_DIR": live},
    )

    assert restored.returncode == 0, restored.stderr
    assert (live / "synthetic-receipt.bin").read_bytes() == b"synthetic-receipt-bytes"
    assert not (live / "stale.bin").exists()


@pytest.mark.skipif(NEEDS_BASH, reason="backup script test requires a POSIX shell")
def test_restore_succeeds_when_the_receipts_archive_is_missing(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    fake_bin.mkdir()
    dump = backup_root / "nightly" / "financial_planner_20260927T060000Z.dump"
    dump.parent.mkdir(parents=True)
    dump.write_text("synthetic dump")
    live = backup_root / "receipts-live"
    live.mkdir(parents=True)
    leftover = live / "pre-release.bin"
    leftover.write_bytes(b"leave-unchanged")
    _fake_pg(fake_bin)

    restored = _run_restore(fake_bin, dump, {"RECEIPTS_DIR": live})

    assert restored.returncode == 0, restored.stderr
    assert "no receipts archive for this backup; receipts directory left unchanged" in (
        restored.stderr + restored.stdout
    )
    assert leftover.read_bytes() == b"leave-unchanged"
