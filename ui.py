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
from typing import TYPE_CHECKING, Any, Optional
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

def _lazy_import_vlc() -> Any:
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


# ---------------------------------------------------------------
# Type + contrast helpers (module-level so tests can pin them)
# ---------------------------------------------------------------
# Segoe UI doesn't exist on macOS; Tk would silently fall back to a
# default font there. Pick the platform's UI family once, here.
_UI_FONT_FAMILY = "Helvetica Neue" if sys.platform == "darwin" else "Segoe UI"


def _ui_font(size: int, *style: str) -> tuple:
    """Build a Tk font tuple with the platform's UI family."""
    return (_UI_FONT_FAMILY, size, *style)


def _rel_luminance(color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` color (0.0 .. 1.0)."""
    c = str(color).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 0.0

    def _lin(ch: float) -> float:
        return ch / 12.92 if ch <= 0.03928 else ((ch + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two colors (1.0 .. 21.0)."""
    a, b = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _on_color(bg: str, *candidates: str) -> str:
    """Return the candidate with the best contrast against ``bg``.

    Filled buttons pick their label color with this: the palette's text
    color on dark fills, the palette's bg color on light/accent fills —
    whichever reads better, instead of a fixed white that fails WCAG on
    yellow/cyan accents.
    """
    if not candidates:
        return "#ffffff"
    best, best_ratio = candidates[0], 0.0
    for cand in candidates:
        ratio = _contrast(cand, bg)
        if ratio > best_ratio:
            best, best_ratio = cand, ratio
    return best


def _mix(a: str, b: str, t: float) -> str:
    """Blend two '#rrggbb' colors; ``t``=0 keeps ``a``, ``t``=1 gives ``b``.

    CustomTkinter surfaces have no alpha channel, so a "translucent accent"
    (a selected nav chip, a logo tile) is built by mixing the accent into the
    surface it sits on. Deriving it from the palette keeps those tints correct
    on light themes too, where a fixed dark overlay would be invisible.
    """
    def rgb(c):
        c = (c or "#000000").strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        try:
            return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
        except ValueError:
            return (0, 0, 0)

    ca, cb = rgb(a), rgb(b)
    return "#%02x%02x%02x" % tuple(
        int(round(ca[i] + (cb[i] - ca[i]) * t)) for i in range(3))


# ---------------------------------------------------------------
# Rail icons: drawn, not typed
# ---------------------------------------------------------------
# The rail rendered emoji - a down arrow, a clipboard, a clock, a mixer, a
# palette, a bolt. Nothing about an emoji is ours to set: the font owns its size
# and its baseline, the colours come from the emoji font instead of the palette,
# and no two of them carry the same optical weight. The rail is where that shows
# worst - collapsed, the icons are all there is - and it read as a row of
# mismatched stickers that ignored the theme they sat in.
#
# So they are drawn. One 24x24 grid, one stroke weight, round caps, and an ink
# colour that is a parameter: the accent while a row is live, the muted tone
# after. No font is involved, so no font can drift it either.
_ICON_GRID = 24


def _icon_shapes(name: str) -> tuple[tuple[Any, ...], ...]:
    """Vector shapes for *name* on a 24x24 grid, y down.

    Shape tuples: ``('line', x1, y1, x2, y2)``, ``('poly', [(x, y), ...])``
    (stroked), ``('polyfill', [(x, y), ...])`` (filled), ``('oval', cx, cy, r)``
    (outline), ``('disc', cx, cy, r)`` (filled) and ``('half', cx, cy, r)``
    (outline with its left half filled).

    Geometry only - no canvas, no colours - so a test can pin every icon
    without opening a window.
    """
    if name == 'download':
        return (('line', 12, 3.5, 12, 14.5),
                ('poly', [(7, 10), (12, 15), (17, 10)]),
                ('line', 4.5, 20, 19.5, 20))
    if name == 'queue':
        # Rows of dot + bar: a list you can read at 18px, unlike a little tray.
        # The dots need their size at this scale or they dissolve into the bars.
        return tuple(shape for y in (6, 12, 18)
                     for shape in (('disc', 5.0, y, 1.7),
                                   ('line', 9.8, y, 19, y)))
    if name == 'history':
        return (('oval', 12, 12, 8.2),
                ('line', 12, 12, 12, 6.6),
                ('line', 12, 12, 16.4, 14))
    if name == 'studio':
        return tuple(('line', x, 12 - h / 2, x, 12 + h / 2)
                     for x, h in ((5.5, 7), (9.8, 15), (14.2, 9), (18.5, 13)))
    if name == 'settings':
        return (('half', 12, 12, 8.4),)
    if name == 'performance':
        return (('polyfill', [(13.4, 3), (6.6, 13.6), (11, 13.6),
                              (10.4, 21), (17.4, 10.4), (13, 10.4)]),)
    if name == 'note':
        return (('disc', 8.6, 17.4, 3.2),
                ('line', 11.8, 17.4, 11.8, 5.6),
                ('poly', [(11.8, 5.6), (17.6, 7.8), (17.6, 11.4)]))
    if name == 'chevron_left':
        return (('poly', [(14.4, 6.4), (8.4, 12), (14.4, 17.6)]),)
    if name == 'chevron_right':
        return (('poly', [(9.6, 6.4), (15.6, 12), (9.6, 17.6)]),)
    return ()


def _paint_icon(canvas, name, color, size=None, width=2):
    """Draw icon *name* into *canvas* in *color*, centred. Returns the count.

    The canvas it draws on is a plain Tk widget, so its background is a real
    colour - the chip it sits on, never "transparent" - and it must be sized to
    the mark: an opaque box that reaches a chip's corners paints its square over
    the rounding the chip is there to have (see UITheme.SB_ICON_BOX).
    """
    try:
        canvas.delete('icon')
        box_w = int(canvas.winfo_reqwidth())
        box_h = int(canvas.winfo_reqheight())
    except Exception:
        return 0
    if not box_w or not box_h:
        return 0
    extent = float(size if size is not None else min(box_w, box_h) - 2)
    ox, oy = (box_w - extent) / 2.0, (box_h - extent) / 2.0
    k = extent / _ICON_GRID

    def xy(points: list[tuple[float, float]]) -> list[float]:
        flat: list[float] = []
        for x, y in points:
            flat.extend((ox + x * k, oy + y * k))
        return flat

    def box(cx: float, cy: float, r: float) -> tuple[float, float, float, float]:
        return (ox + (cx - r) * k, oy + (cy - r) * k,
                ox + (cx + r) * k, oy + (cy + r) * k)

    stroke = dict(fill=color, width=width, capstyle="round",
                  joinstyle="round", tags="icon")
    drawn = 0
    for shape in _icon_shapes(name):
        kind, rest = shape[0], shape[1:]
        try:
            if kind == 'line':
                canvas.create_line(*xy([(rest[0], rest[1]),
                                        (rest[2], rest[3])]), **stroke)
            elif kind == 'poly':
                canvas.create_line(*xy(rest[0]), **stroke)
            elif kind == 'polyfill':
                canvas.create_polygon(*xy(rest[0]), fill=color,
                                      outline=color, width=1,
                                      joinstyle="round", tags="icon")
            elif kind == 'oval':
                canvas.create_oval(*box(*rest), outline=color,
                                   width=width, tags="icon")
            elif kind == 'disc':
                canvas.create_oval(*box(*rest), fill=color,
                                   outline=color, tags="icon")
            elif kind == 'half':
                canvas.create_arc(*box(*rest), start=90, extent=180,
                                  style="pieslice", fill=color,
                                  outline=color, width=width, tags="icon")
                canvas.create_oval(*box(*rest), outline=color,
                                   width=width, tags="icon")
            else:
                continue
            drawn += 1
        except Exception:
            continue
    return drawn



def _ring_color(ring, fill, base):
    """A border color CustomTkinter will actually accept.

    CTk 6 refuses a ``border_color`` of "transparent" - it raises ValueError
    from the middle of ``CTkFrame.configure()``, after the frame has already
    handed its new ``fg_color`` down to every child label. The children repaint
    and the frame does not, so a hovered nav row became three loose boxes of
    the new tint floating over a row still painted the old colour, and the row a
    click just left kept the chip of the state it lost. A ring is only ever
    drawn when there is a ``border_width``, so wherever there is no ring the
    surface's own color stands in as a no-op.
    """
    if ring and ring != "transparent":
        return ring
    return fill if fill and fill != "transparent" else base


# Fragments that betray a machine-generated wall of text inside a dialog
# message: yt-dlp tracebacks, argparse errors, aria2 and HTTP failures.
_TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "',
                      "Error:", "ERROR:", "yt_dlp", "aria2", "HTTP Error",
                      "usage:")


def _split_headline(message, detail=None):
    """Split dialog text into ``(headline, technical detail)``.

    Call sites used to interpolate whole tracebacks straight into the message,
    which a system message box would render as one unreadable clipped line.
    Here the first line stays as the headline and everything technical below it
    moves into the detail block, where it is capped on screen but always fully
    copyable. A caller-supplied ``detail`` keeps its place in front of the
    folded text, and friendly multi-line messages are left exactly as written.
    """
    message = "" if message is None else str(message).strip()
    detail = "" if detail is None else str(detail).strip()
    if not message:
        return (detail or "(no message)"), ""
    lines = message.splitlines()
    tail = "\n".join(lines[1:])
    if len(lines) > 4 and any(m in tail for m in _TRACEBACK_MARKERS):
        return lines[0].strip(), (f"{detail}\n\n{tail.strip()}"
                                  if detail else tail.strip())
    return message, detail


class UITheme:
    # Typography (base sizes; actual scaling handled by preferences)
    TITLE_FONT = _ui_font(24, "bold")
    SECTION_FONT = _ui_font(18, "bold")
    BODY_FONT = _ui_font(12)

    @staticmethod
    def F(size: int, *style: str) -> tuple:
        """Type token: platform-correct font tuple for a base size.

        Every widget font goes through here (``UITheme.F(12, "bold")``)
        instead of a hard-coded font tuple, so the family or the size
        scale changes in one place. Scaling still happens via
        the ``font_scale`` preference / CTk widget scaling.
        """
        return _ui_font(size, *style)

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

    # Radius tokens: structural surfaces (content frame, cards) share the
    # card radius; transient chrome (toasts, status pills) the medium one;
    # small controls the small one. Keeps corners consistent per class
    # instead of an ad-hoc 6/8/10/12/14/20 mix.
    RADIUS_SM = 6
    RADIUS_MD = 10
    RADIUS_LG = 14
    RADIUS_CARD = 20
    # Fields and menus sit between a control and a card: big enough to read
    # as curved at a glance, small enough not to balloon a 32px-tall box.
    RADIUS_FIELD = 12
    # Every raised surface (rail, content card, cards, row lists, fields)
    # wears a hairline edge in the palette's ``hover`` tone. Without it a
    # card whose fill is close to the window fill just looks like a flat
    # rectangle, and rounded corners stop reading as rounded.
    BORDER_W = 1

    # Collapsible rail geometry. Nav rows are placed once at fixed pixel
    # coordinates inside the rail and never re-laid out: Tk clips child
    # windows to their parent, so narrowing the rail wipes the labels out of
    # view. That is what removed the old "pop" - the rail used to swap every
    # row's text, anchor, font size and width mid-slide, so items jumped.
    # SB_W_COLLAPSED is exactly pad + icon column + pad, which keeps the icon
    # column centred in the collapsed rail instead of drifting left.
    SB_PAD_X = 10
    SB_ICON_W = 30
    SB_TEXT_X = 44        # row-local: inside a chip (which is itself at SB_PAD_X)
    SB_LABEL_X = SB_PAD_X + SB_TEXT_X   # rail-local x of a row's label
    # ^ Anything placed directly in the rail (the wordmark, the group headers)
    #   must start at SB_LABEL_X, not at SB_TEXT_X: the collapsed rail is only
    #   SB_W_COLLAPSED wide, so text starting before that is sliced by its own
    #   clip and leaves a half-glyph poking out beside the icon column.
    SB_HINT_X = 150       # shortcut digit / live badge slot (clipped too)
    SB_ROW_H = 38
    SB_ROW_GAP = 4
    SB_ROW_W = 180
    # The drawn icon mark, and the canvas box it lives in. The box is the mark
    # plus a pixel for the round stroke caps that overhang a line's end, and it
    # is deliberately no bigger than that: a canvas is opaque where a CTkLabel
    # was transparent, so a box that reaches the chip's corners paints its
    # square over the chip's rounding. Centred in a 30x38 chip, 20x20 stays
    # inside the RADIUS_MD arc at every corner; 28x36 does not.
    SB_ICON_MARK = 18
    SB_ICON_BOX = SB_ICON_MARK + 2
    SB_ICON_X = (SB_ICON_W - SB_ICON_BOX) // 2
    SB_ICON_Y = (SB_ROW_H - SB_ICON_BOX) // 2
    # Where that box sits inside a row: centred on the icon column, centred in
    # the row. Same centring as the labels it sits beside.
    SB_ICON_X = (SB_ICON_W - SB_ICON_BOX) // 2
    SB_ICON_Y = (SB_ROW_H - SB_ICON_BOX) // 2
    SB_W_EXPANDED = SB_PAD_X + SB_ROW_W + SB_PAD_X      # 200
    SB_W_COLLAPSED = SB_PAD_X + SB_ICON_W + SB_PAD_X    # 50


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
        'text': '#ecf0f1', 'sub': '#9baaab',
        'success': '#27ae60', 'success_hover': '#229954',
        'warning': '#f39c12', 'warning_hover': '#d68910',
        'danger': '#e74c3c', 'danger_hover': '#c0392b',
        'purple': '#9e56bc', 'purple_hover': '#7d3c98',
        'mode': 'dark',
    },
    'Serika Dark': {
        'bg': '#323437', 'surface': '#2c2e31', 'sidebar': '#2c2e31',
        'sidebar_active': '#3c4043', 'hover': '#3c4043',
        'accent': '#e2b714', 'accent_hover': '#c9a512',
        'text': '#d1d0c5', 'sub': '#a9aaad',
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
        'text': '#dde5f5', 'sub': '#8590a8',
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
        'text': '#e4e4f4', 'sub': '#9fa1bc',
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
        'text': '#eceff4', 'sub': '#a7afbe',
        'success': '#a3be8c', 'success_hover': '#90aa7b',
        'warning': '#ebcb8b', 'warning_hover': '#d4b574',
        'danger': '#c36a73', 'danger_hover': '#a8535c',
        'purple': '#b48ead', 'purple_hover': '#9e7a97',
        'mode': 'dark',
    },
    'Gruvbox Dark': {
        # ``sidebar`` was bg (#282828): the rail had no fill of its own and read
        # as a hairline outline drawn on the window rather than as a panel.
        # bg0_hard is the same family one step down, like every other palette.
        'bg': '#282828', 'surface': '#32302f', 'sidebar': '#1d2021',
        'sidebar_active': '#3c3836', 'hover': '#45403d',
        'accent': '#fabd2f', 'accent_hover': '#e3a91c',
        'text': '#ebdbb2', 'sub': '#aca195',
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
        'text': '#f8f8f2', 'sub': '#aeb7d0',
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
        'text': '#c0caf5', 'sub': '#8890b4',
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
        'text': '#cdd6f4', 'sub': '#979aab',
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
        'text': '#073642', 'sub': '#485a60',
        'success': '#7a8c00', 'success_hover': '#6f8000',
        'warning': '#a87f00', 'warning_hover': '#9a7500',
        'danger': '#dc322f', 'danger_hover': '#c22a27',
        'purple': '#6c71c4', 'purple_hover': '#5a5eae',
        'mode': 'light',
    },
    'Gruvbox Light': {
        'bg': '#fbf1c7', 'surface': '#f2e5bc', 'sidebar': '#f2e5bc',
        'sidebar_active': '#e5d4a8', 'hover': '#d6c594',
        'accent': '#b57614', 'accent_hover': '#9a6410',
        'text': '#3c3836', 'sub': '#635850',
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
        'text': '#f8f8f2', 'sub': '#aba796',
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
        'text': '#e6e6e6', 'sub': '#8d8b93',
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
        'text': '#d5d6da', 'sub': '#9599a7',
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
        'text': '#d8dee7', 'sub': '#8996a4',
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
        'text': '#dce8dd', 'sub': '#859989',
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
        'text': '#e9dff5', 'sub': '#a499b2',
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
        'text': '#f3e8ff', 'sub': '#b09fcb',
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
        'text': '#f5e6e8', 'sub': '#a5888c',
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
        'text': '#2d1f3d', 'sub': '#5e506c',
        'success': '#149644', 'success_hover': '#15803d',
        'warning': '#c56c05', 'warning_hover': '#b45309',
        'danger': '#dc2626', 'danger_hover': '#b91c1c',
        'purple': '#a855f7', 'purple_hover': '#9333ea',
        'mode': 'light',
    },
    'Serika Light': {
        'bg': '#e8e8e8', 'surface': '#f4f4f4', 'sidebar': '#dddddd',
        'sidebar_active': '#cccccc', 'hover': '#d4d4d4',
        'accent': '#444444', 'accent_hover': '#2f2f2f',
        'text': '#323437', 'sub': '#555758',
        'success': '#398c64', 'success_hover': '#35855d',
        'warning': '#a07625', 'warning_hover': '#b07f24',
        'danger': '#c94949', 'danger_hover': '#ad3c3c',
        'purple': '#7d5bb5', 'purple_hover': '#694a9e',
        'mode': 'light',
    },
}

# Legacy hardcoded hexes -> palette role (used to recolor existing widgets).
LEGACY_HEX_ROLES = {
    '#3498db': 'accent', '#2980b9': 'accent_hover', '#21618c': 'accent_hover',
    '#27ae60': 'success', '#229954': 'success_hover', '#2ecc71': 'success',
    '#16a085': 'success', '#138d75': 'success_hover', '#1e8449': 'success_hover',
    '#f39c12': 'warning', '#d68910': 'warning_hover',
    '#e74c3c': 'danger', '#c0392b': 'danger_hover',
    '#8e44ad': 'purple', '#7d3c98': 'purple_hover', '#9b59b6': 'purple',
    '#95a5a6': 'sub', '#646669': 'sub', '#7f8c8d': 'sub',
    '#636e72': 'sidebar_active', '#57606f': 'hover',
    '#ecf0f1': 'text', '#d1d0c5': 'text', '#bdc3c7': 'text',
    '#1b2532': 'sidebar', '#1f2a3a': 'sidebar',
    '#2c3e50': 'sidebar_active', '#34495e': 'hover',
    '#2b2b2b': 'surface',
}


class _QueueRowList(ctk.CTkScrollableFrame):
    """Themed stand-in for the queue's raw ``tk.Listbox``.

    Speaks the small subset of the Listbox API that
    ``_rebuild_queue_list`` and friends use (``size``/``insert``/
    ``delete``/``get``/``itemconfig``/``curselection``), so the refresh
    logic — in-place row updates instead of a full rebuild on every
    progress tick — is unchanged. Rows are CTk labels, so they follow the
    active palette like every other widget; the old tk.Listbox sat
    entirely outside the theme walker and kept hardcoded dark rows after
    switching to a Light theme.
    """

    def __init__(self, master, palette=None, **kwargs):
        # Plain attributes first: CTkFrame.__init__ may re-configure itself
        # through the override below, which touches them.
        pal = dict(palette or {})
        self._labels: list[str] = []
        self._rows: list[Any] = []
        self._overrides: dict[int, str] = {}  # per-row text_color (placeholder)
        self._selected: int | None = None
        self._row_fg = pal.get("text", "#ecf0f1")
        self._sel_bg = pal.get("accent", "#3498db")
        self._sel_fg = _on_color(
            self._sel_bg, pal.get("text", "#ecf0f1"), pal.get("bg", "#1e1e24"))
        super().__init__(
            master,
            # The page is a surface card; filling this with the same color would
            # make its own corner notches paint themselves invisible, and it read
            # as a flat ring instead of a well. The window background color sinks
            # it into the card and the notches pick up the page's surface.
            fg_color=pal.get("bg", "#1e1e24"),
            corner_radius=UITheme.RADIUS_LG,
            border_width=UITheme.BORDER_W,
            border_color=pal.get("hover", "#34495e"),
            **kwargs,
        )
        # Let the theme walker repaint the panel like any other widget.
        setattr(self, "_theme_roles", {"fg_color": "bg",
                                       "border_color": "hover"})

    # --- Listbox-compatible API (the only surface ui.py uses) ------

    def size(self) -> int:
        return len(self._labels)

    def get(self, index: int) -> str:
        return self._labels[index] if 0 <= index < len(self._labels) else ""

    def insert(self, index, text: str) -> None:
        # Only append semantics are used (always called with tk.END).
        i = len(self._labels)
        self._labels.append(text)
        row = ctk.CTkLabel(
            self,
            text=text,
            font=UITheme.F(11),
            anchor="w",
            corner_radius=UITheme.RADIUS_MD,
            fg_color="transparent",
            text_color=self._row_fg,
            cursor="hand2",
        )
        row.pack(fill="x", padx=6, pady=1)
        row.bind("<Button-1>", lambda e, idx=i: self._select(idx))
        self._rows.append(row)

    def delete(self, first, last=None) -> None:
        # Listbox semantics: the only call shape is delete(0, tk.END).
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._labels.clear()
        self._overrides.clear()
        self._selected = None

    def itemconfig(self, index, **kwargs) -> None:
        if not isinstance(index, int) or not (0 <= index < len(self._rows)):
            return
        if "text" in kwargs:
            self._labels[index] = str(kwargs["text"])
            # Real content replaces any placeholder styling on the row.
            self._overrides.pop(index, None)
            self._rows[index].configure(text=self._labels[index])
        if "foreground" in kwargs:
            self._overrides[index] = str(kwargs["foreground"])
        self._paint(index)

    def curselection(self):
        return () if self._selected is None else (self._selected,)

    # --- theming ---------------------------------------------------

    def configure(self, cnf=None, **kwargs):
        """Accept CTk options plus the legacy Listbox theming keys
        (bg/fg/selectbackground/selectforeground) the theme pass uses."""
        data = dict(cnf or {})
        data.update(kwargs)
        for old, attr in (("fg", "_row_fg"),
                          ("selectbackground", "_sel_bg"),
                          ("selectforeground", "_sel_fg")):
            if old in data:
                setattr(self, attr, data.pop(old))
        if "bg" in data:
            data["fg_color"] = data.pop("bg")
        if data:
            super().configure(**data)
        for i in range(len(self._rows)):
            self._paint(i)

    config = configure

    def _select(self, index: int) -> None:
        if not (0 <= index < len(self._labels)):
            return
        prev, self._selected = self._selected, index  # browse: click selects
        if prev is not None and prev != index:
            self._paint(prev)
        self._paint(index)

    def _paint(self, i: int) -> None:
        if not (0 <= i < len(self._rows)):
            return
        if i == self._selected:
            self._rows[i].configure(
                fg_color=self._sel_bg, text_color=self._sel_fg)
        elif i in self._overrides:
            self._rows[i].configure(
                fg_color="transparent", text_color=self._overrides[i])
        else:
            self._rows[i].configure(
                fg_color="transparent", text_color=self._row_fg)


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
            # Off by default: a rail that throws itself open every time the
            # pointer crosses it steals attention from whatever you are doing.
            "sidebar_peek_on_hover": False,
            "color_theme": "TuneLab Dark",
            "soundcloud_direct_first": True,
            "save_folder": "",
            "audio_format": "mp3_vbr",
            "filename_template": "",
            "window_geometry": "",
            "window_maximized": False,
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
        # Resolve the path outside the try: the except handler logs with it,
        # so it must be bound even if the very first operation fails.
        path = self._pref_path
        try:
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

    def _remember_window_geometry(self) -> None:
        """Persist the current window size/position for the next launch."""
        try:
            st = str(self.state())
            if st == "zoomed":
                # Maximized: keep the last normal-size geometry (this one is
                # the screen size) but remember to re-maximize on restore.
                self._set_pref("window_maximized", True)
                return
            if st != "normal":
                # Minimized/iconic: neither value is trustworthy here; keep
                # whatever the last known-good save recorded.
                return
            self._set_pref("window_maximized", False)
            geo = str(self.geometry() or "")
            if re.match(r"^\d+x\d+[+-]\d+[+-]\d+$", geo):
                self._set_pref("window_geometry", geo)
        except Exception:
            pass

    def _restore_window_geometry(self) -> None:
        """Reapply the saved window geometry when it still fits a screen.

        Also re-maximizes the window when it was closed that way: the state()
        read happens while the window is still withdrawn (before deiconify),
        and Tk applies the zoomed state when the window maps.
        """
        geo = str(self._prefs.get("window_geometry", "") or "").strip()
        m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geo)
        if m:
            # Clamp to the window's own minimum so a stale tiny geometry can't
            # make the app unusable (minsize(800, 680) in __init__).
            w = max(int(m.group(1)), 800)
            h = max(int(m.group(2)), 680)
            x = int(m.group(3))
            y = int(m.group(4))
            try:
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
            except Exception:
                sw = sh = 0
            # Skip the restore when the saved frame is fully off every attached
            # screen (monitor unplugged since last run): Tk would place it there
            # anyway and the window would be unreachable.
            if sw and sh and not (x >= sw or y >= sh or x + w <= 0 or y + h <= 0):
                try:
                    self.geometry(f"{w}x{h}{m.group(3)}{m.group(4)}")
                except Exception:
                    pass
        if self._prefs.get("window_maximized"):
            try:
                self.state("zoomed")
            except Exception:
                pass  # Tk builds without a zoomed state (macOS) keep geometry.

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

    def _apply_app_icon(self) -> None:
        """Give the window TuneLab's own icon.

        Tk's default is the feather, and that is what the taskbar, the title bar
        and Alt-Tab show until something replaces it. The asset sits next to the
        source in a checkout and in the bundle root when frozen (the PyInstaller
        spec bundles it), so one lookup covers both.

        The PNG goes through iconphoto, which every Tk honours; the .ico is set
        too on Windows, where the taskbar entry prefers it. Neither is worth a
        dialog at startup, so both are best effort.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        # Frozen copies bundle the assets next to the executable (sys._MEIPASS);
        # a checkout has them under assets/. Both are one lookup away.
        roots = [getattr(sys, "_MEIPASS", None), here,
                 os.path.join(here, "assets")]

        def _find(name: str) -> str | None:
            for root in roots:
                if not root:
                    continue
                path = os.path.join(root, name)
                if os.path.isfile(path):
                    return path
            return None

        png = _find("tune_lab.png")
        ico = _find("tune_lab.ico")
        # Windows gets the .ico: it is the format the shell already understands,
        # and Tk's own photo->HICON conversion renders the PNG into the title
        # bar's slot stretched. Everywhere else (macOS, Linux) uses the PNG,
        # which is the only thing iconphoto reads.
        if sys.platform == "win32" and ico:
            try:
                self.iconbitmap(ico)
            except Exception:
                logger.debug("iconbitmap failed", exc_info=True)
        elif png:
            try:
                photo = tk.PhotoImage(file=png)
                self.iconphoto(True, photo)
                self._app_icon = photo      # or the image is collected away
            except Exception:
                logger.debug("iconphoto failed", exc_info=True)

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

        # Build the UI with the window hidden, then reveal it once at the end.
        # While it is mapped, Tk does geometry and redraw work for each of the
        # ~112 widgets as they are added, which is the largest single cost of
        # starting up - and it means the user watches a half-drawn window
        # assemble itself. customtkinter's withdraw() is written for exactly
        # this: it notes the window was never shown and re-shows it on the
        # first update()/mainloop(). We also reveal it explicitly at the end so
        # this never depends on that behaviour staying.
        self.withdraw()

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
        self._preview_via_sd = False
        self._clipper_vlc_instance = None
        self._clipper_vlc_player = None
        self._clipper_auto_select = False
        self._clipper_fast_detect = False
        self._clipper_hover_preview_enabled = True
        self._clipper_hover_preview_muted = True
        self._clipper_thumb_generated = set()
        # Toast stack, unfocused-completion badge and view caches (declared
        # here so Pylance sees them before first use).
        self._active_toasts: list[Any] = []
        self._toast_anim_id: Any = None
        self._unfocused_done: int = 0
        self._queue_refresh_pending: bool = False
        self._history_view_sig: Any = None
        # Last URL/mode started, so a failed download's dialog can offer
        # "Try again" without the user re-pasting the link.
        self._last_dl_url: str = ""
        self._last_dl_was_video: bool = False

        self.title("TuneLab")
        self._apply_app_icon()
        # Let packed widgets determine natural size; no fixed geometry.
        self.minsize(800, 680)

        # Set window background to match theme (prevents white border)
        bg_color = self._palette.get('bg', '#2b2b2b')
        self.configure(bg=bg_color, highlightthickness=0, bd=0)
        try:
            self.attributes('-bg', bg_color)
        except Exception:
            pass

        # Handle window close event to stop the video bridge server
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        self.studio_file_path = None

        # -----------------
        # Collapsible navigation rail (slides by clipping, never re-lays out)
        # -----------------
        self._sidebar_expanded = not bool(self._prefs.get("sidebar_collapsed", False))
        self._sb_shown = self._sidebar_expanded   # state the pixels currently show
        self._sb_anim_after_id = None
        # Hover / press / peek state. Everything visual is derived from these
        # three in _repaint_nav_rows, so no handler has to guess what a row
        # should look like.
        self._active_nav_name = None
        self._sb_hover = None
        self._sb_press = None
        self._sb_peek_after_id = None
        self._sb_peeking = False
        self._sb_peek_pref = False
        self._sb_popups = []      # the one reused tooltip popup, tracked so a
                                  # failed configure can never strand it on screen
        self._sb_tooltip_after_id = None
        self._sb_tip_owner = None  # the row the bubble is describing
        self._sb_badges = {}
        self._sb_footer_word = None
        self._sb_h = 0
        # A floating rounded rail rather than a flush panel: insetting it and
        # giving it the card radius plus a hairline edge is what makes the
        # window read as "curved" instead of four square slabs.
        self.sidebar = ctk.CTkFrame(
            self,
            width=UITheme.SB_W_EXPANDED if self._sidebar_expanded else UITheme.SB_W_COLLAPSED,
            corner_radius=UITheme.RADIUS_CARD,
            fg_color=self._palette.get('sidebar', UITheme.SIDEBAR_BG),
            border_width=UITheme.BORDER_W,
            border_color=self._palette.get('hover', UITheme.SIDEBAR_HOVER),
        )
        setattr(self.sidebar, "_theme_roles",
                {"fg_color": "sidebar", "border_color": "hover"})
        self.sidebar.pack(side="left", fill="y", padx=(12, 0), pady=12)
        self.sidebar.pack_propagate(False)
        self._sidebar_cur_w = UITheme.SB_W_EXPANDED if self._sidebar_expanded else UITheme.SB_W_COLLAPSED

        # -----------------
        # Main area (header + pages)
        # -----------------
        self.main_container = ctk.CTkFrame(self, fg_color=self._palette.get('bg', UITheme.SURFACE_BG))
        self.main_container.pack(side="left", fill="both", expand=True)

        self._header_frame = ctk.CTkFrame(self.main_container, fg_color="transparent", height=42)
        # Lines the title up with the left edge of the content card below it.
        self._header_frame.pack(fill="x", padx=12, pady=(14, 0))
        self.title_lbl = ctk.CTkLabel(
            self._header_frame,
            text="",
            font=UITheme.SECTION_FONT,
            anchor="w",
        )
        self.title_lbl.pack(side="left")

        # Central content area. This one deliberately stays a transparent
        # hole: a filled frame here would paint its own square canvas over the
        # rounded corners the pages draw, which is exactly what made the card
        # look square. The pages below carry the surface, radius and edge.
        self.content_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.content_frame.pack(
            pady=12,
            padx=(12, 12),
            fill="both",
            expand=True,
        )

        self.tab_downloader = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_studio = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_customization = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_performance = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_queue = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.tab_history = ctk.CTkFrame(self.content_frame, fg_color="transparent")

        # Every page is a card: same surface, same corner radius and same
        # hairline edge as the dialogs. They are stacked in one hole and
        # raised on switch, so they all need the identical shape or a switch
        # would flash a differently edged panel.
        for _page in (self.tab_downloader, self.tab_studio,
                      self.tab_customization, self.tab_performance,
                      self.tab_queue, self.tab_history):
            _page.configure(
                fg_color=self._palette.get('surface', UITheme.SURFACE_BG),
                corner_radius=UITheme.RADIUS_CARD,
                border_width=UITheme.BORDER_W,
                border_color=self._palette.get('hover', UITheme.SIDEBAR_HOVER),
            )
            setattr(_page, "_theme_roles",
                    {"fg_color": "surface", "border_color": "hover"})

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

        # Motion + rail preferences are read before any view is built: the
        # "Sidebar & motion" card seeds its widgets from these values.
        try:
            self.nav_anim_enabled = bool(self._prefs.get('nav_animation_enabled', True))
            self.nav_anim_speed = int(self._prefs.get('nav_animation_speed', 12) or 12)
        except Exception:
            self.nav_anim_enabled = True
            self.nav_anim_speed = 12
        # Hover-peek only means something while the rail is collapsed, and it
        # stays off unless it was asked for.
        self._sb_peek_pref = bool(self._prefs.get('sidebar_peek_on_hover', False))

        self.build_downloader_view()
        self.build_studio_view()
        self.build_customization_view()
        self.build_performance_view()
        self.build_queue_view()
        self.build_history_view()

        # -----------------
        # Rail contents (brand, grouped nav rows, badge slots, footer toggle)
        # -----------------

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

        # Quick nav: Ctrl+1..6 jump straight to a page.
        for _n, _page in enumerate(
            ("downloader", "queue", "history", "studio", "settings", "performance"),
            start=1,
        ):
            self.bind(f'<Control-{_n}>', lambda e, p=_page: self.show_frame(p))

        # Clear the unfocused-completion badge as soon as the user looks at
        # the app again.
        self.bind('<FocusIn>', lambda e: self._clear_completion_badge())

        # Start by showing downloader
        self.show_frame('downloader')

        # Everything exists now. Resolve the layout in one pass and show the
        # finished window, instead of letting Tk reveal it widget by widget.
        self._restore_window_geometry()
        self.update_idletasks()
        self.deiconify()
        # customtkinter 6.0 withdraws the window inside its own __init__
        # (titlebar repaint) and again on the first mainloop(), while its
        # recovery deiconify() is skipped whenever withdraw() ran before the
        # window was flagged as shown - which ours does above. Pumping one
        # update() here flags the window as shown (its update() override
        # sets _window_exists) and paints it once, so mainloop() takes no
        # further window-state actions. Without this the app starts with a
        # hidden window: mainloop runs, no error, nothing on screen.
        self.update()
        # Caret in the URL box from the first frame: paste + Enter should
        # work without reaching for the mouse first.
        self.url_entry.focus_set()

    def _start_worker(self, target):
        threading.Thread(target=target, daemon=True).start()

    def after(self, ms, func=None, *args, **kwargs):  # type: ignore[reportIncompatibleMethodOverride]
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
        # Page title lives in the header only (title_lbl) — no in-page H1.

        self.url_entry = ctk.CTkEntry(
            self.tab_downloader,
            width=300,
            height=36,
            corner_radius=UITheme.RADIUS_FIELD,
            border_width=UITheme.BORDER_W,
            border_color=(getattr(self, "_palette", {}) or {}).get(
                "hover", UITheme.SIDEBAR_HOVER),
            placeholder_text="Paste link or search song name"
        )
        setattr(self.url_entry, "_theme_roles", {"border_color": "hover"})
        self.url_entry.pack(pady=(14,8), padx=UITheme.PAD_X, fill="x")

        # Inline clear button: overlays the field's right edge and only shows
        # while there is text to clear (Escape does the same from the keyboard).
        self.btn_clear_url = ctk.CTkButton(
            self.url_entry, text="✕", width=24, height=24,
            fg_color="transparent", corner_radius=12, cursor="hand2",
            command=self._on_clear_url_clicked,
        )
        self._style_button(self.btn_clear_url, "ghost", bordered=False)
        try:
            # The ✕ should read as muted (sub), not full-strength text.
            sub = (getattr(self, "_palette", {}) or {}).get("sub", "#95a5a6")
            self.btn_clear_url.configure(text_color=sub)
            setattr(self.btn_clear_url, "_theme_roles",
                    {**self._BTN_ROLES["ghost"], "text_color": "sub"})
        except Exception:
            pass
        self._url_clear_btn_visible = False

        # Row 1: the two download actions side by side (they used to be two
        # more stacked full-width rows). MP3 is the screen's one accent.
        action_row = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        action_row.pack(pady=(2, 6))

        self.btn_download_mp3 = ctk.CTkButton(
            action_row,
            text="Download Audio (MP3)",
            width=200, height=36,
            cursor="hand2",
            command=lambda: self._start_worker(self.download_mp3)
        )
        self.btn_download_mp3.pack(side="left", padx=6)
        self._style_button(self.btn_download_mp3, "primary")

        self.btn_download_mp4 = ctk.CTkButton(
            action_row,
            text="Download Video (MP4)",
            width=200, height=36,
            cursor="hand2",
            command=lambda: self._start_worker(self.download_mp4)
        )
        self.btn_download_mp4.pack(side="left", padx=6)
        self._style_button(self.btn_download_mp4, "secondary")

        # Row 2: previews in a single row (was a 2x2 grid); stops are danger.
        preview_frame = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        preview_frame.pack(pady=(0, 8))

        self.btn_preview_audio = ctk.CTkButton(
            preview_frame,
            text="▶ Preview Audio",
            width=118, height=28,
            cursor="hand2",
            command=lambda: self._start_worker(self.preview_audio)
        )
        self.btn_preview_audio.grid(row=0, column=0, padx=4)
        self._style_button(self.btn_preview_audio, "secondary")
        self.btn_stop_audio = ctk.CTkButton(
            preview_frame,
            text="⏹ Stop Audio",
            width=118, height=28,
            cursor="hand2",
            command=self.stop_preview_audio,
            state="disabled"
        )
        self.btn_stop_audio.grid(row=0, column=1, padx=4)
        self._style_button(self.btn_stop_audio, "danger")

        self.btn_preview_video = ctk.CTkButton(
            preview_frame,
            text="▶ Preview Video",
            width=118, height=28,
            cursor="hand2",
            command=lambda: self._start_worker(self.preview_video)
        )
        self.btn_preview_video.grid(row=0, column=2, padx=4)
        self._style_button(self.btn_preview_video, "secondary")
        self.btn_stop_video = ctk.CTkButton(
            preview_frame,
            text="⏹ Stop Video",
            width=118, height=28,
            cursor="hand2",
            command=self.stop_preview_video,
            state="disabled"
        )
        self.btn_stop_video.grid(row=0, column=3, padx=4)
        self._style_button(self.btn_stop_video, "danger")

        self.url_entry.bind("<Enter>", lambda e: self._set_hover_detail("Paste link or search song. Spotify links are auto-converted.", "#bdc3c7"))
        self.url_entry.bind("<Leave>", lambda e: self._restore_hover_detail())
        # Enter = the obvious next action: one input downloads right away,
        # a multi-URL paste goes to the queue. (#1 on the UI wish list.)
        self.url_entry.bind("<Return>", self._on_url_enter)
        self.url_entry.bind("<KP_Enter>", self._on_url_enter)
        # Escape empties the box without leaving the field.
        self.url_entry.bind("<Escape>", self._clear_url_entry)
        self.url_entry.bind("<KeyRelease>", lambda e: self._sync_clear_btn())
        # A paste that skips the keyboard (context menu) fires no KeyRelease
        # on the entry; re-check right after the class binding inserted text.
        self.url_entry.bind(
            "<<Paste>>", lambda e: self.after(1, self._sync_clear_btn)
        )
        self.btn_download_mp3.bind("<Enter>", lambda e: self._set_hover_detail("Download MP3 with artwork and metadata.", "#ecf0f1"))
        self.btn_download_mp3.bind("<Leave>", lambda e: self._restore_hover_detail())
        self.btn_download_mp4.bind("<Enter>", lambda e: self._set_hover_detail("Download high quality MP4 video.", "#ecf0f1"))
        self.btn_download_mp4.bind("<Leave>", lambda e: self._restore_hover_detail())
        self.btn_preview_audio.bind("<Enter>", lambda e: self._set_hover_detail("Listen to a quick 30s audio preview.", "#ecf0f1"))
        self.btn_preview_audio.bind("<Leave>", lambda e: self._restore_hover_detail())
        self.btn_preview_video.bind("<Enter>", lambda e: self._set_hover_detail("Preview video in the stream player.", "#ecf0f1"))
        self.btn_preview_video.bind("<Leave>", lambda e: self._restore_hover_detail())

        # Status pill: a true capsule (radius = half its height) + colored dot
        # instead of bare text.
        pal = getattr(self, "_palette", {}) or {}
        pill_bg = pal.get("sidebar_active", UITheme.SIDEBAR_ACTIVE)
        self._status_pill = ctk.CTkFrame(
            self.tab_downloader,
            corner_radius=15,
            fg_color=pill_bg,
        )
        self._status_pill.pack(pady=(0, 6))
        setattr(self._status_pill, "_theme_roles", {"fg_color": "sidebar_active"})
        self._status_dot = ctk.CTkLabel(
            self._status_pill, text="●", font=UITheme.F(10),
            text_color=pal.get("sub", UITheme.COLOR_GRAY),
        )
        self._status_dot.pack(side="left", padx=(14, 6), pady=(5, 5))
        setattr(self._status_dot, "_theme_roles", {"text_color": "sub"})
        self.dl_status = ctk.CTkLabel(
            self._status_pill, text="System Ready", font=UITheme.F(12),
            text_color=_on_color(
                pill_bg, pal.get("text", "#ecf0f1"), pal.get("bg", "#1e1e24")),
        )
        self.dl_status.pack(side="left", padx=(0, 14), pady=(5, 5))
        setattr(self.dl_status, "_theme_roles", {"text_color": "text"})

        # Progress row: bar + cancel side by side. Cancel only enables while
        # a download is in flight, so it no longer deserves its own row.
        progress_row = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        progress_row.pack(fill="x", padx=UITheme.PAD_X, pady=(0, 4))
        self.progress_bar = ctk.CTkProgressBar(progress_row, width=200)
        self.progress_bar.set(0)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.btn_cancel_download = ctk.CTkButton(
            progress_row,
            text="⏹ Cancel Download",
            width=150, height=28,
            cursor="hand2",
            command=self._cancel_active_download,
            state="disabled",
        )
        self.btn_cancel_download.pack(side="left", padx=(10, 0))
        self._style_button(self.btn_cancel_download, "danger")

        self.dl_detail = ctk.CTkLabel(self.tab_downloader, text="", font=UITheme.F(11), text_color="#95a5a6")
        self.dl_detail.pack(pady=(0, 2))

        # Speed + ETA label below progress bar
        self.dl_speed_lbl = ctk.CTkLabel(self.tab_downloader, text="", font=UITheme.F(11), text_color="#7f8c8d")
        self.dl_speed_lbl.pack(pady=(0, 4))

        # Row: save folder + audio quality side by side (was two stacked rows).
        settings_row = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        settings_row.pack(pady=(0, 2))
        self.btn_choose_folder = ctk.CTkButton(
            settings_row,
            text="📁 Change Save Folder",
            width=175, height=30,
            cursor="hand2",
            command=self._choose_save_folder,
        )
        self.btn_choose_folder.pack(side="left", padx=(0, 8))
        self._style_button(self.btn_choose_folder, "secondary")
        ctk.CTkLabel(settings_row, text="Audio quality:", font=UITheme.F(12)).pack(side="left", padx=(0, 8))
        self.fmt_option = ctk.CTkOptionMenu(
            settings_row,
            width=190,
            values=[p["label"] for p in downloader.AUDIO_FORMATS.values()],
            command=self._on_audio_format_change,
        )
        self.fmt_option.set(
            downloader.AUDIO_FORMATS.get(
                downloader.get_audio_format(), {"label": "MP3 (VBR High)"}
            )["label"]
        )
        self.fmt_option.pack(side="left")
        self._style_option_menu(self.fmt_option)

        self.save_folder_lbl = ctk.CTkLabel(
            self.tab_downloader,
            text="",
            font=UITheme.F(11),
            text_color="#95a5a6",
            wraplength=340,
            justify="center",
        )
        self.save_folder_lbl.pack(padx=UITheme.PAD_X, pady=(2, 4))

        # Outdated yt-dlp notice (hidden until a check finds one).
        self._ytdlp_notice_lbl = ctk.CTkLabel(
            self.tab_downloader,
            text="",
            font=UITheme.F(11, "bold"),
            text_color="#f39c12",
            wraplength=340,
            justify="center",
        )
        self._ytdlp_notice_lbl.pack(padx=UITheme.PAD_X, pady=(0, 2))
        self._update_save_folder_label()

        # place downloader frame in content area
        self.tab_downloader.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Embedded preview panel (hidden until used; re-packs at the end
        # when a preview starts, so build order here does not matter).
        self.preview_panel = tk.Frame(self.tab_downloader, bg="black", width=280, height=170)
        self.preview_panel.pack(pady=(4, 8))
        self.preview_panel.pack_forget()

        # --- Queue + Tag Editor controls ---
        controls_row = ctk.CTkFrame(self.tab_downloader, fg_color="transparent")
        controls_row.pack(pady=(0, 4))

        self.btn_add_to_queue = ctk.CTkButton(
            controls_row, text="➕ Add to Queue", width=130, height=28,
            command=self._add_current_to_queue)
        self.btn_add_to_queue.pack(side="left", padx=(0, 8))
        self._style_button(self.btn_add_to_queue, "secondary")

        self.btn_edit_tags = ctk.CTkButton(
            controls_row, text="Edit Tags", width=100, height=28,
            command=self._open_tag_editor, state="disabled")
        self.btn_edit_tags.pack(side="left", padx=(8, 0))
        self._style_button(self.btn_edit_tags, "secondary")

        self._last_downloaded_file = None
        self._last_dl_was_video = False

    def _sync_clear_btn(self):
        """Show the inline ✕ only while the URL box has text."""
        try:
            show = bool(self.url_entry.get().strip())
            if show == self._url_clear_btn_visible:
                return
            self._url_clear_btn_visible = show
            if show:
                self.btn_clear_url.place(relx=1.0, rely=0.5, anchor="e", x=-6)
            else:
                self.btn_clear_url.place_forget()
        except Exception:
            pass

    def _clear_url_entry(self, _event=None):
        """Escape (and the ✕ button) empty the URL box; keep focus in it."""
        try:
            self.url_entry.delete(0, "end")
        except Exception:
            pass
        self._sync_clear_btn()
        return "break"

    def _on_clear_url_clicked(self):
        self._clear_url_entry()
        self.url_entry.focus_set()

    def _on_url_enter(self, _event=None):
        """Pressing Enter in the URL box starts the obvious next action.

        A single URL (or a search term / Spotify URI) downloads right away;
        multiple pasted URLs go to the queue instead, matching what the
        buttons do. Returns "break" so Tk doesn't beep or move focus.
        """
        try:
            if str(self.btn_download_mp3.cget("state")) == "disabled":
                # A direct download is running (the buttons are disabled
                # while it is); mirror the buttons instead of racing it.
                return "break"
        except Exception:
            pass
        raw = self.url_entry.get().strip()
        if not raw:
            return "break"
        if len(self._split_input_urls(raw)) > 1:
            self._add_current_to_queue()
        else:
            self._start_worker(self.download_mp3)
        return "break"

    def _add_current_to_queue(self):
        """Add the current URL(s) to the queue without starting download.

        Supports bulk paste (one URL per line, or comma/semicolon separated)
        and auto-expands playlist/collection URLs into their individual tracks
        so a pasted YouTube/Spotify/SoundCloud playlist fills the queue instead
        of downloading a single item.
        """
        raw = self.url_entry.get().strip()
        if not raw:
            self.show_toast("Please enter a URL first.", "warning")
            return
        urls = self._split_input_urls(raw)
        if not urls:
            self.show_toast("No valid URLs found.", "warning")
            return
        self._init_queue_manager()
        if not self._queue_manager:
            self._show_error_dialog("Queue Error", "Queue manager is not available.")
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
            self._show_error_dialog("Queue Error", "Queue manager is not available.")
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
                self._bump_completion_badge()
        self.after(0, _do)
        # This callback runs on the queue worker thread; never touch Tk directly.
        self.after(0, self._refresh_queue_tab)

    def _on_queue_progress(self, item, pct=None, text=None):
        """Called from the queue worker thread; refresh the queue display."""
        if pct is not None:
            item._progress = pct
        self._schedule_queue_refresh()

    def _schedule_queue_refresh(self):
        """Coalesce refresh storms from the worker thread.

        yt-dlp fires progress callbacks many times a second; queueing one
        full refresh per event flooded the Tk event loop (visible stutter)
        for updates the eye cannot resolve anyway. At most one refresh per
        ~120 ms keeps the display smooth for a fraction of the cost.
        """
        if self._queue_refresh_pending:
            return
        self._queue_refresh_pending = True

        def _run():
            self._queue_refresh_pending = False
            self._refresh_queue_tab()

        self.after(120, _run)

    def _refresh_queue_tab(self):
        try:
            self._rebuild_queue_list()
        except Exception:
            pass

    def build_queue_view(self):
        # Page title lives in the header only — no in-page H1.

        # Themed row list (CTk labels) replacing the raw tk.Listbox, so the
        # queue follows the active palette like every other widget.
        pal = getattr(self, "_palette", {}) or {}
        self.queue_rows = _QueueRowList(self.tab_queue, palette=pal)
        self.queue_rows.pack(pady=(14, 12), padx=UITheme.PAD_X,
                             fill="both", expand=True)

        queue_btn_row = ctk.CTkFrame(self.tab_queue, fg_color="transparent")
        queue_btn_row.pack(pady=(0, 6))

        self.btn_queue_start = ctk.CTkButton(
            queue_btn_row, text="▶ Start", width=90, height=30,
            command=self._queue_start)
        self.btn_queue_start.pack(side="left", padx=4)
        self._style_button(self.btn_queue_start, "primary")

        self.btn_queue_remove = ctk.CTkButton(
            queue_btn_row, text="🗑 Remove", width=90, height=30,
            command=self._queue_remove_selected)
        self.btn_queue_remove.pack(side="left", padx=4)
        self._style_button(self.btn_queue_remove, "danger")

        self.btn_queue_retry = ctk.CTkButton(
            queue_btn_row, text="↻ Retry", width=90, height=30,
            command=self._queue_retry_failed)
        self.btn_queue_retry.pack(side="left", padx=4)
        self._style_button(self.btn_queue_retry, "secondary")

        self.btn_queue_cancel = ctk.CTkButton(
            queue_btn_row, text="✖ Cancel", width=90, height=30,
            command=self._queue_cancel_all)
        self.btn_queue_cancel.pack(side="left", padx=4)
        self._style_button(self.btn_queue_cancel, "danger")

        # Second row: six buttons in one row (~600px) clipped off the right
        # edge at the 800px minimum window width, especially with the
        # sidebar expanded, so the maintenance actions sit on their own row.
        queue_btn_row2 = ctk.CTkFrame(self.tab_queue, fg_color="transparent")
        queue_btn_row2.pack(pady=(0, 10))

        self.btn_queue_clear = ctk.CTkButton(
            queue_btn_row2, text="🧹 Clear", width=90, height=30,
            command=self._queue_clear)
        self.btn_queue_clear.pack(side="left", padx=4)
        self._style_button(self.btn_queue_clear, "danger")

        self.btn_queue_clear_done = ctk.CTkButton(
            queue_btn_row2, text="✓ Clear Done", width=100, height=30,
            command=self._queue_clear_completed)
        self.btn_queue_clear_done.pack(side="left", padx=4)
        self._style_button(self.btn_queue_clear_done, "secondary")

        self.queue_status = ctk.CTkLabel(self.tab_queue, text="Idle", font=UITheme.F(11),
                                         text_color="#95a5a6")
        self.queue_status.pack(pady=(6, 0))

        self.tab_queue.place(relx=0, rely=0, relwidth=1, relheight=1)

    @staticmethod
    def _queue_row_label(idx, item, active_idx):
        """Format one queue row; kept pure so tests can pin the rendering."""
        icon = {"pending": "⏳", "active": "▶", "done": "✓", "failed": "✗",
                "skipped": "⊘", "cancelled": "⊘"}.get(item.status, "?")
        # Show progress hint for the active item (e.g. "▶ [ACTIVE] 45% ...").
        if idx == active_idx and item.status == "active":
            progress = getattr(item, '_progress', None)
            pct = f" {int(progress*100)}%" if progress is not None else ""
            return f"{icon} [ACTIVE]{pct} {item.url[:45]}"
        return f"{icon} [{item.status.upper():>9}] {item.url[:50]}"

    def _rebuild_queue_list(self):
        lb = self.queue_rows
        if not self._queue_manager:
            lb.delete(0, tk.END)
            self._queue_show_empty_placeholder()
            self._update_queue_status()
            return
        active_idx = self._queue_manager.active_index
        labels = [self._queue_row_label(i, item, active_idx)
                  for i, item in enumerate(self._queue_manager.items)]
        if not labels:
            lb.delete(0, tk.END)
            self._queue_show_empty_placeholder()
        elif lb.size() != len(labels):
            lb.delete(0, tk.END)
            for label in labels:
                lb.insert(tk.END, label)
        else:
            # Same row count (the usual progress-tick case): touch only the
            # rows whose text changed. The old delete-everything + re-insert
            # ran on every yt-dlp progress callback — it flooded the event
            # loop (stutter), repainted the whole list, and threw away the
            # selection on every tick.
            for i, label in enumerate(labels):
                if lb.get(i) != label:
                    lb.itemconfig(i, text=label)
        self._update_queue_status()

    def _queue_show_empty_placeholder(self):
        """Fill the blank row list with a hint so an empty queue reads as
        'nothing here yet' instead of a dead panel."""
        pal = getattr(self, "_palette", {}) or {}
        self.queue_rows.insert(
            tk.END, "  Queue is empty — paste links on the Downloader page")
        try:
            self.queue_rows.itemconfig(0, foreground=pal.get("sub", "#95a5a6"))
        except Exception:
            pass

    def _update_queue_status(self):
        """Status line: "3 of 5 items • 60% • Active"."""
        items = self._queue_manager.items if self._queue_manager else []
        total = len(items)
        done = sum(1 for it in items if it.status in ("done", "failed", "skipped"))
        overall = int((done / total) * 100) if total else 0
        running = bool(self._queue_manager and self._queue_manager.is_running)
        self.queue_status.configure(
            text=f"{done}/{total} items • {overall}% • {'Active' if running else 'Idle'}")
        # Same number in the rail's right slot, so the collapsed rail still
        # reports work waiting for its turn (0 clears back to the "2" digit).
        self._set_nav_badge('queue', max(0, total - done))

    def _queue_start(self):
        self._init_queue_manager()
        if self._queue_manager and self._queue_manager.items and not self._queue_manager.is_running:
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
        if not getattr(self._queue_manager, "items", None):
            # Only the empty-state placeholder row is in the list.
            return
        sel = self.queue_rows.curselection()
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
        # Page title lives in the header only — no in-page H1.

        # Top controls: Clear History button + count label
        ctrl_row = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        ctrl_row.pack(fill="x", padx=UITheme.PAD_X, pady=(14, 6))

        self.btn_clear_history = ctk.CTkButton(
            ctrl_row, text="🧹 Clear History", width=140, height=30,
            command=self._clear_history,
        )
        self.btn_clear_history.pack(side="left")
        self._style_button(self.btn_clear_history, "danger")

        self.history_count_lbl = ctk.CTkLabel(ctrl_row, text="0 entries", font=UITheme.F(12), text_color="#95a5a6")
        self.history_count_lbl.pack(side="left", padx=(12, 0))

        # Search bar
        search_row = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        search_row.pack(fill="x", padx=20, pady=(0, 6))
        self.history_search_var = ctk.StringVar()
        self.history_search_entry = ctk.CTkEntry(
            search_row, placeholder_text="🔍 Search by title or URL...",
            textvariable=self.history_search_var, height=32,
            corner_radius=UITheme.RADIUS_FIELD,
            border_width=UITheme.BORDER_W,
            border_color=(getattr(self, "_palette", {}) or {}).get(
                "hover", UITheme.SIDEBAR_HOVER),
        )
        setattr(self.history_search_entry, "_theme_roles", {"border_color": "hover"})
        self.history_search_entry.pack(side="left", fill="x", expand=True)
        self.history_search_var.trace_add("write", lambda *_: self._refresh_history_view())

        # Sort controls
        self._history_sort_key = "date"
        self._history_sort_reverse = True
        sort_row = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        sort_row.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(sort_row, text="Sort by:", font=UITheme.F(11), text_color="#95a5a6").pack(side="left")
        for lbl, key in [("Date", "date"), ("Title", "title"), ("Status", "status")]:
            btn = ctk.CTkButton(
                sort_row, text=lbl, width=60, height=24,
                font=UITheme.F(10),
                command=lambda k=key: self._set_history_sort(k),
            )
            self._style_button(btn, "ghost")
            btn.pack(side="left", padx=2)

        # Scrollable list of history entries
        _pal = getattr(self, "_palette", {}) or {}
        self.history_scroll = ctk.CTkScrollableFrame(
            self.tab_history, label_text="Past Downloads",
            corner_radius=UITheme.RADIUS_LG,
            border_width=UITheme.BORDER_W,
            # Left on its defaults this panel kept CustomTkinter's own gray,
            # which sat on top of the palette like a foreign window. The page is
            # a surface card, so the well is the window background color: that
            # contrast is also what makes its rounded corners visible.
            fg_color=_pal.get("bg", UITheme.SURFACE_BG),
            border_color=_pal.get("hover", UITheme.SIDEBAR_HOVER),
            label_fg_color=_pal.get("hover", UITheme.SIDEBAR_HOVER),
            label_text_color=_pal.get("text", "#ecf0f1"),
        )
        setattr(self.history_scroll, "_theme_roles",
                {"fg_color": "bg", "border_color": "hover",
                 "label_fg_color": "hover", "label_text_color": "text"})
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

    def _refresh_history_view(self, force: bool = False):
        """Rebuild the history entry list from the saved history file.

        Skips the (expensive) widget rebuild when the filtered and sorted
        result matches what is already on screen: show_frame calls this
        every time the tab opens, and recreating every row on each visit
        was a visible stutter.
        """
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

        # Signature of what should be on screen; identical content means
        # the widgets are already correct and can be left alone.
        sig = (
            query,
            self._history_sort_key,
            self._history_sort_reverse,
            tuple(
                (e.get("url"), e.get("title"), e.get("status"),
                 e.get("filepath"), e.get("completed_at"))
                for e in history
            ),
        )
        if not force and sig == self._history_view_sig:
            return
        self._history_view_sig = sig

        for w in self._history_entries_frame.winfo_children():
            w.destroy()

        self.history_count_lbl.configure(text=f"{len(history)} entries")

        if not history:
            msg = "No matching history entries." if query else "No download history yet.\nDownloads you complete will appear here."
            ctk.CTkLabel(
                self._history_entries_frame,
                text=msg,
                font=UITheme.F(12), text_color="#95a5a6",
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

        title_lbl = ctk.CTkLabel(left, text=title, font=UITheme.F(12, "bold"),
                                 anchor="w", wraplength=380, justify="left")
        title_lbl.pack(anchor="w")

        meta_parts = [f"{'🎬' if is_video else '🎵'} {date_str}"]
        meta_parts.append(f"File: {'✓ exists' if file_exists else '✗ missing'}")
        meta_text = "  |  ".join(meta_parts)
        meta_lbl = ctk.CTkLabel(left, text=meta_text, font=UITheme.F(10),
                                text_color="#7f8c8d", anchor="w")
        meta_lbl.pack(anchor="w")

        # Right: status badge + buttons
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right")

        status_lbl = ctk.CTkLabel(right, text=status.upper(), font=UITheme.F(10, "bold"),
                                  text_color=status_color, width=70)
        status_lbl.pack(side="left", padx=(0, 6))

        # Re-download button
        btn_redl = ctk.CTkButton(
            right, text="↻", width=34, height=28,
            command=lambda u=url, v=is_video: self._redownload(u, v),
        )
        self._style_button(btn_redl, "primary")
        btn_redl.pack(side="left", padx=2)
        # Glyph-only buttons get hover tooltips (reusing the sidebar popup,
        # which already avoids the transient-black-flicker traps).
        btn_redl.bind("<Enter>", lambda e, b=btn_redl: self._schedule_sb_tooltip(b, "Re-download this URL", only_when_collapsed=False))
        btn_redl.bind("<Leave>", lambda e: self._hide_sb_tooltip())

        # Open file button (only if file exists)
        if file_exists:
            btn_open = ctk.CTkButton(
                right, text="📂", width=34, height=28,
                command=lambda p=filepath: self._open_file(p),
            )
            self._style_button(btn_open, "secondary")
            btn_open.pack(side="left", padx=2)
            btn_open.bind("<Enter>", lambda e, b=btn_open: self._schedule_sb_tooltip(b, "Open file", only_when_collapsed=False))
            btn_open.bind("<Leave>", lambda e: self._hide_sb_tooltip())

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
            self._show_error_dialog("Re-download Error",
                                    "Could not start the re-download.",
                                    detail=str(e))

    @staticmethod
    def _open_file_command(filepath, mode="open", platform=None, is_dir=False):
        """Return the OS command that opens *filepath* or reveals its folder.

        Extracted from _open_file so the platform branching is testable
        without a live Tk window. ``mode`` is "open" (launch with the
        default app) or "reveal" (show it in a file manager). Returns a
        Popen argv list, or a shell string on Windows where ``start`` is a
        shell builtin rather than an executable.
        """
        plat = platform if platform is not None else sys.platform
        if plat == "darwin":
            # macOS: `open` launches the file; `-R` reveals it in Finder.
            return ["open", filepath] if mode == "open" else ["open", "-R", filepath]
        if plat.startswith(("linux", "freebsd", "openbsd")):
            return ["xdg-open", filepath]
        # Windows (and anything else): the old code always ran `start`, which
        # silently did nothing on Mac/Linux.
        if mode == "reveal":
            return ["explorer", "/select,", filepath]
        if is_dir:
            return ["explorer", filepath]
        return f'start "" "{filepath}"'

    def _open_file(self, filepath):
        """Open a downloaded file with the system default app."""
        is_dir = os.path.isdir(filepath)
        for mode in ("open", "reveal"):
            try:
                cmd = self._open_file_command(filepath, mode=mode, is_dir=is_dir)
                if isinstance(cmd, str):
                    subprocess.Popen(cmd, shell=True)
                else:
                    subprocess.Popen(cmd, **downloader._no_window_kwargs())
                return
            except Exception:
                continue
        self._show_error_dialog(
            "Open Error",
            "Could not open that file — it may have been moved or deleted.",
            detail=filepath)

    def _clear_history(self):
        """Wipe the download history after confirmation."""
        if not self._queue_manager:
            self._init_queue_manager()
        if not self._queue_manager:
            self._show_error_dialog("Error", "Could not initialize download queue.")
            return
        if not messagebox.askyesno("Clear History", "Delete all download history?\n\nThis cannot be undone."):
            return
        try:
            self._queue_manager.clear_history()
            self._refresh_history_view()
            self.show_toast("History cleared", "success")
        except Exception as e:
            self._show_error_dialog("Clear History", "Could not clear the history.",
                                    detail=str(e))

    # -----------------
    # Themed dialogs (replacement for the gray OS message boxes)
    # -----------------
    def _show_dialog(self, title: str, message: str, kind: str = "info",
                     detail: Optional[str] = None,
                     action_label: Optional[str] = None,
                     action_cb=None):
        """Themed stand-in for ``messagebox.show{info,error,warning}``.

        The system boxes are gray OS chrome: they ignore the active palette,
        squeeze a multi-line yt-dlp failure into one clipped line, and give no
        way to copy the error text. This dialog uses the current theme, wraps
        the message, shows technical detail in a separate muted block with a
        Copy button, and can offer one secondary action (e.g. Try again).

        Same ``(title, message)`` shape as messagebox so call sites stay
        readable. Returns the Toplevel (mostly for tests); nothing blocks.
        """
        message, detail = _split_headline(message, detail)

        pal = getattr(self, "_palette", {}) or {}
        role = {"info": "accent", "success": "success",
                "warning": "warning", "error": "danger"}.get(kind, "accent")
        accent = pal.get(role, UITheme.COLOR_PRIMARY)
        accent_hover = pal.get(role + "_hover", accent)
        bg = pal.get("surface", UITheme.SURFACE_BG)
        field_bg = pal.get("bg", "#1e1e24")
        text = pal.get("text", "#ecf0f1")
        sub = pal.get("sub", "#9baaab")
        on_accent = _on_color(accent, "#ffffff", field_bg)
        glyph = {"info": "ℹ", "success": "✓", "warning": "⚠",
                 "error": "✗"}.get(kind, "ℹ")

        dlg = ctk.CTkToplevel(self, fg_color=bg)
        dlg.title(title)
        dlg.resizable(False, False)
        try:
            dlg.transient(self)
        except Exception:
            pass

        head = ctk.CTkFrame(dlg, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(18, 4))
        ctk.CTkLabel(head, text=glyph, font=UITheme.F(20, "bold"),
                     text_color=accent, width=30).pack(side="left")
        ctk.CTkLabel(head, text=title, font=UITheme.F(15, "bold"),
                     text_color=text).pack(side="left")

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=(2, 2))
        ctk.CTkLabel(body, text=str(message), font=UITheme.F(12),
                     text_color=text, wraplength=430,
                     justify="left").pack(anchor="w")

        detail = "" if detail is None else str(detail).strip()
        if detail:
            lines = detail.splitlines()
            # Cap what is displayed: a 40-line yt-dlp traceback would otherwise
            # push the buttons off the screen. "Copy details" always copies the
            # complete text, never the truncated preview.
            if len(lines) > 8:
                shown = "\n".join(lines[:8]) + \
                    f"\n… plus {len(lines) - 8} more lines — use Copy details"
            else:
                shown = detail
            box = ctk.CTkFrame(body, fg_color=field_bg,
                               corner_radius=UITheme.RADIUS_FIELD,
                               border_width=UITheme.BORDER_W,
                               border_color=pal.get("hover", sub))
            box.pack(fill="x", anchor="w", pady=(12, 0))
            ctk.CTkLabel(box, text=shown, font=UITheme.F(10), text_color=sub,
                         wraplength=400, justify="left", anchor="w",
                         takefocus=1).pack(side="left", fill="x", expand=True,
                                           padx=12, pady=9)

        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=(14, 18))

        def _close(_event=None):
            try:
                dlg.grab_release()
            except Exception:
                pass
            try:
                dlg.destroy()
            except Exception:
                pass

        ctk.CTkButton(row, text="OK", width=104, height=36,
                      corner_radius=UITheme.RADIUS_MD,
                      fg_color=accent,
                      hover_color=accent_hover, text_color=on_accent,
                      font=UITheme.F(12, "bold"),
                      command=_close).pack(side="right")

        if detail:
            def _copy():
                try:
                    dlg.clipboard_clear()
                    dlg.clipboard_append(detail)
                except Exception:
                    logger.exception("Could not copy dialog detail")

            ctk.CTkButton(row, text="Copy details", width=112, height=36,
                          corner_radius=UITheme.RADIUS_MD,
                          fg_color="transparent", border_width=UITheme.BORDER_W,
                          border_color=pal.get("hover", sub), text_color=sub,
                          hover_color=pal.get("hover", bg),
                          font=UITheme.F(11),
                          command=_copy).pack(side="right", padx=(0, 10))

        if action_label and action_cb:
            def _run_action():
                _close()
                try:
                    action_cb()
                except Exception:
                    logger.exception("Dialog action %r failed", action_label)

            ctk.CTkButton(row, text=action_label, width=112, height=36,
                          corner_radius=UITheme.RADIUS_MD,
                          fg_color="transparent", border_width=UITheme.BORDER_W,
                          border_color=accent, text_color=accent,
                          hover_color=pal.get("hover", bg),
                          font=UITheme.F(12, "bold"),
                          command=_run_action).pack(side="right", padx=(0, 10))

        def _on_key(event):
            if event.keysym in ("Return", "KP_Enter", "Escape", "space"):
                _close()
                return "break"

        for _key in ("<Return>", "<KP_Enter>", "<Escape>", "<space>"):
            dlg.bind(_key, _on_key)
        dlg.protocol("WM_DELETE_WINDOW", _close)

        def _center():
            # Center over the app window (not the screen) once the Toplevel has
            # computed its size, so a long error lands where the user is
            # already looking.
            try:
                dlg.update_idletasks()
                w = max(dlg.winfo_reqwidth(), 400)
                h = dlg.winfo_reqheight()
                pw = max(self.winfo_width(), 640)
                ph = max(self.winfo_height(), 420)
                x = self.winfo_rootx() + max(0, (pw - w) // 2)
                y = self.winfo_rooty() + max(0, (ph - h) // 3)
                dlg.geometry(f"{w}x{h}+{x}+{y}")
            except Exception:
                pass

        def _grab():
            # grab_set() only sticks once the window is viewable; doing it on a
            # short delay avoids the blocking wait_visibility() call.
            try:
                dlg.grab_set()
                dlg.focus_set()
            except Exception:
                pass

        dlg.after(0, _center)
        dlg.after(90, _grab)
        return dlg

    def _show_info_dialog(self, title: str, message: str,
                          detail: Optional[str] = None):
        return self._show_dialog(title, message, "info", detail=detail)

    def _show_success_dialog(self, title: str, message: str,
                             detail: Optional[str] = None):
        return self._show_dialog(title, message, "success", detail=detail)

    def _show_warning_dialog(self, title: str, message: str,
                             detail: Optional[str] = None):
        return self._show_dialog(title, message, "warning", detail=detail)

    def _show_error_dialog(self, title: str, message: str,
                           detail: Optional[str] = None,
                           action_label: Optional[str] = None, action_cb=None):
        return self._show_dialog(title, message, "error", detail=detail,
                                 action_label=action_label, action_cb=action_cb)

    # -----------------
    # Toast notifications (non-blocking)
    # -----------------
    def show_toast(self, message: str, toast_type: str = "info", duration: int = 3500):
        """Show a non-blocking toast notification at the bottom-right.

        Toasts stack upward instead of drawing on top of each other, can be
        dismissed early with a click, and are capped at four so a burst of
        messages can never cover the window.

        toast_type: 'info' (accent), 'success', 'warning', 'error' (danger)
        duration: milliseconds before auto-dismiss
        """
        # Toast colors come from the active palette (same semantic roles as
        # the buttons), not fixed hexes that ignore theme switches.
        pal = getattr(self, "_palette", {}) or {}
        role = {"info": "accent", "success": "success",
                "warning": "warning", "error": "danger"}.get(toast_type, "accent")
        fg_color = pal.get(role, "#2980b9")
        on_color = _on_color(fg_color, "#ffffff", pal.get("bg", "#1e1e24"))

        try:
            # Drop entries whose widget was destroyed, then cap the stack
            # (oldest first) so rapid successions can't flood the window.
            self._active_toasts = [t for t in self._active_toasts if t.winfo_exists()]
            while len(self._active_toasts) >= 4:
                try:
                    self._active_toasts.pop(0).destroy()
                except Exception:
                    pass

            toast = ctk.CTkFrame(self, fg_color=fg_color,
                                 corner_radius=UITheme.RADIUS_MD)
            setattr(toast, "_theme_roles", {"fg_color": role})

            icon = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✗"}.get(toast_type, "ℹ")
            lbl = ctk.CTkLabel(
                toast, text=f" {icon} {message}",
                font=UITheme.F(12), text_color=on_color,
                wraplength=340, justify="right",
            )
            lbl.pack(padx=16, pady=10)

            # Start off the right edge and let _reposition_toasts glide it in;
            # the rest of the stack slides up to make room in the same loop.
            # setattr, not plain assignment: a toast is a bare CTkFrame, so
            # these slots are per-instance extras rather than declared widget
            # attributes (the same shape as the _theme_roles tag above).
            setattr(toast, "_toast_relx",
                    1.12 if getattr(self, "nav_anim_enabled", True) else 0.98)
            setattr(toast, "_toast_rely", 0.955)
            setattr(toast, "_toast_leaving", False)

            self._active_toasts.append(toast)
            self._reposition_toasts()

            def _dismiss(*_args):
                if getattr(toast, "_toast_leaving", False):
                    return
                setattr(toast, "_toast_leaving", True)
                try:
                    self._active_toasts.remove(toast)
                except ValueError:
                    pass
                # Recount the slots first so the stack starts closing the gap
                # while this toast is still flying off.
                self._reposition_toasts()
                if not getattr(self, "nav_anim_enabled", True):
                    try:
                        toast.destroy()
                    except Exception:
                        pass
                    return
                self._fly_off_toast(toast)

            # Click anywhere on the toast to dismiss it immediately.
            toast.bind("<Button-1>", _dismiss)
            lbl.bind("<Button-1>", _dismiss)

            # Auto-dismiss after duration
            self.after(duration, _dismiss)
        except Exception:
            pass

    def _fly_off_toast(self, toast, steps: int = 7):
        """Glide a dismissed toast off the right edge, then destroy it."""
        def step(i: int):
            try:
                if not toast.winfo_exists():
                    return
                t = min(1.0, i / steps)
                eased = 1 - (1 - t) * (1 - t)   # ease-out, like the nav indicator
                toast.place(relx=0.98 + 0.16 * eased,
                            rely=getattr(toast, "_toast_rely", 0.955),
                            anchor="se")
                if i < steps:
                    self.after(16, lambda: step(i + 1))
                else:
                    toast.destroy()
            except Exception:
                try:
                    toast.destroy()
                except Exception:
                    pass

        step(0)

    def _reposition_toasts(self, _event=None):
        """Glide every toast toward its slot in the bottom-right stack.

        Positions are eased instead of snapped: a new toast arrives from the
        right edge and the stack below slides up to close the gap, so the pile
        never blinks into place. One loop drives every toast; its trailing
        after() is cancelled and restarted whenever the stack changes.
        """
        self._active_toasts = [t for t in self._active_toasts if t.winfo_exists()]
        if not self._active_toasts:
            return
        animate = bool(getattr(self, "nav_anim_enabled", True))
        moving = False
        for i, t in enumerate(self._active_toasts):
            target_x, target_y = 0.98, 0.955 - i * 0.065
            cur_x = getattr(t, "_toast_relx", target_x)
            cur_y = getattr(t, "_toast_rely", target_y)
            if animate:
                new_x = cur_x + (target_x - cur_x) * 0.35
                new_y = cur_y + (target_y - cur_y) * 0.35
                if abs(target_x - new_x) > 0.001 or abs(target_y - new_y) > 0.001:
                    moving = True
                else:
                    new_x, new_y = target_x, target_y
            else:
                new_x, new_y = target_x, target_y
            t._toast_relx, t._toast_rely = new_x, new_y
            try:
                t.place(relx=new_x, rely=new_y, anchor="se")
            except Exception:
                pass
        if moving:
            pending = getattr(self, "_toast_anim_id", None)
            if pending:
                try:
                    self.after_cancel(pending)
                except Exception:
                    pass
            self._toast_anim_id = self.after(28, self._reposition_toasts)

    def update_speed_label(self, speed_text: str):
        """Update the speed/ETA label below the progress bar."""
        self.after(0, lambda: self.dl_speed_lbl.configure(text=speed_text))

    def _open_tag_editor(self):
        filepath = self._last_downloaded_file
        if not filepath or not os.path.exists(filepath):
            self._show_warning_dialog("No File", "No downloaded file available to edit.")
            return
        try:
            from tag_editor import open_tag_editor
            open_tag_editor(self, filepath, on_save=self._on_tags_saved)
        except Exception as e:
            self._show_error_dialog("Tag Editor", f"Could not open tag editor: {e}")

    def _on_tags_saved(self):
        self.show_toast("Tags updated successfully.", "success")


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
                # The detail label only lives on the Downloader tab; a toast
                # carries the same notice to whatever tab is on screen.
                self.show_toast(
                    f"yt-dlp update available ({local} -> {latest}) — see the Performance tab.",
                    "warning",
                    duration=6000,
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
                # The pip output is long and technical: keep a short headline in
                # the body and push the raw log into the copyable detail box.
                if "already up to date" in msg.lower():
                    self.after(0, lambda: self._show_info_dialog(
                        "yt-dlp Updater", "yt-dlp is already up to date.",
                        detail=msg))
                elif "updated" in msg.lower() and "fail" not in msg.lower():
                    self.after(0, lambda: self._show_success_dialog(
                        "yt-dlp Updater",
                        "Update successful. Restart the app to use the new version.",
                        detail=msg))
                else:
                    self.after(0, lambda: self._show_info_dialog(
                        "yt-dlp Updater", "The updater finished — check the details.",
                        detail=msg))
                self.after(0, lambda: self._ytdlp_update_check_worker())
            except Exception as e:
                logger.exception("yt-dlp update failed")
                self.after(0, lambda: self._show_error_dialog(
                    "Update Error", "Failed to update yt-dlp.", detail=str(e)))
            finally:
                self.after(0, lambda: self.ytdlp_update_btn.configure(state="normal", text="⮔ Update yt-dlp"))
        self._start_worker(worker)

    def check_for_app_updates(self):
        """Check the remote manifest and trigger an in-place self-update."""
        import updater  # local module
        from version import __version__ as local_ver

        if not updater.is_frozen():
            self._show_info_dialog(
                "Update Unavailable",
                "Self-update only works in the packaged app.\n\n"
                "You are running from source (python ui.py), so there is no\n"
                "EXE to update. Build and run the .exe to use app updates.",
            )
            return

        if not updater.self_update_supported():
            # macOS: the app cannot replace itself here, but it can still report
            # that a newer version exists and take the user to the download.
            self._check_macos_update()
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
                self.after(0, lambda: self.show_toast("No update information available right now.", "warning"))
                return

            remote_ver = info["version"]
            dl_url = info["download_url"]

            if not updater.is_newer_version(remote_ver):
                self.after(0, lambda: self.show_toast(f"You already have the latest version ({local_ver}).", "success"))
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
                self.after(0, lambda: self._show_error_dialog("Update Error", "Failed to download the update."))
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
                self.after(0, lambda: self._show_error_dialog(
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


    def _check_macos_update(self):
        """macOS update check: report a new version and offer the download.

        A Mac cannot replace its own .app while it is running, so there is no
        install step here - the user is taken to the disk image instead. Without
        this the button did nothing but explain that self-update is
        Windows-only, so Mac users were never told an update existed.
        """
        import updater  # local module
        from version import __version__ as local_ver

        self.app_update_btn.configure(state="disabled", text="Checking...")
        self.update_dl_status("Checking for app updates...", "#3498db")

        def worker():
            try:
                release = updater.get_remote_release()
            except Exception:
                release = None
            self.after(0, lambda: self._after_macos_update_check(release, local_ver))

        self._start_worker(worker)

    def _after_macos_update_check(self, release, local_ver):
        """Show the result of the macOS update check."""
        import updater  # local module

        self.app_update_btn.configure(state="normal", text="↻ Check for App Updates")
        remote_ver = (release or {}).get("version")
        if not remote_ver:
            self.update_dl_status("Could not reach update server.", "#e74c3c")
            self.show_toast("No update information available right now.", "warning")
            return
        if not updater.is_newer_version(remote_ver):
            self.update_dl_status(f"Up to date (v{local_ver}).", "#27ae60")
            self.show_toast(f"You already have the latest version ({local_ver}).", "success")
            return
        if not updater.macos_download_url(release):
            # Newer, but it ships no Mac build - a Windows-only release. Say so
            # plainly instead of offering a download that does not exist, and
            # never let it turn into a recurring prompt.
            self.update_dl_status(f"Up to date (v{local_ver}).", "#27ae60")
            self._show_info_dialog(
                "No Mac Update",
                f"Version {remote_ver} has been released, but it contains no "
                f"macOS build.\n\nYou already have the newest Mac version "
                f"({local_ver}).")
            return
        self.update_dl_status(f"Update available (v{remote_ver}).", "#f39c12")
        self._offer_macos_download(local_ver, release)

    def _offer_macos_download(self, local_ver, release):
        """Offer to open the new .dmg in the browser (macOS)."""
        import updater  # local module
        import webbrowser

        remote_ver = (release or {}).get("version", "?")
        # The direct disk image when the Release has one for this Mac, otherwise
        # the Release page, so the user is never left without a route.
        target = updater.macos_download_url(release) or updater.RELEASES_PAGE

        if messagebox.askyesno(
            "Update Available",
            f"A new version is available:\n\n"
            f"  Current: {local_ver}\n"
            f"  Latest:  {remote_ver}\n\n"
            f"A Mac cannot replace the app while it is running, so this opens "
            f"the download in your browser.\n"
            f"Then drag the new app over the old one in Applications.\n\n"
            f"Open the download now?",
        ):
            try:
                webbrowser.open(target)
                self.update_dl_status(f"Opened the download for v{remote_ver}.", "#27ae60")
            except Exception as e:
                logger.warning("Could not open a browser: %s", e)
                self._show_info_dialog("Update Available", f"Download it here:\n\n{target}")

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

                release = updater.get_remote_release()
                if not release or not release.get("version"):
                    return

                if not updater.is_newer_version(release["version"]):
                    return
                # Only prompt when this platform can do something with it:
                # Windows needs the packaged ZIP, a Mac needs a .dmg for its own
                # architecture. A Windows-only release must not nag Macs.
                if not updater.available_update(release):
                    return
                download_queue._mark_update_check_done('app')

                self.after(0, lambda: self._offer_startup_update(local_ver, release))
            except Exception:
                # Never let a background update check crash the app on startup.
                pass

        threading.Thread(target=_runner, daemon=True).start()


    def _offer_startup_update(self, local_ver, release):
        """Show the 'update available' prompt found by the startup check."""
        import updater  # local module

        remote_ver = (release or {}).get("version", "?")
        if not updater.self_update_supported():
            if not updater.macos_download_url(release):
                # A Windows-only release: nothing this Mac can use, so stay
                # silent rather than prompting about it.
                return
            # macOS: the dialog below promises a Windows UAC prompt and an
            # automatic relaunch, neither of which exists here, so offer the
            # download instead.
            self.update_dl_status(f"Update available (v{remote_ver}).", "#f39c12")
            self._offer_macos_download(local_ver, release)
            return

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
        self._last_dl_url = url
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
        self._last_dl_url = url
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

    def _bump_completion_badge(self):
        """Count downloads that finished while the window was unfocused.

        The taskbar title is the only signal that survives the user being
        on another tab or another app; the badge clears on focus-in.
        """
        try:
            if self.focus_displayof() is not None:
                return  # the user is already looking at the app
        except Exception:
            return
        self._unfocused_done += 1
        try:
            self.title(f"TuneLab ({self._unfocused_done} new)")
        except Exception:
            pass

    def _clear_completion_badge(self, _event=None):
        self._unfocused_done = 0
        try:
            self.title("TuneLab")
        except Exception:
            pass

    def _downloads_in_flight(self) -> bool:
        """True while a direct download or the queue worker is busy."""
        try:
            if self._queue_manager is not None and self._queue_manager.is_running:
                return True
        except Exception:
            pass
        try:
            # set_download_buttons_state(False) disables these for the whole
            # duration of a direct download, so their state is the flag.
            return str(self.btn_download_mp3.cget("state")) == "disabled"
        except Exception:
            return False

    def _on_closing(self):
        """Handle window close event, asking first when downloads run."""
        if self._downloads_in_flight():
            if not messagebox.askyesno(
                "Downloads in Progress",
                "Downloads are still running.\n\nQuit anyway?",
            ):
                return
        self._remember_window_geometry()
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
            self.show_toast("Please enter a URL to preview.", "warning")
            return
        if not downloader.can_resolve_preview():
            self._show_error_dialog(
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
                # ffmpeg's stdout is bytes at runtime; tolerate the str that
                # some Popen overload resolutions may infer here.
                pcm = raw if isinstance(raw, bytes) else raw.encode()
                data = _np.frombuffer(pcm, dtype=_np.float32).reshape(-1, 2)
                _sd.play(data, 44100)
                _sd.wait()
            except Exception as e:
                logger.exception("Audio preview failed")
                self.after(0, lambda err=e: self._show_error_dialog('Preview Error', f'Audio preview failed:\n{err}'))
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
            self.show_toast("Please enter a URL to preview.", "warning")
            return
        if not downloader.can_resolve_preview():
            self._show_error_dialog(
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
                            # start_vlc itself was dispatched with after(0, ...),
                            # so this is on the main thread and can use the themed
                            # dialog like every other message.
                            self._show_error_dialog(
                                'Preview Error',
                                f'Embedded video preview failed:\n{e}')

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
                self.after(0, lambda err=err: self._show_error_dialog('Preview Error', f'Video preview failed:\n{err}'))
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
        # Remember the palette role so a theme switch while a status is
        # showing repaints the pill instead of freezing the old hue.
        role = LEGACY_HEX_ROLES.get(str(color).lower())

        def _apply():
            # The status pill on the Downloader tab is the single home of
            # download status — there is deliberately no second copy in the
            # header (a duplicate "Finished!" up there was just noise).
            try:
                self.dl_status.configure(text=text, text_color=themed)
                if role:
                    setattr(self.dl_status, "_theme_roles", {"text_color": role})
            except Exception:
                pass
            # The pill's dot echoes the status color.
            try:
                self._status_dot.configure(text_color=themed)
                if role:
                    setattr(self._status_dot, "_theme_roles",
                            {"text_color": role})
            except Exception:
                pass

        self.after(0, _apply)

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
        value = min(max(value, 0.0), 1.0)

        def _apply():
            self.progress_bar.set(value)
            # Mirror progress into the title so the taskbar/hover shows it
            # without focusing the window; skip no-change ticks.
            pct = int(value * 100)
            if pct != getattr(self, "_last_title_pct", None):
                self._last_title_pct = pct
                self.title(f"TuneLab — {pct}%")

        self.after(0, _apply)

    def on_dl_success(self, folder):
        self.after(0, lambda: self._finalize_download(folder, None))

    def on_dl_error(self, err):
        self.after(0, lambda: self._finalize_download(None, err))

    def _finalize_download(self, folder, err):
        if err == "__cancelled__":
            self.update_dl_status("Cancelled", "#f39c12")
            self.progress_bar.set(0)
            self.update_dl_detail("", "#95a5a6")
            self.show_toast("Download cancelled", "warning")
        elif err:
            cleaned_err = downloader._strip_ansi(err) if hasattr(downloader, '_strip_ansi') else str(err)
            self.update_dl_status("Failed", "#e74c3c")
            self.progress_bar.set(0)
            # yt-dlp fails with a wall of text. Lead with the first line so the
            # dialog stays readable, keep the full log copyable, and offer the
            # retry instead of making the user re-paste the link.
            headline = next((ln.strip() for ln in cleaned_err.splitlines()
                             if ln.strip()), "The download failed.")
            self._show_error_dialog(
                "Download Failed", headline, detail=cleaned_err,
                action_label="Try again", action_cb=self._retry_last_download)
        else:
            self.update_dl_status("Finished!", "#2ecc71")
            self.progress_bar.set(1.0)
            self.update_dl_detail(str(folder), "#95a5a6")
            self._bump_completion_badge()
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
            # Non-blocking cue: the file manager just took focus, so a modal
            # "Success!" box would be noise — the toast slides in instead.
            name = os.path.basename(self._last_downloaded_file or "") or "Download complete"
            self.show_toast(f"Saved  {name}", "success")
        self.title("TuneLab")
        self._last_title_pct = None
        self.set_download_buttons_state(True)

    def _retry_last_download(self):
        """Re-run the last download with the same URL and audio/video mode.

        Wired to the "Try again" button of the failure dialog. Falls back to the
        remembered URL when the entry has been edited or cleared since.
        """
        try:
            url = (self.url_entry.get() or "").strip() or self._last_dl_url
        except Exception:
            url = self._last_dl_url
        if not url:
            self.show_toast("Nothing to retry — paste a link first", "warning")
            return
        self.show_frame("downloader")
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, url)
            self._sync_clear_btn()
        except Exception:
            pass
        if getattr(self, "_last_dl_was_video", False):
            self._start_worker(self.download_mp4)
        else:
            self._start_worker(self.download_mp3)

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
        self.file_lbl = ctk.CTkLabel(self.file_frame, text="Load a music file...", font=UITheme.F(12, "italic"), text_color="#95a5a6")
        self.file_lbl.pack(side="left", padx=10, pady=10)
        self.btn_browse_studio = ctk.CTkButton(
            self.file_frame, text="Browse Audio", width=100,
            command=self.load_studio_file)
        self.btn_browse_studio.pack(side="right", padx=10, pady=10)
        self._style_button(self.btn_browse_studio, "secondary")

        self.sp_lbl = ctk.CTkLabel(self.tab_studio, text="Speed: 0.85x", font=UITheme.F(14, "bold"))
        self.sp_lbl.pack(anchor="w", padx=20, pady=(6,0))
        self.sp_sld = ctk.CTkSlider(self.tab_studio, from_=0.5, to=1.5, command=lambda v: self.sp_lbl.configure(text=f"Speed: {float(v):.2f}x"))  # type: ignore[arg-type]


        self.sp_sld.set(0.85)
        self.sp_sld.pack(fill="x", padx=20, pady=(4,10))

        self.rv_lbl = ctk.CTkLabel(self.tab_studio, text="Reverb Depth: 40%", font=UITheme.F(14, "bold"))
        self.rv_lbl.pack(anchor="w", padx=20, pady=(4,0))
        self.rv_sld = ctk.CTkSlider(self.tab_studio, from_=0.0, to=1.0, command=lambda v: self.rv_lbl.configure(text=f"Reverb Depth: {int(float(v)*100)}%"))  # type: ignore[arg-type]


        self.rv_sld.set(0.40)
        self.rv_sld.pack(fill="x", padx=20, pady=(4,10))

        self.ctrl_frame = ctk.CTkFrame(self.tab_studio, fg_color="transparent")
        self.ctrl_frame.pack(pady=(6,10))
        self.btn_play = ctk.CTkButton(self.ctrl_frame, text="▶ Play", width=115, command=self.play_preview)
        self.btn_play.grid(row=0, column=0, padx=6)
        self._style_button(self.btn_play, "primary")
        self.btn_stop = ctk.CTkButton(self.ctrl_frame, text="⏹ Stop", width=115, command=self.stop_preview, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=6)
        self._style_button(self.btn_stop, "danger")

        self.btn_export = ctk.CTkButton(self.tab_studio, text="💾 Export Remix", font=UITheme.F(12, "bold"), width=280, height=40, command=self.export_studio_track)
        self.btn_export.pack(pady=(4,10))
        self._style_button(self.btn_export, "primary")

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
        # Page title lives in the header only — no in-page H1.
        # One control owns theme mode now: every palette carries its own
        # Light/Dark 'mode' and apply_color_theme syncs CTk from it, so the
        # separate "Theme Mode" dropdown was redundant and is gone.

        theme_card = self._settings_card(self.tab_customization, "Theme")
        theme_card.pack(fill="x", padx=UITheme.PAD_X, pady=(14, 8))
        ctk.CTkLabel(
            theme_card, text="Color theme (Monkeytype-style):",
            font=UITheme.F(12), anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 4))
        self.theme_menu = ctk.CTkOptionMenu(
            theme_card,
            values=list(COLOR_THEMES.keys()),
            command=self.apply_color_theme,
            width=220
        )
        self.theme_menu.set(self._color_theme_name)
        self.theme_menu.pack(anchor="w", padx=16, pady=(0, 6))
        self._style_option_menu(self.theme_menu)
        ctk.CTkLabel(
            theme_card,
            text="Light/Dark follows the palette you pick here.",
            font=UITheme.F(10), anchor="w", text_color="#95a5a6",
        ).pack(fill="x", padx=16, pady=(0, 12))

        # Theme selection lives in the dropdown above only (the duplicate row
        # of clickable swatch dots was removed as redundant UI).

        bg_card = self._settings_card(self.tab_customization, "Background")
        bg_card.pack(fill="x", padx=UITheme.PAD_X, pady=(0, 8))

        self.gif_button = ctk.CTkButton(
            bg_card,
            text="Load Animated GIF Background",
            width=260,
            command=self.load_background_gif
        )
        self.gif_button.pack(anchor="w", padx=16, pady=(4, 8))
        self._style_button(self.gif_button, "secondary")

        self.clear_bg_button = ctk.CTkButton(
            bg_card,
            text="Clear Background",
            width=260,
            command=self.clear_background
        )
        self.clear_bg_button.pack(anchor="w", padx=16, pady=(0, 6))
        self._style_button(self.clear_bg_button, "danger")

        # Cache-clearing moved to the Performance tab (it is maintenance,
        # not appearance).

        self.bg_status = ctk.CTkLabel(
            bg_card, text="No animated background loaded.",
            font=UITheme.F(12), anchor="w", text_color="#95a5a6")
        self.bg_status.pack(fill="x", padx=16, pady=(0, 12))
        # Rail behaviour. The collapse state, hover-peek and motion lengths
        # are appearance choices, so they sit here rather than in Performance.
        rail_card = self._settings_card(self.tab_customization,
                                        "Sidebar & motion")
        rail_card.pack(fill="x", padx=UITheme.PAD_X, pady=(0, 8))

        self.sb_collapsed_var = tk.BooleanVar(value=not self._sidebar_expanded)
        self.sb_collapsed_chk = ctk.CTkCheckBox(
            rail_card, text="Start with the rail collapsed to icons",
            variable=self.sb_collapsed_var, command=self._on_sb_collapsed_pref)
        self.sb_collapsed_chk.pack(anchor="w", padx=16, pady=(4, 0))
        ctk.CTkLabel(
            rail_card,
            text="Ctrl+B, or the row at the bottom of the rail, collapses it any time.",
            font=UITheme.F(11), text_color="#95a5a6", anchor="w",
            wraplength=560, justify="left").pack(anchor="w", padx=16, pady=(0, 4))

        self.sb_peek_var = tk.BooleanVar(value=bool(self._sb_peek_pref))
        self.sb_peek_chk = ctk.CTkCheckBox(
            rail_card, text="Glide the rail open while the pointer rests on an icon",
            variable=self.sb_peek_var, command=self._on_sb_peek_pref)
        self.sb_peek_chk.pack(anchor="w", padx=16, pady=(0, 0))
        ctk.CTkLabel(
            rail_card,
            text="Off by default — a rail that opens itself every time the pointer "
                 "crosses it gets in the way. When on, only the icon column arms "
                 "it, nothing moves but the width, and it closes as you leave.",
            font=UITheme.F(11), text_color="#95a5a6", anchor="w",
            wraplength=560, justify="left").pack(anchor="w", padx=16, pady=(0, 4))

        self.nav_anim_var = tk.BooleanVar(value=bool(self.nav_anim_enabled))
        self.nav_anim_chk = ctk.CTkCheckBox(
            rail_card, text="Animate the accent pill and the rail's slide",
            variable=self.nav_anim_var, command=self._on_nav_anim_pref)
        self.nav_anim_chk.pack(anchor="w", padx=16, pady=(0, 2))

        ctk.CTkLabel(rail_card, text="Motion length (lower is snappier):",
                     font=UITheme.F(12), anchor="w").pack(anchor="w", padx=16,
                                                          pady=(2, 0))
        self.nav_anim_speed_slider = ctk.CTkSlider(
            rail_card, from_=4, to=30, number_of_steps=26,
            command=self._on_nav_speed)
        self.nav_anim_speed_slider.set(self.nav_anim_speed)
        self.nav_anim_speed_slider.pack(padx=16, pady=(0, 2), fill="x")
        self.nav_anim_speed_lbl = ctk.CTkLabel(
            rail_card, text=f"{int(self.nav_anim_speed)} frames",
            font=UITheme.F(10), text_color="#95a5a6", anchor="w")
        self.nav_anim_speed_lbl.pack(anchor="w", padx=16, pady=(0, 12))

        self.tab_customization.place(relx=0, rely=0, relwidth=1, relheight=1)

    # --- Sidebar & motion preferences ------------------------------------
    # Each one applies the moment it is changed: the rail is cheap to
    # reconfigure, and making the user restart to see the effect of a
    # checkbox is how you end up with settings nobody tunes.

    def _on_sb_collapsed_pref(self):
        want_collapsed = bool(self.sb_collapsed_var.get())
        self._set_pref('sidebar_collapsed', want_collapsed)
        if want_collapsed == self._sidebar_expanded:
            self.toggle_sidebar()   # preview the choice immediately

    def _on_sb_peek_pref(self):
        self._sb_peek_pref = bool(self.sb_peek_var.get())
        self._set_pref('sidebar_peek_on_hover', self._sb_peek_pref)
        if not self._sb_peek_pref:
            # Turning it off means off now, not whenever the pointer moves.
            self._sb_cancel_peek()
            if self._sb_peeking:
                self._sb_peeking = False
                self._animate_sidebar(False)

    def _on_nav_anim_pref(self):
        self.nav_anim_enabled = bool(self.nav_anim_var.get())
        self._set_pref('nav_animation_enabled', self.nav_anim_enabled)

    def _on_nav_speed(self, value):
        try:
            self.nav_anim_speed = int(float(value))
        except Exception:
            return
        self._set_pref('nav_animation_speed', self.nav_anim_speed)
        try:
            self.nav_anim_speed_lbl.configure(
                text=f"{self.nav_anim_speed} frames")
        except Exception:
            pass

    def build_performance_view(self):
        # Use a scrollable frame so all settings are reachable even on small screens
        saved = downloader.perf_cfg_from_prefs(self._prefs)
        _saved_aria = saved["use_aria2"]
        _saved_connections = saved["aria2_connections"]
        _saved_frags = saved["concurrent_fragment_downloads"]

        # No label_text: the header title already says "Performance Settings".
        scroll = ctk.CTkScrollableFrame(self.tab_performance, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)

        # Card 1: download tuning (+ Apply, which commits these controls).
        tuning = self._settings_card(scroll, "Downloads")
        tuning.pack(fill="x", padx=UITheme.PAD_X, pady=(14, 8))

        self.aria2_var = tk.BooleanVar(value=_saved_aria)
        self.aria2_chk = ctk.CTkCheckBox(tuning, text="Enable aria2 external downloader", variable=self.aria2_var)
        self.aria2_chk.pack(anchor="w", padx=16, pady=(4, 0))
        ctk.CTkLabel(tuning, text="Uses aria2 for parallel connections (faster downloads).", font=UITheme.F(11), text_color="#95a5a6", wraplength=600, justify="left").pack(anchor="w", padx=16, pady=(0, 2))

        ctk.CTkLabel(tuning, text="aria2 connections:  (max 16 — aria2 hard limit)", font=UITheme.F(12)).pack(anchor="w", padx=16, pady=(2, 0))
        self.aria2_conn_slider = ctk.CTkSlider(tuning, from_=1, to=16, number_of_steps=15)
        self.aria2_conn_slider.set(_saved_connections)
        self.aria2_conn_slider.pack(padx=16, pady=(0, 2), fill="x")

        ctk.CTkLabel(tuning, text="Concurrent fragment downloads:", font=UITheme.F(12)).pack(anchor="w", padx=16, pady=(2, 0))
        self.concurrent_frag_slider = ctk.CTkSlider(tuning, from_=1, to=32, number_of_steps=31)
        self.concurrent_frag_slider.set(_saved_frags)
        self.concurrent_frag_slider.pack(padx=16, pady=(0, 2), fill="x")

        ctk.CTkLabel(tuning, text="SoundCloud downloads:", font=UITheme.F(12)).pack(anchor="w", padx=16, pady=(2, 0))
        self.soundcloud_var = tk.BooleanVar(value=bool(self._prefs.get("soundcloud_direct_first", True)))
        self.soundcloud_chk = ctk.CTkCheckBox(tuning, text="Try downloading from SoundCloud first, fall back to YouTube", variable=self.soundcloud_var, command=self.apply_soundcloud_pref)
        self.soundcloud_chk.pack(anchor="w", padx=16, pady=(0, 4))

        # Video download quality cap (saves time/disk when 4K isn't needed;
        # the AE re-encode keeps the source resolution — it does not downscale).
        ctk.CTkLabel(tuning, text="Max video resolution:", font=UITheme.F(12)).pack(anchor="w", padx=16, pady=(2, 0))
        self.video_res_var = tk.StringVar(value=str(self._prefs.get("max_video_resolution", "Best (up to 4K)")))
        self.video_res_menu = ctk.CTkOptionMenu(
            tuning, values=downloader.VIDEO_RESOLUTION_OPTIONS, variable=self.video_res_var,
            command=self._on_video_res_change, width=200)
        self.video_res_menu.pack(anchor="w", padx=16, pady=(0, 4))
        self._style_option_menu(self.video_res_menu)

        # Filename template (yt-dlp output template). Empty = default %(title)s.%(ext)s.
        ctk.CTkLabel(tuning, text="Filename template (optional):", font=UITheme.F(12)).pack(anchor="w", padx=16, pady=(2, 0))
        self.filename_template_entry = ctk.CTkEntry(tuning, width=380, height=28,
            placeholder_text="e.g. %(artist)s - %(title)s.%(ext)s  (leave blank for default)")
        self.filename_template_entry.insert(0, str(self._prefs.get("filename_template", "") or ""))
        self.filename_template_entry.pack(anchor="w", padx=16, pady=(0, 4))

        self.apply_perf_btn = ctk.CTkButton(tuning, text="Apply Performance Settings", command=self.apply_performance_settings, width=260)
        self.apply_perf_btn.pack(anchor="w", padx=16, pady=(6, 14))
        self._style_button(self.apply_perf_btn, "primary")

        # Card 2: maintenance actions.
        maint = self._settings_card(scroll, "Maintenance")
        maint.pack(fill="x", padx=UITheme.PAD_X, pady=(0, 14))

        self.ytdlp_update_btn = ctk.CTkButton(
            maint, text="⫤ Update yt-dlp", width=260,
            cursor="hand2", command=self.update_ytdlp_clicked,
        )
        self.ytdlp_update_btn.pack(anchor="w", padx=16, pady=(4, 1))
        self._style_button(self.ytdlp_update_btn, "secondary")
        self.ytdlp_ver_lbl = ctk.CTkLabel(
            maint, text=f"yt-dlp version: {downloader.get_ytdlp_version() or 'not detected'}",
            font=UITheme.F(10), text_color="#95a5a6", anchor="w",
        )
        self.ytdlp_ver_lbl.pack(anchor="w", padx=16, pady=(0, 2))

        self.app_update_btn = ctk.CTkButton(
            maint, text="🔄 Check for App Updates", width=260,
            cursor="hand2", command=self.check_for_app_updates,
        )
        self.app_update_btn.pack(anchor="w", padx=16, pady=(0, 2))
        self._style_button(self.app_update_btn, "secondary")

        self.clear_cache_btn = ctk.CTkButton(
            maint, text="🧹 Clear Download Cache", width=260,
            command=self.clear_download_cache,
        )
        self.clear_cache_btn.pack(anchor="w", padx=16, pady=(0, 2))
        self._style_button(self.clear_cache_btn, "danger")

        self.perf_status = ctk.CTkLabel(maint, text="Current: default", font=UITheme.F(11), text_color="#95a5a6", anchor="w")
        self.perf_status.pack(anchor="w", padx=16, pady=(2, 14))

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
                    self.show_toast(
                        f"Settings applied — aria2 found at {_aria2_path}",
                        "success", duration=5000)
                else:
                    # The install instructions are too long for a toast;
                    # this one stays a real dialog.
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
                    self._show_info_dialog("Performance", _msg)
            else:
                self.show_toast("Performance settings applied (aria2 disabled).", "success")
        except Exception as e:
            self._show_error_dialog("Performance Error", f"Failed to apply settings:\n{e}")

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
        # Unmap the rest BEFORE any forced repaint. Forcing update_idletasks
        # here (the old code did) painted one frame with every transparent
        # page still stacked, so the outgoing page bled through the incoming
        # one — the "widgets clipping through each other" seen on click.
        # With no forced pass in between, Tk coalesces map + unmap and paints
        # only the final state; the single idletasks after the unmap keeps
        # layout resolved for the nav animation without exposing the stack.
        for f in others:
            try:
                f.place_forget()
            except Exception:
                pass
        try:
            self.update_idletasks()
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
        # Skip entirely when nothing changed: a repaint redraws every row
        # canvas, and page switches fire often (Ctrl+1..6, peek, clicks).
        if active_name == getattr(self, '_active_nav_name', None):
            return
        self._active_nav_name = active_name
        try:
            self._repaint_nav_rows()
        except Exception:
            pass

    # -----------------
    # Navigation indicator helpers
    # -----------------
    def _indicator_slot(self, btn):
        """Where the accent pill sits for a given nav row.

        The pill lives in the gutter between the rail edge and the chips, so
        it never covers a row's icon, and its x does not depend on the rail
        width - which is why collapsing leaves it exactly where it was.
        """
        return (3, int(btn.winfo_y()) + 6, UITheme.SB_ROW_H - 12)

    def _place_indicator(self, x, y, h):
        """Move/size the accent pill.

        CustomTkinter refuses width/height in ``place()`` (it raises), so the
        size goes through ``configure`` - which is also why the pill used to
        sit still: every placement was silently dying inside a try/except.
        """
        self.nav_indicator.configure(width=3, height=max(1, int(h)))
        self.nav_indicator.place(x=int(x), y=int(y))

    def _position_nav_indicator_initial(self, *_):
        try:
            btn = self.nav_buttons.get(getattr(self, '_active_nav_name', None)
                                       or 'downloader')
            if btn is None or not getattr(self, 'nav_indicator', None):
                return
            x, y, h = self._indicator_slot(btn)
            self._place_indicator(x, y, h)
        except Exception:
            logger.debug("nav indicator placement failed", exc_info=True)

    def _animate_nav_to(self, target_btn: ctk.CTkButton):
        if not getattr(self, 'nav_anim_enabled', True) or not getattr(self, 'nav_indicator', None):
            try:
                x, y, h = self._indicator_slot(target_btn)
                self._place_indicator(x, y, h)
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
            tx, target_y, th = self._indicator_slot(target_btn)
 
            steps = max(1, int(self.nav_anim_speed))
            def ease(t: float) -> float:
                return 1 - (1 - t) * (1 - t)
 
            def step(i: int):
                try:
                    t = min(1.0, i / steps)
                    progress = ease(t)
                    ny = start_y + (target_y - start_y) * progress
                    nh = start_h + (th - start_h) * progress
                    self._place_indicator(tx, ny, nh)
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
    # The pages you work in, then the pages you tune. The flat order is
    # unchanged - Ctrl+1..6 still map to downloader, queue, history, studio,
    # settings, performance - grouping only adds a header above each set.
    _SB_GROUPS = (("MENU", ("downloader", "queue", "history", "studio")),
                  ("SETUP", ("settings", "performance")))
    # Shown in each row's right slot, mirroring the <Control-N> bindings.
    _SB_KEYS = {"downloader": "1", "queue": "2", "history": "3",
                "studio": "4", "settings": "5", "performance": "6"}

    def _build_sidebar(self):
        """Brand, grouped nav rows, badge slots, footer toggle, accent pill.

        Everything in the rail is placed once at a fixed pixel position and
        the collapse never moves or rewrites any of it: ``_sb_set_width``
        changes widths only, and Tk clips what ends up outside the rail.
        The old version swapped each row's text, anchor, font size and width
        halfway through the slide, which is exactly what read as items
        popping in and out.
        """
        pal = self._palette
        # name -> (drawn icon, label). The icon names index _icon_shapes, so a
        # row's mark is geometry the palette can ink, not an emoji the system
        # font gets to decide the size, weight and colour of.
        self._sb_items = {
            'downloader': ('download', 'Downloader'),
            'queue': ('queue', 'Queue'),
            'history': ('history', 'History'),
            'studio': ('studio', 'Studio'),
            'settings': ('settings', 'Theme'),
            'performance': ('performance', 'Speed'),
        }
        # Hover tips: the row's own name plus its shortcut. A sentence here sits
        # in a 300px box over the page, which is a lot of furniture to drop next
        # to an icon that already says what it is.
        self._sb_tips = {name: '%s  (Ctrl+%s)' % (label, self._SB_KEYS.get(name, '?'))
                         for name, (_icon, label) in self._sb_items.items()}
        self.nav_buttons = {}
        self._sb_icons = {}
        self._sb_texts = {}
        self._sb_hints = {}
        self._sb_headers = []

        # Brand: an accent-tinted logo tile plus the wordmark. The tile sits
        # in the same 30px icon column as the rows, so it neither moves nor
        # drifts off-centre when the rail collapses to icon width. The wordmark
        # starts where a row's label starts - past the icon column - so the
        # collapsed rail (which is only pad + icon + pad wide) clips it whole
        # instead of leaving the first letter poking out, cut in half.
        self.sb_logo = ctk.CTkFrame(
            self.sidebar, width=UITheme.SB_ICON_W, height=UITheme.SB_ICON_W,
            corner_radius=UITheme.RADIUS_SM, fg_color="transparent",
        )
        self.sb_logo.place(x=UITheme.SB_PAD_X, y=14)
        # The mark is drawn, not typed: a canvas holds the note so the tile's
        # ink is the palette's accent at any size (see _icon_shapes). The box
        # is mark-sized for the same reason as a row's: opaque square corners
        # would square off the tile's RADIUS_SM corners.
        self.sb_logo_glyph = tk.Canvas(
            self.sb_logo, width=UITheme.SB_ICON_BOX + 2,
            height=UITheme.SB_ICON_BOX + 2, highlightthickness=0, bd=0,
            bg=_mix(pal.get('sidebar', UITheme.SIDEBAR_BG),
                    pal.get('accent', UITheme.COLOR_PRIMARY), 0.16),
            cursor="hand2")
        self.sb_logo_glyph.place(relx=0.5, rely=0.5, anchor="center")
        _paint_icon(self.sb_logo_glyph, 'note',
                    pal.get('accent', UITheme.COLOR_PRIMARY),
                    size=UITheme.SB_ICON_MARK - 1)
        self.sb_brand = ctk.CTkLabel(
            self.sidebar, text="TuneLab", font=UITheme.F(14, "bold"),
            anchor="w", cursor="hand2", height=UITheme.SB_ICON_W)
        self.sb_brand.place(x=UITheme.SB_LABEL_X, y=14)
        self._sb_bind_clickable(
            self.sb_logo, (self.sb_logo_glyph, self.sb_brand),
            lambda e: self.toggle_sidebar(),
            'Collapse / expand  (Ctrl+B)', self.sb_logo)

        y = 14 + UITheme.SB_ICON_W + 12
        for title, names in self._SB_GROUPS:
            hdr = ctk.CTkLabel(self.sidebar, text=" ".join(title),
                               font=UITheme.F(9, "bold"), anchor="w",
                               height=16)
            hdr.place(x=UITheme.SB_LABEL_X, y=y + 1)
            setattr(hdr, "_theme_roles", {"text_color": "sub"})
            self._sb_headers.append(hdr)
            y += 20
            for name in names:
                self._make_nav_row(name, y)
                y += UITheme.SB_ROW_H + UITheme.SB_ROW_GAP
            y += 8
        self._sb_nav_bottom = y

        # Footer: the collapse control belongs at the bottom of the rail,
        # under the list it controls, not as a bar above the brand. It is a
        # row like any other, so its label clips away when collapsed.
        self._sb_footer_sep = ctk.CTkFrame(self.sidebar, height=1,
                                           corner_radius=0, fg_color="transparent")
        self.sb_toggle_btn = ctk.CTkFrame(
            self.sidebar, width=UITheme.SB_ROW_W, height=UITheme.SB_ROW_H,
            corner_radius=UITheme.RADIUS_MD, fg_color="transparent",
            border_width=0,
        )
        self.sb_toggle_glyph = tk.Canvas(
            self.sb_toggle_btn, width=UITheme.SB_ICON_BOX,
            height=UITheme.SB_ICON_BOX, highlightthickness=0, bd=0,
            bg=pal.get('sidebar', UITheme.SIDEBAR_BG), cursor="hand2")
        self.sb_toggle_glyph.place(x=UITheme.SB_ICON_X, y=UITheme.SB_ICON_Y)
        _paint_icon(self.sb_toggle_glyph, self._sb_chevron(),
                    pal.get('sub', UITheme.COLOR_GRAY),
                    size=UITheme.SB_ICON_MARK)
        self.sb_toggle_lbl = ctk.CTkLabel(
            self.sb_toggle_btn, text='Collapse', font=UITheme.F(12),
            anchor="w", cursor="hand2", height=UITheme.SB_ROW_H)
        self.sb_toggle_lbl.place(x=UITheme.SB_TEXT_X, y=0)
        self._sb_bind_clickable(
            self.sb_toggle_btn, (self.sb_toggle_glyph, self.sb_toggle_lbl),
            lambda e: self.toggle_sidebar(),
            'Collapse / expand  (Ctrl+B)', self.sb_toggle_glyph,
            name='__footer__')

        # Accent pill: glides down the gutter to the selected row.
        self.nav_indicator = ctk.CTkFrame(
            self.sidebar,
            width=3,
            height=UITheme.SB_ROW_H - 12,
            fg_color=pal.get('accent', UITheme.COLOR_PRIMARY),
            corner_radius=1,
        )

        self._sb_set_width(UITheme.SB_W_EXPANDED if self._sidebar_expanded
                           else UITheme.SB_W_COLLAPSED)
        self._layout_sidebar()

        # Hover, peek and press are all decided in _sb_pointer_update from
        # the real pointer position, and every widget in the rail reports in
        # (Tk does not bubble events, so the chip and its labels each bind).
        for seq in ('<Enter>', '<Leave>', '<Motion>'):
            self.sidebar.bind(seq, self._sb_pointer_update)
        # Keeps the footer glued to the bottom as the window is resized.
        self.sidebar.bind('<Configure>', self._layout_sidebar)
        # Double-click empty rail space also toggles collapse.
        self.sidebar.bind('<Double-Button-1>', lambda e: self.toggle_sidebar())
        self._repaint_nav_rows()

    def _make_nav_row(self, name, y):
        """One nav row: icon column, label, and a right-hand slot.

        The slot shows the Ctrl+<n> digit, or a live badge when something is
        being counted (the Queue row shows unfinished items). Both sit at
        SB_HINT_X, past the collapsed rail's edge, so neither has to be
        hidden and nothing reflows.
        """
        icon, label = self._sb_items[name]
        row = ctk.CTkFrame(
            self.sidebar, width=UITheme.SB_ROW_W, height=UITheme.SB_ROW_H,
            corner_radius=UITheme.RADIUS_MD, fg_color="transparent",
            border_width=0,
        )
        row.place(x=UITheme.SB_PAD_X, y=y)
        # Nothing inside a chip ever moves: the icon column sits at one x, the
        # shortcut slot at another, and Tk clips whatever the rail's edge cuts.
        # The word label stops a pixel short of the chip's edges and two short of
        # its height, because a CTkLabel paints a rectangle of its own background
        # over whatever is under it - labels that spanned the row edge to edge bit
        # the selected row's hairline ring wherever they crossed and it arrived on
        # screen in dashes. It is still centred (1 + (38 - 2) / 2 == 38 / 2).
        # A canvas, not a label: the mark is drawn (see _icon_shapes), so it
        # fits its box exactly and takes whatever ink the row's state calls for.
        # The box is the mark and a pixel, never the whole column: a canvas is
        # opaque where a CTkLabel was transparent, so a box that reached the
        # chip's corners would paint its square over the chip's rounding.
        _pal = getattr(self, '_palette', {}) or {}
        ic = tk.Canvas(row, width=UITheme.SB_ICON_BOX,
                       height=UITheme.SB_ICON_BOX, highlightthickness=0,
                       bd=0, bg=_pal.get('sidebar', UITheme.SIDEBAR_BG),
                       cursor="hand2")
        ic.place(x=UITheme.SB_ICON_X, y=UITheme.SB_ICON_Y)
        _paint_icon(ic, icon, _pal.get('sub', UITheme.COLOR_GRAY),
                    size=UITheme.SB_ICON_MARK)
        tx = ctk.CTkLabel(row, text=label, font=UITheme.F(12), anchor="w",
                          cursor="hand2", height=UITheme.SB_ROW_H - 2)
        tx.place(x=UITheme.SB_TEXT_X, y=1)
        hint = ctk.CTkLabel(row, text=self._SB_KEYS.get(name, ""),
                            width=22, height=18, corner_radius=9,
                            font=UITheme.F(9, "bold"))
        hint.place(x=UITheme.SB_HINT_X, y=(UITheme.SB_ROW_H - 18) // 2)

        self._sb_bind_clickable(
            row, (ic, tx, hint), lambda e, n=name: self.show_frame(n),
            self._sb_tips[name], ic, name=name)
        self.nav_buttons[name] = row
        self._sb_icons[name] = ic
        self._sb_texts[name] = tx
        self._sb_hints[name] = hint
        return row

    def _sb_bind_clickable(self, row, children, on_click, tip, tip_anchor,
                           name=None):
        """Wire a rail row (or the brand/footer) for click, hover and press.

        Events do not bubble in Tk: the chip and each label inside it have to
        carry the bindings themselves, or hovering the text would do nothing.
        """
        def _release(event, n=name):
            # Act on release, like CTkButton: the press tint is visible for the
            # length of the click, and dragging off the row cancels the action.
            self._sb_press_release(n)
            if on_click is not None:
                on_click(event)

        for w in (row, *children):
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass   # CTkFrame has no cursor option; its labels carry it
            w.bind('<Button-1>', lambda e, n=name: self._sb_press_press(n))
            w.bind('<ButtonRelease-1>', _release)
            w.bind('<Enter>', self._sb_pointer_update)
            w.bind('<Leave>', self._sb_pointer_update)
            w.bind('<Motion>', self._sb_pointer_update)
            if tip:
                w.bind('<Enter>',
                       lambda e, a=tip_anchor, t=tip:
                       self._schedule_sb_tooltip(a, t), add='+')
                w.bind('<Leave>', lambda e, a=tip_anchor: self._hide_sb_tooltip(a),
                       add='+')

    def _sb_press_press(self, name=None):
        if name is not None and self._sb_press != name:
            self._sb_press = name
            self._repaint_nav_rows()

    def _sb_press_release(self, name=None):
        if self._sb_press is not None:
            self._sb_press = None
            self._repaint_nav_rows()

    def _refresh_sidebar_items(self):
        """Keep the one label that differs per state honest, and repaint.

        Deliberately does not touch row texts, fonts or positions: those are
        identical collapsed and expanded (the rail clips them), which is the
        whole reason the slide no longer pops. The footer word is only ever
        legible when the rail is wide, so _sb_set_width can rewrite it while
        it is clipped.
        """
        self._sb_set_footer_word(self._sb_shown)
        self._repaint_nav_rows()

    def _sb_chevron(self):
        """The footer's arrow: it points the way a click will move the rail."""
        expanded = getattr(self, '_sb_shown', self._sidebar_expanded)
        return 'chevron_left' if expanded else 'chevron_right'

    def _sb_set_footer_word(self, expanded):
        want = 'Collapse' if expanded else 'Expand'
        if getattr(self, '_sb_footer_word', None) == want:
            return
        self._sb_footer_word = want
        try:
            self.sb_toggle_lbl.configure(text=want)
            # The arrow turns with the word: a footer that reads "Expand" must
            # not still point left. The ink is whatever the last repaint chose.
            _paint_icon(self.sb_toggle_glyph, self._sb_chevron(),
                        getattr(self, '_sb_footer_ink', UITheme.COLOR_GRAY))
        except Exception:
            pass

    def toggle_sidebar(self):
        """Collapse or expand the rail (Ctrl+B, the footer row, or the brand)."""
        self._sb_cancel_peek()
        self._sb_peeking = False   # a click makes the shown state the real one
        self._sidebar_expanded = not self._sidebar_expanded
        self._set_pref('sidebar_collapsed', not self._sidebar_expanded)
        self._hide_sb_tooltip(force=True)
        self._animate_sidebar()  # repositions the accent pill when done

    def _sb_set_width(self, width):
        """The single place the rail's width is set.

        The chips follow the rail so they keep their own rounded corners
        instead of being sliced by its edge; everything *inside* a chip stays
        put and is clipped by Tk, which is what makes the transition read as
        a wipe rather than a reflow.
        """
        w = max(UITheme.SB_W_COLLAPSED,
                min(UITheme.SB_W_EXPANDED, int(round(width))))
        inner = max(UITheme.SB_ICON_W, w - 2 * UITheme.SB_PAD_X)
        try:
            self.sidebar.configure(width=w)
        except Exception:
            pass
        self._sidebar_cur_w = w
        for row in getattr(self, 'nav_buttons', {}).values():
            try:
                row.configure(width=inner)
            except Exception:
                pass
        for widget, shrink in ((getattr(self, 'sb_toggle_btn', None), 0),
                               (getattr(self, '_sb_footer_sep', None), 4)):
            if widget is None:
                continue
            try:
                widget.configure(width=max(1, inner - shrink))
            except Exception:
                pass
        # The footer word lives past the clip line while collapsed. Rewriting
        # it here - at the instant it is covered - is what keeps the wipe clean;
        # changing it at the top of a slide would show the word flipping while
        # still perfectly readable.
        if w <= UITheme.SB_LABEL_X + 8:
            self._sb_set_footer_word(self._sb_shown)

    def _layout_sidebar(self, event=None):
        """Pin the footer row and its hairline to the bottom of the rail."""
        try:
            h = self.sidebar.winfo_height()
        except Exception:
            return
        if h < 40 or h == getattr(self, '_sb_h', 0):
            return
        self._sb_h = h
        footer_y = max(getattr(self, '_sb_nav_bottom', 0) + 6,
                       h - UITheme.SB_ROW_H - 10)
        try:
            self.sb_toggle_btn.place(x=UITheme.SB_PAD_X, y=footer_y)
            self._sb_footer_sep.place(x=UITheme.SB_PAD_X + 2, y=footer_y - 9)
        except Exception:
            pass

    def _animate_sidebar(self, target_expanded=None):
        """Glide the rail's width - the only thing in it that changes.

        One eased run of _sb_set_width calls. The previous version also
        swapped every row's text/anchor/font/width at 60% of the run, which is
        exactly the "pop in and out" this replaces.
        """
        try:
            if self._sb_anim_after_id:
                try:
                    self.after_cancel(self._sb_anim_after_id)
                except Exception:
                    pass
            self._sb_anim_after_id = None

            if target_expanded is None:
                target_expanded = self._sidebar_expanded
            target_w = float(UITheme.SB_W_EXPANDED if target_expanded
                             else UITheme.SB_W_COLLAPSED)
            start_w = float(getattr(self, '_sidebar_cur_w', 0)
                            or self.sidebar.winfo_width()
                            or target_w)
            self._sb_shown = bool(target_expanded)

            def finish():
                self._sb_anim_after_id = None
                self._sb_set_width(target_w)
                # Safety net: a mid-slide reversal may never have reached the
                # clipped widths where the word is normally rewritten.
                self._sb_set_footer_word(self._sb_shown)
                try:
                    self._hide_sb_tooltip(force=True)
                    self._position_nav_indicator_initial()
                    self._repaint_nav_rows()
                except Exception:
                    pass

            if (not getattr(self, 'nav_anim_enabled', True)
                    or abs(target_w - start_w) < 1.5):
                finish()
                return

            _interval = 14  # ms (~70fps)
            _steps = max(6, int(getattr(self, 'nav_anim_speed', 12) or 12))

            def ease(t):
                return 1 - (1 - t) ** 4   # ease-out-quart: leaves fast, stops soft

            def step(i):
                t = min(1.0, i / _steps)
                self._sb_set_width(start_w + (target_w - start_w) * ease(t))
                if i < _steps:
                    self._sb_anim_after_id = self.after(
                        _interval, lambda: step(i + 1))
                else:
                    finish()

            step(0)
        except Exception:
            pass

    def _sb_pointer_update(self, event=None):
        """The single brain for hover and peek: derive it from the pointer.

        Enter/Leave fire per widget and Tk does not bubble, so moving from a
        row onto its own icon label fires Leave then Enter. Re-deriving from
        the real pointer position turns that churn into a no-op instead of a
        flicker, and clipped rows (whose geometry still says "wide") are
        excluded by the rail's visible right edge.
        """
        try:
            rail = getattr(self, 'sidebar', None)
            if rail is None or not rail.winfo_ismapped():
                return
            px, py = self.winfo_pointerxy()
            rx, ry = rail.winfo_rootx(), rail.winfo_rooty()
            rw, rh = rail.winfo_width(), rail.winfo_height()
            inside = (rx <= px < rx + rw) and (ry <= py < ry + rh)
            hover = None
            if inside:
                edge = rx + rw
                candidates = list(getattr(self, 'nav_buttons', {}).items())
                candidates.append(('__footer__',
                                   getattr(self, 'sb_toggle_btn', None)))
                for name, row in candidates:
                    if row is None or not row.winfo_ismapped():
                        continue
                    x, y = row.winfo_rootx(), row.winfo_rooty()
                    w = min(row.winfo_width(), edge - x)
                    if w <= 0:
                        continue
                    if x <= px < x + w and y <= py < y + row.winfo_height():
                        hover = name
                        break
            if hover != getattr(self, '_sb_hover', None):
                self._sb_hover = hover
                self._repaint_nav_rows()
            # Peek is armed by the nav rows, not by the whole rail: resting on
            # the footer chip - which sits in the very same column, so an x
            # band cannot tell them apart - or on the empty space under the
            # list must not throw the rail open. While a peek is already open
            # the pointer may go anywhere inside it, or the rail would slam
            # shut the moment it moved onto the labels it just revealed.
            armed = hover is not None and hover != '__footer__'
            self._sb_peek_track(inside and (armed or self._sb_peeking))
        except Exception:
            pass

    def _sb_peek_track(self, inside):
        """Let a collapsed rail glide open while the pointer is resting in it.

        Only the width changes and the icon column never moves, so the rail
        can open under the cursor without the target item shifting.
        """
        if not getattr(self, '_sb_peek_pref', False):
            return
        if inside:
            if (self._sidebar_expanded or self._sb_peeking
                    or self._sb_peek_after_id is not None
                    or getattr(self, '_sb_anim_after_id', None)):
                return
            try:
                # 220ms: passing through on the way to the content should not
                # open the rail, resting on an icon should.
                self._sb_peek_after_id = self.after(220, self._sb_peek_open)
            except Exception:
                self._sb_peek_after_id = None
            return
        self._sb_cancel_peek()
        if self._sb_peeking:
            self._sb_peeking = False
            self._animate_sidebar(False)

    def _sb_cancel_peek(self):
        pending = getattr(self, '_sb_peek_after_id', None)
        if pending:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        self._sb_peek_after_id = None

    def _sb_peek_open(self):
        self._sb_peek_after_id = None
        if self._sidebar_expanded or self._sb_peeking:
            return
        self._sb_peeking = True
        self._animate_sidebar(True)

    def _repaint_nav_rows(self):
        """Paint every rail piece from the palette + hover/press/active state.

        Selection is an accent-tinted chip with a hairline ring and an accent
        icon. Both are mixed per palette, because the old fixed #2c3e50 chip
        vanished on the dark rails and went muddy on the light ones.
        """
        pal = getattr(self, '_palette', {}) or {}
        base = pal.get('sidebar', UITheme.SIDEBAR_BG)
        accent = pal.get('accent', UITheme.COLOR_PRIMARY)
        text_c = pal.get('text', '#ecf0f1')
        sub_c = pal.get('sub', UITheme.COLOR_GRAY)
        hover_fill = _mix(base, pal.get('hover', UITheme.SIDEBAR_HOVER), 0.85)
        active_fill = _mix(base, accent, 0.18)
        active_edge = _mix(base, accent, 0.45)
        press_fill = _mix(base, accent, 0.32)
        idle_text = _mix(base, text_c, 0.82)
        active = getattr(self, '_active_nav_name', None)
        hover = getattr(self, '_sb_hover', None)
        press = getattr(self, '_sb_press', None)

        for name, row in getattr(self, 'nav_buttons', {}).items():
            is_active = name == active
            fill, edge, bw = "transparent", "transparent", 0
            icon_c, label_c = sub_c, idle_text
            if is_active:
                fill, edge, bw = active_fill, active_edge, 1
                icon_c, label_c = accent, text_c
            elif name == press:
                fill, icon_c, label_c = press_fill, accent, text_c
            elif name == hover:
                fill, icon_c, label_c = hover_fill, accent, text_c
            try:
                row.configure(fg_color=fill, border_width=bw,
                              border_color=_ring_color(edge, fill, base))
                canvas = self._sb_icons[name]
                # The chip's colour travels to the icon by hand: a canvas is an
                # opaque Tk widget, not a transparent CTk label, so a background
                # left behind would frame the glyph in a little rectangle.
                canvas.configure(bg=fill if fill != "transparent" else base)
                _paint_icon(canvas, self._sb_items[name][0], icon_c)
                self._sb_texts[name].configure(text_color=label_c)
            except Exception:
                logger.debug("nav row %r repaint failed", name, exc_info=True)
            self._repaint_nav_hint(name, base, accent, text_c, sub_c)

        # Brand tile, footer chip, hairline, section headers.
        try:
            tile = _mix(base, accent, 0.16)
            self.sb_logo.configure(
                fg_color=tile,
                border_width=1, border_color=_mix(base, accent, 0.34))
            self.sb_logo_glyph.configure(bg=tile)
            _paint_icon(self.sb_logo_glyph, 'note', accent, size=17)
            self.sb_brand.configure(text_color=text_c)
        except Exception:
            pass
        try:
            on_footer = hover == '__footer__'
            foot_fill = (press_fill if press == '__footer__'
                         else (hover_fill if on_footer else "transparent"))
            self.sb_toggle_btn.configure(fg_color=foot_fill, border_width=0)
            self._sb_footer_ink = accent if on_footer else sub_c
            self.sb_toggle_glyph.configure(
                bg=foot_fill if foot_fill != "transparent" else base)
            _paint_icon(self.sb_toggle_glyph, self._sb_chevron(),
                        self._sb_footer_ink)
            self.sb_toggle_lbl.configure(
                text_color=text_c if on_footer else idle_text)
            self._sb_footer_sep.configure(fg_color=_mix(base, sub_c, 0.28))
        except Exception:
            pass
        for hdr in getattr(self, '_sb_headers', []):
            try:
                hdr.configure(text_color=_mix(base, sub_c, 0.9))
            except Exception:
                pass

    def _repaint_nav_hint(self, name, base: Optional[str] = None,
                          accent: Optional[str] = None,
                          text_c: Optional[str] = None,
                          sub_c: Optional[str] = None):
        """Right slot: an accent badge while a count is set, else the digit.

        Takes the already-mixed colors when _repaint_nav_rows is looping (so
        the palette is read once), and can be called with just a name from
        _set_nav_badge when a count changes on its own.
        """
        lbl = getattr(self, '_sb_hints', {}).get(name)
        if lbl is None:
            return
        if base is None:
            pal = getattr(self, '_palette', {}) or {}
            base = pal.get('sidebar', UITheme.SIDEBAR_BG)
            accent = pal.get('accent', UITheme.COLOR_PRIMARY)
            text_c = pal.get('text', '#ecf0f1')
            sub_c = pal.get('sub', UITheme.COLOR_GRAY)
        # A caller may hand over only the row's base fill, and a palette key
        # can come back blank, so settle every slot: _on_color() cannot take
        # a None fill and a None ink would leave the badge unreadable.
        base = base or UITheme.SIDEBAR_BG
        accent = accent or UITheme.COLOR_PRIMARY
        text_c = text_c or '#ecf0f1'
        sub_c = sub_c or UITheme.COLOR_GRAY
        count = getattr(self, '_sb_badges', {}).get(name) or 0
        try:
            if count:
                lbl.configure(
                    text=str(count if count < 100 else "99+"),
                    fg_color=accent,
                    text_color=_on_color(accent, text_c, base),
                    border_width=0)
            else:
                lbl.configure(text=self._SB_KEYS.get(name, ""),
                              fg_color="transparent", text_color=sub_c)
        except Exception:
            pass

    def _set_nav_badge(self, name: str, count) -> None:
        """Put a live count in a nav row's right slot (0 clears it)."""
        try:
            self._sb_badges[name] = int(count or 0)
        except Exception:
            return
        self._repaint_nav_hint(name)

    def _schedule_sb_tooltip(self, widget, text: str, only_when_collapsed: bool = True):
        """Delay tooltip show to avoid flicker when the mouse moves quickly."""
        try:
            # Remember what the bubble belongs to: the row, not the exact label
            # the pointer happened to be over (see _hide_sb_tooltip).
            self._sb_tip_owner = self._sb_tip_row(widget)
            # Repainting a row re-places the CTk canvas/label inside it, and Tk
            # answers by firing Enter/Leave for rows the pointer is nowhere
            # near - which used to open the wrong row's bubble, or cancel the
            # right one. An Enter from the rail only counts when the pointer is
            # genuinely on that row.
            if self._sb_in_rail(widget) and \
                    not self._sb_pointer_on(self._sb_tip_owner):
                return
            pending = getattr(self, '_sb_tooltip_after_id', None)
            if pending:
                try:
                    self.after_cancel(pending)
                except Exception:
                    pass
            self._sb_tooltip_after_id = self.after(
                450, lambda: self._show_sb_tooltip(widget, text, only_when_collapsed))
        except Exception:
            pass

    def _show_sb_tooltip(self, widget, text: str, only_when_collapsed: bool = True):
        """Floating label just outside the rail, level with *widget*.

        The collapsed nav rail uses it for icon names; the History row
        actions reuse it (``only_when_collapsed=False``) for their
        glyph-only buttons.
        """
        try:
            # Suppress tooltip during sidebar animation to prevent flicker.
            if getattr(self, '_sb_anim_after_id', None):
                return
            # Also while peeking: the labels are already readable then, and a
            # bubble over them would only cover the row it describes.
            if only_when_collapsed and (self._sidebar_expanded
                                        or getattr(self, '_sb_shown', False)):
                self._hide_sb_tooltip(force=True)
                return
            # One anchor per row, pinned to the rail's edge: anchoring to
            # whichever label the pointer happened to be over made the bubble
            # jump to three different spots as the mouse crossed an icon, its
            # text and its badge slot. Widgets outside the rail (the History
            # row's glyph buttons) still get their bubble beside themselves.
            rail = self.sidebar
            if self._sb_in_rail(widget):
                x = rail.winfo_rootx() + rail.winfo_width() + 6
            else:
                x = widget.winfo_rootx() + widget.winfo_width() + 8
            y = widget.winfo_rooty() + max(0, (widget.winfo_height() - 26) // 2)

            tip_bg = self._palette.get('sidebar_active', UITheme.SIDEBAR_ACTIVE)
            tip_fg = self._palette.get('text', '#ecf0f1')

            tp = self._sb_popup()
            self._sb_tooltip_lbl.configure(text=text, bg=tip_bg, fg=tip_fg)
            tp.wm_geometry(f'+{x}+{y}')

            # Avoid restack churn: only re-paint/re-show when the popup is
            # not already visible. Rapid mouse passes across icons <Enter>/
            # <Leave> fire repeatedly; re-deiconifying + update_idletasks on
            # every single one is what made the icons flicker.
            if tp.state() != 'normal' or not tp.winfo_viewable():
                tp.update_idletasks()
                tp.deiconify()
        except Exception:
            # Whatever went wrong, nothing may be left painted on screen.
            # Dropping the only reference to a mapped popup here is what used
            # to strand little boxes around the rail that never went away.
            self._drop_sb_popups()

    @staticmethod
    def _alive(widget) -> bool:
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    def _sb_in_rail(self, widget) -> bool:
        """Is *widget*, or one of its ancestors, part of the rail?"""
        rail = getattr(self, 'sidebar', None)
        node = widget
        while node is not None and rail is not None:
            if node is rail:
                return True
            node = getattr(node, 'master', None)
        return False

    def _sb_tip_row(self, widget):
        """The rail row *widget* belongs to, or *widget* itself.

        The bubble is scheduled from an anchor (the icon) but has to stay put
        while the pointer wanders anywhere over the same row, so the region to
        test is the row and not the glyph.
        """
        rail = getattr(self, 'sidebar', None)
        node = getattr(widget, 'master', None)
        while node is not None and rail is not None:
            if getattr(node, 'master', None) is rail:
                return node
            node = getattr(node, 'master', None)
        return widget

    def _sb_pointer_on(self, owner) -> bool:
        """Is the pointer still on *owner* (or on the bubble above it)?

        A rail row is measured up to the rail's visible edge - collapsed, its
        geometry still claims the full expanded width and would swallow part of
        the page. The bubble itself is the exception: it hangs over the page
        just outside the rail, and a tooltip you can dismiss by hovering it
        would vanish the moment you tried to read it.
        """
        try:
            if owner is None or not owner.winfo_exists():
                return False
            px, py = self.winfo_pointerxy()
            for tp in getattr(self, '_sb_popups', []):
                if not tp.winfo_exists() or not tp.winfo_viewable():
                    continue
                if (tp.winfo_rootx() <= px < tp.winfo_rootx() + tp.winfo_width()
                        and tp.winfo_rooty() <= py
                        <= tp.winfo_rooty() + tp.winfo_height()):
                    return True
            x, y = owner.winfo_rootx(), owner.winfo_rooty()
            w, h = owner.winfo_width(), owner.winfo_height()
            rail = getattr(self, 'sidebar', None)
            if rail is not None and owner is not rail and self._sb_in_rail(owner):
                w = min(w, rail.winfo_rootx() + rail.winfo_width() - x)
            return w > 0 and h > 0 and x <= px < x + w and y <= py < y + h
        except Exception:
            return False

    def _sb_popup(self):
        """The single reused popup, created on first use.

        Reusing ONE hidden Toplevel instead of creating/destroying a borderless
        one per hover is deliberate: on Windows a freshly mapped borderless
        Toplevel paints as a solid black rectangle for a frame. Any popup that
        went missing (destroyed with its toplevel, say) is dropped here too, so
        at most one of them can ever exist.
        """
        live = [tp for tp in getattr(self, '_sb_popups', []) if self._alive(tp)]
        for dead in getattr(self, '_sb_popups', []):
            if dead not in live:
                try:
                    dead.destroy()
                except Exception:
                    pass
        self._sb_popups = live
        if live:
            return live[0]
        tp = tk.Toplevel(self)
        tp.withdraw()
        tp.overrideredirect(True)
        tp.attributes('-topmost', True)
        # The bubble is allowed to be hovered (see _sb_pointer_on), and nothing
        # in the rail learns when the pointer finally comes off it - so it has
        # to dismiss itself. A bubble that outlives its hover is the exact
        # fragment this rail used to litter the page with.
        tp.bind('<Leave>', lambda e: self._hide_sb_tooltip(force=True))
        self._sb_tooltip_lbl = tk.Label(
            tp,
            padx=9,
            pady=4,
            font=UITheme.F(10),
        )
        self._sb_tooltip_lbl.pack()
        self._sb_popups = [tp]
        return tp

    def _drop_sb_popups(self):
        """Destroy every tooltip popup (used when one cannot be trusted)."""
        for tp in getattr(self, '_sb_popups', []):
            try:
                tp.destroy()
            except Exception:
                pass
        self._sb_popups = []

    def _hide_sb_tooltip(self, widget=None, force: bool = False):
        """Withdraw the bubble once the pointer has really left its row.

        A repaint that re-places a CTk label makes Tk report a <Leave> for the
        widget the pointer is still sitting on - and hovering a row repaints it
        (icon and label change colour). Cancelling on that Leave is why the
        bubble never appeared at all while a rail row was hovered: the hover
        fired Enter, the repaint it caused fired Leave, and the pending show was
        cancelled 450ms before it was due. So a Leave only counts when the
        pointer is off the row the bubble belongs to.
        """
        owner = widget if widget is not None else getattr(self, '_sb_tip_owner', None)
        if not force and owner is not None and self._sb_pointer_on(owner):
            return
        self._sb_tip_owner = None
        # Cancel any pending delayed show when the mouse moves away (flicker fix).
        pending = getattr(self, '_sb_tooltip_after_id', None)
        if pending:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
            self._sb_tooltip_after_id = None
        for tp in getattr(self, '_sb_popups', []):
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

    # Semantic button roles: one accent per screen (primary), a neutral
    # secondary, danger for destructive actions — no per-widget color
    # improvisation. Maps widget option -> palette role.
    _BTN_ROLES = {
        "primary":   {"fg_color": "accent", "hover_color": "accent_hover"},
        "secondary": {"fg_color": "sidebar_active", "hover_color": "hover"},
        "danger":    {"fg_color": "danger", "hover_color": "danger_hover"},
        "success":   {"fg_color": "success", "hover_color": "success_hover"},
        "warning":   {"fg_color": "warning", "hover_color": "warning_hover"},
        "purple":    {"fg_color": "purple", "hover_color": "purple_hover"},
        "ghost":     {"hover_color": "hover"},
    }

    def _style_button(self, btn, role: str, bordered: bool = True):
        """Paint *btn* with the semantic palette *role*.

        Build-time colors come from the active palette instead of
        hardcoded hexes, the label color is chosen for WCAG contrast
        against the fill (``_on_color``), and the option->role mapping is
        remembered on the widget (``_theme_roles``) so every later theme
        switch repaints it too.

        Shape is part of the recipe too: every button is lifted to the
        shared control radius (a call site may ask for a rounder chip,
        never a boxier one), and ghost buttons get the hairline outline
        the dialogs use for secondary actions. Pass ``bordered=False`` for
        a ghost control that already sits inside another outlined widget.
        """
        pal = getattr(self, "_palette", {}) or {}
        roles = self._BTN_ROLES.get(role)
        if not roles:
            return btn
        # Values are mixed: hex colours plus the int corner_radius/border_width.
        colors: dict[str, Any] = {}
        for opt, role_name in roles.items():
            val = pal.get(role_name)
            if val:
                colors[opt] = val
        if role == "ghost":
            colors.setdefault("fg_color", "transparent")
        fill = colors.get("fg_color", "transparent")
        if fill == "transparent":
            colors["text_color"] = pal.get("text", "#ecf0f1")
        else:
            colors["text_color"] = _on_color(
                fill, pal.get("text", "#ecf0f1"), pal.get("bg", "#1e1e24"))
        # Curved ends everywhere: a call site may ask for something rounder
        # (a pill chip) but never boxier than the shared control radius.
        try:
            if int(btn.cget("corner_radius") or 0) < UITheme.RADIUS_MD:
                colors["corner_radius"] = UITheme.RADIUS_MD
        except Exception:
            colors["corner_radius"] = UITheme.RADIUS_MD
        if role == "ghost":
            # Ghost means outlined — the dialog recipe. Filled buttons
            # already separate themselves from the surface by their fill.
            colors["border_width"] = UITheme.BORDER_W if bordered else 0
            if bordered:
                colors["border_color"] = pal.get("hover", "#34495e")
        try:
            btn.configure(**colors)
        except Exception:
            logger.debug("Styling button as %r failed", role, exc_info=True)
        try:
            tag = dict(getattr(btn, "_theme_roles", None) or {})
            tag.update(roles)
            tag["text_color"] = (
                "text" if colors.get("text_color") == pal.get("text") else "bg")
            if colors.get("border_color"):
                tag["border_color"] = "hover"
            setattr(btn, "_theme_roles", tag)
        except Exception:
            pass
        return btn

    def _style_option_menu(self, menu):
        """Theme a CTkOptionMenu: neutral panel + accent select button."""
        pal = getattr(self, "_palette", {}) or {}
        fill = pal.get("sidebar_active", "#2c3e50")
        text = _on_color(
            fill, pal.get("text", "#ecf0f1"), pal.get("bg", "#1e1e24"))
        colors = {
            "fg_color": fill,
            "button_color": pal.get("accent", "#3498db"),
            "button_hover_color": pal.get("accent_hover", "#2980b9"),
            "text_color": text,
            "corner_radius": UITheme.RADIUS_FIELD,
            "border_width": UITheme.BORDER_W,
            "border_color": pal.get("hover", "#34495e"),
        }
        try:
            menu.configure(**colors)
        except Exception:
            logger.debug("Styling option menu failed", exc_info=True)
        try:
            setattr(menu, "_theme_roles", {
                "fg_color": "sidebar_active",
                "button_color": "accent",
                "button_hover_color": "accent_hover",
                "border_color": "hover",
                "text_color": "text" if text == pal.get("text") else "bg",
            })
        except Exception:
            pass
        return menu

    def _settings_card(self, parent, title):
        """Bordered card with a section header.

        The settings pages group their controls into these instead of one
        flat undifferentiated list. Colors come from the palette and the
        option->role pairs are tagged so theme switches repaint the card.
        """
        pal = getattr(self, "_palette", {}) or {}
        card = ctk.CTkFrame(
            parent,
            corner_radius=UITheme.RADIUS_LG,
            fg_color=pal.get("sidebar", "#1b2532"),
            border_width=1,
            border_color=pal.get("hover", "#34495e"),
        )
        setattr(card, "_theme_roles",
                {"fg_color": "sidebar", "border_color": "hover"})
        hdr = ctk.CTkLabel(
            card, text=title, font=UITheme.F(13, "bold"), anchor="w",
            text_color=pal.get("text", "#ecf0f1"),
        )
        hdr.pack(fill="x", padx=16, pady=(12, 4))
        setattr(hdr, "_theme_roles", {"text_color": "text"})
        return card

    def _iter_widgets(self, root=None):
        if root is None:
            root = self
        for child in root.winfo_children():
            yield child
            yield from self._iter_widgets(child)

    def _style_form_controls(self, pal=None):
        """Paint every checkbox, slider and progress bar from the palette.

        Walks the live tree by *type* instead of a list of attribute names.
        The name lists this replaces had gone stale: they still named four
        checkboxes that no longer exist and missed the two the rail settings
        card adds, so those two kept CustomTkinter's built-in blue in every
        one of the 21 palettes.

        The options a call site never sets are the other half of the problem:
        CTk hands out its own theme colors for those, and ``cget()`` returns
        them as *tuples*, which the role walk skips because it only remaps
        strings. Nothing else would ever have recolored them, so a slider's
        track and a progress bar's trough stayed CTk's grays and a slider's
        knob hovered CTk's blue whatever the palette said.
        """
        pal = pal or getattr(self, "_palette", {}) or {}
        accent = pal.get("accent", UITheme.COLOR_PRIMARY)
        accent_hover = pal.get("accent_hover", "#2980b9")
        text = pal.get("text", "#ecf0f1")
        # The unchecked ring has to be *seen*: the palette's hover tone is a
        # surface step, too close to the page on the light themes, so the
        # ring uses the muted text tone instead.
        edge = pal.get("sub", UITheme.COLOR_GRAY)
        # Tracks and troughs are the inactive half of a control, so they take
        # the raised-surface tone (same one the cards' hairlines use).
        trough = pal.get("hover", UITheme.SIDEBAR_HOVER)
        # The checkmark is drawn *on* the accent, so it is chosen for contrast
        # (a palette text tone would vanish into a yellow or cyan accent).
        on_accent = _on_color(accent, text, pal.get("bg", "#1e1e24"))
        recipes = (
            (ctk.CTkCheckBox, {"fg_color": accent, "hover_color": accent_hover,
                               "border_color": edge,
                               "checkmark_color": on_accent, "text_color": text}),
            (ctk.CTkSlider, {"fg_color": trough, "progress_color": accent,
                             "button_color": text,
                             "button_hover_color": accent_hover}),
            (ctk.CTkProgressBar, {"fg_color": trough, "progress_color": accent,
                                  "border_color": trough}),
        )
        done = 0
        for widget in self._iter_widgets():
            for cls, colors in recipes:
                if not isinstance(widget, cls):
                    continue
                try:
                    widget.configure(**colors)
                    done += 1
                except Exception:
                    logger.debug("Styling a %s failed", cls.__name__,
                                 exc_info=True)
                break
        return done

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

        # Match Light/Dark appearance so entries/menus follow the palette —
        # the palette owns the mode now (the Theme Mode dropdown is gone).
        try:
            ctk.set_appearance_mode('Light' if pal.get('mode') == 'light' else 'Dark')
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
                      'text_color', 'progress_color', 'button_color',
                      'button_hover_color', 'label_fg_color',
                      'label_text_color')
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
        # Queue rows are CTk widgets now, but their palette-derived colors
        # are plain values the role walk can't discover — re-feed them
        # explicitly so a theme switch repaints panel, rows and selection.
        try:
            qrows = getattr(self, "queue_rows", None)
            if qrows is not None:
                qrows.configure(
                    bg=pal.get('bg', '#2c3e50'),
                    fg=pal.get('text', '#ecf0f1'),
                    selectbackground=pal.get('accent', '#3498db'),
                    selectforeground=_on_color(
                        pal.get('accent', '#3498db'),
                        pal.get('text', '#ecf0f1'), pal.get('bg', '#1e1e24')),
                )
        except Exception:
            pass
        # Form controls (checkboxes, sliders, progress bars) are painted from
        # the palette by *type*, not by name: the name lists that used to live
        # here had gone stale — they still named four checkboxes that no
        # longer exist and missed the two the rail settings card adds — and
        # they never touched the options a call site leaves unset (a track, a
        # trough, a checkmark), which is where CTk's own colors leaked in.
        # Every part of a control is set in the one pass below.
        self._style_form_controls(pal)

        # Header + rail text accents
        try:
            self.title_lbl.configure(text_color=pal['text'])
        except Exception:
            pass
        # The rail derives every piece of it (chips, badges, brand tile,
        # footer, section headers) from the palette, so a theme switch is just
        # a repaint - this used to hand-tune two labels and leave the rest of
        # the rail on the previous theme's colors.
        self._repaint_nav_rows()

        # Swatch selection ring: the clickable swatch dots are gone (theme
        # picking lives in the dropdown now), so there is nothing left to ring.

    def load_background_gif(self):
        if Image is None:
            self._show_error_dialog("Dependency Missing", "Pillow is required to load GIF backgrounds. Install with: pip install pillow")
            return

        gif_path = filedialog.askopenfilename(title="Select Animated GIF", filetypes=[("GIF Animation", "*.gif")])
        if not gif_path:
            return

        try:
            pil_img = Image.open(gif_path)
        except Exception as e:
            self._show_error_dialog("Background Error", f"Could not open GIF:\n{e}")
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
            self._show_error_dialog("Background Error", f"Failed processing GIF frames:\n{e}")
            return

        if not frames:
            self._show_error_dialog("Background Error", "Could not load frames from the selected GIF.")
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

                self.after(0, lambda: self.show_toast(msg, "success", duration=6000))
                self.after(0, lambda: self.update_dl_status("Cache cleared.", "#2ecc71"))
            except Exception as e:
                self.after(0, lambda: self._show_error_dialog("Cache Clear Error", str(e)))
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
            self.show_toast("Please load an audio file first.", "warning")
            return
        if _lazy_import_sounddevice() is None:
            self._show_error_dialog("Module Missing", "sounddevice package missing.")
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
            self.after(0, lambda err=e: self._show_error_dialog("Missing Dependency", str(err)))
            self.after(0, self.reset_studio_buttons)
        except Exception as e:
            self.after(0, lambda err=e: self._show_error_dialog("Playback Error", f"Could not play preview:\n{err}"))
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
            self._show_warning_dialog("File Missing", "Please select an audio file first.")
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
                        self.after(0, lambda: self._show_error_dialog(
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
                    self.after(0, lambda: self._show_success_dialog(
                        "Export complete",
                        f"Saved {os.path.basename(dest)}",
                        detail=dest))
                else:
                    self.update_studio_progress(0.0)
                    self.after(0, lambda: self._show_error_dialog(
                        "Export Failed", "The audio could not be processed."))
            except Exception as e:
                self.update_studio_progress(0.0)
                self.after(0, lambda err=e: self._show_error_dialog(
                    "Export Interrupted", "The export failed.", detail=str(err)))
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

    # Single instance on Windows: a second launch brings the running window
    # to the front instead of starting a twin with its own queue and
    # downloads. TUNELAB_MULTI_INSTANCE=1 bypasses it for development.
    if sys.platform == "win32" and os.environ.get("TUNELAB_MULTI_INSTANCE") != "1":
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

            # ERROR_ALREADY_EXISTS (183): the mutex was already there, so a
            # TuneLab is running. Its title may carry a download percentage,
            # so the lookup matches the "TuneLab" prefix.
            _k32.CreateMutexW(None, False, "TuneLab_SingleInstance_Mutex")
            if ctypes.get_last_error() == 183:
                _found: list[Any] = []

                @ctypes.WINFUNCTYPE(
                    wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)
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
                    sys.exit(0)
                # Mutex exists but no window was found (startup race): fall
                # through and launch normally rather than exiting blind.
        except Exception:
            logger.debug("Single-instance check failed", exc_info=True)

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

