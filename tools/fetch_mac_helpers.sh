#!/usr/bin/env bash
# Download the macOS helper binaries that get bundled into the .app.
#
#   bash tools/fetch_mac_helpers.sh
#
# Why this exists: the Windows helpers (ffmpeg.exe / ffprobe.exe / yt-dlp.exe /
# aria2c.exe) are PE (MZ) executables and cannot run on macOS, which needs
# Mach-O binaries. So the helpers are re-fetched here as *static* macOS builds
# and placed in the repository root under the names the app resolves at runtime
# ("ffmpeg", "ffprobe", "yt-dlp" - see downloader.helper_exe_names).
#
# Verified download sources (all static, no Homebrew dylib dependencies):
#   ffmpeg / ffprobe : ffmpeg.martin-riedl.de  (macos arm64 + amd64)
#   yt-dlp           : official yt-dlp release "yt-dlp_macos" (self-contained)
set -euo pipefail

# Same self-announcing failure handling as build_macos.sh.
trap 'status=$?; echo "::error file=tools/fetch_mac_helpers.sh,line=$LINENO::failed (exit $status): $BASH_COMMAND" >&2; exit $status' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

ARCH="$(uname -m)"
case "$ARCH" in
  arm64)  FF_ARCH="arm64" ;;
  x86_64) FF_ARCH="amd64" ;;
  *)      echo "ERROR: unsupported architecture '$ARCH'" >&2; exit 1 ;;
esac

FF_BASE="https://ffmpeg.martin-riedl.de/redirect/latest/macos/${FF_ARCH}/release"

echo "==> macOS $ARCH (using static $FF_ARCH ffmpeg builds)"

# --- ffmpeg + ffprobe -------------------------------------------------------
fetch_ff() {
  local who="$1"
  local tmp
  tmp="$(mktemp -d)"
  echo "==> fetching $who"
  curl -fL --retry 3 --retry-delay 2 -o "$tmp/$who.zip" "$FF_BASE/$who.zip"
  unzip -o -q "$tmp/$who.zip" -d "$tmp"
  local bin
  # -print -quit stops at the first match. Piping into `head -n1` would make
  # `set -o pipefail` abort the script with SIGPIPE if the zip ever contained
  # more than one match.
  bin="$(find "$tmp" -type f -name "$who" -print -quit)"
  if [ -z "$bin" ]; then
    echo "ERROR: '$who' not found inside $who.zip" >&2
    rm -rf "$tmp"
    exit 1
  fi
  cp "$bin" "$HERE/$who"
  chmod +x "$HERE/$who"
  rm -rf "$tmp"
}

fetch_ff ffmpeg
fetch_ff ffprobe

# --- yt-dlp -----------------------------------------------------------------
# "yt-dlp_macos" is the official standalone build; no Python needed to run it.
echo "==> fetching yt-dlp"
curl -fL --retry 3 --retry-delay 2 \
  -o "$HERE/yt-dlp" \
  "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
chmod +x "$HERE/yt-dlp"

# --- aria2c (optional) ------------------------------------------------------
# Deliberately NOT bundled: there is no widely available *static* macOS build,
# and the Homebrew one links against /opt/homebrew dylibs that will not exist on
# the recipient's Mac - shipping it would crash there. The app automatically
# falls back to yt-dlp's built-in downloader, so nothing is lost.
echo "==> aria2c: skipped (optional accelerator; no static macOS build available)"

# --- sanity check -----------------------------------------------------------
echo
echo "==> bundled helpers:"
for f in ffmpeg ffprobe yt-dlp; do
  if [ -x "$HERE/$f" ]; then
    # `|| true` stops `set -o pipefail` from failing the script if the tool
    # exits non-zero or closes the pipe early.
    _ver="$("$HERE/$f" --version 2>/dev/null | head -n1 || true)"
    printf '    %-8s %s  %s\n' "$f" "$(ls -lh "$HERE/$f" | awk '{print $5}')" \
      "${_ver:-VERSION CHECK FAILED}"
  else
    echo "    $f MISSING" >&2
  fi
done
echo
echo "Done. Now build the app:  bash tools/build_macos.sh"
