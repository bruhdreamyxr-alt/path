"""Self-update orchestrator for Universal Audio Studio.

Checks the newest published GitHub Release, compares its version against the one
in :mod:`version`, downloads the new ZIP if available, and launches a small
updater subprocess that replaces the running EXE while the main app exits.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request

from version import __version__ as CURRENT_VERSION

logger = logging.getLogger("universal_audio_studio.updater")

# ---------------------------------------------------------------------------
# UPDATE SOURCE - GitHub Releases, on the public repository whose Actions build
# these artifacts.
#
# Nothing is hosted by hand any more. Pushing a tag such as `v2.1.0` makes the
# release workflow build every platform and attach the assets; this module then
# reads them back from the public Releases API, which needs no token:
#
#     GET https://api.github.com/repos/<owner>/<repo>/releases/latest
#       -> "tag_name" -> the version to compare against
#       -> "assets"   -> the one ending in "_update.zip" is the self-update payload
#
# Replaces an earlier Google Drive manifest (update.json) that had to be edited
# and re-uploaded by hand for every release, and whose file IDs had to be kept
# in sync with this file.
#
# To point updates at a different repository, change GITHUB_REPO below.
# ---------------------------------------------------------------------------
GITHUB_REPO = "bruhdreamyxr-alt/path"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# The self-update payload is the ZIP produced by _make_update_package.py, named
# UniversalAudioStudio_<version>_update.zip. Matching on the suffix rather than
# a full filename means a version bump cannot break the update check.
UPDATE_ASSET_SUFFIX = "_update.zip"

# How many seconds to wait for the main process to exit before the updater
# forcibly proceeds with the replacement.
EXIT_GRACE_SECONDS = 5


def _parse_version(version_str):
    """Return a tuple of integers from a dotted version string."""
    parts = []
    for chunk in str(version_str).strip().lstrip("v").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            # Strip any trailing non-digits (e.g. "1.0.0-beta")
            digits = ""
            for c in chunk:
                if c.isdigit():
                    digits += c
                else:
                    break
            try:
                parts.append(int(digits))
            except ValueError:
                parts.append(0)
    return tuple(parts)


def is_frozen() -> bool:
    """Return True when running from a PyInstaller bundle (not from source)."""
    return bool(getattr(sys, "frozen", False))


def _request_url(url: str, timeout: int = 15):
    """GET *url* and return a response, or ``None`` on failure.

    Redirects are followed automatically, which is what a GitHub release-asset
    link requires: ``github.com/.../releases/download/<tag>/<file>`` answers
    with a 302 to an ``objects.githubusercontent.com`` address.

    An HTML body means we did not reach the file we asked for (a proxy block
    page, or an error document). That case used to be handled by following
    Google Drive's "can't scan this file for viruses" interstitial form; updates
    come from GitHub Releases now, so HTML is simply a failure.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        if "text/html" in resp.headers.get("Content-Type", "").lower():
            resp.close()
            logger.warning("Expected a file but received an HTML page: %s", url)
            return None
        return resp
    except Exception as e:
        logger.debug("Request failed: %s", e)
        return None


def _request_json(url: str, timeout: int = 15):
    """GET *url* and return the decoded JSON, or ``None`` on any failure."""
    resp = _request_url(url, timeout=timeout)
    if resp is None:
        return None
    try:
        return json.loads(resp.read(2_000_000).decode("utf-8", "replace"))
    except Exception as e:
        logger.debug("Failed to parse JSON from %s: %s", url, e)
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass


def is_newer_version(remote_version):
    """Return True if *remote_version* is newer than the local version."""
    return _parse_version(remote_version) > _parse_version(CURRENT_VERSION)


def _release_version(tag_name):
    """Turn a release tag into a plain version: ``"v2.1.0"`` -> ``"2.1.0"``.

    ``_parse_version`` would strip the ``v`` anyway, but the value is also shown
    to the user ("Latest: 2.1.0"), so normalise it once here.
    """
    return str(tag_name or "").strip().lstrip("v").strip()


def _pick_update_asset(assets, suffix=UPDATE_ASSET_SUFFIX):
    """Return the download URL of the first asset whose name ends with *suffix*.

    A Release also carries the two .dmg files and the installer, so the suffix
    is what selects the self-update ZIP from among them.
    """
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).lower()
        url = str(asset.get("browser_download_url", "")).strip()
        if name.endswith(suffix) and url:
            return url
    return None


def get_remote_update_info():
    """Fetch the newest published Release from the GitHub API.

    Returns a dict with ``version``/``download_url`` on success, or ``None`` on
    any error: offline, rate limited, no release published yet, or a release
    with no self-update ZIP attached (a *draft* release has no downloadable
    assets, which is another reason releases must be published).

    This is the unauthenticated public endpoint, so no token ships inside the
    app. It allows 60 requests per hour per address, far more than an app that
    checks once at startup needs.
    """
    data = _request_json(GITHUB_RELEASES_API)
    if not isinstance(data, dict):
        logger.warning("Could not read releases from %s", GITHUB_RELEASES_API)
        return None

    version = _release_version(data.get("tag_name"))
    download_url = _pick_update_asset(data.get("assets"))
    if version and download_url:
        return {"version": version, "download_url": download_url}

    logger.warning(
        "Release %s has no '%s' asset, so there is nothing to update to.",
        data.get("tag_name") or "(unknown tag)", UPDATE_ASSET_SUFFIX,
    )
    return None


def download_update(download_url, dest_path=None):
    """Download the new EXE to a temp file; return the path or ``None``.

    Refuses to run (and returns ``None``) when not inside a frozen build, so
    ``python.exe`` can never be targeted for replacement while developing.
    """
    if not is_frozen():
        logger.warning("Self-update refused: running from source, nothing to replace.")
        return None
    if dest_path is None:
        dest_path = os.path.join(tempfile.gettempdir(), "UniversalAudioStudio_update.exe")
    resp = _request_url(download_url, timeout=60)
    if resp is None:
        logger.error("Download failed: could not reach %s", download_url)
        return None
    try:
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        logger.info("Downloaded update to %s", dest_path)
    except Exception as e:
        logger.error("Download failed: %s", e)
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return dest_path


def _get_updater_cli_path():
    """Return a path to ``updater_cli.exe`` that actually exists, if any.

    Search order: PyInstaller bundle dir, the folder next to the running EXE
    (plain + PyInstaller-6 ``_internal`` layout), then the project folder when
    running from source.
    """
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "updater_cli.exe"))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable or ""))
    if exe_dir:
        candidates.append(os.path.join(exe_dir, "updater_cli.exe"))
        candidates.append(os.path.join(exe_dir, "_internal", "updater_cli.exe"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "updater_cli.exe"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _dir_is_writable(directory: str) -> bool:
    """Return True when *directory* accepts file writes for this process."""
    probe = os.path.join(directory, "_update_probe.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        os.remove(probe)
        return True
    except OSError:
        return False


def _run_elevated(exe_path: str, args) -> bool:
    """Start *exe_path* with *args* behind a UAC elevation prompt.

    Returns True when the elevated process was actually started (the user
    approved the prompt); False on refusal or any error.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        params = subprocess.list2cmdline(list(args))
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe_path, params, None, 1  # SW_SHOWNORMAL
        )
        return bool(int(ret) > 32)
    except Exception as e:
        logger.error("Elevation launch failed: %s", e)
        return False


def self_update_supported() -> bool:
    """True when the in-place self-updater can work on this platform.

    The updater replaces a running ``.exe`` via ``updater_cli.exe`` (asking for
    UAC elevation when the install directory is read-only), so it is inherently
    Windows-only. macOS builds ship as a ``.app`` inside a ``.dmg`` and are
    updated by replacing the bundle, so the in-app updater is disabled there
    rather than downloading a Windows executable.
    """
    return os.name == "nt"


def launch_updater(old_exe_path, new_exe_path, parent_pid, extra_args=None):
    """Spawn the updater subprocess then return True.

    Refuses to run from source so ``python.exe`` can never be replaced.

    The updater waits for *parent_pid* to exit, installs ``new_exe_path``
    over the app directory, and optionally relaunches the app.

    When the install directory is not writable (e.g. ``C:\\Program Files``),
    the updater is started elevated so Windows shows the standard UAC prompt;
    returns False if the user declines it.
    """
    if not is_frozen():
        logger.warning("Refusing to launch updater when running from source.")
        return False
    if not self_update_supported():
        logger.info("Self-update is Windows-only; skipping on this platform.")
        return False
    updater_cli = _get_updater_cli_path()
    if not os.path.exists(updater_cli):
        logger.error("Updater CLI not found at %s", updater_cli)
        return False

    args = [old_exe_path, new_exe_path, str(parent_pid)]
    if extra_args:
        args.extend(extra_args)

    app_dir = os.path.dirname(os.path.abspath(old_exe_path))
    if _dir_is_writable(app_dir):
        try:
            subprocess.Popen([updater_cli, *args], close_fds=True)
            return True
        except Exception as e:
            logger.error("Failed to launch updater: %s", e)
            return False

    # Protected location (Program Files, ...): the updater needs admin rights.
    logger.info("Install dir not writable (%s); requesting elevation.", app_dir)
    if _run_elevated(updater_cli, args):
        return True
    logger.warning("Updater elevation declined or failed.")
    return False