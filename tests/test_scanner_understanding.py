"""Blueprint layer 4 (P1) — scanner understanding as JOURNALED FEATURES:
4.1 self-computed market internals → MarketRegime, 4.2 daily-context,
4.3 news novelty/relevance. Features first, gates second (the multi-filter
overfit trap) — nothing here blocks a trade."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np

from waveapp.engine.newsintel import NoveltyTracker, relevance
from waveapp.persistence.db import Database

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)  # 11:00 ET Tuesday


def _scanner(tmp_path, monkeypatch, n=300, database=None):
    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=database)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    symbols = [f"S{i:04d}" for i in range(n)] + list(Scanner2.SECTOR_ETFS)
    scanner.set_universe([SimpleNamespace(symbol=s, name="", tradable=True) for s in symbols])
    return scanner


# -- 4.3 novelty ------------------------------------------------------------


def test_novelty_first_print_then_decay():
    tracker = NoveltyTracker()
    t0 = NOW.timestamp()
    assert tracker.assess("EIX", "Edison settles wildfire claims for $1B", t0) == 1.0
    # near-duplicate re-hash 3 hours later → stale
    n2 = tracker.assess("EIX", "Edison settles wildfire claims for $1 billion", t0 + 3 * 3600)
    assert n2 < 0.6
    # a genuinely different story stays fresh
    assert tracker.assess("EIX", "Edison CEO steps down immediately", t0 + 4 * 3600) == 1.0
    # outside the 24h window the slate is clean
    assert tracker.assess("EIX", "Edison settles wildfire claims for $1B", t0 + 30 * 3600) == 1.0


def test_relevance_ticker_in_headline():
    assert relevance("EIX", "EIX surges on settlement news") == 1
    assert relevance("EIX", "Utilities rally broadly") == 0
    assert relevance("A", "NVDA beats — A word about margins") == 1  # word-bounded


def test_stale_news_never_relights_the_boost(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=10)
    t0 = NOW.timestamp()
    scanner.on_news(
        ["S0001"], t0, headline="S0001 wins huge contract", category="deal", direction=1
    )
    assert scanner.news_ts["S0001"] == t0
    assert scanner.news_novelty["S0001"] == 1.0
    tape_len = len(scanner.news_feed)
    # the same story re-hashed 2 hours later: anchor NOT refreshed, no tape re-entry
    scanner.on_news(
        ["S0001"], t0 + 7200, headline="S0001 wins huge contract", category="deal", direction=1
    )
    assert scanner.news_ts["S0001"] == t0
    assert scanner.news_novelty["S0001"] < 1.0
    assert len(scanner.news_feed) == tape_len
    # journaled features carry novelty + relevance
    feats = scanner.live_feature_row("S0001")
    assert feats["news_nov"] < 1.0
    assert feats["news_rel"] == 1


# -- 4.1 market internals ---------------------------------------------------


def _paint_tape(scanner, up_fraction: float, rvol: float = 1.0) -> None:
    n = len(scanner.symbols)
    ups = int(n * up_fraction)
    scanner.prev_close_live[:] = 100.0
    scanner.last[:ups] = 102.0
    scanner.last[ups:] = 98.0
    scanner.cum_vol[:] = 1_000_000.0
    scanner.last_dir[:ups] = 1
    scanner.last_dir[ups:] = -1
    scanner.vwap_v[:] = 1.0
    scanner.vwap_pv[:] = 100.0  # vwap 100 → ups above, downs below
    score = np.zeros(n, dtype=np.float32)
    rv = np.full(n, rvol, dtype=np.float32)
    scanner._journal_arrays = (score, rv, score, score)


def test_internals_trend_up_day(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch)
    _paint_tape(scanner, up_fraction=0.85, rvol=1.4)
    out = scanner.compute_market_internals(NOW)
    assert out["regime"] == "TREND_UP"
    assert out["score"] >= 0.5
    assert out["breadth"] > 0.5
    assert out["above_vwap"] > 0.8


def test_internals_dead_and_panic_days(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch)
    _paint_tape(scanner, up_fraction=0.5, rvol=0.6)
    assert scanner.compute_market_internals(NOW)["regime"] == "DEAD"
    _paint_tape(scanner, up_fraction=0.08, rvol=2.5)
    assert scanner.compute_market_internals(NOW)["regime"] == "PANIC"


def test_internals_rotation_needs_dispersion(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch)
    _paint_tape(scanner, up_fraction=0.5, rvol=1.2)
    # stretch two sector ETFs apart → rotation tape
    for sym, px in (("XLE", 103.0), ("XLU", 97.0)):
        scanner.last[scanner._index[sym]] = px
    out = scanner.compute_market_internals(NOW)
    assert out["dispersion"] >= 1.5
    assert out["regime"] == "ROTATION"


def test_internals_journal_market_row_and_warmup(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    db.migrate()
    scanner = _scanner(tmp_path, monkeypatch, database=db)
    _paint_tape(scanner, up_fraction=0.8, rvol=1.3)
    scanner.compute_market_internals(NOW)
    rows = db.query("SELECT * FROM scanner2_snapshots WHERE symbol='_MARKET'")
    assert len(rows) == 1
    # a warm-up sliver (few symbols priced) stays honest
    tiny = _scanner(tmp_path, monkeypatch, n=20)
    assert scanner.market_regime["regime"] != "WARMUP"
    assert tiny.compute_market_internals(NOW)["regime"] == "WARMUP"
    # the labeler never eats the _MARKET row
    from waveapp.engine.brain_nightly import pending_symbol_days

    assert all(sym != "_MARKET" for sym, _d, _s in pending_symbol_days(db))


def test_hod_break_rate_counts_and_resets(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=150)
    scanner._hod_breaks_min = 7
    _paint_tape(scanner, up_fraction=0.6, rvol=1.0)
    out = scanner.compute_market_internals(NOW)
    assert out["hod_rate"] == 7.0
    assert scanner._hod_breaks_min == 0  # per-minute counter reset


# -- 4.2 daily-context ------------------------------------------------------


def test_daily_ctx_flows_into_the_journal(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=5)
    base = scanner.baselines
    row = base.index["S0001"]
    base.daily_ctx[row] = (35.0, 4.0, 3.2, 0.92, -18.0)  # washed-out shape
    feats = scanner.live_feature_row("S0001")
    assert feats["off_52w"] == 35.0
    assert feats["red_days"] == 4.0
    assert feats["pdv_ratio"] == 3.2
    assert feats["pd_close_loc"] == 0.92
    assert feats["ret_5d"] == -18.0
    # v2 vector consumes them
    from waveapp.engine.brain_features import v2_vector

    vec = v2_vector(feats, NOW)
    assert vec["off_52w"] == 35.0
    assert vec["red_days"] == 4.0


def test_daily_ctx_defaults_to_zero_before_backfill(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=3)
    feats = scanner.live_feature_row("S0000")
    assert feats["off_52w"] == 0.0
    assert feats["red_days"] == 0.0


# -- 4.8 EDGAR form semantics (P2 kickoff, 2026-09-02) -----------------------


def test_filing_semantics_journal_dilution_and_activist(tmp_path, monkeypatch):
    import time as time_mod

    scanner = _scanner(tmp_path, monkeypatch, n=5)
    now = time_mod.time()
    scanner.flag_filing("S0001", "S-3,424B5", now)
    scanner.flag_filing("S0002", "SC 13D", now)
    feats1 = scanner.live_feature_row("S0001")
    feats2 = scanner.live_feature_row("S0002")
    feats3 = scanner.live_feature_row("S0003")
    assert feats1["dilution"] == 1 and feats1["activist"] == 0
    assert feats2["activist"] == 1 and feats2["dilution"] == 0
    assert feats3["dilution"] == 0 and feats3["activist"] == 0
    # windows expire: a 3-day-old dilution flag no longer journals
    scanner.dilution_ts["S0001"] = now - 3 * 86400
    assert scanner.live_feature_row("S0001")["dilution"] == 0
    # an 8-K is neither
    scanner.flag_filing("S0004", "8-K", now)
    assert scanner.live_feature_row("S0004")["dilution"] == 0
    assert scanner.live_feature_row("S0004")["activist"] == 0


# -- 4.9 halts · 4.10 flat-day leader · VIX state (P2, 2026-09-02) -----------


def test_halt_count_journals_and_resets_daily(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=5)
    scanner.on_status("S0001", halted=True)
    scanner.on_status("S0001", halted=False)  # resume never counts
    scanner.on_status("S0001", halted=True)
    scanner.on_status("S0001", halted=True)
    assert scanner.live_feature_row("S0001")["halts"] == 3  # exhaustion zone
    assert scanner.live_feature_row("S0002")["halts"] == 0
    scanner._day = "2000-01-01"  # force the day roll
    scanner._roll_day(NOW.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")))
    assert scanner.halt_count == {}


def test_rs_leader_needs_flat_spy_and_a_pressing_leader(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe([NS(symbol=s, name="", tradable=True) for s in ("LEAD", "SPY")])
    spy, lead = scanner._index["SPY"], scanner._index["LEAD"]
    scanner.prev_close_live[spy], scanner.last[spy] = 640.0, 640.5  # +0.08% flat
    scanner.prev_close_live[lead], scanner.last[lead] = 50.0, 51.5  # +3%
    scanner.hod[lead] = 51.6  # within 1% of HOD
    scanner.vwap_pv[lead], scanner.vwap_v[lead] = 51.0, 1.0  # above VWAP
    assert scanner.live_feature_row("LEAD")["rs_leader"] == 1
    # SPY wakes up → the flat-day setup is gone
    scanner.last[spy] = 645.0  # +0.8%
    assert scanner.live_feature_row("LEAD")["rs_leader"] == 0
    # flat SPY but the leader fell off its high → gone
    scanner.last[spy] = 640.5
    scanner.hod[lead] = 53.0
    assert scanner.live_feature_row("LEAD")["rs_leader"] == 0


def test_vix_state_in_market_internals(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch)
    scanner.vix, scanner.vix3m = 28.0, 24.0  # backwardation — panic tape
    _paint_tape(scanner, up_fraction=0.6, rvol=1.2)
    out = scanner.compute_market_internals(NOW)
    assert out["vix"] == 28.0
    assert out["vix_ratio"] > 1.0
    calm = _scanner(tmp_path, monkeypatch, n=120)
    assert calm.compute_market_internals(NOW)["vix_ratio"] == 0.0  # feed unseen


# -- 4.7 squeeze priors + 4.6 themes (P2 final slices, 2026-09-02) -----------


def test_squeeze_composite_counts_precursors(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=5)
    base = scanner.baselines
    row = base.index["S0001"]
    base.short_ctx[row] = (22.0, 7.5, 65.0)  # SI 22% of shares, DTC 7.5
    base.daily_ctx[row] = (35.0, 4.0, 3.0, 0.9, -20.0)  # washed out
    scanner.mention_vel["S0001"] = 3.0  # mentions tripled in 24h
    feats = scanner.live_feature_row("S0001")
    assert feats["si_pct"] == 22.0
    assert feats["dtc"] == 7.5
    assert feats["sv_ratio"] == 65.0
    assert feats["mention_vel"] == 3.0
    assert feats["squeeze"] == 4  # all four live precursors lit
    clean = scanner.live_feature_row("S0002")
    assert clean["squeeze"] == 0


def test_sector_heat_finds_the_hot_theme(tmp_path, monkeypatch):
    import numpy as np

    scanner = _scanner(tmp_path, monkeypatch, n=100)
    n = len(scanner.symbols)
    scanner.sector_id = np.full(n, 10, dtype=np.int8)
    scanner.sector_id[:20] = 5  # ENERGY block
    scanner.sector_id[20:60] = 0  # TECH block
    scanner.sector_names = list(scanner.SECTOR_ETFS)  # placeholder names ok
    from scripts.scanner2_sectors import SECTORS  # real names

    scanner.sector_names = list(SECTORS)
    scanner.last[:] = 50.0
    rvol = np.ones(n, dtype=np.float32)
    rvol[:20] = 4.0  # energy is on fire
    scanner._journal_arrays = (rvol * 0, rvol, rvol * 0, rvol * 0)
    scanner._sector_heat_pass()
    assert scanner.hot_sectors and scanner.hot_sectors[0] == "ENERGY"
    feats = scanner.live_feature_row(scanner.symbols[0])
    assert feats["sector"] == 5
    assert feats["sector_heat"] == 4.0


def test_theme_graph_finds_comovers(tmp_path, monkeypatch):
    import numpy as np

    scanner = _scanner(tmp_path, monkeypatch, n=8)
    for s in ("S0000", "S0001", "S0002"):  # the theme: identical drift
        scanner._ring[s] = {}
    scanner._ring["S0003"] = {}  # the loner: opposite-phase sawtooth
    scanner._ring["SPY"] = {}
    for tick in range(1, 70):
        wave = 1.0 + 0.02 * ((tick % 10) / 10.0)  # shared shape
        for i, s in enumerate(("S0000", "S0001", "S0002")):
            scanner._ring[s][tick] = 50.0 * wave * (1 + i * 0.001)
        # deterministic scrambled series — genuinely uncorrelated loner
        wobble = ((tick * 7919) % 97) / 97.0 - 0.5
        scanner._ring["S0003"][tick] = 50.0 * (1.0 + 0.01 * wobble)
        scanner._ring["SPY"][tick] = 640.0
    scanner._ring_tick = 69
    n = len(scanner.symbols)
    rvol = np.full(n, 3.0, dtype=np.float32)
    scanner._journal_arrays = (rvol * 0, rvol, rvol * 0, rvol * 0)
    scanner._theme_graph_pass()
    assert len(scanner.themes) >= 1
    assert {"S0000", "S0001", "S0002"}.issubset(scanner.themes[0])
    assert "S0003" not in scanner.themes[0]
    assert scanner._theme_of["S0001"] >= 3
    # journal carries membership
    scanner.last[scanner._index["S0001"]] = 51.0
    assert scanner.live_feature_row("S0001")["theme_n"] >= 3


def test_sector_map_absent_means_other(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=4)
    assert (scanner.sector_id == 10).all()
    feats = scanner.live_feature_row("S0001")
    assert feats["sector"] == 10
    assert feats["sector_heat"] == 0.0


# -- Build 3: human-awareness pack (Cycle 1, 2026-09-03) ----------------------


def _bar(symbol, when, close, high=None, low=None, volume=50_000):
    return SimpleNamespace(
        symbol=symbol,
        timestamp=when,
        open=close,
        close=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        volume=volume,
    )


def test_extension_from_open_and_vwap_in_daily_atr_units(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=5)
    idx = scanner._index["S0001"]
    base = scanner.baselines
    base.atr_pct[base.index["S0001"]] = 2.0  # daily ATR = 2% of $100 = $2
    scanner.prev_close_live[idx] = 100.0
    scanner.day_open[idx] = 100.0
    scanner.last[idx] = 104.0
    scanner.vwap_pv[idx], scanner.vwap_v[idx] = 102.0, 1.0  # session vwap 102
    feats = scanner.live_feature_row("S0001")
    assert feats["ext_open_atr"] == 2.0  # (104-100)/2 — the day already ran 2 ATRs
    assert feats["ext_vwap_atr"] == 1.0  # (104-102)/2 — one ATR above VWAP
    # missing inputs (no price, no ATR) → honest zeros, never a raise
    clean = scanner.live_feature_row("S0002")
    assert clean["ext_open_atr"] == 0.0
    assert clean["ext_vwap_atr"] == 0.0


def test_range_pos_bounded_and_degenerate_range(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=5)
    idx = scanner._index["S0001"]
    scanner.hod[idx], scanner.lod[idx] = 110.0, 90.0
    scanner.last[idx] = 105.0
    assert scanner.live_feature_row("S0001")["range_pos"] == 0.75
    scanner.last[idx] = 120.0  # blown past the tracked high → clipped to 1
    assert scanner.live_feature_row("S0001")["range_pos"] == 1.0
    scanner.last[idx] = 80.0  # below the tracked low → clipped to 0
    assert scanner.live_feature_row("S0001")["range_pos"] == 0.0
    # degenerate range (hod == lod, or nothing observed yet) → 0.5
    scanner.hod[idx] = scanner.lod[idx] = 100.0
    assert scanner.live_feature_row("S0001")["range_pos"] == 0.5
    assert scanner.live_feature_row("S0002")["range_pos"] == 0.5


def test_trend_age_fresh_high_stall_and_reset(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=5)
    # 9:35 ET: first RTH bar prints the day high → trend age 0 (new highs NOW)
    scanner.on_bar_msg(_bar("S0001", datetime(2026, 9, 1, 13, 35, tzinfo=UTC), 50.0, low=49.0))
    assert scanner.live_feature_row("S0001")["trend_age_min"] == 0.0
    # 9:50 ET: no new high for 15 minutes → the trend is 15 minutes old
    scanner.on_bar_msg(
        _bar("S0001", datetime(2026, 9, 1, 13, 50, tzinfo=UTC), 49.2, high=49.5, low=49.0)
    )
    feats = scanner.live_feature_row("S0001")
    assert feats["trend_age_min"] == 15.0
    assert feats["mins_since_open"] == 20.0
    # 10:00 ET: a new day high resets the clock
    scanner.on_bar_msg(_bar("S0001", datetime(2026, 9, 1, 14, 0, tzinfo=UTC), 51.0, low=50.5))
    assert scanner.live_feature_row("S0001")["trend_age_min"] == 0.0


def test_mins_since_open_clips_premarket_to_zero(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=3)
    # 9:00 ET pre-market bar: the open clock has not started
    scanner.on_bar_msg(_bar("S0001", datetime(2026, 9, 1, 13, 0, tzinfo=UTC), 10.0))
    assert scanner.live_feature_row("S0001")["mins_since_open"] == 0.0


def test_day2_rep_from_seeded_yesterday_menu(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    db.migrate()
    ts = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    db.execute(
        "INSERT INTO scanner2_menu"
        " (ts, rank, symbol, score, rvol, gap_pct, day_pct, cum_volume, last_price)"
        " VALUES (?, 1, 'S0001', 5.0, 3.0, 4.0, 2.0, 1000000, 25.0)",
        (ts,),
    )
    scanner = _scanner(tmp_path, monkeypatch, n=5, database=db)
    scanner.seed_second_day()
    assert "S0001" in scanner.second_day
    assert scanner.live_feature_row("S0001")["day2_rep"] == 1.0
    assert scanner.live_feature_row("S0002")["day2_rep"] == 0.0


def test_awareness_keys_present_in_both_feature_paths(tmp_path, monkeypatch):
    scanner = _scanner(tmp_path, monkeypatch, n=5)
    scanner.last[scanner._index["S0001"]] = 50.0
    keys = {
        "ext_open_atr",
        "ext_vwap_atr",
        "range_pos",
        "trend_age_min",
        "mins_since_open",
        "day2_rep",
    }
    direct = scanner.feature_row(scanner._index["S0001"])
    live = scanner.live_feature_row("S0001")
    assert keys <= set(direct)  # the journal writer's dict
    assert keys <= set(live)  # the live shadow-scoring dict (same code path)


# -- 2026-09-04 07:50 resize race: the universe swap must never leave stale
# rank/journal state behind (three IndexErrors, one per pass, paged) --


def test_universe_swap_invalidates_rank_state(tmp_path, monkeypatch):
    from waveapp.broker.base import AssetInfo

    s2 = _scanner(tmp_path, monkeypatch)
    big = [AssetInfo(f"S{i:04d}", "", "XNAS", True, True, True, True) for i in range(1200)]
    s2.set_universe(big)
    # simulate a computed rank state on the big universe
    s2._journal_order = list(range(1100, 1180))
    s2._journal_arrays = (None, None, None, None)
    small = big[:1050]
    s2.set_universe(small)
    assert s2._journal_order == []  # root fix: stale ranks cleared
    assert s2._journal_arrays is None
