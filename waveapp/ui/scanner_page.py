"""Scanner tab — "Wave's mind" (Phase 8.7, SPEC.md §5 tab 3).

Top: the NeuralCanvas — an AI-brain particle network that fires on real
scanner events (scan pulse, candidate found BLUE, gate-rejected RED with the
reason floating away, promoted GREEN burst). Below: the live candidate table
from Phase 7, restyled onto a white card. A HEURISTIC/ML chip shows which
ranker is thinking (heuristic until Phase 10 trains the ML ranker, §9)."""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import (
    QAbstractTableModel,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme
from waveapp.ui.neural_canvas import NeuralCanvas
from waveapp.ui.positions_page import _Dot as _PageDot


class NewsTicker(QWidget):
    """NYSE-entrance LED board (2026-09-01 v2): authentic 5×7
    dot-matrix glyphs (the classic GLCD font, public domain) — every letter
    is REAL LED dots, no text rasterization. Headlines join a QUEUE and
    enter from the right edge one after another; finished items rotate to
    the back of the queue, so the tape never jumps or teleports. Items come
    only from the live Benzinga/EDGAR stream (or the Test tab's DEMO items).
    Timer runs only while visible (8GB discipline)."""

    SPEED = 0.85  # LED columns per frame (a bit faster)
    CELL = 3  # px per LED
    ROWS = 7  # the font is 5×7 — authentic board height

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.ROWS * self.CELL + 8)
        self._items: list[tuple[str, int]] = []  # (text, direction) — the queue
        self._offset = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        # LAG FIX (blueprint 9.3): every item's dots are rendered ONCE into a
        # pixmap; each frame is 1-3 blits instead of thousands of antialiased
        # ellipse calls. Rotating an item to the back reuses its pixmap.
        self._item_pix: list[QPixmap] = []
        self._item_cols: list[int] = []  # column width of each queued item
        self._bg: QPixmap | None = None

    # -- queue --------------------------------------------------------------

    def add_news(self, item: dict) -> None:
        direction = int(item.get("direction", 0))
        arrow = "\u25b2" if direction > 0 else ("\u25bc" if direction < 0 else "\u2022")
        headline = str(item.get("headline", ""))
        if len(headline) > 150:  # cut at a WORD, never mid-sentence-looking
            headline = headline[:150].rsplit(" ", 1)[0] + "\u2026"
        text = f"{item.get('symbol', '')} {arrow} {headline}"
        self._items.append((text, direction))
        self._append_strip(text, direction)
        if len(self._items) > 40:
            self._trim_front(force=True)
        if not self._timer.isActive() and self.isVisible():
            self._timer.start()

    def clear(self) -> None:
        """Stop the tape and wipe it (Test-tab clear button)."""
        self._timer.stop()
        self._items.clear()
        self._item_cols.clear()
        self._item_pix.clear()
        self._offset = 0.0
        self.update()

    # -- LED strip ----------------------------------------------------------

    def _glyph_cols(self, text: str) -> list[int]:
        cols: list[int] = []
        for ch in text.upper():
            glyph = _FONT5X7.get(ch, _FONT5X7.get("?", (0, 0, 0, 0, 0)))
            cols.extend(glyph)
            cols.append(0)  # inter-character gap
        cols.extend([0] * 10)  # gap after the item
        return cols

    def _cols_to_mask(self, cols: list[int]) -> np.ndarray:
        arr = np.array(cols, dtype=np.uint8)
        return ((arr[None, :] >> np.arange(self.ROWS)[:, None]) & 1).astype(bool)

    def _append_strip(self, text: str, direction: int) -> None:
        mask = self._cols_to_mask(self._glyph_cols(text))
        self._item_cols.append(mask.shape[1])
        self._item_pix.append(self._render_item(mask, direction))

    def _render_item(self, mask: np.ndarray, direction: int) -> QPixmap:
        """Paint one headline's LED dots ONCE (retina-sharp); frames blit it."""
        colors = {
            1: QColor(theme.GREEN),
            -1: QColor(theme.RED),
            0: QColor(235, 235, 240, 220),
        }
        dpr = float(self.devicePixelRatioF() or 1.0)
        width = mask.shape[1] * self.CELL
        height = self.ROWS * self.CELL + 8
        pix = QPixmap(int(width * dpr), int(height * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colors.get(direction, colors[0]))
        rows, cols = np.nonzero(mask)
        for row, col in zip(rows.tolist(), cols.tolist(), strict=True):
            painter.drawEllipse(QRectF(col * self.CELL + 0.35, row * self.CELL + 4.35, 2.4, 2.4))
        painter.end()
        return pix

    def _rebuild_strip(self) -> None:
        """Full rebuild from the queue (clear/font changes/tests)."""
        self._item_cols = []
        self._item_pix = []
        for text, direction in self._items:
            self._append_strip(text, direction)

    def _trim_front(self, force: bool = False) -> None:
        """Drop the head item once it has fully exited the left edge — and
        rotate it to the back of the queue so the tape flows forever. The
        rotated item keeps its rendered pixmap (no re-render)."""
        if not self._item_cols:
            return
        view_cols = max(self.width() // self.CELL, 1)
        head = self._item_cols[0]
        if force or self._offset - view_cols >= head:
            text, direction = self._items.pop(0)
            self._item_cols.pop(0)
            pix = self._item_pix.pop(0) if self._item_pix else None
            self._offset = max(self._offset - head, 0.0)
            if not force:  # rotate finished headlines to the back
                self._items.append((text, direction))
                if pix is not None:
                    self._item_cols.append(head)
                    self._item_pix.append(pix)
                else:
                    self._append_strip(text, direction)

    def _tick(self) -> None:
        self._offset += self.SPEED
        self._trim_front()
        self.update()

    # -- painting -----------------------------------------------------------

    def _paint_bg(self) -> QPixmap:
        dpr = float(self.devicePixelRatioF() or 1.0)
        pix = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(QColor("#141417"))
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 13))
        cols = self.width() // self.CELL
        for col in range(cols):
            for row in range(self.ROWS):
                painter.drawEllipse(QRectF(col * self.CELL + 0.6, row * self.CELL + 4.6, 1.9, 1.9))
        painter.end()
        return pix

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._bg = None

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._items:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)  # tape keeps rolling hidden

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._bg is None or self._bg.deviceIndependentSize().toSize() != self.size():
            self._bg = self._paint_bg()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._bg)
        if not self._item_pix:
            painter.end()
            return
        # each frame = a few pixmap blits at a sub-column (smooth) offset
        view_cols = self.width() // self.CELL
        x = (view_cols - self._offset) * self.CELL  # head item's left edge, px
        width = float(self.width())
        for cols, pix in zip(self._item_cols, self._item_pix, strict=False):
            item_w = cols * self.CELL
            if x + item_w > 0 and x < width:
                painter.drawPixmap(QPointF(x, 0.0), pix)
            x += item_w
            if x >= width:
                break
        painter.end()


# the classic 5×7 GLCD dot-matrix font (public domain; LSB = top row),
# ASCII 0x20-0x5A + the tape's arrows and bullet
_FONT5X7: dict[str, tuple[int, int, int, int, int]] = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00),
    "!": (0x00, 0x00, 0x5F, 0x00, 0x00),
    '"': (0x00, 0x07, 0x00, 0x07, 0x00),
    "#": (0x14, 0x7F, 0x14, 0x7F, 0x14),
    "$": (0x24, 0x2A, 0x7F, 0x2A, 0x12),
    "%": (0x23, 0x13, 0x08, 0x64, 0x62),
    "&": (0x36, 0x49, 0x56, 0x20, 0x50),
    "'": (0x00, 0x00, 0x07, 0x00, 0x00),
    "(": (0x00, 0x1C, 0x22, 0x41, 0x00),
    ")": (0x00, 0x41, 0x22, 0x1C, 0x00),
    "*": (0x2A, 0x1C, 0x7F, 0x1C, 0x2A),
    "+": (0x08, 0x08, 0x3E, 0x08, 0x08),
    ",": (0x00, 0x80, 0x70, 0x30, 0x00),
    "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    ".": (0x00, 0x00, 0x60, 0x60, 0x00),
    "/": (0x20, 0x10, 0x08, 0x04, 0x02),
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E),
    "1": (0x00, 0x42, 0x7F, 0x40, 0x00),
    "2": (0x72, 0x49, 0x49, 0x49, 0x46),
    "3": (0x21, 0x41, 0x49, 0x4D, 0x33),
    "4": (0x18, 0x14, 0x12, 0x7F, 0x10),
    "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x31),
    "7": (0x41, 0x21, 0x11, 0x09, 0x07),
    "8": (0x36, 0x49, 0x49, 0x49, 0x36),
    "9": (0x46, 0x49, 0x49, 0x29, 0x1E),
    ":": (0x00, 0x00, 0x14, 0x00, 0x00),
    ";": (0x00, 0x40, 0x34, 0x00, 0x00),
    "<": (0x00, 0x08, 0x14, 0x22, 0x41),
    "=": (0x14, 0x14, 0x14, 0x14, 0x14),
    ">": (0x41, 0x22, 0x14, 0x08, 0x00),
    "?": (0x02, 0x01, 0x59, 0x09, 0x06),
    "@": (0x3E, 0x41, 0x5D, 0x59, 0x4E),
    "A": (0x7C, 0x12, 0x11, 0x12, 0x7C),
    "B": (0x7F, 0x49, 0x49, 0x49, 0x36),
    "C": (0x3E, 0x41, 0x41, 0x41, 0x22),
    "D": (0x7F, 0x41, 0x41, 0x41, 0x3E),
    "E": (0x7F, 0x49, 0x49, 0x49, 0x41),
    "F": (0x7F, 0x09, 0x09, 0x09, 0x01),
    "G": (0x3E, 0x41, 0x41, 0x51, 0x73),
    "H": (0x7F, 0x08, 0x08, 0x08, 0x7F),
    "I": (0x00, 0x41, 0x7F, 0x41, 0x00),
    "J": (0x20, 0x40, 0x41, 0x3F, 0x01),
    "K": (0x7F, 0x08, 0x14, 0x22, 0x41),
    "L": (0x7F, 0x40, 0x40, 0x40, 0x40),
    "M": (0x7F, 0x02, 0x1C, 0x02, 0x7F),
    "N": (0x7F, 0x04, 0x08, 0x10, 0x7F),
    "O": (0x3E, 0x41, 0x41, 0x41, 0x3E),
    "P": (0x7F, 0x09, 0x09, 0x09, 0x06),
    "Q": (0x3E, 0x41, 0x51, 0x21, 0x5E),
    "R": (0x7F, 0x09, 0x19, 0x29, 0x46),
    "S": (0x26, 0x49, 0x49, 0x49, 0x32),
    "T": (0x03, 0x01, 0x7F, 0x01, 0x03),
    "U": (0x3F, 0x40, 0x40, 0x40, 0x3F),
    "V": (0x1F, 0x20, 0x40, 0x20, 0x1F),
    "W": (0x3F, 0x40, 0x38, 0x40, 0x3F),
    "X": (0x63, 0x14, 0x08, 0x14, 0x63),
    "Y": (0x03, 0x04, 0x78, 0x04, 0x03),
    "Z": (0x61, 0x59, 0x49, 0x4D, 0x43),
    "\u25b2": (0x40, 0x70, 0x7C, 0x70, 0x40),  # ▲
    "\u25bc": (0x01, 0x07, 0x1F, 0x07, 0x01),  # ▼
    "\u2022": (0x00, 0x1C, 0x1C, 0x1C, 0x00),  # •
}


_COLUMNS = ("Symbol", "Price · Day %", "RVOL", "Score", "Strategy", "Spread %", "Decision")
_COL_SYMBOL, _COL_PRICE, _COL_RVOL, _COL_SCORE, _COL_STRATEGY, _COL_SPREAD, _COL_DECISION = range(
    len(_COLUMNS)
)

# radar tiers (2026-09-03, the approved design): menu names on top,
# gate-accepted watchers next, everything else folded away until asked for
_TIER_MENU, _TIER_WATCH, _TIER_REST = range(3)
_TIER_TITLES = {_TIER_MENU: "ON THE MENU", _TIER_WATCH: "WORTH WATCHING", _TIER_REST: "THE REST"}

# inline mini-bars: RVOL capped at 10× (a 12× runner reads the same as a 40×
# halt-resume freak), score capped at the heuristic's practical ~5.0 ceiling
_BAR_CELLS = 6
_RVOL_BAR_CAP = 10.0
_SCORE_BAR_CAP = 5.0

# how many of each outcome get animated per scan cycle (the table shows all;
# the mind highlights a train of thought, not four hundred flashes)
_ANIMATE_ACCEPTED = 4
_ANIMATE_REJECTED = 5


def _bar(value: float, cap: float) -> str:
    """A tiny unicode meter — text-only, so data() stays allocation-cheap."""
    filled = int(round(max(0.0, min(float(value), cap)) / cap * _BAR_CELLS))
    return "▰" * filled + "▱" * (_BAR_CELLS - filled)


class _CandidatesModel(QAbstractTableModel):
    """Virtualized RADAR candidates table.

    Virtualization (2026-09-02: 'it lags the universe and the tape
    for 2 secs'): the old QTableWidget rebuilt 1,500 × 8 = 12,000 widget
    items on the UI thread every scan. A model materializes ONLY the visible
    cells — a full-set refresh is ONE reset signal, and formatting happens
    lazily per painted cell.

    Radar tiers (2026-09-03): one model computes a display list of painted
    section rows + candidate rows — ON THE MENU (scanner2's live menu),
    WORTH WATCHING (gate-accepted), THE REST (collapsed behind a
    'Show all N' toggle; its rows are never materialized while folded).
    Search text and the chip filters prune rows before tiering; header-click
    sorting reorders WITHIN each tier, never across the tier boundaries."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []
        self._menu: set[str] = set()
        self._search = ""
        self._chip = "all"  # all | menu | accepted | rejected
        self._sort_col: int | None = None
        self._sort_desc = False
        self._rest_expanded = False
        # display entries: ("header", title, tier) | ("toggle", count, tier)
        # | ("row", row_dict, tier)
        self._display: list[tuple] = []
        self._section_rows: list[int] = []  # header + toggle indices (spans)
        self._menu_rows: list[int] = []  # menu-tier row indices (taller)

    # -- feed / interaction API ---------------------------------------------

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = list(rows)
        self._rebuild()

    def set_menu(self, symbols: list[str]) -> None:
        menu = {str(s).upper() for s in symbols if s}
        if menu != self._menu:
            self._menu = menu
            self._rebuild()

    def set_search(self, text: str) -> None:
        text = str(text).strip().upper()
        if text != self._search:
            self._search = text
            self._rebuild()

    def set_chip(self, chip: str) -> None:
        if chip != self._chip:
            self._chip = chip
            self._rebuild()

    def toggle_rest(self) -> None:
        self._rest_expanded = not self._rest_expanded
        self._rebuild()

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        self._sort_col = None if column is None or column < 0 else int(column)
        self._sort_desc = order == Qt.SortOrder.DescendingOrder
        self._rebuild()

    def entry_kind(self, row: int) -> str:
        return self._display[row][0] if 0 <= row < len(self._display) else ""

    def row_dict(self, row: int) -> dict | None:
        """Display row → its candidate dict (None on section/toggle rows)."""
        if 0 <= row < len(self._display) and self._display[row][0] == "row":
            return self._display[row][1]
        return None

    def layout_hints(self) -> tuple[list[int], list[int]]:
        """(full-width section-row indices, menu-tier row indices) for the
        view's post-reset decor pass — only the few special rows, never the
        ~1,500 virtualized ones."""
        return list(self._section_rows), list(self._menu_rows)

    # -- classification helpers ----------------------------------------------

    @staticmethod
    def _accepted(row: dict) -> bool:
        return str(row.get("decision", "")).startswith("✓")

    @staticmethod
    def _spread_pct(row: dict) -> float | None:
        """Spread as % of price (the cost number that actually compares
        across a $5 gapper and a $500 large-cap)."""
        try:
            spread = float(row.get("spread") or 0.0)
            price = float(row.get("price") or 0.0)
            if price > 0:
                return spread / price * 100.0
            if row.get("spread_pct") is not None:
                return float(row["spread_pct"])
        except (TypeError, ValueError):
            pass
        return None

    def _match(self, row: dict) -> bool:
        if self._search and self._search not in str(row.get("symbol", "")).upper():
            return False
        if self._chip == "menu":
            return str(row.get("symbol", "")).upper() in self._menu
        if self._chip == "accepted":
            return self._accepted(row)
        if self._chip == "rejected":
            return not self._accepted(row)
        return True

    def _sort_key(self, row: dict):
        col = self._sort_col
        if col == _COL_SYMBOL:
            return str(row.get("symbol", ""))
        if col == _COL_STRATEGY:
            return str(row.get("strategy", ""))
        if col == _COL_DECISION:
            return float(self._accepted(row))
        try:
            if col == _COL_PRICE:  # the movement is the signal, not the tag
                return float(row.get("day_pct") or 0.0)
            if col == _COL_RVOL:
                return float(row.get("rvol") or 0.0)
            if col == _COL_SCORE:
                return float(row.get("score") or 0.0)
            if col == _COL_SPREAD:
                return self._spread_pct(row) or 0.0
        except (TypeError, ValueError):
            return 0.0
        return 0.0

    # -- the one reset ---------------------------------------------------------

    def _rebuild(self) -> None:
        """Full refresh = ONE model reset (the 2026-09-02 lag fix stands)."""
        self.beginResetModel()
        tiers: tuple[list[dict], ...] = ([], [], [])
        for row in self._rows:
            if not self._match(row):
                continue
            if str(row.get("symbol", "")).upper() in self._menu:
                tiers[_TIER_MENU].append(row)
            elif self._accepted(row):
                tiers[_TIER_WATCH].append(row)
            else:
                tiers[_TIER_REST].append(row)
        # a live search (or the Rejected chip) forces THE REST open — hiding
        # the very rows being hunted would be a dead end
        forced_open = bool(self._search) or self._chip == "rejected"
        display: list[tuple] = []
        self._section_rows = []
        self._menu_rows = []
        for tier, rows in enumerate(tiers):
            if not rows:
                continue
            self._section_rows.append(len(display))
            display.append(("header", f"{_TIER_TITLES[tier]}  ·  {len(rows)}", tier))
            if tier == _TIER_REST:
                if not forced_open:
                    self._section_rows.append(len(display))
                    display.append(("toggle", len(rows), tier))
                if not (self._rest_expanded or forced_open):
                    continue  # collapsed: THE REST never materializes
            if self._sort_col is not None:
                rows = sorted(rows, key=self._sort_key, reverse=self._sort_desc)
            for row in rows:
                if tier == _TIER_MENU:
                    self._menu_rows.append(len(display))
                display.append(("row", row, tier))
        self._display = display
        self.endResetModel()

    # -- Qt model --------------------------------------------------------------

    def rowCount(self, parent=None) -> int:  # noqa: N802
        return len(self._display)

    def columnCount(self, parent=None) -> int:  # noqa: N802
        return len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._display)):
            return None
        kind, payload, tier = self._display[index.row()]
        col = index.column()
        if kind == "header":
            if role == Qt.ItemDataRole.DisplayRole and col == 0:
                return payload
            if role == Qt.ItemDataRole.ForegroundRole:
                return QColor(theme.TEXT_MUTED)
            if role == Qt.ItemDataRole.FontRole:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ItemDataRole.BackgroundRole:
                return QColor(0, 0, 0, 10)
            return None
        if kind == "toggle":
            if role == Qt.ItemDataRole.DisplayRole and col == 0:
                if self._rest_expanded:
                    return f"Hide {payload:,} ▾"
                return f"Show all {payload:,} ▸"
            if role == Qt.ItemDataRole.ForegroundRole:
                return QColor(theme.BLUE)
            return None
        row = payload
        accepted = self._accepted(row)
        if role == Qt.ItemDataRole.DisplayRole:
            try:
                if col == _COL_SYMBOL:
                    # S4: SELL-side candidates carry a ↓ marker — rows without
                    # a "dir" tag (the whole long book) render unchanged
                    if str(row.get("dir", "")) == "short":
                        return f"↓ {row.get('symbol', '')}"
                    return str(row.get("symbol", ""))
                if col == _COL_PRICE:
                    return self._price_text(row)
                if col == _COL_RVOL:
                    rvol = float(row.get("rvol") or 0.0)
                    return f"{rvol:.2f}× {_bar(rvol, _RVOL_BAR_CAP)}"
                if col == _COL_SCORE:
                    score = float(row.get("score") or 0.0)
                    return f"{score:.2f} {_bar(score, _SCORE_BAR_CAP)}"
                if col == _COL_STRATEGY:
                    return str(row.get("strategy", ""))
                if col == _COL_SPREAD:
                    spread_pct = self._spread_pct(row)
                    return "—" if spread_pct is None else f"{spread_pct:.2f}%"
                if col == _COL_DECISION:
                    return "✓" if accepted else "✗"
            except (TypeError, ValueError):
                return ""
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row, accepted)
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == _COL_DECISION:
                return QColor(theme.GREEN if accepted else theme.TEXT_MUTED)
            if not accepted:  # rejected rows read at ~60 % ink
                return QColor(29, 29, 31, 153)
            if col == _COL_PRICE:
                try:
                    day = float(row["day_pct"])
                except (KeyError, TypeError, ValueError):
                    return None
                if day > 0:
                    return QColor(theme.GREEN)
                if day < 0:
                    return QColor(theme.RED)
            return None
        if role == Qt.ItemDataRole.BackgroundRole and tier == _TIER_MENU:
            return QColor(0, 122, 255, 15)  # rgba(0,122,255,0.06) wash
        return None

    # -- lazy per-cell formatting helpers -------------------------------------

    @staticmethod
    def _price_text(row: dict) -> str:
        parts = []
        try:
            parts.append(f"${float(row['price']):,.2f}")
        except (KeyError, TypeError, ValueError):
            pass
        try:
            parts.append(f"{float(row['day_pct']):+.2f}%")
        except (KeyError, TypeError, ValueError):
            pass
        return "  ".join(parts) if parts else "—"

    @staticmethod
    def _tooltip(row: dict, accepted: bool) -> str:
        def num(key: str, fmt: str) -> str:
            try:
                return fmt.format(float(row[key]))
            except (KeyError, TypeError, ValueError):
                return "—"

        lines = [f"Gap {num('gap_pct', '{:+.2f}')}%  ·  ATR {num('atr_pct', '{:.2f}')}%"]
        if not accepted:
            lines.append(f"Rejected: {row.get('decision', '')}")
        return "\n".join(lines)


class CandidatePopup(QWidget):
    """Double-clicked candidate row: a solid card with the full scan data,
    including the complete gate decision text (round 2)."""

    def __init__(self, window: QWidget, row: dict) -> None:
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setObjectName("candidateOverlay")
        self.setStyleSheet("#candidateOverlay { background: rgba(0, 0, 0, 0.22); }")
        self.setGeometry(window.rect())
        self.card = QFrame(self)
        self.card.setObjectName("candidateCard")
        self.card.setStyleSheet(
            "#candidateCard { background: #FFFFFF;"
            " border: 1px solid rgba(0, 0, 0, 0.12); border-radius: 14px; }"
        )
        self.card.setFixedSize(380, 300)
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(6)

        accepted = str(row.get("decision", "")).startswith("✓")
        header = QHBoxLayout()
        symbol = QLabel(str(row.get("symbol", "")))
        symbol.setStyleSheet(
            f"font-size: 22px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        verdict = QLabel("ACCEPTED" if accepted else "REJECTED")
        verdict.setStyleSheet(
            f"color: {'#FFFFFF'}; background: {theme.GREEN if accepted else theme.RED};"
            f" border-radius: 8px; padding: 2px 10px; font-size: 10px; font-weight: 700;"
        )
        header.addWidget(symbol)
        header.addWidget(verdict)
        header.addStretch(1)
        layout.addLayout(header)

        rows = (
            ("Score", f"{float(row.get('score', 0)):.2f}"),
            ("Best strategy", str(row.get("strategy", "—"))),
            ("Relative volume", f"{float(row.get('rvol', 0)):.2f}×"),
            ("Gap", f"{float(row.get('gap_pct', 0)):+.2f}%"),
            ("ATR", f"{float(row.get('atr_pct', 0)):.2f}%"),
            ("Spread", f"${float(row.get('spread', 0)):.3f}"),
        )
        for name, value in rows:
            line = QHBoxLayout()
            key = QLabel(name)
            key.setStyleSheet(
                f"font-size: 12px; color: {theme.TEXT_MUTED}; background: transparent;"
            )
            val = QLabel(value)
            val.setStyleSheet(
                f"font-size: 12px; font-weight: 600; color: {theme.TEXT}; background: transparent;"
            )
            line.addWidget(key)
            line.addStretch(1)
            line.addWidget(val)
            layout.addLayout(line)

        decision = QLabel(str(row.get("decision", "")))
        decision.setWordWrap(True)
        decision.setMinimumHeight(40)
        decision.setStyleSheet(
            f"font-size: 12px; color: {theme.GREEN if accepted else theme.RED};"
            f" background: transparent;"
        )
        layout.addWidget(decision)
        layout.addStretch(1)
        self.card.move(
            (self.width() - self.card.width()) // 2,
            (self.height() - self.card.height()) // 2,
        )
        from waveapp.ui.popup_fx import pop_in

        pop_in(self, self.card)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.card.geometry().contains(event.position().toPoint()):
            self.close()


class ScannerPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel("Wave's mind")
        # bright white like the tab-bar tabs — cardTitle ink vanished on glass
        title.setStyleSheet(
            "font-size: 17px; font-weight: 700;"
            " color: rgba(255, 255, 255, 235); background: transparent;"
        )
        header.addWidget(title)
        self.mode_chip = QLabel("HEURISTIC RANKER")
        self.mode_chip.setStyleSheet(
            f"color: {theme.BLUE}; background: rgba(0, 122, 255, 0.10);"
            f" border-radius: 8px; padding: 2px 10px; font-size: 10px; font-weight: 700;"
        )
        header.addWidget(self.mode_chip)
        # DAY JUDGE badge (R0, 2026-09-23) — what KIND of day the tape is
        # printing (menu breadth verdict), next to the ranker chip
        self.day_badge = QLabel()
        self._style_day_badge("UNCLEAR", 0.0)
        header.addWidget(self.day_badge)
        # 4.1 MarketRegime chip — the tape's own weather report, colored by
        # what kind of day the internals say it is
        self.regime_chip = QLabel("")
        self.regime_chip.hide()
        header.addWidget(self.regime_chip)
        header.addStretch(1)
        # scan-set build indicator (2026-08-20): a small LIVE WAVE +
        # counting numbers while batches download; "ready ✓" when done
        from waveapp.ui.wave_logo import LogoState, WaveLogo

        self.build_wave = WaveLogo()
        self.build_wave.setFixedSize(58, 22)
        self.build_wave.mirror = True  # login-style: flows LEFT → RIGHT
        self.build_wave.set_state(LogoState.ENERGETIC)
        self.build_wave.hide()
        header.addWidget(self.build_wave)
        self.status_label = QLabel("waiting for the first scan…")
        self.status_label.setProperty("muted", True)
        header.addWidget(self.status_label)
        self._last_real_status = "waiting for the first scan…"
        layout.addLayout(header)

        # Scanner 2.0 live menu strip (2026-09-01: "the Scanner tab
        # becomes a live feed") — the top of the full-market menu, refreshed
        # every minute while the market is open
        self.menu_label = QLabel("")
        self.menu_label.setProperty("muted", True)
        self.menu_label.setWordWrap(True)
        self.menu_label.setStyleSheet("font-size: 12px; color: #6E6E73;")
        layout.addWidget(self.menu_label)

        # the NYSE tape — live classified headlines, above the mind
        self.ticker = NewsTicker()
        layout.addWidget(self.ticker)

        # the mind sits on a subtle dark tint so the glow reads on glass
        mind_frame = QFrame()
        mind_frame.setObjectName("mindFrame")
        mind_frame.setStyleSheet(
            "#mindFrame { background: rgba(0, 0, 0, 0.045);"
            " border: 1px solid rgba(0, 0, 0, 0.10); border-radius: 14px; }"
        )
        mind_layout = QVBoxLayout(mind_frame)
        mind_layout.setContentsMargins(2, 2, 2, 2)
        self.mind = NeuralCanvas()
        mind_layout.addWidget(self.mind)
        # two slideable pages (2026-09-01): page 1 = the universe
        # animation, BIG; page 2 = the candidate table, full spread
        self.page_host = QWidget()
        layout.addWidget(self.page_host, stretch=1)
        self._page_one = QWidget(self.page_host)
        page_one_layout = QVBoxLayout(self._page_one)
        page_one_layout.setContentsMargins(0, 0, 0, 0)
        page_one_layout.addWidget(mind_frame, stretch=1)

        table_card = QFrame()
        table_card.setProperty("card", True)
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(8, 8, 8, 8)
        table_layout.setSpacing(6)
        from PyQt6.QtWidgets import (
            QAbstractItemView,
            QButtonGroup,
            QHeaderView,
            QLineEdit,
            QPushButton,
            QTableView,
        )

        self.candidates_model = _CandidatesModel()

        # radar filter row (2026-09-03): symbol search + flat filter chips
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search symbol")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(180)
        self.search_box.textChanged.connect(self.candidates_model.set_search)
        filter_row.addWidget(self.search_box)
        chip_qss = (
            f"QPushButton {{ background: transparent; border: 1px solid {theme.BORDER};"
            f" border-radius: 11px; padding: 3px 12px; font-size: 11px; font-weight: 600;"
            f" color: {theme.TEXT_MUTED}; }}"
            f"QPushButton:checked {{ background: rgba(0, 122, 255, 0.12);"
            f" border: 1px solid transparent; color: {theme.BLUE}; }}"
        )
        self._chip_group = QButtonGroup(self)  # exclusive by default
        self.chip_buttons: dict[str, QPushButton] = {}
        for key, label in (
            ("all", "All"),
            ("menu", "Menu"),
            ("accepted", "Accepted"),
            ("rejected", "Rejected"),
        ):
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(chip_qss)
            chip.clicked.connect(lambda _checked=False, k=key: self.candidates_model.set_chip(k))
            self._chip_group.addButton(chip)
            self.chip_buttons[key] = chip
            filter_row.addWidget(chip)
        self.chip_buttons["all"].setChecked(True)
        filter_row.addStretch(1)
        table_layout.addLayout(filter_row)

        self.table = QTableView()
        self.table.setModel(self.candidates_model)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # round 2: no click-highlight — double-click opens the details
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(False)
        self.table.doubleClicked.connect(lambda ix: self._open_candidate(ix.row(), ix.column()))
        # single click only acts on the 'Show all N' toggle row
        self.table.clicked.connect(self._on_table_clicked)
        self._rows: list[dict] = []
        # resize audit: every column stretches to the available width — the
        # table always fits, never grows a horizontal scrollbar (§5)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setShowGrid(False)
        self.table.setStyleSheet(
            "QTableView { background: transparent; border: none; }"
            f"QHeaderView::section {{ background: transparent; border: none;"
            f" border-bottom: 1px solid {theme.BORDER}; padding: 6px;"
            f" font-weight: 600; color: {theme.TEXT_MUTED}; }}"
        )
        # header-click sorting (tier-aware, handled inside the model);
        # start unsorted = the scanner's own rank order
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        # section spans + menu-row heights re-apply after every model reset
        self.candidates_model.modelReset.connect(self._decorate_sections)
        table_layout.addWidget(self.table)
        self._page_two = QWidget(self.page_host)
        page_two_layout = QVBoxLayout(self._page_two)
        page_two_layout.setContentsMargins(0, 0, 0, 0)
        page_two_layout.addWidget(table_card, stretch=1)
        self._page_two.hide()
        self._pages = [self._page_one, self._page_two]
        self._page_index = 0
        dots_row = QHBoxLayout()
        dots_row.addStretch(1)
        self.page_dots: list[_PageDot] = []
        for index in range(len(self._pages)):
            dot = _PageDot(index)
            dot.clicked.connect(self.set_scanner_page)
            self.page_dots.append(dot)
            dots_row.addWidget(dot)
        dots_row.addStretch(1)
        layout.addLayout(dots_row)
        self._refresh_page_dots()

    # -- feeds ----------------------------------------------------------------

    REGIME_COLORS = {
        "TREND_UP": ("#28CD41", "rgba(40, 205, 65, 0.12)"),
        "TREND_DOWN": ("#FF3B30", "rgba(255, 59, 48, 0.12)"),
        "PANIC": ("#FF3B30", "rgba(255, 59, 48, 0.20)"),
        "ROTATION": ("#FF9500", "rgba(255, 149, 0, 0.12)"),
        "DEAD": ("#8E8E93", "rgba(142, 142, 147, 0.14)"),
        "MIXED": ("#007AFF", "rgba(0, 122, 255, 0.10)"),
    }

    # DAY JUDGE badge (R0): verdict → (text, ink, wash). Colors stay in the
    # theme family: green up, red down, orange chop, gray unclear.
    DAY_BADGE = {
        "TREND_UP": ("📈 TREND ↑", theme.GREEN, "rgba(40, 205, 65, 0.12)"),
        "TREND_DOWN": ("📉 TREND ↓", theme.RED, "rgba(255, 59, 48, 0.12)"),
        "CHOP": ("↔️ CHOP", theme.ORANGE_LIVE, "rgba(255, 149, 0, 0.12)"),
        "UNCLEAR": ("· reading the day…", theme.GREY, "rgba(142, 142, 147, 0.14)"),
    }

    def set_day_regime(self, data: dict) -> None:
        """R0: the Day Judge's verdict badge — rides the same minute refresh
        as the market-regime chip."""
        try:
            verdict = str(data.get("verdict", "UNCLEAR"))
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            verdict, confidence = "UNCLEAR", 0.0
        self._style_day_badge(verdict, confidence)

    def _style_day_badge(self, verdict: str, confidence: float) -> None:
        text, color, bg = self.DAY_BADGE.get(verdict, self.DAY_BADGE["UNCLEAR"])
        if verdict in ("TREND_UP", "TREND_DOWN", "CHOP") and confidence > 0:
            text = f"{text} ({confidence:.1f})"
        self.day_badge.setText(text)
        self.day_badge.setStyleSheet(
            f"color: {color}; background: {bg}; border-radius: 8px;"
            f" padding: 2px 10px; font-size: 10px; font-weight: 700;"
        )
        self.day_badge.setToolTip(
            "Day Judge — live day-type verdict from menu breadth"
            " (fraction of today's menu below its own open)"
        )

    def set_regime(self, internals: dict) -> None:
        """4.1: the market's weather chip — regime + heat score, every minute."""
        day_regime = internals.get("day_regime")  # DAY JUDGE (R0) piggyback
        if isinstance(day_regime, dict):
            self.set_day_regime(day_regime)
        regime = str(internals.get("regime", ""))
        if not regime or regime == "WARMUP":
            self.regime_chip.hide()
            return
        color, bg = self.REGIME_COLORS.get(regime, self.REGIME_COLORS["MIXED"])
        score = float(internals.get("score", 0.0) or 0.0)
        self.regime_chip.setText(f"{regime.replace('_', ' ')} {score:+.2f}")
        self.regime_chip.setStyleSheet(
            f"color: {color}; background: {bg}; border-radius: 8px;"
            f" padding: 2px 10px; font-size: 10px; font-weight: 700;"
        )
        self.regime_chip.setToolTip(
            f"breadth {internals.get('breadth', 0):+.2f} · $vol tilt"
            f" {internals.get('vold', 0):+.2f} · tick {internals.get('tick', 0):+.2f} ·"
            f" {float(internals.get('above_vwap', 0) or 0) * 100:.0f}% above VWAP ·"
            f" median RVOL {internals.get('agg_rvol', 0):.2f} · sector spread"
            f" {internals.get('dispersion', 0):.1f}% · {internals.get('hod_rate', 0):.0f}"
            f" new highs/min"
        )
        self.regime_chip.show()

    def set_status(self, text: str) -> None:
        self._last_real_status = text  # what "clear loader" restores
        self.status_label.setText(text)

    def set_menu(self, symbols: list[str]) -> None:
        """Scanner2's live menu membership → the table's ON THE MENU tier
        (the monitor can call this directly with just the symbol list)."""
        self.candidates_model.set_menu(symbols)

    def update_menu(self, menu: list[dict]) -> None:
        """Scanner 2.0's live menu (top of the full-market rank, 1/min)."""
        self.set_menu([str(m.get("symbol", "")) for m in menu])
        if not menu:
            self.menu_label.setText("")
            return
        parts = [f"{m['symbol']} {m['rvol']:g}× {m['day_pct']:+.1f}%" for m in menu[:10]]
        self.menu_label.setText("Scanner 2.0 menu:  " + "  ·  ".join(parts))

    def restore_status(self) -> None:
        """Back to the last REAL status (2026-08-21: clearing the Test-tab
        loader demo resurrected the obsolete 'allow 2-3 minutes' line)."""
        self.build_wave.hide()
        self.status_label.setText(self._last_real_status)

    def set_build_progress(self, done: int, total: int, label: str) -> None:
        """Universe-build feed (2026-08-20): wave animation + numbers
        counting up while batches land; calm 'ready' line when finished."""
        if total > 0 and done < total:
            self.build_wave.show()
            ranked = min(done, total) * 200  # DAILY_BARS_BATCH symbols/batch
            self.status_label.setText(
                f"building today's scan set — batch {done}/{total} · {ranked:,} symbols ranked…"
            )
        elif total > 0:
            self.build_wave.hide()
            self.status_label.setText("scan set ready ✓")
        else:  # total == 0: idle note (overnight/closed) passes through
            self.build_wave.hide()
            if label:
                self._last_real_status = label
                self.status_label.setText(label)

    def _refresh_page_dots(self) -> None:
        for index, dot in enumerate(self.page_dots):
            dot.active = index == self._page_index
            dot.update()

    def _layout_pages(self) -> None:
        width = self.page_host.width()
        height = self.page_host.height()
        for index, page in enumerate(self._pages):
            if index == self._page_index:
                page.setGeometry(0, 0, width, height)
                page.show()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_pages()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._layout_pages()

    def set_scanner_page(self, index: int) -> None:
        """Dots switch pages with the Positions-tab slide."""
        index = max(0, min(index, len(self._pages) - 1))
        if index == self._page_index:
            return
        from PyQt6 import sip

        direction = 1 if index > self._page_index else -1
        old_page = self._pages[self._page_index]
        new_page = self._pages[index]
        self._page_index = index
        self._refresh_page_dots()
        width = self.page_host.width()
        height = self.page_host.height()
        from PyQt6.QtGui import QGuiApplication

        # offscreen (test) platform: the slide is invisible anyway, and its
        # finished-callback firing into torn-down widgets segfaulted the
        # 2026-09-03 build gate — take the instant path
        headless = QGuiApplication.platformName() == "offscreen"
        if width < 50 or not self.isVisible() or headless:
            old_page.hide()
            new_page.setGeometry(0, 0, width, height)
            new_page.show()
            return
        new_page.setGeometry(direction * width, 0, width, height)
        new_page.show()
        new_page.raise_()
        slide_out = QPropertyAnimation(old_page, b"pos", old_page)
        slide_out.setDuration(260)
        slide_out.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide_out.setEndValue(QPoint(-direction * width, 0))
        slide_in = QPropertyAnimation(new_page, b"pos", new_page)
        slide_in.setDuration(260)
        slide_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide_in.setEndValue(QPoint(0, 0))

        def _done() -> None:
            # 2026-09-03 build segfault: this callback can fire from a LATER
            # event-loop spin after the whole page tree is torn down (seen
            # under the offscreen test platform). Guard everything — a missed
            # hide() is cosmetic; a dead-pointer touch is a crash.
            import contextlib

            with contextlib.suppress(RuntimeError):
                if not sip.isdeleted(self) and not sip.isdeleted(old_page):
                    old_page.hide()

        slide_in.finished.connect(_done)
        slide_out.start()
        slide_in.start()
        self._slide_keep = (slide_out, slide_in)

    def update_candidates(self, rows: list[dict]) -> None:
        from datetime import datetime

        self._rows = list(rows)
        # the mind thinks through this cycle: pulse, then a spaced train of
        # promotions and (sampled) rejections
        self.mind.pulse_scan()
        animated_accepted = 0
        animated_rejected = 0
        for row in rows:
            accepted = str(row["decision"]).startswith("✓")
            if accepted and animated_accepted < _ANIMATE_ACCEPTED:
                self.mind.candidate_found(row["symbol"])
                self.mind.candidate_promoted(row["symbol"])
                animated_accepted += 1
            elif not accepted and animated_rejected < _ANIMATE_REJECTED:
                reason = str(row["decision"])[:34]
                self.mind.candidate_rejected(row["symbol"], reason)
                animated_rejected += 1

        # virtualized refresh (2026-09-02): one model reset, no widget churn
        self.candidates_model.set_rows(self._rows)
        accepted_count = sum(1 for r in rows if str(r["decision"]).startswith("✓"))
        self.status_label.setText(
            f"last scan {datetime.now().strftime('%H:%M:%S')} — "
            f"{len(rows)} candidates, {accepted_count} gate-accepted"
        )

    # -- radar table decor & interactions -------------------------------------

    def _decorate_sections(self) -> None:
        """Post-reset decor: tier headers and the 'Show all' toggle span the
        full width; ON THE MENU rows sit slightly taller. Touches only the
        handful of special rows — never the ~1,500 virtualized ones."""
        self.table.clearSpans()
        section_rows, menu_rows = self.candidates_model.layout_hints()
        columns = self.candidates_model.columnCount()
        # tester BUG-2 (2026-09-03): Qt row heights persist across model
        # resets — restore last pass's special rows to the default height
        # BEFORE decorating, or stale 34px/26px heights land on whatever
        # ordinary row shifts into those display indices next scan
        row_count = self.candidates_model.rowCount()
        for row in getattr(self, "_decorated_rows", ()):
            if row < row_count:
                self.table.setRowHeight(row, 28)
        for row in section_rows:
            self.table.setSpan(row, 0, 1, columns)
            self.table.setRowHeight(row, 26)
        for row in menu_rows:
            self.table.setRowHeight(row, 34)
        self._decorated_rows = set(section_rows) | set(menu_rows)

    def _on_table_clicked(self, index) -> None:
        if self.candidates_model.entry_kind(index.row()) == "toggle":
            self.candidates_model.toggle_rest()

    # -- candidate details (round 2: double-click a row) ----------------------

    def _open_candidate(self, row_index: int, _column: int) -> None:
        row = self.candidates_model.row_dict(row_index)
        if row is None:  # section header / toggle rows have no details
            return
        self._candidate_popup = CandidatePopup(self.window(), row)
        self._candidate_popup.show()
        self._candidate_popup.raise_()

    # -- depth panel ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(12.5, 12.5, self.width() - 25, self.height() - 25)
        painter.setPen(QPen(QColor(0, 0, 0, 26), 1))
        painter.setBrush(QColor(0, 0, 0, 13))
        painter.drawRoundedRect(rect, 14, 14)
