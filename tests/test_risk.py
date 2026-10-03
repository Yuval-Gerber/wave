def test_risk_headroom_admits_beyond_count_when_risk_is_low():
    """WAVE 2 item 11: floor-locked winners consume zero budget, so the
    count gate relaxes while total risk fits the ceiling."""
    from datetime import date

    from waveapp.engine.risk import RiskEngine, RiskLimits

    eng = RiskEngine(RiskLimits(max_positions=2, max_total_risk_pct=6.0, max_positions_hard=4))
    eng.on_session_start(100_000.0, date(2026, 9, 16))
    from waveapp.broker.base import OrderSide

    # at the count cap but open risk is tiny (winners locked): admitted
    d = eng.can_enter(
        "X", OrderSide.BUY, open_positions=2, today=date(2026, 9, 16), open_risk_pct=0.5
    )
    assert d, d.reason
    # at the count cap with the ceiling full: refused
    d = eng.can_enter(
        "X", OrderSide.BUY, open_positions=2, today=date(2026, 9, 16), open_risk_pct=5.5
    )
    assert not d
    # hard count cap always wins
    d = eng.can_enter(
        "X", OrderSide.BUY, open_positions=4, today=date(2026, 9, 16), open_risk_pct=0.0
    )
    assert not d
    # no risk info -> plain old count gate
    d = eng.can_enter("X", OrderSide.BUY, open_positions=2, today=date(2026, 9, 16))
    assert not d


def test_unlimited_count_when_max_positions_zero():
    """max_positions <= 0 = unlimited COUNT (2026-09-21): the book is
    governed by the total-open-risk ceiling alone."""
    from datetime import date

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    eng = RiskEngine(RiskLimits(max_positions=0, max_total_risk_pct=6.0, max_positions_hard=12))
    eng.on_session_start(100_000.0, date(2026, 9, 21))
    # 30 open positions, tiny open risk (floor-locked winners): admitted
    d = eng.can_enter(
        "X", OrderSide.BUY, open_positions=30, today=date(2026, 9, 21), open_risk_pct=0.5
    )
    assert d, d.reason
    # the risk ceiling still refuses — the ONLY governor now
    d = eng.can_enter(
        "X", OrderSide.BUY, open_positions=3, today=date(2026, 9, 21), open_risk_pct=5.5
    )
    assert not d and "ceiling" in d.reason
    # equity unknown (open_risk None): audit A3-6 — the unlimited book must
    # NOT fail open with no governor; the hard count cap takes over
    d = eng.can_enter("X", OrderSide.BUY, open_positions=30, today=date(2026, 9, 21))
    assert not d and "hard position cap" in d.reason
    # …but below the hard cap the entry is still admitted
    d = eng.can_enter("X", OrderSide.BUY, open_positions=11, today=date(2026, 9, 21))
    assert d, d.reason


def test_unlimited_none_risk_falls_back_to_hard_cap():
    """Audit A3-6: max_positions<=0 with open_risk_pct=None used to pass with
    ZERO governor (the risk term was simply skipped). Now the hard count cap
    is the fallback: refuse at/above it, allow below it, with a distinct
    reason string."""
    from datetime import date

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    eng = RiskEngine(RiskLimits(max_positions=0, max_total_risk_pct=6.0, max_positions_hard=4))
    eng.on_session_start(50_000.0, date(2026, 9, 22))
    today = date(2026, 9, 22)
    # at the hard cap: refused with the distinct reason
    d = eng.can_enter("X", OrderSide.BUY, open_positions=4, today=today, open_risk_pct=None)
    assert not d
    assert "open risk unknown" in d.reason and "4" in d.reason
    # above it too
    assert not eng.can_enter("X", OrderSide.BUY, open_positions=9, today=today)
    # below the hard cap the fallback admits
    d = eng.can_enter("X", OrderSide.BUY, open_positions=3, today=today, open_risk_pct=None)
    assert d, d.reason
    # a KNOWN risk read keeps the risk ceiling as the governor (unchanged)
    d = eng.can_enter("X", OrderSide.BUY, open_positions=9, today=today, open_risk_pct=0.5)
    assert d, d.reason


def test_session_roll_rearms_daily_halt_and_rebases_baselines():
    """Audit A3-7: a continuous 24/5 run never re-called on_session_start, so
    the daily loss halt stuck forever and 'down 3% today' was measured
    against a days-old baseline. maybe_roll_session rolls ONLY on an actual
    date change with known equity, via the existing on_session_start."""
    from datetime import date

    from waveapp.engine.risk import HaltState, RiskEngine

    eng = RiskEngine()
    d1, d2 = date(2026, 9, 25), date(2026, 9, 28)  # Friday → Monday
    eng.on_session_start(100_000.0, d1)
    eng.update_equity(96_900.0)  # -3.1% → daily halt
    assert eng.halt_state is HaltState.DAILY_LOSS
    # same day: no roll, halt holds
    assert eng.maybe_roll_session(96_900.0, d1) is False
    assert eng.halt_state is HaltState.DAILY_LOSS
    # unknown equity: date changed but baselines must NOT be rebased blind
    assert eng.maybe_roll_session(None, d2) is False
    assert eng.halt_state is HaltState.DAILY_LOSS
    assert eng._day_start_equity == 100_000.0
    # real roll: new day + known equity → daily halt auto-re-arms (existing
    # on_session_start behavior) and the day baseline rebases
    assert eng.maybe_roll_session(96_900.0, d2) is True
    assert eng.halt_state is HaltState.NONE
    assert eng._day_start_equity == 96_900.0
    assert eng._current_day == d2
    # a -3% day measured from the NEW baseline halts again (Monday also
    # rebased the week to 96,900, so this is a daily halt, not a weekly one)
    eng.update_equity(93_800.0)
    assert eng.halt_state is HaltState.DAILY_LOSS


def test_session_roll_rebases_week_on_monday_and_keeps_weekly_halt():
    """A3-7 companion: Monday rebases the week baseline; a WEEKLY halt is
    NEVER cleared by the roll (manual re-arm only — preserved exactly)."""
    from datetime import date

    from waveapp.engine.risk import HaltState, RiskEngine

    eng = RiskEngine()
    eng.on_session_start(100_000.0, date(2026, 9, 18))  # Friday
    eng.update_equity(93_900.0)  # -6.1% → weekly halt
    assert eng.halt_state is HaltState.WEEKLY_LOSS
    assert eng.maybe_roll_session(93_900.0, date(2026, 9, 21)) is True  # Monday
    assert eng.halt_state is HaltState.WEEKLY_LOSS, "weekly halt needs manual re-arm"
    assert eng._week_start_equity == 93_900.0  # Monday rebases the week


def test_shorts_master_gate_refuses_every_sell_when_disabled(caplog):
    """S1 MASTER GATE: with shorts_enabled=False (the default — matches
    config.shorts_enabled), can_enter refuses side=SELL outright, so no
    short order can ever reach the broker until the owner flips the flag. The
    A3-8 'SSR detection not wired' tripwire is retired (S0 wired the sweep).
    """
    import logging
    from datetime import date

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    assert RiskLimits().shorts_enabled is False, "shorts must default OFF"
    eng = RiskEngine()
    eng.on_session_start(100_000.0, date(2026, 9, 23))
    today = date(2026, 9, 23)
    d = eng.can_enter("XYZ", OrderSide.SELL, 0, today)
    assert not d and d.reason == "shorts are disabled (shorts_enabled=false)"
    # even with plenty of risk headroom, ETB names, no SSR — still refused
    d = eng.can_enter("AAPL", OrderSide.SELL, 0, today, open_risk_pct=0.0, short_open_risk_pct=0.0)
    assert not d and "shorts are disabled" in d.reason
    # longs are untouched
    assert eng.can_enter("XYZ", OrderSide.BUY, 0, today)
    # the retired tripwire never logs anymore
    with caplog.at_level(logging.WARNING, logger="wave.risk"):
        eng.can_enter("XYZ", OrderSide.SELL, 0, today)
    assert not any("SSR detection not wired" in r.message for r in caplog.records)


def test_shorts_allowed_when_enabled_no_ssr_and_share_headroom():
    """S1: shorts_enabled=True + no SSR + short-book headroom → admitted.
    The master gate sits BEFORE the SSR check: an SSR symbol is refused for
    SSR only once the flag is on."""
    from datetime import date

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    eng = RiskEngine(RiskLimits(shorts_enabled=True))
    eng.on_session_start(100_000.0, date(2026, 9, 23))
    today = date(2026, 9, 23)
    d = eng.can_enter("PLTR", OrderSide.SELL, 0, today, open_risk_pct=0.0, short_open_risk_pct=0.0)
    assert d, d.reason
    # SSR still governs the enabled book
    eng.record_ssr_trigger("PLTR", today)
    d = eng.can_enter("PLTR", OrderSide.SELL, 0, today, open_risk_pct=0.0, short_open_risk_pct=0.0)
    assert not d and "SSR active" in d.reason


def test_short_book_risk_share_caps_at_half_the_ceiling():
    """S1 (§12: short book ≤ 50% of long limits): shorts' total open risk
    plus the incoming short's budget (risk_per_trade × short_size_factor)
    must fit short_risk_share × max_total_risk_pct. Longs never read it."""
    from datetime import date

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    limits = RiskLimits(
        shorts_enabled=True,
        max_total_risk_pct=6.0,
        short_risk_share=0.5,
        risk_per_trade_pct=1.0,
        short_size_factor=0.5,
    )
    eng = RiskEngine(limits)
    eng.on_session_start(100_000.0, date(2026, 9, 23))
    today = date(2026, 9, 23)
    # ceiling = 3.0%; incoming short budget = 0.5% → 2.4 + 0.5 ≤ 3.0: admitted
    d = eng.can_enter("X", OrderSide.SELL, 1, today, open_risk_pct=2.4, short_open_risk_pct=2.4)
    assert d, d.reason
    # 2.6 + 0.5 > 3.0: refused with the share reason
    d = eng.can_enter("X", OrderSide.SELL, 1, today, open_risk_pct=2.6, short_open_risk_pct=2.6)
    assert not d and "short book risk share" in d.reason
    # the same open risk on the LONG book does not trip the share gate
    d = eng.can_enter("X", OrderSide.BUY, 1, today, open_risk_pct=2.6)
    assert d, d.reason
    # unknown short risk (equity unknown) → the gate stands down; the A3-6
    # hard-cap / count fallbacks remain the governor
    d = eng.can_enter("X", OrderSide.SELL, 1, today, open_risk_pct=None, short_open_risk_pct=None)
    assert d, d.reason


def test_ssr_trigger_blocks_short_rest_of_day_and_next_day_then_expires():
    """S0: a recorded Rule-201 trigger blocks SELL entries for the rest of
    the trigger day AND the next trading day, never longs, then expires."""
    from datetime import date

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    eng = RiskEngine(RiskLimits(shorts_enabled=True))  # isolate the SSR gate (S1)
    eng.on_session_start(100_000.0, date(2026, 9, 23))  # Wednesday
    eng.record_ssr_trigger("DROP", date(2026, 9, 23))
    assert eng.ssr_trigger_date("drop") == date(2026, 9, 23)  # case-safe accessor
    assert not eng.can_enter("DROP", OrderSide.SELL, 0, date(2026, 9, 23))
    assert not eng.can_enter("DROP", OrderSide.SELL, 0, date(2026, 9, 24))  # next day
    assert eng.can_enter("DROP", OrderSide.BUY, 0, date(2026, 9, 23))  # longs untouched
    assert eng.can_enter("DROP", OrderSide.SELL, 0, date(2026, 9, 25))  # expired
    assert eng.can_enter("OTHER", OrderSide.SELL, 0, date(2026, 9, 23))  # per-symbol


def test_short_entries_gate_on_2000_equity_floor():
    """Live-prep (2026-09-23): new shorts need >= $2,000 equity (US
    margin regulation); a dip below the floor blocks NEW shorts only —
    held shorts keep their exits (which never pass can_enter)."""
    from datetime import date

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    eng = RiskEngine(RiskLimits(max_positions=0, shorts_enabled=True))
    eng.on_session_start(2040.0, date(2026, 9, 23))
    ok = eng.can_enter("XYZ", OrderSide.SELL, 0, date(2026, 9, 23), open_risk_pct=0.0)
    assert ok, ok.reason
    eng.update_equity(1995.0)  # dips under the floor (−2.2%: no loss halt)
    d = eng.can_enter("XYZ", OrderSide.SELL, 0, date(2026, 9, 23), open_risk_pct=0.0)
    assert not d and "$2,000" in d.reason
    # longs unaffected at any balance
    assert eng.can_enter("XYZ", OrderSide.BUY, 0, date(2026, 9, 23), open_risk_pct=0.0)
    eng.update_equity(2050.0)  # balance recovers -> shorts re-arm automatically
    assert eng.can_enter("XYZ", OrderSide.SELL, 0, date(2026, 9, 23), open_risk_pct=0.0)
