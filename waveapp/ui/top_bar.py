"""Top bar (Phase 8.3, design):

- LEFT: the paper/live slide toggle (slick sliding knob; live locked until the
  Phase 11 gate) — the wave animation and wordmark are gone (design
  decision, 2026-09).
- LEFT-MIDDLE: the trade flip board — an airport split-flap style indicator
  that flashes each buy/sell briefly: green ▲ profit / red ▼ loss on exits,
  and a small monochrome up/down graph glyph marking long vs short on entries.
- RIGHT-MIDDLE: the account balance ("PAPER BALANCE" / "LIVE BALANCE" caption
  above the number). Clicking it opens a small anchored card below (not a
  modal): paper → set/reset balance, live → withdraw (locked until Phase 11).
- RIGHT: transport — play, pause, and the square stop (second click on pause).
"""

from __future__ import annotations

import random

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme

ALPACA_PAPER_DASHBOARD = "https://app.alpaca.markets/paper/dashboard/overview"
ALPACA_LIVE_BANKING = "https://app.alpaca.markets/brokerage/banking"

# The official Alpaca logomark (circle with the alpaca head knocked out),
# extracted from alpaca.markets — recolored monochrome at render time.
_ALPACA_MARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 43.4 43.8">
<path fill="{color}" d="M15.1067 42.9709L16.823 24.641H16.6669C15.2924 24.6412 14.1555 24.1443 \
13.1094 23.2396C12.0632 22.3349 11.3689 21.0814 11.1511 19.7042L14.3912 17.5785V17.5521C14.3911 \
16.1033 14.9422 14.7105 15.9294 13.6641C16.9168 12.6176 18.264 11.9985 19.6902 11.9358V11.9299H2\
0.6537V8.50766C21.3367 8.50768 21.9977 8.75318 22.5194 9.20063C23.0411 9.64811 23.3897 10.2686 2\
3.5036 10.9521H23.544V8.50766C24.0575 8.50768 24.5617 8.64649 25.0047 8.90982C25.4478 9.17317 25\
.8137 9.55152 26.0649 10.006C26.3161 10.4605 26.4434 10.9746 26.434 11.4956C26.4243 12.0166 26.2\
781 12.5256 26.0106 12.9702C26.8808 13.5474 27.5954 14.3357 28.0902 15.2638C28.5847 16.1918 28.8\
435 17.2303 28.843 18.2854V40.4075C28.843 40.7318 28.97 41.0427 29.1957 41.2717C29.4217 41.5011 \
29.728 41.6297 30.0473 41.6297H31.4757C29.4696 42.6632 27.2843 43.3889 24.9791 43.7464C35.3846 4\
2.1329 43.3557 33.0111 43.3557 21.9999C43.3557 9.84971 33.6501 0 21.6778 0C9.70548 0 0 9.84971 0\
 21.9999C0 31.8255 6.34679 40.1466 15.1067 42.9709ZM17.6563 18.116C17.5323 18.242 17.4625 18.412\
7 17.4622 18.5908L17.4015 19.2637H18.7262C19.0776 19.2637 19.4146 19.122 19.663 18.8698C19.9114 \
18.6176 20.051 18.2756 20.051 17.9189H18.1241C17.9486 17.9192 17.7804 17.9901 17.6563 18.116Z"/>
</svg>"""


# -- transport ----------------------------------------------------------------


class PlayButton(QWidget):
    """Round play triangle. Doubles as Resume while the engine is paused."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(38, 38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_hint = "Start"  # "Resume" while paused (no tooltip — 8.6 r3)
        self.setEnabled(False)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # round 4: BLUE when the button is active/enabled, faded gray otherwise
        color = QColor(theme.BLUE) if self.isEnabled() else QColor(142, 142, 147, 130)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        w, h = self.width(), self.height()
        path = QPainterPath()
        path.moveTo(w * 0.36, h * 0.28)
        path.lineTo(w * 0.36, h * 0.72)
        path.lineTo(w * 0.72, h * 0.50)
        path.closeSubpath()
        painter.drawPath(path)


class PauseStopButton(QWidget):
    """First click = Pause, second click = Stop (§3 semantics).
    The glyph morphs between pause bars and the square stop."""

    pause_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._morph = 0.0  # 0 = pause bars, 1 = stop square
        self._armed_stop = False
        self.setFixedSize(38, 38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.setEnabled(False)

    def _on_anim(self, value: float) -> None:
        self._morph = value
        self.update()

    def reset(self) -> None:
        self.set_armed(False)

    def set_armed(self, armed: bool) -> None:
        """armed = engine paused: glyph is the stop square, next click stops."""
        self._armed_stop = armed
        self._anim.stop()
        self._anim.setStartValue(self._morph)
        self._anim.setEndValue(1.0 if armed else 0.0)
        self._anim.start()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        if not self._armed_stop:
            self.set_armed(True)
            self.pause_clicked.emit()
        else:
            self.stop_clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # round 4: BLUE when the button is active/enabled, faded gray otherwise
        color = QColor(theme.BLUE) if self.isEnabled() else QColor(142, 142, 147, 130)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)

        w, h = self.width(), self.height()
        t = self._morph
        cx, cy = w / 2, h / 2
        if t >= 0.98:
            # settled: ONE clean stop square (round 2: not two merged bars)
            painter.drawRoundedRect(QRectF(cx - 7.5, cy - 7.5, 15.0, 15.0), 3.5, 3.5)
            return
        # Pause: two 4px bars morphing toward the square.
        bar_h = 16.0 - t * 1.0
        bar_w = 4.0 + t * 3.5
        gap = 5.0 * (1 - t)
        left = QRectF(cx - gap / 2 - bar_w, cy - bar_h / 2, bar_w, bar_h)
        right = QRectF(cx + gap / 2, cy - bar_h / 2, bar_w, bar_h)
        radius = 1.5 + t * 2.0
        painter.drawRoundedRect(left, radius, radius)
        painter.drawRoundedRect(right, radius, radius)


# -- paper/live toggle --------------------------------------------------------


class ModeToggle(QWidget):
    """Slick slide toggle: a knob glides between PAPER and LIVE (both blue —
    round 2). While a side is CONNECTING its label loads from gray to
    blue, and the fill speed mirrors the real connection time: a slow creep
    while waiting, snapping to full blue the moment the connection lands.

    Live switching requires Touch ID (§4) and live keys in Keychain; without
    keys the knob strains toward LIVE and bounces back (a 'locked' feel)."""

    live_requested = pyqtSignal()  # app.py wires the Touch ID gate here
    switched = pyqtSignal(str)  # user switched display mode (e.g. back to paper)

    WIDTH, HEIGHT = 152, 30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode = "paper"
        self.live_enabled = False  # true when live keys exist in Keychain
        self._knob = 0.0  # 0 = paper, 1 = live
        self._fill = {"paper": 0.0, "live": 0.0}  # gray→blue connection fill
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._fill_anim = QVariantAnimation(self)
        self._fill_side = "paper"
        self._fill_anim.valueChanged.connect(self._on_fill)

    def _on_anim(self, value: float) -> None:
        self._knob = float(value)
        self.update()

    def _on_fill(self, value: float) -> None:
        self._fill[self._fill_side] = float(value)
        self.update()

    def begin_connect(self, side: str) -> None:
        """The side's label starts loading gray→blue: a slow creep that only
        finish_connect() completes — so the animation IS the real wait."""
        self._fill_side = side
        self._fill[side] = 0.0
        self._fill_anim.stop()
        self._fill_anim.setDuration(5000)
        self._fill_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._fill_anim.setStartValue(0.0)
        self._fill_anim.setEndValue(0.85)  # never completes on its own
        self._fill_anim.start()

    def finish_connect(self, side: str, ok: bool = True) -> None:
        self._fill_side = side
        self._fill_anim.stop()
        self._fill_anim.setDuration(220 if ok else 320)
        self._fill_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fill_anim.setStartValue(self._fill[side])
        self._fill_anim.setEndValue(1.0 if ok else 0.0)
        self._fill_anim.start()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(1.0 if mode == "live" else 0.0)
        self._anim.start()

    def _bounce(self) -> None:
        """Locked live side: the knob strains toward LIVE and snaps back."""
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(0.22)
        self._anim.setDuration(110)
        self._anim.start()
        QTimer.singleShot(130, self._bounce_back)

    def _bounce_back(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self._anim.start()
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setDuration(260)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        wants_live = event.position().x() > self.width() / 2
        if wants_live and self.mode == "paper":
            if self.live_enabled:
                self.live_requested.emit()
            else:
                self._bounce()
        elif not wants_live and self.mode == "live":
            self.set_mode("paper")
            self.switched.emit("paper")

    @staticmethod
    def _load_ink(fill: float) -> QColor:
        """Connection fill (round 5): gray loads into BLUE while
        connecting and STAYS blue once connected; a failed connect drains it
        back to gray. Same treatment for paper and live."""
        start = QColor(185, 185, 190)
        blue = QColor(theme.BLUE)
        return QColor(
            int(start.red() + (blue.red() - start.red()) * fill),
            int(start.green() + (blue.green() - start.green()) * fill),
            int(start.blue() + (blue.blue() - start.blue()) * fill),
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        radius = h / 2

        # track
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 26))
        painter.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)

        # white knob glides between the sides
        knob_w = w / 2
        knob_x = self._knob * (w - knob_w)
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(QPen(QColor(0, 0, 0, 30), 1))
        painter.drawRoundedRect(QRectF(knob_x + 2, 2, knob_w - 4, h - 4), radius - 2, radius - 2)

        # labels: the active side loads light-gray→ink with its connection
        # fill (round 3: the tab-bar label color, no accent blue)
        font = QFont(theme.FONT_TEXT, 11)
        font.setBold(True)
        painter.setFont(font)
        paper_rect = QRectF(0, 0, w / 2, h)
        live_rect = QRectF(w / 2, 0, w / 2, h)
        paper_active = self._knob < 0.5
        inactive = QColor(160, 160, 165)
        paper_color = self._load_ink(self._fill["paper"]) if paper_active else inactive
        live_color = self._load_ink(self._fill["live"]) if not paper_active else inactive
        painter.setPen(paper_color)
        painter.drawText(paper_rect, Qt.AlignmentFlag.AlignCenter, "PAPER")
        painter.setPen(live_color)
        painter.drawText(live_rect, Qt.AlignmentFlag.AlignCenter, "LIVE")


# -- trade flip board ---------------------------------------------------------


class FlipBoard(QWidget):
    """Airport split-flap board: flashes each trade briefly, then blanks.

    Cells: (kind, value, color) where kind is 'char' | 'tri_up' | 'tri_down'
    | 'graph_up' | 'graph_down'. Round 4: the flap is slower — every
    character visibly "searches" for its letter; the hold timer only starts
    AFTER the flip settles; and messages QUEUE — a new one never interrupts
    the one on the board, it waits its turn. Errors/flags ride the same board
    via show_message()."""

    CELL_W, CELL_H, GAP = 15.0, 24.0, 3.0
    HOLD_MS = 2600  # round 7: shorter stay (was 4500); from the END of the flip
    FLIP_MS = 2200  # round 5: a bit slower still (was 1500, originally 760)
    _FLAP_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$+-."

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setMinimumWidth(300)
        self._cells: list[tuple[str, str, QColor]] = []
        self._prev_cells: list[tuple[str, str, QColor]] = []
        self._queue: list[list[tuple[str, str, QColor]]] = []
        self._busy = False
        self._progress = 1.0
        self._opacity = 1.0
        self._rng = random.Random()  # noqa: S311 — animation flair, not crypto
        self._flip = QVariantAnimation(self)
        self._flip.setDuration(self.FLIP_MS)
        self._flip.valueChanged.connect(self._on_flip)
        self._flip.finished.connect(self._on_flip_settled)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(320)
        self._fade.valueChanged.connect(self._on_fade)
        self._fade.finished.connect(self._on_faded)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hold_elapsed)

    # -- content --------------------------------------------------------------

    @staticmethod
    def cells_for_trade(payload: dict) -> list[tuple[str, str, QColor]]:
        """payload: {action: entry|scale|close, symbol, long: bool,
        pnl: float|None}"""
        ink = QColor(242, 242, 244)
        cells: list[tuple[str, str, QColor]] = []
        action = payload.get("action", "entry")
        symbol = str(payload.get("symbol", ""))[:6]
        is_long = bool(payload.get("long", True))
        pnl = payload.get("pnl")

        if action == "entry":
            kind = "graph_up" if is_long else "graph_down"
            cells.append((kind, "", QColor(theme.GREY)))
            word = "BUY"
        else:
            profit = (pnl or 0.0) >= 0
            tri_color = QColor(theme.GREEN if profit else theme.RED)
            cells.append(("tri_up" if profit else "tri_down", "", tri_color))
            word = "SELL"
        for ch in word:
            cells.append(("char", ch, ink))
        cells.append(("char", " ", ink))
        for ch in symbol:
            cells.append(("char", ch, ink))
        if pnl is not None:
            cells.append(("char", " ", ink))
            color = QColor(theme.GREEN if pnl >= 0 else theme.RED)
            for ch in f"{pnl:+.2f}":
                cells.append(("char", ch, color))
        return cells

    @staticmethod
    def cells_for_message(text: str) -> list[tuple[str, str, QColor]]:
        """An error/flag on the board: red '!' + the message, red."""
        red = QColor(theme.RED)
        cells: list[tuple[str, str, QColor]] = [("char", "!", red), ("char", " ", red)]
        for ch in text.upper()[:24]:
            cells.append(("char", ch, red))
        return cells

    def show_trade(self, payload: dict) -> None:
        self._enqueue(self.cells_for_trade(payload))

    def show_message(self, text: str) -> None:
        self._enqueue(self.cells_for_message(text))

    # -- queue + animation -----------------------------------------------------

    def _enqueue(self, cells: list[tuple[str, str, QColor]]) -> None:
        if self._busy:
            self._queue.append(cells)  # never interrupt — wait your turn
            return
        self._display(cells)

    def _display(
        self,
        cells: list[tuple[str, str, QColor]],
        prev: list[tuple[str, str, QColor]] | None = None,
    ) -> None:
        self._busy = True
        self._prev_cells = prev or []
        self._cells = cells
        self._opacity = 1.0
        self._fade.stop()
        self._hide_timer.stop()
        self._flip.stop()
        self._flip.setStartValue(0.0)
        self._flip.setEndValue(1.0)
        self._flip.start()
        self.update()

    def _on_flip(self, value: float) -> None:
        self._progress = float(value)
        self.update()

    def _on_flip_settled(self) -> None:
        # the display clock starts only once every letter has settled
        self._prev_cells = []
        self._hide_timer.start(self.HOLD_MS)

    def _on_hold_elapsed(self) -> None:
        if self._queue:
            # round 7: the next message flaps IN PLACE over the current one —
            # characters change like a real departures board, no fade between
            self._display(self._queue.pop(0), prev=self._cells)
        else:
            self._start_fade()

    def _start_fade(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade(self, value: float) -> None:
        self._opacity = float(value)
        self.update()

    def _on_faded(self) -> None:
        if self._opacity <= 0.02:
            self._cells = []
            self._busy = False
            self.update()
            if self._queue:  # something arrived during the fade-out
                self._display(self._queue.pop(0))

    # -- painting -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._cells:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._opacity)
        font = QFont("SF Mono", 13)
        font.setBold(True)
        if font.family() != "SF Mono":
            font = QFont("Menlo", 13)
            font.setBold(True)

        y = (self.height() - self.CELL_H) / 2
        x = 0.0
        count = max(len(self._cells), len(self._prev_cells))
        for index in range(count):
            target = self._cells[index] if index < len(self._cells) else None
            old = self._prev_cells[index] if index < len(self._prev_cells) else None
            local = min(1.0, max(0.0, (self._progress * 1.4 - index * 0.035) / 0.30))
            if target is None and local >= 1.0:
                x += self.CELL_W + self.GAP
                continue  # a leftover cell of the previous message flapped out
            rect = QRectF(x, y, self.CELL_W, self.CELL_H)
            # the flap: cell squashes vertically and spins through characters
            squash = abs(1 - 2 * ((local * 3.0) % 1.0)) if 0.0 < local < 1.0 else 1.0
            cell = QRectF(
                rect.x(),
                rect.center().y() - rect.height() * squash / 2,
                rect.width(),
                rect.height() * squash,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(38, 38, 43))
            painter.drawRoundedRect(cell, 3.5, 3.5)
            # split-flap hinge line
            painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
            painter.drawLine(
                QPointF(cell.left() + 1, cell.center().y()),
                QPointF(cell.right() - 1, cell.center().y()),
            )
            # round 7: a queued message CHANGES the characters in place —
            # the old glyph holds until this cell's flap reaches it, spins,
            # and lands on the new one (no fade-out/fade-in between messages)
            if local <= 0.0:
                shown_cell = old
            elif local < 1.0:
                shown_cell = target if target is not None else old
            else:
                shown_cell = target
            if shown_cell is None:
                x += self.CELL_W + self.GAP
                continue
            kind, value, color = shown_cell
            shown = value
            if kind == "char" and 0.0 < local < 1.0 and value.strip():
                shown = self._rng.choice(self._FLAP_CHARS)
            painter.setPen(color)
            painter.setFont(font)
            if kind == "char":
                painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, shown)
            elif kind in ("tri_up", "tri_down"):
                self._draw_triangle(painter, cell, color, up=kind == "tri_up")
            else:
                self._draw_graph(painter, cell, color, up=kind == "graph_up")
            x += self.CELL_W + self.GAP

    @staticmethod
    def _draw_triangle(painter: QPainter, rect: QRectF, color: QColor, up: bool) -> None:
        path = QPainterPath()
        cx, cy = rect.center().x(), rect.center().y()
        r = min(rect.width(), rect.height()) * 0.30
        if up:
            path.moveTo(cx - r, cy + r * 0.8)
            path.lineTo(cx + r, cy + r * 0.8)
            path.lineTo(cx, cy - r)
        else:
            path.moveTo(cx - r, cy - r * 0.8)
            path.lineTo(cx + r, cy - r * 0.8)
            path.lineTo(cx, cy + r)
        path.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)

    @staticmethod
    def _draw_graph(painter: QPainter, rect: QRectF, color: QColor, up: bool) -> None:
        """Tiny monochrome trend glyph: long = rising zigzag, short = falling."""
        pen = QPen(QColor(220, 220, 224), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        w, h = rect.width(), rect.height()
        xs = [rect.x() + w * f for f in (0.18, 0.42, 0.62, 0.84)]
        if up:
            ys = [rect.y() + h * f for f in (0.72, 0.52, 0.60, 0.30)]
        else:
            ys = [rect.y() + h * f for f in (0.28, 0.48, 0.40, 0.70)]
        path = QPainterPath(QPointF(xs[0], ys[0]))
        for px, py in zip(xs[1:], ys[1:], strict=True):
            path.lineTo(px, py)
        painter.drawPath(path)


# -- balance ------------------------------------------------------------------


class BalanceWidget(QWidget):
    """Caption ('PAPER BALANCE' / 'LIVE BALANCE') over the number, in a thin
    bordered chip (round 2). Click → the anchored BalanceCard drops below."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode = "paper"
        self.value: float | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 4, 14, 4)
        layout.setSpacing(0)
        self.caption = QLabel("PAPER BALANCE")
        self.caption.setStyleSheet(
            f"font-size: 9px; font-weight: 700; letter-spacing: 1px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.amount = QLabel("—")
        self.amount.setStyleSheet(
            f"font-size: 17px; font-weight: 600; color: {theme.TEXT}; background: transparent;"
        )
        self.amount.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.caption)
        layout.addWidget(self.amount)

    def set_balance(self, mode: str, value: float | None) -> None:
        self.mode = mode
        self.value = value
        self.caption.setText("LIVE BALANCE" if mode == "live" else "PAPER BALANCE")
        self.amount.setText("—" if value is None else f"${value:,.2f}")

    def paintEvent(self, event) -> None:  # noqa: N802
        # round 2: a thin border + slightly distinct background (a chip)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(QPen(QColor(0, 0, 0, 36), 1))
        painter.setBrush(QColor(255, 255, 255, 110))
        painter.drawRoundedRect(rect, 10, 10)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class AlpacaLogoButton(QWidget):
    """The official Alpaca logomark, monochrome, as a button."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, size: int = 72) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False
        ink = QColor(42, 42, 48)
        self._renderer = QSvgRenderer(
            bytes(
                _ALPACA_MARK_SVG.format(color=f"rgb({ink.red()},{ink.green()},{ink.blue()})"),
                "utf-8",
            )
        )

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
        painter.setOpacity(1.0 if self._hover else 0.82)
        size = min(self.width(), self.height()) - 12
        rect = QRectF((self.width() - size) / 2, (self.height() - size) / 2, size, size)
        self._renderer.render(painter, rect)


class BalanceCard(QFrame):
    """Small anchored card below the balance (a Qt popup, not a dialog —
    clicking anywhere else dismisses it). Round 2: just the Alpaca
    logomark as a button, because everything balance-related happens on
    Alpaca's site — paper: the paper dashboard (set/reset balance); live:
    the banking page (withdraw to the linked bank account)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(210)
        self._url = ALPACA_PAPER_DASHBOARD

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        self.title = QLabel("Paper balance")
        self.title.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.title)
        self.logo_button = AlpacaLogoButton()
        self.logo_button.clicked.connect(self._open_alpaca)
        layout.addWidget(self.logo_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setMinimumHeight(30)  # word-wrapped QLabel under-reports height
        self.note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.note.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.note)
        self.set_mode("paper")

    def set_mode(self, mode: str) -> None:
        paper = mode != "live"
        self.title.setText("Paper balance" if paper else "Live balance")
        self._url = ALPACA_PAPER_DASHBOARD if paper else ALPACA_LIVE_BANKING
        self.note.setText(
            "Set / reset on your Alpaca dashboard"
            if paper
            else "Withdraw to your linked bank on Alpaca"
        )

    def _open_alpaca(self) -> None:
        QDesktopServices.openUrl(QUrl(self._url))

    def open_below(self, anchor: QWidget) -> None:
        self.adjustSize()
        corner = anchor.mapToGlobal(QPoint(anchor.width() // 2, anchor.height() + 6))
        self.move(corner - QPoint(self.width() // 2, 0))
        self.show()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(QPen(QColor(0, 0, 0, 30), 1))
        painter.setBrush(QColor(255, 255, 255, 242))
        painter.drawRoundedRect(rect, theme.RADIUS_CARD, theme.RADIUS_CARD)


# -- the bar ------------------------------------------------------------------


class TopBar(QFrame):
    start_clicked = pyqtSignal()
    pause_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(64)
        # round 3: depth stays on the tabs bar only — the top bar is back to
        # pure glass
        self.setStyleSheet("TopBar { background: transparent; border: none; }")
        self.display_mode = "paper"
        self._equity: dict[str, float | None] = {"paper": None, "live": None}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(12)

        self.mode_toggle = ModeToggle()
        self.mode_toggle.switched.connect(self.set_display_mode)
        layout.addWidget(self.mode_toggle)

        layout.addSpacing(6)
        self.flip_board = FlipBoard()
        layout.addWidget(self.flip_board, stretch=1)

        self.balance = BalanceWidget()
        self.balance_card = BalanceCard(self)
        self.balance.clicked.connect(self._toggle_balance_card)
        layout.addWidget(self.balance)

        layout.addSpacing(28)  # round 2: breathing room before the transport
        self.start_button = PlayButton()
        self.start_button.clicked.connect(self.start_clicked)
        layout.addWidget(self.start_button)

        self.pause_stop = PauseStopButton()
        self.pause_stop.pause_clicked.connect(self.pause_clicked)
        self.pause_stop.stop_clicked.connect(self.stop_clicked)
        layout.addWidget(self.pause_stop)

    # -- balance --------------------------------------------------------------

    def set_balance(self, mode: str, value: float | None) -> None:
        """Equity update for a mode; the display only changes if that mode is
        the one currently shown (paper polls keep flowing while live shows)."""
        self._equity[mode] = value
        if mode == self.display_mode:
            self.balance.set_balance(mode, value)
            self.balance_card.set_mode(mode)

    def set_display_mode(self, mode: str) -> None:
        self.display_mode = mode
        self.balance.set_balance(mode, self._equity.get(mode))
        self.balance_card.set_mode(mode)
        if self.mode_toggle.mode != mode:
            self.mode_toggle.set_mode(mode)

    def _toggle_balance_card(self) -> None:
        if self.balance_card.isVisible():
            self.balance_card.hide()
        else:
            self.balance_card.set_mode(self.balance.mode)
            self.balance_card.open_below(self.balance)

    # -- trades & alerts ------------------------------------------------------

    def show_trade(self, payload: dict) -> None:
        self.flip_board.show_trade(payload)

    def show_alert(self, text: str) -> None:
        """Errors/flags ride the flip board (round 4 — the status bar is gone)."""
        self.flip_board.show_message(text)

    # -- window drag (frameless-style chrome) ---------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()

    # -- engine state → chrome (the engine is the source of truth) ------------

    _ENGINE_UI = {
        "idle": (True, False, False, "Start"),
        "running": (False, True, False, "Start"),
        "paused": (True, True, True, "Resume"),
        "stopping": (False, False, False, "Start"),
        # after a kill, Start becomes the Touch-ID-gated re-arm path (§4)
        "killed": (True, False, False, "Re-arm"),
    }

    def set_engine_state(self, state: str) -> None:
        entry = self._ENGINE_UI.get(state)
        if entry is None:
            return
        start_enabled, pause_enabled, armed, start_tip = entry
        self.start_button.setEnabled(start_enabled)
        self.start_button.action_hint = start_tip
        self.pause_stop.setEnabled(pause_enabled)
        self.pause_stop.set_armed(armed)
