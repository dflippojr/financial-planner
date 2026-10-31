#!/usr/bin/env bash
# Hashed lockfiles (#605). requirements.txt and requirements-test.txt stay the
# hand-edited inputs; these are generated from them:
#
#   requirements.lock       what the production image installs.
#   requirements-test.lock  the same pins plus the test tools; CI installs it.
#
# Both are wheels only and cover every platform on Python 3.13. An existing pin
# stays unless its input moved, so a run after an unrelated PyPI release changes
# nothing. To move a transitive pin, add --upgrade-package NAME to the compile
# arguments for one run. Needs uv, which requirements-test.lock installs.
#
# Usage: bash scripts/lock.sh           regenerate after editing an input
#        bash scripts/lock.sh --check   fail if a lock is out of date (CI)
set -euo pipefail

python_bin="${PYTHON:-python}"
cd "$(dirname "$0")/.."

compile_lock() {
  local input="$1" output="$2"
  shift 2
  "$python_bin" -m uv pip compile "$input" "$@" --output-file "$output" \
    --universal --python-version 3.13 --generate-hashes --only-binary :all: \
    --custom-compile-command "bash scripts/lock.sh" --quiet
}

if [ "${1:-}" != "--check" ]; then
  compile_lock requirements.txt requirements.lock
  compile_lock requirements-test.txt requirements-test.lock --constraint requirements.lock
  exit 0
fi

status=0
allowed_line='^[[:space:]]*#|^[[:space:]]+--hash=sha256:[0-9a-f]{64}( \\)?$|^[A-Za-z0-9._-]+==[^ @]+( ; [^:/@]+)? \\$'

# A package, its version and its markers. Hashes are left out: PyPI may add a
# wheel to a released version, and pip already rejects a hash that does not match.
pins() {
  grep -E '^[A-Za-z0-9]' "$1" | sed -E 's/ *\\$//'
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
cp requirements.lock requirements-test.lock "$scratch/"
compile_lock requirements.txt "$scratch/requirements.lock"
compile_lock requirements-test.txt "$scratch/requirements-test.lock" --constraint "$scratch/requirements.lock"

for lock in requirements.lock requirements-test.lock; do
  if ! diff -u <(pins "$lock") <(pins "$scratch/$lock"); then
    echo "::error file=$lock::$lock is out of date with its inputs. Run: bash scripts/lock.sh" >&2
    status=1
  fi
  # Only comments, name==version pins that go on to a hash, and hashes: so no
  # index options, includes or direct URLs.
  if grep -nvE "$allowed_line" "$lock"; then
    echo "::error file=$lock::$lock may hold only hashed name==version pins: no index options, includes or URLs." >&2
    status=1
  fi
done

exit "$status"
