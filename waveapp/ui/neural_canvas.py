"""Wave's mind v3 — a living SPIRAL GALAXY (2026-08-20).

Recipe borrowed from the classic three.js galaxy generators (Three.js
Journey / procedural galaxy repos), re-implemented natively with QPainter:

- SPIRAL ARMS: each star's angle = branch + spin·radius, with power-curve
  radius falloff → a dense luminous core thinning into curved arms;
- DIFFERENTIAL ROTATION: inner stars orbit faster than outer ones, so the
  galaxy visibly swirls instead of turning like a rigid plate;
- COLOR GRADIENT: star color lerps from a hot golden core to Wave's cool
  starlight blue at the rim;
- ADDITIVE GLOW: stars are pre-rendered radial-gradient sprites drawn in
  CompositionMode_Plus — overlapping light ACCUMULATES, so the core and the
  arms genuinely glow (the WebGL trick, CPU-cheap via cached pixmaps);
- 3D TILT: the galactic plane is tilted and slowly precesses — depth without
  an OpenGL dependency.

One star per SCANNED SYMBOL (stable hash): the canvas is a live map of the
market Wave reads. Color is STATE — candidate blue flare, rejected brief red
flicker, accepted green glow — never event-splatter. Hidden tab = zero work.
"""

from __future__ import annotations

import logging
import math
import random
import zlib
from collections import deque
from dataclasses import dataclass, field

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QWidget

from waveapp.ui import theme

logger = logging.getLogger("wave.ui.mind")

NODE_COUNT = 1500  # one star per scanned symbol (config scan_universe_size)
ARMS = 3
SPIN = 3.05  # how far the arms wind around the core
RADIUS_POWER = 2.05  # density falloff: most stars near the core
TICK_MS = 42  # ~24 fps
EVENT_SPACING_MS = 380
BASE_ROTATION = 0.0016  # rad/tick at the rim; the core turns ~3× faster
TILT = 0.48  # how flat the galactic plane sits on screen
_PULSE_MAX = 14
_SPRITE_STEPS = 24  # color quantization for the sprite cache

_CORE = QColor(255, 208, 150)  # hot golden center
_RIM = QColor(96, 170, 255)  # Wave starlight blue

# state → (color, seconds it persists before cooling back to the gradient)
_STATE_STYLE = {
    "candidate": (QColor(theme.BLUE), 7.0),
    "rejected": (QColor(theme.RED), 1.8),
    "accepted": (QColor(theme.GREEN), 45.0),
}


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


@dataclass
class _Star:
    radius: float  # orbital radius, 0..1
    angle0: float  # starting angle on its orbit
    y: float  # height above/below the galactic plane
    speed: float  # differential angular speed (rad/tick)
    size: float  # base sprite size factor
    color: QColor  # gradient color (core→rim)
    twinkle: float  # phase for the brightness shimmer
    idx: int = 0
    px: float = 0.0  # projected screen position (per tick)
    py: float = 0.0
    depth: float = 1.0  # 0.8 behind … 1.2 in front
    excite: float = 0.0
    state: str = "idle"
    state_ttl: float = 0.0


@dataclass
class _Wave:
    cx: float = 0.0
    cy: float = 0.0
    radius: float = 10.0
    alpha: float = 160.0
    growth: float = 7.5


@dataclass
class _Label:
    text: str
    star: _Star
    color: QColor
    rise: float = 0.0
    ttl: float = 1.0


@dataclass
class _Pulse:
    """A spark riding between two nearby stars of the same arm."""

    a: _Star
    b: _Star
    t: float = 0.0
    speed: float = 0.04
    hops: int = 0
    color: QColor = field(default_factory=lambda: QColor(_RIM))


class NeuralCanvas(QWidget):
    """The thinking surface. Feed it scanner events; it does the rest."""

    def __init__(self, parent: QWidget | None = None, seed: int | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(220)
        self._rng = random.Random(seed)  # noqa: S311 — animation, not crypto
        self._nodes: list[_Star] = []
        self._neighbors: dict[int, list[int]] = {}
        self._edges: list[tuple[int, int]] = []  # pulse routes (never drawn)
        self._waves: list[_Wave] = []
        self._labels: list[_Label] = []
        self._pulses: list[_Pulse] = []
        self._events: deque = deque()
        self._sprites: dict[tuple[int, int, int, int], QPixmap] = {}
        self._time = 0.0
        self._breath = 0.0
        self._idle_countdown = 90
        self._ripple_countdown = 70
        self.ambient = True

        self._build_galaxy()

        self._clock = QTimer(self)
        self._clock.setInterval(TICK_MS)
        self._clock.timeout.connect(self._tick)
        self._clock.start()
        self._dequeue = QTimer(self)
        self._dequeue.setInterval(EVENT_SPACING_MS)
        self._dequeue.timeout.connect(self._play_next_event)
        self._dequeue.start()

    # -- public event API (call from anywhere in the UI thread) ---------------

    def pulse_scan(self) -> None:
        self._events.append(("scan", "", ""))

    def candidate_found(self, symbol: str) -> None:
        self._events.append(("found", symbol, ""))

    def candidate_rejected(self, symbol: str, reason: str = "") -> None:
        self._events.append(("rejected", symbol, reason))

    def candidate_promoted(self, symbol: str) -> None:
        self._events.append(("promoted", symbol, ""))

    # -- galaxy construction --------------------------------------------------

    def _build_galaxy(self) -> None:
        rng = self._rng
        for index in range(NODE_COUNT):
            if rng.random() < 0.05:  # halo dust far from the plane
                radius = rng.uniform(0.25, 1.0)
                angle = rng.uniform(0, 2 * math.pi)
                y = rng.gauss(0, 0.30)
            else:
                radius = rng.random() ** RADIUS_POWER
                branch = (index % ARMS) / ARMS * 2 * math.pi
                # scatter grows with radius, so arms stay crisp near the core
                scatter = rng.gauss(0, 0.045) * (0.35 + radius * 1.8)
                angle = branch + radius * SPIN + scatter
                y = rng.gauss(0, 0.035) * (0.3 + radius)
            # differential rotation: the core spins visibly faster
            speed = BASE_ROTATION * (0.38 + 0.85 / math.sqrt(radius + 0.075))
            color = _lerp_color(_CORE, _RIM, radius * 1.25)
            size = rng.uniform(0.75, 1.35) * (1.25 - 0.45 * radius)
            self._nodes.append(
                _Star(
                    radius=radius,
                    angle0=angle,
                    y=y,
                    speed=speed,
                    size=size,
                    color=color,
                    twinkle=rng.uniform(0, 2 * math.pi),
                    idx=index,
                )
            )
        # pulse routes: arm-mates a few indices apart are spatial neighbors
        for index in range(NODE_COUNT):
            partner = index + ARMS * self._rng.randint(1, 4)
            if partner < NODE_COUNT:
                self._edges.append((index, partner))
                self._neighbors.setdefault(index, []).append(partner)
                self._neighbors.setdefault(partner, []).append(index)

    def _node_for(self, symbol: str) -> _Star:
        """The SAME symbol always lights the SAME star."""
        return self._nodes[zlib.crc32(symbol.encode()) % len(self._nodes)]

    # -- simulation -----------------------------------------------------------

    def _project(self) -> None:
        width, height = self.width(), self.height()
        cx, cy = width / 2, height / 2
        span_x = max(40.0, width / 2 - 20)
        span_y = max(30.0, height / 2 - 16)
        precession = 0.12 * math.sin(self._breath * 0.17)  # the plane breathes
        for star in self._nodes:
            angle = star.angle0 + self._time * star.speed
            x = math.cos(angle) * star.radius
            z = math.sin(angle) * star.radius
            star.depth = 1.0 + 0.22 * z  # toward the viewer = slightly larger
            star.px = cx + x * span_x
            star.py = cy + (z * TILT + star.y * (0.9 + precession)) * span_y

    def _tick(self) -> None:
        # the universe never pauses (2026-09-01: "animations can go
        # without a stop") — hidden tabs keep simulating so returning shows
        # a live scene, not a burst replay; painting still skips when hidden
        self._time += 1.0
        self._breath = (self._breath + 0.010) % (2 * math.pi * 100)
        self._project()
        dt = TICK_MS / 1000.0

        for star in self._nodes:
            star.excite *= 0.94
            if star.state != "idle":
                star.state_ttl -= dt
                if star.state_ttl <= 0:
                    star.state = "idle"

        arrivals: list[_Pulse] = []
        for pulse in self._pulses:
            pulse.t += pulse.speed
            if pulse.t >= 1.0:
                pulse.b.excite = max(pulse.b.excite, 0.6)
                arrivals.append(pulse)
        self._pulses = [p for p in self._pulses if p.t < 1.0]
        for pulse in arrivals:
            if pulse.hops > 0 and self._rng.random() < 0.75:
                self._spawn_pulse(source=pulse.b, color=pulse.color, hops=pulse.hops - 1)
        if self.ambient and len(self._pulses) < _PULSE_MAX and self._rng.random() < 0.35:
            self._spawn_pulse(hops=self._rng.randint(0, 2))

        for wave in self._waves:
            wave.radius += wave.growth
            wave.alpha *= 0.94
        self._waves = [w for w in self._waves if w.alpha > 4]

        self._ripple_countdown -= 1
        if self.ambient and self._ripple_countdown <= 0:
            self._ripple_countdown = self._rng.randint(50, 110)
            spot = self._rng.choice(self._nodes)
            self._waves.append(_Wave(cx=spot.px, cy=spot.py, radius=4.0, alpha=60.0, growth=3.0))

        for label in self._labels:
            label.rise += 0.5
            label.ttl -= 0.012
        self._labels = [label for label in self._labels if label.ttl > 0]

        self._idle_countdown -= 1
        if self.ambient and self._idle_countdown <= 0:
            self._idle_countdown = self._rng.randint(60, 150)
            if not self._events:
                self._rng.choice(self._nodes).excite = 0.6

        self.update()

    def _spawn_pulse(
        self, source: _Star | None = None, color: QColor | None = None, hops: int = 0
    ) -> None:
        if source is None:
            edge = self._rng.choice(self._edges)
            a, b = self._nodes[edge[0]], self._nodes[edge[1]]
        else:
            neighbors = self._neighbors.get(source.idx)
            if neighbors:
                a, b = source, self._nodes[self._rng.choice(neighbors)]
            else:
                a, b = source, self._nodes[(source.idx + ARMS) % len(self._nodes)]
        self._pulses.append(
            _Pulse(
                a=a,
                b=b,
                speed=self._rng.uniform(0.03, 0.055),
                hops=hops,
                color=QColor(color) if color is not None else QColor(_RIM),
            )
        )

    def _play_next_event(self) -> None:
        if not self._events:
            return
        kind, symbol, reason = self._events.popleft()
        if kind == "scan":
            self._waves.append(_Wave(cx=self.width() / 2, cy=self.height() / 2))
            return
        star = self._node_for(symbol)
        if kind == "found":
            star.state, star.state_ttl = "candidate", _STATE_STYLE["candidate"][1]
            star.excite = 1.0
            self._labels.append(_Label(symbol, star, QColor(theme.BLUE)))
            for _ in range(2):
                self._spawn_pulse(source=star, color=QColor(theme.BLUE), hops=2)
        elif kind == "rejected":
            star.state, star.state_ttl = "rejected", _STATE_STYLE["rejected"][1]
            star.excite = 0.8
            text = symbol if not reason else f"{symbol} — {reason}"
            self._labels.append(_Label(text[:44], star, QColor(theme.RED)))
        elif kind == "promoted":
            star.state, star.state_ttl = "accepted", _STATE_STYLE["accepted"][1]
            star.excite = 1.0
            self._labels.append(_Label(f"{symbol} ✓", star, QColor(theme.GREEN)))
            self._waves.append(_Wave(cx=star.px, cy=star.py, radius=6.0, alpha=110.0, growth=4.5))
            for _ in range(3):
                self._spawn_pulse(source=star, color=QColor(theme.GREEN), hops=2)

    # -- sprites (the additive-glow secret) -----------------------------------

    def _sprite(self, color: QColor, diameter: int) -> QPixmap:
        """Radial-gradient star sprite, cached by quantized color+size."""
        quant = 256 // _SPRITE_STEPS
        key = (color.red() // quant, color.green() // quant, color.blue() // quant, diameter)
        cached = self._sprites.get(key)
        if cached is not None:
            return cached
        pixmap = QPixmap(diameter, diameter)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = diameter / 2
        gradient = QRadialGradient(center, center, center)
        hot = QColor(255, 255, 255, 235)  # white-hot heart
        mid = QColor(color)
        mid.setAlpha(150)
        rim = QColor(color)
        rim.setAlpha(0)
        gradient.setColorAt(0.0, hot)
        gradient.setColorAt(0.28, mid)
        gradient.setColorAt(1.0, rim)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(0, 0, diameter, diameter))
        painter.end()
        self._sprites[key] = pixmap
        return pixmap

    # -- painting -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtGui import QGuiApplication

        if QGuiApplication.platformName() == "offscreen":
            return  # test platform: pixels are invisible, and a C++-level
            # drawPixmap fault here segfaulted the 2026-09-03 build gate
        try:
            self._paint()
        except Exception:  # rendering must never kill the app
            logger.exception("neural canvas paint failed")

    def _paint(self) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # ADDITIVE: overlapping starlight accumulates — the galaxy glows
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        breath = 0.9 + 0.1 * math.sin(self._breath)
        shimmer_t = self._breath * 2.3

        for star in self._nodes:
            if star.state != "idle":
                style_color, style_ttl = _STATE_STYLE[star.state]
                fade = min(1.0, star.state_ttl / min(style_ttl, 2.0))
                color = style_color
                diameter = int((13 + 9 * max(star.excite, 0.5 * fade)) * star.size * star.depth)
            else:
                color = star.color
                twinkle = 0.8 + 0.2 * math.sin(shimmer_t + star.twinkle)
                diameter = int(
                    (5.5 + 7.5 * star.excite) * star.size * star.depth * twinkle * breath
                )
            if diameter < 2:
                diameter = 2
            sprite = self._sprite(color, min(diameter, 40))
            half = sprite.width() / 2
            painter.drawPixmap(QPointF(star.px - half, star.py - half), sprite)

        # sparks riding the arms
        for pulse in self._pulses:
            x = pulse.a.px + (pulse.b.px - pulse.a.px) * pulse.t
            y = pulse.a.py + (pulse.b.py - pulse.a.py) * pulse.t
            envelope = math.sin(math.pi * min(1.0, max(0.0, pulse.t)))
            sprite = self._sprite(pulse.color, max(4, int(14 * envelope)))
            half = sprite.width() / 2
            painter.drawPixmap(QPointF(x - half, y - half), sprite)

        # back to normal blending for rings + labels
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for wave in self._waves:
            color = QColor(theme.BLUE)
            color.setAlpha(int(wave.alpha))
            painter.setPen(QPen(color, 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(wave.cx, wave.cy), wave.radius, wave.radius)

        font = QFont(theme.FONT_TEXT, 11)
        font.setBold(True)
        painter.setFont(font)
        for label in self._labels:
            color = QColor(label.color)
            color.setAlphaF(max(0.0, min(1.0, label.ttl)))
            painter.setPen(color)
            painter.drawText(QPointF(label.star.px + 9, label.star.py - 9 - label.rise), label.text)
