import os
import shutil
import tempfile
import unittest

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
            os.path.basename(downloader.pick_produced_media_file(self.folder)),
            "track.mp3",
        )

    def test_ignores_sidecar_and_image_files(self):
        for name in ("track.info.json", "track.jpg", "track.en.vtt", "track.part"):
            self._touch(name, 9_000)
        self._touch("track.mp3", 1_000)
        self._touch("track2.mp3", 5_000)
        self.assertEqual(
            os.path.basename(downloader.pick_produced_media_file(self.folder)),
            "track2.mp3",
        )

    def test_picks_newest_by_mtime(self):
        self._touch("older.mp3", 1_000)
        self._touch("newer.mp3", 2_000)
        self.assertEqual(
            os.path.basename(downloader.pick_produced_media_file(self.folder)),
            "newer.mp3",
        )

    def test_only_new_files_are_considered(self):
        """A pre-existing (even newer) file is not this download's output."""
        self._touch("previous.mp3", 9_000)
        self._touch("fresh.mp3", 1_000)
        picked = downloader.pick_produced_media_file(self.folder, {"previous.mp3"})
        self.assertEqual(os.path.basename(picked), "fresh.mp3")

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


if __name__ == "__main__":
    unittest.main()