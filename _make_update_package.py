"""Build the self-update ZIP: the full app bundle (EXE + _internal).

Run after building the app (python -m PyInstaller UniversalAudioStudio.spec).
Produces dist\\UniversalAudioStudio_<version>_update.zip, which becomes the
self-update asset attached to the GitHub Release.

Both updater copies are written first - see build_update_package().
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER_NAME = "updater_cli.exe"


def read_version():
    """Return __version__ from version.py, or ``dev``."""
    try:
        with open(os.path.join(HERE, "version.py"), encoding="utf-8") as f:
            return f.read().split('__version__ = "')[1].split('"')[0]
    except Exception:
        return "dev"


def helper_sources(app_dir, dist_dir):
    """The (source, archive name) pairs for both updater copies.

    Two copies exist on purpose: ``_internal/updater_cli.exe`` is the one the app
    finds first (PyInstaller's ``sys._MEIPASS``), and the root copy mirrors the
    Inno Setup layout so the lookup succeeds under either arrangement.
    """
    candidates = [
        (os.path.join(app_dir, "_internal", HELPER_NAME), f"_internal/{HELPER_NAME}"),
        (os.path.join(dist_dir, HELPER_NAME), HELPER_NAME),
    ]
    return [(src, arc) for src, arc in candidates if os.path.isfile(src)]


def build_update_package(app_dir, dist_dir, out_path):
    """Zip *app_dir* into *out_path*, with the updater written first.

    Returns ``(entry_count, size_in_bytes)``.

    The order is load-bearing. The copy being updated extracts this archive with
    the ``updater_cli.exe`` it already has, and if that meets a locked file it can
    stop partway through - see the 2026-09-21 incident described in
    updater_cli.py, where two files were written out of 1045. Putting the new
    updater at the *front* means even a partial update leaves the fixed updater
    behind, so the next release installs properly instead of repeating the
    failure forever.
    """
    helpers = helper_sources(app_dir, dist_dir)
    if not helpers:
        raise SystemExit(
            "No %s found next to the app or in %s - this package would carry no "
            "self-updater, so every future update would break silently. Build it "
            "first: pwsh -File tools/build_windows.ps1" % (HELPER_NAME, dist_dir)
        )

    already_written = {arc for _src, arc in helpers}
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # The self-updater goes in first - see the docstring.
        for source, archive_name in helpers:
            zf.write(source, archive_name)
            count += 1

        for root, _dirs, files in os.walk(app_dir):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, app_dir).replace("\\", "/")
                if rel in already_written:
                    continue  # already added above, at the front
                zf.write(full, rel)
                count += 1

    return count, os.path.getsize(out_path)


def main():
    app_dir = os.path.join(HERE, "dist", "UniversalAudioStudio")
    if not os.path.isdir(app_dir):
        sys.exit("dist\\UniversalAudioStudio missing - build the app first.")

    out_path = os.path.join(
        HERE, "dist", f"UniversalAudioStudio_{read_version()}_update.zip")
    count, size = build_update_package(app_dir, os.path.join(HERE, "dist"), out_path)

    print(f"wrote {out_path}")
    print(f"entries: {count}, size: {size:,} bytes")


if __name__ == "__main__":
    main()
