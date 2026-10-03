"""Monochrome line icons (SPEC.md §5 sidebar), drawn with QPainter — no
image assets. Each drawer paints into a normalized 24x24 box."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPen


def _pen(color, width: float = 1.8) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def draw_positions(p: QPainter, r: QRectF, color) -> None:
    """2x2 card grid."""
    p.setPen(_pen(color))
    gap = r.width() * 0.12
    cell_w = (r.width() - gap) / 2
    cell_h = (r.height() - gap) / 2
    for ix in (0, 1):
        for iy in (0, 1):
            cell = QRectF(
                r.left() + ix * (cell_w + gap),
                r.top() + iy * (cell_h + gap),
                cell_w,
                cell_h,
            )
            p.drawRoundedRect(cell, 2.5, 2.5)


def draw_performance(p: QPainter, r: QRectF, color) -> None:
    """8.6 r3: clean monochrome performance glyph — axis + rising curve with
    a dot on the latest point (the old arrowhead looked off)."""
    p.setPen(_pen(color))
    # axis: left + bottom
    p.drawLine(QPointF(r.left(), r.top() + 0.05 * r.height()), QPointF(r.left(), r.bottom()))
    p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right() - 0.05 * r.width(), r.bottom()))
    pts = [(0.16, 0.78), (0.42, 0.52), (0.62, 0.62), (0.92, 0.22)]
    path = QPainterPath()
    for i, (x, y) in enumerate(pts):
        pt = QPointF(r.left() + x * r.width(), r.top() + y * r.height())
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    p.drawPath(path)
    p.setBrush(color)
    p.drawEllipse(
        QPointF(r.left() + pts[-1][0] * r.width(), r.top() + pts[-1][1] * r.height()), 2.2, 2.2
    )
    p.setBrush(Qt.BrushStyle.NoBrush)


def draw_scanner(p: QPainter, r: QRectF, color) -> None:
    """Magnifier with a pulse dot."""
    p.setPen(_pen(color))
    d = min(r.width(), r.height()) * 0.62
    lens = QRectF(r.left(), r.top(), d, d)
    p.drawEllipse(lens)
    p.drawLine(
        QPointF(lens.right() - d * 0.08, lens.bottom() - d * 0.08),
        QPointF(r.right(), r.bottom()),
    )
    p.setBrush(color)
    p.drawEllipse(lens.center(), 1.6, 1.6)
    p.setBrush(Qt.BrushStyle.NoBrush)


def draw_system(p: QPainter, r: QRectF, color) -> None:
    """Pulse/heartbeat line in a rounded frame."""
    p.setPen(_pen(color))
    p.drawRoundedRect(r, 3, 3)
    mid = r.center().y()
    path = QPainterPath(QPointF(r.left() + r.width() * 0.12, mid))
    path.lineTo(r.left() + r.width() * 0.35, mid)
    path.lineTo(r.left() + r.width() * 0.45, r.top() + r.height() * 0.25)
    path.lineTo(r.left() + r.width() * 0.58, r.top() + r.height() * 0.75)
    path.lineTo(r.left() + r.width() * 0.68, mid)
    path.lineTo(r.left() + r.width() * 0.88, mid)
    p.drawPath(path)


def draw_log(p: QPainter, r: QRectF, color) -> None:
    """List lines with bullet dots."""
    p.setPen(_pen(color))
    for i, frac in enumerate((0.2, 0.5, 0.8)):
        y = r.top() + frac * r.height()
        p.setBrush(color)
        p.drawEllipse(QPointF(r.left() + 1.5, y), 1.3, 1.3)
        p.setBrush(Qt.BrushStyle.NoBrush)
        end_frac = (0.95, 0.75, 0.88)[i]
        p.drawLine(
            QPointF(r.left() + r.width() * 0.22, y),
            QPointF(r.left() + r.width() * end_frac, y),
        )


def draw_settings(p: QPainter, r: QRectF, color) -> None:
    """Three slider rails with offset knobs."""
    p.setPen(_pen(color))
    knob_x = (0.7, 0.3, 0.55)
    for i, frac in enumerate((0.2, 0.5, 0.8)):
        y = r.top() + frac * r.height()
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setBrush(color)
        p.drawEllipse(QPointF(r.left() + knob_x[i] * r.width(), y), 2.4, 2.4)
        p.setBrush(Qt.BrushStyle.NoBrush)


def draw_test(p: QPainter, r: QRectF, color) -> None:
    """Lab flask (kept for the Settings→Test bench, should it ever want an icon)."""
    p.setPen(_pen(color))
    w, h = r.width(), r.height()
    path = QPainterPath()
    path.moveTo(QPointF(r.left() + 0.38 * w, r.top() + 0.08 * h))
    path.lineTo(QPointF(r.left() + 0.38 * w, r.top() + 0.42 * h))
    path.lineTo(QPointF(r.left() + 0.16 * w, r.top() + 0.86 * h))
    path.quadTo(
        QPointF(r.left() + 0.13 * w, r.top() + 0.95 * h),
        QPointF(r.left() + 0.24 * w, r.top() + 0.95 * h),
    )
    path.lineTo(QPointF(r.left() + 0.76 * w, r.top() + 0.95 * h))
    path.quadTo(
        QPointF(r.left() + 0.87 * w, r.top() + 0.95 * h),
        QPointF(r.left() + 0.84 * w, r.top() + 0.86 * h),
    )
    path.lineTo(QPointF(r.left() + 0.62 * w, r.top() + 0.42 * h))
    path.lineTo(QPointF(r.left() + 0.62 * w, r.top() + 0.08 * h))
    p.drawPath(path)
    p.drawLine(  # the neck lip
        QPointF(r.left() + 0.30 * w, r.top() + 0.08 * h),
        QPointF(r.left() + 0.70 * w, r.top() + 0.08 * h),
    )
    p.drawLine(  # liquid level
        QPointF(r.left() + 0.30 * w, r.top() + 0.68 * h),
        QPointF(r.left() + 0.70 * w, r.top() + 0.68 * h),
    )


IconDrawer = Callable[[QPainter, QRectF, object], None]


def draw_ml(p: QPainter, r: QRectF, color) -> None:
    """A minimal brain: two rounded lobes with a center fold."""
    p.setPen(_pen(color))
    w, h = r.width(), r.height()
    path = QPainterPath(QPointF(r.center().x(), r.top() + h * 0.10))
    # left lobe
    path.cubicTo(
        QPointF(r.left() + w * 0.06, r.top() + h * 0.02),
        QPointF(r.left() - w * 0.06, r.top() + h * 0.62),
        QPointF(r.left() + w * 0.30, r.bottom() - h * 0.08),
    )
    # chin bridge
    path.quadTo(
        QPointF(r.center().x(), r.bottom() + h * 0.06),
        QPointF(r.right() - w * 0.30, r.bottom() - h * 0.08),
    )
    # right lobe
    path.cubicTo(
        QPointF(r.right() + w * 0.06, r.top() + h * 0.62),
        QPointF(r.right() - w * 0.06, r.top() + h * 0.02),
        QPointF(r.center().x(), r.top() + h * 0.10),
    )
    p.drawPath(path)
    # the center fold
    p.drawLine(
        QPointF(r.center().x(), r.top() + h * 0.14),
        QPointF(r.center().x(), r.bottom() - h * 0.16),
    )
    # one gyrus per lobe
    fold = QPainterPath(QPointF(r.left() + w * 0.24, r.top() + h * 0.38))
    fold.quadTo(
        QPointF(r.left() + w * 0.40, r.top() + h * 0.28),
        QPointF(r.left() + w * 0.38, r.top() + h * 0.52),
    )
    p.drawPath(fold)
    fold2 = QPainterPath(QPointF(r.right() - w * 0.24, r.top() + h * 0.40))
    fold2.quadTo(
        QPointF(r.right() - w * 0.40, r.top() + h * 0.30),
        QPointF(r.right() - w * 0.38, r.top() + h * 0.54),
    )
    p.drawPath(fold2)


def draw_duel(p: QPainter, r: QRectF, color) -> None:
    """Two crossed blades — kitchen vs judge (2026-09-20)."""
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(
        QPointF(r.left() + r.width() * 0.18, r.top() + r.height() * 0.18),
        QPointF(r.left() + r.width() * 0.82, r.top() + r.height() * 0.82),
    )
    p.drawLine(
        QPointF(r.left() + r.width() * 0.82, r.top() + r.height() * 0.18),
        QPointF(r.left() + r.width() * 0.18, r.top() + r.height() * 0.82),
    )
    # the two guards
    p.drawLine(
        QPointF(r.left() + r.width() * 0.30, r.top() + r.height() * 0.62),
        QPointF(r.left() + r.width() * 0.38, r.top() + r.height() * 0.70),
    )
    p.drawLine(
        QPointF(r.left() + r.width() * 0.70, r.top() + r.height() * 0.62),
        QPointF(r.left() + r.width() * 0.62, r.top() + r.height() * 0.70),
    )


TAB_ICONS: dict[str, IconDrawer] = {
    "Positions": draw_positions,
    "Performance": draw_performance,
    "Scanner": draw_scanner,
    "ML": draw_ml,
    "System": draw_system,
    "Log": draw_log,
    "Settings": draw_settings,
    # "Test" left the sidebar 2026-08-18 — the bench lives inside Settings now
}
