"""KITCHEN 2.0 seams (2026-09-02 kitchen hunt, blueprint II.10–II.14).

Every layer is parameter-disabled by default — the last test proves the
default path is byte-identical to the pre-kitchen engine. Each layer test
arms ONLY its own parameters (plus k_t1/k_be=999 to silence the default
scale-out/breakeven where they would fire first).
"""

from datetime import UTC, datetime, timedelta

from waveapp.broker.base import OrderSide
from waveapp.data.hub import Bar
from waveapp.engine.exits import ExitAction, ExitEngine, ExitParams

T0 = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)


def kbar(i: int, open_: float, high: float, low: float, close: float, volume=1000.0) -> Bar:
    return Bar(
        symbol="AXTI",
        start=T0 + timedelta(minutes=i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


def bar(i: int, close: float, high=None, low=None, volume=1000.0) -> Bar:
    return kbar(
        i,
        close,
        high if high is not None else close + 0.05,
        low if low is not None else close - 0.05,
        close,
        volume,
    )


def engine(entry=100.0, qty=10, atr=0.5, **overrides) -> ExitEngine:
    return ExitEngine(
        side=OrderSide.BUY,
        entry_price=entry,
        qty=qty,
        atr_at_entry=atr,
        params=ExitParams(**overrides),
        entry_time=T0,
        entry_bar_volume=1000.0,
    )


# -- K5: trail clamp ---------------------------------------------------------


def test_trail_cap_binds_on_extreme_natr_names():
    """8%-of-price ATR: the uncapped chandelier is 28 points away (inert);
    the 1.5% cap makes it a real trail."""
    uncapped = engine(atr=8.0, k_t1=999.0, k_be=999.0)
    capped = engine(atr=8.0, k_t1=999.0, k_be=999.0, trail_cap_pct=1.5)
    move = bar(1, 110.0)
    loose = uncapped.on_bar(move)
    assert loose.new_stop is None or loose.new_stop <= 94.05  # 2×ATR = 16 pts away
    decision = capped.on_bar(move)
    assert decision.action is ExitAction.AMEND_STOP
    assert decision.new_stop is not None and decision.new_stop >= 108.5  # best 110.05 − 1.5


def test_trail_cap_never_binds_on_normal_names():
    """On a 0.5%-ATR name the k×ATR trail is already tighter than the cap —
    identical decisions with and without the cap (zero regression risk)."""
    plain = engine(k_t1=999.0, k_be=999.0)
    capped = engine(k_t1=999.0, k_be=999.0, trail_cap_pct=1.5)
    for i, px in enumerate((100.4, 100.9, 101.4), start=1):
        a, b = plain.on_bar(bar(i, px)), capped.on_bar(bar(i, px))
        assert (a.action, a.new_stop) == (b.action, b.new_stop)


# -- K1: floor-armed R giveback cap -----------------------------------------


def test_giveback_floor_exits_after_arming():
    ex = engine(k_t1=999.0, giveback_arm_r=1.0, giveback_frac_r=0.5)
    # peak +1.0 = 1.33R → armed; profit 0.9 still above half the peak
    assert ex.on_bar(bar(1, 100.9, high=101.0)).action is not ExitAction.EXIT_NOW
    decision = ex.on_bar(bar(2, 100.45, high=100.5))  # profit 0.45 ≤ 0.5×peak
    assert decision.action is ExitAction.EXIT_NOW
    assert "giveback floor" in decision.reason


def test_giveback_floor_dead_below_arming():
    """Sub-floor noise never triggers it — the rejected always-on cap did."""
    ex = engine(k_t1=999.0, k_be=999.0, giveback_arm_r=1.0, giveback_frac_r=0.5)
    ex.on_bar(bar(1, 100.4, high=100.5))  # peak 0.5 = 0.67R — NOT armed
    decision = ex.on_bar(bar(2, 100.05, high=100.1))  # 90% retrace of the peak
    assert decision.action is not ExitAction.EXIT_NOW


def test_giveback_tightens_as_peak_grows():
    ex = engine(
        k_t1=999.0,
        k_be=999.0,
        giveback_arm_r=1.0,
        giveback_frac_r=0.5,
        giveback_tighten_per_r=0.1,
    )
    ex.on_bar(bar(1, 102.9, high=103.0))  # peak 3.0 = 4R → g = 0.5 − 0.1×3 = 0.2
    decision = ex.on_bar(bar(2, 102.35, high=102.4))  # profit 2.35 ≤ 0.8×3.0
    assert decision.action is ExitAction.EXIT_NOW


# -- K2: first-red-candle ----------------------------------------------------


def test_first_red_banks_the_spike():
    ex = engine(k_t1=999.0, first_red_arm_r=0.5)
    up = kbar(1, 100.2, 101.0, 100.1, 100.9)  # peak 1.0 = 1.33R → armed
    assert ex.on_bar(up).action is not ExitAction.EXIT_NOW
    red = kbar(2, 100.8, 100.85, 100.55, 100.6)  # closes red, still green
    decision = ex.on_bar(red)
    assert decision.action is ExitAction.EXIT_NOW
    assert "first red" in decision.reason


def test_first_red_unarmed_ignores_red_bars():
    ex = engine(k_t1=999.0, k_be=999.0, first_red_arm_r=2.0)
    ex.on_bar(kbar(1, 100.2, 100.6, 100.1, 100.5))  # peak 0.8R < 2R
    decision = ex.on_bar(kbar(2, 100.4, 100.45, 100.15, 100.2))
    assert decision.action is not ExitAction.EXIT_NOW


# -- K3: no-new-high timeout -------------------------------------------------


def test_no_new_high_timeout_banks_a_stall():
    ex = engine(k_t1=999.0, k_be=999.0, no_new_high_minutes=10)
    ex.on_bar(bar(1, 100.5, high=100.6))  # new best at T0+1
    for i in range(2, 11):  # stalled below the best, still green
        assert ex.on_bar(bar(i, 100.3, high=100.35)).action is not ExitAction.EXIT_NOW
    decision = ex.on_bar(bar(11, 100.3, high=100.35))  # T0+11 − T0+1 = 10m
    assert decision.action is ExitAction.EXIT_NOW
    assert "no new high" in decision.reason


def test_no_new_high_keeps_running_winners():
    """A fresh high resets the clock — runners are never cut."""
    ex = engine(k_t1=999.0, k_be=999.0, no_new_high_minutes=10)
    for i in range(1, 15):  # a new high every bar
        decision = ex.on_bar(bar(i, 100.0 + i * 0.2))
        assert decision.action is not ExitAction.EXIT_NOW


# -- K4: extension-bar sell into strength ------------------------------------


def test_extension_bar_scales_out_into_strength():
    ex = engine(k_t1=999.0, k_be=999.0, ext_bar_atr=2.0, ext_bar_fraction=0.5)
    stretch = kbar(1, 100.1, 101.5, 100.1, 101.4)  # range 1.4 ≥ 2×ATR(0.5)
    decision = ex.on_bar(stretch)
    assert decision.action is ExitAction.SCALE_OUT
    assert "extension bar" in decision.reason
    assert decision.qty == 5
    # A1-6: the ledger commit is deferred until the actor confirms the submit
    assert ex.remaining_qty == 10
    ex.confirm_scale_out()
    assert ex.remaining_qty == 5
    # once per position: a second stretch bar does not re-fire the layer
    again = ex.on_bar(kbar(2, 101.4, 102.9, 101.4, 102.8))
    assert again.action is not ExitAction.SCALE_OUT


# -- K6: immediacy clock -----------------------------------------------------


def test_immediacy_clock_tightens_a_trade_that_never_worked():
    ex = engine(k_t1=999.0, k_be=999.0, immediacy_minutes=15, immediacy_r=0.25)
    for i in range(1, 15):  # bars T0+1 … T0+14 — clock not yet elapsed
        assert ex.on_bar(bar(i, 100.05)).action is ExitAction.NONE
    decision = ex.on_bar(bar(15, 100.05))  # age = 15 min exactly
    assert decision.action is ExitAction.AMEND_STOP
    assert decision.new_stop == 99.62  # entry − 0.5R = 100 − 0.375, floored
    # ratchet-only: the tightening happens once, never loosens or repeats
    assert ex.on_bar(bar(16, 100.05)).action is ExitAction.NONE


def test_immediacy_never_touches_a_working_trade():
    ex = engine(k_t1=999.0, k_be=999.0, immediacy_minutes=15, immediacy_r=0.25)
    ex.on_bar(bar(1, 100.4, high=100.5))  # peak 0.5 = 0.67R > 0.25R — it worked
    for i in range(2, 20):
        decision = ex.on_bar(bar(i, 100.05, high=100.1))
        assert decision.new_stop is None or decision.new_stop < 99.6


# -- the default path is untouched -------------------------------------------


def test_all_kitchen_layers_off_by_default():
    params = ExitParams()
    assert params.trail_cap_pct == 0.0
    assert params.giveback_arm_r == 0.0
    assert params.first_red_arm_r == 0.0
    assert params.ext_bar_atr == 0.0
    assert params.no_new_high_minutes == 0
    assert params.immediacy_minutes == 0
    ex = engine()  # plain defaults: the first target still fires as before
    decision = ex.on_bar(bar(1, 100.7))
    assert decision.action is ExitAction.SCALE_OUT
    assert "first target" in decision.reason


# -- phase-2 ablation switch --------------------------------------------------


def test_vwap_recross_switch():
    """The recross exit fires by default and goes silent when switched off —
    the phase-2 ablation isolates the one layer the bare control contained."""
    on = engine(k_t1=999.0, k_be=999.0)
    off = engine(k_t1=999.0, k_be=999.0, vwap_recross_exit=False)
    green_below_vwap = bar(1, 100.3)  # in profit, VWAP lost (vwap above price)
    fired = on.on_bar(green_below_vwap, session_vwap=100.5)
    assert fired.action is ExitAction.EXIT_NOW and "VWAP recross" in fired.reason
    silent = off.on_bar(green_below_vwap, session_vwap=100.5)
    assert silent.action is not ExitAction.EXIT_NOW


def test_vwap_stop_trail_exits_at_a_loss_too():
    """II.21: the VWAP trail fires on ANY close through VWAP against the
    position — unlike the recross layer, profit is not required. OFF by
    default (live unchanged)."""
    armed = engine(k_t1=999.0, k_be=999.0, vwap_stop_trail=True, vwap_recross_exit=False)
    losing_below_vwap = bar(1, 99.6)  # underwater AND below VWAP
    fired = armed.on_bar(losing_below_vwap, session_vwap=99.9)
    assert fired.action is ExitAction.EXIT_NOW and "VWAP trail" in fired.reason
    off = engine(k_t1=999.0, k_be=999.0, vwap_recross_exit=False)
    assert off.on_bar(bar(1, 99.6), session_vwap=99.9).action is not ExitAction.EXIT_NOW


# -- Cycle-1 Build 2 seams ----------------------------------------------------


def test_dead_money_scratch_cuts_the_stale_loser():
    """15+ min old, no new high for 10 min, underwater, below VWAP → scratch.
    Keyed on staleness — a fresh dip never triggers it."""
    ex = engine(k_t1=999.0, k_be=999.0, dead_scratch_minutes=15)
    for i in range(1, 15):  # young loser — protected by the stop only
        d = ex.on_bar(bar(i, 99.8), session_vwap=100.2)
        assert d.action is not ExitAction.EXIT_NOW
    d = ex.on_bar(bar(16, 99.8), session_vwap=100.2)  # old + stale + wrong side
    assert d.action is ExitAction.EXIT_NOW and "dead money" in d.reason


def test_dead_money_never_touches_green_or_recovering():
    ex = engine(k_t1=999.0, k_be=999.0, dead_scratch_minutes=15)
    # green trade at the same age → untouched
    d = ex.on_bar(bar(20, 100.4), session_vwap=100.1)
    assert d.action is not ExitAction.EXIT_NOW
    # underwater but ABOVE VWAP (right side) → untouched
    ex2 = engine(k_t1=999.0, k_be=999.0, dead_scratch_minutes=15)
    d = ex2.on_bar(bar(20, 99.8), session_vwap=99.5)
    assert d.action is not ExitAction.EXIT_NOW


def test_statistical_breakeven_is_peak_armed():
    """R-based trigger arms on the PEAK, then the stop rides at breakeven
    even after price falls back — the round-trip dies here."""
    ex = engine(k_t1=999.0, k_be=999.0, k_be_r=0.5, amend_throttle_seconds=0)
    # initial risk = 0.75 (1.5×ATR 0.5); peak +0.5R = +0.375
    d = ex.on_bar(bar(1, 100.2, high=100.4))  # peak 0.4 ≥ 0.375 → armed
    assert d.action is ExitAction.AMEND_STOP and d.new_stop >= 100.01
    assert ex.breakeven_done
    ex.on_bar(bar(2, 100.05))  # fade back — stop stays at/above breakeven
    assert ex.current_stop >= 100.01


# -- Cycle-2 B1: the late-low cut ---------------------------------------------


def test_late_low_cuts_fresh_adverse_extreme_late():
    """A NEW low printed ≥30m in while ≥0.5% underwater = the 17%-recovery
    population — cut. (Depth via close, freshness via the bar's extreme.)"""
    ex = engine(k_t1=999.0, k_be=999.0, late_low_minutes=30)
    ex.on_bar(bar(1, 99.6, low=99.5))  # early dip — worst set, NOT cut (early)
    for i in range(2, 30):  # drifting, no NEW lows late enough yet
        d = ex.on_bar(bar(i, 99.6, low=99.55))
        assert d.action is not ExitAction.EXIT_NOW
    d = ex.on_bar(bar(31, 99.4, low=99.3))  # fresh low at minute 31, −0.6%
    assert d.action is ExitAction.EXIT_NOW and "late low" in d.reason


def test_late_low_leaves_early_violence_and_shallow_dips_alone():
    ex = engine(k_t1=999.0, k_be=999.0, late_low_minutes=30)
    # early deep dip then recovery — never cut (the 70%-recovery population)
    ex.on_bar(bar(1, 99.0, low=98.9))
    for i in range(2, 40):
        d = ex.on_bar(bar(i, 100.3, high=100.4, low=100.1))
        assert d.action is not ExitAction.EXIT_NOW
    # late NEW low but only −0.2% underwater (shallow) — not cut
    ex2 = engine(k_t1=999.0, k_be=999.0, late_low_minutes=30)
    ex2.on_bar(bar(1, 99.9, low=99.85))
    d = ex2.on_bar(bar(35, 99.8, low=99.75))
    assert d.action is not ExitAction.EXIT_NOW


def test_late_low_short_side_mirrors():
    from waveapp.broker.base import OrderSide as _S

    ex = ExitEngine(
        side=_S.SELL,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=ExitParams(k_t1=999.0, k_be=999.0, late_low_minutes=30),
        entry_time=T0,
        entry_bar_volume=1000.0,
    )
    ex.on_bar(kbar(1, 100.3, 100.5, 100.2, 100.4))  # against a short: highs
    d = ex.on_bar(kbar(35, 100.5, 100.8, 100.5, 100.7))  # fresh HIGH late, −0.7%
    assert d.action is ExitAction.EXIT_NOW and "late low" in d.reason


# -- Peak Engine Stage B: the cone + the disorder filter ----------------------


def test_cone_tightens_toward_the_close():
    """min(chandelier, z·σ̂·√τ): far out the chandelier binds; close to the
    boundary (but above the 10-min flatten) the cone takes over."""
    far = engine(k_t1=999.0, k_be=999.0, cone_z=0.8)
    near = engine(k_t1=999.0, k_be=999.0, cone_z=0.8)
    up = bar(1, 102.0)  # best 102.05, chandelier dist 2×0.5 = 1.0
    d_far = far.on_bar(up, minutes_to_close_boundary=380.0)
    d_near = near.on_bar(up, minutes_to_close_boundary=12.0)
    # cone far: 0.8·(0.5/1.6)·√380 ≈ 4.9 → chandelier binds (stop ≈ 101.05)
    # cone near: 0.8·0.3125·√12 ≈ 0.87 → cone binds (stop ≈ 101.18)
    assert d_far.new_stop is not None and d_near.new_stop is not None
    assert d_near.new_stop > d_far.new_stop


def test_cone_off_without_boundary_or_param():
    plain = engine(k_t1=999.0, k_be=999.0)
    coned = engine(k_t1=999.0, k_be=999.0, cone_z=1.12)
    a = plain.on_bar(bar(1, 102.0))
    b = coned.on_bar(bar(1, 102.0))  # no minutes passed → cone inert
    assert (a.action, a.new_stop) == (b.action, b.new_stop)


def test_disorder_filter_fires_on_a_flip():
    """Steady up-bars keep Π low; a run of sharp adverse bars drives
    P(flip) over the threshold and exits."""
    ex = engine(
        k_t1=999.0,
        k_be=999.0,
        disorder_hazard=0.05,
        disorder_drift=0.3,
        disorder_threshold=0.9,
    )
    px = 100.0
    for i in range(1, 8):  # steady climb — no exit
        px += 0.2
        d = ex.on_bar(bar(i, px))
        assert d.action is not ExitAction.EXIT_NOW
    fired = False
    for i in range(8, 20):  # hard reversal
        px -= 0.35
        d = ex.on_bar(bar(i, px))
        if d.action is ExitAction.EXIT_NOW:
            assert "disorder" in d.reason
            fired = True
            break
    assert fired, "the flip must be detected within the reversal"


def test_disorder_off_by_default():
    params = ExitParams()
    assert params.disorder_hazard == 0.0 and params.cone_z == 0.0
