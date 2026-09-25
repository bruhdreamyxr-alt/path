"""Tests for tools/make_update_manifest.py - the Google Drive retirement hop.

Copies of 2.0.0 and earlier fetch a manifest from a fixed Google Drive file and
download whatever ZIP it names. That code is already inside them, so the only way
to move them onto the GitHub updater is to use the old channel one last time.

The schema below is pinned on purpose: it is the contract the *shipped* app
parses. Changing it here would break the very release meant to fix things, and it
would fail silently - the old app would simply decide it is up to date and never
update.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(PROJECT_DIR, "tools")
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, TOOLS_DIR)

import make_update_manifest  # noqa: E402  # type: ignore[reportMissingImports]
import updater  # noqa: E402
from get_version import get_version  # noqa: E402  # type: ignore[reportMissingImports]

ZIP_NAME = "UniversalAudioStudio_2.1.0_update.zip"


class FindUpdateZipTests(unittest.TestCase):
    """The asset name is read from disk, so it cannot drift from the packager."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="uas-manifest-")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def _write(self, name):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as fh:
            fh.write("x")

    def test_finds_the_release_zip(self):
        self._write(ZIP_NAME)
        self.assertEqual(os.path.basename(
            make_update_manifest.find_update_zip(self.folder)), ZIP_NAME)

    def test_ignores_the_other_release_files(self):
        self._write("mysetup210.exe")
        self._write("UniversalAudioStudio-2.1.0-arm64.dmg")
        self._write(ZIP_NAME)
        self.assertEqual(os.path.basename(
            make_update_manifest.find_update_zip(self.folder)), ZIP_NAME)

    def test_a_missing_zip_is_an_error(self):
        with self.assertRaises(SystemExit):
            make_update_manifest.find_update_zip(self.folder)

    def test_two_zips_are_not_guessed_at(self):
        self._write(ZIP_NAME)
        self._write("UniversalAudioStudio_2.0.0_update.zip")
        with self.assertRaises(SystemExit):
            make_update_manifest.find_update_zip(self.folder)


class ManifestContentTests(unittest.TestCase):
    """The exact contract the already-installed app depends on."""

    def test_download_url_points_at_the_github_release_asset(self):
        manifest = make_update_manifest.build_manifest(
            "2.1.0", "owner/name", "v2.1.0", ZIP_NAME)
        self.assertEqual(
            manifest["download_url"],
            "https://github.com/owner/name/releases/download/v2.1.0/" + ZIP_NAME)

    def test_has_exactly_the_two_keys_the_old_app_reads(self):
        manifest = make_update_manifest.build_manifest(
            "2.1.0", "o/n", "v2.1.0", ZIP_NAME)
        self.assertEqual(set(manifest), {"version", "download_url"})
        self.assertTrue(manifest["version"])
        self.assertTrue(manifest["download_url"])

    def test_version_matches_version_py(self):
        version = get_version()
        manifest = make_update_manifest.build_manifest(
            version, "o/n", "v" + version, "UniversalAudioStudio_x_update.zip")
        self.assertEqual(manifest["version"], version)

    def test_the_transition_release_is_newer_than_what_is_installed(self):
        """2.0.0 is out there, and the old updater ignores anything <= its own.

        This is the single most important property of the whole transition: if
        version.py were left at 2.0.0, every installed copy would decide it was
        already up to date and the hop would never happen.
        """
        # Pretend to be the copy in the wild rather than the version being built,
        # otherwise this compares the version against itself and proves nothing.
        with mock.patch.object(updater, "CURRENT_VERSION", "2.0.0"):
            self.assertTrue(
                updater.is_newer_version(get_version()),
                "version.py must be a strictly higher version than the 2.0.0 "
                "copies already installed, or they will never take this update")

    def test_a_mismatched_tag_is_refused(self):
        """A wrong tag yields a 404 URL and a silently dead update path."""
        with self.assertRaises(SystemExit) as caught:
            make_update_manifest.build_manifest("2.1.0", "o/n", "v2.2.0", ZIP_NAME)
        self.assertIn("does not match version", str(caught.exception))


class ManifestCliTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="uas-manifest-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        with open(os.path.join(self.folder, ZIP_NAME), "w", encoding="utf-8") as fh:
            fh.write("x")

    def _run(self, *extra):
        return make_update_manifest.main([
            "--release-dir", self.folder, "--repo", "o/n", "--tag", "v2.1.0",
            "--version", "2.1.0",
        ] + list(extra))

    def test_writes_update_json_into_the_release_directory(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(self._run(), 0)
        with open(os.path.join(self.folder, "update.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["version"], "2.1.0")
        self.assertIn("releases/download/v2.1.0/", manifest["download_url"])

    def test_print_mode_writes_nothing(self):
        """Lets the URL be eyeballed before anything is uploaded to Drive."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(self._run("--print"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "update.json")))
        self.assertEqual(json.loads(buffer.getvalue())["version"], "2.1.0")


if __name__ == "__main__":
    unittest.main()
