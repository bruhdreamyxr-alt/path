"""Measure the drawn corner curvature of the app's chrome surfaces.

CustomTkinter paints a rounded frame by leaving the corner triangles in the
parent's color (``_bg_color``) — so a corner only reads as curved if the pixels
along its diagonal stop being that color before the frame's own box corner.
That is exactly what this measures, using the widget's *own* reference color
instead of guessing at whatever happens to be painted behind it.

Why this exists: DWMWA_WINDOW_CORNER_PREFERENCE is a confirmed no-op on a Tk
window — Win10 has no such API and Win11 rounds the whole frame regardless of
what is requested — so a "rounded window" here is an optical trick: rounded
cards floating on a matching background. This probe proves the trick happens at
every level of the chrome, and names the culprit when it does not: a corner
whose notch color equals its own fill is a palette problem (the curve is drawn
but invisible), and a corner covered by a sibling is a layering problem.

Run:  python tools/probe_chrome.py [output-dir]   the two themes, with detail
      python tools/probe_chrome.py --all           every palette, failures only
      python tools/probe_chrome.py --roles         palette sanity, no window
Prints one line per surface and saves magnified corner crops plus a
whole-window shot (default output dir: %TEMP%/chrome_probe).
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# First non-flag argument, if any, overrides where the crops land.
OUT = next((a for a in sys.argv[1:] if not a.startswith("-")),
           os.path.join(tempfile.gettempdir(), "chrome_probe"))

import customtkinter as ctk  # noqa: E402
from PIL import Image, ImageGrab  # noqa: E402
from ui import COLOR_THEMES, UniversalAudioStudio  # noqa: E402

TOL = 5  # per-channel difference still counted as "same color" — palettes can
# sit only ~10 apart per channel (light themes), so this has to stay tight.


def shot(img, name, scale=1):
    """Save a crop for eyeballing, outside the repo so a run stays clean."""
    os.makedirs(OUT, exist_ok=True)
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale),
                         resample=Image.NEAREST)
    path = os.path.join(OUT, f"{name}.png")
    img.save(path)
    return path



def pump(win, rounds=25):
    for _ in range(rounds):
        win.update_idletasks()
        win.update()
        time.sleep(0.01)


def virtual_origin():
    import ctypes
    user32 = ctypes.windll.user32
    # SM_X/YVIRTUALSCREEN = 76/77, SM_CX/CYVIRTUALSCREEN = 78/79. (Off by one
    # here silently reads the *widths* as the origin and the whole measurement
    # describes pixels that are not where the widgets are.)
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def grab():
    """Screen capture whose (0,0) is guaranteed to be the virtual origin.

    Handing PIL the explicit box removes any guesswork about what
    ``all_screens=True`` decides to include on multi-monitor rigs, and every
    widget coordinate below is derived from that same origin.
    """
    ox, oy, cw, ch = virtual_origin()
    return ImageGrab.grab(bbox=(ox, oy, ox + cw, oy + ch), all_screens=True), ox, oy


def norm(widget, spec):
    """Resolve a CTk/tk color spec ('#hex', 'gray86', (light, dark)) to rgb."""
    if isinstance(spec, (tuple, list)):
        dark = ctk.get_appearance_mode().lower().startswith("dark")
        spec = spec[1] if dark and len(spec) > 1 else spec[0]
    try:
        r, g, b = widget.winfo_rgb(spec)
        return (r >> 8, g >> 8, b >> 8)
    except Exception:
        return None


def _close(a, b):
    return a is not None and b is not None and all(
        abs(x - y) <= TOL for x, y in zip(a, b))


def _hex(c):
    return '-' if c is None else '#' + '%02x%02x%02x' % c


def card_of(widget):
    """The CTkFrame that draws a widget's rounded corners.

    A CTkScrollableFrame is a plain tk.Frame nested in a canvas inside a
    CTkFrame, so its box sits border_spacing in from the visible card and its
    own geometry says nothing about the corners.
    """
    return getattr(widget, "_parent_frame", None) or widget


def _reference_color(widget):
    """Color CustomTkinter paints this widget's corner notches with.

    Older/newer CTk builds keep it in ``_bg_color``; some widget classes never
    set that attribute but do paint the corners from the internal canvas's own
    ``bg``. Sampling a nearby pixel as a fallback is wrong whenever a sibling
    covers that spot, so both real sources are tried before giving up.
    """
    cand = getattr(widget, "_bg_color", None)
    if cand is not None:
        return norm(widget, cand)
    for child in widget.winfo_children():
        try:
            if child.winfo_class() != "Canvas":
                continue
            got = norm(child, child.cget("bg"))
        except Exception:
            continue
        if got is not None:
            return got
    return None


def corner_probe(img, ox, oy, name, widget, before=None, save=True):
    """Report where ink first appears on the corner diagonals of `widget`.

    A corner counts as curved when the pixels along it stop matching the
    widget's parent color at roughly r*0.29 px — the point where a 45° line
    from the box corner meets the rounded arc. Corners clipped by the window
    edge or painted over by a sibling are reported but excluded from the
    verdict, because the pixels there describe something else.

    `before` is the widget's image-space position measured *before* the screen
    capture; a page transition that is still sliding moves the widget after the
    pixels were written, and comparing the two catches that instead of
    reporting someone else's corner pixels as a square card.

    Returns (verdict, detail) so a sweep can collect results; the caller prints.
    """
    # A scrollable frame is a plain tk.Frame inside a canvas inside the rounded
    # CTkFrame that actually draws the corners, so its own box sits border_spacing
    # in from the visible card. Measure the card.
    widget = card_of(widget)
    try:
        x0 = widget.winfo_rootx() - ox
        y0 = widget.winfo_rooty() - oy
        w, h = widget.winfo_width(), widget.winfo_height()
        radius = int(widget.cget("corner_radius"))
        bw = int(widget.cget("border_width"))
    except Exception as exc:
        return "SKIP", f"probe failed: {exc}"

    if before is not None and (abs(before[0] - x0) > 1 or abs(before[1] - y0) > 1):
        return "SKIP", (f"moved ({x0 - before[0]}, {y0 - before[1]})px between "
                        "capture and read")

    if not widget.winfo_ismapped() or min(w, h) < 30:
        return "SKIP", (f"not visible (mapped={widget.winfo_ismapped()} "
                        f"{w}x{h})")

    ref = _reference_color(widget)
    fill = norm(widget, widget.cget("fg_color"))
    border = norm(widget, widget.cget("border_color")) if bw else None
    if ref is None:  # not a CTk widget: sample just outside the box
        if not (3 <= x0 < img.size[0] and 3 <= y0 < img.size[1]):
            return "SKIP", f"outside the captured screen at {x0},{y0}"
        ref = img.getpixel((x0 - 3, y0 - 3))[:3]

    toplevel = widget.winfo_toplevel()
    x1, y1 = x0 + w - 1, y0 + h - 1
    maxk = radius + 8
    parts, measured = [], []

    for label, (cx, cy) in (("TL", (x0, y0)), ("BR", (x1, y1))):
        step = 1 if label == "TL" else -1
        owner = None
        try:
            owner = toplevel.winfo_containing(cx + ox, cy + oy)
        except Exception:
            pass
        # CTk paints frames on an internal canvas that does not claim the very
        # last pixel row/column, so the corner point can resolve to the
        # toplevel even though the drawn arc is right there. Only refuse a
        # corner that is off every window, or one a sibling really covers.
        if owner is None:
            parts.append(f"{label} off-window - n/a")
            continue
        if owner is not toplevel and not _is_or_descendant(widget, owner):
            parts.append(f"{label} {type(owner).__name__} on top - n/a")
            continue
        inset, what = None, ""
        for k in range(maxk + 1):
            px, py = cx + step * k, cy + step * k
            if not (0 <= px < img.size[0] and 0 <= py < img.size[1]):
                break
            col = img.getpixel((px, py))[:3]
            if _close(col, ref):
                continue
            inset = k
            what = ("border" if _close(col, border) else
                    "fill" if _close(col, fill) else "other")
            break
        if inset is None:
            # Corner triangles the same color as the fill/border: the curve is
            # there but invisible. Name the color and whatever owns that spot,
            # because this is a palette/layering problem, not a radius one.
            who = type(owner).__name__ if owner is not None else "?"
            parts.append(f"{label} no color change in {maxk}px ({_hex(ref)},"
                         f" on {who})")
            continue
        if inset == 0 and what == "other":
            # Ink right at the box corner that matches neither the border nor
            # the fill: that is not this widget - something else is painted
            # over the app's edge there (another window, a taskbar sliver).
            parts.append(f"{label} foreign paint at edge - n/a")
            continue
        measured.append(inset)
        parts.append(f"{label} ink@{inset}[{what}]")
        box = ((max(0, cx - 6), max(0, cy - 6), cx + radius + 14, y0 + radius + 14)
               if label == "TL" else
               (x1 - radius - 14, y1 - radius - 14,
                min(img.size[0], x1 + 6), min(img.size[1], y1 + 6)))
        if save and box[2] > box[0] and box[3] > box[1]:
            shot(img.crop(box), f"corner_{name}_{label}", scale=5)

    expected = round(radius * 0.29)
    if not measured:
        verdict = "NO DATA"
    elif all(abs(i - expected) <= 3 for i in measured):
        verdict = "CURVED"
    else:
        verdict = "CHECK"
    return verdict, (f"r={radius} bw={bw} ref={_hex(ref)} "
                     f"border={_hex(border)} | {' | '.join(parts)} | "
                     f"want~{expected}")



def _is_or_descendant(widget, other):
    while other is not None:
        if other is widget:
            return True
        other = getattr(other, "master", None)
    return False



# Role pairs whose two colors have to stay distinct: a bordered widget still
# draws its own outline, but with no color difference between a backdrop and the
# thing sitting on it there is nothing left for the rounding to separate, and the
# layout flattens into one slab with lines on it. (backdrop, thing on it)
ROLE_PAIRS = (
    ("bg", "sidebar"),              # rail notches
    ("bg", "surface"),              # page / card notches
    ("sidebar", "sidebar_active"),  # selected nav chip inside the rail
    ("surface", "hover"),           # field fills and hairline edges on a card
)


def check_theme_roles():
    """Static pre-check: palettes where a corner has nothing to stand out from.

    Needs no window, so it runs in CI. The pixel probe below is still the ground
    truth for what actually renders; this only names the palettes that cannot
    possibly show their rounding.
    """
    bad = []
    for name, pal in COLOR_THEMES.items():
        clashing = [f"{back}=={fg}" for back, fg in ROLE_PAIRS
                    if pal.get(back) == pal.get(fg)]
        if clashing:
            bad.append(name)
            print(f"  {name:<18} {pal.get('mode', '?'):<5} {'  '.join(clashing)}")
    print(f"{len(bad)} of {len(COLOR_THEMES)} palettes have a role collision")
    return bad


def main():
    import ui as ui_mod

    if "--roles" in sys.argv:  # no GUI: safe for headless runs
        sys.exit(1 if check_theme_roles() else 0)

    # A diagnostic must not leave fingerprints: prefs writes are disabled so
    # the run cannot overwrite the user's saved window position or theme.
    ui_mod.UniversalAudioStudio._set_pref = lambda self, k, v: None

    ctk.set_appearance_mode("dark")
    app = UniversalAudioStudio()
    # Page transitions slide; a slide that is still running when the screen is
    # captured puts the pixels somewhere the widget coordinates no longer are.
    app.nav_anim_enabled = False
    app.geometry("1180x780+80+60")
    pump(app)
    app.attributes("-topmost", True)
    app.lift()
    pump(app)
    time.sleep(0.8)
    img, ox, oy = grab()
    print(f"prefs file: {app._get_pref_path()} (writes suppressed)")
    print(f"capture {img.size[0]}x{img.size[1]} from virtual origin ({ox},{oy}); "
          f"state={app.state()} geometry={app.geometry()} "
          f"root=({app.winfo_rootx()},{app.winfo_rooty()})")
    # The window has to be fully on the grabbed surface or every measurement
    # below describes pixels that do not exist.
    if app.winfo_rootx() - ox < 4 or app.winfo_rooty() - oy < 4:
        app.geometry(f"1180x780+{max(ox, 0) + 60}+{max(oy, 0) + 60}")
        pump(app)
        time.sleep(0.4)
        img, ox, oy = grab()
        print(f"re-parked at root=({app.winfo_rootx()},{app.winfo_rooty()}) "
              f"geometry={app.geometry()}")

    def probe_all(label, verbose=True, save=True):
        """Probe every measurable surface; return the ones that are not curved."""
        if verbose:
            print(f"\n{label}  (theme={getattr(app, '_color_theme_name', '?')}, "
                  f"appearance={ctk.get_appearance_mode()})")
        # Only widgets on the raised page can be measured: a hidden page is
        # painted over, so probing one would describe the covering page.
        plan = [
            ("downloader", [("rail", app.sidebar),
                            ("page", app.tab_downloader),
                            ("url_field", app.url_entry),
                            ("status_pill", app._status_pill),
                            ("nav_button", app.nav_buttons["downloader"])]),
            ("queue", [("queue_list", getattr(app, "queue_rows", None))]),
            ("history", [("hist_panel", getattr(app, "history_scroll", None)),
                         ("hist_field", getattr(app, "history_search_entry", None))]),
        ]
        bad = []
        for page, targets in plan:
            try:
                app.show_frame(page)
            except Exception:
                pass
            pump(app, 40)
            px0, py0, _, _ = virtual_origin()
            snaps = []
            for name, widget in targets:
                if widget is None:
                    continue
                snaps.append((name, widget,
                              (card_of(widget).winfo_rootx() - px0,
                               card_of(widget).winfo_rooty() - py0)))
            img, ox, oy = grab()
            if save and page == "downloader":  # one whole-window shot to eyeball
                wx, wy = app.winfo_rootx() - ox, app.winfo_rooty() - oy
                shot(img.crop((wx, wy, wx + app.winfo_width(),
                               wy + app.winfo_height())), "window")
            if verbose:
                print(f"  -- page: {page}")
            for name, widget, before in snaps:
                verdict, detail = corner_probe(img, ox, oy, name, widget,
                                               before, save=save)
                if verbose:
                    print(f"  {name:<14} {detail} {verdict}")
                if verdict not in ("CURVED", "SKIP"):
                    bad.append(f"{name}[{page}]: {verdict} {detail}")
        return bad

    if "--all" in sys.argv:
        # Every palette, every surface: a corner can be drawn correctly and
        # still be invisible because the theme gives the notch the same color
        # as the fill, and only the full sweep catches that combination.
        total, fails = 0, []
        for name in COLOR_THEMES:
            app.apply_color_theme(name)
            pump(app, 45)
            found = probe_all(name, verbose=False, save=False)
            total += 1
            print(f"  {name:<18} {'ok' if not found else f'{len(found)} problem(s)'}")
            fails += [f"{name}: {f}" for f in found]
        print(f"\n{total} themes swept, "
              + (f"{len(fails)} problem(s):\n  " + "\n  ".join(fails)
                 if fails else "every surface curved"))
    else:
        probe_all("Startup theme")

        # The corner triangles hold inherited colors, so a stale one would show
        # up as a wrongly tinted notch the moment the palette changes.
        others = [n for n in COLOR_THEMES if n != app._color_theme_name]
        if others:
            app.apply_color_theme(others[-1])
            pump(app, 45)
            probe_all("Switched theme")

    dlg = app._show_dialog("Download failed", "Could not fetch the page.",
                           "error", detail="HTTP Error 403: Forbidden",
                           action_label="Try again", action_cb=lambda: None)
    pump(dlg)
    img, ox, oy = grab()
    x0, y0 = dlg.winfo_rootx() - ox, dlg.winfo_rooty() - oy
    path = shot(img.crop((x0 - 8, y0 - 8, x0 + 470, y0 + 300)), "dialog")
    print(f"\ndialog crop -> {path}")
    app.destroy()
    # os._exit skips interpreter shutdown, so buffered stdout would be lost
    # whenever this runs with the output piped somewhere.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()


