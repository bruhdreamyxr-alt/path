"""Download Queue + History for TuneLab."""
import os
import sys
import json
import time
import shutil
import tempfile
import threading
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("universal_audio_studio.queue")

_DATA_FILE_NAMES = ("download_history.json", "download_queue.json")


def _legacy_data_dirs():
    """Directories older builds may have written runtime data to.

    Only used to migrate existing files into the new per-user location so
    users upgrading from an older build keep their history and queue.
    """
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(meipass)
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    return [d for d in dirs if d]


def _user_data_base() -> Optional[str]:
    """Return this platform's per-user data root, or ``None`` if unknown.

    Windows exposes ``%APPDATA%``/``%LOCALAPPDATA%``, but neither variable
    exists on macOS or Linux. A Windows-only lookup therefore finds nothing
    there and falls through to the folder next to the executable - which inside
    a macOS ``.app`` bundle is read-only once Gatekeeper applies App
    Translocation to a downloaded app, and writing to it also invalidates the
    ad-hoc code signature.

    So: prefer the Windows variables, then ``~/Library/Application Support`` on
    macOS, then ``$XDG_DATA_HOME`` (or ``~/.local/share``) on Linux/BSD.
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


def _get_app_data_dir() -> str:
    """Return a writable per-user directory for runtime data (history/queue).

    The packaged app installs under ``C:\\Program Files`` (via the Inno Setup
    installer), which is read-only without elevation. Writing history next to
    the executable there fails with PermissionError and the error was silently
    swallowed, so the History tab always looked empty. The platform's per-user
    data root (see :func:`_user_data_base`) is always writable for the current
    user, so prefer it and fall back to the executable or module folder only if
    it is unavailable.
    """
    base = _user_data_base()
    if base:
        target = os.path.join(base, "AudioDownloader")
        try:
            os.makedirs(target, exist_ok=True)
            return target
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _migrate_legacy_files(target_dir: str) -> None:
    """One-time copy of history/queue from older install locations.

    Guarded by a marker file so that clearing history (which deletes the JSON
    file) is never undone by a re-import on the next launch.
    """
    marker = os.path.join(target_dir, ".legacy_data_migrated")
    if os.path.exists(marker):
        return
    legacy_dirs = _legacy_data_dirs()
    for name in _DATA_FILE_NAMES:
        dest = os.path.join(target_dir, name)
        if os.path.exists(dest):
            continue
        for legacy in legacy_dirs:
            try:
                src = os.path.join(legacy, name)
                if os.path.abspath(src) == os.path.abspath(dest):
                    continue
                if not os.path.isfile(src):
                    continue
                shutil.copyfile(src, dest)
                logger.debug("Migrated %s from legacy location %s", name, legacy)
                break
            except Exception:
                continue
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        pass


APP_DATA_DIR = _get_app_data_dir()
_migrate_legacy_files(APP_DATA_DIR)
HISTORY_FILE = os.path.join(APP_DATA_DIR, "download_history.json")
QUEUE_FILE = os.path.join(APP_DATA_DIR, "download_queue.json")

# Guard all history/queue reads/writes so the queue worker thread and the UI thread
# can never corrupt the JSON files by writing at the same moment. Writes go to a
# temp file and are atomically renamed into place.
_STORE_LOCK = threading.Lock()


def _read_json_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _write_json_file(path, data):
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


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


def save_pending_queue(items):
    """Persist the given queue items (pending/active) atomically."""
    with _STORE_LOCK:
        _write_json_file(QUEUE_FILE, [item.to_dict() for item in items])


def load_pending_queue():
    """Load previously-persisted pending/active queue items (oldest first)."""
    return _read_json_file(QUEUE_FILE)


class QueueItem:
    __slots__ = ('url', 'is_video', 'status', 'filepath', 'error', 'title',
                 'started_at', 'completed_at', 'skip_existing', '_progress')
    url: str
    is_video: bool
    status: str
    filepath: Optional[str]
    error: Optional[str]
    title: Optional[str]
    started_at: Optional[float]
    completed_at: Optional[float]
    skip_existing: bool
    _progress: Optional[float]

    def __init__(self, url, is_video=False, skip_existing=True):
        self.url = url
        self.is_video = is_video
        self.status = "pending"
        self.filepath = None
        self.error = None
        self.title = None
        self.started_at = None
        self.completed_at = None
        self.skip_existing = skip_existing
        self._progress = None

    def to_dict(self):
        return {"url": self.url, "is_video": self.is_video, "status": self.status,
                "filepath": self.filepath, "error": self.error, "title": self.title,
                "started_at": self.started_at, "completed_at": self.completed_at}

    @classmethod
    def from_dict(cls, data: dict) -> "QueueItem":
        """Rebuild a QueueItem from a persisted dict (status forced to 'pending')."""
        item = cls(url=str(data.get("url", "")),
                   is_video=bool(data.get("is_video", False)),
                   skip_existing=bool(data.get("skip_existing", True)))
        item.title = data.get("title")
        # Always restart as pending regardless of what was saved, so a restored
        # queue actually re-downloads on launch.
        item.status = "pending"
        return item


class DownloadQueue:
    def __init__(self, download_fn, video_fn, find_file_fn, save_folder_fn, max_history=200):
        self._items = []
        self._lock = threading.Lock()
        self._running = False
        self._cancel_requested = False
        self._download_fn = download_fn
        self._video_fn = video_fn
        self._find_file_fn = find_file_fn
        self._save_folder_fn = save_folder_fn
        self._max_history = max_history
        self._on_item_complete = None
        self._on_queue_done = None
        self._on_progress = None
        self._active_index = -1

    @property
    def items(self):
        with self._lock: return list(self._items)

    def remove_at(self, index: int) -> bool:
        """Remove the item at *index* (only when not currently active).

        Returns True if an item was removed.
        """
        with self._lock:
            if not (0 <= index < len(self._items)):
                return False
            if self._items[index].status == "active":
                return False
            self._items.pop(index)
        self._persist()
        return True

    def retry_failed(self) -> int:
        """Reset any failed/skipped items back to pending so they re-download.

        Returns the number of items reset.
        """
        reset = 0
        with self._lock:
            for it in self._items:
                if it.status in ("failed", "skipped"):
                    it.status = "pending"
                    it.error = None
                    it.completed_at = None
                    reset += 1
        if reset:
            self._persist()
        return reset

    @property
    def active_index(self):
        with self._lock:
            return self._active_index

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return any(it.status in ("pending", "active") for it in self._items)

    def set_callbacks(self, on_item_complete=None, on_queue_done=None, on_progress=None):
        self._on_item_complete = on_item_complete
        self._on_queue_done = on_queue_done
        self._on_progress = on_progress

    def add(self, urls, is_video=False, skip_existing=True):
        added = 0
        with self._lock:
            for url in urls:
                url = url.strip()
                if url and url.startswith(('http://', 'https://')):
                    self._items.append(QueueItem(url, is_video, skip_existing))
                    added += 1
        if added:
            self._persist()
        return added

    def restore(self):
        """Load previously-persisted pending items into the queue.

        Returns the number of items restored. Safe to call once right after
        construction and before start().
        """
        restored = 0
        for data in load_pending_queue():
            if isinstance(data, dict) and data.get("url"):
                with self._lock:
                    self._items.append(QueueItem.from_dict(data))
                restored += 1
        if restored:
            logger.debug("Restored %d queued item(s) from previous session", restored)
        return restored

    def clear(self):
        with self._lock:
            self._items = [it for it in self._items if it.status == "active"]
        self._persist()

    def clear_completed(self) -> int:
        """Remove all done/failed/skipped items. Returns the number removed."""
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.status in ("pending", "active")]
            removed = before - len(self._items)
        if removed:
            self._persist()
        return removed

    def cancel(self):
        self._cancel_requested = True
        import downloader
        downloader.request_cancel_download()
        with self._lock:
            for it in self._items:
                if it.status == "pending": it.status = "skipped"

    def start(self):
        if self._running: return
        self._running = True
        self._cancel_requested = False
        threading.Thread(target=self._process_queue, daemon=True).start()

    def stop(self):
        self._cancel_requested = True
        import downloader
        downloader.request_cancel_download()

    def _already_downloaded(self, item):
        history = self._load_history()
        for entry in history:
            if entry.get("url") == item.url and entry.get("status") == "done":
                fp = entry.get("filepath", "")
                if not fp:
                    continue
                # Some history writers store just the destination FOLDER
                # instead of a file path (see ui._save_direct_download_to_history
                # in older builds). A folder always "exists", which made every
                # previously-downloaded URL permanently skip even after the
                # actual media file was deleted. Only treat a real, still-present
                # FILE as proof the download already exists on disk.
                if os.path.isfile(fp):
                    return True
        return False

    def _snapshot(self, folder):
        try: return {p.name for p in Path(folder).iterdir() if p.is_file()}
        except: return set()

    def _notify(self, item):
        if self._on_item_complete:
            try: self._on_item_complete(item.status, item)
            except: pass
        self._persist()

    def _persist(self):
        """Save current pending/active items so they survive an app restart."""
        with self._lock:
            pending = [it for it in self._items if it.status in ("pending", "active")]
        try:
            save_pending_queue(pending)
        except Exception as e:
            logger.warning("Queue persist failed (%s): %s", QUEUE_FILE, e)

    def _save_history(self):
        with _STORE_LOCK:
            try:
                history = _read_json_file(HISTORY_FILE)
                for item in self._items:
                    if item.status in ("done", "failed"):
                        entry = item.to_dict()
                        # Replace any older record for the same URL so file paths /
                        # timestamps stay accurate after a re-download.
                        for i, h in enumerate(history):
                            if h.get("url") == item.url:
                                history[i] = entry
                                break
                        else:
                            history.insert(0, entry)
                _write_json_file(HISTORY_FILE, history[:self._max_history])
            except Exception as e:
                logger.warning("History save failed (%s): %s", HISTORY_FILE, e)

    def _load_history(self):
        with _STORE_LOCK:
            return _read_json_file(HISTORY_FILE)

    def get_history(self):
        return self._load_history()

    def upsert_history_entry(self, entry: dict):
        """Insert or replace a history record for the same URL (thread-safe, atomic).

        Used by the UI after a direct (non-queue) download so the queue's own
        history writer never races it mid-write.
        """
        if not isinstance(entry, dict):
            return
        with _STORE_LOCK:
            try:
                history = _read_json_file(HISTORY_FILE)
                url = str(entry.get("url", ""))
                for i, h in enumerate(history):
                    if h.get("url") == url:
                        history[i] = entry
                        break
                else:
                    history.insert(0, entry)
                _write_json_file(HISTORY_FILE, history[:self._max_history])
            except Exception as e:
                logger.warning("History upsert failed (%s): %s", HISTORY_FILE, e)

    def clear_history(self):
        """Remove all saved download history entries."""
        with _STORE_LOCK:
            try:
                if os.path.exists(HISTORY_FILE):
                    os.remove(HISTORY_FILE)
                if os.path.exists(HISTORY_FILE + ".tmp"):
                    os.remove(HISTORY_FILE + ".tmp")
            except Exception as e:
                logger.warning("Clear history failed (%s): %s", HISTORY_FILE, e)

    def _process_queue(self):
        for idx, item in enumerate(self._items):
            if self._cancel_requested:
                if item.status == "pending": item.status = "skipped"
                continue
            if item.status != "pending": continue

            if item.skip_existing and self._already_downloaded(item):
                item.status = "skipped"
                item.completed_at = time.time()
                self._notify(item)
                continue

            item.status = "active"
            item.started_at = time.time()
            with self._lock:
                self._active_index = idx
            self._notify(item)

            before = self._snapshot(self._save_folder_fn())
            import downloader
            downloader.begin_download_session()

            def _on_progress(pct):
                if self._on_progress:
                    try:
                        self._on_progress(item, pct)
                    except Exception:
                        pass

            def _status_cb(text, color):
                if self._on_progress:
                    try:
                        self._on_progress(item, None, text)
                    except Exception:
                        pass

            try:
                if item.is_video:
                    self._video_fn(item.url, _status_cb, lambda f: None, lambda e: None, progress_callback=_on_progress)
                else:
                    self._download_fn(item.url, _status_cb, lambda f: None, lambda e: None, progress_callback=_on_progress)

                folder = self._save_folder_fn()
                after = self._snapshot(folder)
                new_files = after - before
                # Strip the "Mark of the Web" so Windows Defender doesn't
                # quarantine the file as a suspicious internet download.
                for name in new_files:
                    downloader._strip_zone_identifier(os.path.join(folder, name))

                # Pick the media file THIS download produced. The previous code
                # used sorted(new_files)[0], which is alphabetical: a thumbnail
                # (track.webp) or a .info.json sidecar sorted ahead of
                # track.mp3, so item.filepath pointed at an image and no ID3
                # tags were ever written to the audio.
                produced = downloader.pick_produced_media_file(folder, before)
                if produced:
                    item.filepath = produced
                    if not item.is_video:
                        # Write ID3 tags so Windows file details show
                        # artist/title for queued downloads too. Previously only
                        # the direct (non-queue) download path tagged anything.
                        meta = downloader.get_last_dl_metadata() or {}
                        if meta and str(produced).lower().endswith(('.mp3', '.flac')):
                            try:
                                downloader.write_id3_tags(
                                    produced,
                                    artist=meta.get("artist", ""),
                                    title=meta.get("title", ""),
                                    album=meta.get("album", ""),
                                    genre=meta.get("genre", ""),
                                    year=meta.get("year", ""),
                                    artwork_path=meta.get("artwork_path", ""),
                                )
                            except Exception as tag_err:
                                logger.debug("Queue ID3 tagging failed for %s: %s", produced, tag_err)
                        if meta.get("title"):
                            item.title = meta["title"]
                item.status = "done"
                item.completed_at = time.time()
            except Exception as e:
                if isinstance(e, downloader.DownloadCancelled):
                    item.status = "skipped"
                else:
                    item.status = "failed"
                    item.error = str(e)[:300]
                item.completed_at = time.time()

            with self._lock:
                self._active_index = -1
            self._notify(item)
            self._save_history()

        self._running = False
        self._save_history()
        if self._on_queue_done:
            self._on_queue_done()

