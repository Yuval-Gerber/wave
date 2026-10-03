"""THE EFFICACY GUARD (R1.5) — Wave's missing reflex, built after the −$3,928
day (2026-09-24, the worst ever): the first two trades lost $1,500 at FULL
size within 25 minutes of the open, then 16 more losers followed with zero
feedback — nothing watched whether Wave's OWN entries were following through,
so it never adjusted. The Day Judge's breadth-based CHOP call came at 11:12,
eleven minutes before the daily halt. This tracker watches every fill in real
time and reacts in minutes, not hours.

directive (same day): Wave must MAKE money, not trade smaller — the
response to failing signals is to FLIP them, not to shrink or skip. TWO
modes, full normal sizing in both:

  MOMENTUM  — every day starts here; signals execute as-is.
  INVERTED  — every momentum signal executes through the shared
              ``invert_signal`` transform (strategies.py) at FULL size; the
              entry pipeline owns that application (connection_monitor).

Scoring (per entry, first touch wins, one outcome per position):
  PASS — reached +1.0 ATR before −1.0 ATR within EFFICACY_WINDOW_MIN minutes
         of the fill;
  FAIL — reached −1.0 ATR first, or the position was stopped/cut (closed)
         inside the window before passing;
  LATE — the window expired flat — neutral, excluded from all rates.

Flips (symmetric, evidence from the CURRENT mode's own trades only — every
entry is tagged with the mode that placed it):
  2 consecutive FAILs, or fail_rate >= 0.6 with n >= 3 scored outcomes.
  Hysteresis: after any flip, no re-flip until at least
  HYSTERESIS_MIN_SCORED outcomes have been scored in the new mode.
  Day rollover resets to MOMENTUM with fresh stats.

The tracker is PURE state — no broker, no DB handle, no clock of its own.
The shadow kitchen feeds it per-tick profit readings (efficacy_cb), the
monitor feeds fills/closes and consumes ``mode()`` in the entry pipeline,
and every flip is journaled through the M0 journal_db_cb pattern
(efficacy_events, migration 014) plus a notify_cb line for the phone.
The existing hard 3% daily-loss halt (RiskEngine, §12) is untouched above
all of this — hard rule 2.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("wave.engine.efficacy")

ET = ZoneInfo("America/New_York")

# -- modes --------------------------------------------------------------------
MODE_MOMENTUM = "MOMENTUM"
MODE_INVERTED = "INVERTED"

# -- outcomes -----------------------------------------------------------------
PASS = "PASS"  # noqa: S105 — an outcome label, not a secret
FAIL = "FAIL"
LATE = "LATE"

# -- the scoring rule ---------------------------------------------------------
# An entry must touch +PASS_ATR (position-ATR units, judge-frame so shorts
# score like longs) before −FAIL_ATR within this many minutes of the fill.
EFFICACY_WINDOW_MIN = 10
PASS_ATR = 1.0
FAIL_ATR = -1.0

# -- the flip rule (symmetric for both modes) --------------------------------
FLIP_AFTER_CONSEC_FAILS = 2
# R0.5 PRE-OPEN BIAS seed: with a bias_hint set ('SHORT'/'LONG', handed over
# by the monitor from the 9:25 PreOpenBias verdict), a FAIL on a MOMENTUM
# entry whose side CONTRADICTS the hint (a long fighting a SHORT-bias tape,
# or the mirror) flips after this many consecutive fails instead of the
# usual two — the biased morning was warned, so the first counter-bias fail
# is confirmation, not noise. Aligned-side fails and everything in INVERTED
# mode keep the normal ladder. The hint clears on day rollover and when the
# Day Judge takes the handoff (monitor calls set_bias_hint(None)).
BIAS_HINT_CONSEC_FAILS = 1
FLIP_FAIL_RATE = 0.6
FLIP_MIN_N = 3
# after any flip, at least this many scored outcomes must land in the new
# mode before the next flip is allowed (oscillation damper)
HYSTERESIS_MIN_SCORED = 3


@dataclass
class _Entry:
    symbol: str
    side: str
    t_ms: int
    mode: str  # the mode that placed this trade — its outcome scores THAT mode
    outcome: str | None = None


@dataclass
class _ModeStats:
    consecutive_fails: int = 0
    n_pass: int = 0
    n_fail: int = 0

    @property
    def n_scored(self) -> int:
        return self.n_pass + self.n_fail

    def fail_rate(self, min_n: int = FLIP_MIN_N) -> float | None:
        if self.n_scored < min_n:
            return None
        return self.n_fail / self.n_scored


class EfficacyTracker:
    """Per-ET-day efficacy state. Pure and injectable — see the module doc."""

    def __init__(
        self,
        journal_db_cb: Callable[..., object] | None = None,
        notify_cb: Callable[[str], object] | None = None,
    ) -> None:
        self._journal_db_cb = journal_db_cb
        self._notify_cb = notify_cb
        self._day: str | None = None
        self._bias_hint: str | None = None  # R0.5 pre-open seed: 'SHORT'/'LONG'
        self._mode = MODE_MOMENTUM
        self._flipped_today = False
        self._entries: dict[str, _Entry] = {}
        self._stats: dict[str, _ModeStats] = {
            MODE_MOMENTUM: _ModeStats(),
            MODE_INVERTED: _ModeStats(),
        }
        self.n_entries_today = 0

    # -- reads ---------------------------------------------------------------

    def mode(self) -> str:
        return self._mode

    @property
    def consecutive_fails(self) -> int:
        return self._stats[self._mode].consecutive_fails

    @property
    def n_scored(self) -> int:
        return self._stats[self._mode].n_scored

    def fail_rate(self, min_n: int = FLIP_MIN_N) -> float | None:
        return self._stats[self._mode].fail_rate(min_n)

    @property
    def bias_hint(self) -> str | None:
        return self._bias_hint

    def set_bias_hint(self, hint: str | None, day: str | None = None) -> None:
        """R0.5 PRE-OPEN BIAS seed (see BIAS_HINT_CONSEC_FAILS). 'SHORT' /
        'LONG' / None; anything else clears it — never a guessed side.
        ``day`` (the ET day the hint is FOR) rolls the tracker onto that day
        first, so the seed survives the day's first record_entry — at 9:25
        the tracker may still be sitting on yesterday, and a later roll_day
        would silently wipe an unstamped hint."""
        if day is not None:
            self.roll_day(day)
        hint = str(hint).strip().upper() if hint is not None else None
        self._bias_hint = hint if hint in ("SHORT", "LONG") else None

    # -- day lifecycle --------------------------------------------------------

    def roll_day(self, day: str) -> None:
        """New ET day → everything resets and the mode returns to MOMENTUM.
        Idempotent for the same day; called by the monitor's signal-day roll
        and defensively from record_entry."""
        if day == self._day:
            return
        was = self._mode
        self._day = day
        self._entries = {}
        self._stats = {MODE_MOMENTUM: _ModeStats(), MODE_INVERTED: _ModeStats()}
        self._flipped_today = False
        self._bias_hint = None  # yesterday's pre-open verdict never carries over
        self.n_entries_today = 0
        if was != MODE_MOMENTUM:
            # quiet reset — a journal row and a log line, but no phone push
            # at the day boundary (nothing is failing at midnight)
            self._mode = MODE_MOMENTUM
            self._journal_flip(was, MODE_MOMENTUM, "day rollover", _ModeStats(), notify=False)

    # -- feeds ----------------------------------------------------------------

    def record_entry(self, key: str, symbol: str, side: str, t_ms: int) -> None:
        """One FILLED entry, keyed by position key, stamped with the CURRENT
        mode (the mode that placed it). Duplicate keys are ignored."""
        try:
            day = datetime.fromtimestamp(t_ms / 1000, tz=UTC).astimezone(ET).date().isoformat()
        except (OSError, OverflowError, ValueError):
            day = self._day or ""
        if day:
            self.roll_day(day)
        if key in self._entries:
            return
        self._entries[key] = _Entry(symbol=symbol, side=side, t_ms=int(t_ms), mode=self._mode)
        self.n_entries_today += 1

    def record_tick(self, key: str, profit_atr: float, age_ms: int) -> str | None:
        """Per-tick early-outcome feed (the kitchen's judge path): first touch
        of +1A → PASS, first touch of −1A → FAIL, window expiry → LATE.
        Returns the outcome it just scored, else None."""
        entry = self._entries.get(key)
        if entry is None or entry.outcome is not None:
            return None
        if age_ms > EFFICACY_WINDOW_MIN * 60_000:
            return self._score(entry, LATE)
        if profit_atr >= PASS_ATR:
            return self._score(entry, PASS)
        if profit_atr <= FAIL_ATR:
            return self._score(entry, FAIL)
        return None

    def record_close(self, key: str, t_ms: int) -> str | None:
        """A closed round trip closes out any unscored entry: a judge cut /
        stop-close INSIDE the window is a FAIL (the entry never proved
        itself); a close past the window is LATE (neutral). Already-scored
        positions are untouched — one outcome per position, first touch
        wins."""
        entry = self._entries.get(key)
        if entry is None or entry.outcome is not None:
            return None
        age_ms = int(t_ms) - entry.t_ms
        return self._score(entry, FAIL if age_ms <= EFFICACY_WINDOW_MIN * 60_000 else LATE)

    def record_outcome(self, key: str, outcome: str) -> str | None:
        """Direct scoring (tests / explicit callers). Same one-outcome rule."""
        entry = self._entries.get(key)
        if entry is None or entry.outcome is not None or outcome not in (PASS, FAIL, LATE):
            return None
        return self._score(entry, outcome)

    # -- internals -------------------------------------------------------------

    def _score(self, entry: _Entry, outcome: str) -> str:
        entry.outcome = outcome
        if outcome == LATE:
            return outcome  # neutral — excluded from every rate and counter
        stats = self._stats[entry.mode]
        if outcome == FAIL:
            stats.n_fail += 1
            stats.consecutive_fails += 1
        else:
            stats.n_pass += 1
            stats.consecutive_fails = 0  # PASSes reset the current streak
        logger.info(
            "EFFICACY scored %s %s → %s (%s mode: %d/%d failed, streak %d)",
            entry.symbol,
            entry.side,
            outcome,
            entry.mode,
            stats.n_fail,
            stats.n_scored,
            stats.consecutive_fails,
        )
        # only the CURRENT mode's own trades can flip it
        if entry.mode == self._mode:
            self._maybe_flip(counter_bias_fail=(outcome == FAIL and self._counter_bias(entry.side)))
        return outcome

    def _counter_bias(self, side: str) -> bool:
        """True when a MOMENTUM entry's side fights the pre-open bias_hint
        (a long under a SHORT hint, a short under a LONG hint) — its FAIL
        confirms the 9:25 read and flips on the fast ladder."""
        if self._bias_hint is None or self._mode != MODE_MOMENTUM:
            return False
        s = str(side or "").strip().upper()
        is_long = s in ("BUY", "LONG")
        is_short = s in ("SELL", "SHORT")
        return (self._bias_hint == "SHORT" and is_long) or (self._bias_hint == "LONG" and is_short)

    def _maybe_flip(self, counter_bias_fail: bool = False) -> None:
        stats = self._stats[self._mode]
        rate = stats.fail_rate(FLIP_MIN_N)
        streak_needed = BIAS_HINT_CONSEC_FAILS if counter_bias_fail else FLIP_AFTER_CONSEC_FAILS
        by_streak = stats.consecutive_fails >= streak_needed
        by_rate = rate is not None and rate >= FLIP_FAIL_RATE
        if not (by_streak or by_rate):
            return
        if self._flipped_today and stats.n_scored < HYSTERESIS_MIN_SCORED:
            return  # hysteresis: the new mode gets its fair sample first
        if by_streak and counter_bias_fail and stats.consecutive_fails < FLIP_AFTER_CONSEC_FAILS:
            reason = f"first counter-bias fail (pre-open {self._bias_hint} bias)"
        elif by_streak:
            reason = f"{stats.consecutive_fails} consecutive fails"
        else:
            reason = f"signals failing {stats.n_fail}/{stats.n_scored}"
        old = self._mode
        new = MODE_INVERTED if old == MODE_MOMENTUM else MODE_MOMENTUM
        self._mode = new
        self._flipped_today = True
        # the new stint judges itself on its own trades only
        self._stats[new] = _ModeStats()
        self._journal_flip(old, new, reason, stats, notify=True)

    def _journal_flip(
        self, old: str, new: str, reason: str, stats: _ModeStats, notify: bool
    ) -> None:
        text = f"EFFICACY: flipping to {new} — {reason}"
        logger.warning(text)
        if self._journal_db_cb is not None:
            try:
                self._journal_db_cb(
                    "efficacy_event",
                    {
                        "ts": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
                        "day": self._day or "",
                        "from_mode": old,
                        "to_mode": new,
                        "reason": reason,
                        "consecutive_fails": stats.consecutive_fails,
                        "n_pass": stats.n_pass,
                        "n_fail": stats.n_fail,
                    },
                )
            except Exception:
                logger.debug("efficacy journal write failed", exc_info=True)
        if notify and self._notify_cb is not None:
            try:
                self._notify_cb(f"🔁 {text}")
            except Exception:
                logger.debug("efficacy notify failed", exc_info=True)
