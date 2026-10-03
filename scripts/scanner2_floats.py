#!/usr/bin/env python
"""Scanner 2.0 float backfill: shares outstanding (float ceiling proxy) from
the Massive ticker overview for every symbol with a real baseline curve.
Slow prior, journaled as an ML feature — never a trigger (weakest of the ten
evidence-ranked signals). Weekly refresh is plenty.

Usage: nice -n 19 .venv/bin/python scripts/scanner2_floats.py
"""

from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from waveapp.research.history import KEYCHAIN_POLYGON_KEY

from waveapp.engine.scanner2 import BaselineStore
from waveapp.security import secrets

KEY = secrets.get_secret(KEYCHAIN_POLYGON_KEY)


def fetch(symbol: str):
    url = f"https://api.polygon.io/v3/reference/tickers/{symbol}?apiKey={KEY}"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310
                data = json.loads(r.read()).get("results") or {}
                shares = data.get("share_class_shares_outstanding") or data.get(
                    "weighted_shares_outstanding"
                )
                return symbol, float(shares) if shares else 0.0
        except Exception:
            if attempt:
                return symbol, 0.0
            time.sleep(1.0)
    return symbol, 0.0


def main() -> int:
    store = BaselineStore()
    if not store.load():
        print("no baseline store — run scanner2_backfill.py first")
        return 1
    if store.shares_out is None:
        store.shares_out = np.zeros(len(store.symbols), dtype=np.float32)
    targets = [s for s, d in zip(store.symbols, store.days, strict=True) if d >= 3.0]
    print(f"fetching shares outstanding for {len(targets)} symbols…", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch, s): s for s in targets}
        for future in as_completed(futures):
            symbol, shares = future.result()
            store.shares_out[store.index[symbol]] = shares
            done += 1
            if done % 500 == 0:
                print(f"{done}/{len(targets)}", flush=True)
    store.save()
    filled = int((store.shares_out > 0).sum())
    print(f"=== FLOATS DONE: {filled} symbols have shares outstanding ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
