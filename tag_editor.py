"""Tag Editor Dialog - edit audio tags using mutagen."""
import logging
import os

logger = logging.getLogger("universal_audio_studio.tag_editor")

from typing import Any

# Optional-dependency names are declared up front (typed Any) so a failed
# import below doesn't make every use "possibly unbound" for type checkers.
# Runtime behaviour is unchanged: mutagen uses stay behind HAS_MUTAGEN, and
# a missing tkinter/customtkinter still fails at class definition time.
ctk: Any = None
filedialog: Any = None
messagebox: Any = None
MP3: Any = None
ID3: Any = None
TIT2: Any = None
TPE1: Any = None
TALB: Any = None
TCON: Any = None
TDRC: Any = None
APIC: Any = None
FLAC: Any = None
Picture: Any = None

try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TDRC, APIC
    from mutagen.flac import FLAC, Picture
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
except ImportError:
    pass


class TagEditorDialog(ctk.CTkToplevel):
    """The one place tags are edited, dressed in the app's own palette.

    This used to be its own little app: hard-coded #2ecc71, a fixed Segoe UI
    stack and CustomTkinter's default greys, so opening it dropped the user out
    of the theme they had chosen. Everything below is taken from the master's
    palette, and the window is a plain card with the same radius and hairline as
    every dialog in the app.
    """

    def __init__(self, master, filepath, on_save=None):
        super().__init__(master)
        self.filepath = filepath
        self.on_save = on_save
        self._cover_data = None
        self._cover_mime = None

        try:
            from ui import UITheme, _on_color
        except Exception:                       # pragma: no cover - defensive
            UITheme = _on_color = None
        self._theme = UITheme

        pal = dict(getattr(master, "_palette", None) or {})
        self._pal = pal
        bg = pal.get("bg", "#1e1e24")
        surface = pal.get("surface", "#2b2b2b")
        text = pal.get("text", "#ecf0f1")
        sub = pal.get("sub", "#95a5a6")
        accent = pal.get("accent", "#3498db")
        accent_hover = pal.get("accent_hover", "#2980b9")
        self._accent, self._text = accent, text
        radius = getattr(UITheme, "RADIUS_LG", 14) if UITheme else 14
        radius_md = getattr(UITheme, "RADIUS_MD", 10) if UITheme else 10
        field_h = getattr(UITheme, "H_FIELD", 32) if UITheme else 32
        border = getattr(UITheme, "BORDER_W", 1) if UITheme else 1
        font = UITheme.F(12) if UITheme else ("Segoe UI", 12)

        self.title(f"Edit Tags - {os.path.basename(filepath)[:50]}")
        self.geometry("440x580")
        self.resizable(False, False)
        self.attributes('-topmost', True)
        self.grab_set()
        try:
            self.configure(fg_color=bg)
        except Exception:
            pass

        tags = self._read_tags()
        main = ctk.CTkFrame(self, fg_color=surface, corner_radius=radius,
                           border_width=border,
                           border_color=pal.get("hover", "#34495e"))
        main.pack(fill="both", expand=True, padx=18, pady=18)

        ctk.CTkLabel(main, text=f"Editing: {os.path.basename(filepath)[:50]}",
                     font=UITheme.F(12, "bold") if UITheme else None,
                     text_color=accent).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(main, text="These are the tags stored inside the file.",
                     font=UITheme.F(10) if UITheme else None,
                     text_color=sub).pack(anchor="w", pady=(0, 10))

        self._entries = {}
        for label, key in [("Title:", "title"), ("Artist:", "artist"),
                           ("Album:", "album"), ("Genre:", "genre"),
                           ("Year:", "year")]:
            ctk.CTkLabel(main, text=label, font=font,
                         text_color=text).pack(anchor="w", pady=(4, 0))
            e = ctk.CTkEntry(main, width=380, height=field_h,
                             corner_radius=radius_md,
                             fg_color=bg, border_width=border,
                             border_color=pal.get("hover", "#34495e"),
                             text_color=text)
            e.pack(fill="x", pady=(0, 4))
            e.insert(0, tags.get(key, ""))
            self._entries[key] = e

        cover_frame = ctk.CTkFrame(main, fg_color="transparent")
        cover_frame.pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(cover_frame, text="Cover Art:", font=font,
                     text_color=text).pack(anchor="w")
        btn_cover = ctk.CTkButton(cover_frame, text="Choose Image...",
                                  width=180, height=30, corner_radius=radius_md,
                                  fg_color=pal.get("sidebar_active", "#2c3e50"),
                                  hover_color=pal.get("hover", "#34495e"),
                                  text_color=_on_color(
                                      pal.get("sidebar_active", "#2c3e50"),
                                      text, bg) if _on_color else text,
                                  command=self._choose_cover)
        btn_cover.pack(anchor="w", pady=(4, 0))
        self.cover_lbl = ctk.CTkLabel(cover_frame, text="No new image selected",
                                      font=UITheme.F(10) if UITheme else None,
                                      text_color=sub)
        self.cover_lbl.pack(anchor="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=18, pady=(0, 18))
        save = ctk.CTkButton(btn_frame, text="Save Tags", width=170, height=36,
                             corner_radius=radius_md, fg_color=accent,
                             hover_color=accent_hover,
                             text_color=_on_color(accent, text, bg)
                             if _on_color else "#ffffff",
                             command=self._save)
        save.pack(side="left", padx=(0, 8))
        cancel = ctk.CTkButton(btn_frame, text="Cancel", width=110, height=36,
                               corner_radius=radius_md,
                               fg_color=pal.get("sidebar_active", "#2c3e50"),
                               hover_color=pal.get("hover", "#34495e"),
                               text_color=text, command=self.destroy)
        cancel.pack(side="left")

    def _read_tags(self):
        result = {"title": "", "artist": "", "album": "", "genre": "", "year": ""}
        if not HAS_MUTAGEN or not os.path.exists(self.filepath):
            return result
        ext = os.path.splitext(self.filepath)[1].lower()
        try:
            if ext == '.mp3':
                audio = MP3(self.filepath)
                t = audio.tags
                if t:
                    for k, tag_id in [('title', 'TIT2'), ('artist', 'TPE1'),
                                      ('album', 'TALB'), ('genre', 'TCON'), ('year', 'TDRC')]:
                        result[k] = str(t.get(tag_id, ''))
            elif ext == '.flac':
                audio = FLAC(self.filepath)
                for k in ('title', 'artist', 'album', 'genre', 'date'):
                    v = audio.get(k, [''])
                    result['year' if k == 'date' else k] = v[0] if v else ''
            else:
                from mutagen import File as MF
                audio = MF(self.filepath, easy=True)
                if audio and audio.tags:
                    for k in ('title', 'artist', 'album', 'genre', 'date'):
                        v = audio.tags.get(k, [''])
                        if v: result['year' if k == 'date' else k] = v[0]
        except Exception as e:
            logger.warning("TagEditor read: %s", e)
        return result

    def _choose_cover(self):
        path = filedialog.askopenfilename(title="Choose Cover Art",
            filetypes=[("Image", "*.jpg *.jpeg *.png *.webp")])
        if path and os.path.exists(path):
            with open(path, 'rb') as f: self._cover_data = f.read()
            self._cover_mime = 'image/png' if path.lower().endswith('.png') else 'image/jpeg'
            self.cover_lbl.configure(text=os.path.basename(path),
                                     text_color=self._accent)


    def _save(self):
        if not HAS_MUTAGEN: return
        ext = os.path.splitext(self.filepath)[1].lower()
        vals = {k: self._entries[k].get().strip() for k in self._entries}
        try:
            if ext == '.mp3':
                audio = MP3(self.filepath, ID3=ID3)
                if audio.tags is None: audio.add_tags()
                t = audio.tags
                assert t is not None  # add_tags() above guarantees tags exist now
                for k, cls in [('title', TIT2), ('artist', TPE1), ('album', TALB), ('genre', TCON), ('year', TDRC)]:
                    if vals[k]: t.add(cls(encoding=3, text=vals[k]))
                if self._cover_data:
                    t.delall('APIC')
                    t.add(APIC(encoding=3, mime=self._cover_mime, type=3, desc='Cover', data=self._cover_data))
                audio.save(v2_version=3)
            elif ext == '.flac':
                audio = FLAC(self.filepath)
                for k in ('title', 'artist', 'album', 'genre'):
                    if vals[k]: audio[k] = vals[k]
                if vals['year']: audio['date'] = vals['year']
                if self._cover_data:
                    audio.clear_pictures()
                    pic = Picture(); pic.type = 3; pic.mime = self._cover_mime; pic.desc = 'Cover'; pic.data = self._cover_data
                    audio.add_picture(pic)
                audio.save()
            else:
                from mutagen import File as MF
                audio = MF(self.filepath, easy=True)
                if audio and audio.tags is not None:
                    for k in ('title', 'artist', 'album', 'genre'):
                        if vals[k]: audio.tags[k] = vals[k]
                    if vals['year']: audio.tags['date'] = vals['year']
                    audio.save()
        except Exception as e:
            logger.warning("TagEditor save error: %s", e)
        if self.on_save: self.on_save()
        self.destroy()


def open_tag_editor(master, filepath, on_save=None):
    if not HAS_MUTAGEN:
        messagebox.showerror('Missing', 'Install mutagen: pip install mutagen')
        return None
    if not os.path.exists(filepath): return None
    return TagEditorDialog(master, filepath, on_save=on_save)
