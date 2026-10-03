"""LoginWindow (Phase 8.2, round 3) — design:

- frameless liquid-glass card, slightly darkened, rounded glass corners,
  monochrome traffic lights (close/minimize/zoom→responsive resize);
- the wave draws itself L→R; below it "Wave" writes itself — TRACED from
  the own handwriting reference image. Both start and end together, and
  both erase FIFO (what appeared first disappears first);
- two large circle buttons pulled toward the center: user (opens the password
  bar) and Touch ID (real fingerprint icon, Tabler Icons, MIT);
- the password bar grows out of the user button: user icon stays as the left
  end (its circle's left half preserved), a small check-circle at the right
  end submits, the Touch ID button hides while open, clicking the card
  background tucks the bar back in;
- wrong password / failed Touch ID: the light BEHIND the wave + word flashes
  dim red twice (a lamp, not a message) + shake. No lockout — the round-5
  decision: "if someone doesn't know the password it will not know it";
- flashlight hover glow on wave/word/buttons.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSequentialAnimationGroup,
    Qt,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from waveapp.security import auth
from waveapp.ui import theme
from waveapp.ui.wave_logo import loop_segment
from waveapp.ui.wave_shape import wave_points

logger = logging.getLogger("wave.ui.login")

INK = QColor(42, 42, 48, 248)  # round 8: darker + more opaque so the drawings/buttons pop
HERO_INK = QColor(24, 24, 28, 255)  # round 9: the wave + word pop on their own, sans flashlight
GLOW = QColor(255, 255, 255, 235)
FLASH_RED = QColor(255, 59, 48)

COMPACT_SIZE = (360, 450)
LARGE_SIZE = (520, 620)
CORNER_RADIUS = 24.0

_WORD_FILE = Path(__file__).with_name("word_stroke.json")

# Tabler Icons (MIT) — monochrome stroke icons, recolored to INK at render.
_FINGERPRINT_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
 fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round"
 stroke-linejoin="round">
<path d="M18.9 7a8 8 0 0 1 1.1 5v1a6 6 0 0 0 .8 3"/>
<path d="M8 11a4 4 0 0 1 8 0v1a10 10 0 0 0 2 6"/>
<path d="M12 11v2a14 14 0 0 0 2.5 8"/>
<path d="M8 15a18 18 0 0 0 1.8 6"/>
<path d="M4.9 19a22 22 0 0 1 -.9 -7v-1a8 8 0 0 1 12 -6.95"/>
</svg>"""

_USER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
 fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round"
 stroke-linejoin="round">
<path d="M8 7a4 4 0 1 0 8 0a4 4 0 0 0 -8 0"/>
<path d="M6 21v-2a4 4 0 0 1 4 -4h4a4 4 0 0 1 4 4v2"/>
</svg>"""

_CHECK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
 fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round"
 stroke-linejoin="round"><path d="M5 12l5 5l10 -10"/></svg>"""


def _ink_hex() -> str:
    return f"rgb({INK.red()},{INK.green()},{INK.blue()})"


@lru_cache(maxsize=8)
def _svg_renderer(template: str) -> QSvgRenderer:
    return QSvgRenderer(bytes(template.format(color=_ink_hex()), "utf-8"))


@lru_cache(maxsize=1)
def word_segments() -> tuple[tuple[tuple[float, float], ...], ...]:
    """the handwriting, traced: subpaths of normalized points."""
    data = json.loads(_WORD_FILE.read_text())
    return tuple(tuple((p[0], p[1]) for p in seg) for seg in data["segments"])


@lru_cache(maxsize=1)
def word_aspect() -> float:
    return float(json.loads(_WORD_FILE.read_text())["aspect"])


@lru_cache(maxsize=1)
def _word_flat() -> tuple[tuple[float, float, bool], ...]:
    """(x, y, starts_new_subpath) flattened in writing order."""
    flat: list[tuple[float, float, bool]] = []
    for segment in word_segments():
        for i, (x, y) in enumerate(segment):
            flat.append((x, y, i == 0))
    return tuple(flat)


def _flashlight_pen(cursor: QPointF, radius: float, width: float) -> QPen:
    gradient = QRadialGradient(cursor, radius)
    gradient.setColorAt(0.0, GLOW)
    gradient.setColorAt(0.55, QColor(255, 255, 255, 90))
    gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
    pen = QPen()
    pen.setBrush(gradient)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


class HeroCanvas(QWidget):
    """Wave (L→R) + handwritten 'Wave', starting and ending TOGETHER, both
    erasing FIFO; flashlight hover."""

    CYCLE_SECONDS = 6.0

    def __init__(self, parent: QWidget | None = None, show_word: bool = True) -> None:
        super().__init__(parent)
        self._show_word = show_word  # False: wave only (e.g. market-closed card)
        self.setMinimumHeight(190 if show_word else 90)
        self.setMouseTracking(True)
        self._phase = 0.0
        self._cursor: QPointF | None = None
        self._flash_alpha = 0  # the red backlight behind the wave + word

        self._clock = QVariantAnimation(self)
        self._clock.setStartValue(0.0)
        self._clock.setEndValue(1.0)
        self._clock.setDuration(int(self.CYCLE_SECONDS * 1000))
        self._clock.setLoopCount(-1)
        self._clock.valueChanged.connect(self._tick)
        self._clock.start()

    def _tick(self, value: float) -> None:
        self._phase = value
        self.update()

    def set_flash_alpha(self, value) -> None:
        self._flash_alpha = int(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        try:
            self._paint()
        except Exception:
            logger.exception("hero paint failed")

    def _wave_path(self, seg_start: float, seg_end: float) -> QPainterPath | None:
        start, end = 1.0 - seg_end, 1.0 - seg_start  # draw from the LEFT tip
        wave_w = self.width() * 0.72
        wave_h = self.height() * (0.32 if self._show_word else 0.62)
        offset_x = (self.width() - wave_w) / 2
        offset_y = 10.0 if self._show_word else (self.height() - wave_h) / 2
        points = wave_points(wave_w, wave_h, start=start, end=end)
        if len(points) < 2:
            return None
        path = QPainterPath()
        for i, (x, y) in enumerate(points):
            point = QPointF(offset_x + x, offset_y + y)
            path.moveTo(point) if i == 0 else path.lineTo(point)
        return path

    def _word_path(self, seg_start: float, seg_end: float) -> QPainterPath | None:
        """Same (start, end) fractions as the wave → identical timing; the
        visible window slides FIFO: first written, first erased."""
        flat = _word_flat()
        total = len(flat)
        first = int(total * seg_start)
        last = int(total * seg_end)
        if last - first < 2:
            return None
        word_w = self.width() * 0.62
        word_h = word_w / word_aspect()
        offset_x = (self.width() - word_w) / 2
        offset_y = self.height() * 0.50
        path = QPainterPath()
        pen_down = False
        for index in range(first, last):
            x, y, starts_subpath = flat[index]
            point = QPointF(offset_x + x * word_w, offset_y + y * word_h)
            if starts_subpath or not pen_down or index == first:
                path.moveTo(point)
                pen_down = True
            else:
                path.lineTo(point)
        return path

    def _paint(self) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        seg_start, seg_end = loop_segment(self._phase)

        if self._flash_alpha > 0:
            # auth failure: a dim red lamp glows BEHIND the wave and the word
            gradient = QRadialGradient(
                QPointF(self.width() / 2, self.height() / 2), self.width() * 0.42
            )
            core = QColor(FLASH_RED)
            core.setAlpha(self._flash_alpha)
            gradient.setColorAt(0.0, core)
            gradient.setColorAt(1.0, QColor(255, 59, 48, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRect(self.rect())

        wave_path = self._wave_path(seg_start, seg_end)
        word_path = self._word_path(seg_start, seg_end) if self._show_word else None

        wave_pen = QPen(HERO_INK, 3.6)
        wave_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        wave_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        word_pen = QPen(HERO_INK, 3.8)
        word_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        word_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        if wave_path is not None:
            painter.setPen(wave_pen)
            painter.drawPath(wave_path)
        if word_path is not None:
            painter.setPen(word_pen)
            painter.drawPath(word_path)

        if self._cursor is not None:
            if wave_path is not None:
                painter.setPen(_flashlight_pen(self._cursor, 90.0, 3.6))
                painter.drawPath(wave_path)
            if word_path is not None:
                painter.setPen(_flashlight_pen(self._cursor, 90.0, 3.8))
                painter.drawPath(word_path)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._cursor = event.position()
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._cursor = None
        self.update()


class TrafficLight(QWidget):
    clicked = pyqtSignal()
    SIZE = 16

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
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
        ink = QColor(INK)
        if self._hover:
            ink.setAlpha(255)
        painter.setPen(QPen(ink, 1.5))
        painter.setBrush(QColor(255, 255, 255, 85 if self._hover else 50))
        painter.drawEllipse(rect)
        center = rect.center()
        glyph = QPen(ink, 1.5)
        glyph.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(glyph)
        r = 3.2
        if self.kind == "close":
            painter.drawLine(
                QPointF(center.x() - r, center.y() - r), QPointF(center.x() + r, center.y() + r)
            )
            painter.drawLine(
                QPointF(center.x() - r, center.y() + r), QPointF(center.x() + r, center.y() - r)
            )
        elif self.kind == "minimize":
            painter.drawLine(
                QPointF(center.x() - r, center.y()), QPointF(center.x() + r, center.y())
            )
        else:
            painter.drawLine(
                QPointF(center.x() - r, center.y() + r),
                QPointF(center.x() - r * 0.1, center.y() + r * 0.1),
            )
            painter.drawLine(
                QPointF(center.x() + r, center.y() - r),
                QPointF(center.x() + r * 0.1, center.y() - r * 0.1),
            )
            painter.drawLine(
                QPointF(center.x() - r, center.y() + r),
                QPointF(center.x() - r, center.y() + r * 0.25),
            )
            painter.drawLine(
                QPointF(center.x() - r, center.y() + r),
                QPointF(center.x() - r * 0.25, center.y() + r),
            )
            painter.drawLine(
                QPointF(center.x() + r, center.y() - r),
                QPointF(center.x() + r, center.y() - r * 0.25),
            )
            painter.drawLine(
                QPointF(center.x() + r, center.y() - r),
                QPointF(center.x() + r * 0.25, center.y() - r),
            )


class CircleButton(QWidget):
    """Large circle with an SVG icon (Tabler, monochrome) and hover glow."""

    clicked = pyqtSignal()
    DIAMETER = 88

    def __init__(self, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = icon  # "fingerprint" | "user"
        self.setFixedSize(self.DIAMETER, self.DIAMETER)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setColor(QColor(255, 255, 255, 220))
        self._glow.setOffset(0, 0)
        self._glow.setBlurRadius(0)
        self.setGraphicsEffect(self._glow)
        self._glow_anim = QPropertyAnimation(self._glow, b"blurRadius", self)
        self._glow_anim.setDuration(160)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._glow_anim.stop()
        self._glow_anim.setStartValue(self._glow.blurRadius())
        self._glow_anim.setEndValue(32.0)
        self._glow_anim.start()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._glow_anim.stop()
        self._glow_anim.setStartValue(self._glow.blurRadius())
        self._glow_anim.setEndValue(0.0)
        self._glow_anim.start()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        painter.setPen(QPen(INK, 2.2))
        painter.setBrush(QColor(255, 255, 255, 56))
        painter.drawEllipse(rect)
        template = _FINGERPRINT_SVG if self.icon == "fingerprint" else _USER_SVG
        icon_size = self.width() * 0.46
        icon_rect = QRectF(
            (self.width() - icon_size) / 2, (self.height() - icon_size) / 2, icon_size, icon_size
        )
        _svg_renderer(template).render(painter, icon_rect)


class PasswordBar(QWidget):
    """Round 5: the user button's circle STAYS in place — its left half is
    preserved as the bar's left cap; the right side stretches into the bar.
    A small check-circle submits, with a tight dim-green flashlight glow
    on hover. No divider (no need)."""

    submitted = pyqtSignal(str, str)
    collapsed = pyqtSignal()

    def __init__(self, first_run: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.first_run = first_run
        self.bar_height = CircleButton.DIAMETER
        self._check_hover = False
        self.setMouseTracking(True)
        self.setFixedHeight(self.bar_height if not first_run else self.bar_height + 30)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        fields = QVBoxLayout(self)
        left_pad = self.bar_height + 8  # clear the user-icon circle
        right_pad = 48
        fields.setContentsMargins(left_pad, 10, right_pad, 10)
        fields.setSpacing(0)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("choose a password (min 8)" if first_run else "password")
        self.password.setStyleSheet("border: none; background: transparent; font-size: 14px;")
        self.password.returnPressed.connect(self._submit)
        fields.addWidget(self.password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm.setPlaceholderText("confirm password")
        self.confirm.setStyleSheet("border: none; background: transparent; font-size: 14px;")
        self.confirm.returnPressed.connect(self._submit)
        self.confirm.setVisible(first_run)
        fields.addWidget(self.confirm)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.setFixedWidth(int(v)))
        # round 9: the check mark fades out FAST on collapse so it vanishes
        # before the retracting bar carries it into the user icon
        self._check_opacity = 1.0
        self._check_fade = QVariantAnimation(self)
        self._check_fade.setDuration(90)
        self._check_fade.valueChanged.connect(self._set_check_opacity)
        self.setFixedWidth(0)
        self.hide()

    def _set_check_opacity(self, value) -> None:
        self._check_opacity = float(value)
        self.update()

    # -- painting -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.bar_height
        radius = h / 2.0
        width = self.width()
        if width < h * 0.9:
            return

        # stadium whose LEFT CAP is the user button's original circle: its
        # left half is preserved; the right half stretched into the bar.
        body = QPainterPath()
        body.moveTo(radius, 1)
        body.lineTo(width - radius, 1)
        body.arcTo(QRectF(width - h + 1, 1, h - 2, h - 2), 90, -180)
        body.lineTo(radius, h - 1)
        body.arcTo(QRectF(1, 1, h - 2, h - 2), 270, -180)  # the kept left half
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1.4))
        painter.setBrush(QColor(255, 255, 255, 150))
        painter.drawPath(body)

        # the user icon exactly where it always was (circle center)
        icon_size = h * 0.46
        icon_rect = QRectF(radius - icon_size / 2, (h - icon_size) / 2, icon_size, icon_size)
        _svg_renderer(_USER_SVG).render(painter, icon_rect)

        # small check-circle submit; TIGHT dim-green flashlight glow on hover
        # (glow radius stays well inside the bar so nothing else lights up)
        if self._check_opacity <= 0.02:
            self._check_rect = None
            return
        painter.setOpacity(self._check_opacity)
        check_d = h * 0.30
        check_rect = QRectF(width - check_d - 16, (h - check_d) / 2, check_d, check_d)
        if self._check_hover:
            glow_r = check_d * 0.95
            glow = QRadialGradient(check_rect.center(), glow_r)
            glow.setColorAt(0.0, QColor(52, 199, 89, 90))  # dim systemGreen
            glow.setColorAt(1.0, QColor(52, 199, 89, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(check_rect.center(), glow_r, glow_r)
        painter.setPen(QPen(INK, 1.4))
        painter.setBrush(QColor(255, 255, 255, 70))
        painter.drawEllipse(check_rect)
        inner = check_rect.adjusted(
            check_d * 0.26, check_d * 0.26, -check_d * 0.26, -check_d * 0.26
        )
        _svg_renderer(_CHECK_SVG).render(painter, inner)
        self._check_rect = check_rect

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        rect = getattr(self, "_check_rect", None)
        hover = rect is not None and rect.contains(event.position())
        if hover != self._check_hover:
            self._check_hover = hover
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor
            )
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._check_hover:
            self._check_hover = False
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        rect = getattr(self, "_check_rect", None)
        if rect is not None and rect.contains(event.position()):
            self._submit()

    # -- behavior -------------------------------------------------------------

    def _submit(self) -> None:
        self.submitted.emit(self.password.text(), self.confirm.text())

    def expand(self, target_width: int) -> None:
        # the circle is always present: the bar starts AT the circle's width
        # and only its right side stretches out
        self.setFixedWidth(max(self.width(), self.bar_height))
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(target_width)
        self._anim.start()
        self._check_fade.stop()
        self._check_fade.setStartValue(self._check_opacity)
        self._check_fade.setEndValue(1.0)
        self._check_fade.start()
        self.password.setFocus()

    def collapse(self) -> None:
        if not self.isVisible():
            return
        self._check_fade.stop()
        self._check_fade.setStartValue(self._check_opacity)
        self._check_fade.setEndValue(0.0)
        self._check_fade.start()  # gone in 90ms, long before the bar lands
        self._anim.stop()
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(self.bar_height)  # retract INTO the circle
        try:
            self._anim.finished.disconnect()
        except TypeError:
            pass
        self._anim.finished.connect(self._after_collapse)
        self._anim.start()

    def _after_collapse(self) -> None:
        try:
            self._anim.finished.disconnect(self._after_collapse)
        except TypeError:
            pass
        if self.width() <= self.bar_height + 1:
            self.hide()
            self.collapsed.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.collapse()
        else:
            super().keyPressEvent(event)


class LoginWindow(QWidget):
    authenticated = pyqtSignal()
    _touch_id_result = pyqtSignal(bool)

    def __init__(self, password_auth: auth.PasswordAuth) -> None:
        super().__init__()
        self.auth = password_auth
        self.first_run = not password_auth.is_password_set()
        self._drag_offset: QPoint | None = None
        self._zoomed = False
        self._touch_id_result.connect(self._on_touch_id_result)

        self.setWindowTitle("Wave")
        self.setMinimumSize(*COMPACT_SIZE)
        self.resize(*COMPACT_SIZE)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 22)
        root.setSpacing(theme.SPACE_S)

        lights_row = QHBoxLayout()
        lights_row.setSpacing(7)
        self.close_button = TrafficLight("close")
        self.minimize_button = TrafficLight("minimize")
        self.zoom_button = TrafficLight("zoom")
        self.close_button.clicked.connect(self.close)
        self.minimize_button.clicked.connect(self.showMinimized)
        self.zoom_button.clicked.connect(self.toggle_zoom)
        for light in (self.close_button, self.minimize_button, self.zoom_button):
            lights_row.addWidget(light)
        lights_row.addStretch(1)
        root.addLayout(lights_row)

        self.hero = HeroCanvas()
        root.addWidget(self.hero, stretch=1)

        # buttons pulled toward the center
        self.buttons_row = QWidget()
        self.buttons_row.setFixedHeight(CircleButton.DIAMETER + 6)
        row = QHBoxLayout(self.buttons_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.user_button = CircleButton("user")
        self.touch_button = CircleButton("fingerprint")
        row.addStretch(1)
        row.addWidget(self.user_button)
        row.addSpacing(34)
        row.addWidget(self.touch_button)
        row.addStretch(1)
        root.addWidget(self.buttons_row)

        self.bar = PasswordBar(self.first_run, self.buttons_row)
        self.bar.submitted.connect(self._on_password_submitted)
        self.bar.collapsed.connect(self._on_bar_collapsed)

        self.user_button.clicked.connect(self.open_password_bar)
        self.touch_button.clicked.connect(self._start_touch_id)
        touch_available = (
            not self.first_run
            and password_auth.config.touch_id_enabled
            and auth.touch_id_available()
        )
        self.touch_button.setVisible(touch_available or not self.first_run)

        self._apply_glass_when_shown = True

    # -- password bar ---------------------------------------------------------

    def open_password_bar(self) -> None:
        origin_x = self.user_button.geometry().left()
        self.bar.move(origin_x, self.user_button.geometry().top() - (30 if self.first_run else 0))
        self.user_button.hide()
        self.touch_button.hide()
        target = self.buttons_row.width() - origin_x - 16
        self.bar.expand(target)

    def _on_bar_collapsed(self) -> None:
        self.user_button.show()
        if not self.first_run:
            self.touch_button.show()

    # -- window controls ------------------------------------------------------

    def toggle_zoom(self) -> None:
        self._zoomed = not self._zoomed
        target_w, target_h = LARGE_SIZE if self._zoomed else COMPACT_SIZE
        center = self.geometry().center()
        target = QRect(0, 0, target_w, target_h)
        target.moveCenter(center)
        animation = QPropertyAnimation(self, b"geometry", self)
        animation.setDuration(260)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(self.geometry())
        animation.setEndValue(target)
        animation.start()
        self._zoom_anim = animation

    # -- glass ----------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._apply_glass_when_shown:
            return
        self._apply_glass_when_shown = False
        if sys.platform != "darwin" or os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        try:
            import pyqt_liquidglass as glass

            glass.prepare_window_for_glass(self, frameless=True)
            glass.apply_glass_to_window(
                self,
                options=glass.GlassOptions(
                    corner_radius=CORNER_RADIUS,
                    material=glass.GlassMaterial.HUD,
                    blending_mode=glass.BlendingMode.BEHIND_WINDOW,
                ),
            )
            view = getattr(self, "_glass_view", None)
            if view is not None:
                view.setWantsLayer_(True)
                view.layer().setCornerRadius_(CORNER_RADIUS)
                view.layer().setMasksToBounds_(True)
        except Exception:
            logger.exception("login glass failed — translucent fallback")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(QPen(QColor(255, 255, 255, 100), 1))
        painter.setBrush(QColor(44, 44, 52, 48))  # round 8: lighter tint, more glass
        painter.drawRoundedRect(rect, CORNER_RADIUS, CORNER_RADIUS)

    def flash(self) -> None:
        """Auth failure: the light behind the wave + word flashes dim red
        twice, then back to normal."""
        sequence = QSequentialAnimationGroup(self)
        for start, target in ((0, 64), (64, 0), (0, 64), (64, 0)):
            step = QVariantAnimation(sequence)
            step.setDuration(110)
            step.setStartValue(start)
            step.setEndValue(target)
            step.valueChanged.connect(self.hero.set_flash_alpha)
            sequence.addAnimation(step)
        sequence.start()
        self._flash_sequence = sequence

    # -- auth flows -----------------------------------------------------------

    def _on_password_submitted(self, password: str, confirm: str) -> None:
        if self.first_run:
            if password != confirm:
                self._reject()
                return
            try:
                self.auth.set_password(password)
            except ValueError:
                self._reject()
                return
            self.authenticated.emit()
            return
        if self.auth.verify(password):
            self.authenticated.emit()
            return
        self._reject()
        self.bar.password.clear()

    def _reject(self) -> None:
        """Auth failed: the hero backlight flashes dim red twice + shake."""
        self.flash()
        self.shake()

    def shake(self) -> None:
        origin = self.pos()
        group = QSequentialAnimationGroup(self)
        for offset in (14, -11, 7, -4, 0):
            step = QPropertyAnimation(self, b"pos", group)
            step.setDuration(44)
            step.setEndValue(origin + QPoint(offset, 0))
            group.addAnimation(step)
        group.start()
        self._shake_group = group

    # -- Touch ID -------------------------------------------------------------

    def _start_touch_id(self) -> None:
        def worker() -> None:
            ok = auth.authenticate_touch_id("unlock Wave")
            self._touch_id_result.emit(ok)

        threading.Thread(target=worker, daemon=True).start()

    def _on_touch_id_result(self, ok: bool) -> None:
        if ok:
            self.authenticated.emit()
        else:
            self._reject()

    # -- background click: tuck the bar back in; dragging still works ---------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self.bar.isVisible():
            bar_rect = self.bar.geometry().translated(self.buttons_row.pos())
            if not bar_rect.contains(event.position().toPoint()):
                self.bar.collapse()
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
