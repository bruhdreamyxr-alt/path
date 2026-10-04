"""Colour, icon and typography primitives for the app UI.

Lifted out of ui.py verbatim (ui.py lines 114-995) so the pure parts of the
interface live apart from the widget code that uses them. Everything here is
pure: colour maths, icon geometry, the palette tables and the control-height
tokens. No Tk widget is created or touched.

ui.py imports every public name from this module, so existing call sites
(``ui.UITheme``, ``from ui import COLOR_THEMES``) are unaffected - this is a
move, not a rename. Anything importing the theme straight from here works too.
"""
import logging
import re
import sys
from typing import Any, Optional

logger = logging.getLogger("universal_audio_studio.ui")

_pil_image = None
_pil_image_draw = None


def _lazy_import_pil():
    """Import Pillow's Image/ImageDraw on first use, caching both.

    Pillow is only needed to rasterize the button and rail icons, which happens
    once at startup - but importing it unconditionally cost ~100ms on every
    launch, including the ones where the app is only being closed again. This
    matches the sounddevice/numpy/vlc pattern used for the other optional
    imports.
    """
    global _pil_image, _pil_image_draw
    if _pil_image is None and _pil_image_draw is None:
        try:
            from PIL import Image as _img
            from PIL import ImageDraw as _draw
            _pil_image = _img
            _pil_image_draw = _draw
        except Exception:
            logger.debug("Pillow is unavailable; icons fall back to text",
                         exc_info=True)
    return _pil_image, _pil_image_draw


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
    if name == 'play':
        # A solid triangle, not a text "play" character: the glyph in a UI font
        # sits on the font's baseline at the font's own weight, so it never
        # matched the drawn marks around it. Centred by its own bounding box,
        # which is what the centring check measures.
        return (('polyfill', [(7.2, 4.8), (16.8, 12), (7.2, 19.2)]),)
    if name == 'stop':
        return (('polyfill', [(7.4, 7.4), (16.6, 7.4), (16.6, 16.6),
                              (7.4, 16.6)]),)
    if name == 'close':
        return (('line', 7.4, 7.4, 16.6, 16.6),
                ('line', 16.6, 7.4, 7.4, 16.6))
    if name == 'check':
        return (('poly', [(5, 12.4), (9.7, 17.2), (19, 6.8)]),)
    if name == 'plus':
        return (('line', 12, 5.8, 12, 18.2),
                ('line', 5.8, 12, 18.2, 12))
    if name == 'refresh':
        # An arc with a gap, and an arrowhead on the end that opens it. The gap
        # is what makes it read as "again" rather than as a plain circle.
        return (('arc', 12, 12, 7.8, 30, 290),
                ('polyfill', [(19.6, 6.4), (14.9, 5.6), (17.4, 9.4)]))
    if name == 'trash':
        return (('line', 4.4, 7, 19.6, 7),
                ('poly', [(9, 7), (9, 4.6), (15, 4.6), (15, 7)]),
                ('poly', [(6.4, 7), (7.5, 20), (16.5, 20), (17.6, 7), (6.4, 7)]),
                ('line', 10.4, 10.6, 10.8, 17.2),
                ('line', 13.6, 10.6, 13.2, 17.2))
    if name == 'folder':
        # Closed outline (the first point repeated), with the tab on the left so
        # it reads as a folder and not as a plain box.
        return (('poly', [(3.2, 19.4), (3.2, 5.6), (9.4, 5.6), (11.6, 8.4),
                          (20.8, 8.4), (20.8, 19.4), (3.2, 19.4)]),)
    if name == 'search':
        return (('oval', 10.4, 10.4, 6.2),
                ('line', 15, 15, 20.2, 20.2))
    if name == 'chevron_left':
        return (('poly', [(14.4, 6.4), (8.4, 12), (14.4, 17.6)]),)
    if name == 'chevron_right':
        return (('poly', [(9.6, 6.4), (15.6, 12), (9.6, 17.6)]),)
    if name == 'chevron_up':
        return (('poly', [(6.4, 14.4), (12, 8.4), (17.6, 14.4)]),)
    if name == 'chevron_down':
        return (('poly', [(6.4, 9.6), (12, 15.6), (17.6, 9.6)]),)
    if name == 'skip':
        # A bar and a triangle, pointing right: "not this one", rather than the
        # plain X the queue-wide Cancel already uses.
        return (('polyfill', [(6.6, 6), (9.2, 6), (9.2, 18), (6.6, 18)]),
                ('polyfill', [(11.2, 6), (18.6, 12), (11.2, 18)]))
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
            elif kind == 'arc':
                cx, cy, rad, start, extent = rest
                canvas.create_arc(*box(cx, cy, rad),
                                  start=start, extent=extent,
                                  style="arc", outline=color, width=width,
                                  capstyle="round", tags="icon")
            else:
                continue
            drawn += 1
        except Exception:
            continue
    return drawn



_ICON_SS = 4


def _render_icon_image(name, color, px):
    """Rasterise drawn icon *name* to a PIL RGBA image, or None.

    Buttons cannot host a canvas the way the rail does (CustomTkinter lays its
    own label out over the button and only ``image=`` is offered), so a mark for
    a button has to arrive as a picture. Rather than a second, hand-drawn set
    of bitmaps, this walks the *same* ``_icon_shapes`` geometry the rail uses,
    so a button icon and a rail icon of the same name are the same drawing.

    Drawn four times oversize and then downsampled: Tk's canvas strokes are
    antialiased and Pillow's are not, and at 18px the difference is the whole
    icon looking cheap. Returns None when Pillow is missing, so the caller can
    fall back to a text-only label instead of losing the control.
    """
    shapes = _icon_shapes(name)
    _Image, _ImageDraw = _lazy_import_pil()
    if not shapes or _Image is None or _ImageDraw is None or px <= 0:
        return None
    side = int(px) * _ICON_SS
    k = side / float(_ICON_GRID)
    stroke = max(1, int(round(2 * k)))
    try:
        r, g, b = (int(str(color).lstrip("#")[i:i + 2], 16)
                   for i in (0, 2, 4))
    except Exception:
        return None
    ink = (r, g, b, 255)
    try:
        img = _Image.new("RGBA", (side, side), (0, 0, 0, 0))
        draw = _ImageDraw.Draw(img)

        def pt(x, y):
            return (x * k, y * k)

        def box(cx, cy, rad):
            return (cx * k - rad * k, cy * k - rad * k,
                    cx * k + rad * k, cy * k + rad * k)

        def cap(x, y):
            # Tk draws a line with round caps; Pillow has no cap style, so the
            # ends get the dot that stands in for one.
            draw.ellipse(box(x, y, stroke / 2.0 / k), fill=ink)

        for shape in shapes:
            kind, rest = shape[0], shape[1:]
            if kind == "line":
                draw.line([pt(rest[0], rest[1]), pt(rest[2], rest[3])],
                          fill=ink, width=stroke)
                cap(rest[0], rest[1])
                cap(rest[2], rest[3])
            elif kind == "poly":
                points = [pt(x, y) for x, y in rest[0]]
                draw.line(points, fill=ink, width=stroke, joint="curve")
                cap(*rest[0][0])
                cap(*rest[0][-1])
            elif kind == "polyfill":
                draw.polygon([pt(x, y) for x, y in rest[0]], fill=ink)
            elif kind == "oval":
                draw.ellipse(box(*rest), outline=ink, width=stroke)
            elif kind == "disc":
                draw.ellipse(box(*rest), fill=ink)
            elif kind == "half":
                draw.pieslice(box(*rest), 90, 270, fill=ink)
                draw.ellipse(box(*rest), outline=ink, width=stroke)
            elif kind == "arc":
                # Tk and Pillow agree here: both measure from 3 o'clock and
                # both sweep with the y axis pointing down, so the angles pass
                # through untouched. An extent that wraps past 360 is fine for
                # either - both keep going clockwise.
                draw.arc(box(rest[0], rest[1], rest[2]), rest[3],
                         rest[3] + rest[4], fill=ink, width=stroke)
        return img.resize((int(px), int(px)), _Image.Resampling.LANCZOS)
    except Exception:
        logger.debug("Rendering icon %r failed", name, exc_info=True)
        return None


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
    # One vertical rhythm for stacked page rows. The Downloader page used to mix
    # 2, 4, 6 and 8 here, so the gaps between groups read as arbitrary and the
    # page looked unbalanced rather than calm. Rows now sit a whole step apart.
    RHYTHM = 8

    # Control heights, by what the control *is*. These were bare numbers at each
    # call site - 24, 28, 30, 32 and 36 all appeared - so two controls that ended
    # up side by side could land a couple of pixels off each other with nothing
    # in the code to say which of them was wrong. A name says it: the primary
    # action and the field it sits with are H_LG, a toolbar button is H_CTRL, a
    # secondary row is H_SM, a form field standing alone is H_FIELD, and a
    # glyph-only button is H_XS.
    H_XS = 24         # inline glyph buttons (the clear-X in the URL field)
    H_SM = 28         # secondary rows: previews, row actions, small entries
    H_CTRL = 30       # toolbars: the queue's start/remove/retry, clear, ...
    H_FIELD = 32      # a text field standing on its own (history search)
    H_LG = 36         # primary actions, the URL field, dialog buttons

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

# --- Custom (user-built) palette --------------------------------------------
# The 21 palettes above are curated: each one's colours were picked together so
# the hairlines stay one step apart and the semantic tones stay readable on their
# own surfaces. A custom palette cannot promise that, so this feature is built
# around two concessions to make hand-picking safe:
#
#   * the user chooses 12 colours, not 19 - every ``*_hover`` is *derived* from
#     its base role by mixing it toward ``bg``, which is what the curated
#     palettes do by hand and what keeps a hover from going lighter than its
#     button on a dark theme (where hover reads as "recede");
#   * every change is contrast-checked live against the same pairs the static
#     audit uses, so a failing combination is reported where it is made rather
#     than discovered later.
CUSTOM_THEME_NAME = 'My Theme'

# The roles a user picks. Order is the display order: structure first, then the
# text that sits on it, then the semantics that colour status.
CUSTOM_THEME_ROLES = (
    'bg', 'surface', 'sidebar',
    'text', 'sub', 'accent',
    'success', 'warning', 'danger', 'purple',
)

# Roles that are never offered, because a hand-picked palette cannot get them
# right and should not pretend to. ``sidebar_active`` is the selected-nav fill
# and ``hover`` is the one-pixel step between a card and the window behind it;
# both have to stay close to their own neighbours, which is a judgement the
# derivation can make and a colour picker cannot. Deriving them is also what
# keeps the card from growing two more rows the user would have to understand.
CUSTOM_DERIVED_ROLES = {
    # derived role -> (source role, how far to lift it)
    # sidebar_active is capped low deliberately. It is the selected-nav pill, and
    # it has to hold 'sub' at 4.5:1 while the pill is a *lighter* step than the
    # sidebar it sits on. Every preset still passes at 0.06; 0.08 and above
    # push 'sub' on sidebar_active under 4.5 on at least one seeded palette.
    'sidebar_active': ('sidebar', 0.06),
    'hover': ('surface', 0.14),
}

# base role -> the hover role derived from it.
CUSTOM_THEME_HOVERS = {
    'accent': 'accent_hover', 'success': 'success_hover',
    'warning': 'warning_hover', 'danger': 'danger_hover',
    'purple': 'purple_hover',
}

# Mirrored rather than imported because tools/audit_contrast.py parses this
# module with ast and must keep working without importing customtkinter.
CUSTOM_CONTRAST_PAIRS = (
    ('text', 'bg', 4.5), ('text', 'surface', 4.5), ('text', 'sidebar', 4.5),
    ('text', 'sidebar_active', 4.5),
    ('sub', 'bg', 4.5), ('sub', 'surface', 4.5), ('sub', 'sidebar', 4.5),
    ('sub', 'sidebar_active', 4.5),
    ('accent', 'surface', 3.0), ('accent', 'bg', 3.0), ('accent', 'sidebar', 3.0),
    ('success', 'surface', 3.0), ('warning', 'surface', 3.0),
    ('danger', 'surface', 3.0), ('purple', 'surface', 3.0),
)

# "Further from the window" is what a hover means, and which way that is depends
# on the mode: a dark card lifts toward the light text, a light card drops
# toward the dark text. The named colours are only the direction of travel - the
# hue always comes from the source role, so an accent hover still looks like its
# accent and a surface hover still looks like its surface.
CUSTOM_LIFT_TARGET = {'dark': '#ffffff', 'light': '#000000'}


def _lift(color: str, amount: float, mode: str) -> str:
    """Move *color* one step away from the window, in the sense *mode* implies."""
    return _mix(color, CUSTOM_LIFT_TARGET.get(mode, '#ffffff'), amount)

_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')


def _normalize_hex(value) -> Optional[str]:
    """Return *value* as a lowercase ``#rrggbb``, or None if it is not a colour."""
    text = str(value or '').strip()
    if not _HEX_RE.match(text):
        return None
    if len(text) == 4:  # #abc -> #aabbcc
        text = '#' + ''.join(ch * 2 for ch in text[1:])
    return text.lower()


def custom_palette(overrides: Optional[dict] = None,
                   mode: str = 'dark') -> dict:
    """Build a complete palette dict from a partial set of role overrides.

    Missing roles fall back to TuneLab Dark (dark) or TuneLab Light's own
    background family (light), so a half-configured custom theme is always
    complete and renderable rather than missing keys the role walk would then
    leave on whatever value it had before.
    """
    # A light base takes Serika Light wholesale rather than only its
    # backgrounds: swapping the surfaces alone would leave the dark theme's
    # blue accent and green success sitting on them, which is exactly the
    # unreadable pairing the contrast report exists to warn about. Starting
    # from the whole palette means the default custom light theme passes.
    base = dict(COLOR_THEMES['TuneLab Dark']
                if mode != 'light' else COLOR_THEMES['Serika Light'])
    pal = dict(base)
    # Stamped before the derivations below read it. The base's own 'mode' agrees
    # today only because the base is chosen by the same flag; setting it here
    # means _lift follows the mode the caller actually asked for.
    pal['mode'] = 'light' if mode == 'light' else 'dark'
    for role, value in (overrides or {}).items():
        if role in CUSTOM_THEME_ROLES:
            norm = _normalize_hex(value)
            if norm:
                pal[role] = norm
    # Derive hovers from the (possibly just-edited) base roles, so a custom
    # palette can never be left with a hover that belongs to a different colour.
    # Lifted, not mixed toward bg: a card is usually *lighter* than the window in
    # dark mode, so mixing toward bg made every hover recede into the background
    # and the row looked like it was being erased rather than pressed.
    for base_role, hover_role in CUSTOM_THEME_HOVERS.items():
        pal[hover_role] = _lift(pal[base_role], 0.18, pal['mode'])
    # ... and the two structural steps nobody is asked to pick.
    for derived, (source, amount) in CUSTOM_DERIVED_ROLES.items():
        pal[derived] = _lift(pal[source], amount, pal['mode'])
    return pal


def contrast_failures(pal: dict) -> list:
    """(fg, bg, needed, got) for every pair in *pal* that misses its bar."""
    bad = []
    for fg, bkey, need in CUSTOM_CONTRAST_PAIRS:
        if fg not in pal or bkey not in pal:
            continue
        got = _contrast(pal[fg], pal[bkey])
        if got < need:
            bad.append((fg, bkey, need, got))
    return bad


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

