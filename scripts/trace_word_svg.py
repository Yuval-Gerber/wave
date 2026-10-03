"""Trace assets/wave.svg (the handwritten 'Wave') into
waveapp/ui/word_stroke.json — the source of the login hero's word animation.

Re-run whenever there is a new wave.svg:
    .venv/bin/python scripts/trace_word_svg.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "assets" / "wave.svg"
OUT = ROOT / "waveapp" / "ui" / "word_stroke.json"
SAMPLES = 10  # points per cubic Bézier


def _cubic(p0, p1, p2, p3, n):
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        yield (
            u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        )


def main() -> None:
    svg = SVG.read_text()
    segments: list[list[tuple[float, float]]] = []
    for d in re.findall(r'\sd="([^"]+)"', svg):
        if not d.strip().startswith("M"):
            continue  # not a pen path
        points: list[tuple[float, float]] = []
        current: tuple[float, float] | None = None
        for cmd, blob in re.findall(r"([MC])([^MC]*)", d):
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", blob)]
            if cmd == "M":
                current = (nums[0], nums[1])
                points.append(current)
            else:
                for i in range(0, len(nums) - 5, 6):
                    p1 = (nums[i], nums[i + 1])
                    p2 = (nums[i + 2], nums[i + 3])
                    p3 = (nums[i + 4], nums[i + 5])
                    points.extend(_cubic(current, p1, p2, p3, SAMPLES))
                    current = p3
        if len(points) > 1:
            segments.append(points)
    xs = [x for seg in segments for x, _ in seg]
    ys = [y for seg in segments for _, y in seg]
    min_x, min_y = min(xs), min(ys)
    width, height = max(xs) - min_x, max(ys) - min_y
    data = {
        "aspect": width / height,
        "segments": [
            [[round((x - min_x) / width, 4), round((y - min_y) / height, 4)] for x, y in seg]
            for seg in segments
        ],
    }
    OUT.write_text(json.dumps(data))
    total = sum(len(s) for s in segments)
    print(f"{len(segments)} strokes, {total} points, aspect {width / height:.2f} -> {OUT}")


if __name__ == "__main__":
    main()
