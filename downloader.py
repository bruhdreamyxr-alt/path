import os
import shutil
import sys
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, cast

import threading

logger = logging.getLogger("universal_audio_studio.downloader")

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: Any) -> str:
    """Remove terminal ANSI control sequences from error strings."""
    if text is None:
        return ""
    text = str(text)
    return _ANSI_ESCAPE_RE.sub("", text)


def _clean_error_text(text: Any, fallback: str = "") -> str:
    """Best-effort sanitization of yt-dlp/CLI error output for UI display."""
    cleaned = _strip_ansi(text).strip()
    return cleaned if cleaned else fallback
    
def _is_aria2_failure(text: Any) -> bool:
    """Return whether an error indicates that aria2 itself failed."""
    cleaned = _clean_error_text(text).lower()
    return "aria2c exited with code" in cleaned or ("aria2c" in cleaned and "code 28" in cleaned)


def _no_window_kwargs() -> dict:
    """Subprocess kwargs that hide the console window on Windows only.

    ``creationflags`` is a Windows-only ``subprocess`` parameter: CPython's
    POSIX implementation raises ``ValueError("creationflags is only supported
    on Windows platforms")`` whenever it is non-zero. Passing CREATE_NO_WINDOW
    unconditionally would therefore crash every subprocess call -- downloads,
    yt-dlp.exe, ffmpeg, previews and studio export -- the moment the app runs
    on macOS or Linux, so the flag is applied only where it means something.
    """
    if os.name == "nt":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def open_in_file_manager(path: str) -> None:
    """Open *path* in the platform's file manager (Explorer/Finder/xdg)."""
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        logger.debug("Could not open folder %s: %s", path, e)


class DownloadCancelled(Exception):
    """Raised when the user cancels an in-flight download."""


# One download runs at a time (the UI serialises jobs). This event is checked
# by progress hooks and the yt-dlp.exe runner so a cancel request halts work
# mid-download instead of waiting for it to finish.
_ACTIVE_CANCEL = threading.Event()


def request_cancel_download() -> bool:
    """Request cancellation of the active download.

    Returns True when a request was registered (i.e. a download may be
    running), False when nothing needed cancelling.
    """
    if _ACTIVE_CANCEL.is_set():
        return False
    _ACTIVE_CANCEL.set()
    return True


def is_download_cancel_requested() -> bool:
    """True if the user asked the current download to stop."""
    return _ACTIVE_CANCEL.is_set()


def begin_download_session() -> None:
    """Reset any stale cancellation flag; call when a NEW download starts."""
    _ACTIVE_CANCEL.clear()
    # Also drop metadata left over from the previous download. Without this a
    # direct link (which resolves no metadata of its own) would inherit the last
    # search result's artist/title/artwork and tag the new file wrongly.
    _last_dl_metadata.clear()

try:
    import soundfile as sf
except Exception:
    sf = None

try:
    import mutagen
except Exception:
    mutagen = None

try:
    import yt_dlp
except Exception:
    yt_dlp = None

try:
    from PIL import Image
except Exception:
    Image = None

import io
import tempfile
import glob
import html
import json
import urllib.request
import urllib.parse
import subprocess

try:
    from pedalboard import Reverb
    from pedalboard.io import AudioFile
except Exception:
    Reverb = None
    AudioFile = None

# Pylance sometimes cannot resolve Pedalboard export from pedalboard.
# Fallback import keeps runtime behavior unchanged while improving type-checking.
try:
    from pedalboard import Pedalboard  # type: ignore
except Exception:  # pragma: no cover
    try:
        from pedalboard._pedalboard import Pedalboard  # type: ignore
    except Exception:
        Pedalboard = None

# Global performance configuration (modifiable from UI)
ARIA2_MAX_CONNECTIONS = 16  # aria2's hard limit for --max-connection-per-server (-x)

DEFAULT_PERF_CONFIG = {
    'use_aria2': True,
    'aria2_connections': 16,
    'concurrent_fragment_downloads': 8,
    'http_chunk_size': 1 << 20,
    'max_video_resolution': 'Best (up to 4K)',
}

VIDEO_RESOLUTION_OPTIONS = ['Best (up to 4K)', '1080p', '720p', '480p']
_VIDEO_RESOLUTION_MAP = {'1080p': '1080', '720p': '720', '480p': '480'}


def video_format_string(max_resolution: str = 'Best (up to 4K)') -> str:
    """Return a yt-dlp format selector capped at *max_resolution*.

    'Best (up to 4K)' keeps the existing unrestricted behavior. Otherwise we
    restrict the video stream to the chosen height (e.g. 1080p -> bestvideo[height<=1080]),
    which the After Effects re-encode step already downscales anyway, so it
    saves time and disk on large 4K downloads.
    """
    height = _VIDEO_RESOLUTION_MAP.get(max_resolution)
    if height is None or height == 'best':
        # No cap requested (or unrecognized value) — unrestricted best.
        return 'bestvideo*+bestaudio/best'
    return f'bestvideo[height<={height}]+bestaudio/best[height<={height}]/best'

performance_config = dict(DEFAULT_PERF_CONFIG)


def _clamp_aria2_connections(value) -> int:
    """Keep aria2 per-server concurrency inside aria2's supported range (1-16).

    Passing a larger value makes aria2 abort with exit code 28 ("We encountered a
    problem while processing the option...") which yt-dlp surfaces as:
    ERROR: aria2c exited with code 28
    """
    try:
        conn = int(value)
    except (TypeError, ValueError):
        conn = ARIA2_MAX_CONNECTIONS
    return max(1, min(conn, ARIA2_MAX_CONNECTIONS))


def perf_cfg_from_prefs(prefs: Optional[dict] = None) -> dict:
    """Build a sanitized perf config from saved prefs (or defaults).

    Single source of truth for the default values AND the aria2 clamp, so the
    UI never has to hardcode or re-clamp them. Missing/invalid keys fall back to
    defaults, and aria2_connections is always clamped to a safe range.
    """
    prefs = prefs or {}
    cfg = dict(DEFAULT_PERF_CONFIG)
    cfg['use_aria2'] = bool(prefs.get('use_aria2', DEFAULT_PERF_CONFIG['use_aria2']))
    cfg['aria2_connections'] = _clamp_aria2_connections(
        prefs.get('aria2_connections', DEFAULT_PERF_CONFIG['aria2_connections']))
    try:
        cfg['concurrent_fragment_downloads'] = int(
            prefs.get('concurrent_fragment_downloads',
                      DEFAULT_PERF_CONFIG['concurrent_fragment_downloads']))
    except (TypeError, ValueError):
        cfg['concurrent_fragment_downloads'] = DEFAULT_PERF_CONFIG['concurrent_fragment_downloads']
    try:
        cfg['http_chunk_size'] = int(prefs.get('http_chunk_size', DEFAULT_PERF_CONFIG['http_chunk_size']))
    except (TypeError, ValueError):
        cfg['http_chunk_size'] = DEFAULT_PERF_CONFIG['http_chunk_size']
    res = str(prefs.get('max_video_resolution', DEFAULT_PERF_CONFIG['max_video_resolution']))
    cfg['max_video_resolution'] = res if res in VIDEO_RESOLUTION_OPTIONS else DEFAULT_PERF_CONFIG['max_video_resolution']
    return cfg


def set_performance_config(cfg: dict):
    """Update the module-level performance configuration.

    Expected keys: use_aria2 (bool), aria2_connections (int), concurrent_fragment_downloads (int), http_chunk_size (int)
    """
    global performance_config
    cfg = dict(cfg)
    if 'aria2_connections' in cfg:
        cfg['aria2_connections'] = _clamp_aria2_connections(cfg['aria2_connections'])
    performance_config.update(cfg)


# --- Speed callback for UI speed label ---
_speed_ui_callback: Optional[Callable[[str], Any]] = None


def set_speed_ui_callback(cb: Optional[Callable[[str], Any]]):
    """Set the callback used to push live download speed to the UI.
    
    Called with a formatted speed string (e.g. "2.4 MiB/s • ETA 0:15").
    Pass "" to clear the label. Pass None to disable.
    """
    global _speed_ui_callback
    _speed_ui_callback = cb


# --- Runtime Execution Base Paths ---
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _user_data_base() -> Optional[str]:
    """Return this platform's per-user data root, or ``None`` if unknown.

    Mirrors ``download_queue._user_data_base`` so history, the artwork cache and
    the updatable yt-dlp copy all land in the *same* folder. Windows uses
    ``%APPDATA%``/``%LOCALAPPDATA%``, macOS ``~/Library/Application Support``
    and Linux/BSD ``$XDG_DATA_HOME`` or ``~/.local/share``. The macOS/Linux
    branches matter because neither Windows variable exists there, which
    previously made writes target the (unwritable) ``.app`` bundle.
    """
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return base
    if sys.platform == "darwin":
        home = os.path.expanduser("~")
        if home and home != "~":
            return os.path.join(home, "Library", "Application Support")
        return None
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return xdg
    home = os.path.expanduser("~")
    if home and home != "~":
        return os.path.join(home, ".local", "share")
    return None


def _get_user_data_dir() -> str:
    """Return a writable per-user directory for runtime data (caches).

    The packaged app installs under ``C:\\Program Files``, which standard users
    cannot write to. ``get_base_dir()`` therefore points at a read-only folder
    in frozen builds and must never be used as a write target. This returns the
    platform's per-user data folder (the same one history/queue use) and falls
    back to the executable/module folder only if that is unavailable.
    """
    base = _user_data_base()
    if base:
        target = os.path.join(base, "AudioDownloader")
        try:
            os.makedirs(target, exist_ok=True)
            return target
        except Exception:
            pass
    return get_base_dir()


def find_bundled_exe(name: str, include_path: bool = False) -> Optional[str]:
    """Locate a helper executable shipped with the app.

    Search order: PyInstaller bundle dir (``_internal`` for 6.x onedir, the
    temp extraction dir for onefile), the folder next to the app EXE, the
    source folder, then PATH when *include_path* is true. Returns ``None``
    when not found.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, name))
    exe_dir = os.path.dirname(os.path.abspath(getattr(sys, "executable", "") or ""))
    if exe_dir:
        candidates.append(os.path.join(exe_dir, name))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    if include_path:
        stem, _, _ = name.rpartition(".")
        found = shutil.which(name) or shutil.which(stem) if stem else shutil.which(name)
        if found:
            return found
    return None


def helper_exe_names(stem: str) -> tuple:
    """Candidate filenames for a bundled helper on this platform.

    Windows bundles ship as ``ffmpeg.exe``; macOS/Linux ones as ``ffmpeg``.
    The other spelling is still tried second so a bundle produced on a
    different OS resolves instead of silently reporting "not installed".
    """
    if os.name == "nt":
        return (stem + ".exe", stem)
    return (stem, stem + ".exe")


def find_helper(stem: str, include_path: bool = False) -> Optional[str]:
    """Locate a bundled helper by *stem* (e.g. ``"ffmpeg"``) on any platform."""
    for name in helper_exe_names(stem):
        found = find_bundled_exe(name, include_path=include_path)
        if found:
            return found
    return None


def get_ffmpeg_location() -> str:
    """Return a directory containing a usable ffmpeg for yt-dlp's ffmpeg_location.

    Resolves the actual bundled ffmpeg (PyInstaller 6 keeps it under
    ``_internal``), falling back to the runtime base dir and finally PATH.
    """
    exe = find_helper("ffmpeg", include_path=True)
    if exe:
        return os.path.dirname(exe)
    return get_base_dir()


def get_fast_downloader_path():
    """Return the path to an aria2 executable, or None if not found.

    Search order: bundled with the app (PyInstaller _internal / exe dir / source
    dir), then the system PATH. This way a packaged aria2c.exe ships with the
    app and works without the user installing anything.
    """
    # 1) Bundled with the app (PyInstaller _internal, exe dir, source dir)
    bundled = find_helper("aria2c")
    if bundled:
        return bundled
    # 2) System PATH
    for exe in ("aria2c", "aria2c.exe"):
        path = shutil.which(exe)
        if path:
            return path
    return None


def get_download_folder() -> str:
    """Current save directory for downloads.

    Returns the user-chosen override when it points at a real directory,
    otherwise the standard Downloads folder.
    """
    global _DOWNLOAD_FOLDER_OVERRIDE
    override = _DOWNLOAD_FOLDER_OVERRIDE
    if override and os.path.isdir(override):
        return override
    return str(Path.home() / "Downloads")


# Set by the UI when the user picks a custom save location (see
# set_download_folder / ui._choose_save_folder). Empty string = default.
_DOWNLOAD_FOLDER_OVERRIDE = ""


def set_download_folder(path: str) -> None:
    """Persist this session's download folder override ('' restores default).

    The value is validated lazily by get_download_folder(), so a folder that
    later disappears falls back to ~/Downloads automatically.
    """
    global _DOWNLOAD_FOLDER_OVERRIDE
    _DOWNLOAD_FOLDER_OVERRIDE = str(path or '').strip()


# ---- Audio output format presets (#2) -------------------------------------
AUDIO_FORMATS: dict[str, dict] = {
    # quality: yt-dlp semantics -- <=10 is the VBR scale (0 best), >10 means
    # an explicit bitrate in kbps. None means lossless/irrelevant.
    'mp3_vbr': {'label': 'MP3 (VBR High)',   'codec': 'mp3',  'quality': '2'},
    'mp3_320': {'label': 'MP3 320 kbps',     'codec': 'mp3',  'quality': '320'},
    'flac':    {'label': 'FLAC (Lossless)',  'codec': 'flac', 'quality': None},
    'wav':     {'label': 'WAV (Lossless)',   'codec': 'wav',  'quality': None},
    'opus':    {'label': 'Opus',             'codec': 'opus', 'quality': '0'},
}
_CURRENT_AUDIO_FORMAT = 'mp3_vbr'


def set_audio_format(key: str) -> None:
    """Select the audio preset used by every MP3/audio download path."""
    global _CURRENT_AUDIO_FORMAT
    if key not in AUDIO_FORMATS:
        logger.warning("Unknown audio format %r; keeping %r", key, _CURRENT_AUDIO_FORMAT)
        return
    _CURRENT_AUDIO_FORMAT = key


def get_audio_format() -> str:
    return _CURRENT_AUDIO_FORMAT


def get_audio_extension() -> str:
    """File extension produced by the currently selected audio preset."""
    fmt = AUDIO_FORMATS.get(_CURRENT_AUDIO_FORMAT, AUDIO_FORMATS['mp3_vbr'])
    return '.' + fmt['codec']


_TIKTOK_URL_RE = re.compile(
    r'https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/'
    r'|https?://(?:www\.)?t\.tiktok\.com/'
)

# Instagram posts, reels, and stories
_INSTAGRAM_URL_RE = re.compile(
    r'https?://(?:www\.)?instagram\.com/'
    r'(?:p/|reel/|reels/|stories/|tv/)'
)

_BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')


# Matches SoundCloud set/playlist URLs (e.g. /sets/...). These should not be
# bulk-downloaded directly; we resolve to a single track via YouTube instead.
_SOUNDCLOUD_SET_RE = re.compile(r'/sets/', re.IGNORECASE)

def _normalize_spotify_url(raw_input: str) -> str:
    """Resolve Spotify share-link variants into an open.spotify.com URL.

    Handles `spotify:track:...` URIs and `spotify.link` short links (which the
    mobile/desktop Share button produces) by following the redirect, so the rest
    of the pipeline can treat them like normal Spotify links.
    """
    raw = (raw_input or '').strip()
    if not raw:
        return raw_input
    uri_match = re.match(r'^spotify:(track|album|playlist|artist|episode|show):([A-Za-z0-9]+)$', raw)
    if uri_match:
        return f"https://open.spotify.com/{uri_match.group(1)}/{uri_match.group(2)}"
    if re.search(r'https?://(?:www\.)?spotify\.link/[\w]+', raw.lower()):
        try:
            req = urllib.request.Request(raw, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.geturl()
        except Exception:
            pass
    # Normalize embed URLs (/embed/track/...) to their canonical page.
    raw = re.sub(r'https?://(?:www\.)?(?:open\.)?spotify\.com/embed/', 'https://open.spotify.com/', raw, flags=re.IGNORECASE)
    return raw


def _spotify_entity(raw_input: str) -> Optional[str]:
    """Return which Spotify entity a link points to (track/album/playlist/artist)."""
    u = (raw_input or '').lower()
    for kind in ('track', 'album', 'playlist', 'artist', 'episode', 'show'):
        if re.search(r'/(?:embed/)?' + kind + r'[/?]', u):
            return kind
    return None


def _spotify_embed_metadata(raw_input: str):
    """Extract (title, artist) from Spotify's public embed-page JSON.

    Works for tracks (uses the track title + first artist) and for albums /
    playlists (uses the FIRST track in the track list, so we search for an
    actual song rather than the whole collection). Returns None if the embed
    page can't be fetched/parsed. This avoids both the DRM wall (we never try
    to download the stream) and any login requirement.
    """
    m = re.search(
        r'(?:www\.)?(?:open\.)?spotify\.com/(?:[\w-]+/)?(?:embed/)?'
        r'(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)',
        raw_input or '',
        re.IGNORECASE,
    )
    if not m:
        return None
    kind, spotify_id = m.group(1), m.group(2)
    try:
        req = urllib.request.Request(
            f"https://open.spotify.com/embed/{kind}/{spotify_id}",
            headers={"User-Agent": _BROWSER_UA},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            txt = resp.read(2000000).decode('utf-8', errors='replace')
        m2 = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', txt, re.DOTALL)
        if not m2:
            return None
        j = json.loads(m2.group(1))
        ent = j.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})
        if not isinstance(ent, dict):
            return None
        if ent.get('type') == 'track':
            title = ent.get('title') or ent.get('name') or ''
            artists = ent.get('artists') or []
            artist = artists[0].get('name', '') if artists and isinstance(artists[0], dict) else ''
            if title and artist:
                return title, artist
        # Albums / playlists: first track item has title + subtitle (artist).
        track_list = ent.get('trackList')
        if isinstance(track_list, list) and track_list and isinstance(track_list[0], dict):
            title = track_list[0].get('title') or ''
            artist = track_list[0].get('subtitle') or ''
            if title and artist:
                return title, artist
    except Exception:
        pass
    return None


def _apply_platform_headers(ydl_opts: dict, url: str) -> dict:
    """Patch downloader options for sites that block generic clients.

    TikTok's WAF rejects yt-dlp's default browser fingerprint (and rejects
    downloads unless `Referer` points at tiktok.com), so when the URL is a
    TikTok link we:
      1. swap the aria2 referer to tiktok.com, and
      2. pin an impersonation target (`chrome/120`) that TikTok accepts,
         which requires the optional `curl_cffi` dependency to be installed.

      1. A proper Referer header, and
      2. An impersonated browser fingerprint (yt-dlp's anime extractor uses
         this internally for HLS stream resolution).
        """
    if _TIKTOK_URL_RE.search(url or ''):
        try:
            args = ydl_opts.get('external_downloader_args')
            if isinstance(args, list):
                ydl_opts['external_downloader_args'] = [
                    a.replace('--referer=https://www.youtube.com/', '--referer=https://www.tiktok.com/')
                    for a in args
                ]
        except Exception:
            pass
        if yt_dlp is not None:
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget
                ydl_opts['impersonate'] = ImpersonateTarget('chrome', '120', 'macos', '14')
            except Exception:
                pass

    if _INSTAGRAM_URL_RE.search(url or ''):
        # Instagram requires proper browser headers and referer
        ydl_opts.setdefault('http_headers', {})
        ydl_opts['http_headers']['User-Agent'] = _BROWSER_UA
        ydl_opts['http_headers']['Referer'] = 'https://www.instagram.com/'
        ydl_opts['http_headers']['Origin'] = 'https://www.instagram.com'

    return ydl_opts


def get_ytdlp_version() -> Optional[str]:
    """Best-effort yt-dlp version: Python module first, then bundled exe."""
    try:
        from importlib.metadata import version as _md_version
        return _md_version('yt-dlp')
    except Exception:
        pass
    exe = _get_ytdlp_exe()
    if not exe:
        return None
    try:
        completed = subprocess.run(
            [exe, '--version'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace',
            **_no_window_kwargs(), timeout=20,
        )
        out = (completed.stdout or '').strip().splitlines()
        if completed.returncode == 0 and out:
            return out[-1].strip()
    except Exception as e:
        logger.debug("yt-dlp --version probe failed: %s", e)
    return None


def update_ytdlp(status_callback: Optional[Callable[[str, str], Any]] = None) -> str:
    """Update whichever yt-dlp engine is active (module -> pip; else bundled exe -U).

    In packaged (frozen) builds ``sys.executable`` is the PyInstaller bootloader,
    NOT python.exe, so the pip route can never work there -- the bundled
    yt-dlp.exe (-U) is used instead.
    """
    def _say(text: str, color: str) -> None:
        if status_callback:
            try:
                status_callback(text, color)
            except Exception:
                pass

    _say("Checking for yt-dlp updates...", "#3498db")
    before = get_ytdlp_version()

    if not getattr(sys, "frozen", False) and yt_dlp is not None:
        # Development/source environment: upgrade the installed Python module.
        _say("Updating yt-dlp...", "#3498db")
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"]
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=600,
            )
        except FileNotFoundError:
            return f"Could not run pip via {sys.executable}: executable not found."
        except OSError as e:
            return f"Could not run pip ({sys.executable}):\n{e}"
        except subprocess.TimeoutExpired:
            return "The pip upgrade timed out after 10 minutes. Try again later."

        out = (completed.stdout or "")
        if completed.returncode != 0:
            tail = "\n".join(out.strip().splitlines()[-8:])
            return f"pip upgrade failed (exit code {completed.returncode}):\n{tail}"

        after = get_ytdlp_version()
        changed = (before != after)
        tail = "\n".join(out.strip().splitlines()[-6:])
        if changed:
            headline = f"Updated yt-dlp: {before or '?'} -> {after or '?'}"
            extra = "\n\nRestart the app so it loads the updated module."
        else:
            headline = f"yt-dlp is already up to date ({after or 'unknown version'})."
            extra = ""
        return f"{headline}\n{tail}".strip() + extra

    # Packaged build (or no Python module): update the bundled yt-dlp.exe, which
    # is the only engine that can refresh itself without a real pip.
    exe = _writable_ytdlp_copy()
    if not exe:
        return (
            "Neither the 'yt_dlp' Python module nor a bundled yt-dlp.exe was found.\n"
            "Install it manually with:\n"
            "    python -m pip install -U yt-dlp"
        )

    _say("Updating yt-dlp.exe...", "#3498db")
    try:
        completed = subprocess.run(
            [exe, "-U"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            **_no_window_kwargs(), timeout=300,
        )
    except FileNotFoundError:
        return f"yt-dlp.exe disappeared from the expected path:\n{exe}"
    except OSError as e:
        return f"Could not run the updater ({exe}):\n{e}"
    except subprocess.TimeoutExpired:
        return "The updater timed out after 5 minutes. Try again later."

    out = (completed.stdout or "")
    if completed.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-8:])
        return f"Updater failed (exit code {completed.returncode}):\n{tail}"

    after = get_ytdlp_version()
    changed = (before != after)
    tail = "\n".join(out.strip().splitlines()[-6:])
    headline = f"Updated yt-dlp: {before or '?'} -> {after or '?'}" if changed \
        else f"yt-dlp is already up to date ({after or 'unknown version'})."
    return f"{headline}\n\n{tail}".strip()


_YTDLP_RELEASES_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"


def _parse_version_tuple(value):
    """'v2026.08.14-beta' -> (2026, 8, 14); None when no dotted digits found."""
    m = re.search(r"\d+(?:\.\d+)+", value or "")
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(0).split("."))
    except Exception:
        return None


# Daily update-check throttle. Both startup probes consult this so we hit the
# network at most once per calendar day instead of on every single launch.
_UPDATE_CHECK_CACHE = os.path.join(tempfile.gettempdir(), "tunelab_update_check.json")


def _update_check_done_today(kind: str) -> bool:
    """Return True if an update check of *kind* already ran today."""
    try:
        with open(_UPDATE_CHECK_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    return isinstance(data, dict) and data.get(kind) == time.strftime("%Y-%m-%d")


def _mark_update_check_done(kind: str) -> None:
    """Record that an update check of *kind* ran today."""
    try:
        data = {}
        try:
            with open(_UPDATE_CHECK_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f) if f.readable() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[kind] = time.strftime("%Y-%m-%d")
        with open(_UPDATE_CHECK_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass  # Non-writable cache just means we check again next launch


def check_ytdlp_update_available(timeout: float = 8.0) -> dict:
    """Compare local yt-dlp vs latest GitHub release.

    Returns {'status': 'update'|'current'|'unknown', 'local', 'latest'};
    never raises -- trouble degrades to status 'unknown'.
    """
    local = get_ytdlp_version()
    latest = None
    try:
        req = urllib.request.Request(
            _YTDLP_RELEASES_API, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            latest = str(data.get("tag_name") or data.get("name") or "").strip()
    except Exception as e:
        logger.debug("yt-dlp update check failed: %s", e)
        return {"status": "unknown", "local": local, "latest": None}

    lt = _parse_version_tuple(latest)
    lvt = _parse_version_tuple(local or "")
    if not latest or not lt or not lvt:
        status = "unknown"
    else:
        status = "update" if lt > lvt else "current"
    return {"status": status, "local": local, "latest": latest}



def _get_ytdlp_exe() -> Optional[str]:
    """Return path to a usable yt-dlp executable (if present).

    The writable per-user copy (created by :func:`update_ytdlp` on first run)
    takes precedence: the packaged app ships yt-dlp.exe inside the read-only
    install directory, so a self-updated binary can only ever live elsewhere.
    """
    candidates = []
    names = helper_exe_names("yt-dlp")
    user_copy = os.path.join(_get_user_data_dir(), names[0])
    candidates.append(user_copy)
    for name in names:
        found = find_bundled_exe(name)
        if found:
            candidates.append(found)
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _writable_ytdlp_copy() -> Optional[str]:
    """Return a writable yt-dlp.exe path the updater can safely modify.

    ``yt-dlp.exe -U`` rewrites its own binary, which fails on the Program Files
    install directory. When the resolved executable is not writable we copy it
    into ``%APPDATA%\\AudioDownloader`` once and update that copy; because
    ``_get_ytdlp_exe()`` prefers the user copy, downloads then use the updated
    binary. Returns the resolved (possibly copied) path, or ``None``.
    """
    exe = _get_ytdlp_exe()
    if not exe:
        return None
    dest = os.path.join(_get_user_data_dir(), helper_exe_names("yt-dlp")[0])
    if os.path.abspath(exe) == os.path.abspath(dest):
        return dest
    # os.access(W_OK) only reflects the read-only attribute on Windows and
    # ignores ACLs, so it reports True for Program Files. Detect the install
    # directory explicitly instead.
    install_dir = os.path.abspath(get_base_dir()) if getattr(sys, "frozen", False) else ""
    try:
        in_install_dir = bool(install_dir) and os.path.abspath(exe).lower().startswith(
            install_dir.lower() + os.sep
        )
        if not in_install_dir and os.access(exe, os.W_OK):
            return exe
        if not os.path.isfile(dest) or os.path.getmtime(dest) < os.path.getmtime(exe):
            shutil.copyfile(exe, dest)
            logger.info("Copied yt-dlp.exe to writable location %s", dest)
        return dest
    except Exception as e:
        logger.warning("Could not stage yt-dlp.exe for update: %s", e)
        return exe


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort terminate a spawned subprocess (and children on Windows)."""
    try:
        if os.name == 'nt':
            subprocess.run(
                ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_no_window_kwargs(),
                timeout=5,
            )
        else:
            proc.terminate()
    except Exception:
        logger.debug("Graceful kill failed for pid %s; forcing", getattr(proc, 'pid', '?'))
        try:
            proc.kill()
        except Exception:
            logger.debug("Could not kill child process %s", getattr(proc, 'pid', '?'))


def _run_yt_dlp_exe(args: list[str], status_callback: Optional[Callable[[str, str], Any]] = None) -> None:
    """Run yt-dlp.exe as a fallback when the Python module isn't available."""
    ytdlp_exe = _get_ytdlp_exe()
    if not ytdlp_exe:
        raise RuntimeError("Missing dependency: neither Python 'yt_dlp' nor local 'yt-dlp.exe' is available.")

    if status_callback:
        try:
            status_callback("Running yt-dlp.exe fallback...", "#3498db")
        except Exception:
            pass

    # Popen + poll loop (instead of subprocess.run) so a user cancel can kill
    # the child immediately rather than waiting for the full download.
    process = subprocess.Popen(
        [ytdlp_exe] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_no_window_kwargs(),  # CREATE_NO_WINDOW
    )
    try:
        output = ""
        while True:
            try:
                chunk, _ = process.communicate(timeout=0.5)
                if chunk:
                    output += chunk
                break
            except subprocess.TimeoutExpired:
                if is_download_cancel_requested():
                    _kill_process_tree(process)
                    raise DownloadCancelled("Download cancelled by user.")
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                logger.debug("Failed to clean up yt-dlp.exe process")
    if process.returncode != 0:
        raise RuntimeError("yt-dlp.exe failed:\n" + (output or ""))


def can_resolve_preview() -> bool:
    """True if a preview stream URL can be resolved.

    Returns True when either the Python ``yt_dlp`` module is importable or a
    bundled ``yt-dlp.exe`` (in the base dir or on PATH) is available.
    """
    if yt_dlp is not None:
        return True
    return _get_ytdlp_exe() is not None


def resolve_preview_stream(url: str, audio_only: bool = False) -> str:
    """Return a direct, playable stream URL for previewing ``url``.

    Tries the Python ``yt_dlp`` module first (``extract_info``), then falls back
    to the bundled ``yt-dlp.exe`` (``-g``) when the module is missing. Raises a
    ``RuntimeError`` with an actionable message if neither is available.
    """
    if yt_dlp is not None:
        fmt = 'bestaudio/best' if audio_only else 'best'
        ydl_opts: dict[str, Any] = {
            'format': fmt,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 20,
            'force_ipv4': True,
        }
        ydl_opts = _apply_platform_headers(ydl_opts, url)
        with cast(Any, yt_dlp).YoutubeDL(cast(Any, ydl_opts)) as ydl:
            info = ydl.extract_info(url, download=False)
        stream_url: Optional[str] = None
        if isinstance(info, dict):
            if info.get('url'):
                stream_url = info['url']
            elif info.get('formats'):
                if audio_only:
                    chosen = max(
                        info['formats'],
                        key=lambda f: f.get('abr') or f.get('tbr') or 0,
                    )
                else:
                    chosen = max(
                        info['formats'],
                        key=lambda f: (f.get('height') or 0) + (f.get('tbr') or 0),
                    )
                stream_url = chosen.get('url')
        if not stream_url:
            raise RuntimeError('Could not determine a playable stream URL.')
        return stream_url

    # Module missing: fall back to the bundled yt-dlp.exe (-g resolves URLs).
    ytdlp_exe = _get_ytdlp_exe()
    if not ytdlp_exe:
        raise RuntimeError(
            "Missing dependency: the 'yt_dlp' Python module is not available and no "
            "local 'yt-dlp.exe' was found. Install yt-dlp "
            "(python -m pip install yt-dlp) or run a download first to generate the "
            "bundled yt-dlp.exe, then retry the preview."
        )
    args = ['-g', '--no-playlist', '-f', 'bestaudio/best' if audio_only else 'best', url]
    try:
        completed = subprocess.run(
            [ytdlp_exe] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"yt-dlp.exe was not found at the expected path: {ytdlp_exe}"
        )
    except OSError as e:
        raise RuntimeError(
            f"Could not run yt-dlp.exe ({ytdlp_exe}): {e}"
        )
    if completed.returncode != 0:
        raise RuntimeError(
            "yt-dlp.exe stream resolution failed:\n" + (completed.stdout or "")
        )
    lines = (completed.stdout or '').strip().splitlines()
    if not lines or not lines[0]:
        raise RuntimeError('yt-dlp.exe returned no stream URL.')
    return lines[0].strip()


def _build_youtube_dl(options: dict[str, Any]) -> Any:
    if yt_dlp is None:
        raise RuntimeError(
            "Missing dependency: 'yt-dlp' Python module is required for this codepath. "
            "If you have yt-dlp.exe, downloader() functions should fall back to it."
        )
    return yt_dlp.YoutubeDL(cast(Any, options))


def build_fast_yt_dlp_options(base_dir, output_template, audio_only: bool = False, perf_cfg: Optional[dict] = None, format_override: Optional[str] = None, player_client: Optional[list] = None) -> dict[str, Any]:
    # Merge perf config first so the video resolution cap (and everything else)
    # respects both the saved prefs and any caller-provided overrides.
    cfg = performance_config.copy()
    if isinstance(perf_cfg, dict):
        cfg.update(perf_cfg)
    # yt-dlp options are a loose dict; type-checkers often treat values as Unknown.
    # For video, cap the resolution if the user picked one (saves time/disk since
    # the After Effects re-encode downscales anyway).
    if audio_only:
        base_format = 'bestaudio/best'
    else:
        base_format = video_format_string(str(cfg.get('max_video_resolution', 'Best (up to 4K)')))
    # Use custom filename template if set (yt-dlp output template syntax).
    _custom_tmpl = str(cfg.get('filename_template', '') or '').strip()
    _outtmpl = _custom_tmpl if _custom_tmpl else output_template
    ydl_opts: dict[str, Any] = {
        'format': base_format,
        'outtmpl': _outtmpl,
        'ffmpeg_location': get_ffmpeg_location(),
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'retries': 5,
        'fragment_retries': 10,
        'continue': True,  # resume interrupted .part downloads (aria2 also gets -c below)
        'skip_unavailable_fragments': False,  # False = retry failed fragments instead of silently dropping them (dropping causes pixelated/blocky frames)
        'socket_timeout': 20,
        'concurrent_fragment_downloads': 8,
        'http_chunk_size': 1 << 20,
        'hls_prefer_native': True,
        'prefer_ffmpeg': True,
        'extractor_retries': 5,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'force_ipv4': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-Dest': 'document',
        },
        # NOTE: Do NOT pin youtube:player_client. Hardcoding legacy clients
        # ('android','ios','tv','web_embedded') caps YouTube formats far below
        # maximum (observed ~360p); letting yt-dlp use its defaults exposes
        # VP9/AV1 up to 4K+. Callers may still pass an explicit override
        # (e.g. the 403-retry paths) and that is honoured below.
    }


    # Apply tuned concurrency settings
    try:
        ydl_opts['concurrent_fragment_downloads'] = int(cfg.get('concurrent_fragment_downloads', ydl_opts['concurrent_fragment_downloads']))
        ydl_opts['http_chunk_size'] = int(cfg.get('http_chunk_size', ydl_opts['http_chunk_size']))
    except Exception:
        pass

    # External downloader (aria2) optional override
    if cfg.get('use_aria2'):
        aria2c = get_fast_downloader_path()
        if aria2c:
            conn = _clamp_aria2_connections(cfg.get('aria2_connections', 16))
            args = ['-c', '-x', str(conn), '-s', str(conn), '-k', '1M', '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36', '--referer=https://www.youtube.com/']
            ydl_opts.update({
                'external_downloader': 'aria2c',
                'external_downloader_args': args,
            })

    if format_override is not None:
        ydl_opts['format'] = format_override
    if player_client:
        # Explicit caller-provided client list (retry paths); empty/None = defaults.
        ydl_opts['extractor_args'] = {'youtube': {'player_client': list(player_client)}}
    if not audio_only:
        ydl_opts['merge_output_format'] = 'mp4'
        ydl_opts['remux_video'] = 'mp4'
        ydl_opts['writethumbnail'] = False
        ydl_opts['embedthumbnail'] = False
        ydl_opts['writeinfojson'] = False
        ydl_opts['writesubtitles'] = False
        # Prefer codecs After Effects can import directly. AV1 (av01),
        # 10-bit HEVC/H.264 and Opus audio are common yt-dlp picks that VLC
        # plays but AE rejects, so sort for H.264 + AAC 8-bit when available;
        # ensure_after_effects_compatibility re-encodes only when needed.
        ydl_opts['format_sort'] = ['vcodec:h264', 'acodec:aac', 'res', 'quality']

    if audio_only:
        fmt = AUDIO_FORMATS.get(_CURRENT_AUDIO_FORMAT, AUDIO_FORMATS['mp3_vbr'])
        pp: dict[str, Any] = {
            'key': 'FFmpegExtractAudio',
            'preferredcodec': fmt['codec'],
        }
        if fmt['quality'] is not None:
            pp['preferredquality'] = fmt['quality']
        ydl_opts['postprocessors'] = [pp]
        ydl_opts['addmetadata'] = True
        ydl_opts['writethumbnail'] = True
        # Cover-art embedding is unreliable/impossible on raw WAV.
        ydl_opts['embedthumbnail'] = fmt['codec'] != 'wav'

    return ydl_opts


def _capture_download_metadata(progress: dict) -> None:
    """Record artist/title from a download's ``info_dict`` for ID3 tagging.

    Progress hooks run *during* the transfer, so the post-download tag writer
    gets real values even for a direct link -- where no search step ran to
    populate them. Canonical metadata resolved by the search path is never
    second-guessed: if a title is already present we leave it alone.
    """
    info = progress.get('info_dict') or {}
    if not isinstance(info, dict):
        return
    title = info.get('title') or ''
    if not title:
        return
    current = get_last_dl_metadata()
    if current.get('title'):
        # The search path already resolved canonical metadata for this track.
        return
    artist = (info.get('artist') or info.get('uploader')
              or info.get('creator') or info.get('channel') or '')
    set_last_dl_metadata(
        artist=artist,
        title=title,
        album=current.get('album') or '',
        genre=current.get('genre') or '',
        year=current.get('year') or '',
        artwork_path=current.get('artwork_path') or '',
    )


def build_progress_hook(status_callback: Callable[[str, str], Any], progress_callback: Optional[Callable[[float], Any]] = None, speed_callback: Optional[Callable[[str], Any]] = None):
    def hook(progress):
        # Cooperative cancellation: abort the transfer as soon as the flag is
        # seen. Raising here propagates through YoutubeDL.download().
        if is_download_cancel_requested():
            raise DownloadCancelled("Download cancelled by user.")
        # Grab artist/title so a direct link or the exe fallback still ends up
        # with correct tags (and a real title in history).
        try:
            _capture_download_metadata(progress)
        except Exception:
            pass
        status = progress.get('status')
        if status == 'downloading':
            percent_str = progress.get('_percent_str', '0%').strip()
            try:
                percent = float(percent_str.replace('%', '')) / 100.0
            except Exception:
                percent = 0.0
            if progress_callback:
                progress_callback(percent)
            eta = progress.get('_eta_str', 'Unknown')
            speed = progress.get('_speed_str', '')
            status_callback(f"Downloading... {percent_str} ETA {eta}", "#3498db")
            speed_text = f"{speed} • ETA {eta}" if speed else ""
            if speed_callback and speed_text:
                speed_callback(speed_text)
            if _speed_ui_callback and speed_text:
                _speed_ui_callback(speed_text)
        elif status == 'finished':
            if progress_callback:
                progress_callback(1.0)
            if speed_callback:
                speed_callback("")
            if _speed_ui_callback:
                _speed_ui_callback("")
            status_callback("Finalizing download...", "#3498db")
    return hook


def _snapshot_download_files(folder: str) -> set:
    """Return the set of file *names* currently present in `folder`."""
    names: set = set()
    try:
        for p in Path(folder).iterdir():
            if p.is_file():
                names.add(p.name)
    except Exception:
        pass
    return names


def _remove_new_media_files(folder: str, before: set) -> None:
    """Best-effort delete files created since snapshot `before` that look like
    media or partial download artifacts left behind by a failed attempt."""
    try:
        for p in Path(folder).iterdir():
            if not p.is_file():
                continue
            if p.name in before:
                continue  # pre-existing; never touch it
            lower = p.name.lower()
            if (lower.endswith(('.part', '.ytdl', '.youtube-dl', '.tmp', '.temp'))
                    or lower.endswith(('.mp3', '.mp4', '.m4a', '.webm', '.opus',
                                       '.wav', '.flac', '.ogg', '.aac', '.3gp'))):
                try:
                    os.remove(str(p))
                except Exception:
                    pass
    except Exception:
        pass


def _remove_new_thumbnail_files(folder: str, before: set) -> None:
    """Remove image sidecars created by a video download attempt."""
    try:
        for p in Path(folder).iterdir():
            if p.is_file() and p.name not in before and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
                try:
                    os.remove(str(p))
                except Exception:
                    pass
    except Exception:
        pass


def _strip_zone_identifier(filepath: str) -> None:
    """Remove the 'Mark of the Web' ADS that Windows attaches to downloaded files.

    Windows Defender flags files with the Zone.Identifier alternate data stream
    (the 'Mark of the Web') and may quarantine them. Stripping it after the file
    is fully written stops Defender from treating the file as an internet
    download and auto-deleting it.
    """
    try:
        ads_path = filepath + ":Zone.Identifier"
        if os.path.exists(ads_path):
            os.remove(ads_path)
    except Exception:
        pass  # Non-fatal: if we can't strip it, the file still works


def _strip_zone_identifiers_from_folder(folder: str) -> None:
    """Strip the Mark of the Web from every media file in *folder*."""
    if not folder or not os.path.isdir(folder):
        return
    media_exts = ('.mp3', '.mp4', '.m4a', '.flac', '.wav', '.opus', '.ogg',
                  '.aac', '.webm', '.mkv', '.jpg', '.jpeg', '.png', '.webp')
    try:
        for p in Path(folder).iterdir():
            if p.is_file() and p.suffix.lower() in media_exts:
                _strip_zone_identifier(str(p))
    except Exception:
        pass


def _find_latest_mp3(download_folder: str, before_files: Optional[set] = None) -> Optional[str]:
    """Find the audio file just written by a download (any supported codec).

    Prefers files that appeared *after* the `before_files` snapshot (a set of
    file names captured at the start of the download). This avoids embedding
    artwork into an unrelated file that happens to be the newest in the folder
    (e.g. another app wrote to Downloads concurrently). Falls back to the
    newest matching file overall if no snapshot was given. Files produced by
    the *currently selected* audio preset are preferred over other extensions.
    """
    wanted_ext = get_audio_extension()
    media_exts = {wanted_ext, '.mp3', '.flac', '.wav', '.opus', '.m4a', '.aac', '.ogg'}
    try:
        files = [
            (os.path.getmtime(str(p)), str(p))
            for p in Path(download_folder).glob('*')
            if p.is_file() and p.suffix.lower() in media_exts
        ]
    except Exception:
        return None
    if not files:
        return None

    if before_files:
        new_files = [entry for entry in files if os.path.basename(entry[1]) not in before_files]
        pool = new_files or files
    else:
        pool = files
    # Prefer the selected preset's extension within whichever pool we have.
    preferred = [e for e in pool if str(e[1]).lower().endswith(wanted_ext)]
    return max(preferred or pool)[1]


def pick_produced_media_file(folder: str, before_files: Optional[set] = None) -> Optional[str]:
    """Return the media file a just-finished download produced, or ``None``.

    Single source of truth for "which file did this download write?", shared by
    the download queue, the direct-download path and the history writer so they
    can never disagree. Two failure modes are guarded against:

    * Thumbnails, subtitles and yt-dlp sidecar files are excluded, so a
      ``track.webp`` sitting next to ``track.mp3`` can never be treated as the
      audio. The queue previously took ``sorted(new_files)[0]``, which is
      alphabetical -- the thumbnail (or ``.info.json``) sorted ahead of the
      media file, so ``filepath`` pointed at an image and the ID3 tags were
      never written to the audio at all.
    * When *before_files* (the set of names present before the download
      started) is supplied, only names that did not exist before are
      considered, so a file another program wrote into the same folder
      concurrently is not mistaken for this download's output.

    Among the remaining candidates the newest by modification time wins,
    preferring the extension produced by the selected audio preset.
    """
    wanted_ext = get_audio_extension()
    media_exts = {wanted_ext, '.mp3', '.mp4', '.m4a', '.flac', '.wav', '.opus',
                  '.ogg', '.aac', '.webm', '.mkv'}
    try:
        candidates = [
            (os.path.getmtime(str(p)), str(p))
            for p in Path(folder).glob('*')
            if p.is_file()
            and p.suffix.lower() in media_exts
            and (before_files is None or p.name not in before_files)
        ]
    except Exception:
        return None
    if not candidates:
        return None
    preferred = [e for e in candidates if str(e[1]).lower().endswith(wanted_ext)]
    return max(preferred or candidates)[1]


def _mp3_has_embedded_artwork(mp3_path: str) -> bool:
    if mutagen is None:
        return False
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import APIC

        audio = MP3(mp3_path)
        tags = audio.tags
        if tags is None:
            return False
        return any(isinstance(frame, APIC) for frame in tags.values())
    except Exception:
        return False


def _find_thumbnail_for_mp3(mp3_path: str) -> Optional[str]:
    base_path = os.path.splitext(mp3_path)[0]
    base_name = os.path.splitext(os.path.basename(mp3_path))[0]
    # 1) Exact sidecar files sharing the same base name as the MP3.
    #    (".image" is what yt-dlp names thumbnails whose format it can't infer --
    #    e.g. TikTok covers -- the real type is detected from magic bytes later.)
    for ext in ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.image'):
        candidate = f"{base_path}{ext}"
        if os.path.exists(candidate):
            return candidate
    # 2) Any image in the same folder whose name starts with this MP3's name.
    #    yt-dlp writes e.g. "Song.webp" or "Song - [abc].webp".
    #    (We match by name instead of "newest file anywhere in the folder" so we
    #    never embed artwork belonging to a different track.)
    try:
        for p in Path(os.path.dirname(mp3_path)).glob(f"{base_name}*"):
            if p.is_file() and p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.image'}:
                return str(p)
    except Exception:
        pass
    return None


def _normalize_image_bytes(raw: bytes):
    """Detect the real image format and return (mime, bytes) for embedding.

    Re-encodes anything that is not a plain JPEG/Png to a clean RGB JPEG using
    Pillow. This is essential for YouTube thumbnails, which are often `.webp`:
    writing webp bytes with an 'image/jpeg' MIME tag (the old behavior) produced
    a blank/invisible cover. Falls back to a best-effort read when Pillow is
    unavailable or the image is unreadable.
    """
    if not raw:
        return None, None

    fmt = None
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        fmt = 'PNG'
    elif raw[:2] == b'\xff\xd8':
        fmt = 'JPEG'
    elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        fmt = 'WEBP'
    elif raw[:6] in (b'GIF87a', b'GIF89a'):
        fmt = 'GIF'
    elif raw[:2] == b'BM':
        fmt = 'BMP'

    # Convert every non-JPEG format to JPEG for maximum player compatibility.
    if Image is not None and fmt != 'JPEG':
        try:
            im = Image.open(io.BytesIO(raw))
            im = im.convert('RGB')
            out = io.BytesIO()
            im.save(out, format='JPEG', quality=92)
            return 'image/jpeg', out.getvalue()
        except Exception:
            pass

    if fmt == 'PNG':
        return 'image/png', raw
    if fmt == 'WEBP':
        return 'image/jpeg', raw
    return 'image/jpeg', raw


def _normalize_image_file(image_path: str):
    """Convenience wrapper to normalize an image on disk into (mime, bytes)."""
    try:
        with open(image_path, 'rb') as f:
            raw = f.read()
    except Exception:
        return None, None
    return _normalize_image_bytes(raw)


def _embed_mp3_thumbnail(mp3_path: str, image_path: str) -> bool:
    if mutagen is None:
        return False
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import APIC, ID3
    except Exception:
        return False

    try:
        mime, image_data = _normalize_image_file(image_path)
        if not image_data:
            return False

        audio = MP3(mp3_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        if tags is None:
            return False

        tags.delall('APIC')
        tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc='Cover',
                data=image_data,
            )
        )
        audio.save(v2_version=3)
        return True
    except Exception:
        return False


def _embed_artwork_bytes(mp3_path: str, image_bytes: bytes) -> bool:
    """Embed raw image bytes into an MP3, normalizing the format first."""
    if mutagen is None or not image_bytes:
        return False
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import APIC, ID3
    except Exception:
        return False
    try:
        mime, data = _normalize_image_bytes(image_bytes)
        if not data:
            return False
        audio = MP3(mp3_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        if tags is None:
            return False
        tags.delall('APIC')
        tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=data))
        audio.save(v2_version=3)
        return True
    except Exception:
        return False


def _fetch_thumbnail_bytes(thumbnail_url: str) -> Optional[bytes]:
    """Download a thumbnail URL into raw bytes (browser UA, small read)."""
    if not thumbnail_url:
        return None
    try:
        req = urllib.request.Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


def _get_source_thumbnail_url(source_ref: Optional[str]) -> Optional[str]:
    """Best-effort extract the source media's thumbnail URL via yt-dlp.

    Works for a direct video link as well as a `ytsearchN:...` spec (in which
    case the first result's thumbnail is returned).
    """
    if yt_dlp is None or not source_ref:
        return None
    try:
        with yt_dlp.YoutubeDL(cast(Any, {'quiet': True, 'noplaylist': True, 'skip_download': True})) as ydl:
            info = ydl.extract_info(source_ref, download=False)
            if not isinstance(info, dict):
                return None
            entries = info.get('entries')
            if entries:
                first = entries[0] if isinstance(entries, list) else None
                if not isinstance(first, dict):
                    return None
                info = first
            return info.get('thumbnail')
    except Exception:
        return None


def _embed_thumbnail_if_missing(
    mp3_path: str,
    status_callback: Callable[[str, str], Any],
    detail_callback: Optional[Callable[[str, str], Any]] = None,
) -> bool:
    if _mp3_has_embedded_artwork(mp3_path):
        return True

    image_path = _find_thumbnail_for_mp3(mp3_path)
    if image_path is None:
        return False

    if not _embed_mp3_thumbnail(mp3_path, image_path):
        return False

    try:
        os.remove(image_path)
    except Exception:
        pass

    callback = detail_callback or status_callback
    callback(f"Artwork embedded successfully into {os.path.basename(mp3_path)}.", "#2ecc71")
    return True


def _build_spotify_search_query(raw_input: str) -> Optional[str]:
    """Build a YouTube search query for a Spotify track/album/playlist link.

    Spotify streams are DRM-protected so they can't be downloaded directly --
    instead we extract the track title + artist and search YouTube for that
    track (the same strategy used for SoundCloud links). Covers track links,
    album/playlist links (uses the first track), `spotify:` URIs, embed links
    and `spotify.link` short links.
    """
    raw_input = _normalize_spotify_url(raw_input)

    # 1) Spotify embed-page JSON: precise title + artist for tracks and the
    #    FIRST track of albums/playlists. No DRM wall, no login, no rate limit.
    meta = _spotify_embed_metadata(raw_input)
    if meta:
        title, artist = meta
        canon = _canonical_metadata(artist, title)
        if canon:
            artist, title = canon['artist'], canon['title']
        return f"ytsearch1:{artist} {title} official audio"

    # 2) oEmbed: title-only (Spotify's oEmbed omits author_name). Still useful
    #    when the embed fetch is blocked, and a bare song-title search works.
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(raw_input, safe=':/?&=') }"
        with urllib.request.urlopen(oembed_url, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
        title = data.get('title', '')
        if title:
            canon = _canonical_metadata("", title)
            if canon:
                title = canon['title']
            return f"ytsearch1:{title} official audio"
    except Exception:
        pass

    # 3) Last resort: parse the page HTML for og: metadata (reliable for tracks).
    try:
        req = urllib.request.Request(raw_input, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            page_text = resp.read(200000).decode('utf-8', errors='replace')

        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', page_text)
        desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', page_text)
        title = html.unescape(title_match.group(1)) if title_match else ''
        desc = html.unescape(desc_match.group(1)) if desc_match else ''

        if title and desc:
            title_text = title
            # og:description for tracks is usually "Artist · Song · Year".
            artist_text = desc.split('•')[0].strip() if '•' in desc else desc.split('·')[0].strip()
            artist_text = artist_text.replace('Artist', '').replace('Album', '').strip(' -–')
            if artist_text and title_text:
                canon = _canonical_metadata(artist_text, title_text)
                if canon:
                    artist_text, title_text = canon['artist'], canon['title']
                return f"ytsearch1:{artist_text} {title_text} official audio"
            if title_text:
                canon = _canonical_metadata("", title_text)
                if canon:
                    title_text = canon['title']
                return f"ytsearch1:{title_text} official audio"
        elif title:
            canon = _canonical_metadata("", title)
            if canon:
                title = canon['title']
            return f"ytsearch1:{title} official audio"
    except Exception:
        pass

    return None


def _strip_soundcloud_noise(text):
    """Remove SoundCloud page chrome from a title/artist string.

    Handles the common formats:
      "Track by Artist"
      "Stream Track by Artist | Listen online for free on SoundCloud"
      "Listen to Track by Artist #np on #SoundCloud"
    Returns the cleaned string (or '' when empty).
    """
    if not text:
        return ''
    s = str(text).strip()
    s = s.split('|')[0].strip()                            # "... | Listen online ..."
    s = re.sub(r'^Stream\s+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'^Listen to\s+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*#np on #SoundCloud\s*', ' ', s, flags=re.IGNORECASE).strip()
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _build_soundcloud_search_query(raw_input: str) -> Optional[str]:
    """Extract track title/artist from a SoundCloud link and build a YouTube search query.

    This prevents yt-dlp from downloading an entire album/playlist when given a
    SoundCloud track URL. Instead, we search YouTube for the specific track.
    """
    try:
        if yt_dlp is not None:
            with yt_dlp.YoutubeDL({'quiet': True, 'noplaylist': True}) as ydl_meta:
                info = ydl_meta.extract_info(raw_input, download=False)
                if isinstance(info, dict):
                    # If it's a playlist/album, take the first entry
                    entries = info.get('entries')
                    if entries:
                        try:
                            entry_list = entries if isinstance(entries, list) else list(entries)  # type: ignore[assignment]
                            first = entry_list[0] if entry_list else None
                            if isinstance(first, dict):
                                info = first
                        except (TypeError, IndexError):
                            pass
                    title = info.get('title', '')
                    artist = info.get('artist', info.get('uploader', info.get('creator', '')))
                    if title and artist:
                        # Use the canonical catalog spelling when available so the
                        # YouTube query targets the right recording, not a remix.
                        meta = _canonical_metadata(artist, title)
                        if meta:
                            artist, title = meta['artist'], meta['title']
                        return f"ytsearch1:{artist} {title} official audio"
    except Exception:
        pass

    # Fallback: parse the SoundCloud page HTML for og:title / og:description.
    # SoundCloud's og tags carry a lot of chrome ("Listen to <T> by <A> #np
    # on #SoundCloud", "<title> | Listen online for free ..."), so we strip
    # that noise before extracting artist/title instead of trusting the raw
    # segments (which previously turned the whole description into the artist
    # and made YouTube search return a completely different track).
    try:
        req = urllib.request.Request(raw_input, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            # Meta tags live in <head>, which is typically the first few KB.
            # Reading just 32KB (not the full page) is plenty and avoids
            # downloading ~250KB of page body we never use.
            page_text = resp.read(32000).decode('utf-8', errors='replace')

        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', page_text)
        desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', page_text)
        og_title = html.unescape(title_match.group(1)) if title_match else ''
        og_desc = html.unescape(desc_match.group(1)) if desc_match else ''
        title, artist = '', ''

        # 1) og:title is either the bare title ("Mary") or "Track by Artist".
        if og_title:
            cand = _strip_soundcloud_noise(og_title)
            if ' by ' in cand:
                _t, _a = (p.strip() for p in cand.rsplit(' by ', 1))
                title, artist = _t, _a
            else:
                title = cand

        # 2) og:description "Listen to <T> by <A> #np on #SoundCloud" is the
        #    reliable source of the artist when og:title is bare.
        if og_desc and not artist:
            m = re.match(
                r'^Listen to\s+(?P<t>.+?)\s+by\s+(?P<a>.+?)(?:\s*#np on #SoundCloud.*)?$',
                og_desc, re.IGNORECASE | re.DOTALL,
            )
            if m:
                artist = _strip_soundcloud_noise(m.group('a'))
                if not title:
                    title = _strip_soundcloud_noise(m.group('t'))
            else:
                # Plain "Artist · Year" style description.
                artist = og_desc.split('•')[0].strip() if '•' in og_desc else og_desc.split('·')[0].strip()
                artist = artist.replace('Artist', '').replace('Album', '').strip(' -–')
                artist = _strip_soundcloud_noise(artist)

        # 3) page <title> "Stream <T> by <A> | Listen online for free on SoundCloud".
        if (not artist or not title) and page_text:
            pt = re.search(r'<title>(.*?)</title>', page_text, re.IGNORECASE | re.DOTALL)
            if pt:
                page_title = re.sub(r'\s+', ' ', pt.group(1)).strip()
                page_title = _strip_soundcloud_noise(page_title)
                if ' by ' in page_title:
                    p_t, p_a = (x.strip() for x in page_title.rsplit(' by ', 1))
                    if not title:
                        title = p_t
                    if not artist:
                        artist = p_a
                elif not title:
                    title = page_title

        if title and artist:
            meta = _canonical_metadata(artist, title)
            if meta:
                artist, title = meta['artist'], meta['title']
            return f"ytsearch1:{artist} {title} official audio"
        if title:
            meta = _canonical_metadata('', title)
            if meta:
                title = meta['title']
            return f"ytsearch1:{title} official audio"
    except Exception:
        pass

    # Last resort: build a query straight from the URL slug so a bare
    # /artist/track-title link still resolves to something searchable
    # instead of hard-erroring "could not extract metadata".
    try:
        parts = [p for p in urllib.parse.urlparse(raw_input).path.split('/') if p]
        if len(parts) >= 2:
            slug_artist = urllib.parse.unquote(parts[-2]).replace('-', ' ').strip()
            slug_title = urllib.parse.unquote(parts[-1]).replace('-', ' ').strip()
            if slug_title:
                meta = _canonical_metadata(slug_artist, slug_title)
                if meta:
                    slug_artist, slug_title = meta['artist'], meta['title']
                return f"ytsearch1:{slug_artist} {slug_title} official audio"
    except Exception:
        pass

    return None


def _embed_artwork_lossless(filepath, ext, status_callback, detail_callback,
                              source_ref=None, thumbnail_url=None):
    """Embed cover art into FLAC/Opus/Ogg using mutagen's native handlers."""
    callback = detail_callback or status_callback
    if mutagen is None:
        callback(f"Saved as {ext.lstrip('.').upper()} (mutagen not available for artwork).", "#f39c12")
        return

    # Try to find the thumbnail file that yt-dlp downloaded
    thumb_path = _find_thumbnail_for_mp3(filepath)
    image_data = None
    if thumb_path and os.path.exists(thumb_path):
        with open(thumb_path, 'rb') as f:
            image_data = f.read()
    elif thumbnail_url or source_ref:
        callback("Fetching artwork from source...", "#f39c12")
        url = thumbnail_url or _get_source_thumbnail_url(source_ref)
        if url:
            image_data = _fetch_thumbnail_bytes(url)

    if not image_data:
        callback(f"Saved as {ext.lstrip('.').upper()} (no thumbnail available).", "#f39c12")
        return

    mime, data = _normalize_image_bytes(image_data)
    if not data:
        callback(f"Saved as {ext.lstrip('.').upper()} (could not process artwork).", "#f39c12")
        return

    try:
        if ext in ('.flac',):
            from mutagen.flac import FLAC, Picture
            audio = FLAC(filepath)
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3  # Front cover
            pic.mime = mime
            pic.desc = 'Cover'
            pic.data = data
            audio.add_picture(pic)
            audio.save()
        elif ext in ('.opus', '.ogg'):
            from mutagen.oggopus import OggOpus
            from mutagen.flac import Picture
            import base64
            audio = OggOpus(filepath)
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = 'Cover'
            pic.data = data
            audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            audio.save()
        else:
            callback(f"Saved as {ext.lstrip('.').upper()} (format not supported for embedding).", "#f39c12")
            return

        callback(f"Artwork embedded into {os.path.basename(filepath)}.", "#2ecc71")

        # Clean up the thumbnail file if it exists
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
    except Exception as e:
        logger.warning("Lossless artwork embed failed: %s", e)
        callback(f"Saved as {ext.lstrip('.').upper()} (artwork embed failed: {e}).", "#f39c12")


def _save_sidecar_artwork(filepath, status_callback, detail_callback,
                           source_ref=None, thumbnail_url=None):
    """For WAV/AIFF: save the cover art as a sidecar JPEG next to the audio."""
    callback = detail_callback or status_callback
    base = os.path.splitext(filepath)[0]
    sidecar = base + ".jpg"

    thumb_path = _find_thumbnail_for_mp3(filepath)
    if thumb_path and os.path.exists(thumb_path):
        try:
            import shutil
            shutil.copy2(thumb_path, sidecar)
            callback(f"Saved as WAV. Cover art saved as {os.path.basename(sidecar)}.", "#2ecc71")
            try:
                os.remove(thumb_path)
            except Exception:
                pass
            return
        except Exception:
            pass

    # Try fetching from URL
    url = thumbnail_url or _get_source_thumbnail_url(source_ref)
    if url:
        raw = _fetch_thumbnail_bytes(url)
        if raw:
            mime, data = _normalize_image_bytes(raw)
            if data:
                with open(sidecar, 'wb') as f:
                    f.write(data)
                callback(f"Saved as WAV. Cover art saved as {os.path.basename(sidecar)}.", "#2ecc71")
                return

    callback(f"Saved as WAV (no cover art available).", "#f39c12")


def _report_mp3_artwork_status(
    download_folder: str,
    status_callback: Callable[[str, str], Any],
    detail_callback: Optional[Callable[[str, str], Any]] = None,
    source_ref: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    before_files: Optional[set] = None,
) -> None:
    mp3_path = _find_latest_mp3(download_folder, before_files=before_files)
    if mp3_path is None:
        callback = detail_callback or status_callback
        callback("Finished, but no audio file was found for artwork verification.", "#f39c12")
        return

    ext = os.path.splitext(mp3_path)[1].lower()
    if ext in ('.flac', '.opus', '.ogg'):
        _embed_artwork_lossless(mp3_path, ext, status_callback, detail_callback,
                                 source_ref, thumbnail_url)
        return
    elif ext in ('.wav', '.aiff'):
        _save_sidecar_artwork(mp3_path, status_callback, detail_callback,
                               source_ref, thumbnail_url)
        return

    if _embed_thumbnail_if_missing(mp3_path, status_callback, detail_callback):
        return

    # If no local thumbnail was produced (e.g. yt-dlp deleted it or couldn't
    # embed it), fetch the artwork directly from the source media's metadata.
    # Prefer a known thumbnail URL when the search already fetched one -- that
    # avoids yet another yt-dlp extraction just to look up the cover.
    if not _mp3_has_embedded_artwork(mp3_path):
        if thumbnail_url or source_ref:
            callback = detail_callback or status_callback
            callback("Fetching artwork from source metadata...", "#f39c12")
            thumb_url = thumbnail_url or _get_source_thumbnail_url(source_ref)
            raw = _fetch_thumbnail_bytes(thumb_url) if thumb_url else None
            if raw and _embed_artwork_bytes(mp3_path, raw):
                callback = detail_callback or status_callback
                callback(f"Artwork embedded successfully into {os.path.basename(mp3_path)}.", "#2ecc71")
                return

    if _mp3_has_embedded_artwork(mp3_path):
        callback = detail_callback or status_callback
        callback(f"Artwork embedded successfully into {os.path.basename(mp3_path)}.", "#2ecc71")
    else:
        callback = detail_callback or status_callback
        callback(
            "Download finished but no artwork was embedded. The source may not provide a thumbnail.",
            "#e74c3c",
        )


def _fallback_download_with_ytdlp_exe(
    input_query: str,
    output_template: str,
    audio_only: bool,
    status_callback: Callable[[str, str], Any],
    perf_cfg: Optional[dict] = None,
    format_override: Optional[str] = None,
    referer: str = 'https://www.youtube.com/',
) -> None:
    """Fallback download when yt_dlp module isn't available."""
    cfg = performance_config.copy()
    if isinstance(perf_cfg, dict):
        cfg.update(perf_cfg)

    cmd = [
        '--no-playlist',
        '--continue',
        '--retries', '5',
        '--fragment-retries', '10',
        '--socket-timeout', '20',
        '--hls-prefer-native',
        '--extractor-retries', '5',
        '--no-warnings',
        '--quiet',
        '--ignore-errors',
        '--geo-bypass',
        '--geo-bypass-country', 'US',
        '--force-ipv4',
        '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        # NOTE: no --extractor-args player_client override here either; see
        # build_fast_yt_dlp_options - pinned legacy clients cap YouTube quality.
        '--referer', referer,
        '-o', output_template,
    ]

    if cfg.get('use_aria2'):
        aria2c = get_fast_downloader_path()
        if aria2c:
            conn = _clamp_aria2_connections(cfg.get('aria2_connections', 16))
            # NOTE: never embed a space-containing value in this string. yt-dlp
            # splits --external-downloader-args on whitespace before invoking
            # aria2c, so a user-agent like
            #     Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/...
            # was chopped into separate tokens and aria2c treated "(Windows" as
            # a URI, aborting every download with:
            #   [download_helper.cc:451] errorCode=1
            #   Unrecognized URI or unsupported protocol: (Windows
            #   ERROR: aria2c exited with code 1
            # The Python API path passes these as a *list* so each element stays
            # one argv entry and it was unaffected -- which is why this only ever
            # broke the yt-dlp.exe fallback used by the packaged EXE.
            # yt-dlp already forwards its own --user-agent/--referer to aria2c as
            # --header arguments (see Aria2cFD._make_cmd), so repeating them here
            # is unnecessary as well as harmful.
            cmd += ['--external-downloader', 'aria2c',
                    '--external-downloader-args', f'-c -x {conn} -s {conn} -k 1M']

    # Format selection
    if format_override is not None:
        cmd += ['-f', format_override]
        if not audio_only:
            cmd += ['--merge-output-format', 'mp4', '--remux-video', 'mp4']
    elif audio_only:
        fmt = AUDIO_FORMATS.get(_CURRENT_AUDIO_FORMAT, AUDIO_FORMATS['mp3_vbr'])
        cmd += ['-f', 'bestaudio/best']
        cmd += ['-x', '--audio-format', fmt['codec']]
        if fmt['quality']:
            cmd += ['--audio-quality', fmt['quality']]
        # Raw WAV has no place for embedded covers/metadata via ID3 hooks.
        if mutagen is not None and fmt['codec'] != 'wav':
            cmd += ['--embed-thumbnail', '--add-metadata']
    else:
        cmd += ['-f', 'bestvideo*+bestaudio/best']
        cmd += ['--merge-output-format', 'mp4', '--remux-video', 'mp4']

    # Concurrency knobs: best-effort, older yt-dlp.exe may not support them
    concurrency_flags = []
    try:
        concurrency_flags += ['--concurrent-fragment-downloads', str(int(cfg.get('concurrent_fragment_downloads', 8)))]
    except Exception:
        pass
    try:
        concurrency_flags += ['--http-chunk-size', str(int(cfg.get('http_chunk_size', 1 << 20)))]
    except Exception:
        pass

    full_cmd = cmd + concurrency_flags + [input_query]

    status_callback("yt-dlp.exe downloading...", "#3498db")
    try:
        _run_yt_dlp_exe(full_cmd, status_callback=status_callback)
    except RuntimeError as e:
        err_msg = str(e)
        # Retry without the concurrency flags only when yt-dlp.exe genuinely does
        # not understand them (older build). The check must stay narrow: aria2c's
        # "Unrecognized URI or unsupported protocol" error also contains the word
        # "unrecognized", which used to trigger a pointless retry (with the same
        # broken aria2 arguments) instead of letting the aria2 fallback run.
        _lower = err_msg.lower()
        _unknown_option = ("no such option" in _lower
                           or "unrecognized arguments" in _lower)
        if concurrency_flags and _unknown_option and not _is_aria2_failure(err_msg):
            status_callback("Retrying without advanced concurrency flags (older yt-dlp.exe)...", "#f39c12")
            _run_yt_dlp_exe(cmd + [input_query], status_callback=status_callback)
        else:
            raise


# ==========================================
#   TAB 1 BACKEND: Downloader Pipeline
# ==========================================

def _score_yt_result(query_tokens: list, entry: dict, index: int) -> float:
    """Score how well a YouTube search result matches the user's query tokens.

    Higher is better. YouTube's own ordering is a strong prior, so we only move
    away from it when a later result clearly matches the query better or the top
    results are obvious non-original variants.
    """
    title = (entry.get('title') or '').lower()
    uploader = (entry.get('uploader') or entry.get('channel') or entry.get('creator') or '').lower()

    score = 0.0
    covered = 0
    for tok in query_tokens:
        if tok in title:
            score += 2.0
            covered += 1
        # An artist token appearing in the channel/uploader is a strong signal.
        if tok in uploader:
            score += 0.7

    # Reward how much of the query is covered by the title (avoids picking a
    # video that only matches one tiny word).
    if query_tokens:
        score += (covered / len(query_tokens)) * 1.5

    # Prefer the exact query phrase appearing in the title.
    clean = re.sub(r'\s+', ' ', title)
    if any(tok in clean for tok in query_tokens) and ' '.join(query_tokens[:3]) in clean:
        score += 1.0

    # Penalize obvious non-original variants -- but ONLY if the user did not
    # explicitly ask for that kind of video (e.g. "remix", "live").
    bad_words = ('cover', 'karaoke', 'instrumental', 'tribute', 'remix', 'slowed',
                 'reverb', 'nightcore', 'mashup', 'sped up', 'spedup', '10 hour',
                 '1 hour', 'hour loop', 'loop', 'official trailers', 'unplugged',
                 'acoustic', 'reaction', 'live session', '8d', 'lyrics', 'lyric',
                 'music video', 'visualizer', 'bass boosted', 'bassboosted',
                 'amped', 'slowed and reverb')
    for bad in bad_words:
        if bad in title and bad not in query_tokens:
            score -= 1.2
    if 'live' in title and 'live' not in query_tokens:
        score -= 0.8
    # Stronger penalty: these are fundamentally *different recordings* (a
    # session/alternate take), not just video variants -- so matching every
    # keyword should NOT let them beat the studio original.
    for alt in ('unplugged', 'acoustic', 'live session', 'mtv unplugged',
                'reaction', 'stripped', 'piano', 'pianoforte', 'orchestral'):
        if alt in title and alt not in query_tokens:
            score -= 3.0

    # Even the *clean* studio upload must beat lyric/visualizer/8D/remix-styled
    # re-uploads that happen to rank high. These format markers are never what
    # a music search wants when they aren't part of the query already.
    for fv in ('8d', 'lyrics', 'lyric', 'lyric video', 'music video',
               'visualizer', 'official video', 'official music video',
               'slowed and reverb', 'sped up', 'slowed', 'bass boosted'):
        if fv in title and fv not in query_tokens:
            score -= 2.0

    # More precise handling of processed/variant titles: "Alex G - Mary
    # (8D Audio)" contains every query word (alex/mary/audio) so it would
    # otherwise outrank the clean studio "Mary". Penalize extra title words the
    # query never asked for, and reward titles made of ONLY query words.
    title_words = [w for w in re.findall(r'\w+', title) if len(w) > 1]
    query_set = set(query_tokens)
    extra_words = [w for w in title_words if w not in query_set]
    if extra_words:
        score -= 0.8 * len(extra_words)
    if title_words and all(w in query_set for w in title_words):
        score += 0.8

    # Real songs usually run ~1.5-8 minutes; reward that range, penalize clips.
    duration = entry.get('duration') or 0
    if isinstance(duration, (int, float)):
        if 20 <= duration <= 480:
            score += 0.8
        elif duration and duration < 15:
            score -= 2.0

    # Prefer official uploads.
    if 'official' in title:
        score += 0.4
    if 'topic' in uploader:
        score += 0.6

    # Weak prior favoring YouTube's earlier (higher-ranked) results.
    score -= index * 0.6
    return score


# Words that add noise to a music search. Doubling as "official audio" for
# accuracy, but when nothing matches well they should not keep dragging the
# query down -- the simplified retry drops them entirely.
_SEARCH_FILLER = {
    "official", "audio", "video", "lyric", "lyrics", "song", "full",
    "hd", "4k", "hq", "feat", "ft", "with",
}

_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


def _tokenize(text) -> set:
    """Return the set of lowercase word tokens (len>1) in *text*."""
    return {t.lower() for t in re.findall(r"\w+", text or "") if len(t) > 1}


def _overlap(a: set, b: set) -> float:
    """Fraction of *b*'s tokens that appear in *a*."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(b)


def _http_get_json(url: str, timeout: float = 8.0):
    """GET *url* and parse JSON, or return None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _http_get_bytes(url: str, timeout: float = 8.0) -> Optional[bytes]:
    """GET *url* and return raw bytes, or None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _canonical_metadata(artist: str, title: str) -> Optional[dict]:
    """Best-effort canonical artist/title/duration/artwork via the iTunes Search API.

    The iTunes catalog is a clean source of the *correct* spelling of an
    artist + track name. Using it before a YouTube search fixes the classic
    "wrong song" failures (e.g. "Radiohead Karma Police" vs "Karma Police
    Remix"). Returns None on any error/empty so callers keep their existing
    fallback chain.
    """
    try:
        term = f"{artist} {title}".strip()
        if not term:
            return None
        url = ("{0}?media=music&entity=song&limit=8&term={1}".format(
            _ITUNES_SEARCH_URL, urllib.parse.quote(term)))
        data = _http_get_json(url, timeout=6.0)
        if not data or not data.get("results"):
            return None
        qart, qtit = _tokenize(artist), _tokenize(title)
        best, best_score = None, 0.5
        for r in data["results"]:
            rtit, rart = _tokenize(r.get("trackName")), _tokenize(r.get("artistName"))
            if not rtit and not rart:
                continue
            title_score = _overlap(rtit, qtit) if qtit else (0.4 if rtit else 0.0)
            artist_score = _overlap(rart, qart) if qart else (0.2 if rart else 0.0)
            score = 0.7 * title_score + 0.3 * artist_score
            if score > best_score:
                best_score = score
                best = r
        if not best:
            return None
        duration = None
        ms = best.get("trackTimeMillis")
        if isinstance(ms, (int, float)) and ms:
            duration = ms / 1000.0
        # iTunes artwork URL (replace '100x100' with '600x600' for high-res)
        artwork_url = best.get("artworkUrl100", "").replace("100x100", "600x600") if best.get("artworkUrl100") else ""
        return {
            "artist": (best.get("artistName") or artist).strip(),
            "title": (best.get("trackName") or title).strip(),
            "album": (best.get("collectionName") or "").strip(),
            "genre": (best.get("primaryGenreName") or "").strip(),
            "year": str(best.get("releaseDate", "")[:4]) if best.get("releaseDate") else "",
            "duration": duration,
            "artwork_url": artwork_url,
        }
    except Exception:
        return None


# Module-level store for the last downloaded track's metadata (artist, title, album).
# Set by the download paths after canonical lookup so _finalize_download can write
# proper ID3 tags into the resulting MP3/FLAC.
_last_dl_metadata: dict = {}


def set_last_dl_metadata(artist: str = "", title: str = "", album: str = "",
                         genre: str = "", year: str = "", artwork_path: str = "",
                         artwork_url: str = ""):
    """Record the canonical artist/title/album of the track currently being downloaded.
    
    If artwork_url is provided, the image is downloaded to a local file next to the
    download folder so it can be embedded into the file's tags.
    """
    global _last_dl_metadata
    # If artwork_url is provided but no local path, download it
    if artwork_url and not artwork_path:
        artwork_path = _download_artwork_to_cache(artwork_url)
    _last_dl_metadata = {
        "artist": artist, "title": title, "album": album,
        "genre": genre, "year": year, "artwork_path": artwork_path or "",
    }


def get_last_dl_metadata() -> dict:
    """Return the metadata for the most recent download (or an empty dict)."""
    return dict(_last_dl_metadata)


def clear_last_dl_metadata():
    """Clear the stored metadata (called after tags are written or on cancel)."""
    global _last_dl_metadata
    _last_dl_metadata = {}


def _download_artwork_to_cache(artwork_url: str) -> str:
    """Download artwork image to a writable per-user cache folder.

    The cache deliberately does NOT live next to the executable: the packaged
    app installs under ``C:\\Program Files``, which standard users cannot write
    to, so folder creation there failed and artwork caching silently gave up
    (no embedded covers). A per-user cache is always writable.
    """
    if not artwork_url:
        return ""
    try:
        data = _http_get_bytes(artwork_url, timeout=10.0)
        if not data:
            return ""
        # Save to a per-user cache folder
        cache_dir = os.path.join(_get_user_data_dir(), ".artwork_cache")
        os.makedirs(cache_dir, exist_ok=True)
        # Use a hash of the URL as filename to avoid re-downloading
        import hashlib
        url_hash = hashlib.md5(artwork_url.encode()).hexdigest()[:12]
        ext = ".jpg" if artwork_url.lower().endswith((".jpg", ".jpeg")) else ".png"
        cache_path = os.path.join(cache_dir, f"artwork_{url_hash}{ext}")
        if not os.path.exists(cache_path):
            with open(cache_path, "wb") as f:
                f.write(data)
        return cache_path
    except Exception:
        return ""


def write_id3_tags(filepath: str, artist: str = "", title: str = "", album: str = "",
                    genre: str = "", year: str = "", artwork_path: str = "") -> bool:
    """Write proper ID3 tags to an MP3/FLAC so Windows file details show artist/title.

    Uses mutagen. Safe to call with empty metadata (returns False). Also embeds
    album artwork from artwork_path (or a sidecar thumbnail next to the file).
    """
    if mutagen is None:
        return False
    if not filepath or not os.path.exists(filepath):
        return False
    if not artist and not title:
        return False

    ext = os.path.splitext(filepath)[1].lower()

    # Resolve artwork: explicit path → sidecar thumbnail → None
    resolved_artwork = artwork_path
    if not resolved_artwork or not os.path.exists(resolved_artwork):
        art_dir = os.path.dirname(filepath)
        base = os.path.splitext(os.path.basename(filepath))[0]
        for art_name in (base + ".jpg", base + ".png", base + ".jpeg",
                         "cover.jpg", "cover.png", "thumbnail.jpg", "thumbnail.png"):
            candidate = os.path.join(art_dir, art_name)
            if os.path.exists(candidate):
                resolved_artwork = candidate
                break

    try:
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TDRC, APIC, error
            try:
                audio = MP3(filepath)
            except Exception:
                return False
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            if tags is None:
                return False
            if title:
                tags["TIT2"] = TIT2(encoding=3, text=title)
            if artist:
                tags["TPE1"] = TPE1(encoding=3, text=artist)
            if album:
                tags["TALB"] = TALB(encoding=3, text=album)
            if genre:
                tags["TCON"] = TCON(encoding=3, text=genre)
            if year:
                tags["TDRC"] = TDRC(encoding=3, text=str(year))
            if resolved_artwork and os.path.exists(resolved_artwork):
                try:
                    with open(resolved_artwork, "rb") as art_f:
                        art_data = art_f.read()
                    # Match the MIME to the actual image so players render the
                    # cover (hardcoding image/jpeg made PNG album art blank).
                    art_lower = str(resolved_artwork).lower()
                    if art_lower.endswith(".png"):
                        _art_mime = "image/png"
                    elif art_lower.endswith((".webp", ".gif")):
                        _art_mime = "image/webp" if art_lower.endswith(".webp") else "image/gif"
                    else:
                        _art_mime = "image/jpeg"
                    tags["APIC"] = APIC(encoding=3, mime=_art_mime, type=3, desc="Cover", data=art_data)
                except Exception:
                    pass
            audio.save()
            return True

        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            from mutagen.id3 import error
            try:
                audio = FLAC(filepath)
            except Exception:
                return False
            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
            if album:
                audio["album"] = album
            if genre:
                audio["genre"] = genre
            if year:
                audio["date"] = str(year)
            # Embed album artwork
            if artwork_path and os.path.exists(artwork_path):
                try:
                    with open(artwork_path, "rb") as f:
                        art_data = f.read()
                    pic = Picture()
                    pic.data = art_data
                    pic.type = 3
                    pic.desc = "Cover"
                    pic.mime = "image/jpeg" if artwork_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
                    audio.add_picture(pic)
                except Exception:
                    pass
            audio.save()
            return True

        else:
            return False
    except Exception as e:
        logger.debug("write_id3_tags failed for %s: %s", filepath, e)
        return False


def _simplify_search(query_text: str) -> str:
    """Drop filler words so a follow-up search has more room to hit.

    e.g. 'Radiohead Karma Police official audio' -> 'Radiohead Karma Police'.
    """
    m = re.match(r"^ytsearch\d+:\s*(.*)$", query_text or "", re.IGNORECASE)
    text = m.group(1) if m else (query_text or "")
    tokens = [t for t in re.findall(r"\w+", text) if t.lower() not in _SEARCH_FILLER]
    return " ".join(tokens)


def _pick_best_entry(entries: list, query_tokens: list) -> Optional[dict]:
    """Pick the best-scoring YouTube search result, with a sanity gate.

    The existing scorer weights offsets toward "official / topic / earlier";
    we add one hard check on top: the chosen title must actually contain a
    meaningful fraction of the query tokens, otherwise it is a wrong match
    and we report "nothing found" so the caller can retry/fall back instead
    of silently downloading a completely different song.
    """
    best, best_score = None, float("-inf")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        score = _score_yt_result(query_tokens, entry, i)
        if score > best_score:
            best_score = score
            best = entry
    if best is None:
        return None
    meaningful = [t for t in query_tokens if t not in _SEARCH_FILLER]
    if meaningful:
        title = (best.get("title") or "").lower()
        covered = sum(1 for t in meaningful if t in title)
        if covered / len(meaningful) < 0.5:
            return None
    return best


def _prepare_search(query_text: str):
    """Normalize a query (or 'ytsearchN:...' spec) and produce the search term + tokens."""
    m = re.match(r'^ytsearch\d+:\s*(.*)$', query_text or '', re.IGNORECASE)
    if m:
        query_text = m.group(1).strip()
    query_text = (query_text or '').strip()
    # Append "official audio" so YouTube itself ranks the real track first --
    # but don't double it if the query already ends with it.
    if query_text and not re.search(r'\bofficial audio\b', query_text, re.IGNORECASE):
        search_term = f"{query_text} official audio".strip()
    else:
        search_term = query_text
    query_tokens = [t.lower() for t in re.findall(r'\w+', search_term) if len(t) > 1]
    return search_term, query_tokens


def _search_and_download_best(query_text: str, ydl_opts: dict) -> Optional[dict]:
    """Search for `query_text` and download the best match in ONE yt-dlp session.

    This is the fast path: it runs a `ytsearch5` extraction (so the accuracy
    scorer can pick the best result) and then downloads straight from the
    already-fetched entry via `process_ie_result` -- skipping the second
    network extraction that the old "resolve to URL, then download URL" flow
    performed.

    Improvement over the plain scorer: each candidate must actually contain a
    meaningful fraction of the query tokens (see _pick_best_entry). If the
    first search finds no trustworthy match, the query is retried once with
    filler words (official/audio/lyrics/...) removed so obscure or
    remix-suffixed tracks still resolve.

    Returns the chosen entry (so callers can report its title / use its known
    thumbnail without any extra lookups), or None on failure.
    """
    if yt_dlp is None or not query_text:
        return None
    search_term, query_tokens = _prepare_search(query_text)
    if not query_tokens:
        return None
    # Second attempt drops filler words ("official audio", "lyrics", "feat."...).
    # NOTE: build tokens directly -- _prepare_search() would re-append
    # "official audio", recreating the identical query.
    simplified = _simplify_search(search_term)
    queries = [(search_term, query_tokens)]
    if simplified and simplified != search_term:
        simple_tokens = [t.lower() for t in re.findall(r"\w+", simplified) if len(t) > 1]
        if simple_tokens:
            queries.append((simplified, simple_tokens))

    with _build_youtube_dl(ydl_opts) as ydl:
        for s_term, s_tokens in queries:
            try:
                # ytsearch3 (not 5): the SC-derived query is precise
                # (artist+title+"official audio"), so the right result is almost
                # always rank 1-2. Three candidates keep the scorer accurate while
                # cutting YouTube search response size/parse time.
                info = ydl.extract_info(f"ytsearch3:{s_term}", download=False)
            except Exception:
                continue
            if not isinstance(info, dict):
                continue
            entries = info.get('entries') or []
            best = _pick_best_entry(entries, s_tokens)
            if best is None:
                continue
            # Store canonical metadata from the chosen entry so the post-download
            # ID3 tag writer (ui._finalize_download) can tag the file with the
            # correct artist/title instead of the YouTube video title.
            try:
                best_title = best.get("title", "")
                best_artist = best.get("uploader", best.get("creator", best.get("channel", "")))
                if best_title:
                    set_last_dl_metadata(artist=best_artist, title=best_title)
            except Exception:
                pass
            # Download from the already-extracted info dict (no re-extraction).
            ydl.process_ie_result(best, download=True)
            return best
    return None


def download_track(
    raw_input: str,
    status_callback: Callable[[str, str], Any],
    success_callback: Callable[[str], Any],
    error_callback: Callable[[str], Any],
    progress_callback: Optional[Callable[[float], Any]] = None,
    detail_callback: Optional[Callable[[str, str], Any]] = None,
    soundcloud_direct_first: bool = True,
) -> None:
    base_dir = get_base_dir()
    download_folder = get_download_folder()
    output_template = os.path.join(download_folder, "%(title)s.%(ext)s")

    # Snapshot the folder so artwork targeting prefers files created by THIS
    # download rather than an unrelated newest MP3 (e.g. from another app).
    track_before_files = _snapshot_download_files(download_folder)

    # Resolve Spotify share-link variants (spotify.link shorts, spotify: URIs).
    raw_input = _normalize_spotify_url(raw_input)

    ydl_opts = build_fast_yt_dlp_options(base_dir, output_template, audio_only=True)
    # Sites like TikTok reject downloads unless the referer matches their CDN.
    _apply_platform_headers(ydl_opts, raw_input)

    is_url = raw_input.startswith(('http://', 'https://'))
    is_spotify = ('spotify.com/' in raw_input.lower()
                  or 'open.spotify.com/' in raw_input.lower()
                  or 'spotify.link/' in raw_input.lower())
    is_soundcloud = 'soundcloud.com/' in raw_input.lower() or 'snd.sc/' in raw_input.lower()

    # If Python module missing, use exe fallback for the actual download.
    python_can_download = yt_dlp is not None

    def _finish_audio_success(download_folder_name: str, source_ref: str,
                             thumbnail_url: Optional[str] = None) -> bool:
        """Only report success if this download actually produced a usable file."""
        mp3_path = _find_latest_mp3(download_folder_name, before_files=track_before_files)
        if not mp3_path:
            error_callback("Could not finish the audio download: no media file was produced.")
            return False
        # Safety net for the yt-dlp.exe fallback path (no info_dict hook): use
        # the produced filename as the title so tags/history aren't left blank.
        if not get_last_dl_metadata().get('title'):
            try:
                set_last_dl_metadata(title=Path(mp3_path).stem)
            except Exception:
                pass
        _report_mp3_artwork_status(
            download_folder_name,
            status_callback,
            detail_callback,
            source_ref=source_ref,
            thumbnail_url=thumbnail_url,
            before_files=track_before_files,
        )
        success_callback(download_folder_name)
        return True

    if is_soundcloud:
        # #2: A set/playlist URL should not be bulk-downloaded directly.
        #      Skip straight to the single-track YouTube fallback.
        is_sc_set = bool(_SOUNDCLOUD_SET_RE.search(raw_input))

        # Snapshot the folder so we can (a) verify a real file appeared and
        # (b) clean up any partial/junk a failed direct attempt may leave.
        sc_before_files = _snapshot_download_files(download_folder)

        # #5: Let users opt OUT of the direct-attempt via the soundcloud_direct_first pref.
        can_direct = soundcloud_direct_first and not is_sc_set

        sc_downloaded = False
        if can_direct:
            # Step 1: Try to download directly from SoundCloud first.
            status_callback("Attempting SoundCloud direct download...", "#f1c40f")
            try:
                if progress_callback:
                    ydl_opts['progress_hooks'] = [build_progress_hook(status_callback, progress_callback)]

                if python_can_download:
                    with _build_youtube_dl(ydl_opts) as ydl:
                        ydl.download([raw_input])
                else:
                    _fallback_download_with_ytdlp_exe(
                        raw_input,
                        output_template,
                        audio_only=True,
                        status_callback=status_callback,
                        perf_cfg=None,
                        referer='https://www.soundcloud.com/',
                    )

                # #3: Only treat as success if a brand-new media file appeared.
                after_files = _snapshot_download_files(download_folder)
                if after_files - sc_before_files:
                    sc_downloaded = True
            except DownloadCancelled:
                raise
            except Exception as e:
                # #4: Give a clearer explanation of why the direct attempt failed.
                logger.warning("SoundCloud direct download failed: %s", e)
                if _is_aria2_failure(e):
                    ydl_opts.pop('external_downloader', None)
                    ydl_opts.pop('external_downloader_args', None)
                e_str = (str(e) or '').lower()
                if '403' in e_str or 'forbidden' in e_str:
                    reason = "blocked"
                elif '429' in e_str or 'too many' in e_str:
                    reason = "rate-limited"
                elif 'timed out' in e_str or 'timeout' in e_str or 'getaddrinfo' in e_str:
                    reason = "network timeout"
                else:
                    reason = "unavailable"
                status_callback(
                    f"SoundCloud direct download {reason}. Trying YouTube fallback...",
                    "#f39c12",
                )
                # #1: Delete any partial/garbage files created by the failed attempt.
                _remove_new_media_files(download_folder, sc_before_files)

        elif is_sc_set:
            status_callback(
                "SoundCloud playlist/set detected — resolving to a single track on YouTube...",
                "#f39c12",
            )
        else:
            status_callback("Direct SoundCloud download disabled — using YouTube...", "#f39c12")

        if sc_downloaded:
            if _finish_audio_success(download_folder, raw_input):
                return
            return

        # Step 2 (fallback): SoundCloud download failed — search YouTube
        # for the track using metadata extracted from the SoundCloud link.
        status_callback("Reading SoundCloud Metadata...", "#f1c40f")
        try:
            search_query = _build_soundcloud_search_query(raw_input)
            if not search_query:
                raise RuntimeError('Could not extract metadata from SoundCloud link.')

            status_callback("Searching YouTube for SoundCloud track...", "#f1c40f")
            if progress_callback:
                ydl_opts['progress_hooks'] = [build_progress_hook(status_callback, progress_callback)]

            best = None
            if python_can_download:
                best = _search_and_download_best(search_query, ydl_opts)
                if best is None:
                    # Fast path failed (no search results / extraction error):
                    # fall back to letting yt-dlp download the search spec directly.
                    with _build_youtube_dl(ydl_opts) as ydl:
                        ydl.download([search_query])
            else:
                _fallback_download_with_ytdlp_exe(
                    search_query,
                    output_template,
                    audio_only=True,
                    status_callback=status_callback,
                    perf_cfg=None,
                )

            _report_mp3_artwork_status(
                download_folder,
                status_callback,
                detail_callback,
                source_ref=search_query,
                thumbnail_url=(best.get('thumbnail') if best else None),
                before_files=track_before_files,
            )
            success_callback(download_folder)
            return
        except DownloadCancelled:
            raise
        except Exception as e:
            error_callback(f"Could not process SoundCloud track info:\n{e}")
            return

    if is_spotify:
        status_callback("Reading Spotify Metadata...", "#f1c40f")
        try:
            search_query = _build_spotify_search_query(raw_input)
            if not search_query:
                raise RuntimeError('Could not extract metadata from Spotify link.')

            status_callback("Searching YouTube for Spotify track...", "#f1c40f")
            if progress_callback:
                ydl_opts['progress_hooks'] = [build_progress_hook(status_callback, progress_callback)]

            best = None
            if python_can_download:
                best = _search_and_download_best(search_query, ydl_opts)
                if best is None:
                    # Fast path failed (no search results / extraction error):
                    # fall back to letting yt-dlp download the search spec directly.
                    with _build_youtube_dl(ydl_opts) as ydl:
                        ydl.download([search_query])
            else:
                _fallback_download_with_ytdlp_exe(
                    search_query,
                    output_template,
                    audio_only=True,
                    status_callback=status_callback,
                    perf_cfg=None,
                )

            _report_mp3_artwork_status(
                download_folder,
                status_callback,
                detail_callback,
                source_ref=search_query,
                thumbnail_url=(best.get('thumbnail') if best else None),
                before_files=track_before_files,
            )
            success_callback(download_folder)
            return
        except DownloadCancelled:
            raise
        except Exception as e:
            error_callback(f"Could not process Spotify track info:\n{e}")
            return

    final_input = raw_input
    best_entry = None
    if not is_url:
        status_callback("Searching YouTube...", "#f1c40f")
        if progress_callback:
            ydl_opts['progress_hooks'] = [build_progress_hook(status_callback, progress_callback)]

        if python_can_download:
            # Fast path: search + download in one session (no double extraction).
            best_entry = _search_and_download_best(raw_input, ydl_opts)
        if best_entry is None:
            # Fast path unavailable/failed: fall back to a plain ytsearch1 spec
            # so the normal download path below still works.
            final_input = f"ytsearch1:{raw_input} official audio"
    else:
        status_callback("Downloading audio...", "#f1c40f")

    try:
        if best_entry is None:
            if not python_can_download:
                _fallback_download_with_ytdlp_exe(
                    final_input,
                    output_template,
                    audio_only=True,
                    status_callback=status_callback,
                    perf_cfg=None,
                    referer='https://www.tiktok.com/' if _TIKTOK_URL_RE.search(final_input) else 'https://www.youtube.com/',
                )
            else:
                if progress_callback:
                    ydl_opts['progress_hooks'] = [build_progress_hook(status_callback, progress_callback)]

                with _build_youtube_dl(ydl_opts) as ydl:
                    ydl.download([final_input])

        if not _finish_audio_success(
            download_folder,
            final_input,
            thumbnail_url=(best_entry.get('thumbnail') if best_entry else None),
        ):
            return
    except Exception as download_error:
        # Keep existing fallback behavior, but if yt_dlp missing, we still can try exe-based fallback.
        if ydl_opts.get('external_downloader') and _is_aria2_failure(download_error):
            status_callback("aria2 timed out. Retrying audio with the built-in downloader...", "#f39c12")
            try:
                ydl_opts_no_aria2 = dict(ydl_opts)
                ydl_opts_no_aria2.pop('external_downloader', None)
                ydl_opts_no_aria2.pop('external_downloader_args', None)
                if python_can_download:
                    with _build_youtube_dl(ydl_opts_no_aria2) as ydl:
                        ydl.download([final_input])
                else:
                    _fallback_download_with_ytdlp_exe(
                        final_input,
                        output_template,
                        audio_only=True,
                        status_callback=status_callback,
                        perf_cfg={'use_aria2': False},
                    )
                if _finish_audio_success(download_folder, final_input):
                    return
            except DownloadCancelled:
                raise
            except Exception:
                _remove_new_media_files(download_folder, track_before_files)
                _remove_new_thumbnail_files(download_folder, track_before_files)

        if is_url:
            # Check if this is a direct download URL (YouTube, TikTok, Instagram, etc.)
            is_direct_download = bool(
                _TIKTOK_URL_RE.search(raw_input) or
                _INSTAGRAM_URL_RE.search(raw_input) or
                'youtube.com/' in raw_input.lower() or
                'youtu.be/' in raw_input.lower() or
                'tiktok.com/' in raw_input.lower() or
                'instagram.com/' in raw_input.lower()
            )
            
            if is_direct_download:
                # The shared aria2 retry above has already run, if applicable.
                if _INSTAGRAM_URL_RE.search(raw_input):
                    _remove_new_media_files(download_folder, track_before_files)
                    _remove_new_thumbnail_files(download_folder, track_before_files)
                    error_callback(
                        _clean_error_text(
                            f"MP3 Error: Could not download from Instagram.\n\n{download_error}",
                            "MP3 Error: Could not download from Instagram. Instagram may require login or the post may be private.",
                        )
                    )
                    return
                status_callback("Direct audio download failed. Trying YouTube alternative...", "#f39c12")
            else:
                status_callback("Link protected. Reading track info...", "#f1c40f")
            track_title, artist_name = "", ""

            if python_can_download and yt_dlp is not None:
                try:
                    with yt_dlp.YoutubeDL({'quiet': True}) as ydl_meta:
                        info = ydl_meta.extract_info(raw_input, download=False)
                        track_title = info.get('title', '')
                        artist_name = info.get('artist', info.get('uploader', ''))
                except Exception:
                    pass
            else:
                # Derive a crude title/artist from URL; best-effort.
                url_parts = [p for p in raw_input.split('/') if p]
                if url_parts:
                    track_title = url_parts[-1].replace('-', ' ').replace('_', ' ').split('?')[0]
                    if len(url_parts) > 2:
                        artist_name = url_parts[-2].replace('-', ' ').replace('_', ' ')

            if not track_title:
                url_parts = [p for p in raw_input.split('/') if p]
                track_title = url_parts[-1].replace('-', ' ').replace('_', ' ').split('?')[0]
                artist_name = url_parts[-2].replace('-', ' ').replace('_', ' ') if len(url_parts) > 2 else ""

            fallback_text = f"{artist_name} {track_title}".strip()
            fallback_query = fallback_text or raw_input
            status_callback("Searching YouTube Alternative...", "#f1c40f")
            try:
                if not python_can_download:
                    _fallback_download_with_ytdlp_exe(
                        f"ytsearch1:{fallback_query} official audio",
                        output_template,
                        audio_only=True,
                        status_callback=status_callback,
                        perf_cfg=None,
                    )
                else:
                    if progress_callback:
                        ydl_opts['progress_hooks'] = [build_progress_hook(status_callback, progress_callback)]
                    # Fast path: search + download in one session.
                    best = _search_and_download_best(fallback_query, ydl_opts)
                    if best is None:
                        raise RuntimeError("No matching YouTube track was found.")

                if not _finish_audio_success(download_folder, fallback_query):
                    return
            except DownloadCancelled:
                raise
            except Exception:
                error_callback("Could not find any matching track. Please check the spelling and try again.")
        else:
            error_callback("Could not find any matching track. Please check the spelling and try again.")


# ==========================================
#   TAB 2 BACKEND: Slowed + Reverb DSP
# ==========================================
def process_studio_dsp(input_path, speed_factor, reverb_wet):
    """Manipulates audio with high-performance buffer settings for instant rendering."""
    if not input_path or not os.path.exists(input_path):
        return None, None

    # Ensure pedalboard and related IO are available
    if AudioFile is None or Pedalboard is None or Reverb is None:
        raise RuntimeError(
            "Missing dependency: 'pedalboard' and/or its components are required for DSP processing.\n"
            "Install it in your active environment with:\n"
            "    python -m pip install pedalboard"
        )

    with AudioFile(input_path) as f:
        audio_data = f.read(f.frames)
        sr = cast(int, f.samplerate)

    new_sample_rate = int(sr * speed_factor)

    board = Pedalboard([
        Reverb(room_size=0.5, wet_level=reverb_wet, dry_level=0.85)
    ])

    processed_data = board(audio_data, sr)
    return processed_data, new_sample_rate


def _get_ffmpeg_bin():
    return find_helper("ffmpeg", include_path=True)


def _get_ffprobe_bin():
    return find_helper("ffprobe", include_path=True)


def _probe_video_codec(filepath):
    import json
    ffprobe = _get_ffprobe_bin()
    if not ffprobe or not os.path.exists(filepath):
        return None
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,pix_fmt,bit_depth",
             "-of", "json", filepath],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **_no_window_kwargs(), timeout=30,
        )
        if completed.returncode != 0:
            return None
        data = json.loads(completed.stdout or "{}")
        return data.get("streams") or []
    except Exception:
        return None


def _needs_ae_reencode(filepath):
    streams = _probe_video_codec(filepath)
    if not streams:
        return False
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        return False
    vcodec = (video.get("codec_name") or "").lower()
    bd = int(video.get("bit_depth") or 0)
    pix = (video.get("pix_fmt") or "").lower()
    if vcodec in ("av01", "av1", "hevc", "h265", "vp9"):
        return True
    if vcodec in ("h264", "avc1") and (bd > 8 or (pix and "p10" in pix)):
        return True
    acodec = (audio.get("codec_name") or "").lower() if audio else ""
    if acodec == "opus":
        return True
    return False


def _reencode_for_ae(filepath):
    ffmpeg = _get_ffmpeg_bin()
    if not ffmpeg or not os.path.exists(filepath):
        return filepath
    tmp = filepath + ".ae_fix.mp4"
    try:
        completed = subprocess.run(
            [ffmpeg, "-y", "-i", filepath,
             "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
             "-pix_fmt", "yuv420p", "-crf", "18",
             "-c:a", "aac", "-b:a", "192k", "-ac", "2",
             "-movflags", "+faststart", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **_no_window_kwargs(), timeout=600,
        )
        if completed.returncode != 0:
            logger.warning("AE re-encode failed for %s", filepath)
            return filepath
        os.replace(tmp, filepath)
        return filepath
    except Exception as e:
        logger.warning("AE re-encode error for %s: %s", filepath, e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return filepath


def ensure_after_effects_compatibility(filepath, status_callback=None):
    if not filepath or not os.path.exists(filepath):
        return filepath
    if status_callback:
        try:
            status_callback("Checking video compatibility...", "#f39c12")
        except Exception:
            pass
    try:
        if _needs_ae_reencode(filepath):
            if status_callback:
                try:
                    status_callback("Re-encoding for After Effects compatibility...", "#f39c12")
                except Exception:
                    pass
            return _reencode_for_ae(filepath)
    except Exception as e:
        logger.debug("Compatibility check error: %s", e)
    return filepath


def _latest_video_file(folder, before):
    exts = (".mp4", ".webm", ".mkv", ".mov", ".m4v")
    try:
        files = [
            (os.path.getmtime(str(p)), str(p))
            for p in Path(folder).iterdir()
            if p.is_file() and p.suffix.lower() in exts
        ]
    except Exception:
        return None
    if before:
        new = [e for e in files if os.path.basename(e[1]) not in before]
        pool = new or files
    else:
        pool = files
    if not pool:
        return None
    mp4 = [e for e in pool if str(e[1]).lower().endswith(".mp4")]
    return max(mp4 or pool)[1]


def download_video_mp4(
    url: str,
    status_callback: Callable[[str, str], Any],
    success_callback: Callable[[str], Any],
    error_callback: Callable[[str], Any],
    progress_callback: Optional[Callable[[float], Any]] = None,
    ) -> None:
    download_folder = get_download_folder()
    ydl_opts = build_fast_yt_dlp_options(
        base_dir=get_base_dir(),
        output_template=os.path.join(download_folder, '%(title)s.%(ext)s'),
        audio_only=False,
    )
    # TikTok's CDN rejects downloads unless the referer points at tiktok.com.
    _apply_platform_headers(ydl_opts, url)
    is_tiktok = bool(_TIKTOK_URL_RE.search(url))
    referer = 'https://www.tiktok.com/' if is_tiktok else 'https://www.youtube.com/'
    video_files_before = _snapshot_download_files(download_folder)

    if progress_callback:
        ydl_opts['progress_hooks'] = [build_progress_hook(status_callback, progress_callback)]

    python_can_download = yt_dlp is not None

    def _try_download(opts):
        if not python_can_download:
            output_template = os.path.join(download_folder, '%(title)s.%(ext)s')
            _fallback_download_with_ytdlp_exe(
                url,
                output_template,
                audio_only=False,
                status_callback=status_callback,
                perf_cfg=None,
                format_override=opts.get('format'),
                referer=referer,
            )
            return True
        with _build_youtube_dl(opts) as ydl:
            ydl.download([url])
        return True

    try:
        status_callback("Downloading video...", "#f1c40f")
        _try_download(ydl_opts)
        # Make the output editable in After Effects (re-encode when needed).
        status_callback("Finalizing video...", "#3498db")
        _compat = _latest_video_file(download_folder, video_files_before)
        if _compat:
            ensure_after_effects_compatibility(_compat, status_callback)
        _remove_new_thumbnail_files(download_folder, video_files_before)
        success_callback(download_folder)
    except DownloadCancelled:
        raise
    except Exception as e:
        err_str = _clean_error_text(e)
        is_403 = '403' in err_str or 'Forbidden' in err_str

        # aria2 can time out on a CDN while yt-dlp's native downloader would
        # still succeed. Retry the exact URL without aria2 before reporting a
        # failure to the user.
        if ydl_opts.get('external_downloader') and _is_aria2_failure(err_str):
            status_callback("aria2 timed out. Retrying with the built-in downloader...", "#f39c12")
            try:
                ydl_opts_no_aria2 = dict(ydl_opts)
                ydl_opts_no_aria2.pop('external_downloader', None)
                ydl_opts_no_aria2.pop('external_downloader_args', None)
                _try_download(ydl_opts_no_aria2)
                _remove_new_thumbnail_files(download_folder, video_files_before)
                success_callback(download_folder)
                return
            except DownloadCancelled:
                raise
            except Exception as retry_error:
                err_str = _clean_error_text(retry_error, err_str)

        # For Instagram, show the actual error and don't try fallbacks
        if _INSTAGRAM_URL_RE.search(url):
            error_callback(
                _clean_error_text(
                    f"MP4 Error: Could not download from Instagram.\n\n{err_str or 'Instagram may require login or the post may be private.'}",
                    "MP4 Error: Could not download from Instagram.\n\nInstagram may require login or the post may be private.",
                )
            )
            return

        if is_403 and ydl_opts.get('external_downloader'):
            status_callback("Retrying without external downloader (403 blocked)...", "#f39c12")
            try:
                ydl_opts_no_ext = dict(ydl_opts)
                ydl_opts_no_ext.pop('external_downloader', None)
                ydl_opts_no_ext.pop('external_downloader_args', None)
                _try_download(ydl_opts_no_ext)
                success_callback(download_folder)
                return
            except Exception:
                pass

        # Retry with a different player client if 403 (YouTube blocks certain clients)
        if is_403:
            status_callback("Retrying with alternate player client (403 blocked)...", "#f39c12")
            try:
                ydl_opts_alt = dict(ydl_opts)
                ydl_opts_alt['extractor_args'] = {
                    'youtube': {
                        'player_client': ['tv_embedded', 'web_safari', 'mweb'],
                        'web_rtc': ['0'],
                    },
                }
                _try_download(ydl_opts_alt)
                success_callback(download_folder)
                return
            except Exception:
                pass

            # First fallback: try to discover direct media URLs on the page and download them directly.
        try:
            status_callback("Primary download failed. Searching page for direct media URLs...", "#f39c12")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read(200000)
                page_text = raw.decode('utf-8', errors='replace')
            except Exception:
                page_text = ''

            direct_urls = []
            try:
                direct_urls.extend(re.findall(r'https?://[^\"\'\s<>]+\.m3u8(?:\?[^\"\'\s<>]*)?', page_text, flags=re.IGNORECASE))
                direct_urls.extend(re.findall(r'https?://[^\"\'\s<>]+\.(?:mp4|webm)(?:\?[^\"\'\s<>]*)?', page_text, flags=re.IGNORECASE))
                direct_urls.extend(re.findall(r'file\s*[:=]\s*"(https?://[^\"]+)"', page_text, flags=re.IGNORECASE))
                direct_urls.extend(re.findall(r"file\s*[:=]\s*'(https?://[^']+)'", page_text, flags=re.IGNORECASE))
                direct_urls.extend(re.findall(r'"file"\s*:\s*"(https?://[^\"]+)"', page_text, flags=re.IGNORECASE))
            except Exception:
                direct_urls = []

            seen = set()
            candidates = []
            for u in direct_urls:
                if not u:
                    continue
                if u not in seen:
                    seen.add(u)
                    candidates.append(u)

            output_template = os.path.join(download_folder, '%(title)s.%(ext)s')

            for stream_url in candidates:
                try:
                    status_callback(f"Found direct media: {stream_url}", "#f1c40f")
                    if not python_can_download:
                        _fallback_download_with_ytdlp_exe(
                            stream_url,
                            output_template,
                            audio_only=False,
                            status_callback=status_callback,
                            perf_cfg=None,
                        )
                    else:
                        with _build_youtube_dl(ydl_opts) as ydl_direct:
                            ydl_direct.download([stream_url])
                    success_callback(download_folder)
                    return
                except Exception:
                    continue
        except Exception:
            pass

        # Fallback: try to discover a matching YouTube video by page title.
        try:
            status_callback("Primary download failed. Attempting fallback search...", "#f39c12")
            title = None

            if python_can_download and yt_dlp is not None:
                try:
                    with yt_dlp.YoutubeDL({'quiet': True}) as ydl_meta:
                        info = ydl_meta.extract_info(url, download=False)
                        title = info.get('title') if isinstance(info, dict) else None
                except Exception:
                    title = None
            else:
                # Best-effort: try HTML title tag.
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        raw = resp.read(65536)
                    html_snip = raw.decode('utf-8', errors='replace')
                    m = re.search(r'<title>(.*?)</title>', html_snip, flags=re.IGNORECASE | re.DOTALL)
                    if m:
                        title = re.sub(r'\s+', ' ', m.group(1)).strip()
                except Exception:
                    title = None

            if title:
                search_query = f"ytsearch1:{title}"
                if not python_can_download:
                    _fallback_download_with_ytdlp_exe(
                        search_query,
                        output_template,
                        audio_only=False,
                        status_callback=status_callback,
                        perf_cfg=None,
                    )
                else:
                    with _build_youtube_dl(ydl_opts) as ydl_search:
                        ydl_search.download([search_query])
                success_callback(download_folder)
                return

        except Exception:
            pass

        error_callback(f"MP4 Error: {e}")


# ==========================================
#   Cache Cleanup Utilities
# ==========================================

def _safe_remove_path(p: str) -> int:
    """Best-effort delete of files/dirs. Returns number of removed filesystem entries."""
    removed = 0
    if not p:
        return 0
    try:
        if os.path.isfile(p) or os.path.islink(p):
            os.remove(p)
            return 1
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p, topdown=False):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        os.remove(fp)
                        removed += 1
                    except Exception:
                        pass
                for name in dirs:
                    dp = os.path.join(root, name)
                    try:
                        os.rmdir(dp)
                    except Exception:
                        pass
            try:
                os.rmdir(p)
                removed += 1
            except Exception:
                pass
    except Exception:
        return removed
    return removed


def clear_app_cache():
    """Attempts to clear cache/temp folders created by this app/yt-dlp.

    Returns:
        dict with keys:
          - total_removed (int)
          - removed_paths (list[str])
          - total_freed_bytes (int) (best-effort)
    """
    removed_total = 0
    removed_paths: list[str] = []
    total_freed_bytes = 0

    # 1) yt-dlp cache (best guess)
    candidates: list[str] = []
    ytdlp_home = os.environ.get("YTDLP_HOME")
    if ytdlp_home:
        candidates.append(os.path.join(ytdlp_home, "cache"))

    # Common cache locations
    try:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "yt-dlp"))
            candidates.append(os.path.join(local_app_data, "yt-dlp", "cache"))
    except Exception:
        pass

    # Fallback: user cache dir (works on Windows too via HOME)
    home = str(Path.home())
    candidates.append(os.path.join(home, ".cache", "yt-dlp"))
    candidates.append(os.path.join(home, ".cache", "yt-dlp", "cache"))

    # 2) __pycache__ next to app code/executable
    try:
        base_dir = get_base_dir()
        for root, dirs, files in os.walk(base_dir):
            if "node_modules" in root:
                continue
            for d in dirs:
                if d == "__pycache__":
                    candidates.append(os.path.join(root, d))
    except Exception:
        pass

    # 3) Temporary download/extraction leftovers
    try:
        tmp_dir = tempfile.gettempdir()
        patterns = ["yt-dlp*", "youtube_dl*", "ffmpeg*", "*.part", "*.ytdlp*", "*.temp*"]
        for pat in patterns:
            for match in glob.glob(os.path.join(tmp_dir, pat)):
                candidates.append(match)
    except Exception:
        pass

    # De-dup
    unique = []
    seen = set()
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)

    for p in unique:
        if not os.path.exists(p):
            continue
        try:
            size_here = 0
            try:
                if os.path.isfile(p) or os.path.islink(p):
                    size_here = os.path.getsize(p)
                elif os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for name in files:
                            fp = os.path.join(root, name)
                            try:
                                size_here += os.path.getsize(fp)
                            except Exception:
                                pass
            except Exception:
                size_here = 0

            removed_here = _safe_remove_path(p)
            if removed_here > 0 or not os.path.exists(p):
                removed_total += removed_here
                removed_paths.append(p)
                total_freed_bytes += size_here
        except Exception:
            continue

    return {
        "total_removed": removed_total,
        "removed_paths": removed_paths,
        "total_freed_bytes": total_freed_bytes,
    }

