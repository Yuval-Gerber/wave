"""The Position Judge (2026-09-20): stance transitions,
stickiness, and the INTW-2026-09-17 scenario — losing on weak volume with the
story intact must be WAIT, never CUT."""

import pytest

from waveapp.engine.position_judge import (
    BANK,
    CUT,
    FLIP_DWELL_SECONDS,
    READY,
    RIDE,
    WAIT,
    JudgeState,
    judge_second,
    ready_trigger_px,
)


def _state(entry=100.0, stop=98.0, atr=1.0) -> JudgeState:
    return JudgeState(entry_px=entry, strategy_stop=stop, atr_px=atr)


def _run(st, seconds):
    stance = st.stance
    for t_ms, px, v in seconds:
        stance = judge_second(st, t_ms, px, v)
    return stance


def test_short_judge_armed_logs_once_at_birth(caplog):
    """S2 (2026-09-23): the A2-5 long-only refusal is retired — a short is
    judged through the normalized frame. Its state announces itself with one
    INFO line at birth, and the winning tape the old guard froze on (price
    bleeding below entry on real volume) now RIDES instead of being frozen
    at WAIT (or, under the pre-frame inverted math, CUT at its best)."""
    import logging

    with caplog.at_level(logging.INFO, logger="wave.judge"):
        st = JudgeState(entry_px=100.0, strategy_stop=102.0, atr_px=1.0, side=-1, symbol="SHRT")
        stance = st.stance
        for i in range(1, 90):
            stance = judge_second(st, i * 1000, 100.0 - i * 0.05, 3000)
    armed = [r for r in caplog.records if "short judge armed" in r.message]
    assert len(armed) == 1, "one armed line per state, at creation"
    assert "SHRT" in armed[0].getMessage()
    assert stance == RIDE  # a winning short is a winner, not a frozen orphan
    assert len(st.ring) > 0  # evidence accumulates now


def test_big_profit_alive_rides():
    st = _state()
    # steady climb to +3 ATR on healthy volume
    secs = [(i * 1000, 100 + i * 0.05, 1000) for i in range(1, 80)]
    assert _run(st, secs) == RIDE


def test_big_profit_dying_arms_ready_and_trigger_is_below_peak():
    st = _state()
    secs = [(i * 1000, 100 + i * 0.05, 2000) for i in range(1, 61)]  # to +3A
    _run(st, secs)
    # push dies: no new highs, volume collapses, dwell elapses. With the
    # momentum-death detector (2026-09-21) this path accumulates dying fuel
    # AND an aged peak (2/3 signs) -> READY with the below-peak trigger;
    # far from the peak zone, no failed-high sign fires (no 3/3 take-now).
    t0 = 61_000
    secs = [(t0 + i * 1000, 102.6, 100) for i in range(200 + FLIP_DWELL_SECONDS)]
    assert _run(st, secs) == READY
    assert not st.take_now
    assert ready_trigger_px(st) < st.peak_px


def test_all_three_death_signs_take_now():
    """Dying fuel + failed highs + aged peak = READY with an immediate
    trigger (sell into the remaining strength, not after the slide)."""
    st = _state()
    secs = [(i * 1000, 100 + i * 0.05, 3000) for i in range(1, 61)]  # to +3A
    _run(st, secs)
    peak = st.peak_px
    t = 61_000
    seq = []
    # two pushes into the peak zone that fail (sign 2), on dying volume
    for _ in range(2):
        for _ in range(6):
            seq.append((t, peak - 0.05, 120))
            t += 1000  # in the zone
        for _ in range(6):
            seq.append((t, peak - 0.40, 120))
            t += 1000  # backs off
    # then time passes: peak ages beyond the death threshold (sign 3)
    for _ in range(200 + FLIP_DWELL_SECONDS):
        seq.append((t, peak - 0.30, 100))
        t += 1000
    assert _run(st, seq) == READY
    assert st.take_now
    assert ready_trigger_px(st) == float("inf")


def test_intw_scenario_weak_dip_with_story_intact_is_wait_not_cut():
    st = _state(entry=25.43, stop=24.52, atr=0.25)
    # 60s of ordinary trade to build the volume baseline
    secs = [(i * 1000, 25.40 + (i % 3) * 0.01, 1000) for i in range(1, 61)]
    _run(st, secs)
    # the opening dip: price bleeds toward 25.0 (ABOVE the 24.52 story line)
    # on weak volume — the 09-17 live kitchen cut this at 24.98 for −$583
    secs = [(61_000 + i * 1000, 25.20 - i * 0.005, 300) for i in range(40)]
    assert _run(st, secs) == WAIT


def test_story_broken_on_real_volume_is_cut():
    st = _state(entry=100.0, stop=98.0, atr=1.0)
    secs = [(i * 1000, 100.0, 1000) for i in range(1, 61)]
    _run(st, secs)
    # crashes THROUGH the strategy stop on heavy volume, persists past dwell
    secs = [(61_000 + i * 1000, 97.5, 5000) for i in range(FLIP_DWELL_SECONDS + 5)]
    assert _run(st, secs) == CUT


def test_small_stale_profit_banks():
    st = _state()
    secs = [(i * 1000, 100.8, 2000) for i in range(1, 30)]  # +0.8A quickly
    _run(st, secs)
    # then nothing happens for 5 minutes and the push dies
    secs = [(30_000 + i * 1000, 100.75, 100) for i in range(320)]
    assert _run(st, secs) == BANK


def test_one_red_tick_never_flips_a_stance():
    st = _state()
    secs = [(i * 1000, 100 + i * 0.05, 2000) for i in range(1, 61)]
    assert _run(st, secs) == RIDE
    # a single ugly second — far too short for the dwell
    assert judge_second(st, 61_000, 97.0, 9000) == RIDE


def test_hold_cuts_suppresses_wrong_cut_but_not_forever():
    """WAIT holds the kitchen's wrong-cut fire; releasing it restores the cut
    (the broker hard stop is outside this engine and never touched)."""
    from waveapp.engine.master_key import MasterKeyEngine, MasterKeyParams

    p = MasterKeyParams()
    eng = MasterKeyEngine(
        entry_price=100.0, qty=10, atr_pct=1.0, opened_ms=0, last_reentry_ms=10**15, params=p
    )
    eng.on_second(1000, 100.0, 100.0, 100.0, False, False, False, vol60=100)  # entry
    assert eng.held > 0
    # a drop deep enough for the wrong-cut, but WAIT holds fire
    eng.hold_cuts = True
    eng.on_second(2000, 97.0, 97.0, 100.0, False, False, False, vol60=100)
    assert eng.held > 0  # still holding — the judge said WAIT
    # judge releases (story broke): the cut fires normally
    eng.hold_cuts = False
    eng.on_second(3000, 97.0, 97.0, 100.0, False, False, False, vol60=100)
    assert eng.held == 0


# -- closing guard (2026-09-21: "take any profit that is possible than to
# lose" — the 15:50 flatten sold 7 positions for −$718) ----------------------


def _climb(st, to_atr=3.0, vol=2000, guard=None):
    """Steady climb on healthy volume; returns the last t_ms."""
    n = int(to_atr / 0.05)
    for i in range(1, n + 1):
        judge_second(st, i * 1000, 100 + i * 0.05, vol, mins_to_flatten=guard)
    return n * 1000


def test_far_from_close_guard_is_off_and_behavior_unchanged():
    st = _state()
    secs = [(i * 1000, 100 + i * 0.05, 1000) for i in range(1, 80)]
    for t, px, v in secs:
        judge_second(st, t, px, v, mins_to_flatten=200.0)
    assert st.stance == RIDE
    assert not st.close_guard


def test_close_guard_one_death_sign_arms_ready():
    """Aged peak alone: far from the close that's RIDE — inside the guard
    it arms READY, with the tighter 0.20A trigger."""
    st = _state()
    t = _climb(st, guard=5.0)
    peak = st.peak_px
    for i in range(1, 200 + FLIP_DWELL_SECONDS):
        judge_second(st, t + i * 1000, peak - 0.30, 1000, mins_to_flatten=5.0)
    assert st.stance == READY
    assert not st.take_now
    assert ready_trigger_px(st) == peak - 0.20  # tightened off the peak


def test_close_guard_two_signs_take_now():
    st = _state()
    t = _climb(st, guard=5.0)
    peak = st.peak_px
    # dying fuel + aged peak (2/3): guard says take NOW into the strength
    for i in range(1, 200 + FLIP_DWELL_SECONDS):
        judge_second(st, t + i * 1000, peak - 0.30, 100, mins_to_flatten=5.0)
    assert st.stance == READY
    assert st.take_now
    assert ready_trigger_px(st) == float("inf")


def test_close_guard_banks_small_profit_when_dying():
    """+0.35A — outside the guard that's not even 'small profit'; inside it,
    a dying push banks the crumbs instead of donating them to the flatten."""
    st = _state()
    for i in range(1, 9):
        judge_second(st, i * 1000, 100 + i * 0.05, 2000, mins_to_flatten=5.0)
    t = 8000  # peak 100.40, +0.4A
    for i in range(1, 130 + FLIP_DWELL_SECONDS):
        judge_second(st, t + i * 1000, 100.35, 100, mins_to_flatten=5.0)
    assert st.stance == BANK


def test_close_guard_loser_new_low_cuts_where_wait_would_hold():
    """A losing position printing fresh 120s lows inside the guard is CUT —
    the same tape far from the close stays WAIT (the INTW lesson holds)."""

    def run(guard):
        st = _state(entry=100.0, stop=98.0, atr=1.0)
        for i in range(1, 62):
            judge_second(st, i * 1000, 99.80, 1000, mins_to_flatten=guard)
        for i in range(40):
            judge_second(st, 62_000 + i * 1000, 99.70 - i * 0.01, 300, mins_to_flatten=guard)
        return st.stance

    assert run(None) == WAIT
    assert run(200.0) == WAIT
    assert run(5.0) == CUT


# -- 2026-09-22: give-back protection, the bleeder rule, the door judge ------


def test_twlo_giveback_banks_before_the_round_trip():
    """TWLO 2026-09-22: peaked +0.71A (+$200) — under the 1.2A hair-trigger —
    then slid to a loss. The give-back rule banks it while it is still a win."""
    st = _state()
    for i in range(1, 15):
        judge_second(st, i * 1000, 100 + i * 0.05, 2000)  # peak 100.70
    t = 15_000
    stance = None
    for i in range(1, 200):
        px = max(100.70 - i * 0.01, 100.20)  # bleeding the peak away
        stance = judge_second(st, t + i * 1000, px, 100)
        if stance == BANK:
            break
    assert stance == BANK
    assert (px - 100.0) > 0.10  # banked while still clearly positive


def test_big_peak_stays_armed_through_a_fast_fall():
    """The TWLO mechanism bug: a fast fall used to drop current profit below
    the BIG branch and DISARM the very READY it should have fired. The peak
    owns the regime now."""
    st = _state()
    secs = [(i * 1000, 100 + i * 0.05, 2000) for i in range(1, 61)]  # peak +3A
    _run(st, secs)
    # dying + aged → READY arms
    t = 61_000
    for i in range(1, 200 + FLIP_DWELL_SECONDS):
        judge_second(st, t + i * 1000, 102.6, 100)
    assert st.stance == READY
    # price knifes to +0.5A — READY must survive (profit still above floor)
    assert judge_second(st, 300_000, 100.5, 100) == READY


def test_bleeder_is_cut_after_long_slow_bleed():
    """META 2026-09-22: a loser drifting down on dead volume all day is not
    'a dip with the story intact'. 45 minutes under water with no peak
    challenge → CUT. A minutes-long dip (INTW) never reaches this."""
    st = _state(entry=100.0, stop=97.0, atr=1.0)
    for i in range(1, 61):
        judge_second(st, i * 1000, 100.05, 2000)  # brief peak, baseline vol
    t = 61_000
    # 50 minutes at −0.4A on dead volume, above the 97 stop the whole time
    stance = None
    for i in range(50 * 60):
        stance = judge_second(st, t + i * 1000, 99.60, 100)
    assert stance == CUT
    assert "bleeding" in st.last_reason


def test_short_dip_is_still_wait_not_bleeder():
    st = _state(entry=100.0, stop=97.0, atr=1.0)
    for i in range(1, 61):
        judge_second(st, i * 1000, 100.05, 2000)
    # only 10 minutes under water — the INTW protection holds
    stance = None
    for i in range(10 * 60):
        stance = judge_second(st, 61_000 + i * 1000, 99.60, 100)
    assert stance == WAIT


def test_adopted_peak_seeding_arms_protection():
    """A judge state whose peak was seeded from the backfill (adoption)
    protects the give-back immediately — no fresh peak needed."""
    st = _state()
    st.peak_px, st.peak_t_ms, st.peak_vol60 = 100.90, 0, 2000  # adopted history
    stance = None
    for i in range(1, 120):
        stance = judge_second(st, i * 1000, 100.30, 100)  # +0.3A of a +0.9A peak
    assert stance == BANK
    assert "giving it back" in st.last_reason


def test_judge_entry_door():
    from waveapp.engine.position_judge import judge_entry

    # CUE 2026-09-22: decision 31.88, bid 30.90 / ask 30.99 → mid 30.945,
    # still −2.9% past the decision → still vetoed (A2-10 reads the MID)
    ok, why = judge_entry(True, 31.88, 30.90, 30.99, "CUE")
    assert not ok and "broke" in why
    # ordinary submit: quote at the decision → clean pass
    ok, why = judge_entry(True, 100.00, 99.95, 100.05, "AMD")
    assert ok and why == ""
    # small dip: buyable, but say so (mid 99.30 → −0.7%)
    ok, why = judge_entry(True, 100.00, 99.25, 99.35, "SHOP")
    assert ok and "dip" in why
    # no quote → not this gate's call
    ok, why = judge_entry(True, 100.00, 0.0, 0.0, "X")
    assert ok
    # A2-10: wide-spread gapper (§7's big-expected-move class), STABLE price —
    # bid sits 1.1% below the decision purely on spread while the mid is at
    # 99.70. The old bid-read vetoed this on spread alone; the mid passes it
    # (pure spread is the TradeGate's cost problem, not a broken move).
    ok, why = judge_entry(True, 100.00, 98.90, 100.50, "GAPR")
    assert ok and why == ""


def test_new_peak_decays_old_peak_volume_evidence():
    """A2-9 (audit 2026-09-22): peak_vol60 was a pure ratchet — one giant
    opening burst set the 'alive' bar (vol_alive needs vol60 >= 0.4x peak)
    for the whole day, so every afternoon push read 'dying' one real sign
    early. A NEW peak halves the old evidence before competing with the
    fresh vol60; without a new peak the ratchet still resists tick noise."""
    st = _state()
    judge_second(st, 1_000, 101.0, 100_000)  # opening blow-off peak
    assert st.peak_vol60 == 100_000
    judge_second(st, 2_000, 101.5, 10_000)  # afternoon push: new high, normal vol
    assert st.peak_vol60 == 50_000  # halved, not held at the opening burst
    judge_second(st, 3_000, 102.0, 10_000)
    assert st.peak_vol60 == 25_000  # keeps fading peak by peak
    judge_second(st, 4_000, 102.5, 60_000)  # real volume simply restores the bar
    assert st.peak_vol60 == 60_000
    judge_second(st, 5_000, 102.0, 5)  # no new peak → no decay (noise-proof)
    assert st.peak_vol60 == 60_000


def test_judge_entry_chase_veto():
    """A3-5 (audit 2026-09-22): the GRML/CRCL shape — the decision price is
    minutes old, the breakout already ran, and Wave used to pay the ask at
    the top with the ORIGINAL stop (true risk ~2x the sized budget). The
    door now vetoes the favorable side too, reading the ASK for buys."""
    from waveapp.engine.position_judge import judge_entry

    # buy, ask +1.6% above the decision → vetoed, not chasing the top
    ok, why = judge_entry(True, 100.00, 101.50, 101.60, "GRML")
    assert not ok and ("chas" in why or "ran" in why)
    # buy, ask +0.7% → allowed, but the call site gets a reason to log
    ok, why = judge_entry(True, 100.00, 100.60, 100.70, "CRCL")
    assert ok and why != "" and "chas" in why
    # buy, ask +0.2% → ordinary submit, clean pass
    ok, why = judge_entry(True, 100.00, 100.10, 100.20, "AMD")
    assert ok and why == ""
    # sell mirror: bid 1.6% BELOW the decision → the down-move already ran
    ok, why = judge_entry(False, 100.00, 98.40, 98.50, "SHRT")
    assert not ok and ("chas" in why or "ran" in why)
    # sell mirror small: bid −0.7% → allowed with a reason
    ok, why = judge_entry(False, 100.00, 99.30, 99.40, "SHRT")
    assert ok and why != ""
    # missing ask on a buy → chase side has no quote, prior behavior holds
    ok, why = judge_entry(True, 100.00, 100.05, 0.0, "NOASK")
    assert ok and why == ""


def test_adopted_meta_shape_bleeds_out_before_the_flatten():
    """The 2026-09-22 close bug: META was adopted at 14:46 with a seeded
    peak but no peak TIME or VOLUME — health returned 'alive' forever and
    the bleeder/give-back/guard all went blind while it bled into the 15:50
    flatten (−$403). A seeded, hours-old peak with real volume must let the
    bleeder fire."""
    st = _state(entry=751.09, stop=730.0, atr=8.6)
    # adoption: peak seeded from the 09:38 bar — price, TIME and volume
    st.peak_px, st.peak_t_ms, st.peak_vol60 = 757.27, 9_000_000, 50_000
    t0 = 27_000_000  # ~5h later (the 14:47 adoption moment)
    stance = None
    for i in range(50 * 60):  # bleeding at −1.3A on dead volume
        stance = judge_second(st, t0 + i * 1000, 740.0, 800)
    assert stance == CUT
    assert "bleeding" in st.last_reason


# -- A2-1 (audit 2026-09-22): the knife hole in the candidate/dwell layer ----


def test_knife_through_the_dwell_still_fires_ready():
    """+1.5A peak, 2 death signs arm the READY candidate; 12s into the 20s
    dwell the price KNIFES — one tick gaps from above the trigger to −0.5A.
    The old bug: profit under the 0.15A floor skipped the BIG branch,
    want=WAIT reset the candidate, READY never became the stance, and the
    position rode to the broker hard stop. The stance must be READY on the
    very tick px crosses/gaps through the trigger — while acting still means
    selling near the peak, not after the slide."""
    st = _state()  # entry 100, atr 1.0
    for i in range(1, 31):
        judge_second(st, i * 1000, 100 + i * 0.05, 2000)  # peak 101.50 = +1.5A
    # push dies at 101.40: dying fuel + (later) aged peak = 2 signs → the
    # READY candidate arms while the stance is still RIDE
    t, i = 31_000, 0
    while st.candidate != READY:
        i += 1
        assert i < 400, "READY candidate never armed"
        judge_second(st, t + i * 1000, 101.40, 100)
    armed_t = t + i * 1000
    assert st.stance == RIDE  # dwell running — not flipped yet
    for j in range(1, 12):  # 11 more seconds inside the dwell, still armed
        assert judge_second(st, armed_t + j * 1000, 101.40, 100) == RIDE
    trigger = ready_trigger_px(st)
    knife_px = 99.50  # −0.5A, far through the peak−0.35A trigger
    assert knife_px <= trigger
    stance = judge_second(st, armed_t + 12_000, knife_px, 100)
    # READY the moment the trigger is crossed — profit was still positive at
    # the trigger price (+1.15A), so the kitchen's acting block fires before
    # the win is gone, not never
    assert stance == READY
    assert st.stance == READY


def test_one_red_tick_with_ready_opinion_still_never_flips():
    """The knife shortcut must not weaken one-red-tick protection: the very
    FIRST second whose want is READY — even with px already through the
    trigger — only arms the candidate. Flipping needs the candidate to hold
    a couple of consecutive seconds."""
    st = _state()
    for i in range(1, 31):
        judge_second(st, i * 1000, 100 + i * 0.05, 2000)  # peak 101.50, RIDE
    # one ugly second: aged peak + dying volume (2 signs → want READY) and
    # px already below the peak−0.35A trigger
    assert judge_second(st, 300_000, 100.90, 100) == RIDE  # arms only
    assert st.candidate == READY
    assert judge_second(st, 301_000, 100.90, 100) == RIDE  # 1s armed — holds
    # third consecutive READY second (2s armed, through the trigger) flips
    assert judge_second(st, 302_000, 100.90, 100) == READY


# -- A2-3 (audit 2026-09-22): the closing-guard loser-cut is LEVEL-based ------


def test_close_guard_staircase_bleed_cuts_before_the_flatten():
    """The exact −$718 shape: a loser bleeding down in a staircase (down 3s,
    flat 2s) inside the guard. The old event-based rule needed a strictly
    NEW low on EVERY judged second — each flat second computed want=WAIT,
    reset the candidate, and the 10s dwell never completed; the 15:50
    flatten then dumped it at a worse price. Level-based arming (px within
    the eps band of the lowest print since the guard armed) keeps the CUT
    candidate armed through the pauses, so the dwell completes."""
    st = _state(entry=100.0, stop=98.0, atr=1.0)
    # a minute of history slightly green — no branch fires, ring fills
    for i in range(1, 62):
        judge_second(st, i * 1000, 100.10, 1000, mins_to_flatten=5.0)
    assert st.stance == WAIT
    # staircase: −1¢/s for 3s, flat for 2s, repeating — net −3¢ per 5s
    t0, px, cut_at = 61_000, 100.10, None
    for i in range(60):
        if i % 5 < 3:
            px -= 0.01
        stance = judge_second(st, t0 + (i + 1) * 1000, px, 300, mins_to_flatten=5.0)
        if stance == CUT:
            cut_at = i + 1
            break
    assert cut_at is not None, "staircase bleed never cut — the −$718 wound is open"
    assert cut_at < 60
    assert "into the close" in st.last_reason


def test_close_guard_monotone_knife_still_cuts():
    """The pre-fix shape (strictly lower every second) must remain a CUT —
    the level fix is strictly-better, never weaker."""
    st = _state(entry=100.0, stop=98.0, atr=1.0)
    for i in range(1, 62):
        judge_second(st, i * 1000, 99.80, 1000, mins_to_flatten=5.0)
    stance = None
    for i in range(40):
        stance = judge_second(st, 62_000 + i * 1000, 99.70 - i * 0.01, 300, mins_to_flatten=5.0)
    assert stance == CUT


def test_close_guard_v_dip_that_lifts_off_the_low_stays_wait():
    """A dip that bounces off its low inside the guard: the CUT candidate
    arms while price sits at the low, but a genuine lift above the eps band
    returns want=WAIT before the dwell completes — no false cut on a
    V-shaped dip (the INTW lesson survives the level-based fix)."""
    st = _state(entry=100.0, stop=98.0, atr=1.0)
    for i in range(1, 62):
        judge_second(st, i * 1000, 100.10, 1000, mins_to_flatten=5.0)
    # the V: ~6s down to 99.86 and pausing there (want=CUT arms, < 10s dwell)
    dip = [99.98, 99.94, 99.90, 99.86, 99.86, 99.86]
    # then the bounce: clear of the 99.86 + 0.05A band, and a hover above it
    recovery = [99.95, 100.00] + [100.02] * 30
    t0 = 61_000
    for i, px in enumerate(dip + recovery):
        stance = judge_second(st, t0 + (i + 1) * 1000, px, 300, mins_to_flatten=5.0)
        assert stance == WAIT, f"false cut at second {i + 1} (px {px})"
    assert st.stance == WAIT
    assert st.candidate != CUT  # the armed candidate let go on the lift


# -- A2-4 (audit 2026-09-22): extreme giveback overrides the health veto ------


def test_high_volume_distribution_banks_despite_alive_health():
    """Peak +0.9A, then a heavy-volume dump hands back 80% of it. vol60
    stays well above 0.4x peak_vol60 the whole way down (health 'alive' —
    and the peak is <90s old, so the fresh-peak grace reads 'alive' too),
    yet the extreme-giveback leg must BANK while profit is still above the
    0.15A floor. The old health-vetoed rule rode this as '+0.2A building'
    all the way back."""
    st = _state()  # entry 100, atr 1.0
    for i in range(1, 19):
        judge_second(st, i * 1000, 100 + i * 0.05, 2000)  # peak 100.90 = +0.9A
    for i in range(FLIP_DWELL_SECONDS + 5):  # hold the peak: RIDE latches
        judge_second(st, 19_000 + i * 1000, 100.90, 2000)
    assert st.stance == RIDE
    # the dump: straight to +0.18A (giveback 0.72 >= 0.7 * 0.9) on HEAVY
    # volume — 1500 >= 0.4 * 2000, so health stays "alive" throughout, and
    # the peak is still <90s old the whole way (fresh-peak grace active too)
    t0 = 45_000
    stance = None
    for i in range(FLIP_DWELL_SECONDS + 5):
        stance = judge_second(st, t0 + i * 1000, 100.18, 1500)
    assert stance == BANK
    assert "distribution" in st.last_reason
    # profit never went below the floor — banked while still a win
    assert (100.18 - 100.0) > 0.15


def test_shallow_pullback_on_heavy_volume_still_rides():
    """No new trigger-happiness: a 40% giveback of a +0.9A peak on heavy
    volume is an ordinary pullback — under both the dying-giveback and the
    0.7x extreme thresholds — and must stay RIDE."""
    st = _state()
    for i in range(1, 19):
        judge_second(st, i * 1000, 100 + i * 0.05, 2000)  # peak 100.90
    t0 = 19_000
    stance = None
    for i in range(100):  # well under the 240s stall window
        stance = judge_second(st, t0 + i * 1000, 100.54, 1500)  # giveback 0.36
    assert stance == RIDE
    assert st.candidate not in (BANK, CUT, READY)


def test_aged_peak_without_volume_evidence_is_not_alive_forever():
    """Health fallback: an old peak with peak_vol60 == 0 (no evidence) must
    read 'dying', not permanently 'alive'."""
    st = _state()
    st.peak_px, st.peak_t_ms, st.peak_vol60 = 100.90, 0, 0.0  # evidence-free seed
    stance = None
    for i in range(1, 120):
        stance = judge_second(st, 500_000 + i * 1000, 100.30, 100)
    assert stance == BANK  # give-back protection works despite the empty seed


# ============================================================================
# S2 — THE SHORT JUDGE (2026-09-23): the normalized frame. Every long rule
# must mirror EXACTLY: profit = (entry − px)/A, the peak is the LOWEST print
# (the trough), failed LOWS, the story line is the buy-stop ABOVE holding,
# READY's trigger is trough + 0.35A in real space, the closing guard cuts a
# losing short on a new HIGH into the close. The frame guarantees this by
# construction; these tests pin both the guarantee (exact second-by-second
# mirror equivalence) and the real-space semantics.
# ============================================================================


def _short(entry=100.0, stop=102.0, atr=1.0) -> JudgeState:
    """A short: the strategy stop (buy-to-cover invalidation) sits ABOVE."""
    return JudgeState(entry_px=entry, strategy_stop=stop, atr_px=atr, side=-1, symbol="SHRT")


def test_short_mirror_is_exact_second_by_second():
    """THE FRAME GUARANTEE: for any tape, a short judged on the reflected
    tape (px' = 2*entry − px, stop' = 2*entry − stop) produces the IDENTICAL
    stance stream, candidates, take_now and close_guard as the long — and
    the two real-space READY triggers are each other's reflection."""
    entry = 100.0

    def climb_and_die():
        tape = [(i * 1000, 100 + i * 0.05, 2000) for i in range(1, 61)]
        tape += [(61_000 + i * 1000, 102.6, 100) for i in range(260)]
        return 98.0, tape, None

    def giveback_twlo():
        tape = [(i * 1000, 100 + i * 0.05, 2000) for i in range(1, 15)]
        tape += [(15_000 + i * 1000, max(100.70 - i * 0.01, 100.20), 100) for i in range(1, 200)]
        return 98.0, tape, None

    def guard_staircase():
        tape = [(i * 1000, 100.10, 1000) for i in range(1, 62)]
        px, t = 100.10, 61_000
        for i in range(60):
            if i % 5 < 3:
                px -= 0.01
            tape.append((t + (i + 1) * 1000, px, 300))
        return 98.0, tape, 5.0

    def knife_through_dwell():
        tape = [(i * 1000, 100 + i * 0.05, 2000) for i in range(1, 31)]
        tape += [(31_000 + i * 1000, 101.40, 100) for i in range(1, 300)]
        tape += [(331_000 + i * 1000, 99.50, 100) for i in range(3)]
        return 98.0, tape, None

    def distribution_dump():
        tape = [(i * 1000, 100 + i * 0.05, 2000) for i in range(1, 19)]
        tape += [(19_000 + i * 1000, 100.90, 2000) for i in range(FLIP_DWELL_SECONDS + 5)]
        tape += [(45_000 + i * 1000, 100.18, 1500) for i in range(FLIP_DWELL_SECONDS + 5)]
        return 98.0, tape, None

    for scenario in (
        climb_and_die,
        giveback_twlo,
        guard_staircase,
        knife_through_dwell,
        distribution_dump,
    ):
        stop, tape, guard = scenario()
        lo = JudgeState(entry_px=entry, strategy_stop=stop, atr_px=1.0, symbol="LONG")
        sh = JudgeState(
            entry_px=entry, strategy_stop=2 * entry - stop, atr_px=1.0, symbol="SHRT", side=-1
        )
        for t, px, v in tape:
            s_lo = judge_second(lo, t, px, v, mins_to_flatten=guard)
            s_sh = judge_second(sh, t, 2 * entry - px, v, mins_to_flatten=guard)
            assert s_lo == s_sh, f"{scenario.__name__}: mirror drift at t={t} ({s_lo}!={s_sh})"
            assert lo.candidate == sh.candidate
            assert lo.take_now == sh.take_now
            assert lo.close_guard == sh.close_guard
            trig_lo, trig_sh = ready_trigger_px(lo), ready_trigger_px(sh)
            if trig_lo != float("inf"):
                assert abs((trig_lo - entry) - (entry - trig_sh)) < 1e-9
            else:
                assert trig_sh == float("-inf")
        assert lo.failed_highs == sh.failed_highs
        assert lo.last_reason == sh.last_reason


def test_winning_short_rides_as_price_falls():
    st = _short()
    # steady slide to −3 ATR below entry on healthy volume — a winning short
    stance = None
    for i in range(1, 80):
        stance = judge_second(st, i * 1000, 100 - i * 0.05, 1000)
    assert stance == RIDE


def test_short_big_profit_dying_arms_ready_trigger_above_trough():
    """Mirror of the below-peak trigger: the short's hair trigger is a REAL
    price ABOVE the trough — trough + 0.35A — and the take fires on a
    rebound THROUGH it (px >= trigger), not below it."""
    st = _short()
    for i in range(1, 61):
        judge_second(st, i * 1000, 100 - i * 0.05, 2000)  # slide to 97.00
    trough = 97.00
    # push dies: hovering 0.40 above the trough on collapsed volume — dying
    # fuel + aged trough (2/3 signs), far from the trough zone (no 3/3)
    stance = None
    for i in range(200 + FLIP_DWELL_SECONDS):
        stance = judge_second(st, 61_000 + i * 1000, 97.40, 100)
    assert stance == READY
    assert not st.take_now
    trig = ready_trigger_px(st)
    assert trig == pytest.approx(trough + 0.35 * st.atr_px)
    assert trig > trough  # real space: the trigger sits ABOVE the low


def test_short_all_three_death_signs_take_now_trigger_minus_inf():
    """Dying fuel + failed LOWS + aged trough = READY with an immediate
    trigger: −inf in real space, so ANY print crosses px >= trigger —
    the exact mirror of the long's +inf."""
    st = _short()
    for i in range(1, 61):
        judge_second(st, i * 1000, 100 - i * 0.05, 3000)  # trough 97.00
    trough = 97.00
    t = 61_000
    seq = []
    # two pushes DOWN into the trough zone that fail (failed lows), dying vol
    for _ in range(2):
        for _ in range(6):
            seq.append((t, trough + 0.05, 120))
            t += 1000  # in the zone
        for _ in range(6):
            seq.append((t, trough + 0.40, 120))
            t += 1000  # backs off up
    for _ in range(200 + FLIP_DWELL_SECONDS):
        seq.append((t, trough + 0.30, 100))
        t += 1000
    stance = None
    for tt, px, v in seq:
        stance = judge_second(st, tt, px, v)
    assert stance == READY
    assert st.take_now
    assert st.failed_highs >= 2  # frame "failed highs" == real failed LOWS
    assert ready_trigger_px(st) == float("-inf")


def test_short_intw_mirror_weak_pop_against_short_is_wait():
    """INTW mirrored: a short losing on a weak-volume pop UPWARD, price
    still BELOW the buy-stop — the story is intact, sit through the noise."""
    st = _short(entry=25.43, stop=26.34, atr=0.25)
    for i in range(1, 61):
        judge_second(st, i * 1000, 25.46 - (i % 3) * 0.01, 1000)
    # the pop: price climbs against the short toward 25.86 on weak volume —
    # above entry (losing) but well under the 26.34 story line
    stance = None
    for i in range(40):
        stance = judge_second(st, 61_000 + i * 1000, 25.66 + i * 0.005, 300)
    assert stance == WAIT


def test_short_story_broken_on_real_volume_is_cut():
    st = _short(entry=100.0, stop=102.0, atr=1.0)
    for i in range(1, 61):
        judge_second(st, i * 1000, 100.0, 1000)
    # rips UP through the buy-stop on heavy volume, persists past the dwell
    stance = None
    for i in range(FLIP_DWELL_SECONDS + 5):
        stance = judge_second(st, 61_000 + i * 1000, 102.5, 5000)
    assert stance == CUT


def test_short_small_stale_profit_banks():
    st = _short()
    for i in range(1, 30):
        judge_second(st, i * 1000, 99.20, 2000)  # +0.8A quickly (price fell)
    stance = None
    for i in range(320):  # then nothing happens and the push dies
        stance = judge_second(st, 30_000 + i * 1000, 99.25, 100)
    assert stance == BANK


def test_short_one_green_spike_never_flips_a_stance():
    st = _short()
    stance = None
    for i in range(1, 61):
        stance = judge_second(st, i * 1000, 100 - i * 0.05, 2000)
    assert stance == RIDE
    # a single ugly second AGAINST the short — far too short for the dwell
    assert judge_second(st, 61_000, 103.0, 9000) == RIDE


def test_short_giveback_banks_a_rebounding_trough():
    """The TWLO shape mirrored: the short troughs at −0.7A (+0.7A profit),
    then price REBOUNDS upward on dead volume — giving the win back. The
    give-back rule banks it while it is still clearly a win."""
    st = _short()
    for i in range(1, 15):
        judge_second(st, i * 1000, 100 - i * 0.05, 2000)  # trough 99.30
    stance, px = None, 0.0
    for i in range(1, 200):
        px = min(99.30 + i * 0.01, 99.80)  # price climbing back = giveback
        stance = judge_second(st, 15_000 + i * 1000, px, 100)
        if stance == BANK:
            break
    assert stance == BANK
    assert (100.0 - px) > 0.10  # banked while the short is still clearly green


def test_short_bleeder_drifting_up_45m_is_cut():
    """META mirrored: a short drifting UP on dead volume for 45+ minutes with
    no trough challenge is bleeding out — CUT, not 'a pop with the story
    intact'. A minutes-long pop never reaches this."""
    st = _short(entry=100.0, stop=103.0, atr=1.0)
    for i in range(1, 61):
        judge_second(st, i * 1000, 99.95, 2000)  # brief trough, baseline vol
    stance = None
    for i in range(50 * 60):  # 50 minutes at −0.4A (price UP), dead volume
        stance = judge_second(st, 61_000 + i * 1000, 100.40, 100)
    assert stance == CUT
    assert "bleeding" in st.last_reason


def test_short_brief_pop_is_still_wait_not_bleeder():
    st = _short(entry=100.0, stop=103.0, atr=1.0)
    for i in range(1, 61):
        judge_second(st, i * 1000, 99.95, 2000)
    stance = None
    for i in range(10 * 60):  # only 10 minutes against — protection holds
        stance = judge_second(st, 61_000 + i * 1000, 100.40, 100)
    assert stance == WAIT


def test_short_close_guard_new_high_into_close_cuts():
    """The closing-guard loser rule mirrored: a losing short printing fresh
    HIGHS into the close is CUT — the same tape far from the close stays
    WAIT (the INTW mirror survives inside the guard logic)."""

    def run(guard):
        st = _short(entry=100.0, stop=102.0, atr=1.0)
        for i in range(1, 62):
            judge_second(st, i * 1000, 100.20, 1000, mins_to_flatten=guard)
        stance = None
        for i in range(40):
            stance = judge_second(
                st, 62_000 + i * 1000, 100.30 + i * 0.01, 300, mins_to_flatten=guard
            )
        return stance

    assert run(None) == WAIT
    assert run(200.0) == WAIT
    assert run(5.0) == CUT


def test_short_distribution_override_banks_despite_alive_health():
    """A2-4 mirrored: the short troughs at −0.9A, then a heavy-volume rally
    hands back 80% of the win. Heavy volume AGAINST the short is short
    covering/accumulation — the extreme-giveback leg banks with no volume
    permission needed, while the position is still a win."""
    st = _short()
    for i in range(1, 19):
        judge_second(st, i * 1000, 100 - i * 0.05, 2000)  # trough 99.10
    for i in range(FLIP_DWELL_SECONDS + 5):  # hold the trough: RIDE latches
        judge_second(st, 19_000 + i * 1000, 99.10, 2000)
    assert st.stance == RIDE
    stance = None
    for i in range(FLIP_DWELL_SECONDS + 5):  # rally to 99.82 on HEAVY volume
        stance = judge_second(st, 45_000 + i * 1000, 99.82, 1500)
    assert stance == BANK
    assert "distribution" in st.last_reason
    assert (100.0 - 99.82) > 0.15  # still above the profit floor when banked


def test_short_adopted_trough_seed_arms_protection():
    """Adoption mirrored: the seeded 'peak' for a short is the LOW since
    entry, stored in frame space (2*entry − trough). Give-back protection
    arms from it immediately — no fresh trough needed."""
    st = _short()
    st.peak_px = st._to_frame(99.10)  # adopted trough 99.10 → frame 100.90
    st.peak_t_ms, st.peak_vol60 = 0, 2000
    stance = None
    for i in range(1, 120):
        stance = judge_second(st, i * 1000, 99.70, 100)  # +0.3A of a +0.9A trough
    assert stance == BANK
    assert "giving it back" in st.last_reason


def test_judge_entry_short_drift_veto():
    """The door judge for a SELL (already side-aware — S2 pins it): a live
    mid 1.5% ABOVE the decision means the breakdown the signal wanted to
    short no longer exists — vetoed; a stable quote passes."""
    from waveapp.engine.position_judge import judge_entry

    ok, why = judge_entry(False, 100.00, 101.45, 101.55, "SHRT")
    assert not ok and "broke" in why
    ok, why = judge_entry(False, 100.00, 99.95, 100.05, "SHRT")
    assert ok and why == ""


def test_fsly_shape_big_peak_banks_at_capped_giveback():
    """FSLY 2026-09-23: +5A peak, judge waited for 2/3 death signs while the
    half-the-peak giveback rule wanted 2.5A back. The absolute cap banks a
    big winner once ~0.8A is gone on dying volume — before the signs finish."""
    st = _state()
    for i in range(1, 101):
        judge_second(st, i * 1000, 100 + i * 0.05, 3000)  # peak +5A at 105.0
    t = 101_000
    stance = None
    px = 105.0
    for i in range(1, 120):
        px = max(105.0 - i * 0.02, 103.9)  # sliding back on dead volume
        stance = judge_second(st, t + i * 1000, px, 100)
        if stance in (BANK, READY):
            break
    giveback_at_flip = 105.0 - px
    assert stance in (BANK, READY)
    assert giveback_at_flip <= 1.1  # banked near the 0.8A cap, not at 2.5A


# -- fast-fail (loss-side study 2026-09-25) ----------------------------------


def test_fast_fail_cuts_young_loser_at_73pct_of_stop():
    """A fresh position 73%+ of the way to its stop gets cut — this week
    zero such positions recovered; all rode to the full broker stop."""
    st = _state(entry=100.0, stop=98.5, atr=0.5)  # stop distance 1.5
    st.entry_t_ms = 1000
    for i in range(1, 61):
        judge_second(st, i * 1000, 99.95, 1000)  # baseline minute
    stance = None
    for i in range(40):  # falls to 98.85 = 77% of the stop distance, age ~2m
        stance = judge_second(st, 61_000 + i * 1000, 98.85, 800)
    assert stance == CUT
    assert "fast-fail" in st.last_reason


def test_fast_fail_spares_the_ibit_shape_and_old_losers():
    """−1.02A-of-stop dips survive (the week's one true recovery), and a
    position older than 20 minutes is the bleeder's business, not fast-fail."""
    st = _state(entry=100.0, stop=98.5, atr=0.5)
    st.entry_t_ms = 1000
    for i in range(1, 61):
        judge_second(st, i * 1000, 99.95, 1000)
    for i in range(40):  # 99.0 = 67% of stop distance — below the line
        assert judge_second(st, 61_000 + i * 1000, 99.0, 800) == WAIT
    # old position: same depth at minute 25+ → not fast-fail (bleeder later)
    st2 = _state(entry=100.0, stop=98.5, atr=0.5)
    st2.entry_t_ms = 1000
    t = 25 * 60_000
    for i in range(1, 61):
        judge_second(st2, t + i * 1000, 99.95, 1000)
    r = judge_second(st2, t + 62_000, 98.85, 800)
    assert "fast-fail" not in st2.last_reason and r == WAIT


def test_fast_fail_short_mirror():
    st = _state(entry=100.0, stop=101.5, atr=0.5)
    st.side = -1
    st.entry_t_ms = 1000
    for i in range(1, 61):
        judge_second(st, i * 1000, 100.05, 1000)
    stance = None
    for i in range(40):  # rallies to 101.15 = 77% toward the buy-stop
        stance = judge_second(st, 61_000 + i * 1000, 101.15, 800)
    assert stance == CUT
    assert "fast-fail" in st.last_reason
