"""Build the self-update ZIP: full app bundle (EXE + _internal) for Drive upload.

Run after building the app (python -m PyInstaller UniversalAudioStudio.spec).
Produces dist\\UniversalAudioStudio_<version>_update.zip
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "dist", "UniversalAudioStudio")

if not os.path.isdir(SRC):
    sys.exit("dist\\UniversalAudioStudio missing - build the app first.")

try:
    with open(os.path.join(HERE, "version.py"), encoding="utf-8") as f:
        version = f.read().split('__version__ = "')[1].split('"')[0]
except Exception:
    version = "dev"

OUT = os.path.join(HERE, "dist", f"UniversalAudioStudio_{version}_update.zip")

count = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, _dirs, files in os.walk(SRC):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, SRC).replace("\\", "/")
            zf.write(full, rel)
            count += 1

    # Keep a copy of the updater helper at the package root too, mirroring
    # the Inno Setup install layout (which places it next to the main EXE).
    helper = os.path.join(HERE, "dist", "updater_cli.exe")
    if os.path.isfile(helper) and "updater_cli.exe" not in zf.namelist():
        zf.write(helper, "updater_cli.exe")
        count += 1

print(f"wrote {OUT}")
print(f"entries: {count}, size: {os.path.getsize(OUT):,} bytes")