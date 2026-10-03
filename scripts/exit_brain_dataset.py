#!/usr/bin/env python
"""EXIT-BRAIN DATASET builder (blueprint §II.11 finding #2 / kitchen stage 5).

Turns the journal's closed trades into POSITION-MINUTE training rows for the
future ML exit gate: for every minute a position was open, the features the
gate would have seen at that minute plus a FORWARD triple-barrier label —
"from THIS minute, did the position later gain >= +0.25R more before
retracing an additional 0.5R from the current level?" (1 = more profit was
ahead, 0 = this was effectively the peak). Direction, stops and the engine
are untouched — this script only reads the journal and writes
`exit_brain_rows` in the paper DB. DATASET ONLY; no model training here.

R normalization: risk-per-share = |avg_entry - stop| from the earliest
recorded stop order of the position. Fallback when no stop row exists
(documented): risk = 1.5 x ATR(14) on the 1-min bars at/before entry —
1.5 matches the champion k_stop (and labeler.DOWN_ATR), so R units stay
comparable across both sources.

Minute paths come from the local HistoryStore first, with a Polygon
fallback for trades newer than the store (same pattern as
scripts/research_journal_mine.py). Idempotent: INSERT OR REPLACE on
(position_uuid, minute_index) — re-runs update in place.

Usage: nice -n 19 .venv/bin/python scripts/exit_brain_dataset.py
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from waveapp.research.history import HistoryStore, MinuteBar

DB = Path.home() / "Library/Application Support/Wave/wave_paper.db"

GAIN_AHEAD_R = 0.25  # forward upper barrier: "more profit ahead" (§II.11 #2)
GIVEBACK_R = 0.50  # forward lower barrier: additional retrace from here
K_STOP_ATR = 1.5  # fallback risk multiple — matches champion k_stop
ATR_PERIOD = 14  # fast intraday ATR(14) on 1-min bars (§8.2)

_polygon_client = None
_polygon_cache: dict[tuple[str, str], list[MinuteBar]] = {}


# -- pure math (unit-tested in tests/test_exit_brain_dataset.py) -------------


def atr_fallback_risk(day_bars: list[MinuteBar], entry_ms: float) -> float | None:
    """No stop recorded → risk = K_STOP_ATR x ATR(ATR_PERIOD) on the 1-min
    bars at/before entry (true-range mean over the last 14 pre-entry bars;
    fewer bars → mean of what exists). None when it cannot be computed."""
    prior = [b for b in day_bars if b.ts_ms <= entry_ms]
    if len(prior) < 2:
        return None
    true_ranges = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(prior, prior[1:], strict=False)
    ]
    window = true_ranges[-ATR_PERIOD:]
    atr = sum(window) / len(window)
    return K_STOP_ATR * atr if atr > 0 else None


def resolve_risk(
    entry: float | None,
    stop_price: float | None,
    day_bars: list[MinuteBar],
    entry_ms: float,
) -> float | None:
    """The R denominator (dollars per share). Recorded server-side stop wins:
    |entry - stop|; otherwise the documented 1.5xATR(14) fallback."""
    if entry is None or entry <= 0:
        return None
    if stop_price is not None and abs(entry - float(stop_price)) > 0:
        return abs(entry - float(stop_price))
    return atr_fallback_risk(day_bars, entry_ms)


def forward_label(unreal: list[float], fav: list[float], adv: list[float], i: int) -> int:
    """Forward triple-barrier from minute i over the remaining HELD path:
    1 if favorable excursion reaches unreal[i] + GAIN_AHEAD_R before adverse
    excursion reaches unreal[i] - GIVEBACK_R; 0 on giveback-first, on both
    barriers inside one bar (conservative — never flatter the dataset, same
    convention as waveapp/engine/labeler.py), or if the position closed
    before either barrier (no more profit materialized)."""
    up = unreal[i] + GAIN_AHEAD_R
    down = unreal[i] - GIVEBACK_R
    for j in range(i + 1, len(unreal)):
        if adv[j] <= down:
            return 0
        if fav[j] >= up:
            return 1
    return 0


def build_rows(
    day_bars: list[MinuteBar],
    opened_ms: float,
    closed_ms: float,
    entry: float,
    side: str,
    risk: float | None,
) -> list[dict]:
    """Per-minute feature+label rows for one closed trade. Pure math.

    unrealized_r is close-based; peak_r is the running max of the favorable
    excursion (high for longs, low for shorts); retrace_frac and the forward
    label follow the blueprint definitions. Session VWAP is cumulative
    typical-price x volume over ALL of the day's bars up to each minute
    (deterministic — independent of the feed's vwap column); vwap_dist_r is
    the spec-literal (price - vwap) / risk, unsigned by side."""
    if risk is None or risk <= 0 or entry <= 0:
        return []
    held = [b for b in day_bars if opened_ms <= b.ts_ms <= closed_ms]
    if len(held) < 2:
        return []
    sign = -1.0 if side == "short" else 1.0

    vwap_at: dict[int, float] = {}
    price_volume = 0.0
    volume_sum = 0.0
    for bar in day_bars:
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = bar.volume or 0.0
        price_volume += typical * vol
        volume_sum += vol
        vwap_at[bar.ts_ms] = (price_volume / volume_sum) if volume_sum > 0 else bar.close

    unreal = [sign * (b.close - entry) / risk for b in held]
    fav = [sign * ((b.high if sign > 0 else b.low) - entry) / risk for b in held]
    adv = [sign * ((b.low if sign > 0 else b.high) - entry) / risk for b in held]

    rows: list[dict] = []
    peak = float("-inf")
    peak_idx = 0
    entry_volume = held[0].volume or 0.0
    for i, bar in enumerate(held):
        if fav[i] > peak:  # strictly better → the peak was extended here
            peak = fav[i]
            peak_idx = i
        retrace = (1.0 - unreal[i] / peak) if peak > 0 else 0.0
        rows.append(
            {
                "minute_index": i,
                "ts": datetime.fromtimestamp(bar.ts_ms / 1000, tz=UTC).isoformat(
                    timespec="seconds"
                ),
                "unrealized_r": round(unreal[i], 4),
                "peak_r": round(peak, 4),
                "retrace_frac": round(retrace, 4),
                "minutes_since_peak": i - peak_idx,
                "vwap_dist_r": round((bar.close - vwap_at[bar.ts_ms]) / risk, 4),
                "vol_ratio": round((bar.volume or 0.0) / entry_volume, 4)
                if entry_volume > 0
                else 0.0,
                "label_more_ahead": forward_label(unreal, fav, adv, i),
            }
        )
    return rows


# -- data access (HistoryStore first, Polygon fallback — journal_mine style) --


def _polygon_bars(symbol: str, day_iso: str) -> list[MinuteBar]:
    """Minute bars via Polygon — the history store ends at the sweep windows;
    live trades are newer. Cached per (symbol, day)."""
    global _polygon_client
    key = (symbol, day_iso)
    if key not in _polygon_cache:
        try:
            if _polygon_client is None:
                from waveapp.research.history import PolygonClient

                _polygon_client = PolygonClient()
            day = date.fromisoformat(day_iso)
            _polygon_cache[key] = _polygon_client.fetch_minute_bars(symbol, day, day)
        except Exception:
            _polygon_cache[key] = []
    return _polygon_cache[key]


def _trade_day_bars(
    store: HistoryStore, symbol: str, opened: datetime, opened_ms: float, closed_ms: float
) -> list[MinuteBar]:
    """The whole trading day's minute bars (UTC-day window, like
    research_journal_mine.py). Fewer than 2 bars inside the held window →
    the store doesn't cover this trade yet → Polygon."""
    day_bars = store.load_bars(symbol, opened.date(), opened.date())
    held = [b for b in day_bars if opened_ms <= b.ts_ms <= closed_ms]
    if len(held) < 2:
        day_bars = _polygon_bars(symbol, opened.date().isoformat())
    return day_bars


def _stop_price(database, position_uuid: str) -> float | None:
    """The position's earliest recorded stop order — the initial server-side
    protective stop (hard rule 3). None if the journal has no stop row."""
    rows = database.query(
        "SELECT stop_price FROM orders WHERE position_uuid = ? AND stop_price IS NOT NULL"
        " ORDER BY COALESCE(submitted_at, updated_at, '') LIMIT 1",
        (position_uuid,),
    )
    return float(rows[0]["stop_price"]) if rows else None


# -- the builder -------------------------------------------------------------

INSERT_SQL = (
    "INSERT OR REPLACE INTO exit_brain_rows"
    " (position_uuid, symbol, strategy, minute_index, ts, unrealized_r, peak_r,"
    "  retrace_frac, minutes_since_peak, vwap_dist_r, vol_ratio, label_more_ahead,"
    "  created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def run(database, store: HistoryStore) -> dict:
    """Process every closed trade in `database` into exit_brain_rows.
    Returns the summary dict (also printed by main)."""
    from waveapp.persistence.db import utc_now

    trades = database.query(
        "SELECT position_uuid, symbol, side, strategy, avg_entry, opened_at, closed_at"
        " FROM positions WHERE state != 'superseded' AND avg_entry IS NOT NULL"
        " AND opened_at IS NOT NULL AND closed_at IS NOT NULL"
        " AND realized_pnl IS NOT NULL AND qty != 0"
        " AND COALESCE(exit_path, '') != 'entry canceled'"
        " ORDER BY opened_at"
    )
    created = utc_now()
    processed = 0
    skipped = 0
    total_rows = 0
    label_ones = 0
    per_strategy: dict[str, int] = {}
    for trade in trades:
        opened = datetime.fromisoformat(str(trade["opened_at"])).astimezone(UTC)
        closed = datetime.fromisoformat(str(trade["closed_at"])).astimezone(UTC)
        opened_ms = opened.timestamp() * 1000
        closed_ms = closed.timestamp() * 1000
        symbol = str(trade["symbol"])
        day_bars = _trade_day_bars(store, symbol, opened, opened_ms, closed_ms)
        entry = float(trade["avg_entry"])
        risk = resolve_risk(
            entry, _stop_price(database, trade["position_uuid"]), day_bars, opened_ms
        )
        rows = build_rows(day_bars, opened_ms, closed_ms, entry, str(trade["side"]), risk)
        if not rows:
            skipped += 1
            continue
        strategy = str(trade["strategy"] or "")
        database.executemany(
            INSERT_SQL,
            [
                (
                    trade["position_uuid"],
                    symbol,
                    strategy,
                    row["minute_index"],
                    row["ts"],
                    row["unrealized_r"],
                    row["peak_r"],
                    row["retrace_frac"],
                    row["minutes_since_peak"],
                    row["vwap_dist_r"],
                    row["vol_ratio"],
                    row["label_more_ahead"],
                    created,
                )
                for row in rows
            ],
        )
        processed += 1
        total_rows += len(rows)
        label_ones += sum(row["label_more_ahead"] for row in rows)
        per_strategy[strategy or "?"] = per_strategy.get(strategy or "?", 0) + len(rows)
    return {
        "trades_seen": len(trades),
        "trades_processed": processed,
        "trades_skipped": skipped,
        "rows_written": total_rows,
        "p_more_ahead": (label_ones / total_rows) if total_rows else 0.0,
        "rows_per_strategy": per_strategy,
    }


def main() -> int:
    from waveapp.persistence.db import Database

    database = Database(DB)  # migrations run on open → exit_brain_rows exists
    store = HistoryStore()
    summary = run(database, store)
    print("== EXIT-BRAIN DATASET ==")
    print(
        f"trades: {summary['trades_processed']} processed"
        f" / {summary['trades_skipped']} skipped (no path or no risk)"
        f" of {summary['trades_seen']} closed"
    )
    print(f"rows written: {summary['rows_written']}")
    print(f"label balance P(more_ahead): {summary['p_more_ahead']:.1%}")
    print("rows per strategy:")
    for strategy, count in sorted(summary["rows_per_strategy"].items()):
        print(f"  {strategy:>8}: {count}")
    database.close()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
