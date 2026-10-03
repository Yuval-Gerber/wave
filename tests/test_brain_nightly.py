"""Master blueprint 8.1/8.2/8.4/8.5 — the nightly ML engine (P1, 2026-09-01).

Covers: multi-horizon triple-barrier math, the snapshot labeler against a
real migrated DB, the trainer guards, a full (shrunken) nightly retrain with
artifact armor, live-feature parity, and shadow-score resolution.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from waveapp.engine.brain_features import V2_FEATURE_COLS, v2_vector
from waveapp.engine.labeler import compute_outcome_multi
from waveapp.persistence.db import Database

BASE_TS = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)  # 10:00 ET, a Monday


def _bars(path):
    """1-min bars from a list of (minute_offset, o, h, l, c)."""
    return [(BASE_TS.timestamp() + m * 60, o, h, low, c) for m, o, h, low, c in path]


def flat_path(minutes=120, px=100.0):
    return [(m, px, px + 0.05, px - 0.05, px) for m in range(minutes)]


# -- compute_outcome_multi -------------------------------------------------


def test_multi_horizon_win_after_30m_is_flat_then_win():
    """Target hit at minute 45: 30m label flat, 60m and session win."""
    path = flat_path(40)
    path += [(45, 100.0, 102.5, 100.0, 102.0)]  # +2.5 > 2×ATR(1.0)? barrier=+1×ATR
    path += [(50 + i, 102.0, 102.1, 101.9, 102.0) for i in range(60)]
    out = compute_outcome_multi(_bars(path), BASE_TS.timestamp(), atr=2.0)
    assert out is not None
    assert out["label_30m"] == "flat"
    assert out["label_60m"] == "win"
    assert out["label_sess"] == "win"
    assert out["mfe_atr"] > 1.0


def test_multi_horizon_loss_and_conservative_double_hit():
    # stop hit at minute 10 → loss everywhere
    path = flat_path(9)
    path += [(10, 100.0, 100.1, 96.0, 97.0)]
    path += [(11 + i, 97.0, 97.1, 96.9, 97.0) for i in range(60)]
    out = compute_outcome_multi(_bars(path), BASE_TS.timestamp(), atr=2.0)
    assert out["label_30m"] == "loss"
    assert out["label_60m"] == "loss"
    assert out["label_sess"] == "loss"
    # both barriers inside one bar → conservative loss
    wild = flat_path(5) + [(6, 100.0, 103.0, 96.0, 100.0)]
    wild += [(7 + i, 100.0, 100.1, 99.9, 100.0) for i in range(40)]
    out2 = compute_outcome_multi(_bars(wild), BASE_TS.timestamp(), atr=2.0)
    assert out2["label_30m"] == "loss"


def test_multi_horizon_refuses_dishonest_inputs():
    assert compute_outcome_multi([], BASE_TS.timestamp(), 1.0) is None
    assert compute_outcome_multi(_bars(flat_path(60)), BASE_TS.timestamp(), 0.0) is None
    late = BASE_TS.timestamp() + 200 * 60  # after all bars
    assert compute_outcome_multi(_bars(flat_path(60)), late, 1.0) is None


# -- migration + labeler ---------------------------------------------------


def _seed_snapshots(db, symbol="TEST", day_offset=-1, minutes=(0, 5, 7, 10)):
    """Journal rows at BASE_TS+minutes; 7 is off the 5-min grid."""
    feats = {"px": 100.0, "atr_pct": 2.0, "rvol": 3.0, "score": 5.0}
    rows = []
    day = BASE_TS + timedelta(days=day_offset)
    for m in minutes:
        ts = (day + timedelta(minutes=m)).isoformat()
        rows.append((ts, symbol, json.dumps(feats)))
    db.executemany(
        "INSERT OR REPLACE INTO scanner2_snapshots (ts, symbol, features) VALUES (?, ?, ?)",
        rows,
    )
    return day


def test_migration_008_and_labeler_end_to_end(tmp_path):
    from waveapp.engine import brain_nightly

    db = Database(tmp_path / "t.db")
    db.migrate()
    # migration: snapshot_labels exists, brain_scores has the model column
    db.execute(
        "INSERT INTO brain_scores (ts, symbol, p_win, approved, features, executed, model)"
        " VALUES ('2026-08-31T14:00:00+00:00', 'X', 0.6, 1, '{}', 0, 'v2')"
    )
    assert db.query("SELECT model FROM brain_scores")[0]["model"] == "v2"

    day = _seed_snapshots(db, day_offset=-10)
    pending = brain_nightly.pending_symbol_days(db)
    assert len(pending) == 1
    symbol, session_date, snaps = pending[0]
    assert symbol == "TEST"
    assert session_date == day.astimezone(brain_nightly.ET).date().isoformat()
    assert len(snaps) == 3  # 0, 5, 10 — the off-grid 7 excluded

    # bars: barrier +1×ATR(=2.0$) hit 40 minutes after the first snapshot
    path = [(m, 100.0, 100.1, 99.9, 100.0) for m in range(39)]
    path += [(40, 100.0, 103.0, 100.0, 102.5)]
    path += [(41 + i, 102.5, 102.6, 102.4, 102.5) for i in range(90)]
    bars = [((day + timedelta(minutes=m)).timestamp(), o, h, low, c) for m, o, h, low, c in path]
    labeled = brain_nightly.label_symbol_day(db, symbol, session_date, snaps, bars)
    assert labeled == 3
    rows = db.query("SELECT * FROM snapshot_labels ORDER BY ts")
    assert len(rows) == 3
    assert rows[0]["label_60m"] == "win"  # hit at +40m
    assert rows[0]["label_30m"] == "flat"
    assert rows[0]["n_snapshots"] == 3  # uniqueness weight denominator
    # labeled rows never come back as pending
    assert brain_nightly.pending_symbol_days(db) == []


def test_labeler_stamps_unlabelable_rows(tmp_path):
    from waveapp.engine import brain_nightly

    db = Database(tmp_path / "t.db")
    db.migrate()
    day = _seed_snapshots(db, day_offset=-3, minutes=(0, 5))
    pending = brain_nightly.pending_symbol_days(db)
    labeled = brain_nightly.label_symbol_day(db, "TEST", pending[0][1], pending[0][2], bars=[])
    assert labeled == 0
    assert len(db.query("SELECT * FROM snapshot_labels WHERE label_60m IS NULL")) == 2
    assert brain_nightly.pending_symbol_days(db) == []  # stamped, never retried
    del day


# -- trainer guards + full retrain -----------------------------------------


def test_trainer_guards_refuse_thin_data(tmp_path):
    from waveapp.engine import brain_nightly

    db = Database(tmp_path / "t.db")
    db.migrate()
    assert brain_nightly.build_training_frame(db) is None  # empty
    _seed_snapshots(db, day_offset=-5)
    pending = brain_nightly.pending_symbol_days(db)
    brain_nightly.label_symbol_day(db, "TEST", pending[0][1], pending[0][2], bars=[])
    assert brain_nightly.build_training_frame(db) is None  # only NULL labels
    assert brain_nightly.train_nightly(db, tmp_path) is None


def _seed_labeled_dataset(db, n_days=12, per_day=100):
    """Synthetic labeled rows: 'win' correlates with rvol (learnable)."""
    rows = []
    for d in range(n_days):
        day = (BASE_TS - timedelta(days=20 + n_days - d)).astimezone(UTC)
        date_str = day.date().isoformat()
        for i in range(per_day):
            rvol = 1.0 + (i % 10)
            win = (i % 10) >= 6  # 40% wins, driven by rvol
            feats = {"px": 50.0 + i, "atr_pct": 2.0, "rvol": rvol, "score": rvol * 2}
            ts = (day + timedelta(minutes=5 * i)).isoformat()
            rows.append(
                (
                    ts,
                    f"SYM{i % 25}",
                    date_str,
                    json.dumps(feats),
                    50.0,
                    1.0,
                    "win" if win else "loss",
                    "win" if win else "loss",
                    "win" if win else "loss",
                    1.5 if win else 0.2,
                    0.3 if win else 1.6,
                    per_day // 25,
                )
            )
    db.executemany(
        "INSERT OR REPLACE INTO snapshot_labels"
        " (ts, symbol, session_date, features, entry, atr, label_30m, label_60m,"
        "  label_sess, mfe_atr, mae_atr, n_snapshots)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def test_nightly_retrain_end_to_end(tmp_path, monkeypatch):
    """Full 8.2 path (shrunken iterations): trains, saves an armored
    artifact that load_brain accepts, writes the OOF csv + registry row."""
    from waveapp.engine import brain_nightly
    from waveapp.engine.brain import load_brain

    monkeypatch.setitem(brain_nightly.HGB_PARAMS, "max_iter", 20)
    db = Database(tmp_path / "t.db")
    db.migrate()
    _seed_labeled_dataset(db)
    summary = brain_nightly.train_nightly(db, tmp_path)
    assert summary is not None
    assert summary["rows"] == 1200
    assert summary["days"] == 12
    assert summary["auc"] > 0.7  # rvol drives the label — must be learnable
    assert (tmp_path / brain_nightly.V2_ARTIFACT).exists()
    assert (tmp_path / "research" / brain_nightly.V2_OOF_CSV).exists()
    reg = db.query("SELECT * FROM model_registry WHERE kind='brain_v2_nightly'")
    assert len(reg) == 1
    assert reg[0]["active"] == 0  # NEVER auto-armed

    brain = load_brain(path=tmp_path / brain_nightly.V2_ARTIFACT)
    assert brain is not None  # checksum + golden self-test passed
    vec = v2_vector({"px": 60.0, "atr_pct": 2.0, "rvol": 9.0, "score": 18.0}, BASE_TS)
    vec[brain_nightly.NOISE_PROBES[0]] = 0.5
    vec[brain_nightly.NOISE_PROBES[1]] = 0.5
    p_hot = brain.score(vec)
    vec_cold = dict(vec, rvol=1.0, score=2.0)
    p_cold = brain.score(vec_cold)
    assert 0.0 <= p_cold < p_hot <= 1.0  # ranks the learnable signal


# -- live parity + shadow resolution ---------------------------------------


def test_live_feature_row_matches_journal_keys(tmp_path, monkeypatch):
    """The v2 vector consumes exactly what the journal writes (one path)."""
    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([SimpleNamespace(symbol="TEST", name="", tradable=True)])
    row = scanner._index["TEST"]
    scanner.last[row] = 50.0
    feats = scanner.live_feature_row("TEST")
    assert feats is not None
    assert "atr_pct" in feats  # barrier geometry journaled as-of
    vec = v2_vector(feats, BASE_TS)
    for col in V2_FEATURE_COLS:
        assert col in vec, f"v2 vector missing {col}"
    assert scanner.live_feature_row("UNKNOWN") is None


def test_resolve_shadow_scores_head_to_head(tmp_path):
    from waveapp.engine.connection_monitor import ConnectionMonitor

    db = Database(tmp_path / "t.db")
    db.migrate()
    old_ts = (datetime.now(UTC) - timedelta(days=2)).replace(microsecond=0)
    label_ts = (old_ts + timedelta(minutes=2)).isoformat()
    db.execute(
        "INSERT INTO snapshot_labels (ts, symbol, session_date, features, label_60m,"
        " n_snapshots) VALUES (?, 'TEST', ?, '{}', 'win', 1)",
        (label_ts, old_ts.date().isoformat()),
    )
    for model in ("v1", "v2"):
        db.execute(
            "INSERT INTO brain_scores (ts, symbol, p_win, approved, features,"
            " executed, model) VALUES (?, 'TEST', 0.6, 1, '{}', 0, ?)",
            (old_ts.isoformat(), model),
        )
    monitor = ConnectionMonitor(on_status=lambda *_: None, database=db)
    monitor._resolve_shadow_scores()
    rows = db.query("SELECT model, won FROM brain_scores ORDER BY model")
    assert [(r["model"], r["won"]) for r in rows] == [("v1", 1), ("v2", 1)]
