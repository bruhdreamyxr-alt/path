#!/usr/bin/env python3
"""Write the legacy update.json that points at this release's self-update ZIP.

This file exists for exactly one release: the one that moves everybody off
Google Drive.

Copies of 2.0.0 and earlier fetch a manifest from a fixed Google Drive file
(id 1XEnyAh2Vq_CRK0_SBjJSrCcXjDQCfPp6) and download whichever ZIP it names. That
code is already inside those copies, so no new release can reach them - the *old*
mechanism has to be used one last time to hand them a build that knows about
GitHub.

What makes this cheap: the manifest's ``download_url`` may point anywhere, and a
GitHub release asset is a plain HTTPS URL that the old code follows without
complaint. So this writes a manifest naming the ZIP attached to the GitHub
Release, the old app downloads the new build from GitHub, and the only thing
ever uploaded to Drive is a ~120 byte JSON file. Drive is then abandoned.

The version must be strictly greater than the version already installed
(everything out there is 2.0.0), or the old app decides it is up to date and the
hop never happens.

Usage:
    python tools/make_update_manifest.py \
        --release-dir release-files --repo owner/name --tag v2.1.0

    # print it without writing anything
    python tools/make_update_manifest.py ... --print
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from get_version import get_version  # noqa: E402  (same directory)

MANIFEST_FILENAME = "update.json"


def find_update_zip(release_dir):
    """Return the path of the single ``*_update.zip`` in *release_dir*.

    The filename is taken from the actual file rather than rebuilt from the
    version, so it cannot drift from what _make_update_package.py produced.
    """
    matches = sorted(glob.glob(os.path.join(release_dir, "*_update.zip")))
    if not matches:
        raise SystemExit(
            "ERROR: no '*_update.zip' in %s - is the build ZIP missing from the "
            "release?" % release_dir)
    if len(matches) > 1:
        raise SystemExit(
            "ERROR: %d '*_update.zip' files in %s (%s); expected one."
            % (len(matches), release_dir, ", ".join(os.path.basename(m) for m in matches)))
    return matches[0]


def build_manifest(version, repo, tag, asset_name):
    """Return the legacy manifest dict for a release asset."""
    if tag != "v" + version:
        # A wrong tag makes the URL 404, and the old app simply never updates -
        # a silent failure that would be very hard to diagnose later.
        raise SystemExit(
            "ERROR: tag %r does not match version %r (expected %r). The download "
            "URL is built from both." % (tag, version, "v" + version))
    return {
        "version": version,
        "download_url": "https://github.com/%s/releases/download/%s/%s"
                        % (repo, tag, asset_name),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Write the legacy Google Drive update manifest.")
    parser.add_argument("--release-dir", default="release-files",
                        help="directory holding the downloaded release files")
    parser.add_argument("--repo", required=True,
                        help="GitHub repository as owner/name")
    parser.add_argument("--tag", required=True,
                        help="the release tag, e.g. v2.1.0")
    parser.add_argument("--version", default=None,
                        help="override the version (default: version.py)")
    parser.add_argument("--out", default=None,
                        help="output path (default: <release-dir>/update.json)")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the manifest without writing a file")
    args = parser.parse_args(argv)

    version = args.version or get_version()
    asset_name = os.path.basename(find_update_zip(args.release_dir))
    manifest = build_manifest(version, args.repo, args.tag, asset_name)
    text = json.dumps(manifest, indent=4) + "\n"

    if args.print_only:
        print(text, end="")
        return 0

    out_path = args.out or os.path.join(args.release_dir, MANIFEST_FILENAME)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("wrote %s" % out_path)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
