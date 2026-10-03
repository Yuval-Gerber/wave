"""ML tab — the BRAIN (2026-08-30; "the Judge" is dead).

Wave's learning mind gets the same universe as the Scanner's — a NeuralCanvas
galaxy, full-bleed on the page (no card behind it) — and it shows REAL brain
activity, never a loop: every new labeled lesson, every would-approve and
would-reject verdict arrives as an event and lights the galaxy. Sub-tabs:
Brain (the living universe + vitals), Ladder (the win-rate climb, drawn),
Data (the lesson deck), Models (the Brain's report card).
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme

SECTIONS = ("Brain", "Ladder", "Data", "Models")
MODES = (("off", "OFF"), ("shadow", "SHADOW"), ("active", "ACTIVE"))
MILESTONES = (70, 75, 80)

MODE_WORDS = {
    "off": "The Brain is resting — Wave trades on rules alone.",
    "shadow": "SHADOW — the Brain watches every candidate and writes its "
    "verdict in the journal. Zero influence on trading.",
    "active": "ACTIVE — the Brain gates entries (validated model required).",
}


def _wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


class FlowCanvas(QWidget):
    """THE NETWORK (round 7 — exactly screenshot from Tonumoy/
    Simple-AI-Animations, animated): the classic architecture diagram —
    Input → 10 → 10 → 5 → Output neurons joined by a dense web of curved
    threads — with candidate-dots RACING through it at brain speed. A dot
    that fails a stage flares RED and dies right there; a dot that survives
    every layer bursts GREEN at the output. Real events inject guaranteed
    dots (lesson blue-white, approve destined-green, reject destined-red);
    the ambient stream runs at the live approve ratio. Transparent widget —
    threads and neurons straight on the dark glass."""

    LAYERS = (1, 10, 10, 5, 1)
    STAGE_NAMES = ("candidate", "price & volume", "market context", "pattern memory", "verdict")
    HOP_SECONDS = 0.13  # brain speed (faster — it should FEEL fast)
    SPAWN_PER_SECOND = 9.0
    MAX_DOTS = 70

    def __init__(self, parent: QWidget | None = None, seed: int | None = None) -> None:
        super().__init__(parent)
        import random

        self.setMinimumHeight(300)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._rng = random.Random(seed)  # noqa: S311 — animation, not crypto
        self._nodes: list[list] = []  # [layer][index] = (x, y) filled on resize
        self._dots: list[dict] = []
        self._flares: list[dict] = []  # node verdict glows
        self._queued: list[str] = []  # real-event dots awaiting spawn
        self._accept_ratio = 0.62
        self._spawn_carry = 0.0
        self._time = 0.0
        from PyQt6.QtCore import QTimer

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    # -- REAL event API (unchanged names) ------------------------------------

    def candidate_found(self, _symbol: str = "") -> None:
        self._queued.append("lesson")

    def candidate_promoted(self, _symbol: str = "") -> None:
        self._queued.append("approve")

    def candidate_rejected(self, _symbol: str = "", _reason: str = "") -> None:
        self._queued.append("reject")

    def pulse_scan(self) -> None:
        for _ in range(4):
            self._queued.append("lesson")

    def set_activity(self, _dots_per_second: float, accept_ratio: float) -> None:
        self._accept_ratio = min(1.0, max(0.05, accept_ratio))

    # -- lifecycle ------------------------------------------------------------

    def hideEvent(self, event) -> None:  # noqa: N802
        # keeps running hidden (the Brain animation never stops)
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        if not self._timer.isActive():
            self._timer.start(33)
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)

    # -- geometry --------------------------------------------------------------

    def _layout_nodes(self) -> None:
        w, h = self.width(), self.height()
        self._nodes = []
        margin_x, top, bottom = w * 0.06, h * 0.10, h * 0.90
        span = w - margin_x * 2
        for li, count in enumerate(self.LAYERS):
            x = margin_x + span * li / (len(self.LAYERS) - 1)
            column = []
            for ni in range(count):
                if count == 1:
                    y = (top + bottom) / 2
                else:
                    y = top + (bottom - top) * ni / (count - 1)
                column.append((x, y))
            self._nodes.append(column)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._layout_nodes()
        super().resizeEvent(event)

    @staticmethod
    def _edge_point(a, b, t: float):
        """Point along the S-curved thread from node a to node b."""
        from PyQt6.QtCore import QPointF

        mx = (a[0] + b[0]) / 2
        # cubic bezier with horizontal control handles → the classic diagram curve
        s = 1.0 - t
        x = s * s * s * a[0] + 3 * s * s * t * mx + 3 * s * t * t * mx + t * t * t * b[0]
        y = s * s * s * a[1] + 3 * s * s * t * a[1] + 3 * s * t * t * b[1] + t * t * t * b[1]
        return QPointF(x, y)

    # -- simulation ------------------------------------------------------------

    def _spawn_dot(self, kind: str) -> None:
        rng = self._rng
        if kind == "approve":
            fate = None  # destined to finish — a REAL approval
        elif kind == "reject":
            fate = rng.randrange(1, len(self.LAYERS) - 1)  # a REAL rejection dies inside
        else:
            fate = None  # ambient thinking is NEUTRAL — no invented verdicts
        self._dots.append(
            {
                "stage": 0,
                "t": rng.uniform(0.0, 0.2),
                "from": 0,
                "to": rng.randrange(self.LAYERS[1]),
                "fail_at": fate,
                "speed": rng.uniform(0.85, 1.25),
                "kind": kind,
            }
        )

    def _tick(self) -> None:
        self._time += 0.033
        if not self._nodes and self.width() > 20:
            self._layout_nodes()
        if not self._nodes:
            return
        rng = self._rng
        # spawns: queued real events first, then the ambient stream
        while self._queued and len(self._dots) < self.MAX_DOTS:
            self._spawn_dot(self._queued.pop(0))
        self._spawn_carry += self.SPAWN_PER_SECOND * 0.033
        while self._spawn_carry >= 1.0:
            self._spawn_carry -= 1.0
            if len(self._dots) < self.MAX_DOTS:
                self._spawn_dot("ambient")
        alive = []
        for dot in self._dots:
            dot["t"] += 0.033 / self.HOP_SECONDS * dot["speed"] / 3.3
            if dot["t"] < 1.0:
                alive.append(dot)
                continue
            arrived_layer = dot["stage"] + 1
            node = dot["to"]
            if dot["fail_at"] == arrived_layer:
                # a REAL rejection dies here — quick dim red flare
                self._flares.append(
                    {"layer": arrived_layer, "node": node, "age": 0.0, "color": (255, 92, 80)}
                )
                continue
            if arrived_layer == len(self.LAYERS) - 1:
                if dot["kind"] == "approve":
                    color = (74, 230, 128)  # a REAL approval — green at the output
                elif dot["kind"] == "lesson":
                    color = (150, 200, 255)  # knowledge arriving — soft blue
                else:
                    color = (220, 228, 255)  # neutral thought completing — faint white
                self._flares.append(
                    {
                        "layer": arrived_layer,
                        "node": node,
                        "age": 0.0,
                        "color": color,
                        "dim": dot["kind"] not in ("approve", "reject"),
                    }
                )
                continue
            dot["stage"] = arrived_layer
            dot["from"] = node
            dot["to"] = rng.randrange(self.LAYERS[arrived_layer + 1])
            dot["t"] = 0.0
            alive.append(dot)
        self._dots = alive
        for flare in self._flares:
            flare["age"] += 0.033
        self._flares = [f for f in self._flares if f["age"] < 0.55]
        self.update()

    # -- painting --------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QPainterPath, QRadialGradient

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._nodes:
            return

        # the web — every thread between consecutive layers, faint
        painter.setPen(QPen(QColor(225, 232, 255, 17), 1))
        for li in range(len(self.LAYERS) - 1):
            for a in self._nodes[li]:
                for b in self._nodes[li + 1]:
                    mx = (a[0] + b[0]) / 2
                    path = QPainterPath(QPointF(a[0], a[1]))
                    path.cubicTo(QPointF(mx, a[1]), QPointF(mx, b[1]), QPointF(b[0], b[1]))
                    painter.drawPath(path)

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        # neurons — white, breathing softly
        breath = 0.8 + 0.2 * math.sin(self._time * 1.1)
        for li, column in enumerate(self._nodes):
            radius = 7.0 if li in (0, len(self.LAYERS) - 1) else 5.2
            for x, y in column:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(235, 240, 255, int(200 * breath)))
                painter.drawEllipse(QPointF(x, y), radius, radius)

        # verdict flares — red death / green glory at the node
        for flare in self._flares:
            x, y = self._nodes[flare["layer"]][flare["node"]]
            fade = 1.0 - flare["age"] / 0.55
            r, g, b = flare["color"]
            radius = 8.0 if flare.get("dim") else 12.0
            peak = 60 if flare.get("dim") else 120
            halo = QRadialGradient(QPointF(x, y), radius)
            halo.setColorAt(0.0, QColor(r, g, b, int(peak * fade)))
            halo.setColorAt(1.0, QColor(r, g, b, 0))
            painter.setBrush(halo)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(x, y), radius, radius)

        # stage captions — a glance tells you what each column weighs
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QPen(QColor(185, 190, 205, 170)))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 1.0)
        painter.setFont(font)
        h = self.height()
        for li, column in enumerate(self._nodes):
            x = column[0][0]
            painter.drawText(
                int(x - 70),
                int(h * 0.93),
                140,
                16,
                Qt.AlignmentFlag.AlignCenter,
                self.STAGE_NAMES[li].upper(),
            )
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        # the dots — candidates racing the web at brain speed
        for dot in self._dots:
            a = self._nodes[dot["stage"]][dot["from"]]
            b = self._nodes[dot["stage"] + 1][dot["to"]]
            pos = self._edge_point(a, b, min(dot["t"], 1.0))
            color = {
                "approve": QColor(120, 240, 160, 235),
                "reject": QColor(255, 150, 130, 235),
                "lesson": QColor(160, 205, 255, 235),
            }.get(dot["kind"], QColor(200, 215, 255, 210))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(pos, 2.6, 2.6)
            # a short motion tail
            tail = self._edge_point(a, b, max(0.0, dot["t"] - 0.08))
            painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 2))
            painter.drawLine(tail, pos)


class _Vital(QFrame):
    """A vitals tile: big number, caption, small delta line, accent bar."""

    def __init__(self, title: str, accent: str = theme.BLUE) -> None:
        super().__init__()
        self.setProperty("card", True)
        self._accent = accent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(3)
        self.value = QLabel("—")
        self.value.setStyleSheet(
            f"font-size: 26px; font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        self.delta = QLabel(" ")
        self.delta.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {accent}; background: transparent;"
        )
        caption = QLabel(title.upper())
        caption.setStyleSheet(
            f"font-size: 10px; font-weight: 700; letter-spacing: 0.6px;"
            f" color: {theme.TEXT_MUTED}; background: transparent;"
        )
        layout.addWidget(self.value)
        layout.addWidget(self.delta)
        layout.addWidget(caption)

    def set(self, value: str, delta: str = " ") -> None:
        self.value.setText(value)
        self.delta.setText(delta or " ")


class _LadderRow(QWidget):
    """One rung of the win-rate ladder, painted: threshold, a win-rate bar
    with milestone ticks, the proven floor, and coverage — beautiful and
    exact, not a pasted table."""

    HEIGHT = 44

    def __init__(self, row: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self.setFixedHeight(self.HEIGHT)
        self.setMinimumWidth(420)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._row
        w, h = self.width(), self.height()
        bar_left, bar_right = 118.0, w - 215.0
        bar_w = max(10.0, bar_right - bar_left)
        mid = h / 2.0

        # threshold chip
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 122, 255, 24))
        painter.drawRoundedRect(0, int(mid) - 12, 100, 24, 7, 7)
        painter.setPen(QPen(QColor(theme.BLUE)))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            0,
            int(mid) - 12,
            100,
            24,
            Qt.AlignmentFlag.AlignCenter,
            f"≥ {r['threshold'] * 100:.0f}% sure",
        )

        if r.get("empty"):
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(
                int(bar_left),
                0,
                int(bar_w),
                h,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                "not enough trades at this confidence yet — the 3-year class will fill it",
            )
            return
        # track + win-rate bar (blue → green past 70). NO lines across the
        # bar (confusing) — the floor is a marker ABOVE the bar tip
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 14))
        painter.drawRoundedRect(int(bar_left), int(mid) - 7, int(bar_w), 14, 7, 7)
        frac = min(1.0, r["win_rate"] / 100.0)
        wr_color = QColor(theme.GREEN) if r["win_rate"] >= 70 else QColor(theme.BLUE)
        painter.setBrush(wr_color)
        painter.drawRoundedRect(int(bar_left), int(mid) - 7, int(bar_w * frac), 14, 7, 7)
        # the proven floor — a small solid triangle under the bar
        floor_x = bar_left + bar_w * min(1.0, r["wilson"] / 100.0)
        tri = [
            QPointF(floor_x, mid + 9),
            QPointF(floor_x - 4, mid + 15),
            QPointF(floor_x + 4, mid + 15),
        ]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.TEXT))
        painter.drawPolygon(*tri)

        # right column: win rate + coverage
        painter.setPen(QPen(wr_color))
        painter.drawText(
            int(bar_right) + 10,
            int(mid) - 12,
            200,
            14,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"wins {r['win_rate']:.1f}%  (floor {r['wilson']:.0f}%)",
        )
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        painter.drawText(
            int(bar_right) + 10,
            int(mid) + 0,
            200,
            14,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{r['trades']} trades · {r['coverage']:.0f}% kept",
        )


class _PrecisionChart(QWidget):
    """M2b: the Brain's daily live precision (approved judgments, resolved
    nightly) as a small line — the ladder's evidence, drawn."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._series: list[dict] = []
        self.setMinimumHeight(72)
        self.hide()

    def set_series(self, series: list[dict]) -> None:
        self._series = series or []
        self.setVisible(len(self._series) >= 2)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor, QPainter, QPen

        if len(self._series) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad = 14.0
        lo, hi = 30.0, 90.0

        def y_of(wr: float) -> float:
            clamped = max(lo, min(hi, wr))
            return pad + (h - 2 * pad) * (1 - (clamped - lo) / (hi - lo))

        # the 55% approval-threshold guide
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(pad, y_of(55)), QPointF(w - pad, y_of(55)))
        step = (w - 2 * pad) / max(len(self._series) - 1, 1)
        points = [
            QPointF(pad + i * step, y_of(float(p.get("wr", 0.0))))
            for i, p in enumerate(self._series)
        ]
        painter.setPen(QPen(QColor(theme.BLUE), 2))
        for a, b in zip(points[:-1], points[1:], strict=True):
            painter.drawLine(a, b)
        painter.setPen(Qt.PenStyle.NoPen)
        for point, p in zip(points, self._series, strict=True):
            wr = float(p.get("wr", 0.0))
            painter.setBrush(QColor(theme.GREEN if wr >= 55 else theme.RED))
            painter.drawEllipse(point, 3.0, 3.0)
        last = self._series[-1]
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            QRectF(0, 0, w - 6, pad + 2),
            Qt.AlignmentFlag.AlignRight,
            f"{last.get('wr', 0):.0f}% on {last.get('n', 0)} ({last.get('day', '')})",
        )
        painter.end()


class MLPage(QWidget):
    """The Brain's home. External contract unchanged (ml_mode_changed /
    set_ml_mode_display / update_ml) — the galaxy shows real activity only."""

    ml_mode_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last_labeled: int | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Brain")
        title.setStyleSheet(
            "font-size: 22px; font-weight: 700; color: #E8E8ED; background: transparent;"
        )  # sits on the dark glass — bright like the sidebar tabs
        header.addWidget(title)
        header.addStretch(1)
        self.ml_mode_buttons: dict[str, QPushButton] = {}
        for key, label in MODES:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { padding: 4px 14px; border: 1px solid rgba(0,0,0,0.14);"
                " border-radius: 7px; font-weight: 600; background: white; }"
                f"QPushButton:checked {{ background: {theme.BLUE}; color: white;"
                f" border-color: {theme.BLUE}; }}"
                "QPushButton:disabled { color: rgba(0,0,0,0.25); }"
            )
            button.clicked.connect(lambda _c=False, k=key: self._mode_clicked(k))
            header.addWidget(button)
            self.ml_mode_buttons[key] = button
        self.ml_mode_buttons["off"].setChecked(True)
        self.ml_mode_buttons["active"].setEnabled(False)  # earned, never given
        outer.addLayout(header)

        self.status_line = QLabel(MODE_WORDS["off"])
        self.status_line.setStyleSheet("font-size: 12px; color: #B9B9C4; background: transparent;")
        outer.addWidget(self.status_line)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.section_buttons: list[QPushButton] = []
        for index, name in enumerate(SECTIONS):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { padding: 3px 12px; border: none; border-radius: 6px;"
                f" color: {theme.TEXT_MUTED}; background: transparent; font-weight: 600; }}"
                "QPushButton:checked { background: rgba(0,122,255,0.14);"
                f" color: {theme.BLUE}; }}"
            )
            button.clicked.connect(lambda _c=False, i=index: self.select_section(i))
            tabs.addWidget(button)
            self.section_buttons.append(button)
        tabs.addStretch(1)
        outer.addLayout(tabs)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, stretch=1)

        # -- Brain: the living galaxy (full-bleed, no card) ------------------
        brain = QWidget()
        brain_layout = QVBoxLayout(brain)
        brain_layout.setContentsMargins(0, 0, 0, 0)
        brain_layout.setSpacing(12)
        self.canvas = FlowCanvas()
        self.canvas.setMinimumHeight(300)
        brain_layout.addWidget(self.canvas, stretch=1)
        vitals = QHBoxLayout()
        vitals.setSpacing(12)
        self.vital_lessons = _Vital("lessons learned", theme.BLUE)
        self.vital_wins = _Vital("winning lessons", theme.GREEN)
        self.vital_losses = _Vital("losing lessons", theme.RED)
        self.vital_mode = _Vital("state", theme.BLUE)
        for tile in (self.vital_lessons, self.vital_wins, self.vital_losses, self.vital_mode):
            vitals.addWidget(tile)
        brain_layout.addLayout(vitals)
        self.stack.addWidget(brain)

        # -- Ladder ----------------------------------------------------------
        ladder = QFrame()
        ladder.setProperty("card", True)
        ladder_layout = QVBoxLayout(ladder)
        ladder_layout.setContentsMargins(24, 18, 24, 18)
        ladder_layout.setSpacing(4)
        ladder_title = QLabel("The win-rate ladder")
        ladder_title.setProperty("cardTitle", True)
        ladder_sub = QLabel(
            "Each rung answers one question: if Wave only took trades the Brain is "
            "THIS sure about, what would the win rate be? The colored bar is the "
            "measured win rate (green once it clears 70%); the small ▲ under the bar "
            "is the PROVEN floor — what the statistics guarantee, not what we hope."
        )
        ladder_sub.setWordWrap(True)
        ladder_sub.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        ladder_layout.addWidget(ladder_title)
        ladder_layout.addWidget(ladder_sub)
        self._ladder_rows_layout = QVBoxLayout()
        self._ladder_rows_layout.setSpacing(2)
        ladder_layout.addLayout(self._ladder_rows_layout)
        self.ladder_note = QLabel("")
        self.ladder_note.setWordWrap(True)
        self.ladder_note.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        ladder_layout.addWidget(self.ladder_note)
        self.precision_chart = _PrecisionChart()  # M2b: live daily precision
        ladder_layout.addWidget(self.precision_chart)
        ladder_layout.addStretch(1)
        self.stack.addWidget(ladder)

        # -- Data ------------------------------------------------------------
        data = QWidget()
        data_layout = QVBoxLayout(data)
        data_layout.setContentsMargins(0, 0, 0, 0)
        data_layout.setSpacing(12)
        deck = QFrame()
        deck.setProperty("card", True)
        deck_layout = QVBoxLayout(deck)
        deck_layout.setContentsMargins(24, 18, 24, 18)
        deck_layout.setSpacing(8)
        deck_title = QLabel("The lesson deck")
        deck_title.setProperty("cardTitle", True)
        deck_layout.addWidget(deck_title)
        self.deck_bar = _SplitBar()
        deck_layout.addWidget(self.deck_bar)
        self.deck_label = QLabel("Waiting for the first dataset snapshot…")
        self.deck_label.setWordWrap(True)
        self.deck_label.setStyleSheet(
            f"font-size: 13px; color: {theme.TEXT}; background: transparent;"
        )
        deck_layout.addWidget(self.deck_label)
        schedule = QLabel(
            "How the deck grows: the labeler studies every scanned candidate at night "
            "and on weekends — including the trades Wave never took. A 3-year history "
            "class is downloading now; every market day adds fresh lessons forever."
        )
        schedule.setWordWrap(True)
        schedule.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_MUTED}; background: transparent;"
        )
        deck_layout.addWidget(schedule)
        data_layout.addWidget(deck)
        data_layout.addStretch(1)
        self.stack.addWidget(data)

        # -- Models ----------------------------------------------------------
        models = QFrame()
        models.setProperty("card", True)
        models_layout = QVBoxLayout(models)
        models_layout.setContentsMargins(24, 18, 24, 18)
        models_layout.setSpacing(8)
        models_title = QLabel("Report card")
        models_title.setProperty("cardTitle", True)
        models_layout.addWidget(models_title)
        self.models_label = QLabel("")
        self.models_label.setWordWrap(True)
        self.models_label.setStyleSheet(
            f"font-size: 13px; color: {theme.TEXT}; background: transparent; line-height: 150%;"
        )
        models_layout.addWidget(self.models_label)
        models_layout.addStretch(1)
        self.stack.addWidget(models)

        self.select_section(0)
        self._load_curve_file()

    # -- sections ------------------------------------------------------------

    def select_section(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self.section_buttons):
            button.setChecked(i == index)

    # -- mode switch ----------------------------------------------------------

    def _mode_clicked(self, key: str) -> None:
        for name, button in self.ml_mode_buttons.items():
            button.setChecked(name == key)
        self.ml_mode_changed.emit(key)

    def _ml_mode_clicked(self, key: str) -> None:  # back-compat alias
        self._mode_clicked(key)

    def set_ml_mode_display(self, mode: str) -> None:
        for name, button in self.ml_mode_buttons.items():
            button.setChecked(name == mode)
        self.status_line.setText(MODE_WORDS.get(mode, mode))

    def set_ml_status(self, text: str) -> None:
        """Free-form status line (e.g. the ACTIVE refusal) — same widget the
        mode words live on, so the message shows up where the pills are."""
        self.status_line.setText(text)

    # -- live stats → galaxy events (REAL activity, never a loop) -------------

    def update_ml(self, stats: dict) -> None:
        if not stats:
            return
        # blueprint 9.4: ladder + report card follow the OOF curve FILE — a
        # nightly retrain rewrites it and the sub-tabs refresh on the next
        # payload instead of waiting for a relaunch
        stamp = stats.get("curve_stamp")
        if stamp and stamp != getattr(self, "_curve_stamp", None):
            self._curve_stamp = stamp
            self._load_curve_file()
        # lessons = close-time labeled deck + LIVE shadow verdicts (
        # "the water is flowing so it should drink nonstop")
        labeled = int(stats.get("labeled", 0)) + int(stats.get("shadow_scored", 0))
        wins = int(stats.get("label_wins", 0))
        losses = int(stats.get("label_losses", 0))
        delta = 0 if self._last_labeled is None else max(0, labeled - self._last_labeled)
        self._last_labeled = labeled
        # every NEW lesson since the last snapshot lights a star (capped so a
        # backfill burst can't flood the queue)
        for _ in range(min(delta, 25)):
            self.canvas.candidate_found("")
        if delta:
            self.canvas.pulse_scan()
        # shadow verdicts, once the Brain starts watching live
        for _ in range(min(int(stats.get("shadow_approved_new", 0)), 15)):
            self.canvas.candidate_promoted("")
        for _ in range(min(int(stats.get("shadow_rejected_new", 0)), 15)):
            self.canvas.candidate_rejected("")

        self.vital_lessons.set(f"{labeled:,}", f"+{delta}" if delta else " ")
        self.vital_wins.set(f"{wins:,}")
        self.vital_losses.set(f"{losses:,}")
        self.vital_mode.set(str(stats.get("mode", "off")).upper())
        if stats.get("model_ready"):
            self.ml_mode_buttons["active"].setEnabled(True)
        self.deck_bar.set_split(wins, losses, max(labeled - wins - losses, 0))
        decisive = wins + losses
        deck_text = (
            f"{labeled:,} lessons · {decisive:,} decisive ({wins:,} wins, {losses:,} "
            f"losses) · {max(labeled - decisive, 0):,} flat days that teach patience."
        )
        # v2 school progress vs the retrain guards (10 days / 1,000 / 300)
        school_days = int(stats.get("school_days", 0))
        school_rows = int(stats.get("school_rows", 0))
        if school_rows:
            deck_text += (
                f"\nv2 night school: {school_days}/10 days · {school_rows:,}/1,000"
                f" snapshot lessons · {int(stats.get('school_wins', 0)):,}/300 wins —"
                f" the first nightly model trains when all three fill."
            )
        self.deck_label.setText(deck_text)
        # LIVE shadow head-to-head → ladder note + report card (the sub-tabs
        # tell the live story, not the training-day story — 2026-09-02)
        self._render_live_story(stats)
        self.precision_chart.set_series(stats.get("precision_series") or [])

    @staticmethod
    def _wr_text(wins: int, n: int) -> str:
        return f"{wins}/{n} ({wins / n * 100:.0f}%)" if n else "0 resolved yet"

    def _render_live_story(self, stats: dict) -> None:
        """The live-shadow lines under the ladder + the report card truth."""
        lines = []
        trades = stats.get("shadow_trades") or {}
        if trades.get("n"):
            lines.append(
                "LIVE (real entries): Brain-approved trades won "
                f"{self._wr_text(trades.get('approved_wins', 0), trades.get('approved_n', 0))}"
                f" vs {self._wr_text(trades.get('wins', 0), trades.get('n', 0))} for"
                " everything Wave took."
            )
        menu = stats.get("shadow_menu") or {}
        for model in sorted(menu):
            m = menu[model]
            if m.get("n"):
                lines.append(
                    f"LIVE (menu judgments, {model}): approvals won "
                    f"{self._wr_text(m.get('approved_wins', 0), m.get('approved_n', 0))}"
                    f" of {m.get('n', 0):,} resolved."
                )
        if lines:
            base = self.ladder_note.text().split("\nLIVE")[0]
            self.ladder_note.setText(base + "\n" + "\n".join(lines))
        # report card: the current truth, not the training-day story
        v1_line = "Brain v1 — the frozen champion, judging LIVE in shadow."
        if trades.get("n") or any(m.get("n") for m in menu.values()):
            v1_line += " Its live precision line is filling nightly (above)."
        school_days = int(stats.get("school_days", 0))
        if stats.get("brain2_loaded"):
            v2_line = (
                "Brain v2 — TRAINED (nightly live-features challenger); battling"
                " v1 in shadow on the same stream. Promotion only via §11."
            )
        elif school_days:
            v2_line = (
                f"Brain v2 — in night school ({school_days}/10 days of snapshot"
                " lessons). It trains itself the night the guards fill, then"
                " challenges v1 in shadow."
            )
        else:
            v2_line = "Brain v2 — waiting for its first snapshot lessons (tonight)."
        self.models_label.setText(v1_line + "\n" + v2_line)

    # -- the ladder -----------------------------------------------------------

    def set_curve(self, rows: list[dict]) -> None:
        while self._ladder_rows_layout.count():
            item = self._ladder_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for row in rows:
            self._ladder_rows_layout.addWidget(_LadderRow(row))

    def _load_curve_file(self) -> None:
        from waveapp.config import support_dir

        path = Path(support_dir()) / "research" / "judge_v1_oof.csv"
        if not path.exists():
            self.ladder_note.setText("No trained Brain yet — the ladder appears after Stage 0.")
            self.models_label.setText("No models yet. Stage 0 (school) produces Brain v1.")
            return
        try:
            with open(path, newline="") as fh:
                rows = [(float(r["p_win"]), int(r["label"])) for r in csv.DictReader(fh)]
        except Exception:
            self.ladder_note.setText("Curve file unreadable.")
            return
        total = len(rows)
        curve = []
        for step in range(50, 90, 5):  # EVERY rung, 50%→85% (full clarity)
            threshold = step / 100.0
            taken = [(p, y) for p, y in rows if p >= threshold]
            if len(taken) < 30:  # under 30 trades a rung is statistics-free
                curve.append({"threshold": threshold, "empty": True})
                continue
            wins = sum(y for _p, y in taken)
            curve.append(
                {
                    "threshold": threshold,
                    "trades": len(taken),
                    "coverage": len(taken) / total * 100,
                    "win_rate": wins / len(taken) * 100,
                    "wilson": _wilson_lower(wins, len(taken)) * 100,
                }
            )
        self.set_curve(curve)
        base = sum(y for _p, y in rows) / max(total, 1) * 100
        self.ladder_note.setText(
            f"Brain v1 · judged on {total:,} trades it never saw in training · base win "
            f"rate without the Brain: {base:.1f}%. The floors tighten as the 3-year "
            f"class multiplies the lessons — a milestone counts only when its floor "
            f"clears it."
        )
        self.models_label.setText(
            f"Brain v1 — trained on the champion year ({total:,} trades), "
            f"10 student-copies averaged, honest exam (purged folds + embargo).\n"
            f"Status: school passed → next stop SHADOW. Every retrain re-takes the "
            f"exam as a challenger; promotion only through the §11 bar."
        )


class _SplitBar(QWidget):
    """Win / loss / flat split of the lesson deck, painted as one bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(18)
        self._parts = (0, 0, 0)

    def set_split(self, wins: int, losses: int, flat: int) -> None:
        self._parts = (wins, losses, flat)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        wins, losses, flat = self._parts
        total = max(wins + losses + flat, 1)
        w, h = self.width(), self.height()
        x = 0.0
        painter.setPen(Qt.PenStyle.NoPen)
        for count, color in (
            (wins, QColor(theme.GREEN)),
            (losses, QColor(theme.RED)),
            (flat, QColor(0, 0, 0, 24)),
        ):
            part = w * count / total
            painter.setBrush(color)
            painter.drawRoundedRect(int(x), 0, max(int(part), 2 if count else 0), h, 5, 5)
            x += part
