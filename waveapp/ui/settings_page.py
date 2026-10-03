"""Settings tab (Phase 8.10 → rework 2026-08-21, §5 tab 6).

Every edit saves to config.toml with the fresh-load pattern (never clobbers
external edits). The rework (blueprint approved 2026-08-21) adds:

- a circle-loading lifecycle on every save/restore — ⟳ spinner until the
  SYSTEM confirms the change (readback from the live engine, auto connection
  tests for keys), ✓ green when confirmed, amber when it applies on the next
  launch, ✗ red on failure;
- the Trading section tells the truth: auto-trade is armable (Touch ID to
  ARM, free to disarm — disarming only makes Wave safer), the champion exit
  system is described in plain words, and the price floor is visible;
- Risk gains the market-impact cap and RE-LOCKS when leaving the section
  (§4: hard gates have no session carry-over);
- Restore-defaults buttons on Trading/Risk/Scanner/Telegram/Appearance
  (never Connections — "default keys" could only mean deleting keys);
- Backup exports are WAL-safe (SQLite backup API, not a raw file copy) and
  restore sits behind Touch ID;
- the System tab refreshes on every confirmed change (wired in app.py).
"""

from __future__ import annotations

import logging
import shutil
import time
import zipfile
from dataclasses import MISSING
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from waveapp.config import AppConfig
from waveapp.ui import theme
from waveapp.ui.positions_page import _ConfirmPopup

logger = logging.getLogger("wave.ui.settings")

SECTIONS = (
    "Trading",
    "Risk",
    "Scanner",
    "Connections",
    "Telegram",
    "Appearance",
    "Backup",
)

_RISK_SECTION = SECTIONS.index("Risk")


def _caption(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet(
        f"font-size: 10px; font-weight: 700; letter-spacing: 1px;"
        f" color: {theme.TEXT_MUTED}; background: transparent;"
    )
    return label


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setMinimumHeight(30)
    label.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;")
    return label


class _SpinnerIcon(QWidget):
    """The 18px icon area — a real subclass (never a monkey-patched
    paintEvent) so Qt's paint path stays safe through destruction."""

    def __init__(self, owner: _SaveSpinner) -> None:
        super().__init__(owner)
        self._owner = owner
        self.setFixedSize(18, 18)

    def paintEvent(self, event) -> None:  # noqa: N802
        owner = self._owner
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(3, 3, 12, 12)
        if owner._state == "spin":
            pen = QPen(QColor(owner._COLORS["spin"]), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(rect, int(-owner._angle * 16), 110 * 16)
        elif owner._state in ("ok", "fail"):
            painter.setPen(QPen(QColor(owner._COLORS[owner._state]), 1))
            font = QFont(self.font())
            font.setPixelSize(13)
            font.setBold(True)
            painter.setFont(font)
            glyph = "✓" if owner._state == "ok" else "✗"
            painter.drawText(QRectF(0, 0, 18, 18), Qt.AlignmentFlag.AlignCenter, glyph)
        elif owner._state == "pending":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(owner._COLORS["pending"]))
            painter.drawEllipse(QRectF(5.5, 5.5, 7, 7))
        painter.end()


class _SaveSpinner(QWidget):
    """The save lifecycle in one widget: ⟳ spinning arc while the system is
    catching the change, ✓ green once it CONFIRMED, amber ● for "applies on
    the next launch", ✗ red on failure.

    `setText()` keeps the old plain-label API alive (app.py's async test
    handlers write results through it) and classifies the text into a state
    by its wording, so existing call sites get the icons for free."""

    min_spin_ms = 700  # a confirm faster than this still SHOWS the spinner

    _COLORS = {
        "spin": theme.BLUE,
        "ok": theme.GREEN,
        "pending": "#FF9500",
        "fail": theme.RED,
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._angle = 0.0
        self._spin_started = 0.0
        self._deferred: tuple[str, str] | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._icon = _SpinnerIcon(self)
        layout.addWidget(self._icon, alignment=Qt.AlignmentFlag.AlignTop)
        # bound-method slot on a child timer: Qt auto-disconnects it when this
        # widget dies — a loose lambda here segfaulted the test run 2026-08-21
        self._finish_timer = QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.timeout.connect(self._apply_deferred)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setMinimumHeight(18)
        self._label.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self._label, stretch=1)
        self.setMinimumHeight(30)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setDuration(900)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_angle)

    # -- state machine -------------------------------------------------------

    def spin(self, text: str) -> None:
        self._finish_timer.stop()
        self._deferred = None
        self._state = "spin"
        self._spin_started = time.monotonic()
        self._label.setText(text)
        if self._anim.state() != QVariantAnimation.State.Running:
            self._anim.start()
        self._icon.update()

    def ok(self, text: str) -> None:
        self._finish("ok", text)

    def pending(self, text: str) -> None:
        self._finish("pending", text)

    def fail(self, text: str) -> None:
        self._finish("fail", text)

    def clear(self) -> None:
        self._finish_timer.stop()
        self._deferred = None
        self._apply("idle", "")

    def setText(self, text: str) -> None:  # noqa: N802 — QLabel-compatible
        if not text:
            self.clear()
            return
        lowered = text.lower()
        if "fail" in lowered or "error" in lowered or "could not" in lowered:
            self._finish("fail", text)
        elif any(
            word in lowered
            for word in ("connected", "saved", "applied", "works", "written", "restored", "armed")
        ):
            self._finish("ok", text)
        elif any(word in lowered for word in ("not configured", "missing", "next launch", "off —")):
            self._finish("pending", text)
        else:
            self._finish("plain", text)

    def text(self) -> str:
        return self._label.text()

    def _finish(self, state: str, text: str) -> None:
        """Land on a final state — but never before the spinner had its
        moment: a sub-`min_spin_ms` confirm still reads as save→spin→✓."""
        if self._state == "spin" and self.min_spin_ms > 0:
            elapsed_ms = (time.monotonic() - self._spin_started) * 1000.0
            remaining = int(self.min_spin_ms - elapsed_ms)
            if remaining > 5:
                self._deferred = (state, text)
                self._finish_timer.start(remaining)
                return
        self._apply(state, text)

    def _apply_deferred(self) -> None:
        if self._deferred is not None:
            state, text = self._deferred
            self._deferred = None
            self._apply(state, text)

    def _apply(self, state: str, text: str) -> None:
        self._state = state
        self._label.setText(text)
        if state != "spin":
            self._anim.stop()
        self._icon.update()

    def _on_angle(self, value) -> None:
        self._angle = float(value)
        if self._state == "spin":
            self._icon.update()

    @property
    def state(self) -> str:
        return self._state


class _Row(QWidget):
    """Label on the left, editor on the right."""

    def __init__(self, label: str, editor: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        name = QLabel(label)
        name.setStyleSheet(f"font-size: 13px; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(name)
        layout.addStretch(1)
        layout.addWidget(editor)


class _SectionButton(QPushButton):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton { text-align: left; border: none; background: transparent;"
            f" padding: 8px 14px; border-radius: 8px; color: {theme.TEXT};"
            " font-size: 13px; }"
            "QPushButton:checked { background: rgba(0, 122, 255, 0.14);"
            f" color: {theme.BLUE}; font-weight: 600; }}"
            "QPushButton:hover:!checked { background: rgba(0, 0, 0, 0.05); }"
        )


class SettingsPage(QWidget):
    risk_changed = pyqtSignal(dict)  # live-applied to the engine
    scanner_changed = pyqtSignal(dict)
    trading_changed = pyqtSignal(bool)  # auto-trade armed/disarmed
    telegram_id_changed = pyqtSignal(int)  # bridge restarts on the new id
    telegram_mode_changed = pyqtSignal(str)  # notification mode: on/minimum/off
    telegram_test_requested = pyqtSignal()
    paper_test_requested = pyqtSignal()
    live_test_requested = pyqtSignal()
    polygon_test_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, config_path: Path | None = None) -> None:
        super().__init__(parent)
        self._config_path = config_path
        self._db_path: Path | None = None
        self._risk_unlocked = False
        self._confirm: _ConfirmPopup | None = None
        self._current_section = 0
        self._loaded_watchlist = ""
        self._watchlist_pending = False
        # ack-fallback timers (bound-method slots; see _SaveSpinner note)
        self._risk_fallback = QTimer(self)
        self._scanner_fallback = QTimer(self)
        self._telegram_fallback = QTimer(self)
        for timer, slot in (
            (self._risk_fallback, self._risk_ack_timeout),
            (self._scanner_fallback, self._scanner_ack_timeout),
            (self._telegram_fallback, self._telegram_ack_timeout),
        ):
            timer.setSingleShot(True)
            timer.timeout.connect(slot)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)

        rail = QFrame()
        rail.setProperty("card", True)
        rail.setFixedWidth(170)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(8, 10, 8, 10)
        rail_layout.setSpacing(2)
        self.section_buttons: list[_SectionButton] = []
        for index, name in enumerate(SECTIONS):
            button = _SectionButton(name)
            button.clicked.connect(lambda _c, i=index: self.select_section(i))
            rail_layout.addWidget(button)
            self.section_buttons.append(button)
        rail_layout.addStretch(1)
        outer.addWidget(rail)

        self.stack = QStackedWidget()
        for name in SECTIONS:
            self.stack.addWidget(getattr(self, f"_build_{name.lower()}")())
        outer.addWidget(self.stack, stretch=1)
        self.select_section(0)
        self.reload()

    # -- config plumbing (fresh-load pattern: never clobber) ------------------

    def _load(self) -> AppConfig:
        return AppConfig.load(self._config_path)

    def _save_fields(self, **fields) -> None:
        fresh = self._load()
        for name, value in fields.items():
            setattr(fresh, name, value)
        fresh.save(self._config_path)
        logger.info("settings saved: %s", ", ".join(fields))

    @staticmethod
    def _default(name: str):
        """The shipped default for a config field — what Restore restores."""
        field = AppConfig.__dataclass_fields__[name]
        return field.default_factory() if field.default is MISSING else field.default

    def select_section(self, index: int) -> None:
        # §4: hard gates have NO session carry-over — walking away from the
        # Risk section locks it again (rework 2026-08-21)
        if self._current_section == _RISK_SECTION and index != _RISK_SECTION:
            self._lock_risk()
        self._current_section = index
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self.section_buttons):
            button.setChecked(i == index)

    def attach_test_page(self, widget: QWidget) -> None:
        """decision (2026-08-18): the Test bench is not deleted after
        Phase 8 — it lives on as a Settings section ("who knows, maybe i will
        need it in the future")."""
        index = self.stack.count()
        button = _SectionButton("Test")
        button.clicked.connect(lambda _c, i=index: self.select_section(i))
        # before the rail's trailing stretch
        rail_layout = self.section_buttons[0].parentWidget().layout()
        rail_layout.insertWidget(rail_layout.count() - 1, button)
        self.section_buttons.append(button)
        self.stack.addWidget(widget)

    def attach_backup_sources(self, config_path: Path, db_path: Path) -> None:
        self._config_path = self._config_path or config_path
        self._backup_config_path = config_path
        self._db_path = db_path

    def _ask(self, message: str, action: str, on_confirm) -> None:
        self._confirm = _ConfirmPopup(self.window(), message, action, on_confirm)
        self._confirm.show()
        self._confirm.raise_()

    def _restore_row(self, message: str, on_confirm) -> QPushButton:
        button = QPushButton("Restore defaults…")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self._ask(message, "Restore", on_confirm))
        return button

    # -- sections -------------------------------------------------------------

    def _card(self) -> tuple[QWidget, QVBoxLayout]:
        wrapper = QWidget()
        wrap = QVBoxLayout(wrapper)
        wrap.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setProperty("card", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(10)
        wrap.addWidget(card)
        wrap.addStretch(1)
        return wrapper, layout

    def _build_trading(self) -> QWidget:
        page, layout = self._card()
        layout.addWidget(_caption("Trading"))
        self.auto_trade_check = QCheckBox("Auto-trade (entries fire without asking)")
        self.auto_trade_check.clicked.connect(self._auto_trade_clicked)
        layout.addWidget(self.auto_trade_check)
        self.trading_status = _SaveSpinner()
        layout.addWidget(self.trading_status)
        layout.addWidget(
            _note(
                "Arming needs Touch ID (entries then fire by themselves, paper "
                "account). Turning it OFF is always free — Wave goes back to "
                "sending signals to Telegram only."
            )
        )
        layout.addWidget(_caption("Champion exit system — read-only (§8.3)"))
        self.params_label = QLabel("")
        self.params_label.setStyleSheet(
            f"font-family: Menlo, monospace; font-size: 11px; color: {theme.TEXT};"
            f" background: transparent;"
        )
        layout.addWidget(self.params_label)
        self.floor_label = _note("")
        layout.addWidget(self.floor_label)
        layout.addWidget(
            _note(
                "Read-only by design (§8.3): exit parameters change only "
                "through the training pipeline's statistical adoption bar — "
                "never by hand."
            )
        )
        self.trading_restore_button = self._restore_row(
            "Restore the champion trading defaults? This resets the price "
            "floor to $15. Auto-trade is not touched.",
            self._restore_trading_defaults,
        )
        layout.addWidget(self.trading_restore_button, alignment=Qt.AlignmentFlag.AlignLeft)
        return page

    def _build_risk(self) -> QWidget:
        page, layout = self._card()
        layout.addWidget(_caption("Risk limits — Touch ID required"))
        self.risk_locked_box = QWidget()
        locked = QVBoxLayout(self.risk_locked_box)
        locked.setContentsMargins(0, 0, 0, 0)
        locked.setSpacing(8)
        self.unlock_button = QPushButton("Unlock with Touch ID")
        self.unlock_button.setProperty("accent", True)
        self.unlock_button.setCursor(Qt.CursorShape.PointingHandCursor)
        locked.addWidget(self.unlock_button, alignment=Qt.AlignmentFlag.AlignLeft)
        locked.addWidget(
            _note(
                "Editing risk limits is a §4 hard gate — and it re-locks when "
                "you leave this section or save. Checks can be tightened, "
                "never removed."
            )
        )
        layout.addWidget(self.risk_locked_box)

        self.risk_editors_box = QWidget()
        editors = QVBoxLayout(self.risk_editors_box)
        editors.setContentsMargins(0, 0, 0, 0)
        editors.setSpacing(8)
        self.risk_per_trade = QLineEdit()
        self.risk_daily = QLineEdit()
        self.risk_weekly = QLineEdit()
        self.risk_positions = QLineEdit()
        self.risk_notional = QLineEdit()
        self.risk_impact = QLineEdit()
        for editor in (
            self.risk_per_trade,
            self.risk_daily,
            self.risk_weekly,
            self.risk_positions,
            self.risk_notional,
            self.risk_impact,
        ):
            editor.setFixedWidth(90)
            editor.setAlignment(Qt.AlignmentFlag.AlignRight)
        editors.addWidget(_Row("Risk per trade (% of equity)", self.risk_per_trade))
        editors.addWidget(_Row("Max daily loss (%) — entry halt", self.risk_daily))
        editors.addWidget(_Row("Max weekly loss (%) — halt until re-armed", self.risk_weekly))
        editors.addWidget(_Row("Max concurrent positions", self.risk_positions))
        editors.addWidget(_Row("Max position notional (% of equity)", self.risk_notional))
        editors.addWidget(_Row("Max share of a stock's daily volume (%)", self.risk_impact))
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.risk_save_button = QPushButton("Save && apply to the running engine")
        self.risk_save_button.setProperty("accent", True)
        self.risk_save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addWidget(self.risk_save_button)
        self.risk_restore_button = self._restore_row(
            "Restore the §12 default risk limits? 1% per trade, 3% daily, "
            "6% weekly, 3 positions, 25% notional, 0.5% of daily volume.",
            self._restore_risk_defaults,
        )
        buttons.addWidget(self.risk_restore_button)
        buttons.addStretch(1)
        editors.addLayout(buttons)
        editors.addWidget(
            _note(
                "Fixed by spec (not editable): overnight risk 0.5% · short "
                "book ≤50% of long sizing."
            )
        )
        layout.addWidget(self.risk_editors_box)
        # OUTSIDE the editors box so the ✓/✗ stays visible after the re-lock
        self.risk_status = _SaveSpinner()
        layout.addWidget(self.risk_status)
        self.risk_editors_box.hide()

        self.unlock_button.clicked.connect(self._unlock_risk_requested)
        self.risk_save_button.clicked.connect(self._save_risk)
        return page

    def _build_scanner(self) -> QWidget:
        page, layout = self._card()
        layout.addWidget(_caption("Scanner"))
        self.scan_size = QLineEdit()
        self.scan_interval = QLineEdit()
        self.watchlist_edit = QLineEdit()
        for editor in (self.scan_size, self.scan_interval):
            editor.setFixedWidth(90)
            editor.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(_Row("Scan set size (most-active symbols)", self.scan_size))
        layout.addWidget(_Row("Scan interval (seconds)", self.scan_interval))
        layout.addWidget(_Row("Base watchlist (comma-separated)", self.watchlist_edit))
        self.watchlist_edit.setMinimumWidth(280)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.scanner_save_button = QPushButton("Save && apply")
        self.scanner_save_button.setProperty("accent", True)
        self.scanner_save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addWidget(self.scanner_save_button)
        self.scanner_restore_button = self._restore_row(
            "Restore the scanner defaults? 1500 symbols, 120s interval, and "
            "the standard base watchlist.",
            self._restore_scanner_defaults,
        )
        buttons.addWidget(self.scanner_restore_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.scanner_status = _SaveSpinner()
        layout.addWidget(self.scanner_status)
        self.scanner_floor_note = _note("")
        layout.addWidget(self.scanner_floor_note)
        self.scanner_save_button.clicked.connect(self._save_scanner)
        return page

    def _build_connections(self) -> QWidget:
        from waveapp.broker.alpaca import KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET
        from waveapp.broker.live_reader import KEYCHAIN_LIVE_KEY_ID, KEYCHAIN_LIVE_SECRET

        self._key_entries = {
            "paper": (KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET),
            "live": (KEYCHAIN_LIVE_KEY_ID, KEYCHAIN_LIVE_SECRET),
        }
        page, layout = self._card()
        layout.addWidget(_caption("Alpaca keys — stored ONLY in macOS Keychain"))
        self.key_fields: dict[str, tuple[QLineEdit, QLineEdit]] = {}
        self.key_status: dict[str, _SaveSpinner] = {}
        for kind in ("paper", "live"):
            layout.addWidget(_caption(f"{kind} account"))
            key_edit = QLineEdit()
            key_edit.setPlaceholderText("API key id")
            secret_edit = QLineEdit()
            secret_edit.setPlaceholderText("API secret")
            secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(key_edit, stretch=1)
            row.addWidget(secret_edit, stretch=1)
            save = QPushButton("Save to Keychain")
            save.setCursor(Qt.CursorShape.PointingHandCursor)
            save.clicked.connect(lambda _c, k=kind: self._save_keys_requested(k))
            row.addWidget(save)
            test = QPushButton("Test")
            test.setCursor(Qt.CursorShape.PointingHandCursor)
            signal = self.paper_test_requested if kind == "paper" else self.live_test_requested
            test.clicked.connect(lambda _c, k=kind, s=signal: self._test_keys(k, s))
            row.addWidget(test)
            layout.addLayout(row)
            status = _SaveSpinner()
            layout.addWidget(status)
            self.key_fields[kind] = (key_edit, secret_edit)
            self.key_status[kind] = status
        # Polygon.io (Phase 10.1): one API key, same Keychain-only rules
        layout.addWidget(_caption("Polygon.io — historical data for training"))
        polygon_row = QHBoxLayout()
        polygon_row.setSpacing(8)
        self.polygon_key_edit = QLineEdit()
        self.polygon_key_edit.setPlaceholderText("Polygon API key")
        self.polygon_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.polygon_save_button = QPushButton("Save to Keychain")
        self.polygon_save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.polygon_test_button = QPushButton("Test")
        self.polygon_test_button.setCursor(Qt.CursorShape.PointingHandCursor)
        polygon_row.addWidget(self.polygon_key_edit, stretch=1)
        polygon_row.addWidget(self.polygon_save_button)
        polygon_row.addWidget(self.polygon_test_button)
        layout.addLayout(polygon_row)
        self.polygon_status = _SaveSpinner()
        layout.addWidget(self.polygon_status)
        self.polygon_save_button.clicked.connect(self._save_polygon_requested)
        self.polygon_test_button.clicked.connect(self._test_polygon)

        # market-data tier (Phase 10.1): IEX (free) vs SIP (Algo Trader Plus)
        layout.addWidget(_caption("Alpaca market-data feed"))
        self.feed_combo = QComboBox()
        self.feed_combo.addItem("IEX — free (≈30 channel subscriptions)", "iex")
        self.feed_combo.addItem("SIP — Algo Trader Plus (full feed)", "sip")
        self.feed_save_button = QPushButton("Save")
        self.feed_save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        feed_row = QHBoxLayout()
        feed_row.setSpacing(8)
        feed_row.addWidget(self.feed_combo)
        feed_row.addWidget(self.feed_save_button)
        feed_row.addStretch(1)
        layout.addLayout(feed_row)
        self.feed_status = _SaveSpinner()
        layout.addWidget(self.feed_status)
        self.feed_save_button.clicked.connect(self._save_feed)

        layout.addWidget(
            _note(
                "Keys go straight into the Keychain and never touch a file. "
                "Fields stay blank on purpose — saved keys are shown only as "
                "set/not set. Saving runs a connection test automatically, so "
                "the ✓ means the key really works. No restore button here: "
                "there is no such thing as default keys."
            )
        )
        self._refresh_polygon_status()
        return page

    def _build_telegram(self) -> QWidget:
        page, layout = self._card()
        layout.addWidget(_caption("Telegram"))
        self.telegram_id_edit = QLineEdit()
        self.telegram_id_edit.setFixedWidth(160)
        self.telegram_id_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(_Row("Your Telegram user id", self.telegram_id_edit))
        # Notification mode (2026-09-20): ON = everything; MINIMUM =
        # only fills, banks, risk halts and the day summary; OFF = no pushes.
        # Commands (/status /positions /pnl) always answer in every mode.
        # Segmented control, not a dropdown (2026-09-21): three pills —
        # left ON, middle MINIMUM, right OFF — same idiom as the Performance
        # range picker; picking one saves instantly and Telegram confirms.
        segmented = QFrame()
        segmented.setStyleSheet("QFrame { background: #EDEDF2; border-radius: 8px; }")
        seg_layout = QHBoxLayout(segmented)
        seg_layout.setContentsMargins(3, 3, 3, 3)
        seg_layout.setSpacing(2)
        self.telegram_mode_buttons: dict[str, QPushButton] = {}
        for label in ("ON", "MINIMUM", "OFF"):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { border: none; background: transparent; padding: 3px 12px;"
                f" border-radius: 6px; font-size: 12px; font-weight: 600; color: {theme.TEXT}; }}"
                "QPushButton:checked { background: #FFFFFF; }"
            )
            button.clicked.connect(lambda _c=False, k=label: self._telegram_mode_changed(k))
            seg_layout.addWidget(button)
            self.telegram_mode_buttons[label] = button
        layout.addWidget(_Row("Notifications", segmented))
        layout.addWidget(
            _note("MINIMUM: only fills, banks, risk halts, day summary. Commands always answer.")
        )
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.telegram_save_button = QPushButton("Save")
        self.telegram_save_button.setProperty("accent", True)
        self.telegram_test_button = QPushButton("Send test message")
        for button in (self.telegram_save_button, self.telegram_test_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(button)
        self.telegram_restore_button = self._restore_row(
            "Turn Telegram off? The user id is cleared and the bridge "
            "disconnects until you set an id again.",
            self._restore_telegram_defaults,
        )
        buttons.addWidget(self.telegram_restore_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.telegram_status = _SaveSpinner()
        layout.addWidget(self.telegram_status)
        self.telegram_bridge_line = _note("")
        layout.addWidget(self.telegram_bridge_line)
        # bot token entry (Phase 9: fresh-Mac setup needs a UI path — the
        # token was hand-loaded into Keychain back in Phase 4)
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        self.telegram_token_edit = QLineEdit()
        self.telegram_token_edit.setPlaceholderText("Bot token (from BotFather)")
        self.telegram_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_token_save = QPushButton("Save to Keychain")
        self.telegram_token_save.setCursor(Qt.CursorShape.PointingHandCursor)
        token_row.addWidget(self.telegram_token_edit, stretch=1)
        token_row.addWidget(self.telegram_token_save)
        layout.addLayout(token_row)
        self.telegram_token_status = _SaveSpinner()
        layout.addWidget(self.telegram_token_status)
        layout.addWidget(
            _note(
                "The bot token lives ONLY in macOS Keychain — never in a file "
                "or backup. Commands are accepted only from this user id; "
                "live/paper switching is never available over Telegram."
            )
        )
        self.telegram_save_button.clicked.connect(self._save_telegram)
        self.telegram_test_button.clicked.connect(self._test_telegram)
        self.telegram_token_save.clicked.connect(self._save_token_requested)
        self._refresh_token_status()
        return page

    def _build_appearance(self) -> QWidget:
        page, layout = self._card()
        layout.addWidget(_caption("Appearance"))
        self.glass_check = QCheckBox("Liquid glass materials")
        layout.addWidget(self.glass_check)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.appearance_save_button = QPushButton("Save")
        self.appearance_save_button.setProperty("accent", True)
        self.appearance_save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addWidget(self.appearance_save_button)
        self.appearance_restore_button = self._restore_row(
            "Restore the appearance defaults? Liquid glass turns back on "
            "(applies on the next launch).",
            self._restore_appearance_defaults,
        )
        buttons.addWidget(self.appearance_restore_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.appearance_status = _SaveSpinner()
        layout.addWidget(self.appearance_status)
        self.appearance_save_button.clicked.connect(self._save_appearance)
        return page

    def _build_backup(self) -> QWidget:
        page, layout = self._card()
        layout.addWidget(_caption("Backup & restore"))
        buttons = QHBoxLayout()
        self.backup_button = QPushButton("Export backup…")
        self.backup_button.setProperty("accent", True)
        self.restore_button = QPushButton("Restore from backup…")
        for button in (self.backup_button, self.restore_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.backup_status = _SaveSpinner()
        layout.addWidget(self.backup_status)
        self.last_backup_label = _note("")
        layout.addWidget(self.last_backup_label)
        layout.addWidget(
            _note(
                "The backup zip holds config.toml and a CONSISTENT snapshot "
                "of the paper database (taken with SQLite's backup API, safe "
                "while Wave is running) — enough to migrate Wave to a new Mac "
                "(keys are re-entered there; Keychain never leaves this "
                "machine). Restoring needs Touch ID, overwrites current data "
                "and needs a restart."
            )
        )
        self.backup_button.clicked.connect(self._export_backup_clicked)
        self.restore_button.clicked.connect(self._restore_backup_clicked)
        return page

    # -- load current values ---------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 — Qt override
        """A4-5 leg 4 (audit 2026-09-22): widgets were populated once at
        construction, so a section Save hours later could write app-start
        values over fields changed elsewhere meanwhile (another section's
        fresh-load→save, a restore, an external edit). Re-reading the file
        the moment the page becomes VISIBLE — before the user can touch an
        editor — keeps every widget honest without ever clobbering an edit
        in progress. reload() itself is signal-safe: value widgets only save
        on explicit clicked() signals, and the instant-save mode segments
        are guarded by _tg_mode_loading."""
        super().showEvent(event)
        if self._any_spinner_spinning():
            # a save ack is mid-flight (its widgets/spinner text would be
            # clobbered mid-confirm); the 4s ack fallbacks resolve every
            # spin, so the next show picks the fresh values up
            return
        try:
            self.reload()
        except Exception:
            logger.exception("settings reload on show failed")

    def _any_spinner_spinning(self) -> bool:
        spinners = [
            self.trading_status,
            self.risk_status,
            self.scanner_status,
            self.polygon_status,
            self.feed_status,
            self.telegram_status,
            self.telegram_token_status,
            self.appearance_status,
            self.backup_status,
            *self.key_status.values(),
        ]
        return any(s.state == "spin" for s in spinners)

    def reload(self) -> None:
        config = self._load()
        self.auto_trade_check.setChecked(config.auto_trade)
        self.risk_per_trade.setText(f"{config.risk_per_trade_pct:g}")
        self.risk_daily.setText(f"{config.max_daily_loss_pct:g}")
        self.risk_weekly.setText(f"{config.max_weekly_loss_pct:g}")
        self.risk_positions.setText(str(config.max_positions))
        self.risk_notional.setText(f"{config.max_notional_pct:g}")
        self.risk_impact.setText(f"{config.impact_participation_pct:g}")
        self.scan_size.setText(str(config.scan_universe_size))
        self.scan_interval.setText(str(config.scan_interval_seconds))
        self._loaded_watchlist = ", ".join(config.watchlist)
        self.watchlist_edit.setText(self._loaded_watchlist)
        self.telegram_id_edit.setText(str(config.telegram_user_id or ""))
        _tg_mode = str(getattr(config, "telegram_mode", "minimum") or "minimum").upper()
        self._tg_mode_loading = True
        self._set_tg_mode_ui(_tg_mode if _tg_mode in ("ON", "MINIMUM", "OFF") else "MINIMUM")
        self._tg_mode_loading = False
        self.glass_check.setChecked(config.glass_enabled)
        self.feed_combo.setCurrentIndex(max(0, self.feed_combo.findData(config.data_feed)))
        self.floor_label.setText(
            f"Price floor: ${config.min_entry_price:g} — entries below this "
            "price are skipped (champion filter; changes only through the "
            "pipeline)."
        )
        self.scanner_floor_note.setText(
            "Interval applies from the next cycle; scan-set size from the "
            "next universe build; the base watchlist on the next launch. "
            f"Candidates under ${config.min_entry_price:g} are skipped "
            "(champion price floor)."
        )
        self.last_backup_label.setText(
            f"Last backup: {config.last_backup_at.replace('T', ' ')[:16]} UTC"
            if config.last_backup_at
            else "No backup exported yet."
        )
        self._refresh_key_status()
        self._refresh_params()

    def _refresh_params(self) -> None:
        try:
            from waveapp.engine.exits import params_for
            from waveapp.engine.session import Regime

            lines = ["trail-only + recovery exit — the restored champion (2026-09-04)", ""]
            for regime in (Regime.OPEN_DRIVE, Regime.MIDDAY, Regime.POWER_HOUR, Regime.POST):
                params = params_for(regime)
                be = "off" if params.k_be >= 900 else f"{params.k_be:g}×"
                t1 = "off" if params.k_t1 >= 900 else f"{params.k_t1:g}×"
                trail = "off (ride)" if params.k_trail >= 900 else f"{params.k_trail:g}×ATR"
                cap = (
                    "session close" if params.t_max_minutes >= 9000 else f"{params.t_max_minutes}m"
                )
                depth = getattr(params, "recovery_depth_atr", 0) or 0
                recovery = "on" if depth > 0 else "off"
                lines.append(
                    f"{regime.value:<11} trail {trail}  stop"
                    f" {params.k_stop:g}×  breakeven {be}  scale-out {t1}"
                    f"  recovery {recovery}  hold until {cap}"
                )
            self.params_label.setText("\n".join(lines))
        except Exception:
            logger.exception("exit params render failed")
            self.params_label.setText("—")

    def _refresh_key_status(self) -> None:
        from waveapp.security import secrets

        for kind, (key_entry, secret_entry) in self._key_entries.items():
            key_set = secrets.get_secret(key_entry) is not None
            secret_set = secrets.get_secret(secret_entry) is not None
            state = "set" if key_set and secret_set else "NOT set"
            self.key_status[kind].setText(f"{kind} keys: {state}")

    # -- trading (auto-trade arm/disarm + champion restore) --------------------

    def _auto_trade_clicked(self, checked: bool) -> None:
        if checked:
            # arming is the dangerous direction — Touch ID first, every time
            self.auto_trade_check.setChecked(False)
            from waveapp.security.gate import require_gate

            require_gate("arm auto-trading", self._arm_auto_trade)
        else:
            self.trading_status.spin("turning auto-trade off…")
            self._save_fields(auto_trade=False)
            self.trading_status.ok("auto-trade OFF — Wave only sends signals to Telegram now")
            self.trading_changed.emit(False)

    def _arm_auto_trade(self) -> None:
        self.auto_trade_check.setChecked(True)
        self.trading_status.spin("arming…")
        self._save_fields(auto_trade=True)
        self.trading_status.ok("auto-trade ARMED — entries fire by themselves (paper)")
        self.trading_changed.emit(True)

    def _restore_trading_defaults(self) -> None:
        self.trading_status.spin("restoring champion defaults…")
        self._save_fields(min_entry_price=self._default("min_entry_price"))
        self.reload()
        self.trading_status.ok("champion defaults restored — price floor $15")
        self.trading_changed.emit(self.auto_trade_check.isChecked())

    # -- risk gate -------------------------------------------------------------

    def _unlock_risk_requested(self) -> None:
        """§4 hard gate via the shared helper (Touch ID on a worker thread)."""
        from waveapp.security.gate import require_gate

        require_gate("edit Wave risk limits", self._unlock_risk)

    def _unlock_risk(self) -> None:
        self._risk_unlocked = True
        self.risk_locked_box.hide()
        self.risk_editors_box.show()

    def _lock_risk(self) -> None:
        self._risk_unlocked = False
        self.risk_editors_box.hide()
        self.risk_locked_box.show()

    def _save_risk(self) -> None:
        if not self._risk_unlocked:
            return
        try:
            values = {
                "risk_per_trade_pct": float(self.risk_per_trade.text()),
                "max_daily_loss_pct": float(self.risk_daily.text()),
                "max_weekly_loss_pct": float(self.risk_weekly.text()),
                "max_positions": int(self.risk_positions.text()),
                "max_notional_pct": float(self.risk_notional.text()),
                "impact_participation_pct": float(self.risk_impact.text()),
            }
        except ValueError:
            self.risk_status.fail("numbers only — nothing saved")
            return
        # max_positions 0 = unlimited count (2026-09-21) — every
        # other limit must stay strictly positive
        if (
            values["max_positions"] < 0
            or min(v for k, v in values.items() if k != "max_positions") <= 0
        ):
            self.risk_status.fail("limits must be positive — nothing saved")
            return
        self._save_fields(**values)
        self.risk_status.spin("applying to the running engine…")
        # if nothing confirms (engine offline, tests), land honestly anyway
        self._risk_fallback.start(3000)
        self.risk_changed.emit(values)
        # §4: the gate closes behind every save — no session carry-over
        self._lock_risk()

    def confirm_risk_applied(self, readback: dict | None) -> None:
        """app.py calls this AFTER the engine applied the new limits, with the
        values read back from the live RiskEngine — the ✓ shows the truth."""
        self._risk_fallback.stop()
        if readback:
            _mp = int(readback.get("max_positions", 0))
            _pos_txt = "unlimited positions" if _mp <= 0 else f"max {_mp} positions"
            self.risk_status.ok(
                "applied — the engine now runs "
                f"{readback.get('risk_per_trade_pct', 0):g}% / trade · "
                f"{readback.get('max_daily_loss_pct', 0):g}% day · "
                f"{_pos_txt} · "
                f"≤{readback.get('impact_participation_pct', 0):g}% of daily volume"
            )
        else:
            self.risk_status.ok("saved — the engine picks it up at Start")

    def _restore_risk_defaults(self) -> None:
        if not self._risk_unlocked:
            return
        self.risk_per_trade.setText(f"{self._default('risk_per_trade_pct'):g}")
        self.risk_daily.setText(f"{self._default('max_daily_loss_pct'):g}")
        self.risk_weekly.setText(f"{self._default('max_weekly_loss_pct'):g}")
        self.risk_positions.setText(str(self._default("max_positions")))
        self.risk_notional.setText(f"{self._default('max_notional_pct'):g}")
        self.risk_impact.setText(f"{self._default('impact_participation_pct'):g}")
        self._save_risk()

    # -- scanner ---------------------------------------------------------------

    def _save_scanner(self) -> None:
        try:
            size = int(self.scan_size.text())
            interval = int(self.scan_interval.text())
        except ValueError:
            self.scanner_status.fail("numbers only — nothing saved")
            return
        if size <= 0 or interval <= 0:
            self.scanner_status.fail("numbers must be positive — nothing saved")
            return
        watchlist = [
            symbol.strip().upper()
            for symbol in self.watchlist_edit.text().split(",")
            if symbol.strip()
        ]
        self._watchlist_pending = ", ".join(watchlist) != self._loaded_watchlist
        self._save_fields(
            scan_universe_size=size,
            scan_interval_seconds=interval,
            watchlist=watchlist,
        )
        self.scanner_status.spin("applying to the scanner…")
        self._scanner_fallback.start(3000)
        self.scanner_changed.emit({"universe_size": size, "interval_seconds": interval})

    def confirm_scanner_applied(self, size: int, interval: int) -> None:
        self._scanner_fallback.stop()
        text = f"applied — scanning {size} symbols every {interval}s"
        if self._watchlist_pending:
            self.scanner_status.pending(text + " · the watchlist applies on the next launch")
        else:
            self.scanner_status.ok(text)

    def _restore_scanner_defaults(self) -> None:
        self.scan_size.setText(str(self._default("scan_universe_size")))
        self.scan_interval.setText(str(self._default("scan_interval_seconds")))
        self.watchlist_edit.setText(", ".join(self._default("watchlist")))
        self._save_scanner()

    # -- telegram ---------------------------------------------------------------

    def _save_telegram(self) -> None:
        text = self.telegram_id_edit.text().strip()
        try:
            user_id = int(text) if text else 0
        except ValueError:
            self.telegram_status.fail("the user id is a number")
            return
        self._save_fields(telegram_user_id=user_id)
        self.telegram_status.spin(
            "saved — reconnecting the bridge…" if user_id else "saved — turning the bridge off…"
        )
        self._telegram_fallback.start(4000)
        self.telegram_id_changed.emit(user_id)

    def confirm_telegram_applied(self, text: str) -> None:
        self._telegram_fallback.stop()
        self.telegram_status.setText(text)

    def _risk_ack_timeout(self) -> None:
        if self.risk_status.state == "spin":
            self.risk_status.ok("saved — the engine picks it up at Start")

    def _scanner_ack_timeout(self) -> None:
        if self.scanner_status.state == "spin":
            self.scanner_status.ok("saved — applies when the engine starts")

    def _set_tg_mode_ui(self, text: str) -> None:
        """Reflect a mode in the segmented control without saving."""
        for label, button in self.telegram_mode_buttons.items():
            button.setChecked(label == text)

    def _telegram_mode_changed(self, text: str) -> None:
        """Save the notification mode the moment it is picked (live-read by
        the push gate — no restart). Guarded during config load."""
        if getattr(self, "_tg_mode_loading", False):
            return
        self._set_tg_mode_ui(text)  # segments are exclusive by hand
        try:
            config = self._load()
            config.telegram_mode = text.lower()
            config.save()
            self.telegram_bridge_line.setText(f"Notifications: {text} — saved.")
            # the bridge confirms on the phone (2026-09-21) — proof the
            # toggle really reached the engine, not just the config file
            self.telegram_mode_changed.emit(text.lower())
        except Exception:
            logger.exception("telegram mode save failed")
            self.telegram_bridge_line.setText("Could not save the notification mode.")

    def _telegram_ack_timeout(self) -> None:
        if self.telegram_status.state == "spin":
            self.telegram_status.ok("saved — the bridge reconnects on its next cycle")

    def set_bridge_status(self, text: str) -> None:
        """Live bridge state from the monitor's status feed (rework)."""
        self.telegram_bridge_line.setText(text)

    def _test_telegram(self) -> None:
        self.telegram_status.spin("sending a test message…")
        self.telegram_test_requested.emit()

    def _restore_telegram_defaults(self) -> None:
        self.telegram_id_edit.setText("")
        self._save_telegram()

    def _refresh_token_status(self) -> None:
        from waveapp.security import secrets
        from waveapp.telegram.bridge import KEYCHAIN_BOT_TOKEN

        state = "set" if secrets.get_secret(KEYCHAIN_BOT_TOKEN) is not None else "NOT set"
        self.telegram_token_status.setText(f"bot token: {state}")

    def _save_token_requested(self) -> None:
        """Rotating the bot token is a §4 hard gate, like the Alpaca keys."""
        from waveapp.security.gate import require_gate

        if not self.telegram_token_edit.text().strip():
            self.telegram_token_status.setText("nothing to save")
            return
        require_gate("save the Telegram bot token", self._save_token)

    def _save_token(self) -> None:
        from waveapp.security import secrets
        from waveapp.telegram.bridge import KEYCHAIN_BOT_TOKEN

        secrets.set_secret(KEYCHAIN_BOT_TOKEN, self.telegram_token_edit.text().strip())
        self.telegram_token_edit.clear()
        self._refresh_token_status()
        self.telegram_token_status.ok("bot token: saved to Keychain")
        logger.info("Telegram bot token updated in Keychain")

    # -- appearance --------------------------------------------------------------

    def _save_appearance(self) -> None:
        self._save_fields(glass_enabled=self.glass_check.isChecked())
        self.appearance_status.pending("saved — applies on the next launch")

    def _restore_appearance_defaults(self) -> None:
        self.glass_check.setChecked(bool(self._default("glass_enabled")))
        self._save_appearance()

    # -- connections -------------------------------------------------------------

    def _refresh_polygon_status(self) -> None:
        from waveapp.research.history import KEYCHAIN_POLYGON_KEY
        from waveapp.security import secrets

        state = "set" if secrets.get_secret(KEYCHAIN_POLYGON_KEY) is not None else "NOT set"
        self.polygon_status.setText(f"polygon key: {state}")

    def _save_polygon_requested(self) -> None:
        from waveapp.security.gate import require_gate

        if not self.polygon_key_edit.text().strip():
            self.polygon_status.setText("nothing to save")
            return
        require_gate("save the Polygon API key", self._save_polygon)

    def _save_polygon(self) -> None:
        from waveapp.research.history import KEYCHAIN_POLYGON_KEY
        from waveapp.security import secrets

        secrets.set_secret(KEYCHAIN_POLYGON_KEY, self.polygon_key_edit.text().strip())
        self.polygon_key_edit.clear()
        # ✓ only after the key PROVES it works — auto-test on save
        self.polygon_status.spin("saved to Keychain — testing the connection…")
        self.polygon_test_requested.emit()
        logger.info("Polygon API key updated in Keychain")

    def _test_polygon(self) -> None:
        self.polygon_status.spin("testing the Polygon connection…")
        self.polygon_test_requested.emit()

    def _test_keys(self, kind: str, signal) -> None:
        self.key_status[kind].spin(f"testing the {kind} connection…")
        signal.emit()

    def _save_feed(self) -> None:
        feed = self.feed_combo.currentData()
        self._save_fields(data_feed=feed)
        self.feed_status.pending(f"saved — the {feed.upper()} feed connects on the next launch")

    def _save_keys_requested(self, kind: str) -> None:
        """Rotating API keys is a §4 hard gate — Touch ID first, every time."""
        from waveapp.security.gate import require_gate

        key_edit, secret_edit = self.key_fields[kind]
        if not key_edit.text().strip() or not secret_edit.text().strip():
            self.key_status[kind].fail("both fields are needed — nothing saved")
            return
        require_gate(f"save {kind} Alpaca keys", lambda: self._save_keys(kind))

    def _save_keys(self, kind: str) -> None:
        from waveapp.security import secrets

        key_edit, secret_edit = self.key_fields[kind]
        key_entry, secret_entry = self._key_entries[kind]
        key = key_edit.text().strip()
        secret = secret_edit.text().strip()
        if not key or not secret:
            self.key_status[kind].fail("both fields are needed — nothing saved")
            return
        secrets.set_secret(key_entry, key)
        secrets.set_secret(secret_entry, secret)
        key_edit.clear()
        secret_edit.clear()
        logger.info("%s Alpaca keys updated in Keychain", kind)
        # ✓ only after the keys PROVE they work — auto-test on save
        self.key_status[kind].spin(f"{kind} keys saved to Keychain — testing the connection…")
        signal = self.paper_test_requested if kind == "paper" else self.live_test_requested
        signal.emit()

    # -- backup / restore -------------------------------------------------------

    def _export_backup_clicked(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export Wave backup", "wave_backup.zip", "Zip (*.zip)"
        )
        if path:
            self.export_backup(path)

    def _snapshot_db(self, dest: Path) -> None:
        """A CONSISTENT copy of the live WAL database via SQLite's backup
        API — a raw file copy can miss trades still sitting in the -wal
        sidecar (rework 2026-08-21)."""
        import sqlite3

        source = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(dest)
            try:
                with target:
                    source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    def export_backup(self, path: str) -> bool:
        config_path = getattr(self, "_backup_config_path", None) or self._config_path
        if config_path is None or self._db_path is None:
            self.backup_status.fail("nothing to back up yet")
            return False
        self.backup_status.spin("exporting…")
        snapshot: Path | None = None
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
                if Path(config_path).exists():
                    bundle.write(config_path, "config.toml")
                if self._db_path.exists():
                    snapshot = Path(path).with_suffix(".db.snapshot")
                    try:
                        self._snapshot_db(snapshot)
                        bundle.write(snapshot, self._db_path.name)
                    except Exception:
                        # not a real SQLite file (tests) or backup API failure —
                        # fall back to the raw copy rather than exporting nothing
                        logger.warning("WAL-safe snapshot failed — raw copy fallback")
                        bundle.write(self._db_path, self._db_path.name)
            from datetime import UTC, datetime

            self._save_fields(last_backup_at=datetime.now(UTC).isoformat(timespec="seconds"))
            self.reload()
            self.backup_status.ok(f"backup written: {path}")
            logger.info("backup exported to %s", path)
            return True
        except Exception:
            logger.exception("backup export failed")
            self.backup_status.fail("backup FAILED — see the log")
            return False
        finally:
            if snapshot is not None and snapshot.exists():
                snapshot.unlink()

    def _restore_backup_clicked(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Restore Wave backup", "", "Zip (*.zip)")
        if not path:
            return
        # overwriting the database is destructive — Touch ID, then confirm
        from waveapp.security.gate import require_gate

        require_gate("restore a Wave backup", lambda: self._confirm_restore(path))

    def _confirm_restore(self, path: str) -> None:
        self._ask(
            "Restore this backup? Current settings and the paper database "
            "will be OVERWRITTEN. Wave must be restarted afterwards.",
            "Restore",
            lambda: self.restore_backup(path),
        )

    def restore_backup(self, path: str) -> bool:
        config_path = getattr(self, "_backup_config_path", None) or self._config_path
        if config_path is None or self._db_path is None:
            return False
        try:
            with zipfile.ZipFile(path) as bundle:
                names = set(bundle.namelist())
                if "config.toml" in names:
                    with bundle.open("config.toml") as src, open(config_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                db_name = self._db_path.name
                if db_name in names:
                    # NEVER overwrite the live WAL database — stage it; the
                    # next launch swaps it in before the DB opens
                    pending = self._db_path.with_suffix(".db.restored")
                    with bundle.open(db_name) as src, open(pending, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            self.backup_status.ok("restored — quit and relaunch Wave")
            logger.warning("backup restored from %s — restart required", path)
            self.reload()
            return True
        except Exception:
            logger.exception("backup restore failed")
            self.backup_status.fail("restore FAILED — see the log")
            return False

    # -- depth panel ------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(12.5, 12.5, self.width() - 25, self.height() - 25)
        painter.setPen(QPen(QColor(0, 0, 0, 26), 1))
        painter.setBrush(QColor(0, 0, 0, 13))
        painter.drawRoundedRect(rect, 14, 14)
