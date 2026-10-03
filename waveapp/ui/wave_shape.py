"""The Wave line — traced from a reference video (2026-08-04): one
continuous stroke, right tail → curl → crest → long left tail.

The stroke lives in `wave_stroke.json` (normalized to its bounding box,
ordered as the video draws it: index 0 = right tip, where the self-drawing
animation starts). This module loads it, resamples it uniformly by arc length
(so partial reveals move at constant speed along the line), and applies the
state transforms: slicing (self-draw/erase), flatten (paused) and jagged
(error). Pure math + stdlib so it stays unit-testable headless.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

_STROKE_FILE = Path(__file__).with_name("wave_stroke.json")
_RESAMPLED_N = 260


@lru_cache(maxsize=1)
def _load() -> tuple[float, list[tuple[float, float]]]:
    data = json.loads(_STROKE_FILE.read_text())
    raw = [(float(x), float(y)) for x, y in data["points"]]

    # cumulative arc length
    dists = [0.0]
    for (x1, y1), (x2, y2) in zip(raw, raw[1:], strict=False):
        dists.append(dists[-1] + math.hypot(x2 - x1, y2 - y1))
    total = dists[-1] or 1.0

    # uniform resample: constant spacing along the line
    resampled: list[tuple[float, float]] = []
    j = 0
    for i in range(_RESAMPLED_N):
        target = total * i / (_RESAMPLED_N - 1)
        while j < len(dists) - 2 and dists[j + 1] < target:
            j += 1
        seg = dists[j + 1] - dists[j] or 1.0
        t = (target - dists[j]) / seg
        x = raw[j][0] + (raw[j + 1][0] - raw[j][0]) * t
        y = raw[j][1] + (raw[j + 1][1] - raw[j][1]) * t
        resampled.append((x, y))
    return float(data["aspect"]), resampled


def stroke_aspect() -> float:
    """Natural width/height ratio of the traced wave."""
    return _load()[0]


def wave_points(
    width: float,
    height: float,
    *,
    start: float = 0.0,
    end: float = 1.0,
    flatten: float = 1.0,
    jagged: float = 0.0,
) -> list[tuple[float, float]]:
    """The wave (or the [start..end] slice of it, as arc-length fractions,
    for the self-drawing animation) scaled to `width` x `height`.

    flatten: 1 = full silhouette, 0 = flat midline (the paused flatline).
    jagged:  0 smooth .. 1 hard zigzag (the error state).
    """
    _, pts = _load()
    n = len(pts)
    i0 = max(0, min(n - 1, round(start * (n - 1))))
    i1 = max(0, min(n - 1, round(end * (n - 1))))
    if i1 - i0 < 1:
        return []
    line = pts[i0 : i1 + 1]

    if flatten < 1.0:
        mid = 0.5
        line = [(x, mid + (y - mid) * flatten) for x, y in line]

    scaled = [(x * width, y * height) for x, y in line]

    # Jagged runs in pixel space so the zigzag is uniform despite the wave's
    # wide aspect stretch.
    if jagged > 0.0:
        out: list[tuple[float, float]] = []
        amp = jagged * height * 0.05
        for i, (x, y) in enumerate(scaled):
            if 0 < i < len(scaled) - 1:
                px, py = scaled[i - 1]
                nx_, ny_ = scaled[i + 1]
                dx, dy = nx_ - px, ny_ - py
                length = math.hypot(dx, dy) or 1.0
                sign = 1.0 if i % 2 == 0 else -1.0
                y += sign * amp * (dx / length)
                x -= sign * amp * (dy / length)
            out.append((x, y))
        scaled = out

    return scaled
