# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS .app bundle.

Build this on macOS only -- PyInstaller cannot cross-compile, so it will not
produce a Mac build from Windows or Linux. Use tools/build_macos.sh.

Differences from the Windows spec (UniversalAudioStudio.spec):
  * helpers are Mach-O builds named without ".exe" (ffmpeg/ffprobe/yt-dlp),
  * updater_cli.exe is not required (in-place self-update is Windows-only),
  * the result is wrapped in a .app bundle via BUNDLE, not a bare folder,
  * UPX is disabled (unavailable / harmful for macOS binaries).
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(SPEC))


def _version() -> str:
    try:
        with open(os.path.join(_HERE, "version.py"), encoding="utf-8") as fh:
            found = re.search(r'__version__\s*=\s*["\']([^"\']+)', fh.read())
            if found:
                return found.group(1)
    except Exception:
        pass
    return "0.0.0"


_VERSION = _version()

# Helpers produced by tools/fetch_mac_helpers.sh. ffmpeg/ffprobe/yt-dlp are
# required (conversion, probing and downloading all depend on them); aria2c and
# ffplay are optional because the app falls back to yt-dlp's built-in
# downloader and to ffmpeg-based previews.
_REQUIRED_HELPERS = ("ffmpeg", "ffprobe", "yt-dlp")
_OPTIONAL_HELPERS = ("aria2c", "ffplay")

_missing = [n for n in _REQUIRED_HELPERS if not os.path.isfile(os.path.join(_HERE, n))]
if _missing:
    raise SystemExit(
        "Missing bundled helper(s): " + ", ".join(_missing) + "\n"
        "Run tools/fetch_mac_helpers.sh first - it downloads static macOS builds.\n"
        "(The Windows .exe helpers cannot be used on macOS.)"
    )

_datas = []
for _name in _REQUIRED_HELPERS + _OPTIONAL_HELPERS:
    if os.path.isfile(os.path.join(_HERE, _name)):
        _datas.append((_name, "."))

a = Analysis(
    ['ui.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=[
        'yt_dlp',
        'yt_dlp.postprocessor.embedthumbnail',
        'yt_dlp.postprocessor.ffmpeg',
        'yt_dlp.networking._curlcffi',
        'curl_cffi',
        'curl_cffi.requests',
        'customtkinter',
        'pedalboard',
        'soundfile',
        'sounddevice',
        'numpy',
        'mutagen',
        'mutagen.mp3',
        'mutagen.id3',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='UniversalAudioStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='UniversalAudioStudio',
)

_icon = os.path.join(_HERE, "icon.icns")
app = BUNDLE(
    coll,
    name='UniversalAudioStudio.app',
    icon=_icon if os.path.isfile(_icon) else None,
    bundle_identifier='com.tunelab.universalaudiostudio',
    info_plist={
        'CFBundleName': 'UniversalAudioStudio',
        'CFBundleDisplayName': 'Universal Audio Studio',
        'CFBundleShortVersionString': _VERSION,
        'CFBundleVersion': _VERSION,
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
        'NSRequiresAquaSystemAppearance': False,
    },
)
