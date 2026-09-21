"""Tests for the GitHub-based release pipeline.

These cover what changed when updates moved off Google Drive:

* ``updater.py`` reads the newest GitHub Release instead of a hand-maintained
  ``update.json``. The self-update asset is chosen by filename *suffix*, so a
  version bump cannot break the update check - but a rename would, which is why
  the suffix is cross-checked against ``_make_update_package.py`` below.
* the release tooling: the cloud Windows build has to fetch its own helper
  binaries (``ffmpeg.exe`` alone is 227 MB, over GitHub's 100 MB file limit),
  and the installer must not hardcode absolute paths - doing so is what tied the
  earlier version of it to one machine and would have broken any cloud build.
"""
import os
import sys
import unittest
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import updater  # noqa: E402

_UPDATE_ASSET = "UniversalAudioStudio_2.1.0_update.zip"


def _release_payload(tag="v2.1.0", assets=None):
    """A trimmed copy of a real GitHub ``releases/latest`` response."""
    if assets is None:
        assets = [
            {
                "name": "UniversalAudioStudio-2.1.0-arm64.dmg",
                "browser_download_url": "https://example.invalid/arm64.dmg",
            },
            {
                "name": _UPDATE_ASSET,
                "browser_download_url": "https://example.invalid/update.zip",
            },
            {
                "name": "mysetup210.exe",
                "browser_download_url": "https://example.invalid/setup.exe",
            },
        ]
    return {"tag_name": tag, "assets": assets}


def _read(relative):
    with open(os.path.join(PROJECT_DIR, relative), encoding="utf-8") as handle:
        return handle.read()


class ReleaseVersionTests(unittest.TestCase):
    """A release tag carries a 'v' that the version comparison must not see."""

    def test_strips_leading_v(self):
        self.assertEqual(updater._release_version("v2.1.0"), "2.1.0")

    def test_tolerates_a_tag_without_v(self):
        self.assertEqual(updater._release_version("2.1.0"), "2.1.0")

    def test_survives_missing_or_odd_tags(self):
        for raw, expected in ((None, ""), ("", ""), ("  v3.0.0  ", "3.0.0")):
            self.assertEqual(updater._release_version(raw), expected)

    def test_stripped_version_still_compares_as_newer(self):
        """The two functions are used together, so check them together."""
        self.assertTrue(
            updater.is_newer_version(updater._release_version("v99.0.0")))
        self.assertFalse(
            updater.is_newer_version(updater._release_version("v0.0.1")))


class PickUpdateAssetTests(unittest.TestCase):
    """A Release holds four files; only one of them is the self-update ZIP."""

    def test_picks_the_update_zip_out_of_everything_else(self):
        url = updater._pick_update_asset(_release_payload()["assets"])
        self.assertEqual(url, "https://example.invalid/update.zip")

    def test_returns_none_when_no_update_zip_is_attached(self):
        assets = [{"name": "UniversalAudioStudio-2.1.0-arm64.dmg",
                   "browser_download_url": "https://example.invalid/arm64.dmg"}]
        self.assertIsNone(updater._pick_update_asset(assets))

    def test_returns_none_for_missing_or_empty_assets(self):
        for assets in (None, [], [{}], ["not-a-dict", 42]):
            self.assertIsNone(updater._pick_update_asset(assets))

    def test_ignores_an_asset_with_no_download_url(self):
        """The name matching but the URL empty must not be treated as a hit."""
        assets = [{"name": _UPDATE_ASSET, "browser_download_url": ""}]
        self.assertIsNone(updater._pick_update_asset(assets))

    def test_matches_the_suffix_without_case_sensitivity(self):
        assets = [{"name": _UPDATE_ASSET.upper(),
                   "browser_download_url": "https://example.invalid/upper.zip"}]
        self.assertEqual(updater._pick_update_asset(assets),
                         "https://example.invalid/upper.zip")

    def test_suffix_matches_what_the_packager_produces(self):
        """The suffix is the only link between updater and packager.

        Renaming the ZIP in _make_update_package.py without updating
        UPDATE_ASSET_SUFFIX would silently stop every future update.
        """
        packager = _read("_make_update_package.py")
        self.assertIn(
            updater.UPDATE_ASSET_SUFFIX, packager,
            "updater.UPDATE_ASSET_SUFFIX is not present in the ZIP name built "
            "by _make_update_package.py - the update check would never match.",
        )


class RemoteUpdateInfoTests(unittest.TestCase):
    """get_remote_update_info() must never raise, whatever it receives.

    ui.py calls it on a background thread at startup and again from the button,
    so an exception here would take out the update check entirely.
    """

    def test_parses_a_real_release_payload(self):
        with mock.patch.object(updater, "_request_json",
                               return_value=_release_payload()):
            info = updater.get_remote_update_info()
        self.assertEqual(info, {
            "version": "2.1.0",
            "download_url": "https://example.invalid/update.zip",
        })

    def test_asks_github_for_the_latest_release(self):
        with mock.patch.object(updater, "_request_json",
                               return_value=_release_payload()) as request:
            updater.get_remote_update_info()
        self.assertEqual(request.call_args[0][0], updater.GITHUB_RELEASES_API)
        self.assertIn("api.github.com", updater.GITHUB_RELEASES_API)

    def test_returns_none_instead_of_raising(self):
        """Everything that can realistically go wrong, including a 404 body."""
        payloads = (
            None,            # offline / rate limited / no release published yet
            [],              # JSON, but not an object
            "html",          # an error document instead of the API response
            7,
            {},
            {"tag_name": ""},
            {"tag_name": "v2.1.0"},                    # release, no assets
            {"tag_name": "v2.1.0", "assets": []},
            _release_payload(tag=""),                  # assets, but no version
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with mock.patch.object(updater, "_request_json",
                                       return_value=payload):
                    self.assertIsNone(updater.get_remote_update_info())

    def test_return_shape_is_what_ui_reads(self):
        """ui.py reads both keys, in two separate code paths."""
        with mock.patch.object(updater, "_request_json",
                               return_value=_release_payload()):
            info = updater.get_remote_update_info()
        self.assertEqual(set(info), {"version", "download_url"})


class DriveRetiredTests(unittest.TestCase):
    """Updates no longer come from Google Drive, which is the point of all this."""

    def test_updater_has_no_drive_plumbing_left(self):
        source = _read("updater.py")
        for needle in ("drive.google.com", "driveusercontent",
                       "UPDATE_MANIFEST_URL", "_drive_confirm_url"):
            self.assertNotIn(
                needle, source,
                "updater.py still references %r; updates are served by the "
                "GitHub Releases API now." % needle,
            )

    def test_the_drive_manifest_is_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(PROJECT_DIR, "update.json")),
            "update.json was the Google Drive manifest and is no longer used.",
        )


class ReleaseWorkflowTests(unittest.TestCase):
    """One workflow publishes one Release.

    A second workflow triggered by the same tag would race this one to create
    the Release, and whichever lost would have its files missing from it.
    """

    def test_the_macos_only_workflow_is_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(PROJECT_DIR, ".github", "workflows",
                                        "build-macos.yml")),
            "Two workflows triggered by the same tag race to create the Release.",
        )

    def test_workflow_builds_every_platform(self):
        text = _read(".github/workflows/release.yml")
        for needle in ("macos-15", "macos-15-intel", "windows-latest",
                       "bash tools/build_macos.sh", "tools/build_windows.ps1",
                       "innosetup"):
            self.assertIn(needle, text)

    def test_release_job_waits_for_every_build(self):
        self.assertIn("needs: [build-macos, build-windows]",
                      _read(".github/workflows/release.yml"))

    def test_release_job_runs_only_for_a_tag(self):
        self.assertIn("startsWith(github.ref, 'refs/tags/')",
                      _read(".github/workflows/release.yml"))

    def test_it_uploads_the_asset_the_updater_looks_for(self):
        self.assertIn(updater.UPDATE_ASSET_SUFFIX,
                      _read(".github/workflows/release.yml"))

    def test_macos_stays_on_python_313(self):
        """pedalboard publishes no cp314 macOS x86_64 wheel.

        Moving the macOS job to 3.14 would build the Apple Silicon app and fail
        the Intel one, which is exactly what happened before.
        """
        text = _read(".github/workflows/release.yml")
        self.assertIn("PYTHON_MACOS: '3.13'", text)
        self.assertIn("PYTHON_WINDOWS: '3.14'", text)

    def test_the_version_check_guards_the_release_filenames(self):
        self.assertIn("does not match version.py",
                      _read(".github/workflows/release.yml"))

    def test_the_release_job_has_python_for_the_version_check(self):
        """It reads version.py, and ubuntu-latest has no reliable `python`."""
        text = _read(".github/workflows/release.yml")
        release_job = text.split("\n  release:")[1]
        self.assertIn("actions/setup-python@", release_job)
        self.assertIn("tools/get_version.py", release_job)

    def test_the_release_writes_the_legacy_drive_manifest(self):
        """The one file that still goes to Drive, for the transition release."""
        release_job = _read(".github/workflows/release.yml").split(
            "\n  release:")[1]
        self.assertIn("tools/make_update_manifest.py", release_job)
        self.assertIn('--repo "${{ github.repository }}"', release_job)
        self.assertIn('--tag "${{ github.ref_name }}"', release_job)

    def test_every_release_file_is_attached(self):
        """update.json has to be published, or it cannot be downloaded."""
        self.assertIn("files: release-files/*",
                      _read(".github/workflows/release.yml"))


class WindowsToolingTests(unittest.TestCase):
    def test_fetcher_covers_every_bundled_binary(self):
        text = _read("tools/fetch_win_helpers.ps1")
        for name in ("ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe", "aria2c.exe"):
            self.assertIn(name, text)

    def test_fetcher_puts_helpers_where_the_spec_reads_them(self):
        """The spec bundles these from the repository root."""
        spec = _read("UniversalAudioStudio.spec")
        fetcher = _read("tools/fetch_win_helpers.ps1")
        for name in ("ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe"):
            self.assertIn(name, spec)
            self.assertIn(name, fetcher)

    def test_installer_takes_version_and_name_from_the_command_line(self):
        text = _read("installer_200.iss")
        self.assertIn("#ifndef AppVersion", text)
        self.assertIn("#ifndef OutputBase", text)
        self.assertIn("OutputBaseFilename={#OutputBase}", text)

    def test_installer_has_no_absolute_paths(self):
        """Absolute paths are why the installer only worked on one machine."""
        for line in _read("installer_200.iss").splitlines():
            stripped = line.strip()
            if stripped.startswith(";"):
                continue
            self.assertNotIn("C:\\Users", stripped, stripped)
            self.assertNotIn("Downloads\\scripts", stripped, stripped)

    def test_gitattributes_keeps_powershell_crlf(self):
        self.assertRegex(_read(".gitattributes"), r"\*\.ps1\s+text\s+eol=crlf")

    def test_ci_runs_the_same_script_you_run_locally(self):
        self.assertIn("tools/build_windows.ps1",
                      _read(".github/workflows/release.yml"))
        script = _read("tools/build_windows.ps1")
        for needle in ("fetch_win_helpers.ps1", "unittest",
                       "'--name', 'updater_cli", "UniversalAudioStudio.spec",
                       "_make_update_package.py", "installer_200.iss"):
            self.assertIn(needle, script)

    def test_helper_is_built_before_the_app(self):
        """UniversalAudioStudio.spec aborts without dist\\updater_cli.exe."""
        script = _read("tools/build_windows.ps1")
        helper = script.index("'--name', 'updater_cli'")
        app = script.index("'UniversalAudioStudio.spec', '-y'")
        self.assertLess(helper, app,
                        "updater_cli.exe must be built before the app spec runs.")


if __name__ == "__main__":
    unittest.main()


