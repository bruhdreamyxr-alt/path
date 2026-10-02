"""Frozen-runtime probe: what the packaged app can actually import and find.

Run inside the PyInstaller bundle so this exercises the real frozen import
mechanism (no source tree on sys.path), which is the only place the lazy
imports and the _internal helper lookup are actually proven.
"""
import json
import os
import subprocess
import sys
from typing import Optional

print("frozen          :", bool(getattr(sys, "frozen", False)))
print("sys.executable  :", sys.executable)
print("_MEIPASS        :", getattr(sys, "_MEIPASS", "<unset>"))
print()

# The helpers that were the whole point of the _internal fix.
import downloader as D

print("search dirs:")
for d in D._bundle_search_dirs():
    print("   ", d)
print()
print("aria2c  ->", D.get_fast_downloader_path())
print("ffmpeg  ->", D.find_helper("ffmpeg", include_path=True))
print("ffprobe ->", D.find_helper("ffprobe", include_path=True))
print("yt-dlp  ->", D.find_helper("yt-dlp", include_path=True))
print()

# The lazy imports: these MUST resolve in the frozen bundle or the studio,
# the tag editor and the drawn icons break at runtime.
print("lazy imports:")
for label, fn in (
    ("yt_dlp", D._mod_yt_dlp),
    ("mutagen", D._mod_mutagen),
    ("PIL.Image", D._mod_image),
    ("soundfile", D._mod_soundfile),
    ("pedalboard trio", D._dsp_backends),
):
    try:
        value = fn()
        ok = value is not None and not (
            isinstance(value, tuple) and all(v is None for v in value)
        )
        print("   %-16s %s" % (label, "OK" if ok else "MISSING -> %r" % (value,)))
    except Exception as exc:
        print("   %-16s RAISED %r" % (label, exc))

print()
# YoutubeDL must come back genuinely usable, not merely not raise.
try:
    ydl = D._build_youtube_dl({"quiet": True, "no_warnings": True})
    print("youtube-dl opts  -> %d options set, extractor class %s"
          % (len(getattr(ydl, "params", {})), type(ydl).__name__))
except Exception as exc:
    print("youtube-dl       -> RAISED %r" % exc)

print()
# Prefs round-trip through the frozen app's own data dir.
print("data dir :", D.get_base_dir())
print("OK-PROBE-COMPLETE")

# --- real work: does the frozen build actually process audio and video? ------
# Importing is not processing. Generate a tone, run it through the same DSP path
# the Studio page uses, and confirm ffmpeg/ffprobe really run as subprocesses.
import tempfile
import time
import numpy as np
import soundfile as sf

tmp = tempfile.mkdtemp(prefix="tunelab_probe_")
src = os.path.join(tmp, "tone.wav")
sr = 44100
dur = 6.0
t = np.linspace(0, dur, int(sr * dur), endpoint=False)
audio = (0.4 * np.sin(2 * np.pi * 440.0 * t) * np.exp(-t / 2.0)).astype(np.float32)
sf.write(src, audio, sr)
print()
print("wrote %s (%.1fs tone)" % (os.path.basename(src), dur))

for label, speed, wet in (
    ("reverb", 1.0, 0.45),
    ("slowdown", 0.75, 0.0),
    ("both", 0.8, 0.3),
):
    started = time.time()
    try:
        # Returns (numpy audio, new_sample_rate); it does not write a file.
        data, new_sr = D.process_studio_dsp(src, speed, wet)
        if data is None:
            print("%-9s -> returned None" % label)
            continue
        frames = np.asarray(data).shape[-1]   # (channels, samples), not len()
        grew = frames / new_sr
        # At speed 1.0 with reverb only, duration is preserved.
        expected = dur / speed
        # Reverb must actually change the samples, not pass audio through.
        peak = float(abs(data).max())
        print("%-9s -> OK  %.2fs (expect ~%.2fs) %.0fHz peak=%.3f in %.1fs"
              % (label, grew, expected, new_sr, peak, time.time() - started))
    except Exception as exc:
        print("%-9s -> RAISED %r" % (label, exc))

# ffprobe on a real file, to prove the video/streaming path has a working binary.
probe = D.find_helper("ffprobe", include_path=True)
if probe:
    r = subprocess.run([probe, "-v", "error", "-show_entries",
                        "format=duration", "-of", "json", src],
                       capture_output=True, text=True)
    print("ffprobe   -> rc=%d %s" % (r.returncode, r.stdout.strip()[:70]))

# ffmpeg transcode, the path a real download+convert takes.
out_mp3 = os.path.join(tmp, "tone.mp3")
ffmpeg = D.find_helper("ffmpeg", include_path=True)
if ffmpeg:
    r = subprocess.run([ffmpeg, "-y", "-loglevel",
                        "error", "-i", src, out_mp3], capture_output=True, text=True)
    print("ffmpeg mp3-> rc=%d exists=%s %s" % (r.returncode, os.path.isfile(out_mp3),
                                               r.stderr.strip()[:60]))
else:
    print("ffmpeg mp3-> SKIPPED (ffmpeg not found)")

print()
import ui as UI

# --- do the Performance settings survive a restart? ---------------------------
# The reported bug was that moving a slider looked fine but the value was gone
# on the next launch. _load_prefs keeps only keys already in _default_prefs, so
# a written-but-undeclared key is silently dropped - this exercises that exact
# filter rather than a plain json.load, which would not catch it.

# Subclass the real window class rather than stubbing the methods onto a bare
# object: _get_pref_path is an instance method, and re-implementing it here
# would test my copy instead of the shipping one.
class _PrefsProbe(UI.UniversalAudioStudio):
    _forced_pref_path: Optional[str] = None

    def __init__(self) -> None:  # skips CTk.__init__ on purpose
        self._forced_pref_path = None

    def _get_pref_path(self) -> str:
        return self._forced_pref_path or super()._get_pref_path()


# Where the real app would look: %APPDATA% when frozen, beside ui.py otherwise.
sentinel = {"concurrent_fragment_downloads": 19, "aria2_connections": 11,
            "use_aria2": True}
frozen_path = D._get_user_data_dir()
probe = _PrefsProbe()

# Use a scratch file so the probe never overwrites the real user's settings.
scratch = os.path.join(frozen_path, "_probe_scratch_prefs.json")
if os.path.exists(scratch):
    os.remove(scratch)
probe._forced_pref_path = scratch

try:
    print("real prefs path :", UI.UniversalAudioStudio._get_pref_path(probe))
    print("scratch path    :", scratch)

    with open(scratch, "w", encoding="utf-8") as fh:
        json.dump(sentinel, fh)

    loaded = probe._load_prefs()
    print("round-trip:")
    for key, want in sentinel.items():
        got = loaded.get(key)
        print("   %-32s %-6r %s" % (key, got, "OK" if got == want else "LOST"))
    missing = [k for k in sentinel if k not in loaded]
    print("performance settings dropped:", missing or "none")

    # The values must also reach the downloader config, not just the dict.
    cfg = D.perf_cfg_from_prefs(loaded)
    print("perf config     :", {k: cfg.get(k) for k in sentinel})

    # Out-of-range hand-edits must be clamped, not handed to aria2/yt-dlp.
    over = dict(sentinel, aria2_connections=99999, concurrent_fragment_downloads=0)
    with open(scratch, "w", encoding="utf-8") as fh:
        json.dump(over, fh)
    clamped = D.perf_cfg_from_prefs(probe._load_prefs())
    print("clamp 99999 -> %s (max %d)"
          % (clamped["aria2_connections"], D.ARIA2_MAX_CONNECTIONS))
    print("clamp 0     -> %s (min 1)" % clamped["concurrent_fragment_downloads"])

    # A corrupt file must fall back to defaults instead of raising.
    with open(scratch, "w", encoding="utf-8") as fh:
        fh.write("{not json at all")
    fallback = probe._load_prefs()
    print("corrupt file -> defaults intact:", fallback == probe._default_prefs())
finally:
    if os.path.exists(scratch):
        os.remove(scratch)
    print("(probe removed its scratch prefs file)")

print("DONE-PROBE-COMPLETE")
