"""Tests for the cross-platform plumbing that the macOS build depends on.

These guard the things that used to be Windows-only and would have crashed or
silently failed on macOS:

* ``creationflags`` is Windows-only -- CPython's POSIX subprocess raises
  ``ValueError`` for a non-zero value, which would break every download.
* helper binaries are named ``ffmpeg.exe`` on Windows but ``ffmpeg`` on macOS.
"""
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import download_queue  # noqa: E402
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


class UserDataDirTests(unittest.TestCase):
    """Where runtime data goes on macOS.

    ``%APPDATA%``/``%LOCALAPPDATA%`` do not exist on macOS, so a Windows-only
    lookup finds nothing and falls through to the folder next to the
    executable. Inside a ``.app`` bundle that is read-only as soon as Gatekeeper
    applies App Translocation to a downloaded app, and writing to it also
    invalidates the ad-hoc code signature - so history, the artwork cache and
    the UI prefs all have to live in ``~/Library/Application Support``.
    """

    _WINDOWS_ONLY_VARS = ("APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME")
    _MAC_HOME = "/Users/tester"
    # Joined from components exactly like the code under test, so the value
    # matches on whichever platform the suite runs (Windows inserts "\\").
    _MAC_EXPECTED = os.path.join("/Users/tester", "Library",
                                 "Application Support", "AudioDownloader")

    def _simulate(self, platform, home):
        """Make the environment look like a native Mac/Linux run.

        Clears the Windows-only variables and fakes the platform + home dir.
        Returns an ExitStack for use as a context manager.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in self._WINDOWS_ONLY_VARS}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(mock.patch.object(sys, "platform", platform))
        stack.enter_context(mock.patch.object(os.path, "expanduser",
                                              lambda p: home))
        return stack

    def test_macos_uses_library_application_support(self):
        # os.makedirs is patched: the faked "/Users/tester" path would otherwise
        # be created for real on a Windows machine running this suite.
        with self._simulate("darwin", self._MAC_HOME), mock.patch("os.makedirs"):
            self.assertEqual(download_queue._get_app_data_dir(),
                             self._MAC_EXPECTED)

    def test_macos_user_data_dir_matches_history_dir(self):
        """Cache, updatable yt-dlp copy and history must share one folder."""
        with self._simulate("darwin", self._MAC_HOME), mock.patch("os.makedirs"):
            self.assertEqual(downloader._get_user_data_dir(),
                             download_queue._get_app_data_dir())

    def test_macos_never_resolves_inside_the_app_bundle(self):
        """The actual regression: writing into the .app breaks its signature."""
        app = "/Users/tester/Applications/UniversalAudioStudio.app"
        exe = os.path.join(app, "Contents", "MacOS", "UniversalAudioStudio")
        with self._simulate("darwin", self._MAC_HOME), \
             mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", exe), \
             mock.patch("os.makedirs"):
            resolved = download_queue._get_app_data_dir()
        self.assertEqual(resolved, self._MAC_EXPECTED)
        self.assertNotIn(".app", resolved)

    def test_linux_uses_xdg_data_home(self):
        with self._simulate("linux", "/home/tester"), mock.patch("os.makedirs"):
            self.assertEqual(
                download_queue._get_app_data_dir(),
                os.path.join("/home/tester", ".local", "share",
                             "AudioDownloader"),
            )

    def test_windows_still_prefers_appdata(self):
        """Regression guard: the original Windows fix must keep working."""
        appdata = r"C:\Users\tester\AppData\Roaming"
        env = {k: v for k, v in os.environ.items()
               if k not in self._WINDOWS_ONLY_VARS}
        env["APPDATA"] = appdata
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("os.makedirs"):
            self.assertEqual(download_queue._get_app_data_dir(),
                             os.path.join(appdata, "AudioDownloader"))

    def test_ui_prefs_use_the_same_folder(self):
        import ui  # lazy: importing ui pulls in customtkinter/tkinter

        # __init__ would create a real Tk window, so bypass it.
        window = object.__new__(ui.UniversalAudioStudio)
        with self._simulate("darwin", self._MAC_HOME), \
             mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("os.makedirs"):
            self.assertEqual(
                window._get_pref_path(),
                os.path.join(self._MAC_EXPECTED,
                             ui.UniversalAudioStudio._PREF_FILENAME),
            )


if __name__ == "__main__":
    unittest.main()