#!/usr/bin/env python
"""Render README screenshots from sample data — no broker, no network.

Builds the real MainWindow offscreen, feeds the Positions tab a handful of
representative demo positions (longs, a short, every judge stance, the
exit-stage badges) and saves PNGs under assets/screenshots/.

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/demo_screenshot.py
"""

from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from waveapp.config import AppConfig  # noqa: E402
from waveapp.ui.main_window import MainWindow  # noqa: E402
from waveapp.ui.theme import APP_QSS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "assets" / "screenshots"


def _bars(entry: float, up: bool, n: int = 78) -> list[tuple]:
    """A plausible intraday 1-min tape drifting toward the position's P&L."""
    t0 = datetime(2026, 9, 18, 9, 30)
    px = entry * (0.9985 if up else 1.0015)
    out = []
    for i in range(n):
        drift = entry * 0.00022 * (1 if up else -1)
        wave = entry * 0.0007 * math.sin(i / 6.5)
        o = px
        c = px + drift + (wave - entry * 0.0007 * math.sin((i - 1) / 6.5))
        h = max(o, c) + entry * 0.0004
        low = min(o, c) - entry * 0.0004
        out.append((t0 + timedelta(minutes=i), o, h, low, c))
        px = c
    return out


DEMO = [
    dict(
        key="NVDA",
        symbol="NVDA",
        side="long",
        qty=40,
        entry=182.45,
        last=184.10,
        sellable=184.08,
        stop=182.45,
        stage="BE",
        strategy="orb",
        stance="RIDE",
        brain=0.64,
        bars=_bars(182.45, True),
        entry_index=12,
    ),
    dict(
        key="TSLA",
        symbol="TSLA",
        side="long",
        qty=25,
        entry=248.60,
        last=251.95,
        sellable=251.90,
        stop=249.40,
        stage="TRAIL",
        strategy="vwap",
        stance="RIDE",
        brain=0.58,
        bars=_bars(248.60, True),
        entry_index=9,
    ),
    dict(
        key="AMD",
        symbol="AMD",
        side="short",
        qty=60,
        entry=164.30,
        last=162.85,
        sellable=162.88,
        stop=165.90,
        strategy="gap",
        stance="BANK",
        brain=0.61,
        bars=_bars(164.30, False),
        entry_index=7,
    ),
    dict(
        key="PLTR",
        symbol="PLTR",
        side="long",
        qty=150,
        entry=42.18,
        last=43.05,
        sellable=43.03,
        stop=42.20,
        stage="SCALED",
        strategy="gap",
        stance="READY",
        brain=0.57,
        bars=_bars(42.18, True),
        entry_index=15,
    ),
    dict(
        key="META",
        symbol="META",
        side="long",
        qty=12,
        entry=512.75,
        last=511.60,
        sellable=511.55,
        stop=508.90,
        strategy="orb",
        stance="WAIT",
        brain=0.52,
        bars=_bars(512.75, False),
        entry_index=5,
    ),
    dict(
        key="COIN",
        symbol="COIN",
        side="short",
        qty=18,
        entry=215.40,
        last=213.75,
        sellable=213.80,
        stop=218.10,
        stage="BE",
        strategy="vwap",
        stance="RIDE",
        brain=0.60,
        bars=_bars(215.40, False),
        entry_index=11,
    ),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)
    window = MainWindow(AppConfig())
    window.resize(1440, 900)
    window.show()
    window.pages.setCurrentWidget(window.positions_page)
    for _ in range(5):
        app.processEvents()
    window.positions_page.update_positions([dict(p) for p in DEMO])
    for _ in range(10):
        app.processEvents()
    window.grab().save(str(OUT / "positions.png"))
    print(f"saved {OUT / 'positions.png'}")

    # the double-click detail popup, over the same grid
    from waveapp.ui.positions_page import PositionDetailPopup

    popup = PositionDetailPopup(window, dict(DEMO[0]))
    popup.show()
    for _ in range(10):
        app.processEvents()
    window.grab().save(str(OUT / "position_detail.png"))
    print(f"saved {OUT / 'position_detail.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
