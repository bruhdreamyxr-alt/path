"""Tag Editor Dialog - edit audio tags using mutagen."""
import logging
import os

logger = logging.getLogger("universal_audio_studio.tag_editor")

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
    def __init__(self, master, filepath, on_save=None):
        super().__init__(master)
        self.filepath = filepath
        self.on_save = on_save
        self._cover_data = None
        self._cover_mime = None

        self.title(f"Edit Tags - {os.path.basename(filepath)[:50]}")
        self.geometry("420x560")
        self.resizable(False, False)
        self.attributes('-topmost', True)
        self.grab_set()

        tags = self._read_tags()
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=16, pady=(12, 8))

        ctk.CTkLabel(main, text=f"Editing: {os.path.basename(filepath)[:50]}",
                     font=("Segoe UI", 11, "bold"), text_color="#2ecc71").pack(anchor="w", pady=(0, 10))

        self._entries = {}
        for label, key in [("Title:", "title"), ("Artist:", "artist"),
                           ("Album:", "album"), ("Genre:", "genre"), ("Year:", "year")]:
            ctk.CTkLabel(main, text=label, font=("Segoe UI", 12)).pack(anchor="w", pady=(4, 0))
            e = ctk.CTkEntry(main, width=380, height=30)
            e.pack(fill="x", pady=(0, 4))
            e.insert(0, tags.get(key, ""))
            self._entries[key] = e

        cover_frame = ctk.CTkFrame(main, fg_color="transparent")
        cover_frame.pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(cover_frame, text="Cover Art:", font=("Segoe UI", 12)).pack(anchor="w")
        ctk.CTkButton(cover_frame, text="Choose Image...", width=180, height=28,
                       command=self._choose_cover).pack(anchor="w", pady=(2, 0))
        self.cover_lbl = ctk.CTkLabel(cover_frame, text="No new image selected",
                                       font=("Segoe UI", 10), text_color="#95a5a6")
        self.cover_lbl.pack(anchor="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkButton(btn_frame, text="Save Tags", width=160, height=36,
                      fg_color="#2ecc71", hover_color="#27ae60",
                      command=self._save).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_frame, text="Cancel", width=100, height=36,
                      fg_color="#636e72", command=self.destroy).pack(side="left")

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
            self.cover_lbl.configure(text=os.path.basename(path), text_color="#2ecc71")


    def _save(self):
        if not HAS_MUTAGEN: return
        ext = os.path.splitext(self.filepath)[1].lower()
        vals = {k: self._entries[k].get().strip() for k in self._entries}
        try:
            if ext == '.mp3':
                audio = MP3(self.filepath, ID3=ID3)
                if audio.tags is None: audio.add_tags()
                t = audio.tags
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
