"""Live smoke test: one real audio download through the app's public API.

Run manually (needs network):
    python tools\\smoke_download.py [url]

Verifies the end-to-end path still works after changes: option building,
aria2 argument passing, tag writing and the on-disk result.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloader  # noqa: E402


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=BaW_jenozKc"
    out_dir = tempfile.mkdtemp(prefix="smoke_dl_")
    downloader.set_download_folder(out_dir)

    logs, result = [], {"error": None, "success": None}

    def status(msg, color):
        logs.append(("status", msg))

    def detail(msg, color):
        logs.append(("detail", msg))

    def progress(pct):
        logs.append(("progress", pct))

    def success(msg):
        result["success"] = msg

    def error(msg):
        result["error"] = msg

    started = time.time()
    downloader.download_track(url, status, success, error, progress, detail)
    elapsed = time.time() - started

    files = []
    for root, _dirs, names in os.walk(out_dir):
        for name in names:
            path = os.path.join(root, name)
            files.append((name, os.path.getsize(path)))

    print("elapsed_s = %.1f" % elapsed)
    print("out_dir   = %s" % out_dir)
    print("error     = %s" % result["error"])
    print("success   = %s" % result["success"])
    print("files     = %s" % files)
    media = [f for f in files if f[0].lower().endswith((".mp3", ".flac", ".m4a", ".opus", ".wav"))]
    print("media_count = %d" % len(media))
    ok = result["error"] is None and len(media) >= 1 and all(s > 1024 for _n, s in media)
    print("RESULT = %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())