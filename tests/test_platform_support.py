"""Tests for the cross-platform plumbing that the macOS build depends on.

These guard the things that used to be Windows-only and would have crashed or
silently failed on macOS:

* ``creationflags`` is Windows-only -- CPython's POSIX subprocess raises
  ``ValueError`` for a non-zero value, which would break every download.
* helper binaries are named ``ffmpeg.exe`` on Windows but ``ffmpeg`` on macOS.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloader  # noqa: E402


class NoWindowKwargsTests(unittest.TestCase):
    def test_empty_on_posix(self):
        with mock.patch.object(downloader.os, "name", "posix"):
            self.assertEqual(downloader._no_window_kwargs(), {})

    def test_sets_create_no_window_on_windows(self):
        with mock.patch.object(downloader.os, "name", "nt"):
            self.assertEqual(downloader._no_window_kwargs(),
                             {"creationflags": 0x08000000})

    def test_result_is_accepted_by_subprocess(self):
        """The real regression: a non-empty creationflags on POSIX raises."""
        kwargs = downloader._no_window_kwargs()
        if os.name != "nt":
            self.assertEqual(kwargs, {})
        completed = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, **kwargs,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("ok", completed.stdout)


class HelperExeNameTests(unittest.TestCase):
    def test_posix_prefers_bare_name(self):
        with mock.patch.object(downloader.os, "name", "posix"):
            self.assertEqual(downloader.helper_exe_names("ffmpeg"),
                             ("ffmpeg", "ffmpeg.exe"))
            self.assertEqual(downloader.helper_exe_names("yt-dlp")[0], "yt-dlp")

    def test_windows_prefers_exe_suffix(self):
        with mock.patch.object(downloader.os, "name", "nt"):
            self.assertEqual(downloader.helper_exe_names("ffmpeg"),
                             ("ffmpeg.exe", "ffmpeg"))
            self.assertEqual(downloader.helper_exe_names("yt-dlp")[0], "yt-dlp.exe")

    def test_find_helper_tries_platform_spelling_first(self):
        """find_helper must try this platform's spelling, then the other one."""
        tried = []

        def fake(name, include_path=False):
            tried.append((name, include_path))
            return None  # nothing found anywhere

        with mock.patch.object(downloader, "find_bundled_exe", fake):
            with mock.patch.object(downloader.os, "name", "posix"):
                downloader.find_helper("ffmpeg", include_path=True)
                self.assertEqual([n for n, _ in tried], ["ffmpeg", "ffmpeg.exe"])
                self.assertTrue(all(ip for _, ip in tried))
            tried.clear()
            with mock.patch.object(downloader.os, "name", "nt"):
                downloader.find_helper("ffmpeg")
                self.assertEqual([n for n, _ in tried], ["ffmpeg.exe", "ffmpeg"])
                self.assertTrue(all(not ip for _, ip in tried))

    def test_find_helper_returns_first_hit(self):
        """A bare (macOS-style) name is found even on Windows."""
        def fake(name, include_path=False):
            return "/bundle/" + name if name == "ffmpeg" else None

        with mock.patch.object(downloader, "find_bundled_exe", fake):
            with mock.patch.object(downloader.os, "name", "posix"):
                self.assertEqual(downloader.find_helper("ffmpeg"), "/bundle/ffmpeg")
            with mock.patch.object(downloader.os, "name", "nt"):
                self.assertEqual(downloader.find_helper("ffmpeg"), "/bundle/ffmpeg")

    def test_find_helper_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            cwd = os.getcwd()
            try:
                os.chdir(folder)
                with mock.patch.object(downloader, "find_bundled_exe",
                                       lambda name, include_path=False: None):
                    self.assertIsNone(downloader.find_helper("definitely-not-here"))
            finally:
                os.chdir(cwd)


class OpenInFileManagerTests(unittest.TestCase):
    def test_missing_path_does_not_raise(self):
        """A bad folder must never break the download-complete handler."""
        downloader.open_in_file_manager(os.path.join(tempfile.gettempdir(),
                                                     "no-such-folder-xyz"))

    def test_uses_platform_appropriate_command(self):
        calls = []
        # os.name must be patched too: the Windows branch is checked first.
        with mock.patch.object(downloader.subprocess, "Popen",
                               lambda cmd: calls.append(cmd)), \
             mock.patch.object(downloader.os, "name", "posix"), \
             mock.patch.object(downloader.sys, "platform", "darwin"):
            downloader.open_in_file_manager("/tmp")
            self.assertEqual(calls[-1], ["open", "/tmp"])
            with mock.patch.object(downloader.sys, "platform", "linux"):
                downloader.open_in_file_manager("/tmp")
            self.assertEqual(calls[-1], ["xdg-open", "/tmp"])

    def test_windows_branch_uses_startfile(self):
        with mock.patch.object(downloader.os, "name", "nt"), \
             mock.patch.object(downloader.os, "startfile",
                               create=True) as startfile:
            downloader.open_in_file_manager(r"C:\Temp")
            startfile.assert_called_once_with(r"C:\Temp")


if __name__ == "__main__":
    unittest.main()