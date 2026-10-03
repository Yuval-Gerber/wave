#!/usr/bin/env python
"""Scanner 2.0 baseline backfill (2026-08-31, S1).

Fills scanner2_baselines.npz from Polygon/Massive history so the shadow
scanner starts with real 20-day volume curves instead of the cold fallback:

1. ~25 grouped-daily calls (whole market per day) → ADV(20), ATR%(14),
   prev_close for EVERY symbol.
2. Per-symbol minute aggregates (one call each, 20 days, 4:00-16:00 ET) →
   the same-minute-of-day cumulative volume curve — only for symbols that
   clear a soft floor (ADV ≥ 300k and price ≥ $10); everything else uses
   the ADV × U-curve fallback until it earns a curve live.

Delayed data tier is fine — this is history. Unlimited REST calls.

Usage: nice -n 19 .venv/bin/python scripts/scanner2_backfill.py
"""

from __future__ import annotations

import json
import time as time_mod
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from waveapp.research.history import KEYCHAIN_POLYGON_KEY

from waveapp.config import support_dir
from waveapp.engine.scanner2 import MINUTES, BaselineStore, minute_index
from waveapp.security import secrets

ET = ZoneInfo("America/New_York")
API = "https://api.polygon.io"
KEY = secrets.get_secret(KEYCHAIN_POLYGON_KEY)
CURVE_MIN_ADV = 300_000.0
CURVE_MIN_PRICE = 10.0
DAYS_BACK = 20


def get(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            sep = "&" if "?" in url else "?"
            with urllib.request.urlopen(f"{url}{sep}apiKey={KEY}", timeout=30) as r:  # noqa: S310
                return json.loads(r.read())
        except Exception:
            if attempt == retries - 1:
                return None
            time_mod.sleep(1.0 + attempt)
    return None


def trading_days(n: int) -> list[date]:
    days: list[date] = []
    probe = date.today()
    while len(days) < n:
        probe -= timedelta(days=1)
        if probe.weekday() < 5:
            days.append(probe)
    return days


def get_paginated(url: str, max_pages: int = 60) -> list[dict]:
    """Follow next_url pagination (Massive bulk endpoints)."""
    rows: list[dict] = []
    for _page in range(max_pages):
        data = get(url)
        if not data:
            break
        rows.extend(data.get("results") or [])
        url = data.get("next_url") or ""
        if not url:
            break
    return rows


def _fetch_short_ctx(store, universe: set[str]) -> int:
    """Stage 1c — 4.7 squeeze priors: latest FINRA short interest (bi-monthly,
    ~2wk stale by design — a SLOW structural feature) + the latest daily
    off-exchange short-volume ratio. Journaled only; the §11 pipeline decides
    if the composite ever gates anything."""
    from datetime import date as date_cls

    # latest short interest per ticker (last ~35 days covers 2 settlements)
    since = (date_cls.today() - timedelta(days=35)).isoformat()
    si_rows = get_paginated(
        f"{API}/stocks/v1/short-interest?settlement_date.gte={since}&limit=1000"
    )
    latest_si: dict[str, dict] = {}
    for row in si_rows:
        ticker = str(row.get("ticker", ""))
        if universe and ticker not in universe:
            continue
        prev = latest_si.get(ticker)
        if prev is None or str(row.get("settlement_date", "")) > str(prev.get("settlement_date")):
            latest_si[ticker] = row
    # latest short-volume day (try back a few days for publication lag)
    sv_map: dict[str, float] = {}
    for back in range(1, 5):
        day = (date_cls.today() - timedelta(days=back)).isoformat()
        sv_rows = get_paginated(f"{API}/stocks/v1/short-volume?date={day}&limit=1000")
        if sv_rows:
            for row in sv_rows:
                ticker = str(row.get("ticker", ""))
                if not universe or ticker in universe:
                    sv_map[ticker] = float(row.get("short_volume_ratio") or 0.0)
            print(f"short-volume day {day}: {len(sv_map)} tickers")
            break
    filled = 0
    for ticker, row in latest_si.items():
        idx = store.index.get(ticker)
        if idx is None:
            continue
        shares = float(store.shares_out[idx]) if store.shares_out is not None else 0.0
        si_shares = float(row.get("short_interest") or 0.0)
        si_pct = (si_shares / shares * 100.0) if shares > 0 else 0.0
        store.short_ctx[idx] = (
            si_pct,
            float(row.get("days_to_cover") or 0.0),
            sv_map.get(ticker, 0.0),
        )
        filled += 1
    for ticker, ratio in sv_map.items():  # SV-only names still get the ratio
        idx = store.index.get(ticker)
        if idx is not None and store.short_ctx[idx][2] == 0.0:
            store.short_ctx[idx][2] = ratio
    return filled


def _load_52w_highs(universe: set[str]) -> dict[str, float]:
    """52-week high per symbol (4.2 pct_off_52w_high), cached weekly — the
    peak moves slowly, so ~250 grouped-daily calls run at most once a week."""
    cache_path = support_dir() / "scanner2_52w.npz"
    try:
        if cache_path.exists():
            age_days = (time_mod.time() - cache_path.stat().st_mtime) / 86400.0
            if age_days < 7.0:
                data = np.load(cache_path, allow_pickle=False)
                highs = {
                    str(s): float(h) for s, h in zip(data["symbols"], data["highs"], strict=True)
                }
                print(f"52w cache: {len(highs)} symbols ({age_days:.1f}d old)")
                return highs
    except Exception as exc:
        print(f"52w cache unreadable ({exc}) — rebuilding")
    highs: dict[str, float] = {}
    days = trading_days(252)
    print(f"52w build: fetching {len(days)} grouped dailies (weekly refresh)…", flush=True)
    for i, day in enumerate(sorted(days)):
        data = get(f"{API}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}?adjusted=true")
        for row in (data or {}).get("results") or []:
            symbol = row.get("T", "")
            if universe and symbol not in universe:
                continue
            high = float(row.get("h") or 0)
            if high > highs.get(symbol, 0.0):
                highs[symbol] = high
        if (i + 1) % 50 == 0:
            print(f"  52w progress {i + 1}/{len(days)}", flush=True)
    try:
        syms = sorted(highs)
        np.savez_compressed(
            cache_path,
            symbols=np.array(syms),
            highs=np.array([highs[s] for s in syms], dtype=np.float32),
        )
    except Exception as exc:
        print(f"52w cache save failed: {exc}")
    return highs


def main() -> int:
    universe_path = support_dir() / "scanner2_universe.json"
    universe: set[str] = set()
    if universe_path.exists():
        universe = set(json.loads(universe_path.read_text())["symbols"])
        print(f"universe file: {len(universe)} symbols")

    # -- stage 1: grouped dailies → ADV / ATR% / prev_close ------------------
    days = trading_days(DAYS_BACK + 5)
    per_symbol: dict[str, list[tuple[str, float, float, float, float]]] = {}
    for i, day in enumerate(sorted(days)):
        data = get(f"{API}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}?adjusted=true")
        results = (data or {}).get("results") or []
        print(f"grouped {day} → {len(results)} symbols ({i + 1}/{len(days)})", flush=True)
        for row in results:
            symbol = row.get("T", "")
            if universe and symbol not in universe:
                continue
            per_symbol.setdefault(symbol, []).append(
                (
                    day.isoformat(),
                    float(row.get("o") or 0),
                    float(row.get("h") or 0),
                    float(row.get("l") or 0),
                    float(row.get("c") or 0),
                    float(row.get("v") or 0),
                )
            )

    store = BaselineStore()
    store.load()
    symbols = sorted(per_symbol)
    store.ensure(symbols)
    curve_targets = []
    for symbol in symbols:
        series = sorted(per_symbol[symbol])[-DAYS_BACK:]
        if len(series) < 5:
            continue
        vols = [s[5] for s in series]
        closes = [s[4] for s in series]
        ranges = [s[2] - s[3] for s in series]
        row = store.index[symbol]
        store.adv[row] = float(np.mean(vols))
        store.prev_close[row] = closes[-1]
        if closes[-1] > 0:
            store.atr_pct[row] = float(np.mean(ranges[-14:])) / closes[-1] * 100.0
        if store.adv[row] >= CURVE_MIN_ADV and closes[-1] >= CURVE_MIN_PRICE:
            curve_targets.append(symbol)

    # -- stage 1b: 4.2 daily-context (capitulation/washout shape) ------------
    high_52w = _load_52w_highs(universe)
    for symbol in symbols:
        series = sorted(per_symbol[symbol])[-DAYS_BACK:]
        if len(series) < 5:
            continue
        row = store.index[symbol]
        opens = [s[1] for s in series]
        highs = [s[2] for s in series]
        lows = [s[3] for s in series]
        closes = [s[4] for s in series]
        vols = [s[5] for s in series]
        # consecutive red days into today (close < open), capped at window
        red = 0
        for o, c in zip(reversed(opens), reversed(closes), strict=True):
            if c < o:
                red += 1
            else:
                break
        pdv_ratio = vols[-1] / max(float(np.mean(vols)), 1.0)
        day_range = highs[-1] - lows[-1]
        close_loc = (closes[-1] - lows[-1]) / day_range if day_range > 0 else 0.5
        ret_5d = (closes[-1] / closes[-6] - 1.0) * 100.0 if len(closes) >= 6 else 0.0
        peak = max(high_52w.get(symbol, 0.0), max(highs))
        off_52w = (1.0 - closes[-1] / peak) * 100.0 if peak > 0 else 0.0
        store.daily_ctx[row] = (off_52w, float(red), pdv_ratio, close_loc, ret_5d)

    # -- stage 1c: 4.7 squeeze priors (short interest / short volume) --------
    try:
        n_short = _fetch_short_ctx(store, universe)
        print(f"stage 1c done: short context for {n_short} symbols", flush=True)
    except Exception as exc:
        print(f"stage 1c (short context) failed — journals stay zero: {exc}")
    store.save()
    print(
        f"stage 1 done: {len(symbols)} symbols with dailies;"
        f" {len(curve_targets)} qualify for curves",
        flush=True,
    )  # noqa: E501

    # -- stage 2: minute curves for the qualifying set -----------------------
    start = sorted(days)[0].isoformat()
    end = sorted(days)[-1].isoformat()

    def fetch_curve(symbol: str):
        data = get(
            f"{API}/v2/aggs/ticker/{symbol}/range/1/minute/{start}/{end}?adjusted=true&sort=asc&limit=50000"
        )
        results = (data or {}).get("results") or []
        if not results:
            return symbol, None
        by_day: dict[str, np.ndarray] = {}
        for bar in results:
            ts = datetime.fromtimestamp(bar["t"] / 1000.0, UTC).astimezone(ET)
            idx = minute_index(ts)
            if idx is None:
                continue  # overnight/after-hours prints stay OUT (honest axis)
            key = ts.date().isoformat()
            if key not in by_day:
                by_day[key] = np.zeros(MINUTES, dtype=np.float32)
            by_day[key][idx] += float(bar.get("v") or 0)
        if not by_day:
            return symbol, None
        curves = np.array([np.cumsum(v) for v in by_day.values()], dtype=np.float32)
        return symbol, curves.mean(axis=0)

    done = 0
    batch_symbols: list[str] = []
    batch_curves: list[np.ndarray] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(fetch_curve, s): s for s in curve_targets}
        for future in as_completed(futures):
            symbol, curve = future.result()
            done += 1
            if curve is not None:
                batch_symbols.append(symbol)
                batch_curves.append(curve)
            if done % 250 == 0:
                print(f"curves {done}/{len(curve_targets)}", flush=True)

    if batch_symbols:
        rows = np.array([store.index[s] for s in batch_symbols])
        store.curve[rows] = np.array(batch_curves, dtype=np.float32)
        store.days[rows] = float(DAYS_BACK)
        store.save()
    print(f"stage 2 done: {len(batch_symbols)} curves stored → {store.path}", flush=True)
    print("=== SCANNER2 BACKFILL DONE ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
