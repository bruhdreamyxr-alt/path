"""Tests for the build tooling the macOS cloud build depends on.

Both things pinned here have already broken a real run:

* ``tools/get_version.py`` replaced a here-document nested inside a command
  substitution in ``tools/build_macos.sh``. macOS ships bash 3.2 as
  ``/bin/bash`` (the GitHub macOS runners report "Bash 3.2.57(1)-release") and
  bash 3.2 cannot parse that nesting - it reads to EOF looking for the matching
  ``)`` and aborts with ``unexpected EOF while looking for matching `)'`` and
  exit status 2.
* ``tools/check_mac_wheels.py`` decides whether a pinned version is installable
  on macOS. Its first version wrongly rejected pyinstaller, whose wheel is
  tagged ``py3-none-macosx_10_13_universal2`` instead of ``cp3XX``.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(PROJECT_DIR, "tools")
sys.path.insert(0, TOOLS_DIR)

import check_mac_wheels  # noqa: E402
import get_version  # noqa: E402

_SHELL_SCRIPTS = ("tools/build_macos.sh", "tools/fetch_mac_helpers.sh")
_VERSION_PATTERN = r'__version__\s*=\s*["\']([^"\']+)'


class VersionHelperTests(unittest.TestCase):
    """tools/get_version.py - read by build_macos.sh to name the .dmg."""

    def test_matches_version_py(self):
        with open(os.path.join(PROJECT_DIR, "version.py"), encoding="utf-8") as fh:
            expected = re.search(_VERSION_PATTERN, fh.read()).group(1)
        self.assertEqual(get_version.get_version(), expected)
        self.assertRegex(expected, r"^\d+\.\d+\.\d+$")

    def test_unreadable_file_falls_back(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-version-file.py")
        self.assertEqual(get_version.get_version(missing), "0.0.0")

    def test_accepts_single_quoted_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "version.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("__version__ = '9.9.9'\n")
            self.assertEqual(get_version.get_version(path), "9.9.9")

    def test_runs_as_a_script_exactly_as_the_build_calls_it(self):
        completed = subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "get_version.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=PROJECT_DIR,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(completed.stdout.strip(), r"^\d+\.\d+\.\d+$")


class ShellScriptPortabilityTests(unittest.TestCase):
    """The build scripts have to run under macOS's bash 3.2."""

    def _read(self, relative):
        with open(os.path.join(PROJECT_DIR, relative), encoding="utf-8") as fh:
            return fh.read()

    def test_no_here_documents(self):
        """A heredoc is fine at the top level, but bash 3.2 cannot parse one
        nested inside "$( )". Banning them outright keeps the scripts safe."""
        for relative in _SHELL_SCRIPTS:
            self.assertNotIn(
                "<<", self._read(relative),
                "%s contains a here-document; bash 3.2 (macOS /bin/bash) "
                "cannot parse one inside a command substitution" % relative,
            )

    def test_gitattributes_keeps_shell_scripts_lf(self):
        """CRLF makes bash fail with `$'\\r': command not found`."""
        self.assertRegex(self._read(".gitattributes"), r"\*\.sh\s+text\s+eol=lf")

    def test_build_script_uses_the_version_helper(self):
        self.assertIn("tools/get_version.py", self._read("tools/build_macos.sh"))


class WheelSupportTests(unittest.TestCase):
    """tools/check_mac_wheels.py must not give false passes or false alarms."""

    def test_accepts_pure_python_wheel(self):
        self.assertTrue(check_mac_wheels.wheel_supports(
            "mutagen-1.48.1-py3-none-any.whl", (3, 12), "arm64"))

    def test_accepts_py3_tagged_platform_wheel(self):
        """The pyinstaller shape the first version of the checker rejected."""
        self.assertTrue(check_mac_wheels.wheel_supports(
            "pyinstaller-6.22.2-py3-none-macosx_10_13_universal2.whl",
            (3, 12), "arm64"))
        self.assertTrue(check_mac_wheels.wheel_supports(
            "soundfile-0.14.0-py2.py3-none-macosx_11_0_arm64.whl",
            (3, 12), "arm64"))

    def test_accepts_exact_and_abi3_cp_tags(self):
        self.assertTrue(check_mac_wheels.wheel_supports(
            "numpy-2.5.0-cp312-cp312-macosx_11_0_arm64.whl", (3, 12), "arm64"))
        self.assertTrue(check_mac_wheels.wheel_supports(
            "curl_cffi-0.15.0-cp310-abi3-macosx_11_0_arm64.whl", (3, 12), "arm64"))

    def test_rejects_newer_cp_tag(self):
        self.assertFalse(check_mac_wheels.wheel_supports(
            "numpy-2.5.0-cp314-cp314-macosx_11_0_arm64.whl", (3, 12), "arm64"))

    def test_rejects_wrong_platform_or_arch(self):
        for name in (
            "numpy-2.5.0-cp312-cp312-win_amd64.whl",
            "numpy-2.5.0-cp312-cp312-manylinux_2_17_aarch64.whl",
            "numpy-2.5.0-cp312-cp312-macosx_10_13_x86_64.whl",  # Intel-only
        ):
            self.assertFalse(
                check_mac_wheels.wheel_supports(name, (3, 12), "arm64"), name)

    def test_x86_64_runner_accepts_universal2(self):
        self.assertTrue(check_mac_wheels.wheel_supports(
            "pyinstaller-6.22.2-py3-none-macosx_10_13_universal2.whl",
            (3, 12), "x86_64"))


class RequiresPythonTests(unittest.TestCase):
    """A version can be excluded by metadata even when a wheel exists."""

    def test_absent_spec_allows_everything(self):
        for spec in (None, ""):
            self.assertTrue(check_mac_wheels.requires_python_ok(spec, (3, 12)))

    def test_minimum_version(self):
        self.assertTrue(check_mac_wheels.requires_python_ok(">=3.10", (3, 12)))
        self.assertFalse(check_mac_wheels.requires_python_ok(">=3.13", (3, 12)))

    def test_range(self):
        self.assertTrue(check_mac_wheels.requires_python_ok(">=3.10,<4", (3, 12)))
        self.assertFalse(
            check_mac_wheels.requires_python_ok(">=3.9,<3.12", (3, 12)))

    def test_compatible_release_operator(self):
        self.assertTrue(check_mac_wheels.requires_python_ok("~=3.9", (3, 12)))
        self.assertFalse(check_mac_wheels.requires_python_ok("~=3.9.1", (3, 12)))


if __name__ == "__main__":
    unittest.main()