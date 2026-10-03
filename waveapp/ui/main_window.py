"""Main window (§5): top bar, hover sidebar, six pages, fully
resizable with a sensible minimum size. Layouts reflow; nothing overlaps or
scrolls.

Phase 8.3: whole-window liquid glass. Round 3: the NATIVE macOS
traffic lights stay (the custom monochrome trio was deleted)."""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from waveapp.config import AppConfig
from waveapp.ui import theme
from waveapp.ui.scanner_page import ScannerPage
from waveapp.ui.sidebar import ConnectionStatus, Sidebar
from waveapp.ui.top_bar import TopBar

logger = logging.getLogger("wave.ui.main")

MIN_WIDTH = 980
MIN_HEIGHT = 640


_PAGES = (
    ("Positions", "Position cards arrive in Phase 5; charts in Phase 8."),
    ("Performance", "Equity curve and stats arrive in Phase 8."),
    ("Scanner", "Wave's mind — heuristic ranker arrives in Phase 7."),
    ("ML", "The Judge — Wave's learning brain (Win Campaign, 2026-08-30)."),
    ("System", "Connection health and engine state arrive in Phases 2–5."),
    ("Log", "The searchable log arrives with the database in Phase 3."),
    ("Settings", "Settings sub-tabs arrive in Phase 8."),
)


def _placeholder_page(title: str, note: str) -> QWidget:
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(24, 24, 24, 24)

    card = QFrame()
    card.setProperty("card", True)
    inner = QVBoxLayout(card)
    inner.setContentsMargins(32, 32, 32, 32)
    inner.setSpacing(8)

    heading = QLabel(title)
    heading.setProperty("cardTitle", True)
    subtitle = QLabel(note)
    subtitle.setProperty("muted", True)
    subtitle.setMinimumHeight(40)  # word-wrapped QLabel under-reports height
    subtitle.setWordWrap(True)

    inner.addStretch(1)
    inner.addWidget(heading, alignment=Qt.AlignmentFlag.AlignHCenter)
    inner.addWidget(subtitle, alignment=Qt.AlignmentFlag.AlignHCenter)
    inner.addStretch(1)

    outer.addWidget(card)
    return page


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__()
        self.config = config or AppConfig()
        self.setWindowTitle("Wave")
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.resize(
            max(self.config.window_width, MIN_WIDTH),
            max(self.config.window_height, MIN_HEIGHT),
        )

        self.status = ConnectionStatus()

        # macOS split layout: full-height sidebar (glass panel in glass mode),
        # content column (top bar + pages) opaque on the right.
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar(self.status)
        root.addWidget(self.sidebar)

        self.content_column = QWidget()
        self.content_column.setObjectName("contentColumn")
        self.content_column.setStyleSheet(f"#contentColumn {{ background: {theme.BG_SOFT}; }}")
        column = QVBoxLayout(self.content_column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.top_bar = TopBar()
        column.addWidget(self.top_bar)

        from waveapp.ui.log_page import LogPage
        from waveapp.ui.ml_page import MLPage
        from waveapp.ui.performance_page import PerformancePage
        from waveapp.ui.positions_page import PositionsPage
        from waveapp.ui.settings_page import SettingsPage
        from waveapp.ui.system_page import SystemPage
        from waveapp.ui.test_page import TestPage

        self.pages = QStackedWidget()
        self.scanner_page = ScannerPage()
        self.ml_page = MLPage()
        self.positions_page = PositionsPage()
        self.performance_page = PerformancePage()
        self.system_page = SystemPage()
        self.system_page.attach_status(self.status)
        self.log_page = LogPage()
        self.settings_page = SettingsPage()
        # Test bench lives INSIDE Settings (2026-08-18 — kept, not
        # deleted after Phase 8; no sidebar entry of its own anymore)
        self.test_page = TestPage(
            self.top_bar,
            positions_page=self.positions_page,
            performance_page=self.performance_page,
            scanner_page=self.scanner_page,
            ml_page=self.ml_page,
        )
        self.settings_page.attach_test_page(self.test_page)
        for title, note in _PAGES:
            if title == "Positions":
                self.pages.addWidget(self.positions_page)  # Phase 8.4: live cards
            elif title == "Performance":
                self.pages.addWidget(self.performance_page)  # Phase 8.6: equity curve
            elif title == "Scanner":
                self.pages.addWidget(self.scanner_page)  # Phase 7: live table
            elif title == "ML":
                self.pages.addWidget(self.ml_page)  # the Judge (2026-08-30)
            elif title == "System":
                self.pages.addWidget(self.system_page)  # Phase 8.8
            elif title == "Log":
                self.pages.addWidget(self.log_page)  # Phase 8.9
            elif title == "Settings":
                self.pages.addWidget(self.settings_page)  # Phase 8.10
            else:
                self.pages.addWidget(_placeholder_page(title, note))
        column.addWidget(self.pages, stretch=1)

        root.addWidget(self.content_column, stretch=1)
        self.setCentralWidget(central)

        self.sidebar.page_selected.connect(self.pages.setCurrentIndex)
        # the engine card counts what the Positions tab DISPLAYS (fakes too)
        self.positions_page.count_changed.connect(self.system_page.set_open_positions)
        self.setStatusBar(None)  # round 4: no bottom bar

    def show_error(self, text: str) -> None:
        """Any logged ERROR anywhere in Wave lands on the flip board (and on
        Telegram). Round 4: the bottom status bar is gone."""
        first_line = text.replace("\n", " — ")
        self.top_bar.show_alert(first_line)

    def on_engine_state(self, state: str) -> None:
        self.top_bar.set_engine_state(state)
        self.system_page.set_engine_state(state)  # instant, not next 30s poll

    def set_glass_mode(self, active: bool) -> None:
        """Phase 8.3: EVERYTHING is liquid glass. The whole window
        surface goes transparent so the native glass panel behind it shows
        through; cards/controls keep their translucent theme."""
        if active:
            self.setStyleSheet("QMainWindow { background: transparent; }")
            self.content_column.setStyleSheet("#contentColumn { background: transparent; }")
        self.sidebar.set_glass(active)

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            # fresh-load + targeted update: never clobber externally-changed
            # settings with this process's stale snapshot
            self.config.save_window_geometry(self.width(), self.height())
        except OSError:
            pass
        super().closeEvent(event)
