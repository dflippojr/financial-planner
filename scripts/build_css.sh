#!/usr/bin/env bash
# Download the pinned Tailwind standalone binary, verify its checksum, verify
# vendored daisyUI plugins, and compile static/src/app.css. No Node required.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$root/scripts/css_pins.env"

cache_dir="$root/.cache/tailwindcss/$TAILWIND_VERSION"
mkdir -p "$cache_dir" "$root/static/dist"

case "$(uname -s)" in
  Linux*)
    case "$(uname -m)" in
      aarch64|arm64)
        asset="tailwindcss-linux-arm64"
        expected="$TAILWIND_SHA256_LINUX_ARM64"
        ;;
      *)
        asset="tailwindcss-linux-x64"
        expected="$TAILWIND_SHA256_LINUX_X64"
        ;;
    esac
    binary="$cache_dir/$asset"
    ;;
  Darwin*)
    if [ "$(uname -m)" = "arm64" ]; then
      asset="tailwindcss-macos-arm64"
      expected="$TAILWIND_SHA256_MACOS_ARM64"
    else
      asset="tailwindcss-macos-x64"
      expected="$TAILWIND_SHA256_MACOS_X64"
    fi
    binary="$cache_dir/$asset"
    ;;
  MINGW*|MSYS*|CYGWIN*)
    asset="tailwindcss-windows-x64.exe"
    expected="$TAILWIND_SHA256_WINDOWS_X64"
    binary="$cache_dir/$asset"
    ;;
  *)
    echo "Unsupported platform: $(uname -s) $(uname -m)" >&2
    exit 1
    ;;
esac

verify_file() {
  local sum="$1"
  local path="$2"
  echo "$sum  $path" | sha256sum -c -
}

verify_file "$DAISYUI_SHA256" "$root/static/src/vendor/daisyui.mjs"
verify_file "$DAISYUI_THEME_SHA256" "$root/static/src/vendor/daisyui-theme.mjs"

if [ ! -x "$binary" ] && [ ! -f "$binary" ]; then
  url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"
  echo "Downloading $url"
  if ! curl -fsSL --retry 3 -o "$binary" "$url"; then
    rm -f "$binary"
    echo "Failed to download the pinned Tailwind standalone binary from GitHub." >&2
    echo "Do not fall back to a CDN. Retry when GitHub releases are reachable." >&2
    exit 1
  fi
fi

verify_file "$expected" "$binary"
chmod +x "$binary"

watch=0
minify=1
for arg in "$@"; do
  case "$arg" in
    --watch) watch=1 ;;
    --no-minify) minify=0 ;;
  esac
done

cmd=("$binary" -i "$root/static/src/app.css" -o "$root/static/dist/app.css")
if [ "$minify" -eq 1 ] && [ "$watch" -eq 0 ]; then
  cmd+=(--minify)
fi
if [ "$watch" -eq 1 ]; then
  cmd+=(--watch)
fi

exec "${cmd[@]}"
