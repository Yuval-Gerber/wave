"""Nightly ML engine — master blueprint 8.1/8.2/8.3/8.5 (P1, 2026-09-01).

Three jobs, all run while the market world is asleep:

1. LABEL EVERYTHING SCANNED (8.1): every journaled scanner2 snapshot on the
   5-minute grid gets as-of triple-barrier outcomes (30m/60m/session) —
   ~thousands of lessons per day instead of the handful of taken trades.
   The features column is COPIED from the journal (compute at score time,
   store forever); only the barrier walk touches later bars.

2. NIGHTLY FULL RETRAIN (8.2): the v2 live-features challenger — 10-seed
   HGB from scratch, FROZEN hyperparameters (v1's — anything that searches
   is weekly, 8.8), day-grouped purged 5-fold + 1-day embargo, snapshot
   down-weighting 1/n(symbol-day), Platt on OOF. Artifact saved as
   brain_v2.joblib with the same sha256 + golden-vector armor as v1.
   The v2 model NEVER gates a trade — it shadow-scores the same stream as
   the frozen v1 champion (8.13: the only honest "is it studying faster"
   test is ≥30 sessions of that head-to-head).

3. ANTI-SELF-DECEPTION (8.5): two deterministic noise-probe features ride
   along every retrain; metrics are reported at the symbol-day level
   (per-day precision@5), never per-snapshot AUC alone.

Guards: no retrain below 10 distinct days / 1,000 rows / 200 wins — under
that the trainer logs "still in school" and does nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np

from waveapp.engine.brain_features import V2_FEATURE_COLS, v2_vector
from waveapp.engine.labeler import compute_outcome_multi

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("wave.brain.nightly")

GRID_MINUTES = 5  # label the 5-minute snapshot grid (II.4: score live every 5min)
MIN_DAYS = 10
MIN_ROWS = 1000
MIN_WINS = 300  # features ≪ positives/10 (8.5): ~19 columns → need ≥300 wins
SEEDS = 10
EMBARGO_DAYS = 1
NOISE_PROBES = ("noise_probe_a", "noise_probe_b")

V2_ARTIFACT = "brain_v2.joblib"
V2_OOF_CSV = "brain_v2_oof.csv"

# FROZEN hyperparameters — identical to v1 (train_judge.py). Changing them
# is a WEEKLY search decision that counts toward the DSR trial budget (8.8).
HGB_PARAMS = {
    "max_leaf_nodes": 15,
    "min_samples_leaf": 60,
    "learning_rate": 0.05,
    "max_iter": 400,
    "early_stopping": True,
    "validation_fraction": 0.15,
    "l2_regularization": 5.0,
}


# -- 8.1: labeling ---------------------------------------------------------


def pending_symbol_days(database, limit_days: int = 40) -> list[tuple[str, str, list[dict]]]:
    """Unlabeled (symbol, session_date) groups from the snapshot journal —
    finished sessions only, 5-minute grid. WORKER THREAD (SQL)."""
    now_et = datetime.now(ET)
    rows = database.query(
        "SELECT s.ts, s.symbol, s.features FROM scanner2_snapshots s"
        " WHERE s.symbol != '_MARKET'"
        " AND CAST(substr(s.ts, 15, 2) AS INTEGER) % ? = 0"
        " AND NOT EXISTS (SELECT 1 FROM snapshot_labels l"
        "                 WHERE l.ts = s.ts AND l.symbol = s.symbol)"
        " ORDER BY s.ts",
        (GRID_MINUTES,),
    )
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        try:
            ts_et = datetime.fromisoformat(str(row["ts"])).astimezone(ET)
        except ValueError:
            continue
        session_date = ts_et.date().isoformat()
        if session_date >= now_et.date().isoformat():
            continue  # today's outcome isn't knowable yet
        groups.setdefault((row["symbol"], session_date), []).append(dict(row))
    ordered = sorted(groups.items(), key=lambda kv: kv[0][1])
    return [(sym, day, snaps) for (sym, day), snaps in ordered[:limit_days]]


def label_symbol_day(
    database,
    symbol: str,
    session_date: str,
    snapshots: list[dict],
    bars: list[tuple[float, float, float, float, float]],
) -> int:
    """Label one symbol-day's snapshots against its 1-min bars. Rows that
    cannot be labeled honestly (no ATR, no bars left) are stamped with NULL
    labels so they are never retried forever. Returns labeled count."""
    out_rows = []
    labeled = 0
    for snap in snapshots:
        try:
            feats = json.loads(snap["features"])
        except (ValueError, TypeError):
            feats = {}
        px = float(feats.get("px", 0.0) or 0.0)
        atr = px * float(feats.get("atr_pct", 0.0) or 0.0) / 100.0
        outcome = None
        if atr > 0 and bars:
            try:
                entry_ts = datetime.fromisoformat(str(snap["ts"])).timestamp()
                outcome = compute_outcome_multi(bars, entry_ts, atr)
            except (ValueError, TypeError):
                outcome = None
        if outcome is not None:
            labeled += 1
        out_rows.append(
            (
                snap["ts"],
                symbol,
                session_date,
                snap["features"],
                (outcome or {}).get("entry"),
                round(atr, 6) if atr > 0 else None,
                (outcome or {}).get("label_30m"),
                (outcome or {}).get("label_60m"),
                (outcome or {}).get("label_sess"),
                (outcome or {}).get("mfe_atr"),
                (outcome or {}).get("mae_atr"),
            )
        )
    if out_rows:
        database.executemany(
            "INSERT OR REPLACE INTO snapshot_labels"
            " (ts, symbol, session_date, features, entry, atr,"
            "  label_30m, label_60m, label_sess, mfe_atr, mae_atr, n_snapshots)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            out_rows,
        )
        database.execute(
            "UPDATE snapshot_labels SET n_snapshots ="
            " (SELECT COUNT(*) FROM snapshot_labels i"
            "  WHERE i.symbol = snapshot_labels.symbol"
            "  AND i.session_date = snapshot_labels.session_date"
            "  AND i.label_60m IS NOT NULL)"
            " WHERE symbol = ? AND session_date = ?",
            (symbol, session_date),
        )
    return labeled


# -- 8.2/8.5: the nightly trainer ------------------------------------------


def noise_probe(symbol: str, ts: str, salt: str) -> float:
    """Deterministic pseudo-noise in [0,1) — same value every retrain, so
    runs are reproducible without Date/random (8.5 probe features)."""
    digest = hashlib.sha256(f"{salt}:{symbol}:{ts}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def build_training_frame(database):
    """snapshot_labels → (X, y, weights, days, meta). Returns None when the
    guards say there isn't enough honest data yet."""
    rows = database.query(
        "SELECT ts, symbol, session_date, features, n_snapshots, label_60m"
        " FROM snapshot_labels WHERE label_60m IS NOT NULL ORDER BY ts"
    )
    if not rows:
        return None
    feature_cols = list(V2_FEATURE_COLS) + list(NOISE_PROBES)
    x_rows: list[list[float]] = []
    y: list[int] = []
    weights: list[float] = []
    days: list[str] = []
    for row in rows:
        try:
            feats = json.loads(str(row["features"]))
            ts_et = datetime.fromisoformat(str(row["ts"])).astimezone(ET)
        except (ValueError, TypeError):
            continue
        vec = v2_vector(feats, ts_et)
        vec[NOISE_PROBES[0]] = noise_probe(str(row["symbol"]), str(row["ts"]), "a")
        vec[NOISE_PROBES[1]] = noise_probe(str(row["symbol"]), str(row["ts"]), "b")
        x_rows.append([float(vec.get(c, 0.0)) for c in feature_cols])
        y.append(1 if str(row["label_60m"]) == "win" else 0)
        n = int(row["n_snapshots"] or 1)
        weights.append(1.0 / max(n, 1))
        days.append(str(row["session_date"]))
    if not x_rows:
        return None
    x = np.asarray(x_rows, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.int64)
    w = np.asarray(weights, dtype=np.float64)
    day_arr = np.asarray(days)
    n_days = len(set(days))
    n_wins = int(y_arr.sum())
    if n_days < MIN_DAYS or len(y_arr) < MIN_ROWS or n_wins < MIN_WINS:
        logger.info(
            "v2 nightly: still in school — %d days / %d rows / %d wins"
            " (need %d / %d / %d); no retrain",
            n_days,
            len(y_arr),
            n_wins,
            MIN_DAYS,
            MIN_ROWS,
            MIN_WINS,
        )
        return None
    return x, y_arr, w, day_arr, feature_cols


def train_nightly(database, support_dir_path) -> dict | None:
    """The full 8.2 retrain. Returns the summary dict (also journaled to
    model_registry, active=0 ALWAYS) or None when guarded off."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    frame = build_training_frame(database)
    if frame is None:
        return None
    x, y, w, day_arr, feature_cols = frame
    days = sorted(set(day_arr.tolist()))
    folds = np.array_split(np.array(days), 5)

    oof = np.full(len(y), np.nan)
    for fold_days in folds:
        if len(fold_days) == 0:
            continue
        fold_set = set(fold_days.tolist())
        lo_i = days.index(fold_days[0])
        hi_i = days.index(fold_days[-1])
        embargo = set(days[max(0, lo_i - EMBARGO_DAYS) : lo_i]) | set(
            days[hi_i + 1 : hi_i + 1 + EMBARGO_DAYS]
        )
        train_mask = np.array([d not in fold_set | embargo for d in day_arr])
        test_mask = np.array([d in fold_set for d in day_arr])
        if not train_mask.any() or not test_mask.any():
            continue
        preds = np.zeros(int(test_mask.sum()))
        for seed in range(SEEDS):
            model = HistGradientBoostingClassifier(random_state=seed, **HGB_PARAMS)
            model.fit(x[train_mask], y[train_mask], sample_weight=w[train_mask])
            preds += model.predict_proba(x[test_mask])[:, 1]
        oof[test_mask] = preds / SEEDS

    valid = ~np.isnan(oof)
    if valid.sum() < MIN_ROWS // 2 or len(set(y[valid].tolist())) < 2:
        logger.info("v2 nightly: OOF too thin to calibrate — no artifact")
        return None
    # calibrate UNWEIGHTED so the output stays honest P(win) (8.7)
    platt = LogisticRegression()
    platt.fit(oof[valid].reshape(-1, 1), y[valid])
    p_win = platt.predict_proba(oof[valid].reshape(-1, 1))[:, 1]

    # -- 8.5 report: symbol-day-level metric + probe check ------------------
    auc = float(roc_auc_score(y[valid], oof[valid]))
    day_prec: list[float] = []
    for day in days:
        mask = (day_arr[valid] == day) & ~np.isnan(p_win)
        if mask.sum() < 5:
            continue
        top = np.argsort(p_win[mask])[-5:]
        day_prec.append(float(y[valid][mask][top].mean()))
    prec5 = float(np.mean(day_prec)) if day_prec else 0.0
    probe_flags = []
    for i, col in enumerate(feature_cols):
        if col in NOISE_PROBES:
            continue
        try:
            col_auc = abs(roc_auc_score(y[valid], x[valid][:, i]) - 0.5)
            probe_auc = max(
                abs(roc_auc_score(y[valid], x[valid][:, feature_cols.index(p)]) - 0.5)
                for p in NOISE_PROBES
            )
            if col_auc <= probe_auc:
                probe_flags.append(col)
        except ValueError:
            continue

    # -- final ensemble on ALL data + armored artifact ----------------------
    from waveapp.engine.brain import save_brain

    final_models = []
    for seed in range(SEEDS):
        model = HistGradientBoostingClassifier(random_state=seed, **HGB_PARAMS)
        model.fit(x, y, sample_weight=w)
        final_models.append(model)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    artifact = support_dir_path / V2_ARTIFACT
    digest = save_brain(
        final_models,
        platt,
        feature_cols,
        x[:5],
        {
            "trained_at": stamp,
            "n_trades": int(len(y)),
            "base_wr": float(y.mean()),
            "model": "v2_nightly",
        },
        path=artifact,
    )

    # OOF csv for the audit trail (p_win + label — the ladder's food)
    oof_path = support_dir_path / "research" / V2_OOF_CSV
    oof_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["p_win,label,day"]
    lines += [
        f"{p:.4f},{int(label)},{day}"
        for p, label, day in zip(p_win, y[valid], day_arr[valid], strict=True)
    ]
    oof_path.write_text("\n".join(lines) + "\n")

    summary = {
        "trained_at": stamp,
        "rows": int(len(y)),
        "days": len(days),
        "wins": int(y.sum()),
        "auc": round(auc, 4),
        "day_precision_at5": round(prec5, 4),
        "probe_flagged": probe_flags,
        "sha256": digest[:16],
    }
    database.execute(
        "INSERT INTO model_registry (ts, kind, version, artifact_path, validation, active)"
        " VALUES (?, ?, ?, ?, ?, 0)",
        (stamp, "brain_v2_nightly", stamp[:10], str(artifact), json.dumps(summary)),
    )
    logger.info(
        "v2 nightly retrain: %d rows / %d days → AUC %.3f, day-precision@5 %.1f%%"
        " (probes beat: %s) — artifact %s…",
        len(y),
        len(days),
        auc,
        prec5 * 100,
        ", ".join(probe_flags) if probe_flags else "none",
        digest[:12],
    )
    return summary
