"""scripts/ci_browser_deps.sh against stub sudo, apt-get and playwright (#693).

The stub apt-get crawls (sleeps past every limit) while the Azure mirror is
listed, like the slow runners did, and "downloads" instantly once it is gone.
"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_browser_deps.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    os.name == "nt" or BASH is None or shutil.which("timeout") is None,
    reason="needs bash and coreutils timeout",
)

PACKAGES = ("fonts-synthetic", "libsynthetic1")

MIRRORS = (
    "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
    "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
    "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
)

STUB_SUDO = '#!/bin/sh\nexec "$@"\n'

STUB_PLAYWRIGHT = """#!/bin/sh
if [ -f "$STUB_STATE/nothing-missing" ]; then
  echo "All system dependencies are installed."
  exit 0
fi
printf 'Missing system dependencies (2):\\n  fonts-synthetic\\n  libsynthetic1\\n'
exit 1
"""

# A .deb's content is "deb:<file>", so its hash is known to the test.
STUB_APT = r"""#!/bin/sh
echo "apt-get $*" >> "$STUB_STATE/apt.log"
[ "$1" = update ] && exit 0
files=""
for arg in "$@"; do
  case "$arg" in -*|install|*=*) ;; *) files="$files ${arg}_1.0_amd64.deb" ;; esac
done
case " $* " in
  *" --print-uris "*)
    for f in $files; do
      hash=$(printf 'deb:%s' "$f" | sha256sum | cut -d ' ' -f 1)
      echo "'http://mirror.invalid/$f' $f 10 SHA256:$hash"
    done ;;
  *" --download-only "*)
    if [ -f "$STUB_STATE/always-slow" ] || grep -q azure "$PW_APT_MIRRORS"; then
      exec sleep 30
    fi
    for f in $files; do
      [ -f "$PW_APT_ARCHIVES/$f" ] && continue
      printf 'deb:%s' "$f" > "$PW_APT_ARCHIVES/$f"
      echo "fetched $f" >> "$STUB_STATE/apt.log"
    done ;;
  *" --no-download "*)
    for f in $files; do
      [ "$(cat "$PW_APT_ARCHIVES/$f" 2>/dev/null)" = "deb:$f" ] || exit 100
    done
    echo "installed" >> "$STUB_STATE/apt.log" ;;
esac
"""


def _executable(path, content):
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "sudo", STUB_SUDO)
    _executable(bin_dir / "apt-get", STUB_APT)
    _executable(tmp_path / "playwright", STUB_PLAYWRIGHT)
    (tmp_path / "archives").mkdir()
    (tmp_path / "apt-mirrors.txt").write_text(MIRRORS, encoding="utf-8")
    (tmp_path / "apt.log").touch()
    return tmp_path


def _run(stubs, limits, cache=None):
    env = {
        **os.environ,
        "PATH": f"{stubs / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "STUB_STATE": str(stubs),
        "PW_APT_LIMITS": limits,
        "PW_APT_ARCHIVES": str(stubs / "archives"),
        "PW_APT_MIRRORS": str(stubs / "apt-mirrors.txt"),
    }
    env.pop("PW_APT_CACHE", None)
    if cache is not None:
        env["PW_APT_CACHE"] = str(cache)
    return subprocess.run(
        [BASH, str(SCRIPT), str(stubs / "playwright"), "--", "chromium", "webkit"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _log(stubs):
    return (stubs / "apt.log").read_text(encoding="utf-8")


def test_a_slow_first_attempt_is_stopped_and_the_next_mirror_finishes(stubs):
    result = _run(stubs, "1 10")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "apt download attempt 1 stopped" in result.stdout
    assert "Download attempt 2 finished" in result.stdout
    mirrors = (stubs / "apt-mirrors.txt").read_text(encoding="utf-8")
    assert "azure" not in mirrors
    assert mirrors.startswith("https://archive.ubuntu.com/ubuntu/")
    log = _log(stubs)
    assert "--no-download" in log
    assert log.rstrip().endswith("installed")


def test_it_fails_clearly_when_every_attempt_runs_out(stubs):
    (stubs / "always-slow").touch()

    result = _run(stubs, "1 1")

    assert result.returncode == 1
    assert "::error::The apt mirrors were too slow" in result.stdout
    assert "installed" not in _log(stubs)


def test_only_cached_files_matching_the_index_are_used(stubs):
    (stubs / "apt-mirrors.txt").write_text(MIRRORS.split("\n", 1)[1], encoding="utf-8")
    cache = stubs / "cache"
    cache.mkdir()
    good, tampered = (f"{name}_1.0_amd64.deb" for name in PACKAGES)
    (cache / good).write_text(f"deb:{good}", encoding="utf-8")
    # Same size as the real file, so apt alone would accept it.
    (cache / tampered).write_text("x" * len(f"deb:{tampered}"), encoding="utf-8")

    result = _run(stubs, "10", cache=cache)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Seeded 1 of 2 .deb files from the cache (1 did not match the index)." in result.stdout
    log = _log(stubs)
    assert f"fetched {tampered}" in log
    assert f"fetched {good}" not in log
    assert (cache / tampered).read_text(encoding="utf-8") == f"deb:{tampered}"
    assert "Kept 2 of 2 .deb files for the cache." in result.stdout


def test_nothing_is_installed_when_no_package_is_missing(stubs):
    (stubs / "nothing-missing").touch()

    result = _run(stubs, "1")

    assert result.returncode == 0
    assert "already installed" in result.stdout
    assert "install" not in _log(stubs)
