"""Real end-to-end download check: actual network, actual media files.

The frozen probe proves the helpers exist and the DSP math is right using
synthetic audio, but neither touches a real network download. This drives the
shipping ``download_track`` / ``download_video_mp4`` entry points - the same
functions the UI calls - so the whole chain runs: URL resolution, yt-dlp
options, ffmpeg transcode, output templating, and the success callbacks.

Downloads are redirected to a scratch folder so a real Downloads directory is
never written to. Requires network access.

Usage:  python _e2e_download_check.py
"""
import os
import subprocess
import sys
import tempfile

import downloader as D

SCRATCH = tempfile.mkdtemp(prefix="e2e_dl_")
D.set_download_folder(SCRATCH)
print("scratch dir:", SCRATCH)


def on_status(stage, detail=""):
    print("   [status] %s %s" % (stage, str(detail)[:70]))


def on_success(msg):
    print("   [SUCCESS] %s" % str(msg)[:100])


last_error = ""


def on_error(msg):
    global last_error
    last_error = str(msg)
    print("   [ERROR] %s" % last_error[:200])


def media_files():
    out = []
    for root, _dirs, files in os.walk(SCRATCH):
        for f in files:
            if f.lower().endswith((".mp3", ".mp4", ".m4a", ".webm", ".opus")):
                out.append(os.path.join(root, f))
    return out


def probe(path):
    """Confirm real media, not a zero-byte or HTML error placeholder."""
    ffprobe = D.find_helper("ffprobe", include_path=True)
    if not ffprobe:
        return "no ffprobe"
    r = subprocess.run(
        [ffprobe, "-v", "error",
         "-show_entries", "format=duration,format_name",
         "-show_entries", "stream=codec_type,codec_name",
         "-of", "default=noprint_wrappers=1", path],
        capture_output=True, text=True)
    return r.stdout.strip().replace("\n", " ")[:160] or r.stderr.strip()[:160]


def run(label, fn, *args):
    before = set(media_files())
    print()
    print("=== %s ===" % label)
    fn(*args, on_status, on_success, on_error)
    # Only files this run produced count, or a leftover would mask a failure.
    new = [f for f in media_files() if f not in before]
    if not new:
        print("   RESULT: no media file produced")
        return False
    newest = max(new, key=os.path.getmtime)
    size = os.path.getsize(newest)
    print("   file   : %s (%d bytes)" % (os.path.basename(newest), size))
    print("   ffprobe: %s" % probe(newest))
    if size < 10000:
        print("   RESULT: SUSPICIOUS - too small to be real media")
        return False
    print("   RESULT: OK")
    return True


# A short, freely-licensed fixture on a CDN that allows automated requests.
CLIP = ("https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/"
        "Big_Buck_Bunny_360_10s_1MB.mp4")

ok_mp4 = run("MP4 download (direct media)", D.download_video_mp4, CLIP)
ok_mp3 = run("MP3 extraction (direct media)", D.download_track, CLIP)
# The YouTube MP4 case is deliberately NOT run by default: the source is 4K and
# a full re-encode runs for many minutes even with the thread cap. _cpu_check.py
# covers that path on a generated one-minute clip, so verifying it never needs a
# ten-minute 4K download.
# The paths above hit a direct-media URL, which yt-dlp serves straight through
# with no extractor involved. Real users paste YouTube/SoundCloud links, so the
# MP3 case runs against an actual extractor to cover that path too. The fixture
# is short on purpose - the extractor path is identical regardless of length, so
# a ten-minute source only makes the check slow. Override with E2E_YOUTUBE_URL.
YT = os.environ.get("E2E_YOUTUBE_URL",
                    "https://www.youtube.com/watch?v=jNQXAC9IVRw")
ok_yt_mp3 = run("MP3 extraction (YouTube)", D.download_track, YT)
bot_blocked = (not ok_yt_mp3
               and ("not a bot" in last_error or "Sign in to confirm" in last_error))
if bot_blocked:
    print("   NOTE: YouTube bot-checked this runner; the extractor path could "
          "not be exercised here (that is the runner being throttled, not a "
          "code failure).")

print()
print("=" * 60)
for label, good in (("mp4 direct", ok_mp4), ("mp3 direct", ok_mp3)):
    print("%-14s %s" % (label + ":", "OK" if good else "FAILED"))
yt_state = "OK" if ok_yt_mp3 else ("SKIPPED (bot check)" if bot_blocked
                                   else "FAILED")
print("%-14s %s" % ("mp3 youtube:", yt_state))
print("scratch kept at", SCRATCH)
# A YouTube bot check is the runner being throttled, not a broken product, so it
# downgrades the extractor case to a skip rather than a failure.
failed = (not ok_mp4) or (not ok_mp3) or (not ok_yt_mp3 and not bot_blocked)
sys.exit(1 if failed else 0)
