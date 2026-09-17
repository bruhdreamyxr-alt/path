# -*- mode: python ; coding: utf-8 -*-

import os

# The updater helper must exist BEFORE this build runs:
#   build_updaters.bat   (produces dist\updater_cli.exe)
_HERE = os.path.dirname(os.path.abspath(SPEC))
_UPDATE_HELPER = os.path.join(_HERE, 'dist', 'updater_cli.exe')
if not os.path.isfile(_UPDATE_HELPER):
    raise SystemExit(
        'dist\\updater_cli.exe not found - run build_updaters.bat first '
        'so the app can self-update.'
    )

# Optional: bundle helper executables so end users don't install anything.
# Place the files next to this spec before building. Destination '.' = the
# bundle root next to the main EXE (also mirrored into _internal for the
# sys._MEIPASS lookup).
def _bundle_if_exists(name):
    src = os.path.join(_HERE, name)
    if os.path.isfile(src):
        # (source, dest_dir) — dest '.' = bundle root next to the .exe
        return [(name, '.')]
    return []

_ARIA2_DATAS = _bundle_if_exists('aria2c.exe')
_FFPLAY_DATAS = _bundle_if_exists('ffplay.exe')

a = Analysis(
    ['ui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ffmpeg.exe', '.'),
        ('ffprobe.exe', '.'),
        ('yt-dlp.exe', '.'),
        (_UPDATE_HELPER, '.'),
    ] + _ARIA2_DATAS + _FFPLAY_DATAS,
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
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='UniversalAudioStudio',
)
