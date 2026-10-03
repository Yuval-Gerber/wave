#!/usr/bin/env python
"""Phase 10.13: per-symbol-day EFFECTIVE-spread estimates via the EDGE
estimator (Ardia/Guidotti/Kroencke, JFE 2024) from our own minute bars.
Fallback tier table (SIFMA 2024 quoted averages) when EDGE can't estimate."""

from __future__ import annotations

import sys
import warnings
from datetime import date

warnings.filterwarnings("ignore")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from research_exits import build_picks  # noqa: E402
from waveapp.research.frames import minute_frame, rth_only  # noqa: E402
from waveapp.research.history import HistoryStore  # noqa: E402
from waveapp.research.progress import finish, report  # noqa: E402

FULL = (date(2025, 8, 18), date(2026, 8, 14))


def main() -> int:
    import numpy as np
    from bidask import edge

    store = HistoryStore()
    store._conn.execute(
        "CREATE TABLE IF NOT EXISTS spread_estimates ("
        " symbol TEXT NOT NULL, day TEXT NOT NULL, spread_frac REAL,"
        " PRIMARY KEY (symbol, day))"
    )
    store._conn.commit()
    picks = build_picks(*FULL, stage="spread-picks")
    done_pairs = {
        (r[0], r[1])
        for r in store._conn.execute("SELECT symbol, day FROM spread_estimates").fetchall()
    }
    total = sum(len(s) for s in picks.values())
    saved = 0
    for index, (day_iso, symbols) in enumerate(picks.items()):
        if index % 10 == 0:
            report("spreads", index, len(picks), f"EDGE spreads: {index}/{len(picks)} days")
        day = date.fromisoformat(day_iso)
        rows = []
        for symbol in symbols:
            if (symbol, day_iso) in done_pairs:
                continue
            f = rth_only(minute_frame(store, symbol, day, day))
            spread = None
            if len(f) >= 100:
                try:
                    s = edge(f["open"], f["high"], f["low"], f["close"], sign=False)
                    if np.isfinite(s) and 0 <= s < 0.2:
                        spread = float(s)
                except Exception:  # noqa: S110 — EDGE failure = fallback tier
                    pass
            rows.append((symbol, day_iso, spread))
        store._conn.executemany("INSERT OR REPLACE INTO spread_estimates VALUES (?,?,?)", rows)
        store._conn.commit()
        saved += len(rows)
    finish(f"EDGE spreads DONE — {saved:,}/{total:,} symbol-days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
