"""Drive the collapsed nav rail with the real cursor and prove it behaves.

Three things used to happen here, and all three were invisible to a unit test
because they live in the gap between Tk's widget geometry and what the screen
actually shows:

1. a text label clipped by the narrow rail drew its first letters *past* the
   clip, so a half-cut glyph hung beside the icon - the "sliver";
2. a tooltip could be left on screen with nothing pointing at it, because the
   repaint a hover triggers re-places the CTk label inside the row and Tk
   reports that as a <Leave> for the widget the pointer is still on - the hide
   fired, the show did not, and whatever was already up stayed up;
3. the rail slid open whenever the pointer so much as rested on the footer,
   because peek was armed by "pointer inside the rail";
4. hovering a row tinted its icon, its word and its shortcut digit as three
   loose rectangles over a row that was still the old colour. CustomTkinter
   hands a frame's new fg_color to its child labels before repainting the
   frame, so anything that aborts configure() partway - CTk 6 raises on
   border_color="transparent" - leaves the labels tinted and the chip not.
   A chip's hairline ring used to arrive in dashes for a sibling reason: a
   label that spanned the row height painted its own background over it.

So this probe moves the OS cursor (SetCursorPos) and lets the real Enter/Leave
traffic, the 450ms tooltip delay and the animation timers run, then measures
pixels. A unit test can assert that ``_sb_pointer_update`` derives hover from
``winfo_pointerxy``; only a probe like this can show that resting on an icon
with peek off leaves the rail at 50px and one bubble beside it.

Run:  python tools/probe_sidebar.py [output-dir]
      saves magnified crops there (default %TEMP%/sidebar_probe) and prints one
      PASS/FAIL line per check. Exit code is the number of failures, so it can
      gate a change to the rail. It moves the mouse; don't fight it for a second.
"""
import ctypes
import ctypes.wintypes
import os
import shutil
import sys
import tempfile
import time
from collections import Counter

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    # sys.stdout is annotated TextIO, which has no reconfigure(); the stream
    # underneath (a TextIOWrapper) does. Ask for it by name so the console can
    # still be switched to UTF-8 without the attribute read failing a checker.
    _reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

OUT = next((a for a in sys.argv[1:] if not a.startswith("-")),
           os.path.join(tempfile.gettempdir(), "sidebar_probe"))
os.makedirs(OUT, exist_ok=True)
PREFS = os.path.join(ROOT, "ui_prefs.json")
BAK = os.path.join(OUT, "ui_prefs.bak.json")

if sys.platform != "win32":
    print("SKIP  this probe steers the real cursor through user32 (Windows only)")
    sys.exit(0)

import tkinter as tk  # noqa: E402
import customtkinter as ctk  # noqa: E402
from PIL import Image, ImageGrab  # noqa: E402
import ui  # noqa: E402

T = ui.UITheme
if os.path.exists(PREFS):
    shutil.copy2(PREFS, BAK)

user32 = ctypes.windll.user32
pt = ctypes.wintypes.POINT()
user32.GetCursorPos(ctypes.byref(pt))
saved_cursor = (pt.x, pt.y)

fails = []


def check(label, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + label + ("  " + extra if extra else ""))
    if not ok:
        fails.append(label)


app = ui.UniversalAudioStudio()
app.geometry("1180x780+40+20")
app.deiconify()
app.lift()
app.attributes("-topmost", True)
app.update()


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.update_idletasks()
        app.update()
        time.sleep(0.01)


def move(x, y, seconds=1.0):
    """Put the real cursor somewhere and let the event traffic settle."""
    user32.SetCursorPos(int(x), int(y))
    pump(seconds)


def set_rail(expanded):
    app._sb_cancel_peek()
    app._sb_peeking = False
    app._sidebar_expanded = bool(expanded)
    app._sb_shown = bool(expanded)
    app._sb_set_width(T.SB_W_EXPANDED if expanded else T.SB_W_COLLAPSED)
    app._sb_set_footer_word(bool(expanded))
    pump(0.35)


def grab():
    x, y = app.winfo_rootx(), app.winfo_rooty()
    return ImageGrab.grab(bbox=(x, y, x + app.winfo_width(),
                                y + app.winfo_height()))


def popups():
    """Visible tooltip windows, plus the text each one is showing."""
    found = []
    for w in app.winfo_children():
        if isinstance(w, tk.Toplevel) and w is not app:
            if w.state() == 'normal' and w.winfo_viewable():
                txt = ''
                try:
                    txt = str(w.winfo_children()[0].cget('text'))
                except Exception:
                    pass
                found.append((w, txt))
    return found


def slivers():
    """(name, visible_px, wanted_px) for any text the clip slices in half."""
    rail = app.sidebar
    rx, rw = rail.winfo_rootx(), rail.winfo_width()
    out = []
    targets = [("brand", app.sb_brand)]
    targets += [("hdr%d" % i, h) for i, h in enumerate(app._sb_headers)]
    targets += [(n, app._sb_texts[n]) for n in app.nav_buttons]
    targets += [(n + ":hint", app._sb_hints[n]) for n in app.nav_buttons]
    targets += [("footer", app.sb_toggle_lbl)]
    for name, w in targets:
        wx, req = w.winfo_rootx() - rx, w.winfo_reqwidth()
        clip = rw
        if w.master is not rail:
            clip = min(clip, w.master.winfo_rootx() - rx + w.master.winfo_width())
        visible = max(0, min(wx + req, clip) - wx)
        if 0 < visible < req:
            out.append((name, visible, req))
    return out


def fragments(img):
    """(row, column) where a half-drawn word pokes out beside a chip.

    Geometry can say a label is clipped and still be wrong about the pixels:
    CustomTkinter draws a label into a window of its own, and it is that window
    the rail clips. So measure the strip of rail right of every row's chip - a
    sliced word always shows up there first. Only a tall run of ink counts: the
    rail's hairline, its footer separator and its rounded corners all leave a
    couple of pixels in that strip and are meant to be there, while a glyph is
    a dozen tall.
    """
    rail = app.sidebar
    ax, ay = app.winfo_rootx(), app.winfo_rooty()
    px = img.load()
    base = px[rail.winfo_rootx() - ax + 6,
              ay + rail.winfo_height() * 3 // 4]   # plain stretch of rail
    edge = rail.winfo_rootx() + rail.winfo_width() - ax - 3
    rows = list(app.nav_buttons.items()) + [('footer', app.sb_toggle_btn)]
    bad = []
    for name, row in rows:
        x0 = row.winfo_rootx() - ax + row.winfo_width() + 1
        y0, hh = row.winfo_rooty() - ay, row.winfo_height()
        for x in range(max(0, x0), max(0, edge)):
            run = best = 0
            for y in range(y0, y0 + hh):
                if sum(abs(a - b) for a, b in zip(px[x, y], base)) > 24:
                    run += 1
                    best = max(best, run)
                else:
                    run = 0
            if best >= 6:
                bad.append((name, x))
    return bad


def _rgb(col):
    """A CTk color option (a hex string, or a light/dark pair) as a triple."""
    if isinstance(col, (list, tuple)):
        col = col[0] if ctk.get_appearance_mode().startswith('Light') else col[-1]
    s = str(col or '#000000').lstrip('#')
    if len(s) == 3:
        s = ''.join(ch * 2 for ch in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _away(a, b):
    return sum(abs(i - j) for i, j in zip(a[:3], b[:3]))


def _mode(px, x0, y0, x1, y1):
    """The colour covering most of a rectangle - its background, past the ink."""
    seen = Counter()
    for y in range(y0, y1):
        for x in range(x0, x1):
            seen[px[x, y][:3]] += 1
    return seen.most_common(1)[0][0] if seen else None


def _rail_rows():
    """(name, chip, [(tag, label)]) for every clickable row in the rail."""
    rows = [(n, app.nav_buttons[n],
             [('icon', app._sb_icons[n]), ('word', app._sb_texts[n]),
              ('hint', app._sb_hints[n])]) for n in app.nav_buttons]
    rows.append(('footer', app.sb_toggle_btn,
                 [('icon', app.sb_toggle_glyph), ('word', app.sb_toggle_lbl)]))
    return rows


def painted(img):
    """(row, part, wanted, shown) where the rail's pixels disagree with its state.

    CustomTkinter hands a frame's new colour to its child labels *before* it
    repaints the frame itself, so a configure() that dies partway - CTk 6 raises
    on border_color="transparent", and for a while the rail swallowed that -
    leaves every label in the new tint over a chip still wearing the old one.
    On screen that is three loose boxes where one row should be. The probe asks
    each widget what colour it believes it is and then looks at the screen,
    because that is the only way to notice half a repaint went missing.
    """
    rail = app.sidebar
    ax, ay = app.winfo_rootx(), app.winfo_rooty()
    px = img.load()
    rail_c = px[rail.winfo_rootx() - ax + 6,
                ay + rail.winfo_height() * 3 // 4][:3]
    edge = rail.winfo_rootx() + rail.winfo_width() - ax - 2
    bad = []
    for name, row, labels in _rail_rows():
        x0 = row.winfo_rootx() - ax
        x1 = min(x0 + row.winfo_width(), edge)
        y0 = row.winfo_rooty() - ay
        y1 = y0 + row.winfo_height()
        if x1 - x0 < 12:
            continue
        fg = str(row.cget('fg_color'))
        wanted = rail_c if fg == 'transparent' else _rgb(fg)
        boxes = [(l.winfo_rootx() - ax, l.winfo_rooty() - ay,
                  l.winfo_rootx() - ax + l.winfo_width(),
                  l.winfo_rooty() - ay + l.winfo_height())
                 for _t, l in labels]
        # The chip's own surface, nine pixels in (past its rounded corners) and
        # clear of every label: out here there is nothing but the fill, so any
        # other colour is a row painted in pieces.
        spot = None
        for y in range(y0 + 9, y1 - 9):
            for x in range(x0 + 9, x1 - 9):
                if any(b[0] - 1 <= x < b[2] + 1 and b[1] - 1 <= y < b[3] + 1
                       for b in boxes):
                    continue
                if _away(px[x, y], wanted) > 20:
                    spot = (x, y, px[x, y][:3])
                    break
            if spot:
                break
        if spot:
            bad.append((name, 'row', wanted, spot[2]))
        for tag, lbl in labels:
            if tag == 'hint' and (getattr(app, '_sb_badges', {}).get(name) or 0):
                continue          # a badge is meant to be its own accent chip
            lx0, ly0 = lbl.winfo_rootx() - ax, lbl.winfo_rooty() - ay
            lx1 = min(lx0 + lbl.winfo_width(), edge)
            if lx1 - lx0 < 8 or lbl.winfo_height() < 8:
                continue
            shown = _mode(px, lx0 + 1, ly0 + 1, lx1, ly0 + lbl.winfo_height() - 1)
            if shown is not None and _away(shown, wanted) > 20:
                bad.append((name, tag, wanted, shown))
    return bad


def ring_gaps(img):
    """(row, x, y) where the selected row's hairline ring did not get drawn.

    The ring is one pixel on the chip's edge, and a CTkLabel paints a rectangle
    of its own background over anything under it - so labels that spanned the
    row edge to edge used to bite the ring wherever they crossed, and it
    arrived on screen in dashes with its left side missing altogether.
    """
    ax, ay = app.winfo_rootx(), app.winfo_rooty()
    px = img.load()
    wide, tall = img.size
    bad = []
    for name, row, _labels in _rail_rows():
        if int(row.cget('border_width') or 0) <= 0:
            continue
        ring = _rgb(row.cget('border_color'))
        x0, y0 = row.winfo_rootx() - ax, row.winfo_rooty() - ay
        x1, y1 = x0 + row.winfo_width(), y0 + row.winfo_height()
        if x1 - x0 < 24:
            continue

        def seen(x, y, ring=ring):
            # Read a two pixel band along the nominal edge: a ring pixel drawn
            # one step inside the widget's own bounds is still the ring.
            for ny in (y - 1, y, y + 1):
                for nx in (x - 1, x, x + 1):
                    if 0 <= nx < wide and 0 <= ny < tall and \
                            _away(px[nx, ny], ring) <= 24:
                        return True
            return False

        for y in (y0, y1 - 1):                          # horizontal edges
            for x in range(x0 + 10, x1 - 10):
                if not seen(x, y):
                    bad.append((name, x, y))
        for x in (x0, x1 - 1):                          # vertical edges
            for y in range(y0 + 10, y1 - 10):
                if not seen(x, y):
                    bad.append((name, x, y))
    return bad


# ---- 1. text is either shown whole or wiped whole ------------------------
move(app.winfo_rootx() + 700, app.winfo_rooty() + 400, 0.8)   # park the cursor
set_rail(False)
check("collapsed rail leaves no half-drawn text", not slivers(), str(slivers()))
shot = grab()
shot.save(os.path.join(OUT, "30_collapsed.png"))
shot.crop((0, 0, 240, 380)).resize((720, 1140), Image.Resampling.NEAREST).save(
    os.path.join(OUT, "31_collapsed_zoom.png"))
frag = fragments(shot)
check("nothing is drawn in the rail beside the icons", not frag, str(frag))

set_rail(True)
check("expanded rail shows every label whole", not slivers(), str(slivers()))
check("wordmark lines up with the row labels",
      app.sb_brand.winfo_rootx() == app._sb_texts['queue'].winfo_rootx(),
      f"{app.sb_brand.winfo_rootx()} vs {app._sb_texts['queue'].winfo_rootx()}")
grab().crop((0, 0, 240, 380)).resize((720, 1140), Image.Resampling.NEAREST).save(
    os.path.join(OUT, "32_expanded_zoom.png"))


# ---- 2./3. hover behaviour with the default (peek off) -------------------
app._sb_peek_pref = bool(app._prefs.get('sidebar_peek_on_hover', False))
check("peek is off in the saved prefs",
      app._prefs.get('sidebar_peek_on_hover') is False,
      str(app._prefs.get('sidebar_peek_on_hover')))

set_rail(False)
icon = app._sb_icons['history']
move(icon.winfo_rootx() + 14, icon.winfo_rooty() + 18, 1.6)
check("resting on an icon does not open the rail (peek off)",
      app.sidebar.winfo_width() == T.SB_W_COLLAPSED and not app._sb_peeking,
      f"w={app.sidebar.winfo_width()} peeking={app._sb_peeking}")
bubbles = popups()
check("exactly one hover bubble", len(bubbles) == 1, str(len(bubbles)))
if bubbles:
    tp, txt = bubbles[0]
    gap = tp.winfo_rootx() - (app.sidebar.winfo_rootx()
                              + app.sidebar.winfo_width())
    check("bubble sits just outside the rail", 0 <= gap <= 10, str(gap))
    check("bubble reads the row's name", txt.strip().startswith('History'), txt)
    row = app.nav_buttons['history']
    check("bubble is level with its row",
          abs((tp.winfo_rooty() + tp.winfo_height() / 2)
              - (row.winfo_rooty() + row.winfo_height() / 2)) <= 6,
          f"{tp.winfo_rooty()} vs {row.winfo_rooty()}")
    grab().crop((0, 120, 320, 300)).resize((960, 540), Image.Resampling.NEAREST).save(
        os.path.join(OUT, "33_hover_bubble.png"))
    # drifting across the row must not move the bubble anywhere new: it belongs
    # to the row, not to whichever of its labels the pointer is over
    move(icon.winfo_rootx() + 6, icon.winfo_rooty() + 18, 0.9)
    drifted = popups()
    check("bubble does not jump as the pointer drifts within a row",
          len(drifted) == 1 and drifted[0][0].winfo_rootx() == tp.winfo_rootx(),
          str([(x.winfo_rootx(), t) for x, t in drifted]))
    # and reading it must not dismiss it - it stays while it is under the cursor
    move(tp.winfo_rootx() + 20, tp.winfo_rooty() + 14, 0.9)
    check("the bubble survives being hovered", len(popups()) == 1,
          str(len(popups())))

move(app.winfo_rootx() + 700, icon.winfo_rooty() + 18, 0.8)
check("bubble is gone once the pointer leaves", not popups(), str(len(popups())))
check("no popup was stranded", len(app._sb_popups) <= 1,
      str(len(app._sb_popups)))

# ---- 3b. peek on: only a nav row arms it, never the footer ---------------
app._sb_peek_pref = True
set_rail(False)
foot = app.sb_toggle_btn
move(foot.winfo_rootx() + 14, foot.winfo_rooty() + 18, 1.4)
check("peek on: the footer does not arm it",
      app.sidebar.winfo_width() == T.SB_W_COLLAPSED,
      str(app.sidebar.winfo_width()))
move(app.sidebar.winfo_rootx() + 25,
     app.nav_buttons['performance'].winfo_rooty() + 60, 1.2)
check("peek on: the empty stretch under the list does not arm it",
      app.sidebar.winfo_width() == T.SB_W_COLLAPSED,
      str(app.sidebar.winfo_width()))
move(icon.winfo_rootx() + 14, icon.winfo_rooty() + 18, 1.4)
check("peek on: resting on an icon opens the rail",
      app.sidebar.winfo_width() == T.SB_W_EXPANDED and app._sb_peeking,
      f"w={app.sidebar.winfo_width()}")
check("peek does not change the state it is previewing",
      app._sidebar_expanded is False)
pump(0.6)
check("no bubble while peeking", not popups(), str(len(popups())))
move(app.sidebar.winfo_rootx() + 90, icon.winfo_rooty() + 18, 1.0)
check("peek stays open once the labels are under the pointer",
      app.sidebar.winfo_width() == T.SB_W_EXPANDED,
      str(app.sidebar.winfo_width()))
grab().crop((0, 0, 320, 380)).resize((960, 1140), Image.Resampling.NEAREST).save(
    os.path.join(OUT, "34_peek_open.png"))
move(app.winfo_rootx() + 700, icon.winfo_rooty() + 18, 1.4)
check("peek closes when the pointer leaves",
      app.sidebar.winfo_width() == T.SB_W_COLLAPSED and not app._sb_peeking,
      f"w={app.sidebar.winfo_width()} peeking={app._sb_peeking}")

# ---- 4. a repaint paints a row, never just the labels standing on it -----
# Hover, press and selection all re-tint the chip. CustomTkinter tints the
# labels first, so a repaint that stops early shows up as loose boxes of the
# new colour on a row still in the old one - which is exactly what a hover used
# to look like, and what a page switch used to leave behind on the row it left.
app._sb_peek_pref = False
set_rail(True)
row = app.nav_buttons['history']
move(app.winfo_rootx() + 700, row.winfo_rooty() + 18, 0.8)
shot = grab()
check("an idle rail paints each row whole", not painted(shot), str(painted(shot)))
move(row.winfo_rootx() + 90, row.winfo_rooty() + 18, 1.1)
shot = grab()
shot.crop((0, 0, 240, 380)).resize((720, 1140), Image.Resampling.NEAREST).save(
    os.path.join(OUT, "35_hover_whole.png"))
check("a hovered row is one chip, not a box per label",
      not painted(shot), str(painted(shot)))
# Clicking through leaves one row active, one hovered and four idle at once -
# the mixed frame a plain screenshot of a settled page never produces.
app.show_frame('history')
row2 = app.nav_buttons['settings']
move(row2.winfo_rootx() + 90, row2.winfo_rooty() + 18, 1.2)
shot = grab()
shot.crop((0, 0, 240, 380)).resize((720, 1140), Image.Resampling.NEAREST).save(
    os.path.join(OUT, "36_after_switch.png"))
check("switching pages paints every row whole",
      not painted(shot), str(painted(shot)))
check("the selected row's ring survives the labels crossing it",
      not ring_gaps(shot), str(ring_gaps(shot)[:6]))
set_rail(False)
move(app.sidebar.winfo_rootx() + 25,
     app.nav_buttons['queue'].winfo_rooty() + 18, 1.1)
shot = grab()
shot.crop((0, 0, 240, 380)).resize((720, 1140), Image.Resampling.NEAREST).save(
    os.path.join(OUT, "37_collapsed_hover.png"))
check("collapsed, a hovered icon still sits on a whole chip",
      not painted(shot), str(painted(shot)))

user32.SetCursorPos(saved_cursor[0], saved_cursor[1])
pump(0.2)
app.destroy()
if os.path.exists(BAK):
    shutil.copy2(BAK, PREFS)
    os.remove(BAK)
print()
print("FAILURES:", fails if fails else "none")
print("crops:", OUT)
sys.exit(min(1, len(fails)))

