#!/usr/bin/env python
"""Sector map for the theme graph (blueprint 4.6 step 1, 2026-09-02).

One-time-ish (weekly refresh): fetch every universe symbol's SIC code from
the Massive ticker-overview endpoint (detail-only field — the bulk listing
does not carry it) and bucket it into 11 broad sectors. Saved to
scanner2_sectors.npz; Scanner 2.0 loads it at set_universe for per-sector
RVOL heat ("what is the market trading today"). ETFs and unknowns land in
OTHER — themes live in common stocks.

Usage: nice -n 19 .venv/bin/python scripts/scanner2_sectors.py
"""

from __future__ import annotations

import json
import time as time_mod
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from waveapp.research.history import KEYCHAIN_POLYGON_KEY

from waveapp.config import support_dir
from waveapp.security import secrets

API = "https://api.polygon.io"
KEY = secrets.get_secret(KEYCHAIN_POLYGON_KEY)
REFRESH_DAYS = 7.0

SECTORS = (
    "TECH",  # 0
    "HEALTHCARE",  # 1
    "FINANCIALS",  # 2
    "CONSUMER",  # 3
    "INDUSTRIALS",  # 4
    "ENERGY",  # 5
    "MATERIALS",  # 6
    "UTILITIES",  # 7
    "REAL_ESTATE",  # 8
    "COMM",  # 9
    "OTHER",  # 10
)


def sic_to_sector(sic: int) -> int:
    """Coarse SIC-major-group → 11 buckets. Imperfect on purpose — heat
    z-scores only need broadly-right groups."""
    major = sic // 100
    if sic // 10 == 283 or major == 80:  # pharma / health services
        return 1
    if major in (35, 36, 38) or sic // 10 == 737 or major == 73:
        return 0  # hardware, electronics, instruments, software/services
    if major == 13 or major == 29:
        return 5  # oil & gas, refining
    if major in (10, 12, 14, 28, 32, 33):
        return 6  # mining, chemicals, metals
    if major in (15, 16, 17, 34, 37) or 40 <= major <= 47:
        return 4  # construction, machinery, transport
    if major == 48 or major in (78, 79):
        return 9  # communications, media/entertainment
    if major == 49:
        return 7  # utilities
    if major in (65, 67):
        return 8  # real estate, REITs
    if 60 <= major <= 64:
        return 2  # banks, insurance, brokers
    if 20 <= major <= 39 or 50 <= major <= 59 or 70 <= major <= 89:
        return 3  # everything consumer-facing that's left
    return 10


def fetch_one(symbol: str) -> tuple[str, int]:
    url = f"{API}/v3/reference/tickers/{symbol}?apiKey={KEY}"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310
                data = json.loads(r.read())
            sic = data.get("results", {}).get("sic_code")
            return symbol, sic_to_sector(int(sic)) if sic else 10
        except Exception:
            if attempt:
                return symbol, 10
            time_mod.sleep(1.0)
    return symbol, 10


def main() -> int:
    out_path = support_dir() / "scanner2_sectors.npz"
    if out_path.exists():
        age = (time_mod.time() - out_path.stat().st_mtime) / 86400.0
        if age < REFRESH_DAYS:
            print(f"sector map is {age:.1f}d old (< {REFRESH_DAYS:g}d) — nothing to do")
            return 0
    universe_path = support_dir() / "scanner2_universe.json"
    symbols = sorted(json.loads(universe_path.read_text())["symbols"])
    print(f"fetching SIC for {len(symbols)} symbols (threaded, gentle)…", flush=True)
    results: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_one, s): s for s in symbols}
        for i, future in enumerate(as_completed(futures)):
            symbol, sector = future.result()
            results[symbol] = sector
            if (i + 1) % 1000 == 0:
                print(f"  {i + 1}/{len(symbols)}", flush=True)
    ordered = sorted(results)
    np.savez_compressed(
        out_path,
        symbols=np.array(ordered),
        sector_ids=np.array([results[s] for s in ordered], dtype=np.int8),
        sector_names=np.array(SECTORS),
    )
    counts = {SECTORS[i]: sum(1 for v in results.values() if v == i) for i in range(len(SECTORS))}
    print(f"saved {out_path.name}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
