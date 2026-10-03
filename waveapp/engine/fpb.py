"""THE SNIPER BOOK (R2, 2026-09-24 — the sniper validation study).

The validated entry-book rebuild: over the 310 real paper trades of
2026-08-19 → 2026-09-24 the two proven edges — the first-pullback (FPB)
replacement for extended entries and the jewel band — turn +$853 actual into
+$5,995 (SNIPER) / +$9,073 (SNIPER+SIZE), lift green days 11/22 → 16/22 and
shrink the worst day −$3,933 → −$2,497. This module holds the pure state and
math; the routing (which signals convert, which starve, all gates) lives in
connection_monitor._entry_pipeline / _sniper_watch_pass.

Four legs (all behind config.sniper_book, default ON — decision):

1. FPB CONVERSION (+$2,498 replaced + $2,644 skipped in the replay): an
   ordinary momentum signal ≥2% extended from the day's open is NOT chased —
   it becomes a PULLBACK WATCH; the entry fires only on the study's tested
   rule (check_trigger below), with the tighter pullback stop. No holding
   pullback within 20 min = no trade (the proven skip).
2. JEWEL SIZING: GAP BUY signals <1% above open (n=33, WR 85%, +$4,709 —
   the whole jewel band's profit is GAP's) ride JEWEL_SIZE_MULT risk.
3. POCKET STARVES (measured losers): ORB long on TREND_DOWN (14% WR,
   −$2,274), any ordinary momentum entry in the 14:00-14:59 ET hour (19%
   WR, −$1,334), ORB <1%-extension on CHOP (31% WR, −$1,221).
4. JOURNALING: every conversion/watch/trigger/expiry/starve logs; FPB
   entries carry the "FPB: " / "FPB-short: " reason prefix so they are
   separable in journals and the entry_lag/candidates datasets.

Scope contract (documented, tested): sniper transforms touch ORDINARY
momentum signals only. Inverted signals of any kind (CHOP / EFFICACY), the
climber lane and the FPB-gapper/FPB-strategy lanes are EXEMPT — those are
pullback or fade logic with their own contracts; there is no chase to
convert and their pockets were not the measured losers.

Hard rule 8 stands: nothing here predicts price. The FPB rule is the same
momentum direction at a better, evidence-tested entry; the starves remove
measured-negative pockets; the jewel mult sizes a measured-positive one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from waveapp.broker.base import OrderSide
from waveapp.engine.strategies import EntrySignal

logger = logging.getLogger("wave.engine.sniper")

# -- constants (VALIDATION.md; §8.3: changes only through the pipeline) ------

FPB_WINDOW_MIN = 20  # minutes a watch lives — the study's give-up window
FPB_MAX_WATCHES = 8  # concurrent-watch cap
SNIPER_EXT_PCT = 2.0  # ≥ this % from the open = extended → convert to watch
JEWEL_EXT_PCT = 1.0  # GAP BUY < this % above open = the jewel band
JEWEL_SIZE_MULT = 1.5  # jewel risk multiplier (GAP-only, the sharper variant)
WATCH_CUTOFF_ET = time(15, 20)  # close guard: no new watches, watches die
STARVE_HOUR_ET = 14  # the 14:00-14:59 ET graveyard (19% WR, n=27)
FPB_PREFIX = "FPB: "
FPB_SHORT_PREFIX = "FPB-short: "  # the untested SELL mirror — separable


def extension_pct(entry_price: float, day_open: float) -> float:
    """Signed % of the entry price vs the day's open (the study's axis)."""
    if day_open <= 0:
        return 0.0
    return (entry_price / day_open - 1.0) * 100.0


def is_extended(signal: EntrySignal, day_open: float) -> bool:
    """Extended in the SIGNAL's direction: a BUY ≥2% above the open or a
    SELL ≥2% below it — the band the replay converted to FPB watches."""
    ext = extension_pct(signal.entry_price, day_open)
    if signal.side is OrderSide.BUY:
        return ext >= SNIPER_EXT_PCT
    return ext <= -SNIPER_EXT_PCT


def is_jewel(signal: EntrySignal, day_open: float) -> bool:
    """The sized jewel band: GAP-strategy BUY <1% above the open (WR 85%,
    +$4,709 on n=33 — GAP-only per the validation's sharper-variant note)."""
    return (
        signal.strategy == "GAP"
        and signal.side is OrderSide.BUY
        and day_open > 0
        and extension_pct(signal.entry_price, day_open) < JEWEL_EXT_PCT
    )


def is_fpb_entry(signal) -> bool:
    """True for a signal produced by check_trigger (reason-prefix tag)."""
    return str(getattr(signal, "reason", "") or "").startswith((FPB_PREFIX, FPB_SHORT_PREFIX))


@dataclass
class FpbWatch:
    """One pullback watch: the remembered momentum signal + the signal-bar
    anchors the tested rule needs. Frozen facts only — no live state."""

    symbol: str
    signal: EntrySignal  # the ORIGINAL momentum signal, untouched
    features: object  # SymbolFeatures at signal time (day_open, atr_pct…)
    trigger_price: float  # the signal's entry price — the level to undercut
    signal_bar_low: float
    signal_bar_high: float
    signal_bar_start: datetime  # UTC minute stamp of the signal bar
    created_at: datetime  # UTC t0 — the 20-min window anchor
    avg_daily_volume: float | None


def check_trigger(
    watch: FpbWatch, bars: list, session_vwap: float | None, now_utc: datetime
) -> EntrySignal | None:
    """THE TESTED RULE, implemented verbatim (entry_timing_study.md Study B /
    VALIDATION.md, cross-checked to the cent against fpb_detail.csv):

    LONG — the FIRST completed minute bar after the signal bar whose low
    UNDERCUTS the trigger price while HOLDING above BOTH the 9:30-anchored
    session VWAP and the signal bar's low. The undercut IS the retrace
    minute; the study requires no separate prior red candle, and fidelity to
    the tested rule beats cleverness. Fill = that bar's close, capped at the
    trigger price (a resting limit fills no worse than the chase would
    have). Stop = the pullback bar's low (tighter, per the study), floored
    at the original signal stop.

    SHORT mirror (untested nuance, shipped reflected + separably tagged
    "FPB-short:"): first completed bar whose high pokes ABOVE the trigger
    while holding BELOW both session VWAP and the signal bar's high; fill =
    bar close floored at the trigger, stop = the pullback high capped at
    the original stop.

    Live-fidelity note (same as FirstPullbackStrategy): each bar is judged
    against the CURRENT session VWAP; the study used the running value at
    that bar — mid-morning drift is small. Only COMPLETED bars count (start
    + 60s ≤ now): the pipeline's minute cadence matches the study's minute
    bars. Bars are scanned in order; the first qualifying bar wins even if
    later bars broke down — the door judge downstream owns drift-at-entry.
    Returns the ready-to-gate EntrySignal or None (keep waiting).
    """
    if session_vwap is None or not bars:
        return None
    long_side = watch.signal.side is OrderSide.BUY
    window_end = watch.signal_bar_start + timedelta(minutes=FPB_WINDOW_MIN)
    for bar in bars:
        if bar.start <= watch.signal_bar_start or bar.start > window_end:
            continue
        if bar.start + timedelta(seconds=60) > now_utc:
            continue  # still forming — the study's bars are completed minutes
        if long_side:
            if not (
                bar.low < watch.trigger_price
                and bar.low > session_vwap
                and bar.low > watch.signal_bar_low
            ):
                continue
            entry = min(bar.close, watch.trigger_price)
            stop = max(round(bar.low, 2), watch.signal.stop_price)
            if stop >= entry:
                continue  # degenerate (bar closed on its low) — keep waiting
            return replace(
                watch.signal,
                entry_price=entry,
                stop_price=stop,
                reason=FPB_PREFIX + watch.signal.reason,
            )
        if not (
            bar.high > watch.trigger_price
            and bar.high < session_vwap
            and bar.high < watch.signal_bar_high
        ):
            continue
        entry = max(bar.close, watch.trigger_price)
        stop = min(round(bar.high, 2), watch.signal.stop_price)
        if stop <= entry:
            continue
        return replace(
            watch.signal,
            entry_price=entry,
            stop_price=stop,
            reason=FPB_SHORT_PREFIX + watch.signal.reason,
        )
    return None


class SniperBook:
    """The watch book + the day's sniper counters. Day-keyed like the
    one-trade-per-day mark; connection_monitor._roll_signal_day rolls it."""

    def __init__(self) -> None:
        self.watches: dict[str, FpbWatch] = {}
        self._spent: set[str] = set()  # expired watches — the proven skip
        self._day: str | None = None
        self.stats: dict[str, int] = self._zero_stats()

    @staticmethod
    def _zero_stats() -> dict[str, int]:
        return {"converted": 0, "triggered": 0, "expired": 0, "killed": 0, "starved": 0, "jewel": 0}

    @property
    def active(self) -> bool:
        return bool(self.watches)

    def roll_day(self, day: str) -> None:
        if day == self._day:
            return
        self._day = day
        self.watches.clear()
        self._spent.clear()
        self.stats = self._zero_stats()

    def convert(
        self,
        signal: EntrySignal,
        features,
        bars: list,
        now_utc: datetime,
        now_et: datetime,
    ) -> str:
        """Try to turn an extended momentum signal into a pullback watch.
        Returns the outcome: "watch" (created), "dup" (one already pending),
        "spent" (its watch already expired today — the skip is final),
        "late" (≥15:20 ET close guard) or "cap" (book full). In every
        non-"watch" case the extended entry is still NOT taken as-is."""
        symbol = signal.symbol
        if symbol in self.watches:
            return "dup"
        if symbol in self._spent:
            return "spent"
        if now_et.time() >= WATCH_CUTOFF_ET:
            return "late"
        if len(self.watches) >= FPB_MAX_WATCHES:
            return "cap"
        last = bars[-1]
        self.watches[symbol] = FpbWatch(
            symbol=symbol,
            signal=signal,
            features=features,
            trigger_price=signal.entry_price,
            signal_bar_low=last.low,
            signal_bar_high=last.high,
            signal_bar_start=last.start,
            created_at=now_utc,
            avg_daily_volume=getattr(features, "avg_daily_volume", None),
        )
        self.stats["converted"] += 1
        return "watch"

    def remove(self, symbol: str) -> None:
        self.watches.pop(symbol, None)

    def prune(
        self, now_utc: datetime, burned: set[str], inverted: bool, now_et: datetime
    ) -> list[tuple[FpbWatch, str]]:
        """Kill watches whose reason-to-die arrived; returns (watch, why)
        pairs for the caller to journal. Deaths:
        - INVERTED mode (efficacy flip): the anti-momentum book has no
          pullback-buys — every watch dies. NOT marked spent: a flip back
          to MOMENTUM may honestly re-watch fresh signals.
        - day-mark burned: another entry took the symbol's one trade today.
        - close guard (≥15:20 ET): no watch survives into the close window.
        - window expiry (>20 min): the proven +$2,644 skip — marked spent so
          the symbol's extended signals stay skipped for the day."""
        dead: list[tuple[FpbWatch, str]] = []
        for symbol, watch in list(self.watches.items()):
            if inverted:
                why = "mode flipped to INVERTED — anti-momentum book has no pullback-buys"
            elif symbol in burned:
                why = "day-mark burned by another entry"
            elif now_et.time() >= WATCH_CUTOFF_ET:
                why = "close guard (15:20 ET)"
                self._spent.add(symbol)
            elif now_utc - watch.created_at > timedelta(minutes=FPB_WINDOW_MIN):
                why = f"no holding pullback in {FPB_WINDOW_MIN} min — the proven skip"
                self._spent.add(symbol)
                self.stats["expired"] += 1
                del self.watches[symbol]
                dead.append((watch, why))
                continue
            else:
                continue
            self.stats["killed"] += 1
            del self.watches[symbol]
            dead.append((watch, why))
        return dead
