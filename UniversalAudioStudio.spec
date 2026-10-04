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

# The probe entry point: a copy of the app spec that runs a diagnostic script
# instead of the GUI, so the lazy imports and the _internal helper lookup can be
# proven inside a real frozen bundle. Only built when asked for explicitly.
_PROBE = os.environ.get('TUNELAB_FROZEN_PROBE')

a = Analysis(
    [_PROBE] if _PROBE else ['ui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ffmpeg.exe', '.'),
        ('ffprobe.exe', '.'),
        ('yt-dlp.exe', '.'),
        (_UPDATE_HELPER, '.'),
        # The app's own icon, next to the executable so ui.py can find it in a
        # frozen build (sys._MEIPASS). The .exe gets its icon from icon= below.
        ('assets/tune_lab.png', '.'),
        ('assets/tune_lab.ico', '.'),
    ] + _ARIA2_DATAS + _FFPLAY_DATAS,
    hiddenimports=[
        # The theme/icon primitives and the queue list widget were split out of
        # ui.py. ui.py imports both with literal `from ... import ...`, so
        # static analysis follows them, but they are listed explicitly so a
        # frozen build that somehow missed them fails loudly here rather than at
        # the user's first click.
        'ui_theme',
        'ui_widgets',
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
        # downloader.py and ui.py resolve these through
        # importlib.import_module(<variable>), which PyInstaller's static
        # analysis cannot follow - it only follows literal `import x` and a
        # literal import_module(). Without them here a frozen build imports
        # fine and then fails at runtime, the first time the user tags a file
        # or draws an icon. Keep this list in step with the names
        # _optional_import() / _lazy_import_pil() are called with.
        'PIL.Image',
        'PIL.ImageDraw',
        'pedalboard.io',
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
    name='UniversalAudioStudio' if not _PROBE else 'FrozenProbe',
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
    # The app icon, so the .exe in Explorer, on the taskbar and in Alt-Tab is
    # TuneLab and not PyInstaller's default. (The macOS bundle keeps its own
    # .icns, so this spec stays Windows-only.)
    icon='assets/tune_lab.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='UniversalAudioStudio' if not _PROBE else 'FrozenProbe',
)
