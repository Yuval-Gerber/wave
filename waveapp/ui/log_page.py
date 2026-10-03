"""Log tab (Phase 8.9, §5 tab 5): a virtualized view over the
SQLite log.

- full-text search (word or fragment), live with a 250ms debounce;
- category filter (TRADE / ORDER / SCANNER / RISK / SYSTEM / ERROR /
  TRAINING) and level=ERROR shortcut via the ERRORS chip;
- date range via the themed calendar buttons;
- rows load lazily in batches (fetchMore) — hundreds of thousands of rows
  stay smooth;
- multi-select (or "all filtered") → export to a standalone .sqlite, or
  delete (behind a confirm — deletions are destructive).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QRectF,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme
from waveapp.ui.performance_page import _CalendarButton, _detach_native_combo_chrome
from waveapp.ui.positions_page import _ConfirmPopup

logger = logging.getLogger("wave.ui.log")


def _display_ts(iso: str) -> str:
    """DB timestamps are UTC (§3); the table shows the user's LOCAL time
    (2026-08-19: 'the time in the log is wrong')."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d  %H:%M:%S")
    except ValueError:
        return iso[:19].replace("T", "  ")


def _local_day_start_utc(day) -> str:
    """Local midnight of `day` expressed as a UTC ISO string for ts filters."""
    return datetime.combine(day, time.min).astimezone().astimezone(UTC).isoformat()


CATEGORIES = ("All", "TRADE", "ORDER", "SCANNER", "RISK", "SYSTEM", "ERROR", "TRAINING")
_BATCH = 400
_COLUMNS = ("", "Time", "Category", "Level", "Message")  # col 0 = checkbox
_ROOT = QModelIndex()

_LOG_DDL = """CREATE TABLE log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    category     TEXT NOT NULL,
    level        TEXT NOT NULL,
    message      TEXT NOT NULL,
    json_payload TEXT
)"""


class _LogModel(QAbstractTableModel):
    """Lazy pages over `log` — only loaded rows live in memory."""

    def __init__(self) -> None:
        super().__init__()
        self._db = None
        self._rows: list[sqlite3.Row] = []
        self._total = 0
        self._where = ""
        self._params: tuple = ()
        # checkbox state (r2): explicit ids, or "all filtered" minus exceptions
        self._checked: set[int] = set()
        self._all = False
        self._unchecked: set[int] = set()

    # -- filters --------------------------------------------------------------

    def set_database(self, database) -> None:
        self._db = database
        self.apply_filters("", "All", None, None)

    def apply_filters(self, search: str, category: str, start, end) -> None:
        clauses = []
        params: list = []
        if search:
            clauses.append("message LIKE ?")
            params.append(f"%{search}%")
        if category and category != "All":
            clauses.append("category = ?")
            params.append(category)
        # calendar picks are LOCAL days; the DB stores UTC (§3) — convert the
        # local-midnight boundaries to UTC so a day filter matches what the
        # user actually saw on the wall clock (2026-08-19)
        if start is not None:
            clauses.append("ts >= ?")
            params.append(_local_day_start_utc(start))
        if end is not None:
            clauses.append("ts < ?")
            params.append(_local_day_start_utc(end + timedelta(days=1)))
        self._where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        self._params = tuple(params)
        self.beginResetModel()
        self._rows = []
        self._total = 0
        self._checked.clear()
        self._all = False
        self._unchecked.clear()
        if self._db is not None:
            try:
                rows = self._db.query(
                    f"SELECT COUNT(*) AS n FROM log{self._where}",  # noqa: S608 — fixed clauses, bound params
                    self._params,
                )
                self._total = int(rows[0]["n"]) if rows else 0
            except Exception:
                logger.exception("log count failed")
        self.endResetModel()

    @property
    def where_clause(self) -> tuple[str, tuple]:
        return self._where, self._params

    @property
    def total(self) -> int:
        return self._total

    def row_id(self, row: int) -> int | None:
        if 0 <= row < len(self._rows):
            return int(self._rows[row]["id"])
        return None

    # -- checkbox selection (r2) ----------------------------------------------

    def is_checked(self, row_id: int) -> bool:
        if self._all:
            return row_id not in self._unchecked
        return row_id in self._checked

    def selection_count(self) -> int:
        if self._all:
            return max(0, self._total - len(self._unchecked))
        return len(self._checked)

    def whole_filter_selected(self) -> bool:
        return (self._all and not self._unchecked) or (0 < self._total == len(self._checked))

    def selection(self) -> tuple[str, tuple[int, ...]]:
        """("all", excluded_ids) — the whole filter minus exceptions — or
        ("ids", checked_ids)."""
        if self._all:
            return "all", tuple(sorted(self._unchecked))
        return "ids", tuple(sorted(self._checked))

    def check_all(self) -> None:
        self._all = True
        self._checked.clear()
        self._unchecked.clear()
        self._emit_checks_changed()

    def clear_checks(self) -> None:
        self._all = False
        self._checked.clear()
        self._unchecked.clear()
        self._emit_checks_changed()

    def _emit_checks_changed(self) -> None:
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0), [])

    # -- Qt model -------------------------------------------------------------

    def rowCount(self, parent=_ROOT) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=_ROOT) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_COLUMNS)

    def canFetchMore(self, parent=_ROOT) -> bool:  # noqa: N802
        return not parent.isValid() and len(self._rows) < self._total

    def fetchMore(self, parent=_ROOT) -> None:  # noqa: N802
        if parent.isValid() or self._db is None:
            return
        try:
            batch = self._db.query(
                f"SELECT id, ts, category, level, message, json_payload"  # noqa: S608 — fixed clauses, bound params
                f" FROM log{self._where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*self._params, _BATCH, len(self._rows)),
            )
        except Exception:
            logger.exception("log page load failed")
            return
        if not batch:
            self._total = len(self._rows)
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(batch) - 1)
        self._rows.extend(batch)
        self.endInsertRows()

    def headerData(self, section, orientation, role):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _COLUMNS[section]
        return None

    def flags(self, index):  # noqa: N802
        base = Qt.ItemFlag.ItemIsEnabled
        if index.column() == 0:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
            return False
        row_id = self.row_id(index.row())
        if row_id is None:
            return False
        checked = Qt.CheckState(value) == Qt.CheckState.Checked
        if self._all:
            self._unchecked.discard(row_id) if checked else self._unchecked.add(row_id)
        else:
            self._checked.add(row_id) if checked else self._checked.discard(row_id)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            return (
                Qt.CheckState.Checked
                if self.is_checked(int(row["id"]))
                else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 1:
                return _display_ts(str(row["ts"]))
            if column == 2:
                return row["category"]
            if column == 3:
                return row["level"]
            if column == 4:
                return row["message"]
            return None
        if role == Qt.ItemDataRole.ForegroundRole:
            if row["level"] == "ERROR":
                return QColor(theme.RED)
            if row["level"] == "WARNING":
                return QColor(theme.ORANGE_LIVE)
            if column != 4:
                return QColor(theme.TEXT_MUTED)
            return QColor(theme.TEXT)
        return None

    def row_dict(self, row: int) -> dict | None:
        if 0 <= row < len(self._rows):
            return dict(self._rows[row])
        return None


class LogEntryPopup(QWidget):
    """Double-clicked log row (r2): full timestamp, category/level, the whole
    message (selectable), and the JSON payload when one exists."""

    def __init__(self, window: QWidget, row: dict) -> None:
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setObjectName("logEntryOverlay")
        self.setStyleSheet("#logEntryOverlay { background: rgba(0, 0, 0, 0.22); }")
        self.setGeometry(window.rect())
        self.card = QFrame(self)
        self.card.setObjectName("logEntryCard")
        self.card.setStyleSheet(
            "#logEntryCard { background: #FFFFFF;"
            " border: 1px solid rgba(0, 0, 0, 0.12); border-radius: 14px; }"
        )
        self.card.setFixedSize(520, 340)
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(8)

        level = str(row.get("level", ""))
        level_color = {
            "ERROR": theme.RED,
            "WARNING": theme.ORANGE_LIVE,
        }.get(level, theme.TEXT_MUTED)
        header = QHBoxLayout()
        header.setSpacing(8)
        category = QLabel(str(row.get("category", "")))
        category.setStyleSheet(
            f"color: {theme.BLUE}; background: rgba(0, 122, 255, 0.10);"
            f" border-radius: 8px; padding: 2px 10px; font-size: 10px; font-weight: 700;"
        )
        level_chip = QLabel(level)
        level_chip.setStyleSheet(
            f"color: #FFFFFF; background: {level_color};"
            f" border-radius: 8px; padding: 2px 10px; font-size: 10px; font-weight: 700;"
        )
        stamp = QLabel(_display_ts(str(row.get("ts", ""))))
        stamp.setStyleSheet(
            f"font-family: Menlo, monospace; font-size: 11px; color: {theme.TEXT_MUTED};"
            f" background: transparent;"
        )
        header.addWidget(category)
        header.addWidget(level_chip)
        header.addStretch(1)
        header.addWidget(stamp)
        layout.addLayout(header)

        message = QLabel(str(row.get("message", "")))
        message.setWordWrap(True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        message.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        message.setStyleSheet(f"font-size: 13px; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(message, stretch=2)

        payload = row.get("json_payload")
        if payload:
            import json

            try:
                pretty = json.dumps(json.loads(payload), indent=2)[:1200]
            except (ValueError, TypeError):
                pretty = str(payload)[:1200]
            payload_label = QLabel(pretty)
            payload_label.setWordWrap(True)
            payload_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            payload_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            payload_label.setStyleSheet(
                f"font-family: Menlo, monospace; font-size: 11px; color: {theme.TEXT_MUTED};"
                f" background: rgba(0, 0, 0, 0.03); border-radius: 8px; padding: 8px;"
            )
            layout.addWidget(payload_label, stretch=1)
        layout.addStretch(0)
        self.card.move(
            (self.width() - self.card.width()) // 2,
            (self.height() - self.card.height()) // 2,
        )
        from waveapp.ui.popup_fx import pop_in

        pop_in(self, self.card)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.card.geometry().contains(event.position().toPoint()):
            self.close()


class LogPage(QWidget):
    exported = pyqtSignal(str)  # path (tests / status)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = None
        self._confirm: _ConfirmPopup | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(10)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("search the log — word or fragment…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(280)
        controls.addWidget(self.search)
        self.category = QComboBox()
        for name in CATEGORIES:
            self.category.addItem(name)
        _detach_native_combo_chrome(self.category)
        controls.addWidget(self.category)
        self.from_button = _CalendarButton("start date")
        self.to_button = _CalendarButton("end date")
        controls.addWidget(self.from_button)
        controls.addWidget(self.to_button)
        controls.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet(
            f"font-size: 12px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        controls.addWidget(self.count_label)
        outer.addLayout(controls)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.select_all_button = QPushButton("Select all filtered")
        self.export_button = QPushButton("Export…")
        self.delete_button = QPushButton("Delete")
        self.delete_button.setStyleSheet(
            f"QPushButton {{ color: {theme.RED}; background: rgba(255, 59, 48, 0.09);"
            f" border: 1px solid rgba(255, 59, 48, 0.35); border-radius: 7px;"
            f" padding: 5px 14px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: rgba(255, 59, 48, 0.16); }}"
        )
        for button in (self.select_all_button, self.export_button, self.delete_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        outer.addLayout(actions)

        card = QFrame()
        card.setProperty("card", True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 8, 8, 8)
        self.model = _LogModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        # r2: no click-highlight — checkboxes carry the selection
        self.table.setSelectionMode(QTableView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setShowGrid(False)
        self.table.setColumnWidth(0, 34)
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 80)
        self.table.doubleClicked.connect(self._open_entry)
        check_icon = (Path(__file__).with_name("check.svg")).as_posix()
        self.table.setStyleSheet(
            "QTableView { background: transparent; border: none; }"
            f"QHeaderView::section {{ background: transparent; border: none;"
            f" border-bottom: 1px solid {theme.BORDER}; padding: 6px;"
            f" font-weight: 600; color: {theme.TEXT_MUTED}; }}"
            # r2: Apple-style squares — white box, blue fill + white ✓ when on
            "QTableView::indicator { width: 16px; height: 16px; border-radius: 4px;"
            " border: 1px solid rgba(0, 0, 0, 0.28); background: #FFFFFF; }"
            f"QTableView::indicator:checked {{ background: {theme.BLUE};"
            f" border-color: {theme.BLUE}; image: url({check_icon}); }}"
        )
        card_layout.addWidget(self.table)
        outer.addWidget(card, stretch=1)

        # live search with a debounce; other filters apply instantly
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._apply_filters)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        self.category.currentIndexChanged.connect(lambda _i: self._apply_filters())
        self.from_button.date_selected.connect(lambda _d: self._apply_filters())
        self.to_button.date_selected.connect(lambda _d: self._apply_filters())
        self.select_all_button.clicked.connect(self._toggle_select_all)
        self.export_button.clicked.connect(self._export_clicked)
        self.delete_button.clicked.connect(self._delete_clicked)
        self.model.dataChanged.connect(lambda *_a: self._sync_select_button())
        self.model.modelReset.connect(self._sync_select_button)

    # -- data -----------------------------------------------------------------

    def attach_database(self, database) -> None:
        self._db = database
        self.model.set_database(database)
        self._refresh_count()

    def _apply_filters(self) -> None:
        self.model.apply_filters(
            self.search.text().strip(),
            self.category.currentText(),
            self.from_button.selected,
            self.to_button.selected,
        )
        self._refresh_count()

    def _refresh_count(self) -> None:
        self.count_label.setText(f"{self.model.total:,} entries")

    def _toggle_select_all(self) -> None:
        """r2: one button, two moods — check every filtered row, or, when
        everything is already checked, clear all the checkmarks."""
        if self.model.whole_filter_selected():
            self.model.clear_checks()
        else:
            self.model.check_all()
        self._sync_select_button()

    def _sync_select_button(self) -> None:
        self.select_all_button.setText(
            "Unselect all filtered"
            if self.model.whole_filter_selected() and self.model.total > 0
            else "Select all filtered"
        )
        count = self.model.selection_count()
        suffix = f" · {count:,} selected" if count else ""
        self.count_label.setText(f"{self.model.total:,} entries{suffix}")

    # -- entry details (r2: double-click a row) --------------------------------

    def _open_entry(self, index) -> None:
        row = self.model.row_dict(index.row())
        if row is not None:
            self._entry_popup = LogEntryPopup(self.window(), row)
            self._entry_popup.show()
            self._entry_popup.raise_()

    # -- export ---------------------------------------------------------------

    def _export_clicked(self) -> None:
        if self._db is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export log", "wave_log_export.sqlite", "SQLite (*.sqlite)"
        )
        if path:
            self.export_to(path)

    def export_to(self, path: str) -> int:
        """Checked rows (or the whole filter minus unchecked exceptions);
        with nothing checked, everything matching the filter. Returns rows
        exported."""
        where, params = self.model.where_clause
        mode, ids = self.model.selection()
        if mode == "ids" and ids:
            placeholders = ",".join("?" * len(ids))
            rows = self._db.query(
                "SELECT ts, category, level, message, json_payload FROM log"  # noqa: S608
                f" WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            )
        elif mode == "all" and ids:  # all filtered minus the unchecked few
            placeholders = ",".join("?" * len(ids))
            glue = " AND" if where else " WHERE"
            rows = self._db.query(
                f"SELECT ts, category, level, message, json_payload FROM log{where}"  # noqa: S608
                f"{glue} id NOT IN ({placeholders}) ORDER BY id",
                (*params, *ids),
            )
        else:  # whole filter (explicitly all-checked, or nothing checked)
            rows = self._db.query(
                f"SELECT ts, category, level, message, json_payload FROM log{where} ORDER BY id",  # noqa: S608
                params,
            )
        out = sqlite3.connect(path)
        try:
            out.execute(_LOG_DDL)
            out.executemany(
                "INSERT INTO log (ts, category, level, message, json_payload) VALUES (?,?,?,?,?)",
                [tuple(row) for row in rows],
            )
            out.commit()
        finally:
            out.close()
        logger.info("exported %d log rows to %s", len(rows), path)
        self.exported.emit(path)
        return len(rows)

    # -- delete (destructive → confirm) ---------------------------------------

    def _delete_clicked(self) -> None:
        if self._db is None:
            return
        count = self.model.selection_count()
        if count == 0:
            return
        self._confirm = _ConfirmPopup(
            self.window(),
            f"Delete {count:,} log entr{'ies' if count != 1 else 'y'}? This cannot be undone.",
            "Delete",
            lambda: self.delete_selected(),
        )
        self._confirm.show()
        self._confirm.raise_()

    def delete_selected(self) -> int:
        """Delete the checked rows (or the whole filter minus exceptions)."""
        if self._db is None:
            return 0
        where, params = self.model.where_clause
        mode, ids = self.model.selection()
        if mode == "ids":
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            cursor = self._db.execute(f"DELETE FROM log WHERE id IN ({placeholders})", ids)  # noqa: S608
        elif ids:  # all filtered minus the unchecked few
            placeholders = ",".join("?" * len(ids))
            glue = " AND" if where else " WHERE"
            cursor = self._db.execute(
                f"DELETE FROM log{where}{glue} id NOT IN ({placeholders})",  # noqa: S608
                (*params, *ids),
            )
        else:
            cursor = self._db.execute(f"DELETE FROM log{where}", params)  # noqa: S608
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        logger.info("deleted %d log rows", deleted)
        self._apply_filters()
        return deleted

    # -- depth panel ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(12.5, 12.5, self.width() - 25, self.height() - 25)
        painter.setPen(QPen(QColor(0, 0, 0, 26), 1))
        painter.setBrush(QColor(0, 0, 0, 13))
        painter.drawRoundedRect(rect, 14, 14)
