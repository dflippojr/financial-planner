#!/usr/bin/env bash
# Install the Ubuntu packages Playwright's browsers need, for CI only (#693).
#
#   bash scripts/ci_browser_deps.sh <playwright command...> -- <browser...>
#   bash scripts/ci_browser_deps.sh node_modules/.bin/playwright -- chromium webkit
#   bash scripts/ci_browser_deps.sh .venv/bin/python -m playwright -- chromium
#
# Run `playwright install <browser>` (without --with-deps) afterwards.
#
# Hosted runners fetch these .deb files from azure.archive.ubuntu.com. Most
# runs get 139 MB in about 6 seconds, but on some runners the same mirror
# crawls at about 40 kB/s, and the job used to hit its time limit inside
# `playwright install --with-deps` before any test ran. So:
#
# - Playwright's install-deps --dry-run names the missing packages, so the
#   list stays the one the pinned Playwright version wants.
# - .deb files restored into $PW_APT_CACHE (by actions/cache) are copied into
#   apt's archive directory only when their hash matches the signed package
#   index, because apt itself trusts an archive that is already there.
# - The rest is downloaded with --download-only in attempts limited by
#   $PW_APT_LIMITS (seconds each). Stopping a download is safe; finished and
#   partial files carry over. After the first attempt runs out, the Azure
#   mirror is dropped and apt uses the runner's next listed mirror
#   (archive.ubuntu.com).
# - The install itself runs with --no-download, so nothing reaches a mirror
#   outside those limits and dpkg is never interrupted.
# - The .deb files of this install are copied back into $PW_APT_CACHE.
#
# If every attempt runs out, the script fails with a clear error instead of
# leaving the job to its time limit. PW_APT_ARCHIVES and PW_APT_MIRRORS exist
# for tests/test_ci_browser_deps.py.
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
archives=${PW_APT_ARCHIVES:-/var/cache/apt/archives}
mirrors=${PW_APT_MIRRORS:-/etc/apt/apt-mirrors.txt}

if [[ -f $mirrors ]]; then
  echo "apt mirror list ($mirrors):"
  cat "$mirrors"
fi

update_index() {
  sudo timeout -k 10 120 apt-get update -q
}

# hash_matches FILE ALGO:HEX
hash_matches() {
  local tool
  case ${2%%:*} in
    SHA512) tool=sha512sum ;;
    SHA256) tool=sha256sum ;;
    *) return 1 ;;
  esac
  [[ $("$tool" "$1" | cut -d ' ' -f 1) == "${2#*:}" ]]
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
echo "${#packages[@]} packages to install for ${browsers[*]}."

# One line per .deb apt would fetch: 'URI' FILE SIZE SHA256:HEX
mapfile -t debs < <(
  sudo apt-get install -qq --print-uris --no-install-recommends \
    -o Acquire::ForceHash=SHA256 "${packages[@]}" |
    awk '{ print $2, $4 }'
)

if [[ -n $cache_dir && -d $cache_dir ]]; then
  seeded=0
  rejected=0
  for entry in "${debs[@]}"; do
    file=${entry%% *}
    [[ -f $cache_dir/$file ]] || continue
    if hash_matches "$cache_dir/$file" "${entry#* }"; then
      sudo cp "$cache_dir/$file" "$archives/"
      seeded=$((seeded + 1))
    else
      rejected=$((rejected + 1))
    fi
  done
  echo "Seeded $seeded of ${#debs[@]} .deb files from the cache ($rejected did not match the index)."
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

sudo apt-get install -y -q --no-install-recommends --no-download "${packages[@]}"

if [[ -n $cache_dir ]]; then
  mkdir -p "$cache_dir"
  find "$cache_dir" -maxdepth 1 -name '*.deb' -delete
  kept=0
  for entry in "${debs[@]}"; do
    if [[ -f $archives/${entry%% *} ]]; then
      cp "$archives/${entry%% *}" "$cache_dir/"
      kept=$((kept + 1))
    fi
  done
  echo "Kept $kept of ${#debs[@]} .deb files for the cache."
fi
