"""Brain Stage M1 — live shadow scoring tests (2026-09-01)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from waveapp.engine.brain_features import STRATEGY_ID, build_features
from waveapp.engine.connection_monitor import ConnectionMonitor


def _signal(symbol="TEST", strategy="ORB", entry=50.0, stop=49.0):
    return SimpleNamespace(symbol=symbol, strategy=strategy, entry_price=entry, stop_price=stop)


def test_feature_vector_matches_the_trained_brain():
    """Every feature the artifact expects must be produced (parity rule)."""
    from waveapp.engine.brain import load_brain

    features = build_features(_signal(), datetime(2026, 9, 1, 14, 45, tzinfo=UTC))
    brain = load_brain()
    if brain is not None:  # artifact present on this machine
        for col in brain.feature_cols:
            assert col in features, f"live builder missing trained feature: {col}"
        p = brain.score(features)
        assert 0.0 <= p <= 1.0
    assert features["strategy_id"] == STRATEGY_ID["ORB"]
    assert features["stop_dist_pct"] == pytest.approx(2.0)
    assert features["entry_minute"] == 10 * 60 + 45  # 14:45 UTC = 10:45 ET


def test_pm_watch_and_scanner2_feed_the_vector(tmp_path, monkeypatch):

    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe(
        [SimpleNamespace(symbol=s, name="", tradable=True) for s in ("TEST", "SPY", "QQQ")]
    )
    for sym, prev, open_px in (("SPY", 640.0, 646.4), ("QQQ", 570.0, 567.2)):
        row = scanner._index[sym]
        scanner.prev_close_live[row] = prev
        scanner.day_open[row] = open_px
    pm_watch = {
        "TEST": {
            "first": 48.0,
            "last": 50.0,
            "features": SimpleNamespace(
                gap_pct=2.5, atr_pct=3.0, avg_daily_volume=2_000_000.0, day_volume=400_000.0
            ),
        }
    }
    features = build_features(
        _signal(), datetime(2026, 9, 1, 13, 31, tzinfo=UTC), pm_watch=pm_watch, scanner2=scanner
    )
    assert features["pm_ramp"] == pytest.approx((50 / 48 - 1) * 100)
    assert features["rvol_pm"] == pytest.approx(0.2)
    assert features["spy_gap"] == pytest.approx(1.0, abs=1e-4)  # float32 store
    assert features["qqq_gap"] == pytest.approx(-0.4912, abs=1e-3)


def test_shadow_scoring_journals_and_never_blocks(tmp_path, monkeypatch):
    import waveapp.config as config_module
    from waveapp.persistence.db import Database

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    db = Database(tmp_path / "t.db")
    db.migrate()
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = db

    class FakeBrain:
        feature_cols = ["gap_pct"]

        def score(self, features):
            return 0.71

    monitor._brain = FakeBrain()
    monitor._brain_shadow_score(_signal("GOOD"))
    rows = db.query("SELECT * FROM brain_scores")
    assert len(rows) == 1
    assert rows[0]["p_win"] == pytest.approx(0.71)
    assert rows[0]["approved"] == 1
    assert monitor._brain_shadow["scored"] == 1
    assert monitor._brain_shadow["approved_new"] == 1

    class ExplodingBrain:
        def score(self, features):
            raise RuntimeError("boom")

    monitor._brain = ExplodingBrain()
    monitor._brain_shadow_score(_signal("BOOM"))  # must not raise
    assert monitor._brain_shadow["scored"] == 1  # unchanged — failure contained
    db.close()


def test_ml_payload_carries_shadow_dots_once(tmp_path, monkeypatch):
    import waveapp.config as config_module

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._brain = object()
    monitor._brain_shadow.update(scored=5, approved=3, rejected=2, approved_new=3, rejected_new=2)
    stats = monitor._ml_stats()
    assert stats["shadow_scored"] == 5
    assert stats["shadow_approve_ratio"] == 0.6
    assert stats["shadow_approved_new"] == 3 and stats["shadow_rejected_new"] == 2
    assert stats["brain_loaded"] is True
    stats2 = monitor._ml_stats()
    assert stats2["shadow_approved_new"] == 0  # deltas consumed once → dots fire once


def test_nightly_resolution_builds_the_scoreboard(tmp_path, monkeypatch):
    import waveapp.config as config_module
    from waveapp.persistence.db import Database

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    db = Database(tmp_path / "t.db")
    db.migrate()
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = db
    db.execute(
        "INSERT INTO brain_scores (ts, symbol, strategy, p_win, approved, features, executed)"
        " VALUES ('2026-08-31T14:31:00+00:00', 'WIN', 'ORB', 0.7, 1, '{}', 1)"
    )
    db.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, state, opened_at,"
        " realized_pnl, trading_mode) VALUES ('u1', 'WIN', 'long', 10, 'closed',"
        " '2026-08-31T14:32:00+00:00', 55.0, 'paper')"
    )
    monitor._resolve_brain_scores()
    row = db.query("SELECT * FROM brain_scores")[0]
    assert row["won"] == 1
    assert row["outcome_pnl"] == pytest.approx(55.0)
    assert row["resolved_at"] is not None
    db.close()


def test_menu_scoring_drinks_nonstop_with_dedupe(tmp_path, monkeypatch):
    """The water is flowing, so it should drink nonstop — every menu
    candidate scored in shadow (executed=0), once per 15 min per symbol."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    import waveapp.config as config_module
    from waveapp.persistence.db import Database

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    db = Database(tmp_path / "t.db")
    db.migrate()
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = db

    class FakeBrain:
        def score(self, features):
            return 0.62

    monitor._brain = FakeBrain()
    monitor._scanner2 = SimpleNamespace(
        last_menu=[{"symbol": "AAA", "last": 30.0}, {"symbol": "BBB", "last": 45.0}],
        baselines=SimpleNamespace(index={}, atr_pct=None),
    )
    now_et = _dt(2026, 9, 1, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    monitor._brain_score_menu(now_et)
    rows = db.query("SELECT symbol, strategy, executed, approved FROM brain_scores ORDER BY symbol")
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]
    assert all(r["executed"] == 0 for r in rows)  # learning, not trading
    assert all(r["strategy"] == "SCAN" for r in rows)
    assert monitor._brain_shadow["approved_new"] == 2  # dots flow

    monitor._brain_score_menu(now_et)  # same minute again → deduped
    assert len(db.query("SELECT * FROM brain_scores")) == 2
    db.close()


def test_resolution_skips_unexecuted_scores(tmp_path, monkeypatch):
    import waveapp.config as config_module
    from waveapp.persistence.db import Database

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    db = Database(tmp_path / "t.db")
    db.migrate()
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = db
    db.execute(
        "INSERT INTO brain_scores (ts, symbol, strategy, p_win, approved, features, executed)"
        " VALUES ('2026-08-30T14:31:00+00:00', 'SCANONLY', 'SCAN', 0.6, 1, '{}', 0)"
    )
    monitor._resolve_brain_scores()
    assert db.query("SELECT * FROM brain_scores")[0]["won"] is None  # untouched
    db.close()


def test_scanner2_loop_heals_the_bar_tap(tmp_path, monkeypatch):
    """Day-1 bug regression: scanner2 starting before the hub must attach
    the taps as soon as the hub exists."""
    import asyncio as _asyncio

    import waveapp.config as config_module

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    monitor = ConnectionMonitor(on_status=lambda *a: None)

    scanner = SimpleNamespace(
        on_bar_msg=lambda bar: None,
        on_status=lambda s, h: None,
        latest_quotes={},
        last_menu=[],
        stream_bars_ts=0.0,
        ui_events=[],
        news_feed=[],
        minute_tick=lambda now: None,
        rerank_only=lambda now: [],
        update_focus=lambda: [],
        _events_seen=0,
        _fetch_snapshots=None,
    )
    monitor._scanner2 = scanner
    hub = SimpleNamespace(bar_tap=None, status_tap=None, latest_quotes={"X": 1})
    monitor._hub = hub

    async def one_tick():
        task = _asyncio.ensure_future(monitor._scanner2_loop())
        await _asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task

    _asyncio.run(one_tick())
    assert hub.bar_tap is scanner.on_bar_msg, "tap not healed"
    assert hub.status_tap is scanner.on_status
    assert scanner.latest_quotes is hub.latest_quotes
