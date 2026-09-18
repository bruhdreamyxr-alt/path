#!/usr/bin/env python3
"""Print ``__version__`` from version.py.

Exists so tools/build_macos.sh does not need a here-document inside a command
substitution to read the version:

    VERSION="$(python - <<'PY'
    ...
    PY
    )"

macOS still ships bash 3.2 as ``/bin/bash`` (including the GitHub macOS
runners, which report "Bash 3.2.57(1)-release"), and bash 3.2 cannot parse a
here-document nested inside ``$( )``. It reads to EOF looking for the matching
``)`` and aborts with:

    tools/build_macos.sh: line 57: unexpected EOF while looking for matching `)'

That is a *parse* error, so bash exits with status 2 - the step fails with a
bare "Process completed with exit code 2" and no clue which line was at fault.

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