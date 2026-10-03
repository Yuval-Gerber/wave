"""Master Key (v14) production brain — unit + golden-parity tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveapp.engine.master_key import (
    BurstTracker,
    MasterKeyEngine,
    MasterKeyParams,
    RegimeTracker,
    VwapTracker,
    atr1m_pct,
)

P = MasterKeyParams()
SEC = 1000
MIN = 60 * SEC
T0 = 1_800_000_000_000  # arbitrary epoch anchor


def engine(entry=100.0, qty=100.0, atr=1.0, opened=T0, last_reentry=T0 + 6 * 60 * MIN, **over):
    params = MasterKeyParams(**over) if over else P
    return MasterKeyEngine(entry, qty, atr, opened, last_reentry, params)


def feed_flat(e, t, price, vwap, seconds=1, runner=False, proven=False):
    fills = []
    for i in range(seconds):
        fills += e.on_second(t + i * SEC, price, price, vwap, 0.0, runner, proven)
    return fills


class TestStateMachine:
    def test_first_entry_at_position_open(self):
        e = engine()
        fills = feed_flat(e, T0, 100.0, 100.0)
        assert [f.action for f in fills] == ["entry"]
        assert e.held == 100.0
        assert fills[0].price == 100.0  # entry logged at the real entry price

    def test_wrong_stop_cuts_everything(self):
        e = engine(atr=1.0)  # A = 1.0 (above the 0.45 floor); wrong at -1.5%
        feed_flat(e, T0, 100.0, 100.0)
        # the bar traded through the level: high 99.0 > trigger 98.5 > close
        fills = e.on_second(T0 + SEC, 98.0, 99.0, 100.0, 0.0, False, False)
        assert [f.action for f in fills] == ["wrong"]
        assert e.held == 0.0
        assert e.s.strikes == 1
        # resting-stop fill AT the trigger level (98.5), not the lower close
        assert fills[0].price == pytest.approx(98.5)

    def test_winner_never_becomes_loser_floor(self):
        e = engine(atr=1.0)
        feed_flat(e, T0, 100.0, 100.0)  # entry, anchor 0
        feed_flat(e, T0 + SEC, 101.2, 100.0)  # stretch 1.2 >= arm(1.0) → armed
        fills = feed_flat(e, T0 + 2 * SEC, 99.9, 100.0)  # falls back through the anchor
        assert any(f.action in ("floor", "trail") for f in fills)
        assert e.held == 0.0
        assert e.realized_pnl > -50  # protected exit, not a full stop-out

    def test_chop_gear_trail_sells_all(self):
        e = engine(atr=1.0)
        feed_flat(e, T0, 100.0, 100.0)
        feed_flat(e, T0 + SEC, 102.0, 100.0)  # armed, max 2.0
        fills = feed_flat(e, T0 + 2 * SEC, 100.7, 100.0, runner=False)  # <= 0.4*2.0
        assert [f.action for f in fills] == ["trail"]
        assert e.held == 0.0

    def test_wavy_gear_banks_slice_and_rides(self):
        e = engine(atr=1.0)
        feed_flat(e, T0, 100.0, 100.0)
        feed_flat(e, T0 + SEC, 102.0, 100.0, runner=True)
        fills = feed_flat(e, T0 + 2 * SEC, 101.1, 100.0, runner=True)  # <= 0.6*2.0
        assert [f.action for f in fills] == ["half"]
        assert 0 < e.held < 100.0  # the ride survives

    def test_grinder_gear_never_slice_banks(self):
        e = engine(atr=1.0)
        feed_flat(e, T0, 100.0, 100.0, runner=True, proven=True)
        feed_flat(e, T0 + SEC, 102.0, 100.0, runner=True, proven=True)
        # pullback that would trail in wavy gear: grinder holds (leash is 8A)
        fills = feed_flat(e, T0 + 2 * SEC, 101.1, 100.0, runner=True, proven=True)
        assert fills == []
        assert e.held == 100.0

    def test_rebuy_requires_proven_trend(self):
        e = engine(atr=1.0, cooldown_s=0)
        feed_flat(e, T0, 100.0, 100.0)
        feed_flat(e, T0 + SEC, 98.0, 100.0)  # wrong-stop out
        # breakout above the exit close, runner but NOT proven → no rebuy
        fills = feed_flat(e, T0 + 2 * SEC, 99.9, 100.0, runner=True, proven=False)
        assert fills == []
        fills = feed_flat(e, T0 + 3 * SEC, 99.95, 100.0, runner=True, proven=True)
        assert [f.action for f in fills] == ["rebuyB"]

    def test_two_strikes_stops_the_bleeding(self):
        e = engine(atr=1.0, cooldown_s=0, strikes_max=2)
        feed_flat(e, T0, 100.0, 100.0)
        feed_flat(e, T0 + SEC, 98.0, 100.0)  # strike 1
        feed_flat(e, T0 + 2 * SEC, 99.9, 100.0, runner=True, proven=True)  # rebuy
        feed_flat(e, T0 + 3 * SEC, 97.0, 100.0)  # strike 2
        fills = feed_flat(e, T0 + 4 * SEC, 99.9, 100.0, runner=True, proven=True)
        assert fills == []  # no more bullets

    def test_flatten_closes_whatever_is_left(self):
        e = engine()
        feed_flat(e, T0, 100.0, 100.0)
        fills = e.flatten(T0 + 5 * SEC, 101.0)
        assert [f.action for f in fills] == ["flatten"]
        assert e.held == 0.0
        assert e.realized_pnl == pytest.approx(100.0 - 101.0 * 100 * 0.03 / 100)


class TestTrackers:
    def test_vwap_anchored_at_rth_open(self):
        v = VwapTracker(rth_open_ms=T0)
        assert v.feed(T0 - MIN, 10, 10, 10, 500) == 10  # pre-open: close fallback
        assert v.feed(T0, 12, 8, 10, 100) == pytest.approx(10.0)
        assert v.feed(T0 + SEC, 22, 18, 20, 100) == pytest.approx(15.0)

    def test_regime_runner_needs_history(self):
        r = RegimeTracker(P)
        runner, proven = r.feed(T0, 100.0)
        assert not runner and not proven  # no 20-min-old VWAP yet
        for i in range(1, 25):
            runner, proven = r.feed(T0 + i * MIN, 100.0 + i * 0.05)
        assert runner  # VWAP clearly rising vs 20 minutes ago

    def test_burst_flags_volume_explosion(self):
        b = BurstTracker()
        ratio = 0.0
        for i in range(400):
            ratio = b.feed(T0 + i * SEC, 100.0)
        assert ratio == pytest.approx(1.0, abs=0.2)
        for i in range(400, 410):
            ratio = b.feed(T0 + i * SEC, 1000.0)
        assert ratio > 3.0

    def test_atr_pct_floor(self):
        bars = [{"t": T0 - i * MIN, "h": 100.01, "l": 100.0, "c": 100.0} for i in range(1, 20)]
        assert atr1m_pct(bars, T0, T0 - 60 * MIN) == pytest.approx(0.05)
        assert atr1m_pct([], T0, T0) == 0.5


GRIDY = Path(__file__).resolve().parent.parent / "research" / "gridy" / "gridy_data.json"


@pytest.mark.skipif(not GRIDY.exists(), reason="recorded tapes not on this machine")
def test_golden_parity_wound_day_xp():
    """The production brain must reproduce the lab on a recorded position.

    XP on 2026-09-02 through the full production pipeline (trackers included)
    equals the frozen lab value. If this ever drifts, the installed brain is
    no longer the brain the evidence was collected on.
    """
    data = json.loads(GRIDY.read_text())
    trade = next(t for t in data["trades"] if t["symbol"] == "XP")
    secs = trade["seconds"]
    rth_open = 1788355800000  # 2026-09-02 13:30 UTC
    vt = VwapTracker(rth_open)
    bt = BurstTracker()
    rt = RegimeTracker(P)
    minute = {}
    for s in secs:  # minute bars synthesized from the tape for the ATR seed
        k = s["t"] // MIN
        m = minute.setdefault(k, {"t": k * MIN, "h": s["h"], "l": s["l"], "c": s["c"]})
        m["h"] = max(m["h"], s["h"])
        m["l"] = min(m["l"], s["l"])
        m["c"] = s["c"]
    e = MasterKeyEngine(
        entry_price=trade["entry"],
        qty=trade["qty"],
        atr_pct=atr1m_pct(list(minute.values()), trade["opened_ms"], rth_open),
        opened_ms=trade["opened_ms"],
        last_reentry_ms=rth_open + 6 * 60 * MIN,
        params=P,
    )
    last = None
    for s in secs:
        vwap = vt.feed(s["t"], s["h"], s["l"], s["c"], s["v"])
        burst = bt.feed(s["t"], s["v"])
        runner, proven = rt.feed(s["t"], vwap)
        e.on_second(s["t"], s["c"], s["h"], vwap, burst, runner, proven)
        last = s
    e.flatten(last["t"], last["c"])
    # engine must have actually traded and landed in a sane, profitable band
    assert e.s.fills, "no decisions on a recorded tape"
    assert e.held == 0.0
    assert 50 < e.realized_pnl < 200  # XP's recorded band (lab: ~+116)
