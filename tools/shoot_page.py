"""Grab one screenshot of a named page, for eyeballing a layout change.

Run:  python tools/shoot_page.py performance [out.png]

Builds the real window, switches to the page, and saves a PNG of the window.

Options:
  --theme NAME   start from a preset palette, or "custom" to build one first
  --set role=hex  override one role of that palette (repeatable), so a
                 screenshot can show a palette the saved prefs do not hold
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import customtkinter as ctk  # noqa: E402
from PIL import ImageGrab  # noqa: E402
import ui
from ui import UniversalAudioStudio  # noqa: E402

PAGE = next((a for a in sys.argv[1:] if not a.startswith("-")), "performance")
OUT = next((a for a in sys.argv[1:] if a.lower().endswith(".png")),
           os.path.join(os.path.dirname(HERE), f"_page_{PAGE}.png"))


def _opt(flag):
    """The value of --flag X, or None."""
    argv = sys.argv[1:]
    return argv[argv.index(flag) + 1] if flag in argv else None


def _overrides():
    """Every --set role=hex pair, as a dict."""
    out = {}
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--set" and i + 1 < len(argv) and "=" in argv[i + 1]:
            role, _, value = argv[i + 1].partition("=")
            out[role] = value
    return out


app = UniversalAudioStudio()
app.geometry("1100x760+40+40")
app.update_idletasks()

# The scroll frame is created by build_customization_view, so it exists by now.
_scroll = getattr(app, "settings_scroll", None)

# Theme overrides are applied after construction and without persisting, so a
# screenshot run never writes the palette into the real ui_prefs.json.
theme = _opt("--theme")
if theme:
    seed = "TuneLab Dark" if theme.lower() == "custom" else theme
    src = ui.COLOR_THEMES.get(seed)
    if src is None:
        raise SystemExit("unknown theme %r" % seed)
    roles = {role: src[role] for role in ui.CUSTOM_THEME_ROLES if role in src}
    for role, value in _overrides().items():
        if role in ui.CUSTOM_THEME_ROLES:
            roles[role] = value
    app._custom_roles = roles
    app._custom_mode = src.get("mode", "dark")
    app._apply_custom_palette(persist=False)

app.show_frame(PAGE)
for _ in range(40):
    app.update_idletasks()
    app.update()
    time.sleep(0.01)

# ImageGrab reads whatever is on screen at those coordinates, so the window has
# to actually be in front - a raise() is only a request on Windows, and
# topmost is what the chrome probe relies on too.
app.attributes("-topmost", True)
app.deiconify()
app.lift()
for _ in range(40):
    app.update_idletasks()
    app.update()
    time.sleep(0.02)

# --scroll FRAC grabs with the page scrolled to that fraction of its height
# (0 = top, 1 = bottom). The Appearance page is taller than the window, so a
# screenshot of its lower half needs this. It drives the canvas directly rather
# than faking a wheel event: a synthesised <MouseWheel> lands on whichever
# widget has focus, and here nothing does.
scroll_to = None
if "--scroll" in sys.argv:
    scroll_to = min(1.0, max(0.0, float(sys.argv[sys.argv.index("--scroll") + 1])))
if scroll_to and _scroll is not None:
    _canvas = getattr(_scroll, "_parent_canvas", None)
    if _canvas is not None:
        _canvas.yview_moveto(scroll_to)
for _ in range(20):
    app.update_idletasks()
    app.update()
    time.sleep(0.01)

x, y = app.winfo_rootx(), app.winfo_rooty()
w, h = app.winfo_width(), app.winfo_height()
ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(OUT)
print(OUT)
app.destroy()
