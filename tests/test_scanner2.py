"""Scanner 2.0 S1 shadow tests (2026-08-31).

The contract: watch everything tradable, measure volume against the
same-minute-of-day baseline, gate the MENU by the §7 floors, journal every
minute, know every universe add/drop — and never touch the live pipeline.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from waveapp.engine.scanner2 import (
    _FALLBACK,
    MINUTES,
    PM_MINUTES,
    BaselineStore,
    Scanner2,
    minute_index,
)

ET = ZoneInfo("America/New_York")


def _dt(h, m):
    return datetime(2026, 9, 1, h, m, tzinfo=ET)


def test_minute_index_covers_the_honest_day():
    assert minute_index(_dt(4, 0)) == 0
    assert minute_index(_dt(9, 29)) == PM_MINUTES - 1
    assert minute_index(_dt(9, 30)) == PM_MINUTES
    assert minute_index(_dt(15, 59)) == MINUTES - 1
    assert minute_index(_dt(16, 0)) is None
    assert minute_index(_dt(3, 59)) is None  # overnight prints stay OUT


def test_fallback_curve_is_monotone_and_u_shaped():
    assert _FALLBACK[0] == 0.0
    assert abs(_FALLBACK[-1] - 1.0) < 1e-5
    assert np.all(np.diff(_FALLBACK) >= -1e-9)  # cumulative → monotone
    # open and close are busier than midday (U-shape)
    open_rate = _FALLBACK[PM_MINUTES + 30] - _FALLBACK[PM_MINUTES]
    midday_rate = _FALLBACK[PM_MINUTES + 180] - _FALLBACK[PM_MINUTES + 150]
    close_rate = _FALLBACK[MINUTES - 1] - _FALLBACK[MINUTES - 31]
    assert open_rate > midday_rate
    assert close_rate > midday_rate


def test_baseline_store_learns_curves_and_round_trips(tmp_path):
    store = BaselineStore(path=tmp_path / "b.npz")
    day = np.tile(np.linspace(0, 1_000_000, MINUTES, dtype=np.float32), (2, 1))
    store.update_from_day(["AAA", "BBB"], day)
    store.update_from_day(["AAA", "BBB"], day * 3)
    store.update_from_day(["AAA", "BBB"], day * 2)  # 3 observed days → trusted
    rows = np.array([store.index["AAA"]])
    mid = store.expected_cum(rows, MINUTES // 2)[0]
    assert 900_000 < mid < 1_100_000  # mean of (0.5, 1.5, 1.0)M at halfway
    store.adv[:] = 5_000_000.0
    store.atr_pct[:] = 2.0
    store.save()
    reloaded = BaselineStore(path=tmp_path / "b.npz")
    assert reloaded.load()
    assert reloaded.symbols == ["AAA", "BBB"]
    assert reloaded.days[0] == 3.0
    assert float(reloaded.adv[0]) == 5_000_000.0


def test_baseline_fallback_used_until_three_days_observed(tmp_path):
    store = BaselineStore(path=tmp_path / "b.npz")
    store.ensure(["NEW"])
    store.adv[0] = 1_000_000.0
    rows = np.array([0])
    expected = store.expected_cum(rows, PM_MINUTES)[0]  # at the open
    assert abs(expected - 1_000_000.0 * _FALLBACK[PM_MINUTES]) < 1.0


def _asset(symbol, name="", tradable=True, asset_class="AssetClass.US_EQUITY"):
    return SimpleNamespace(symbol=symbol, name=name, tradable=tradable, asset_class=asset_class)


def test_universe_tracks_adds_and_drops(tmp_path, monkeypatch):
    import waveapp.engine.scanner2 as s2mod

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    added, dropped = scanner.set_universe(
        [
            _asset("AAA"),
            _asset("BBB"),
            _asset("SKIP", tradable=False),
            _asset("BTCUSD", asset_class="AssetClass.CRYPTO"),
        ]
    )
    assert scanner.symbols == ["AAA", "BBB"]  # only tradable equities
    assert added == [] and dropped == []  # first run: no previous file

    scanner2 = Scanner2(data_client=None, database=None)
    scanner2.baselines = BaselineStore(path=tmp_path / "b.npz")
    added, dropped = scanner2.set_universe([_asset("AAA"), _asset("CCC")])
    assert added == ["CCC"]
    assert dropped == ["BBB"]  # the requirement: drops are KNOWN


class _Snap:
    def __init__(self, price, cum_vol, day_open, prev_close):
        self.daily_bar = SimpleNamespace(volume=cum_vol, open=day_open, close=price)
        self.previous_daily_bar = SimpleNamespace(close=prev_close)
        self.latest_trade = SimpleNamespace(price=price)


class _FakeClient:
    """Returns whatever snapshot dict the test loads into it."""

    def __init__(self):
        self.snapshots = {}

    def get_stock_snapshot(self, request):
        return {s: v for s, v in self.snapshots.items() if s in request.symbol_or_symbols}


@pytest.mark.asyncio
async def test_sleeper_wakes_up_and_enters_the_menu(tmp_path, monkeypatch):
    """THE RBLX TEST: flat pre-market, invisible at 9:28 — when it starts
    doing 8x its normal volume at 10:47 it must be on the menu that minute."""
    import waveapp.engine.scanner2 as s2mod

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    client = _FakeClient()
    scanner = Scanner2(data_client=client, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([_asset("SLPR"), _asset("QUIET")])
    # both symbols have known baselines: ADV 2M, normal cumulative curves
    curve = np.linspace(0, 2_000_000, MINUTES, dtype=np.float32)[None, :].repeat(2, axis=0)
    scanner.baselines.update_from_day(["SLPR", "QUIET"], curve)
    scanner.baselines.update_from_day(["SLPR", "QUIET"], curve)
    scanner.baselines.update_from_day(["SLPR", "QUIET"], curve)
    scanner.baselines.adv[:] = 2_000_000.0
    scanner.baselines.atr_pct[:] = 2.0

    minute = minute_index(_dt(10, 47))
    normal = float(curve[0, minute])
    # 9:28-style morning: both quiet → QUIET ranks by tiny margins, fine
    client.snapshots = {
        "SLPR": _Snap(price=40.0, cum_vol=normal, day_open=40.0, prev_close=40.0),
        "QUIET": _Snap(price=50.0, cum_vol=normal, day_open=50.0, prev_close=50.0),
    }
    menu = await scanner.step(_dt(10, 46))
    baseline_ranks = {m["symbol"]: m["rvol"] for m in menu}
    assert abs(baseline_ranks["SLPR"] - 1.0) < 0.1  # normal volume ⇒ RVOL ≈ 1

    # 10:47: SLPR wakes up — 8× its normal cumulative volume, +4% on the day
    client.snapshots["SLPR"] = _Snap(price=41.6, cum_vol=normal * 8, day_open=40.0, prev_close=40.0)
    menu = await scanner.step(_dt(10, 47))
    assert menu[0]["symbol"] == "SLPR", f"sleeper not on top: {menu}"
    assert menu[0]["rvol"] > 6.0


@pytest.mark.asyncio
async def test_menu_floors_and_leveraged_cap(tmp_path, monkeypatch):
    import waveapp.engine.scanner2 as s2mod

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    client = _FakeClient()
    scanner = Scanner2(data_client=client, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    assets = [_asset("GOOD"), _asset("CHEAP"), _asset("THIN")] + [
        _asset(f"LEV{i}", name=f"Ultra 3X Bull Fund {i}") for i in range(8)
    ]
    scanner.set_universe(assets)
    curve = np.linspace(0, 1_000_000, MINUTES, dtype=np.float32)
    names = scanner.symbols
    scanner.baselines.update_from_day(names, curve[None, :].repeat(len(names), axis=0))
    scanner.baselines.update_from_day(names, curve[None, :].repeat(len(names), axis=0))
    scanner.baselines.update_from_day(names, curve[None, :].repeat(len(names), axis=0))
    scanner.baselines.adv[:] = 2_000_000.0
    scanner.baselines.atr_pct[:] = 2.0
    # THIN fails the ADV floor
    scanner.baselines.adv[scanner.baselines.index["THIN"]] = 100_000.0

    minute = minute_index(_dt(10, 0))
    normal = float(curve[minute])
    hot = lambda px: _Snap(price=px, cum_vol=normal * 10, day_open=px * 0.97, prev_close=px * 0.96)  # noqa: E731
    client.snapshots = {s: hot(30.0) for s in names}
    client.snapshots["CHEAP"] = hot(9.0)  # below the $15 floor

    menu = await scanner.step(_dt(10, 0))
    symbols = [m["symbol"] for m in menu]
    assert "CHEAP" not in symbols  # price floor
    assert "THIN" not in symbols  # ADV floor
    assert "GOOD" in symbols
    leveraged_admitted = sum(1 for s in symbols if s.startswith("LEV"))
    assert leveraged_admitted <= 5  # leveraged cap (25% of 20 = 5)


@pytest.mark.asyncio
async def test_shadow_journal_writes_menu_rows(tmp_path, monkeypatch):
    import waveapp.engine.scanner2 as s2mod
    from waveapp.persistence.db import Database

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    db = Database(tmp_path / "t.db")
    db.migrate()
    client = _FakeClient()
    scanner = Scanner2(data_client=client, database=db)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([_asset("JRNL")])
    curve = np.linspace(0, 1_000_000, MINUTES, dtype=np.float32)[None, :]
    for _ in range(3):
        scanner.baselines.update_from_day(["JRNL"], curve)
    scanner.baselines.adv[:] = 2_000_000.0
    scanner.baselines.atr_pct[:] = 2.0
    minute = minute_index(_dt(11, 0))
    client.snapshots = {
        "JRNL": _Snap(
            price=25.0, cum_vol=float(curve[0, minute]) * 5, day_open=24.0, prev_close=23.8
        )  # noqa: E501
    }
    menu = await scanner.step(_dt(11, 0))
    assert menu and menu[0]["symbol"] == "JRNL"
    rows = db.query("SELECT * FROM scanner2_menu")
    assert len(rows) == 1 and rows[0]["symbol"] == "JRNL" and rows[0]["rank"] == 1
    snaps = db.query("SELECT * FROM scanner2_snapshots")
    assert len(snaps) == 1
    db.close()


def test_day_roll_folds_baselines(tmp_path, monkeypatch):
    import waveapp.engine.scanner2 as s2mod

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([_asset("ROLL")])
    scanner._day = "2026-08-31"
    scanner.day_curve[0, :] = np.linspace(0, 500_000, MINUTES, dtype=np.float32)
    scanner.last[0] = 42.0
    scanner._roll_day(_dt(9, 40))  # new day → fold yesterday
    row = scanner.baselines.index["ROLL"]
    assert scanner.baselines.days[row] == 1.0
    assert scanner.baselines.curve[row, -1] == pytest.approx(500_000, rel=1e-3)
    assert scanner.baselines.prev_close[row] == pytest.approx(42.0)
    assert float(scanner.day_curve.max()) == 0.0  # today starts clean


def test_universe_accepts_wave_asset_info_without_asset_class(tmp_path, monkeypatch):
    """2026-09-01 bug: Wave's AssetInfo has no asset_class field — the old
    filter silently emptied the whole universe. Missing field = equity."""
    import waveapp.engine.scanner2 as s2mod
    from waveapp.broker.base import AssetInfo

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    assets = [
        AssetInfo(
            symbol="AAPL",
            name="Apple",
            exchange="NASDAQ",
            tradable=True,
            shortable=True,
            easy_to_borrow=True,
            fractionable=True,
        ),
        AssetInfo(
            symbol="DEAD",
            name="Gone",
            exchange="NYSE",
            tradable=False,
            shortable=False,
            easy_to_borrow=False,
            fractionable=False,
        ),
    ]
    scanner.set_universe(assets)
    assert scanner.symbols == ["AAPL"], "Wave AssetInfo objects must populate the universe"


def test_universe_wipe_guard_keeps_previous_list(tmp_path, monkeypatch):
    """A broken feed shrinking 13k → a stub must be refused, not adopted."""
    import waveapp.engine.scanner2 as s2mod

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    big = [_asset(f"S{i:05d}") for i in range(1500)]
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe(big)
    assert len(scanner.symbols) == 1500

    scanner2 = Scanner2(data_client=None, database=None)
    scanner2.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner2.set_universe(big[:3])  # broken feed: 3 symbols
    assert len(scanner2.symbols) == 1500, "wipe guard must keep the known universe"


# ---- Architecture B (S2): stream ingest, triggers, focus, boosts ----------


def _b_scanner(tmp_path, monkeypatch, symbols=("HOT", "COLD")):
    import waveapp.engine.scanner2 as s2mod

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([_asset(s) for s in symbols])
    curve = np.linspace(0, 2_000_000, MINUTES, dtype=np.float32)[None, :].repeat(
        len(symbols), axis=0
    )  # noqa: E501
    for _ in range(3):
        scanner.baselines.update_from_day(list(symbols), curve)
    scanner.baselines.adv[:] = 2_000_000.0
    scanner.baselines.atr_pct[:] = 2.0
    scanner._day = _dt(10, 0).date().isoformat()  # today already rolled
    return scanner


def _bar(symbol, when, close, volume, high=None, open_=None, low=None):
    return SimpleNamespace(
        symbol=symbol,
        timestamp=when,
        open=open_ or close,
        high=high or close,
        low=low or close,
        close=close,
        volume=volume,
    )


def test_stream_ingest_triggers_fire(tmp_path, monkeypatch):
    """One symbol wakes up on the stream: RVOL cross fires once, HOD break
    fires on participation, volume spike fires on a 5× minute."""
    scanner = _b_scanner(tmp_path, monkeypatch)
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    scanner.day_open[scanner._index["HOT"]] = 40.0

    # dump 3× the normal day-so-far volume in one bar → rvol cross + spike
    scanner.on_bar_msg(_bar("HOT", _dt(10, 30), close=41.5, volume=normal_cum * 3, high=41.6))
    kinds = [e[2] for e in scanner._pending_events]
    assert "rvol_cross" in kinds
    assert "vol_spike" in kinds
    assert scanner.stream_bars_ts > 0

    # second bar makes a new high with rvol>2 and day% ≈ +4 → hod_break, and
    # the rvol cross must NOT fire again (armed once)
    scanner._pending_events.clear()
    scanner.on_bar_msg(_bar("HOT", _dt(10, 31), close=41.8, volume=50_000, high=41.9))
    kinds = [e[2] for e in scanner._pending_events]
    assert "hod_break" in kinds
    assert "rvol_cross" not in kinds
    assert scanner.event_score[scanner._index["HOT"]] >= 3.0


def test_stream_state_feeds_the_rank_without_rest(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch)
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    row = scanner._index["HOT"]
    scanner.day_open[row] = 40.0
    scanner.prev_close_live[row] = 39.8
    scanner.on_bar_msg(_bar("HOT", _dt(10, 30), close=41.5, volume=normal_cum * 5, high=41.6))
    menu = scanner.rerank_only(_dt(10, 30))
    assert menu and menu[0]["symbol"] == "HOT"
    assert menu[0]["rvol"] > 3.0  # pure stream state, no snapshots involved


def test_event_leaderboard_decays(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch)
    row = scanner._index["HOT"]
    scanner.event_score[row] = 8.0
    scanner.decay_events(_dt(10, 0).timestamp())
    scanner.decay_events(_dt(11, 30).timestamp())  # one half-life (90 min)
    assert 3.5 < scanner.event_score[row] < 4.5


def test_news_and_second_day_and_cluster_boosts(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch, symbols=("PLAIN", "NEWSY", "YDAY", "COIN", "MARA"))
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    for s in ("PLAIN", "NEWSY", "YDAY", "COIN", "MARA"):
        row = scanner._index[s]
        scanner.day_open[row] = 40.0
        scanner.prev_close_live[row] = 40.0
        scanner.on_bar_msg(_bar(s, _dt(10, 30), close=41.0, volume=normal_cum * 2))
    # identical stats → boosts decide the order
    scanner.on_news(["NEWSY"], _dt(10, 30).timestamp())
    scanner.second_day = {"YDAY"}
    row = scanner._index["COIN"]  # crypto anchor runs hot → MARA gets chain boost
    scanner.day_open[row] = 40.0
    scanner.on_bar_msg(_bar("COIN", _dt(10, 31), close=42.0, volume=normal_cum * 2))
    menu = scanner.rerank_only(_dt(10, 31))
    order = [m["symbol"] for m in menu]
    assert order.index("NEWSY") < order.index("PLAIN"), "news catalyst must outrank plain"
    assert order.index("YDAY") < order.index("PLAIN"), "2nd-day play must outrank plain"
    assert order.index("MARA") < order.index("PLAIN"), "cluster member must outrank plain"


def test_focus_manager_promotes_with_hysteresis(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch)
    scanner.last_menu = [{"symbol": "HOT"}]
    assert scanner.update_focus() == []  # first appearance: streak 1, no promo
    promoted = scanner.update_focus()  # second consecutive: promoted
    assert promoted == ["HOT"]
    assert "HOT" in scanner.focus
    assert scanner.update_focus() == []  # already in focus — never re-promoted


def test_minute_tick_journals_from_stream_state(tmp_path, monkeypatch):
    import waveapp.engine.scanner2 as s2mod
    from waveapp.persistence.db import Database

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    db = Database(tmp_path / "t.db")
    db.migrate()
    scanner = Scanner2(data_client=None, database=db)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([_asset("TICK")])
    curve = np.linspace(0, 2_000_000, MINUTES, dtype=np.float32)[None, :]
    for _ in range(3):
        scanner.baselines.update_from_day(["TICK"], curve)
    scanner.baselines.adv[:] = 2_000_000.0
    scanner.baselines.atr_pct[:] = 2.0
    scanner._day = _dt(10, 0).date().isoformat()
    minute = minute_index(_dt(10, 30))
    row = scanner._index["TICK"]
    scanner.day_open[row] = 40.0
    scanner.prev_close_live[row] = 39.9
    scanner.on_bar_msg(
        _bar("TICK", _dt(10, 30), close=41.0, volume=2_000_000 * minute / MINUTES * 4)
    )  # noqa: E501
    scanner.rerank_only(_dt(10, 30))
    scanner.minute_tick(_dt(10, 30))
    assert db.query("SELECT * FROM scanner2_menu")
    assert db.query("SELECT * FROM scanner2_events")  # triggers journaled too
    db.close()


# ---- 100% completion pieces (2026-09-01) -------------------


def test_lunch_consolidation_breakout_fires_once(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch)
    row = scanner._index["HOT"]
    scanner.day_open[row] = 40.0
    # build the 11:00–13:30 range around 40.0–40.5
    scanner.on_bar_msg(_bar("HOT", _dt(11, 30), close=40.2, volume=3_000, high=40.5, low=40.0))
    scanner.on_bar_msg(_bar("HOT", _dt(12, 30), close=40.3, volume=3_000, high=40.4, low=40.1))
    assert float(scanner.lunch_hi[row]) == pytest.approx(40.5)
    scanner._pending_events.clear()
    # afternoon: in play (rvol≥2) and breaking the range on heavy volume
    minute = minute_index(_dt(14, 0))
    scanner.cum_vol[row] = scanner.cum_vol_stream[row] = 2_000_000 * minute / MINUTES * 3
    scanner.on_bar_msg(_bar("HOT", _dt(14, 0), close=40.9, volume=60_000, high=40.95))
    kinds = [e[2] for e in scanner._pending_events]
    assert "consol_break" in kinds
    scanner._pending_events.clear()
    scanner.on_bar_msg(_bar("HOT", _dt(14, 1), close=41.2, volume=60_000, high=41.3))
    assert "consol_break" not in [e[2] for e in scanner._pending_events]  # fires ONCE


def test_halt_and_resume_become_events(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch)
    scanner.on_status("HOT", True)
    scanner.on_status("HOT", False)
    kinds = [e[2] for e in scanner._pending_events]
    assert kinds.count("halt") == 1 and kinds.count("resume") == 1
    assert scanner.event_score[scanner._index["HOT"]] >= 2.0


def test_earnings_and_attention_boosts(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch, symbols=("PLAIN", "ERN", "APE"))
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    for s in ("PLAIN", "ERN", "APE"):
        row = scanner._index[s]
        scanner.day_open[row] = 40.0
        scanner.prev_close_live[row] = 40.0
        scanner.on_bar_msg(_bar(s, _dt(10, 30), close=41.0, volume=normal_cum * 2))
    scanner.earnings_today = {"ERN"}
    scanner.attention = {"APE"}
    menu = scanner.rerank_only(_dt(10, 30))
    order = [m["symbol"] for m in menu]
    assert order.index("ERN") < order.index("PLAIN")
    assert order.index("APE") < order.index("PLAIN")


def test_ui_events_feed_the_mind(tmp_path, monkeypatch):
    scanner = _b_scanner(tmp_path, monkeypatch)
    row = scanner._index["HOT"]
    scanner._fire(_dt(10, 30), row, "hod_break", "")
    assert scanner.ui_events == [("HOT", "hod_break")]


def test_shares_out_round_trips(tmp_path):
    store = BaselineStore(path=tmp_path / "b.npz")
    store.ensure(["FLT"])
    store.shares_out[0] = 18_000_000.0
    store.save()
    reloaded = BaselineStore(path=tmp_path / "b.npz")
    assert reloaded.load()
    assert float(reloaded.shares_out[0]) == 18_000_000.0


def test_journal_features_carry_the_full_vector(tmp_path, monkeypatch):
    """The ML dataset must include hod/vwap/spread/events/news/shares_out."""
    import json as _json

    import waveapp.engine.scanner2 as s2mod
    from waveapp.persistence.db import Database

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    db = Database(tmp_path / "t.db")
    db.migrate()
    scanner = Scanner2(data_client=None, database=db)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([_asset("FULL")])
    curve = np.linspace(0, 2_000_000, MINUTES, dtype=np.float32)[None, :]
    for _ in range(3):
        scanner.baselines.update_from_day(["FULL"], curve)
    scanner.baselines.adv[:] = 2_000_000.0
    scanner.baselines.atr_pct[:] = 2.0
    scanner.baselines.shares_out[:] = 25_000_000.0
    scanner._day = _dt(10, 0).date().isoformat()
    row = scanner._index["FULL"]
    scanner.day_open[row] = 40.0
    scanner.prev_close_live[row] = 39.9
    minute = minute_index(_dt(10, 30))
    scanner.latest_quotes = {"FULL": SimpleNamespace(bid_price=40.98, ask_price=41.02)}
    scanner.on_bar_msg(
        _bar("FULL", _dt(10, 30), close=41.0, volume=2_000_000 * minute / MINUTES * 4, high=41.1)
    )
    scanner.rerank_only(_dt(10, 30))
    scanner.minute_tick(_dt(10, 30))
    feats = _json.loads(db.query("SELECT features FROM scanner2_snapshots")[0]["features"])
    for key in ("rvol", "hod_dist", "vwap_dist", "spread", "events", "news", "shares_out"):
        assert key in feats, f"missing ML feature: {key}"
    assert feats["shares_out"] == 25_000_000.0
    assert feats["spread"] == pytest.approx(0.04)
    db.close()


def test_btc_anchor_lights_the_crypto_cluster(tmp_path, monkeypatch):
    """spec verbatim: 'Bitcoin jumps 3% → the whole crypto family
    gets bumped' — BTC itself is the primary anchor, no equity proxy needed."""
    scanner = _b_scanner(tmp_path, monkeypatch, symbols=("PLAIN", "MARA"))
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    for s in ("PLAIN", "MARA"):
        row = scanner._index[s]
        scanner.day_open[row] = 40.0
        scanner.prev_close_live[row] = 40.0
        scanner.on_bar_msg(_bar(s, _dt(10, 30), close=41.0, volume=normal_cum * 2))
    menu = scanner.rerank_only(_dt(10, 30))
    flat = {m["symbol"]: m["score"] for m in menu}
    scanner.btc_day_pct = 4.2  # bitcoin runs — no equity anchor is hot
    menu = scanner.rerank_only(_dt(10, 30))
    hot = {m["symbol"]: m["score"] for m in menu}
    assert hot["MARA"] > flat["MARA"] * 1.2  # chain boosted
    assert hot["PLAIN"] == pytest.approx(flat["PLAIN"], rel=1e-5)  # bystander untouched


# ---- S3 (the short side): weak-side triggers, flag-gated -------------------


def test_lod_break_silent_while_shorts_disabled(tmp_path, monkeypatch):
    """Prime directive: with shorts_enabled=False (the default) a new-low
    weak tape fires NO lod_break — event_score feeds the menu rank, so an
    ungated weak-side event would reshuffle the LONG-only menu."""
    scanner = _b_scanner(tmp_path, monkeypatch)
    assert scanner.shorts_enabled is False  # the default is OFF
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    scanner.day_open[scanner._index["HOT"]] = 40.0
    scanner.on_bar_msg(_bar("HOT", _dt(10, 30), close=39.0, volume=normal_cum * 3, low=38.9))
    scanner.on_bar_msg(_bar("HOT", _dt(10, 31), close=38.5, volume=50_000, low=38.4))
    kinds = [e[2] for e in scanner._pending_events]
    assert "lod_break" not in kinds
    # (vol_spike still fires/promotes on |day_pct| — pre-existing side-blind
    # behavior, unchanged by S3; only the lod_break event itself is gated)
    assert "vol_spike" in kinds


def test_lod_break_fires_on_new_low_tape_when_enabled(tmp_path, monkeypatch):
    """S3 mirror of the hod_break test: shorts_enabled=True + a new LOW of
    day on participation (same rvol ruler, day% ≤ −HOD_MIN_DAY_PCT) fires a
    direction-tagged lod_break and promotes the weak igniter."""
    scanner = _b_scanner(tmp_path, monkeypatch)
    scanner.shorts_enabled = True
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    scanner.day_open[scanner._index["HOT"]] = 40.0
    # bar 1 seeds the LOD (prior_lod == 0 → no fire, mirroring prior_hod)
    scanner.on_bar_msg(_bar("HOT", _dt(10, 30), close=39.0, volume=normal_cum * 3, low=38.9))
    assert "lod_break" not in [e[2] for e in scanner._pending_events]
    # bar 2: new LOD, rvol ≥ trigger, day −3.75% → lod_break, dir-tagged
    scanner.on_bar_msg(_bar("HOT", _dt(10, 31), close=38.5, volume=50_000, low=38.4))
    events = [(e[2], e[3]) for e in scanner._pending_events]
    assert any(k == "lod_break" and "dir=short" in d for k, d in events)
    assert "HOT" in scanner.promotion_queue  # weak igniter promoted (flag-gated)


def test_negative_news_and_crashing_anchor_boost_only_when_enabled(tmp_path, monkeypatch):
    """Menu scoring symmetry (S3): a negative headline and a crashing
    cluster anchor boost weakness ONLY behind shorts_enabled — flag off,
    today's long-only ranking is bit-identical."""
    scanner = _b_scanner(tmp_path, monkeypatch, symbols=("PLAIN", "BADNEWS", "COIN", "MARA"))
    minute = minute_index(_dt(10, 30))
    normal_cum = 2_000_000 * minute / MINUTES
    for s in ("PLAIN", "BADNEWS", "COIN", "MARA"):
        row = scanner._index[s]
        scanner.day_open[row] = 40.0
        scanner.prev_close_live[row] = 40.0
        scanner.on_bar_msg(_bar(s, _dt(10, 30), close=41.0, volume=normal_cum * 2))
    scanner.on_news(["BADNEWS"], _dt(10, 30).timestamp(), direction=-1)
    # crash the crypto anchor: COIN −8% on the day on QUIET volume (rvol
    # stays under the 3.0 anchor bar, so only the day_pct leg can light it)
    row = scanner._index["COIN"]
    scanner.on_bar_msg(_bar("COIN", _dt(10, 31), close=36.8, volume=10_000))
    off = {m["symbol"]: m["score"] for m in scanner.rerank_only(_dt(10, 31))}
    assert off["BADNEWS"] == pytest.approx(off["PLAIN"], rel=1e-5)  # no boost, flag off
    scanner.shorts_enabled = True
    on = {m["symbol"]: m["score"] for m in scanner.rerank_only(_dt(10, 31))}
    assert on["BADNEWS"] > on["PLAIN"] * 1.2  # negative news now boosts
    assert on["MARA"] > off["MARA"] * 1.2  # crashing anchor lights the chain
    assert on["PLAIN"] == pytest.approx(off["PLAIN"], rel=1e-5)  # bystander untouched
