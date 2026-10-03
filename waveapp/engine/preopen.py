"""PRE-OPEN BIAS BRAIN (R0.5, the demand after −$854 / −$3,928): Wave
must know what to trade FROM THE FIRST TRADE, not pay tuition first. The
efficacy guard (R1.5) corrects mid-day; this is the missing front half — a
verdict issued once, ~9:25 ET, from PRE-MARKET evidence only:

    LONG_BIAS / SHORT_BIAS / NEUTRAL  + confidence + reasons

Evidence (all knowable before the bell, read from scanner2's live arrays by
the monitor): SPY/QQQ pre-market last print vs prior close, and the universe
gap map — the fraction of Wave's watched names gapping down vs their prior
close.

Rules v1 (the strict AND form — the thresholds the validation study tested):
  SHORT_BIAS: market gap <= −GAP_THRESHOLD_PCT AND red map
              (gap-down fraction >= MAP_RED_FRAC)
  LONG_BIAS:  mirrored
  NEUTRAL:    everything else — no pre-open edge claimed; the efficacy
              guard discovers the paying side intraday.

THE STUDY'S HONEST VERDICT (15 sessions
09-01..09-24): pre-open prediction is WEAK — Rules v1 fired 4 directional
calls and hit ZERO; the relaxed variant fired 9 and also hit zero. Most
mornings mean-revert into chop by 11:00 (9/15), including both big red-gap
mornings. Per the R0.5 brief this module therefore ships CONSERVATIVE:
  - the strict AND rule only (silent most days),
  - a bias NEVER hard-blocks an entry — under SHORT_BIAS a momentum LONG
    merely needs the name GREEN vs its OWN prior close (real relative
    strength — a monster gapper bucking the tape passes; mirrored for
    LONG_BIAS); shorts trade unchanged, and vice versa,
  - the main teeth are the EFFICACY SEED: the bias hands the R1.5 tracker
    a bias_hint, so the FIRST counter-bias fail flips the book (1 fail
    instead of 2) — the guard corrects a wrong morning minutes faster
    without pre-committing capital to a 0-for-4 signal.
Re-derive the thresholds through §11 when the menu-day span doubles.

Lifecycle: computed once per ET day inside COMPUTE_WINDOW_ET (~9:25); the
verdict logs loudly, pushes to the phone (klass="risk" at the monitor), and
journals one `day_regime` row with the verdict prefixed ``PREOPEN_``. It
governs from 9:30 until the Day Judge publishes its first non-UNCLEAR
verdict of the day (note_day_judge → retired; the live verdict + efficacy
own the day from there). Pure and injectable — no sockets, no Qt, no
Database; the M0 journal_db_cb pattern, like DayJudge and EfficacyTracker.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

logger = logging.getLogger("wave.scanner.preopen")  # prefix → SCANNER category

# -- verdicts -----------------------------------------------------------------
LONG_BIAS = "LONG_BIAS"
SHORT_BIAS = "SHORT_BIAS"
NEUTRAL = "NEUTRAL"

# -- Rules v1 thresholds (validated — honestly, NOT validated: the study went
# -- 0/4 directional; these are the strict form kept deliberately quiet.
# -- §8.3 discipline applies before any loosening) ----------------------------
GAP_THRESHOLD_PCT = 0.30  # |SPY/QQQ mean gap %| that counts as directional
MAP_RED_FRAC = 0.60  # >= this fraction of the menu gapping down = red map
MAP_GREEN_FRAC = 0.40  # <= this = green map
MIN_MAP_N = 10  # fewer watched names than this = no map, no verdict
COMPUTE_WINDOW_ET = ((9, 25), (9, 30))  # [from, to) — one shot before the bell
CONF_CAP = 0.90  # a 0-for-4 study never earns 1.0


class PreOpenBias:
    """One pre-open verdict per ET day. The monitor computes it (~9:25),
    the entry pipeline reads ``bias``/``confidence`` while ``governs()``,
    and the Day Judge's first real verdict retires it (note_day_judge)."""

    def __init__(self, journal_db_cb: Callable[[str, dict], Any] | None = None) -> None:
        self._journal_db_cb = journal_db_cb
        self.bias: str = NEUTRAL
        self.confidence: float = 0.0
        self.reasons: str = ""
        self.computed_day: str | None = None
        self.retired = False

    # -- lifecycle ------------------------------------------------------------

    def should_compute(self, now_et: datetime) -> bool:
        """True exactly while inside the pre-open window on a day that has
        no verdict yet. A late start (first tick after 9:30) computes
        nothing — a stale gap map is not pre-open evidence."""
        if self.computed_day == now_et.date().isoformat():
            return False
        return COMPUTE_WINDOW_ET[0] <= (now_et.hour, now_et.minute) < COMPUTE_WINDOW_ET[1]

    def compute(
        self,
        now_et: datetime,
        spy_gap_pct: float | None,
        qqq_gap_pct: float | None,
        gap_down_frac: float | None,
        n_map: int = 0,
    ) -> tuple[str, float]:
        """The one verdict. Missing evidence (no index print, thin map)
        reads NEUTRAL — the conservative default, never a guess."""
        self.computed_day = now_et.date().isoformat()
        self.retired = False
        gaps = [g for g in (spy_gap_pct, qqq_gap_pct) if g is not None]
        mkt_gap = sum(gaps) / len(gaps) if gaps else None
        map_ok = gap_down_frac is not None and n_map >= MIN_MAP_N

        bias = NEUTRAL
        if mkt_gap is not None and map_ok:
            if mkt_gap <= -GAP_THRESHOLD_PCT and gap_down_frac >= MAP_RED_FRAC:
                bias = SHORT_BIAS
            elif mkt_gap >= GAP_THRESHOLD_PCT and gap_down_frac <= MAP_GREEN_FRAC:
                bias = LONG_BIAS
        self.bias = bias
        self.confidence = self._confidence(bias, mkt_gap, gap_down_frac)
        self.reasons = self._reasons(spy_gap_pct, gap_down_frac, n_map, map_ok)
        logger.info("PRE-OPEN BIAS: %s (conf %.2f) — %s", bias, self.confidence, self.reasons)
        self._journal(spy_gap_pct, qqq_gap_pct, mkt_gap, gap_down_frac, n_map)
        return bias, self.confidence

    def note_day_judge(self, verdict: str | None) -> bool:
        """The handoff: the Day Judge's first non-UNCLEAR verdict of the day
        retires the pre-open bias for good (the live verdict + efficacy own
        the day). Returns True exactly once — at the moment of retirement."""
        if self.retired or self.computed_day is None:
            return False
        v = str(getattr(verdict, "value", verdict) or "").strip().upper()
        if v in ("", "UNCLEAR"):
            return False
        self.retired = True
        if self.bias != NEUTRAL:
            logger.info(
                "PRE-OPEN BIAS retired — the Day Judge owns the day (%s)",
                v,
            )
        return True

    # -- reads ----------------------------------------------------------------

    def governs(self, day_iso: str) -> bool:
        """True while this verdict should steer entries: computed for THIS
        ET day, directional, and not yet handed off to the Day Judge."""
        return self.computed_day == day_iso and not self.retired and self.bias != NEUTRAL

    def hint(self) -> str | None:
        """The efficacy seed: 'SHORT' / 'LONG' for the R1.5 tracker's
        bias_hint, None on NEUTRAL."""
        return {SHORT_BIAS: "SHORT", LONG_BIAS: "LONG"}.get(self.bias)

    def headline(self) -> str:
        """The 9:25 phone/log line."""
        if self.bias == SHORT_BIAS:
            return f"PRE-OPEN: SHORT bias ({self.reasons}) — shorts full, longs strict."
        if self.bias == LONG_BIAS:
            return f"PRE-OPEN: LONG bias ({self.reasons}) — longs full, shorts strict."
        return f"PRE-OPEN: NEUTRAL ({self.reasons}) — no pre-open edge, efficacy discovers."

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _confidence(bias: str, mkt_gap: float | None, frac_down: float | None) -> float:
        if bias == NEUTRAL or mkt_gap is None or frac_down is None:
            return 0.0
        gap_extra = min(1.0, max(0.0, (abs(mkt_gap) - GAP_THRESHOLD_PCT) / 0.7))
        one_sided = frac_down if bias == SHORT_BIAS else 1.0 - frac_down
        map_extra = min(1.0, max(0.0, (one_sided - MAP_RED_FRAC) / (1.0 - MAP_RED_FRAC)))
        return round(min(CONF_CAP, 0.5 + 0.2 * gap_extra + 0.2 * map_extra), 2)

    @staticmethod
    def _reasons(spy_gap: float | None, frac_down: float | None, n_map: int, map_ok: bool) -> str:
        spy_txt = f"SPY {spy_gap:+.1f}%" if spy_gap is not None else "SPY gap unknown"
        if not map_ok:
            return f"{spy_txt}, gap map unknown ({n_map} names)"
        pct_down = round(frac_down * 100)
        map_txt = (
            f"{pct_down}% of names gapping down"
            if frac_down >= 0.5
            else f"{100 - pct_down}% of names gapping up"
        )
        return f"{spy_txt}, {map_txt}"

    def _journal(
        self,
        spy_gap: float | None,
        qqq_gap: float | None,
        mkt_gap: float | None,
        frac_down: float | None,
        n_map: int,
    ) -> None:
        if self._journal_db_cb is None:
            return
        try:
            from waveapp.persistence.db import utc_now

            rnd = lambda v: round(v, 3) if v is not None else None  # noqa: E731
            self._journal_db_cb(
                "day_regime",
                {
                    "ts": utc_now(),
                    "verdict": f"PREOPEN_{self.bias}",
                    "confidence": round(self.confidence, 3),
                    "breadth": rnd(frac_down),
                    "evidence": json.dumps(
                        {
                            "why": self.reasons,
                            "spy_gap_pct": rnd(spy_gap),
                            "qqq_gap_pct": rnd(qqq_gap),
                            "mkt_gap_pct": rnd(mkt_gap),
                            "gap_down_frac": rnd(frac_down),
                            "n_map": n_map,
                            "study": "R0.5 STUDY.md: 0/4 directional over 15d — conservative",
                        }
                    ),
                },
            )
        except Exception:  # a broken journal must never wound the verdict
            logger.debug("preopen day_regime journal failed", exc_info=True)
