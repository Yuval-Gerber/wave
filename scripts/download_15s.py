#!/usr/bin/env python
"""Phase 10.8: download 15-second bars for every picked symbol-day (both
windows), incrementally, with progress reporting."""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from research_exits import IN_SAMPLE, OUT_SAMPLE, build_picks  # noqa: E402
from waveapp.research.history import (  # noqa: E402
    HistoryStore,
    PolygonClient,
    _days_between,
    covered15_days,
    fetch_15s_bars,
    mark_covered15,
    save_bars15,
)
from waveapp.research.progress import finish, report  # noqa: E402


def main() -> int:
    store = HistoryStore()
    client = PolygonClient()
    picks_in = build_picks(*IN_SAMPLE, stage="picks-in")
    picks_out = build_picks(*OUT_SAMPLE, stage="picks-oos")
    by_symbol: dict[str, list[date]] = defaultdict(list)
    for picks in (picks_in, picks_out):
        for day_iso, symbols in picks.items():
            for symbol in symbols:
                by_symbol[symbol].append(date.fromisoformat(day_iso))

    total = len(by_symbol)
    saved_total = 0
    for index, (symbol, days) in enumerate(sorted(by_symbol.items())):
        if index % 5 == 0:
            report("dl15", index, total, f"15s download: {index}/{total} symbols")
        missing = sorted(set(days) - covered15_days(store, symbol))
        if not missing:
            continue
        # contiguous runs (≤7-day gaps) to limit request count
        runs, run = [], [missing[0]]
        for d in missing[1:]:
            if (d - run[-1]).days <= 7:
                run.append(d)
            else:
                runs.append(run)
                run = [d]
        runs.append(run)
        for r in runs:
            bars = fetch_15s_bars(client, symbol, r[0], r[-1])
            saved_total += save_bars15(store, bars)
            mark_covered15(store, symbol, _days_between(r[0], r[-1]))
    finish(f"15s download DONE — {saved_total:,} bars saved for {total} symbols")
    print(f"saved {saved_total:,} bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
