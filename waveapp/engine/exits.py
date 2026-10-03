"""The adaptive exit system (SPEC.md §8.2) — Phase 6.

Seven layers, all active simultaneously; the binding constraint wins. All stop
math is ATR-based (fast intraday ATR(14) on 1-minute bars, parameterized) so
it adapts to each symbol's volatility. Parameters live in one dataclass per
regime (§8.3) and change only through the training pipeline later.

This module is PURE DECISION LOGIC: `ExitEngine.on_bar()` consumes closed
1-minute bars and returns decisions (amend stop / scale out / exit now /
nothing). The PositionActor executes decisions against the broker (step 6.2),
including the server-side stop amendment with throttling.

Layers:
1. Hard stop        — server-side from entry (owned by the bracket; the floor)
2. Breakeven ratchet— profit ≥ k_be×ATR → stop to entry ± fees buffer
3. Chandelier trail — k_trail×ATR behind the best price since entry, ratchet-only
4. Momentum exit    — volume death or VWAP recross against us → exit now
5. Scale-out        — at k_t1×ATR take `scale_fraction` off, stop → breakeven
6. Time stop        — flat-ish after t_max minutes → exit
7. Session boundary — flatten before CLOSED (weekend/holiday)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from waveapp.broker.base import OrderSide
from waveapp.data.hub import Bar
from waveapp.engine.session import Regime

logger = logging.getLogger("wave.trade.exits")


# -- parameters (§8.3: one dataclass per regime, versioned via settings later) --


@dataclass(frozen=True)
class ExitParams:
    k_stop: float = 1.5  # hard stop distance, ×ATR (used at entry sizing)
    k_be: float = 1.0  # breakeven trigger, ×ATR
    k_trail: float = 2.0  # chandelier distance, ×ATR
    k_t1: float = 1.2  # first scale-out target, ×ATR
    scale_fraction: float = 0.5  # fraction sold at the first target
    t_max_minutes: int = 90  # time stop
    flat_band_atr: float = 0.35  # |unrealized| below this ×ATR counts as flat-ish
    # parameter-disabled (§8.2 style) — research seam 2026-08-26: a
    # floor-armed position (ran ≥ k_be×ATR) proved momentum; True exempts it
    # from the time stop so the trail alone decides. Adopt only via §11.
    t_max_exempt_floored: bool = False
    # research seam 2026-08-31 (ACGL/PYPL all-day bleeders): one-sided time
    # stop — after t_max, cut ANYTHING not in meaningful profit (< flat_band
    # above entry), not just the |flat| band. A trade underwater at 90min
    # currently holds forever in the crack between the flat band and the
    # loiter depth. False = OFF; adopt only via §11.
    t_max_cut_losers: bool = False
    # research seam 2026-08-31 (HDB penny-trail): minimum trail distance as
    # % of entry price — on micro-ATR symbols the 3.5×ATR chandelier hugs
    # within pennies and ratchets to entry for a $0 exit. 0.0 = OFF.
    trail_floor_pct: float = 0.0
    # research seams 2026-08-28 (the dead-zone campaign) — both 0 = OFF:
    # 8. VWAP-ratchet floor (Zarattini SPY/Maróy): after N minutes the stop
    #    can never rest on the wrong side of session VWAP
    vwap_floor_minutes: float = 0.0
    # 9. loiter cut (Sweeney MAE × time): sustained time below a depth —
    #    a winner dips and bounces; a corpse dips and stays
    loiter_depth_pct: float = 0.0
    loiter_minutes: int = 60
    volume_death_fraction: float = 0.25  # bar volume vs entry baseline
    fees_buffer: float = 0.01  # $ added past entry on the breakeven stop
    amend_min_move_atr: float = 0.15  # don't amend for less than this ×ATR
    amend_throttle_seconds: float = 20.0
    atr_period: int = 14
    flatten_before_close_minutes: int = 10  # layer 7 lead time
    # staged profit lock (2026-08-20, the ask): once PEAK profit reaches
    # lock_trigger_atr×ATR, the stop ratchets to entry + lock_fraction×peak —
    # securing a growing fraction of the best profit seen. 0 = disabled;
    # values come ONLY from the §11 pipeline (research_profit_lock sweep).
    lock_trigger_atr: float = 0.0
    lock_fraction: float = 0.0
    # time-sensitive recovery exit (2026-08-20: four positions sat at
    # −2% for half an hour, bounced to +1%, Wave held, they fell back): a
    # trade that stayed underwater long enough has shown it is not the day's
    # runner — bank the first real bounce instead of round-tripping again.
    # 0 = disabled; values come ONLY from the §11 pipeline.
    recovery_depth_atr: float = 0.0  # "hurt" once profit < −this ×ATR
    recovery_minutes: int = 20  # hurt must persist this long to arm
    recovery_exit_atr: float = 0.0  # bank when profit recovers to ≥ this ×ATR
    # goal ladder (2026-08-21: "when it crosses a goal, tighten and
    # set a new goal and a new floor — always look up"): peak profit crossing
    # rung N×step lifts the stop to rung (N − lag)×step; the NEXT rung is the
    # card's live goal. 0 = disabled; values only via the §11 pipeline.
    ladder_step_atr: float = 0.0  # rung spacing, ×ATR
    ladder_floor_lag: float = 1.0  # floor trails this many rungs behind
    # PEAK-GIVEBACK CAP (2026-08-24 red-day directive — the FIFTH
    # tightening shape, untested by the previous four sweeps): once peak
    # profit ≥ giveback_arm_atr×ATR, exit if profit retraces to ≤
    # (1 − giveback_fraction) of the peak. 0.0 = layer disabled (default).
    giveback_fraction: float = 0.0
    giveback_arm_atr: float = 0.75
    # ARMED-ONLY SQUEEZE MODE (master blueprint 6.2, 2026-09-01 — the EIX
    # +$1,114-manual vs +$684-auto shape; the always-on giveback failed 4/4
    # windows, so this arms ONLY on a detected vertical): squeeze_bars > 0
    # enables the detector — that many consecutive favorable bars with
    # expanding ranges AND bar volume ≥ squeeze_vol_mult × the rolling
    # average arms the mode. While armed: the chandelier's ATR input FREEZES
    # at its pre-spike value, the trail tightens to the prior bar's low
    # (mirror for shorts), one third is sold into strength at
    # squeeze_scale_r × initial risk, and a volume-climax bar that fails to
    # make a new high exits the rest. 0 = OFF; values only via §11.
    squeeze_bars: int = 0
    squeeze_vol_mult: float = 3.0
    squeeze_scale_r: float = 3.0
    squeeze_climax_mult: float = 5.0
    # LADDERED SCALE-OUTS (master blueprint 6.3 — the only profitable
    # variant in the 9,794-gap Concretum study): sell ladder_scaleout_frac
    # of the ORIGINAL size at each rung × initial risk, trail the remainder.
    # Empty tuple = OFF; meant for the high-RVOL/gap class, values via §11.
    ladder_scaleout_rungs: tuple[float, ...] = ()
    ladder_scaleout_frac: float = 0.25
    # ---- KITCHEN 2.0 seams (2026-09-02 KITCHEN HUNT, blueprint II.10–II.14;
    # the −$292-day autopsy: +$2,108 held at the peaks, $2,400 given back).
    # All default OFF; values only via the §11 pipeline.
    # Trail clamp (agents 1/2/5 — ATR-multiples go inert on 8%+ NATR names;
    # Zarattini's own stop is a daily-ATR fraction ≈ 1–2% of price):
    # chandelier distance = min(k_trail×ATR, trail_cap_pct% of entry price).
    trail_cap_pct: float = 0.0
    # Floor-armed R-based giveback cap (ALL FIVE agents; differs from the
    # 4/4-rejected always-on ATR cap: dead until peak ≥ arm×R, then the
    # allowed giveback fraction TIGHTENS as the peak grows):
    giveback_arm_r: float = 0.0  # 0 = OFF; arming threshold in R units
    giveback_frac_r: float = 0.5  # base allowed giveback of peak at arming
    giveback_tighten_per_r: float = 0.0  # g shrinks this much per R beyond arm
    giveback_floor_frac: float = 0.2  # g never tightens below this
    # First-red-candle exit (Cameron/BBT doctrine, agents 2/4 — EXTREME class):
    # once peak ≥ arm×R, the first bar closing against the position exits all.
    first_red_arm_r: float = 0.0
    # Extension-bar sell INTO strength (agents 2/3/4 — bank the climax at the
    # extension, not after the retrace): favorable bar with range ≥ k×ATR
    # while in profit → sell this fraction of the remainder, stop → BE.
    ext_bar_atr: float = 0.0
    ext_bar_fraction: float = 0.5
    # No-new-high timeout (agent 1 / Howard: the time stack beat every price
    # stop): in profit but no new favorable extreme for M minutes → exit.
    no_new_high_minutes: int = 0
    # Immediacy clock (agent 4 — three independent Schwager wizards): a trade
    # that never reached immediacy_r×R after immediacy_minutes tightens its
    # stop to −immediacy_stop_r×R (ratchet-only; cuts −1R losers to −0.5R).
    immediacy_minutes: int = 0
    immediacy_r: float = 0.25
    immediacy_stop_r: float = 0.5
    # ---- CYCLE-1 Build 2 seams (2026-09-03 factory; the owner watched
    # losers fall "and Wave did nothing"). Both default OFF; §11 owns values.
    # Dead-money scratch (II.28/II.23 — keyed on STALENESS, not depth, which
    # is what separates it from the six rejected giveback shapes): a trade
    # at least dead_scratch_minutes old, with no new favorable extreme for
    # dead_scratch_stale_minutes, underwater AND on the wrong side of VWAP,
    # is a corpse — scratch it before the full stop.
    dead_scratch_minutes: int = 0
    dead_scratch_stale_minutes: int = 10
    # Statistical breakeven (II.28 — the trigger sits above the level losers
    # rarely pass, fitted from the journal in R units; peak-armed): once PEAK
    # profit ≥ k_be_r × initial risk, the stop rides at breakeven forever.
    k_be_r: float = 0.0
    # THE CONE (Peak Engine Stage B, II.35/II.36 — independently derived by
    # two fields: the optimal trailing leash tightens like √(time-left),
    # optimal z ≈ 1.1228; a fixed-width trail is provably too loose near the
    # close). Trail distance becomes min(chandelier, cone_z·σ̂·√τ) where
    # σ̂ ≈ ATR/1.6 per-minute vol and τ = minutes to the flatten boundary.
    # 0 = OFF; values only via the owner's oven→10-day→year chain + §11.
    cone_z: float = 0.0
    # THE DISORDER FILTER (Peak Engine Stage B, II.36 — Shiryaev/Ekström-
    # Lindberg: one-line Bayesian posterior that the drift already flipped;
    # papers: hard reversals detected in ~2-3 min). Π updates every bar from
    # the standardized return; exit when Π ≥ disorder_threshold. hazard =
    # per-bar prior of a flip; drift in σ-per-bar units. 0 hazard = OFF.
    disorder_hazard: float = 0.0
    disorder_drift: float = 0.10
    disorder_threshold: float = 0.90
    # LATE-LOW CUT (Cycle 2 B1, 2026-09-03 — from Wave's own never-recover
    # frontier: deep dips whose LOW prints in the first 10 minutes recover
    # 70%; a NEW adverse extreme printed ≥30 minutes in, while ≥0.5%
    # underwater, recovers 17% and averages −$176). Fires ONLY underwater on
    # a FRESH adverse extreme — no peak reference; the giveback family stays
    # closed. 0 = OFF; values only via §11.
    late_low_minutes: int = 0
    late_low_depth_pct: float = 0.5
    # Research switch (2026-09-02 phase-2 ablation): the VWAP-recross momentum
    # exit had no off-switch — it is the one layer the winning "no-manage"
    # control arm still contained, so the ablation must isolate it. True =
    # current behavior (default, live unchanged).
    vwap_recross_exit: bool = True
    # VWAP-as-trailing-stop (II.21 — the source VWAP system's ACTUAL exit,
    # twice replicated net of costs): exit on ANY 1-min close through session
    # VWAP against the position, profit or loss, hold to EoD otherwise.
    # False = OFF (default, live unchanged); per-strategy seam for VWAP-class
    # positions via the (regime, strategy) params dispatch.
    vwap_stop_trail: bool = False


# Regime-keyed parameters — THE BARE KITCHEN, adopted 2026-09-03 (the
# go after the KITCHEN HUNT campaign; blueprint II.10–II.22, kitchen2/3 +
# knife3 + final_sweep): hard stop + session flatten + ride winners to the
# close. The layer-audit ablation proved EVERY managed layer net-destroys
# value on our deck (trail −$36k in H2 alone; k_be/recovery negative in all
# four windows; recross −$88k in H2) — bare beat the old champion 4/4, and
# with FPB + the ORB 10:30 deadline the composition beat BARE 4/4 with a
# confirmed plateau. All layer CODE stays (§8.2) — parameter-disabled, and
# the §8.3 pipeline owns these values. Paper adoption; rule 10 stands.
_BARE_KITCHEN = dict(
    k_trail=999.0,
    k_be=999.0,
    k_t1=999.0,
    t_max_minutes=9999,
    volume_death_fraction=0.0,
    recovery_depth_atr=0.0,
    loiter_depth_pct=0.0,
    vwap_recross_exit=False,
)

# The pre-2026-09-03 champion, kept for reference/rollback (and tests that
# exercise the managed layers explicitly):
_TRAIL_ONLY = dict(
    k_trail=3.5,
    t_max_minutes=90,
    k_t1=999.0,
    # BREAKEVEN FLOOR adopted 2026-08-26 (directive after the BBWI
    # give-back; scripts/research_trail.py, full-year floor/ceiling sweep):
    # once profit ≥ 2×ATR the stop can never sit below breakeven again — a
    # real winner cannot become a loser. Beat the champion in BOTH windows
    # (IS +$307 → 26,571; OOS +$1,020 → 59,474, positive all 4 OOS months),
    # win rate 58→63% OOS, max DD better in both. Earlier arms (0.75/1.25)
    # were split/neutral — 2.0 is the plateau edge that stays positive.
    k_be=2.0,
    volume_death_fraction=0.0,
    # RECOVERY EXIT adopted 2026-08-20 (the hypothesis, §11 sweep
    # scripts/research_recovery.py): trades ≥20min below −0.75×ATR bank the
    # first bounce ≥ +0.3×ATR. Beat trail-only IS (+$11.7k vs +$10.1k) AND
    # OOS (+$20,840 vs +$19,385, PF 1.29 vs 1.26, same maxDD −$5,484);
    # both exit thresholds tested (0 / 0.3×ATR) landed within $9 → plateau.
    recovery_depth_atr=0.75,
    recovery_minutes=20,
    recovery_exit_atr=0.3,
    # LOITER CUT adopted 2026-08-28 (the dead-zone directive after CRM
    # bled −2% for 4.7h untouched; scripts/research_layers.py, 12-config
    # full-year sweep): a position below −1% for 30 CONSECUTIVE minutes is a
    # corpse — sell it. Won BOTH windows (IS +$6,224 → 30,649; OOS +$3,339 →
    # 63,618, PF 2.02), max DD better in both, and the neighboring settings
    # (0.5%/60m, momentum-only variants) all positive → a stable plateau.
    # Winners dip and bounce (61% dip below −1%); the CLOCK is what separates
    # them from corpses — touch-based cuts failed 3 sweeps, this won.
    loiter_depth_pct=1.0,
    loiter_minutes=30,
)
# ROLLED BACK 2026-09-04 ~02:00 (his words: the bare kitchen +
# daily flatten realizes winners ONLY at the close and rides every fade to a
# full stop-loss — unacceptable to the owner regardless of window sums. The
# old champion returns; the next kitchen gets built under his supervision.)
DEFAULT_PARAMS: dict[Regime, ExitParams] = {
    Regime.PRE: ExitParams(**_TRAIL_ONLY),
    Regime.OPEN_DRIVE: ExitParams(**_TRAIL_ONLY),
    Regime.MIDDAY: ExitParams(**_TRAIL_ONLY),
    Regime.POWER_HOUR: ExitParams(**_TRAIL_ONLY),
    Regime.POST: ExitParams(**_TRAIL_ONLY),
    Regime.OVERNIGHT: ExitParams(**_TRAIL_ONLY),
    Regime.CLOSED: ExitParams(**_TRAIL_ONLY),
}


def params_for(regime: Regime) -> ExitParams:
    return DEFAULT_PARAMS.get(regime, ExitParams())


# Master Key mode (promoted to drive 2026-09-09): the classic
# per-minute layers stand down — the v14 per-second brain drives every exit
# through the actor. What this per-minute engine still owns in MK mode: the
# session-boundary flatten backstop and the resting-stop bookkeeping. The
# server-side hard stop itself NEVER stands down (hard rule 3).
MK_DELEGATED = ExitParams(**_BARE_KITCHEN)


def mk_delegated_params_for(regime: Regime) -> ExitParams:
    return MK_DELEGATED


# -- ATR ---------------------------------------------------------------------


def compute_atr(bars: list[Bar], period: int = 14) -> float | None:
    """Wilder's ATR over closed 1-minute bars; None until enough history."""
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for prev, cur in zip(bars[-(period + 1) : -1], bars[-period:], strict=True):
        true_ranges.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    return sum(true_ranges) / period


# -- decisions ---------------------------------------------------------------


class ExitAction(Enum):
    NONE = "none"
    AMEND_STOP = "amend_stop"
    SCALE_OUT = "scale_out"
    EXIT_NOW = "exit_now"


@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    reason: str = ""
    new_stop: float | None = None  # AMEND_STOP
    qty: float | None = None  # SCALE_OUT


NO_DECISION = ExitDecision(ExitAction.NONE)


class ExitEngine:
    """Per-position exit brain. Feed it closed bars; it answers with at most
    one decision per bar (the binding constraint wins; EXIT_NOW dominates)."""

    def __init__(
        self,
        side: OrderSide,
        entry_price: float,
        qty: float,
        atr_at_entry: float,
        params: ExitParams,
        entry_time: datetime,
        entry_bar_volume: float | None = None,
        initial_stop: float | None = None,
    ) -> None:
        self.side = side
        self.entry_price = entry_price
        self.qty = qty
        self.remaining_qty = qty
        self.atr = atr_at_entry
        self.params = params
        self.entry_time = entry_time
        self.entry_bar_volume = entry_bar_volume
        self._loiter_since = None  # first bar of the current below-depth run
        direction = 1.0 if side is OrderSide.BUY else -1.0
        self.direction = direction
        self.current_stop = (
            initial_stop
            if initial_stop is not None
            else entry_price - direction * params.k_stop * atr_at_entry
        )
        self.best_price = entry_price  # highest for long, lowest for short
        self.breakeven_done = False
        self.scaled_out = False
        self.last_amend_time: datetime | None = None
        self.pending_stop: float | None = None
        # audit A1-6: a SCALE_OUT decision's ledger commit (remaining_qty
        # decrement + its one-shot latch) is DEFERRED here as (qty, latch)
        # until the actor confirms the sale submit succeeded. Committing
        # before the submit meant a failed submit left remaining_qty lying,
        # the resting stop undersized, and the layer latched off forever —
        # a later close then sold only the reduced qty and CLOSED with
        # orphan stopless shares at the broker.
        self._pending_scale: tuple[float, str] | None = None
        self.hurt_minutes = 0.0  # time spent underwater (recovery exit)
        # initial risk per share (R) — the ladder/squeeze rung unit
        self.initial_risk = max(abs(entry_price - self.current_stop), 0.01)
        # 6.2 squeeze-mode state (inert while squeeze_bars == 0)
        self.squeeze_armed = False
        self.squeeze_scaled = False
        self._squeeze_atr: float | None = None  # frozen pre-spike ATR
        self._squeeze_run = 0  # consecutive favorable expanding bars
        self._vol_ema: float | None = None  # rolling bar-volume average
        self._prev_bar: Bar | None = None
        # 6.3 ladder scale-out state
        self._ladder_rungs_done = 0
        # Kitchen 2.0 state
        self._last_best_time: datetime = entry_time  # no-new-high clock
        self._ext_bar_done = False
        self._worst_price: float | None = None  # late-low cut (C2-B1)
        self._disorder_pi = 0.0  # Peak Engine: P(drift already flipped)
        self._prev_close: float | None = None

    # -- helpers -------------------------------------------------------------

    def _profit(self, price: float) -> float:
        return (price - self.entry_price) * self.direction

    def _better(self, a: float, b: float) -> float:
        """The more protective stop for our side (never loosens)."""
        return max(a, b) if self.side is OrderSide.BUY else min(a, b)

    # -- the brain -----------------------------------------------------------

    def on_bar(
        self,
        bar: Bar,
        atr: float | None = None,
        minutes_to_close_boundary: float | None = None,
        session_vwap: float | None = None,
    ) -> ExitDecision:
        if atr is not None:
            self.atr = atr
        params = self.params
        price = bar.close
        previous_best = self.best_price
        self.best_price = (
            max(self.best_price, bar.high)
            if self.side is OrderSide.BUY
            else min(self.best_price, bar.low)
        )
        if self.best_price != previous_best:
            self._last_best_time = bar.start  # no-new-high clock resets

        # 6.2 squeeze detector — state updates run EVERY bar (inert when off)
        prev_bar = self._prev_bar
        self._prev_bar = bar
        vol_baseline = self._vol_ema
        if params.squeeze_bars > 0:
            vol = float(bar.volume or 0.0)
            self._vol_ema = vol if vol_baseline is None else 0.9 * vol_baseline + 0.1 * vol
            favorable = (bar.close - bar.open) * self.direction > 0
            expanding = prev_bar is None or (bar.high - bar.low) >= (prev_bar.high - prev_bar.low)
            self._squeeze_run = self._squeeze_run + 1 if (favorable and expanding) else 0
            if (
                not self.squeeze_armed
                and self._squeeze_run >= params.squeeze_bars
                and vol_baseline
                and vol >= params.squeeze_vol_mult * vol_baseline
            ):
                self.squeeze_armed = True
                self._squeeze_atr = self.atr  # freeze the chandelier's input
                logger.info("squeeze mode ARMED (run %d, vol %.0f)", self._squeeze_run, vol)

        # 7. session boundary — flatten before CLOSED, overrides everything
        if (
            minutes_to_close_boundary is not None
            and minutes_to_close_boundary <= params.flatten_before_close_minutes
        ):
            return ExitDecision(ExitAction.EXIT_NOW, reason="session boundary (flatten)")

        # PE-B. disorder filter (Peak Engine Stage B): Bayesian posterior
        # that the favorable drift has flipped — the papers' optimal exit.
        if params.disorder_hazard > 0 and self.atr > 0:
            if self._prev_close is not None:
                z = (price - self._prev_close) * self.direction / (self.atr / 1.6)
                z = max(min(z, 6.0), -6.0)
                pi = self._disorder_pi
                pi = pi + params.disorder_hazard * (1.0 - pi)
                like = math.exp(-2.0 * params.disorder_drift * z)
                pi = pi * like / (pi * like + (1.0 - pi))
                self._disorder_pi = min(max(pi, 0.0), 1.0)
            self._prev_close = price
            if self._disorder_pi >= params.disorder_threshold:
                return ExitDecision(
                    ExitAction.EXIT_NOW,
                    reason=f"disorder filter (P(flip)={self._disorder_pi:.2f})",
                )
        elif params.disorder_hazard > 0:
            self._prev_close = price

        # 4b. VWAP trailing stop (II.21, param-disabled): any close through
        # session VWAP against the position exits — profit or loss
        if (
            params.vwap_stop_trail
            and session_vwap is not None
            and (price - session_vwap) * self.direction < 0
        ):
            return ExitDecision(ExitAction.EXIT_NOW, reason="VWAP trail (close-through)")

        # 4. momentum exit — the push died: bank it before it fades
        if self._profit(price) > 0:
            if (
                self.entry_bar_volume
                and bar.volume < params.volume_death_fraction * self.entry_bar_volume
            ):
                return ExitDecision(ExitAction.EXIT_NOW, reason="volume death")
            if (
                params.vwap_recross_exit
                and session_vwap is not None
                and self._profit(session_vwap) > 0  # we entered on the right side
                and (price - session_vwap) * self.direction < 0  # ...and lost it
            ):
                return ExitDecision(ExitAction.EXIT_NOW, reason="VWAP recross")

        # recovery exit (2026-08-20): long-underwater trades bank the
        # first real bounce — they've shown they're not today's runner
        if params.recovery_depth_atr > 0:
            if self._profit(price) < -params.recovery_depth_atr * self.atr:
                self.hurt_minutes += 1.0
            if (
                self.hurt_minutes >= params.recovery_minutes
                and self._profit(price) >= params.recovery_exit_atr * self.atr
            ):
                return ExitDecision(
                    ExitAction.EXIT_NOW, reason="recovered after drawdown (bank the bounce)"
                )

        # 6b. peak-giveback cap (parameter-disabled unless the §11 pipeline
        # arms it): never surrender more than giveback_fraction of the best
        # profit reached
        if params.giveback_fraction > 0:
            peak_profit_now = self._profit(self.best_price)
            if peak_profit_now >= params.giveback_arm_atr * self.atr and self._profit(
                price
            ) <= peak_profit_now * (1.0 - params.giveback_fraction):
                return ExitDecision(ExitAction.EXIT_NOW, reason="giveback cap (protect the peak)")

        # -- Kitchen 2.0 layers (all parameter-disabled by default; §11) ------
        peak_profit_now = self._profit(self.best_price)
        peak_r = peak_profit_now / self.initial_risk if self.initial_risk > 0 else 0.0

        # K1. floor-armed R-based giveback cap: dead until the move is real,
        # then the allowed giveback tightens as the peak grows
        if params.giveback_arm_r > 0 and peak_r >= params.giveback_arm_r:
            g_eff = max(
                params.giveback_floor_frac,
                params.giveback_frac_r
                - params.giveback_tighten_per_r * (peak_r - params.giveback_arm_r),
            )
            if self._profit(price) <= peak_profit_now * (1.0 - g_eff):
                return ExitDecision(
                    ExitAction.EXIT_NOW,
                    reason=f"giveback floor ({g_eff * 100:.0f}% of +{peak_r:.1f}R peak)",
                )

        # K2. first-red-candle exit: armed by profit, exits on the first bar
        # closing against the position while still green
        if (
            params.first_red_arm_r > 0
            and peak_r >= params.first_red_arm_r
            and (bar.close - bar.open) * self.direction < 0
            and self._profit(price) > 0
        ):
            return ExitDecision(ExitAction.EXIT_NOW, reason="first red bar (bank the spike)")

        # C2-B1. late-low cut: a FRESH adverse extreme printing late in the
        # trade while meaningfully underwater is the 17%-recovery population —
        # cut it before the full stop (early violence is left alone: winners
        # dip hard in the first minutes and recover 70% of the time)
        if params.late_low_minutes > 0:
            worst = self._worst_price if self._worst_price is not None else price
            adverse_now = bar.low if self.side is OrderSide.BUY else bar.high
            new_worst = (adverse_now - worst) * self.direction < 0
            if new_worst:
                self._worst_price = adverse_now
            if (
                new_worst
                and bar.start - self.entry_time >= timedelta(minutes=params.late_low_minutes)
                and self._profit(price) <= -params.late_low_depth_pct / 100.0 * self.entry_price
            ):
                return ExitDecision(
                    ExitAction.EXIT_NOW, reason="late low (fresh adverse extreme, cut)"
                )

        # B2. dead-money scratch (staleness-keyed; underwater + wrong side of
        # VWAP + stale = a corpse holding a seat another trade could use)
        if (
            params.dead_scratch_minutes > 0
            and self._profit(price) < 0
            and bar.start - self.entry_time >= timedelta(minutes=params.dead_scratch_minutes)
            and bar.start - self._last_best_time
            >= timedelta(minutes=params.dead_scratch_stale_minutes)
            and session_vwap is not None
            and (price - session_vwap) * self.direction < 0
        ):
            return ExitDecision(ExitAction.EXIT_NOW, reason="dead money scratch (stale loser)")

        # K3. no-new-high timeout: green but stalled — the push is over
        if (
            params.no_new_high_minutes > 0
            and self._profit(price) > 0
            and bar.start - self._last_best_time >= timedelta(minutes=params.no_new_high_minutes)
        ):
            return ExitDecision(
                ExitAction.EXIT_NOW,
                reason=f"no new high in {params.no_new_high_minutes}m (bank it)",
            )

        # 6.2 armed squeeze mode: prior-bar trail + sell into strength +
        # climax exit. Arms only on a detected vertical (the always-on
        # giveback failed 4/4 windows — this is the surgical version).
        if self.squeeze_armed and prev_bar is not None:
            prior_extreme = prev_bar.low if self.side is OrderSide.BUY else prev_bar.high
            if (bar.close - prior_extreme) * self.direction < 0:
                return ExitDecision(ExitAction.EXIT_NOW, reason="squeeze break (prior-bar stop)")
            no_new_extreme = (
                bar.high <= prev_bar.high if self.side is OrderSide.BUY else bar.low >= prev_bar.low
            )
            if (
                vol_baseline
                and float(bar.volume or 0.0) >= params.squeeze_climax_mult * vol_baseline
                and no_new_extreme
            ):
                return ExitDecision(ExitAction.EXIT_NOW, reason="volume climax (no new extreme)")
            if (
                not self.squeeze_scaled
                and self._pending_scale is None
                and self._profit(price) >= params.squeeze_scale_r * self.initial_risk
            ):
                scale_qty = max(1, int(self.remaining_qty / 3))
                if scale_qty < self.remaining_qty:
                    # A1-6: ledger commit deferred to confirm_scale_out
                    be_stop = self.entry_price + self.direction * params.fees_buffer
                    self.pending_stop = self._better(self.current_stop, be_stop)
                    self._pending_scale = (float(scale_qty), "squeeze")
                    return ExitDecision(
                        ExitAction.SCALE_OUT,
                        reason=f"squeeze scale (+{params.squeeze_scale_r:g}R into strength)",
                        qty=scale_qty,
                        new_stop=self.pending_stop,
                    )
                return ExitDecision(ExitAction.EXIT_NOW, reason="squeeze target (full size)")

        # 6. time stop — capital must not sit dead
        age = bar.start - self.entry_time
        if age >= timedelta(minutes=params.t_max_minutes) and not (
            params.t_max_exempt_floored and self.breakeven_done
        ):
            profit_now = self._profit(price)
            if params.t_max_cut_losers and profit_now < params.flat_band_atr * self.atr:
                return ExitDecision(ExitAction.EXIT_NOW, reason="time stop (not working)")
            if abs(profit_now) < params.flat_band_atr * self.atr:
                return ExitDecision(ExitAction.EXIT_NOW, reason="time stop (flat-ish)")

        # 9. loiter cut (research, default off): sustained time below a depth.
        # Touch-based cuts amputate winners (61% of ours dip below −1% first);
        # SUSTAINED time underwater is the corpse signature.
        if params.loiter_depth_pct > 0:
            loiter_level = self.entry_price * (1 - self.direction * params.loiter_depth_pct / 100.0)
            if self.direction * (price - loiter_level) < 0:
                if self._loiter_since is None:
                    self._loiter_since = bar.start
                elif bar.start - self._loiter_since >= timedelta(minutes=params.loiter_minutes):
                    return ExitDecision(
                        ExitAction.EXIT_NOW,
                        reason=(
                            f"loiter cut ({params.loiter_depth_pct:g}% for "
                            f"{params.loiter_minutes}m)"
                        ),
                    )
            else:
                self._loiter_since = None

        # K4. extension-bar sell INTO strength: a favorable bar stretching
        # ≥ ext_bar_atr×ATR while in profit banks part of the climax at the
        # extension (limit-into-strength when live; once per position)
        if (
            params.ext_bar_atr > 0
            and not self._ext_bar_done
            and self._pending_scale is None
            and self._profit(price) > 0
            and (bar.close - bar.open) * self.direction > 0
            and (bar.high - bar.low) >= params.ext_bar_atr * self.atr
        ):
            scale_qty = max(1, int(self.remaining_qty * params.ext_bar_fraction))
            if scale_qty < self.remaining_qty:
                # A1-6: ledger commit deferred to confirm_scale_out
                be_stop = self.entry_price + self.direction * params.fees_buffer
                self.pending_stop = self._better(self.current_stop, be_stop)
                self._pending_scale = (float(scale_qty), "ext_bar")
                return ExitDecision(
                    ExitAction.SCALE_OUT,
                    reason=f"extension bar ({params.ext_bar_atr:g}×ATR — sell into strength)",
                    qty=scale_qty,
                    new_stop=self.pending_stop,
                )
            return ExitDecision(ExitAction.EXIT_NOW, reason="extension bar (full size)")

        # 5b. laddered scale-outs (6.3 — Concretum: the only profitable gap
        # variant sold 25% at 2R/4R/8R and trailed the rest). When rungs are
        # set they REPLACE the single k_t1 scale-out for this position.
        if params.ladder_scaleout_rungs:
            rungs = params.ladder_scaleout_rungs
            if (
                self._ladder_rungs_done < len(rungs)
                and self._pending_scale is None
                and self._profit(price) >= rungs[self._ladder_rungs_done] * self.initial_risk
            ):
                rung_r = rungs[self._ladder_rungs_done]
                scale_qty = max(1, int(self.qty * params.ladder_scaleout_frac))
                if scale_qty < self.remaining_qty:
                    # A1-6: ledger commit (incl. the rung advance) deferred
                    # to confirm_scale_out
                    be_stop = self.entry_price + self.direction * params.fees_buffer
                    self.pending_stop = self._better(self.current_stop, be_stop)
                    self._pending_scale = (float(scale_qty), "ladder")
                    return ExitDecision(
                        ExitAction.SCALE_OUT,
                        reason=f"ladder rung (+{rung_r:g}R)",
                        qty=scale_qty,
                        new_stop=self.pending_stop,
                    )
                return ExitDecision(ExitAction.EXIT_NOW, reason="final ladder rung (full size)")
        # 5. scale-out at the first target
        elif (
            not self.scaled_out
            and self._pending_scale is None
            and self._profit(price) >= params.k_t1 * self.atr
        ):
            scale_qty = max(1, int(self.qty * params.scale_fraction))
            if scale_qty < self.remaining_qty:
                # A1-6: ledger commit deferred to confirm_scale_out
                be_stop = self.entry_price + self.direction * params.fees_buffer
                self.pending_stop = self._better(self.current_stop, be_stop)
                self._pending_scale = (float(scale_qty), "scaled_out")
                return ExitDecision(
                    ExitAction.SCALE_OUT,
                    reason=f"first target (+{params.k_t1}×ATR)",
                    qty=scale_qty,
                    new_stop=self.pending_stop,
                )
            return ExitDecision(ExitAction.EXIT_NOW, reason="target reached (full size)")

        # 2 & 3. breakeven ratchet + chandelier trail → one stop candidate
        candidate = self.current_stop
        if not self.breakeven_done and self._profit(price) >= params.k_be * self.atr:
            self.breakeven_done = True
            candidate = self._better(
                candidate, self.entry_price + self.direction * params.fees_buffer
            )
        # B2. statistical breakeven: PEAK-armed, R-based trigger (fitted above
        # the loser-MFE percentile — once cleared, a winner never round-trips).
        # No one-shot latch (tester BUG-1, 2026-09-03): the arming amend can
        # be voided by the too-close-to-price clamp below, so the candidate is
        # RE-PROPOSED every bar until the resting stop actually holds it —
        # the ratchet makes re-proposal idempotent.
        if params.k_be_r > 0 and self._profit(self.best_price) >= params.k_be_r * self.initial_risk:
            self.breakeven_done = True
            candidate = self._better(
                candidate, self.entry_price + self.direction * params.fees_buffer
            )
        # squeeze mode freezes the chandelier's ATR at its pre-spike value —
        # an inflating ATR is exactly how verticals give the whole leg back
        chandelier_atr = (
            self._squeeze_atr if (self.squeeze_armed and self._squeeze_atr) else self.atr
        )
        trail_atr = max(chandelier_atr, params.trail_floor_pct / 100.0 * self.entry_price)
        trail_dist = params.k_trail * trail_atr
        # K5. trail clamp: on extreme-NATR names k×ATR is 5–10% of price and
        # can never bind intraday — cap the distance in percent-of-price terms
        # (never binds on normal names, so zero regression risk there)
        if params.trail_cap_pct > 0:
            trail_dist = min(trail_dist, params.trail_cap_pct / 100.0 * self.entry_price)
        # PE-A. the cone (Peak Engine): leash ≤ cone_z · σ̂ · √(minutes left)
        if params.cone_z > 0 and minutes_to_close_boundary is not None:
            tau = max(float(minutes_to_close_boundary), 1.0)
            cone_dist = params.cone_z * (self.atr / 1.6) * math.sqrt(tau)
            trail_dist = min(trail_dist, cone_dist)
        trail = self.best_price - self.direction * trail_dist
        if self.breakeven_done or self._profit(trail) > -params.k_stop * self.atr * 0.99:
            candidate = self._better(candidate, trail)
        if self.squeeze_armed and prev_bar is not None:
            # armed trail = the prior bar's extreme (ratchet-only via _better)
            prior_extreme = prev_bar.low if self.side is OrderSide.BUY else prev_bar.high
            candidate = self._better(candidate, prior_extreme)

        # 8. VWAP-ratchet floor (research, default off): after N minutes the
        # stop rests no worse than session VWAP — a loiterer gets overtaken
        # by VWAP and exits itself; healthy trades never notice
        if (
            params.vwap_floor_minutes > 0
            and session_vwap is not None
            and bar.start - self.entry_time >= timedelta(minutes=params.vwap_floor_minutes)
        ):
            candidate = self._better(candidate, session_vwap)

        # staged profit lock: secure a fraction of the PEAK profit once the
        # move is real; tightens automatically as the peak extends
        peak_profit = self._profit(self.best_price)
        if params.lock_trigger_atr > 0 and peak_profit >= params.lock_trigger_atr * self.atr:
            lock_stop = self.entry_price + self.direction * params.lock_fraction * peak_profit
            candidate = self._better(candidate, lock_stop)

        # K6. immediacy clock: a trade that never worked (peak < i×R after
        # T minutes) tightens its stop to −immediacy_stop_r×R — cuts the
        # ARKG-class full-stop bleeders to half-losers (ratchet-only)
        if (
            params.immediacy_minutes > 0
            and age >= timedelta(minutes=params.immediacy_minutes)
            and peak_profit < params.immediacy_r * self.initial_risk
        ):
            immediacy_stop = self.entry_price - self.direction * (
                params.immediacy_stop_r * self.initial_risk
            )
            candidate = self._better(candidate, immediacy_stop)

        # goal ladder: every rung crossed lifts the floor; the next rung
        # becomes the new goal (exposed via next_goal for the card)
        if params.ladder_step_atr > 0 and self.atr > 0:
            step = params.ladder_step_atr * self.atr
            rung = int(peak_profit / step)
            if rung >= 1:
                ladder_stop = self.entry_price + self.direction * (
                    (rung - params.ladder_floor_lag) * step
                )
                candidate = self._better(candidate, ladder_stop)

        # a protective stop must rest BELOW the market (mirror for shorts) —
        # PYPL 2026-08-28: an amendment landed AT/ABOVE the live price and
        # executed instantly, ejecting a healthy position at a forced loss.
        # Clamp: never propose a stop within 1¢ of the current price; the
        # ratchet simply waits for the next tick instead.
        if self.direction * (price - candidate) < 0.01:
            candidate = self.current_stop

        return self._maybe_amend(candidate, bar.start)

    def _maybe_amend(self, candidate: float, now: datetime) -> ExitDecision:
        """Throttled, ratchet-only stop amendment (§8.2 layer 3)."""
        # HDB 2026-08-31: the broker order is rounded to a whole cent while the
        # engine kept the raw value — the card promised "$2 locked" (22.6418)
        # and the resting order banked $0 (22.64). Round HERE, direction-aware
        # (never claim more protection than the order provides): floor for
        # longs, ceil for shorts. Engine, log, card and broker now agree.
        if self.direction > 0:
            candidate = math.floor(candidate * 100.0) / 100.0
        else:
            candidate = math.ceil(candidate * 100.0) / 100.0
        if self._better(candidate, self.current_stop) == self.current_stop:
            return NO_DECISION  # not more protective — never loosen
        # while squeeze mode is armed the amend geometry uses the FROZEN ATR
        # too — an inflating live ATR must not slow the very ratchets the
        # mode exists to deliver (6.2)
        amend_atr = self._squeeze_atr if (self.squeeze_armed and self._squeeze_atr) else self.atr
        min_move = self.params.amend_min_move_atr * amend_atr
        if abs(candidate - self.current_stop) < min_move:
            return NO_DECISION
        if (
            self.last_amend_time is not None
            and (now - self.last_amend_time).total_seconds() < self.params.amend_throttle_seconds
        ):
            return NO_DECISION
        self.last_amend_time = now
        previous, self.current_stop = self.current_stop, candidate
        logger.info("stop ratchet: %.2f → %.2f", previous, candidate)
        # NOTE (audit 2026-09-16): this commit is optimistic — the ACTOR
        # rolls current_stop/last_amend_time back to the broker-confirmed
        # level if the replace fails, so a failed amend is re-proposed
        # next tick instead of diverging forever.
        return ExitDecision(
            ExitAction.AMEND_STOP,
            reason="ratchet (breakeven/trail)",
            new_stop=candidate,
        )

    @property
    def next_goal(self) -> float | None:
        """The ladder's CURRENT goal — the next uncrossed rung (card UI).
        None when the ladder is disabled."""
        if self.params.ladder_step_atr <= 0 or self.atr <= 0:
            return None
        step = self.params.ladder_step_atr * self.atr
        rung = int(self._profit(self.best_price) / step)
        return self.entry_price + self.direction * (rung + 1) * step

    @property
    def planned_remaining(self) -> float:
        """The post-scale remainder while a scale-out is in flight (what the
        resting stop must be resized to). Equals remaining_qty when no
        deferred commit is pending — e.g. the shadow-kitchen MK path, which
        adjusts remaining_qty itself before issuing its SCALE_OUT."""
        ps = self._pending_scale
        return self.remaining_qty - (ps[0] if ps is not None else 0.0)

    def confirm_scale_out(self) -> None:
        """Called by the actor once the scale-out order is ACCEPTED by the
        broker: the pending breakeven stop becomes current, and the deferred
        ledger commit (audit A1-6) lands — remaining_qty decremented and the
        firing layer's one-shot latch set, only now that the sale is real."""
        if self.pending_stop is not None:
            self.current_stop = self.pending_stop
            self.pending_stop = None
        if self._pending_scale is not None:
            qty, latch = self._pending_scale
            self._pending_scale = None
            self.remaining_qty -= qty
            self.breakeven_done = True
            if latch == "scaled_out":
                self.scaled_out = True
            elif latch == "squeeze":
                self.squeeze_scaled = True
            elif latch == "ladder":
                self._ladder_rungs_done += 1
            elif latch == "ext_bar":
                self._ext_bar_done = True

    def abort_scale_out(self) -> None:
        """Called by the actor when the scale-out could not be submitted
        (audit A1-6): drop the deferred commit so remaining_qty stays
        truthful and the layer's latch stays unset — it re-arms and retries
        on a later bar. Idempotent; safe after confirm (both fields None)."""
        self._pending_scale = None
        self.pending_stop = None

    def rollback_scale_out(self, unfilled_qty: float) -> None:
        """Audit A1-7: the broker ACCEPTED a scale sale's submit and then
        async-REJECTED the order — confirm_scale_out already landed the
        ledger commit, so the remaining_qty decrement is a lie (the shares
        were never sold). Restore the unfilled count so the protective stop
        covers the true position. When the staging is somehow still pending
        (the rejection raced the confirm), dropping it is enough. The
        one-shot latch stays set — conservative: the layer will not refire
        on this position, but the ledger and the stop are truthful."""
        if self._pending_scale is not None:
            self.abort_scale_out()
            return
        if unfilled_qty > 0:
            self.remaining_qty = min(self.qty, self.remaining_qty + unfilled_qty)
