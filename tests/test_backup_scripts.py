import os
from pathlib import Path
import shutil
import subprocess

import pytest


BACKUP_SCRIPT = Path(__file__).parents[1] / "ops" / "backup" / "backup.sh"


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="backup script test requires a native POSIX shell",
)
def test_backup_is_verified_and_retains_latest_nightly_and_weekly_files(tmp_path):
    fake_bin = tmp_path / "bin"
    backup_root = tmp_path / "backups"
    nightly = backup_root / "nightly"
    weekly = backup_root / "weekly"
    fake_bin.mkdir()
    nightly.mkdir(parents=True)
    weekly.mkdir()

    _write_executable(
        fake_bin / "date",
        "#!/bin/sh\nif [ \"${1:-}\" = '-u' ]; then echo 20260927T060000Z; else echo 7; fi\n",
    )
    _write_executable(
        fake_bin / "pg_dump",
        "#!/bin/sh\nwhile [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = '--file' ]; then shift; printf 'synthetic dump' > \"$1\"; exit 0; fi\n"
        "  shift\n"
        "done\nexit 2\n",
    )
    _write_executable(
        fake_bin / "pg_restore",
        "#!/bin/sh\n[ \"${1:-}\" = '--list' ] && [ -s \"$2\" ]\n",
    )

    for index in range(1, 16):
        (nightly / f"financial_planner_202609{index:02d}T060000Z.dump").write_text("old")
    for index in range(1, 10):
        (weekly / f"financial_planner_202608{index:02d}T060000Z.dump").write_text("old")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "BACKUP_ROOT": backup_root.as_posix(),
            "POSTGRES_DB": "financial_planner",
            "POSTGRES_USER": "financial_planner",
            "POSTGRES_PASSWORD": "synthetic-test-password",
            "POSTGRES_HOST": "db",
            "NIGHTLY_RETENTION": "14",
            "WEEKLY_RETENTION": "8",
        }
    )

    result = subprocess.run(
        ["bash", BACKUP_SCRIPT.as_posix()],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert len(list(nightly.glob("*.dump"))) == 14
    assert len(list(weekly.glob("*.dump"))) == 8
    assert (nightly / "financial_planner_20260927T060000Z.dump").read_text() == "synthetic dump"
    assert (weekly / "financial_planner_20260927T060000Z.dump").read_text() == "synthetic dump"
    assert not list(nightly.glob("*.partial"))
