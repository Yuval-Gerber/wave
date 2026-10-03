"""Entry strategies (§8.1) — Phase 7.2.

Each strategy is a pluggable class producing EntrySignal(symbol, side,
confidence, reason) with a concrete initial stop. Direction ALWAYS comes from
these rules; the scanner only ranked tradability (hard rule 8), and ML later
only gates/sizes (§9).

Strategies arm/disarm by session regime, evaluate one candidate at a time from
pure inputs (features + the symbol's 1-minute bars + session VWAP), and stand
down rather than guess. Parameters live in per-strategy dataclasses (§8.3);
Phase 10 tunes them through the training pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from waveapp.broker.base import OrderSide
from waveapp.data.hub import Bar
from waveapp.engine.scanner import SymbolFeatures
from waveapp.engine.session import Regime

logger = logging.getLogger("wave.scanner.strategies")

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    side: OrderSide
    confidence: float  # 0..1
    reason: str
    strategy: str
    entry_price: float  # reference price at signal time
    stop_price: float  # initial hard stop for the bracket (hard rule 3)
    half_size: bool = False  # e.g. midday lull (§8.1 VWAP)
    # R2 SNIPER jewel sizing (2026-09-24, VALIDATION.md): risk-budget
    # multiplier applied INSIDE RiskEngine.position_size, so the notional
    # and impact caps bind on the SCALED qty. 1.0 = ordinary sizing.
    size_mult: float = 1.0


# -- helpers -----------------------------------------------------------------


def _bars_in_window(bars: list[Bar], start_et: time, end_et: time) -> list[Bar]:
    result = []
    for bar in bars:
        et = bar.start.astimezone(ET).time()
        if start_et <= et < end_et:
            result.append(bar)
    return result


def _intraday_atr_estimate(bars: list[Bar], features: SymbolFeatures) -> float:
    """1-min ATR when enough bars exist, else scaled from the daily ATR%."""
    from waveapp.engine.exits import compute_atr

    atr = compute_atr(bars) if len(bars) >= 15 else None
    if atr:
        return atr
    daily_atr = features.atr_pct / 100.0 * features.price
    return max(daily_atr / 16.0, features.price * 0.0008)  # ~1-min share of the day


def _shorts_enabled() -> bool:
    """S3 (the short side): every SELL emission gates on the master config
    flag — read live the same way the VWAP climber knobs are (AppConfig.load
    inside evaluate), so a flip needs no rebuild. Any config problem answers
    False: fail closed, the long side never notices. With the flag False the
    short branches below are dead code and every strategy is bit-identical
    to the long-only champion."""
    try:
        from waveapp.config import AppConfig

        return bool(AppConfig.load().shorts_enabled)
    except Exception:
        return False


# -- CHOP-DAY SIGNAL INVERSION (2026-09-23) -------------------
# After the −$854 chop day: "if today we bought the longs as shorts and the
# shorts as longs we would have made money." On a DayJudge CHOP verdict the
# entry pipeline flips its OWN momentum signals at the same trigger — shorting
# the failed breakout, buying the failed breakdown. These are the pure-math
# helpers; the routing (verdict/confidence/flag/cap/lane checks) lives in
# connection_monitor._entry_pipeline. Direction still comes from rules (hard
# rule 8): the inversion is itself a rule — the mirror of the momentum rule —
# keyed off a rule-based day classifier, never off a price prediction.

CHOP_INVERT_PREFIX = "CHOP-INVERTED: "
# R1.5 EFFICACY GUARD (after the −$3,928 morning, 2026-09-24):
# the same transform, triggered by Wave's OWN entries failing instead of the
# day-type detector. One code path (this function) serves both.
EFFICACY_INVERT_PREFIX = "EFFICACY-INVERTED: "
_INVERT_PREFIXES = (CHOP_INVERT_PREFIX, EFFICACY_INVERT_PREFIX)


def invert_signal(signal: EntrySignal, prefix: str = CHOP_INVERT_PREFIX) -> EntrySignal | None:
    """Mirror a momentum EntrySignal — the SHARED transform for chop-day
    inversion and the R1.5 efficacy guard (only the reason prefix differs).

    - side flips (BUY <-> SELL). A BUY inverted is a plain short and must
      then pass every short-side gate (shorts_enabled, SSR, ETB/borrow fee,
      $2k floor) downstream; a SELL inverted is a plain long — no borrow
      logic needed, by construction it can never produce a "naked" anything.
    - stop mirrors around the entry trigger: new_stop = 2*entry − old_stop
      (the same dollar distance on the other side), rounded to cents.
    - half_size: always set here — the CHOP rule (a brand-new behavior
      starts small). The efficacy caller OVERRIDES it back to the original
      signal's size (2026-09-24: Wave must MAKE money, not trade
      smaller — efficacy inversions ride FULL size).
    - reason gets the given prefix so journals, logs and pushes identify
      inverted entries at a glance (strategy tag stays untouched).

    Returns None when the mirror is degenerate (stop would sit at or on the
    wrong side of the entry — e.g. old stop at/through the entry price);
    callers then leave the original signal alone rather than trade nonsense.
    """
    import dataclasses

    new_side = OrderSide.SELL if signal.side is OrderSide.BUY else OrderSide.BUY
    new_stop = round(2.0 * signal.entry_price - signal.stop_price, 2)
    if new_side is OrderSide.SELL and new_stop <= signal.entry_price:
        return None  # short's protective stop must rest ABOVE the entry
    if new_side is OrderSide.BUY and new_stop >= signal.entry_price:
        return None  # long's protective stop must rest BELOW the entry
    return dataclasses.replace(
        signal,
        side=new_side,
        stop_price=new_stop,
        half_size=True,
        reason=prefix + signal.reason,
    )


def is_inverted(signal) -> bool:
    """True when a signal was produced by invert_signal, either flavor
    (reason-prefix tag) — the trend gate stands aside for both."""
    return str(getattr(signal, "reason", "") or "").startswith(_INVERT_PREFIXES)


def is_chop_inverted(signal) -> bool:
    """True only for CHOP-detector inversions — the chop_invert_max_per_day
    cap counts these alone; efficacy inversions self-correct via their own
    flip-back rule and never burn the detector's damage cap."""
    return str(getattr(signal, "reason", "") or "").startswith(CHOP_INVERT_PREFIX)


# -- 1. ORB-5 on stocks-in-play ---------------------------------------------


@dataclass(frozen=True)
class ORBParams:
    # Sim fidelity (2026-08-25, scanner rework): the validated champion takes
    # the FIRST break of the 5-min range at ANY hour, high-based, with no
    # rvol/range-size gates at signal time — stock selection lives in the
    # day list (stocks-in-play, §9), exactly as in the adoption evidence.
    min_rvol: float = 0.0
    range_start: time = time(9, 30)
    range_end: time = time(9, 35)
    min_range_atr: float = 0.0
    breakout_buffer: float = 0.0  # $ beyond the range edge
    # ORB DEADLINE — ADOPTED 2026-09-03 (knife3 sweep, 4/4 with FPB on the
    # bare kitchen; blueprint II.18/II.22): 46.6% of mover HODs print in the
    # first 15 min, 85% by 10:30 — a late range-break buys the fade (the
    # ARKG −$661 class). No ORB entries at/after this ET time. None = off.
    # rolled back 2026-09-04 with the kitchen (its 4/4 was on the bare
    # base; on the old kitchen the deadline tested 1/4 — evidence-rejected)
    entry_deadline: time | None = None


class ORB5Strategy:
    """§8.1(1): 5-min opening-range breakout on stocks-in-play. Long on the
    first tick above the range high, stop at the range low. Armed ALL DAY —
    the champion sim (+$53.9k OOS at 6 slots) takes midday and power-hour
    breakouts too; the old OPEN_DRIVE-only arming silently dropped them."""

    name = "ORB"
    arms_in = (Regime.OPEN_DRIVE, Regime.MIDDAY, Regime.POWER_HOUR)

    def __init__(self, params: ORBParams | None = None) -> None:
        self.params = params or ORBParams()

    def evaluate(
        self,
        features: SymbolFeatures,
        bars: list[Bar],
        session_vwap: float | None,
        regime: Regime,
        is_lull: bool = False,
        now: datetime | None = None,
    ) -> EntrySignal | None:
        p = self.params
        if regime not in self.arms_in:
            return None
        if p.min_rvol > 0 and features.rvol < p.min_rvol:
            return None
        if p.entry_deadline is not None and now is not None and now.time() >= p.entry_deadline:
            return None  # adopted deadline: late breaks buy the fade
        opening = _bars_in_window(bars, p.range_start, p.range_end)
        if len(opening) < 3:
            return None  # range not established yet / no data
        range_high = max(b.high for b in opening)
        range_low = min(b.low for b in opening)
        if p.min_range_atr > 0:
            atr = _intraday_atr_estimate(bars, features)
            if (range_high - range_low) < p.min_range_atr * atr:
                return None  # dead open — no meaningful range to break
        last = bars[-1]
        if last.high > range_high + p.breakout_buffer:
            # sim-exact entry reference: the break level, or the close if the
            # bar already ran past it (max of the two)
            entry_ref = max(range_high, last.close)
            return EntrySignal(
                symbol=features.symbol,
                side=OrderSide.BUY,
                confidence=min(max(features.rvol, 1.0) / 5.0, 1.0),
                reason=(
                    f"ORB-5 breakout: high {last.high:.2f} > range high {range_high:.2f} "
                    f"(rvol {features.rvol:.1f})"
                ),
                strategy=self.name,
                entry_price=entry_ref,
                stop_price=round(range_low, 2),
            )
        # breakdown SHORT (S3, 2026-09-23 — §8.1(1) mirror): break BELOW the
        # 5-min range low, stop at the range HIGH (the opposite extreme).
        # Gated on shorts_enabled: until the owner flips the flag this branch is
        # dead and the strategy stays the long-only champion (the 2026-08-21
        # GBTC stand-down). SSR arming is enforced at the pipeline + risk
        # levels (§12) — the strategy has no symbol SSR state.
        if last.low < range_low - p.breakout_buffer and _shorts_enabled():
            entry_ref = min(range_low, last.close)  # sim-exact mirror
            return EntrySignal(
                symbol=features.symbol,
                side=OrderSide.SELL,
                confidence=min(max(features.rvol, 1.0) / 5.0, 1.0),
                reason=(
                    f"ORB-5 breakdown (short): low {last.low:.2f} < range low "
                    f"{range_low:.2f} (rvol {features.rvol:.1f})"
                ),
                strategy=self.name,
                entry_price=entry_ref,
                stop_price=round(range_high, 2),  # stop ABOVE entry
            )
        return None


# -- 2. Gap-and-Go momentum --------------------------------------------------


@dataclass(frozen=True)
class GapParams:
    min_gap_pct: float = 3.0
    min_rvol: float = 3.0
    min_premarket_volume: float = 100_000
    stop_daily_atr: float = 1.0  # validated Phase 10 champion: entry − 1×daily ATR


class GapAndGoStrategy:
    """§8.1(3): gap ≥3% + heavy volume, continuation in the gap direction.
    Arms PRE→OPEN_DRIVE and re-arms in POWER_HOUR (intraday momentum)."""

    name = "GAP"
    arms_in = (Regime.PRE, Regime.OPEN_DRIVE, Regime.POWER_HOUR)

    def __init__(self, params: GapParams | None = None) -> None:
        self.params = params or GapParams()

    def evaluate(
        self,
        features: SymbolFeatures,
        bars: list[Bar],
        session_vwap: float | None,
        regime: Regime,
        is_lull: bool = False,
        now: datetime | None = None,
    ) -> EntrySignal | None:
        p = self.params
        if regime not in self.arms_in:
            return None
        if abs(features.gap_pct) < p.min_gap_pct or features.rvol < p.min_rvol:
            return None
        if features.day_volume < p.min_premarket_volume:
            return None
        if not bars or session_vwap is None:
            return None  # need live data to confirm the push
        price = bars[-1].close
        gap_up = features.gap_pct > 0
        # champion fidelity (2026-08-20, after the MRNA short): the adopted
        # Phase 10 portfolio is LONG-ONLY — gap-down shorts stand down UNLESS
        # the S3 master flag is on (2026-09-23): gap ≤ −3% + the same rvol /
        # premarket-volume bars above + continuation BELOW VWAP = the §8.1(3)
        # mirror, stop ABOVE entry. §11 still owns parameter adoption.
        if not gap_up and not _shorts_enabled():
            return None
        side = OrderSide.BUY if gap_up else OrderSide.SELL
        # continuation confirmation: price on the gap side of session VWAP
        if gap_up and price <= session_vwap:
            return None
        if not gap_up and price >= session_vwap:
            return None
        # stop distance in DAILY ATR dollars — the geometry the Phase 10
        # champion validated (2026-08-19: the old 1.5×1-min-ATR stop was
        # ~10× tighter than what the adoption evidence covers)
        daily_atr = max(features.atr_pct / 100.0 * features.price, 0.01)
        offset = p.stop_daily_atr * daily_atr
        stop = price - offset if gap_up else price + offset
        return EntrySignal(
            symbol=features.symbol,
            side=side,
            confidence=min(abs(features.gap_pct) / 6.0 + features.rvol / 10.0, 1.0),
            reason=(
                f"Gap-and-Go: gap {features.gap_pct:+.1f}%, rvol {features.rvol:.1f}, "
                f"price {'above' if gap_up else 'below'} VWAP {session_vwap:.2f}"
            ),
            strategy=self.name,
            entry_price=price,
            stop_price=round(stop, 2),
        )


# -- 3. VWAP regime-switch ---------------------------------------------------


@dataclass(frozen=True)
class VWAPParams:
    # Phase 10 champion values (adopted 2026-08-19 — §8.3: parameters come
    # from the pipeline). Trend-day LONG pullbacks only; range-day fades and
    # shorts were never validated and stand down.
    min_gap_pct: float = 2.5  # gap up ≥ this → trend day worth trading
    pullback_daily_atr: float = 0.6  # entry: within this ×dailyATR of VWAP
    stop_daily_atr: float = 1.25  # stop: VWAP − this ×dailyATR
    entry_start_et: time = time(10, 0)  # let VWAP settle first
    # CLIMBER challenger (2026-09-16, "no chance we cannot
    # make money out of 13,000 stocks midday"): the gap gate blinded the
    # all-day strategy to steady climbers — names that never gapped but
    # trend on their own tape. Trend evidence = day change ≥ climber_day_pct
    # above the OPEN with rvol ≥ climber_rvol. Always half-size; tagged
    # "climber" so the cohort grades separately.
    climber_enabled: bool = True
    climber_day_pct: float = 2.0
    climber_rvol: float = 1.5


class VWAPRegimeStrategy:
    """§8.1(2): classify the day (trend vs range), then trade WITH VWAP on
    trend days (pullback entries) or fade extensions on range days. When the
    detector is unsure, stand down — wrong-regime trading is the documented
    failure mode."""

    name = "VWAP"
    arms_in = (
        Regime.OPEN_DRIVE,
        Regime.MIDDAY,
        Regime.POWER_HOUR,
    )

    def __init__(self, params: VWAPParams | None = None) -> None:
        self.params = params or VWAPParams()

    def evaluate(
        self,
        features: SymbolFeatures,
        bars: list[Bar],
        session_vwap: float | None,
        regime: Regime,
        is_lull: bool = False,
        now: datetime | None = None,
    ) -> EntrySignal | None:
        p = self.params
        if regime not in self.arms_in:
            return None
        if not bars or session_vwap is None:
            return None
        # champion gate: a real gap-up trend day — OR (climber challenger)
        # a name trending on its own tape: ≥ climber_day_pct above its OPEN
        # on real volume. S3 mirror (2026-09-23, shorts_enabled only): a
        # gap-DOWN trend day (gap ≤ −min_gap_pct) trades WITH the downtrend —
        # price below VWAP, pullbacks UP toward VWAP from below, stop ABOVE
        # VWAP. No short climber lane: that challenger is long-only cohort.
        climber = False
        short_trend = _shorts_enabled() and features.gap_pct <= -p.min_gap_pct
        if not short_trend and features.gap_pct < p.min_gap_pct:
            # THE CLOSED DOOR (2026-09-16 X-ray): the scanner never fills
            # features.day_open — derive the session open from the bars
            # themselves (backfill seeds them from 9:30).
            day_open = features.day_open or (bars[0].open if bars else 0.0)
            day_pct = (features.price / day_open - 1.0) * 100.0 if day_open else 0.0
            # thresholds live-read so the climber lane and this gate can
            # never disagree (the midday-supply order, 2026-09-16);
            # dataclass values remain the fallback.
            day_pct_min, rvol_min = p.climber_day_pct, p.climber_rvol
            try:
                from waveapp.config import AppConfig as _ClCfg

                _cfg = _ClCfg.load()
                day_pct_min = float(getattr(_cfg, "climber_day_pct", day_pct_min))
                rvol_min = float(getattr(_cfg, "climber_rvol", rvol_min))
            except Exception:  # noqa: S110 — config miss falls back to params
                pass
            if not (p.climber_enabled and day_pct >= day_pct_min and features.rvol >= rvol_min):
                return None
            climber = True
        current = now or datetime.now(UTC)
        if current.astimezone(ZoneInfo("America/New_York")).time() < p.entry_start_et:
            return None  # VWAP hasn't settled yet
        price = bars[-1].close
        daily_atr = max(features.atr_pct / 100.0 * features.price, 0.01)
        if short_trend:
            # exact reflection of the long geometry below: downtrend intact
            # means price BELOW VWAP; the entry is a pullback TOWARD VWAP
            # from below (within pullback_daily_atr), stop ABOVE VWAP.
            if price >= session_vwap:
                return None  # downtrend broken — no pullback short
            if session_vwap - price > p.pullback_daily_atr * daily_atr:
                return None  # stretched below, not a pullback
            stop = session_vwap + p.stop_daily_atr * daily_atr
            return EntrySignal(
                symbol=features.symbol,
                side=OrderSide.SELL,
                confidence=min(-features.gap_pct / (2 * p.min_gap_pct), 1.0),
                reason=(
                    f"VWAP trend-day pullback (short): gap {features.gap_pct:+.1f}%, "
                    f"price {price:.2f} near VWAP {session_vwap:.2f}"
                ),
                strategy=self.name,
                entry_price=price,
                stop_price=round(stop, 2),
                half_size=is_lull,  # §8.1: half size in the midday lull
            )
        if price <= session_vwap:
            return None  # trend broken — no pullback entry
        if price - session_vwap > p.pullback_daily_atr * daily_atr:
            return None  # stretched, not a pullback
        stop = session_vwap - p.stop_daily_atr * daily_atr
        # a climber's gap is ~0 by definition — its conviction is the climb
        # itself, not the gap (a gap-based confidence would be ~0 and could
        # zero downstream sizing)
        if climber:
            day_open = features.day_open or (bars[0].open if bars else 0.0)
            day_pct = (features.price / day_open - 1.0) * 100.0 if day_open else 0.0
            confidence = max(min(day_pct / 4.0, 1.0), 0.3)
        else:
            confidence = min(features.gap_pct / (2 * p.min_gap_pct), 1.0)
        return EntrySignal(
            symbol=features.symbol,
            side=OrderSide.BUY,
            confidence=confidence,
            reason=(
                f"VWAP {'climber' if climber else 'trend-day'} pullback: "
                f"gap {features.gap_pct:+.1f}%, price "
                f"{price:.2f} near VWAP {session_vwap:.2f}"
            ),
            strategy=self.name,
            entry_price=price,
            stop_price=round(stop, 2),
            half_size=is_lull or climber,  # challenger cohort rides half-size
        )


# -- 4. First pullback (blueprint 5.1 — ADOPTED on adopted 2026-09-01) ----


@dataclass(frozen=True)
class FirstPullbackParams:
    # Values are the SIM-EXACT swept configuration that won ALL FOUR windows
    # (research_p1.py p1_fp: +$181/+$5,034/+$3,848/+$5,091 vs the champion;
    # FPB trades standalone positive in every window). §8.3: changes only
    # through the pipeline.
    entry_start_et: time = time(9, 45)
    entry_end_et: time = time(11, 0)
    min_day_pct: float = 2.0  # the leg that makes it a leader
    max_pullback_reds: int = 3  # 1-3 red 1-min candles
    max_retrace_of_leg: float = 0.5  # pullback keeps >50% of the leg


class FirstPullbackStrategy:
    """Blueprint 5.1 (the FRVO fix): a scanner leader up ≥2% and above VWAP
    pulls back 1-3 red 1-min candles (shallow — under half the leg, never
    losing VWAP); the first green candle that breaks the prior candle's high
    is the entry, stop at the pullback low. This is the mid-move trigger the
    engine never had — movers found after the open finally have a path in.

    Stateless by design: the pullback is re-read from the bar history every
    evaluation, so restarts cannot corrupt a state machine. Live fidelity
    note: each red candle's VWAP check uses the CURRENT session VWAP (the
    sim used the running value at that bar; mid-morning drift is small)."""

    name = "FPB"
    arms_in = (Regime.OPEN_DRIVE, Regime.MIDDAY)  # the 9:45-11:00 window

    def __init__(self, params: FirstPullbackParams | None = None) -> None:
        self.params = params or FirstPullbackParams()

    def evaluate(
        self,
        features: SymbolFeatures,
        bars: list[Bar],
        session_vwap: float | None,
        regime: Regime,
        is_lull: bool = False,
        now: datetime | None = None,
    ) -> EntrySignal | None:
        p = self.params
        if regime not in self.arms_in:
            return None
        if len(bars) < 3 or session_vwap is None or features.day_open <= 0:
            return None
        current = now or datetime.now(UTC)
        et = current.astimezone(ET).time()
        if not (p.entry_start_et <= et <= p.entry_end_et):
            return None
        day_open = features.day_open
        last = bars[-1]
        day_pct = (last.close / day_open - 1.0) * 100.0
        if day_pct < p.min_day_pct:
            return None
        # the entry bar: green, and it takes out the prior candle's high
        if last.close <= last.open or last.high <= bars[-2].high:
            return None
        # walk back the consecutive red pullback candles
        reds: list[Bar] = []
        for bar in reversed(bars[:-1]):
            if bar.close < bar.open:
                reds.append(bar)
            else:
                break
        if not 1 <= len(reds) <= p.max_pullback_reds:
            return None
        pullback_low = min(b.low for b in reds)
        for red in reds:
            if red.close <= session_vwap:
                return None  # pullback lost VWAP — not the clean form
            if (red.close / day_open - 1.0) * 100.0 < p.min_day_pct:
                return None  # the leg died during the pullback
        hod = max(b.high for b in bars)
        leg = hod - day_open
        if leg <= 0 or (hod - pullback_low) > p.max_retrace_of_leg * leg:
            return None  # too deep — half the leg is gone
        if pullback_low >= last.close:
            return None
        return EntrySignal(
            symbol=features.symbol,
            side=OrderSide.BUY,
            confidence=min(day_pct / 6.0, 1.0),
            reason=(
                f"first pullback: leader +{day_pct:.1f}%, {len(reds)} red candle(s) "
                f"held VWAP, green break of {bars[-2].high:.2f}"
            ),
            strategy=self.name,
            entry_price=last.close,
            stop_price=round(pullback_low, 2),
        )


ALL_STRATEGIES = (ORB5Strategy, GapAndGoStrategy, VWAPRegimeStrategy, FirstPullbackStrategy)


def armed_strategies(regime: Regime) -> list:
    """Fresh instances of every strategy armed in this regime."""
    return [cls() for cls in ALL_STRATEGIES if regime in cls.arms_in]
