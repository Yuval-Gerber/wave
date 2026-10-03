#!/usr/bin/env python
"""3-YEAR TRAINING BACKFILL (2026-08-30 — the Wave Brain, Stage 0).

Extends the history DB two more years back (2023-08-18 → 2025-08-17):
1. Grouped dailies for every trading day (1 Polygon call per day).
2. Stocks-in-play picks per day (the SAME selection the sims/day list use).
3. Minute bars for picked symbols only (the label/feature raw material).

Incremental + resumable: coverage tables skip anything already fetched.

Usage: nice -n 19 .venv/bin/python scripts/backfill_3y.py
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from waveapp.research.history import HistoryStore, PolygonClient, download_history
from waveapp.research.progress import finish, report
from waveapp.research.stocks_in_play import (
    download_grouped_daily,
    stocks_in_play,
    stocks_in_play_premarket,
    trading_days,
)

START = date(2023, 8, 18)
END = date(2025, 8, 17)
POOL_N = 60
TOP_N = 20


def main() -> int:
    started = datetime.now(UTC)
    store = HistoryStore()
    client = PolygonClient()

    print("== stage 1: grouped dailies ==", flush=True)
    download_grouped_daily(START, END, store, client, progress=lambda m: print("  ", m, flush=True))

    print("== stage 2+3: pools -> minute bars -> premarket re-rank ==", flush=True)
    days = trading_days(store, START, END)
    for index, day in enumerate(days):
        pool = stocks_in_play(store, day, top_n=POOL_N, min_rvol=1.5)
        if not pool:
            continue
        # premarket re-ranking needs the day's minute bars — fetch the POOL
        # first (same order as the original 1-year pipeline), then re-rank
        download_history([p.symbol for p in pool], day, day, store=store, client=client)
        stocks_in_play_premarket(store, day, pool, top_n=TOP_N)  # warms nothing; sanity
        if index % 10 == 0:
            report("backfill", index, len(days), f"{day}: pool {len(pool)}")
    store.close()
    finish(f"3y backfill done in {(datetime.now(UTC) - started).seconds}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
