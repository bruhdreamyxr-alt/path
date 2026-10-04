#!/usr/bin/env python3
"""Print ``__version__`` from version.py.

Used by both build entry points - tools/build_windows.ps1 and the release
workflow's tag check - so a Release can never be published under one version
while its artifacts are named another.

It is a script rather than an inline read because the version has to be read
from two different shells (PowerShell and bash) without either of them
re-implementing the parsing.

Usage:
    python tools/get_version.py            # reads version.py next to the repo
    python tools/get_version.py path.py    # or an explicit file
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT = os.path.join(os.path.dirname(_HERE), "version.py")
_FALLBACK = "0.0.0"
_PATTERN = re.compile(r'__version__\s*=\s*["\']([^"\']+)')


def get_version(path=_DEFAULT):
    """Return the version string in *path*, or ``0.0.0`` if it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            found = _PATTERN.search(handle.read())
        if found:
            return found.group(1)
    except OSError:
        pass
    return _FALLBACK


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    print(get_version(argv[0] if argv else _DEFAULT))
    return 0


if __name__ == "__main__":
    sys.exit(main())