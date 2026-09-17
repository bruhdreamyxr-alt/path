"""Self-update orchestrator for Universal Audio Studio.

Checks a remote ``update.json`` manifest, compares versions against the one
in :mod:`version`, downloads the new EXE if available, and launches a small
updater subprocess that replaces the running EXE while the main app exits.
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

from version import __version__ as CURRENT_VERSION

logger = logging.getLogger("universal_audio_studio.updater")

# ---------------------------------------------------------------------------
# UPDATE MANIFEST URL - the ONLY thing you have to host yourself.
#
# Google Drive setup (files live in folder "106mUGyqs8MTFMhY76eBh_TcrAlLtkKaO"):
#   update.json  ->  https://drive.google.com/file/d/1XEnyAh2Vq_CRK0_SBjJSrCcXjDQCfPp6/view
#   app EXE      ->  https://drive.google.com/file/d/12FXG2PP8x5R3ljAmesLehcBaH6QZABnZ/view
#
# The direct-download form below (uc?export=download&id=<FILE_ID>) is what
# Drive serves to scripts. update.json holds the ZIP file ID in its
# "download_url" field (see update.json in the repo).
#
# To change where updates come from, just replace the ID below with another
# Drive file ID (or put a plain https:// URL for a normal web server).
# ---------------------------------------------------------------------------
UPDATE_MANIFEST_URL = "https://drive.google.com/uc?export=download&id=1XEnyAh2Vq_CRK0_SBjJSrCcXjDQCfPp6"

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


def _request_url(url: str, timeout: int = 15, _recursing: bool = False):
    """GET *url* and return a response, or ``None`` on failure.

    Handles Google Drive's interstitial pages ("can't scan this file", large
    file warnings) by following the embedded download form — usually at
    ``drive.usercontent.google.com/download`` — with its hidden inputs
    (``id``/``export``/``confirm``/``uuid``), exactly like a browser does.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        ctype = resp.headers.get("Content-Type", "").lower()
        if "text/html" in ctype:
            html = resp.read(300_000).decode("utf-8", "replace")
            resp.close()
            if _recursing:
                # Already followed the form once and still got HTML.
                return None
            next_url = _drive_confirm_url(html, url)
            if not next_url:
                return None
            return _request_url(next_url, timeout=timeout, _recursing=True)
        return resp
    except Exception as e:
        logger.debug("Request failed: %s", e)
        return None


def _drive_confirm_url(html: str, original_url: str):
    """Extract the real download URL from a Google Drive interstitial page.

    Returns the follow-up URL (with confirmation/``uuid`` params applied) or
    ``None`` if the page isn't a form we can use.
    """
    from urllib.parse import urlencode
    action = re.search(r"<form[^>]*action=\"([^\"]+)\"", html)
    fields = {
        k: v.replace("&amp;", "&")
        for k, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html)
    }
    if not action or not fields.get("id", ""):
        # Legacy fallback: drive.google.com/uc?...&confirm=<token>
        confirm = fields.get("confirm")
        if confirm:
            sep = "&" if "?" in original_url else "?"
            return f"{original_url}{sep}confirm={confirm}"
        return None
    action_url = action.group(1).replace("&amp;", "&")
    if action_url.startswith("/"):
        parsed = original_url.split("/")
        action_url = f"{parsed[0]}//{parsed[2]}{action_url}"
    return action_url + "?" + urlencode(fields)


def is_newer_version(remote_version):
    """Return True if *remote_version* is newer than the local version."""
    return _parse_version(remote_version) > _parse_version(CURRENT_VERSION)


def get_remote_update_info():
    """Fetch update info from the remote manifest URL.

    Returns a dict with ``version``/``download_url`` on success, or ``None``
    on any error (network failure, bad JSON, empty response).
    """
    resp = _request_url(UPDATE_MANIFEST_URL)
    if resp is None:
        return None
    try:
        data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("Failed to parse update manifest: %s", e)
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass
    version = str(data.get("version", "")).strip()
    download_url = str(data.get("download_url", "")).strip()
    if version and download_url:
        return {"version": version, "download_url": download_url}
    logger.warning("Manifest missing version/download_url: %s", data)
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