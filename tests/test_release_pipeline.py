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
                "name": "UniversalAudioStudio-2.1.0-x86_64.dmg",
                "browser_download_url": "https://example.invalid/x86_64.dmg",
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


class MacosUpdateDetectionTests(unittest.TestCase):
    """macOS cannot self-update, so it points at the .dmg instead.

    Until now the Mac button only said self-update is Windows-only, and the
    startup check offered an install that promised a UAC prompt which cannot
    exist on a Mac - so a Mac user was never told an update was available.
    """

    ASSETS = {
        "UniversalAudioStudio-2.1.1-arm64.dmg": "https://x/arm64",
        "UniversalAudioStudio-2.1.1-x86_64.dmg": "https://x/intel",
        "UniversalAudioStudio_2.1.1_update.zip": "https://x/zip",
        "mysetup211.exe": "https://x/exe",
    }

    def _release(self, version="2.1.1"):
        return {"version": version,
                "download_url": self.ASSETS["UniversalAudioStudio_2.1.1_update.zip"],
                "assets": dict(self.ASSETS)}

    def test_apple_silicon_gets_the_arm64_image(self):
        self.assertEqual(updater.macos_download_url(self._release(), "arm64"),
                         "https://x/arm64")

    def test_intel_gets_the_x86_64_image(self):
        self.assertEqual(updater.macos_download_url(self._release(), "x86_64"),
                         "https://x/intel")

    def test_architecture_aliases_are_understood(self):
        for alias in ("ARM64", "aarch64", " arm64 "):
            self.assertEqual(updater.macos_arch(alias), "arm64", alias)
        for alias in ("x86_64", "AMD64"):
            self.assertEqual(updater.macos_arch(alias), "x86_64", alias)

    def test_an_unknown_architecture_gets_no_download(self):
        """Better to send them to the Release page than the wrong binary."""
        self.assertIsNone(updater.macos_arch("mips"))
        self.assertIsNone(updater.macos_download_url(self._release(), "mips"))

    def test_an_image_from_another_version_is_never_offered(self):
        release = {"version": "2.2.0", "download_url": None,
                   "assets": {"UniversalAudioStudio-2.1.1-arm64.dmg": "https://x/old"}}
        self.assertIsNone(updater.macos_download_url(release, "arm64"))

    def test_a_release_with_no_disk_image(self):
        release = {"version": "2.1.1", "download_url": "https://x/zip", "assets": {}}
        self.assertIsNone(updater.macos_download_url(release, "arm64"))

    def test_a_missing_release_is_survivable(self):
        for release in (None, {}, {"version": ""}):
            self.assertIsNone(updater.macos_download_url(release, "arm64"))

    def test_there_is_always_a_fallback_page(self):
        self.assertIn("github.com", updater.RELEASES_PAGE)
        self.assertTrue(updater.RELEASES_PAGE.endswith("/releases/latest"))


class RemoteReleaseTests(unittest.TestCase):
    """get_remote_release() feeds the macOS path; Windows uses the ZIP only."""

    def test_exposes_every_asset_by_name(self):
        with mock.patch.object(updater, "_request_json",
                               return_value=_release_payload()):
            release = updater.get_remote_release()
        self.assertEqual(release["version"], "2.1.0")
        self.assertIn("UniversalAudioStudio-2.1.0-arm64.dmg", release["assets"])
        self.assertIn("UniversalAudioStudio-2.1.0-x86_64.dmg", release["assets"])
        # The self-update payload is chosen by the asset's *name* (the fixture's
        # URLs are deliberately not the real filenames), which is what has to
        # keep the suffix.
        self.assertEqual(release["download_url"], "https://example.invalid/update.zip")
        self.assertTrue(_UPDATE_ASSET.endswith(updater.UPDATE_ASSET_SUFFIX))

    def test_a_release_without_the_zip_still_yields_a_version(self):
        """Windows cannot use it; a Mac only needs the version and a disk image."""
        payload = _release_payload(assets=[
            {"name": "UniversalAudioStudio-2.1.0-arm64.dmg",
             "browser_download_url": "https://example.invalid/arm64.dmg"},
        ])
        with mock.patch.object(updater, "_request_json", return_value=payload):
            release = updater.get_remote_release()
            self.assertEqual(release["version"], "2.1.0")
            self.assertIsNone(release["download_url"])
            self.assertIsNone(updater.get_remote_update_info())

    def test_returns_none_instead_of_raising(self):
        for payload in (None, [], "html", 7, {}, {"tag_name": ""}):
            with self.subTest(payload=payload):
                with mock.patch.object(updater, "_request_json",
                                       return_value=payload):
                    self.assertIsNone(updater.get_remote_release())

    def test_malformed_assets_are_skipped(self):
        payload = _release_payload(assets=[
            "not-a-dict",
            {"name": "", "browser_download_url": "https://x"},
            {"name": "no-url.dmg"},
            {"name": "good_2.1.0_update.zip", "browser_download_url": "https://x/ok"},
        ])
        with mock.patch.object(updater, "_request_json", return_value=payload):
            release = updater.get_remote_release()
        self.assertEqual(release["assets"], {"good_2.1.0_update.zip": "https://x/ok"})
        self.assertEqual(release["download_url"], "https://x/ok")


class MacUiWiringTests(unittest.TestCase):
    """Guards on ui.py, which needs a live Tk window to exercise properly."""

    def test_the_dead_end_message_is_gone(self):
        self.assertNotIn(
            "App self-update is only available in the Windows build",
            _read("ui.py"),
            "the macOS button must report updates now, not just explain itself",
        )

    def test_the_mac_flow_exists_and_opens_the_download(self):
        source = _read("ui.py")
        self.assertIn("_check_macos_update", source)
        self.assertIn("_after_macos_update_check", source)
        self.assertIn("_offer_macos_download", source)
        self.assertIn("webbrowser.open", source)

    def test_the_startup_prompt_knows_the_platform(self):
        source = _read("ui.py")
        self.assertIn("def _offer_startup_update(self, local_ver, release)", source)
        self.assertIn("updater.macos_download_url", source)
        self.assertIn("updater.RELEASES_PAGE", source)


class FallbackToAnOlderReleaseTests(unittest.TestCase):
    """A Release exists before its files do.

    Publishing creates the Release immediately; the build attaches the files
    minutes later. When that build fails, the newest Release stays empty for good
    - which is what happened to v2.1.1, after which every installed copy reported
    "no update information available" even though 2.1.0 was perfectly usable.
    """

    EMPTY_LATEST = {"tag_name": "v2.1.1", "assets": []}

    def _fake_requests(self, latest, listing):
        def fake(url, timeout=15):
            return listing if url == updater.GITHUB_RELEASES_LIST_API else latest
        return fake

    def test_falls_back_to_the_newest_release_that_has_the_package(self):
        with mock.patch.object(
                updater, "_request_json",
                side_effect=self._fake_requests(
                    self.EMPTY_LATEST,
                    [self.EMPTY_LATEST, _release_payload(tag="v2.1.0")])):
            info = updater.get_remote_update_info()
        self.assertEqual(info["version"], "2.1.0")
        self.assertEqual(info["download_url"], "https://example.invalid/update.zip")

    def test_a_usable_newest_release_needs_no_second_request(self):
        """The fallback must not add a request to the normal happy path."""
        with mock.patch.object(updater, "_request_json",
                               return_value=_release_payload()) as request:
            updater.get_remote_update_info()
        self.assertEqual(request.call_count, 1)

    def test_pre_releases_are_not_used_as_a_fallback(self):
        """`releases/latest` skips them, so the fallback must agree."""
        with mock.patch.object(
                updater, "_request_json",
                side_effect=self._fake_requests(
                    self.EMPTY_LATEST,
                    [{"tag_name": "v2.2.0", "prerelease": True,
                      "assets": [{"name": "UniversalAudioStudio_2.2.0_update.zip",
                                  "browser_download_url": "https://x/pre"}]}])):
            self.assertIsNone(updater.get_remote_update_info())

    def test_nothing_usable_anywhere_is_reported_as_such(self):
        with mock.patch.object(
                updater, "_request_json",
                side_effect=self._fake_requests(
                    self.EMPTY_LATEST, [self.EMPTY_LATEST, "junk"])):
            self.assertIsNone(updater.get_remote_update_info())

    def test_the_macos_path_still_accepts_an_empty_release(self):
        """A Mac only needs the version, so it must not pay for a fallback."""
        with mock.patch.object(updater, "_request_json",
                               return_value=self.EMPTY_LATEST) as request:
            release = updater.get_remote_release()
        self.assertEqual(release["version"], "2.1.1")
        self.assertIsNone(release["download_url"])
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()


