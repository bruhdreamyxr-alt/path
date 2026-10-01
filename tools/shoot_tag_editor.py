"""Screenshot the tag editor, to check it wears the app's palette.

Run:  python tools/shoot_tag_editor.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PIL import ImageGrab  # noqa: E402
from ui import UniversalAudioStudio  # noqa: E402
import tag_editor  # noqa: E402

OUT = os.path.join(ROOT, "_page_tageditor.png")

app = UniversalAudioStudio()
app.geometry("1100x760+40+40")
app.update()

path = os.path.join(tempfile.gettempdir(), "tunelab_probe.mp3")
with open(path, "wb") as f:
    f.write(b"")

dlg = tag_editor.TagEditorDialog(app, path)
dlg.geometry("460x600+560+80")
app.attributes("-topmost", True)
dlg.attributes("-topmost", True)
for _ in range(40):
    app.update_idletasks()
    app.update()
    time.sleep(0.02)

x, y = dlg.winfo_rootx(), dlg.winfo_rooty()
w, h = dlg.winfo_width(), dlg.winfo_height()
ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(OUT)
print(OUT)
dlg.destroy()
app.destroy()
