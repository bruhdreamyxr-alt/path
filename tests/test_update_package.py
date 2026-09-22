"""Tests for _make_update_package.py - the archive the in-app updater consumes.

The entry ORDER is what these tests are about.

On 2026-09-21 an update stopped after writing two files. The app survived only
because the untouched files happened to be compatible. Had the updater itself
been near the end of that archive, the abort would have left the *old* updater in
place, the next release would fail the same way, and the install would be stuck
on a broken updater permanently - with nothing in the UI to say so.

Writing it first turns "stuck forever" into "self-heals on the next release".
"""
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import _make_update_package  # noqa: E402


class UpdatePackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uas-package-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.dist_dir = os.path.join(self.tmp, "dist")
        self.app_dir = os.path.join(self.dist_dir, "UniversalAudioStudio")
        os.makedirs(os.path.join(self.app_dir, "_internal", "nested"))

        self._write(os.path.join(self.app_dir, "UniversalAudioStudio.exe"), "app")
        self._write(os.path.join(self.app_dir, "_internal", "VCRUNTIME140.dll"), "dll")
        self._write(os.path.join(self.app_dir, "_internal", "tcl90.dll"), "tcl")
        self._write(os.path.join(self.app_dir, "_internal", "nested", "deep.txt"), "deep")
        self._write(os.path.join(self.app_dir, "_internal", "updater_cli.exe"),
                    "inner-updater")
        self._write(os.path.join(self.dist_dir, "updater_cli.exe"), "outer-updater")

        self.out = os.path.join(self.tmp, "package.zip")

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _build(self):
        return _make_update_package.build_update_package(
            self.app_dir, self.dist_dir, self.out)

    def _names(self):
        with zipfile.ZipFile(self.out) as zf:
            return zf.namelist()

    def test_both_updater_copies_come_first(self):
        self._build()
        names = self._names()
        self.assertEqual(names[0], "_internal/updater_cli.exe")
        self.assertEqual(names[1], "updater_cli.exe")

    def test_an_early_abort_still_leaves_the_new_updater(self):
        """Reproduces the real failure: extraction stops after two entries."""
        self._build()
        extracted_before_any_failure = self._names()[:2]
        self.assertIn("_internal/updater_cli.exe", extracted_before_any_failure)
        self.assertIn("updater_cli.exe", extracted_before_any_failure)

    def test_every_app_file_is_included(self):
        count, size = self._build()
        names = set(self._names())
        for expected in ("UniversalAudioStudio.exe",
                         "_internal/VCRUNTIME140.dll",
                         "_internal/tcl90.dll",
                         "_internal/nested/deep.txt"):
            self.assertIn(expected, names)
        self.assertGreater(size, 0)
        self.assertEqual(count, len(names))

    def test_no_duplicate_entries(self):
        """Adding the helpers first must not leave the walk to add them again."""
        self._build()
        names = self._names()
        self.assertEqual(len(names), len(set(names)))

    def test_the_outer_build_is_used_for_the_root_copy(self):
        self._build()
        with zipfile.ZipFile(self.out) as zf:
            self.assertEqual(zf.read("updater_cli.exe").decode(), "outer-updater")
            self.assertEqual(
                zf.read("_internal/updater_cli.exe").decode(), "inner-updater")

    def test_refuses_to_build_a_package_with_no_updater(self):
        """Shipping without one would break every future update, silently."""
        os.remove(os.path.join(self.app_dir, "_internal", "updater_cli.exe"))
        os.remove(os.path.join(self.dist_dir, "updater_cli.exe"))
        with self.assertRaises(SystemExit) as caught:
            self._build()
        self.assertIn("self-updater", str(caught.exception))

    def test_one_helper_is_enough(self):
        """A single EXE-only build still produces a usable package."""
        os.remove(os.path.join(self.app_dir, "_internal", "updater_cli.exe"))
        self._build()
        names = self._names()
        self.assertEqual(names[0], "updater_cli.exe")

    def test_version_is_read_from_version_py(self):
        self.assertRegex(_make_update_package.read_version(), r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
