#!/usr/bin/env bash
# Prefetch the Ubuntu packages Playwright's browsers need, for CI only (#693).
#
#   bash scripts/ci_browser_deps.sh <playwright command...> -- <browser...>
#   bash scripts/ci_browser_deps.sh node_modules/.bin/playwright -- chromium webkit
#   bash scripts/ci_browser_deps.sh .venv/bin/python -m playwright -- chromium
#
# Hosted runners fetch these .deb files from azure.archive.ubuntu.com. Most
# runs get 139 MB in about 6 seconds, but on some runners the same mirror
# crawls at about 40 kB/s, and the job used to hit its time limit inside
# `playwright install --with-deps` before any test ran. This script only
# downloads, so it is always safe to stop:
#
# - It asks Playwright which packages are missing (install-deps --dry-run),
#   so the list stays the one the pinned Playwright version wants.
# - It seeds /var/cache/apt/archives from $PW_APT_CACHE (a directory that
#   actions/cache restores), when set. apt checks each cached file against
#   the signed package index before using it, so a stale or altered file is
#   fetched again rather than installed.
# - Each download attempt has its own time limit ($PW_APT_LIMITS, seconds).
#   After the first one runs out, apt drops the Azure mirror and uses the
#   runner's next listed mirror (archive.ubuntu.com). Finished and partial
#   downloads carry over between attempts.
# - On success it copies the current .deb files back into $PW_APT_CACHE.
#
# The caller's `playwright install --with-deps` then finds every package on
# disk and only unpacks it. If every attempt runs out, the script fails with
# a clear error instead of leaving the job to its time limit.
set -euo pipefail

playwright=()
while (($#)) && [[ $1 != -- ]]; do
  playwright+=("$1")
  shift
done
if ((${#playwright[@]} == 0)) || (($# < 2)); then
  echo "usage: $0 <playwright command...> -- <browser...>" >&2
  exit 2
fi
shift
browsers=("$@")

read -r -a limits <<< "${PW_APT_LIMITS:-90 90 90}"
cache_dir=${PW_APT_CACHE:-}
archives=/var/cache/apt/archives
mirrors=/etc/apt/apt-mirrors.txt

if [[ -f $mirrors ]]; then
  echo "apt mirror list ($mirrors):"
  cat "$mirrors"
fi

update_index() {
  sudo timeout -k 10 120 apt-get update -q
}

update_index

# install-deps --dry-run simulates the install against the fresh index and
# prints the packages it would add, one per line under a header. It exits 1
# when any are missing, so its status cannot stop the script here.
dry_run=$("${playwright[@]}" install-deps --dry-run "${browsers[@]}" 2>&1) || true
mapfile -t packages < <(
  awk '/^Missing system dependencies/ { listed = 1; next }
       listed && /^  [^ ]+$/ { print $1 }' <<< "$dry_run"
)
if ((${#packages[@]} == 0)); then
  if grep -q "All system dependencies are installed" <<< "$dry_run"; then
    echo "All system packages for ${browsers[*]} are already installed."
    exit 0
  fi
  echo "$dry_run"
  echo "::error::Could not read the package list from playwright install-deps --dry-run."
  exit 1
fi
echo "${#packages[@]} packages to fetch for ${browsers[*]}."

if [[ -n $cache_dir ]] && compgen -G "$cache_dir/*.deb" > /dev/null; then
  sudo cp "$cache_dir"/*.deb "$archives"/
  echo "Seeded $(find "$cache_dir" -maxdepth 1 -name '*.deb' | wc -l) cached .deb files."
fi

fetched=false
for i in "${!limits[@]}"; do
  attempt=$((i + 1))
  started=$SECONDS
  if sudo timeout -k 10 "${limits[i]}" \
    apt-get install -y -q --download-only --no-install-recommends "${packages[@]}"; then
    echo "Download attempt $attempt finished in $((SECONDS - started))s."
    fetched=true
    break
  else
    status=$?
  fi
  echo "::warning::apt download attempt $attempt stopped after $((SECONDS - started))s (exit $status)."
  if ((attempt == 1)) && [[ -f $mirrors ]] && grep -q 'azure\.archive\.ubuntu\.com' "$mirrors"; then
    echo "Dropping the Azure mirror for the next attempts."
    sudo sed -i '/azure\.archive\.ubuntu\.com/d' "$mirrors"
    cat "$mirrors"
    update_index
  fi
done

if [[ $fetched != true ]]; then
  echo "::error::The apt mirrors were too slow to fetch the browser packages within ${limits[*]} seconds."
  exit 1
fi

if [[ -n $cache_dir ]]; then
  # autoclean drops .deb files the current index no longer offers (older
  # versions from a restored cache), so the cache does not grow.
  sudo apt-get autoclean -q
  mkdir -p "$cache_dir"
  find "$cache_dir" -maxdepth 1 -name '*.deb' -delete
  cp "$archives"/*.deb "$cache_dir"/
  echo "Kept $(find "$cache_dir" -maxdepth 1 -name '*.deb' | wc -l) .deb files for the cache."
fi
