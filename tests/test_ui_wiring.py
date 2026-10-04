"""Regression guards for ui.py's interactive wiring.

ui.py needs a live Tk window to exercise properly (see MacUiWiringTests in
test_release_pipeline.py), so these tests pin the *source-level* seams that
recent fixes depend on, plus the two helpers that were deliberately made
pure so they can be tested without Tk: the per-platform "open file" command
and the queue row formatter.

Each guard exists because the corresponding behaviour is invisible to the
rest of the suite: a dropped Enter binding, a reintroduced repaint between
map/unmap, or a full rebuild of the queue rows per progress tick would all
ship silently.
"""
import ast
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
_HAS_CTK = importlib.util.find_spec("customtkinter") is not None

# downloader is pure Python (no Tk), so the bundle-helper search can be
# exercised on a machine without a display. It is imported unconditionally so
# BundleHelperSearchTests does not silently skip in CI.
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
import downloader  # noqa: E402

# The custom-theme editor tests drive the real window, so they need ctk and ui.
# Both are optional for the rest of this file, which is source-level only: the
# suite has to stay runnable wherever Tk is unavailable (CI lint jobs). They are
# typed Any so that the None fallback below does not turn every ui.* reference
# in this file into a type error.
from typing import Any  # noqa: E402

ctk: Any
ui: Any

if _HAS_CTK:
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    import customtkinter as ctk  # type: ignore[no-redef]
    import ui  # type: ignore[no-redef]
else:  # pragma: no cover - only on a machine without Tk
    ctk = None
    ui = None

requires_ctk = unittest.skipUnless(
    _HAS_CTK, "customtkinter/Tk is unavailable in this environment")


def _source() -> str:
    return (_REPO / "ui.py").read_text(encoding="utf-8")


def _class_source() -> str:
    """The source of the app class only.

    Scoped deliberately: _QueueRowList (a helper class) also defines an
    ``__init__``, and the method regex below must keep resolving names
    against UniversalAudioStudio, not whichever class comes first.
    """
    src = _source()
    m = re.search(r"\nclass UniversalAudioStudio\(.*", src, re.DOTALL)
    return m.group(0) if m else src


def _method_source(name: str) -> str:
    """Return the source of the 4-space-indented method *name* (incl. docstring)."""
    m = re.search(
        r"\n    def %s\(.*?(?=\n    def |\n    @|\Z)" % re.escape(name),
        _class_source(),
        re.DOTALL,
    )
    assert m is not None, "ui.py has no method named %r" % name
    return m.group(0)


def _place_calls():
    """Every ``.place(...)`` call in ui.py as ``(line number, call text)``.

    Balanced by hand rather than matched with one regex: these calls span
    lines and carry nested calls, so a pattern that stops at the first ")"
    would sometimes read half the arguments and miss the size keywords.
    """
    src = _source()
    calls = []
    for m in re.finditer(r"\.place\(", src):
        i, depth = m.end(), 1
        while i < len(src) and depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        calls.append((src[:m.start()].count("\n") + 1, src[m.start():i]))
    return calls


class UrlEnterBindingTests(unittest.TestCase):
    """Enter in the URL box must start the primary action (Tier 1 #1)."""

    def test_url_entry_binds_enter(self):
        src = _source()
        self.assertIn('self.url_entry.bind("<Return>", self._on_url_enter)', src)
        self.assertIn('self.url_entry.bind("<KP_Enter>", self._on_url_enter)', src)

    def test_handler_routes_bulk_input_to_the_queue_and_single_to_download(self):
        body = _method_source("_on_url_enter")
        self.assertIn("_add_current_to_queue", body)  # multi-URL paste
        self.assertIn("download_mp3", body)           # single input
        self.assertIn('"break"', body)                # no focus jump / bell

    def test_handler_respects_an_in_flight_download(self):
        body = _method_source("_on_url_enter")
        self.assertIn('cget("state")', body)


class PasteAnywhereTests(unittest.TestCase):
    """Ctrl+V anywhere is the downloader's most common action."""

    @classmethod
    def setUpClass(cls):
        import ui
        cls.app = ui.UniversalAudioStudio()
        cls.app.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        except Exception:
            pass

    def setUp(self):
        self.app.clipboard_clear()
        self.app.clipboard_append("https://soundcloud.com/x/mary")
        self.app.url_entry.delete(0, "end")

    def test_a_paste_from_another_page_lands_in_the_url_box(self):
        app = self.app
        app.show_frame("studio")
        app.update_idletasks()
        self.assertEqual(app._paste_url_anywhere(), "break")
        self.assertEqual(app.url_entry.get(), "https://soundcloud.com/x/mary")
        # And it brings the user to the box that took it.
        self.assertEqual(app._current_page, "downloader")

    def test_a_visible_field_keeps_its_own_paste(self):
        app = self.app
        app.show_frame("downloader")
        app.update_idletasks()
        app.url_entry.insert(0, "already typed")
        app.url_entry.focus_set()
        app.update_idletasks()
        # Returning None lets the event keep bubbling to the entry's own
        # Ctrl+V, instead of the toplevel stamping over what is in there.
        self.assertIsNone(app._paste_url_anywhere())
        self.assertEqual(app.url_entry.get(), "already typed")

    def test_the_binding_is_on_the_window_so_any_page_reaches_it(self):
        src = _source()
        self.assertIn("self.bind('<Control-v>', self._paste_url_anywhere)", src)


class CloseWhileDownloadingTests(unittest.TestCase):
    """Closing mid-download must ask first (Tier 1 #2)."""

    def test_closing_checks_and_confirms(self):
        body = _method_source("_on_closing")
        self.assertIn("_downloads_in_flight", body)
        self.assertIn("askyesno", body)

    def test_guard_reads_the_queue_running_flag_and_button_state(self):
        body = _method_source("_downloads_in_flight")
        self.assertIn("is_running", body)             # queue worker
        self.assertIn('cget("state")', body)          # direct download


class PageSwitchPaintTests(unittest.TestCase):
    """No forced repaint while the transparent pages are stacked."""

    def test_forced_repaint_happens_only_after_the_unmap(self):
        body = _method_source("show_frame")
        unmap = body.index("f.place_forget()")
        repaint = body.index("self.update_idletasks()")
        self.assertLess(
            unmap, repaint,
            "show_frame forces a repaint between mapping the new page and "
            "unmapping the old ones; that paints one frame with both pages "
            "overlapping (the 'clipping through each other' flash).",
        )


class QueueRefreshTests(unittest.TestCase):
    """The queue/history tabs must not rebuild their widgets needlessly."""

    def test_progress_events_are_coalesced(self):
        body = _method_source("_on_queue_progress")
        self.assertIn("_schedule_queue_refresh", body)
        sched = _method_source("_schedule_queue_refresh")
        self.assertIn("_queue_refresh_pending", sched)
        self.assertIn("self.after(120", sched)

    def test_rebuild_updates_rows_in_place(self):
        body = _method_source("_rebuild_queue_list")
        self.assertIn("itemconfig", body)
        self.assertIn("lb.size() != len(labels)", body)

    def test_history_refresh_skips_unchanged_content(self):
        body = _method_source("_refresh_history_view")
        self.assertIn("_history_view_sig", body)


    def test_empty_queue_shows_a_placeholder(self):
        body = _method_source("_rebuild_queue_list")
        self.assertIn("_queue_show_empty_placeholder", body)
        src = _source()
        self.assertIn("Queue is empty", src)


class ProgressTitleTests(unittest.TestCase):
    """Live progress should be readable from the taskbar, unfocused."""

    def test_progress_bar_updates_the_window_title(self):
        body = _method_source("update_progress_bar")
        self.assertIn("self.title(", body)

    def test_title_resets_when_the_download_finishes(self):
        body = _method_source("_finalize_download")
        self.assertIn('self.title("TuneLab")', body)


class ToastTests(unittest.TestCase):
    """Toasts stack, dismiss on click, and are capped (Tier 2 #7/#8)."""

    def test_show_toast_tracks_and_repositions_the_stack(self):
        body = _method_source("show_toast")
        self.assertIn("_active_toasts", body)
        self.assertIn("_reposition_toasts", body)
        self.assertIn("<Button-1>", body)   # click to dismiss
        self.assertIn(">= 4", body)          # cap so bursts can't cover the UI
        self.assertIn("wraplength", body)    # long messages must wrap

    def test_routine_queue_warnings_use_toasts_now(self):
        src = _source()
        self.assertNotIn('messagebox.showwarning("Queue"', src)
        self.assertIn('self.show_toast("No valid URLs found.", "warning")', src)


    def test_escape_clears_the_url_entry(self):
        src = _source()
        self.assertIn(
            'self.url_entry.bind("<Escape>", self._clear_url_entry)', src)
        body = _method_source("_clear_url_entry")
        self.assertIn("delete(0", body)
        self.assertIn('"break"', body)


class WindowGeometryTests(unittest.TestCase):
    """Window size/position persists across launches (Tier 2 #10)."""

    def test_geometry_pref_exists_and_is_restored_on_startup(self):
        src = _source()
        self.assertIn('"window_geometry": "",', _method_source("_default_prefs"))
        self.assertIn("self._restore_window_geometry()", src)

    def test_geometry_is_remembered_before_destroy(self):
        body = _method_source("_on_closing")
        self.assertIn("_remember_window_geometry", body)

    def test_offscreen_positions_are_rejected(self):
        body = _method_source("_restore_window_geometry")
        self.assertIn("winfo_screenwidth", body)
        self.assertIn("winfo_screenheight", body)


    def test_maximized_state_is_persisted(self):
        remember = _method_source("_remember_window_geometry")
        self.assertIn('"window_maximized"', remember)
        self.assertIn('"zoomed"', remember)

    def test_maximized_state_is_restored(self):
        restore = _method_source("_restore_window_geometry")
        self.assertIn('self._prefs.get("window_maximized")', restore)
        self.assertIn('self.state("zoomed")', restore)


class StartupRevealTests(unittest.TestCase):
    """The window must be on screen when mainloop starts.

    customtkinter 6.0 withdraws the window inside its own __init__ and again
    on the first mainloop() (titlebar repaint), then skips its recovery
    deiconify() whenever withdraw() ran before the window was flagged as
    shown. Without a pump of update() after deiconify(), mainloop() runs
    against a hidden window: no error, nothing visible.
    """

    def test_init_deiconifies_then_pumps_one_update(self):
        body = _method_source("__init__")
        deiconify = body.index("self.deiconify()")
        update = body.index("self.update()", deiconify)
        self.assertLess(
            deiconify, update,
            "ui.py must call update() after deiconify(); otherwise "
            "customtkinter 6.0's first mainloop() leaves the window "
            "withdrawn and the app starts hidden with no error.",
        )

    def test_init_focuses_the_url_box_after_reveal(self):
        body = _method_source("__init__")
        update = body.index("self.update()")
        focus = body.index("self.url_entry.focus_set()", update)
        self.assertLess(
            update, focus,
            "startup should put the caret in the URL box once the window is "
            "revealed, so paste + Enter works without reaching for the mouse.",
        )


class SingleInstanceTests(unittest.TestCase):
    """A second launch must focus the running window, not start a twin."""

    def test_windows_mutex_guard_exists(self):
        # The mutex logic moved to main.py when startup was split out of ui.py;
        # this is about the app having the guard, not about which file holds it.
        src = (_REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("TuneLab_SingleInstance_Mutex", src)
        self.assertIn("183", src)  # ERROR_ALREADY_EXISTS, spelled numerically
        self.assertIn("SetForegroundWindow", src)

    def test_ui_still_starts_the_app(self):
        # ui.py keeps its __main__ (the PyInstaller entry script depends on it),
        # and it must delegate rather than re-implement.
        src = _source()
        self.assertIn('if __name__ == "__main__":', src)
        self.assertIn("main.run(", src)


class ShortcutTests(unittest.TestCase):
    def test_ctrl_digit_navigation_exists(self):
        src = _source()
        self.assertIn("f'<Control-{_n}>'", src)
        for page in ("downloader", "queue", "history", "studio",
                     "settings", "performance"):
            self.assertIn(f'"{page}"', src)


class OpenFilePlatformTests(unittest.TestCase):
    """_open_file must work per platform, not just on Windows (Tier 1 #3)."""

    def setUp(self):
        if not _HAS_CTK:
            self.skipTest("customtkinter not installed")
        from ui import UniversalAudioStudio
        self.app = UniversalAudioStudio

    def test_macos_opens_and_reveals(self):
        self.assertEqual(
            self.app._open_file_command("/a/b.mp3", mode="open", platform="darwin"),
            ["open", "/a/b.mp3"],
        )
        self.assertEqual(
            self.app._open_file_command("/a/b.mp3", mode="reveal", platform="darwin"),
            ["open", "-R", "/a/b.mp3"],
        )

    def test_windows_opens_via_the_shell_builtin(self):
        cmd = self.app._open_file_command(r"C:\x\y.mp3", mode="open", platform="win32")
        self.assertIsInstance(cmd, str)
        self.assertIn('start ""', cmd)
        self.assertIn(r"C:\x\y.mp3", cmd)

    def test_windows_reveal_selects_the_file(self):
        self.assertEqual(
            self.app._open_file_command(r"C:\x\y.mp3", mode="reveal", platform="win32"),
            ["explorer", "/select,", r"C:\x\y.mp3"],
        )

    def test_windows_folders_open_in_explorer(self):
        self.assertEqual(
            self.app._open_file_command(r"C:\x", mode="open", platform="win32", is_dir=True),
            ["explorer", r"C:\x"],
        )

    def test_linux_uses_xdg_open(self):
        self.assertEqual(
            self.app._open_file_command("/a/b.mp3", mode="open", platform="linux"),
            ["xdg-open", "/a/b.mp3"],
        )

    def test_platform_defaults_to_this_machine(self):
        cmd = self.app._open_file_command("whatever")
        self.assertIsInstance(cmd, (str, list))


class QueueRowLabelTests(unittest.TestCase):
    """The queue row formatter is pure, so pin its rendering exactly."""

    def setUp(self):
        if not _HAS_CTK:
            self.skipTest("customtkinter not installed")
        from ui import UniversalAudioStudio
        self.app = UniversalAudioStudio

    def test_active_row_shows_progress(self):
        url = "https://example.com/very/long/path/that/is/definitely/cut"
        item = SimpleNamespace(status="active", url=url)
        item._progress = 0.45
        self.assertEqual(
            self.app._queue_row_label(0, item, 0),
            "▶ [ACTIVE] 45% " + url[:45],
        )

    def test_active_row_without_progress_omits_the_percentage(self):
        url = "https://example.com/x"
        item = SimpleNamespace(status="active", url=url)
        self.assertEqual(
            self.app._queue_row_label(0, item, 0),
            "▶ [ACTIVE] " + url[:45],
        )

    def test_other_rows_show_padded_status_and_longer_urls(self):
        url = "https://example.com/a-very-long-url-that-gets-truncated-here"
        item = SimpleNamespace(status="pending", url=url)
        self.assertEqual(
            self.app._queue_row_label(3, item, 0),
            "○ [  PENDING] " + url[:50],
        )

    def test_unknown_status_gets_the_placeholder_icon(self):
        item = SimpleNamespace(status="weird", url="u")
        self.assertTrue(self.app._queue_row_label(0, item, 9).startswith("○ "))

    def test_a_row_names_the_track_once_it_is_known(self):
        item = SimpleNamespace(status="done", url="https://soundcloud.com/x/mary")
        item.title = "Mary"
        label = self.app._queue_row_label(0, item, -1)
        self.assertIn("Mary", label)
        self.assertNotIn("soundcloud.com", label)

    def test_a_pending_row_still_shows_the_url_it_was_given(self):
        # There is no title before the download, so the address is the only
        # honest thing to show - not an empty row.
        item = SimpleNamespace(status="pending", url="https://example.com/x")
        item.title = None
        self.assertIn("https://example.com/x",
                      self.app._queue_row_label(0, item, -1))

    def test_queue_status_marks_are_plain_geometry(self):
        """A status column of colour emoji reads as a row of mismatched stickers.

        An emoji brings its own palette, its own size and its own baseline, and
        no two share a stroke weight - so the marks a queue row shows have to
        come from one allowlist of geometric characters that a UI font draws the
        same way every time, rather than from whatever the status is.
        """
        allowed = {"○", "▶", "✓", "✗", "⊘"}
        for status in ("pending", "active", "done", "failed",
                       "skipped", "cancelled", "weird"):
            item = SimpleNamespace(status=status, url="u")
            mark = self.app._queue_row_label(0, item, 9)[0]
            self.assertIn(mark, allowed,
                          f"{status} uses {mark!r} (U+{ord(mark):04X})")


class TypeTokenTests(unittest.TestCase):
    """Fonts flow through UITheme.F — family/scale change in one place."""

    def test_no_hardcoded_font_tuples_remain(self):
        self.assertNotIn(
            '("Segoe UI"', _source(),
            "bypasses the platform-aware UITheme.F helper")

    def test_font_helper_returns_a_platform_family_tuple(self):
        if not _HAS_CTK:
            self.skipTest("customtkinter not installed")
        from ui import UITheme, _UI_FONT_FAMILY
        self.assertEqual(UITheme.F(12), (_UI_FONT_FAMILY, 12))
        self.assertEqual(UITheme.F(12, "bold"), (_UI_FONT_FAMILY, 12, "bold"))


class AnnotationTests(unittest.TestCase):
    """A promise the checker rejects is a bug waiting in that code path.

    ui.py is built out of optional strings and mixed colour recipes; typing
    them honestly (``Optional[str]``, ``dict[str, Any]``) is what keeps the
    Problems panel quiet enough that a real error - like a ``border_color``
    CustomTkinter refuses - stands out instead of drowning in noise.
    """

    # `detail: str = None` says "required string" and then hands over None.
    REQUIRED_BUT_NONE = re.compile(
        r": (?:str|int|bool|float|dict|list|tuple) = None\b")

    def test_no_parameter_promises_a_type_it_defaults_to_None(self):
        src = _source()
        for line_no, line in enumerate(src.splitlines(), 1):
            self.assertIsNone(
                self.REQUIRED_BUT_NONE.search(line),
                "ui.py:%d %s defaults to None, so it is not required - "
                "annotate it Optional[...]" % (line_no, line.strip()))
        typing_imports = [l for l in src.splitlines()
                          if l.startswith("from typing import")]
        self.assertTrue(typing_imports, "ui.py imports nothing from typing")
        self.assertIn("Optional", " ".join(typing_imports),
                      "Optional[...] is used but not imported")


class ButtonPaletteTests(unittest.TestCase):
    """Build sites pick button colors from the palette, not hex literals.

    Hardcoded fills were the root of the rainbow: seven competing hues on
    one screen, and any widget whose hex missed LEGACY_HEX_ROLES simply
    ignored theme switches. _style_button roles replace all of it.
    """

    BUILD_METHODS = (
        "build_downloader_view", "build_queue_view", "build_history_view",
        "build_studio_view", "build_customization_view",
        "build_performance_view", "_add_history_entry",
    )

    def test_no_hardcoded_fill_or_hover_hexes_at_build_sites(self):
        for name in self.BUILD_METHODS:
            body = _method_source(name)
            for pat in ('fg_color="#', 'hover_color="#'):
                self.assertNotIn(
                    pat, body,
                    "%s hardcodes %s — use _style_button(btn, role) so "
                    "build-time colors follow the active palette" % (name, pat))

    def test_style_button_tags_roles_for_later_theme_switches(self):
        body = _method_source("_style_button")
        self.assertIn("_theme_roles", body)
        self.assertIn("self._BTN_ROLES", body)
        src = _source()
        for role in ('"primary":', '"secondary":', '"danger":'):
            self.assertIn(role, src)

    def test_styles_toasts_from_the_palette(self):
        body = _method_source("show_toast")
        self.assertIn("pal.get(role", body)
        self.assertNotIn("colors = {", body)

    def test_the_button_recipe_admits_its_int_values(self):
        # corner_radius/border_width are ints, so a dict[str, str] recipe both
        # misleads a reader and is reported by the checker on every write.
        self.assertIn("colors: dict[str, Any]",
                      _method_source("_style_button"))
        self.assertIn("RADIUS_MD", _method_source("_style_button"))


class PageTitleTests(unittest.TestCase):
    """The header owns each page title; pages must not repeat it."""

    def test_no_in_page_h1_titles(self):
        src = _source()
        for title in ("Media Downloader", "Download Queue",
                      "Download History", "Appearance Settings"):
            self.assertNotIn(
                'text="%s"' % title, src,
                "duplicate H1 — the header title_lbl already shows %r" % title)

    def test_performance_scroll_does_not_repeat_the_header_title(self):
        self.assertNotIn('label_text="Performance Settings"', _source())

    def test_no_second_status_copy_in_the_header(self):
        # The Downloader pill is the single home of download status; a
        # mirrored "Finished!" in the header corner was duplicate noise.
        src = _source()
        self.assertNotIn("header_status_lbl", src)
        body = _method_source("update_dl_status")
        self.assertIn("dl_status", body)
        self.assertIn("_status_dot", body)


class MergedThemeControlTests(unittest.TestCase):
    """One dropdown owns the theme; the palette owns Light/Dark."""

    def test_the_redundant_mode_dropdown_is_gone(self):
        src = _source()
        self.assertNotIn("mode_option", src)
        self.assertNotIn("Theme Mode:", src)   # the old label, colon and all
        self.assertNotIn("change_appearance_mode", src)

    def test_palette_mode_syncs_ctk_appearance(self):
        body = _method_source("apply_color_theme")
        self.assertIn("set_appearance_mode", body)
        self.assertIn("'light'", body)


class QueueRowThemingTests(unittest.TestCase):
    """The queue list is themed CTk rows, not a raw tk.Listbox."""

    @staticmethod
    def _ui_source_everywhere() -> str:
        """The UI's own source, across the modules it now lives in.

        The list widget moved to ui_widgets.py, so a grep of ui.py alone would
        report "no _QueueRowList, no Listbox API" and pass/fail for the wrong
        reason. These guards are about what the app uses, not which file it is
        written in.
        """
        return "\n".join(
            (_REPO / name).read_text(encoding="utf-8")
            for name in ("ui.py", "ui_widgets.py", "ui_theme.py")
        )

    def test_no_live_listbox_construction(self):
        src = self._ui_source_everywhere()
        self.assertNotIn("tk.Listbox(", src)
        self.assertIn("class _QueueRowList", src)

    def test_the_app_still_uses_the_standalone_class(self):
        # Not just "the class exists somewhere": ui.py must be the module that
        # instantiates it, or the split silently left a dead class behind.
        import ui
        from ui_widgets import _QueueRowList
        self.assertIs(ui._QueueRowList, _QueueRowList)

    def test_rows_speak_the_listbox_api_the_refresh_logic_pins(self):
        src = self._ui_source_everywhere()
        for api in ("def size(", "def insert(", "def delete(",
                    "def get(", "def itemconfig(", "def curselection("):
            self.assertIn(api, src)


class StatusPillTests(unittest.TestCase):
    """Download status reads as a pill (dot + text), not bare floating text."""

    def test_dl_status_is_a_pill_with_a_colored_dot(self):
        body = _method_source("build_downloader_view")
        self.assertIn("_status_pill", body)
        self.assertIn("_status_dot", body)

    def test_status_updates_recolor_the_dot(self):
        self.assertIn("_status_dot", _method_source("update_dl_status"))


class ContrastTests(unittest.TestCase):
    """WCAG AA: 'sub' and 'text' pass 4.5:1 on every surface they land on.

    'sub' text sits on four different backgrounds across the app (page
    bg, content surface, sidebar, cards) and on the queue panel
    (sidebar_active) — all four must hold, per palette.
    """

    @staticmethod
    def _lum(hexstr: str) -> float:
        c = hexstr.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))

        def lin(ch: float) -> float:
            return ch / 12.92 if ch <= 0.03928 else ((ch + 0.055) / 1.055) ** 2.4

        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    @classmethod
    def _ratio(cls, fg: str, bg: str) -> float:
        a, b = cls._lum(fg), cls._lum(bg)
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    def test_sub_text_passes_aa_on_all_backgrounds(self):
        if not _HAS_CTK:
            self.skipTest("customtkinter not installed")
        from ui import COLOR_THEMES
        for name, pal in COLOR_THEMES.items():
            for bg_key in ("bg", "surface", "sidebar", "sidebar_active"):
                r = self._ratio(pal["sub"], pal[bg_key])
                self.assertGreaterEqual(
                    r, 4.5,
                    "%s: sub on %s is %.2f:1 (needs 4.5)" % (name, bg_key, r))

    def test_text_passes_aa_on_all_backgrounds(self):
        if not _HAS_CTK:
            self.skipTest("customtkinter not installed")
        from ui import COLOR_THEMES
        for name, pal in COLOR_THEMES.items():
            for bg_key in ("bg", "surface", "sidebar", "sidebar_active"):
                r = self._ratio(pal["text"], pal[bg_key])
                self.assertGreaterEqual(
                    r, 4.5,
                    "%s: text on %s is %.2f:1 (needs 4.5)" % (name, bg_key, r))


class OnColorPickerTests(unittest.TestCase):
    """Filled buttons choose their label color by contrast, not fixed white."""

    def setUp(self):
        if not _HAS_CTK:
            self.skipTest("customtkinter not installed")
        from ui import _contrast, _on_color
        self._contrast = _contrast
        self._on_color = _on_color

    def test_dark_text_wins_on_a_light_accent(self):
        # Serika's yellow accent: dark bg text beats near-white text.
        self.assertEqual(
            self._on_color("#e2b714", "#ecf0f1", "#323437"), "#323437")

    def test_light_text_wins_on_a_dark_fill(self):
        self.assertEqual(
            self._on_color("#2b2b2b", "#ecf0f1", "#1e1e24"), "#ecf0f1")

    def test_contrast_extremes(self):
        self.assertAlmostEqual(
            self._contrast("#ffffff", "#000000"), 21.0, delta=0.1)


class ThemedDialogTests(unittest.TestCase):
    """Modal OS message boxes are replaced by themed, non-blocking dialogs.

    The system boxes were gray OS chrome that ignored the active theme,
    swallowed long yt-dlp tracebacks, and blocked the thread that raised
    them. Each guard below pins one of those regressions.
    """

    def test_the_dialog_family_exists(self):
        for name in ("_show_dialog", "_show_info_dialog", "_show_success_dialog",
                     "_show_warning_dialog", "_show_error_dialog"):
            _method_source(name)          # asserts the method is defined

    def test_dialog_is_a_themed_toplevel_and_never_blocks_a_thread(self):
        body = _method_source("_show_dialog")
        self.assertIn("ctk.CTkToplevel", body)
        self.assertIn("transient(self)", body)      # stays with the app window
        # Modality must not cost a blocked interpreter: grab_set() is deferred
        # on a timer instead of being preceded by a blocking wait, and the
        # method hands the Toplevel back to the caller immediately.
        self.assertNotIn("dlg.wait_window", body)
        self.assertNotIn("dlg.wait_visibility", body)
        self.assertIn("dlg.after(90, _grab)", body)
        self.assertEqual(body.rstrip().rsplit("\n", 1)[-1].strip(), "return dlg")

    def test_technical_detail_is_capped_on_screen_but_not_when_copied(self):
        body = _method_source("_show_dialog")
        self.assertIn("Copy details", body)
        self.assertIn("clipboard_append(detail)", body)   # full text, not preview
        self.assertIn("len(lines) > 8", body)             # preview is capped
        self.assertIn('if detail:', body)                 # block only when needed

    def test_the_dialog_runs_its_text_through_the_splitter(self):
        # Headline tidying lives in ui._split_headline (unit-tested on its own);
        # the dialog must actually use it before laying anything out.
        body = _method_source("_show_dialog")
        self.assertIn("message, detail = _split_headline(message, detail)", body)
        self.assertLess(body.index("_split_headline(message, detail)"),
                        body.index("ctk.CTkToplevel"))

    def test_action_button_sits_left_of_ok(self):
        self.assertIn("action_label", _method_source("_show_error_dialog"))
        body = _method_source("_show_dialog")
        # Every button packs side="right", which lays out right-to-left: the
        # later a button is packed, the further left it lands.
        ok = body.index('text="OK"')
        copy = body.index('text="Copy details"')
        action = body.index("if action_label and action_cb:")
        self.assertLess(ok, copy)
        self.assertLess(copy, action)

    def test_action_callback_survives_being_wrapped(self):
        # "Try again" must still fire after the dialog has torn itself down.
        body = _method_source("_show_dialog")
        close = body.index("def _run_action():")
        self.assertIn("_close()", body[close:close + 120])
        self.assertIn("action_cb()", body[close:close + 200])

    def test_failure_call_sites_route_to_the_dialog(self):
        src = _source()
        self.assertIn('"Download Failed", headline,', src)
        self.assertIn('action_label="Try again", action_cb=self._retry_last_download',
                      src)

    def test_routine_modal_info_boxes_are_gone(self):
        src = _source()
        for gone in ('messagebox.showinfo("Success"',
                     'messagebox.showinfo("Complete"',
                     'messagebox.showinfo("Queue"',
                     'messagebox.showwarning("Queue"',
                     'messagebox.showerror("Download Failed"'):
            self.assertNotIn(gone, src)


class SplitHeadlineTests(unittest.TestCase):
    """``_split_headline`` keeps a dialog headline readable, without losing logs.

    Call sites used to interpolate whole yt-dlp tracebacks into the message, and
    a system message box rendered that as one clipped, uncopyable line. The
    splitter keeps line one and moves the technical tail into the detail block.
    """

    TRACE = ("Traceback (most recent call last):\n"
             '  File "ui.py", line 12, in download_mp3\n'
             "    yt_dlp.main(argv)\n"
             "yt_dlp.utils.DownloadError: ERROR: HTTP Error 403: Forbidden\n"
             "(forwarded from aria2)")

    def setUp(self):
        from ui import _split_headline
        self.split = _split_headline

    def test_a_traceback_message_keeps_only_its_first_line(self):
        head, detail = self.split("Download failed.\n" + self.TRACE)
        self.assertEqual(head, "Download failed.")
        self.assertTrue(detail.startswith("Traceback"))
        self.assertIn("HTTP Error 403", detail)

    def test_caller_detail_stays_in_front_of_the_folded_trace(self):
        head, detail = self.split("Download failed.\n" + self.TRACE,
                                  detail="aria2 exited with code 1")
        self.assertEqual(head, "Download failed.")
        self.assertTrue(detail.startswith("aria2 exited with code 1"))
        self.assertIn("Traceback (most recent call last)", detail)

    def test_friendly_multi_line_copy_is_left_alone(self):
        # Human-written lines are not technical detail: demoting them would
        # hide the message behind a block most users never open.
        friendly = "All done.\nSaved 3 files.\nInto your Music folder.\nSee you soon.\nBye."
        self.assertEqual(self.split(friendly), (friendly, ""))

    def test_short_technical_messages_stay_in_the_headline(self):
        short = "Failed.\nERROR: nope\nmore\nlines"
        self.assertEqual(len(short.splitlines()), 4)     # under the fold size
        self.assertEqual(self.split(short), (short, ""))

    def test_an_empty_message_falls_back_to_the_detail(self):
        self.assertEqual(self.split("", "Only a detail."),
                         ("Only a detail.", ""))
        self.assertEqual(self.split(None, None), ("(no message)", ""))
        self.assertEqual(self.split("   ", None), ("(no message)", ""))

    def test_non_string_input_is_tolerated(self):
        self.assertEqual(self.split(None, 404), ("404", ""))
        self.assertEqual(self.split(500), ("500", ""))


class MessageboxRetirementTests(unittest.TestCase):
    """What is left of tkinter.messagebox, and why.

    ask* confirmations stay: the themed dialog is deliberately fire-and-forget
    and cannot answer a question back to a caller. The single surviving show*
    is the "customtkinter is missing" path, which runs before any themed window
    could possibly exist.
    """

    SHOW_CALLS = re.compile(r"\bmessagebox\.(show\w*)\(")
    ASK_CALLS = re.compile(r"\bmessagebox\.(ask\w*)\(")

    def test_no_informational_or_warning_boxes_are_left(self):
        self.assertEqual(self.SHOW_CALLS.findall(_source()), ["showerror"])

    def test_confirmations_are_still_synchronous(self):
        # 7: queue remove/cancel/clear, Clear History, the cache clear, and
        # Remove Missing Files (history). The exact number is a tripwire - a
        # confirmation dialog appearing or disappearing unnoticed is how a
        # destructive action ends up with no prompt, so a new one means updating
        # this count deliberately rather than by accident.
        self.assertEqual(len(self.ASK_CALLS.findall(_source())), 7)

    def test_worker_thread_dialogs_are_marshalled(self):
        # A Toplevel built on a worker thread is a Tk threading crash, so the
        # dialog calls that live inside worker bodies go through after().
        src = _source()
        for needle in ('lambda: self._show_error_dialog("Cache Clear Error"',
                       'lambda err=e: self._show_error_dialog('):
            self.assertIn(needle, src)
            self.assertIn("self.after(0, ", src[max(0, src.index(needle) - 40):src.index(needle)])


class TechnicalDetailTests(unittest.TestCase):
    """Long failures must not be allowed to dictate the dialog's size."""

    def test_retry_method_reuses_the_last_url(self):
        body = _method_source("_retry_last_download")
        self.assertIn("_last_dl_url", body)
        self.assertIn('show_frame("downloader")', body)
        self.assertIn("Nothing to retry", body)   # empty-state, not a dialog

    def test_export_and_download_failures_keep_the_raw_log_copyable(self):
        src = _source()
        # Headline for the box, untouched error text for the detail block.
        self.assertIn("detail=cleaned_err", src)
        self.assertIn('detail=str(err)', src)


class ToastMotionTests(unittest.TestCase):
    """Toasts glide in from off-edge, the stack re-flows as they arrive, and
    a dismissed toast flies off before it is destroyed."""

    def test_the_app_owns_the_stack_loop_token(self):
        self.assertIn("self._toast_anim_id", _method_source("__init__"))

    def test_a_new_toast_starts_off_the_edge_and_is_glided_in(self):
        body = _method_source("show_toast")
        # relx 1.12 is past the right edge (resting slot is 0.98); with motion
        # off it is placed straight on its slot. (setattr, because a toast slot
        # is a per-instance extra on a CTkFrame, not a declared attribute.)
        self.assertIn('setattr(toast, "_toast_relx"', body)
        self.assertIn("1.12 if getattr(self, \"nav_anim_enabled\", True) else 0.98",
                      body)
        self.assertIn("nav_anim_enabled", body)
        self.assertIn("self._reposition_toasts()", body)

    def test_the_stack_closes_the_gap_while_the_toast_is_still_leaving(self):
        body = _method_source("show_toast")
        dismiss = body[body.index("def _dismiss("):]
        self.assertLess(dismiss.index("self._active_toasts.remove(toast)"),
                        dismiss.index("self._reposition_toasts()"))
        self.assertLess(dismiss.index("self._reposition_toasts()"),
                        dismiss.index("self._fly_off_toast(toast)"))
        # Double dismissal (click then timer) must not animate twice.
        self.assertIn("_toast_leaving", dismiss)

    def test_toast_slots_are_set_on_the_instance_not_the_class(self):
        # A toast is a plain CTkFrame, so relx/rely/leaving are per-instance
        # extras: they go on with setattr (like the _theme_roles tag) rather
        # than as direct writes a checker reports as unknown class attributes.
        body = _method_source("show_toast")
        for slot in ("_toast_relx", "_toast_rely", "_toast_leaving"):
            self.assertIn('setattr(toast, "%s"' % slot, body)
        self.assertNotIn("toast._toast_relx =", body)

    def test_fly_off_moves_right_past_the_resting_slot_then_destroys(self):
        body = _method_source("_fly_off_toast")
        self.assertIn("0.98 + 0.16", body)      # rightward, never leftward
        self.assertIn("1 - (1 - t) * (1 - t)", body)   # ease-out, like the nav
        self.assertLess(body.index("self.after(16"), body.index("toast.destroy()"))

    def test_repositioning_eases_toward_relative_slots(self):
        body = _method_source("_reposition_toasts")
        self.assertIn("0.955 - i * 0.065", body)   # relative: survives resizes
        self.assertIn("* 0.35", body)              # eased, not snapped
        self.assertIn("anchor=", body)

    def test_the_loop_restarts_itself_only_while_something_is_moving(self):
        body = _method_source("_reposition_toasts")
        self.assertIn("moving = False", body)
        self.assertIn("if moving:", body)
        self.assertLess(body.index("if moving:"),
                        body.index("self.after_cancel(pending)"))
        self.assertIn("_toast_anim_id = self.after(28, self._reposition_toasts)", body)

    def test_motion_can_be_turned_off_entirely(self):
        # Reduced-motion users must get the static placement, not a shorter
        # animation: show_toast snaps to 0.98 and skips the fly-off.
        show = _method_source("show_toast")
        self.assertIn("else 0.98", show)
        reflow = _method_source("_reposition_toasts")
        self.assertIn("animate = bool(getattr(self, \"nav_anim_enabled\", True))", reflow)

    def test_toasts_are_only_built_from_the_main_loop(self):
        # show_toast() places widgets; a worker reaches it through after().
        src = _source()
        for line in src.splitlines():
            if "_finalize_download(" in line and "def " not in line:
                self.assertIn("self.after(", line,
                              "worker calls _finalize_download directly")
        self.assertIn("self.show_toast(f\"Saved", _source())


class SidebarRailTests(unittest.TestCase):
    """The rail collapses by width alone and never fails silently.

    Every guard here pins a failure that ships invisibly. CustomTkinter's
    ``place()`` *raises* on width=/height=, so a size sent that way inside a
    try/except never applies at all: that is how the accent pill sat still and
    the brand/toggle labels kept their build-time geometry. And because the
    rail's clip is what hides the labels, any text changed at a readable width
    is the pop-in/pop-out the wipe exists to remove.
    """

    def test_no_size_is_ever_passed_to_place(self):
        bad = ["line %d: %s" % (n, " ".join(call.split()))
               for n, call in _place_calls()
               if re.search(r"\b(width|height)=", call)]
        self.assertEqual(
            bad, [],
            "CustomTkinter rejects width=/height= in place() (it raises, so "
            "inside a try/except the size silently never applies); size with "
            "configure() and only position with place(x=, y=): " + " | ".join(bad))

    def test_the_rail_resizes_through_one_clamped_helper(self):
        body = _method_source("_sb_set_width")
        self.assertIn("self.sidebar.configure(width=w)", body)
        self.assertIn("row.configure(width=inner)", body)   # chips follow the edge
        self.assertIn("self._sidebar_cur_w = w", body)      # frames read this back
        self.assertIn("max(UITheme.SB_W_COLLAPSED", body)   # never past either end
        self.assertIn("min(UITheme.SB_W_EXPANDED", body)

    def test_the_footer_word_is_rewritten_only_under_the_clip(self):
        body = _method_source("_sb_set_width")
        self.assertIn("if w <= UITheme.SB_LABEL_X + 8:", body)
        self.assertIn("self._sb_set_footer_word(self._sb_shown)", body)
        self.assertLess(body.index("_sidebar_cur_w = w"),
                        body.index("_sb_set_footer_word"),
                        "the width has to be known before the word is judged "
                        "readable or covered")

    def test_a_slide_is_one_eased_run_with_a_cancelable_token(self):
        body = _method_source("_animate_sidebar")
        self.assertIn("after_cancel", body)                 # a reversal restarts
        self.assertIn("self._sb_shown = bool(target_expanded)", body)
        self.assertIn("if (not getattr(self, 'nav_anim_enabled', True)", body)
        self.assertIn("abs(target_w - start_w) < 1.5", body)  # no-op runs end now
        # When the run lands, the pill and the word are re-seated.
        self.assertIn("self._sb_set_footer_word(self._sb_shown)", body)
        self.assertIn("self._position_nav_indicator_initial()", body)
        # Only the width moves: no font or anchor flip mid-slide.
        self.assertNotIn("configure(font=", body)
        self.assertNotIn("anchor=", body)

    def test_nothing_but_the_width_helper_touches_the_footer_word(self):
        for name in ("toggle_sidebar", "_sb_peek_open", "_sb_pointer_update"):
            self.assertNotIn(
                "_sb_set_footer_word", _method_source(name),
                "%s must let the clip decide when the word may change" % name)

    def test_the_pill_is_sized_by_configure_not_place(self):
        body = _method_source("_place_indicator")
        self.assertIn("self.nav_indicator.configure(width=3, height=", body)
        self.assertIn("self.nav_indicator.place(x=int(x), y=int(y))", body)
        # The gutter x does not depend on the rail width, which is why
        # collapsing leaves the pill exactly where it was.
        self.assertIn("return (3, int(btn.winfo_y()) + 6",
                      _method_source("_indicator_slot"))


    def test_a_clipped_row_cannot_be_hovered_through_its_chip(self):
        body = _method_source("_sb_pointer_update")
        self.assertIn("self.winfo_pointerxy()", body)        # not Enter/Leave churn
        self.assertIn("edge = rx + rw", body)                # the rail's visible edge
        self.assertIn("min(row.winfo_width(), edge - x)", body)
        self.assertIn("self._repaint_nav_rows()", body)
        # Peek is driven from the same derived state, armed by nav rows only.
        self.assertIn("self._sb_peek_track(inside and (armed or "
                      "self._sb_peeking))", body)

    def test_every_visual_state_change_goes_through_one_painter(self):
        for name in ("_build_sidebar", "_set_active_nav", "_sb_press_press",
                     "_sb_press_release", "_sb_pointer_update",
                     "apply_color_theme"):
            self.assertIn(
                "_repaint_nav_rows()", _method_source(name),
                "%s changes how the rail looks but does not use the shared "
                "painter, so it drifts on the next theme switch" % name)
        # The painter reads the live palette - that is what makes a theme
        # switch repaint the rail instead of leaving built widgets behind.
        self.assertIn("_palette", _method_source("_repaint_nav_rows"))

    def test_the_badge_slot_caps_and_clears(self):
        body = _method_source("_repaint_nav_hint")
        self.assertIn('str(count if count < 100 else "99+")', body)
        self.assertIn('text=self._SB_KEYS.get(name, "")', body)   # back to the digit
        self.assertIn("fg_color=accent", body)
        self.assertIn("_on_color(accent, text_c, base)", body)   # readable ink
        # The queue's remaining count is what drives the queue row's badge.
        self.assertIn("self._set_nav_badge('queue'",
                      _method_source("_update_queue_status"))

    def test_a_badge_with_a_missing_colour_still_paints(self):
        """_on_color() takes str: a None fill would raise, not degrade.

        The badge slot is the one painter a caller can reach with nothing but
        a name (_set_nav_badge), and a palette key can come back blank, so
        every slot has to be settled before it is used.
        """
        body = _method_source("_repaint_nav_hint")
        for slot in ("base", "accent", "text_c", "sub_c"):
            self.assertIn(
                "%s = %s or " % (slot, slot), body,
                "%s can still reach _on_color() as None" % slot)

    def test_peek_waits_for_a_rest_and_only_moves_the_width(self):
        body = _method_source("_sb_peek_track")
        self.assertIn("self.after(220, self._sb_peek_open)", body)
        self.assertIn("'_sb_peek_pref', False", body)         # unset = never peek
        # A pointer merely passing through must not fight a slide in flight.
        self.assertIn("_sb_anim_after_id", body)
        self.assertIn("self._animate_sidebar(False)", body)
        # A second pointer sample (or a click that already expanded the rail)
        # must not restart a peek that is open or on its way open.
        opened = _method_source("_sb_peek_open")
        self.assertIn("if self._sidebar_expanded or self._sb_peeking:", opened)
        self.assertIn("self._sb_peeking = True", opened)
        self.assertIn("self._animate_sidebar(True)", opened)
        # Peeking is a preview, not a state change: no pref is written.
        self.assertNotIn("_set_pref", _method_source("_sb_peek_open"))

    def test_each_rail_preference_applies_and_persists(self):
        for handler, pref in (("_on_sb_collapsed_pref", "sidebar_collapsed"),
                              ("_on_sb_peek_pref", "sidebar_peek_on_hover"),
                              ("_on_nav_anim_pref", "nav_animation_enabled"),
                              ("_on_nav_speed", "nav_animation_speed")):
            self.assertIn("_set_pref('%s'" % pref, _method_source(handler),
                          "%s must persist %s" % (handler, pref))
        self.assertIn('f"{self.nav_anim_speed} frames"',
                      _method_source("_on_nav_speed"))
        # Turning peek off ends one that is already open, now rather than later.
        peek = _method_source("_on_sb_peek_pref")
        self.assertLess(peek.index("_sb_cancel_peek()"),
                        peek.index("_animate_sidebar(False)"))

    def test_the_card_is_built_from_prefs_read_before_the_rail(self):
        card = _method_source("build_customization_view")
        for attr in ("sb_collapsed_chk", "sb_peek_chk", "nav_anim_chk",
                     "nav_anim_speed_slider", "nav_anim_speed_lbl"):
            self.assertIn(attr, card)
        self.assertIn('"Sidebar & motion"', card)
        # Seeding those widgets needs the values already in hand, so the
        # reads have to precede the rail's own build.
        init = _method_source("__init__")
        self.assertIn('self._prefs.get("sidebar_collapsed"', init)
        self.assertIn("self._prefs.get('nav_animation_speed', 12)", init)
        self.assertIn("self._prefs.get('sidebar_peek_on_hover', False)", init)
        self.assertLess(init.index("'nav_animation_speed', 12"),
                        init.index("self._build_sidebar()"))


    def test_a_repaint_leave_may_not_cancel_the_hover_it_just_painted(self):
        # Repainting the hovered row (its icon and label change colour) re-places
        # the CTk canvas/label inside it, and Tk reports that as a <Leave> for
        # the widget the pointer is still on. Honouring it hid the bubble again:
        # the hover fired Enter, its own repaint fired Leave, and the pending
        # show died 450ms before it was due.
        hide = _method_source("_hide_sb_tooltip")
        self.assertIn("if not force and owner is not None and "
                      "self._sb_pointer_on(owner):", hide)
        self.assertIn("self._sb_tip_owner = self._sb_tip_row(widget)",
                      _method_source("_schedule_sb_tooltip"))
        # The rail's Enter is only trusted when the pointer is really there, so
        # the same churn cannot open a bubble for a row it never touched.
        self.assertIn("not self._sb_pointer_on(self._sb_tip_owner)",
                      _method_source("_schedule_sb_tooltip"))
        # Everything that tears the rail down (toggle, animation end, the
        # expanded rail) has to hide the bubble outright.
        self.assertIn("self._hide_sb_tooltip(force=True)",
                      _method_source("toggle_sidebar"))
        self.assertIn("self._hide_sb_tooltip(force=True)",
                      _method_source("_animate_sidebar"))
        # A row is measured to the rail's edge, and the bubble itself counts as
        # "still on it" - otherwise reading it would dismiss it.
        on = _method_source("_sb_pointer_on")
        self.assertIn("w = min(w, rail.winfo_rootx() + rail.winfo_width() - x)",
                      on)
        self.assertIn("for tp in getattr(self, '_sb_popups', [])", on)
        # The footer's own Leave names its anchor instead of guessing.
        self.assertIn("lambda e, a=tip_anchor: self._hide_sb_tooltip(a)",
                      _method_source("_sb_bind_clickable"))

    def test_rail_level_text_starts_past_the_collapsed_rail(self):
        # The collapsed rail is the clip. A wordmark or group header that starts
        # before SB_W_COLLAPSED is sliced by its own parent and leaves half a
        # letter poking out beside the icon column, which reads as "the text is
        # spilling out of the sidebar".
        if not _HAS_CTK:
            self.skipTest("customtkinter not installed")
        from ui import UITheme
        self.assertGreaterEqual(UITheme.SB_LABEL_X, UITheme.SB_W_COLLAPSED)
        self.assertEqual(UITheme.SB_LABEL_X,
                         UITheme.SB_PAD_X + UITheme.SB_TEXT_X)
        build = _method_source("_build_sidebar")
        # Wordmark + group headers live directly in the rail, so they use it...
        self.assertEqual(build.count("x=UITheme.SB_LABEL_X"), 2)
        # ...while a row's label is chip-local and keeps the other constant.
        self.assertIn("tx.place(x=UITheme.SB_TEXT_X, y=1)",
                      _method_source("_make_nav_row"))

    def test_peek_is_off_by_default_and_armed_by_nav_rows_only(self):
        # The complaint this default answers: a rail that throws itself open
        # every time the pointer crosses it. Opt in, and even then only an
        # actual nav row arms it - the footer chip and the empty stretch under
        # the list must not.
        self.assertIn('"sidebar_peek_on_hover": False',
                      _method_source("_default_prefs"))
        self.assertIn("self._prefs.get('sidebar_peek_on_hover', False)",
                      _method_source("__init__"))
        body = _method_source("_sb_pointer_update")
        # An x band cannot exclude the footer: its chip sits in the same column
        # as the icons, so the decision has to come from the derived hover.
        self.assertNotIn("icons_edge", body)
        self.assertIn("armed = hover is not None and hover != '__footer__'",
                      body)
        self.assertIn(
            "self._sb_peek_track(inside and (armed or self._sb_peeking))",
            body)

    def test_a_tooltip_popup_can_never_be_stranded(self):
        # A mapped borderless popup whose last reference is dropped is stranded:
        # nothing can withdraw it any more, so it sits next to the rail for the
        # rest of the session. Every path out of _show_sb_tooltip has to keep
        # hold of it, or destroy it.
        show = _method_source("_show_sb_tooltip")
        self.assertIn("self._drop_sb_popups()", show)
        popup = _method_source("_sb_popup")
        self.assertIn("for dead in getattr(self, '_sb_popups', [])", popup)
        self.assertIn("dead.destroy()", popup)
        self.assertIn("self._sb_popups = [tp]", popup)
        # The bubble may be hovered (reading it must not dismiss it), and the
        # rail stops seeing the pointer the moment the bubble takes it - so the
        # popup has to dismiss itself, or it outlives its hover over the page.
        self.assertIn("tp.bind('<Leave>', lambda e: "
                      "self._hide_sb_tooltip(force=True))", popup)
        hide = _method_source("_hide_sb_tooltip")
        self.assertIn("for tp in getattr(self, '_sb_popups', [])", hide)
        self.assertIn("tp.withdraw()", hide)
        self.assertIn("_sb_popups", _method_source("__init__"))
        # Everything that moves the rail under the bubble hides it outright.
        self.assertEqual(_source().count("_hide_sb_tooltip(force=True)"), 4)
        # One bubble per row, pinned to the rail's edge: anchoring it to
        # whichever label the pointer is over made it hop between the icon, the
        # text and the badge slot as the mouse crossed a row.
        self.assertIn("self._sb_in_rail(widget)", show)
        self.assertIn("rail.winfo_rootx() + rail.winfo_width() + 6", show)
        # Tips stay a name plus its shortcut: a sentence is a 300px box parked
        # over the page beside the icon.
        self.assertNotIn("Switch to MP3/MP4 download tools", _source())
        self.assertIn("self._sb_tips[name], ic, name=name)",
                      _method_source("_make_nav_row"))

    def test_a_row_repaint_cannot_stop_halfway_through_the_chip(self):
        # CustomTkinter 6 refuses border_color="transparent": it raises from the
        # middle of CTkFrame.configure() - after the frame has already handed
        # its new fg_color to every child label. The labels repaint, the frame
        # does not, so a hovered row became three loose boxes of the new tint
        # over a chip still wearing the old one, and the row a click just left
        # kept the chip of the state it lost (the "little boxes and black" a
        # page switch used to flicker). The rail asked for that exact border
        # color on every idle, hovered and pressed row, and a bare except
        # swallowed the raise for as long as the rail existed.
        repaint = _method_source("_repaint_nav_rows")
        self.assertIn("border_color=_ring_color(edge, fill, base)", repaint)
        self.assertNotIn("border_color=edge", repaint)
        # ...and a failed repaint has to say so, instead of vanishing.
        self.assertIn('logger.debug("nav row %r repaint failed"', repaint)
        src = _source()
        # No surface in the app may ask for a transparent border again.
        self.assertNotIn('border_color="transparent"', src)
        self.assertNotIn("border_color='transparent'", src)

    def test_the_border_color_a_surface_gets_is_one_ctk_will_accept(self):
        # _ring_color is pure on purpose: this is the value CTk parses before it
        # draws anything, so it can be pinned without a Tk window.
        from ui import _ring_color
        self.assertEqual(_ring_color("#aabbcc", "#112233", "#445566"), "#aabbcc")
        self.assertEqual(_ring_color("transparent", "#112233", "#445566"),
                         "#112233")
        self.assertEqual(_ring_color(None, "transparent", "#445566"), "#445566")
        for fill in ("#112233", "transparent"):
            for ring in (None, "transparent", ""):
                self.assertNotEqual(_ring_color(ring, fill, "#445566"),
                                    "transparent")

    def test_a_rows_labels_leave_the_ring_band_of_its_chip(self):
        # A CTkLabel paints a rectangle of its own background over whatever sits
        # under it. Labels the full height of a row therefore ate the selected
        # row's one-pixel ring wherever they crossed it, and the ring arrived on
        # screen in dashes. One pixel clear of each edge, and they still sit
        # dead centre in the row.
        build = _method_source("_make_nav_row")
        self.assertIn("tx.place(x=UITheme.SB_TEXT_X, y=1)", build)
        self.assertEqual(build.count("place(x=UITheme.SB_PAD_X, y=y)"), 1)
        # The icon cannot be "a pixel shorter" any more: it is a canvas now, so
        # it has to be small enough that its opaque square never reaches the
        # chip's corner arc (the next test).
        self.assertIn("width=UITheme.SB_ICON_BOX", build)
        self.assertIn("height=UITheme.SB_ICON_BOX", build)
        self.assertIn("ic.place(x=UITheme.SB_ICON_X, y=UITheme.SB_ICON_Y)",
                      build)

    def test_the_icon_box_cannot_reach_the_chips_rounded_corners(self):
        # A chip's corner is an arc of RADIUS_MD centred that far in from the
        # corner, so a point belongs to the chip only when it is within
        # RADIUS_MD of that centre. The icon box has to satisfy that at all four
        # corners: the canvas paints over the arc as a square, which is how the
        # selected row grew flat corners next to its own icon.
        import math
        from ui import UITheme as T
        r = T.RADIUS_MD
        w, h, box = T.SB_ICON_W, T.SB_ROW_H, T.SB_ICON_BOX
        x0, y0 = T.SB_ICON_X, T.SB_ICON_Y
        corners = ((x0, y0), (x0 + box, y0), (x0, y0 + box),
                   (x0 + box, y0 + box))
        for cx_, cy_ in corners:
            # A point is inside a rounded rect when it is inside the rect and,
            # in the corner it belongs to, within RADIUS_MD of that arc's centre.
            arc_x = r if cx_ < w - r else w - r
            arc_y = r if cy_ < h - r else h - r
            self.assertLessEqual(
                math.hypot(cx_ - arc_x, cy_ - arc_y), r,
                f"icon corner ({cx_},{cy_}) falls outside the chip's arc "
                f"centred at ({arc_x},{arc_y})")


class RailIconTests(unittest.TestCase):
    """The rail's marks are drawn geometry, not emoji out of a system font."""

    ICONS = ("download", "queue", "history", "studio", "settings",
             "performance", "note", "chevron_left", "chevron_right",
             "play", "stop", "close", "check", "plus", "refresh",
             "trash", "folder", "search")

    @staticmethod
    def _points(shape):
        """The (x, y) pairs one shape draws (extreme points for circles)."""
        kind, rest = shape[0], shape[1:]
        if kind in ("poly", "polyfill"):
            return [(p[0], p[1]) for p in rest[0]]
        if kind in ("oval", "disc", "half"):
            cx, cy, r = rest
            return [(cx - r, cy), (cx + r, cy)]
        if kind == "arc":
            cx, cy, r = rest[0], rest[1], rest[2]
            return [(cx - r, cy), (cx + r, cy)]
        return [(rest[0], rest[1]), (rest[2], rest[3])]

    def test_no_colour_emoji_are_left_anywhere_in_the_ui(self):
        # An emoji brings its own palette, its own size and its own baseline, and
        # no two of them share a stroke weight. The rail showed six of them and
        # the eye read a row of unrelated stickers.
        for glyph in ("⬇", "📋", "🕒", "🎛", "🎨", "⚡", "♪", "«"):
            self.assertNotIn(glyph, _source(), "emoji left in the UI")

    def test_not_one_astral_emoji_character_remains(self):
        """The whole file, not a hand-picked list of the glyphs we remembered.

        A check that names the emoji it expects to find can only ever catch the
        ones somebody thought of. Every colour emoji lives above the BMP
        (U+1F000 and up), so scanning for that range catches all of them -
        including the ones a future change adds without anyone noticing.
        """
        source = _source()
        # Line 1 comments and the icon docstrings discuss emoji by name; only
        # the code that reaches a label matters.
        offenders = sorted({ch for ch in source if ord(ch) >= 0x1F000})
        self.assertEqual(offenders, [],
                         f"colour emoji left in the UI: {offenders}")
        # A variation selector is how a plain-looking character is asked to
        # render as an emoji, so it is the same problem one codepoint later.
        self.assertNotIn("️", source, "emoji variation selector left in the UI")

    def test_every_nav_row_maps_to_an_icon_that_draws_something(self):
        body = _method_source("_build_sidebar")
        for name in ("download", "queue", "history", "studio", "settings",
                     "performance"):
            self.assertIn(f"('{name}',", body)
        from ui import _icon_shapes
        for name in self.ICONS:
            self.assertTrue(_icon_shapes(name), f"{name} draws nothing")

    def test_icons_stay_inside_the_grid_they_are_drawn_on(self):
        from ui import _ICON_GRID, _icon_shapes
        slack = 1 / _ICON_GRID          # the round caps overhang by half a stroke
        for name in self.ICONS:
            for shape in _icon_shapes(name):
                for x, y in self._points(shape):
                    self.assertGreaterEqual(x, 0.5 - slack, name)
                    self.assertLessEqual(x, _ICON_GRID - 0.5 + slack, name)
                    self.assertGreaterEqual(y, 0.5 - slack, name)
                    self.assertLessEqual(y, _ICON_GRID - 0.5 + slack, name)

    def test_every_icon_is_centred_on_its_grid(self):
        # An icon whose ink hangs to one side of the grid reads off-centre in a
        # 30px column, and the whole rail is aligned on that column.
        from ui import _icon_shapes
        for name in self.ICONS:
            xs = [x for shape in _icon_shapes(name)
                  for x, _y in self._points(shape)]
            self.assertTrue(xs, name)
            centre = (min(xs) + max(xs)) / 2
            self.assertLessEqual(abs(centre - 12), 1.5,
                                 f"{name} ink is centred at {centre}")

    def test_the_icons_are_painted_with_the_row_state_not_a_font(self):
        # The row repaint has to hand the icon its colour: a canvas cannot be
        # tinted by a text_color option, and the chip's own fill has to travel
        # to the canvas background at the same time or the mark sits in a box.
        repaint = _method_source("_repaint_nav_rows")
        self.assertIn("canvas.configure(bg=fill", repaint)
        self.assertIn("_paint_icon(canvas, self._sb_items[name][0], icon_c)",
                      repaint)
        self.assertIn("accent", repaint)          # a live row
        self.assertIn("sub_c", repaint)           # an idle one

    def test_the_footers_arrow_turns_with_the_rail(self):
        chevron = _method_source("_sb_chevron")
        self.assertIn("chevron_left", chevron)
        self.assertIn("chevron_right", chevron)
        # ... and it is redrawn when the word flips, otherwise a footer reading
        # "Expand" keeps pointing left.
        self.assertIn("_paint_icon(self.sb_toggle_glyph, self._sb_chevron()",
                      _method_source("_sb_set_footer_word"))


class KeyboardAndPressTests(unittest.TestCase):
    """Tab has to go somewhere visible, and a click has to look like a click."""

    def test_the_traversal_only_visits_controls_that_are_on_screen(self):
        body = _method_source("_collect_focusables")
        # Pages are stacked with place(): a control on a page that is not
        # showing is unmapped, and Tab that lands on one rings something the
        # user cannot see.
        self.assertIn("winfo_ismapped()", body)
        self.assertIn("CTkButton", body)
        self.assertIn('self.bind("<Tab>"', body)
        self.assertIn('self.bind("<Shift-Tab>"', body)
        self.assertIn("self._collect_focusables()", _source())

    def test_the_focus_lands_where_customtkinter_puts_the_bindings(self):
        # CTkButton.bind() forwards to the canvas and the labels inside, so
        # focusing the control itself leaves the key bindings behind: the ring
        # moved and Enter did nothing.
        target = _method_source("_focus_target")
        self.assertIn('"_entry"', target)
        self.assertIn('"_canvas"', target)
        self.assertIn("self._focus_target(target).focus_set()",
                      _method_source("_focus_step"))

    def test_buttons_and_checkboxes_answer_to_the_keyboard(self):
        body = _method_source("_style_button")
        self.assertIn('btn.bind("<Return>"', body)
        self.assertIn('btn.bind("<space>"', body)
        self.assertIn("b.invoke()", body)      # the same path a click takes
        self.assertIn("CTkCheckBox", _method_source("_collect_focusables"))
        self.assertIn("toggle", _method_source("_collect_focusables"))

    def test_a_held_button_shows_a_press_and_gives_its_fill_back(self):
        style = _method_source("_style_button")
        self.assertIn("self._press_in(b)", style)
        self.assertIn("self._press_out(b)", style)
        self.assertIn("_mix(fill", _method_source("_press_tint"))
        self.assertIn("fg_color=saved[1]", _method_source("_press_out"))
        # A ghost button has no fill to deepen, and must not be given one.
        self.assertIn('"transparent"', _method_source("_press_in"))

    def test_the_ring_puts_the_border_it_found_back(self):
        # The control left behind has to look exactly as the mouse left it: a
        # ghost button had no border and a filled one had the card's edge.
        ring = _method_source("_set_focus_ring")
        self.assertIn("self._restore_focus_ring()", ring)
        self.assertIn("UITheme.BORDER_W", ring)
        self.assertIn("border_width=width, border_color=color",
                      _method_source("_restore_focus_ring"))
        self.assertIn('self.bind("<Button-1>"', _method_source("_collect_focusables"))


class RailKeyboardTests(unittest.TestCase):
    """The rail is the one control that decides what the rest of the app shows."""

    @classmethod
    def setUpClass(cls):
        import ui
        cls.app = ui.UniversalAudioStudio()
        cls.app.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        except Exception:
            pass

    def test_every_rail_row_is_on_the_tab_order(self):
        app = self.app
        self.assertTrue(app.nav_buttons, "no rail rows were built")
        for name, row in app.nav_buttons.items():
            self.assertIn(row, app._focusables,
                          f"the {name} row is not reachable by Tab")

    def test_the_rail_comes_first_because_it_is_the_leftmost_column(self):
        # You cannot use a page until you can get to it, so navigation has to
        # lead rather than sit at the end of the page's controls.
        first = self.app._focusables[0]
        self.assertEqual(getattr(first, "_nav_name", None), "downloader")

    def test_a_row_answers_enter_and_space_by_switching_page(self):
        app = self.app
        row = app.nav_buttons["history"]
        # CTkFrame.bind() forwards to the frame's own canvas, so that is where
        # focus has to sit for Enter and space to reach the row at all.
        target = app._focus_target(row)
        for sequence in ("<Return>", "<space>"):
            app.show_frame("downloader")
            app.update_idletasks()
            target.focus_set()
            target.event_generate(sequence, when="now")
            app.update_idletasks()
            self.assertEqual(app._current_page, "history",
                             f"{sequence} did not switch page")

    def test_a_row_takes_focus_so_traversal_can_stop_on_it(self):
        import tkinter as tk
        # A CTkFrame is skipped by Tk's own traversal unless it is told it may
        # hold focus, and CTkFrame.configure() does not know the option.
        for name, row in self.app.nav_buttons.items():
            self.assertEqual(str(tk.Frame.cget(row, "takefocus")), "1", name)

    def test_the_ring_follows_focus_and_is_cleared_when_focus_leaves(self):
        app = self.app
        app.show_frame("downloader")
        app._nav_focus("queue")
        self.assertEqual(app._sb_focus_name, "queue")
        row = app.nav_buttons["queue"]
        # The ring goes through the rail's own repaint, because a border set
        # directly would be wiped by the next hover.
        self.assertGreaterEqual(int(row.cget("border_width")), 1)
        app._nav_blur("queue")
        self.assertIsNone(app._sb_focus_name)
        self.assertEqual(int(row.cget("border_width")), 0)

    def test_the_focus_list_is_built_after_the_window_is_shown(self):
        # Built while the window was still withdrawn, the list came out empty
        # of rail rows: they are CTkFrames and none had been laid out yet, so
        # Tab reached every button on the page and still could not change page.
        src = _source()
        init = src[src.index("self.deiconify()"):src.index("def _start_worker")]
        self.assertIn("self._collect_focusables()", init)


class TagEditorThemeTests(unittest.TestCase):
    """The tag editor is the one place tags are edited; it must look like ours."""

    @staticmethod
    def _body():
        import inspect
        import tag_editor
        return inspect.getsource(tag_editor.TagEditorDialog)

    def test_it_takes_its_colours_from_the_master_palette(self):
        body = self._body()
        # Not pal.get("accent", "#2ecc71"): a fixed fallback that is not the
        # palette's accent is exactly the old bug, only quieter.
        self.assertIn('getattr(master, "_palette"', body)
        for key in ("bg", "surface", "text", "accent"):
            self.assertIn(f'pal.get("{key}"', body)

    def test_it_reuses_the_apps_shape_and_type_tokens(self):
        body = self._body()
        self.assertIn("UITheme", body)
        self.assertNotIn('font=("Segoe UI"', body)

    def test_the_dialog_says_it_edits_the_tags_inside_the_file(self):
        # It reads as the single tag area, and says what the values are.
        body = self._body()
        self.assertIn("inside the file", body)


class CompositeWidgetThemeTests(unittest.TestCase):
    """Scroll wells and dropdowns keep colour in children the role walk misses."""

    def test_the_theme_pass_repaints_them(self):
        self.assertIn("self._style_composites(pal)",
                      _method_source("apply_color_theme"))

    def test_it_finds_them_by_type(self):
        body = _method_source("_style_composites")
        self.assertIn("CTkScrollableFrame", body)
        self.assertIn("CTkOptionMenu", body)
        self.assertIn("_parent_canvas", body)
        self.assertIn("_dropdown_menu", body)
        self.assertIn("_scrollbar", body)

    def test_a_wells_fill_is_resolved_through_its_role_not_its_old_hex(self):
        # Keeping the literal fg_color captured at build time *is* the staleness
        # being fixed; the role is what says which tone it is, and the palette is
        # what says what that tone is now.
        body = _method_source("_style_composites")
        self.assertIn('_theme_roles', body)
        self.assertIn('roles.get("fg_color")', body)


class ControlHeightScaleTests(unittest.TestCase):
    """Control heights come from a named scale, not from a spread of numbers."""

    def test_the_scale_is_ordered_and_its_names_are_used(self):
        from ui import UITheme as T
        scale = [T.H_XS, T.H_SM, T.H_CTRL, T.H_FIELD, T.H_LG]
        self.assertEqual(scale, sorted(scale))
        self.assertEqual(len(set(scale)), 5)
        src = _source()
        for token in ("H_XS", "H_SM", "H_CTRL", "H_FIELD", "H_LG"):
            self.assertIn(f"UITheme.{token}", src)

    def test_no_control_height_is_a_bare_number_again(self):
        # 24/28/30/32/36 all appeared at call sites, which is how two controls
        # that end up side by side drift a couple of pixels apart.
        src = _source()
        for bare in ("height=24", "height=28", "height=30", "height=32",
                     "height=36"):
            self.assertNotIn(bare, src, f"{bare} is a bare literal again")


class AppIconTests(unittest.TestCase):
    """The window, the .exe and the shortcuts all wear the same mark."""

    def test_the_assets_are_in_the_tree(self):
        for name in ("tune_lab.png", "tune_lab.ico"):
            path = _REPO / "assets" / name
            self.assertTrue(path.is_file(), f"{name} is missing")

    def test_the_mark_is_not_drawn_twice(self):
        # The generator reads the rail's own geometry, so the app icon and the
        # brand tile cannot drift apart the way two hand-drawn copies would.
        gen = (_REPO / "tools" / "make_app_icon.py").read_text(encoding="utf-8")
        self.assertIn("_icon_shapes", gen)
        self.assertIn('"note"', gen)
        self.assertIn("LANCZOS", gen)          # supersampled, then downsampled

    def test_the_window_takes_the_native_format_for_its_platform(self):
        body = _method_source("_apply_app_icon")
        # Windows renders an iconphoto through Tk's own conversion, which comes
        # out stretched; the .ico is what the shell understands there.
        self.assertIn('sys.platform == "win32"', body)
        self.assertIn("iconbitmap", body)
        self.assertIn("iconphoto", body)
        self.assertIn("_MEIPASS", body)        # a frozen build bundles them
        self.assertIn("self._apply_app_icon()", _source())
        # The PhotoImage has to be kept: a collected image is a blank icon.
        self.assertIn("self._app_icon = photo", body)

    def test_the_packaging_uses_the_same_asset(self):
        spec = (_REPO / "UniversalAudioStudio.spec").read_text(encoding="utf-8")
        self.assertIn("icon='assets/tune_lab.ico'", spec)
        self.assertIn("('assets/tune_lab.png', '.')", spec)
        iss = (_REPO / "installer_200.iss").read_text(encoding="utf-8")
        self.assertIn("SetupIconFile=assets\\tune_lab.ico", iss)
        self.assertIn('IconFilename: "assets\\tune_lab.ico"', iss)


@requires_ctk
class CustomPaletteTests(unittest.TestCase):
    """The user-built palette: derivation, validation and persistence.

    No window needed - custom_palette is pure.
    """

    def test_default_custom_palette_is_complete_and_dark(self):
        pal = ui.custom_palette()
        for role in ui.COLOR_THEMES['TuneLab Dark']:
            self.assertIn(role, pal, "%s missing from default custom palette" % role)
        self.assertEqual(pal['mode'], 'dark')
        self.assertEqual(pal['bg'], ui.COLOR_THEMES['TuneLab Dark']['bg'])

    def test_light_base_uses_light_backgrounds(self):
        pal = ui.custom_palette(mode='light')
        self.assertEqual(pal['mode'], 'light')
        self.assertEqual(pal['bg'], ui.COLOR_THEMES['Serika Light']['bg'])
        # A light base must not inherit the dark theme's text colour.
        self.assertNotEqual(pal['text'], ui.COLOR_THEMES['TuneLab Dark']['text'])

    def test_hover_shades_are_derived_not_inherited(self):
        """Editing a base colour must move its hover with it.

        The derived hovers are why a custom palette asks for 12 colours instead
        of 17. A hover left over from the previous accent would show a pink
        button shading toward the blue it used to be.
        """
        pal = ui.custom_palette({'accent': '#ff0055'})
        self.assertNotEqual(pal['accent_hover'],
                            ui.COLOR_THEMES['TuneLab Dark']['accent_hover'])
        self.assertNotEqual(pal['accent_hover'], pal['accent'])
        self.assertEqual(pal['accent_hover'],
                         ui._lift(pal['accent'], 0.18, pal['mode']))

    def test_every_derived_hover_follows_its_base(self):
        for base_role, hover_role in ui.CUSTOM_THEME_HOVERS.items():
            pal = ui.custom_palette({base_role: '#00ffaa'})
            self.assertEqual(pal[hover_role],
                             ui._lift(pal[base_role], 0.18, pal['mode']),
                             "%s was not derived from %s" % (hover_role, base_role))

    def test_default_custom_palette_passes_contrast(self):
        """The starting point has to be usable as-is.

        Someone who opens the card and changes nothing must not be dropped into
        a palette that already fails - the safety story rests on seeding from
        something that passes.
        """
        self.assertEqual(ui.contrast_failures(ui.custom_palette()), [])
        self.assertEqual(ui.contrast_failures(ui.custom_palette(mode='light')), [])

    def test_seeding_from_every_preset_passes_contrast(self):
        """start_custom_from must not be able to produce a failing palette."""
        for name in ui.COLOR_THEMES:
            if name == ui.CUSTOM_THEME_NAME:
                continue
            src = ui.COLOR_THEMES[name]
            seeded = ui.custom_palette(
                {r: src[r] for r in ui.CUSTOM_THEME_ROLES if r in src},
                src.get('mode') or 'dark')
            bad = ui.contrast_failures(seeded)
            self.assertEqual(bad, [], "%s seeds a palette that fails: %s" % (name, bad))

    def test_contrast_failures_names_the_pair_and_ratio(self):
        pal = ui.custom_palette({'text': '#111111', 'bg': '#222222'})
        bad = ui.contrast_failures(pal)
        self.assertTrue(bad)
        failing = {(fg, bkey) for fg, bkey, _n, _g in bad}
        self.assertIn(('text', 'bg'), failing)
        for _fg, _bkey, need, got in bad:
            self.assertLess(got, need)

    def test_contrast_check_covers_semantics_not_just_body_text(self):
        """The report must catch an accent that sinks into the surface.

        A check looking only at body text would pass a palette whose status
        colours vanish - the same failure the curated audit guards against.
        """
        pal = ui.custom_palette({'accent': '#101010'})
        failing = {(fg, bkey) for fg, bkey, _n, _g in ui.contrast_failures(pal)}
        self.assertTrue(any(fg == 'accent' for fg, _ in failing), failing)

    def test_normalize_hex_accepts_both_forms_and_rejects_junk(self):
        self.assertEqual(ui._normalize_hex('#ABCDEF'), '#abcdef')
        self.assertEqual(ui._normalize_hex('  #abc  '), '#aabbcc')
        for junk in ('', 'red', '#12345', '#gggggg', 'blue-ish', None, 12345):
            self.assertIsNone(ui._normalize_hex(junk), repr(junk))

    def test_partial_overrides_fall_back_rather_than_break(self):
        pal = ui.custom_palette({'accent': '#123456'})
        self.assertEqual(pal['accent'], '#123456')
        self.assertEqual(pal['bg'], ui.COLOR_THEMES['TuneLab Dark']['bg'])

    def test_invalid_override_is_ignored_not_stored(self):
        pal = ui.custom_palette({'accent': 'chartreuse', 'text': '#abcdef'})
        self.assertEqual(pal['accent'], ui.COLOR_THEMES['TuneLab Dark']['accent'])
        self.assertEqual(pal['text'], '#abcdef')

    def test_every_role_the_card_shows_is_editable(self):
        """A row shown in the picker must be a role custom_palette accepts.

        Guards against adding a row to the card and forgetting it in the
        editable list, which would silently drop the user's pick.
        """
        for _title, roles in ui.UniversalAudioStudio._CUSTOM_ROLE_GROUPS:
            for role in roles:
                pal = ui.custom_palette({role: '#abcdef'})
                self.assertEqual(pal[role], '#abcdef',
                                 "%s shown but not editable" % role)

    def test_the_card_shows_every_editable_role_exactly_once(self):
        """The picker is the whole surface: nothing offered, nothing missed.

        A role in CUSTOM_THEME_ROLES with no row would be unchangeable from the
        UI while still claiming to be a role, and a row twice would give the
        user two swatches that disagree.
        """
        shown = [r for _t, roles in ui.UniversalAudioStudio._CUSTOM_ROLE_GROUPS
                 for r in roles]
        self.assertEqual(sorted(shown), sorted(ui.CUSTOM_THEME_ROLES))
        self.assertEqual(len(shown), len(set(shown)), "a role is listed twice")

    def test_the_derived_roles_are_not_offered_to_the_user(self):
        """The two structural steps must stay derived, not pickable.

        ``hover`` is a hairline one step off its neighbour and
        ``sidebar_active`` is the selected-nav fill. Both are judgements about
        how close two surfaces should sit; a colour picker cannot make them, and
        offering them was how the card grew two rows nobody could get right.
        """
        shown = {r for _t, roles in ui.UniversalAudioStudio._CUSTOM_ROLE_GROUPS
                 for r in roles}
        self.assertFalse(shown & set(ui.CUSTOM_DERIVED_ROLES))

    def test_derived_roles_follow_their_source(self):
        for derived, (source, amount) in ui.CUSTOM_DERIVED_ROLES.items():
            pal = ui.custom_palette({source: '#00ffaa'})
            self.assertEqual(pal[derived],
                             ui._lift(pal[source], amount, pal['mode']),
                             "%s was not derived from %s" % (derived, source))

    def test_a_hover_lifts_away_from_the_window_in_both_modes(self):
        """Hovering a card must make it stand out, never sink into the window.

        The derivation used to mix toward ``bg``, which is only "outward" when
        the window is darker than the card. Cards are commonly *lighter* than the
        window in dark mode, and there the hover came out darker than the card it
        belonged to - the row looked erased rather than pressed. Dark mode lifts
        toward white, light mode drops toward black, and both must be verifiable
        without reading the mix direction.
        """
        for mode, sign in (('dark', 1), ('light', -1)):
            for roles in ({'bg': '#101014', 'surface': '#2a2a30'},
                          {'bg': '#333338', 'surface': '#22222a'}):
                pal = ui.custom_palette(roles, mode)
                self.assertNotEqual(pal['hover'], pal['surface'],
                                    "hover is invisible in %s" % mode)
                # sign +1 = must get lighter in dark mode, -1 = darker in light.
                moved = sign * (ui._rel_luminance(pal['hover'])
                                - ui._rel_luminance(pal['surface']))
                self.assertGreater(moved, 0,
                                   "hover did not move away from the window "
                                   "in %s mode" % mode)

    def test_every_seeded_preset_still_passes_the_contrast_report(self):
        """The lift must not trade a correct hover for an unreadable palette.

        Lifting made sidebar_active lighter, which eats into its own 'sub'
        contrast; the derived amounts are tuned against exactly this.
        """
        for name, src in ui.COLOR_THEMES.items():
            seeded = ui.custom_palette(
                {r: src[r] for r in ui.CUSTOM_THEME_ROLES if r in src},
                src.get('mode') or 'dark')
            self.assertEqual(ui.contrast_failures(seeded), [], name)

    def test_editing_a_background_role_moves_the_derived_steps(self):
        """The point of deriving them: one edit repaints all three surfaces.

        If the derivation lagged, the card border and the selected-nav fill
        would stay on the previous palette - the exact class of stale-colour
        bug the role walk was built to prevent.
        """
        pal = ui.custom_palette({'surface': '#203040'}, 'dark')
        self.assertEqual(pal['hover'],
                         ui._lift('#203040', ui.CUSTOM_DERIVED_ROLES['hover'][1],
                                  'dark'))
        pal = ui.custom_palette({'sidebar': '#301030'}, 'dark')
        self.assertEqual(
            pal['sidebar_active'],
            ui._lift('#301030', ui.CUSTOM_DERIVED_ROLES['sidebar_active'][1],
                     'dark'))

    def test_custom_palette_survives_a_round_trip_through_prefs(self):
        """Roles are saved raw; the palette is rebuilt from them on load."""
        roles = {'accent': '#ff0055', 'bg': '#101014'}
        pal = ui.custom_palette(roles, 'dark')
        rebuilt = ui.custom_palette(dict(roles), 'dark')
        self.assertEqual(pal, rebuilt)
        self.assertEqual(pal['accent_hover'], rebuilt['accent_hover'])

    def test_every_start_from_option_exists(self):
        for name in ui.UniversalAudioStudio._CUSTOM_START_THEMES:
            self.assertIn(name, ui.COLOR_THEMES, name)

    def test_start_from_options_exclude_the_custom_entry(self):
        """Copying My Theme onto itself would freeze the current picks."""
        self.assertNotIn(ui.CUSTOM_THEME_NAME,
                         ui.UniversalAudioStudio._CUSTOM_START_THEMES)


@requires_ctk
class CustomThemeEditorTests(unittest.TestCase):
    """The custom editor wired into the running window."""

    def setUp(self):
        # Redirect prefs into a temp file for the whole construction, not just
        # the writes: set_custom_role persists, and the default path is the real
        # ui_prefs.json beside ui.py when running from source. Without this the
        # suite both rewrites the user's saved settings and *reads* whatever a
        # previous run left there, so a test could pass on one machine and fail
        # on the next. Patching the path resolver is the only seam that covers
        # the load in __init__ as well as every later save.
        self._tmpdir = tempfile.TemporaryDirectory()
        temp_pref = os.path.join(self._tmpdir.name, "ui_prefs.json")
        # Bound via a default argument, not `self`: as a method the lambda's
        # `self` is the *app*, which would shadow this TestCase.
        patcher = mock.patch.object(
            ui.UniversalAudioStudio, "_get_pref_path",
            lambda _app, _p=temp_pref: _p)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = ui.UniversalAudioStudio()
        self.app.withdraw()
        self.app.update_idletasks()
        self._pref_path = self.app._pref_path

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        self._tmpdir.cleanup()

    def _rebuild_with_prefs(self, prefs):
        """Reconstruct the app against a given prefs dict.

        The load happens in __init__, so a test that wants to observe how a
        prefs file is read has to stand the window up again around it. The first
        instance is torn down first so the temp file is not left open by a
        lingering Tk.
        """
        try:
            self.app.destroy()
        except Exception:
            pass
        with open(self._pref_path, "w", encoding="utf-8") as fh:
            json.dump(prefs, fh)
        self.app = ui.UniversalAudioStudio()
        self.app.withdraw()
        self.app.update_idletasks()
        return self.app

    def test_a_derived_role_saved_by_an_older_build_is_dropped_on_load(self):
        """A prefs file may still name a role the picker no longer offers.

        ``hover`` was pickable before it became derived. Reading such a file
        must not resurrect the key: the value is ignored when the palette is
        built anyway, so keeping it would only mean the next save writes back a
        colour the UI can neither show nor change - a preference that survives
        every reset.
        """
        app = self._rebuild_with_prefs(
            {'color_theme': ui.CUSTOM_THEME_NAME,
             'custom_theme_roles': {'accent': '#ff0055', 'hover': '#8b0735'},
             'custom_theme_mode': 'dark'})
        self.assertNotIn('hover', app._custom_roles)
        self.assertEqual(app._custom_roles, {'accent': '#ff0055'})
        # And the dead key must not come back on the next write.
        app.set_custom_role('bg', '#123456')
        self.assertNotIn('hover', app._prefs['custom_theme_roles'])

    def test_a_non_dict_custom_roles_is_ignored(self):
        """A corrupted key must not take the window down with it.

        The value comes from a hand-editable JSON file, so a list where a dict
        was expected is a real possibility rather than a hypothetical.
        """
        app = self._rebuild_with_prefs(
            {'custom_theme_roles': ['#ff0000'], 'color_theme': 'TuneLab Dark'})
        self.assertEqual(app._custom_roles, {})

    def test_the_page_registry_matches_the_tab_attributes(self):
        """``_page_frames`` is what the theme-follow probe iterates.

        It has to name the same six pages ``show_frame`` can switch to, or the
        probe quietly stops covering a page. The probe itself asserts it found at
        least one scroll well, so it would stay green on a partial registry.
        """
        self.assertEqual(set(self.app._page_frames),
                         {'downloader', 'queue', 'history', 'studio',
                          'settings', 'performance'})
        for name, frame in self.app._page_frames.items():
            self.assertIsNotNone(frame, name)
        # And each entry really is the frame the matching tab attribute holds,
        # so the two cannot drift apart.
        for name, attr in (('downloader', 'tab_downloader'),
                            ('queue', 'tab_queue'),
                            ('history', 'tab_history'),
                            ('studio', 'tab_studio'),
                            ('settings', 'tab_customization'),
                            ('performance', 'tab_performance')):
            self.assertIs(self.app._page_frames[name],
                          getattr(self.app, attr), name)

    def test_the_appearance_page_has_a_scroll_well(self):
        """Its content is taller than the minimum window height.

        Without the well the lower cards are simply unreachable at the default
        size, and no test that only builds the window would notice.
        """
        import customtkinter as ctk
        wells = [w for w in self.app._iter_widgets(self.app.tab_customization)
                 if isinstance(w, ctk.CTkScrollableFrame)]
        self.assertEqual(len(wells), 1)
        self.assertEqual(wells[0], self.app.settings_scroll)

    def test_custom_theme_is_offered_in_the_theme_menu(self):
        self.assertIn(ui.CUSTOM_THEME_NAME, list(self.app.theme_menu.cget('values')))

    def test_custom_theme_is_registered_in_the_palette_table(self):
        """It lives in COLOR_THEMES so every lookup resolves it uniformly."""
        self.assertIn(ui.CUSTOM_THEME_NAME, ui.COLOR_THEMES)

    def test_start_from_seeds_and_applies(self):
        self.app.start_custom_from('Dracula')
        self.assertEqual(self.app._color_theme_name, ui.CUSTOM_THEME_NAME)
        self.assertEqual(self.app._palette['accent'],
                         ui.COLOR_THEMES['Dracula']['accent'])
        self.assertEqual(self.app._palette['bg'], ui.COLOR_THEMES['Dracula']['bg'])

    def test_set_role_repaints_and_persists(self):
        self.app.start_custom_from('Dracula')
        self.assertTrue(self.app.set_custom_role('accent', '#00FF00'))
        self.assertEqual(self.app._palette['accent'], '#00ff00')
        # The hover follows, so the accent button hovers green too.
        self.assertEqual(self.app._palette['accent_hover'],
                         ui._lift('#00ff00', 0.18, self.app._palette['mode']))
        self.assertEqual(self.app._prefs['custom_theme_roles']['accent'], '#00ff00')

    def test_set_role_rejects_a_bad_colour_and_an_unknown_role(self):
        before = dict(self.app._palette)
        self.assertFalse(self.app.set_custom_role('accent', 'not a colour'))
        self.assertFalse(self.app.set_custom_role('nonexistent', '#00ff00'))
        self.assertEqual(self.app._palette, before)

    def test_swatch_keeps_its_own_colour_through_a_theme_switch(self):
        """A swatch is a sample, so it must not be repainted as a role.

        It carries an empty role map: if it joined the role walk it would take
        the palette's value and stop showing the colour being edited.
        """
        self.app.start_custom_from('Dracula')
        _label, swatch, _entry = self.app._custom_swatch_rows['accent']
        self.assertEqual(ui._normalize_hex(swatch.cget('fg_color')),
                         self.app._palette['accent'])
        self.app.apply_color_theme('TuneLab Dark')
        self.app.apply_color_theme(ui.CUSTOM_THEME_NAME)
        self.assertEqual(ui._normalize_hex(swatch.cget('fg_color')),
                         self.app._palette['accent'])

    def test_hex_entry_reflects_the_palette(self):
        self.app.start_custom_from('Nord')
        _label, _swatch, entry = self.app._custom_swatch_rows['accent']
        self.assertEqual(entry.get().strip().lower(),
                         ui.COLOR_THEMES['Nord']['accent'].lower())

    def test_invalid_hex_entry_is_reported_not_swallowed(self):
        self.app.start_custom_from('Nord')
        _label, _swatch, entry = self.app._custom_swatch_rows['accent']
        before = self.app._palette['accent']
        entry.delete(0, 'end')
        entry.insert(0, 'not a colour')
        toasts = []
        self.app.show_toast = lambda *a, **k: toasts.append(a)
        self.app._commit_custom_hex('accent')
        self.assertEqual(self.app._palette['accent'], before)
        self.assertTrue(toasts, 'an invalid value must say so')

    def test_no_contrast_text_is_shown_on_the_theme_page(self):
        """The custom-theme card must not print a pass/fail line.

        It read as an error message even when it was reporting success, which
        is worse than showing nothing: "All contrast checks pass" sitting under
        your colour swatches looks like something has gone wrong. The contrast
        checking itself is not gone - it still runs offline in
        tools/audit_contrast.py and in the tests below - it just is not
        surfaced as UI copy any more.
        """
        self.app.start_custom_from('Dracula')
        self.app._refresh_custom_theme_ui()
        self.assertFalse(
            hasattr(self.app, '_custom_report'),
            "the contrast report label must not be created any more",
        )
        texts = []
        for widget in self.app._iter_widgets():
            try:
                value = widget.cget('text')
            except Exception:
                continue
            if isinstance(value, str):
                texts.append(value.lower())
        for phrase in ('contrast check', 'below target', 'all contrast'):
            self.assertFalse(
                any(phrase in t for t in texts),
                "found %r still displayed on the page" % phrase,
            )

    def test_mode_switch_rebases_the_fallback_palette(self):
        """With nothing overridden, switching mode has to move the palette.

        Roles the user has chosen are kept either way - their picks are the
        point of the feature - so this only holds for an untouched palette.
        """
        self.app.reset_custom_palette('dark')
        dark_bg = self.app._palette['bg']
        self.app._on_custom_mode('light')
        self.assertEqual(self.app._palette['mode'], 'light')
        self.assertNotEqual(self.app._palette['bg'], dark_bg)
        self.assertEqual(ui.contrast_failures(self.app._palette), [])
        self.app._on_custom_mode('dark')
        self.assertEqual(self.app._palette['mode'], 'dark')
        self.assertEqual(self.app._palette['bg'], dark_bg)

    def test_mode_switch_keeps_the_users_own_picks(self):
        """A mode change rebases the fallbacks, not the colours already chosen.

        Losing your picks on every base toggle would make the control feel like
        it resets the work.
        """
        self.app.start_custom_from('Nord')
        self.app.set_custom_role('accent', '#ff0055')
        self.app._on_custom_mode('light')
        self.assertEqual(self.app._palette['accent'], '#ff0055')
        self.assertEqual(self.app._palette['mode'], 'light')

    def test_reset_clears_every_override(self):
        self.app.start_custom_from('Dracula')
        self.app.set_custom_role('accent', '#00ff00')
        self.app.reset_custom_palette('dark')
        self.assertEqual(self.app._custom_roles, {})
        self.assertEqual(self.app._palette['bg'],
                         ui.COLOR_THEMES['TuneLab Dark']['bg'])

    def test_custom_edits_do_not_leak_into_the_built_in_palettes(self):
        dracula_before = dict(ui.COLOR_THEMES['Dracula'])
        self.app.start_custom_from('Dracula')
        self.app.set_custom_role('accent', '#00ff00')
        self.assertEqual(ui.COLOR_THEMES['Dracula'], dracula_before)

    def test_start_from_refuses_the_custom_entry_itself(self):
        """Copying My Theme onto itself would freeze the current picks."""
        self.app.start_custom_from('Nord')
        self.app.set_custom_role('accent', '#00ff00')
        self.app.start_custom_from(ui.CUSTOM_THEME_NAME)
        self.assertEqual(self.app._palette['accent'], '#00ff00')

    def test_a_custom_palette_reaches_the_scroll_wells(self):
        """A hand-built palette must follow through the composite widgets.

        The role walk covers ordinary widgets; scroll wells and dropdowns are
        painted by a separate pass, so they are the likeliest place for a
        custom palette to look half-applied.
        """
        self.app.start_custom_from('Midnight')
        self.app.set_custom_role('surface', '#203040')
        self.app.set_custom_role('bg', '#101820')
        self.app.update_idletasks()
        for page in ('tab_performance', 'tab_history', 'tab_queue'):
            frame = getattr(self.app, page, None)
            for widget in self.app._iter_widgets(frame):
                if not isinstance(widget, ctk.CTkScrollableFrame):
                    continue
                got = widget._parent_canvas.cget('bg')
                if isinstance(got, (tuple, list)):
                    got = got[0]
                self.assertNotEqual(
                    ui._normalize_hex(got), ui.COLOR_THEMES['Midnight']['bg'],
                    "%s well kept the palette it started with" % page)


class PrefDeclarationTests(unittest.TestCase):
    """Every pref the app writes must be declared in ``_default_prefs``.

    ``_load_prefs`` keeps only keys already present in the defaults table
    (``defaults.update({k: v for k, v in data.items() if k in defaults})``).
    That guard is deliberate - it stops a corrupt or hand-edited file inventing
    settings - but it makes ``_default_prefs`` the real schema. A key that is
    written without being declared is therefore dropped on *every* launch, and
    the next save persists the truncated table, so the user's value is destroyed
    rather than merely ignored.

    That is not hypothetical: all four Performance settings (use_aria2,
    aria2_connections, concurrent_fragment_downloads, max_video_resolution)
    were written but never declared, so "concurrent fragment downloads" reverted
    to 8 on every start no matter what was applied.
    """

    def _declared(self):
        body = _method_source("_default_prefs")
        return set(re.findall(r'["\']([a-z_0-9]+)["\']\s*:', body))
    def _written(self):
        return set(re.findall(r"_set_pref\(\s*['\"]([a-z_0-9]+)['\"]", _source()))

    def test_no_written_pref_is_missing_from_the_defaults_table(self):
        missing = self._written() - self._declared()
        self.assertEqual(
            missing, set(),
            "written but not declared, so dropped on every load: %s"
            % sorted(missing))

    def test_the_performance_settings_are_declared(self):
        """Named explicitly, so the regression names itself if it ever returns."""
        declared = self._declared()
        for key in ("use_aria2", "aria2_connections",
                    "concurrent_fragment_downloads", "max_video_resolution"):
            self.assertIn(key, declared)

    def test_the_declared_perf_defaults_match_the_downloader_ones(self):
        """Two default tables would drift the moment either side is edited.

        The UI reads downloader.DEFAULT_PERF_CONFIG for the live config but
        _default_prefs for the on-disk value, so a mismatch shows up as a
        control sitting at a position the slider was never told about.
        """
        body = _method_source("_default_prefs")
        for key in ("use_aria2", "aria2_connections",
                    "concurrent_fragment_downloads", "max_video_resolution"):
            m = re.search(r'["\']%s["\']\s*:\s*([^,\n]+)' % re.escape(key), body)
            self.assertIsNotNone(m, key)
            assert m is not None  # narrows the Optional for the type checker
            self.assertEqual(eval(m.group(1).strip()),  # noqa: S307
                             downloader.DEFAULT_PERF_CONFIG[key], key)


@requires_ctk
class PerformanceSettingsPersistTests(unittest.TestCase):
    """The Performance page's controls must survive a restart.

    The user-facing symptom was "concurrent fragments resets every time I open
    the app", which read like a slider that never applied. It applied fine - the
    value was written to disk and then dropped on the next load, and the save
    after that removed it from the file entirely.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pref = os.path.join(self._tmpdir.name, "ui_prefs.json")
        with open(self._pref, "w", encoding="utf-8") as fh:
            fh.write("{}")
        patcher = mock.patch.object(
            ui.UniversalAudioStudio, "_get_pref_path",
            lambda _app, _p=self._pref: _p)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = self._build()

    def _build(self):
        app = ui.UniversalAudioStudio()
        app.withdraw()
        app.update_idletasks()
        return app

    def _reopen(self):
        """Close the window and stand a fresh one up on the same prefs file."""
        try:
            self.app.destroy()
        except Exception:
            pass
        self.app = self._build()
        return self.app

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        self._tmpdir.cleanup()

    def test_the_fragment_slider_keeps_its_value_across_a_restart(self):
        self.app.concurrent_frag_slider.set(24)
        self.app.apply_performance_settings()
        self.assertEqual(self._reopen().concurrent_frag_slider.get(), 24)

    def test_the_aria2_connection_slider_keeps_its_value_across_a_restart(self):
        self.app.aria2_conn_slider.set(9)
        self.app.apply_performance_settings()
        self.assertEqual(self._reopen().aria2_conn_slider.get(), 9)

    def test_the_aria2_toggle_keeps_its_value_across_a_restart(self):
        self.app.aria2_var.set(False)
        self.app.apply_performance_settings()
        self.assertFalse(self._reopen().aria2_var.get())

    def test_the_applied_value_stays_on_disk_after_a_restart(self):
        """Not just reloaded correctly - not deleted from the file.

        The load dropped the key and the next save persisted the truncated table,
        so the user's number was gone for good even though the control still
        looked right in the session that had set it.
        """
        self.app.concurrent_frag_slider.set(24)
        self.app.apply_performance_settings()
        self._reopen()
        with open(self._pref, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["concurrent_fragment_downloads"], 24)

    def test_the_slider_range_and_the_clamp_agree_on_the_maximum(self):
        """The slider and the saved-value clamp must not disagree.

        The UI builds the slider from downloader.FRAGMENT_DOWNLOADS_MAX; a
        literal in either place would let the control offer a value the clamp
        then silently rewrites, so the setting would appear not to stick.
        """
        src = pathlib.Path(ui.__file__).read_text(encoding="utf-8")
        match = re.search(r"concurrent_frag_slider = .*?\n\n", src, re.DOTALL)
        self.assertTrue(match, "could not find the fragment slider in ui.py")
        body = match.group(0) if match else ""
        self.assertIn("downloader.FRAGMENT_DOWNLOADS_MAX", body)
        self.assertNotIn("to=32", body)

    def test_the_resolution_cap_survives_a_restart(self):
        self.app.video_res_var.set("720p")
        self.app._on_video_res_change("720p")
        self.assertEqual(self._reopen().video_res_var.get(), "720p")

    def test_a_corrupt_prefs_file_falls_back_to_defaults(self):
        """A truncated or hand-mangled file must not take the app down.

        _load_prefs swallows the parse error and returns the defaults, which is
        what keeps a bad write from turning into a window that will not open.
        """
        with open(self._pref, "w", encoding="utf-8") as fh:
            fh.write("{this is not json")
        app = self._reopen()
        self.assertEqual(app._prefs, app._default_prefs())
        self.assertEqual(app.concurrent_frag_slider.get(),
                         downloader.DEFAULT_PERF_CONFIG["concurrent_fragment_downloads"])

    def test_the_loaded_values_reach_the_downloader_config(self):
        """Persisting in the prefs file is not the same as being applied.

        The slider value is only useful once it survives _load_prefs *and* the
        config build, so both halves are asserted together.
        """
        self.app.concurrent_frag_slider.set(19)
        self.app.aria2_conn_slider.set(11)
        self.app.apply_performance_settings()
        app = self._reopen()
        cfg = downloader.perf_cfg_from_prefs(app._load_prefs())
        self.assertEqual(cfg["concurrent_fragment_downloads"], 19)
        self.assertEqual(cfg["aria2_connections"], 11)

    def test_an_out_of_range_hand_edited_value_is_clamped_not_honoured(self):
        """A hand-edited file must not be able to ask aria2 for 99999 sockets."""
        with open(self._pref, "w", encoding="utf-8") as fh:
            json.dump({"aria2_connections": 99999,
                       "concurrent_fragment_downloads": 0}, fh)
        app = self._reopen()
        cfg = downloader.perf_cfg_from_prefs(app._load_prefs())
        self.assertEqual(cfg["aria2_connections"], downloader.ARIA2_MAX_CONNECTIONS)
        self.assertEqual(cfg["concurrent_fragment_downloads"], 1)


class BundleHelperSearchTests(unittest.TestCase):
    """Bundled helpers have to be found inside a PyInstaller onedir bundle.

    PyInstaller 6 collects binaries into a ``_internal`` subfolder *next to* the
    app EXE, not into the EXE's own folder. ``find_bundled_exe`` documented
    searching ``_internal`` but only ever checked ``sys._MEIPASS`` (the onefile
    extraction dir) and the EXE's folder, so a shipped
    ``_internal\\aria2c.exe`` was reported to the user as "aria2 is NOT
    installed on your system" - with the file sitting a few hundred KB away.
    """

    def _bundle_with(self, *relpaths):
        """A fake onedir bundle; returns (bundle dir, {relpath: abspath})."""
        bundle = tempfile.mkdtemp(prefix="bundle_")
        self.addCleanup(shutil.rmtree, bundle, True)
        made = {}
        for rel in relpaths:
            path = os.path.join(bundle, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("")
            made[rel] = path
        return bundle, made

    def _pretend_running_from(self, bundle):
        """Point sys.executable at the bundle and hide PATH."""
        real_executable, real_which = sys.executable, shutil.which
        sys.executable = os.path.join(bundle, "AudioDownloader.exe")
        shutil.which = lambda *_a, **_k: None  # ignore PATH for these checks
        self.addCleanup(lambda: setattr(sys, "executable", real_executable))
        self.addCleanup(lambda: setattr(shutil, "which", real_which))

    def test_the_internal_subfolder_is_searched(self):
        dirs = downloader._bundle_search_dirs()
        self.assertTrue(any(d.endswith("_internal") for d in dirs),
                        "no _internal candidate: %s" % dirs)

    def test_a_helper_in_the_internal_folder_is_found(self):
        bundle, made = self._bundle_with("_internal/aria2c.exe")
        self._pretend_running_from(bundle)
        self.assertEqual(downloader.find_bundled_exe("aria2c.exe"),
                         made["_internal/aria2c.exe"])
        self.assertEqual(downloader.get_fast_downloader_path(),
                         made["_internal/aria2c.exe"])

    def test_a_helper_next_to_the_exe_is_still_found(self):
        """The fix adds _internal; it must not have replaced the old search."""
        bundle, made = self._bundle_with("yt-dlp.exe")
        self._pretend_running_from(bundle)
        self.assertEqual(downloader.find_bundled_exe("yt-dlp.exe"),
                         made["yt-dlp.exe"])

    def test_a_missing_helper_is_still_reported_as_missing(self):
        """The negative case: the search must not resolve to a wrong path."""
        bundle, _ = self._bundle_with()
        self._pretend_running_from(bundle)
        self.assertIsNone(
            downloader.find_bundled_exe("definitely_absent.exe"))

    def test_the_search_list_has_no_repeated_folders(self):
        """The aria2 dialog prints this list, so it has to read sensibly.

        In a frozen onedir build sys._MEIPASS points at the _internal folder and
        __file__ lives inside it, so the three natural candidates collapse to
        one path and the list came out as "X, X, X".
        """
        bundle, made = self._bundle_with("_internal/aria2c.exe")
        self._pretend_running_from(bundle)
        dirs = downloader._bundle_search_dirs()
        self.assertEqual(len(dirs), len(set(dirs)), dirs)

        # And explicitly with _MEIPASS set to the same folder, as PyInstaller does.
        real = getattr(sys, "_MEIPASS", None)
        sys._MEIPASS = os.path.join(bundle, "_internal")
        self.addCleanup(lambda: setattr(sys, "_MEIPASS", real)
                        if real is not None else delattr(sys, "_MEIPASS"))
        dirs = downloader._bundle_search_dirs()
        self.assertEqual(len(dirs), len(set(dirs)), dirs)
        self.assertEqual(dirs[0], os.path.join(bundle, "_internal"))

    def test_the_not_found_message_names_the_real_search_folders(self):
        """The dialog must not send the user to a folder we never look in.

        It used to say "place aria2c.exe next to the app EXE", which is exactly
        wrong for a packaged build: PyInstaller puts the collected binaries in
        _internal *beside* the EXE, so a user following that instruction to the
        letter would still get "aria2 was NOT found". The message now lists
        the folders _bundle_search_dirs() actually returns.
        """
        body = _method_source("apply_performance_settings")
        self.assertNotIn("next to the app EXE", body)
        self.assertIn("downloader._bundle_search_dirs()", body)
        # And the list it prints has to actually contain the _internal folder,
        # which is the one a packaged build uses.
        bundle, made = self._bundle_with("_internal/aria2c.exe")
        self._pretend_running_from(bundle)
        self.assertTrue(any("_internal" in d for d in
                            downloader._bundle_search_dirs()),
                        downloader._bundle_search_dirs())


class FormControlThemeTests(unittest.TestCase):
    """Checkboxes, sliders and bars follow the palette, by type not by name."""

    def test_the_theme_pass_styles_controls_through_the_type_walk(self):
        self.assertIn("self._style_form_controls(pal)",
                      _method_source("apply_color_theme"))

    def test_the_stale_name_lists_are_gone(self):
        # These named four checkboxes that no longer exist and missed the two
        # the rail settings card adds — the two left out kept CustomTkinter's
        # blue in every one of the 21 palettes, because a name list cannot
        # notice a control being added somewhere else in the file.
        src = _source()
        for legacy in ("overlay_chk", "compact_chk", "opacity_chk",
                       "disable_max_chk", "opacity_slider"):
            self.assertNotIn(legacy, src)
        self.assertNotIn("for cb_name in", src)
        self.assertNotIn("for bar_name in", src)

    def test_controls_are_found_by_their_class_not_their_attribute(self):
        body = _method_source("_style_form_controls")
        for cls in ("CTkCheckBox", "CTkSlider", "CTkProgressBar"):
            self.assertIn(cls, body)
        self.assertIn("self._iter_widgets()", body)

    def test_the_options_a_call_site_leaves_unset_are_set_here(self):
        # Those are the leak: CTk substitutes its own theme colors for them and
        # cget() returns them as tuples, which the role walk skips because it
        # only remaps strings. A track, a trough, a ring and a checkmark are
        # exactly what no call site was setting.
        body = _method_source("_style_form_controls")
        for opt in ("fg_color", "border_color", "checkmark_color",
                    "button_hover_color", "progress_color"):
            self.assertIn(opt, body)

    def test_the_checkmark_and_the_ring_are_chosen_for_contrast(self):
        body = _method_source("_style_form_controls")
        # The checkmark is drawn *on* the accent, so a fixed palette text tone
        # would vanish into a yellow or cyan accent; the unchecked ring uses
        # the muted tone because the hover tone is too close to the page on
        # the light palettes.
        self.assertIn("_on_color(", body)
        self.assertIn('pal.get("sub"', body)

    def test_the_probe_watches_the_control_colors_and_fails_on_them(self):
        probe = (_REPO / "tools" / "probe_chrome.py").read_text(encoding="utf-8")
        self.assertIn("check_form_controls", probe)
        self.assertIn("os._exit(1 if control_fails else 0)", probe)


class OptionalRowVisibilityTests(unittest.TestCase):
    """Empty text rows must not claim vertical space.

    The Downloader page stacked five labels that are all empty on a fresh
    launch (detail, speed, save folder, yt-dlp notice, tag hint). A packed but
    empty CTkLabel still reserves its full line height, which is what produced
    the dead gap down the middle of the page.
    """

    def test_the_helper_watches_configure_not_just_a_textvariable(self):
        body = _method_source("_pack_h_if_used")
        # Every real update is configure(text=...). Watching a textvariable (the
        # obvious approach) silently misses all of them and the row stays hidden
        # after its first message.
        self.assertIn("label.configure = _configure_and_sync", body)
        self.assertIn('if "text" in kw', body)

    def test_the_helper_refuses_a_blank_row(self):
        body = _method_source("_sync_h_if_used")
        self.assertIn("pack_forget", body)
        self.assertIn("strip()", body)

    def test_the_downloader_rows_go_through_the_helper(self):
        body = _method_source("build_downloader_view")
        for name in ("dl_detail", "dl_speed_lbl", "save_folder_lbl",
                     "_ytdlp_notice_lbl"):
            # Matched with a regex rather than a fixed substring: the call is
            # wrapped across lines for the longer ones, so a literal check would
            # pass on the short calls and silently skip the rest.
            self.assertRegex(
                body, r"_pack_h_if_used\(\s*self\.%s\b" % name,
                "%s should be optional, not always packed" % name)


class PreviewStopButtonTests(unittest.TestCase):
    """A Stop button must only exist while its preview is running.

    Both were permanently visible and disabled, which read as two live controls
    and put "stop" on screen when there was nothing to stop.
    """

    def test_stops_start_hidden(self):
        src = _source()
        self.assertEqual(src.count("self.btn_stop_audio.grid_remove()"), 1)
        self.assertEqual(src.count("self.btn_stop_video.grid_remove()"), 1)

    def test_every_stop_toggle_also_changes_visibility(self):
        src = _source()
        # No bare configure(state=...) may survive: that is what let a visible
        # disabled Stop button exist in the first place.
        self.assertNotIn("btn_stop_audio.configure(state=", src)
        self.assertNotIn("btn_stop_video.configure(state=", src)
        self.assertIn("self._show_stop_button(self.btn_stop_audio, True)", src)
        self.assertIn("self._show_stop_button(self.btn_stop_video, False)", src)

    def test_the_helper_shows_and_hides(self):
        body = _method_source("_show_stop_button")
        self.assertIn("button.grid()", body)
        self.assertIn("button.grid_remove()", body)


class ProgressRowVisibilityTests(unittest.TestCase):
    """The progress bar and Cancel are noise until a download is running."""

    def test_the_row_starts_packed_away(self):
        self.assertIn("progress_row.pack_forget()",
                      _method_source("build_downloader_view"))

    def test_it_follows_the_download_lifecycle(self):
        body = _method_source("set_download_buttons_state")
        self.assertIn("self._show_progress_row(not enabled)", body)

    def test_the_helper_toggles_the_row(self):
        body = _method_source("_show_progress_row")
        self.assertIn("row.pack(", body)
        self.assertIn("row.pack_forget()", body)


class HistorySearchFieldTests(unittest.TestCase):
    """The search box was an unlabelled empty rectangle.

    customtkinter 6.0.0 never draws the placeholder of an entry that has a
    textvariable, because _activate_placeholder() guards on
    ``self._textvariable == ""`` - a StringVar compared to a string, always
    False. Forcing it on by hand was tried and reverted (it collided with the
    library's own focus/insert handling), so the field is labelled instead.
    """

    def test_there_is_no_placeholder_to_silently_fail(self):
        body = _source()
        history_view = body[body.index("def build_history_view"):]
        history_view = history_view[:history_view.index("\n    def ", 10)]
        self.assertNotIn("placeholder_text=", history_view)

    def test_the_field_carries_a_visible_label(self):
        self.assertIn('search_row, text="Search"', _source())

    def test_the_filter_never_matches_the_placeholder_text(self):
        body = _method_source("_refresh_history_view")
        # A shown placeholder lives in the same StringVar as real input, so the
        # filter has to ask whether the placeholder is active.
        self.assertIn("_placeholder_text_active", body)


class PruneMissingHistoryTests(unittest.TestCase):
    """History entries whose file is gone are removable, but never silently."""

    def test_pruning_needs_a_confirmation(self):
        self.assertIn("messagebox.askyesno",
                      _method_source("_prune_missing_history"))

    def test_the_button_is_only_offered_when_something_is_prunable(self):
        body = _method_source("_refresh_history_view")
        self.assertIn('state="normal" if missing else "disabled"', body)

    def test_prune_keeps_entries_with_no_recorded_path(self):
        queue_src = (_REPO / "download_queue.py").read_text(encoding="utf-8")
        body = re.search(r"\n    def prune_missing_history\(.*?(?=\n    def |\Z)",
                         queue_src, re.DOTALL)
        assert body is not None, "download_queue.py lost prune_missing_history"
        src = body.group(0)
        self.assertIn("if filepath and not os.path.exists(filepath)", src)
        self.assertIn("kept.append(entry)", src)

    def test_prune_reports_how_many_it_removed(self):
        queue_src = (_REPO / "download_queue.py").read_text(encoding="utf-8")
        self.assertIn("def prune_missing_history(self) -> int:", queue_src)


class DestructiveButtonWeightTests(unittest.TestCase):
    """Actions that open a confirmation should not be the loudest control."""

    def test_confirmed_clears_are_not_full_danger_fills(self):
        src = _source()
        for name in ("btn_clear_history", "clear_cache_btn"):
            self.assertIn('_style_button(self.%s, "ghost"' % name, src,
                          "%s opens a dialog; a full danger fill overweights it"
                          % name)

    def test_immediate_stop_actions_keep_the_danger_fill(self):
        src = _source()
        for name in ("btn_stop_audio", "btn_stop_video", "btn_cancel_download"):
            self.assertIn('_style_button(self.%s, "danger"' % name, src,
                          "%s acts immediately and should stay red" % name)


class VerticalRhythmTests(unittest.TestCase):
    """Stacked rows are a whole rhythm step apart."""

    @staticmethod
    def _theme_source() -> str:
        # The UITheme tokens moved to ui_theme.py; this assertion is about where
        # the constant is *defined*, so it has to read that file now.
        return (_REPO / "ui_theme.py").read_text(encoding="utf-8")

    def test_the_rhythm_is_one_token(self):
        self.assertEqual(
            re.search(r"RHYTHM = (\d+)", self._theme_source()).group(1), "8")

    def test_the_rhythm_token_is_available_to_the_ui(self):
        import ui
        self.assertEqual(ui.UITheme.RHYTHM, 8)

    def test_the_downloader_page_uses_it(self):
        body = _method_source("build_downloader_view")
        # No ad-hoc 2/4/6/8 paddings left between the stacked rows.
        self.assertNotIn("pady=(0, 2)", body)
        self.assertNotIn("pady=(0, 4)", body)
        self.assertGreaterEqual(body.count("UITheme.RHYTHM"), 6)


class ModuleSplitTests(unittest.TestCase):
    """The UI was split out of the 7k-line ui.py; keep the split honest.

    Each assertion is about a property that can rot silently: a module that is
    created but never imported still "passes" a file-exists check, and a
    re-export that is dropped breaks an import at runtime, not at build time.
    """

    def test_the_new_modules_exist_and_import(self):
        import ui_theme
        import ui_widgets
        self.assertTrue(ui_theme.UITheme)
        self.assertTrue(ui_widgets._QueueRowList)

    def test_ui_reexports_the_theme_names(self):
        # Call sites say ui.UITheme / from ui import COLOR_THEMES. If a name
        # stops being re-exported, every one of those breaks at runtime.
        import ui
        import ui_theme
        for name in ("UITheme", "COLOR_THEMES", "CUSTOM_THEME_ROLES",
                     "LEGACY_HEX_ROLES", "_on_color", "_split_headline",
                     "_icon_shapes", "_paint_icon", "_render_icon_image",
                     "custom_palette", "contrast_failures", "_normalize_hex",
                     "_lift", "_mix", "_contrast", "_rel_luminance",
                     "_ring_color", "_ui_font", "_UI_FONT_FAMILY"):
            self.assertTrue(hasattr(ui, name),
                            "ui.%s is no longer re-exported" % name)
            if hasattr(ui_theme, name):
                self.assertIs(getattr(ui, name), getattr(ui_theme, name),
                              "ui.%s is a copy, not the shared object" % name)

    def test_the_theme_module_is_free_of_widget_code(self):
        # The reason this split is safe is that ui_theme.py never touches Tk. A
        # stray widget there would mean the "pure primitives" split had quietly
        # become a dependency cycle waiting to happen. Docstrings are stripped
        # first: they legitimately mention widget names (CTkFrame.configure() is
        # described in a comment about the theme walker), and matching prose
        # would make this guard cry wolf on its first real edit.
        src = (_REPO / "ui_theme.py").read_text(encoding="utf-8")
        import io
        import tokenize
        code = []
        prev_type = tokenize.INDENT
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            # Drop any STRING token that is a whole statement (a docstring).
            if tok.type == tokenize.STRING and prev_type in (
                    tokenize.INDENT, tokenize.NEWLINE, tokenize.NL,
                    tokenize.DEDENT):
                prev_type = tok.type
                continue
            if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
                prev_type = tok.type
                code.append(tok.string)
        code_src = " ".join(code)
        for banned in ("CTkButton", "CTkFrame", "CTkEntry", ".pack(", ".grid("):
            self.assertNotIn(banned, code_src,
                             "ui_theme.py must stay free of widget code (%r)"
                             % banned)
        self.assertNotIn("import tkinter", code_src)

    def test_main_owns_startup_and_ui_delegates(self):
        main_src = (_REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("def run(", main_src)
        self.assertIn("def _focus_running_instance(", main_src)
        # ui.py must not keep a second copy of the single-instance logic.
        self.assertNotIn("TuneLab_SingleInstance_Mutex", _source())

    def test_the_split_actually_reduced_ui(self):
        lines = len((_REPO / "ui.py").read_text(encoding="utf-8").splitlines())
        self.assertLess(lines, 6000,
                        "ui.py is back to being the 7k-line monolith")


class DeferredExceptionBindingTests(unittest.TestCase):
    """A queued lambda must not close over an `except ... as e` variable.

    Python deletes that name when the except block ends, and self.after()
    lambdas run on a later mainloop turn - so `lambda: str(e)` raised
    NameError and the user saw no error dialog at all. Both instances here
    were found with pyflakes; this keeps a third from appearing.
    """

    def test_no_module_fails_to_compile(self):
        """A syntax error in any app module should fail loudly, not at runtime."""
        for name in ("ui.py", "ui_theme.py", "ui_widgets.py", "main.py",
                     "downloader.py", "download_queue.py", "updater.py",
                     "tag_editor.py"):
            path = _REPO / name
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as exc:  # pragma: no cover - a real breakage
                self.fail("%s no longer compiles: %s" % (name, exc))

    def _handler_variable(self, node):
        """The name an `except ... as <name>` handler binds, if any."""
        return node.name if isinstance(node, ast.ExceptHandler) and node.name else None

    def test_no_deferred_lambda_reads_the_except_variable(self):
        """The real check, done on the syntax tree.

        `except ... as e:` unbinds ``e`` when the block ends, but a callback
        handed to ``self.after()`` runs on a later mainloop turn - by then the
        name is gone and the dialog raises NameError instead of appearing. The
        correct rewrite binds what it needs to a local (``err_text = ...``) and
        the lambda closes over *that*.

        Walking the AST is what makes this trustworthy: a regex cannot tell an
        ``except ... as e`` from the same words inside a comment or docstring,
        which is how the first version of this test reported its own fix as
        the bug it was guarding against.
        """
        problems = []
        for name in ("ui.py", "ui_theme.py", "ui_widgets.py", "main.py",
                     "downloader.py", "download_queue.py", "updater.py",
                     "tag_editor.py"):
            path = _REPO / name
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for handler in (n for n in ast.walk(tree)
                            if self._handler_variable(n)):
                var = self._handler_variable(handler)
                # Only the handler's own statements can reach the deferred call;
                # a nested def/lambda closes over its own locals.
                for node in ast.walk(handler):
                    if not isinstance(node, ast.Lambda):
                        continue
                    loads = {n.id for n in ast.walk(node.body)
                             if isinstance(n, ast.Name) and isinstance(
                                 n.ctx, ast.Load)}
                    if var in loads:
                        problems.append(
                            "%s:%d - a deferred lambda reads the except-variable "
                            "%r; bind the message to a local first, it is deleted "
                            "when the block ends" % (name, node.lineno, var))
        self.assertEqual([], problems, "\n".join(problems))

    def test_no_lambda_reads_the_except_variable(self):
        """Deprecated alias kept so an existing -m unittest selector still works."""
        self.test_no_deferred_lambda_reads_the_except_variable()

    def test_the_two_known_sites_bind_their_message(self):
        src = (_REPO / "ui.py").read_text(encoding="utf-8")
        self.assertIn('"Update Error", "Failed to update yt-dlp.", detail=err_text',
                      src)
        self.assertIn('_show_error_dialog("Cache Clear Error", err_text)', src)


if __name__ == "__main__":
    unittest.main()
