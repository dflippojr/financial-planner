import os
from pathlib import Path
import shutil
import subprocess

import pytest


BACKUP_SCRIPT = Path(__file__).parents[1] / "ops" / "backup" / "backup.sh"


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
            "[ \"${1:-}\" = '--list' ] && [ -s \"$2\" ] || exit 1\n"
            "echo '; 1 TABLE DATA public finance_account'\n"
            "echo '; 2 TABLE DATA public finance_transaction'\n",
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


def _fake_rclone(fake_bin, remote_root, *, fail=False):
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
        "    [ -d \"$dir\" ] || exit 0\n"
        "    ls -1 \"$dir\"\n"
        "    ;;\n"
        "  deletefile)\n"
        "    rm -f \"$(to_path \"$1\")\"\n"
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
        }
    )
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


def _read_status(backup_root):
    text = (backup_root / "health" / "status").read_text(encoding="utf-8")
    data = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key] = value
    return data


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
    assert "Off-site upload failed" in status["last_error"] or "rclone refused" in status["last_error"]


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
    assert "age encryption failed" in status["last_error"]
