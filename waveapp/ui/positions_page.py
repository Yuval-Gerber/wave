"""Positions tab (Phase 8.4, §5): a grid of live position cards —
symbol, side, qty, entry, live P&L ($ and %), current stop, exit-stage badge
(BE / TRAIL / SCALED), strategy tag, SSR/halt badge — with pagination dots
when cards overflow the grid. The engine feeds `update_positions` snapshots;
the UI only observes (§3).

Position snapshot dict:
{key, symbol, side: "long"|"short", qty, entry, last, stop,
 stage: ""|"BE"|"TRAIL"|"SCALED", strategy, halted: bool, ssr: bool}
"""

from __future__ import annotations

import logging
import math

from PyQt6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme

logger = logging.getLogger("wave.ui.positions")

CARD_W, CARD_H, GAP = 300, 176, 16


class _Chip(QLabel):
    """Small rounded tag (side, strategy, stage, halt)."""

    def __init__(self, text: str = "", parent: QWidget | None = None, scale: float = 1.0) -> None:
        super().__init__(text, parent)
        self._scale = scale
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_colors(theme.TEXT_MUTED, "rgba(0, 0, 0, 0.06)")

    def set_colors(self, fg: str, bg: str) -> None:
        s = self._scale
        self.setStyleSheet(
            f"color: {fg}; background: {bg}; border-radius: {int(8 * s)}px;"
            f" padding: {max(1, int(s))}px {int(8 * s)}px;"
            f" font-size: {int(10 * s)}px; font-weight: 700;"
        )


class PositionCard(QFrame):
    """One open position. Fields update in place. `scale` > 1 renders the
    SAME card bigger (the round-4 double-click popup). A SELL button fires
    immediately (positions close independently); double-click opens the
    popup."""

    sell_clicked = pyqtSignal()
    opened = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, scale: float = 1.0) -> None:
        super().__init__(parent)
        self._scale = scale
        self.setProperty("card", True)
        self.setFixedSize(int(CARD_W * scale), int(CARD_H * scale))

        def px(value: float) -> int:
            return max(1, int(value * scale))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(18), px(14), px(18), px(14))
        layout.setSpacing(px(6))

        header = QHBoxLayout()
        header.setSpacing(px(8))
        self.symbol = QLabel("—")
        self.symbol.setStyleSheet(
            f"font-size: {px(19)}px; font-weight: 700; color: {theme.TEXT};"
            f" background: transparent;"
        )
        self.side_chip = _Chip(scale=scale)
        self.strategy_chip = _Chip(scale=scale)
        self.brain_chip = _Chip(scale=scale)  # M2 advisory: the Brain's read
        self.brain_chip.hide()
        self.halt_chip = _Chip(scale=scale)
        header.addWidget(self.symbol)
        header.addWidget(self.side_chip)
        header.addWidget(self.strategy_chip)
        header.addWidget(self.brain_chip)
        header.addStretch(1)
        header.addWidget(self.halt_chip)
        layout.addLayout(header)

        pnl_row = QHBoxLayout()
        pnl_row.setSpacing(px(8))
        self.pnl = QLabel("—")
        self.pnl_pct = QLabel("")
        pnl_row.addWidget(self.pnl)
        pnl_row.addWidget(self.pnl_pct, alignment=Qt.AlignmentFlag.AlignBottom)
        pnl_row.addStretch(1)
        layout.addLayout(pnl_row)

        self.detail = QLabel("")
        self.detail.setStyleSheet(
            f"font-size: {px(12)}px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.detail)

        stop_row = QHBoxLayout()
        stop_row.setSpacing(px(8))
        self.stop_label = QLabel("")
        self.stop_label.setStyleSheet(
            f"font-size: {px(12)}px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        self.stage_chip = _Chip(scale=scale)
        self.stance_chip = _Chip(scale=scale)  # the judge's live opinion
        self._stance_pulse_on = False
        self._stance_timer = QTimer(self)
        self._stance_timer.timeout.connect(self._pulse_stance)
        stop_row.addWidget(self.stop_label, 1)  # label gets the width first
        stop_row.addWidget(self.stance_chip)
        stop_row.addWidget(self.stage_chip)
        self.sell_button = QPushButton("SELL")
        self.sell_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sell_button.setStyleSheet(
            f"QPushButton {{ color: {theme.RED}; background: rgba(255, 59, 48, 0.09);"
            f" border: 1px solid rgba(255, 59, 48, 0.35); border-radius: 7px;"
            f" padding: {px(3)}px {px(14)}px; font-size: {px(11)}px; font-weight: 700; }}"
            f"QPushButton:hover {{ background: rgba(255, 59, 48, 0.16); }}"
        )
        self.sell_button.clicked.connect(self.sell_clicked)
        stop_row.addWidget(self.sell_button)
        layout.addLayout(stop_row)
        layout.addStretch(1)

    _STANCE_COLORS = {
        "RIDE": ("#28CD41", "rgba(40, 205, 65, 0.12)"),
        "READY": ("#FF9500", "rgba(255, 149, 0, 0.16)"),
        "WAIT": ("#6E6E73", "rgba(110, 110, 115, 0.12)"),
        "BANK": ("#007AFF", "rgba(0, 122, 255, 0.12)"),
        "CUT": ("#FF3B30", "rgba(255, 59, 48, 0.14)"),
    }

    def _set_stance_chip(self, stance) -> None:
        """The judge's opinion as a colored chip. READY pulses — the card
        blinks when the judge is armed to take (2026-09-21)."""
        chip = getattr(self, "stance_chip", None)
        if chip is None:
            return
        if not stance:
            chip.setVisible(False)
            self._stance_timer.stop()
            return
        fg, bg = self._STANCE_COLORS.get(stance, ("#6E6E73", "rgba(110,110,115,0.12)"))
        chip.setText(f"⚖ {stance}")
        chip.set_colors(fg, bg)
        chip.setVisible(True)
        if stance == "READY":
            if not self._stance_timer.isActive():
                self._stance_timer.start(450)
        else:
            self._stance_timer.stop()
            chip.setVisible(True)

    def _pulse_stance(self) -> None:
        self._stance_pulse_on = not self._stance_pulse_on
        chip = getattr(self, "stance_chip", None)
        if chip is not None:
            chip.setVisible(self._stance_pulse_on)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.opened.emit()

    def update_mark(self, last: float) -> None:
        """250ms light tick (blueprint 9.2): only the numbers that move with
        the market — P&L, %, and the 'last' readout. Everything else waits
        for the 1s full snapshot."""
        position = getattr(self, "_position", None)
        if not position or not last:
            return
        position["last"] = last
        entry = position.get("entry") or 0.0
        qty = position.get("qty", 0)
        direction = 1.0 if position.get("side", "long") == "long" else -1.0
        pnl_dollars = (last - entry) * qty * direction if entry else 0.0
        pnl_pct = ((last - entry) / entry * 100.0 * direction) if entry else 0.0
        color = theme.GREEN if pnl_dollars >= 0 else theme.RED
        scale = self._scale
        self.pnl.setText(f"{pnl_dollars:+,.2f}")
        self.pnl.setStyleSheet(
            f"font-size: {int(22 * scale)}px; font-weight: 700; color: {color};"
            f" background: transparent;"
        )
        self.pnl_pct.setText(f"{pnl_pct:+.2f}%")
        self.pnl_pct.setStyleSheet(
            f"font-size: {int(13 * scale)}px; font-weight: 600; color: {color};"
            f" background: transparent;"
        )
        sell = self._position.get("sellable") if getattr(self, "_position", None) else None
        tail = f"   sell {sell:,.2f}" if sell else ""
        self.detail.setText(f"{qty:g} @ {entry:,.2f}   last {last:,.2f}{tail}")

    def update_data(self, position: dict) -> None:
        self._position = dict(position)  # kept for the 250ms mark ticks (9.2)
        symbol = position.get("symbol", "—")
        side = position.get("side", "long")
        qty = position.get("qty", 0)
        entry = position.get("entry") or 0.0
        last = position.get("last") or entry
        stop = position.get("stop")
        stage = position.get("stage", "")
        strategy = position.get("strategy", "")
        halted = bool(position.get("halted"))
        ssr = bool(position.get("ssr"))

        self.symbol.setText(symbol)
        long_side = side == "long"
        self.side_chip.setText("LONG" if long_side else "SHORT")
        self.side_chip.set_colors(
            theme.BLUE if long_side else "#AF52DE",  # systemPurple for shorts
            "rgba(0, 122, 255, 0.10)" if long_side else "rgba(175, 82, 222, 0.12)",
        )
        # S4: closing a short is a buy-to-cover — same close_now action,
        # honest label (the "keep it simple")
        self.sell_button.setText("SELL" if long_side else "COVER")
        self.strategy_chip.setVisible(bool(strategy))
        self.strategy_chip.setText(strategy.upper())
        # M2 advisory (blueprint 8.11): the Brain's entry-time P(win) — shown on
        # the card, ignored by Wave (shadow stays shadow until M3's evidence)
        brain = position.get("brain")
        if brain is not None:
            pct = float(brain) * 100.0
            self.brain_chip.setText(f"🧠 {pct:.0f}%")
            self.brain_chip.set_colors(
                theme.GREEN if pct >= 55 else ("#8E8E93" if pct >= 45 else theme.RED),
                "rgba(0, 0, 0, 0.05)",
            )
            self.brain_chip.show()
        else:
            self.brain_chip.hide()

        if halted:
            self.halt_chip.setText("HALT")
            self.halt_chip.set_colors("#FFFFFF", theme.RED)
        elif ssr:
            self.halt_chip.setText("SSR")
            self.halt_chip.set_colors("#FFFFFF", theme.ORANGE_LIVE)
        self.halt_chip.setVisible(halted or ssr)

        direction = 1.0 if long_side else -1.0
        pnl_dollars = (last - entry) * qty * direction if entry else 0.0
        pnl_pct = ((last - entry) / entry * 100.0 * direction) if entry else 0.0
        color = theme.GREEN if pnl_dollars >= 0 else theme.RED
        scale = self._scale
        self.pnl.setText(f"{pnl_dollars:+,.2f}")
        self.pnl.setStyleSheet(
            f"font-size: {int(22 * scale)}px; font-weight: 700; color: {color};"
            f" background: transparent;"
        )
        self.pnl_pct.setText(f"{pnl_pct:+.2f}%")
        self.pnl_pct.setStyleSheet(
            f"font-size: {int(13 * scale)}px; font-weight: 600; color: {color};"
            f" background: transparent;"
        )

        sell = position.get("sellable")
        tail = f"   sell {sell:,.2f}" if sell else ""
        # 2026-09-21 ("[J] overlaps and why do I need it?"): the duel
        # is over — every position is judge-managed, so the badge said
        # nothing and overflowed the line. The stance lives in its own
        # colored chip now (and PULSES when the judge is armed to sell).
        self.detail.setText(f"{qty:g} @ {entry:,.2f}   last {last:,.2f}{tail}")
        stance = position.get("stance")
        self._set_stance_chip(stance)
        stop_text, stop_state = stop_goal_text(entry, stop, long_side, qty)
        self.stop_label.setText(stop_text)
        self.stop_label.setStyleSheet(
            f"font-size: {int(12 * scale)}px;"
            f" color: {STOP_STATE_COLORS[stop_state]};"
            f" background: transparent;"
        )
        self.stage_chip.setVisible(bool(stage))
        # (stance chip updated in _set_stance_chip)
        if stage:
            self.stage_chip.setText(stage)
            self.stage_chip.set_colors(theme.GREEN, "rgba(40, 205, 65, 0.12)")


def stop_goal_text(entry: float, stop: float, long_side: bool, qty: float = 0) -> tuple[str, str]:
    """'sells at X · Y $' — the TRUTH about exits (2026-08-27/28): the
    resting stop/trail is the only guaranteed sell price, and Y is what that
    sale means in DOLLARS for the invested amount (unsigned — the color says
    the direction). Returns (text, state): "loss" → red,
    "breakeven" → regular, "profit" → green."""
    verb = "sells" if long_side else "covers"  # S4: a short's stop BUYS back
    if not stop or not entry:
        return f"{verb} at —", "loss"
    band = entry * 0.0015  # ±0.15% counts as breakeven (fees buffer zone)
    edge = (stop - entry) if long_side else (entry - stop)
    dollars = ""
    if qty:
        amount = edge * qty
        dollars = f" · {abs(amount):,.0f} $"
    if edge > band:
        state = "profit"
        text = f"{verb} at {stop:,.2f}{dollars} ✓ locked"
    elif edge >= -band:
        state = "breakeven"
        text = f"{verb} at {stop:,.2f}{dollars} · breakeven"
    else:
        state = "loss"
        text = f"{verb} at {stop:,.2f}{dollars}"
    return text, state


STOP_STATE_COLORS = {"loss": theme.RED, "breakeven": theme.TEXT_MUTED, "profit": theme.GREEN}


class _CandleChart(QWidget):
    """Intraday candlestick chart (round 6 — candles preferred): green up
    / red down bodies with wicks, dashed entry line. No axes — Apple-clean.

    Accepts candles as (open, high, low, close) tuples; bare floats are
    tolerated and rendered as flat candles."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(240)
        self._candles: list[tuple[float, float, float, float]] = []
        self._times: list[float | None] = []
        self._entry: float | None = None
        self._stop: float | None = None
        self._target: float | None = None
        self._entry_index: int | None = None
        # hover tooltip (2026-08-24): time/date + prices per candle
        self._hover_index: int | None = None
        self.setMouseTracking(True)

    def set_data(
        self,
        candles: list,
        entry: float | None,
        stop: float | None = None,
        target: float | None = None,
        entry_index: int | None = None,
    ) -> None:
        self._entry_index = entry_index
        normalized = []
        times: list[float | None] = []
        for item in candles:
            if isinstance(item, int | float):
                value = float(item)
                normalized.append((value, value, value, value))
                times.append(None)
            else:
                row = tuple(item)
                if len(row) >= 5:  # (ts, o, h, l, c) — ts feeds the tooltip
                    stamp = row[0]
                    ts = stamp.timestamp() if hasattr(stamp, "timestamp") else float(stamp)
                    times.append(ts)
                    row = row[1:]
                else:
                    times.append(None)
                o, h, low_, c = (float(v) for v in row[:4])
                normalized.append((o, h, low_, c))
        self._candles = normalized
        self._times = times
        if self._hover_index is not None and self._hover_index >= len(normalized):
            self._hover_index = None
        self._entry = entry
        self._stop = stop
        self._target = target
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if len(self._candles) < 2:
            return
        pad = 8.0
        slot = (self.width() - pad * 2) / len(self._candles)
        index = int((event.position().x() - pad) / slot) if slot > 0 else -1
        index = index if 0 <= index < len(self._candles) else None
        if index != self._hover_index:
            self._hover_index = index
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        if self._hover_index is not None:
            self._hover_index = None
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if len(self._candles) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad = 8.0
        w = self.width() - pad * 2
        h = self.height() - pad * 2
        lows = [c[2] for c in self._candles]
        highs = [c[1] for c in self._candles]
        # scale to the PRICE ACTION; a level (stop/entry/goal) only widens
        # the scale when it's near the candles — a wide trail stop squashed
        # the candles into a sliver (a GDX screenshot, 2026-08-19).
        # Far-away levels get pinned to the chart edge below instead.
        price_low, price_high = min(lows), max(highs)
        price_span = (price_high - price_low) or (price_low * 0.002) or 1.0
        for level in (self._entry, self._stop, self._target):
            if level and (price_low - price_span) <= level <= (price_high + price_span):
                lows.append(level)
                highs.append(level)
        low, high = min(lows), max(highs)
        span = (high - low) or 1.0
        low -= span * 0.05  # breathing room so extremes aren't glued to edges
        high += span * 0.05
        span = high - low

        def y_of(price: float) -> float:
            return pad + h * (1 - (price - low) / span)

        count = len(self._candles)
        slot = w / count
        body_w = max(2.0, min(9.0, slot * 0.7))
        up = QColor(theme.GREEN)
        down = QColor(theme.RED)
        for index, (open_, high_, low_, close) in enumerate(self._candles):
            x_center = pad + slot * (index + 0.5)
            color = up if close >= open_ else down
            # wick
            painter.setPen(QPen(color, 1.2))
            painter.drawLine(QPointF(x_center, y_of(high_)), QPointF(x_center, y_of(low_)))
            # body (at least 1px tall so dojis stay visible)
            top = y_of(max(open_, close))
            bottom = y_of(min(open_, close))
            height = max(1.2, bottom - top)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x_center - body_w / 2, top, body_w, height), 1.0, 1.0)
        # dashed levels (8.5): entry gray, stop red, target green
        levels = (
            (self._entry, QColor(0, 0, 0, 100)),
            (self._stop, QColor(255, 59, 48, 170)),
            (self._target, QColor(40, 205, 65, 170)),
        )
        for level, color in levels:
            if not level:
                continue
            level_pen = QPen(color, 1.2)
            level_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(level_pen)
            # off-scale levels pin to the edge: still says "stop far below"
            y = min(max(y_of(level), pad), pad + h)
            painter.drawLine(QPointF(pad, y), QPointF(pad + w, y))

        # entry marker (2026-08-21): WHERE Wave bought — a soft blue
        # column through the entry candle + a ▲ flag beneath it
        idx = self._entry_index
        if idx is not None and 0 <= idx < len(self._candles):
            x_center = pad + slot * (idx + 0.5)
            column = QColor(theme.BLUE)
            column.setAlphaF(0.10)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(column)
            painter.drawRoundedRect(
                QRectF(x_center - max(2.5, body_w * 0.8), pad, max(5.0, body_w * 1.6), h),
                2.0,
                2.0,
            )
            flag = QColor(theme.BLUE)
            flag.setAlphaF(0.95)
            painter.setBrush(flag)
            tip_y = pad + h - 2.0
            size = max(4.0, body_w * 0.9)
            triangle = [
                QPointF(x_center, tip_y - size),
                QPointF(x_center - size * 0.8, tip_y),
                QPointF(x_center + size * 0.8, tip_y),
            ]
            from PyQt6.QtGui import QPolygonF

            painter.drawPolygon(QPolygonF(triangle))

        # hover tooltip (2026-08-24): the candle under the cursor —
        # date, time and its four prices, in a small chip that stays inside
        # the chart
        hover = self._hover_index
        if hover is not None and 0 <= hover < len(self._candles):
            open_, high_, low_, close = self._candles[hover]
            x_center = pad + slot * (hover + 0.5)
            highlight = QPen(QColor(0, 0, 0, 60), 1, Qt.PenStyle.DashLine)
            painter.setPen(highlight)
            painter.drawLine(QPointF(x_center, pad), QPointF(x_center, pad + h))
            stamp = self._times[hover] if hover < len(self._times) else None
            if stamp is not None:
                from datetime import UTC, datetime
                from zoneinfo import ZoneInfo

                moment = datetime.fromtimestamp(float(stamp), UTC).astimezone(
                    ZoneInfo("America/New_York")
                )
                when = moment.strftime("%b %d · %H:%M ET")
            else:
                when = f"bar {hover + 1} of {len(self._candles)}"
            lines = [
                when,
                f"open  {open_:,.2f}   high {high_:,.2f}",
                f"close {close:,.2f}   low  {low_:,.2f}",
            ]
            font = self.font()
            font.setPointSizeF(10.5)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            chip_w = max(metrics.horizontalAdvance(t) for t in lines) + 20
            chip_h = len(lines) * (metrics.height() + 1) + 12
            chip_x = x_center + 12
            if chip_x + chip_w > pad + w:
                chip_x = x_center - 12 - chip_w
            chip_y = pad + 6
            painter.setPen(QPen(QColor(0, 0, 0, 28), 1))
            painter.setBrush(QColor(250, 250, 252, 242))
            painter.drawRoundedRect(QRectF(chip_x, chip_y, chip_w, chip_h), 8.0, 8.0)
            painter.setPen(QColor(theme.TEXT))
            text_y = chip_y + 6 + metrics.ascent()
            for i, line in enumerate(lines):
                if i == 0:
                    painter.setPen(QColor(theme.TEXT_MUTED))
                else:
                    painter.setPen(QColor(theme.TEXT))
                painter.drawText(QPointF(chip_x + 10, text_y), line)
                text_y += metrics.height() + 1


class PositionDetailCard(QFrame):
    """The double-click card (round 5): SOLID white, same design language as
    the small card but bigger, plus the full company name and the intraday
    graph — or, when the market is closed, the login wave + 'market is
    closed'."""

    sell_clicked = pyqtSignal()
    tighten_clicked = pyqtSignal()
    WIDTH, HEIGHT = 640, 600  # roomier chart (2026-08-19)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailCard")
        self.setStyleSheet(
            "#detailCard { background: #FFFFFF;"  # solid (no opacity)
            " border: 1px solid rgba(0, 0, 0, 0.12); border-radius: 14px; }"
        )
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        scale = 1.35

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 20, 26, 20)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.symbol = QLabel("—")
        self.symbol.setStyleSheet(
            f"font-size: 27px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        self.side_chip = _Chip(scale=scale)
        self.strategy_chip = _Chip(scale=scale)
        self.halt_chip = _Chip(scale=scale)
        header.addWidget(self.symbol)
        header.addWidget(self.side_chip)
        header.addWidget(self.strategy_chip)
        header.addWidget(self.halt_chip)
        header.addStretch(1)
        self.tighten_button = QPushButton("TIGHTEN STOP")
        self.tighten_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tighten_button.setStyleSheet(
            f"QPushButton {{ color: {theme.BLUE}; background: rgba(0, 122, 255, 0.08);"
            f" border: 1px solid rgba(0, 122, 255, 0.35); border-radius: 8px;"
            f" padding: 5px 14px; font-size: 13px; font-weight: 700; }}"
            f"QPushButton:hover {{ background: rgba(0, 122, 255, 0.15); }}"
        )
        self.tighten_button.clicked.connect(self.tighten_clicked)
        header.addWidget(self.tighten_button)
        self.sell_button = QPushButton("SELL")
        self.sell_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sell_button.setStyleSheet(
            f"QPushButton {{ color: {theme.RED}; background: rgba(255, 59, 48, 0.09);"
            f" border: 1px solid rgba(255, 59, 48, 0.35); border-radius: 8px;"
            f" padding: 5px 18px; font-size: 13px; font-weight: 700; }}"
            f"QPushButton:hover {{ background: rgba(255, 59, 48, 0.16); }}"
        )
        self.sell_button.clicked.connect(self.sell_clicked)
        header.addWidget(self.sell_button)
        layout.addLayout(header)

        self.name = QLabel("")
        self.name.setStyleSheet(
            f"font-size: 14px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.name)

        pnl_row = QHBoxLayout()
        pnl_row.setSpacing(10)
        self.pnl = QLabel("—")
        self.pnl_pct = QLabel("")
        pnl_row.addWidget(self.pnl)
        pnl_row.addWidget(self.pnl_pct, alignment=Qt.AlignmentFlag.AlignBottom)
        pnl_row.addStretch(1)
        layout.addLayout(pnl_row)

        self.detail = QLabel("")
        self.detail.setStyleSheet(
            f"font-size: 14px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.detail)

        stop_row = QHBoxLayout()
        stop_row.setSpacing(10)
        self.stop_label = QLabel("")
        self.stop_label.setStyleSheet(
            f"font-size: 14px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        self.stage_chip = _Chip(scale=scale)
        stop_row.addWidget(self.stop_label)
        stop_row.addWidget(self.stage_chip)
        stop_row.addStretch(1)
        layout.addLayout(stop_row)

        # graph when the market is live; the login wave + note when closed
        self.chart = _CandleChart()
        layout.addWidget(self.chart, stretch=1)
        from waveapp.ui.login_window import HeroCanvas

        self.closed_box = QWidget()
        self.closed_box.setStyleSheet("background: transparent;")
        closed_layout = QVBoxLayout(self.closed_box)
        closed_layout.setContentsMargins(0, 0, 0, 0)
        closed_layout.setSpacing(2)
        self.closed_wave = HeroCanvas(show_word=False)
        self.closed_wave.setMinimumHeight(96)
        self.closed_label = QLabel("market is closed")
        self.closed_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.closed_label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {theme.TEXT_MUTED};"
            f" background: transparent;"
        )
        closed_layout.addWidget(self.closed_wave)
        closed_layout.addWidget(self.closed_label)
        layout.addWidget(self.closed_box, stretch=1)
        self.closed_box.hide()

        # order history (8.5): every order this position placed, from the DB
        self.history_caption = QLabel("ORDERS")
        self.history_caption.setStyleSheet(
            f"font-size: 10px; font-weight: 700; letter-spacing: 1px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.history_caption)
        self.history_label = QLabel("")
        self.history_label.setStyleSheet(
            f"font-family: Menlo, monospace; font-size: 11px; color: {theme.TEXT};"
            f" background: transparent;"
        )
        layout.addWidget(self.history_label)
        self.history_caption.hide()
        self.history_label.hide()

    def set_history(self, rows: list[str]) -> None:
        show = bool(rows)
        self.history_caption.setVisible(show)
        self.history_label.setVisible(show)
        self.history_label.setText("\n".join(rows[-5:]))  # newest five fit the card

    def update_data(self, position: dict) -> None:
        symbol = position.get("symbol", "—")
        side = position.get("side", "long")
        qty = position.get("qty", 0)
        entry = position.get("entry") or 0.0
        last = position.get("last") or entry
        stop = position.get("stop")
        stage = position.get("stage", "")
        strategy = position.get("strategy", "")
        halted = bool(position.get("halted"))
        ssr = bool(position.get("ssr"))

        self.symbol.setText(symbol)
        self.name.setText(position.get("name") or "")
        long_side = side == "long"
        self.side_chip.setText("LONG" if long_side else "SHORT")
        self.side_chip.set_colors(
            theme.BLUE if long_side else "#AF52DE",
            "rgba(0, 122, 255, 0.10)" if long_side else "rgba(175, 82, 222, 0.12)",
        )
        self.sell_button.setText("SELL" if long_side else "COVER")  # S4
        self.strategy_chip.setVisible(bool(strategy))
        self.strategy_chip.setText(strategy.upper())
        if halted:
            self.halt_chip.setText("HALT")
            self.halt_chip.set_colors("#FFFFFF", theme.RED)
        elif ssr:
            self.halt_chip.setText("SSR")
            self.halt_chip.set_colors("#FFFFFF", theme.ORANGE_LIVE)
        self.halt_chip.setVisible(halted or ssr)

        direction = 1.0 if long_side else -1.0
        pnl_dollars = (last - entry) * qty * direction if entry else 0.0
        pnl_pct = ((last - entry) / entry * 100.0 * direction) if entry else 0.0
        color = theme.GREEN if pnl_dollars >= 0 else theme.RED
        self.pnl.setText(f"{pnl_dollars:+,.2f}")
        self.pnl.setStyleSheet(
            f"font-size: 30px; font-weight: 700; color: {color}; background: transparent;"
        )
        self.pnl_pct.setText(f"{pnl_pct:+.2f}%")
        self.pnl_pct.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {color}; background: transparent;"
        )
        self.detail.setText(
            f"{qty:g} shares @ {entry:,.2f}   last {last:,.2f}   ·   invested ${entry * qty:,.2f}"
        )
        stop_text, stop_state = stop_goal_text(entry, stop, long_side, qty)
        self.stop_label.setText(stop_text)
        self.stop_label.setStyleSheet(
            f"font-size: 14px; color: {STOP_STATE_COLORS[stop_state]}; background: transparent;"
        )
        self.stage_chip.setVisible(bool(stage))
        if stage:
            self.stage_chip.setText(stage)
            self.stage_chip.set_colors(theme.GREEN, "rgba(40, 205, 65, 0.12)")

        bars = position.get("bars") or []
        market_open = position.get("market_open", True)
        self.chart.setVisible(market_open)  # paints blank until data flows
        self.closed_box.setVisible(not market_open)
        if market_open:
            # trail-only exits have no resting target — draw the +0.4% goal
            # zone instead so "where do we sell for profit" is visible
            target = position.get("target")
            if target is None and entry:
                target = entry * (1.004 if long_side else 0.996)
            self.chart.set_data(
                bars, entry, stop=stop, target=target, entry_index=position.get("entry_index")
            )


class _ConfirmPopup(QWidget):
    """Small confirm card over a dim backdrop (§5: manual overrides sit
    behind a confirm). Cancel or click-outside dismisses."""

    def __init__(self, window: QWidget, message: str, action_label: str, on_confirm) -> None:
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setObjectName("confirmOverlay")
        self.setStyleSheet("#confirmOverlay { background: rgba(0, 0, 0, 0.25); }")
        self.setGeometry(window.rect())
        self._on_confirm = on_confirm

        self.card = QFrame(self)
        self.card.setObjectName("confirmCard")
        self.card.setStyleSheet(
            "#confirmCard { background: #FFFFFF;"
            " border: 1px solid rgba(0, 0, 0, 0.12); border-radius: 14px; }"
        )
        self.card.setFixedWidth(340)  # height fits the message below (r7)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(22, 18, 22, 16)
        card_layout.setSpacing(12)
        self.message = QLabel(message)
        self.message.setWordWrap(True)
        self.message.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {theme.TEXT}; background: transparent;"
        )
        card_layout.addWidget(self.message)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.close)
        self.confirm_button = QPushButton(action_label)
        self.confirm_button.setProperty("accent", True)
        self.confirm_button.clicked.connect(self._confirm)
        for button in (self.cancel_button, self.confirm_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(button)
        card_layout.addLayout(buttons)
        # size to CONTENT: the fixed 330×132 clipped two-line messages
        # (screenshot, 2026-08-19). Layout sizeHint UNDERESTIMATES
        # word-wrapped labels, so the height is computed explicitly from the
        # label's wrap height at the card's inner width.
        inner_width = 340 - 22 - 22  # card width − left/right margins
        message_height = self.message.heightForWidth(inner_width)
        buttons_height = self.confirm_button.sizeHint().height()
        height = 18 + message_height + 12 + buttons_height + 16 + 4  # margins+spacing+slack
        self.card.setFixedSize(340, max(132, height))
        self.card.move(
            (self.width() - self.card.width()) // 2,
            (self.height() - self.card.height()) // 2,
        )
        from waveapp.ui.popup_fx import pop_in

        pop_in(self, self.card)

    def _confirm(self) -> None:
        self.close()
        self._on_confirm()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.card.geometry().contains(event.position().toPoint()):
            self.close()


class PositionDetailPopup(QWidget):
    """Double-click popup: dimmed backdrop + the solid detail card centered.
    Click anywhere outside to dismiss; numbers keep ticking while open."""

    def __init__(self, window: QWidget, position: dict) -> None:
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setObjectName("detailOverlay")
        # scoped: dim ONLY the backdrop, never the card children
        self.setStyleSheet("#detailOverlay { background: rgba(0, 0, 0, 0.22); }")
        self.setGeometry(window.rect())
        self.card = PositionDetailCard(self)
        self.card.update_data(position)
        self._center_card()
        from waveapp.ui.popup_fx import pop_in

        pop_in(self, self.card)

    def _center_card(self) -> None:
        self.card.move(
            (self.width() - self.card.width()) // 2,
            (self.height() - self.card.height()) // 2,
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.setGeometry(self.parentWidget().rect())
        self._center_card()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.card.geometry().contains(event.position().toPoint()):
            self.close()


class _Dot(QWidget):
    clicked = pyqtSignal(int)

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.active = False
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.index)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # round 7: inactive dots BRIGHT (white with a hairline ring) — dark
        # blended into the depth panel
        radius = 5.0 if self.active else 4.2
        if self.active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.BLUE))
        else:
            painter.setPen(QPen(QColor(0, 0, 0, 70), 1))
            painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawEllipse(QRectF(8 - radius, 8 - radius, radius * 2, radius * 2))


class PositionsPage(QWidget):
    """Reflowing card grid + pagination dots + empty state, on a translucent
    depth panel (round 2 — glass still shows through).

    Two sources: the ENGINE feeds update_positions every second; the Test tab
    feeds set_test_positions. While test positions exist they take over the
    display, and the engine's (empty) snapshots can't blink them away."""

    sell_requested = pyqtSignal(str)  # position key — each sell fires its own task
    tighten_requested = pyqtSignal(str)  # manual stop-tighten override (8.5)
    count_changed = pyqtSignal(int)  # displayed positions (engine OR fakes) — 8.8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine_positions: list[dict] = []
        self._test_positions: list[dict] = []
        self._positions: list[dict] = []
        self._cards: dict[str, PositionCard] = {}
        self._page = 0
        self._detail_popup: PositionDetailPopup | None = None
        self._detail_key: str | None = None
        self._confirm: _ConfirmPopup | None = None
        self.history_provider = None  # Callable[[key], list[str]] — app.py wires the DB

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 12)
        outer.setSpacing(8)

        self.empty = QLabel("No open positions")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(
            f"font-size: 15px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        outer.addWidget(self.empty, stretch=1)

        # viewport + inner slider: pages slide horizontally (round 3)
        self.grid_host = QWidget()
        outer.addWidget(self.grid_host, stretch=10)
        self._slider = QWidget(self.grid_host)
        self.grid = self._make_grid(self._slider)

        self.dots_row = QWidget()
        dots_layout = QHBoxLayout(self.dots_row)
        dots_layout.setContentsMargins(0, 0, 0, 0)
        dots_layout.setSpacing(2)
        dots_layout.addStretch(1)
        self._dots_layout = dots_layout
        dots_layout.addStretch(1)
        self._dots: list[_Dot] = []
        outer.addWidget(self.dots_row)

    @staticmethod
    def _make_grid(host: QWidget) -> QGridLayout:
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(GAP)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        return grid

    # -- geometry -------------------------------------------------------------

    def _capacity(self) -> tuple[int, int]:
        width = max(self.grid_host.width(), CARD_W)
        height = max(self.grid_host.height(), CARD_H)
        cols = max(1, (width + GAP) // (CARD_W + GAP))
        rows = max(1, (height + GAP) // (CARD_H + GAP))
        return int(cols), int(rows)

    def per_page(self) -> int:
        cols, rows = self._capacity()
        return cols * rows

    def page_count(self) -> int:
        if not self._positions:
            return 0
        return math.ceil(len(self._positions) / self.per_page())

    # -- updates --------------------------------------------------------------

    def update_positions(self, positions: list[dict]) -> None:
        """The engine's 1s snapshot. Ignored for display while the Test tab
        has fake positions up (round-2 blink fix)."""
        self._engine_positions = positions
        if not self._test_positions:
            self._positions = positions
            self._rebuild()

    def update_marks(self, marks: dict) -> None:
        """The engine's 250ms light tick — {position_key: sellable mark}.
        Patches visible cards' P&L in place (blueprint 9.2); the stored
        snapshots are patched too so page flips/popups never show stale
        numbers between full snapshots."""
        if self._test_positions or not marks:
            return
        for positions in (self._engine_positions, self._positions):
            for position in positions:
                key = str(position.get("key", position.get("symbol")))
                if key in marks:
                    position["last"] = marks[key]
        for key, card in self._cards.items():
            if key in marks:
                card.update_mark(marks[key])

    def set_test_positions(self, positions: list[dict]) -> None:
        """Test-bench override (Settings → Test): bench fakes win over engine pushes."""
        self._test_positions = positions
        self._positions = positions if positions else self._engine_positions
        self._rebuild()

    def set_page(self, page: int) -> None:
        """Round 3: switching pages SLIDES — the old page glides out, the new
        one glides in from the side you're heading toward."""
        pages = self.page_count()
        page = max(0, min(page, pages - 1)) if pages else 0
        if page == self._page:
            return
        direction = 1 if page > self._page else -1
        self._page = page

        width = self.grid_host.width()
        height = self.grid_host.height()
        if width < 50 or not self.isVisible():
            self._rebuild()
            return

        old_slider = self._slider
        self._slider = QWidget(self.grid_host)
        self.grid = self._make_grid(self._slider)
        self._cards = {}
        self._slider.setGeometry(direction * width, 0, width, height)
        cols, _rows = self._capacity()
        self._populate(self._visible_slice(), cols)
        self._slider.show()

        slide_out = QPropertyAnimation(old_slider, b"pos", self)
        slide_out.setDuration(280)
        slide_out.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide_out.setEndValue(QPoint(-direction * width, 0))
        slide_in = QPropertyAnimation(self._slider, b"pos", self)
        slide_in.setDuration(280)
        slide_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide_in.setStartValue(QPoint(direction * width, 0))
        slide_in.setEndValue(QPoint(0, 0))
        group = QParallelAnimationGroup(self)
        group.addAnimation(slide_out)
        group.addAnimation(slide_in)
        group.finished.connect(old_slider.deleteLater)
        group.start()
        self._slide_group = group
        self._sync_dots(self.page_count())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._slider.setGeometry(0, 0, self.grid_host.width(), self.grid_host.height())
        self._rebuild()

    def paintEvent(self, event) -> None:  # noqa: N802
        """Round 2: a translucent depth panel behind the cards — darker wash
        + hairline, so the tab reads as a surface while the glass shows
        through (same treatment as the tabs bar)."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(12.5, 12.5, self.width() - 25, self.height() - 25)
        painter.setPen(QPen(QColor(0, 0, 0, 26), 1))
        painter.setBrush(QColor(0, 0, 0, 13))
        painter.drawRoundedRect(rect, 14, 14)

    def _visible_slice(self) -> list[dict]:
        pages = self.page_count()
        self._page = max(0, min(self._page, pages - 1)) if pages else 0
        per_page = self.per_page()
        return self._positions[self._page * per_page : (self._page + 1) * per_page]

    def _populate(self, visible: list[dict], cols: int) -> None:
        wanted_keys = [str(p.get("key", p.get("symbol"))) for p in visible]
        # drop cards that are no longer visible
        for key in list(self._cards):
            if key not in wanted_keys:
                card = self._cards.pop(key)
                self.grid.removeWidget(card)
                card.deleteLater()
        # place/update the visible ones
        for index, (key, position) in enumerate(zip(wanted_keys, visible, strict=True)):
            card = self._cards.get(key)
            if card is None:
                card = self._make_card(key)
                self._cards[key] = card
            self.grid.addWidget(card, index // cols, index % cols)
            card.update_data(position)
            card.show()

    def _make_card(self, key: str) -> PositionCard:
        card = PositionCard()
        card.sell_clicked.connect(lambda k=key: self.sell_requested.emit(k))
        card.opened.connect(lambda k=key: self.open_details(k))
        return card

    def _rebuild(self) -> None:
        positions = self._positions
        self.empty.setVisible(not positions)
        self.grid_host.setVisible(bool(positions))
        if self._slider.size() != self.grid_host.size():
            self._slider.setGeometry(0, 0, self.grid_host.width(), self.grid_host.height())
        cols, _rows = self._capacity()
        self._populate(self._visible_slice(), cols)
        self._sync_dots(self.page_count())
        self._refresh_detail_popup()
        self.count_changed.emit(len(self._positions))

    # -- detail popup (round 4: the SAME card look, just bigger) --------------

    def open_details(self, key: str) -> None:
        # instrumented 2026-08-26 ("double click opens nothing") — the
        # path works in offscreen repro, so the log must say which half fails
        logger.info("detail popup requested for %s", key)
        if self._detail_popup is not None:
            # A5-9: a second double-click must not orphan the first overlay —
            # it would sit under the new one, never refreshed and never
            # auto-closed. Dismiss it the same way _refresh_detail_popup does
            # (close + drop the reference), plus deleteLater so the hidden
            # child widget doesn't accumulate on the window.
            self._detail_popup.close()
            self._detail_popup.deleteLater()
            self._detail_popup = None
            self._detail_key = None
        position = self._find_position(key)
        if position is None:
            logger.warning(
                "detail popup: no position for key %s (have: %s)",
                key,
                [str(p.get("key", p.get("symbol"))) for p in self._positions],
            )
            return
        symbol = position.get("symbol", "")
        try:
            popup = PositionDetailPopup(self.window(), position)
        except Exception as exc:
            # %r lands in the DB log (the DB handler stores only the message,
            # not the traceback) — 2026-08-27: CAMT failed here namelessly
            logger.exception("detail popup construction failed for %s: %r", symbol, exc)
            return

        # S4: a short closes by buying to cover — the confirm says so
        close_verb = "Sell" if position.get("side", "long") == "long" else "Cover"

        def confirm_sell() -> None:
            self._confirm = _ConfirmPopup(
                self.window(),
                f"{close_verb} {symbol} now?",
                close_verb,
                lambda: (self.sell_requested.emit(key), popup.close()),
            )
            self._confirm.show()
            self._confirm.raise_()

        def confirm_tighten() -> None:
            self._confirm = _ConfirmPopup(
                self.window(),
                f"Tighten the {symbol} stop toward the current price?",
                "Tighten",
                lambda: self.tighten_requested.emit(key),
            )
            self._confirm.show()
            self._confirm.raise_()

        popup.card.sell_clicked.connect(confirm_sell)
        popup.card.tighten_clicked.connect(confirm_tighten)
        rows = position.get("history")
        if rows is None and self.history_provider is not None:
            try:
                rows = self.history_provider(key)
            except Exception:
                rows = []
        popup.card.set_history(rows or [])
        self._detail_popup = popup
        self._detail_key = key
        popup.show()
        popup.raise_()
        logger.info(
            "detail popup SHOWN for %s: visible=%s, %d chart candles, geom=%s",
            symbol,
            popup.isVisible(),
            len(popup.card.chart._candles),
            popup.geometry(),
        )

    def _find_position(self, key: str) -> dict | None:
        return next((p for p in self._positions if str(p.get("key", p.get("symbol"))) == key), None)

    def _refresh_detail_popup(self) -> None:
        """Keep the open popup ticking with the live numbers; close it if its
        position closed."""
        popup = self._detail_popup
        if popup is None or popup.isHidden():  # isHidden = explicitly closed
            return
        position = self._find_position(self._detail_key or "")
        if position is None:
            popup.close()
            self._detail_popup = None
        else:
            popup.card.update_data(position)

    def _sync_dots(self, pages: int) -> None:
        show = pages > 1
        self.dots_row.setVisible(show)
        while len(self._dots) < pages:
            dot = _Dot(len(self._dots))
            dot.clicked.connect(self.set_page)
            self._dots.append(dot)
            self._dots_layout.insertWidget(self._dots_layout.count() - 1, dot)
        while len(self._dots) > pages:
            dot = self._dots.pop()
            self._dots_layout.removeWidget(dot)
            dot.deleteLater()
        for index, dot in enumerate(self._dots):
            dot.active = index == self._page
            dot.update()
