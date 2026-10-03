#!/usr/bin/env python
"""CYCLE-2 NIGHT-0 PATH-WALKER (supervisor-conditioned).

Walks each night0 rich trade's minute path (entry → session end, HistoryStore)
and produces:
 A) THE FRONTIER REFIT — B1's binding guard: does the live journal's cliff
    (deep dips whose LOW prints ≥30m in recover far worse) replicate at sim
    scale on H1+IS? Printed as the gate verdict for the Night-1 sweep.
 B) EXIT-BRAIN SIM ROWS — B3's accelerator: per-minute features + forward
    triple-barrier labels (same math as scripts/exit_brain_dataset.py),
    H1+IS ONLY (H2/OOS quarantined), written to
    research/exit_brain_sim_rows.csv.gz — pretraining data ONLY, never mixed
    with live rows (the live table stays the validation set).

Usage: nice -n 19 .venv/bin/python scripts/exit_brain_walker.py
"""

from __future__ import annotations

import gzip
from datetime import datetime

import numpy as np
import pandas as pd
from waveapp.research.history import HistoryStore

from waveapp.config import support_dir

R = support_dir() / "research"
SESSION_END_UTC = 20 * 60  # 16:00 ET in minutes-of-day UTC (summer)


def day_bars(store, symbol, day_iso):
    day = datetime.fromisoformat(day_iso + "T00:00:00+00:00")
    a = int(day.timestamp() * 1000)
    return store._conn.execute(
        "SELECT ts_ms, open, high, low, close, volume FROM bars"
        " WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
        (symbol, a, a + 86_400_000),
    ).fetchall()


def main() -> int:
    store = HistoryStore()
    frames = [
        pd.read_csv(R / "core_H1-night0_rich.csv"),
        pd.read_csv(R / "core_IS-night0_rich.csv"),
    ]
    trades = pd.concat(frames, ignore_index=True)
    trades = trades[trades.entry_ts.notna() & (trades.qty > 0)]
    print(f"anchored trades: {len(trades)}")

    frontier = []  # (early_low: bool, deep: bool, recovered: bool)
    out = gzip.open(R / "exit_brain_sim_rows.csv.gz", "wt")
    out.write(
        "day,symbol,strategy,minute_index,unrealized_r,peak_r,retrace_frac,"
        "minutes_since_peak,vol_ratio,label_more_ahead\n"
    )
    rows_written = 0
    walked = skipped = 0
    for t in trades.itertuples():
        bars = day_bars(store, t.symbol, t.day)
        entry_ms = int(datetime.fromisoformat(t.entry_ts).timestamp() * 1000)
        held = [b for b in bars if b[0] >= entry_ms]
        if len(held) < 5:
            skipped += 1
            continue
        walked += 1
        risk = max(abs(t.entry_px - t.stop_px), 0.01)
        closes = np.array([b[4] for b in held])
        highs = np.array([b[2] for b in held])
        lows = np.array([b[3] for b in held])
        vols = np.array([b[5] for b in held], dtype=float)
        ur = (closes - t.entry_px) / risk  # unrealized R per minute (long book)
        fav = (highs - t.entry_px) / risk
        adv = (lows - t.entry_px) / risk
        peak = np.maximum.accumulate(fav)
        # frontier stats (uncensored to session end)
        depth_pct = (t.entry_px - lows.min()) / t.entry_px * 100
        deep = depth_pct >= 0.5
        t_low = int(np.argmin(lows))
        early_low = t_low < 30
        recovered = closes[-1] > t.entry_px
        if deep:
            frontier.append((early_low, recovered))
        # exit-brain rows (every 2nd minute to bound volume; labels forward)
        entry_vol = vols[0] if vols[0] > 0 else 1.0
        last_peak_i = 0
        for i in range(len(held)):
            if fav[i] >= peak[i] and (i == 0 or peak[i] > peak[i - 1]):
                last_peak_i = i
            if i % 2:
                continue
            future_fav = fav[i + 1 :]
            future_adv = adv[i + 1 :]
            label = 0
            for j in range(len(future_fav)):
                if future_adv[j] <= ur[i] - 0.5:
                    break
                if future_fav[j] >= ur[i] + 0.25:
                    label = 1
                    break
            retrace = 1.0 - ur[i] / peak[i] if peak[i] > 0 else 0.0
            out.write(
                f"{t.day},{t.symbol},{t.strategy},{i},{ur[i]:.3f},{peak[i]:.3f},"
                f"{retrace:.3f},{i - last_peak_i},{vols[i] / entry_vol:.2f},{label}\n"
            )
            rows_written += 1
    out.close()
    store.close()

    fr = pd.DataFrame(frontier, columns=["early_low", "recovered"])
    early = fr[fr.early_low]
    late = fr[~fr.early_low]
    print(f"\nwalked {walked}, skipped {skipped}; sim rows written: {rows_written}")
    print("\n== THE CLIFF (B1's binding gate) — deep dips (>=0.5%) ==")
    print(f"LOW in first 30m: {early.recovered.mean():.0%} recover (n={len(early)})")
    print(f"LOW after 30m:    {late.recovered.mean():.0%} recover (n={len(late)})")
    gap = early.recovered.mean() - late.recovered.mean()
    verdict = "OPEN (sweep may run)" if gap >= 0.15 else "CLOSED (no late-low sweep)"
    print(f"cliff gap: {gap:+.0%} — GATE {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
