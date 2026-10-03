#!/usr/bin/env python
"""Phase 10.13: SLEEVE PORTFOLIO through the engine with the evidence-based
cost model — each family runs its OWN engine (no starvation), EDGE-estimated
effective spreads per symbol-day, price-improvement sensitivity.

Runs: GAPGO / ORB / VWAP solo sleeves at pi=1.0 (pay the full EDGE effective
half-spread — the tape-average, NO extra optimism), plus the combined sleeve
portfolio, plus pi ∈ {1.25, 0.6} sensitivity on the combined result.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, date, datetime, time

import pandas as pd
from waveapp.research.progress import finish, report

from waveapp.config import support_dir

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from research_exits import build_picks  # noqa: E402
from research_vwap import daily_meta  # noqa: E402

FULL = (date(2025, 8, 18), date(2026, 8, 14))
EQUITY = 100_000.0
RISK_PCT = 1.0
NOTIONAL_CAP = 0.25
OR_END = time(9, 35)
TRAIL_ONLY = {
    "k_trail": 3.5,
    "t_max_minutes": 90,
    "k_t1": 999.0,
    "k_be": 999.0,
    "volume_death_fraction": 0.0,
}
PI = float(os.environ.get("WAVE_PI", "1.0"))
FAMILY = os.environ.get("WAVE_FAMILY", "GAPGO")


def _replay_day(day_iso: str, symbols: list[str], meta_day: dict, spreads: dict) -> list[dict]:
    from dataclasses import replace as dc_replace
    from zoneinfo import ZoneInfo

    from waveapp.research.frames import minute_frame, rth_only
    from waveapp.research.history import HistoryStore
    from waveapp.research.simulator import replay_session

    from waveapp.broker.base import OrderSide
    from waveapp.engine.actor import PositionSpec
    from waveapp.engine.exits import DEFAULT_PARAMS
    from waveapp.engine.risk import RiskLimits

    day = date.fromisoformat(day_iso)
    store = HistoryStore()
    frames = {}
    for symbol in symbols:
        frame = rth_only(minute_frame(store, symbol, day, day))
        if len(frame) >= 60:
            frames[symbol] = frame
    store.close()
    if not frames:
        return []

    def exit_params_fn(regime):
        return dc_replace(DEFAULT_PARAMS[regime], **TRAIL_ONLY)

    ET = ZoneInfo("America/New_York")
    journal: dict = {}
    state: dict[str, dict] = {}

    def _spec(symbol, entry_ref, stop, strategy):
        if stop <= 0.01:
            return None
        risk_per_share = max(entry_ref - stop, 0.01)
        qty = int(EQUITY * RISK_PCT / 100.0 / risk_per_share)
        qty = min(qty, int(EQUITY * NOTIONAL_CAP / max(entry_ref, 0.01)))
        if qty < 1:
            return None
        journal[symbol] = {"stop": stop, "entry_ref": entry_ref, "qty": qty}
        return PositionSpec(
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            stop_price=round(stop, 2),
            strategy=strategy,
        )

    def signal(symbol, bars):
        info = state.setdefault(
            symbol, {"hi": None, "lo": None, "done": False, "vn": 0.0, "vv": 0.0}
        )
        if info["done"]:
            return None
        meta = meta_day.get(symbol)
        gap_pct, atr = meta if meta else (0.0, 0.0)
        last = bars[-1]
        et_time = last.start.astimezone(ET).time()
        typical = (last.high + last.low + last.close) / 3.0
        info["vn"] += typical * last.volume
        info["vv"] += last.volume
        vwap = info["vn"] / info["vv"] if info["vv"] > 0 else last.close

        if FAMILY == "GAPGO":
            if len(bars) == 1 and gap_pct >= 3.0 and atr > 0:
                info["done"] = True
                return _spec(symbol, last.close, last.close - atr, "GAPGO")
            return None
        if FAMILY == "GAPGO_LATE":
            # evidence (McInish/Wood + ETF studies): 09:30-09:35 spreads are
            # 2-5x midday, normalized by ~09:45 — same signal, enter at 09:40
            if len(bars) == 10 and gap_pct >= 3.0 and atr > 0:
                info["done"] = True
                return _spec(symbol, last.close, last.close - atr, "GAPGO_LATE")
            return None
        if et_time < OR_END:
            if info["hi"] is None:
                info["hi"], info["lo"] = last.high, last.low
            else:
                info["hi"] = max(info["hi"], last.high)
                info["lo"] = min(info["lo"], last.low)
            return None
        if FAMILY == "ORB":
            if info["hi"] is not None and last.high > info["hi"]:
                info["done"] = True
                return _spec(symbol, max(info["hi"], last.close), info["lo"], "ORB")
            return None
        if FAMILY == "VWAP":
            if (
                gap_pct >= 2.5
                and atr > 0
                and et_time >= time(10, 0)
                and last.close > vwap
                and abs(last.close - vwap) <= 0.6 * atr
            ):
                info["done"] = True
                return _spec(symbol, last.close, vwap - 1.25 * atr, "VWAP")
            return None
        return None

    result = asyncio.run(
        replay_session(
            frames,
            signal,
            equity=EQUITY,
            limits=RiskLimits(),
            exit_params_fn=exit_params_fn,
            spread_fracs=spreads,
            pi_factor=PI,
        )
    )
    from waveapp.broker.base import OrderSide as _Side

    by_symbol: dict[str, dict] = {}
    for fill in result.fills:
        book = by_symbol.setdefault(
            fill.symbol, {"bought": 0.0, "cost": 0.0, "sold": 0.0, "proceeds": 0.0, "fees": 0.0}
        )
        book["fees"] += fill.fees
        if fill.side is _Side.BUY:
            book["bought"] += fill.qty
            book["cost"] += fill.qty * fill.price
        else:
            book["sold"] += fill.qty
            book["proceeds"] += fill.qty * fill.price
    rows = []
    for symbol, book in by_symbol.items():
        if book["bought"] == 0 or symbol not in journal:
            continue
        leftover = book["bought"] - book["sold"]
        mark = frames[symbol].iloc[-1]["close"] if symbol in frames else 0.0
        pnl = book["proceeds"] - book["cost"] - book["fees"] + leftover * mark
        rows.append(
            {
                "day": day_iso,
                "symbol": symbol,
                "strategy": FAMILY,
                "pnl": round(pnl, 2),
                "leftover": leftover,
            }
        )
    return rows


def main() -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from waveapp.research.history import HistoryStore

    out_dir = support_dir() / "research"
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    picks = build_picks(*FULL, stage="sleeve-picks")
    symbols = set()
    for day_symbols in picks.values():
        symbols.update(day_symbols)
    report("sleeve-meta", 0, 1, f"Sleeve {FAMILY} pi={PI}: meta…")
    meta = daily_meta(symbols, *FULL)

    store = HistoryStore()
    spread_rows = store._conn.execute(
        "SELECT symbol, day, spread_frac FROM spread_estimates WHERE spread_frac IS NOT NULL"
    ).fetchall()
    store.close()
    spreads_by_day: dict[str, dict[str, float]] = {}
    for symbol, day_iso, frac in spread_rows:
        spreads_by_day.setdefault(day_iso, {})[symbol] = frac

    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=8) as pool:
        futures = {}
        for day_iso, day_symbols in picks.items():
            meta_day = {s: meta[(s, day_iso)] for s in day_symbols if (s, day_iso) in meta}
            futures[
                pool.submit(
                    _replay_day, day_iso, day_symbols, meta_day, spreads_by_day.get(day_iso, {})
                )
            ] = day_iso
        for done, future in enumerate(as_completed(futures)):
            rows.extend(future.result())
            if done % 15 == 0:
                report(
                    "sleeve",
                    done,
                    len(futures),
                    f"Sleeve {FAMILY} pi={PI}: {done}/{len(futures)} sessions",
                )

    trades = pd.DataFrame(rows)
    tag = f"{FAMILY}_pi{PI}"
    trades.to_csv(out_dir / f"sleeve_{tag}_{stamp}.csv", index=False)
    if trades.empty:
        finish(f"Sleeve {tag} DONE — no trades")
        return 0
    wins = trades[trades.pnl > 0]
    losses = trades[trades.pnl <= 0]
    daily = trades.groupby("day")["pnl"].sum()
    curve = daily.cumsum()
    summary = (
        f"{tag}: trades {len(trades)} | win {len(wins) / len(trades):.1%}"
        f" | PF {wins.pnl.sum() / max(-losses.pnl.sum(), 1e-9):.2f}"
        f" | net ${curve.iloc[-1]:,.0f} | maxDD ${(curve - curve.cummax()).min():,.0f}"
        f" | leftovers {int((trades.leftover != 0).sum())}"
    )
    print(summary)
    finish(f"Sleeve DONE — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
