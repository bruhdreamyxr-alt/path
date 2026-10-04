"""The themed queue list widget.

Lifted out of ui.py (class _QueueRowList, 143 lines) so the queue's scrollable
row container lives with the other reusable widgets instead of inside the
7k-line module. ui.py imports it, so ``ui._QueueRowList`` and every existing
reference still resolve to this class - it is the same object, not a copy.
"""
import customtkinter as ctk
from typing import Any

from ui_theme import UITheme, _on_color


class _QueueRowList(ctk.CTkScrollableFrame):
    """Themed stand-in for the queue's raw ``tk.Listbox``.

    Speaks the small subset of the Listbox API that
    ``_rebuild_queue_list`` and friends use (``size``/``insert``/
    ``delete``/``get``/``itemconfig``/``curselection``), so the refresh
    logic — in-place row updates instead of a full rebuild on every
    progress tick — is unchanged. Rows are CTk labels, so they follow the
    active palette like every other widget; the old tk.Listbox sat
    entirely outside the theme walker and kept hardcoded dark rows after
    switching to a Light theme.
    """

    def __init__(self, master, palette=None, **kwargs):
        # Plain attributes first: CTkFrame.__init__ may re-configure itself
        # through the override below, which touches them.
        pal = dict(palette or {})
        self._labels: list[str] = []
        self._rows: list[Any] = []
        self._overrides: dict[int, str] = {}  # per-row text_color (placeholder)
        self._selected: int | None = None
        self._row_fg = pal.get("text", "#ecf0f1")
        self._sel_bg = pal.get("accent", "#3498db")
        self._sel_fg = _on_color(
            self._sel_bg, pal.get("text", "#ecf0f1"), pal.get("bg", "#1e1e24"))
        super().__init__(
            master,
            # The page is a surface card; filling this with the same color would
            # make its own corner notches paint themselves invisible, and it read
            # as a flat ring instead of a well. The window background color sinks
            # it into the card and the notches pick up the page's surface.
            fg_color=pal.get("bg", "#1e1e24"),
            corner_radius=UITheme.RADIUS_LG,
            border_width=UITheme.BORDER_W,
            border_color=pal.get("hover", "#34495e"),
            **kwargs,
        )
        # Let the theme walker repaint the panel like any other widget.
        setattr(self, "_theme_roles", {"fg_color": "bg",
                                       "border_color": "hover"})

    # --- Listbox-compatible API (the only surface ui.py uses) ------

    def size(self) -> int:
        return len(self._labels)

    def get(self, index: int) -> str:
        return self._labels[index] if 0 <= index < len(self._labels) else ""

    def insert(self, index, text: str) -> None:
        # Only append semantics are used (always called with tk.END).
        i = len(self._labels)
        self._labels.append(text)
        row = ctk.CTkLabel(
            self,
            text=text,
            font=UITheme.F(11),
            anchor="w",
            corner_radius=UITheme.RADIUS_MD,
            fg_color="transparent",
            text_color=self._row_fg,
            cursor="hand2",
        )
        row.pack(fill="x", padx=6, pady=1)
        row.bind("<Button-1>", lambda e, idx=i: self._select(idx))
        self._rows.append(row)

    def delete(self, first, last=None) -> None:
        # Listbox semantics: the only call shape is delete(0, tk.END).
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._labels.clear()
        self._overrides.clear()
        self._selected = None

    def itemconfig(self, index, **kwargs) -> None:
        if not isinstance(index, int) or not (0 <= index < len(self._rows)):
            return
        if "text" in kwargs:
            self._labels[index] = str(kwargs["text"])
            # Real content replaces any placeholder styling on the row.
            self._overrides.pop(index, None)
            self._rows[index].configure(text=self._labels[index])
        if "foreground" in kwargs:
            self._overrides[index] = str(kwargs["foreground"])
        self._paint(index)

    def curselection(self):
        return () if self._selected is None else (self._selected,)

    # --- theming ---------------------------------------------------

    def configure(self, cnf=None, **kwargs):
        """Accept CTk options plus the legacy Listbox theming keys
        (bg/fg/selectbackground/selectforeground) the theme pass uses."""
        data = dict(cnf or {})
        data.update(kwargs)
        for old, attr in (("fg", "_row_fg"),
                          ("selectbackground", "_sel_bg"),
                          ("selectforeground", "_sel_fg")):
            if old in data:
                setattr(self, attr, data.pop(old))
        if "bg" in data:
            data["fg_color"] = data.pop("bg")
        if data:
            super().configure(**data)
        for i in range(len(self._rows)):
            self._paint(i)

    config = configure

    def _select(self, index: int) -> None:
        if not (0 <= index < len(self._labels)):
            return
        prev, self._selected = self._selected, index  # browse: click selects
        if prev is not None and prev != index:
            self._paint(prev)
        self._paint(index)

    def select_set(self, index: int) -> None:
        """Move the highlight without a click - the listbox spelling.

        Reordering a row puts it somewhere new, and the highlight has to go
        with it: leaving the selection behind would mean the next action
        applies to whatever row happens to land on the old index.
        """
        self._select(index)

    def _paint(self, i: int) -> None:
        if not (0 <= i < len(self._rows)):
            return
        if i == self._selected:
            self._rows[i].configure(
                fg_color=self._sel_bg, text_color=self._sel_fg)
        elif i in self._overrides:
            self._rows[i].configure(
                fg_color="transparent", text_color=self._overrides[i])
        else:
            self._rows[i].configure(
                fg_color="transparent", text_color=self._row_fg)
