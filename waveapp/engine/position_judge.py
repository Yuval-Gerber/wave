"""The Position Judge (2026-09-20).

Every open position is re-judged EVERY SECOND from its own live path into
one of five whole-position stances — no partial sells, no one-shot
first-second verdicts (the 09-17 wound: every position classified in its
first second from poisoned pre-market evidence, never reconsidered, cut at
the bottoms of ordinary opening dips while +$1,000 winners were never
banked):

  RIDE   — big or building profit, push alive: hands off.
  READY  — big profit, push dying: hair-trigger armed; the FIRST real
           weakness sells the whole position at the top, not after the slide.
  WAIT   — losing, but the story is intact (above the strategy level, dips
           on weak volume): sit through the noise. (INTW was cut −$583 here
           and was fine three minutes later.)
  BANK   — small profit, going nowhere: take ALL of it now; capital hunts
           instead of parking.
  CUT    — the story broke (strategy level gone on real volume): out, once.

Stances need EVIDENCE to flip (dwell + hysteresis — a measured ~$191 is
burned per flip-flop), and the broker hard stop always rests underneath
(hard rule 3): the judge only ever acts between entry and that floor.

`judge_mode` config: "off" | "shadow" (compute, display on the card,
journal — act on nothing) | "drive" (stances act). SHADOW IS THE DEFAULT:
drive arms only after the referee replay shows the per-stance dollars
and he flips the config himself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("wave.judge")

# stance constants (strings so journals/cards read naturally)
RIDE = "RIDE"
READY = "READY"
WAIT = "WAIT"
BANK = "BANK"
CUT = "CUT"

# -- tuning (2026-09-21 regime change: "take the money BEFORE the momentum
# stops". Momentum-death = 2 of 3 signals: dying fuel, failed high,
# aged peak; all three = take NOW into remaining strength) -------------------
BIG_PROFIT_ATR = 1.2  # unrealized >= this many position-ATRs = worth taking
SMALL_PROFIT_ATR = 0.5  # between small and big = "some profit"
STALL_SECONDS = 240  # no new high for this long while in small profit = stale
PEAK_AGE_DEATH_S = 180  # a peak unchallenged this long is a top (death sign 3)
FAILED_HIGH_EPS_ATR = 0.15  # a push within this of the peak that fails = a failed high
FAILED_HIGH_COUNT = 2  # this many failed pushes = death sign 2
WEAK_VOL_FRAC = 0.5  # dip volume below this fraction of push volume = weak dip
DYING_VOL_FRAC = 0.4  # push volume decayed below this fraction of its peak
FLIP_DWELL_SECONDS = 15  # a new stance must persist this long to take over
# (20→15 approved 2026-09-23: M2-A calibration on real second tapes,
# +$243 over the era at +4% churn, worst single position −$8)
READY_TRIGGER_ATR = 0.35  # READY sells on a pullback this deep off the peak
STORY_BREAK_VOL = 1.3  # break volume >= this x recent average = "real volume"

# -- closing guard (2026-09-21: "the market is closing soon so it is
# better to take any profit that is possible than to lose" — the forced 15:50
# flatten sold 7 positions for −$718 at whatever price the close gave).
# Inside the last CLOSE_GUARD_MINUTES before the flatten the judge stops
# being patient: every death-sign threshold drops by one, any profit banks
# on the first sign of weakness, losers stop sitting through the noise, and
# stances flip on a shorter dwell. Before the window NOTHING changes — the
# morning rides stay exactly as validated. -----------------------------------
CLOSE_GUARD_MINUTES = 30.0  # guard arms this many minutes before the flatten
CLOSE_GUARD_PROFIT_ATR = 0.3  # near the close, any profit >= this is worth taking
CLOSE_GUARD_TRIGGER_ATR = 0.20  # READY's pullback trigger tightens off the peak
CLOSE_GUARD_DWELL_SECONDS = 10  # flips need less patience when time is short
# A2-3 (audit 2026-09-22): the "new low into the close" cut is LEVEL-based —
# a loser sitting within this band of its lowest print since the guard armed
# keeps the CUT candidate armed through pauses AT the low, so the dwell can
# complete on a staircase bleed (down 3s, flat 2s never fired the old
# strictly-lower-every-second rule; the 15:50 flatten then dumped it worse).
# 0.05A: a third of the 0.15A "near the peak" band — big enough that penny
# jitter at the low never reads as a recovery, small enough that any real
# bounce (the READY/failed-high scales all start at 0.15A+) escapes to WAIT.
CLOSE_GUARD_LOW_EPS_ATR = 0.05
CLOSE_GUARD_LOW_EPS_MIN = 0.02  # floor: one-tick wobble on low-ATR names

# -- 2026-09-22 (the TWLO round-trip: +$200 peak was only +0.71A on a
# high-ATR name, UNDER the 1.2A hair-trigger — no protection existed until
# the slow 240s stall, and it rode to −$80; the META bleed: a loser drifting
# down all day never triggers anything until the stop or the flatten) --------
MID_PROTECT_PEAK_ATR = 0.6  # a peak this high arms give-back protection
MID_PROTECT_GIVEBACK = 0.5  # banking fires when this fraction of the peak is gone
MID_PROTECT_MIN_GIVEBACK_ATR = 0.35  # ... and at least this many ATRs are gone
GIVEBACK_CAP_ATR = 0.8  # a big peak never waits for more than this giveback
# (FSLY 2026-09-23: fraction-of-peak scaled to $470 at +5A; capped absolute)
# A2-4 (audit 2026-09-22): giving back THIS fraction of the peak overrides the
# health veto entirely. Every protective stance used to require
# health=="dying", but health reads "alive" whenever vol60 >= 0.4x its peak —
# so a HIGH-VOLUME breakdown (heavy selling = distribution, not fuel) rode a
# TWLO-shape peak all the way back down as "building". Same for a blow-off
# reversal off a <90s peak: the fresh-peak grace made "dying" unreachable by
# construction for ~3 minutes. A deep giveback needs no volume permission.
EXTREME_GIVEBACK_FRAC = 0.7
PROFIT_FLOOR_ATR = 0.15  # "still a win" line — protection acts above it only
READY_KNIFE_MIN_ARM_S = 2  # a knife shortcuts the dwell only after the READY
# candidate held this many consecutive seconds — one ugly second never flips
BLEED_MINUTES = 45  # a loser with no peak challenge this long is bleeding out
# FAST-FAIL (loss-side study 2026-09-25:
# the week's asymmetry killer was losers riding the full broker stop —
# avgL $207 vs avgW $112. Cutting a YOUNG position at 73% of its stop
# distance (the study's -1.1A stop-scale winner) earned +$847/wk with
# ZERO recovering winners sacrificed; every cut position had ridden to
# its full stop in baseline. No volume condition — the health gate's 90s
# fresh-peak grace made "dying" unreachable young; the data backed the
# unconditional form.)
FF_AGE_MIN = 20  # fast-fail only while the position is this young
FF_STOP_FRAC = 0.733  # cut at this fraction of the distance to the stop
BLEED_LOSS_ATR = 0.3  # ... once it is at least this far under water
ENTRY_DRIFT_VETO_PCT = 1.0  # live price this % past the decision = the move broke
# A3-5 (audit 2026-09-22): the mirror veto for the FAVORABLE side. Decision
# features can be minutes old at the open — a breakout signal fires, the price
# runs +1.5-2% before the order goes out, and Wave pays the ask at the top with
# the ORIGINAL stop: true risk ~2x the sized budget (the stop distance grew by
# the whole chase), and it is exactly the "bought after the up trend already
# happened" entry banned (GRML/CRCL shapes). Kept as its OWN constant
# (symmetric with ENTRY_DRIFT_VETO_PCT today) so the two sides tune apart.
ENTRY_CHASE_VETO_PCT = 1.0  # live already ran this % past the decision = not chasing
# A2-9 (audit 2026-09-22): peak volume evidence must FADE as new peaks print.
# The old pure ratchet (max(vol60, peak_vol60)) meant one giant opening burst
# set the "alive" bar (vol_alive needs vol60 >= 0.4x peak_vol60) for the whole
# day — every afternoon push on an adopted gapper read "dying" and protection
# needed one fewer real death sign. Each NEW peak now halves the old ratchet
# before competing with the fresh vol60: a single thin new-peak print can at
# most halve the evidence (noise-resistant), while a run of new highs on
# normal volume converges the bar to today's actual tape within a few peaks.
PEAK_VOL_DECAY = 0.5


@dataclass
class JudgeState:
    """Per-position rolling evidence + the current stance.

    THE NORMALIZED FRAME (S2, 2026-09-23 — replaces the A2-5 long-only
    refusal): all judging math lives in PROFIT SPACE. For a long the frame
    IS real price space (the transform is the identity, so long behavior is
    bit-identical). For a short (side=-1) every price is reflected around
    the entry — px' = 2*entry − px — so "up" always means "in profit":
    the real trough since entry becomes the frame peak, a real rebound
    against the short becomes a frame giveback, the buy-stop ABOVE entry
    becomes a frame stop below. The ring, peak_px/peak_t_ms/peak_vol60,
    guard_low and every ATR distance therefore live in FRAME space for
    shorts; entry_px and strategy_stop stay in REAL space (the transform
    is applied where they are compared), and the only frame value that
    ever leaves this module — ready_trigger_px — is reflected back to a
    real price before return. The mirror can never drift from the long
    logic because there is no second copy of the logic to drift.
    """

    entry_px: float
    strategy_stop: float  # the strategy's own invalidation — the story line (REAL px)
    atr_px: float  # one position-ATR in dollars (updated by the caller)
    symbol: str = ""  # for the logs — flips without a name are unreadable
    side: int = 1  # +1 long, -1 short (judged via the normalized frame)
    stance: str = WAIT
    stance_since_ms: int = 0
    candidate: str | None = None
    candidate_since_ms: int = 0
    peak_px: float = 0.0  # FRAME space: the best-for-us print (short: 2*entry − trough)
    peak_t_ms: int = 0
    peak_vol60: float = 0.0
    failed_highs: int = 0  # pushes near the frame peak that could not print through
    _near_peak: bool = False
    take_now: bool = False  # all three death signs — close into remaining strength
    close_guard: bool = False  # inside the closing window — impatient mode
    entry_t_ms: int = 0  # fill moment (fast-fail age anchor; 0 = unknown)
    guard_low: float | None = None  # lowest FRAME print since the guard armed (A2-3):
    # for a short that is the HIGHEST real print — "sitting on its lows" in
    # profit space. A dedicated field, not the ring min — the ring is 120
    # SAMPLES, which can span far longer than 120s on a sparse tape and
    # forgets lows it scrolls off.
    last_reason: str = ""
    # M0 telemetry (2026-09-23): how long the winning candidate was armed at
    # the moment it took the stance (candidate_since→flip). Written at flip,
    # read by the kitchen's judge_transitions journal — never by stance logic.
    last_flip_dwell_ms: int = 0

    ring: list = field(default_factory=list)  # (t_ms, FRAME px, vol60) ~120s

    def __post_init__(self) -> None:
        if self.side != 1:
            # S2: the short mirror is live — the frame transform below is the
            # whole of it. Announce once, at birth (the old A2-5 guard's
            # scream is retired with the guard itself).
            logger.info("short judge armed for %s", self.symbol or "?")

    def _to_frame(self, px: float) -> float:
        """Real price → profit space (identity for longs; reflection around
        the entry for shorts, so rising frame price = growing profit)."""
        return px if self.side == 1 else 2.0 * self.entry_px - px

    def _from_frame(self, px: float) -> float:
        """Profit space → real price (the reflection is its own inverse)."""
        return px if self.side == 1 else 2.0 * self.entry_px - px

    def update_atr(self, atr_px: float) -> None:
        if atr_px > 0:
            self.atr_px = atr_px


def _push_health(st: JudgeState, t_ms: int, vol60: float) -> str:
    """alive | dying — is the move still being fed?"""
    fresh_peak = t_ms - st.peak_t_ms <= 90_000
    if st.peak_vol60 <= 0:
        # no volume evidence for the peak. Fresh = benefit of the doubt;
        # an AGED peak with no evidence is NOT "alive" — the 2026-09-22
        # close: adopted peaks were seeded without volume, this returned
        # "alive" forever, every death-based rule went blind, and the
        # 15:50 flatten dumped META/SPAX/WDC for −$612.
        return "alive" if fresh_peak else "dying"
    vol_alive = vol60 >= DYING_VOL_FRAC * st.peak_vol60
    if fresh_peak or vol_alive:
        return "alive"
    return "dying"


def _story_intact(st: JudgeState, px: float, vol60: float) -> bool:
    """The strategy's level holds, or it broke only on weak volume.

    `px` arrives in FRAME space (judge_second transformed it); the stored
    strategy_stop is a REAL price, so it is transformed here — for a short
    the buy-stop ABOVE entry reflects to a frame level below, and "the
    story holds" reads identically: frame price above the frame stop."""
    if px > st._to_frame(st.strategy_stop):
        return True
    ring = st.ring
    if len(ring) < 30:
        return False  # broke with no history to excuse it
    avg_vol = sum(v for _, _, v in ring[-60:]) / max(len(ring[-60:]), 1)
    return vol60 < STORY_BREAK_VOL * avg_vol


def judge_second(
    st: JudgeState,
    t_ms: int,
    px: float,
    vol60: float,
    mins_to_flatten: float | None = None,
) -> str:
    """One second of evidence in → the current stance out.

    `mins_to_flatten`: minutes until the session-boundary flatten fires
    (None = far away / unknown → no closing guard, prior behavior exactly).
    """
    # THE NORMALIZED FRAME (S2, 2026-09-23 — replaces the A2-5 refusal):
    # one transform, then every line below runs VERBATIM long math in
    # profit space. For a long this is the identity (bit-identical). For a
    # short, px' = 2*entry − px reflects the tape around the entry: a
    # falling real price is a rising frame price (profit), the real trough
    # is the frame peak, a rebound against the short is a frame giveback,
    # and the buy-stop above entry is a frame story line below
    # (_story_intact applies the same reflection to strategy_stop).
    # Volume and time need no transform — fuel is fuel on either side.
    # ready_trigger_px reflects its answer BACK to a real price on the way
    # out; nothing else escapes the frame.
    px = st._to_frame(px)
    st.close_guard = mins_to_flatten is not None and mins_to_flatten <= CLOSE_GUARD_MINUTES
    # A2-3: the guard's loser-cut arms by LEVEL, not by event. guard_low is
    # the lowest print since the guard armed, seeded from the pre-guard
    # 120-sample low (bare-ring adoption seeds from the live print) so "at
    # the recent lows" counts from the guard's first second.
    if st.close_guard:
        if st.guard_low is None:
            st.guard_low = min((p for _, p, _ in st.ring), default=px)
        if px < st.guard_low:
            st.guard_low = px
    else:
        st.guard_low = None  # guard off — re-arm cleanly if it comes back
    st.ring.append((t_ms, px, vol60))
    if len(st.ring) > 120:
        del st.ring[0]
    if px > st.peak_px:
        # A2-9: decayed ratchet — old giant volume evidence fades as new
        # peaks print (see PEAK_VOL_DECAY), so a huge opening burst cannot
        # brand every afternoon push "dying" for the rest of the day
        st.peak_px, st.peak_t_ms = px, t_ms
        st.peak_vol60 = max(vol60, st.peak_vol60 * PEAK_VOL_DECAY)
        st.failed_highs = 0  # a real new high resets the failure count
        st._near_peak = False
    else:
        near = px >= st.peak_px - FAILED_HIGH_EPS_ATR * max(st.atr_px, 0.01)
        if near and not st._near_peak:
            st._near_peak = True  # a push into the peak zone begins
        elif not near and st._near_peak:
            st._near_peak = False
            st.failed_highs += 1  # it backed off without printing through

    atr = max(st.atr_px, 0.01)
    profit_atr = (px - st.entry_px) / atr
    peak_profit_atr = (st.peak_px - st.entry_px) / atr
    health = _push_health(st, t_ms, vol60)
    # give-back protection (TWLO 2026-09-22): a position that PEAKED high
    # enough and is handing it back on dying volume banks what is left —
    # while it is still a win. The peak, not the current tick, decides the
    # regime, so a fast fall can no longer disarm the very trigger it fires.
    giveback = peak_profit_atr - profit_atr
    dying_giveback = (
        peak_profit_atr >= MID_PROTECT_PEAK_ATR
        and profit_atr >= PROFIT_FLOOR_ATR
        and giveback
        >= min(
            # FSLY 2026-09-23 (+$900 peak closed +$720): at a +5A peak the
            # half-the-peak rule demanded a 2.5A ($470) giveback before it
            # would fire — the fraction scales with the peak, the pain is
            # absolute. GIVEBACK_CAP_ATR bounds it: a big winner handing
            # back this many ATRs on dying volume banks, period.
            max(MID_PROTECT_GIVEBACK * peak_profit_atr, MID_PROTECT_MIN_GIVEBACK_ATR),
            GIVEBACK_CAP_ATR,
        )
        and health == "dying"
    )
    # A2-4: EXTREME giveback fires regardless of health. Heavy volume on the
    # way DOWN is distribution, not fuel — and a fresh (<90s) blow-off peak
    # gets no "dying" verdict by construction. When most of the peak is
    # already gone the health veto must not hold the door open. NO health
    # term here, so both the vol_alive read and the fresh-peak grace are
    # bypassed. The dwell below still applies (that is the flip-flop guard).
    extreme_giveback = (
        peak_profit_atr >= MID_PROTECT_PEAK_ATR
        and profit_atr >= PROFIT_FLOOR_ATR
        and giveback >= EXTREME_GIVEBACK_FRAC * peak_profit_atr
    )
    giveback_bank = dying_giveback or extreme_giveback
    # distinct reason so journals separate the distribution shape from the
    # ordinary dying-volume giveback
    if dying_giveback:
        giveback_why = f"peaked +{peak_profit_atr:.1f}A, giving it back — banking"
    else:
        giveback_why = f"peaked +{peak_profit_atr:.1f}A, dumping on volume — distribution, banking"

    # A2-1 (2026-09-22): a knife DURING the dwell used to drop profit below
    # the floor on a single tick, skip this branch, and the resulting
    # want=WAIT reset the armed READY candidate — the position then rode
    # from near-peak to the broker hard stop. While a READY candidate (or
    # the READY stance itself) is armed, falling below the floor is exactly
    # what READY exists to catch: the PEAK keeps owning the regime and the
    # death signs keep deciding.
    big_peak_regime = peak_profit_atr >= BIG_PROFIT_ATR and (
        profit_atr >= PROFIT_FLOOR_ATR or st.candidate == READY or st.stance == READY
    )
    if big_peak_regime:
        # MOMENTUM DEATH (2026-09-21): three signs — dying fuel, failed
        # highs, aged peak. Two of three arms READY (hair trigger below the
        # peak); ALL THREE returns TAKE-NOW semantics: the stance is READY
        # and the trigger price is the CURRENT price (sell into what's left
        # of the strength, not after the slide).
        signs = int(health == "dying")
        signs += int(st.failed_highs >= FAILED_HIGH_COUNT)
        signs += int(t_ms - st.peak_t_ms >= PEAK_AGE_DEATH_S * 1000)
        # closing guard: every threshold drops by one — near the close a
        # single death sign arms the hair trigger, two mean take NOW
        take_at, ready_at = (2, 1) if st.close_guard else (3, 2)
        if signs >= take_at:
            st.take_now = True
            want = READY
            why = f"+{profit_atr:.1f}A momentum DEAD ({signs}/3) — take now"
        elif signs >= ready_at:
            st.take_now = False
            want = READY
            why = f"+{profit_atr:.1f}A momentum dying ({signs}/3)"
        elif giveback_bank:
            # FSLY 2026-09-23: the BIG branch consulted only death signs —
            # a +5A peak sliding back on dead volume RODE until 2/3 signs
            # finished. Give-back protection (with the absolute cap) now
            # applies here too: a big winner bleeding its peak banks.
            st.take_now = False
            want, why = BANK, giveback_why
        else:
            st.take_now = False
            want = RIDE
            why = f"+{profit_atr:.1f}A, push {health}"
    elif profit_atr >= SMALL_PROFIT_ATR:
        stalled = t_ms - st.peak_t_ms >= STALL_SECONDS * 1000
        if giveback_bank:
            want, why = BANK, giveback_why
        elif stalled and health == "dying":
            want, why = BANK, f"+{profit_atr:.1f}A stale {int((t_ms - st.peak_t_ms) / 1000)}s"
        elif st.close_guard and (
            health == "dying"
            or st.failed_highs >= 1
            or t_ms - st.peak_t_ms >= PEAK_AGE_DEATH_S * 1000
        ):
            want, why = BANK, f"+{profit_atr:.1f}A, closing soon — taking it"
        else:
            want, why = RIDE, f"+{profit_atr:.1f}A building"
    elif giveback_bank:
        # TWLO zone: below "small profit" but the PEAK was real and most of
        # it is gone — take the remaining win before it turns into a loss
        want, why = BANK, giveback_why
    elif st.close_guard and profit_atr >= CLOSE_GUARD_PROFIT_ATR:
        # small profit near the close: one sign of weakness banks it —
        # "take any profit that is possible" beats donating it to the flatten
        if health == "dying" or st.failed_highs >= 1:
            want, why = BANK, f"+{profit_atr:.1f}A, closing soon — taking it"
        else:
            want, why = RIDE, f"+{profit_atr:.1f}A building"
    else:  # flat-to-losing
        stop_f = st._to_frame(st.strategy_stop)
        stop_dist = st.entry_px - stop_f
        if (
            st.entry_t_ms > 0
            and t_ms - st.entry_t_ms <= FF_AGE_MIN * 60_000
            and stop_dist > 0
            and (st.entry_px - px) >= FF_STOP_FRAC * stop_dist
        ):
            # FAST-FAIL: young and already 73%+ of the way to the stop —
            # this week zero such positions recovered; all rode to the stop
            want, why = (
                CUT,
                (
                    f"fast-fail: {100 * (st.entry_px - px) / stop_dist:.0f}% of stop "
                    f"in {int((t_ms - st.entry_t_ms) / 60000)}m"
                ),
            )
        elif not _story_intact(st, px, vol60):
            want, why = CUT, f"{profit_atr:+.1f}A, story broke on volume"
        elif (
            profit_atr <= -BLEED_LOSS_ATR
            and t_ms - st.peak_t_ms >= BLEED_MINUTES * 60_000
            and health == "dying"
        ):
            # the META bleed (2026-09-22): a loser that has not challenged
            # its peak for BLEED_MINUTES on dead volume is not "a dip with
            # the story intact" — it is bleeding out. A real dip (INTW)
            # resolves in minutes and never gets here.
            want, why = CUT, f"{profit_atr:+.1f}A, bleeding {int((t_ms - st.peak_t_ms) / 60000)}m"
        elif (
            st.close_guard
            and profit_atr < 0
            and st.guard_low is not None
            and len(st.ring) > 60
            and px <= st.guard_low + max(CLOSE_GUARD_LOW_EPS_ATR * atr, CLOSE_GUARD_LOW_EPS_MIN)
        ):
            # closing guard: a loser sitting ON its lows stops waiting — it
            # was going to be sold by the flatten anyway, at a worse price.
            # A2-3: level-based, so a pause AT the low keeps the CUT armed
            # and the dwell completes on a staircase bleed (the −$718 shape:
            # down 3s, flat 2s — the old strictly-new-low rule reset the
            # candidate on every flat second and only a pure knife ever
            # fired). A genuine lift off the low (px above the eps band)
            # falls through to WAIT exactly as before.
            want, why = CUT, f"{profit_atr:+.1f}A, new low into the close"
        else:
            want, why = WAIT, f"{profit_atr:+.1f}A, story intact"

    # stickiness: a new stance must hold its opinion for the dwell
    # (shorter inside the closing guard — less patience when time is short)
    dwell_s = CLOSE_GUARD_DWELL_SECONDS if st.close_guard else FLIP_DWELL_SECONDS
    if want != st.stance:
        if st.candidate != want:
            st.candidate, st.candidate_since_ms = want, t_ms
        else:
            armed_ms = t_ms - st.candidate_since_ms
            # A2-1: the dwell exists to prevent flip-flop, not to let a
            # knife through — a READY opinion whose price is ALREADY
            # at/through the hair trigger flips as soon as the candidate
            # has held a couple of consecutive seconds (a single ugly
            # second still never flips: a fresh candidate is 0s armed).
            knife = (
                want == READY
                and armed_ms >= READY_KNIFE_MIN_ARM_S * 1000
                # frame-space on both sides: px is already transformed here,
                # so the long comparison direction is correct for either side
                and px <= _ready_trigger_frame(st)
            )
            if armed_ms >= dwell_s * 1000 or knife:
                st.stance, st.stance_since_ms = want, t_ms
                st.last_flip_dwell_ms = armed_ms  # M0 telemetry, observation only
                st.candidate = None
                logger.info("JUDGE %s flip → %s (%s)", st.symbol or "?", want, why)
    else:
        st.candidate = None
    st.last_reason = why
    return st.stance


def _ready_trigger_frame(st: JudgeState) -> float:
    """READY's hair trigger in FRAME space: below the frame peak by the
    trigger distance, or +inf (any print crosses) when all three death
    signs fired. Internal — judge_second's knife compares frame-to-frame."""
    if getattr(st, "take_now", False):
        return float("inf")
    trigger_atr = CLOSE_GUARD_TRIGGER_ATR if st.close_guard else READY_TRIGGER_ATR
    return st.peak_px - trigger_atr * max(st.atr_px, 0.01)


def ready_trigger_px(st: JudgeState) -> float:
    """READY's hair trigger as a REAL price: the whole position closes at
    this pullback off the frame peak — or immediately when all three death
    signs fired (take NOW into remaining strength).

    Longs: peak − trigger_atr×A, take_now → +inf; the caller's crossing
    test is px <= trigger. Shorts: the frame value reflects back to
    trough + trigger_atr×A (a rebound off the low), and take_now's +inf
    reflects to −inf — the caller's crossing test must therefore be
    SIDE-AWARE: px >= trigger for a short (any real print >= −inf crosses,
    exactly mirroring the long's px <= +inf)."""
    return st._from_frame(_ready_trigger_frame(st))


def judge_entry(
    is_buy: bool, decision_px: float, bid: float, ask: float, symbol: str = ""
) -> tuple[bool, str]:
    """The judge at the DOOR (2026-09-22 — the CUE lesson: the signal
    said 31.88 while the live bid was 30.90; the spike had already broken a
    full dollar before Wave acted, and it bought a collapsing top for −$323).

    Returns (buy, reason). The live quote is compared to the signal's
    decision price: a small unfavorable drift is a dip worth buying, past
    ENTRY_DRIFT_VETO_PCT the move the signal wanted to buy no longer exists.

    A3-5 (audit 2026-09-22): the FAVORABLE side is vetoed too. A buy whose
    ask already ran past the decision is paying for a move that already
    happened, with the ORIGINAL stop underneath — the chase inflates real
    risk toward 2x the sized budget. The chase side reads the ask for buys
    (that is the price a marketable buy pays) and the bid for sells; both
    vetoes are checked before either allow-with-reason so a degenerate
    wide-spread quote (dip on the bid AND chase on the ask) refuses rather
    than passes.

    A2-10 (audit 2026-09-22): the collapse veto reads the MID, not the far
    side. Reading the bid for buys meant a stable price with a >1%
    half-spread — exactly the wide-spread gapper class §7 admits on a big
    expected move — was vetoed on spread alone even when the ask sat at the
    decision. Pure spread is a cost problem (the TradeGate's job), not a
    broken move; the mid is where the price actually is. The chase veto
    keeps reading the PAYING side — that is a real price about to be paid.
    """
    if bid > 0 and ask > 0:
        live = (bid + ask) / 2.0
    else:
        live = bid if bid > 0 else ask  # one-sided quote — the available side
    if live <= 0 or decision_px <= 0:
        return True, ""  # no quote — the no-quote deferral upstream owns this
    drift = (decision_px - live) if is_buy else (live - decision_px)
    pct = drift / decision_px * 100.0
    if pct > ENTRY_DRIFT_VETO_PCT:
        return False, (
            f"{symbol}: live {live:.2f} is {pct:.1f}% past the decision "
            f"{decision_px:.2f} — the move broke, not buying the collapse"
        )
    # chase side: for a buy you pay the ASK, so that is the price that decides
    # whether the run-up already happened (mirror: a sell hits the BID below)
    chase_live = ask if is_buy else bid
    chase_pct = 0.0
    if chase_live > 0:
        chase = (chase_live - decision_px) if is_buy else (decision_px - chase_live)
        chase_pct = chase / decision_px * 100.0
    if chase_pct > ENTRY_CHASE_VETO_PCT:
        return False, (
            f"{symbol}: live {chase_live:.2f} already ran {chase_pct:.1f}% past "
            f"the decision {decision_px:.2f} — not chasing the top"
        )
    if pct > ENTRY_DRIFT_VETO_PCT / 2:
        return True, f"{symbol}: {pct:.1f}% past the decision — dip judged buyable"
    if chase_pct > ENTRY_CHASE_VETO_PCT / 2:
        return True, f"{symbol}: chasing {chase_pct:.1f}% past the decision — allowed"
    return True, ""
