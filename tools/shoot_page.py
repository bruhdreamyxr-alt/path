"""Grab one screenshot of a named page, for eyeballing a layout change.

Run:  python tools/shoot_page.py performance [out.png]

Builds the real window, switches to the page, and saves a PNG of the window.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import customtkinter as ctk  # noqa: E402
from PIL import ImageGrab  # noqa: E402
from ui import UniversalAudioStudio  # noqa: E402

PAGE = next((a for a in sys.argv[1:] if not a.startswith("-")), "performance")
OUT = next((a for a in sys.argv[1:] if a.lower().endswith(".png")),
           os.path.join(os.path.dirname(HERE), f"_page_{PAGE}.png"))

app = UniversalAudioStudio()
app.geometry("1100x760+40+40")
app.update_idletasks()
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

x, y = app.winfo_rootx(), app.winfo_rooty()
w, h = app.winfo_width(), app.winfo_height()
ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(OUT)
print(OUT)
app.destroy()
