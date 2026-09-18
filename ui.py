import json
import os
import re
import subprocess
import tempfile
import concurrent.futures
import shutil
import threading
import queue
import logging
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING
import sys
import ctypes

logger = logging.getLogger("universal_audio_studio.ui")

try:
    import customtkinter as ctk
except Exception:
    # Provide a clear actionable error when running without the required UI package.
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing Dependency",
            "The required package 'customtkinter' is not installed in this environment.\n\n"
            "Install it using:\n\n"
            "    pip install customtkinter\n\n"
            "Then re-run the application."
        )
    except Exception:
        # If tkinter messagebox also fails for some reason, fallback to console.
        print(
            "Missing dependency: customtkinter. Install it with 'pip install customtkinter' and rerun."
        )
    sys.exit(1)

import downloader
import download_queue

if TYPE_CHECKING:
    # Provide typings to the language server without requiring the package at runtime.
    import vlc  # type: ignore


try:
    from PIL import Image, ImageSequence
except Exception:
    Image = None
    ImageSequence = None

# Heavy imports deferred until actually needed
sd = None
_vlc = None

def _lazy_import_sounddevice():
    global sd
    if sd is None:
        try:
            import sounddevice as _sd
            sd = _sd
        except ImportError:
            pass
    return sd

_np = None

def _lazy_import_numpy():
    global _np
    if _np is None:
        try:
            import numpy as _numpy
            _np = _numpy
        except Exception:
            pass
    return _np

def _lazy_import_vlc():
    global _vlc
    if _vlc is None:
        try:
            import vlc as __vlc  # type: ignore[reportMissingImports]
            _vlc = __vlc
        except Exception:
            pass
    return _vlc


# Helper for faster PySceneDetect execution in a separate process to avoid GIL/GUI freezes
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class UITheme:
    # Typography (base sizes; actual scaling handled by preferences)
    TITLE_FONT = ("Segoe UI", 24, "bold")
    SECTION_FONT = ("Segoe UI", 18, "bold")
    BODY_FONT = ("Segoe UI", 12)

    # Colors
    COLOR_PRIMARY = "#3498db"
    COLOR_SUCCESS = "#2ecc71"
    COLOR_DANGER = "#e74c3c"
    COLOR_WARNING = "#f39c12"
    COLOR_PURPLE = "#8e44ad"
    COLOR_GRAY = "#95a5a6"

    # Surfaces
    SURFACE_BG = "#2b2b2b"
    SIDEBAR_BG = "#1b2532"
    SIDEBAR_ACTIVE = "#2c3e50"
    SIDEBAR_HOVER = "#34495e"

    # Layout (base; scaled by prefs)
    PAD_X = 20
    PAD_Y_SMALL = 6
    PAD_Y_MED = 10

    # Collapsible sidebar dimensions
    SB_W_EXPANDED = 176
    SB_W_COLLAPSED = 54


# ==========================================================
# Monkeytype-style color themes
# Each theme: bg / surface / sidebar / sidebar_active / hover /
# accent (+hover) / text / sub / semantic (success/warning/danger/purple).
# 'mode' hints Light/Dark so form controls match the palette.
# ==========================================================
COLOR_THEMES = {
    'TuneLab Dark': {
        'bg': '#1e1e24', 'surface': '#2b2b2b', 'sidebar': '#1b2532',
        'sidebar_active': '#2c3e50', 'hover': '#34495e',
        'accent': '#3498db', 'accent_hover': '#2980b9',
        'text': '#ecf0f1', 'sub': '#95a5a6',
        'success': '#27ae60', 'success_hover': '#229954',
        'warning': '#f39c12', 'warning_hover': '#d68910',
        'danger': '#e74c3c', 'danger_hover': '#c0392b',
        'purple': '#8e44ad', 'purple_hover': '#7d3c98',
        'mode': 'dark',
    },
    'Serika Dark': {
        'bg': '#323437', 'surface': '#2c2e31', 'sidebar': '#2c2e31',
        'sidebar_active': '#3c4043', 'hover': '#3c4043',
        'accent': '#e2b714', 'accent_hover': '#c9a512',
        'text': '#d1d0c5', 'sub': '#646669',
        'success': '#9ece6a', 'success_hover': '#86c05b',
        'warning': '#e0af68', 'warning_hover': '#c99a55',
        'danger': '#f7768e', 'danger_hover': '#dd6578',
        'purple': '#bb9af7', 'purple_hover': '#a586dd',
        'mode': 'dark',
    },
    'Midnight': {
        'bg': '#0f1220', 'surface': '#161b2c', 'sidebar': '#121728',
        'sidebar_active': '#202842', 'hover': '#1e2740',
        'accent': '#5b8cff', 'accent_hover': '#4a78e0',
        'text': '#dde5f5', 'sub': '#5f6b85',
        'success': '#3fd08f', 'success_hover': '#36b67c',
        'warning': '#ffb454', 'warning_hover': '#e09c3f',
        'danger': '#ff5d73', 'danger_hover': '#e04f63',
        'purple': '#a78bfa', 'purple_hover': '#8f74e0',
        'mode': 'dark',
    },
    'Moon': {
        'bg': '#22243a', 'surface': '#2a2c46', 'sidebar': '#26283f',
        'sidebar_active': '#343655', 'hover': '#313350',
        'accent': '#b4b4fc', 'accent_hover': '#9c9ce8',
        'text': '#e4e4f4', 'sub': '#7c7ea3',
        'success': '#8fd6a4', 'success_hover': '#77bd8c',
        'warning': '#f0c987', 'warning_hover': '#d8b06c',
        'danger': '#ef8a9a', 'danger_hover': '#d76f81',
        'purple': '#c9b8ff', 'purple_hover': '#b09ceb',
        'mode': 'dark',
    },
    'Nord': {
        'bg': '#2e3440', 'surface': '#333b4a', 'sidebar': '#2b313c',
        'sidebar_active': '#3b4252', 'hover': '#434c5e',
        'accent': '#88c0d0', 'accent_hover': '#74aec0',
        'text': '#eceff4', 'sub': '#7f8ba0',
        'success': '#a3be8c', 'success_hover': '#90aa7b',
        'warning': '#ebcb8b', 'warning_hover': '#d4b574',
        'danger': '#bf616a', 'danger_hover': '#a8535c',
        'purple': '#b48ead', 'purple_hover': '#9e7a97',
        'mode': 'dark',
    },
    'Gruvbox Dark': {
        'bg': '#282828', 'surface': '#32302f', 'sidebar': '#282828',
        'sidebar_active': '#3c3836', 'hover': '#45403d',
        'accent': '#fabd2f', 'accent_hover': '#e3a91c',
        'text': '#ebdbb2', 'sub': '#928374',
        'success': '#b8bb26', 'success_hover': '#a4a71f',
        'warning': '#fe8019', 'warning_hover': '#e56f10',
        'danger': '#fb4934', 'danger_hover': '#e13c28',
        'purple': '#d3869b', 'purple_hover': '#bd6f84',
        'mode': 'dark',
    },
    'Dracula': {
        'bg': '#282a36', 'surface': '#2d2f3d', 'sidebar': '#21222c',
        'sidebar_active': '#44475a', 'hover': '#44475a',
        'accent': '#bd93f9', 'accent_hover': '#a67fd6',
        'text': '#f8f8f2', 'sub': '#6272a4',
        'success': '#50fa7b', 'success_hover': '#40d466',
        'warning': '#f1fa8c', 'warning_hover': '#d4dd72',
        'danger': '#ff5555', 'danger_hover': '#e04646',
        'purple': '#ff79c6', 'purple_hover': '#e066ad',
        'mode': 'dark',
    },
    'Tokyo Night': {
        'bg': '#1a1b26', 'surface': '#1f2335', 'sidebar': '#16161e',
        'sidebar_active': '#24283b', 'hover': '#292e42',
        'accent': '#7aa2f7', 'accent_hover': '#668ad6',
        'text': '#c0caf5', 'sub': '#565f89',
        'success': '#9ece6a', 'success_hover': '#86b655',
        'warning': '#e0af68', 'warning_hover': '#c8954f',
        'danger': '#f7768e', 'danger_hover': '#dd6078',
        'purple': '#bb9af7', 'purple_hover': '#a282dd',
        'mode': 'dark',
    },
    'Catppuccin Mocha': {
        'bg': '#1e1e2e', 'surface': '#25273a', 'sidebar': '#181825',
        'sidebar_active': '#313244', 'hover': '#45475a',
        'accent': '#89b4fa', 'accent_hover': '#7098d6',
        'text': '#cdd6f4', 'sub': '#6c7086',
        'success': '#a6e3a1', 'success_hover': '#8ec688',
        'warning': '#f9e2af', 'warning_hover': '#dcc792',
        'danger': '#f38ba8', 'danger_hover': '#d6738f',
        'purple': '#cba6f7', 'purple_hover': '#b08cd6',
        'mode': 'dark',
    },
    'Solarized Light': {
        'bg': '#fdf6e3', 'surface': '#eee8d5', 'sidebar': '#eee8d5',
        'sidebar_active': '#d6cdb7', 'hover': '#c9bfa5',
        'accent': '#268bd2', 'accent_hover': '#1f74b0',
        'text': '#073642', 'sub': '#586e75',
        'success': '#859900', 'success_hover': '#6f8000',
        'warning': '#b58900', 'warning_hover': '#9a7500',
        'danger': '#dc322f', 'danger_hover': '#c22a27',
        'purple': '#6c71c4', 'purple_hover': '#5a5eae',
        'mode': 'light',
    },
    'Gruvbox Light': {
        'bg': '#fbf1c7', 'surface': '#f2e5bc', 'sidebar': '#f2e5bc',
        'sidebar_active': '#e5d4a8', 'hover': '#d6c594',
        'accent': '#b57614', 'accent_hover': '#9a6410',
        'text': '#3c3836', 'sub': '#7c6f64',
        'success': '#79740e', 'success_hover': '#65600b',
        'warning': '#af3a03', 'warning_hover': '#943002',
        'danger': '#9d0006', 'danger_hover': '#850005',
        'purple': '#8f3f71', 'purple_hover': '#7a355f',
        'mode': 'light',
    },


    'Monokai': {
        'bg': '#272822', 'surface': '#2f3028', 'sidebar': '#24251f',
        'sidebar_active': '#3e3d32', 'hover': '#49483e',
        'accent': '#f92672', 'accent_hover': '#e01d61',
        'text': '#f8f8f2', 'sub': '#75715e',
        'success': '#a6e22e', 'success_hover': '#92ca24',
        'warning': '#e6db74', 'warning_hover': '#cfc765',
        'danger': '#ff5c57', 'danger_hover': '#e64b46',
        'purple': '#ae81ff', 'purple_hover': '#9568e8',
        'mode': 'dark',
    },
    'Metropolis': {
        'bg': '#100f12', 'surface': '#19181c', 'sidebar': '#141317',
        'sidebar_active': '#26242b', 'hover': '#211f26',
        'accent': '#f5c66b', 'accent_hover': '#dfae53',
        'text': '#e6e6e6', 'sub': '#6f6d75',
        'success': '#7ad2af', 'success_hover': '#63bd97',
        'warning': '#e6a532', 'warning_hover': '#cd8f24',
        'danger': '#ed5c65', 'danger_hover': '#d24952',
        'purple': '#b195ca', 'purple_hover': '#987cb0',
        'mode': 'dark',
    },
    'Horizon': {
        'bg': '#1c1e26', 'surface': '#252837', 'sidebar': '#191b24',
        'sidebar_active': '#2e3040', 'hover': '#31344a',
        'accent': '#ee6a8c', 'accent_hover': '#d55677',
        'text': '#d5d6da', 'sub': '#5b5f6e',
        'success': '#59d3b2', 'success_hover': '#47bb9c',
        'warning': '#f0975c', 'warning_hover': '#d67f45',
        'danger': '#e95678', 'danger_hover': '#cf4463',
        'purple': '#b877db', 'purple_hover': '#9f5fc2',
        'mode': 'dark',
    },
    'Laserbeam': {
        'bg': '#181c22', 'surface': '#212730', 'sidebar': '#161a20',
        'sidebar_active': '#242c37', 'hover': '#2b3441',
        'accent': '#5cf2ff', 'accent_hover': '#3fd9e6',
        'text': '#d8dee7', 'sub': '#5d6a78',
        'success': '#5cf2b4', 'success_hover': '#43d89a',
        'warning': '#ffd166', 'warning_hover': '#e6b94f',
        'danger': '#ff6b81', 'danger_hover': '#e64f66',
        'purple': '#c792ea', 'purple_hover': '#ad74d6',
        'mode': 'dark',
    },
    'Botanical': {
        'bg': '#141b16', 'surface': '#1c2620', 'sidebar': '#111814',
        'sidebar_active': '#22302a', 'hover': '#2a3a31',
        'accent': '#a3cfa4', 'accent_hover': '#8ab98b',
        'text': '#dce8dd', 'sub': '#5f7263',
        'success': '#8fce91', 'success_hover': '#79b67c',
        'warning': '#d9b56a', 'warning_hover': '#c19e51',
        'danger': '#e08c8c', 'danger_hover': '#c97272',
        'purple': '#b9a3d1', 'purple_hover': '#a189bc',
        'mode': 'dark',
    },
    'Velvet Purple': {
        'bg': '#1a1424', 'surface': '#241c30', 'sidebar': '#16101e',
        'sidebar_active': '#3a2e52', 'hover': '#2e2442',
        'accent': '#a855f7', 'accent_hover': '#9333ea',
        'text': '#e9dff5', 'sub': '#7e6f92',
        'success': '#4ade80', 'success_hover': '#22c55e',
        'warning': '#fbbf24', 'warning_hover': '#f59e0b',
        'danger': '#f87171', 'danger_hover': '#ef4444',
        'purple': '#c084fc', 'purple_hover': '#a855f7',
        'mode': 'dark',
    },
    'Pure Purple': {
        'bg': '#1a0a2e', 'surface': '#2d1b4e', 'sidebar': '#150826',
        'sidebar_active': '#4c1d95', 'hover': '#3b2670',
        'accent': '#a855f7', 'accent_hover': '#9333ea',
        'text': '#f3e8ff', 'sub': '#937db8',
        'success': '#c084fc', 'success_hover': '#a855f7',
        'warning': '#e879f9', 'warning_hover': '#d946ef',
        'danger': '#f472b6', 'danger_hover': '#ec4899',
        'purple': '#e879f9', 'purple_hover': '#d946ef',
        'mode': 'dark',
    },
    'Crimson': {
        'bg': '#1a1012', 'surface': '#241618', 'sidebar': '#160d0f',
        'sidebar_active': '#3a1f23', 'hover': '#2e191d',
        'accent': '#ef4444', 'accent_hover': '#dc2626',
        'text': '#f5e6e8', 'sub': '#927075',
        'success': '#4ade80', 'success_hover': '#22c55e',
        'warning': '#fbbf24', 'warning_hover': '#f59e0b',
        'danger': '#f87171', 'danger_hover': '#ef4444',
        'purple': '#c084fc', 'purple_hover': '#a855f7',
        'mode': 'dark',
    },
    'Soft Lilac Light': {
        'bg': '#f5f0fa', 'surface': '#ffffff', 'sidebar': '#ebe3f2',
        'sidebar_active': '#d6c8e6', 'hover': '#e0d2ec',
        'accent': '#9333ea', 'accent_hover': '#7e22ce',
        'text': '#2d1f3d', 'sub': '#7c6a8f',
        'success': '#16a34a', 'success_hover': '#15803d',
        'warning': '#d97706', 'warning_hover': '#b45309',
        'danger': '#dc2626', 'danger_hover': '#b91c1c',
        'purple': '#a855f7', 'purple_hover': '#9333ea',
        'mode': 'light',
    },
    'Serika Light': {
        'bg': '#e8e8e8', 'surface': '#f4f4f4', 'sidebar': '#dddddd',
        'sidebar_active': '#cccccc', 'hover': '#d4d4d4',
        'accent': '#444444', 'accent_hover': '#2f2f2f',
        'text': '#323437', 'sub': '#7e8182',
        'success': '#3f9b6e', 'success_hover': '#35855d',
        'warning': '#c9952f', 'warning_hover': '#b07f24',
        'danger': '#c94949', 'danger_hover': '#ad3c3c',
        'purple': '#7d5bb5', 'purple_hover': '#694a9e',
        'mode': 'light',
    },
}

# Legacy hardcoded hexes -> palette role (used to recolor existing widgets).
LEGACY_HEX_ROLES = {
    '#3498db': 'accent', '#2980b9': 'accent_hover',
    '#27ae60': 'success', '#229954': 'success_hover', '#2ecc71': 'success',
    '#f39c12': 'warning', '#d68910': 'warning_hover',
    '#e74c3c': 'danger', '#c0392b': 'danger_hover',
    '#8e44ad': 'purple', '#7d3c98': 'purple_hover',
    '#95a5a6': 'sub', '#646669': 'sub',
    '#ecf0f1': 'text', '#d1d0c5': 'text', '#bdc3c7': 'text',
    '#1b2532': 'sidebar', '#1f2a3a': 'sidebar',
    '#2c3e50': 'sidebar_active', '#34495e': 'hover',
    '#2b2b2b': 'surface',
}


class UniversalAudioStudio(ctk.CTk):
    # -----------------
    # Persistent UI Preferences
    # -----------------
    _PREF_FILENAME = "ui_prefs.json"

    def _get_pref_path(self) -> str:
        """Store prefs in the per-user data folder when packaged, beside ui.py
        when running from source.

        The Program Files install directory is not writable by standard users,
        so storing next to the EXE would silently drop every preference.
        Deliberately delegates to ``downloader._get_user_data_dir()`` so prefs
        land in the same folder as history and the caches on every platform
        (``%APPDATA%`` on Windows, ``~/Library/Application Support`` on macOS).
        """
        if getattr(sys, "frozen", False):
            base_dir = downloader._get_user_data_dir()
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, self._PREF_FILENAME)

    def _default_prefs(self) -> dict:
        return {
            "theme_mode": "Dark",
            "accent_theme": "blue",
            "overlay_enabled": False,
            "compact_enabled": False,
            "opacity_enabled": False,
            "opacity_alpha": 1.0,
            "background_style": "gif",  # none | gif | solid
            "background_gif_path": "",
            "background_solid_color": "#2b2b2b",
            "font_scale": 1.0,
            "padding_scale": 1.0,
            "disable_maximize": True,
            "nav_animation_enabled": True,
            "nav_animation_speed": 12,
            "sidebar_collapsed": False,
            "color_theme": "TuneLab Dark",
            "soundcloud_direct_first": True,
            "save_folder": "",
            "audio_format": "mp3_vbr",
            "filename_template": "",
        }

    def _load_prefs(self) -> dict:
        path = self._get_pref_path()
        defaults = self._default_prefs()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    defaults.update({k: v for k, v in data.items() if k in defaults})
        except Exception:
            return defaults
        return defaults

    def _save_prefs(self) -> None:
        try:
            path = self._pref_path
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._prefs, f, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            # Never crash the UI over a pref write, but do leave a trace: a
            # silently unwritable prefs path would drop every setting.
            logger.warning("Could not save preferences to %s: %s", path, e)

    def _set_pref(self, key: str, value) -> None:
        if key not in self._prefs:
            self._prefs[key] = value
        else:
            self._prefs[key] = value
        self._save_prefs()

    def _apply_prefs_to_ctk(self) -> None:
        try:
            ctk.set_appearance_mode(self._prefs.get("theme_mode", "Dark") or "Dark")
        except Exception:
            pass
        try:
            ctk.set_default_color_theme(self._prefs.get("accent_theme", "blue") or "blue")
        except Exception:
            pass
        try:
            font_scale = float(self._prefs.get("font_scale", 1.0) or 1.0)
            ctk.set_widget_scaling(font_scale)
        except Exception:
            pass

    def _apply_prefs_to_window(self) -> None:
        try:
            enabled = bool(self._prefs.get("opacity_enabled", False))
            alpha = float(self._prefs.get("opacity_alpha", 1.0) or 1.0)
            # Always route through the helper: on Windows an exact 1.0 alpha
            # opts OUT of DWM's composited/double-buffered presentation, which
            # lets unpainted intermediate states flash black on screen. A
            # 0.999 alpha is visually identical but keeps compositing active.
            if enabled:
                self._apply_window_alpha(alpha)
            else:
                self._apply_window_alpha(1.0)
        except Exception:
            pass

    def _apply_window_alpha(self, alpha: float) -> None:
        """Set window opacity, keeping DWM double-buffering alive on Windows."""
        try:
            a = float(alpha)
            win32 = str(self.tk.call('tk', 'windowingsystem')) == 'win32'
            if win32 and a >= 0.999:
                a = 0.999
            self.wm_attributes('-alpha', a)
        except Exception:
            pass
        # Apply window constraints such as disabling maximize if requested
        try:
            if bool(self._prefs.get("disable_maximize", False)):
                self._disable_maximize(True)
            else:
                self._disable_maximize(False)
        except Exception:
            pass

    def _disable_maximize(self, disable: bool = True) -> None:
        """On Windows, remove the maximize box and thick frame to prevent fullscreen/maximize.

        This is a best-effort change using Win32 APIs; non-Windows platforms are ignored.
        """
        if sys.platform != "win32":
            try:
                # Tkinter fullscreen attribute as fallback
                self.wm_attributes("-fullscreen", False if disable else False)
            except Exception:
                pass
            return

        try:
            GWL_STYLE = -16
            WS_MAXIMIZEBOX = 0x00010000
            WS_THICKFRAME = 0x00040000
            hwnd = int(self.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            if disable:
                style = style & ~WS_MAXIMIZEBOX & ~WS_THICKFRAME
            else:
                style = style | WS_MAXIMIZEBOX | WS_THICKFRAME
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
            # Apply the change
            SWP_NOMOVE = 0x2
            SWP_NOSIZE = 0x1
            SWP_NOZORDER = 0x4
            SWP_FRAMECHANGED = 0x20
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception:
            pass

    def _apply_prefs_to_background(self) -> None:

        style = str(self._prefs.get("background_style", "gif") or "gif").lower()
        gif_path = str(self._prefs.get("background_gif_path", "") or "")
        solid_color = str(self._prefs.get("background_solid_color", UITheme.SURFACE_BG) or UITheme.SURFACE_BG)

        if style == "none":
            self.clear_background()
            return

        if style == "solid":
            self.clear_background()
            try:
                # Set background label color by using CTkLabel's bg via place.
                self.bg_label.configure(text="", fg_color=solid_color)  # type: ignore[arg-type]
                self.bg_label.lift()
            except Exception:
                pass
            return

        # Default: GIF
        if gif_path and os.path.exists(gif_path):
            try:
                # Load GIF frames without opening a dialog.
                if Image is None:
                    return
                pil_img = Image.open(gif_path)
                self.update_idletasks()
                w = max(self.winfo_width(), 520)
                h = max(self.winfo_height(), 520)
                size = (w, h)

                frames = []
                if ImageSequence is not None:
                    for frame in ImageSequence.Iterator(pil_img):
                        f = frame.convert('RGBA')
                        ctk_img = ctk.CTkImage(light_image=f, dark_image=f, size=size)
                        frames.append(ctk_img)

                if frames:
                    self.bg_frames = frames
                    self.bg_frame_index = 0
                    self.bg_enabled = True
                    self.bg_status.configure(text=os.path.basename(gif_path), text_color=UITheme.COLOR_SUCCESS)
                    self.bg_label.configure(image=self.bg_frames[0])
                    self.bg_label.lower()
                    self.start_background_animation()
            except Exception:
                # If GIF fails to load, just clear.
                self.clear_background()
        else:
            # Missing GIF path => clear
            self.clear_background()

    def __init__(self):
        # Thread-dispatch state must exist BEFORE super().__init__() so the
        # overridden after() (below) is safe even while CTk builds the widget.
        self._ui_closed = False
        self._ui_queue = queue.Queue()
        self._tk_main_thread = threading.current_thread()

        super().__init__()

        # Start the main-thread poller that drains worker-queued UI callbacks.
        # It runs once the main loop gets control and keeps re-arming itself.
        try:
            super().after(25, self._drain_ui_queue)
        except Exception:
            pass

        # -----------------
        # UI Preferences
        # -----------------
        self._pref_path = self._get_pref_path()
        self._prefs = self._load_prefs()

        # Cache derived scales (recomputed on apply)
        self._font_scale = float(self._prefs.get("font_scale", 1.0) or 1.0)
        self._padding_scale = float(self._prefs.get("padding_scale", 1.0) or 1.0)

        # Active color palette (Monkeytype-style theme engine)
        self._color_theme_name = self._prefs.get("color_theme", "TuneLab Dark")
        if self._color_theme_name not in COLOR_THEMES:
            self._color_theme_name = "TuneLab Dark"
        self._palette = dict(COLOR_THEMES[self._color_theme_name])


        # Apply CTk global scaling/theme early (before building widgets)
        self._apply_prefs_to_ctk()

        # Make window opacity reflect saved prefs early
        self._apply_prefs_to_window()

        # Pylance: declare dynamic attributes up-front to avoid "attribute of None" / unknown attribute errors.

        self._vlc_instance = None
        self._vlc_player = None
        self._preview_proc = None
        self._clipper_vlc_instance = None
        self._clipper_vlc_player = None
        self._clipper_auto_select = False
        self._clipper_fast_detect = False
        self._clipper_hover_preview_enabled = True
        self._clipper_hover_preview_muted = True
        self._clipper_thumb_generated = set()

        self.title("TuneLab")
        # Let packed widgets determine natural size; no fixed geometry.
        self.minsize(800, 680)

        # Set window background to match theme (prevents white border)
        bg_color = self._palette.get('bg', '#2b2b2b')
        self.configure(bg=bg_color, highlightthickness=0, bd=0)
        try:
            self.attributes('-bg', bg_color)
        except Exception:
            pass
        # Also set via Tkinter's configure to ensure it sticks
        try:
            self.tk.configure(bg=bg_color)
        except Exception:
            pass

        # Handle window close event to stop the video bridge server
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        self.studio_file_path = None

        # -----------------
        # Collapsible sidebar navigation (slides in/out)
        # -----------------
        self._sidebar_expanded = not bool(self._prefs.get("sidebar_collapsed", False))
        self._sb_anim_after_id = None
        self.sidebar = ctk.CTkFrame(
            self,
            width=UITheme.SB_W_EXPANDED if self._sidebar_expanded else UITheme.SB_W_COLLAPSED,
            corner_radius=0,
            fg_color=UITheme.SIDEBAR_BG,
        )
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._sidebar_cur_w = UITheme.SB_W_EXPANDED if self._sidebar_expanded else UITheme.SB_W_COLLAPSED

        # -----------------
        # Main area (header + pages)
        # -----------------
        self.main_container = ctk.CTkFrame(self, fg_color=self._palette.get('bg', UITheme.SURFACE_BG))
        self.main_container.pack(side="left", fill="both", expand=True)

        self._header_frame = ctk.CTkFrame(self.main_container, fg_color="transparent", height=42)
        self._header_frame.pack(fill="x", padx=18, pady=(14, 0))
        self.title_lbl = ctk.CTkLabel(
            self._header_frame,
            text="",
            font=UITheme.SECTION_FONT,
            anchor="w",
        )
        self.title_lbl.pack(side="left")

        # Central content area (pages are swapped inside here)
        self.content_frame = ctk.CTkFrame(self.main_container, fg_color=UITheme.SURFACE_BG, corner_radius=20)
        self.content_frame.pack(
            pady=10,
            padx=14,
            fill="both",
            expand=True,
        )

        self.tab_downloader = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_studio = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_customization = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_performance = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_queue = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_history = ctk.CTkFrame(self.content_frame, fg_color="transparent")

        # --- Download queue manager (lazy import to avoid circular deps) ---
        self._queue_manager = None
        self._queue_initialized = False

        self.bg_label = ctk.CTkLabel(self, text="", corner_radius=0)
        self.bg_label.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.bg_label.lower()

        self.bg_frames = []
        self.bg_frame_index = 0
        self.bg_animation_id = None
        self.bg_enabled = False

        self.build_downloader_view()
        self.build_studio_view()
        self.build_customization_view()
        self.build_performance_view()
        self.build_queue_view()
        self.build_history_view()

        # -----------------
        # Sidebar contents (toggle, brand, nav items, accent indicator)
        # -----------------
        try:
            self.nav_anim_enabled = bool(self._prefs.get('nav_animation_enabled', True))
            self.nav_anim_speed = int(self._prefs.get('nav_animation_speed', 12) or 12)
        except Exception:
            self.nav_anim_enabled = True
            self.nav_anim_speed = 12

        # Saved download settings (#1 save folder, #2 audio format).
        try:
            _saved_folder = str(self._prefs.get('save_folder', '') or '').strip()
            if _saved_folder:
                downloader.set_download_folder(_saved_folder)
            downloader.set_audio_format(str(self._prefs.get('audio_format', 'mp3_vbr')))
        except Exception:
            logger.debug("Applying download prefs failed", exc_info=True)

        # Apply saved performance prefs at startup so the "fast downloader"
        # settings survive restarts and are active before the user opens the
        # Performance tab.
        try:
            _perf_cfg = downloader.perf_cfg_from_prefs(self._prefs)
            downloader.set_performance_config(_perf_cfg)
            # Register the speed label callback for live download speed updates
            try:
                downloader.set_speed_ui_callback(self.update_speed_label)
            except Exception:
                logger.debug("Failed to register speed UI callback", exc_info=True)
        except Exception:
            logger.debug("Applying performance prefs failed", exc_info=True)

        # Proactive yt-dlp staleness check (#3): background, non-blocking.
        self._start_worker(self._ytdlp_update_check_worker)

        self._build_sidebar()
        self._nav_anim_after_id = None
        self.after(200, self._position_nav_indicator_initial)

        # Apply saved runtime preferences to controls + visuals
        # (controls are created in build_customization_view / apply_* methods)
        self._apply_prefs_to_window()
        self._apply_prefs_to_background()

        # Auto-check for app updates (non-blocking, shows banner if available)
        self.after(3000, self._check_updates_on_startup)

        # Ensure the whole window background matches solid style preference
        # and apply the saved Monkeytype-style color theme to every widget
        self.apply_color_theme(self._color_theme_name)

        # Collapse/expand shortcut (like VS Code's side bar)
        self.bind('<Control-b>', lambda e: self.toggle_sidebar())

        # Start by showing downloader
        self.show_frame('downloader')

    def _start_worker(self, target):
        threading.Thread(target=target, daemon=True).start()

    def after(self, ms, func=None, *args, **kwargs):
        """Thread-safe ``after``: worker threads never touch Tk directly.

        Calls made on the Tk owner (main) thread pass through to the normal
        ``after`` so timer IDs and ``after_cancel`` keep working exactly as
        before. Calls from background threads are marshalled onto the main
        thread via ``_ui_queue``; the requested delay (if any) is honored
        with a real main-thread timer. If the app is shutting down the call
        is dropped instead of crashing Tkinter.
        """
        if func is None:
            return None
        if threading.current_thread() is self._tk_main_thread:
            return super().after(ms, func, *args, **kwargs)
        if self._ui_closed:
            return None
        try:
            self._ui_queue.put((int(ms or 0), func, args, kwargs))
        except Exception:
            pass
        return None

    def _drain_ui_queue(self):
        """Main-thread poller that runs UI callbacks queued by workers."""
        while True:
            try:
                ms, func, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if ms:
                    super().after(
                        ms, lambda f=func, a=args, k=kwargs: f(*a, **k)
                    )
                else:
                    func(*args, **kwargs)
            except Exception:
                logger.debug("Deferred UI callback failed", exc_info=True)
        if not getattr(self, "_ui_closed", False):
            try:
                super().after(25, self._drain_ui_queue)
            except Exception:
                pass

    # --- TAB 1 DESIGN ---
    def build_downloader_view(self):
        ctk.CTkLabel(self.tab_downloader, text="Media Downloader", font=("Segoe UI", 18, "bold")).pack(pady=(10,4))

        self.url_entry = ctk.CTkEntry(
            self.tab_downloader,
            width=300,
            height=36,
            placeholder_text="Paste link or search song name"
        )
        self.url_entry.pack(pady=(4,8), padx=20, fill="x")

        # Button for MP3/Spotify
        self.btn_download_mp3 = ctk.CTkButton(
            self.tab_downloader,
            text="Download Audio (MP3)",
            width=240, height=36,
            fg_color="#3498db",
            hover_color="#2980b9",
            cursor="hand2",
            command=lambda: self._start_worker(self.download_mp3)
        )
        self.btn_download_mp3.pack(pady=(2,6))

        self.btn_download_mp4 = ctk.CTkButton(
            self.tab_downloader,
            text="Download Video (MP4)",
            width=240, height=36,
            fg_color="#27ae60",
            hover_color="#229954",
            cursor="hand2",
            command=lambda: self._start_worker(self.download_mp4)
        )
        self.btn_download_mp4.pack(pady=(0,6))

        # Cancellation: enabled only while a download is in flight (#1).
        self.btn_cancel_download = ctk.CTkButton(
            self.tab_downloader,
            text="\u23f9 Cancel Download",
            width=240, height=32,
            fg_color="#e74c3c",
            hover_color="#c0392b",
            cursor="hand2",
            command=self._cancel_active_download,
            state="disabled",
        )
        self.btn_cancel_download.pack(pady=(0,6))

        preview_frame = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        preview_frame.pack(pady=(2,6))

        self.btn_preview_audio = ctk.CTkButton(
            preview_frame,
            text="▶ Preview Audio",
            width=115,
            fg_color="#f39c12",
            hover_color="#d68910",
            cursor="hand2",
            command=lambda: self._start_worker(self.preview_audio)
        )
        self.btn_preview_audio.grid(row=0, column=0, padx=6)
        self.btn_stop_audio = ctk.CTkButton(
            preview_frame,
            text="⏹ Stop Audio",
            width=115,
            fg_color="#e74c3c",
            hover_color="#c0392b",
            cursor="hand2",
            command=self.stop_preview_audio,
            state="disabled"
        )
        self.btn_stop_audio.grid(row=0, column=1, padx=6)

        self.btn_preview_video = ctk.CTkButton(
            preview_frame,
            text="▶ Preview Video",
            width=115,
            fg_color="#8e44ad",
            hover_color="#7d3c98",
            cursor="hand2",
            command=lambda: self._start_worker(self.preview_video)
        )
        self.btn_preview_video.grid(row=1, column=0, padx=6, pady=(4,0))
        self.btn_stop_video = ctk.CTkButton(
            preview_frame,
            text="⏹ Stop Video",
            width=115,
            fg_color="#e74c3c",
            hover_color="#c0392b",
            cursor="hand2",
            command=self.stop_preview_video,
            state="disabled"
        )
        self.btn_stop_video.grid(row=1, column=1, padx=6, pady=(4,0))

        self.url_entry.bind("<Enter>", lambda e: self._set_hover_detail("Paste link or search song. Spotify links are auto-converted.", "#bdc3c7"))
        self.url_entry.bind("<Leave>", lambda e: self._restore_hover_detail())
        self.btn_download_mp3.bind("<Enter>", lambda e: self._set_hover_detail("Download MP3 with artwork and metadata.", "#ecf0f1"))
        self.btn_download_mp3.bind("<Leave>", lambda e: self._restore_hover_detail())
        self.btn_download_mp4.bind("<Enter>", lambda e: self._set_hover_detail("Download high quality MP4 video.", "#ecf0f1"))
        self.btn_download_mp4.bind("<Leave>", lambda e: self._restore_hover_detail())
        self.btn_preview_audio.bind("<Enter>", lambda e: self._set_hover_detail("Listen to a quick 30s audio preview.", "#ecf0f1"))
        self.btn_preview_audio.bind("<Leave>", lambda e: self._restore_hover_detail())
        self.btn_preview_video.bind("<Enter>", lambda e: self._set_hover_detail("Preview video in the stream player.", "#ecf0f1"))
        self.btn_preview_video.bind("<Leave>", lambda e: self._restore_hover_detail())

        # Embedded preview panel (hidden until used)
        self.preview_panel = tk.Frame(self.tab_downloader, bg='black', width=280, height=170)
        self.preview_panel.pack(pady=(4,8))
        self.preview_panel.pack_forget()

        # Save-location + audio-quality controls (#1, #2).
        self.save_folder_lbl = ctk.CTkLabel(
            self.tab_downloader,
            text="",
            font=("Segoe UI", 11),
            text_color="#95a5a6",
            wraplength=340,
            justify="center",
        )
        self.save_folder_lbl.pack(padx=20, pady=(2, 1))
        self.btn_choose_folder = ctk.CTkButton(
            self.tab_downloader,
            text="📁 Change Save Folder",
            width=200, height=30,
            fg_color="#16a085",
            hover_color="#138d75",
            cursor="hand2",
            command=self._choose_save_folder,
        )
        self.btn_choose_folder.pack(pady=(0, 4))

        fmt_row = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        fmt_row.pack(pady=(0, 2))
        ctk.CTkLabel(fmt_row, text="Audio quality:", font=("Segoe UI", 12)).pack(side="left", padx=(0, 8))
        self.fmt_option = ctk.CTkOptionMenu(
            fmt_row,
            width=190,
            values=[p['label'] for p in downloader.AUDIO_FORMATS.values()],
            command=self._on_audio_format_change,
        )
        self.fmt_option.set(
            downloader.AUDIO_FORMATS.get(
                downloader.get_audio_format(), {'label': 'MP3 (VBR High)'}
            )['label']
        )
        self.fmt_option.pack(side="left")

        # Outdated yt-dlp notice (hidden until a check finds one).
        self._ytdlp_notice_lbl = ctk.CTkLabel(
            self.tab_downloader,
            text="",
            font=("Segoe UI", 11, "bold"),
            text_color="#f39c12",
            wraplength=340,
            justify="center",
        )
        self._ytdlp_notice_lbl.pack(padx=20, pady=(0, 2))
        self._update_save_folder_label()

        # place downloader frame in content area
        self.tab_downloader.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.dl_status = ctk.CTkLabel(self.tab_downloader, text="System Ready", font=("Segoe UI", 13))
        self.dl_status.pack(pady=(4,1))

        self.dl_detail = ctk.CTkLabel(self.tab_downloader, text="", font=("Segoe UI", 11), text_color="#95a5a6")
        self.dl_detail.pack(pady=(0,2))

        self.progress_bar = ctk.CTkProgressBar(self.tab_downloader, width=300)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=(1, 8), padx=20, fill="x")

        # Speed + ETA label below progress bar
        self.dl_speed_lbl = ctk.CTkLabel(self.tab_downloader, text="", font=("Segoe UI", 11), text_color="#7f8c8d")
        self.dl_speed_lbl.pack(pady=(0, 2))

        # --- Queue + Tag Editor controls ---
        controls_row = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        controls_row.pack(pady=(0, 4))

        self.btn_add_to_queue = ctk.CTkButton(
            controls_row, text="➕ Add to Queue", width=130, height=28,
            fg_color="#27ae60", hover_color="#229954",
            command=self._add_current_to_queue)
        self.btn_add_to_queue.pack(side="left", padx=(0, 8))

        self.btn_edit_tags = ctk.CTkButton(
            controls_row, text="Edit Tags", width=100, height=28,
            fg_color="#9b59b6", hover_color="#8e44ad",
            command=self._open_tag_editor, state="disabled")
        self.btn_edit_tags.pack(side="left", padx=(8, 0))

        self._last_downloaded_file = None
        self._last_dl_was_video = False

    def _add_current_to_queue(self):
        """Add the current URL(s) to the queue without starting download.

        Supports bulk paste (one URL per line, or comma/semicolon separated)
        and auto-expands playlist/collection URLs into their individual tracks
        so a pasted YouTube/Spotify/SoundCloud playlist fills the queue instead
        of downloading a single item.
        """
        raw = self.url_entry.get().strip()
        if not raw:
            messagebox.showwarning("Queue", "Please enter a URL first.")
            return
        urls = self._split_input_urls(raw)
        if not urls:
            messagebox.showwarning("Queue", "No valid URLs found.")
            return
        self._init_queue_manager()
        if not self._queue_manager:
            messagebox.showerror("Queue Error", "Queue manager is not available.")
            return
        self._enqueue_urls(urls, is_video=False)

    @staticmethod
    def _split_input_urls(raw: str) -> list[str]:
        """Split free-form input into a list of http(s) URLs."""
        urls: list[str] = []
        for chunk in re.split(r'[\n,;]+', raw):
            chunk = chunk.strip()
            if chunk.startswith(('http://', 'https://')):
                urls.append(chunk)
        return urls

    def _enqueue_urls(self, urls: list[str], is_video: bool = False):
        """Add URLs to the queue (supports bulk paste of multiple URLs)."""
        queue = self._queue_manager
        if queue is None:
            messagebox.showerror("Queue Error", "Queue manager is not available.")
            return

        added = queue.add(urls, is_video=is_video)
        if not queue.is_running:
            queue.start()
        self._refresh_queue_tab()
        self.update_dl_status(f"Added {added} item(s) to queue.", "#27ae60")

    def _init_queue_manager(self):
        if self._queue_initialized:
            return
        self._queue_initialized = True
        try:
            import download_queue
            self._queue_manager = download_queue.DownloadQueue(
                download_fn=downloader.download_track,
                video_fn=downloader.download_video_mp4,
                find_file_fn=downloader._find_latest_mp3,
                save_folder_fn=downloader.get_download_folder,
            )
            self._queue_manager.set_callbacks(
                on_item_complete=self._on_queue_item_complete,
                on_queue_done=lambda: self.after(0, self._refresh_queue_tab),
                on_progress=self._on_queue_progress)
            # Re-fill the queue with items left pending from the previous session.
            restored = self._queue_manager.restore()
            if restored:
                logger.debug("Restored %d item(s) into the queue", restored)
                self._refresh_queue_tab()
        except Exception as e:
            logger.debug("Queue manager init failed: %s", e)

    def _on_queue_item_complete(self, status, item):
        def _do():
            if status == "done":
                self._last_downloaded_file = getattr(item, 'filepath', None) or self._last_downloaded_file
                self.btn_edit_tags.configure(state="normal")
                self.update_dl_status(f"Downloaded: {os.path.basename(str(item.filepath) if item.filepath else '')}", "#2ecc71")
        self.after(0, _do)
        # This callback runs on the queue worker thread; never touch Tk directly.
        self.after(0, self._refresh_queue_tab)

    def _on_queue_progress(self, item, pct=None, text=None):
        """Called from the queue worker thread; refresh the queue display."""
        if pct is not None:
            item._progress = pct
        self.after(0, self._refresh_queue_tab)

    def _refresh_queue_tab(self):
        try:
            self._rebuild_queue_list()
        except Exception:
            pass

    def build_queue_view(self):
        ctk.CTkLabel(self.tab_queue, text="Download Queue", font=("Segoe UI", 18, "bold")).pack(pady=(20, 10))

        self.queue_listbox = tk.Listbox(self.tab_queue, height=12, font=("Segoe UI", 11),
                                        bg="#2c3e50", fg="#ecf0f1", selectmode="browse")
        self.queue_listbox.pack(pady=(0, 12), padx=20, fill="both", expand=True)

        queue_btn_row = ctk.CTkFrame(self.tab_queue, fg_color="transparent")
        queue_btn_row.pack(pady=(0, 10))

        self.btn_queue_start = ctk.CTkButton(
            queue_btn_row, text="▶ Start", width=90, height=30,
            command=self._queue_start)
        self.btn_queue_start.pack(side="left", padx=4)

        self.btn_queue_remove = ctk.CTkButton(
            queue_btn_row, text="🗑 Remove", width=90, height=30,
            fg_color="#95a5a6", hover_color="#7f8c8d",
            command=self._queue_remove_selected)
        self.btn_queue_remove.pack(side="left", padx=4)

        self.btn_queue_retry = ctk.CTkButton(
            queue_btn_row, text="↻ Retry", width=90, height=30,
            fg_color="#2980b9", hover_color="#21618c",
            command=self._queue_retry_failed)
        self.btn_queue_retry.pack(side="left", padx=4)

        self.btn_queue_cancel = ctk.CTkButton(
            queue_btn_row, text="✖ Cancel", width=90, height=30,
            fg_color="#e74c3c", hover_color="#c0392b",
            command=self._queue_cancel_all)
        self.btn_queue_cancel.pack(side="left", padx=4)

        self.btn_queue_clear = ctk.CTkButton(
            queue_btn_row, text="🧹 Clear", width=90, height=30,
            fg_color="#636e72", hover_color="#57606f",
            command=self._queue_clear)
        self.btn_queue_clear.pack(side="left", padx=4)

        self.btn_queue_clear_done = ctk.CTkButton(
            queue_btn_row, text="✓ Clear Done", width=100, height=30,
            fg_color="#27ae60", hover_color="#229954",
            command=self._queue_clear_completed)
        self.btn_queue_clear_done.pack(side="left", padx=4)

        self.queue_status = ctk.CTkLabel(self.tab_queue, text="Idle", font=("Segoe UI", 11),
                                         text_color="#95a5a6")
        self.queue_status.pack(pady=(6, 0))

        self.tab_queue.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _rebuild_queue_list(self):
        self.queue_listbox.delete(0, tk.END)
        if not self._queue_manager:
            return
        active_idx = self._queue_manager.active_index
        for i, item in enumerate(self._queue_manager.items):
            icon = {"pending": "⏳", "active": "▶", "done": "✓", "failed": "✗",
                    "skipped": "⊘", "cancelled": "⊘"}.get(item.status, "?")
            # Show progress hint for the active item (e.g. "▶ [ACTIVE] 45% ...").
            if i == active_idx and item.status == "active":
                progress = getattr(item, '_progress', None)
                pct = f" {int(progress*100)}%" if progress is not None else ""
                label = f"{icon} [ACTIVE]{pct} {item.url[:45]}"
            else:
                label = f"{icon} [{item.status.upper():>9}] {item.url[:50]}"
            self.queue_listbox.insert(tk.END, label)
        # Status line with total progress: "3 of 5 items • 60% • Active".
        items = self._queue_manager.items
        total = len(items)
        done = sum(1 for it in items if it.status in ("done", "failed", "skipped"))
        overall = int((done / total) * 100) if total else 0
        running = self._queue_manager.is_running
        self.queue_status.configure(
            text=f"{done}/{total} items • {overall}% • {'Active' if running else 'Idle'}")

    def _queue_start(self):
        self._init_queue_manager()
        if self._queue_manager and not self._queue_manager.is_running:
            self._queue_manager.start()
            self._refresh_queue_tab()

    def _queue_cancel_all(self):
        if self._queue_manager:
            self._queue_manager.cancel()
            self._refresh_queue_tab()

    def _queue_clear(self):
        if self._queue_manager:
            self._queue_manager.clear()
            self._refresh_queue_tab()

    def _queue_clear_completed(self):
        """Remove all done/failed/skipped items from the queue."""
        if not self._queue_manager:
            return
        cleared = self._queue_manager.clear_completed()
        if cleared:
            self._refresh_queue_tab()
            self.show_toast(f"Cleared {cleared} item(s)", "success")
        else:
            self.show_toast("Nothing to clear", "info")

    def _queue_remove_selected(self):
        if not self._queue_manager:
            return
        sel = self.queue_listbox.curselection()
        if not sel:
            self.show_toast("Select an item to remove", "warning")
            return
        # Remove by index; iterate in reverse if multiple selected (browse mode = single).
        for idx in reversed(sel):
            self._queue_manager.remove_at(idx)
        self._refresh_queue_tab()

    def _queue_retry_failed(self):
        if not self._queue_manager:
            return
        reset = self._queue_manager.retry_failed()
        if reset:
            self._refresh_queue_tab()
            self.show_toast(f"Retrying {reset} item(s)", "success")
        else:
            self.show_toast("No failed items to retry", "info")

    def build_history_view(self):
        """Build the Download History tab: scrollable list of past downloads
        with re-download buttons and a clear-history control."""
        ctk.CTkLabel(self.tab_history, text="Download History", font=("Segoe UI", 18, "bold")).pack(pady=(10, 6))

        # Top controls: Clear History button + count label
        ctrl_row = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        ctrl_row.pack(fill="x", padx=20, pady=(0, 6))

        self.btn_clear_history = ctk.CTkButton(
            ctrl_row, text="🧹 Clear History", width=140, height=30,
            fg_color="#e74c3c", hover_color="#c0392b",
            command=self._clear_history,
        )
        self.btn_clear_history.pack(side="left")

        self.history_count_lbl = ctk.CTkLabel(ctrl_row, text="0 entries", font=("Segoe UI", 12), text_color="#95a5a6")
        self.history_count_lbl.pack(side="left", padx=(12, 0))

        # Search bar
        search_row = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        search_row.pack(fill="x", padx=20, pady=(0, 6))
        self.history_search_var = ctk.StringVar()
        self.history_search_entry = ctk.CTkEntry(
            search_row, placeholder_text="🔍 Search by title or URL...",
            textvariable=self.history_search_var, height=32,
        )
        self.history_search_entry.pack(side="left", fill="x", expand=True)
        self.history_search_var.trace_add("write", lambda *_: self._refresh_history_view())

        # Sort controls
        self._history_sort_key = "date"
        self._history_sort_reverse = True
        sort_row = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        sort_row.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(sort_row, text="Sort by:", font=("Segoe UI", 11), text_color="#95a5a6").pack(side="left")
        for lbl, key in [("Date", "date"), ("Title", "title"), ("Status", "status")]:
            btn = ctk.CTkButton(
                sort_row, text=lbl, width=60, height=24,
                fg_color="transparent", hover_color="#34495e",
                text_color="#bdc3c7", font=("Segoe UI", 10),
                command=lambda k=key: self._set_history_sort(k),
            )
            btn.pack(side="left", padx=2)

        # Scrollable list of history entries
        self.history_scroll = ctk.CTkScrollableFrame(self.tab_history, label_text="Past Downloads")
        self.history_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # Internal container for entry widgets (rebuilt on refresh)
        self._history_entries_frame = ctk.CTkFrame(self.history_scroll, fg_color="transparent")
        self._history_entries_frame.pack(fill="both", expand=True)

        self._refresh_history_view()
        self.tab_history.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _set_history_sort(self, key):
        if self._history_sort_key == key:
            self._history_sort_reverse = not self._history_sort_reverse
        else:
            self._history_sort_key = key
            self._history_sort_reverse = True if key == "date" else False
        self._refresh_history_view()

    def _refresh_history_view(self):
        """Rebuild the history entry list from the saved history file."""
        for w in self._history_entries_frame.winfo_children():
            w.destroy()

        history = []
        self._init_queue_manager()
        if self._queue_manager:
            try:
                history = self._queue_manager.get_history()
            except Exception:
                history = []

        # Apply search filter
        query = self.history_search_var.get().strip().lower()
        if query:
            history = [e for e in history if query in e.get("title", "").lower() or query in e.get("url", "").lower()]

        # Apply sorting
        sort_key = self._history_sort_key
        reverse = self._history_sort_reverse
        def _sort_key(entry):
            if sort_key == "date":
                return entry.get("completed_at", 0) or 0
            elif sort_key == "title":
                return (entry.get("title") or entry.get("url") or "").lower()
            elif sort_key == "status":
                return entry.get("status", "")
            return 0
        history = sorted(history, key=_sort_key, reverse=reverse)

        self.history_count_lbl.configure(text=f"{len(history)} entries")

        if not history:
            msg = "No matching history entries." if query else "No download history yet.\nDownloads you complete will appear here."
            ctk.CTkLabel(
                self._history_entries_frame,
                text=msg,
                font=("Segoe UI", 12), text_color="#95a5a6",
            ).pack(pady=30)
            return

        for idx, entry in enumerate(history):
            self._add_history_entry(idx, entry)

    def _add_history_entry(self, idx, entry):
        """Add a single history row with title, date, status, and re-download."""
        url = entry.get("url", "")
        title = entry.get("title") or url[:60] or "Unknown"
        status = entry.get("status", "unknown")
        filepath = entry.get("filepath", "")
        completed = entry.get("completed_at")
        is_video = entry.get("is_video", False)

        # Format timestamp
        date_str = "Unknown"
        if completed:
            try:
                from datetime import datetime
                date_str = datetime.fromtimestamp(completed).strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_str = "Unknown"

        # Status styling
        status_colors = {
            "done": "#2ecc71", "failed": "#e74c3c",
            "skipped": "#f39c12", "pending": "#3498db",
        }
        status_color = status_colors.get(status, "#95a5a6")

        # File existence check
        file_exists = bool(filepath and os.path.exists(filepath))

        # Entry frame
        row = ctk.CTkFrame(self._history_entries_frame, fg_color="transparent")
        row.pack(fill="x", pady=3, padx=4)

        # Left: title + metadata
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True)

        title_lbl = ctk.CTkLabel(left, text=title, font=("Segoe UI", 12, "bold"),
                                 anchor="w", wraplength=380, justify="left")
        title_lbl.pack(anchor="w")

        meta_parts = [f"{'🎬' if is_video else '🎵'} {date_str}"]
        meta_parts.append(f"File: {'✓ exists' if file_exists else '✗ missing'}")
        meta_text = "  |  ".join(meta_parts)
        meta_lbl = ctk.CTkLabel(left, text=meta_text, font=("Segoe UI", 10),
                                text_color="#7f8c8d", anchor="w")
        meta_lbl.pack(anchor="w")

        # Right: status badge + buttons
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right")

        status_lbl = ctk.CTkLabel(right, text=status.upper(), font=("Segoe UI", 10, "bold"),
                                  text_color=status_color, width=70)
        status_lbl.pack(side="left", padx=(0, 6))

        # Re-download button
        btn_redl = ctk.CTkButton(
            right, text="↻", width=34, height=28,
            fg_color="#27ae60", hover_color="#1e8449",
            command=lambda u=url, v=is_video: self._redownload(u, v),
        )
        btn_redl.pack(side="left", padx=2)

        # Open file button (only if file exists)
        if file_exists:
            btn_open = ctk.CTkButton(
                right, text="📂", width=34, height=28,
                fg_color="#2980b9", hover_color="#21618c",
                command=lambda p=filepath: self._open_file(p),
            )
            btn_open.pack(side="left", padx=2)

    def _redownload(self, url, is_video):
        """Re-download a URL from history by injecting it into the downloader."""
        self.show_frame("downloader")
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, url)
            if is_video:
                self._start_worker(self.download_mp4)
            else:
                self._start_worker(self.download_mp3)
        except Exception as e:
            messagebox.showerror("Re-download Error", f"Could not start re-download:\n{e}")

    def _open_file(self, filepath):
        """Open a downloaded file with the system default app."""
        try:
            import subprocess
            subprocess.Popen(["start", "", filepath], shell=True)
        except Exception:
            try:
                import subprocess
                subprocess.Popen(["explorer", "/select,", filepath])
            except Exception as e:
                messagebox.showerror("Open Error", f"Could not open file:\n{e}")

    def _clear_history(self):
        """Wipe the download history after confirmation."""
        if not self._queue_manager:
            self._init_queue_manager()
        if not self._queue_manager:
            messagebox.showerror("Error", "Could not initialize download queue.")
            return
        if not messagebox.askyesno("Clear History", "Delete all download history?\n\nThis cannot be undone."):
            return
        try:
            self._queue_manager.clear_history()
            self._refresh_history_view()
            self.show_toast("History cleared", "success")
        except Exception as e:
            messagebox.showerror("Error", f"Could not clear history:\n{e}")

    # -----------------
    # Toast notifications (non-blocking替代 messagebox)
    # -----------------
    def show_toast(self, message: str, toast_type: str = "info", duration: int = 3500):
        """Show a non-blocking toast notification at the bottom-right.
        
        toast_type: 'info' (blue), 'success' (green), 'warning' (orange), 'error' (red)
        duration: milliseconds before auto-dismiss
        """
        colors = {
            "info": ("#2980b9", "#21618c"),
            "success": ("#27ae60", "#1e8449"),
            "warning": ("#f39c12", "#d68910"),
            "error": ("#e74c3c", "#c0392b"),
        }
        fg_color, hover_color = colors.get(toast_type, colors["info"])

        try:
            toast = ctk.CTkFrame(self, fg_color=fg_color, corner_radius=10)
            toast.place(relx=0.98, rely=0.95, anchor="se")

            icon = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✗"}.get(toast_type, "ℹ")
            lbl = ctk.CTkLabel(toast, text=f" {icon} {message}", font=("Segoe UI", 12), text_color="white")
            lbl.pack(padx=16, pady=10)

            # Auto-dismiss after duration
            def _dismiss():
                try:
                    toast.destroy()
                except Exception:
                    pass
            self.after(duration, _dismiss)
        except Exception:
            pass

    def update_speed_label(self, speed_text: str):
        """Update the speed/ETA label below the progress bar."""
        self.after(0, lambda: self.dl_speed_lbl.configure(text=speed_text))

    def _open_tag_editor(self):
        filepath = self._last_downloaded_file
        if not filepath or not os.path.exists(filepath):
            messagebox.showwarning("No File", "No downloaded file available to edit.")
            return
        try:
            from tag_editor import open_tag_editor
            open_tag_editor(self, filepath, on_save=self._on_tags_saved)
        except Exception as e:
            messagebox.showerror("Tag Editor", f"Could not open tag editor: {e}")

    def _on_tags_saved(self):
        messagebox.showinfo("Tags Saved", "Tags updated successfully.")


    def _update_save_folder_label(self):
        try:
            self.save_folder_lbl.configure(
                text=f"Saves to: {downloader.get_download_folder()}"
            )
        except Exception:
            logger.debug("Save-folder label update failed", exc_info=True)

    def _choose_save_folder(self):
        start = downloader.get_download_folder()
        choice = filedialog.askdirectory(
            initialdir=start if os.path.isdir(start) else os.path.expanduser("~"),
            title="Choose where downloads are saved",
        )
        if not choice:
            return
        downloader.set_download_folder(choice)
        self._set_pref('save_folder', choice)
        self._update_save_folder_label()
        self.update_dl_detail(f"New downloads will be saved to:\n{choice}", "#95a5a6")

    def _on_audio_format_change(self, label):
        reverse = {p['label']: key for key, p in downloader.AUDIO_FORMATS.items()}
        key = reverse.get(label, 'mp3_vbr')
        downloader.set_audio_format(key)
        self._set_pref('audio_format', key)
        fmt_info = downloader.AUDIO_FORMATS.get(key)
        fmt_label = fmt_info.get('label', key) if fmt_info else key
        try:
            if hasattr(self, 'fmt_option'):
                self.fmt_option.set(fmt_label)
        except Exception:
            pass
        self.update_dl_detail(f"Audio format set to {fmt_label}.", "#95a5a6")

    def _ytdlp_update_check_worker(self):
        """Background startup probe: warn only if a yt-dlp update is available.

        Rate-limited to once per day so we don't hit GitHub on every launch.
        """
        if download_queue._update_check_done_today('ytdlp'):
            logger.debug("yt-dlp update check already ran today; skipping")
            return
        try:
            res = downloader.check_ytdlp_update_available(timeout=10.0)
        except Exception as e:
            logger.debug("yt-dlp update check failed: %s", e)
            return
        download_queue._mark_update_check_done('ytdlp')

        status = res.get("status")
        local = res.get("local")
        latest = res.get("latest")

        def refresh_label():
            # Rebuild the friendly version label text based on probe result.
            if status == "update" and local and latest:
                self.ytdlp_ver_lbl.configure(
                    text=f"yt-dlp: {local} -> {latest} (update available!)",
                    text_color="#f39c12",
                )
            elif status == "current" and local:
                self.ytdlp_ver_lbl.configure(
                    text=f"yt-dlp version: {local}",
                    text_color="#27ae60",
                )
            else:
                # "unknown" or not-detected: stay neutral, don't spam user.
                self.ytdlp_ver_lbl.configure(
                    text=f"yt-dlp version: {local or 'not detected'}",
                    text_color="#95a5a6",
                )

        # Always refresh the label on the main thread first (non-blocking).
        self.after(0, refresh_label)

        # Only push the nudge banner when an update is genuinely available.
        if status == "update" and local and latest:
            def push_banner():
                self.update_dl_detail(
                    f"yt-dlp update available ({local} -> {latest}). Click the "
                    f"Update button in the Performance tab.",
                    "#f39c12",
                )
            self.after(0, push_banner)


    def update_ytdlp_clicked(self):
        self.ytdlp_update_btn.configure(state="disabled", text="Updating...")
        self.ytdlp_ver_lbl.configure(text="Updating yt-dlp...")
        def worker():
            try:
                msg = downloader.update_ytdlp(
                    status_callback=lambda text, color: self.after(0, lambda: self.ytdlp_ver_lbl.configure(text=text))
                )
                if "already up to date" in msg.lower():
                    self.after(0, lambda: messagebox.showinfo("yt-dlp Updater", f"yt-dlp is already up to date!\n\n{msg}"))
                elif "updated" in msg.lower() and "fail" not in msg.lower():
                    self.after(0, lambda: messagebox.showinfo("yt-dlp Updater", f"Update successful!\n\n{msg}\n\nRestart the app to use the new version."))
                else:
                    self.after(0, lambda: messagebox.showinfo("yt-dlp Updater", msg))
                self.after(0, lambda: self._ytdlp_update_check_worker())
            except Exception as e:
                logger.exception("yt-dlp update failed")
                self.after(0, lambda: messagebox.showerror("Update Error", f"Failed to update yt-dlp:\n{e}"))
            finally:
                self.after(0, lambda: self.ytdlp_update_btn.configure(state="normal", text="⮔ Update yt-dlp"))
        self._start_worker(worker)

    def check_for_app_updates(self):
        """Check the remote manifest and trigger an in-place self-update."""
        import updater  # local module
        from version import __version__ as local_ver

        if not updater.is_frozen():
            messagebox.showinfo(
                "Update Unavailable",
                "Self-update only works in the packaged app.\n\n"
                "You are running from source (python ui.py), so there is no\n"
                "EXE to update. Build and run the .exe to use app updates.",
            )
            return

        if not updater.self_update_supported():
            messagebox.showinfo(
                "Update Unavailable",
                "App self-update is only available in the Windows build.\n\n"
                "On macOS, download the newest .dmg and replace "
                "UniversalAudioStudio.app with the new copy.",
            )
            return

        self.app_update_btn.configure(state="disabled", text="Checking...")
        self.update_dl_status("Checking for app updates...", "#3498db")

        def worker():
            try:
                info = updater.get_remote_update_info()
            except Exception:
                info = None

            if not info or not info.get("version") or not info.get("download_url"):
                self.after(0, lambda: self.update_dl_status("Could not reach update server.", "#e74c3c"))
                self.after(0, lambda: self.app_update_btn.configure(state="normal", text="↻ Check for App Updates"))
                self.after(0, lambda: messagebox.showinfo("Update Check", "No update information available right now."))
                return

            remote_ver = info["version"]
            dl_url = info["download_url"]

            if not updater.is_newer_version(remote_ver):
                self.after(0, lambda: messagebox.showinfo("Up to Date", f"You already have the latest version ({local_ver}).\nNo update needed."))
                self.after(0, lambda: self.app_update_btn.configure(state="normal", text="↻ Check for App Updates"))
                return

            # Update available -- confirm with the user.
            msg = (
                f"A new version is available:\n\n"
                f"  Current: {local_ver}\n"
                f"  Latest:  {remote_ver}\n\n"
                f"Do you want to download and install it now?\n"
                f"The app will close automatically when the update is ready.\n\n"
                f"Because this app is installed under 'C:\\Program Files', "
                f"Windows will show an admin (UAC) prompt when installing. "
                f"Click Yes, and the app will update and relaunch automatically."
            )

            import threading
            confirm_event = threading.Event()
            confirm_result = {"val": False}

            def _ask_user():
                confirm_result["val"] = messagebox.askyesno("Update Available", msg)
                confirm_event.set()   # Signal only after the user has answered.

            self.after(0, lambda: self.update_dl_status(f"Update available (v{remote_ver}). Confirming...", "#f39c12"))
            self.after(0, _ask_user)

            # Block the worker thread until the dialog closes (or times out).
            confirm_event.wait(timeout=120)
            result = confirm_result["val"] if confirm_event.is_set() else False

            if not result:
                # User declined (or timed out).
                self.after(0, lambda: self.app_update_btn.configure(state="normal", text="↻ Check for App Updates"))
                return

            self.after(0, lambda: self.update_dl_status(f"Downloading update v{remote_ver}...", "#3498db"))

            # Download the new EXE.
            new_exe_path = updater.download_update(dl_url)
            if not new_exe_path:
                self.after(0, lambda: self.update_dl_status("Download failed. Please try again.", "#e74c3c"))
                self.after(0, lambda: messagebox.showerror("Update Error", "Failed to download the update."))
                self.after(0, lambda: self.app_update_btn.configure(state="normal", text=f"↻ Update to v{remote_ver}"))
                return

            old_exe_path = sys.executable
            parent_pid = os.getpid()

            self.after(0, lambda: self.update_dl_status("Update downloaded. Installing...", "#f39c12"))

            launched = updater.launch_updater(
                old_exe_path,
                new_exe_path,
                parent_pid,
                # Pass the app exe so the updater relaunches it after installing.
                extra_args=[old_exe_path],
            )

            if not launched:
                self.after(0, lambda: messagebox.showerror(
                    "Update Not Installed",
                    "Could not start the updater.\n\n"
                    "Either updater_cli.exe is missing, or admin permission "
                    "was declined when Windows asked."))
                self.after(0, lambda: self.app_update_btn.configure(state="normal", text="↻ Check for App Updates"))
                return

            # The updater will replace the EXE and we exit so the file can be replaced.
            self.after(0, lambda: self.update_dl_status("Update installed. Restarting...", "#27ae60"))
            self.after(500, lambda: self._do_self_restart())

        self._start_worker(worker)


    def _check_updates_on_startup(self):
        """Quiet startup auto-check: only prompts if a newer version exists.

        Runs in a background thread so the UI stays responsive; any UI touch
        is marshalled back to the main thread via after(). Silent when the
        app is already up-to-date (no popup on every launch). Rate-limited to
        once per day.
        """
        if download_queue._update_check_done_today('app'):
            logger.debug("App update check already ran today; skipping")
            return
        import threading

        def _runner():
            try:
                import updater  # local module
                from version import __version__ as local_ver

                if not updater.is_frozen():
                    return

                info = updater.get_remote_update_info()
                if not info or not info.get("version") or not info.get("download_url"):
                    return

                if not updater.is_newer_version(info["version"]):
                    return
                download_queue._mark_update_check_done('app')

                remote_ver = info["version"]
                self.after(0, lambda: self._offer_startup_update(local_ver, remote_ver))
            except Exception:
                # Never let a background update check crash the app on startup.
                pass

        threading.Thread(target=_runner, daemon=True).start()


    def _offer_startup_update(self, local_ver, remote_ver):
        """Show the 'update available' prompt found by the startup check."""
        msg = (
            f"A new version is available:\n\n"
            f"  Current: {local_ver}\n"
            f"  Latest:  {remote_ver}\n\n"
            f"Do you want to download and install it now?\n"
            f"The app will close automatically when the update is ready.\n\n"
            f"Because this app is installed under 'C:\\Program Files', "
            f"Windows will show an admin (UAC) prompt when installing. "
            f"Click Yes, and the app will update and relaunch automatically."
        )
        if messagebox.askyesno("Update Available", msg):
            # Reuse the full manual update flow (download + elevated install + relaunch).
            try:
                self.check_for_app_updates()
            except Exception:
                pass


    def _do_self_restart(self):
        """Graceful shutdown so the updater can replace the running EXE."""
        self._ui_closed = True
        try:
            self.quit()
            self.destroy()
        except Exception:
            pass
        os._exit(0)


    def download_mp3(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        self._last_dl_was_video = False
        self._last_downloaded_file = None
        self.set_download_buttons_state(False)
        self.progress_bar.set(0)
        self.update_dl_status("Downloading audio...", "#3498db")
        self.update_dl_detail("", "#95a5a6")
        downloader.begin_download_session()
        try:
            downloader.download_track(
                url,
                self.update_dl_status,
                self.on_dl_success,
                self.on_dl_error,
                progress_callback=self.update_progress_bar,
                detail_callback=self.update_dl_detail,
                soundcloud_direct_first=bool(self._prefs.get('soundcloud_direct_first', True)),
            )
        except downloader.DownloadCancelled:
            self.on_dl_cancelled()
        except Exception as e:
            logger.exception("MP3 download failed unexpectedly")
            self.on_dl_error(f"Unexpected download error: {e}")
        finally:
            self.set_download_buttons_state(True)


    def download_mp4(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        self._last_dl_was_video = True
        self._last_downloaded_file = None
        self.set_download_buttons_state(False)
        self.progress_bar.set(0)
        self.update_dl_status("Downloading video...", "#27ae60")
        downloader.begin_download_session()
        try:
            downloader.download_video_mp4(url, self.update_dl_status, self.on_dl_success, self.on_dl_error, progress_callback=self.update_progress_bar)
        except downloader.DownloadCancelled:
            self.on_dl_cancelled()
        except Exception as e:
            logger.exception("MP4 download failed unexpectedly")
            self.on_dl_error(f"Unexpected download error: {e}")
        finally:
            self.set_download_buttons_state(True)

    def _on_closing(self):
        """Handle window close event."""
        self._ui_closed = True
        self.destroy()

    # --- PREVIEW HANDLING ---
    def _ensure_ffplay(self):
        """Return a usable ffplay executable, or None if it can't be found."""
        return downloader.find_helper("ffplay", include_path=True)

    def _ensure_ffmpeg(self):
        """Return a usable ffmpeg executable (bundled or on PATH), else None."""
        return downloader.find_helper("ffmpeg", include_path=True)

    def preview_audio(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("URL Missing", "Please enter a URL to preview.")
            return
        if not downloader.can_resolve_preview():
            messagebox.showerror(
                "Dependency Missing",
                "Neither the 'yt_dlp' Python module nor a local 'yt-dlp.exe' was found.\n\n"
                "Install yt-dlp (python -m pip install yt-dlp) or run a download first to "
                "generate the bundled yt-dlp.exe, then retry.",
            )
            return

        self.btn_preview_audio.configure(state="disabled")
        self.btn_stop_audio.configure(state="normal")
        self.update_dl_status("Preparing audio preview...", "#f39c12")

        def worker():
            try:
                stream_url = downloader.resolve_preview_stream(url, audio_only=True)
                ffplay = self._ensure_ffplay()
                if ffplay:
                    cmd = [ffplay, '-nodisp', '-autoexit', '-t', '30', stream_url]
                    self._preview_proc = subprocess.Popen(cmd, **downloader._no_window_kwargs())
                    self._preview_proc.wait()
                    return
                # ffplay is not bundled -- decode the first 30s with the bundled
                # ffmpeg into raw PCM and play it through sounddevice, so the
                # preview works in the packaged app without extra installs.
                _sd = _lazy_import_sounddevice()
                _np = _lazy_import_numpy()
                ffmpeg = self._ensure_ffmpeg()
                if _sd is None or _np is None or not ffmpeg:
                    raise RuntimeError(
                        "Audio preview needs ffplay.exe or (ffmpeg.exe + sounddevice). "
                        "ffplay was not found.\n\n"
                        "Install ffmpeg/ffplay or place ffplay.exe next to the app "
                        "to use previews."
                    )
                self._preview_via_sd = True
                proc = subprocess.Popen(
                    [ffmpeg, '-v', 'error', '-t', '30', '-i', stream_url,
                     '-f', 'f32le', '-ac', '2', '-ar', '44100', '-'],
                    stdout=subprocess.PIPE,
                    **downloader._no_window_kwargs(),
                )
                self._preview_proc = proc
                raw, _ = proc.communicate()
                if proc.returncode != 0 or not raw:
                    raise RuntimeError(
                        "Could not stream a preview (ffmpeg returned an error). "
                        "The video may not allow previews."
                    )
                data = _np.frombuffer(raw, dtype=_np.float32).reshape(-1, 2)
                _sd.play(data, 44100)
                _sd.wait()
            except Exception as e:
                logger.exception("Audio preview failed")
                self.after(0, lambda err=e: messagebox.showerror('Preview Error', f'Audio preview failed:\n{err}'))
            finally:
                self._preview_via_sd = False
                self.after(0, lambda: self.btn_preview_audio.configure(state='normal'))
                self.after(0, lambda: self.btn_stop_audio.configure(state='disabled'))
                self.after(0, lambda: self.update_dl_status('System Ready', '#95a5a6'))

        self._start_worker(worker)

    def stop_preview_audio(self):
        try:
            _sd = _lazy_import_sounddevice()
            if getattr(self, '_preview_via_sd', False) and _sd is not None:
                # Playing through sounddevice: interrupt the playback.
                try:
                    _sd.stop()
                except Exception:
                    logger.debug("Failed to stop sounddevice preview", exc_info=True)
            elif hasattr(self, '_preview_proc') and self._preview_proc:
                try:
                    self._preview_proc.terminate()
                except Exception:
                    try:
                        self._preview_proc.kill()
                    except Exception:
                        logger.debug("Failed to kill preview process")
            self._preview_proc = None
        finally:
            self.btn_preview_audio.configure(state="normal")
            self.btn_stop_audio.configure(state="disabled")

    def preview_video(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("URL Missing", "Please enter a URL to preview.")
            return
        if not downloader.can_resolve_preview():
            messagebox.showerror(
                "Dependency Missing",
                "Neither the 'yt_dlp' Python module nor a local 'yt-dlp.exe' was found.\n\n"
                "Install yt-dlp (python -m pip install yt-dlp) or run a download first to "
                "generate the bundled yt-dlp.exe, then retry.",
            )
            return

        self.btn_preview_video.configure(state="disabled")
        self.btn_stop_video.configure(state="normal")
        self.update_dl_status("Preparing video preview...", "#8e44ad")

        def worker():
            try:
                stream_url = downloader.resolve_preview_stream(url, audio_only=False)

                if _lazy_import_vlc() is not None:
                    def start_vlc():  # type: ignore[call-arg]

                        try:
                            try:
                                if hasattr(self, '_vlc_player') and self._vlc_player:
                                    self._vlc_player.stop()
                            except Exception:
                                pass

                            self.preview_panel.pack(pady=(6,12))
                            self.preview_panel.update_idletasks()
                            handle = self.preview_panel.winfo_id()

                            # VLC module is optional at type-check time.
                            _vlc = _lazy_import_vlc()
                            if _vlc is None:
                                raise RuntimeError("VLC module not available")
                            vlc_instance = _vlc.Instance()
                            self._vlc_instance = vlc_instance
                            vlc_player = vlc_instance.media_player_new()
                            self._vlc_player = vlc_player

                            media = vlc_instance.media_new(stream_url)
                            vlc_player.set_media(media)

                            try:
                                vlc_player.set_hwnd(handle)
                            except Exception:
                                try:
                                    vlc_player.set_xwindow(handle)
                                except Exception:
                                    pass

                            vlc_player.play()

                        except Exception as e:
                            messagebox.showerror('Preview Error', f'Embedded video preview failed:\n{e}')

                    self.after(0, start_vlc)
                else:
                    ffplay = self._ensure_ffplay()
                    if not ffplay:
                        raise RuntimeError(
                            "ffplay was not found. Install ffmpeg/ffplay or place ffplay.exe "
                            "next to the app to use previews."
                        )
                    cmd = [
                        ffplay,
                        '-autoexit',
                        '-t', '30',
                        '-fs', '0',
                        '-noborder',
                        '-window_title', 'Video Preview',
                        '-geometry', '480x270',
                        stream_url,
                    ]
                    self._preview_proc = subprocess.Popen(cmd, **downloader._no_window_kwargs())
                    self._preview_proc.wait()

            except Exception as e:
                err = e
                self.after(0, lambda err=err: messagebox.showerror('Preview Error', f'Video preview failed:\n{err}'))
            finally:
                self.after(0, lambda: self.btn_preview_video.configure(state='normal'))
                self.after(0, lambda: self.btn_stop_video.configure(state='disabled'))
                self.after(0, lambda: self.update_dl_status('System Ready', '#95a5a6'))

        self._start_worker(worker)

    def stop_preview_video(self):
        try:
            if _lazy_import_vlc() is not None and hasattr(self, '_vlc_player') and self._vlc_player:
                try:
                    self._vlc_player.stop()
                except Exception:
                    pass
                try:
                    self.preview_panel.pack_forget()
                except Exception:
                    pass
                self._vlc_player = None

            if hasattr(self, '_preview_proc') and self._preview_proc:
                try:
                    self._preview_proc.terminate()
                except Exception:
                    try:
                        self._preview_proc.kill()
                    except Exception:
                        logger.debug("Failed to kill preview process")
                        pass
                self._preview_proc = None
        finally:
            self.btn_preview_video.configure(state="normal")
            self.btn_stop_video.configure(state="disabled")

    def set_download_buttons_state(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.after(0, lambda: self.btn_download_mp3.configure(state=state))
        self.after(0, lambda: self.btn_download_mp4.configure(state=state))
        # The cancel button mirrors the inverse state of the download buttons.
        cancel_state = "disabled" if enabled else "normal"
        self.after(0, lambda: self.btn_cancel_download.configure(state=cancel_state))

    def _cancel_active_download(self):
        """Ask the running download worker to stop (cooperative cancellation)."""
        requested = downloader.request_cancel_download()
        if not requested:
            return
        self.update_dl_status("Cancelling...", "#f39c12")
        self.update_dl_detail("Waiting for the current download to stop...", "#95a5a6")
        self.btn_cancel_download.configure(state="disabled")

    def on_dl_cancelled(self):
        self.after(0, lambda: self._finalize_download(None, "__cancelled__"))

    def update_dl_status(self, text, color):
        themed = self._map_color(color)
        self.after(0, lambda: self.dl_status.configure(text=text, text_color=themed))

    def update_dl_detail(self, text, color):
        themed = self._map_color(color)
        self.after(0, lambda: self.dl_detail.configure(text=text, text_color=themed))

    def _set_hover_detail(self, text, color):
        try:
            self._dl_detail_backup_text = self.dl_detail.cget("text")
            self._dl_detail_backup_color = self.dl_detail.cget("text_color")
        except Exception:
            self._dl_detail_backup_text = ""
            self._dl_detail_backup_color = "#95a5a6"
        self.update_dl_detail(text, color)

    def _restore_hover_detail(self):
        if hasattr(self, "_dl_detail_backup_text"):
            self.update_dl_detail(self._dl_detail_backup_text, getattr(self, "_dl_detail_backup_color", "#95a5a6"))

    def update_progress_bar(self, value):
        self.after(0, lambda: self.progress_bar.set(min(max(value, 0.0), 1.0)))

    def on_dl_success(self, folder):
        self.after(0, lambda: self._finalize_download(folder, None))

    def on_dl_error(self, err):
        self.after(0, lambda: self._finalize_download(None, err))

    def _finalize_download(self, folder, err):
        if err == "__cancelled__":
            self.update_dl_status("Cancelled", "#f39c12")
            self.progress_bar.set(0)
            self.update_dl_detail("", "#95a5a6")
        elif err:
            cleaned_err = downloader._strip_ansi(err) if hasattr(downloader, '_strip_ansi') else str(err)
            self.update_dl_status("Failed", "#e74c3c")
            messagebox.showerror("Download Error", cleaned_err)
            self.progress_bar.set(0)
        else:
            self.update_dl_status("Finished!", "#2ecc71")
            self.progress_bar.set(1.0)
            self.update_dl_detail(str(folder), "#95a5a6")
            # Find the file THIS download just produced (newest by mtime) so the
            # tag-editor button and the ID3 writer target the right file. The old
            # reverse-sorted directory scan picked the alphabetically-last media
            # file in the folder, so tags were written to an unrelated older
            # download and the new file kept blank title/artist/album tags.
            if folder and os.path.isdir(str(folder)):
                downloaded_file = self._newest_media_file(folder)
                if downloaded_file:
                    self._last_downloaded_file = downloaded_file
                    self.btn_edit_tags.configure(state="normal")
                    # Strip the "Mark of the Web" so Windows Defender doesn't
                    # quarantine the file as a suspicious internet download.
                    downloader._strip_zone_identifier(downloaded_file)
                    # Write proper ID3 tags (artist/title/album/artwork) so Windows file details show correctly
                    meta = downloader.get_last_dl_metadata()
                    if meta and downloaded_file.lower().endswith(('.mp3', '.flac')):
                        downloader.write_id3_tags(
                            downloaded_file,
                            artist=meta.get("artist", ""),
                            title=meta.get("title", ""),
                            album=meta.get("album", ""),
                            genre=meta.get("genre", ""),
                            year=meta.get("year", ""),
                            artwork_path=meta.get("artwork_path", ""),
                        )
            downloader.open_in_file_manager(str(folder))
            # Save to history
            self._save_direct_download_to_history(folder, err,
                filepath=self._last_downloaded_file, is_video=self._last_dl_was_video)
        self.set_download_buttons_state(True)

    @staticmethod
    def _newest_media_file(folder):
        """Return the path to the newest media file in *folder*, or None.

        Used by the history writer so saved entries point at a real file (not
        the folder) for skip-detection and the "open file" action. Delegates to
        the downloader helper so the direct-download path, the queue and the
        history writer all agree on which file counts as "the media file"
        (thumbnails excluded, newest by mtime).
        """
        if not folder or not os.path.isdir(folder):
            return None
        return downloader.pick_produced_media_file(str(folder))

    def _save_direct_download_to_history(self, folder, err, filepath=None, is_video=False):
        """Save a direct (non-queue) download to the history file."""
        try:
            self._init_queue_manager()
            if not self._queue_manager:
                return
            url = self.url_entry.get().strip()
            if not url:
                return
            meta = downloader.get_last_dl_metadata() or {}
            # Resolve the actual downloaded file: prefer the explicit path, else
            # find the newest media file in the folder so history always points at
            # a real file (not the folder) for the "skip if already downloaded"
            # check and the "open file" action.
            resolved_path = filepath
            if not resolved_path:
                resolved_path = self._newest_media_file(folder)
            entry = {
                "url": url,
                "title": meta.get("title") or meta.get("artist") or url[:60],
                "status": "done" if not err else "failed",
                "filepath": str(resolved_path) if resolved_path else (str(folder) if folder else ""),
                "timestamp": __import__("time").time(),
                "is_video": bool(is_video),
            }
            # Delegate the write to the queue manager so it is atomic and
            # thread-safe (temp file + os.replace under a shared lock).
            self._queue_manager.upsert_history_entry(entry)
        except Exception as e:
            logger.warning("Failed to save download to history: %s", e)

    # --- TAB 2 DESIGN ---
    def build_studio_view(self):
        self.file_frame = ctk.CTkFrame(self.tab_studio, fg_color="transparent")
        self.file_frame.pack(pady=(12,4), padx=20, fill="x")
        self.file_lbl = ctk.CTkLabel(self.file_frame, text="Load a music file...", font=("Segoe UI", 12, "italic"), text_color="#95a5a6")
        self.file_lbl.pack(side="left", padx=10, pady=10)
        ctk.CTkButton(self.file_frame, text="Browse Audio", width=100, command=self.load_studio_file).pack(side="right", padx=10, pady=10)

        self.sp_lbl = ctk.CTkLabel(self.tab_studio, text="Speed: 0.85x", font=("Segoe UI", 14, "bold"))
        self.sp_lbl.pack(anchor="w", padx=20, pady=(6,0))
        self.sp_sld = ctk.CTkSlider(self.tab_studio, from_=0.5, to=1.5, command=lambda v: self.sp_lbl.configure(text=f"Speed: {float(v):.2f}x"))  # type: ignore[arg-type]


        self.sp_sld.set(0.85)
        self.sp_sld.pack(fill="x", padx=20, pady=(4,10))

        self.rv_lbl = ctk.CTkLabel(self.tab_studio, text="Reverb Depth: 40%", font=("Segoe UI", 14, "bold"))
        self.rv_lbl.pack(anchor="w", padx=20, pady=(4,0))
        self.rv_sld = ctk.CTkSlider(self.tab_studio, from_=0.0, to=1.0, command=lambda v: self.rv_lbl.configure(text=f"Reverb Depth: {int(float(v)*100)}%"))  # type: ignore[arg-type]


        self.rv_sld.set(0.40)
        self.rv_sld.pack(fill="x", padx=20, pady=(4,10))

        self.ctrl_frame = ctk.CTkFrame(self.tab_studio, fg_color="transparent")
        self.ctrl_frame.pack(pady=(6,10))
        self.btn_play = ctk.CTkButton(self.ctrl_frame, text="▶ Play", fg_color="#2ecc71", hover_color="#27ae60", width=115, command=self.play_preview)
        self.btn_play.grid(row=0, column=0, padx=6)
        self.btn_stop = ctk.CTkButton(self.ctrl_frame, text="⏹ Stop", fg_color="#e74c3c", hover_color="#c0392b", width=115, command=self.stop_preview, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=6)

        self.btn_export = ctk.CTkButton(self.tab_studio, text="💾 Export Remix", font=("Segoe UI", 12, "bold"), width=280, height=40, command=self.export_studio_track)
        self.btn_export.pack(pady=(4,10))

        self.studio_progress_bar = ctk.CTkProgressBar(self.tab_studio, width=300)
        self.studio_progress_bar.set(0)
        self.studio_progress_bar.pack(pady=(4, 10), padx=20, fill="x")
        self.tab_studio.place(relx=0, rely=0, relwidth=1, relheight=1)

    def load_studio_file(self):
        sel = filedialog.askopenfilename(title="Select File", filetypes=[("Audio Assets", "*.mp3 *.wav *.flac *.m4a")])
        if sel:
            self.studio_file_path = sel
            self.file_lbl.configure(text=os.path.basename(sel), text_color="#2ecc71")

    def build_customization_view(self):
        ctk.CTkLabel(self.tab_customization, text="Appearance Settings", font=("Segoe UI", 18, "bold")).pack(pady=(18,12))

        ctk.CTkLabel(self.tab_customization, text="Theme Mode:", font=("Segoe UI", 14)).pack(anchor="w", padx=20, pady=(10, 0))
        self.mode_option = ctk.CTkOptionMenu(
            self.tab_customization,
            values=["Dark", "Light", "System"],
            command=self.change_appearance_mode,
            width=220
        )
        self.mode_option.set(self._prefs.get("theme_mode", "Dark") or "Dark")
        self.mode_option.pack(padx=20, pady=(0, 12))

        ctk.CTkLabel(self.tab_customization, text="Color Theme (Monkeytype-style):", font=("Segoe UI", 14)).pack(anchor="w", padx=20, pady=(10, 0))
        self.theme_menu = ctk.CTkOptionMenu(
            self.tab_customization,
            values=list(COLOR_THEMES.keys()),
            command=self.apply_color_theme,
            width=220
        )
        self.theme_menu.set(self._color_theme_name)
        self.theme_menu.pack(padx=20, pady=(0, 8))

        # Theme selection lives in the dropdown above only (the duplicate row
        # of clickable swatch dots was removed as redundant UI).

        self.gif_button = ctk.CTkButton(
            self.tab_customization,
            text="Load Animated GIF Background",
            width=260,
            command=self.load_background_gif
        )
        self.gif_button.pack(pady=(16, 8))

        self.clear_bg_button = ctk.CTkButton(
            self.tab_customization,
            text="Clear Background",
            width=260,
            fg_color="#e74c3c",
            hover_color="#c0392b",
            command=self.clear_background
        )
        self.clear_bg_button.pack(pady=(0,12))

        # Cache-clearing moved to the Performance tab (it is maintenance,
        # not appearance).

        self.bg_status = ctk.CTkLabel(self.tab_customization, text="No animated background loaded.", font=("Segoe UI", 12), text_color="#95a5a6")
        self.bg_status.pack(pady=(6, 12))
        self.tab_customization.place(relx=0, rely=0, relwidth=1, relheight=1)

    def build_performance_view(self):
        # Use a scrollable frame so all settings are reachable even on small screens
        saved = downloader.perf_cfg_from_prefs(self._prefs)
        _saved_aria = saved['use_aria2']
        _saved_connections = saved['aria2_connections']
        _saved_frags = saved['concurrent_fragment_downloads']

        scroll = ctk.CTkScrollableFrame(self.tab_performance, label_text="Performance Settings", fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)

        self.aria2_var = tk.BooleanVar(value=_saved_aria)
        self.aria2_chk = ctk.CTkCheckBox(scroll, text="Enable aria2 external downloader", variable=self.aria2_var)
        self.aria2_chk.pack(anchor="w", padx=20, pady=(8, 0))
        ctk.CTkLabel(scroll, text="Uses aria2 for parallel connections (faster downloads).", font=("Segoe UI", 11), text_color="#95a5a6", wraplength=600, justify="left").pack(anchor="w", padx=20, pady=(0, 2))

        ctk.CTkLabel(scroll, text="aria2 connections:  (max 16 — aria2 hard limit)", font=("Segoe UI", 12)).pack(anchor="w", padx=20, pady=(2, 0))
        self.aria2_conn_slider = ctk.CTkSlider(scroll, from_=1, to=16, number_of_steps=15)
        self.aria2_conn_slider.set(_saved_connections)
        self.aria2_conn_slider.pack(padx=20, pady=(0, 2), fill="x")

        ctk.CTkLabel(scroll, text="Concurrent fragment downloads:", font=("Segoe UI", 12)).pack(anchor="w", padx=20, pady=(2, 0))
        self.concurrent_frag_slider = ctk.CTkSlider(scroll, from_=1, to=32, number_of_steps=31)
        self.concurrent_frag_slider.set(_saved_frags)
        self.concurrent_frag_slider.pack(padx=20, pady=(0, 2), fill="x")

        ctk.CTkLabel(scroll, text="SoundCloud downloads:", font=("Segoe UI", 12)).pack(anchor="w", padx=20, pady=(2, 0))
        self.soundcloud_var = tk.BooleanVar(value=bool(self._prefs.get('soundcloud_direct_first', True)))
        self.soundcloud_chk = ctk.CTkCheckBox(scroll, text="Try downloading from SoundCloud first, fall back to YouTube", variable=self.soundcloud_var, command=self.apply_soundcloud_pref)
        self.soundcloud_chk.pack(anchor="w", padx=20, pady=(0, 4))

        # Video download quality cap (the AE re-encode downscales anyway).
        ctk.CTkLabel(scroll, text="Max video resolution:", font=("Segoe UI", 12)).pack(anchor="w", padx=20, pady=(2, 0))
        self.video_res_var = tk.StringVar(value=str(self._prefs.get('max_video_resolution', 'Best (up to 4K)')))
        self.video_res_menu = ctk.CTkOptionMenu(
            scroll, values=downloader.VIDEO_RESOLUTION_OPTIONS, variable=self.video_res_var,
            command=self._on_video_res_change, width=200)
        self.video_res_menu.pack(anchor="w", padx=20, pady=(0, 4))

        # Filename template (yt-dlp output template). Empty = default %(title)s.%(ext)s.
        ctk.CTkLabel(scroll, text="Filename template (optional):", font=("Segoe UI", 12)).pack(anchor="w", padx=20, pady=(2, 0))
        self.filename_template_entry = ctk.CTkEntry(scroll, width=380, height=28,
            placeholder_text="e.g. %(artist)s - %(title)s.%(ext)s  (leave blank for default)")
        self.filename_template_entry.insert(0, str(self._prefs.get('filename_template', '') or ''))
        self.filename_template_entry.pack(anchor="w", padx=20, pady=(0, 4))

        # Maintenance actions
        self.apply_perf_btn = ctk.CTkButton(scroll, text="Apply Performance Settings", command=self.apply_performance_settings, width=260)
        self.apply_perf_btn.pack(pady=(4, 4))

        self.ytdlp_update_btn = ctk.CTkButton(
            scroll, text="⮔ Update yt-dlp", width=260,
            fg_color="#2980b9", hover_color="#21618c",
            cursor="hand2", command=self.update_ytdlp_clicked,
        )
        self.ytdlp_update_btn.pack(pady=(2, 1))
        self.ytdlp_ver_lbl = ctk.CTkLabel(
            scroll, text=f"yt-dlp version: {downloader.get_ytdlp_version() or 'not detected'}",
            font=("Segoe UI", 10), text_color="#95a5a6",
        )
        self.ytdlp_ver_lbl.pack(pady=(0, 2))

        self.app_update_btn = ctk.CTkButton(
            scroll, text="🔄 Check for App Updates", width=260,
            fg_color="#27ae60", hover_color="#1e8449",
            cursor="hand2", command=self.check_for_app_updates,
        )
        self.app_update_btn.pack(pady=(0, 2))

        self.clear_cache_btn = ctk.CTkButton(
            scroll, text="🧹 Clear Download Cache", width=260,
            fg_color="#e74c3c", hover_color="#c0392b",
            command=self.clear_download_cache,
        )
        self.clear_cache_btn.pack(pady=(0, 2))

        self.perf_status = ctk.CTkLabel(scroll, text="Current: default", font=("Segoe UI", 11), text_color="#95a5a6")
        self.perf_status.pack(pady=(2, 0))

    def apply_performance_settings(self):
        cfg = {
            'use_aria2': bool(self.aria2_var.get()),
            'aria2_connections': downloader._clamp_aria2_connections(self.aria2_conn_slider.get()),
            'concurrent_fragment_downloads': int(self.concurrent_frag_slider.get()),
        }
        try:
            downloader.set_performance_config(cfg)
            # Persist all settings
            self._set_pref('use_aria2', cfg['use_aria2'])
            self._set_pref('aria2_connections', cfg['aria2_connections'])
            self._set_pref('concurrent_fragment_downloads', cfg['concurrent_fragment_downloads'])
            # Persist filename template (stripped; empty = use default)
            _tmpl = self.filename_template_entry.get().strip()
            self._set_pref('filename_template', _tmpl)
            self.perf_status.configure(text=f"Current: aria2={cfg['use_aria2']}, conns={cfg['aria2_connections']}, frags={cfg['concurrent_fragment_downloads']}")
            # Honest feedback
            _aria2_path = downloader.get_fast_downloader_path() if cfg['use_aria2'] else None
            if cfg['use_aria2']:
                if _aria2_path:
                    _msg = f"aria2 found at:\n{_aria2_path}\n\nSettings applied."
                else:
                    _msg = (
                        "Settings applied, but aria2 was NOT found on your system.\n\n"
                        "The 'use aria2' toggle will have no effect until aria2 is installed.\n\n"
                        "To get aria2:\n"
                        "  1. Download from https://aria2.github.io/\n"
                        "  2. Place aria2c.exe next to the app EXE (or in the same folder as ui.py)\n"
                        "  3. Restart the app\n\n"
                        "Or install via package manager:\n"
                        "  - Windows: choco install aria2   (or scoop install aria2)\n"
                        "  - macOS:   brew install aria2\n\n"
                        "Until then, leave this disabled."
                    )
            else:
                _msg = "Performance settings applied (aria2 disabled)."
            messagebox.showinfo("Performance", _msg)
        except Exception as e:
            messagebox.showerror("Performance Error", f"Failed to apply settings:\n{e}")

    def apply_soundcloud_pref(self):
        """Persist the 'try SoundCloud direct first' preference for MP3 downloads."""
        enabled = bool(self.soundcloud_var.get())
        self._set_pref('soundcloud_direct_first', enabled)
        self.update_dl_detail(
            "SoundCloud direct download " + ("ENABLED" if enabled else "DISABLED")
            + " (falls back to YouTube when unavailable).",
            "#95a5a6",
        )

    def _on_video_res_change(self, value):
        """Persist the max video resolution preference live (no Apply needed)."""
        res = str(value)
        self._set_pref('max_video_resolution', res)
        downloader.performance_config['max_video_resolution'] = res
        label = res  # Already human-friendly (e.g. "1080p", "Best (up to 4K)")
        self.update_dl_detail(f"Max video resolution: {label}.", "#95a5a6")

    def show_frame(self, name: str):
        # Re-clicking the already-active nav button must not remap pages,
        # force repaints, or restart animations — that churn was part of
        # the residual black-flash on click-through.
        if name == getattr(self, '_current_page', None):
            return
        pages = {
            'downloader': self.tab_downloader,
            'queue': self.tab_queue,
            'history': self.tab_history,
            'studio': self.tab_studio,
            'settings': self.tab_customization,
            'performance': self.tab_performance,
        }
        page_titles = {
            'downloader': 'Media Downloader',
            'queue': 'Download Queue',
            'history': 'Download History',
            'studio': 'Slowed + Reverb Studio',
            'settings': 'Appearance & Background',
            'performance': 'Performance Settings',
        }
        self.title_lbl.configure(text=page_titles.get(name, ''))

        target = pages.get(name)
        others = [f for f in pages.values() if f is not target]

        # Map the incoming page FIRST and unmap the remaining ones after.
        # The old order (unmap everything, then map the target) briefly
        # left nothing visible; if anything forced an intermediate repaint
        # mid-switch (CustomTkinter's transparent-color detection does),
        # that showed up as a one-frame black flash.
        if target is not None:
            try:
                target.place(relx=0, rely=0, relwidth=1, relheight=1)
            except Exception:
                pass
        try:
            self.update_idletasks()
        except Exception:
            pass
        for f in others:
            try:
                f.place_forget()
            except Exception:
                pass

        btn = self.nav_buttons.get(name)
        if btn is not None:
            try:
                self._animate_nav_to(btn)
            except Exception:
                pass

        self._set_active_nav(name)
        self._current_page = name

        # Refresh dynamic tabs when they become visible
        if name == "history":
            self._refresh_history_view()
        elif name == "queue":
            self._refresh_queue_tab()

    def _set_active_nav(self, active_name: str):
        # Skip entirely when nothing changed: each fg_color configure makes
        # CustomTkinter redraw every nav canvas, which compounds flicker.
        if active_name == getattr(self, '_active_nav_name', None):
            return
        active_color = UITheme.SIDEBAR_ACTIVE
        inactive_color = "transparent"
        for name, button in getattr(self, 'nav_buttons', {}).items():
            try:
                button.configure(fg_color=active_color if name == active_name else inactive_color)
            except Exception:
                pass
        self._active_nav_name = active_name

    # -----------------
    # Navigation indicator helpers
    # -----------------
    def _position_nav_indicator_initial(self):
        try:
            btn = self.nav_buttons.get('downloader')
            if btn is None or not getattr(self, 'nav_indicator', None):
                return
            y = btn.winfo_y()
            h = btn.winfo_height()
            self.nav_indicator.place(x=0, y=y + 4, width=4, height=max(20, h - 8))
        except Exception:
            pass
 
    def _animate_nav_to(self, target_btn: ctk.CTkButton):
        if not getattr(self, 'nav_anim_enabled', True) or not getattr(self, 'nav_indicator', None):
            try:
                y = target_btn.winfo_y()
                h = target_btn.winfo_height()
                self.nav_indicator.place(x=0, y=y + 4, width=4, height=max(20, h - 8))
            except Exception:
                pass
            return
 
        try:
            if self._nav_anim_after_id:
                try:
                    self.after_cancel(self._nav_anim_after_id)
                except Exception:
                    pass
 
            start_y = float(self.nav_indicator.winfo_y())
            start_h = float(self.nav_indicator.winfo_height())
            target_y = float(target_btn.winfo_y() + 4)
            target_h = float(max(20, target_btn.winfo_height() - 8))
 
            steps = max(1, int(self.nav_anim_speed))
            def ease(t: float) -> float:
                return 1 - (1 - t) * (1 - t)
 
            def step(i: int):
                try:
                    t = min(1.0, i / steps)
                    progress = ease(t)
                    ny = start_y + (target_y - start_y) * progress
                    nh = start_h + (target_h - start_h) * progress
                    self.nav_indicator.place(x=0, y=int(ny), width=4, height=int(nh))
                    if i < steps:
                        self._nav_anim_after_id = self.after(12, lambda: step(i + 1))
                except Exception:
                    pass
 
            step(0)
        except Exception:
            pass

    # -----------------
    # Collapsible sidebar
    # -----------------
    def _build_sidebar(self):
        """Create the toggle, brand, nav items and sliding accent indicator."""
        expanded = self._sidebar_expanded

        self.sb_toggle_btn = ctk.CTkButton(
            self.sidebar,
            text='«' if expanded else '»',
            width=32, height=28,
            fg_color="transparent",
            hover_color=UITheme.SIDEBAR_HOVER,
            text_color="#ecf0f1",
            cursor="hand2",
            corner_radius=6,
            font=("Segoe UI", 14, "bold"),
            command=self.toggle_sidebar,
        )
        self.sb_toggle_btn.pack(fill="x", padx=8, pady=(10, 2))
        self.sb_toggle_btn.configure(anchor='e' if expanded else 'center')

        self.sb_brand = ctk.CTkLabel(
            self.sidebar,
            text='🎵 TuneLab' if expanded else '🎵',
            font=("Segoe UI", 15, "bold"),
            text_color="#ecf0f1",
            anchor='w' if expanded else 'center',
        )
        self.sb_brand.pack(fill="x", padx=12, pady=(2, 2))

        sep = ctk.CTkFrame(self.sidebar, height=1, fg_color="#2c3e50", corner_radius=0)
        sep.pack(fill="x", padx=10, pady=(4, 10))

        self._sb_items = {
            'downloader': ('⬇️', 'Downloader'),
            'queue': ('📋', 'Queue'),
            'history': ('🕒', 'History'),
            'studio': ('🎛️', 'Studio'),
            'settings': ('🎨', 'Theme'),
            'performance': ('⚡', 'Speed'),
        }
        self._sb_tips = {
            'downloader': 'Switch to MP3/MP4 download tools.',
            'queue': 'View and manage the download queue.',
            'history': 'View and re-download past downloads.',
            'studio': 'Open the slowed/reverb studio view.',
            'settings': 'Customize UI style and performance.',
            'performance': 'Tune performance options for smoother operation.',
        }

                
        
        self.nav_buttons = {}
        for name, (icon, label) in self._sb_items.items():
            btn = ctk.CTkButton(
                self.sidebar,
                text=f'{icon}  {label}' if expanded else icon,
                anchor='w' if expanded else 'center',
                fg_color="transparent",
                hover_color=UITheme.SIDEBAR_HOVER,
                text_color="#ecf0f1",
                cursor="hand2",
                corner_radius=8,
                font=("Segoe UI", 17 if not expanded else 13),
                height=42,
                width=46 if not expanded else 180,
                command=lambda n=name: self.show_frame(n),
            )
            if not expanded:
                btn.pack(pady=3, padx=4, anchor="center")
            else:
                btn.pack(fill="x", padx=8, pady=3)
            btn.bind('<Enter>', lambda e, b=btn, t=self._sb_tips[name]: self._schedule_sb_tooltip(b, t))
            btn.bind('<Leave>', lambda e: self._hide_sb_tooltip())
            btn.bind('<Motion>', lambda e: self._hide_sb_tooltip())
            self.nav_buttons[name] = btn

        # Sliding accent indicator (vertical bar that glides to the active item)
        self.nav_indicator = ctk.CTkFrame(
            self.sidebar,
            width=4,
            height=26,
            fg_color=UITheme.COLOR_PRIMARY,
            corner_radius=2,
        )

        # Double-click empty sidebar space also toggles collapse
        self.sidebar.bind('<Double-Button-1>', lambda e: self.toggle_sidebar())

    def _refresh_sidebar_items(self):
        """Swap sidebar texts/anchors to match the current collapsed state."""
        expanded = self._sidebar_expanded
        try:
            self.sb_toggle_btn.configure(text='«' if expanded else '»', anchor='e' if expanded else 'center')
            self.sb_brand.configure(text='🎵 TuneLab' if expanded else '🎵', anchor='w' if expanded else 'center')
        except Exception:
            pass
        for name, (icon, label) in getattr(self, '_sb_items', {}).items():
            btn = self.nav_buttons.get(name)
            if btn is None:
                continue
            try:
                btn.configure(
                    text=f'{icon}  {label}' if expanded else icon,
                    anchor='w' if expanded else 'center',
                    font=("Segoe UI", 16 if not expanded else 13),
                    width=44 if not expanded else 180,
                )
            except Exception:
                pass
        self._hide_sb_tooltip()

    def toggle_sidebar(self):
        """Collapse or expand the sidebar with a smooth slide animation."""
        self._sidebar_expanded = not self._sidebar_expanded
        self._set_pref('sidebar_collapsed', not self._sidebar_expanded)
        self._hide_sb_tooltip()
        self._animate_sidebar()  # repositions the nav indicator when done

    def _animate_sidebar(self):
        """Slide the sidebar width between collapsed and expanded."""
        try:
            if self._sb_anim_after_id:
                try:
                    self.after_cancel(self._sb_anim_after_id)
                except Exception:
                    pass

            start_w = float(getattr(self, '_sidebar_cur_w', None)
                            or self.sidebar.winfo_width()
                            or UITheme.SB_W_EXPANDED)
            target_w = float(UITheme.SB_W_EXPANDED if self._sidebar_expanded else UITheme.SB_W_COLLAPSED)
            swap_done = False
            _interval = 16  # ms (~60fps)
            _steps = max(10, int(getattr(self, 'nav_anim_speed', 12) or 12))
            self._sidebar_cur_w = int(start_w)

            def ease(t):
                return 1 - (1 - t) ** 3  # ease-out-cubic for natural feel

            def step(i):
                nonlocal swap_done
                t = min(1.0, i / _steps)
                w = int(round(start_w + (target_w - start_w) * ease(t)))
                try:
                    self.sidebar.configure(width=w)
                    self._sidebar_cur_w = w
                except Exception:
                    pass
                # Swap text exactly once at 60% (past the visual midpoint).
                if not swap_done and i >= int(_steps * 0.6):
                    swap_done = True
                    self._refresh_sidebar_items()
                if i < _steps:
                    self._sb_anim_after_id = self.after(_interval, lambda: step(i + 1))
                else:
                    # Guarantee the exact final width.
                    try:
                        self.sidebar.configure(width=int(target_w))
                        self._sidebar_cur_w = int(target_w)
                    except Exception:
                        pass
                    self._sb_anim_after_id = None
                    try:
                        self._hide_sb_tooltip()
                        self._position_nav_indicator_initial()
                    except Exception:
                        pass

            step(0)
        except Exception:
            pass

    def _schedule_sb_tooltip(self, widget, text: str):
        """Delay tooltip show to avoid flicker when the mouse moves quickly."""
        try:
            pending = getattr(self, '_sb_tooltip_after_id', None)
            if pending:
                try:
                    self.after_cancel(pending)
                except Exception:
                    pass
            self._sb_tooltip_after_id = self.after(450, lambda: self._show_sb_tooltip(widget, text))
        except Exception:
            pass

    def _show_sb_tooltip(self, widget, text: str):
        """Floating label beside the rail when the sidebar is collapsed."""
        try:
            # Suppress tooltip during sidebar animation to prevent flicker.
            if getattr(self, '_sb_anim_after_id', None):
                return
            if self._sidebar_expanded:
                self._hide_sb_tooltip()
                return
            x = widget.winfo_rootx() + widget.winfo_width() + 8
            y = widget.winfo_rooty() + max(0, (widget.winfo_height() - 26) // 2)

            tip_bg = self._palette.get('sidebar_active', UITheme.SIDEBAR_ACTIVE)
            tip_fg = self._palette.get('text', '#ecf0f1')

            tp = getattr(self, '_sb_tooltip', None)
            if tp is None or not tp.winfo_exists():
                # Reuse ONE hidden popup instead of creating/destroying a
                # borderless Toplevel on every hover. On Windows a freshly
                # mapped borderless Toplevel paints as a solid black
                # rectangle for a frame until its contents render, which
                # showed up as random black flickers while clicking around.
                tp = tk.Toplevel(self)
                tp.withdraw()
                tp.overrideredirect(True)
                tp.attributes('-topmost', True)
                self._sb_tooltip_lbl = tk.Label(
                    tp,
                    padx=9,
                    pady=4,
                    font=("Segoe UI", 10),
                )
                self._sb_tooltip_lbl.pack()
                self._sb_tooltip = tp

            try:
                self._sb_tooltip_lbl.configure(text=text, bg=tip_bg, fg=tip_fg)
            except Exception:
                pass

            tp.wm_geometry(f'+{x}+{y}')

            # Avoid restack churn: only re-paint/re-show when the popup is
            # not already visible. Rapid mouse passes across icons <Enter>/
            # <Leave> fire repeatedly; re-deiconifying + update_idletasks on
            # every single one is what made the icons flicker.
            if tp.state() != 'normal' or not tp.winfo_viewable():
                tp.update_idletasks()
                tp.deiconify()
        except Exception:
            self._sb_tooltip = None

    def _hide_sb_tooltip(self):
        # Cancel any pending delayed show when the mouse moves away (flicker fix).
        pending = getattr(self, '_sb_tooltip_after_id', None)
        if pending:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
            self._sb_tooltip_after_id = None
        tp = getattr(self, '_sb_tooltip', None)
        if tp is not None:
            try:
                # Withdraw instead of destroy: creating a brand-new
                # borderless popup per Enter/Leave event is what produced
                # the transient black rectangles.
                tp.withdraw()
            except Exception:
                pass

    # -----------------
    # Color theme engine (Monkeytype-style)
    # -----------------
    def _map_color(self, color):
        """Translate a legacy hardcoded hex into the active palette."""
        try:
            role = LEGACY_HEX_ROLES.get(str(color).lower())
            if role:
                return self._palette.get(role, color)
        except Exception:
            pass
        return color

    def _iter_widgets(self, root=None):
        if root is None:
            root = self
        for child in root.winfo_children():
            yield child
            yield from self._iter_widgets(child)

    def apply_color_theme(self, name):
        """Apply a named color theme to the entire UI and remember it."""
        pal = COLOR_THEMES.get(name)
        if not pal:
            return
        self._color_theme_name = name
        self._palette = dict(pal)
        self._set_pref('color_theme', name)

        # Keep the dropdown label in sync when switched via swatches/shortcut.
        try:
            if hasattr(self, 'theme_menu'):
                self.theme_menu.set(name)
        except Exception:
            pass

        # Match Light/Dark appearance so entries/menus follow the palette.
        try:
            ctk.set_appearance_mode('Light' if pal.get('mode') == 'light' else 'Dark')
            if hasattr(self, 'mode_option'):
                self.mode_option.set('Light' if pal.get('mode') == 'light' else 'Dark')
        except Exception:
            pass

        # Window background
        try:
            self.configure(fg_color=pal['bg'])
        except Exception:
            pass
        try:
            self._window_bg = pal['bg']
            self.configure(bg=pal['bg'])
        except Exception:
            pass

        # Main container background
        try:
            self.main_container.configure(fg_color=pal['bg'])
        except Exception:
            pass

        # Walk every widget and remap any themed colors by remembered role.
        # The first pass discovers roles from legacy hardcoded hexes; afterwards
        # each widget remembers which role each option holds, so switching
        # between themes keeps working even though old values are long gone.
        roles_seen = ('fg_color', 'hover_color', 'border_color',
                      'text_color', 'progress_color', 'button_color')
        for w in self._iter_widgets():
            try:
                roles = getattr(w, '_theme_roles', None)
                if roles is None:
                    roles = {}
                    setattr(w, '_theme_roles', roles)
            except Exception:
                continue
            for opt in roles_seen:
                try:
                    cur = w.cget(opt)
                except Exception:
                    continue
                if not isinstance(cur, str) or cur.lower() in ('transparent', ''):
                    continue
                role = roles.get(opt)
                if role is None:
                    role = LEGACY_HEX_ROLES.get(cur.lower())
                    if role is None:
                        continue
                    roles[opt] = role
                new_val = pal.get(role)
                if not new_val:
                    continue
                try:
                    w.configure(**{opt: new_val})
                except Exception:
                    pass

        # Structural pieces that don't use legacy hexes:
        try:
            self.nav_indicator.configure(fg_color=pal['accent'])
        except Exception:
            pass
        for bar_name in ('progress_bar', 'studio_progress_bar'):
            bar = getattr(self, bar_name, None)
            if bar is not None:
                try:
                    bar.configure(progress_color=pal['accent'])
                except Exception:
                    pass
        for sld_name in ('sp_sld', 'rv_sld', 'opacity_slider', 'aria2_conn_slider',
                         'concurrent_frag_slider', 'nav_anim_speed_slider'):
            sld = getattr(self, sld_name, None)
            if sld is not None:
                try:
                    sld.configure(progress_color=pal['accent'], button_color=pal['text'])
                except Exception:
                    pass

        # Header + sidebar text accents
        try:
            self.title_lbl.configure(text_color=pal['text'])
            self.sb_brand.configure(text_color=pal['text'])
            self.sb_toggle_btn.configure(text_color=pal['text'])
        except Exception:
            pass

        # Checkboxes: checked fill follows the accent.
        for cb_name in ('overlay_chk', 'compact_chk', 'opacity_chk', 'disable_max_chk',
                        'nav_anim_chk', 'aria2_chk'):
            cb = getattr(self, cb_name, None)
            if cb is not None:
                try:
                    cb.configure(fg_color=pal['accent'], hover_color=pal['accent_hover'])
                except Exception:
                    pass

        # Swatch selection ring
        self._update_theme_swatches()

    def _update_theme_swatches(self):
        for tname, sw in getattr(self, '_swatches', {}).items():
            try:
                selected = (tname == self._color_theme_name)
                sw.configure(
                    border_width=2 if selected else 0,
                    border_color=COLOR_THEMES[tname]['text'] if selected else 'transparent',
                )
            except Exception:
                pass

    def apply_opacity_toggle(self):
        enabled = bool(self.opacity_var.get())
        self._set_pref('opacity_enabled', enabled)
        try:
            if enabled:
                val = float(self.opacity_slider.get())
                self._set_pref('opacity_alpha', val)
                self._apply_window_alpha(val)
            else:
                self._apply_window_alpha(1.0)
        except Exception:
            pass

    def set_window_opacity(self, val):
        try:
            val_float = float(val)
            if self.opacity_var.get():
                self._set_pref('opacity_alpha', val_float)
                self._apply_window_alpha(val_float)
        except Exception:
            pass

    def change_appearance_mode(self, mode):
        ctk.set_appearance_mode(mode)
        self._set_pref('theme_mode', mode)

    def _on_disable_max_toggle(self):
        try:
            val = bool(self.disable_max_var.get())
            self._set_pref('disable_maximize', val)
            self._disable_maximize(val)
        except Exception:
            pass

    def change_color_theme(self, theme):
        self._set_pref('accent_theme', theme)

        try:
            ctk.set_default_color_theme(theme)
        except FileNotFoundError:
            try:
                ctk.set_default_color_theme("blue")
                messagebox.showwarning("Theme Not Found", f"Theme '{theme}' not found. Reverted to 'blue'.")
                if hasattr(self, 'color_option'):
                    self.color_option.set("blue")
            except Exception:
                pass
        except Exception as e:
            try:
                ctk.set_default_color_theme("blue")
                messagebox.showwarning("Theme Error", f"Could not apply theme '{theme}': {e}\nReverted to 'blue'.")
                if hasattr(self, 'color_option'):
                    self.color_option.set("blue")
            except Exception:
                pass

    def load_background_gif(self):
        if Image is None:
            messagebox.showerror("Dependency Missing", "Pillow is required to load GIF backgrounds. Install with: pip install pillow")
            return

        gif_path = filedialog.askopenfilename(title="Select Animated GIF", filetypes=[("GIF Animation", "*.gif")])
        if not gif_path:
            return

        try:
            pil_img = Image.open(gif_path)
        except Exception as e:
            messagebox.showerror("Background Error", f"Could not open GIF:\n{e}")
            return

        try:
            self.update_idletasks()
            w = max(self.winfo_width(), 520)
            h = max(self.winfo_height(), 520)
            size = (w, h)
        except Exception:
            size = (520, 520)

        frames = []
        try:
            if ImageSequence is not None:
                for frame in ImageSequence.Iterator(pil_img):
                    f = frame.convert('RGBA')
                    ctk_img = ctk.CTkImage(light_image=f, dark_image=f, size=size)
                    frames.append(ctk_img)
        except Exception as e:
            messagebox.showerror("Background Error", f"Failed processing GIF frames:\n{e}")
            return

        if not frames:
            messagebox.showerror("Background Error", "Could not load frames from the selected GIF.")
            return

        self.bg_frames = frames
        self.bg_frame_index = 0
        self.bg_enabled = True
        self.bg_status.configure(text=os.path.basename(gif_path), text_color="#2ecc71")
        self.bg_label.configure(image=self.bg_frames[0])
        self.bg_label.lower()
        self.start_background_animation()

    def clear_background(self):
        if self.bg_animation_id:
            self.after_cancel(self.bg_animation_id)
            self.bg_animation_id = None
        self.bg_frames = []
        self.bg_enabled = False
        self.bg_frame_index = 0
        self.bg_label.configure(image=None)
        self.bg_status.configure(text="No animated background loaded.", text_color="#95a5a6")

    def clear_download_cache(self):
        # Run in a worker thread because filesystem cleanup can be slow
        def worker():
            try:
                # MessageBoxes must appear on the main thread; ask the user
                # there and block the worker until they answer.
                confirm_event = threading.Event()
                confirm_result = {"val": False}

                def _ask():
                    confirm_result["val"] = messagebox.askyesno(
                        "Clear Download Cache",
                        "This will try to remove yt-dlp cache and app temp files.\n\nProceed?",
                    )
                    confirm_event.set()

                self.after(0, _ask)  # marshalled to the main thread
                confirm_event.wait(timeout=120)
                if not confirm_result["val"]:
                    return

                self.after(0, lambda: self.clear_cache_btn.configure(state="disabled"))

                result = downloader.clear_app_cache()
                total = int(result.get("total_removed", 0))
                removed_paths = result.get("removed_paths", [])
                freed_bytes = int(result.get("total_freed_bytes", 0) or 0)

                def _fmt_bytes(n: int) -> str:
                    units = ["B", "KB", "MB", "GB", "TB"]
                    f = float(n)
                    for u in units:
                        if f < 1024 or u == units[-1]:
                            return f"{f:.2f} {u}" if u != "B" else f"{int(f)} {u}"
                        f /= 1024
                    return f"{n} B"

                msg = f"Cleared cache. Removed {total} item(s) (~{_fmt_bytes(freed_bytes)})."

                # Keep messagebox short; only show first few paths if any
                if removed_paths:
                    preview = ", ".join(removed_paths[:5])
                    if len(removed_paths) > 5:
                        preview += f" (+{len(removed_paths)-5} more)"
                    msg += f"\n\nExamples: {preview}"

                self.after(0, lambda: messagebox.showinfo("Cache Cleared", msg))
                self.after(0, lambda: self.update_dl_status("Cache cleared.", "#2ecc71"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Cache Clear Error", str(e)))
            finally:
                self.after(0, lambda: self.clear_cache_btn.configure(state="normal"))

        self._start_worker(worker)

    def start_background_animation(self):
        if not self.bg_enabled or not self.bg_frames:
            return

        self.bg_label.configure(image=self.bg_frames[self.bg_frame_index])
        self.bg_frame_index = (self.bg_frame_index + 1) % len(self.bg_frames)
        # NOTE: deliberately no per-frame .lower() here. Stacking order
        # never changes between animation ticks, and re-lowering a mapped
        # widget every 100ms adds restack churn that amplified the click
        # flicker. The load paths already push the label behind everything.
        self.bg_animation_id = self.after(100, self.start_background_animation)

    def play_preview(self):
        if not self.studio_file_path:
            messagebox.showwarning("File Missing", "Please load an audio file first.")
            return
        if _lazy_import_sounddevice() is None:
            messagebox.showerror("Module Missing", "sounddevice package missing.")
            return
        self.btn_play.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        # Read widget values on the main thread; playback_worker must not touch Tk.
        _path = self.studio_file_path
        _speed = float(self.sp_sld.get())
        _reverb = float(self.rv_sld.get())
        self._start_worker(lambda: self.playback_worker(_path, _speed, _reverb))

    def playback_worker(self, path, speed, reverb):
        try:
            _sd = _lazy_import_sounddevice()
            if _sd is None:
                raise RuntimeError("The 'sounddevice' package is not installed. Please install it in your environment using:\n\n    pip install sounddevice")
            
            audio, sr = downloader.process_studio_dsp(path, speed, reverb)
            if audio is not None and sr is not None:
                _sd.play(audio.T, sr)
                try:
                    _sd.wait()
                except Exception:
                    logger.debug("sounddevice playback wait interrupted", exc_info=True)
                self.after(0, self.reset_studio_buttons)
            else:
                self.after(0, self.reset_studio_buttons)
        except RuntimeError as e:
            self.after(0, lambda err=e: messagebox.showerror("Missing Dependency", str(err)))
            self.after(0, self.reset_studio_buttons)
        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("Playback Error", f"Could not play preview:\n{err}"))
            self.after(0, self.reset_studio_buttons)

    def stop_preview(self):
        _sd = _lazy_import_sounddevice()
        if _sd:
            try:
                _sd.stop()
            except Exception:
                logger.debug("sounddevice stop raised", exc_info=True)
        self.reset_studio_buttons()

    def reset_studio_buttons(self):
        self.after(0, lambda: self.btn_play.configure(state="normal"))
        self.after(0, lambda: self.btn_stop.configure(state="disabled"))

    def update_studio_progress(self, value):
        self.after(0, lambda: self.studio_progress_bar.set(min(max(value, 0.0), 1.0)))

    def export_studio_track(self):
        if not self.studio_file_path:
            messagebox.showwarning("File Missing", "Please select an audio file first.")
            return

        original_name = os.path.splitext(os.path.basename(self.studio_file_path))[0]
        default_output_name = f"{original_name} - Slowed + Reverb.mp3"

        dest = filedialog.asksaveasfilename(
            initialfile=default_output_name,
            defaultextension=".mp3",
            filetypes=[("MP3 Audio file", "*.mp3")],
            title="Export Remix Output Target"
        )
        if not dest:
            return

        self.btn_export.configure(text="Encoding (Using all CPU cores)...", state="disabled")
        self.update_studio_progress(0.0)

        # Capture widget values on the main thread before starting the worker.
        _src = self.studio_file_path
        _speed = float(self.sp_sld.get())
        _reverb = float(self.rv_sld.get())

        def run_export():
            temp_wav = dest + ".temp.wav"
            try:
                self.update_studio_progress(0.1)
                audio, sr = downloader.process_studio_dsp(_src, _speed, _reverb)

                if audio is not None:
                    self.update_studio_progress(0.4)
                    try:
                        import soundfile as sf
                    except Exception:
                        self.after(0, lambda: messagebox.showerror(
                            "Missing Dependency",
                            "The required package 'soundfile' is not installed.\n\n"
                            "Install it in your environment with:\n    python -m pip install soundfile\n\n"
                            "Then retry the export."
                        ))
                        return
                    sr_value = sr if sr is not None else 44100
                    sr_int = int(sr_value)
                    sf.write(temp_wav, audio.T, sr_int, subtype='PCM_16')


                    self.update_studio_progress(0.7)

                    ffmpeg_bin = self._ensure_ffmpeg()
                    if not ffmpeg_bin:
                        raise RuntimeError(
                            "ffmpeg was not found. Install ffmpeg or place ffmpeg.exe "
                            "next to the app to export the studio remix."
                        )
                    cmd = [
                        ffmpeg_bin, "-y",
                        "-i", temp_wav,
                        "-threads", "0",
                        "-preset", "ultrafast",
                        "-c:a", "libmp3lame",
                        "-q:a", "2",
                        dest
                    ]

                    completed = subprocess.run(cmd, **downloader._no_window_kwargs())
                    if completed.returncode != 0:
                        raise RuntimeError(
                            f"ffmpeg failed with exit code {completed.returncode}."
                        )
                    self.update_studio_progress(1.0)
                    self.after(0, lambda: messagebox.showinfo("Success", "Audio exported successfully!"))
                else:
                    self.update_studio_progress(0.0)
                    self.after(0, lambda: messagebox.showerror("Error", "Could not process audio."))
            except Exception as e:
                self.update_studio_progress(0.0)
                self.after(0, lambda err=e: messagebox.showerror("Export Interrupted", f"Failed: {err}"))
            finally:
                if os.path.exists(temp_wav):
                    try:
                        os.remove(temp_wav)
                    except Exception:
                        logger.debug("Failed to remove temp wav", exc_info=True)
                self.after(0, lambda: self.btn_export.configure(text="💾 Render & Export Full Studio Remix Track", state="normal"))

        self._start_worker(run_export)


def _setup_logging() -> None:
    """Route all app logs to %TEMP%/tunelab.log plus the console.

    Safe to call more than once: adding a handler is idempotent because we
    guard against duplicate handlers on the root logger.
    """
    log_path = os.path.join(tempfile.gettempdir(), "tunelab.log")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == os.path.abspath(log_path)
               for h in root.handlers):
        try:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except Exception:
            pass  # Non-writable TEMP etc. — fall back to console only.
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)


if __name__ == "__main__":
    _setup_logging()
    app = UniversalAudioStudio()

    # One-time DWM tweak: disallow window transition animations so Windows
    # cannot fade/flash the whole surface during heavy repaint bursts
    # (e.g. switching pages over an animated GIF background).
    try:
        import ctypes
        _hwnd = int(app.winfo_id())
        _val = ctypes.c_int(1)  # TRUE
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            _hwnd, 3, ctypes.byref(_val), ctypes.sizeof(_val)  # 3 = DWMWA_TRANSITIONS_FORCEDISALLOWED
        )
    except Exception:
        pass

    app.mainloop()

