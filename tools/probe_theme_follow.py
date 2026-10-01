"""Prove the page scroll wells and dropdowns follow a palette switch.

Run:  python tools/probe_theme_follow.py

The Performance page kept its old background after a theme change because
CTkScrollableFrame paints a bare ``tkinter.Canvas`` behind its contents and
resolves that canvas's colour once, at construction - never again on a switch
between two palettes of the same light/dark mode. Option menus had the same
problem one level down, in a ``tkinter.Menu``.

This builds the real window, records those colours, switches to a palette with
a *different* surface, and fails if anything kept the old value. Exits non-zero
on any mismatch.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import customtkinter as ctk  # noqa: E402
from ui import COLOR_THEMES, UniversalAudioStudio  # noqa: E402


def _rgb(value):
    """Normalise a colour option to a '#rrggbb' string, or None."""
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    if isinstance(value, str) and value.startswith("#") and len(value) == 7:
        return value.lower()
    return None


def scroll_well(app):
    """(page name, frame, canvas, scrollbar) for every scrollable page.

    The scrollable frame hangs its canvas off an inner child, so it has to be
    found as a CTkScrollableFrame in the page's subtree - asking each direct
    child for ``_parent_canvas`` finds nothing and the whole check passes
    vacuously.

    The page list is read off the app's own ``_page_frames`` rather than written
    here. It was a literal tuple, and a page that later gained a scroll well (the
    Appearance page) was simply invisible to this check - the probe would have
    reported green while the newest well went untested.
    """
    found = []
    pages = getattr(app, "_page_frames", None) or {}
    for page, frame in sorted(pages.items()):
        if frame is None:
            continue
        for widget in app._iter_widgets(frame):
            if isinstance(widget, ctk.CTkScrollableFrame):
                found.append((page, widget, widget._parent_canvas,
                              getattr(widget, "_scrollbar", None)))
    return found


def _pick_target(app):
    """A palette with a different surface that shares the current light/dark mode."""
    start = app._color_theme_name
    target = next(
        (n for n, p in COLOR_THEMES.items()
         if p.get("surface") != COLOR_THEMES[start].get("surface")
         and p.get("mode") == COLOR_THEMES[start].get("mode")),
        None,
    )
    return start, target


def dropdowns(app):
    return [(w, w._dropdown_menu) for w in app._iter_widgets()
            if getattr(w, "_dropdown_menu", None) is not None]


def _expected_fill(app, frame):
    """What this well's own fill should be under the active palette.

    The history and queue wells are sunk panels inside the surface card and so
    carry the bg tone; the Performance well is transparent and shows the card it
    sits on. The rule is the widget's remembered role, exactly as the app uses
    it - hard-coding one colour for all three would demand the wrong thing.
    """
    role = (getattr(frame, "_theme_roles", None) or {}).get("fg_color")
    if role:
        return COLOR_THEMES[app._color_theme_name].get(role)
    return COLOR_THEMES[app._color_theme_name].get("surface")


def main() -> int:
    app = UniversalAudioStudio()
    app.withdraw()
    app.update_idletasks()

    start, target = _pick_target(app)
    if target is None:
        print("skip: no second palette shares a mode with a different surface")
        return 0

    wells = dict((name, (frame, canvas, bar))
                 for name, frame, canvas, bar in scroll_well(app))
    before = {name: _rgb(c.cget("bg")) for name, (_, c, _) in wells.items()}
    bars_before = {name: _rgb(bar.cget("button_color"))
                   for name, (_, _, bar) in wells.items() if bar is not None}
    menus_before = {id(m): _rgb(m.cget("fg_color")) for _, m in dropdowns(app)}

    app.apply_color_theme(target)
    app.update_idletasks()

    want_handle = _rgb(COLOR_THEMES[target]["sub"])
    want_panel = _rgb(COLOR_THEMES[target]["sidebar_active"])
    failures = []

    if not wells:
        failures.append("no scroll wells were found - the check would be vacuous")

    for name, (frame, canvas, bar) in wells.items():
        want = _rgb(_expected_fill(app, frame))
        got = _rgb(canvas.cget("bg"))
        if got != want:
            failures.append(f"{name}: scroll well is {got} (was {before.get(name)}), "
                            f"expected {want}")
        new_bar = _rgb(bar.cget("button_color")) if bar is not None else None
        if new_bar != want_handle:
            failures.append(f"{name}: scrollbar handle is {new_bar}, "
                            f"palette sub is {want_handle}")
        elif bars_before.get(name) == new_bar:
            failures.append(f"{name}: scrollbar handle did not follow the palette")

    for menu, drop in dropdowns(app):
        got = _rgb(drop.cget("fg_color"))
        if got != want_panel:
            failures.append(f"dropdown is {got}, expected sidebar_active {want_panel}")
        elif menus_before.get(id(drop)) == got:
            failures.append("dropdown did not follow the palette")

    if failures:
        print(f"FAIL {start} -> {target}")
        for f in failures:
            print("  -", f)
        return 1
    print(f"ok: scroll wells, scrollbars and dropdowns follow {start} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
