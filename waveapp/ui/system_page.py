"""System tab (Phase 8.8, §5 tab 4): connection health, feed
heartbeats, engine state, session regime + countdown to the next boundary,
DB stats, app version, and the fee-schedule constants currently in force.

Everything is REAL data: connections ride the same ConnectionStatus signal
as the sidebar dots; the monitor pushes a system snapshot every poll; the
session card ticks its countdown locally every second."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme

logger = logging.getLogger("wave.ui.system")

_ET_ZONE = ZoneInfo("America/New_York")


def _title(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet(
        f"font-size: 10px; font-weight: 700; letter-spacing: 1px;"
        f" color: {theme.TEXT_MUTED}; background: transparent;"
    )
    return label


def _row_label(text: str = "", muted: bool = False) -> QLabel:
    label = QLabel(text)
    color = theme.TEXT_MUTED if muted else theme.TEXT
    label.setStyleSheet(f"font-size: 12px; color: {color}; background: transparent;")
    return label


class _Card(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 14, 18, 14)
        self.body.setSpacing(6)
        self.title_row = QHBoxLayout()
        self.title_row.addWidget(_title(title))
        self.title_row.addStretch(1)
        self.body.addLayout(self.title_row)


class _StatusRow(QWidget):
    """Colored dot + name + live detail text."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.color = theme.GREY
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        self.label = _row_label(name)
        self.label.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {theme.TEXT}; background: transparent;"
        )
        self.detail = _row_label("—", muted=True)
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.addWidget(self.detail, stretch=1)
        self.set_state(theme.GREY, "not connected")

    def set_state(self, color: str, detail: str) -> None:
        self.color = color
        self.dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
        # detail arrives as "Name: detail" — show just the detail part
        text = detail.split(": ", 1)[1] if ": " in detail else detail
        self.detail.setText(text)


class _CountdownRing(QWidget):
    """A cooler clock (8.8 r3): circular progress ring with the countdown in
    the middle. The ring fills as the current phase elapses."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(150, 150)
        self.progress = 0.0  # 0..1 elapsed of the phase
        self.text = "—"
        self.color = QColor(theme.BLUE)

    def set_state(self, progress: float, text: str, color: str) -> None:
        self.progress = max(0.0, min(1.0, progress))
        self.text = text
        self.color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 14
        rect = QRectF(
            (self.width() - side) / 2, (self.height() - side) / 2, float(side), float(side)
        )
        track = QPen(QColor(0, 0, 0, 26), 7)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawEllipse(rect)
        arc = QPen(self.color, 7)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc)
        painter.drawArc(rect, 90 * 16, -int(self.progress * 360 * 16))
        from PyQt6.QtGui import QFont

        font = QFont("Menlo", 17)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text)


class SystemPage(QWidget):
    # §4 hard-gated commands; app.py routes these into the engine
    kill_confirmed = pyqtSignal()
    rearm_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: dict = {}
        self._ages_received_at: float | None = None
        self._clock_info: dict | None = None
        self._closed_boundary = None
        self._holiday_name: str | None = None
        self._targets_refreshed: float | None = None
        self._confirm: QWidget | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 10)
        outer.setSpacing(0)
        # two slideable pages (10.4): page 1 = status cards,
        # page 2 = the loading bars; dots below, like the Positions tab
        self.page_host = QWidget()
        outer.addWidget(self.page_host, stretch=1)
        self._page_one = QWidget(self.page_host)
        page_one_layout = QVBoxLayout(self._page_one)
        page_one_layout.setContentsMargins(0, 14, 0, 14)
        grid = QGridLayout()
        grid.setSpacing(14)
        page_one_layout.addLayout(grid)
        page_one_layout.addStretch(1)
        self._page_two = QWidget(self.page_host)
        page_two_layout = QVBoxLayout(self._page_two)
        page_two_layout.setContentsMargins(0, 14, 0, 14)
        page_two_layout.setSpacing(14)
        self._page_two.hide()
        # (the scoreboard table moved to the Performance tab, 2026-08-22)
        # page 3 (agenda #3 redo, 2026-08-23, step 1): the ML page — UI ONLY
        # at this step: the mode switch + empty dataset card. No DB queries,
        # no background work; those arrive in later supervised steps.
        # page three (the ML sub-page) left the System tab entirely on
        # 2026-08-30 — the Brain lives in its own top-level tab now
        self._sys_pages = [self._page_one, self._page_two]
        self._sys_page_index = 0

        # connections card (same signal as the sidebar dots) + the Alpaca
        # dashboard link behind the monochrome logomark (r2)
        from waveapp.ui.top_bar import ALPACA_PAPER_DASHBOARD, AlpacaLogoButton

        self.connections_card = _Card("Connections")
        self.alpaca_link = AlpacaLogoButton(size=26)
        self.alpaca_link.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(ALPACA_PAPER_DASHBOARD))
        )
        self.connections_card.title_row.addWidget(self.alpaca_link)
        self.status_rows: dict[str, _StatusRow] = {}
        for name in ("Alpaca", "Data feed", "Telegram", "DB"):
            row = _StatusRow(name)
            self.status_rows[name] = row
            self.connections_card.body.addWidget(row)
        grid.addWidget(self.connections_card, 0, 0)

        # market clock card (8.8 r3): ring counting to today's open/close
        self.clock_card = _Card("Market clock")
        self.clock_caption = _row_label("", muted=True)
        self.clock_caption.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 1px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        self.clock_ring = _CountdownRing()
        self.clock_target_label = _row_label("", muted=True)
        self.regime_label = _row_label("", muted=True)
        self.market_regime_label = _row_label("", muted=True)  # 4.1 internals
        self.clock_card.body.addWidget(self.clock_caption, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.clock_card.body.addWidget(self.clock_ring, stretch=1)
        self.clock_card.body.addWidget(
            self.clock_target_label, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        self.clock_card.body.addWidget(self.regime_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.clock_card.body.addWidget(
            self.market_regime_label, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        grid.addWidget(self.clock_card, 0, 1, 2, 1)

        # week-close card (8.8 r3): weekend or NAMED holiday closure
        self.week_card = _Card("Week close")
        self._week_title = self.week_card.title_row.itemAt(0).widget()
        self.week_countdown_label = QLabel("—")
        self.week_countdown_label.setStyleSheet(
            f"font-family: Menlo, monospace; font-size: 19px; font-weight: 700;"
            f" color: {theme.TEXT}; background: transparent;"
        )
        self.week_reason_label = _row_label("", muted=True)
        self.week_card.body.addWidget(self.week_countdown_label)
        self.week_card.body.addWidget(self.week_reason_label)
        grid.addWidget(self.week_card, 0, 2)

        # engine card
        self.engine_card = _Card("Engine")
        self.engine_state_label = QLabel("offline")
        self.engine_state_label.setStyleSheet(
            f"font-size: 20px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        self.engine_positions_label = _row_label("", muted=True)
        self.engine_card.body.addWidget(self.engine_state_label)
        self.engine_card.body.addWidget(self.engine_positions_label)
        grid.addWidget(self.engine_card, 1, 2)

        # risk card (r2): halts, freezes, and the limits in force (§12)
        self.risk_card = _Card("Risk & limits")
        self.halt_label = QLabel("—")
        self.halt_label.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.GREEN}; background: transparent;"
        )
        self.freeze_label = _row_label("", muted=True)
        self.limits_label = _row_label("", muted=True)
        self.risk_card.body.addWidget(self.halt_label)
        self.risk_card.body.addWidget(self.freeze_label)
        self.risk_card.body.addWidget(self.limits_label)

        # §4 hard gates: kill switch always available; re-arm appears only
        # while a weekly-loss halt is holding entries. Both go through
        # Touch ID, and the kill switch through a confirm card on top.
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)
        self.kill_button = QPushButton("Kill switch")
        self.kill_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.kill_button.setStyleSheet(
            f"QPushButton {{ background: {theme.RED}; color: white; border: none;"
            f" border-radius: 7px; padding: 5px 14px; font-weight: 600; }}"
            f"QPushButton:pressed {{ background: #D70015; }}"
        )
        self.rearm_button = QPushButton("Re-arm trading")
        self.rearm_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rearm_button.hide()
        # resize audit: rigid heights — a squeezed grid row must never clip
        # the §4 buttons
        self.kill_button.setFixedHeight(26)
        self.rearm_button.setFixedHeight(26)
        buttons_row.addWidget(self.kill_button)
        buttons_row.addWidget(self.rearm_button)
        buttons_row.addStretch(1)
        self.risk_card.body.addLayout(buttons_row)
        self.kill_button.clicked.connect(self._kill_clicked)
        self.rearm_button.clicked.connect(self._rearm_clicked)
        grid.addWidget(self.risk_card, 1, 0)

        # trading card (r2): auto-trade arm state + scanner cadence
        self.trading_card = _Card("Trading")
        self.auto_trade_label = QLabel("—")
        self.auto_trade_label.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.ORANGE_LIVE};"
            f" background: transparent;"
        )
        self.scanner_label = _row_label("", muted=True)
        self.scanner_label.setWordWrap(True)  # resize audit: never truncate at 980px
        self.last_scan_label = _row_label("", muted=True)
        self.llm_label = _row_label("", muted=True)  # layer 7 spend visibility
        self.ladder_label = _row_label("", muted=True)  # 6.5 fallback visibility
        self.trading_card.body.addWidget(self.auto_trade_label)
        self.trading_card.body.addWidget(self.scanner_label)
        self.trading_card.body.addWidget(self.last_scan_label)
        self.trading_card.body.addWidget(self.llm_label)
        self.trading_card.body.addWidget(self.ladder_label)
        grid.addWidget(self.trading_card, 2, 0)

        # feed heartbeats card (ages re-tick locally between pushes)
        self.feed_card = _Card("Data feed heartbeats")
        self.feed_rows: dict[str, QLabel] = {}
        for channel in ("trades", "quotes", "bars"):
            row = QHBoxLayout()
            row.setSpacing(8)
            name = _row_label(channel)
            name.setStyleSheet(
                f"font-size: 12px; font-weight: 600; color: {theme.TEXT}; background: transparent;"
            )
            age = _row_label("—", muted=True)
            age.setMinimumWidth(90)
            age.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.feed_rows[channel] = age
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(age)
            self.feed_card.body.addLayout(row)
        grid.addWidget(self.feed_card, 2, 1)

        # database + version card
        self.db_card = _Card("Database & app")
        self.db_label = _row_label("—", muted=True)
        self.version_label = _row_label("", muted=True)
        self.uptime_label = _row_label("", muted=True)
        self.db_card.body.addWidget(self.db_label)
        self.db_card.body.addWidget(self.version_label)
        self.db_card.body.addWidget(self.uptime_label)
        grid.addWidget(self.db_card, 2, 2)

        # market-data subscriptions card (Phase 10.1, request):
        # which Alpaca feed tier is CONNECTED and whether Polygon answers
        self.market_data_card = _Card("Market data")
        self.feed_tier_label = QLabel("—")
        self.feed_tier_label.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        self.feed_detail_label = _row_label("", muted=True)
        self.polygon_label = _row_label("", muted=True)
        self.market_data_card.body.addWidget(self.feed_tier_label)
        self.market_data_card.body.addWidget(self.feed_detail_label)
        self.market_data_card.body.addWidget(self.polygon_label)
        grid.addWidget(self.market_data_card, 3, 0)

        # scan-set loading card (10.2, request): the universe build
        # (the ~6-min daily-bar download) shows as a real progress bar

        self.scanset_card = _Card("Scan set")
        self.scanset_bar = QProgressBar()
        self.scanset_bar.setTextVisible(False)
        self.scanset_bar.setFixedHeight(6)
        self.scanset_bar.setRange(0, 100)
        self.scanset_bar.setStyleSheet(
            "QProgressBar { background: rgba(0, 0, 0, 0.08); border: none;"
            " border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {theme.BLUE}; border-radius: 3px; }}"
        )
        self.scanset_label = _row_label("waiting for market data", muted=True)
        self.scanset_card.body.addWidget(self.scanset_bar)
        self.scanset_card.body.addWidget(self.scanset_label)
        page_two_layout.addWidget(self.scanset_card)

        # research-run card (10.3, request): scripts run OUTSIDE the
        # app and report via a progress file; the card watches it live
        self.research_card = _Card("Research")
        self.research_bar = QProgressBar()
        self.research_bar.setTextVisible(False)
        self.research_bar.setFixedHeight(6)
        self.research_bar.setRange(0, 100)
        self.research_bar.setStyleSheet(
            "QProgressBar { background: rgba(0, 0, 0, 0.08); border: none;"
            " border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {theme.BLUE}; border-radius: 3px; }}"
        )
        self.research_label = _row_label("no research running", muted=True)
        self.research_card.body.addWidget(self.research_bar)
        self.research_card.body.addWidget(self.research_label)
        page_two_layout.addWidget(self.research_card)
        page_two_layout.addStretch(1)

        # fee constants card (§7: fees are data, not literals)
        self.fees_card = _Card("Fee schedule in force")
        self.fees_label = _row_label("—", muted=True)
        self.fees_label.setWordWrap(True)
        self.fees_label.setMinimumHeight(72)  # 4 lines — wrapped QLabel under-reports
        self.fees_label.setStyleSheet(
            f"font-family: Menlo, monospace; font-size: 11px; color: {theme.TEXT};"
            f" background: transparent;"
        )
        self.fees_card.body.addWidget(self.fees_label)
        grid.addWidget(self.fees_card, 3, 1, 1, 2)

        for column in range(3):
            grid.setColumnStretch(column, 1)

        from waveapp.ui.positions_page import _Dot

        dots_row = QHBoxLayout()
        dots_row.addStretch(1)
        self.sys_dots: list[_Dot] = []
        for index in range(len(self._sys_pages)):  # 2 since the ML page left (2026-08-31)
            dot = _Dot(index)
            dot.clicked.connect(self.set_system_page)
            dots_row.addWidget(dot)
            self.sys_dots.append(dot)
        dots_row.addStretch(1)
        outer.addLayout(dots_row)
        self._refresh_sys_dots()

        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)
        self._ticker.start()
        self._tick()

    # -- feeds ----------------------------------------------------------------

    def set_universe_progress(self, done: int, total: int, label: str) -> None:
        """Scan-set build progress (10.2): green full bar when ready."""
        if total <= 0:
            self.scanset_bar.setValue(0)
            self.scanset_label.setText(label or "waiting for market data")
            return
        percent = max(0, min(100, int(done / total * 100)))
        self.scanset_bar.setValue(percent)
        ready = done >= total
        color = theme.GREEN if ready else theme.BLUE
        self.scanset_bar.setStyleSheet(
            "QProgressBar { background: rgba(0, 0, 0, 0.08); border: none;"
            " border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )
        self.scanset_label.setText(label)

    # -- §4 hard gates ---------------------------------------------------------

    def _kill_clicked(self) -> None:
        from waveapp.security.gate import require_gate

        require_gate("engage the Wave kill switch", self._kill_confirm)

    def _kill_confirm(self) -> None:
        # gate first, THEN confirm: the popup states exactly what fires
        from waveapp.ui.positions_page import _ConfirmPopup

        self._confirm = _ConfirmPopup(
            self.window(),
            "Flatten every position, cancel every order, and halt the engine?",
            "Kill",
            self.kill_confirmed.emit,
        )
        self._confirm.show()
        self._confirm.raise_()

    def _rearm_clicked(self) -> None:
        from waveapp.security.gate import require_gate

        require_gate("re-arm trading after the weekly-loss halt", self.rearm_requested.emit)

    def attach_status(self, status) -> None:
        """Ride the same ConnectionStatus signal as the sidebar dots."""
        status.changed.connect(self._on_status)

    def _on_status(self, name: str, color: str, tooltip: str) -> None:
        row = self.status_rows.get(name)
        if row is not None:
            row.set_state(color, tooltip)

    def set_engine_state(self, state: str) -> None:
        """Instant (rides the same engine-state signal as the top bar) —
        the 30s snapshot was too slow (8.8 r5)."""
        self.engine_state_label.setText(state)

    def set_open_positions(self, count: int) -> None:
        """Driven by what the Positions tab DISPLAYS (engine or Test-tab
        fakes) so the two tabs always agree (8.8 r5)."""
        self.engine_positions_label.setText(f"{count} open position{'s' if count != 1 else ''}")

    def update_system(self, data: dict) -> None:
        """The monitor's snapshot (every poll)."""
        self._data = data
        self._ages_received_at = time.monotonic()
        self.engine_state_label.setText(str(data.get("engine_state") or "offline"))
        llm = data.get("llm") or {}
        if llm.get("enabled"):
            balance = llm.get("balance")
            balance_text = (
                f" · est. balance ${balance:.2f} of ${llm.get('budget', 0):.0f}"
                if balance is not None
                else ""
            )
            self.llm_label.setText(
                f"news brain: on · {llm.get('calls_today', 0)} headlines judged ·"
                f" ${llm.get('spend_today', 0):.3f} / ${llm.get('cap', 0):.2f} today" + balance_text
            )
        elif "llm" in data:
            self.llm_label.setText("news brain: off (no API key)")
        regime = data.get("market_regime") or {}
        name = str(regime.get("regime", ""))
        if name and name != "WARMUP":
            self.market_regime_label.setText(
                f"tape: {name.replace('_', ' ')} ({float(regime.get('score', 0) or 0):+.2f})"
            )
        else:
            self.market_regime_label.setText("")
        db = data.get("db") or {}
        if db:
            self.db_label.setText(
                f"{db.get('name', '—')} — schema v{db.get('schema', '?')},"
                f" {db.get('size_mb', 0):.1f} MB"
            )
        self.version_label.setText(f"Wave {data.get('version', '')}")
        uptime = data.get("uptime_seconds")
        if uptime is not None:
            hours, rest = divmod(int(uptime), 3600)
            minutes = rest // 60
            errors = data.get("errors_today")
            errors_text = (
                f" · {errors} error{'s' if errors != 1 else ''} today" if errors is not None else ""
            )
            self.uptime_label.setText(f"up {hours}h {minutes:02d}m{errors_text}")
        risk = data.get("risk") or {}
        halt = str(risk.get("halt", "none"))
        halted = halt not in ("none", "NONE")
        self.halt_label.setText("halted: " + halt.replace("_", " ") if halted else "no halts")
        self.halt_label.setStyleSheet(
            f"font-size: 16px; font-weight: 700; background: transparent;"
            f" color: {theme.RED if halted else theme.GREEN};"
        )
        self.rearm_button.setVisible(halt == "weekly_loss")
        freezes = risk.get("freezes") or []
        self.freeze_label.setText(
            "entries frozen: " + ", ".join(freezes) if freezes else "entries flowing"
        )
        limits = risk.get("limits") or {}
        if limits:
            extras = ""
            if limits.get("max_notional_pct"):
                extras += f" · ≤{limits['max_notional_pct']:g}% notional"
            if limits.get("impact_participation_pct"):
                extras += f" · ≤{limits['impact_participation_pct']:g}% of daily volume"
            if limits.get("shorts_enabled"):  # S4: invisible until it is flipped manually
                share = float(limits.get("short_risk_share") or 0.5) * 100.0
                extras += f" · shorts ON (≤{share:g}% book)"
            gates = risk.get("gates") or {}
            if gates:
                # the ADOPTED behavior gates — the card tells the whole truth
                cutoff = int(gates.get("cutoff_minutes", 0))
                extras += (
                    f"\n${gates.get('min_entry_price', 0):g} floor · trend gate ·"
                    f" no entries <{cutoff}min runway · 1 trade/symbol/day"
                )
            _mp = limits.get("max_positions", 0) or 0
            # max_positions <= 0 = unlimited count (2026-09-21);
            # the total-open-risk ceiling governs the book size instead
            _pos_txt = "unlimited positions (risk-capped)" if _mp <= 0 else f"max {_mp} positions"
            self.limits_label.setText(
                f"{limits.get('risk_per_trade_pct', 0):g}% / trade · "
                f"{limits.get('max_daily_loss_pct', 0):g}% day · "
                f"{limits.get('max_weekly_loss_pct', 0):g}% week · "
                f"{_pos_txt}" + extras
            )
        market = data.get("market_data") or {}
        if market:
            feed = market.get("feed") or market.get("configured_feed") or "iex"
            live = bool(market.get("stream_live"))
            tier = "SIP — Algo Trader Plus" if feed == "sip" else "IEX — free plan"
            self.feed_tier_label.setText(tier)
            self.feed_tier_label.setStyleSheet(
                f"font-size: 16px; font-weight: 700; background: transparent;"
                f" color: {theme.GREEN if live else theme.TEXT_MUTED};"
            )
            if live:
                budget = market.get("budget")
                self.feed_detail_label.setText(
                    f"stream live · watching {market.get('watched', 0)} dynamic symbols"
                    + (f" · budget {budget} channels" if budget else "")
                )
            else:
                self.feed_detail_label.setText("stream offline")
            configured = market.get("configured_feed")
            if configured and market.get("feed") and configured != market.get("feed"):
                self.feed_detail_label.setText(
                    self.feed_detail_label.text()
                    + f" · {configured.upper()} configured — relaunch to apply"
                )
            polygon = str(market.get("polygon", "—"))
            self.polygon_label.setText(f"Polygon history: {polygon}")
            self.polygon_label.setStyleSheet(
                f"font-size: 12px; background: transparent; color:"
                f" {theme.GREEN if polygon == 'connected' else theme.TEXT_MUTED};"
            )
        trading = data.get("trading") or {}
        auto = trading.get("auto_trade")
        if auto is not None:
            self.auto_trade_label.setText("AUTO-TRADE ON" if auto else "AUTO-TRADE OFF")
            self.auto_trade_label.setStyleSheet(
                f"font-size: 16px; font-weight: 700; background: transparent;"
                f" color: {theme.GREEN if auto else theme.ORANGE_LIVE};"
            )
            s2_symbols = int(trading.get("scanner2_symbols") or 0)
            if s2_symbols:
                # Scanner 2.0 truth (2026-09-02: the old "1500 every
                # 120s" line described the retired legacy scan)
                stream = "streaming" if trading.get("scanner2_stream_fresh") else "REST repair"
                self.scanner_label.setText(
                    f"scanner 2.0: {s2_symbols:,} symbols on the SIP {stream} ·"
                    f" menu re-ranked every second"
                    + ("" if auto else " — signals only until Phase 10")
                )
            else:
                self.scanner_label.setText(
                    f"scanning {trading.get('scan_universe', 0)} symbols every"
                    f" {trading.get('scan_interval', 0)}s"
                    + ("" if auto else " — signals only until Phase 10")
                )
            last_scan = trading.get("last_scan")
            self.last_scan_label.setText(f"last scan {last_scan}" if last_scan else "no scan yet")
            ladder = trading.get("ladder") or {}
            if ladder.get("posted"):
                self.ladder_label.setText(
                    f"entry ladder: {ladder.get('posted', 0)} posted ·"
                    f" {ladder.get('captured', 0)} captured mid ·"
                    f" {ladder.get('fallback', 0)} fell back to market"
                )
            elif "ladder" in trading:
                self.ladder_label.setText("entry ladder: armed — no entries yet")
        # ml stats route to the Brain tab now (MLPage.update_ml)
        fees = data.get("fees") or []
        if fees:
            units = {
                "usd_per_million_sold": "per $1M sold",
                "usd_per_share_sold": "per share sold",
            }
            lines = []
            for fee in fees:
                cap = f", cap ${fee['cap']:.2f}" if fee.get("cap") else ""
                unit = units.get(fee["unit"], fee["unit"])
                lines.append(
                    f"{fee['fee_name']}\n  ${fee['rate']:g} {unit}{cap}"
                    f" (since {fee['effective_date']})"
                )
            self.fees_label.setText("\n".join(lines))
        self._tick()

    # -- local ticking (countdown + heartbeat re-aging) -----------------------

    def _refresh_targets(self) -> None:
        """Boundary scans are heavier than a tick — refresh once a minute."""
        from waveapp.engine.session import SessionScheduler

        self._clock_info = SessionScheduler.market_clock()
        self._closed_boundary, self._holiday_name = SessionScheduler.next_closed_info()
        self._targets_refreshed = time.monotonic()

    def _tick(self) -> None:
        from waveapp.engine.session import SessionScheduler

        now = datetime.now(UTC)
        try:
            stale = (
                self._targets_refreshed is None
                or time.monotonic() - self._targets_refreshed > 60
                or (self._clock_info and now >= self._clock_info["target"])
                or (self._closed_boundary and now >= self._closed_boundary)
            )
            if stale:
                self._refresh_targets()

            info = SessionScheduler.info()
            lull = " · midday lull" if info.is_lull else ""
            self.regime_label.setText(
                f"{info.regime.value.replace('_', ' ')} · {info.et_time:%H:%M:%S} ET{lull}"
            )

            clock = self._clock_info
            if clock:
                remaining = max(0, int((clock["target"] - now).total_seconds()))
                span = max(1, int((clock["target"] - clock["anchor"]).total_seconds()))
                hours, rest = divmod(remaining, 3600)
                minutes, seconds = divmod(rest, 60)
                opens = clock["mode"] == "opens"
                self.clock_caption.setText("MARKET OPENS IN" if opens else "MARKET CLOSES IN")
                self.clock_ring.set_state(
                    1.0 - remaining / span,
                    f"{hours}:{minutes:02d}:{seconds:02d}",
                    theme.BLUE if opens else theme.GREEN,
                )
                target_et = clock["target"].astimezone(_ET_ZONE)
                self.clock_target_label.setText(f"{target_et:%a %H:%M} ET")

            if self._closed_boundary:
                from waveapp.engine.session import Regime, SessionScheduler

                if SessionScheduler.regime() is Regime.CLOSED:
                    # the market IS closed: count down to the week OPENING
                    # (2026-08-22, screenshot: the old card counted
                    # to "the next closed minute" — always 1 min away)
                    target = SessionScheduler.next_open_boundary()
                    self._week_title.setText("WEEK OPENS")
                    remaining = max(0, int((target - now).total_seconds()))
                    target_et = target.astimezone(_ET_ZONE)
                    # Sun 20:00 ET opens the trading WEEK (24/5 venue: data +
                    # exit management). The scanner sleeps until Monday 04:00
                    # (overnight is exit-only, §6) and the market-clock card
                    # owns the real RTH open (2026-08-23 round 2 — my
                    # "scanner wakes" label here was wrong)
                    reason = f"trading week opens — {target_et:%a %H:%M} ET"
                else:
                    target = self._closed_boundary
                    self._week_title.setText("WEEK CLOSE")
                    remaining = max(0, int((target - now).total_seconds()))
                    boundary_et = target.astimezone(_ET_ZONE)
                    reason = f"closes for the weekend — {boundary_et:%a %H:%M} ET"
                days, rest = divmod(remaining, 86400)
                hours, rest = divmod(rest, 3600)
                minutes, seconds = divmod(rest, 60)
                prefix = f"{days}d " if days else ""
                self.week_countdown_label.setText(
                    f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"
                )
                # the holiday name belongs to the NEXT closure — show it only
                # while counting TOWARD that closure, otherwise a plain
                # weekend wears next week's holiday (2026-08-23)
                counting_to_close = self._week_title.text() == "WEEK CLOSE"
                if self._holiday_name and counting_to_close:
                    self.week_reason_label.setText(f"holiday — {self._holiday_name}")
                    self.week_reason_label.setStyleSheet(
                        f"font-size: 12px; font-weight: 600; color: {theme.ORANGE_LIVE};"
                        f" background: transparent;"
                    )
                else:
                    self.week_reason_label.setText(reason)
        except Exception:
            self.regime_label.setText("—")

        ages = (self._data or {}).get("feed_ages") or {}
        elapsed = time.monotonic() - self._ages_received_at if self._ages_received_at else None
        for channel, label in self.feed_rows.items():
            age = ages.get(channel)
            if elapsed is None:
                label.setText("—")
            elif age is None:
                label.setText("no messages yet")  # connected but quiet
            else:
                label.setText(f"{age + elapsed:.0f}s ago")
        self._refresh_research()

    # -- page slide (10.4) -----------------------------------------------------

    def set_ml_status(self, text: str) -> None:
        # The ML sub-page left the System tab 2026-08-30; the status line
        # lives on MLPage now. Guarded no-op so a stray caller can never
        # raise across the Qt signal boundary (qFatal abort — audit A5-1).
        label = getattr(self, "ml_status_label", None)
        if label is None:
            logger.debug("set_ml_status ignored (no ml_status_label): %s", text)
            return
        label.setText(text)

    def _refresh_sys_dots(self) -> None:
        for index, dot in enumerate(self.sys_dots):
            dot.active = index == self._sys_page_index
            dot.update()

    def _layout_sys_pages(self) -> None:
        width = self.page_host.width()
        height = self.page_host.height()
        for index, page in enumerate(self._sys_pages):
            if index == self._sys_page_index:
                page.setGeometry(0, 0, width, height)
                page.show()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_sys_pages()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._layout_sys_pages()

    def set_system_page(self, index: int) -> None:
        """Dots switch pages with the Positions-tab slide."""
        index = max(0, min(index, len(self._sys_pages) - 1))
        if index == self._sys_page_index:
            return
        from PyQt6 import sip

        direction = 1 if index > self._sys_page_index else -1
        old_page = self._sys_pages[self._sys_page_index]
        new_page = self._sys_pages[index]
        self._sys_page_index = index
        self._refresh_sys_dots()
        width = self.page_host.width()
        height = self.page_host.height()
        if width < 50 or not self.isVisible():
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
            if not sip.isdeleted(old_page):
                old_page.hide()

        slide_in.finished.connect(_done)
        slide_out.start()
        slide_in.start()
        self._slide_keep = (slide_out, slide_in)

    def _refresh_research(self) -> None:
        """Research card: reads the progress file research scripts write.
        Stale (>5 min, unfinished) or missing reports show as idle."""
        from waveapp.research.progress import read

        data = read()
        if data is None:
            self.research_bar.setValue(0)
            self.research_label.setText("no research running")
            return
        finished = bool(data.get("finished"))
        total = max(1, int(data.get("total", 1)))
        done = int(data.get("done", 0))
        # a RUNNING report never fills the bar — full means DONE, nothing else
        # (a between-stages 1/1 report read as "finished")
        percent = 100 if finished else max(0, min(99, int(done / total * 100)))
        self.research_bar.setValue(percent)
        color = theme.GREEN if finished else theme.BLUE
        self.research_bar.setStyleSheet(
            "QProgressBar { background: rgba(0, 0, 0, 0.08); border: none;"
            " border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )
        self.research_label.setText(str(data.get("label", "")))

    # -- depth panel ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(12.5, 12.5, self.width() - 25, self.height() - 25)
        painter.setPen(QPen(QColor(0, 0, 0, 26), 1))
        painter.setBrush(QColor(0, 0, 0, 13))
        painter.drawRoundedRect(rect, 14, 14)
