"""Blueprint 6.2 (armed-only squeeze mode) + 6.3 (laddered scale-outs) —
parameter-disabled seams on synthetic paths. The always-on giveback failed
4/4 windows; these arm surgically and ship OFF until the §11 sweep rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from waveapp.broker.base import OrderSide
from waveapp.data.hub import Bar
from waveapp.engine.exits import DEFAULT_PARAMS, ExitAction, ExitEngine, ExitParams

T0 = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def bar(i, open_, close, high=None, low=None, volume=1000.0):
    return Bar(
        symbol="EIX",
        start=T0 + timedelta(minutes=i),
        open=open_,
        high=high if high is not None else max(open_, close) + 0.05,
        low=low if low is not None else min(open_, close) - 0.05,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


def make_engine(qty=100, atr=0.5, **overrides):
    return ExitEngine(
        side=OrderSide.BUY,
        entry_price=100.0,
        qty=qty,
        atr_at_entry=atr,
        params=ExitParams(**overrides),
        entry_time=T0,
        entry_bar_volume=1000.0,
    )


def warmup(engine, n=5):
    for i in range(n):
        engine.on_bar(bar(i, 100.0, 100.0, high=100.05, low=99.95))


def vertical(engine, start=5, suppress_scale=False):
    """Three expanding green high-volume bars — the EIX shape."""
    decisions = []
    decisions.append(engine.on_bar(bar(start, 100.0, 100.5, low=99.9, volume=1500)))
    decisions.append(engine.on_bar(bar(start + 1, 100.5, 101.5, low=100.4, volume=2500)))
    decisions.append(engine.on_bar(bar(start + 2, 101.5, 103.0, low=101.4, volume=5000)))
    return decisions


def test_defaults_keep_both_seams_off():
    for params in DEFAULT_PARAMS.values():
        assert params.squeeze_bars == 0
        assert params.ladder_scaleout_rungs == ()


def test_squeeze_never_arms_when_off():
    engine = make_engine()
    warmup(engine)
    vertical(engine)
    assert engine.squeeze_armed is False


def test_squeeze_arms_and_sells_into_strength():
    engine = make_engine(squeeze_bars=3, squeeze_scale_r=3.0, k_t1=999.0)
    warmup(engine)
    decisions = vertical(engine)
    assert engine.squeeze_armed is True
    # +3R = +2.25 on 0.75 initial risk → the arming bar (profit +3.0) sells ⅓
    last = decisions[-1]
    assert last.action is ExitAction.SCALE_OUT
    assert "squeeze scale" in last.reason
    assert last.qty == 33  # a third of 100 remaining
    # A1-6: the ledger commit is deferred until the actor confirms the submit
    assert engine.remaining_qty == 100
    engine.confirm_scale_out()
    assert engine.remaining_qty == 67


def test_squeeze_freezes_atr_and_trails_prior_bar_low():
    engine = make_engine(squeeze_bars=3, squeeze_scale_r=99.0, k_trail=3.5, k_t1=999.0)
    warmup(engine)
    vertical(engine)
    assert engine.squeeze_armed is True
    assert engine._squeeze_atr == 0.5  # frozen pre-spike
    # follow-through bar with a wildly INFLATED live ATR (2.0): the frozen
    # chandelier (103.25 − 3.5×0.5 = 101.5) rules; unfrozen it would sag to
    # 96.25 and only the prior-bar low (101.4) would hold the line
    decision = engine.on_bar(bar(8, 103.0, 103.2, low=102.9, volume=2000), atr=2.0)
    assert decision.action is ExitAction.AMEND_STOP
    assert decision.new_stop == 101.5
    # next bar: prior-bar (8) low 102.9 becomes the trail
    decision = engine.on_bar(bar(9, 103.2, 103.4, low=103.1, volume=1800), atr=2.0)
    assert decision.new_stop == 102.9


def test_squeeze_hard_exit_on_prior_bar_break():
    engine = make_engine(squeeze_bars=3, squeeze_scale_r=99.0)
    warmup(engine)
    vertical(engine)
    # close below the prior bar's low → out NOW
    decision = engine.on_bar(bar(8, 103.0, 101.0, high=103.1, low=100.9, volume=3000))
    assert decision.action is ExitAction.EXIT_NOW
    assert "squeeze break" in decision.reason


def test_squeeze_volume_climax_exit():
    engine = make_engine(squeeze_bars=3, squeeze_scale_r=99.0, squeeze_climax_mult=5.0)
    warmup(engine)
    vertical(engine)
    # huge volume, fails to make a new high, closes above prior low → climax
    decision = engine.on_bar(bar(8, 103.0, 102.8, high=103.0, low=102.5, volume=50_000))
    assert decision.action is ExitAction.EXIT_NOW
    assert "climax" in decision.reason


def test_ladder_sells_quarters_at_2_4_8r():
    engine = make_engine(ladder_scaleout_rungs=(2.0, 4.0, 8.0), k_t1=1.2)
    # initial risk 0.75 → rungs at +1.5 / +3.0 / +6.0
    d1 = engine.on_bar(bar(0, 101.0, 101.6, low=100.9))
    assert d1.action is ExitAction.SCALE_OUT
    assert "+2R" in d1.reason
    assert d1.qty == 25
    engine.confirm_scale_out()  # A1-6: commit lands on confirmed submit
    assert engine.remaining_qty == 75
    # the single k_t1 scale-out is REPLACED, not stacked
    assert engine.scaled_out is False
    d2 = engine.on_bar(bar(1, 102.9, 103.1, low=102.8))
    assert d2.action is ExitAction.SCALE_OUT
    assert "+4R" in d2.reason
    engine.confirm_scale_out()
    assert engine.remaining_qty == 50
    d3 = engine.on_bar(bar(2, 105.9, 106.1, low=105.8))
    assert "+8R" in d3.reason
    engine.confirm_scale_out()
    assert engine.remaining_qty == 25  # the trail hunts the rest


def test_ladder_moves_stop_to_breakeven_on_first_rung():
    engine = make_engine(ladder_scaleout_rungs=(2.0, 4.0, 8.0))
    decision = engine.on_bar(bar(0, 101.0, 101.6, low=100.9))
    assert decision.new_stop >= 100.0  # never below entry again
    engine.confirm_scale_out()
    assert engine.current_stop >= 100.0
    assert engine.breakeven_done is True
