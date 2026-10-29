"""scripts/build-synthetic-dump.sh without Docker or PostgreSQL (#538).

A docker stand-in on PATH logs every call and answers the few the script
makes; PYTHON names a stand-in for the seeding step.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "build-synthetic-dump.sh"
CONTAINER_ID = "c" * 64


def _stand_ins(tmp_path, run_exit=0):
    log = tmp_path / "docker-calls.log"
    stub = tmp_path / "fake_docker.py"
    stub.write_text(
        "import sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(' '.join(args) + '\\n')\n"
        "if args[:1] == ['run']:\n"
        f"    print({CONTAINER_ID!r}) if {run_exit} == 0 else sys.exit({run_exit})\n"
        "elif args[:1] == ['port']:\n"
        "    print('127.0.0.1:55432')\n"
        "elif args[:1] == ['exec'] and 'pg_dump' in args:\n"
        "    sys.stdout.write('synthetic dump')\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    seed = tmp_path / "seed"
    seed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    seed.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], PYTHON=str(seed))
    return env, log


def _build(tmp_path, output, run_exit=0):
    env, log = _stand_ins(tmp_path, run_exit)
    result = subprocess.run(["bash", str(SCRIPT), str(output), "0061_savings_goal_wishlist"],
                            capture_output=True, text=True, env=env, timeout=60, check=False, cwd=ROOT)
    return result, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_output_directory_is_created_when_missing(tmp_path):
    # The documented command writes under .tmp/, which a clean checkout lacks.
    output = tmp_path / "missing" / "deeper" / "synthetic.dump"

    result, _ = _build(tmp_path, output)

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "synthetic dump"


def test_cleanup_removes_only_the_container_this_run_created(tmp_path):
    result, calls = _build(tmp_path, tmp_path / "synthetic.dump")

    assert result.returncode == 0, result.stderr
    assert [call for call in calls if call.startswith("rm ")] == [f"rm -f {CONTAINER_ID}"]


def test_a_failed_start_removes_nothing(tmp_path):
    # A name that already belongs to another run makes `docker run` fail; that
    # container is not this run's to remove.
    result, calls = _build(tmp_path, tmp_path / "synthetic.dump", run_exit=125)

    assert result.returncode != 0
    assert [call for call in calls if call.startswith("rm ")] == []
