#!/usr/bin/env python3
"""Generate the Wave app icon: the breaking wave inside a thin-lined triangle
on a transparent background (2026-08-04), sharing the exact
wave geometry with the animated logo. Writes assets/wave.icns via iconutil.

Run from the repo root: .venv/bin/python scripts/make_icon.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from waveapp.ui import theme  # noqa: E402
from waveapp.ui.wave_shape import wave_points  # noqa: E402

SIZE = 1024
TRIANGLE_PEN = 34.0
WAVE_PEN = 25.0

# Triangle vertices (apex top), with margin for the stroke.
APEX = QPointF(512, 118)
BASE_L = QPointF(132, 892)
BASE_R = QPointF(892, 892)

# Icon composition (2026-08-04): don't squeeze the whole 6.2:1 line in —
# crop to the curl plus a bit of tail on each side, centered in the triangle.
CROP_X_LO, CROP_X_HI = 0.30, 0.72  # window of the stroke's unit x to show
WAVE_X, WAVE_Y, WAVE_W, WAVE_H = 302.0, 566.0, 420.0, 200.0


def render_master() -> QImage:
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    color = QColor(theme.OCEAN)  # ocean blue (2026-08-04)

    tri_pen = QPen(color, TRIANGLE_PEN)
    tri_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    tri_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(tri_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolygon(QPolygonF([APEX, BASE_L, BASE_R]))

    # Crop a contiguous run of the stroke around the curl (the line is
    # continuous, so the index range covering the x-window is contiguous).
    raw = wave_points(1.0, 1.0)
    idx = [i for i, (x, _) in enumerate(raw) if CROP_X_LO <= x <= CROP_X_HI]
    sliced = raw[min(idx) : max(idx) + 1]

    xs, ys = [pt[0] for pt in sliced], [pt[1] for pt in sliced]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    scale_x = WAVE_W / (max_x - min_x)
    scale_y = WAVE_H / (max_y - min_y)

    path = QPainterPath()
    for i, (x, y) in enumerate(sliced):
        pt = QPointF(WAVE_X + (x - min_x) * scale_x, WAVE_Y + (y - min_y) * scale_y)
        path.moveTo(pt) if i == 0 else path.lineTo(pt)

    wave_pen = QPen(color, WAVE_PEN)
    wave_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    wave_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(wave_pen)
    painter.drawPath(path)
    painter.end()
    return img


def main() -> int:
    QApplication.instance() or QApplication([])
    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(exist_ok=True)

    master = render_master()
    master.save(str(assets / "icon_1024.png"))

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "wave.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = base * scale
                name = f"icon_{base}x{base}" + ("@2x" if scale == 2 else "") + ".png"
                scaled = master.scaled(
                    px,
                    px,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                scaled.save(str(iconset / name))
        subprocess.run(  # noqa: S603
            ["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(assets / "wave.icns")],
            check=True,
        )
    print(f"Wrote {assets / 'wave.icns'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
