"""Cycle-1 factory TESTER bug pass (2026-09-03) — adversarial tests on the
day's merged code: exits.py Build-2 seams (dead-scratch, k_be_r), the
scanner2 awareness features, and the radar tier model.

Tests named test_bugN_* are PROOFS of live defects: they assert the intended
behavior and are marked xfail(strict=True). While the bug exists the suite
stays green (xfail); the moment someone fixes the defect the test XPASSes
and strict=True flips it red — the reminder to delete the marker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from waveapp.broker.base import OrderSide
from waveapp.data.hub import Bar
from waveapp.engine.exits import ExitAction, ExitEngine, ExitParams

T0 = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)  # 10:30 ET
ET = ZoneInfo("America/New_York")

BARE = dict(
    k_trail=999.0,
    k_be=999.0,
    k_t1=999.0,
    t_max_minutes=9999,
    volume_death_fraction=0.0,
    recovery_depth_atr=0.0,
    loiter_depth_pct=0.0,
    vwap_recross_exit=False,
)


def bar(i: int, close: float, high=None, low=None, volume=1000.0) -> Bar:
    return Bar(
        symbol="TEST",
        start=T0 + timedelta(minutes=i),
        open=close,
        high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


def engine(side=OrderSide.BUY, entry=100.0, qty=10, atr=0.5, **overrides) -> ExitEngine:
    return ExitEngine(
        side=side,
        entry_price=entry,
        qty=qty,
        atr_at_entry=atr,
        params=ExitParams(**{**BARE, **overrides}),
        entry_time=T0,
        entry_bar_volume=1000.0,
    )


# -- BUG-1: k_be_r one-shot latch consumed without effect --------------------


# BUG-1 FIXED 2026-09-03 (the k_be_r latch is gone — the BE candidate is
# re-proposed every bar until the resting stop holds it); this test is
# now the permanent regression guard.
def test_bug1_k_be_r_floor_survives_a_red_arming_bar():
    # entry 100, ATR 0.5, k_stop 1.5 -> initial stop 99.25, R = 0.75
    eng = engine(k_be_r=0.5)  # arm at peak >= +0.375
    # arming bar: spikes to 100.60 (peak = 0.6 = 0.8R, armed) but CLOSES red
    d1 = eng.on_bar(bar(1, 99.90, high=100.60, low=99.85))
    assert d1.action is not ExitAction.EXIT_NOW
    # next bars: price comfortably back above entry — the armed BE floor
    # must now be placed (this is the seam's whole promise)
    eng.on_bar(bar(2, 100.40, high=100.45, low=100.20))
    eng.on_bar(bar(3, 100.42, high=100.50, low=100.30))
    assert eng.current_stop >= 100.0, (
        f"BE floor lost: stop still {eng.current_stop} after k_be_r armed"
    )


def test_k_be_r_clean_path_sets_breakeven():
    """Sanity: on an arming bar that closes clear of entry the seam works."""
    eng = engine(k_be_r=0.5)
    d = eng.on_bar(bar(1, 100.50, high=100.60, low=100.10))
    assert d.action is ExitAction.AMEND_STOP
    assert d.new_stop is not None and d.new_stop >= 100.0
    assert eng.current_stop >= 100.0


def test_bug1_scope_plain_k_be_not_exposed():
    """The old k_be layer triggers on CLOSE profit (not peak) so the clamp
    can't fire on its arming bar — the defect is specific to k_be_r."""
    eng = engine(k_be=1.0)  # trigger: close-profit >= 0.5
    d = eng.on_bar(bar(1, 100.55, high=100.60, low=100.10))
    assert d.action is ExitAction.AMEND_STOP
    assert eng.current_stop >= 100.0


# -- dead-money scratch: staleness clock, short side, defaults ---------------


def test_dead_scratch_fires_on_stale_underwater_long():
    eng = engine(dead_scratch_minutes=15, dead_scratch_stale_minutes=10)
    decision = None
    for i in range(1, 20):
        # never a new high (high stays below entry), underwater, below vwap
        decision = eng.on_bar(bar(i, 99.70, high=99.75, low=99.60), session_vwap=99.90)
        if decision.action is ExitAction.EXIT_NOW:
            break
    assert decision.action is ExitAction.EXIT_NOW
    assert "dead money" in decision.reason
    # fired at >= max(15m age, 10m staleness), not before
    assert i >= 15


def test_dead_scratch_short_side_staleness_clock():
    """For a SHORT the favorable extreme is the LOW: a short that keeps
    printing new lows is WORKING and must never scratch; one that stops
    making lows, goes underwater (price above entry) and sits above VWAP
    is the corpse."""
    # working short: new lows every bar -> clock resets, no scratch
    eng = engine(side=OrderSide.SELL, dead_scratch_minutes=5, dead_scratch_stale_minutes=3)
    for i in range(1, 25):
        d = eng.on_bar(
            bar(i, 100.0 - i * 0.01, high=100.0 - i * 0.01 + 0.02, low=100.0 - i * 0.02),
            session_vwap=100.5,
        )
        assert d.action is not ExitAction.EXIT_NOW, f"working short scratched at bar {i}"
    # stalled short: underwater (price > entry), above vwap, no new lows
    eng2 = engine(side=OrderSide.SELL, dead_scratch_minutes=5, dead_scratch_stale_minutes=3)
    fired = None
    for i in range(1, 12):
        d = eng2.on_bar(bar(i, 100.40, high=100.45, low=100.35), session_vwap=100.10)
        if d.action is ExitAction.EXIT_NOW:
            fired = (i, d.reason)
            break
    assert fired is not None and "dead money" in fired[1]
    assert fired[0] >= 5


def test_dead_scratch_never_fires_in_profit_or_above_vwap():
    eng = engine(dead_scratch_minutes=5, dead_scratch_stale_minutes=3)
    for i in range(1, 30):
        # underwater but ABOVE vwap: not a corpse by the seam's definition
        d = eng.on_bar(bar(i, 99.70, high=99.75, low=99.60), session_vwap=99.50)
        assert d.action is not ExitAction.EXIT_NOW


def test_build2_seams_default_off():
    """dead_scratch_minutes=0 and k_be_r=0 by default: a stale underwater
    position under bare params just sits (the adopted kitchen's behavior)."""
    p = ExitParams()
    assert p.dead_scratch_minutes == 0
    assert p.k_be_r == 0.0
    eng = engine()  # BARE, no seams
    for i in range(1, 40):
        d = eng.on_bar(bar(i, 99.70, high=99.75, low=99.60), session_vwap=99.90)
        assert d.action is not ExitAction.EXIT_NOW
    assert eng.current_stop == pytest.approx(99.25)


# -- radar tier model (scanner_page) -----------------------------------------


def _rows():
    def r(sym, accepted, rvol=2.0, score=1.0, day=1.0):
        return {
            "symbol": sym,
            "decision": ("✓ ok" if accepted else "✗ spread too wide"),
            "rvol": rvol,
            "score": score,
            "day_pct": day,
            "price": 50.0,
            "spread": 0.02,
            "gap_pct": 1.0,
            "atr_pct": 2.0,
            "strategy": "ORB",
        }

    return [
        r("AAA", False, rvol=9.0),
        r("BBB", True, rvol=3.0, score=2.0),
        r("CCC", True, rvol=1.0, score=3.0),
        r("DDD", False, rvol=0.5),
    ]


# (uses pytest-qt's managed `qapp` fixture — a local bare QApplication([])
# here once poisoned every later UI test's font metrics: 4 gate refusals)


def test_display_mapping_skips_headers_and_toggle(qapp):
    from waveapp.ui.scanner_page import _CandidatesModel

    model = _CandidatesModel()
    model.set_menu(["AAA"])
    model.set_rows(_rows())
    # display: [hdr MENU, AAA, hdr WATCH, BBB, CCC, hdr REST, toggle]
    assert model.entry_kind(0) == "header"
    assert model.row_dict(0) is None
    assert model.row_dict(1)["symbol"] == "AAA"
    assert model.entry_kind(2) == "header"
    assert {model.row_dict(3)["symbol"], model.row_dict(4)["symbol"]} == {"BBB", "CCC"}
    assert model.entry_kind(5) == "header"
    assert model.entry_kind(6) == "toggle"
    assert model.rowCount() == 7  # folded: DDD not materialized
    model.toggle_rest()
    assert model.rowCount() == 8
    assert model.row_dict(7)["symbol"] == "DDD"


def test_search_forces_folded_tier_open(qapp):
    from waveapp.ui.scanner_page import _CandidatesModel

    model = _CandidatesModel()
    model.set_menu([])
    model.set_rows(_rows())
    model.set_search("DDD")
    symbols = [
        model.row_dict(i)["symbol"]
        for i in range(model.rowCount())
        if model.row_dict(i) is not None
    ]
    assert symbols == ["DDD"], "a searched-for folded row must be visible"


def test_short_candidates_carry_direction_marker(qapp):
    """S4: a SELL-side candidate row (dir='short') renders a ↓ prefix on the
    symbol cell; every row without the tag — the whole long book — renders
    byte-identical."""
    from PyQt6.QtCore import Qt

    from waveapp.ui.scanner_page import _COL_SYMBOL, _CandidatesModel

    model = _CandidatesModel()
    model.set_menu([])
    rows = _rows()
    rows[1]["dir"] = "short"  # BBB is a short candidate
    model.set_rows(rows)
    model.toggle_rest()
    texts = {}
    for i in range(model.rowCount()):
        row = model.row_dict(i)
        if row is not None:
            index = model.index(i, _COL_SYMBOL)
            texts[row["symbol"]] = model.data(index, Qt.ItemDataRole.DisplayRole)
    assert texts["BBB"] == "↓ BBB"
    assert texts["AAA"] == "AAA" and texts["CCC"] == "CCC" and texts["DDD"] == "DDD"


def test_sort_never_crosses_tier_boundaries(qapp):
    from PyQt6.QtCore import Qt

    from waveapp.ui.scanner_page import _COL_RVOL, _CandidatesModel

    model = _CandidatesModel()
    model.set_menu(["CCC"])  # CCC (rvol 1.0) is menu tier
    model.set_rows(_rows())
    model.toggle_rest()
    model.sort(_COL_RVOL, Qt.SortOrder.DescendingOrder)
    order = [
        model.row_dict(i)["symbol"]
        for i in range(model.rowCount())
        if model.row_dict(i) is not None
    ]
    # menu (CCC) stays on top despite lowest RVOL; rest sorted within tiers
    assert order[0] == "CCC"
    assert order[1] == "BBB"  # watch tier
    assert order[2:] == ["AAA", "DDD"]  # rest tier, 9.0 then 0.5


# BUG-2 FIXED 2026-09-03 (_decorate_sections restores prior special rows
# to the default height before decorating); permanent regression guard.
def test_bug2_row_heights_reset_when_tiers_move(qapp):
    from waveapp.ui.scanner_page import ScannerPage

    page = ScannerPage()
    page.resize(900, 700)
    # scan 1: AAA on the menu -> display row 1 is a 34px menu row
    page.set_menu(["AAA"])
    page.candidates_model.set_rows(_rows())
    assert page.table.rowHeight(1) == 34
    # scan 2: menu empties -> display row 1 is now an ordinary WATCH row
    page.set_menu([])
    page.candidates_model.set_rows(_rows())
    # display: [hdr WATCH(0), BBB(1), CCC(2), hdr REST(3), toggle(4)]
    assert page.candidates_model.row_dict(1)["symbol"] in ("BBB", "CCC")
    assert page.table.rowHeight(1) == 28, (
        f"stale menu-tier height survived the reset: {page.table.rowHeight(1)}px"
    )


# -- scanner2 clock + feature guards -----------------------------------------


def _scanner2(tmp_path, monkeypatch, symbols=("AAA", "SPY")):
    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    assets = [SimpleNamespace(symbol=s, tradable=True, name="") for s in symbols]
    scanner.set_universe(assets)
    return scanner


def _s2bar(symbol, h, m, close, volume=10_000.0, high=None, low=None):
    return SimpleNamespace(
        symbol=symbol,
        timestamp=datetime(2026, 9, 3, h, m, tzinfo=ET),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
    )


def test_cur_minute_monotone_never_regresses(tmp_path, monkeypatch):
    scanner = _scanner2(tmp_path, monkeypatch)
    scanner.on_bar_msg(_s2bar("AAA", 9, 40, 50.0))
    assert scanner._cur_minute == 340
    # late/out-of-order bar must NOT rewind the clock
    scanner.on_bar_msg(_s2bar("AAA", 9, 35, 50.1))
    assert scanner._cur_minute == 340
    # rerank_only advances by wall clock, never double-advances
    scanner.rerank_only(datetime(2026, 9, 3, 9, 45, tzinfo=ET))
    assert scanner._cur_minute == 345
    scanner.rerank_only(datetime(2026, 9, 3, 9, 45, tzinfo=ET))
    assert scanner._cur_minute == 345
    scanner.minute_tick(datetime(2026, 9, 3, 9, 46, tzinfo=ET))
    assert scanner._cur_minute == 346


def test_feature_row_division_guards_zero_atr_zero_range(tmp_path, monkeypatch):
    scanner = _scanner2(tmp_path, monkeypatch)
    idx = scanner._index["AAA"]
    # a symbol with a price but NO baseline ATR, NO day range, NO vwap
    scanner.last[idx] = 50.0
    row = scanner.feature_row(idx)
    assert row["ext_open_atr"] == 0.0
    assert row["ext_vwap_atr"] == 0.0
    assert row["range_pos"] == 0.5  # honest default, no division
    assert row["trend_age_min"] == 0.0
    # degenerate range: hod == lod -> still 0.5, no ZeroDivisionError
    scanner.hod[idx] = 50.0
    scanner.lod[idx] = 50.0
    row = scanner.feature_row(idx)
    assert row["range_pos"] == 0.5


def test_trend_age_uses_last_hod_print(tmp_path, monkeypatch):
    scanner = _scanner2(tmp_path, monkeypatch)
    idx = scanner._index["AAA"]
    scanner.on_bar_msg(_s2bar("AAA", 9, 31, 50.0, high=50.5))  # seeds HOD
    scanner.on_bar_msg(_s2bar("AAA", 9, 40, 51.0, high=51.0))  # new HOD @340
    scanner.on_bar_msg(_s2bar("AAA", 9, 55, 50.8, high=50.9))  # no new HOD
    row = scanner.feature_row(idx)
    assert row["trend_age_min"] == 15.0  # 355 - 340
    assert row["mins_since_open"] == 25.0


# -- MINOR-pass regression guards (M2/M3/M4 scanner2, M5 fork) ----------------


def test_m2_bar_before_first_tick_rolls_the_day(tmp_path, monkeypatch):
    """A 4:00-4:01 ET stream bar landing BEFORE the day's first minute_tick
    must roll the day itself — never write into (then be wiped from)
    yesterday's arrays — and a straggler bar from the previous day must
    neither roll backwards nor contaminate today's clock/volume."""
    scanner = _scanner2(tmp_path, monkeypatch)
    idx = scanner._index["AAA"]
    scanner.minute_tick(datetime(2026, 9, 3, 9, 46, tzinfo=ET))
    scanner.on_bar_msg(_s2bar("AAA", 9, 50, 50.0, high=50.5, low=49.5))
    assert scanner.cum_vol_stream[idx] > 0 and scanner.lod[idx] > 0
    # next day, 4:00 ET: the FIRST touch is a stream bar, before any tick
    bar2 = SimpleNamespace(
        symbol="AAA",
        timestamp=datetime(2026, 9, 4, 4, 0, tzinfo=ET),
        open=51.0,
        high=51.0,
        low=51.0,
        close=51.0,
        volume=500.0,
    )
    scanner.on_bar_msg(bar2)
    assert scanner._day == "2026-09-04"
    assert scanner._cur_minute == 0  # yesterday's clock did not leak
    assert scanner.cum_vol_stream[idx] == 500.0  # today's bar survived the roll
    assert scanner.lod[idx] == 0.0 and scanner.hod_minute[idx] == -1
    # straggler from yesterday: no back-roll, no clock/volume contamination
    stale = SimpleNamespace(
        symbol="AAA",
        timestamp=datetime(2026, 9, 3, 15, 0, tzinfo=ET),
        open=50.0,
        high=50.0,
        low=50.0,
        close=50.0,
        volume=9999.0,
    )
    scanner.on_bar_msg(stale)
    assert scanner._day == "2026-09-04"
    assert scanner._cur_minute == 0
    assert scanner.cum_vol_stream[idx] == 500.0


def test_m2_rerank_only_rolls_the_day(tmp_path, monkeypatch):
    """The 1-second re-rank path can be the new day's first caller too: it
    must roll rather than serve a menu off yesterday's hod/lod/clock."""
    scanner = _scanner2(tmp_path, monkeypatch)
    idx = scanner._index["AAA"]
    scanner.on_bar_msg(_s2bar("AAA", 9, 40, 50.0, high=50.5, low=49.5))
    assert scanner.hod[idx] > 0
    scanner.rerank_only(datetime(2026, 9, 4, 4, 0, 30, tzinfo=ET))
    assert scanner._day == "2026-09-04"
    assert scanner._cur_minute == 0
    assert scanner.hod[idx] == 0.0 and scanner.lod[idx] == 0.0
    assert scanner.hod_minute[idx] == -1


def test_m3_wipe_guard_preserves_leveraged_cap_flags(tmp_path, monkeypatch):
    """When the universe wipe-guard trips, the kept symbols must keep their
    fund NAMES (persisted beside them) so _leveraged — the leveraged menu
    cap — is rebuilt intact instead of silently all-False for the day."""
    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    full = [SimpleNamespace(symbol=f"S{i:04d}", tradable=True, name="") for i in range(1200)]
    full.append(
        SimpleNamespace(symbol="NVDL", tradable=True, name="GraniteShares 2x Long NVDA Daily ETF")
    )
    scanner.set_universe(full)
    assert scanner._leveraged[scanner._index["NVDL"]]
    # a broken feed hands back a stub — the guard keeps the previous list
    stub = [SimpleNamespace(symbol=f"S{i:04d}", tradable=True, name="") for i in range(10)]
    scanner.set_universe(stub)
    assert len(scanner.symbols) == 1201  # guard tripped, universe kept
    assert scanner._leveraged[scanner._index["NVDL"]]  # the cap flag survived


def test_m4_earnings_and_attention_boosts_cleared_on_day_roll(tmp_path, monkeypatch):
    """Yesterday's 1.3x earnings / 1.1x attention score boosts must not leak
    into the new day before the feeds refresh."""
    scanner = _scanner2(tmp_path, monkeypatch)
    scanner.minute_tick(datetime(2026, 9, 3, 9, 46, tzinfo=ET))
    scanner.earnings_today = {"AAA"}
    scanner.attention = {"AAA"}
    scanner.minute_tick(datetime(2026, 9, 4, 4, 1, tzinfo=ET))
    assert scanner.earnings_today == set()
    assert scanner.attention == set()
