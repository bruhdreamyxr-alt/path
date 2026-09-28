import os
import shutil
import tempfile
import unittest
from typing import Any

import downloader
import download_queue


class VideoFormatStringTests(unittest.TestCase):
    def test_best_is_unrestricted(self):
        self.assertEqual(
            downloader.video_format_string("Best (up to 4K)"), "bestvideo*+bestaudio/best"
        )

    def test_height_cap(self):
        self.assertIn("height<=1080", downloader.video_format_string("1080p"))
        self.assertIn("height<=720", downloader.video_format_string("720p"))

    def test_invalid_falls_back_to_best(self):
        self.assertEqual(
            downloader.video_format_string("not-a-number"), "bestvideo*+bestaudio/best"
        )


class PerfConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = downloader.perf_cfg_from_prefs(None)
        self.assertTrue(cfg["use_aria2"])
        self.assertEqual(cfg["aria2_connections"], 16)
        self.assertEqual(cfg["max_video_resolution"], "Best (up to 4K)")

    def test_aria2_connections_clamped_high(self):
        cfg = downloader.perf_cfg_from_prefs({"aria2_connections": 99})
        self.assertEqual(cfg["aria2_connections"], 16)

    def test_aria2_connections_clamped_low(self):
        cfg = downloader.perf_cfg_from_prefs({"aria2_connections": -5})
        self.assertEqual(cfg["aria2_connections"], 1)

    def test_aria2_connections_invalid_string(self):
        cfg = downloader.perf_cfg_from_prefs({"aria2_connections": "abc"})
        self.assertEqual(cfg["aria2_connections"], 16)

    def test_invalid_resolution_falls_back(self):
        cfg = downloader.perf_cfg_from_prefs({"max_video_resolution": "9999"})
        self.assertEqual(cfg["max_video_resolution"], "Best (up to 4K)")

    def test_valid_resolution_accepted(self):
        cfg = downloader.perf_cfg_from_prefs({"max_video_resolution": "720p"})
        self.assertEqual(cfg["max_video_resolution"], "720p")


class QueuePersistenceTests(unittest.TestCase):
    def test_item_round_trip(self):
        item = download_queue.QueueItem("https://youtu.be/abc", is_video=True)
        item.title = "My Video"
        restored = download_queue.QueueItem.from_dict(item.to_dict())
        self.assertEqual(restored.url, "https://youtu.be/abc")
        self.assertTrue(restored.is_video)
        self.assertEqual(restored.title, "My Video")
        # Restored items always restart as pending.
        self.assertEqual(restored.status, "pending")

    def test_save_and_load_pending(self):
        import os
        import tempfile

        tmpdir = tempfile.mkdtemp()
        orig = download_queue.QUEUE_FILE
        try:
            download_queue.QUEUE_FILE = os.path.join(tmpdir, "queue.json")
            items = [
                download_queue.QueueItem("https://a", is_video=False),
                download_queue.QueueItem("https://b", is_video=True),
            ]
            download_queue.save_pending_queue(items)
            loaded = download_queue.load_pending_queue()
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["url"], "https://a")
            self.assertTrue(loaded[1]["is_video"])
        finally:
            download_queue.QUEUE_FILE = orig

    def test_remove_and_retry(self):
        import os
        import tempfile

        tmpdir = tempfile.mkdtemp()
        orig = download_queue.QUEUE_FILE
        try:
            download_queue.QUEUE_FILE = os.path.join(tmpdir, "queue.json")
            q = download_queue.DownloadQueue(
                lambda *a: None, lambda *a: None, lambda: None, lambda: "."
            )
            q.add(["https://a", "https://b", "https://c"])
            self.assertEqual(len(q.items), 3)

            # Remove middle item.
            self.assertTrue(q.remove_at(1))
            self.assertEqual(len(q.items), 2)
            self.assertEqual([i.url for i in q.items], ["https://a", "https://c"])

            # Can't remove an active item.
            q._items[0].status = "active"
            self.assertFalse(q.remove_at(0))

            # Retry failed.
            q._items[0].status = "failed"
            q._items[1].status = "skipped"
            self.assertEqual(q.retry_failed(), 2)
            self.assertTrue(all(i.status == "pending" for i in q.items))
        finally:
            download_queue.QUEUE_FILE = orig


class ProducedMediaFileTests(unittest.TestCase):
    """Picker that decides which file a finished download produced.

    Regression guard: the queue used ``sorted(new_files)[0]``, so an
    alphabetically-earlier thumbnail or ``.info.json`` sidecar won and the ID3
    tags were never written to the audio file.
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="produced_media_")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def _touch(self, name, mtime):
        """Create *name* with a controlled modification time."""
        path = os.path.join(self.folder, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * 64)
        os.utime(path, (mtime, mtime))
        return path

    def test_ignores_newer_thumbnail(self):
        self._touch("track.mp3", 1_000)
        self._touch("track.webp", 2_000)
        self.assertEqual(
            os.path.basename(downloader.pick_produced_media_file(self.folder) or ""),
            "track.mp3",
        )

    def test_ignores_sidecar_and_image_files(self):
        for name in ("track.info.json", "track.jpg", "track.en.vtt", "track.part"):
            self._touch(name, 9_000)
        self._touch("track.mp3", 1_000)
        self._touch("track2.mp3", 5_000)
        self.assertEqual(
            os.path.basename(downloader.pick_produced_media_file(self.folder) or ""),
            "track2.mp3",
        )

    def test_picks_newest_by_mtime(self):
        self._touch("older.mp3", 1_000)
        self._touch("newer.mp3", 2_000)
        self.assertEqual(
            os.path.basename(downloader.pick_produced_media_file(self.folder) or ""),
            "newer.mp3",
        )

    def test_only_new_files_are_considered(self):
        """A pre-existing (even newer) file is not this download's output."""
        self._touch("previous.mp3", 9_000)
        self._touch("fresh.mp3", 1_000)
        picked = downloader.pick_produced_media_file(self.folder, {"previous.mp3"})
        self.assertEqual(os.path.basename(picked or ""), "fresh.mp3")

    def test_returns_none_when_nothing_new_appeared(self):
        self._touch("already.mp3", 1_000)
        self.assertIsNone(
            downloader.pick_produced_media_file(self.folder, {"already.mp3"})
        )

    def test_returns_none_for_folder_without_media(self):
        self._touch("cover.png", 1_000)
        self.assertIsNone(downloader.pick_produced_media_file(self.folder))

    def test_returns_none_for_missing_folder(self):
        self.assertIsNone(
            downloader.pick_produced_media_file(os.path.join(self.folder, "nope"))
        )


class QueueTagWritingTests(unittest.TestCase):
    """End-to-end queue path: tag the audio, never a sidecar or thumbnail.

    The sidecars are given *newer* mtimes and names that sort first on purpose,
    which is exactly what defeated the old ``sorted(new_files)[0]`` selection
    (``song.info.json`` sorted ahead of ``song.mp3``).
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="queue_tags_")
        self._orig_history = download_queue.HISTORY_FILE
        self._orig_queue = download_queue.QUEUE_FILE
        download_queue.HISTORY_FILE = os.path.join(self.folder, "history.json")
        download_queue.QUEUE_FILE = os.path.join(self.folder, "queue.json")
        self._orig_writer = downloader.write_id3_tags
        self.tagged = []
        downloader.write_id3_tags = self._recording_writer

    def tearDown(self):
        download_queue.HISTORY_FILE = self._orig_history
        download_queue.QUEUE_FILE = self._orig_queue
        downloader.write_id3_tags = self._orig_writer
        downloader.begin_download_session()  # drop metadata the fake download set
        shutil.rmtree(self.folder, ignore_errors=True)

    def _recording_writer(self, filepath, **kwargs):
        self.tagged.append((filepath, kwargs))
        return True

    def _write(self, name, mtime, payload=b"sidecar"):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as fh:
            fh.write(payload)
        os.utime(path, (mtime, mtime))
        return path

    def _fake_download(self, url, status, success, error, progress_callback=None):
        """Emulate yt-dlp writing audio plus its info.json and thumbnail."""
        downloader.set_last_dl_metadata(artist="Boards of Canada", title="Roygbiv")
        self._write("song.mp3", 1_000, b"\x00" * 2048)   # the real output
        self._write("song.info.json", 2_000)             # sorts first, newer
        self._write("song.jpg", 3_000)                   # thumbnail, newest

    def _run_queue(self):
        q = download_queue.DownloadQueue(
            self._fake_download, self._fake_download, lambda: None, lambda: self.folder
        )
        q.add(["https://example.com/track"], skip_existing=False)
        q._process_queue()
        return q.items[0]

    def test_queue_tags_the_audio_not_the_sidecars(self):
        item = self._run_queue()
        self.assertEqual(item.status, "done")
        self.assertEqual(os.path.basename(item.filepath), "song.mp3")
        self.assertEqual(len(self.tagged), 1)
        path, kwargs = self.tagged[0]
        self.assertEqual(os.path.basename(path), "song.mp3")
        self.assertEqual(kwargs["artist"], "Boards of Canada")
        self.assertEqual(kwargs["title"], "Roygbiv")
        # History shows a real title rather than the raw URL.
        self.assertEqual(item.title, "Roygbiv")

    def test_queue_history_entry_points_at_the_audio(self):
        self._run_queue()
        history = download_queue._read_json_file(download_queue.HISTORY_FILE)
        self.assertEqual(len(history), 1)
        self.assertEqual(os.path.basename(history[0]["filepath"]), "song.mp3")



class Aria2CommandLineTests(unittest.TestCase):
    """The yt-dlp.exe fallback must not hand aria2 a whitespace-split user-agent.

    yt-dlp splits ``--external-downloader-args`` on whitespace, so a value
    containing a browser user-agent was chopped apart and aria2c treated the
    fragment ``(Windows`` as a URI, aborting the download with::

        Exception: [download_helper.cc:451] errorCode=1
        Unrecognized URI or unsupported protocol: (Windows
        ERROR: aria2c exited with code 1

    The Python API path passes these as a *list*, so each element stays one argv
    entry and it was unaffected -- which is why this only ever broke the
    packaged EXE, where the yt-dlp.exe fallback is used.
    """

    def _capture_args(self, audio_only=False):
        captured = {}
        orig_run = downloader._run_yt_dlp_exe
        orig_path = downloader.get_fast_downloader_path
        downloader._run_yt_dlp_exe = (
            lambda args, status_callback=None: captured.setdefault("args", args)
        )
        downloader.get_fast_downloader_path = lambda: r"C:\fake\aria2c.exe"
        try:
            downloader._fallback_download_with_ytdlp_exe(
                "https://example.com/x",
                "%(title)s.%(ext)s",
                audio_only=audio_only,
                status_callback=lambda msg, color: None,
                perf_cfg={"use_aria2": True, "aria2_connections": 16},
            )
        finally:
            downloader._run_yt_dlp_exe = orig_run
            downloader.get_fast_downloader_path = orig_path
        return captured["args"]

    def test_aria2_args_have_no_space_bearing_values(self):
        args = self._capture_args()
        self.assertIn("aria2c", args)
        self.assertIn("--external-downloader-args", args)
        value = args[args.index("--external-downloader-args") + 1]
        # Every token must be a self-contained aria2 option.
        self.assertIn("-x", value.split())
        self.assertNotIn("(", value)
        self.assertNotIn(")", value)
        self.assertNotIn("Mozilla", value)

    def test_audio_mode_aria2_args_are_also_clean(self):
        args = self._capture_args(audio_only=True)
        if "--external-downloader-args" in args:
            value = args[args.index("--external-downloader-args") + 1]
            self.assertNotIn("Mozilla", value)
            self.assertNotIn("(", value)

    def test_unrecognized_uri_is_classified_as_aria2_failure(self):
        """The real aria2 error must be recognised so the no-aria2 retry runs."""
        err = (
            "yt-dlp.exe failed:\nException caught\n"
            "Exception: [download_helper.cc:451] errorCode=1 "
            "Unrecognized URI or unsupported protocol: (Windows\n"
            "ERROR: aria2c exited with code 1"
        )
        self.assertTrue(downloader._is_aria2_failure(err))


class VideoFormatSortTests(unittest.TestCase):
    """Resolution must outrank codec in format_sort — the pixelation bug.

    'vcodec:h264' used to come first, and yt-dlp ranks user sort fields
    ahead of the defaults, so codec beat resolution: on a 4K video the
    picker returned the top h264 rung (1080p @ ~4.2 Mbps) while 2160p
    VP9 @ ~24 Mbps existed — a bitrate gap that reads exactly as the
    blocky/pixelated output the user reported. These tests drive yt-dlp's
    real FormatSorter over synthetic formats (no network) and pin the
    shipped VIDEO_FORMAT_SORT_FIELDS.
    """

    # FormatSorter's constructor is untyped (yt-dlp ships no py.typed), so
    # keep pyright from over-inferencing the class attrs these tests store.
    _yt_dlp: Any = None
    _FormatSorter: Any = None

    FORMATS = [
        {"format_id": "hdr2160", "url": "https://example.com/f", "height": 2160, "fps": 60,
         "vcodec": "vp9", "acodec": "none", "ext": "webm",
         "dynamic_range": "HDR10"},
        {"format_id": "sdr2160", "url": "https://example.com/f", "height": 2160, "fps": 60,
         "vcodec": "vp9", "acodec": "none", "ext": "webm"},
        {"format_id": "h264_1080", "url": "https://example.com/f", "height": 1080, "fps": 60,
         "vcodec": "h264", "acodec": "none", "ext": "mp4"},
        {"format_id": "vp9_1080", "url": "https://example.com/f", "height": 1080, "fps": 60,
         "vcodec": "vp9", "acodec": "none", "ext": "webm"},
        {"format_id": "h264_720", "url": "https://example.com/f", "height": 720, "fps": 30,
         "vcodec": "h264", "acodec": "none", "ext": "mp4"},
    ]

    @classmethod
    def setUpClass(cls):
        try:
            import yt_dlp
            from yt_dlp.utils._utils import FormatSorter
        except ImportError as e:  # pragma: no cover - env without yt-dlp
            raise unittest.SkipTest("yt-dlp not available: %s" % e)
        cls._yt_dlp = yt_dlp
        cls._FormatSorter = FormatSorter

    def _best(self, formats, fields=None):
        ydl = self._yt_dlp.YoutubeDL(
            {"format_sort": list(fields or downloader.VIDEO_FORMAT_SORT_FIELDS)})
        sorter = self._FormatSorter(ydl, [])
        ranked = sorted((dict(f) for f in formats),
                        key=sorter.calculate_preference)
        return ranked[-1]["format_id"]  # yt-dlp keeps the maximum key

    def test_resolution_outranks_codec(self):
        # The exact regression: codec-first picked h264_1080 over sdr2160.
        self.assertEqual(self._best(self.FORMATS), "sdr2160")

    def test_h264_wins_a_resolution_tie(self):
        tie = [f for f in self.FORMATS if f["height"] == 1080]
        self.assertEqual(self._best(tie), "h264_1080")

    def test_sdr_beats_hdr_at_a_tie(self):
        # The AE re-encode does no tonemapping: HDR input would shift colors.
        self.assertEqual(
            self._best([self.FORMATS[0], self.FORMATS[1]]), "sdr2160")

    def test_codec_still_preferred_where_it_used_to_matter(self):
        # Old codec-first list must still pick h264 at equal resolution...
        tie = [f for f in self.FORMATS if f["height"] == 1080]
        self.assertEqual(
            self._best(tie, ["vcodec:h264", "acodec:aac", "res", "quality"]),
            "h264_1080")
        # ...but that same list loses 4K, which is why it was replaced.
        self.assertEqual(
            self._best(self.FORMATS, ["vcodec:h264", "acodec:aac", "res", "quality"]),
            "h264_1080")

    def test_build_opts_uses_the_shared_resolution_first_sort(self):
        opts = downloader.build_fast_yt_dlp_options(
            "/tmp", "x.%(ext)s", audio_only=False)
        fields = opts["format_sort"]
        self.assertEqual(fields[0], "res")
        self.assertLess(
            fields.index("res"),
            next(i for i, f in enumerate(fields) if f.startswith("vcodec")))

    def test_audio_options_have_no_video_sort(self):
        opts = downloader.build_fast_yt_dlp_options(
            "/tmp", "x.%(ext)s", audio_only=True)
        self.assertNotIn("format_sort", opts)


class FallbackFormatSortTests(unittest.TestCase):
    """The yt-dlp.exe fallback must apply the same resolution-first sort."""

    def _capture_video_args(self, audio_only=False, format_override=None):
        captured = {}
        orig_run = downloader._run_yt_dlp_exe
        try:
            downloader._run_yt_dlp_exe = (
                lambda args, status_callback=None: captured.setdefault("args", args)
            )
            downloader._fallback_download_with_ytdlp_exe(
                "https://example.com/x",
                "%(title)s.%(ext)s",
                audio_only=audio_only,
                status_callback=lambda msg, color: None,
                perf_cfg={"use_aria2": False},
                format_override=format_override,
            )
        finally:
            downloader._run_yt_dlp_exe = orig_run
        return captured["args"]

    def test_video_branch_sorts_resolution_first(self):
        args = self._capture_video_args()
        self.assertIn("--format-sort", args)
        value = args[args.index("--format-sort") + 1]
        self.assertTrue(value.startswith("res,"), value)
        self.assertIn("vcodec:h264", value)

    def test_video_override_branch_also_sorts(self):
        # download_video_mp4's fallback path passes the format through
        # format_override; it must not lose the sort either.
        args = self._capture_video_args(
            format_override="bestvideo*+bestaudio/best")
        self.assertIn("--format-sort", args)

    def test_audio_branch_has_no_format_sort(self):
        args = self._capture_video_args(audio_only=True)
        self.assertNotIn("--format-sort", args)


class AeReencodeArgsTests(unittest.TestCase):
    """The AE re-encode must declare a spec-correct level and keep quality.

    The forced '-level 4.0' was out of spec for >=1080p60/4K; hardware
    decoders sized to the declared level glitch on out-of-spec streams —
    scattered pixelated frames right after downloading.
    """

    def test_no_forced_level(self):
        self.assertNotIn("-level", downloader._ae_reencode_args("a.mp4", "b.mp4"))

    def test_keeps_visually_lossless_quality(self):
        args = downloader._ae_reencode_args("a.mp4", "b.mp4")
        self.assertEqual(args[:3], ["-y", "-i", "a.mp4"])
        self.assertEqual(args[-1], "b.mp4")
        self.assertEqual(args[args.index("-crf") + 1], "18")
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")

    def test_timeout_survives_long_4k_encodes(self):
        # 600s killed long re-encodes mid-way and silently left the
        # AE-incompatible source file behind.
        self.assertGreaterEqual(downloader.AE_REENCODE_TIMEOUT, 3600)


if __name__ == "__main__":
    unittest.main()