#!/usr/bin/env python
"""Phase 10.5 round 2: the ORB replay CAMPAIGN — each session's honest
pre-market picks replayed through the high-fidelity simulator: real
EngineCore/PositionActor/RiskEngine + the FULL seven-layer exit system,
against SimBroker's adverse fills. §12 limits apply (3 concurrent, 1% risk).

Usage:
  .venv/bin/python scripts/sim_orb_campaign.py 2025-08-18 2026-08-14 [top_n]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, date, datetime, time

import pandas as pd
from waveapp.research.frames import minute_frame, rth_only
from waveapp.research.history import HistoryStore, PolygonClient
from waveapp.research.progress import finish, report
from waveapp.research.simulator import replay_session
from waveapp.research.stocks_in_play import (
    download_symbol_meta,
    stocks_in_play,
    stocks_in_play_premarket,
    trading_days,
)

from waveapp.broker.base import OrderSide
from waveapp.config import support_dir
from waveapp.engine.actor import PositionSpec
from waveapp.engine.risk import RiskLimits

EQUITY = 100_000.0
RISK_PCT = 1.0  # §12: risk per trade ≤ 1% of equity
POOL_N = 60
OR_END = time(9, 35)


def make_orb_signal(risk_dollars: float, journal: dict):
    """Long-only ORB-5: after the first 5 RTH minutes, first bar whose high
    breaks the opening-range high → market entry, stop at the range low."""

    state: dict[str, dict] = {}

    def signal(symbol: str, bars) -> PositionSpec | None:
        info = state.setdefault(symbol, {"or_high": None, "or_low": None, "done": False})
        if info["done"]:
            return None
        opening = [b for b in bars if _et_time(b) < OR_END]
        if len(opening) < 5:
            return None
        if info["or_high"] is None:
            info["or_high"] = max(b.high for b in opening[:5])
            info["or_low"] = min(b.low for b in opening[:5])
        last = bars[-1]
        if _et_time(last) < OR_END:
            return None
        if last.high > info["or_high"]:
            info["done"] = True
            stop = info["or_low"]
            entry_ref = max(info["or_high"], last.close)
            risk_per_share = max(entry_ref - stop, 0.01)
            qty = int(risk_dollars / risk_per_share)
            # notional cap: a hair-thin opening range must not size into a
            # position the account couldn't actually carry (≤25% of equity)
            qty = min(qty, int(EQUITY * 0.25 / max(entry_ref, 0.01)))
            if qty < 1:
                return None
            journal[symbol] = {"stop": stop, "entry_ref": entry_ref, "qty": qty}
            return PositionSpec(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=qty,
                stop_price=round(stop, 2),
                strategy="ORB",
            )
        return None

    return signal


def _et_time(bar):
    from zoneinfo import ZoneInfo

    return bar.start.astimezone(ZoneInfo("America/New_York")).time()


async def run_campaign(start: date, end: date, top_n: int) -> pd.DataFrame:
    store = HistoryStore()
    report("meta", 0, 1, "ORB simulator: fetching symbol metadata (leveraged-ETF cap)…")
    fetched_meta = download_symbol_meta(store, PolygonClient())
    if fetched_meta:
        print(f"symbol metadata: {fetched_meta} tickers classified")
    days = trading_days(store, start, end)
    trade_rows: list[dict] = []
    for day_index, day in enumerate(days):
        report(
            "replay",
            day_index,
            len(days),
            f"ORB simulator: replaying {day_index}/{len(days)} sessions",
        )
        pool = stocks_in_play(store, day, top_n=POOL_N, min_rvol=1.5)
        if not pool:
            continue
        picks = stocks_in_play_premarket(store, day, pool, top_n=top_n)
        if not picks:
            continue
        frames = {}
        for name in picks:
            frame = rth_only(minute_frame(store, name.symbol, day, day))
            if len(frame) >= 30:
                frames[name.symbol] = frame
        if not frames:
            continue
        journal: dict = {}
        signal = make_orb_signal(EQUITY * RISK_PCT / 100.0, journal)
        result = await replay_session(
            frames,
            signal,
            equity=EQUITY,
            limits=RiskLimits(),  # §12 defaults: 3 concurrent, 1%
        )
        # pair fills into per-symbol trades
        by_symbol: dict[str, dict] = {}
        for fill in result.fills:
            entry = by_symbol.setdefault(
                fill.symbol, {"bought": 0.0, "cost": 0.0, "sold": 0.0, "proceeds": 0.0, "fees": 0.0}
            )
            entry["fees"] += fill.fees
            if fill.side is OrderSide.BUY:
                entry["bought"] += fill.qty
                entry["cost"] += fill.qty * fill.price
            else:
                entry["sold"] += fill.qty
                entry["proceeds"] += fill.qty * fill.price
        for symbol, book in by_symbol.items():
            if book["bought"] == 0 or symbol not in journal:
                continue
            leftover = book["bought"] - book["sold"]  # should be 0 (conservation)
            mark = frames[symbol].iloc[-1]["close"] if symbol in frames else 0.0
            pnl = book["proceeds"] - book["cost"] - book["fees"] + leftover * mark
            meta = journal[symbol]
            risk_dollars = meta["qty"] * (meta["entry_ref"] - meta["stop"])
            trade_rows.append(
                {
                    "day": day.isoformat(),
                    "symbol": symbol,
                    "qty": book["bought"],
                    "pnl": round(pnl, 2),
                    "fees": round(book["fees"], 4),
                    "risk_dollars": round(risk_dollars, 2),
                    "r_multiple": round(pnl / risk_dollars, 4) if risk_dollars > 0 else 0.0,
                }
            )
    return pd.DataFrame(trade_rows)


def main() -> int:
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])
    top_n = int(sys.argv[3]) if len(sys.argv) > 3 else 20

    trades = asyncio.run(run_campaign(start, end, top_n))
    out_dir = support_dir() / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    out = out_dir / f"sim_orb_{stamp}.csv"
    trades.to_csv(out, index=False)

    if trades.empty:
        finish("ORB simulator DONE — no trades")
        return 0
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]
    profit_factor = (
        wins["pnl"].sum() / -losses["pnl"].sum()
        if len(losses) and losses["pnl"].sum() < 0
        else float("inf")
    )
    expectancy_r = trades["r_multiple"].mean()
    daily = trades.groupby("day")["pnl"].sum().cumsum()
    peak = daily.cummax()
    max_dd = float((daily - peak).min())
    summary = (
        f"trades {len(trades)} | win {len(wins) / len(trades):.1%}"
        f" | PF {profit_factor:.2f} | expectancy {expectancy_r:+.3f}R"
        f" | net ${daily.iloc[-1]:,.0f} | maxDD ${max_dd:,.0f}"
        f" | fees ${trades['fees'].sum():,.0f}"
    )
    print(summary)
    print(f"per-trade rows → {out}")
    finish(f"ORB simulator DONE — {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
