"""Tests for the Windows build tooling.

Kept from the macOS-era version of this file: ``tools/get_version.py`` is what
names the release artifacts, and ``build_windows.ps1`` calls it exactly as
``test_runs_as_a_script_exactly_as_the_build_calls_it`` asserts - so a drift
between ``version.py`` and the shipped filenames would otherwise only surface
as a Release called 2.1.0 containing files called 2.0.0.

The macOS wheel checker and the bash-3.2 portability tests went away with the
Mac build (see the removal commit); there is no longer a second platform whose
build could drift.
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

import get_version  # noqa: E402  # type: ignore[reportMissingImports]

_VERSION_PATTERN = r'__version__\s*=\s*["\']([^"\']+)'


class VersionHelperTests(unittest.TestCase):
    """tools/get_version.py - names every release artifact."""

    def test_matches_version_py(self):
        with open(os.path.join(PROJECT_DIR, "version.py"), encoding="utf-8") as fh:
            match = re.search(_VERSION_PATTERN, fh.read())
            assert match is not None  # version.py always carries __version__
            expected = match.group(1)
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


if __name__ == "__main__":
    unittest.main()