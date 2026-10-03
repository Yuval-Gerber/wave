"""Phase 6 step 6.1: the adaptive exit system on synthetic bar paths.

SPEC.md Phase 6 mandates these scenarios:
- reversal at +0.3% banks profit
- runner reaches trail
- volume-death exit
- breakeven holds
- halt during hold (actor-level, verified in step 6.2 integration; the
  decision-level invariant here: stops NEVER loosen)
Plus: ATR math, throttling, scale-out, time stop, session boundary, regimes.
"""

from datetime import UTC, datetime, timedelta

import pytest

from waveapp.broker.base import OrderSide
from waveapp.data.hub import Bar
from waveapp.engine.exits import (
    DEFAULT_PARAMS,
    ExitAction,
    ExitEngine,
    ExitParams,
    compute_atr,
    params_for,
)
from waveapp.engine.session import Regime, SessionScheduler

T0 = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)  # 10:30 ET


def bar(i: int, close: float, high=None, low=None, volume=1000.0) -> Bar:
    return Bar(
        symbol="SPY",
        start=T0 + timedelta(minutes=i),
        open=close,
        high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


def engine(side=OrderSide.BUY, entry=100.0, qty=10, atr=0.5, **overrides) -> ExitEngine:
    params = ExitParams(**overrides) if overrides else ExitParams()
    return ExitEngine(
        side=side,
        entry_price=entry,
        qty=qty,
        atr_at_entry=atr,
        params=params,
        entry_time=T0,
        entry_bar_volume=1000.0,
    )


# -- ATR ---------------------------------------------------------------------


def test_atr_needs_history_then_computes():
    bars = [bar(i, 100 + i * 0.1) for i in range(10)]
    assert compute_atr(bars, period=14) is None
    bars = [bar(i, 100.0, high=100.4, low=99.9) for i in range(15)]
    atr = compute_atr(bars, period=14)
    assert atr is not None and 0.4 <= atr <= 0.55


# -- mandated scenario 1: reversal at +0.3% banks profit ---------------------


def test_reversal_banks_profit_via_volume_death():
    """Price pushes to ~+0.3%, the push dies (volume collapses) — Wave banks
    the gain instead of riding the reversal down."""
    ex = engine(atr=0.5)
    ex.on_bar(bar(1, 100.15, volume=1200))
    ex.on_bar(bar(2, 100.30, volume=900))
    decision = ex.on_bar(bar(3, 100.30, volume=150))  # +0.3%, volume dead
    assert decision.action is ExitAction.EXIT_NOW
    assert decision.reason == "volume death"


# -- mandated scenario 2: runner reaches trail --------------------------------


def test_runner_ratchets_trail_upward():
    ex = engine(atr=0.5, amend_throttle_seconds=0)  # k_trail=2.0 → $1 behind best
    d1 = ex.on_bar(bar(1, 101.0, high=101.0, volume=900))
    d2 = ex.on_bar(bar(2, 102.0, high=102.0, volume=900))
    d3 = ex.on_bar(bar(3, 103.0, high=103.0, volume=900))
    # trail follows the run up: amendments fire and only ever move the stop up
    amends = [d for d in (d1, d2, d3) if d.action is ExitAction.AMEND_STOP]
    assert amends, "a running winner must ratchet its stop"
    assert ex.current_stop == 102.0  # 103 high − 2.0×0.5 ATR
    # pullback: stop NEVER loosens
    ex.on_bar(bar(4, 102.2, high=102.3, volume=900))
    assert ex.current_stop == 102.0


# -- mandated scenario 3: volume-death exit ----------------------------------


def test_volume_death_only_fires_in_profit():
    ex = engine(atr=0.5)
    # losing position + dead volume → NOT a momentum exit (the stop handles it)
    decision = ex.on_bar(bar(1, 99.5, volume=100))
    assert decision.action is not ExitAction.EXIT_NOW


# -- mandated scenario 4: breakeven holds ------------------------------------


def test_breakeven_ratchet_holds():
    """After +1×ATR the stop moves to entry+buffer; a fade back to entry
    cannot turn the winner into a loser."""
    ex = engine(atr=0.5, amend_throttle_seconds=0)
    decision = ex.on_bar(bar(1, 100.55, high=100.6, volume=900))  # ≥ k_be×ATR
    assert decision.action is ExitAction.AMEND_STOP
    assert decision.new_stop >= 100.0 + 0.01  # entry + fees buffer
    assert ex.breakeven_done
    # price fades to entry: no loosening, stop still at/above breakeven
    ex.on_bar(bar(2, 100.02, volume=900))
    assert ex.current_stop >= 100.01


# -- mandated scenario 5 (decision-level): stops never loosen ----------------


def test_stop_never_loosens_even_on_collapse():
    ex = engine(atr=0.5, amend_throttle_seconds=0, k_t1=10.0)  # isolate the stop layers
    ex.on_bar(bar(1, 102.0, high=102.0, volume=900))
    ratcheted = ex.current_stop
    ex.on_bar(bar(2, 100.2, high=100.3, low=100.0, volume=900))
    ex.on_bar(bar(3, 99.0, high=99.2, low=98.8, volume=900))
    assert ex.current_stop == ratcheted


# -- scale-out ---------------------------------------------------------------


def test_scale_out_at_first_target_then_breakeven():
    ex = engine(atr=0.5, qty=10, amend_throttle_seconds=0)  # k_t1=1.2 → +0.60
    decision = ex.on_bar(bar(1, 100.65, high=100.7, volume=900))
    assert decision.action is ExitAction.SCALE_OUT
    assert decision.qty == 5  # 50% of 10
    assert decision.new_stop >= 100.01  # rest is protected at breakeven
    # A1-6: the ledger commit is deferred until the actor confirms the submit
    assert ex.remaining_qty == 10
    ex.confirm_scale_out()
    assert ex.remaining_qty == 5
    assert ex.current_stop >= 100.01
    # second touch of the target must not scale again
    decision = ex.on_bar(bar(2, 100.7, high=100.75, volume=900))
    assert decision.action is not ExitAction.SCALE_OUT


def test_tiny_position_exits_fully_at_target():
    ex = engine(atr=0.5, qty=1)
    decision = ex.on_bar(bar(1, 100.65, high=100.7, volume=900))
    assert decision.action is ExitAction.EXIT_NOW
    assert "target" in decision.reason


# -- time stop ---------------------------------------------------------------


def test_time_stop_fires_only_when_flatish():
    ex = engine(atr=0.5, t_max_minutes=30)
    # 35 minutes in, position basically flat → exit
    decision = ex.on_bar(bar(35, 100.05, volume=900))
    assert decision.action is ExitAction.EXIT_NOW
    assert "time stop" in decision.reason

    # a clear winner at the same age is NOT time-stopped
    ex2 = engine(atr=0.5, t_max_minutes=30, amend_throttle_seconds=0)
    decision = ex2.on_bar(bar(35, 101.9, high=102.0, volume=900))
    assert decision.action is not ExitAction.EXIT_NOW


# -- session boundary --------------------------------------------------------


def test_session_boundary_flattens():
    ex = engine(atr=0.5)
    decision = ex.on_bar(bar(1, 100.2, volume=900), minutes_to_close_boundary=8)
    assert decision.action is ExitAction.EXIT_NOW
    assert "session boundary" in decision.reason


# -- throttling --------------------------------------------------------------


def test_amend_throttle_and_min_move():
    ex = engine(atr=0.5, amend_throttle_seconds=150, amend_min_move_atr=0.15, k_t1=10.0)
    d1 = ex.on_bar(bar(1, 101.0, high=101.0, volume=900))
    assert d1.action is ExitAction.AMEND_STOP  # stop → 100.01 (breakeven+trail)

    # better trail exists 60s/120s later — inside the 150s throttle window
    assert ex.on_bar(bar(2, 101.2, high=101.2, volume=900)).action is ExitAction.NONE
    assert ex.on_bar(bar(3, 101.2, high=101.2, volume=900)).action is ExitAction.NONE

    # throttle expired (180s) → the postponed ratchet fires (100.2)
    d4 = ex.on_bar(bar(4, 101.2, high=101.2, volume=900))
    assert d4.action is ExitAction.AMEND_STOP
    assert d4.new_stop == 100.2

    # much later: improvement below min-move (0.075) → no amend
    d5 = ex.on_bar(bar(8, 101.25, high=101.25, volume=900))
    assert d5.action is ExitAction.NONE

    # real improvement after the window → ratchet fires again
    d6 = ex.on_bar(bar(12, 101.6, high=101.6, volume=900))
    assert d6.action is ExitAction.AMEND_STOP
    assert d6.new_stop == 100.6


# -- shorts ------------------------------------------------------------------


def test_short_side_mirrors():
    ex = engine(side=OrderSide.SELL, atr=0.5, amend_throttle_seconds=0, k_t1=10.0)
    assert ex.current_stop == 100.75  # entry + 1.5×ATR above
    d = ex.on_bar(bar(1, 99.0, low=99.0, volume=900))
    assert d.action is ExitAction.AMEND_STOP
    assert d.new_stop < 100.75  # ratchets DOWN for a short
    ex.on_bar(bar(2, 100.4, high=100.5, volume=900))
    assert ex.current_stop == d.new_stop  # never loosens upward


# -- regimes & scheduler -----------------------------------------------------


def test_params_exist_for_every_regime():
    assert set(DEFAULT_PARAMS) == set(Regime)
    # THE OLD CHAMPION — restored 2026-09-04 on decision (the bare
    # kitchen's only profit exit was the closing bell; unacceptable to the
    # owner). Trail 3.5×ATR + breakeven floor + recovery + loiter + recross.
    params = params_for(Regime.OPEN_DRIVE)
    assert params.k_trail == 3.5
    assert params.k_t1 == 999.0  # scale-out off
    assert params.k_be == 2.0  # breakeven FLOOR at 2×ATR
    assert params.t_max_minutes == 90
    assert params.recovery_depth_atr == 0.75
    assert params.loiter_depth_pct == 1.0
    assert params.vwap_recross_exit is True


def _regime_at(month, day, hour, minute, year=2026):
    # build ET wall time, convert to UTC for the API
    from waveapp.engine.session import ET

    et = datetime(year, month, day, hour, minute, tzinfo=ET)
    return SessionScheduler.regime(et.astimezone(UTC))


def test_session_regimes_through_the_day():
    assert _regime_at(8, 13, 5, 0) == Regime.PRE
    assert _regime_at(8, 13, 9, 45) == Regime.OPEN_DRIVE
    assert _regime_at(8, 13, 12, 0) == Regime.MIDDAY
    assert _regime_at(8, 13, 15, 30) == Regime.POWER_HOUR
    assert _regime_at(8, 13, 17, 0) == Regime.POST
    assert _regime_at(8, 13, 22, 0) == Regime.OVERNIGHT
    assert _regime_at(8, 13, 2, 0) == Regime.OVERNIGHT


def test_weekend_dead_zone_and_sunday_open():
    assert _regime_at(8, 14, 21, 0) == Regime.CLOSED  # Friday night
    assert _regime_at(8, 15, 12, 0) == Regime.CLOSED  # Saturday
    assert _regime_at(8, 16, 19, 0) == Regime.CLOSED  # Sunday pre-open
    assert _regime_at(8, 16, 20, 30) == Regime.OVERNIGHT  # Sunday 20:00 opens


def test_holiday_closes_day_session():
    assert _regime_at(12, 25, 12, 0) == Regime.CLOSED  # Christmas
    assert _regime_at(12, 25, 2, 0) == Regime.OVERNIGHT  # overnight before


def test_midday_lull_flag():
    from waveapp.engine.session import ET

    et = datetime(2026, 8, 13, 12, 15, tzinfo=ET)
    info = SessionScheduler.info(et.astimezone(UTC))
    assert info.regime is Regime.MIDDAY and info.is_lull
    et = datetime(2026, 8, 13, 14, 0, tzinfo=ET)
    assert not SessionScheduler.info(et.astimezone(UTC)).is_lull


def test_next_closed_boundary_is_friday_night():
    from waveapp.engine.session import ET

    thursday = datetime(2026, 8, 13, 12, 0, tzinfo=ET)
    boundary = SessionScheduler.next_closed_boundary(thursday.astimezone(UTC))
    assert boundary.astimezone(ET).weekday() == 4  # Friday
    assert boundary.astimezone(ET).hour == 20


def test_calendar_covers_future_years_beyond_hardcode():
    """The XNYS calendar library is authoritative: it knows holidays far past
    the static fallback list (which only covers 2026-27)."""
    from waveapp.engine.session import day_info

    assert day_info(datetime(2028, 11, 23).date()) == (False, None)  # Thanksgiving 2028
    assert day_info(datetime(2029, 12, 25).date())[0] is False  # Christmas 2029
    assert day_info(datetime(2028, 11, 22).date())[0] is True  # regular Wednesday


def test_early_close_day_regimes():
    """Black Friday 2026 closes 13:00 ET: 11:45 = power hour, 14:00 = POST."""
    assert _regime_at(11, 27, 11, 15) == Regime.MIDDAY
    assert _regime_at(11, 27, 12, 30) == Regime.POWER_HOUR
    assert _regime_at(11, 27, 14, 0) == Regime.POST
    # a regular Friday at 14:00 is MIDDAY
    assert _regime_at(11, 20, 14, 0) == Regime.MIDDAY


def test_calendar_failure_falls_back_loudly(monkeypatch, caplog):
    """Library failure → static fallback AND a loud ERROR in the log."""
    import logging

    import waveapp.engine.session as session

    monkeypatch.setattr(session, "_fallback_reported", False)
    monkeypatch.setattr(session, "_day_cache", {})
    monkeypatch.setattr(
        session, "_get_calendar", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with caplog.at_level(logging.ERROR, logger="wave.engine.session"):
        trading, early = session.day_info(datetime(2026, 12, 25).date())
    assert trading is False  # fallback list still knows Christmas 2026
    assert early is None
    assert any("static fallback" in r.message for r in caplog.records)
    # second call: no duplicate error spam
    with caplog.at_level(logging.ERROR, logger="wave.engine.session"):
        session.day_info(datetime(2026, 12, 28).date())
    assert sum("static fallback" in r.message for r in caplog.records) == 1


def test_staged_profit_lock_ratchets_with_peak():
    """2026-08-20: once peak profit ≥ trigger×ATR, the stop locks
    entry + fraction×peak and keeps tightening as the peak grows."""
    from datetime import UTC, datetime, timedelta

    from waveapp.broker.base import OrderSide
    from waveapp.data.hub import Bar
    from waveapp.engine.exits import ExitEngine, ExitParams

    params = ExitParams(
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        lock_trigger_atr=1.0,
        lock_fraction=0.5,
        amend_min_move_atr=0.0,
        amend_throttle_seconds=0.0,
    )
    start = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    brain = ExitEngine(
        side=OrderSide.BUY,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=params,
        entry_time=start,
        initial_stop=98.0,
    )

    def bar(i, close, high=None):
        return Bar(
            symbol="T",
            start=start + timedelta(minutes=i),
            open=close,
            high=high or close,
            low=close - 0.05,
            close=close,
            volume=1000.0,
        )

    # +0.4 profit < trigger (1.0×0.5) → no lock yet
    decision = brain.on_bar(bar(1, 100.4))
    assert decision.action.value == "none"
    # peak hits +1.0 (≥ trigger) → lock at 100 + 0.5×1.0 = 100.50
    decision = brain.on_bar(bar(2, 100.9, high=101.0))
    assert decision.action.value == "amend_stop"
    assert decision.new_stop == pytest.approx(100.5)
    # peak extends to +2.0 → lock tightens to 101.00
    decision = brain.on_bar(bar(3, 101.8, high=102.0))
    assert decision.new_stop == pytest.approx(101.0)
    # price falls back but the lock NEVER loosens
    decision = brain.on_bar(bar(4, 101.1))
    assert decision.action.value == "none"
    assert brain.current_stop == pytest.approx(101.0)


def test_lock_disabled_by_default_keeps_champion_behavior():
    from waveapp.engine.exits import DEFAULT_PARAMS

    for params in DEFAULT_PARAMS.values():
        assert params.lock_trigger_atr == 0.0  # champion default until §11


def test_recovery_exit_banks_the_bounce_after_drawdown():
    """2026-08-20: −2% for half an hour → bounce to +1% → SELL, don't
    round-trip. Underwater time arms the exit; the bounce triggers it."""
    from datetime import UTC, datetime, timedelta

    from waveapp.broker.base import OrderSide
    from waveapp.data.hub import Bar
    from waveapp.engine.exits import ExitEngine, ExitParams

    params = ExitParams(
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        recovery_depth_atr=1.0,
        recovery_minutes=3,
        recovery_exit_atr=0.2,
    )
    start = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
    brain = ExitEngine(
        side=OrderSide.BUY,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=params,
        entry_time=start,
        initial_stop=97.0,
    )

    def bar(i, close):
        return Bar(
            symbol="T",
            start=start + timedelta(minutes=i),
            open=close,
            high=close + 0.02,
            low=close - 0.02,
            close=close,
            volume=1000.0,
        )

    # 3 minutes at −0.7 (below −1×ATR=−0.5): hurt arms
    for i in range(1, 4):
        assert brain.on_bar(bar(i, 99.3)).action.value == "none"
    # still underwater → no exit
    assert brain.on_bar(bar(4, 99.6)).action.value == "none"  # −0.4: not recovered
    # the bounce: +0.15 ≥ 0.2×ATR (0.1) → BANK IT
    decision = brain.on_bar(bar(5, 100.15))
    assert decision.action.value == "exit_now"
    assert "recovered after drawdown" in decision.reason


def test_recovery_exit_disabled_by_default_and_never_arms_shallow_dips():
    from datetime import UTC, datetime, timedelta

    from waveapp.broker.base import OrderSide
    from waveapp.data.hub import Bar
    from waveapp.engine.exits import DEFAULT_PARAMS, ExitEngine, ExitParams

    for params in DEFAULT_PARAMS.values():
        # restored 2026-09-04 with the old champion (adopted 2026-08-20)
        assert params.recovery_depth_atr == 0.75
        assert params.recovery_minutes == 20
        assert params.recovery_exit_atr == 0.3
    params = ExitParams(
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        recovery_depth_atr=1.0,
        recovery_minutes=3,
        recovery_exit_atr=0.0,
    )
    start = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
    brain = ExitEngine(
        side=OrderSide.BUY,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=params,
        entry_time=start,
        initial_stop=97.0,
    )

    def bar(i, close):
        return Bar(
            symbol="T",
            start=start + timedelta(minutes=i),
            open=close,
            high=close + 0.02,
            low=close - 0.02,
            close=close,
            volume=1000.0,
        )

    # shallow dip (−0.3 > −0.5 threshold) never accumulates hurt
    for i in range(1, 10):
        brain.on_bar(bar(i, 99.7))
    assert brain.hurt_minutes == 0.0
    assert brain.on_bar(bar(11, 100.3)).action.value != "exit_now"


def test_goal_ladder_lifts_floor_and_goal_each_rung():
    """2026-08-21: crossing a goal tightens the floor AND sets the
    next goal — always looking up."""
    from datetime import UTC, datetime, timedelta

    from waveapp.broker.base import OrderSide
    from waveapp.data.hub import Bar
    from waveapp.engine.exits import ExitEngine, ExitParams

    params = ExitParams(
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        ladder_step_atr=1.0,
        ladder_floor_lag=1.0,
        amend_min_move_atr=0.0,
        amend_throttle_seconds=0.0,
    )
    start = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)
    brain = ExitEngine(
        side=OrderSide.BUY,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=params,
        entry_time=start,
        initial_stop=98.0,
    )

    def bar(i, close, high=None):
        return Bar(
            symbol="T",
            start=start + timedelta(minutes=i),
            open=close,
            high=high or close,
            low=close - 0.03,
            close=close,
            volume=1000.0,
        )

    assert brain.next_goal == pytest.approx(100.5)  # first rung, from entry
    # cross rung 1 (+0.5): floor lifts to entry, goal climbs to rung 2
    decision = brain.on_bar(bar(1, 100.55, high=100.6))
    assert decision.action.value == "amend_stop"
    assert decision.new_stop == pytest.approx(100.0, abs=0.011)
    assert brain.next_goal == pytest.approx(101.0)
    # cross rung 3 (+1.5): floor jumps to rung 2 (+1.0), goal to rung 4
    decision = brain.on_bar(bar(2, 101.55, high=101.6))
    assert decision.new_stop == pytest.approx(101.0, abs=0.011)
    assert brain.next_goal == pytest.approx(102.0)
    # pullback: floor NEVER loosens, goal stays at the peak's next rung
    brain.on_bar(bar(3, 100.8))
    assert brain.current_stop == pytest.approx(101.0, abs=0.011)
    assert brain.next_goal == pytest.approx(102.0)


def test_goal_ladder_disabled_by_default():
    from waveapp.engine.exits import DEFAULT_PARAMS

    for params in DEFAULT_PARAMS.values():
        assert params.ladder_step_atr == 0.0  # §8.3: pipeline decides first


def test_giveback_cap_layer_disabled_by_default_and_fires_when_armed():
    """2026-08-24 red-day directive: the peak-giveback cap — inert at the
    default 0.0 (live engine unchanged), exits on a deep retrace when armed."""
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from waveapp.broker.base import OrderSide
    from waveapp.data.hub import Bar
    from waveapp.engine.exits import DEFAULT_PARAMS, ExitAction, ExitEngine
    from waveapp.engine.session import Regime

    def run(params, prices):
        start = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
        engine = ExitEngine(
            side=OrderSide.BUY,
            entry_price=100.0,
            qty=100,
            atr_at_entry=1.0,
            params=params,
            entry_time=start,
            entry_bar_volume=1000.0,
        )
        for i, price in enumerate(prices):
            bar = Bar(
                symbol="X",
                start=start + timedelta(minutes=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000,
            )
            decision = engine.on_bar(bar, minutes_to_close_boundary=200.0)
            if decision is not None and decision.action is ExitAction.EXIT_NOW:
                return decision
        return None

    base = DEFAULT_PARAMS[Regime.MIDDAY]
    path = [100.0, 100.6, 101.2, 100.4]  # peak +1.2 ATR, retraced to +0.4
    d = run(base, path)
    assert d is None or "giveback" not in (d.reason or "")
    armed = replace(base, giveback_fraction=0.5, giveback_arm_atr=0.75)
    d = run(armed, path)
    assert d is not None and "giveback cap" in d.reason


def test_stop_amendment_never_lands_at_or_above_the_market():
    """PYPL 2026-08-28: with a hair-thin ATR the floor computed a stop AT the
    live price — the amendment executed instantly and ejected a healthy
    position at a forced loss. A candidate within 1¢ of the price now waits
    for the next tick instead of amending."""
    from waveapp.engine.exits import ExitAction

    # tiny ATR: k_be×ATR arms on a 3-cent move; breakeven stop = 100.01
    ex = engine(atr=0.01, k_be=2.0, k_trail=3.5, amend_throttle_seconds=0)
    decision = ex.on_bar(bar(1, 100.015, high=100.03, volume=900))
    # candidate (entry+buffer=100.01) is within 1¢ of price 100.015 → no amend
    assert decision.action is not ExitAction.AMEND_STOP or decision.new_stop < 100.005
    # once price is clearly above the would-be stop, the ratchet proceeds
    decision = ex.on_bar(bar(2, 100.30, high=100.32, volume=900))
    assert decision.action is ExitAction.AMEND_STOP
    assert decision.new_stop <= 100.30 - 0.01  # strictly below the market


def test_vwap_ratchet_floor_after_arming_minutes():
    """Layer 8 (research seam): after N minutes the stop rests no worse than
    session VWAP; before N minutes VWAP is ignored."""
    from waveapp.engine.exits import ExitAction

    ex = engine(atr=0.5, vwap_floor_minutes=30, amend_throttle_seconds=0)
    # minute 5: VWAP above the stop but the floor is not armed yet
    d = ex.on_bar(bar(5, 99.4, volume=900), session_vwap=99.2)
    assert d.action is not ExitAction.AMEND_STOP or d.new_stop < 99.0
    # minute 35: armed — the stop ratchets up to VWAP (price safely above)
    d = ex.on_bar(bar(35, 99.9, volume=900), session_vwap=99.6)
    assert d.action is ExitAction.AMEND_STOP and d.new_stop >= 99.6 - 0.01


def test_loiter_cut_fires_on_sustained_depth_only():
    """Layer 9 (research seam): sustained time below the depth exits; a dip
    that bounces resets the clock (winners dip and bounce)."""
    from waveapp.engine.exits import ExitAction

    ex = engine(atr=0.5, loiter_depth_pct=1.0, loiter_minutes=30)
    # dip below −1% at minute 5, bounce at minute 20 → clock resets
    ex.on_bar(bar(5, 98.9, volume=900))
    ex.on_bar(bar(20, 99.5, volume=900))
    # new dip at 25, still below at 40 (15m < 30m) → no exit yet
    ex.on_bar(bar(25, 98.8, volume=900))
    d = ex.on_bar(bar(40, 98.7, volume=900))
    assert d.action is not ExitAction.EXIT_NOW
    # still below at minute 56 (31m sustained) → loiter cut
    d = ex.on_bar(bar(56, 98.8, volume=900))
    assert d.action is ExitAction.EXIT_NOW and "loiter" in d.reason


# -- S1: the short-side mirror, layer by layer (2026-09-23) -------------------
# Every §8.2 layer verified with direction = -1. The engine's math is written
# through self.direction/_profit/_better, so these tests PIN the mirror —
# any future layer that hardcodes long math breaks here first.


def sbar(i: int, open_: float, close: float, high: float, low: float, volume=1000.0) -> Bar:
    """Bar with an explicit open (the default helper sets open=close, which
    can never satisfy a short's favorable-bar predicate close < open)."""
    return Bar(
        symbol="SPY",
        start=T0 + timedelta(minutes=i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


def test_short_initial_stop_rests_above_entry():
    """Layer 1 mirror: the hard stop for a short is entry + k_stop×ATR."""
    ex = engine(side=OrderSide.SELL, atr=0.5)
    assert ex.current_stop == 100.75  # 100 + 1.5×0.5 — ABOVE entry
    assert ex.direction == -1.0
    assert ex.initial_risk == pytest.approx(0.75)


def test_short_breakeven_ratchet_moves_stop_down_to_entry_minus_buffer():
    """Layer 2 mirror: at +k_be×ATR profit (price BELOW entry) the stop
    ratchets DOWN from above to entry − fees buffer, and never loosens up."""
    ex = engine(side=OrderSide.SELL, atr=0.5, amend_throttle_seconds=0, k_t1=999.0)
    d = ex.on_bar(bar(1, 99.45, low=99.4, volume=900))  # profit 0.55 ≥ 0.5
    assert d.action is ExitAction.AMEND_STOP
    assert d.new_stop <= 100.0 - 0.01  # entry − fees buffer, BELOW entry
    assert ex.breakeven_done
    # price bounces back toward entry: the stop never loosens upward
    ex.on_bar(bar(2, 99.97, high=99.98, volume=900))
    assert ex.current_stop <= 100.0 - 0.01


def test_short_chandelier_trails_above_the_lowest_price():
    """Layer 3 mirror: the trail rides k_trail×ATR ABOVE the LOWEST price
    since entry, ratcheting one direction (down) only."""
    ex = engine(side=OrderSide.SELL, atr=0.5, amend_throttle_seconds=0, k_be=999.0, k_t1=999.0)
    ex.on_bar(bar(1, 99.0, low=99.0, volume=900))
    ex.on_bar(bar(2, 98.0, low=98.0, volume=900))
    ex.on_bar(bar(3, 97.0, low=97.0, volume=900))
    assert ex.best_price == 97.0  # lowest, not highest
    assert ex.current_stop == 98.0  # 97 low + 2.0×0.5 ATR ABOVE
    # pullback UP: the stop never loosens
    ex.on_bar(bar(4, 97.8, high=97.9, volume=900))
    assert ex.current_stop == 98.0


def test_short_scale_out_at_first_target_below_entry():
    """Layer 5 mirror: the first target sits BELOW entry; the decision banks
    half and parks the remainder's stop at breakeven-from-above (below
    entry). The BUY side of the sale itself is the actor's job."""
    ex = engine(side=OrderSide.SELL, atr=0.5, qty=10, amend_throttle_seconds=0)
    d = ex.on_bar(bar(1, 99.35, low=99.3, volume=900))  # profit 0.65 ≥ 1.2×0.5
    assert d.action is ExitAction.SCALE_OUT
    assert d.qty == 5
    assert d.new_stop <= 100.0 - 0.01  # breakeven for a short is BELOW entry
    assert ex.remaining_qty == 10  # A1-6 deferred commit
    ex.confirm_scale_out()
    assert ex.remaining_qty == 5
    assert ex.current_stop <= 100.0 - 0.01


def test_short_vwap_recross_exits_when_price_closes_back_above_vwap():
    """Layer 4 mirror: a short in profit whose price closes back ABOVE the
    session VWAP (against the position) banks it."""
    ex = engine(side=OrderSide.SELL, atr=0.5)
    d = ex.on_bar(bar(1, 99.6, volume=900), session_vwap=99.5)
    assert d.action is ExitAction.EXIT_NOW
    assert d.reason == "VWAP recross"


def test_short_volume_death_fires_only_in_profit():
    """Layer 4 mirror: volume death banks a profitable short; a losing short
    is the stop's problem, exactly like the long side."""
    ex = engine(side=OrderSide.SELL, atr=0.5)
    d = ex.on_bar(bar(1, 99.6, volume=150))  # in profit, volume dead
    assert d.action is ExitAction.EXIT_NOW and d.reason == "volume death"
    ex2 = engine(side=OrderSide.SELL, atr=0.5)
    d = ex2.on_bar(bar(1, 100.5, volume=100))  # losing short + dead volume
    assert d.action is not ExitAction.EXIT_NOW


def test_short_time_stop_and_session_flatten_are_side_free():
    """Layers 6+7 mirror: flat-ish age exit and the session boundary flatten
    behave identically for a short."""
    ex = engine(side=OrderSide.SELL, atr=0.5, t_max_minutes=30)
    d = ex.on_bar(bar(35, 100.05, volume=900))
    assert d.action is ExitAction.EXIT_NOW and "time stop" in d.reason
    ex2 = engine(side=OrderSide.SELL, atr=0.5)
    d = ex2.on_bar(bar(1, 99.8, volume=900), minutes_to_close_boundary=8)
    assert d.action is ExitAction.EXIT_NOW and "session boundary" in d.reason


def test_short_squeeze_arms_on_down_bars_and_breaks_on_prior_bar_high():
    """6.2 mirror: the detector's favorable bar for a short is close < open;
    once armed, the trail is the PRIOR bar's HIGH and a close back above it
    exits."""
    ex = engine(
        side=OrderSide.SELL,
        atr=0.5,
        squeeze_bars=2,
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        amend_throttle_seconds=0,
    )
    ex.on_bar(sbar(1, 100.0, 99.8, high=100.0, low=99.7, volume=100))
    ex.on_bar(sbar(2, 99.7, 99.3, high=99.7, low=99.2, volume=400))  # expanding + 4× vol
    assert ex.squeeze_armed
    d = ex.on_bar(sbar(3, 99.4, 99.9, high=99.9, low=99.3, volume=100))
    assert d.action is ExitAction.EXIT_NOW
    assert "squeeze break" in d.reason  # closed back ABOVE the prior bar's high


def test_short_ladder_scaleout_rung_fires_below_entry():
    """6.3 mirror: the 2R rung for a short sits BELOW entry (initial risk =
    stop-above − entry); the remainder's stop parks below entry."""
    ex = engine(
        side=OrderSide.SELL,
        atr=0.5,
        qty=10,
        ladder_scaleout_rungs=(2.0,),
        ladder_scaleout_frac=0.25,
        k_trail=999.0,
        k_be=999.0,
        volume_death_fraction=0.0,
        amend_throttle_seconds=0,
    )
    # initial risk 0.75 → 2R = 1.5 → price 98.5
    d = ex.on_bar(bar(1, 98.5, low=98.45, volume=900))
    assert d.action is ExitAction.SCALE_OUT and d.qty == 2
    assert d.new_stop <= 100.0 - 0.01
    ex.confirm_scale_out()
    assert ex.remaining_qty == 8
    assert ex._ladder_rungs_done == 1


def test_short_goal_ladder_looks_down_and_floors_above():
    """Goal ladder mirror: rungs descend below entry; the floor (stop) trails
    one rung ABOVE the crossed rung and never loosens."""
    ex = engine(
        side=OrderSide.SELL,
        atr=0.5,
        ladder_step_atr=1.0,
        ladder_floor_lag=1.0,
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        amend_min_move_atr=0.0,
        amend_throttle_seconds=0,
    )
    assert ex.next_goal == pytest.approx(99.5)  # first rung BELOW entry
    d = ex.on_bar(bar(1, 99.42, low=99.4, volume=900))  # crossed rung 1
    assert d.action is ExitAction.AMEND_STOP
    assert d.new_stop == pytest.approx(100.0, abs=0.011)  # floor at entry
    assert ex.next_goal == pytest.approx(99.0)
    d = ex.on_bar(bar(2, 98.42, low=98.4, volume=900))  # crossed rung 3
    assert d.new_stop == pytest.approx(99.0, abs=0.011)  # floor = rung 2
    assert ex.next_goal == pytest.approx(98.0)
    ex.on_bar(bar(3, 98.9, high=98.95, volume=900))  # bounce: never loosens
    assert ex.current_stop == pytest.approx(99.0, abs=0.011)


def test_short_loiter_cut_fires_on_sustained_time_above_depth():
    """Layer 9 mirror: the loiter level for a short is ABOVE entry; sustained
    time above it is the corpse signature, a dip back below resets."""
    ex = engine(side=OrderSide.SELL, atr=0.5, loiter_depth_pct=1.0, loiter_minutes=30)
    ex.on_bar(bar(5, 101.2, volume=900))  # above 101 → clock starts
    ex.on_bar(bar(20, 100.5, volume=900))  # back below the level → reset
    ex.on_bar(bar(25, 101.3, volume=900))
    d = ex.on_bar(bar(40, 101.2, volume=900))  # 15m < 30m
    assert d.action is not ExitAction.EXIT_NOW
    d = ex.on_bar(bar(56, 101.25, volume=900))  # 31m sustained
    assert d.action is ExitAction.EXIT_NOW and "loiter" in d.reason


def test_short_recovery_exit_banks_the_bounce_after_drawdown():
    """Recovery mirror: a short underwater (price ABOVE entry) long enough
    banks the first real drop back into profit."""
    ex = engine(
        side=OrderSide.SELL,
        atr=0.5,
        recovery_depth_atr=1.0,
        recovery_minutes=3,
        recovery_exit_atr=0.2,
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
    )
    for i in range(1, 4):  # 3 minutes at −0.7 (price above entry)
        assert ex.on_bar(bar(i, 100.7, volume=900)).action is not ExitAction.EXIT_NOW
    assert ex.hurt_minutes >= 3
    assert ex.on_bar(bar(4, 100.4, volume=900)).action is not ExitAction.EXIT_NOW  # not yet
    d = ex.on_bar(bar(5, 99.85, volume=900))  # profit 0.15 ≥ 0.2×0.5
    assert d.action is ExitAction.EXIT_NOW and "recovered" in d.reason


def test_short_giveback_cap_protects_the_trough_peak():
    """Giveback mirror: the peak is measured from the LOWEST price; a retrace
    UP surrendering more than the fraction exits."""
    ex = engine(
        side=OrderSide.SELL,
        atr=1.0,
        giveback_fraction=0.5,
        giveback_arm_atr=0.75,
        k_t1=999.0,
        k_be=999.0,
        volume_death_fraction=0.0,
        amend_throttle_seconds=0,
    )
    ex.on_bar(bar(1, 99.4, low=99.4, volume=900))
    ex.on_bar(bar(2, 98.8, low=98.8, volume=900))  # peak profit 1.2 ≥ 0.75 armed
    d = ex.on_bar(bar(3, 99.6, high=99.65, volume=900))  # profit 0.4 ≤ 0.6
    assert d.action is ExitAction.EXIT_NOW and "giveback cap" in d.reason


def test_short_profit_lock_ratchets_down_with_the_trough():
    """Staged profit lock mirror: lock = entry − fraction×peak, tightening
    DOWNWARD as the trough extends; never loosens on a bounce."""
    ex = engine(
        side=OrderSide.SELL,
        atr=0.5,
        lock_trigger_atr=1.0,
        lock_fraction=0.5,
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        amend_min_move_atr=0.0,
        amend_throttle_seconds=0,
    )
    d = ex.on_bar(bar(1, 99.2, low=99.0, volume=900))  # peak 1.0 ≥ trigger
    assert d.action is ExitAction.AMEND_STOP
    assert d.new_stop == pytest.approx(99.5)  # entry − 0.5×1.0, BELOW entry
    d = ex.on_bar(bar(2, 98.2, low=98.0, volume=900))  # peak 2.0
    assert d.new_stop == pytest.approx(99.0)
    ex.on_bar(bar(3, 98.9, high=98.95, volume=900))
    assert ex.current_stop == pytest.approx(99.0)


def test_short_immediacy_clock_tightens_stop_above_entry():
    """K6 mirror: a short that never worked tightens its stop DOWN from
    +1.5R-above to +0.5R-above entry — still ABOVE entry, ceil-rounded so
    the resting order never claims more protection than it provides."""
    ex = engine(
        side=OrderSide.SELL,
        atr=0.5,
        immediacy_minutes=10,
        immediacy_r=0.25,
        immediacy_stop_r=0.5,
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        amend_min_move_atr=0.0,
        amend_throttle_seconds=0,
    )
    d = ex.on_bar(bar(12, 100.05, high=100.1, low=99.95, volume=900))
    assert d.action is ExitAction.AMEND_STOP
    # entry + 0.5×0.75 = 100.375 → ceil to 100.38 (short rounds UP)
    assert d.new_stop == pytest.approx(100.38)
    assert d.new_stop > 100.0  # still above entry — a loser cut in half, not flipped


def test_short_first_red_bar_is_a_green_bar():
    """K2 mirror: the 'first red candle' against a short is a bar closing UP
    while the position is still in profit."""
    ex = engine(
        side=OrderSide.SELL,
        atr=0.5,
        first_red_arm_r=1.0,
        k_trail=999.0,
        k_be=999.0,
        k_t1=999.0,
        volume_death_fraction=0.0,
        amend_throttle_seconds=0,
    )
    ex.on_bar(sbar(1, 100.0, 99.0, high=100.0, low=99.0, volume=900))  # peak ≥ 1R
    d = ex.on_bar(sbar(2, 99.0, 99.3, high=99.35, low=98.95, volume=900))  # up-bar, in profit
    assert d.action is ExitAction.EXIT_NOW and "first red bar" in d.reason


def test_short_stop_amendment_never_lands_at_or_below_the_market():
    """PYPL clamp mirror: a short's protective stop must rest ABOVE the
    market — a candidate within 1¢ of the price waits for the next tick."""
    ex = engine(side=OrderSide.SELL, atr=0.01, k_be=2.0, k_trail=3.5, amend_throttle_seconds=0)
    d = ex.on_bar(bar(1, 99.985, low=99.97, volume=900))
    # candidate hugs the price from above → clamped or clearly above it
    assert d.action is not ExitAction.AMEND_STOP or d.new_stop > 99.995
    d = ex.on_bar(bar(2, 99.70, low=99.68, volume=900))
    assert d.action is ExitAction.AMEND_STOP
    assert d.new_stop >= 99.70 + 0.01  # strictly above the market
