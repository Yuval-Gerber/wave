"""Left sidebar (SPEC.md §5): icon-only rail (~56px) that expands on hover
(~200px, animated) showing labels, plus connection status dots at the bottom
(Alpaca, Data feed, Telegram, DB) — all grey in Phase 1."""

from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from waveapp.ui import theme
from waveapp.ui.icons import TAB_ICONS

RAIL_WIDTH = 56
EXPANDED_WIDTH = 200
_ICON_BOX = 20.0

# icon composition constants mirrored from scripts/make_icon.py (the
# approved desktop icon: apex triangle + the curl of the traced wave)
_CROP_X_LO, _CROP_X_HI = 0.30, 0.72


class BrandBadge(QWidget):
    """The Wave icon (triangle + wave curl, ocean blue) painted natively —
    identical art to assets/wave.icns, crisp at any size (8.8)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(34, 34)

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtGui import QPainterPath, QPolygonF

        from waveapp.ui.wave_shape import wave_points

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = self.width() / 1024.0
        color = QColor(theme.OCEAN)

        tri_pen = QPen(color, 34.0 * scale * 1.6)  # slightly bolder at tiny sizes
        tri_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        tri_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(tri_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        points = (QPointF(512, 118), QPointF(132, 892), QPointF(892, 892))
        painter.drawPolygon(QPolygonF([QPointF(p.x() * scale, p.y() * scale) for p in points]))

        raw = wave_points(1.0, 1.0)
        idx = [i for i, (x, _) in enumerate(raw) if _CROP_X_LO <= x <= _CROP_X_HI]
        sliced = raw[min(idx) : max(idx) + 1]
        xs = [pt[0] for pt in sliced]
        ys = [pt[1] for pt in sliced]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        wave_x, wave_y, wave_w, wave_h = (v * scale for v in (302.0, 566.0, 420.0, 200.0))
        path = QPainterPath()
        for i, (x, y) in enumerate(sliced):
            pt = QPointF(
                wave_x + (x - min_x) / (max_x - min_x) * wave_w,
                wave_y + (y - min_y) / (max_y - min_y) * wave_h,
            )
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        wave_pen = QPen(color, 25.0 * scale * 1.6)
        wave_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        wave_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(wave_pen)
        painter.drawPath(path)


class BrandWord(QWidget):
    """The handwritten "Wave" (assets/wave.svg) writing itself in a loop —
    shown when the sidebar expands (8.8). Same FIFO draw/erase as the
    login hero."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(34)
        self._phase = 0.0
        from PyQt6.QtCore import QVariantAnimation

        self._clock = QVariantAnimation(self)
        self._clock.setStartValue(0.0)
        self._clock.setEndValue(1.0)
        self._clock.setDuration(6000)
        self._clock.setLoopCount(-1)
        self._clock.valueChanged.connect(self._on_tick)
        self._clock.start()

    def _on_tick(self, value: float) -> None:
        self._phase = float(value)
        if self.width() > 50:  # only animate when the sidebar is expanded
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.width() < 50:
            return
        from waveapp.ui.login_window import word_aspect, word_segments
        from waveapp.ui.wave_logo import loop_segment

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        seg_start, seg_end = loop_segment(self._phase)
        flat: list[tuple[float, float, bool]] = []
        for segment in word_segments():
            for i, (x, y) in enumerate(segment):
                flat.append((x, y, i == 0))
        total = len(flat)
        first, last = int(total * seg_start), int(total * seg_end)
        if last - first < 2:
            return
        word_w = min(self.width() - 12.0, 104.0)
        word_h = word_w / word_aspect()
        offset_y = (self.height() - word_h) / 2
        from PyQt6.QtGui import QPainterPath

        path = QPainterPath()
        pen_down = False
        for index in range(first, last):
            x, y, starts = flat[index]
            point = QPointF(6 + x * word_w, offset_y + y * word_h)
            if starts or not pen_down or index == first:
                path.moveTo(point)
                pen_down = True
            else:
                path.lineTo(point)
        pen = QPen(QColor(255, 255, 255, 235), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)


class ConnectionStatus(QObject):
    """Status dot state. The engine (ConnectionMonitor) drives it; the UI
    observes via the `changed` signal — never the other way around."""

    NAMES = ("Alpaca", "Data feed", "Telegram", "DB")

    changed = pyqtSignal(str, str, str)  # name, color, tooltip

    def __init__(self) -> None:
        super().__init__()
        self.colors: dict[str, str] = {name: theme.GREY for name in self.NAMES}

    def set_status(self, name: str, color: str, tooltip: str) -> None:
        if name in self.colors:
            self.colors[name] = color
            self.changed.emit(name, color, tooltip)


class _NavButton(QWidget):
    clicked = pyqtSignal()

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = label
        self.selected = False
        self.bright = False  # glass mode: inactive tabs match the page dots
        self._hover = False
        self.setFixedHeight(44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # no tooltip (8.6 r3: notes belong to the connection dots only)

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

        if self.selected or self._hover:
            if self.selected:  # macOS source-list selection wash
                bg = QColor(theme.BLUE)
                bg.setAlphaF(0.14)
            else:
                bg = QColor(0, 0, 0, 14)
            painter.setBrush(bg)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(self.rect().adjusted(8, 4, -8, -4), 7, 7)

        # 8.5: inactive tabs use the SAME bright white as the
        # inactive page dots (glass mode); solid fallback keeps muted ink
        if self.selected:
            color = QColor(theme.BLUE)
        elif self.bright:
            color = QColor(255, 255, 255, 235)
        else:
            color = QColor(theme.TEXT_MUTED)
        icon_rect = QRectF(
            (RAIL_WIDTH - _ICON_BOX) / 2, (self.height() - _ICON_BOX) / 2, _ICON_BOX, _ICON_BOX
        )
        TAB_ICONS[self.label](painter, icon_rect, color)

        if self.width() > RAIL_WIDTH + 24:
            painter.setPen(color)
            font = painter.font()
            font.setWeight(600 if self.selected else 500)
            painter.setFont(font)
            text_rect = self.rect().adjusted(RAIL_WIDTH + 2, 0, -8, 0)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self.label,
            )


class _HoverNote(QWidget):
    """8.6 r3: our OWN iOS-style note (translucent rounded card) — Qt's
    tooltip window showed sharp corners behind the rounding."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel("")
        self.label.setStyleSheet(
            f"color: {theme.TEXT}; background: transparent; font-size: 12.5px;"
        )
        layout.addWidget(self.label)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(QColor(0, 0, 0, 16), 1))
        painter.setBrush(QColor(235, 235, 240, 247))
        painter.drawRoundedRect(rect, 10, 10)

    def show_text(self, global_pos: QPoint, text: str) -> None:
        self.label.setText(text)
        self.adjustSize()
        self.move(global_pos)
        self.show()


class _StatusDot(QWidget):
    def __init__(self, name: str, status: ConnectionStatus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.status = status
        self.bright = False  # glass mode: idle dots/labels match inactive tabs
        self.setFixedHeight(26)
        self.note_text = f"{name}: not connected"
        self.note = _HoverNote()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.note.show_text(self.mapToGlobal(QPoint(self.width() + 4, 0)), self.note_text)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.note.hide()

    def refresh_tooltip(self, tooltip: str) -> None:
        self.note_text = tooltip
        if self.note.isVisible():  # live-update an already-visible note
            self.note.show_text(self.mapToGlobal(QPoint(self.width() + 4, 0)), tooltip)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        status_color = self.status.colors[self.name]
        if self.bright and status_color == theme.GREY:
            # 8.6: idle connections use the SAME bright white as the
            # inactive tabs/dots — grey vanished into the glass
            painter.setPen(QColor(0, 0, 0, 70))
            painter.setBrush(QColor(255, 255, 255, 235))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(status_color))
        painter.drawEllipse(QRectF(RAIL_WIDTH / 2 - 4, self.height() / 2 - 4, 8, 8))
        if self.width() > RAIL_WIDTH + 24:
            painter.setPen(QColor(255, 255, 255, 235) if self.bright else QColor(theme.TEXT_MUTED))
            text_rect = self.rect().adjusted(RAIL_WIDTH + 2, 0, -8, 0)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self.name,
            )


class Sidebar(QFrame):
    """Hover-expanding navigation rail. Emits `page_selected(index)`."""

    page_selected = pyqtSignal(int)

    def __init__(self, status: ConnectionStatus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status = status
        self.setFixedWidth(RAIL_WIDTH)
        self.setStyleSheet(
            f"Sidebar {{ background: {theme.SIDEBAR_BG}; border-right: 1px solid {theme.BORDER}; }}"
        )

        self._anim = QPropertyAnimation(self, b"minimumWidth", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        # bound-method slot, NEVER a loose lambda: Qt auto-disconnects bound
        # methods when the widget dies; a lambda kept firing into the deleted
        # C++ widget and segfaulted the 2026-09-03 build gate (same class as
        # the settings _SaveSpinner lesson of 2026-08-21)
        self._anim.valueChanged.connect(self._apply_width)

        self._layout = QVBoxLayout(self)
        layout = self._layout
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(2)

        # brand row (8.8): the Wave icon, with the handwritten word
        # writing itself beside it once the rail expands
        brand_row = QWidget()
        brand_layout = QHBoxLayout(brand_row)
        brand_layout.setContentsMargins((RAIL_WIDTH - 34) // 2, 2, 6, 6)
        brand_layout.setSpacing(6)
        self.brand_badge = BrandBadge()
        self.brand_word = BrandWord()
        brand_layout.addWidget(self.brand_badge)
        brand_layout.addWidget(self.brand_word, stretch=1)
        layout.addWidget(brand_row)

        self.buttons: list[_NavButton] = []
        for index, label in enumerate(TAB_ICONS):
            button = _NavButton(label)
            button.clicked.connect(lambda i=index: self.select(i))
            layout.addWidget(button)
            self.buttons.append(button)

        layout.addStretch(1)

        self.dots: list[_StatusDot] = []
        for name in ConnectionStatus.NAMES:
            dot = _StatusDot(name, status)
            layout.addWidget(dot)
            self.dots.append(dot)

        status.changed.connect(self._on_status_changed)
        self.buttons[0].selected = True

    def _apply_width(self, value) -> None:
        self.setFixedWidth(int(value))

    def set_glass(self, active: bool) -> None:
        """Glass mode: the native panel paints the surface — go transparent,
        and push content below the traffic lights (the sidebar extends under
        the transparent titlebar in glass mode)."""
        if active:
            # Phase 8.3 round 2: a bit of depth — darker wash + hairline
            # border, translucent so the glass still shows through
            self.setStyleSheet(
                "Sidebar { background: rgba(0, 0, 0, 0.05);"
                " border-right: 1px solid rgba(0, 0, 0, 0.12); }"
            )
            self._layout.setContentsMargins(0, 44, 0, 16)
            for button in self.buttons:
                button.bright = True
                button.update()
            for dot in self.dots:
                dot.bright = True
                dot.update()
        else:
            self.setStyleSheet(
                f"Sidebar {{ background: {theme.SIDEBAR_BG}; "
                f"border-right: 1px solid {theme.BORDER}; }}"
            )
            self._layout.setContentsMargins(0, 12, 0, 12)

    def _on_status_changed(self, name: str, color: str, tooltip: str) -> None:
        for dot in self.dots:
            if dot.name == name:
                dot.refresh_tooltip(tooltip)
                dot.update()

    def select(self, index: int) -> None:
        for i, button in enumerate(self.buttons):
            button.selected = i == index
            button.update()
        self.page_selected.emit(index)

    def _animate_to(self, width: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(width)
        self._anim.start()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._animate_to(EXPANDED_WIDTH)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._animate_to(RAIL_WIDTH)
