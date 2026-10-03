#!/usr/bin/env python
"""Phase 10.12: THE PORTFOLIO CAMPAIGN — GAPGO + ORB + VWAP together through
the high-fidelity simulator: one engine per session, shared §12 limits
(3 concurrent, 1% risk, notional cap), trail-only exits, long-only v1,
one position per symbol per day (whichever family triggers first).

Usage: .venv/bin/python scripts/sim_portfolio_campaign.py [start] [end] [min_price]
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
MIN_PRICE = float(os.environ.get("WAVE_MIN_PRICE", "0"))
EQUITY = 100_000.0
RISK_PCT = 1.0
NOTIONAL_CAP = 0.25
OR_END = time(9, 35)
# The pre-2026-09-03 champion exit set, pinned EXPLICITLY: the engine's
# DEFAULT_PARAMS became the bare kitchen at adoption, so every research
# baseline that means "the old champion" must carry all its values itself
# (recovery/loiter/recross used to ride in via the engine defaults).
TRAIL_ONLY = {
    "k_trail": 3.5,
    "t_max_minutes": 90,
    "k_t1": 999.0,
    "k_be": 999.0,
    "volume_death_fraction": 0.0,
    "recovery_depth_atr": 0.75,
    "recovery_minutes": 20,
    "recovery_exit_atr": 0.3,
    "loiter_depth_pct": 1.0,
    "loiter_minutes": 30,
    "vwap_recross_exit": True,
}


def _replay_day(
    day_iso: str, symbols: list[str], meta_day: dict, exit_overrides: dict | None = None
) -> list[dict]:
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
        if len(frame) >= 60 and float(frame.iloc[0]["open"]) >= MIN_PRICE:
            frames[symbol] = frame
    store.close()
    if not frames:
        return []

    overrides = {**TRAIL_ONLY, **(exit_overrides or {})}

    def exit_params_fn(regime):
        return dc_replace(DEFAULT_PARAMS[regime], **overrides)

    ET = ZoneInfo("America/New_York")
    journal: dict = {}
    state: dict[str, dict] = {}

    def _spec(symbol, entry_ref, stop, strategy):
        if stop <= 0.01:  # cheap name + wide ATR: no sane stop → no trade
            return None
        risk_per_share = max(entry_ref - stop, 0.01)
        qty = int(EQUITY * RISK_PCT / 100.0 / risk_per_share)
        qty = min(qty, int(EQUITY * NOTIONAL_CAP / max(entry_ref, 0.01)))
        if qty < 1:
            return None
        journal[symbol] = {
            "stop": stop,
            "entry_ref": entry_ref,
            "qty": qty,
            "strategy": strategy,
        }
        return PositionSpec(
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            stop_price=round(stop, 2),
            strategy=strategy,
        )

    def signal(symbol, bars):
        info = state.setdefault(
            symbol, {"hi": None, "lo": None, "done": False, "vwap_n": 0.0, "vwap_v": 0.0}
        )
        if info["done"]:
            return None
        meta = meta_day.get(symbol)
        gap_pct, atr = meta if meta else (0.0, 0.0)
        last = bars[-1]
        et_time = last.start.astimezone(ET).time()
        # session VWAP accumulator
        typical = (last.high + last.low + last.close) / 3.0
        info["vwap_n"] += typical * last.volume
        info["vwap_v"] += last.volume
        vwap = info["vwap_n"] / info["vwap_v"] if info["vwap_v"] > 0 else last.close

        # GAPGO: first RTH bar, gap up >= 3%
        if len(bars) == 1 and gap_pct >= 3.0 and atr > 0:
            info["done"] = True
            return _spec(symbol, last.close, last.close - atr, "GAPGO")
        # opening range for ORB
        if et_time < OR_END:
            if info["hi"] is None:
                info["hi"], info["lo"] = last.high, last.low
            else:
                info["hi"] = max(info["hi"], last.high)
                info["lo"] = min(info["lo"], last.low)
            return None
        # ORB long breakout
        if info["hi"] is not None and last.high > info["hi"]:
            info["done"] = True
            return _spec(symbol, max(info["hi"], last.close), info["lo"], "ORB")
        # VWAP-trend pullback (gap >= 2.5%, after 10:00)
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

    result = asyncio.run(
        replay_session(
            frames, signal, equity=EQUITY, limits=RiskLimits(), exit_params_fn=exit_params_fn
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
        meta = journal[symbol]
        rows.append(
            {
                "day": day_iso,
                "symbol": symbol,
                "strategy": meta["strategy"],
                "pnl": round(pnl, 2),
                "risk_dollars": round(meta["qty"] * (meta["entry_ref"] - meta["stop"]), 2),
                "leftover": leftover,
            }
        )
    return rows


def main() -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed

    start = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 2 else FULL[0]
    end = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else FULL[1]
    out_dir = support_dir() / "research"
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    picks = build_picks(start, end, stage="pf-picks")
    symbols = set()
    for day_symbols in picks.values():
        symbols.update(day_symbols)
    report("pf-meta", 0, 1, "Portfolio sim: daily meta…")
    meta = daily_meta(symbols, start, end)

    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=8) as pool:
        futures = {}
        for day_iso, day_symbols in picks.items():
            meta_day = {s: meta[(s, day_iso)] for s in day_symbols if (s, day_iso) in meta}
            futures[pool.submit(_replay_day, day_iso, day_symbols, meta_day)] = day_iso
        for done, future in enumerate(as_completed(futures)):
            rows.extend(future.result())
            if done % 10 == 0:
                report("pf", done, len(futures), f"Portfolio sim: {done}/{len(futures)} sessions")

    trades = pd.DataFrame(rows)
    trades.to_csv(out_dir / f"portfolio_sim_{stamp}.csv", index=False)
    if trades.empty:
        finish("Portfolio sim DONE — no trades")
        return 0
    trades["month"] = trades["day"].str[:7]
    risk_unit = EQUITY * RISK_PCT / 100.0
    wins = trades[trades.pnl > 0]
    losses = trades[trades.pnl <= 0]
    daily = trades.groupby("day")["pnl"].sum()
    curve = daily.cumsum()
    summary = (
        f"trades {len(trades)} | win {len(wins) / len(trades):.1%}"
        f" | PF {wins.pnl.sum() / -losses.pnl.sum():.2f}"
        f" | expectancy {trades.pnl.mean() / risk_unit:+.3f}R"
        f" | net ${curve.iloc[-1]:,.0f} | maxDD ${(curve - curve.cummax()).min():,.0f}"
        f" | leftovers {int((trades.leftover != 0).sum())}"
    )
    print(summary)
    print("\nby strategy:")
    print(trades.groupby("strategy")["pnl"].agg(["count", "sum", "mean"]).round(1).to_string())
    print("\nby month:")
    print(trades.groupby("month")["pnl"].sum().round(0).to_string())
    finish(f"Portfolio sim DONE — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
