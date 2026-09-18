#!/usr/bin/env python3
"""Check that every pin in requirements.txt has a wheel for the macOS runner.

Why this exists: the first GitHub Actions run failed with an opaque
"Process completed with exit code 2." from ``bash tools/build_macos.sh`` after
only ~45 seconds. The cause was a Python-version mismatch - ``requirements.txt``
was pinned from a CPython 3.14 environment while the workflow asked for 3.12, so
pip could not resolve the versions and aborted before downloading anything.
pip reports that as a wall of resolver output, which looks like a build bug
rather than a pinning mistake.

This script checks the same thing up front and exits non-zero with a short,
readable list, so a mismatch fails fast and obviously.

Usage:
    python tools/check_mac_wheels.py                # arm64, Python 3.14
    python tools/check_mac_wheels.py --python 3.12  # reproduce an older pin set
    python tools/check_mac_wheels.py --arch x86_64  # Intel runners (macos-13)

Exit status: 0 = every pin installable, 1 = at least one problem.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_REQUIREMENTS = os.path.join(os.path.dirname(_HERE), "requirements.txt")

_CP_TAG = re.compile(r"-cp(\d)(\d+)-")
_PIN = re.compile(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;#]+)")
_CLAUSE = re.compile(r"(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+)*)")


def parse_pins(path):
    """Return [(name, version)] for the pinned ``name==version`` lines."""
    pins = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if not line:
                continue
            match = _PIN.match(line)
            if match:
                pins.append((match.group(1), match.group(2)))
    return pins


def _target_tuple(python_version):
    parts = python_version.split(".")
    return (int(parts[0]), int(parts[1]))


def _split_tags(filename):
    """Return (python, abi, platform) for a wheel filename, or None.

    The last three dash-separated fields of a wheel name are always the tags,
    e.g. numpy-2.5.0-cp314-cp314-macosx_14_0_arm64.whl
         pyinstaller-6.22.2-py3-none-macosx_10_13_universal2.whl
    """
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    return parts[-3], parts[-2], parts[-1]


def _python_tag_ok(python_tag, abi_tag, target):
    """True when a wheel's python + abi tags run on *target*.

    This deliberately inspects the abi tag. Accepting any ``cp3X`` at or below
    the target is only correct for the stable ABI; a plain ``cp310-cp310`` wheel
    is built against CPython 3.10's ABI and is NOT installable on 3.14. Ignoring
    the abi tag produced false passes - the checker once claimed pedalboard had
    a macOS x86_64 wheel for 3.14 when such a wheel does not exist.
    """
    if python_tag in ("py3", "py2.py3"):
        return True
    found = re.match(r"^cp3(\d+)(t?)$", python_tag)
    if not found:
        return False
    # Free-threaded wheels are tagged cp3XXt; the marker normally sits in the
    # abi tag (cp314-cp314t) but may appear in the python tag too. Either way
    # they need a free-threaded interpreter, which this build does not use.
    if found.group(2) == "t" or re.match(r"^cp3\d+t$", abi_tag):
        return False
    minor = int(found.group(1))
    if abi_tag == "abi3":
        # Stable ABI: usable by this or any newer CPython 3.
        return (3, minor) <= target
    # Otherwise the wheel is pegged to exactly one CPython version, and the abi
    # tag has to agree with the python tag.
    abi_found = re.match(r"^cp3(\d+)$", abi_tag)
    if abi_found and int(abi_found.group(1)) != minor:
        return False
    return (3, minor) == target


def wheel_supports(filename, target, arch):
    """True when *filename* can be installed on (target, arch)."""
    tags = _split_tags(filename)
    if tags is None:
        return False
    python_tag, abi_tag, platform_tag = tags
    if platform_tag == "any":
        return _python_tag_ok(python_tag, abi_tag, target)
    if not platform_tag.startswith("macosx"):
        return False
    if not (arch in platform_tag or "universal2" in platform_tag):
        return False
    return _python_tag_ok(python_tag, abi_tag, target)


def requires_python_ok(spec, target):
    """Evaluate a ``Requires-Python`` specifier against *target*.

    pip refuses a version whose metadata excludes the running interpreter even
    when a matching wheel exists, so this has to be checked too.
    """
    if not spec:
        return True
    for operator, version in _CLAUSE.findall(spec):
        parts = [int(p) for p in version.split(".")]
        while len(parts) < 2:
            parts.append(0)
        ref = (parts[0], parts[1])
        if operator == ">=" and not target >= ref:
            return False
        if operator == ">" and not target > ref:
            return False
        if operator == "<=" and not target <= ref:
            return False
        if operator == "<" and not target < ref:
            return False
        if operator == "==" and target != ref:
            return False
        if operator == "!=" and target == ref:
            return False
        if operator == "~=":
            # ~=3.9 means ">=3.9, <4"; ~=3.9.1 means ">=3.9.1, <3.10".
            if not target >= ref:
                return False
            if len(parts) == 2:
                if target[0] != parts[0]:
                    return False
            elif target[:2] != (parts[0], parts[1]):
                return False
    return True


def fetch_release(name, version):
    """Return PyPI's json for one release, or None when it does not exist."""
    url = "https://pypi.org/pypi/%s/%s/json" % (name, version)
    request = urllib.request.Request(
        url, headers={"User-Agent": "uas-wheel-check"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except urllib.error.URLError as exc:
        print("  !! network error for %s: %s" % (name, exc.reason))
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="3.14",
                        help="target Python version, e.g. 3.14 (default: 3.14)")
    parser.add_argument("--arch", default="arm64",
                        choices=("arm64", "x86_64"),
                        help="runner CPU (default: arm64)")
    parser.add_argument("--requirements", default=_REQUIREMENTS)
    args = parser.parse_args(argv)

    target = _target_tuple(args.python)
    pins = parse_pins(args.requirements)
    print("Checking %d pins for macOS %s on Python %s\n"
          % (len(pins), args.arch, args.python))

    problems = []
    width = max(len(name) for name, _ in pins)
    for name, version in pins:
        release = fetch_release(name, version)
        if release is None:
            problems.append("%s==%s does not exist on PyPI" % (name, version))
            print("  %-*s %-10s NO SUCH VERSION" % (width, name, version))
            continue
        spec = (release.get("info") or {}).get("requires_python")
        if not requires_python_ok(spec, target):
            problems.append("%s==%s requires Python %s, but the build uses %s"
                            % (name, version, spec, args.python))
            print("  %-*s %-10s NEEDS PYTHON %s"
                  % (width, name, version, spec))
            continue
        wheels = [f["filename"] for f in release.get("urls", [])
                  if f["filename"].endswith(".whl")]
        good = [w for w in wheels if wheel_supports(w, target, args.arch)]
        if good:
            print("  %-*s %-10s ok" % (width, name, version))
        elif wheels:
            problems.append(
                "%s==%s has wheels, but none for macOS %s/Python %s"
                % (name, version, args.arch, args.python)
            )
            print("  %-*s %-10s NO COMPATIBLE WHEEL (%d available)"
                  % (width, name, version, len(wheels)))
        else:
            problems.append("%s==%s ships no wheels (sdist only)" % (name, version))
            print("  %-*s %-10s NO WHEELS AT ALL" % (width, name, version))

    print()
    if problems:
        print("FAILED - %d pin(s) cannot be installed:\n" % len(problems))
        for problem in problems:
            print("  * " + problem)
        print("\nFix requirements.txt (or the workflow's python-version) so the"
              "\npins match the interpreter the build actually uses.")
        return 1
    print("All pins have a compatible wheel for macOS %s on Python %s."
          % (args.arch, args.python))
    return 0


if __name__ == "__main__":
    sys.exit(main())