"""Performance tab (Phase 8.6, §5), drawn with pyqtgraph.

Round 2:
- the graph lives on a WHITE card (same as position cards);
- each closed trade is a triangle marker — green ▲ profit, red ▼ loss —
  and DOUBLE-CLICKING one opens a small card with the trade's data;
- a second, PIE view slides in/out with the same dots as the Positions
  pages: a dropdown picks the breakdown (trades by hour/weekday/week/month,
  wins vs losses) and a calendar dropdown filters by date range.

The equity curve is REALIZED equity: it steps only when a trade CLOSES —
buys never move it. Performance data dict:
{current_equity, trades: [{ts: epoch, pnl, symbol}], cashflows: [{ts,
 amount}], fees, champion}
"""

from __future__ import annotations

import bisect
import logging
import math
import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pyqtgraph as pg
from PyQt6.QtCore import (
    QDate,
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QCalendarWidget,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme
from waveapp.ui.positions_page import _Dot

_SCOREBOARD_COLUMNS = [
    "Group",
    "Trades",
    "Win %",
    "Net $",
    "Avg $",
    "Best $",
    "Worst $",
    "Avg hold",
    "Min hold",
    "Max hold",
]

pg.setConfigOptions(antialias=True)

logger = logging.getLogger("wave.ui.performance")

_ET = ZoneInfo("America/New_York")

_MARKER_SYMBOLS: dict = {}

# graph v2 (2026-08-22): the visible time ranges — the window is
# anchored at the LAST data point (not the wall clock), so a weekend "1D"
# still shows the last trading day instead of an empty graph
GRAPH_RANGES: list[tuple[str, float | None, str]] = [
    ("1D", 86_400.0, "past day"),
    ("1W", 7 * 86_400.0, "past week"),
    ("1M", 30 * 86_400.0, "past month"),
    ("3M", 90 * 86_400.0, "past 3 months"),
    ("ALL", None, "all time"),
]


def _triangle_symbol(win: bool):
    """Graph v2 (2026-08-22): ONE triangle silhouette for every
    marker — green ▲ floating just above the curve for wins, red ▼ hanging
    below it for losses. Clusters reuse the SAME triangle with a ×N badge —
    no more shape zoo."""
    cached = _MARKER_SYMBOLS.get(win)
    if cached is not None:
        return cached
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath, QPolygonF

    if win:  # apex up — sits ON the curve at the close
        polygon = QPolygonF([QPointF(0, -0.5), QPointF(-0.5, 0.5), QPointF(0.5, 0.5)])
    else:  # apex down
        polygon = QPolygonF([QPointF(0, 0.5), QPointF(-0.5, -0.5), QPointF(0.5, -0.5)])
    path = QPainterPath()
    path.addPolygon(polygon)
    path.closeSubpath()
    _MARKER_SYMBOLS[win] = path
    return path


def pie_palette(count: int) -> list[str]:
    """Round 3: a theme-fit ramp — deep systemBlue drifting toward a
    light teal — with every wedge UNIQUE (no repeats, ever)."""
    colors = []
    for index in range(max(1, count)):
        t = index / max(1, count - 1) if count > 1 else 0.0
        hue = ((212 + 28 * t) % 360) / 360.0
        saturation = 0.80 - 0.42 * t
        value = 0.80 + 0.17 * t
        colors.append(QColor.fromHsvF(hue, saturation, value).name())
    return colors


def compute_stats(trades: list[dict]) -> dict:
    """The §5 stat strip, from closed trades only."""
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    stats = {
        "trades": len(pnls),
        "win_rate": (len(wins) / len(pnls) * 100.0) if pnls else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses)))
        if losses
        else (float("inf") if wins else 0.0),
        # R proxy until per-trade risk is journaled in Phase 10: average P&L
        # per trade divided by the average losing trade's size
        "expectancy_r": (
            (sum(pnls) / len(pnls)) / abs(sum(losses) / len(losses)) if losses and pnls else 0.0
        ),
        "max_drawdown": 0.0,
        "today": 0,
    }
    peak = equity = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    stats["max_drawdown"] = drawdown
    # max drawup (8.6 r4): the largest climb off any trough
    trough = equity = 0.0
    drawup = 0.0
    for pnl in pnls:
        equity += pnl
        trough = min(trough, equity)
        drawup = max(drawup, equity - trough)
    stats["max_drawup"] = drawup
    today = datetime.now(UTC).date()
    stats["today"] = sum(
        1 for t in trades if datetime.fromtimestamp(float(t["ts"]), UTC).date() == today
    )
    return stats


# -- pie breakdowns (pure, testable) ------------------------------------------

PIE_MODES = (
    ("Trades by hour (day)", "hour"),
    ("Trades by weekday (week)", "weekday"),
    ("Trades by week (month)", "month_week"),
    ("Trades by month (year)", "year_month"),
    ("Wins vs losses", "winloss"),
)


def bucket_label(trade: dict, mode: str) -> str:
    """Which wedge a trade belongs to (shared by the pie and its details)."""
    if mode == "winloss":
        return "wins" if float(trade["pnl"]) >= 0 else "losses"
    stamp = datetime.fromtimestamp(float(trade["ts"]), UTC).astimezone(_ET)
    if mode == "hour":
        return f"{stamp.hour:02d}:00"
    if mode == "weekday":
        return stamp.strftime("%a")
    if mode == "month_week":
        return f"week {(stamp.day - 1) // 7 + 1}"
    return stamp.strftime("%b")  # year_month


def pie_breakdown(trades: list[dict], mode: str) -> list[tuple[str, float, str]]:
    """(label, value, color) wedges for the chosen breakdown; zero-count
    buckets are dropped so the pie stays readable."""
    if mode == "winloss":
        wins = sum(1 for t in trades if float(t["pnl"]) >= 0)
        losses = len(trades) - wins
        wedges = [("wins", float(wins), theme.GREEN), ("losses", float(losses), theme.RED)]
        return [w for w in wedges if w[1] > 0]

    buckets: dict[str, int] = {}
    order: list[str] = []

    def bump(label: str) -> None:
        if label not in buckets:
            buckets[label] = 0
            order.append(label)
        buckets[label] += 1

    for trade in trades:
        bump(bucket_label(trade, mode))
    if mode == "hour":
        order.sort()
    elif mode == "weekday":
        week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        order.sort(key=week.index)
    elif mode == "year_month":
        months = [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        order.sort(key=months.index)
    colors = pie_palette(len(order))
    return [(label, float(buckets[label]), colors[index]) for index, label in enumerate(order)]


def filter_by_range(trades: list[dict], start: date | None, end: date | None) -> list[dict]:
    if start is None and end is None:
        return trades
    kept = []
    for trade in trades:
        day = datetime.fromtimestamp(float(trade["ts"]), UTC).astimezone(_ET).date()
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        kept.append(trade)
    return kept


def _detach_native_combo_chrome(combo: QComboBox) -> None:
    """r5: the dropdown still showed BLACK top/bottom chrome — that's
    macOS's native popup window (dark system appearance) behind our white
    card. Force a styled list view and make the popup container translucent
    so only the rounded white card remains."""
    from PyQt6.QtWidgets import QListView

    view = QListView()
    view.setStyleSheet(
        "QListView { background: #FFFFFF; border: 1px solid rgba(0, 0, 0, 0.14);"
        " border-radius: 10px; padding: 4px; outline: none; color: #1D1D1F; }"
        "QListView::item { padding: 5px 8px; border-radius: 6px; }"
        "QListView::item:selected { background: rgba(0, 122, 255, 0.14); color: #1D1D1F; }"
    )
    combo.setView(view)
    container = view.parentWidget()
    if container is not None:
        container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        container.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        container.setStyleSheet("background: transparent; border: none;")


# -- widgets ------------------------------------------------------------------


class _SafeInfiniteLine(pg.InfiniteLine):
    """pyqtgraph InfiniteLine whose bounding-rect math survives teardown.

    The stock class dereferences getViewBox().size() and crashes (then
    segfaults Qt) when a paint event lands after the line's scene/viewbox is
    gone — the build-gate rehearsal flake, 2026-09-14. Returning the last
    known rect when the viewbox is absent is safe: the item is invisible or
    dying in that state anyway.
    """

    def _computeBoundingRect(self):  # noqa: N802 — pyqtgraph API name
        if self.getViewBox() is None:
            return self._boundingRect if self._boundingRect is not None else pg.QtCore.QRectF()
        return super()._computeBoundingRect()


class _Stat(QWidget):
    """One stat: tiny caption over the value (same language as the balance)."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)
        self.caption = QLabel(caption.upper())
        self.caption.setStyleSheet(
            f"font-size: 9px; font-weight: 700; letter-spacing: 1px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        self.value = QLabel("—")
        self.value.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {theme.TEXT}; background: transparent;"
        )
        layout.addWidget(self.caption)
        layout.addWidget(self.value)

    def set(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {color or theme.TEXT};"
            f" background: transparent;"
        )


class _TradePopup(QWidget):
    """Double-clicked marker — ONE card for every case (graph v2:
    single trades and clusters must be indistinguishable). Header: net
    result + when; one row per trade with its own Remove…; footer: the
    account value after."""

    def __init__(
        self, window: QWidget, trades: list[dict], equity_after: float, on_remove=None
    ) -> None:
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setObjectName("tradeOverlay")
        self.setStyleSheet("#tradeOverlay { background: rgba(0, 0, 0, 0.22); }")
        self.setGeometry(window.rect())
        self._on_remove = on_remove
        self.card = QFrame(self)
        self.card.setObjectName("tradeCard")
        self.card.setStyleSheet(
            "#tradeCard { background: #FFFFFF;"
            " border: 1px solid rgba(0, 0, 0, 0.12); border-radius: 14px; }"
        )
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(22, 16, 22, 16)
        layout.setSpacing(6)
        net = sum(float(t["pnl"]) for t in trades)
        net_color = theme.GREEN if net >= 0 else theme.RED
        amount = QLabel(f"{net:+,.2f} $")
        amount.setStyleSheet(
            f"font-size: 24px; font-weight: 700; color: {net_color}; background: transparent;"
        )
        layout.addWidget(amount)
        stamp = datetime.fromtimestamp(float(trades[0]["ts"]), UTC).astimezone(_ET)
        prefix = f"{len(trades)} trades closed together  ·  " if len(trades) > 1 else ""
        subtitle = QLabel(prefix + stamp.strftime("%b %d, %Y  ·  %H:%M ET"))
        subtitle.setStyleSheet(
            f"font-size: 12px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(subtitle)
        for trade in trades:
            pnl = float(trade["pnl"])
            color = theme.GREEN if pnl >= 0 else theme.RED
            row = QHBoxLayout()
            row.setSpacing(10)
            label = QLabel(f"{trade.get('symbol', '')}  {pnl:+,.2f} $")
            label.setStyleSheet(
                f"font-size: 14px; font-weight: 600; color: {color}; background: transparent;"
            )
            row.addWidget(label)
            row.addStretch(1)
            # per-trade removal (2026-08-19): hides this trade from
            # the graph/stats — the DB row is untouched
            if on_remove is not None and trade.get("uuid"):
                remove = QPushButton("Remove…")
                remove.setCursor(Qt.CursorShape.PointingHandCursor)
                remove.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: 1px solid"
                    f" {theme.BORDER_STRONG}; border-radius: 6px; padding: 2px 8px;"
                    f" font-size: 11px; color: {theme.RED}; }}"
                    f"QPushButton:hover {{ background: rgba(255, 59, 48, 0.06); }}"
                )
                remove.clicked.connect(lambda _c=False, t=trade: self._remove_one(t))
                row.addWidget(remove)
            layout.addLayout(row)
        footer = QLabel(f"account after: ${equity_after:,.2f}")
        footer.setStyleSheet(
            f"font-size: 12px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(footer)
        self.card.setFixedWidth(320)
        self.card.adjustSize()
        self.card.setFixedSize(320, max(150, self.card.sizeHint().height()))
        self.card.move(
            (self.width() - self.card.width()) // 2,
            (self.height() - self.card.height()) // 2,
        )
        from waveapp.ui.popup_fx import pop_in

        pop_in(self, self.card)

    def _remove_one(self, trade: dict) -> None:
        from waveapp.ui.positions_page import _ConfirmPopup

        window = self.parentWidget()
        on_remove = self._on_remove
        self.close()
        self._confirm = _ConfirmPopup(
            window,
            f"Remove the {trade.get('symbol', '')} trade ({float(trade['pnl']):+,.2f} $)"
            " from the graph?\nIt stays in the database — only the chart and stats forget it.",
            "Remove",
            lambda: on_remove(trade["uuid"]),
        )
        # pop_in animates but does NOT show — forgetting these two lines made
        # the confirm invisible ("remove does nothing", 2026-08-19)
        self._confirm.show()
        self._confirm.raise_()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.card.geometry().contains(event.position().toPoint()):
            self.close()


class _ReadyRow(QWidget):
    """One §11.2 requirement: label · progress bar · value, ✓ green when met."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.name = QLabel(label)
        self.name.setFixedWidth(300)
        self.name.setStyleSheet(f"font-size: 13px; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(self.name)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        layout.addWidget(self.bar, stretch=1)
        self.value = QLabel("")
        self.value.setFixedWidth(190)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.value)

    def update_check(self, check: dict) -> None:
        ok = bool(check["ok"])
        progress = max(0.0, min(1.0, float(check.get("progress", 1.0 if ok else 0.0))))
        self.bar.setValue(int(progress * 1000))
        color = theme.GREEN if ok else theme.BLUE
        self.bar.setStyleSheet(
            "QProgressBar { background: #EDEDF2; border: none; border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )
        mark = "✓ " if ok else ""
        self.value.setText(mark + str(check.get("text", "")))
        self.value.setStyleSheet(
            f"font-size: 12px; font-weight: 600; background: transparent;"
            f" color: {theme.GREEN if ok else theme.TEXT_MUTED};"
        )


class _ResetViewButton(QWidget):
    """Small crosshair button at the axis corner: snap the graph back to the
    full view after panning around (8.6 r4)."""

    clicked = pyqtSignal()
    SIZE = 26

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.SIZE - 2, self.SIZE - 2)
        painter.setPen(QPen(QColor(0, 0, 0, 50), 1))
        painter.setBrush(QColor(255, 255, 255, 245 if self._hover else 210))
        painter.drawEllipse(rect)
        center = rect.center()
        pen = QPen(QColor(theme.BLUE if self._hover else theme.TEXT_MUTED), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        radius = 5.5
        painter.drawEllipse(center, radius, radius)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            painter.drawLine(
                QPointF(center.x() + dx * (radius - 2), center.y() + dy * (radius - 2)),
                QPointF(center.x() + dx * (radius + 3), center.y() + dy * (radius + 3)),
            )


class _PieChart(QWidget):
    """Custom-painted pie + legend. Clicking a wedge OR its legend row emits
    the label; clicking empty background emits cleared (8.6 r4)."""

    wedge_clicked = pyqtSignal(str)
    cleared = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wedges: list[tuple[str, float, str]] = []
        self._pie_rect: QRectF | None = None
        self._legend_rects: list[tuple[QRectF, str]] = []
        self._selected: str | None = None

    def set_wedges(self, wedges: list[tuple[str, float, str]]) -> None:
        self._wedges = wedges
        if self._selected not in {label for label, _v, _c in wedges}:
            self._selected = None
        self.update()

    def set_selected(self, label: str | None) -> None:
        self._selected = label
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._wedges or self._pie_rect is None:
            return
        position = event.position()
        # legend rows act exactly like their slice (8.6 r4)
        for rect, label in self._legend_rects:
            if rect.contains(position):
                self.wedge_clicked.emit(label)
                return
        center = self._pie_rect.center()
        dx = position.x() - center.x()
        dy = position.y() - center.y()
        if math.hypot(dx, dy) > self._pie_rect.width() / 2:
            self.cleared.emit()  # background click resets the panel
            return
        angle = math.degrees(math.atan2(-dy, dx))  # 0° = east, counter-clockwise
        clockwise_from_top = (90 - angle) % 360
        total = sum(value for _, value, _ in self._wedges) or 1.0
        accumulated = 0.0
        for label, value, _color in self._wedges:
            share = value / total * 360
            if clockwise_from_top < accumulated + share:
                self.wedge_clicked.emit(label)
                return
            accumulated += share

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._wedges:
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no trades in this range")
            self._pie_rect = None
            return
        total = sum(value for _, value, _ in self._wedges) or 1.0
        side = min(self.width() * 0.55, float(self.height())) - 16
        pie_rect = QRectF(24, (self.height() - side) / 2, side, side)
        self._pie_rect = pie_rect
        start = 90 * 16
        for label, value, color in self._wedges:
            span = -int(round(value / total * 360 * 16))
            wedge_color = QColor(color)
            if self._selected is not None and label != self._selected:
                wedge_color.setAlphaF(0.35)  # dim the rest, spotlight the pick
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.setBrush(wedge_color)
            painter.drawPie(pie_rect, start, span)
            start += span
        # legend on the right — clickable ONLY over the swatch + the text
        # itself (r5: clicks past the numbers hit the background and reset)
        self._legend_rects = []
        x = pie_rect.right() + 28
        y = max(16.0, (self.height() - len(self._wedges) * 24) / 2)
        metrics = painter.fontMetrics()
        for label, value, color in self._wedges:
            share = value / total * 100
            text = f"{label}   {value:g}  ({share:.0f}%)"
            text_width = metrics.horizontalAdvance(text)
            self._legend_rects.append((QRectF(x - 4, y, 20 + text_width + 10, 22), label))
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(x, y + 4, 12, 12), 3, 3)
            painter.setPen(
                QColor(theme.TEXT)
                if label == self._selected or self._selected is None
                else QColor(theme.TEXT_MUTED)
            )
            painter.drawText(
                QRectF(x + 20, y, max(10.0, self.width() - x - 24), 22),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            y += 24


class _CalendarButton(QPushButton):
    """A date filter button that drops a themed QCalendarWidget popup."""

    date_selected = pyqtSignal(object)  # date | None

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(placeholder, parent)
        self._placeholder = placeholder
        self.selected: date | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open)

    def _open(self) -> None:
        # translucent popup window + inner rounded card: no sharp window
        # corners behind the rounding (round 3)
        popup = QWidget(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        wrap = QVBoxLayout(popup)
        wrap.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("calendarCard")
        card.setStyleSheet(
            "#calendarCard { background: #FFFFFF;"
            " border: 1px solid rgba(0, 0, 0, 0.14); border-radius: 12px; }"
        )
        wrap.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        calendar = QCalendarWidget()
        calendar.setGridVisible(False)
        calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        if self.selected is not None:
            calendar.setSelectedDate(QDate(self.selected))
        layout.addWidget(calendar)
        clear = QPushButton("Clear")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(clear)

        def picked(qdate: QDate) -> None:
            self.selected = qdate.toPyDate()
            self.setText(self.selected.strftime("%b %d, %Y"))
            self.date_selected.emit(self.selected)
            popup.close()

        def cleared() -> None:
            self.selected = None
            self.setText(self._placeholder)
            self.date_selected.emit(None)
            popup.close()

        calendar.clicked.connect(picked)
        clear.clicked.connect(cleared)
        # clamp fully on-screen (round 3: it clipped off the right edge)
        popup.adjustSize()
        target = self.mapToGlobal(QPoint(0, self.height() + 6))
        screen = self.screen().availableGeometry()
        x = max(screen.left() + 8, min(target.x(), screen.right() - popup.width() - 8))
        y = target.y()
        if y + popup.height() > screen.bottom() - 8:
            y = self.mapToGlobal(QPoint(0, 0)).y() - popup.height() - 6
        popup.move(x, y)
        popup.show()
        self._popup = popup  # keep alive


class PerformancePage(QWidget):
    """Stat strip + two sliding views (equity graph / pie breakdowns)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine_data: dict | None = None
        self._test_data: dict | None = None
        self._data: dict = {}
        self._trade_points: list[tuple[float, float, dict]] = []
        self._view = 0
        self._trade_popup: _TradePopup | None = None
        self._range = "ALL"
        self._now_fn = time.time  # tests pin this to freeze the windows
        # wired by the app to ConnectionMonitor.hide_performance_trade
        self.on_hide_trade = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 12)
        outer.setSpacing(10)

        strip = QFrame()
        strip.setProperty("card", True)
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(10, 2, 10, 2)
        strip_layout.setSpacing(0)
        # r4 order: …drawdown, drawup, fees, trades today (champion cut)
        self.stat_equity = _Stat("Realized equity")
        self.stat_win = _Stat("Win rate")
        self.stat_pf = _Stat("Profit factor")
        self.stat_exp = _Stat("Expectancy (R)")
        self.stat_dd = _Stat("Max drawdown")
        self.stat_du = _Stat("Max drawup")
        self.stat_fees = _Stat("Fees paid")
        self.stat_today = _Stat("Trades today")
        for stat in (
            self.stat_equity,
            self.stat_win,
            self.stat_pf,
            self.stat_exp,
            self.stat_dd,
            self.stat_du,
            self.stat_fees,
            self.stat_today,
        ):
            strip_layout.addWidget(stat)
            strip_layout.addStretch(1)
        outer.addWidget(strip)

        # sliding viewport: view 0 = equity graph, view 1 = pie breakdowns
        self.views_host = QWidget()
        outer.addWidget(self.views_host, stretch=1)

        self.graph_view = self._build_graph_view(self.views_host)
        self.pie_view = self._build_pie_view(self.views_host)
        # view 3 (moved from the System tab, 2026-08-22): the scoreboard —
        # per-strategy / per-floor stats for every closed trade
        self.scoreboard_view = self._build_scoreboard_view(self.views_host)
        self.scoreboard_view.hide()  # 2026-08-22: floated over the graph until visited
        # view 4 (weekend agenda #2, 2026-08-22): Road to Live — the §11.2
        # evidence bar and the live-plumbing checklist, tracked live
        self.readiness_view = self._build_readiness_view(self.views_host)
        self.readiness_view.hide()
        # view 5 (weekend agenda #4, 2026-08-23): weekly reports — week-close
        # and week-ahead tabs, calendar browses past weeks
        self.reports_view = self._build_reports_view(self.views_host)
        self.reports_view.hide()

        dots_row = QWidget()
        dots_layout = QHBoxLayout(dots_row)
        dots_layout.setContentsMargins(0, 0, 0, 0)
        dots_layout.setSpacing(2)
        dots_layout.addStretch(1)
        self._dots = [_Dot(0), _Dot(1), _Dot(2), _Dot(3), _Dot(4)]
        for dot in self._dots:
            dot.clicked.connect(self.set_view)
            dots_layout.addWidget(dot)
        dots_layout.addStretch(1)
        outer.addWidget(dots_row)
        self._sync_dots()

    # -- view construction ----------------------------------------------------

    def _build_graph_view(self, host: QWidget) -> QFrame:
        card = QFrame(host)
        card.setProperty("card", True)  # WHITE card (round 2 — like positions)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 6)

        # graph v2 header: performance for the visible range + range picker
        header = QHBoxLayout()
        header.setSpacing(10)
        head_col = QVBoxLayout()
        head_col.setSpacing(0)
        self.range_amount = QLabel("—")
        self.range_amount.setStyleSheet(
            f"font-size: 20px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        self.range_caption = QLabel("all time")
        self.range_caption.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        head_col.addWidget(self.range_amount)
        head_col.addWidget(self.range_caption)
        header.addLayout(head_col)
        header.addStretch(1)
        segmented = QFrame()
        segmented.setStyleSheet("QFrame { background: #EDEDF2; border-radius: 8px; }")
        seg_layout = QHBoxLayout(segmented)
        seg_layout.setContentsMargins(3, 3, 3, 3)
        seg_layout.setSpacing(2)
        self.range_buttons: dict[str, QPushButton] = {}
        for key, _seconds, _caption in GRAPH_RANGES:
            button = QPushButton(key)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { border: none; background: transparent; padding: 3px 10px;"
                f" border-radius: 6px; font-size: 12px; font-weight: 600; color: {theme.TEXT}; }}"
                "QPushButton:checked { background: #FFFFFF; }"
            )
            button.clicked.connect(lambda _c=False, k=key: self.set_range(k))
            seg_layout.addWidget(button)
            self.range_buttons[key] = button
        self.range_buttons[self._range].setChecked(True)
        header.addWidget(segmented)
        # reset (2026-08-19): restart the curve from now — hides
        # (never deletes) earlier history so buggy data can't corrupt it
        self.reset_history_button = QPushButton("Reset history…")
        self.reset_history_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_history_button.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {theme.BORDER_STRONG};"
            f" border-radius: 7px; padding: 3px 12px; font-size: 12px; color: {theme.TEXT}; }}"
            f"QPushButton:hover {{ background: rgba(0, 0, 0, 0.04); }}"
        )
        self.reset_history_button.clicked.connect(self._confirm_reset_history)
        header.addWidget(self.reset_history_button)
        layout.addLayout(header)

        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setBackground(None)
        self.plot.showGrid(x=False, y=True, alpha=0.06)
        plot_item = self.plot.getPlotItem()
        # Stocks-style: the money axis lives on the RIGHT. The left axis is
        # BLANKED, not hidden — hideAxis() detaches it and a teardown-window
        # repaint then walks the dead ViewBox (part of fix 2)
        plot_item.showAxis("right")
        left_axis = plot_item.getAxis("left")
        left_axis.setStyle(showValues=False)
        left_axis.setWidth(0)
        left_axis.setPen(pg.mkPen(None))
        for side in ("right", "bottom"):
            plot_item.getAxis(side).setPen(pg.mkPen(color=(0, 0, 0, 60)))
            plot_item.getAxis(side).setTextPen(pg.mkPen(color=(110, 110, 115)))
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        plot_item.setDownsampling(auto=True, mode="peak")  # months of 30s snapshots stay smooth
        plot_item.setClipToView(True)
        self._line_color = theme.BLUE
        self.curve = self.plot.plot([], [], pen=pg.mkPen(color=theme.BLUE, width=2.4))
        # dashed baseline: where the visible range started
        self.baseline_line = _SafeInfiniteLine(
            angle=0, pen=pg.mkPen(color=(0, 0, 0, 55), style=Qt.PenStyle.DashLine)
        )
        self.baseline_line.hide()
        self.plot.addItem(self.baseline_line)
        self.markers = pg.ScatterPlotItem(size=13, pen=pg.mkPen("#FFFFFF", width=1))
        self.plot.addItem(self.markers)
        self._marker_badges: list[pg.TextItem] = []
        self._cashflow_lines: list[pg.InfiniteLine] = []
        # scrub cursor (graph v2): glued to the LINE — the dot rides the
        # curve, only horizontal position matters
        self._scrub_line = _SafeInfiniteLine(angle=90, pen=pg.mkPen(color=(0, 0, 0, 70), width=1))
        self._scrub_dot = pg.ScatterPlotItem(size=9, pen=pg.mkPen("#FFFFFF", width=1.5))
        self._scrub_chip = pg.TextItem(anchor=(0.5, 1.35))
        for item in (self._scrub_line, self._scrub_dot, self._scrub_chip):
            item.hide()
            self.plot.addItem(item)
        # live pulse on the newest point
        self._live_point: tuple[float, float] | None = None
        self._live_halo = pg.ScatterPlotItem(pen=None)
        self._live_dot = pg.ScatterPlotItem(size=8, pen=pg.mkPen("#FFFFFF", width=1.5))
        self.plot.addItem(self._live_halo)
        self.plot.addItem(self._live_dot)
        self._pulse = QVariantAnimation(self)
        self._pulse.setStartValue(0.0)
        self._pulse.setEndValue(1.0)
        self._pulse.setDuration(1700)
        self._pulse.setLoopCount(-1)
        self._pulse.valueChanged.connect(self._on_pulse)
        self.plot.destroyed.connect(self._pulse.stop)
        self._pulse.start()
        self.plot.scene().sigMouseClicked.connect(self._on_plot_clicked)
        self.plot.scene().sigMouseMoved.connect(self._on_scrub_move)
        layout.addWidget(self.plot)
        # r4: snap back to the full view after panning (sits at the axis corner)
        self.reset_view_button = _ResetViewButton(self.plot)
        self.reset_view_button.clicked.connect(self.reset_graph_view)
        self.plot.installEventFilter(self)
        self.empty = QLabel("No closed trades yet — the curve starts with the first exit")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(
            f"font-size: 13px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.empty)
        return card

    def _build_scoreboard_view(self, host: QWidget) -> QFrame:
        from PyQt6.QtWidgets import QHeaderView, QTableWidget

        card = QFrame(host)
        card.setProperty("card", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 10)
        title = QLabel("SCOREBOARD — EVERY CLOSED TRADE, KEPT ACROSS RESTARTS")
        title.setStyleSheet(
            f"font-size: 10px; font-weight: 700; letter-spacing: 1px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(title)
        self.scoreboard_empty = QLabel("no closed trades yet")
        self.scoreboard_empty.setStyleSheet(
            f"font-size: 12px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        self.scoreboard_table = QTableWidget(0, len(_SCOREBOARD_COLUMNS))
        self.scoreboard_table.setHorizontalHeaderLabels(_SCOREBOARD_COLUMNS)
        self.scoreboard_table.verticalHeader().setVisible(False)
        self.scoreboard_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.scoreboard_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.scoreboard_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scoreboard_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.scoreboard_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scoreboard_table.setShowGrid(False)
        self.scoreboard_table.setStyleSheet(
            "QTableWidget { background: transparent; border: none; }"
            f"QHeaderView::section {{ background: transparent; border: none;"
            f" border-bottom: 1px solid {theme.BORDER}; padding: 6px;"
            f" font-weight: 600; color: {theme.TEXT_MUTED}; }}"
        )
        self.scoreboard_table.hide()
        layout.addWidget(self.scoreboard_empty)
        layout.addWidget(self.scoreboard_table, stretch=1)
        return card

    @staticmethod
    def _fmt_hold(seconds: float) -> str:
        if seconds < 90:
            return f"{seconds:.0f}s"
        return f"{seconds / 60:.0f}m"

    def update_scoreboard(self, board: dict) -> None:
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QTableWidgetItem

        overall = board.get("overall") or {}
        if not overall.get("trades"):
            self.scoreboard_table.hide()
            self.scoreboard_empty.show()
            return
        self.scoreboard_empty.hide()
        self.scoreboard_table.show()

        rows: list[tuple[str, dict, bool]] = [("ALL", overall, True)]
        rows += [(k, v, False) for k, v in (board.get("by_strategy") or {}).items()]
        rows += [(k, v, False) for k, v in (board.get("by_bucket") or {}).items()]

        table = self.scoreboard_table
        table.setRowCount(len(rows))
        for row_index, (name, s, bold) in enumerate(rows):
            trades = int(s.get("trades") or 0)
            values = [
                name,
                str(trades) if trades else "—",
                f"{s['win_rate']:.0%}" if trades else "—",
                f"{s['net_pnl']:+,.2f}" if trades else "—",
                f"{s['avg_pnl']:+,.2f}" if trades else "—",
                f"{s['best']:+,.2f}" if trades else "—",
                f"{s['worst']:+,.2f}" if trades else "—",
                self._fmt_hold(s["avg_hold_s"]) if trades else "—",
                self._fmt_hold(s["min_hold_s"]) if trades else "—",
                self._fmt_hold(s["max_hold_s"]) if trades else "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                else:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if bold:
                    font = QFont(item.font())
                    font.setBold(True)
                    item.setFont(font)
                if trades and column == 3:
                    net = float(s["net_pnl"])
                    item.setForeground(QColor(theme.GREEN if net >= 0 else theme.RED))
                table.setItem(row_index, column, item)

    def _build_readiness_view(self, host: QWidget) -> QFrame:
        card = QFrame(host)
        card.setProperty("card", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(26, 18, 26, 14)
        layout.setSpacing(8)
        title = QLabel("ROAD TO LIVE")
        title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 1.5px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(title)
        self.readiness_banner = QLabel("collecting evidence…")
        self.readiness_banner.setStyleSheet(
            f"font-size: 18px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        layout.addWidget(self.readiness_banner)
        self._ready_rows_box = QVBoxLayout()
        self._ready_rows_box.setSpacing(6)
        layout.addLayout(self._ready_rows_box)
        self._ready_rows: dict[str, _ReadyRow] = {}
        plumbing_caption = QLabel("LIVE PLUMBING")
        plumbing_caption.setStyleSheet(
            f"font-size: 10px; font-weight: 700; letter-spacing: 1.5px;"
            f" color: {theme.TEXT_MUTED}; background: transparent; padding-top: 8px;"
        )
        layout.addWidget(plumbing_caption)
        self.plumbing_label = QLabel("")
        self.plumbing_label.setStyleSheet(
            f"font-size: 12px; color: {theme.TEXT}; background: transparent;"
        )
        layout.addWidget(self.plumbing_label)
        layout.addStretch(1)
        return card

    def update_readiness(self, data: dict) -> None:
        """The monitor's §11.2 evidence snapshot → checklist rows."""
        checks = data.get("checks") or []
        for check in checks:
            row = self._ready_rows.get(check["key"])
            if row is None:
                row = _ReadyRow(check["label"])
                self._ready_rows[check["key"]] = row
                self._ready_rows_box.addWidget(row)
            row.update_check(check)
        if checks and all(c["ok"] for c in checks):
            self.readiness_banner.setText("Evidence bar met — ready for your Phase 11 review")
            self.readiness_banner.setStyleSheet(
                f"font-size: 18px; font-weight: 700; color: {theme.GREEN}; background: transparent;"
            )
        else:
            met = sum(1 for c in checks if c["ok"])
            self.readiness_banner.setText(
                f"Not yet — {met} of {len(checks)} requirements met. Wave keeps proving itself."
            )
            self.readiness_banner.setStyleSheet(
                f"font-size: 18px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
            )
        lines = []
        for item in data.get("plumbing") or []:
            mark = "✓" if item["ok"] else "…"
            lines.append(f"{mark}  {item['label']}")
        self.plumbing_label.setText("\n".join(lines))

    def _build_reports_view(self, host: QWidget) -> QFrame:
        card = QFrame(host)
        card.setProperty("card", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(26, 18, 26, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("REPORTS")
        title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 1.5px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch(1)
        self.report_tab_buttons: dict[str, QPushButton] = {}
        for key, label in (("week_close", "Week report"), ("week_ahead", "Week ahead")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { border: 1px solid rgba(0,0,0,0.14); background: #FFFFFF;"
                " padding: 4px 14px; border-radius: 7px; font-size: 12px;"
                f" font-weight: 600; color: {theme.TEXT}; }}"
                "QPushButton:checked { background: rgba(0, 122, 255, 0.14);"
                f" border-color: {theme.BLUE}; color: {theme.BLUE}; }}"
            )
            button.clicked.connect(lambda _c=False, k=key: self.show_report(k))
            header.addWidget(button)
            self.report_tab_buttons[key] = button
        self.report_calendar = _CalendarButton("any week")
        header.addWidget(self.report_calendar)
        layout.addLayout(header)
        self.report_body = QLabel("reports appear here — the first ones arrive automatically")
        self.report_body.setWordWrap(True)
        self.report_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.report_body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.report_body.setStyleSheet(
            f"font-size: 14px; color: {theme.TEXT}; background: transparent; line-height: 150%;"
        )
        layout.addWidget(self.report_body, stretch=1)
        self.report_meta = QLabel("")
        self.report_meta.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.report_meta)
        self._report_kind = "week_close"
        self.report_tab_buttons["week_close"].setChecked(True)
        # wired by app.py to ConnectionMonitor.report_for(kind, date)
        self.reports_provider = None
        self.report_calendar.date_selected.connect(lambda _d: self.show_report())
        return card

    def show_report(self, kind: str | None = None) -> None:
        """Render the newest report of `kind` at/before the calendar date
        (today when no date is picked)."""
        if kind is not None:
            self._report_kind = kind
        for name, button in self.report_tab_buttons.items():
            button.setChecked(name == self._report_kind)
        if self.reports_provider is None:
            return
        from datetime import date as _date

        on_or_before = self.report_calendar.selected or _date.today()
        try:
            report = self.reports_provider(self._report_kind, on_or_before)
        except Exception:
            logger.exception("report fetch failed")
            report = None
        if report is None:
            self.report_body.setText(
                "No report for that week yet — the week report arrives every"
                " Friday after the close, the week-ahead every Monday at 9:00."
            )
            self.report_meta.setText("")
            return
        self.report_body.setText(str(report["content"]))
        created = str(report.get("created_at", ""))[:16].replace("T", " ")
        self.report_meta.setText(f"week of {report['report_date']} · written {created} UTC")

    def _build_pie_view(self, host: QWidget) -> QFrame:
        card = QFrame(host)
        card.setProperty("card", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.pie_mode = QComboBox()
        for label, _key in PIE_MODES:
            self.pie_mode.addItem(label)
        self.pie_mode.currentIndexChanged.connect(lambda _i: self._render_pie())
        _detach_native_combo_chrome(self.pie_mode)
        controls.addWidget(self.pie_mode)
        controls.addStretch(1)
        from_label = QLabel("from")
        to_label = QLabel("to")
        for label in (from_label, to_label):
            label.setStyleSheet(
                f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
            )
        self.from_button = _CalendarButton("start date")
        self.to_button = _CalendarButton("end date")
        self.from_button.date_selected.connect(lambda _d: self._render_pie())
        self.to_button.date_selected.connect(lambda _d: self._render_pie())
        controls.addWidget(from_label)
        controls.addWidget(self.from_button)
        controls.addWidget(to_label)
        controls.addWidget(self.to_button)
        layout.addLayout(controls)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.pie = _PieChart()
        self.pie.wedge_clicked.connect(self._on_wedge_clicked)
        self.pie.cleared.connect(self._reset_pie_details)
        body.addWidget(self.pie, stretch=5)
        # round 3: click a slice → its trades appear here
        details = QFrame()
        details.setObjectName("pieDetails")
        details.setStyleSheet(
            "#pieDetails { background: rgba(0, 0, 0, 0.03);"
            " border: 1px solid rgba(0, 0, 0, 0.08); border-radius: 12px; }"
        )
        details.setMinimumWidth(250)
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(16, 12, 16, 12)
        details_layout.setSpacing(6)
        self.pie_details_title = QLabel("click a slice")
        self.pie_details_title.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        self.pie_details_count = QLabel("")
        self.pie_details_count.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        self.pie_details_body = QLabel("")
        self.pie_details_body.setTextFormat(Qt.TextFormat.RichText)
        self.pie_details_body.setStyleSheet(
            "font-family: Menlo, monospace; font-size: 11px; background: transparent;"
        )
        self.pie_details_body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        details_layout.addWidget(self.pie_details_title)
        details_layout.addWidget(self.pie_details_count)
        details_layout.addWidget(self.pie_details_body, stretch=1)
        body.addWidget(details, stretch=3)
        layout.addLayout(body, stretch=1)
        self._pie_groups: dict[str, list[dict]] = {}
        card.hide()
        return card

    def reset_graph_view(self) -> None:
        """Back to the selected range's framing after wandering around."""
        self._render_range()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.plot and event.type() == event.Type.Resize:
            # pin the reset button by the axis corner (bottom-left)
            self.reset_view_button.move(8, self.plot.height() - self.reset_view_button.SIZE - 26)
        if obj is self.plot and event.type() == event.Type.Leave:
            self._hide_scrub()
        return super().eventFilter(obj, event)

    def _reset_pie_details(self) -> None:
        self.pie.set_selected(None)
        self.pie_details_title.setText("click a slice")
        self.pie_details_count.setText("")
        self.pie_details_body.setText("")

    def _on_wedge_clicked(self, label: str) -> None:
        trades = self._pie_groups.get(label, [])
        self.pie.set_selected(label)
        self.pie_details_title.setText(label)
        self.pie_details_count.setText(f"{len(trades)} trade{'s' if len(trades) != 1 else ''}")
        rows = []
        for trade in sorted(trades, key=lambda t: float(t["ts"]))[-14:]:
            stamp = datetime.fromtimestamp(float(trade["ts"]), UTC).astimezone(_ET)
            pnl = float(trade["pnl"])
            color = theme.GREEN if pnl >= 0 else theme.RED
            rows.append(
                f"<span style='color:{theme.TEXT_MUTED}'>{stamp.strftime('%b %d %H:%M')}</span>"
                f"  {trade.get('symbol', '')}"
                f"  <span style='color:{color}'>{pnl:+,.2f}</span>"
            )
        self.pie_details_body.setText("<br>".join(rows) if rows else "—")

    # -- sliding views (same feel as the Positions pages) ---------------------

    def _views(self) -> tuple:
        return (
            self.graph_view,
            self.pie_view,
            self.scoreboard_view,
            self.readiness_view,
            self.reports_view,
        )

    def set_view(self, index: int) -> None:
        views = self._views()
        if index == 4:
            self.show_report()
        index = max(0, min(len(views) - 1, int(index)))
        if index == self._view:
            return
        direction = 1 if index > self._view else -1
        outgoing = views[self._view]
        self._view = index
        width = self.views_host.width()
        height = self.views_host.height()
        incoming = views[index]
        self._sync_dots()
        if width < 50 or not self.isVisible():
            outgoing.hide()
            incoming.setGeometry(0, 0, width, height)
            incoming.show()
            return
        incoming.setGeometry(direction * width, 0, width, height)
        incoming.show()
        slide_out = QPropertyAnimation(outgoing, b"pos", self)
        slide_out.setDuration(280)
        slide_out.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide_out.setEndValue(QPoint(-direction * width, 0))
        slide_in = QPropertyAnimation(incoming, b"pos", self)
        slide_in.setDuration(280)
        slide_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide_in.setStartValue(QPoint(direction * width, 0))
        slide_in.setEndValue(QPoint(0, 0))
        group = QParallelAnimationGroup(self)
        group.addAnimation(slide_out)
        group.addAnimation(slide_in)
        group.finished.connect(outgoing.hide)
        group.start()
        self._slide_group = group

    def _sync_dots(self) -> None:
        for index, dot in enumerate(self._dots):
            dot.active = index == self._view
            dot.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        views = self._views()
        views[self._view].setGeometry(0, 0, self.views_host.width(), self.views_host.height())

    def closeEvent(self, event) -> None:  # noqa: N802
        # fix 2 (2026-08-23): a PlotWidget left to deferred deletion repaints
        # mid-destruction (AxisItem → deleted ViewBox) and SEGFAULTS — every
        # pytest shutdown crashed (the popup dialogs). Dismantle the plot
        # in order while everything is alive; guards the app's quit path too.
        if not getattr(self, "_plot_dead", False):
            self._plot_dead = True
            self._pulse.stop()
            import contextlib

            with contextlib.suppress(Exception):
                self.plot.scene().sigMouseMoved.disconnect(self._on_scrub_move)
            with contextlib.suppress(Exception):
                self.plot.scene().sigMouseClicked.disconnect(self._on_plot_clicked)
            with contextlib.suppress(Exception):
                self.plot.getPlotItem().close()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._pulse.start()
        current = self._views()[self._view]
        current.setGeometry(0, 0, self.views_host.width(), self.views_host.height())
        current.show()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._pulse.stop()  # zero work + zero teardown risk while away

    # -- data (engine vs bench, same pattern as the Positions tab) ------------

    def set_performance(self, data: dict) -> None:
        self._engine_data = data
        if self._test_data is None:
            self._render(data)

    # -- performance reset (2026-08-19) --------------------------------

    def _confirm_reset_history(self) -> None:
        from waveapp.ui.positions_page import _ConfirmPopup

        self._reset_confirm = _ConfirmPopup(
            self.window(),
            "Restart the performance graph from now?\n"
            "Old trades stay in the database — they are only hidden here.",
            "Reset",
            self._do_reset_history,
        )
        self._reset_confirm.show()
        self._reset_confirm.raise_()

    def _do_reset_history(self) -> None:
        from datetime import UTC, datetime

        from waveapp.config import AppConfig

        try:
            config = AppConfig.load()
            config.performance_epoch = datetime.now(UTC).isoformat()
            config.save()
        except Exception:
            logger.exception("performance reset failed to save the epoch")
            return
        # immediate feedback; the monitor's next push re-reads the epoch
        cleared = {**(self._engine_data or {}), "trades": [], "cashflows": []}
        self.set_performance(cleared)

    def set_test_performance(self, data: dict | None) -> None:
        self._test_data = data
        self._render(data if data is not None else (self._engine_data or {}))

    # -- painting -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(12.5, 12.5, self.width() - 25, self.height() - 25)
        painter.setPen(QPen(QColor(0, 0, 0, 26), 1))
        painter.setBrush(QColor(0, 0, 0, 13))
        painter.drawRoundedRect(rect, 14, 14)

    # -- trade info popup (round 2: double-click a triangle) ------------------

    def _on_plot_clicked(self, event) -> None:
        if not event.double() or not self._trade_points:
            return
        view_box = self.plot.getPlotItem().vb
        scene_pos = event.scenePos()
        best = None
        best_distance = 16.0  # px tolerance
        for x, y, members in self._trade_points:
            point_scene = view_box.mapViewToScene(pg.Point(x, y))
            distance = math.hypot(point_scene.x() - scene_pos.x(), point_scene.y() - scene_pos.y())
            if distance < best_distance:
                best_distance = distance
                best = (members, y)
        if best is not None:
            members, equity_after = best
            self.open_trade_info(members, equity_after)

    def open_trade_info(self, trade, equity_after: float) -> None:
        # graph v2: ONE popup for singles and clusters alike
        members = trade if isinstance(trade, list) else [trade]
        self._trade_popup = _TradePopup(
            self.window(), members, equity_after, on_remove=self.on_hide_trade
        )
        self._trade_popup.show()
        self._trade_popup.raise_()

    # -- rendering ------------------------------------------------------------

    def _render(self, data: dict) -> None:
        self._data = data
        trades = sorted(data.get("trades") or [], key=lambda t: float(t["ts"]))
        current_equity = float(data.get("current_equity") or 0.0)
        total_pnl = sum(float(t["pnl"]) for t in trades)
        start_equity = current_equity - total_pnl

        # THE data rule (re-affirmed 2026-08-22): the curve moves ONLY
        # on closed trades and deposits/withdrawals — never on the account's
        # live balance, so open positions can't wiggle the line
        flows = [(float(f["ts"]), float(f["amount"])) for f in (data.get("cashflows") or [])]
        events = sorted(
            [(float(t["ts"]), float(t["pnl"])) for t in trades] + flows, key=lambda e: e[0]
        )
        start_equity -= sum(amount for _ts, amount in flows)
        self._event_xs, self._event_ys = [], []
        equity = start_equity
        for ts, delta in events:
            equity += delta
            self._event_xs.append(ts)
            self._event_ys.append(equity)
        self._series_start = start_equity
        self._trades_sorted = trades
        self.empty.setVisible(not events)

        for line in self._cashflow_lines:
            self.plot.removeItem(line)
        self._cashflow_lines = []
        for flow in data.get("cashflows") or []:
            amount = float(flow["amount"])
            line = _SafeInfiniteLine(
                pos=float(flow["ts"]),
                angle=90,
                pen=pg.mkPen(color=(110, 110, 115, 90), style=Qt.PenStyle.DashLine),
                label="deposit" if amount >= 0 else "withdrawal",
                labelOpts={"position": 0.06, "color": (110, 110, 115)},
            )
            self.plot.addItem(line)
            self._cashflow_lines.append(line)

        stats = compute_stats(trades)
        pnl_color = theme.GREEN if total_pnl >= 0 else theme.RED
        self.stat_equity.set(f"${current_equity:,.2f}" if current_equity else "—")
        self.stat_win.set(f"{stats['win_rate']:.0f}%")
        profit_factor = stats["profit_factor"]
        self.stat_pf.set("∞" if profit_factor == float("inf") else f"{profit_factor:.2f}")
        self.stat_exp.set(f"{stats['expectancy_r']:+.2f}R", pnl_color)
        self.stat_dd.set(
            f"-${stats['max_drawdown']:,.2f}", theme.RED if stats["max_drawdown"] else None
        )
        self.stat_du.set(
            f"+${stats['max_drawup']:,.2f}", theme.GREEN if stats["max_drawup"] else None
        )
        self.stat_fees.set(f"${float(data.get('fees') or 0):,.2f}")
        self.stat_today.set(str(stats["today"]))

        self._render_range()
        self._render_pie()
        if data.get("readiness"):
            self.update_readiness(data["readiness"])

    # -- graph v2: range window, coloring, markers, scrub ----------------------

    def set_range(self, key: str) -> None:
        self._range = key
        for name, button in self.range_buttons.items():
            button.setChecked(name == key)
        self._render_range()

    def _step_value(self, x: float) -> float:
        """The curve's value at any time x — the last event at or before x
        (step semantics: flat between closes)."""
        i = bisect.bisect_right(self._event_xs, x)
        return self._series_start if i == 0 else self._event_ys[i - 1]

    def _clear_marker_badges(self) -> None:
        for badge in self._marker_badges:
            self.plot.removeItem(badge)
        self._marker_badges = []

    def _render_range(self) -> None:
        seconds = {key: secs for key, secs, _c in GRAPH_RANGES}[self._range]
        caption = {key: cap for key, _s, cap in GRAPH_RANGES}[self._range]
        self._hide_scrub()
        self._clear_marker_badges()
        now = float(self._now_fn())
        if not self._event_xs:
            self.curve.setData([], [])
            self.markers.setData([])
            self._trade_points = []
            self.baseline_line.hide()
            self._live_dot.setData([])
            self._live_halo.setData([])
            self._live_point = None
            self._scrub_bounds = None
            self.range_amount.setText("—")
            self.range_amount.setStyleSheet(
                f"font-size: 20px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
            )
            self.range_caption.setText(caption)
            return

        # windows are anchored at NOW, the wall clock (round 4: "today
        # is the 22nd" — anchoring at the last trade made every tab wrong)
        if seconds is not None:
            window_start = now - seconds
        else:
            first = self._event_xs[0]
            window_start = first - max(60.0, (now - first) * 0.02)

        # expand the steps ourselves: start value at the window's left edge,
        # a vertical rise at every event, flat to NOW — no phantom edges
        pxs: list[float] = [window_start]
        pys: list[float] = [self._step_value(window_start)]
        lo = bisect.bisect_right(self._event_xs, window_start)
        for i in range(lo, len(self._event_xs)):
            ts = self._event_xs[i]
            if ts > now:
                break
            pxs.extend((ts, ts))
            pys.extend((pys[-1], self._event_ys[i]))
        pxs.append(now)
        pys.append(pys[-1])
        self._view_xs, self._view_ys = pxs, pys
        self._scrub_bounds = (window_start, now)

        # option A (2026-08-22): the line answers "am I up over what
        # I'm looking at?" — green up, red down, calm blue when flat
        baseline = pys[0]
        delta = pys[-1] - baseline
        pct = (delta / baseline * 100.0) if baseline else 0.0
        color = theme.GREEN if delta > 0 else (theme.RED if delta < 0 else theme.BLUE)
        self._line_color = color
        self.curve.setData(pxs, pys)
        self.curve.setPen(pg.mkPen(color=color, width=2.4))
        top_y = max(pys)
        span = (top_y - min(pys)) or max(1.0, abs(pys[-1]) * 0.002)
        floor = min(pys) - span * 0.10
        # the dimmed fill (screenshot): bright at the line, fading to
        # nothing at the axis. Data-coordinate gradient — ObjectMode restarted
        # per painted section (round 4 fix: the "fresh section" seams)
        near_line = QColor(color)
        near_line.setAlpha(70)
        near_axis = QColor(color)
        near_axis.setAlpha(0)
        gradient = QLinearGradient(0.0, top_y, 0.0, floor)
        gradient.setColorAt(0.0, near_line)
        gradient.setColorAt(1.0, near_axis)
        self.curve.setFillLevel(floor)
        self.curve.setBrush(QBrush(gradient))
        self.baseline_line.setPos(baseline)
        self.baseline_line.setVisible(True)
        x_pad = max(60.0, (now - window_start) * 0.02)
        self.plot.setXRange(window_start, now + x_pad, padding=0)
        self.plot.setYRange(floor, top_y + span * 0.14, padding=0)

        self.range_amount.setText(f"{delta:+,.2f} $ ({pct:+.2f}%)")
        self.range_amount.setStyleSheet(
            f"font-size: 20px; font-weight: 700; background: transparent; color:"
            f" {theme.GREEN if delta > 0 else (theme.RED if delta < 0 else theme.TEXT)};"
        )
        self.range_caption.setText(caption)

        # markers: trades inside the window, anchored ON the curve; trades
        # closing within 90s cluster behind one triangle with a ×N badge
        visible = [t for t in self._trades_sorted if window_start < float(t["ts"]) <= now]
        clusters: list[list[dict]] = []
        for trade in visible:
            if clusters and float(trade["ts"]) - float(clusters[-1][0]["ts"]) <= 90.0:
                clusters[-1].append(trade)
            else:
                clusters.append([trade])
        spots = []
        self._trade_points = []
        for members in clusters:
            x = float(members[-1]["ts"])
            y = self._step_value(x)
            net = sum(float(t["pnl"]) for t in members)
            win = net >= 0
            self._trade_points.append((x, y, members))
            spots.append(
                {
                    "pos": (x, y),
                    "symbol": _triangle_symbol(win),
                    "size": 13,
                    "brush": pg.mkBrush(theme.GREEN if win else theme.RED),
                }
            )
            if len(members) > 1:
                badge = pg.TextItem(
                    f"×{len(members)}", color=(110, 110, 115), anchor=(0.5, 1.7 if win else -0.7)
                )
                badge.setPos(x, y)
                self.plot.addItem(badge)
                self._marker_badges.append(badge)
        self.markers.setData(spots)

        # live pulse on the newest point
        self._live_point = (now, pys[-1])
        self._live_dot.setData([now], [pys[-1]], brush=pg.mkBrush(color), size=8)

    def _on_pulse(self, value) -> None:
        from PyQt6 import sip

        if sip.isdeleted(self.plot) or sip.isdeleted(self._live_halo):
            self._pulse.stop()
            return
        if self._live_point is None or not self.isVisible():
            return
        t = float(value)
        halo = QColor(self._line_color)
        halo.setAlpha(int(100 * (1.0 - t)))
        self._live_halo.setData(
            [self._live_point[0]],
            [self._live_point[1]],
            size=9 + 16 * t,
            brush=pg.mkBrush(halo),
            pen=None,
        )

    def _hide_scrub(self) -> None:
        for item in (self._scrub_line, self._scrub_dot, self._scrub_chip):
            item.hide()

    def _on_scrub_move(self, scene_pos) -> None:
        from PyQt6 import sip

        if sip.isdeleted(self.plot):
            return
        bounds = getattr(self, "_scrub_bounds", None)
        if bounds is None or not self._event_xs:
            return
        plot_item = self.plot.getPlotItem()
        if not plot_item.sceneBoundingRect().contains(scene_pos):
            self._hide_scrub()
            return
        # glued to the LINE, continuously: any hovered time reads the curve's
        # value there (round 4: it jumped triangle-to-triangle and
        # showed nothing in the middle)
        x = plot_item.vb.mapSceneToView(scene_pos).x()
        sx = min(max(x, bounds[0]), bounds[1])
        sy = self._step_value(sx)
        baseline = self._view_ys[0]
        delta = sy - baseline
        pct = (delta / baseline * 100.0) if baseline else 0.0
        chip_color = theme.GREEN if delta >= 0 else theme.RED
        stamp = datetime.fromtimestamp(sx, UTC).astimezone(_ET)
        self._scrub_line.setPos(sx)
        self._scrub_dot.setData([sx], [sy], brush=pg.mkBrush(self._line_color))
        self._scrub_chip.setHtml(
            f'<div style="text-align:center; font-family:-apple-system;">'
            f'<span style="font-size:10px; color:#6E6E73;">'
            f"{stamp.strftime('%b %d · %H:%M ET')}</span><br>"
            f'<span style="font-size:12px; font-weight:700; color:#1D1D1F;">'
            f"${sy:,.2f}</span><br>"
            f'<span style="font-size:10px; font-weight:600; color:{chip_color};">'
            f"{delta:+,.2f} $ ({pct:+.2f}%)</span></div>"
        )
        self._scrub_chip.setPos(sx, sy)
        for item in (self._scrub_line, self._scrub_dot, self._scrub_chip):
            item.show()

    def _render_pie(self) -> None:
        trades = self._data.get("trades") or []
        trades = filter_by_range(trades, self.from_button.selected, self.to_button.selected)
        mode = PIE_MODES[self.pie_mode.currentIndex()][1]
        self.pie.set_wedges(pie_breakdown(trades, mode))
        self._pie_groups = {}
        for trade in trades:
            self._pie_groups.setdefault(bucket_label(trade, mode), []).append(trade)
        # reset the details panel when the breakdown/range changes
        self._reset_pie_details()
