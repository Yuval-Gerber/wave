"""Animated one-line wave logo (SPEC.md §5, top bar).

The line is the reference wave (traced from video — see
`wave_shape.py`). The signature animation is the video's: the wave DRAWS
ITSELF from the right tip — tail, curl, crest, long left tail — holds
complete, then erases itself the same way, and loops. States:

    CALM         slow self-drawing loop        (idle / connected)
    ENERGETIC    fast self-drawing loop        (trades in progress)
    PAUSED       full wave flattens to a pulsing line
    ERROR        full wave, red and jagged
    DISCONNECTED full wave, grey and dotted, still

State changes blend smoothly; entering a static state completes the line.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import QEasingCurve, QPointF, Qt, QVariantAnimation
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from waveapp.ui import theme
from waveapp.ui.wave_shape import wave_points

# Self-draw loop timeline (fractions of one phase cycle), like the video:
# draw on → hold complete → erase (same direction) → brief rest.
_DRAW_END = 0.42
_HOLD_END = 0.62
_ERASE_END = 0.95


def loop_segment(phase: float) -> tuple[float, float]:
    """Visible [start, end] slice of the stroke for a loop phase in [0, 1)."""
    if phase < _DRAW_END:
        return 0.0, phase / _DRAW_END
    if phase < _HOLD_END:
        return 0.0, 1.0
    if phase < _ERASE_END:
        return (phase - _HOLD_END) / (_ERASE_END - _HOLD_END), 1.0
    return 1.0, 1.0  # rest: empty


class LogoState(Enum):
    CALM = "calm"
    ENERGETIC = "energetic"
    PAUSED = "paused"
    ERROR = "error"
    DISCONNECTED = "disconnected"


@dataclass
class _Params:
    """Everything that defines the wave's look at one instant."""

    loop: float  # 1 = self-draw/erase loop runs, 0 = full line always shown
    speed: float  # phase cycles per second
    color: QColor
    flatten: float  # 1 = full silhouette, 0 = flat line
    jagged: float  # 0 = smooth, 1 = hard zigzag
    dotted: float  # 0 = solid stroke, 1 = dotted
    pulse: float  # 0 = steady opacity, 1 = deep opacity pulse

    def lerp(self, other: _Params, t: float) -> _Params:
        def n(a: float, b: float) -> float:
            return a + (b - a) * t

        c1, c2 = self.color, other.color
        color = QColor(
            round(n(c1.red(), c2.red())),
            round(n(c1.green(), c2.green())),
            round(n(c1.blue(), c2.blue())),
        )
        return _Params(
            loop=n(self.loop, other.loop),
            speed=n(self.speed, other.speed),
            color=color,
            flatten=n(self.flatten, other.flatten),
            jagged=n(self.jagged, other.jagged),
            dotted=n(self.dotted, other.dotted),
            pulse=n(self.pulse, other.pulse),
        )


_STATE_PARAMS: dict[LogoState, _Params] = {
    # ~12s per cycle like the reference video
    LogoState.CALM: _Params(1.0, 0.085, QColor(theme.BLUE), 1.0, 0.0, 0.0, 0.0),
    LogoState.ENERGETIC: _Params(1.0, 0.38, QColor(theme.BLUE), 1.0, 0.0, 0.0, 0.0),
    LogoState.PAUSED: _Params(0.0, 0.15, QColor(theme.BLUE), 0.03, 0.0, 0.0, 1.0),
    LogoState.ERROR: _Params(0.0, 0.5, QColor(theme.RED), 1.0, 1.0, 0.0, 0.0),
    LogoState.DISCONNECTED: _Params(0.0, 0.05, QColor(theme.GREY), 1.0, 0.0, 1.0, 0.0),
}


class WaveLogo(QWidget):
    """The animated logo. Call `set_state()`; everything else is automatic."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = LogoState.DISCONNECTED
        self._shown = _STATE_PARAMS[self._state]
        self._from = self._shown
        self._phase = 0.0  # 0..1, one full cycle
        self._paint_error_logged = False
        # the traced stroke runs RIGHT→LEFT; the login flips it to draw from
        # the left tip. mirror=True gives that login-style L→R motion
        # (2026-08-21: the scan loader should flow left to right).
        self.mirror = False

        self.setMinimumSize(90, 28)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Master clock: loops forever, advances the phase by elapsed fraction.
        self._clock = QVariantAnimation(self)
        self._clock.setStartValue(0.0)
        self._clock.setEndValue(1.0)
        self._clock.setDuration(1000)  # one tick = 1s of wall time
        self._clock.setLoopCount(-1)
        self._prev_tick = 0.0
        self._clock.valueChanged.connect(self._on_tick)
        self._clock.start()

        # Transition blender: re-targeted on each state change.
        self._blend = QVariantAnimation(self)
        self._blend.setStartValue(0.0)
        self._blend.setEndValue(1.0)
        self._blend.setDuration(450)
        self._blend.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._blend.valueChanged.connect(self._on_blend)

    # -- public API ---------------------------------------------------------

    @property
    def state(self) -> LogoState:
        return self._state

    def set_state(self, state: LogoState) -> None:
        if state == self._state:
            return
        self._state = state
        self._from = self._shown
        self._blend.stop()
        self._blend.start()

    # -- animation plumbing -------------------------------------------------

    def _on_tick(self, value: float) -> None:
        delta = value - self._prev_tick
        if delta < 0:  # loop wrapped
            delta += 1.0
        self._prev_tick = value
        self._phase = (self._phase + delta * self._shown.speed) % 1.0
        self.update()

    def _on_blend(self, t: float) -> None:
        self._shown = self._from.lerp(_STATE_PARAMS[self._state], t)
        self.update()

    def _visible_segment(self) -> tuple[float, float]:
        """Blend the loop's slice toward the full line by (1 - loop weight)."""
        seg_start, seg_end = loop_segment(self._phase)
        loop = self._shown.loop
        return seg_start * loop, seg_end * loop + (1.0 - loop)

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # An exception escaping a Qt virtual override aborts the whole process
        # in a bundled app — never let painting take Wave down.
        try:
            self._paint()
        except Exception:
            if not self._paint_error_logged:
                self._paint_error_logged = True
                logging.getLogger("wave.ui").exception("WaveLogo paint failed")

    def _paint(self) -> None:
        p = self._shown
        start, end = self._visible_segment()
        if self.mirror:
            start, end = 1.0 - end, 1.0 - start  # draw from the LEFT tip
        if end - start < 0.004:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        margin = 4.0
        points = wave_points(
            self.width() - 2 * margin,
            self.height() - 2 * margin,
            start=start,
            end=end,
            flatten=p.flatten,
            jagged=p.jagged,
        )
        if len(points) < 2:
            return

        path = QPainterPath()
        for i, (x, y) in enumerate(points):
            point = QPointF(margin + x, margin + y)
            if i == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)

        color = QColor(p.color)
        if p.pulse > 0:
            # slow opacity breathing for the paused flatline
            breath = 0.55 + 0.45 * math.sin(self._phase * 2 * math.pi)
            color.setAlphaF(max(0.0, min(1.0, 1.0 - p.pulse * (1.0 - breath))))

        pen = QPen(color, 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if p.dotted > 0.5:
            pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.drawPath(path)
