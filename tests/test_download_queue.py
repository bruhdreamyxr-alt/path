"""Regression guards for DownloadQueue's public surface.

ui.py reads `is_running` in three places, but DownloadQueue only ever
exposed the private `_running`, so every queue action raised AttributeError
in the packaged app (where the traceback goes nowhere). The surface test
below makes that class of mistake impossible to reintroduce: every
attribute ui.py reads off the queue must actually exist.

The lifecycle tests cover exactly what ui.py depends on: is_running is
False before start(), True while the worker is busy, and False again once
the queue drains.
"""
import pathlib
import queue as stdlib_queue  # its members (Queue, Empty) are not queue-manager API
import re
import shutil
import tempfile
import threading
import time
import unittest

from download_queue import DownloadQueue

_REPO = pathlib.Path(__file__).resolve().parents[1]


def _queue_attributes_read_by_ui() -> set[str]:
    """Every name ui.py resolves on the queue manager, as a set of strings."""
    source = (_REPO / "ui.py").read_text(encoding="utf-8")
    names = set(re.findall(r"_queue_manager\.(\w+)", source))
    # ui.py also binds the manager to a local `queue` inside _enqueue_urls;
    # those matches collide with `import queue` (stdlib) uses such as
    # queue.Queue, so anything the stdlib module provides is not queue API.
    aliases = set(re.findall(r"(\w+)\s*=\s*self\._queue_manager\b", source))
    for alias in aliases:
        pattern = r"(?<![\w.])%s\.(\w+)" % alias
        names |= {n for n in re.findall(pattern, source)
                  if not hasattr(stdlib_queue, n)}
    return names


def _make_queue(save_folder, download_fn=None) -> DownloadQueue:
    q = DownloadQueue(
        download_fn=download_fn or (lambda *a, **k: None),
        video_fn=lambda *a, **k: None,
        find_file_fn=lambda *a, **k: None,
        save_folder_fn=lambda: save_folder,
    )
    # Keep the suite out of %APPDATA%: plain instance attributes shadow the
    # history/persist methods, so no JSON is ever read or written.
    q._save_history = lambda: None
    q._persist = lambda: None
    return q


class QueueRowActionTests(unittest.TestCase):
    """Skip one item, and reorder the waiting ones.

    Both act on a single row, which is the whole point: before them the only
    way to drop one item was to cancel the queue and start again.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.q = _make_queue(self.dir)
        self.q.add(["https://example.com/a", "https://example.com/b",
                    "https://example.com/c"])

    def _tails(self):
        return [i.url.rsplit("/", 1)[-1] for i in self.q.items]

    def test_move_swaps_with_a_neighbour(self):
        self.assertTrue(self.q.move(1, 1))
        self.assertEqual(self._tails(), ["a", "c", "b"])

    def test_move_reports_failure_rather_than_raising(self):
        self.assertFalse(self.q.move(0, -1))    # already first
        self.assertFalse(self.q.move(2, 1))     # already last
        self.assertFalse(self.q.move(9, 1))     # out of range
        self.assertFalse(self.q.move(0, 0))     # not a move
        self.assertEqual(self._tails(), ["a", "b", "c"])

    def test_a_finished_item_cannot_be_reordered(self):
        self.q.items[0].status = "done"
        self.assertFalse(self.q.move(0, 1))
        self.assertEqual(self._tails(), ["a", "b", "c"])

    def test_a_move_cannot_jump_the_item_in_flight(self):
        # This is what makes reordering safe while the queue is draining: the
        # active item is never pending, so no move can cross it and leave
        # _active_index pointing at the wrong row.
        self.q.items[1].status = "active"
        self.assertFalse(self.q.move(2, -1))
        self.assertEqual(self._tails(), ["a", "b", "c"])

    def test_skip_marks_one_pending_item_and_leaves_the_others(self):
        self.assertTrue(self.q.skip(1))
        self.assertEqual([i.status for i in self.q.items],
                         ["pending", "skipped", "pending"])

    def test_skip_refuses_items_that_are_not_waiting(self):
        self.q.items[0].status = "done"
        self.assertFalse(self.q.skip(0))
        self.assertFalse(self.q.skip(9))

    def test_a_skipped_item_can_be_retried(self):
        self.q.skip(1)
        self.assertEqual(self.q.retry_failed(), 1)
        self.assertEqual(self.q.items[1].status, "pending")


class QueueSurfaceTests(unittest.TestCase):
    def test_ui_only_reads_attributes_the_queue_actually_has(self):
        names = _queue_attributes_read_by_ui()
        # The bug this test exists for: ui.py called is_running before it was
        # ever implemented, and nothing in the test-suite looked at that seam.
        self.assertIn("is_running", names)
        q = _make_queue("")
        for name in sorted(names):
            self.assertTrue(
                hasattr(q, name),
                "ui.py reads the queue's %r, but DownloadQueue has no "
                "such attribute" % name,
            )


class IsRunningLifecycleTests(unittest.TestCase):
    def test_starts_idle(self):
        self.assertFalse(_make_queue("").is_running)

    def test_is_a_read_only_view_of_the_worker_flag(self):
        q = _make_queue("")
        setattr(q, "_running", True)  # what an in-flight worker looks like
        self.assertTrue(q.is_running)
        setattr(q, "_running", False)
        self.assertFalse(q.is_running)

    def test_start_on_an_empty_queue_returns_to_idle(self):
        q = _make_queue("")
        q.start()
        self._wait_idle(q)
        self.assertFalse(q.is_running, "empty queue should drain immediately")

    def test_reports_busy_while_a_download_is_in_flight(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        started = threading.Event()
        release = threading.Event()

        def blocking_download(url, status, on_file, on_error, progress_callback=None):
            started.set()
            release.wait(5)

        q = _make_queue(tmp, download_fn=blocking_download)
        q.add(["https://example.invalid/file.mp3"], skip_existing=False)
        q.start()
        self.assertTrue(started.wait(5), "worker never reached the download")
        try:
            self.assertTrue(q.is_running, "queue must report active mid-download")
        finally:
            release.set()
        self._wait_idle(q)
        self.assertFalse(q.is_running)

    @staticmethod
    def _wait_idle(q, timeout=5.0):
        deadline = time.monotonic() + timeout
        while q.is_running and time.monotonic() < deadline:
            time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
