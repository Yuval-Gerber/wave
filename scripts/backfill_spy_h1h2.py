#!/usr/bin/env python
"""Backfill SPY minute bars for the H1/H2 sweep windows (2026-09-03,
Goalkeeper's catch: the day-type router's SPY classifier had ZERO bars in
H1/H2 — half its 4-window test was silently inert). Pulls from Polygon in
month chunks and saves through the store's own API.

Usage: nice -n 19 .venv/bin/python scripts/backfill_spy_h1h2.py
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, timedelta

from waveapp.research.history import HistoryStore, MinuteBar

from waveapp.security.secrets import get_secret

START, END = date(2023, 9, 1), date(2025, 8, 16)


def main() -> int:
    key = get_secret("polygon_api_key")
    store = HistoryStore()
    total = 0
    cursor = START
    while cursor <= END:
        chunk_end = min(cursor + timedelta(days=30), END)
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/minute/"
            f"{cursor}/{chunk_end}?adjusted=true&sort=asc&limit=50000&apiKey={key}"
        )
        with urllib.request.urlopen(url, timeout=60) as r:
            results = json.loads(r.read()).get("results") or []
        bars = [
            MinuteBar(
                symbol="SPY",
                ts_ms=int(b["t"]),
                open=float(b["o"]),
                high=float(b["h"]),
                low=float(b["l"]),
                close=float(b["c"]),
                volume=float(b.get("v") or 0),
                vwap=float(b["vw"]) if b.get("vw") is not None else None,
                trades=int(b["n"]) if b.get("n") is not None else None,
            )
            for b in results
        ]
        total += store.save_bars(bars)
        print(f"{cursor} → {chunk_end}: {len(bars)} bars (total {total})", flush=True)
        cursor = chunk_end + timedelta(days=1)
    store.close()
    print(f"done: {total} SPY bars saved for H1/H2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
