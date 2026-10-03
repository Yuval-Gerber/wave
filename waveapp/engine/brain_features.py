"""Brain feature bridge (Stage M1, 2026-09-01): ONE builder for the v1
feature vector, fed from live state. The point-in-time rule: whatever scores
live must be built by the same code path the trainer will use for v2 —
train/live feature skew is the classic silent killer, so the exact vector is
journaled with every score for parity audits.

Known v1 gaps (documented, not hidden): prior_day_up and vol_5d are not
derivable from live state → 0.0 (the missing-feature convention the Brain
documents). v2 trains on the journaled LIVE vectors, closing the gap.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

STRATEGY_ID = {"ORB": 0, "GAPGO": 1, "PMOM": 2, "VWAP": 3, "REENTRY": 4, "FPB": 5}

# blueprint 8.4: the v2 live-features model's column order — FROZEN. The
# trainer and the live scorer both build rows through v2_vector below, so
# the order can never skew between them. Time-of-day features are explicit
# (minute_of_day, dow) per the U-shape evidence; rvol is already the
# time-anchored same-minute-curve ratio at journal time.
V2_FEATURE_COLS: list[str] = [
    "score",
    "rvol",
    "gap",
    "day",
    "vol",
    "px",
    "hod_dist",
    "vwap_dist",
    "spread_pct",
    "events",
    "news",
    "news_dir",
    "news_nov",
    "shares_out_m",
    "atr_pct",
    "off_52w",
    "red_days",
    "pdv_ratio",
    "pd_close_loc",
    "ret_5d",
    "minute_of_day",
    "dow",
]


def v2_vector(feats: dict, ts_et: datetime) -> dict[str, float]:
    """One journaled snapshot dict (+ its ET timestamp) → the v2 model row.
    Same function at train time and score time (the point-in-time rule)."""
    px = float(feats.get("px", 0.0) or 0.0)
    spread = float(feats.get("spread", 0.0) or 0.0)
    shares = float(feats.get("shares_out", 0.0) or 0.0)
    return {
        "score": float(feats.get("score", 0.0) or 0.0),
        "rvol": float(feats.get("rvol", 0.0) or 0.0),
        "gap": float(feats.get("gap", 0.0) or 0.0),
        "day": float(feats.get("day", 0.0) or 0.0),
        "vol": float(feats.get("vol", 0.0) or 0.0),
        "px": px,
        "hod_dist": float(feats.get("hod_dist", 0.0) or 0.0),
        "vwap_dist": float(feats.get("vwap_dist", 0.0) or 0.0),
        "spread_pct": (spread / px * 100.0) if px > 0 else 0.0,
        "events": float(feats.get("events", 0.0) or 0.0),
        "news": float(feats.get("news", 0) or 0),
        "news_dir": float(feats.get("news_dir", 0) or 0),
        "news_nov": float(feats.get("news_nov", 0.0) or 0.0),
        "shares_out_m": shares / 1e6,
        "atr_pct": float(feats.get("atr_pct", 0.0) or 0.0),
        "off_52w": float(feats.get("off_52w", 0.0) or 0.0),
        "red_days": float(feats.get("red_days", 0.0) or 0.0),
        "pdv_ratio": float(feats.get("pdv_ratio", 0.0) or 0.0),
        "pd_close_loc": float(feats.get("pd_close_loc", 0.0) or 0.0),
        "ret_5d": float(feats.get("ret_5d", 0.0) or 0.0),
        "minute_of_day": float(ts_et.hour * 60 + ts_et.minute),
        "dow": float(ts_et.weekday()),
    }


def build_features(
    signal,
    now: datetime,
    pm_watch: dict | None = None,
    scanner2=None,
    or_width: float = 0.0,
) -> dict[str, float]:
    """The v1 vector from live state. Missing inputs become 0.0 —
    identical to the training-time convention."""
    now_et = now.astimezone(ET)
    symbol = str(getattr(signal, "symbol", ""))
    entry_px = float(getattr(signal, "entry_price", 0.0) or 0.0)
    stop_px = float(getattr(signal, "stop_price", 0.0) or 0.0)

    features: dict[str, float] = {
        "strategy_id": float(STRATEGY_ID.get(str(getattr(signal, "strategy", "")), 5)),
        "entry_minute": float(now_et.hour * 60 + now_et.minute),
        "stop_dist_pct": ((entry_px - stop_px) / entry_px * 100.0) if entry_px > 0 else 0.0,
        "or_width_pct": (or_width / entry_px * 100.0) if entry_px > 0 else 0.0,
        "dow": float(now_et.weekday()),
        "prior_day_up": 0.0,  # v1 gap — not derivable live
        "vol_5d": 0.0,  # v1 gap — not derivable live
        "gap_pct": 0.0,
        "atr_pct": 0.0,
        "avg_daily_volume": 0.0,
        "pm_ramp": 0.0,
        "pm_volume": 0.0,
        "rvol_pm": 0.0,
        "spy_gap": 0.0,
        "qqq_gap": 0.0,
    }

    watch = (pm_watch or {}).get(symbol)
    if watch:
        f = watch.get("features")
        first = float(watch.get("first") or 0.0)
        last = float(watch.get("last") or 0.0)
        if f is not None:
            features["gap_pct"] = float(getattr(f, "gap_pct", 0.0) or 0.0)
            features["atr_pct"] = float(getattr(f, "atr_pct", 0.0) or 0.0)
            features["avg_daily_volume"] = float(getattr(f, "avg_daily_volume", 0.0) or 0.0)
            features["pm_volume"] = float(getattr(f, "day_volume", 0.0) or 0.0)
            if features["avg_daily_volume"] > 0:
                features["rvol_pm"] = features["pm_volume"] / features["avg_daily_volume"]
        if first > 0 and last > 0:
            features["pm_ramp"] = (last / first - 1.0) * 100.0

    if scanner2 is not None and getattr(scanner2, "symbols", None):
        idx = scanner2._index
        row = idx.get(symbol)
        if row is not None:
            prev_close = float(scanner2.prev_close_live[row])
            last_px = float(scanner2.last[row])
            if not features["gap_pct"] and prev_close > 0 and scanner2.day_open[row] > 0:
                features["gap_pct"] = (float(scanner2.day_open[row]) / prev_close - 1.0) * 100.0
            base = scanner2.baselines
            brow = base.index.get(symbol)
            if brow is not None:
                if not features["avg_daily_volume"]:
                    features["avg_daily_volume"] = float(base.adv[brow])
                if not features["atr_pct"]:
                    features["atr_pct"] = float(base.atr_pct[brow])
            del last_px
        for name, key in (("SPY", "spy_gap"), ("QQQ", "qqq_gap")):
            srow = idx.get(name)
            if srow is not None:
                prev = float(scanner2.prev_close_live[srow])
                open_px = float(scanner2.day_open[srow])
                if prev > 0 and open_px > 0:
                    features[key] = (open_px / prev - 1.0) * 100.0
    return features
