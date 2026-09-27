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
import importlib.util
import pathlib
import re
import unittest
from types import SimpleNamespace

_REPO = pathlib.Path(__file__).resolve().parents[1]
_HAS_CTK = importlib.util.find_spec("customtkinter") is not None


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
        src = _source()
        self.assertIn("TuneLab_SingleInstance_Mutex", src)
        self.assertIn("ERROR_ALREADY_EXISTS", src)
        self.assertIn("sys.exit(0)", src)


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
            "⏳ [  PENDING] " + url[:50],
        )

    def test_unknown_status_gets_the_placeholder_icon(self):
        item = SimpleNamespace(status="weird", url="u")
        self.assertTrue(self.app._queue_row_label(0, item, 9).startswith("? "))


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

    def test_header_carries_a_live_status_slot(self):
        self.assertIn("header_status_lbl", _source())
        body = _method_source("update_dl_status")
        self.assertIn("header_status_lbl", body)


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

    def test_no_live_listbox_construction(self):
        src = _source()
        self.assertNotIn("tk.Listbox(", src)
        self.assertIn("class _QueueRowList", src)

    def test_rows_speak_the_listbox_api_the_refresh_logic_pins(self):
        src = _source()
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


if __name__ == "__main__":
    unittest.main()
