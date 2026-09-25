"""Tests for updater_cli.py - the code that replaces the app in place.

This is the highest-stakes code in the project: it runs while the app is shutting
down, writes into a directory it may not own, and a mistake leaves someone with
an app that will not start.

A real failure motivated these tests. The updater gave a locked file five
one-second attempts and then called ``sys.exit``, abandoning the rest of the
package. On 2026-09-21 that left an install with the new main EXE but the *old*
``_internal`` beside it - only two of 1045 files written. It happened to keep
working because the old files were compatible, but one file later the new
``_tkinter.pyd`` would have been in place without its matching ``tcl90.dll`` and
the app would not have launched at all.
"""
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import updater_cli  # noqa: E402


def _make_zip(path, files):
    """Write a ZIP containing ``{name: text}``."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


def _force_remove(path):
    """rmtree that copes with read-only files (they cannot be deleted as-is)."""
    def on_error(func, target, _exc):
        try:
            os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=on_error)


class UpdaterCliTestCase(unittest.TestCase):
    """Shared scaffolding: an isolated app dir, zip, and log file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uas-updater-")
        self.addCleanup(_force_remove, self.tmp)
        self.app_dir = os.path.join(self.tmp, "app")
        os.makedirs(self.app_dir)

        # Never touch the real %TEMP%\updater_cli.log from a test run.
        self.log_path = os.path.join(self.tmp, "updater_cli.log")
        self._saved_log = updater_cli.LOG_PATH
        updater_cli.LOG_PATH = self.log_path
        self.addCleanup(self._restore_log_path)

        # _log() prints as well as writing to the file; the tests read the file,
        # so keep the noise out of the suite's own output.
        printer = mock.patch("builtins.print")
        printer.start()
        self.addCleanup(printer.stop)

        self.package = os.path.join(self.tmp, "package.zip")

    def _restore_log_path(self):
        updater_cli.LOG_PATH = self._saved_log

    def log_text(self):
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, encoding="utf-8") as fh:
            return fh.read()

    def read(self, relative):
        with open(os.path.join(self.app_dir, relative), encoding="utf-8") as fh:
            return fh.read()

    def lock(self, relative):
        """Make a file read-only so writing it raises PermissionError.

        Mimics the real failure: another process holding the file.
        """
        os.chmod(os.path.join(self.app_dir, relative), stat.S_IREAD)

    def unlock(self, relative):
        # Read *and* write. On POSIX, S_IWRITE alone is 0o200 - write-only - so
        # the assertions that read the file back would fail with EACCES even on
        # a healthy machine. Windows only uses S_IWRITE to clear the read-only
        # attribute, which is why this passed locally and failed on macOS.
        os.chmod(os.path.join(self.app_dir, relative),
                 stat.S_IREAD | stat.S_IWRITE)


class CopyMemberTests(UpdaterCliTestCase):
    def test_writes_the_member_and_reports_success(self):
        _make_zip(self.package, {"a.txt": "hello"})
        with zipfile.ZipFile(self.package) as zf:
            member = zf.infolist()[0]
            target = os.path.join(self.app_dir, "a.txt")
            self.assertIsNone(updater_cli._copy_member(zf, member, target))
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello")

    def test_reports_a_target_that_cannot_be_opened(self):
        """A directory in the file's place gives an OSError, not a crash."""
        _make_zip(self.package, {"dir/thing.txt": "x"})
        os.makedirs(os.path.join(self.app_dir, "dir", "thing.txt"))
        with zipfile.ZipFile(self.package) as zf:
            member = zf.infolist()[0]
            error = updater_cli._copy_member(
                zf, member, os.path.join(self.app_dir, "dir", "thing.txt"))
        self.assertIsNotNone(error)

    def test_detects_a_short_write(self):
        """A truncated DLL is worse than a retry, so it counts as a failure."""
        _make_zip(self.package, {"big.dll": "0123456789"})

        def truncated(src, dst, *args, **kwargs):
            dst.write(src.read(2))  # deliberately incomplete

        original = updater_cli.shutil.copyfileobj
        updater_cli.shutil.copyfileobj = truncated
        try:
            with zipfile.ZipFile(self.package) as zf:
                member = zf.infolist()[0]
                error = updater_cli._copy_member(
                    zf, member, os.path.join(self.app_dir, "big.dll"))
        finally:
            updater_cli.shutil.copyfileobj = original
        self.assertIsNotNone(error)
        assert error is not None  # the copy above is expected to produce one
        self.assertIn("short write", error)


class ExtractZipTests(UpdaterCliTestCase):
    def test_installs_every_file(self):
        _make_zip(self.package, {
            "UniversalAudioStudio.exe": "exe",
            "_internal/python314.dll": "dll",
            "_internal/_tkinter.pyd": "pyd",
            "_internal/tcl90.dll": "tcl",
        })
        self.assertTrue(
            updater_cli._extract_zip_into(self.package, self.app_dir, retry_seconds=0))
        self.assertEqual(self.read("UniversalAudioStudio.exe"), "exe")
        self.assertEqual(self.read("_internal/tcl90.dll"), "tcl")
        self.assertIn("written and verified", self.log_text())

    def test_creates_missing_subdirectories(self):
        _make_zip(self.package, {"_internal/deep/nested/file.txt": "x"})
        self.assertTrue(
            updater_cli._extract_zip_into(self.package, self.app_dir, retry_seconds=0))
        self.assertEqual(self.read("_internal/deep/nested/file.txt"), "x")

    def test_a_locked_file_does_not_stop_the_rest(self):
        """The regression: one locked file used to abandon the whole package.

        On 2026-09-21 that meant a new main EXE sitting beside the old
        _internal, with 1043 of 1045 files never written.
        """
        _make_zip(self.package, {
            "_internal/VCRUNTIME140.dll": "locked",
            "_internal/_tkinter.pyd": "pyd",
            "_internal/tcl90.dll": "tcl",
            "UniversalAudioStudio.exe": "exe",
        })
        os.makedirs(os.path.join(self.app_dir, "_internal"))
        with open(os.path.join(self.app_dir, "_internal", "VCRUNTIME140.dll"),
                  "w", encoding="utf-8") as fh:
            fh.write("old")
        self.lock("_internal/VCRUNTIME140.dll")

        complete = updater_cli._extract_zip_into(
            self.package, self.app_dir, retry_seconds=0)

        self.assertFalse(complete, "a file that could not be written must be reported")
        # Everything else must still be installed - that is the whole point.
        self.assertEqual(self.read("_internal/_tkinter.pyd"), "pyd")
        self.assertEqual(self.read("_internal/tcl90.dll"), "tcl")
        self.assertEqual(self.read("UniversalAudioStudio.exe"), "exe")
        # And the victim keeps its previous contents rather than being truncated.
        self.assertEqual(self.read("_internal/VCRUNTIME140.dll"), "old")
        self.assertIn("UPDATE INCOMPLETE", self.log_text())

    def test_retries_until_a_locked_file_frees_up(self):
        """A lock is usually a release delay, so waiting is the fix."""
        _make_zip(self.package, {"_internal/VCRUNTIME140.dll": "new"})
        os.makedirs(os.path.join(self.app_dir, "_internal"))
        with open(os.path.join(self.app_dir, "_internal", "VCRUNTIME140.dll"),
                  "w", encoding="utf-8") as fh:
            fh.write("old")
        self.lock("_internal/VCRUNTIME140.dll")

        timer = threading.Timer(
            1.5, self.unlock, args=("_internal/VCRUNTIME140.dll",))
        timer.start()
        self.addCleanup(timer.cancel)

        complete = updater_cli._extract_zip_into(
            self.package, self.app_dir, retry_seconds=30)

        self.assertTrue(complete, self.log_text())
        self.assertEqual(self.read("_internal/VCRUNTIME140.dll"), "new")
        self.assertIn("Retry pass", self.log_text())

    def test_gives_up_once_the_deadline_passes(self):
        _make_zip(self.package, {"_internal/VCRUNTIME140.dll": "new"})
        os.makedirs(os.path.join(self.app_dir, "_internal"))
        with open(os.path.join(self.app_dir, "_internal", "VCRUNTIME140.dll"),
                  "w", encoding="utf-8") as fh:
            fh.write("old")
        self.lock("_internal/VCRUNTIME140.dll")
        self.assertFalse(updater_cli._extract_zip_into(
            self.package, self.app_dir, retry_seconds=0))

    def test_refuses_to_write_outside_the_app_directory(self):
        """Zip-slip guard: a crafted member must not escape the install dir."""
        _make_zip(self.package, {"../escaped.txt": "nope", "ok.txt": "fine"})
        self.assertTrue(
            updater_cli._extract_zip_into(self.package, self.app_dir, retry_seconds=0))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.txt")))
        self.assertEqual(self.read("ok.txt"), "fine")
        self.assertIn("Refusing to extract", self.log_text())


class RelocationTests(UpdaterCliTestCase):
    """The updater must not work from inside the directory it replaces.

    Windows will not replace a running EXE, and it resolves a process's DLLs
    from that process's own directory. While the updater ran from ``_internal``,
    four files stayed locked through all 120 retry passes - ``updater_cli.exe``,
    ``python3.dll`` and both ``VCRUNTIME140*.dll`` - so every update ended as
    "UPDATE INCOMPLETE" and the updater could never improve itself.
    """

    def _fake_running_exe(self, directory):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "updater_cli.exe")
        with open(path, "wb") as fh:
            fh.write(b"MZ fake")
        return path

    def _clear_flag(self):
        os.environ.pop(updater_cli._RELOCATED_ENV, None)

    def test_running_from_inside_the_install_relocates(self):
        running = self._fake_running_exe(os.path.join(self.app_dir, "_internal"))
        self._clear_flag()
        with mock.patch.object(updater_cli.sys, "executable", running), \
             mock.patch.object(updater_cli.subprocess, "Popen") as popen:
            self.assertTrue(updater_cli._relocate_if_inside_app(self.app_dir))

        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args[0][0]
        self.addCleanup(shutil.rmtree, os.path.dirname(argv[0]), True)
        self.assertTrue(os.path.isfile(argv[0]), "the copy must exist before it runs")
        self.assertNotEqual(os.path.dirname(argv[0]), os.path.dirname(running),
                            "the relocated copy must live outside the install")
        self.assertEqual(popen.call_args[1]["env"][updater_cli._RELOCATED_ENV], "1")
        self.assertIn("Relocated", self.log_text())

    def test_the_root_copy_counts_as_inside_too(self):
        """It is launched from either <app>\\ or <app>\\_internal\\."""
        running = self._fake_running_exe(self.app_dir)
        self._clear_flag()
        with mock.patch.object(updater_cli.sys, "executable", running), \
             mock.patch.object(updater_cli.subprocess, "Popen") as popen:
            self.assertTrue(updater_cli._relocate_if_inside_app(self.app_dir))
        argv = popen.call_args[0][0]
        self.addCleanup(shutil.rmtree, os.path.dirname(argv[0]), True)

    def test_a_relocated_run_never_relocates_again(self):
        running = self._fake_running_exe(os.path.join(self.app_dir, "_internal"))
        with mock.patch.dict(os.environ, {updater_cli._RELOCATED_ENV: "1"}), \
             mock.patch.object(updater_cli.sys, "executable", running), \
             mock.patch.object(updater_cli.subprocess, "Popen") as popen:
            self.assertFalse(updater_cli._relocate_if_inside_app(self.app_dir))
        self.assertEqual(popen.call_count, 0, "relocating twice would loop forever")

    def test_running_from_outside_is_left_alone(self):
        running = self._fake_running_exe(os.path.join(self.tmp, "elsewhere"))
        self._clear_flag()
        with mock.patch.object(updater_cli.sys, "executable", running), \
             mock.patch.object(updater_cli.subprocess, "Popen") as popen:
            self.assertFalse(updater_cli._relocate_if_inside_app(self.app_dir))
        self.assertEqual(popen.call_count, 0)


if __name__ == "__main__":
    unittest.main()
