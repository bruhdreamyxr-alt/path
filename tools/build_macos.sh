#!/usr/bin/env bash
# Build UniversalAudioStudio.app and a distributable .dmg on macOS.
#
#   bash tools/build_macos.sh
#
# Outputs:
#   dist/UniversalAudioStudio.app
#   dist/UniversalAudioStudio-<version>.dmg   <-- send THIS to your friend
#
# PyInstaller cannot cross-compile, so this must run on a Mac (or a macOS CI
# runner - see .github/workflows/build-macos.yml, which produces the .dmg for
# you without needing a Mac).
set -euo pipefail

# Turn any failure into a visible GitHub annotation naming the exact line and
# command, so a failed run explains itself on the run page instead of only
# inside the raw step log.
trap 'status=$?; echo "::error file=tools/build_macos.sh,line=$LINENO::failed (exit $status): $BASH_COMMAND" >&2; exit $status' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
VENV=".venv-mac"

echo "==> 0/6 Host: $(uname -s) $(uname -m), $("$PYTHON" -V 2>&1)"

echo "==> 1/6 Python environment"
if [ ! -d "$VENV" ]; then
  "$PYTHON" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

echo
echo "==> 2/6 Fetching bundled helper binaries"
bash tools/fetch_mac_helpers.sh

echo
echo "==> 3/6 Running test suite"
python -m unittest discover -s tests || {
  echo "!! Tests failed. Fix them before shipping a build." >&2
  exit 1
}

echo
echo "==> 4/6 Building .app"
rm -rf build dist/UniversalAudioStudio dist/UniversalAudioStudio.app "dist/UniversalAudioStudio.app"
python -m PyInstaller UniversalAudioStudio_mac.spec -y

echo
echo "==> 5/6 Ad-hoc signing"
# An unsigned app is refused outright by Gatekeeper on other Macs. An ad-hoc
# signature ("-") is fine for running locally, but distributing to someone
# else still requires them to approve it once (see the note printed below).
# For friction-free distribution you need an Apple Developer ID certificate.
codesign --force --deep --sign - "dist/UniversalAudioStudio.app" \
  || echo "!! codesign failed - the app will still run locally"

echo
echo "==> 6/6 Creating .dmg"
VERSION="$(python - <<'PY'
import re
src = open('version.py', encoding='utf-8').read()
m = re.search(r'__version__\s*=\s*["\']([^"\']+)', src)
print(m.group(1) if m else '0.0.0')
PY
)"
DMG="dist/UniversalAudioStudio-${VERSION}.dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "dist/UniversalAudioStudio.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # drag-and-drop install target
hdiutil create -volname "Universal Audio Studio" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo
echo "============================================================"
echo "Built:"
echo "  $HERE/dist/UniversalAudioStudio.app"
echo "  $HERE/$DMG"
echo "============================================================"
echo
echo "Send your friend the .dmg. On their Mac they:"
echo "  1. double-click the .dmg"
echo "  2. drag 'UniversalAudioStudio' onto the Applications shortcut"
echo "  3. first launch only: right-click the app -> Open -> Open"
echo "     (macOS blocks unsigned apps double-clicked the first time;"
echo "      or run:  xattr -dr com.apple.quarantine /Applications/UniversalAudioStudio.app )"
