#!/usr/bin/env python
"""THE JUDGE v1 (Wave Brain Stage 0 — 2026-08-30).

Trains the meta-labeler on the year of simulated champion trades ("the rules
fired — did it win?"), with features rebuilt point-in-time from the history
DB. Honest validation: day-grouped purged 5-fold with a 5-day embargo,
out-of-fold sigmoid calibration, 10-seed ensemble. Output: the
precision-coverage curve — where the 70/75/80% win-rate rungs actually sit
(a rung exists only if its Wilson lower bound clears the milestone).

Usage: nice -n 19 .venv/bin/python scripts/train_judge.py
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
from waveapp.research.history import HistoryStore

from waveapp.config import support_dir

MOMENTUM = {"ORB": 0, "GAPGO": 1, "PMOM": 2, "VWAP": 3, "REENTRY": 4}
SEEDS = 10
EMBARGO_DAYS = 5


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def build_dataset() -> pd.DataFrame:
    frames = []
    for stage in ("H1", "H2", "IS", "OOS"):  # the 3-year deck (2026-08-31)
        path = support_dir() / "research" / f"night_{stage}-stack_s6.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["stage"] = stage
        frames.append(frame)
    trades = pd.concat(frames, ignore_index=True)
    store = HistoryStore()

    symbols = set(trades.symbol) | {"SPY", "QQQ"}
    lo, hi = trades.day.min(), trades.day.max()
    rows = store._conn.execute(
        f"SELECT symbol, day, open, high, low, close, volume FROM daily_bars"  # noqa: S608
        f" WHERE symbol IN ({','.join('?' * len(symbols))}) AND day >= date(?, '-30 days')"
        f" AND day <= ? ORDER BY symbol, day",
        (*symbols, lo, hi),
    ).fetchall()
    daily: dict[str, list] = {}
    for r in rows:
        daily.setdefault(r[0], []).append(r)

    def daily_features(symbol: str, day_iso: str):
        series = daily.get(symbol, [])
        idx = next((i for i, r in enumerate(series) if r[1] == day_iso), None)
        if idx is None or idx < 15:
            return None
        prior = series[idx - 1]
        window = series[idx - 15 : idx]
        closes = [float(r[5]) for r in window]
        atr = float(np.mean([float(r[3]) - float(r[4]) for r in window]))  # simple daily range ATR
        avg_vol = float(np.mean([float(r[6]) for r in window]))
        today = series[idx]
        gap = (float(today[2 + 0]) / float(prior[5]) - 1) * 100  # open vs prior close
        prior_up = 1.0 if float(prior[5]) >= float(prior[2]) else 0.0
        vol5 = float(np.std(np.diff(np.log(closes[-6:])))) if len(closes) >= 6 else 0.0
        return {
            "gap_pct": gap,
            "atr_pct": atr / max(float(prior[5]), 0.01) * 100,
            "avg_daily_volume": avg_vol,
            "prior_day_up": prior_up,
            "vol_5d": vol5,
        }

    # pre-market map per (symbol, day) from minute bars: ramp + volume
    def pm_features(symbol: str, day_iso: str):
        day = date.fromisoformat(day_iso)
        pm_start = int(datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC).timestamp() * 1000)
        pm_end = int(datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC).timestamp() * 1000)
        bars = store._conn.execute(
            "SELECT open, close, volume FROM bars WHERE symbol=? AND ts_ms>=? AND ts_ms<?"
            " ORDER BY ts_ms",
            (symbol, pm_start, pm_end),
        ).fetchall()
        if not bars:
            return {"pm_ramp": 0.0, "pm_volume": 0.0}
        first_open = float(bars[0][0])
        return {
            "pm_ramp": (float(bars[-1][1]) / first_open - 1) * 100 if first_open else 0.0,
            "pm_volume": float(sum(b[2] for b in bars)),
        }

    def or_width(symbol: str, day_iso: str):
        day = date.fromisoformat(day_iso)
        start = int(datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC).timestamp() * 1000)
        end = int(datetime(day.year, day.month, day.day, 13, 35, tzinfo=UTC).timestamp() * 1000)
        bars = store._conn.execute(
            "SELECT high, low FROM bars WHERE symbol=? AND ts_ms>=? AND ts_ms<?",
            (symbol, start, end),
        ).fetchall()
        if not bars:
            return 0.0
        return float(max(b[0] for b in bars) - min(b[1] for b in bars))

    records = []
    for _, t in trades.iterrows():
        base = daily_features(t.symbol, t.day)
        if base is None:
            continue
        spy = daily_features("SPY", t.day) or {}
        qqq = daily_features("QQQ", t.day) or {}
        entry_ts = pd.Timestamp(t.entry_ts)
        rec = {
            "day": t.day,
            "stage": t.stage,
            "label": 1 if float(t.pnl) > 0 else 0,
            "pnl": float(t.pnl),
            "strategy_id": MOMENTUM.get(t.strategy, 5),
            "entry_minute": entry_ts.hour * 60 + entry_ts.minute,
            "entry_px": float(t.entry_px),
            "stop_dist_pct": (float(t.entry_px) - float(t.stop_px))
            / max(float(t.entry_px), 0.01)
            * 100,
            "or_width_pct": or_width(t.symbol, t.day) / max(float(t.entry_px), 0.01) * 100,
            "spy_gap": spy.get("gap_pct", 0.0),
            "qqq_gap": qqq.get("gap_pct", 0.0),
            "dow": pd.Timestamp(t.day).dayofweek,
            **base,
            **pm_features(t.symbol, t.day),
        }
        rec["rvol_pm"] = rec["pm_volume"] / max(rec["avg_daily_volume"], 1.0)
        records.append(rec)
    store.close()
    return pd.DataFrame(records)


def main() -> int:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    data = build_dataset()
    print(f"dataset: {len(data)} trades · win rate {data.label.mean() * 100:.1f}%", flush=True)
    feature_cols = [
        c for c in data.columns if c not in ("day", "stage", "label", "pnl", "entry_px")
    ]
    days = sorted(data.day.unique())
    folds = np.array_split(np.array(days), 5)

    oof_score = np.full(len(data), np.nan)
    for fold_days in folds:
        fold_set = set(fold_days)
        lo_i = days.index(fold_days[0])
        hi_i = days.index(fold_days[-1])
        embargo = set(days[max(0, lo_i - EMBARGO_DAYS) : lo_i]) | set(
            days[hi_i + 1 : hi_i + 1 + EMBARGO_DAYS]
        )
        train_mask = ~data.day.isin(fold_set | embargo)
        test_mask = data.day.isin(fold_set)
        x_train = data.loc[train_mask, feature_cols].values
        y_train = data.loc[train_mask, "label"].values
        x_test = data.loc[test_mask, feature_cols].values
        # per-day cross-sectional uniqueness weights
        day_counts = data.loc[train_mask].groupby("day")["label"].transform("size")
        weights = (1.0 / day_counts).values
        preds = np.zeros(len(x_test))
        for seed in range(SEEDS):
            model = HistGradientBoostingClassifier(
                max_leaf_nodes=15,
                min_samples_leaf=60,
                learning_rate=0.05,
                max_iter=400,
                early_stopping=True,
                validation_fraction=0.15,
                l2_regularization=5.0,
                random_state=seed,
            )
            model.fit(x_train, y_train, sample_weight=weights)
            preds += model.predict_proba(x_test)[:, 1]
        oof_score[test_mask.values] = preds / SEEDS

    valid = ~np.isnan(oof_score)
    data = data.loc[valid].copy()
    data["raw_score"] = oof_score[valid]
    # Platt calibration on the out-of-fold scores
    platt = LogisticRegression()
    platt.fit(data[["raw_score"]].values, data["label"].values)
    data["p_win"] = platt.predict_proba(data[["raw_score"]].values)[:, 1]
    data.to_csv(support_dir() / "research" / "judge_v1_oof.csv", index=False)

    base_wr = data.label.mean() * 100
    print("\n== THE JUDGE v1 — precision/coverage curve (out-of-fold, calibrated) ==")
    print(f"base win rate (no judge): {base_wr:.1f}%\n")
    print(
        f"{'threshold':>10} {'trades':>7} {'coverage':>9} {'win rate':>9} "
        f"{'wilson LB':>10} {'sum pnl $':>10}"
    )
    for threshold in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85):
        taken = data[data.p_win >= threshold]
        if len(taken) == 0:
            continue
        wins = int(taken.label.sum())
        wr = wins / len(taken) * 100
        lb = wilson_lower(wins, len(taken)) * 100
        print(
            f"{threshold:>10.2f} {len(taken):>7} {len(taken) / len(data) * 100:>8.0f}%"
            f" {wr:>8.1f}% {lb:>9.1f}% {taken.pnl.sum():>10,.0f}"
        )
    # -- Stage 1 shadow artifact (2026-08-31): final ensemble on ALL data,
    # calibration from the OOF platt, golden vectors + checksum. The app
    # loads this read-only and refuses it on any self-test doubt.
    from waveapp.engine.brain import save_brain

    x_all = data[feature_cols].values
    y_all = data["label"].values
    day_counts_all = data.groupby("day")["label"].transform("size")
    weights_all = (1.0 / day_counts_all).values
    final_models = []
    for seed in range(SEEDS):
        model = HistGradientBoostingClassifier(
            max_leaf_nodes=15,
            min_samples_leaf=60,
            learning_rate=0.05,
            max_iter=400,
            early_stopping=True,
            validation_fraction=0.15,
            l2_regularization=5.0,
            random_state=seed,
        )
        model.fit(x_all, y_all, sample_weight=weights_all)
        final_models.append(model)
    digest = save_brain(
        final_models,
        platt,
        feature_cols,
        x_all[:5],
        {
            "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "n_trades": len(data),
            "base_wr": float(data.label.mean()),
        },
    )
    print(f"\nbrain artifact saved (sha256 {digest[:16]}…)")

    print("\nrung check (Wilson lower bound must clear the milestone):")
    for milestone in (70, 75, 80):
        best = None
        for threshold in np.arange(0.50, 0.95, 0.01):
            taken = data[data.p_win >= threshold]
            if len(taken) < 30:
                break
            if wilson_lower(int(taken.label.sum()), len(taken)) * 100 >= milestone:
                best = (threshold, len(taken), taken.label.mean() * 100)
                break
        if best:
            print(
                f"  {milestone}% rung: PROVEN at threshold {best[0]:.2f} "
                f"({best[1]} trades, actual {best[2]:.1f}%)"
            )
        else:
            print(f"  {milestone}% rung: not provable yet with this data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
