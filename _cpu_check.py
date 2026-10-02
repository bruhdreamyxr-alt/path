"""Measure what the AE-compat re-encode actually costs the user's machine.

Runs the real ``_reencode_for_ae`` and samples ffmpeg's CPU and memory while it
runs. This is the check behind the "-threads" cap, the shortened rc-lookahead
and the below-normal priority: before them, ffmpeg sat at ~100% of every core
and peaked at ~3.2GB of RAM, and the desktop stalled.

The obvious fixture - Big Buck Bunny on YouTube - is ten minutes of 4K, and a
full re-encode of it runs for the better part of half an hour. That is far
longer than anyone wants to wait just to sanity-check a thread and memory cap,
so by default this *generates* a one-minute 4K clip locally and re-encodes
that. Same code path - a 4K h264/AE-compat re-encode - in about three minutes,
with no network involved. Override the length with CPU_CHECK_SECONDS, or pass
an existing 4K mp4 to re-encode that instead.

Usage:  python _cpu_check.py [existing-4k.mp4]
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

import downloader as D

GEN_SECONDS = int(os.environ.get("CPU_CHECK_SECONDS", "60"))


def generate_clip(path):
    """One minute of busy 4K so the encoder has something real to chew on."""
    ffmpeg = D._get_ffmpeg_bin()
    if not ffmpeg:
        sys.exit("no ffmpeg found")
    print("generating %ds 4K clip..." % GEN_SECONDS)
    started = time.time()
    r = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi",
         "-i", "testsrc2=size=3840x2160:rate=30:duration=%d" % GEN_SECONDS,
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-crf", "23", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.exit("clip generation failed:\n" + (r.stderr or "")[-400:])
    print("   ready in %.1fs (%.1f MB)"
          % (time.time() - started, os.path.getsize(path) / 1e6))


src = sys.argv[1] if len(sys.argv) > 1 else None
if src and not os.path.exists(src):
    sys.exit("no such file: %s" % src)

generated = False
if not src:
    src = os.path.join(tempfile.gettempdir(), "_ae_cpu_check_4k.mp4")
    generate_clip(src)
    generated = True

print("source   :", src, "(%.1f MB)" % (os.path.getsize(src) / 1e6))
print("cores    :", os.cpu_count())
print("threads  :", D._ae_encode_threads())
print("lookahead:", D.AE_X264_PARAMS)

samples = []
stop = threading.Event()


def sample():
    while not stop.is_set():
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process ffmpeg -ErrorAction SilentlyContinue | "
                 "Select-Object -First 1 -ExpandProperty CPU"],
                capture_output=True, text=True, timeout=10)
            peak = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process ffmpeg -ErrorAction SilentlyContinue | "
                 "Measure-Object WorkingSet64 -Sum).Sum"],
                capture_output=True, text=True, timeout=10)
            cpu = float(out.stdout.strip() or 0)
            ram = int(float(peak.stdout.strip() or 0))
            samples.append((cpu, ram))
        except Exception:
            pass
        time.sleep(1)


t = threading.Thread(target=sample, daemon=True)
t.start()
start = time.time()
result = D._reencode_for_ae(src)
elapsed = time.time() - start
stop.set()
t.join(timeout=5)

print("elapsed  : %.1fs" % elapsed)
print("result   :", os.path.basename(result))
if samples:
    peak_cpu = max(c for c, _ in samples)
    peak_ram = max(r for _, r in samples) / 1e6
    print("peak ffmpeg CPU seconds: %.1f" % peak_cpu)
    print("peak ffmpeg RAM MB    : %.0f" % peak_ram)
print("source still present   :", os.path.exists(src))

if generated:
    for f in (src, src + ".ae_fix.mp4"):
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass
    print("cleaned up generated clip")
