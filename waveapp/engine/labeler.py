"""Candidate outcome labeler (§9.2/§10.3 — agenda #3 redo, step 3).

The scanner journals every candidate's FEATURES; ML can only be judged once
each candidate also carries its OUTCOME — what the stock actually did after
the moment Wave looked at it. This module holds the pure math: the
triple-barrier walk (López de Prado, §9.2) over the day's 1-minute bars:

- upper barrier: entry + UP_ATR × ATR   (the profit zone)
- lower barrier: entry − DOWN_ATR × ATR (the champion's stop distance)
- vertical barrier: the session's end

The stored outcome keeps MFE/MAE excursions in ATR units, so labels can be
RE-derived later for any barrier choice without refetching bars. Labels
never gate trading — the mode switch is manual.
"""

from __future__ import annotations

UP_ATR = 1.0  # profit barrier (the 0.3–0.5% zone, ATR-scaled — §1)
DOWN_ATR = 1.5  # stop barrier — matches the champion k_stop


def compute_outcome(
    bars: list[tuple[float, float, float, float, float]],
    entry_ts: float,
    atr: float,
) -> dict | None:
    """Label one candidate from its day's 1-minute bars.

    bars: [(epoch_ts, open, high, low, close), ...] for the whole session;
    entry_ts: when the candidate was journaled; atr: dollars per share.
    Returns the outcome dict, or None when it cannot be labeled honestly
    (no bars after entry, or a degenerate ATR).
    """
    if atr <= 0:
        return None
    after = [b for b in bars if b[0] >= entry_ts]
    if len(after) < 5:  # too little session left to say anything
        return None
    entry = float(after[0][1])  # the next bar's open — no lookahead
    if entry <= 0:
        return None
    upper = entry + UP_ATR * atr
    lower = entry - DOWN_ATR * atr
    label = "flat"
    max_high = entry
    min_low = entry
    for _ts, _open, high, low, _close in after:
        max_high = max(max_high, float(high))
        min_low = min(min_low, float(low))
        hit_up = float(high) >= upper
        hit_down = float(low) <= lower
        if label == "flat" and (hit_up or hit_down):
            # both barriers inside one bar: unknowable order → count it a
            # loss (conservative — never flatter the dataset)
            label = "loss" if hit_down else "win"
    close = float(after[-1][4])
    return {
        "entry": round(entry, 4),
        "mfe_atr": round((max_high - entry) / atr, 3),
        "mae_atr": round((entry - min_low) / atr, 3),
        "close_atr": round((close - entry) / atr, 3),
        "bars_after": len(after),
        "label": label,
    }


def compute_outcome_multi(
    bars: list[tuple[float, float, float, float, float]],
    entry_ts: float,
    atr: float,
) -> dict | None:
    """Blueprint 8.1: as-of-snapshot triple-barrier at THREE vertical
    barriers — 30 min, 60 min, session end — from one bar walk. Barriers
    start at the snapshot's next bar open (no lookahead); the horizon labels
    let v2 answer "tradable from NOW", which is the intraday question.
    Same conservative rule as compute_outcome: both barriers inside one bar
    = loss (never flatter the dataset)."""
    if atr <= 0:
        return None
    after = [b for b in bars if b[0] >= entry_ts]
    if len(after) < 5:
        return None
    entry = float(after[0][1])
    if entry <= 0:
        return None
    upper = entry + UP_ATR * atr
    lower = entry - DOWN_ATR * atr
    horizon_ends = {"30m": entry_ts + 30 * 60, "60m": entry_ts + 60 * 60}
    labels = {"30m": "flat", "60m": "flat", "sess": "flat"}
    decided = {"30m": False, "60m": False, "sess": False}
    max_high = entry
    min_low = entry
    for ts, _open, high, low, _close in after:
        max_high = max(max_high, float(high))
        min_low = min(min_low, float(low))
        hit_up = float(high) >= upper
        hit_down = float(low) <= lower
        if hit_up or hit_down:
            verdict = "loss" if hit_down else "win"
            for key in labels:
                horizon_end = horizon_ends.get(key)
                if not decided[key] and (horizon_end is None or ts <= horizon_end):
                    labels[key] = verdict
                    decided[key] = True
        for key, horizon_end in horizon_ends.items():
            if not decided[key] and ts > horizon_end:
                decided[key] = True  # horizon expired flat
    return {
        "entry": round(entry, 4),
        "label_30m": labels["30m"],
        "label_60m": labels["60m"],
        "label_sess": labels["sess"],
        "mfe_atr": round((max_high - entry) / atr, 3),
        "mae_atr": round((entry - min_low) / atr, 3),
        "bars_after": len(after),
    }
