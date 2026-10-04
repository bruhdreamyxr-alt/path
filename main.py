"""Startup for the app: logging, single-instance check, then the window.

`python main.py` and `python ui.py` both work. ui.py keeps its own __main__ as a
one-line call into here, because UniversalAudioStudio.spec points PyInstaller at
ui.py - changing the spec's entry script would change what the frozen build
bundles, and that cannot be re-verified without the full Windows toolchain.
Everything that decides *how the app starts* lives in this file, so it is the
one place to look for it.
"""
import ctypes
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger("universal_audio_studio.main")


def _focus_running_instance() -> bool:
    """True when TuneLab was already running (its window has been raised).

    A second launch brings the running window to the front instead of starting a
    twin with its own queue and downloads. TUNELAB_MULTI_INSTANCE=1 bypasses it
    for development.
    """
    if sys.platform != "win32":
        return False
    if os.environ.get("TUNELAB_MULTI_INSTANCE") == "1":
        return False
    try:
        from ctypes import wintypes

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _u32 = ctypes.WinDLL("user32", use_last_error=True)
        _k32.CreateMutexW.argtypes = (
            ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        _k32.CreateMutexW.restype = wintypes.HANDLE
        _u32.EnumWindows.argtypes = (ctypes.c_void_p, ctypes.c_ssize_t)
        _u32.EnumWindows.restype = wintypes.BOOL
        _u32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        _u32.GetWindowTextLengthW.restype = ctypes.c_int
        _u32.GetWindowTextW.argtypes = (
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        _u32.GetWindowTextW.restype = ctypes.c_int
        _u32.IsIconic.argtypes = (wintypes.HWND,)
        _u32.IsIconic.restype = wintypes.BOOL
        _u32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        _u32.ShowWindow.restype = wintypes.BOOL
        _u32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        _u32.SetForegroundWindow.restype = wintypes.BOOL

        # ERROR_ALREADY_EXISTS (183): the mutex is there, so a TuneLab is
        # running. Its title may carry a download percentage, so the lookup
        # matches the "TuneLab" prefix.
        _k32.CreateMutexW(None, False, "TuneLab_SingleInstance_Mutex")
        if ctypes.get_last_error() == 183:
            _found: list[Any] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _find(hwnd, _lparam):
                n = _u32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _u32.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value.startswith("TuneLab"):
                        _found.append(hwnd)
                        return False  # stop enumerating
                return True

            _u32.EnumWindows(_find, 0)
            if _found:
                _hwnd = _found[0]
                if _u32.IsIconic(_hwnd):
                    _u32.ShowWindow(_hwnd, 9)  # SW_RESTORE
                _u32.SetForegroundWindow(_hwnd)
                return True
            # Mutex exists but no window was found (startup race): fall through
            # and launch normally rather than exiting blind.
    except Exception:
        logger.debug("Single-instance check failed", exc_info=True)
    return False


def run(main_window_factory: Optional[callable] = None) -> None:
    """Start the app. Imports ui lazily so the import cost lands here, once."""
    if main_window_factory is None:
        from ui import UniversalAudioStudio as main_window_factory
    if _focus_running_instance():
        return
    app = main_window_factory()
    app.mainloop()


if __name__ == "__main__":
    import ui  # noqa: F401  (configures logging before the window is built)

    ui._setup_logging()
    run()