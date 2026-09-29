"""Live smoke test: the themed dialogs and toast motion in a real Tk window.

Run manually (needs a desktop session, no network):
    python tools\\smoke_dialogs.py

The unittest suite guards this code at source level, because building the whole
app in a test harness is not practical. This script instantiates just enough of
the app - a CTk window carrying the real methods - to prove the behavior:
a toast glides in from the right edge and the stack closes the gap, a dismissed
toast flies off and destroys itself, reduced motion skips all of it, and a
dialog renders themed, folds its traceback into a copyable detail block, and
runs its "Try again" callback.
"""
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import customtkinter as ctk

import ui

FAILS = []
clicks = []


def click(widget):
    """Synthesize a left click the way CTk routes it: on the inner canvas.

    CTkFrame/CTkLabel override bind() to attach to their internal canvas, so a
    pointer event has to be generated there for the handler to see it.
    """
    target = getattr(widget, "_canvas", widget)
    target.event_generate("<Button-1>", x=20, y=8)


def check(label, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(label)


def pump(root, ms=120, step_ms=20):
    for _ in range(max(1, ms // step_ms)):
        root.update()
        time.sleep(step_ms / 1000)
    root.update()


def texts(widget, out=None):
    out = [] if out is None else out
    for c in widget.winfo_children():
        try:
            out.append(str(c.cget("text")))
        except Exception:
            pass
        texts(c, out)
    return out


def find_button(widget, wanted, found=None):
    found = [] if found is None else found
    for c in widget.winfo_children():
        try:
            # A CTkButton reports its text twice (widget + inner canvas), so
            # only the button widgets are counted.
            if (c.__class__.__name__ == "CTkButton"
                    and str(c.cget("text")) == wanted):
                found.append(c)
        except Exception:
            pass
        find_button(c, wanted, found)
    return found


class Stub(ui.UniversalAudioStudio):
    """A real CTk window with the app's dialog/toast methods, minus the UI."""

    def __init__(self):
        # The app overrides after() to marshal worker-thread calls; give the
        # stub the few attributes that override needs before Tk starts.
        self._tk_main_thread = threading.main_thread()
        self._ui_queue = queue.Queue()
        self._ui_closed = False
        ctk.CTk.__init__(self, fg_color="#1e1e24")
        self.geometry("900x600+80+60")
        self.title("smoke")
        self._palette = dict(ui.COLOR_THEMES[list(ui.COLOR_THEMES)[0]])
        self._active_toasts = []
        self._toast_anim_id = None
        self.nav_anim_enabled = True
        self.url_entry = None
        self._last_dl_url = ""
        self._last_dl_was_video = False


TRACE = ("Traceback (most recent call last):\n"
         '  File "ui.py", line 1, in download_mp3\n'
         "    yt_dlp.main(argv)\n"
         "yt_dlp.utils.DownloadError: ERROR: HTTP Error 403: Forbidden\n"
         "(forwarded from aria2)")

root = Stub()
pump(root, 200)

print("toast motion:")
root.show_toast("First toast", "success", duration=100000)
first = root._active_toasts[-1]
check("new toast starts off the right edge", first._toast_relx > 1.0,
      f"relx={first._toast_relx:.3f}")
pump(root, 700)
check("toast glides onto its slot", abs(first._toast_relx - 0.98) < 0.01,
      f"relx={first._toast_relx:.3f}")
y_after_one = first._toast_rely

root.show_toast("Second toast", "warning", duration=100000)
second = root._active_toasts[-1]
pump(root, 700)
check("stack pushes the older toast upward", second._toast_rely < y_after_one,
      f"{second._toast_rely:.4f} < {y_after_one:.4f}")
check("newest toast still lands on 0.98", abs(second._toast_relx - 0.98) < 0.01,
      f"relx={second._toast_relx:.3f}")

# Click-to-dismiss: the stack must reflow while the toast is still animating.
before = len(root._active_toasts)
click(first)
pump(root, 60)
check("dismissed toast leaves the stack immediately",
      len(root._active_toasts) == before - 1,
      f"{before} -> {len(root._active_toasts)}")
pump(root, 400)
check("flying toast is destroyed once its motion ends",
      not first.winfo_exists())
pump(root, 700)
check("surviving toast settles at 0.98", abs(second._toast_relx - 0.98) < 0.01,
      f"relx={second._toast_relx:.3f}")

# Reduced motion: no entry offset, no fly-off.
root.nav_anim_enabled = False
root.show_toast("No motion", "info", duration=100000)
static = root._active_toasts[-1]
check("reduced motion places the toast on its slot at once",
      abs(static._toast_relx - 0.98) < 0.001, f"relx={static._toast_relx:.3f}")
pump(root, 150)
click(static)
pump(root, 80)
check("reduced motion destroys without animating", not static.winfo_exists())
pump(root, 400)
root.nav_anim_enabled = True

print("dialogs:")
long_msg = "Download failed.\n" + TRACE
noisy_detail = "\n".join(f"log line {i}: " + "x" * 60 for i in range(12))
dlg = root._show_error_dialog(
    "Download Failed", long_msg,
    detail=noisy_detail,
    action_label="Try again", action_cb=lambda: clicks.append("retry"))
pump(root, 400)
labels = texts(dlg)
check("dialog is live and non-blocking", dlg.winfo_exists())
check("long message keeps its headline only",
      any(t == "Download failed." for t in labels),
      repr([t[:28] for t in labels][:3]))
check("title is shown", any(t == "Download Failed" for t in labels))
check("detail block is truncated on screen",
      any("more lines" in t for t in labels))
found = (find_button(dlg, "OK") + find_button(dlg, "Copy details") +
         find_button(dlg, "Try again"))
btns = [str(b.cget("text")) for b in found]
check("OK / Copy details / Try again all rendered",
      btns == ["OK", "Copy details", "Try again"], str(btns))

find_button(dlg, "Copy details")[0].invoke()
pump(root, 80)
clip = dlg.clipboard_get()
check("Copy details copies everything, preview cap and traceback included",
      "log line 11" in clip and "HTTP Error 403" in clip
      and "more lines" not in clip, f"{len(clip)} chars")
check("caller detail stays in front of the folded traceback",
      clip.index("log line 0") < clip.index("Traceback (most recent"))

find_button(dlg, "Try again")[0].invoke()
pump(root, 150)
check("Try again closes the dialog and fires its callback",
      clicks == ["retry"] and not dlg.winfo_exists())

dlg2 = root._show_info_dialog("Performance", "Simple one-liner.")
pump(root, 350)
check("info dialog renders without a detail block",
      dlg2.winfo_exists() and
      not any("Copy details" in t for t in texts(dlg2)))
dlg2.event_generate("<Escape>")
pump(root, 150)
check("Escape closes the dialog", not dlg2.winfo_exists())

dlg3 = root._show_error_dialog("Bare", "")
pump(root, 300)
check("empty message still produces a readable dialog",
      dlg3.winfo_exists() and bool(texts(dlg3)))
dlg3.destroy()
pump(root, 100)

root.destroy()
print("\n" + ("ALL CHECKS PASSED" if not FAILS
              else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)

