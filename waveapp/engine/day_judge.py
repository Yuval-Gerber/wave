"""DAY JUDGE — the day-type detector (R0, 2026-09-23).

Born on the −$854 chop day where every breakout failed BOTH directions:
Wave must know, live, what KIND of day it is. This module is the pure,
injectable verdict machine — no sockets, no Qt, no Database. The monitor
feeds it once per minute at the scanner2 minute boundary with the SAME
breadth number the menu-breadth recorder writes to
research/menu_breadth/<date>.csv (fraction of today's scanner menu
trading below its own open), and it publishes one of four verdicts:

    TREND_UP / TREND_DOWN / CHOP / UNCLEAR   + confidence 0..1

Rules v1 (transparent; every knob is a module-top constant):
- breadth extreme (>= TREND_BREADTH_DOWN below open) for TREND_PERSIST_MIN+
  minutes and not receding = TREND_DOWN; mirrored around 0.5 for TREND_UP.
- breadth living inside the CHOP_BAND for CHOP_PERSIST_MIN+ minutes with
  low net drift = CHOP.
- anything else = UNCLEAR. Before WARMUP_END_ET (10:00 ET) the published
  verdict is ALWAYS UNCLEAR — the first half hour lies.
- hysteresis: the published verdict flips only after HYSTERESIS_MIN
  consecutive minutes of the same new raw verdict (streaks accrue during
  warmup, so a trend that built through 9:40-10:00 can publish at 10:00).

Every published change journals one line to the DB log (SCANNER category
via the wave.scanner.* logger prefix) and one `day_regime` row through the
injected journal_db_cb (migration 013) — the M0 journal_db_cb pattern.

ROADMAP (v2, documented not built): the update() `counters` dict is the
socket for richer evidence — signals gated/entered this minute and a
failed-breakout counter (entries whose symbol printed back through the
entry price within N minutes). Tonight the monitor passes breadth + the
menu's median |day %| only; counters land in the evidence JSON verbatim
the day the entry pipeline starts counting them.

The entry-routing consumer (a parallel workstream) reads ONLY the
`verdict` / `confidence` attributes off the monitor's instance.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

# -- Rules v1 constants (tunable; §8.3 discipline applies before any of these
# -- gates real entries — R0 is observation + a badge) ------------------------
WARMUP_END_ET = (10, 0)  # before 10:00 ET the published verdict is UNCLEAR
TREND_BREADTH_DOWN = 0.65  # frac below open at/above this = downside extreme
TREND_BREADTH_UP = 0.35  # mirror: at/below this = upside extreme
TREND_PERSIST_MIN = 20  # minutes the extreme must hold for a raw TREND
DRIFT_LOOKBACK_MIN = 20  # net-drift window (now vs ~20 min ago)
TREND_DRIFT_TOL = 0.05  # a trend may ease this much over the lookback
CHOP_BAND = (0.40, 0.60)  # breadth oscillation band
CHOP_PERSIST_MIN = 20  # minutes inside the band for a raw CHOP
CHOP_MAX_DRIFT = 0.08  # |net drift| ceiling for CHOP
HYSTERESIS_MIN = 5  # consecutive raw minutes before the verdict flips
HISTORY_MAX_MIN = 420  # samples kept (a full 9:30-16:00 session + slack)

logger = logging.getLogger("wave.scanner.day_judge")  # prefix → SCANNER


class DayVerdict(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    CHOP = "CHOP"
    UNCLEAR = "UNCLEAR"


class DayJudge:
    """Pure minute-fed day-type detector. Feed update() once per minute;
    read .verdict / .confidence any time (attribute reads only — that is
    the whole contract with the routing consumer)."""

    def __init__(self, journal_db_cb: Callable[[str, dict], Any] | None = None) -> None:
        self._journal_db_cb = journal_db_cb
        self._samples: deque[tuple[datetime, float]] = deque(maxlen=HISTORY_MAX_MIN)
        self._day: str | None = None
        self.verdict: DayVerdict = DayVerdict.UNCLEAR
        self.confidence: float = 0.0
        self._pending_raw: DayVerdict = DayVerdict.UNCLEAR
        self._pending_streak = 0

    # -- feed -----------------------------------------------------------------

    def update(
        self,
        now_et: datetime,
        breadth: float | None,
        counters: dict | None = None,
        median_abs_day_pct: float | None = None,
    ) -> tuple[DayVerdict, float]:
        """One minute of evidence. `breadth` is the menu fraction below its
        own open (None = no menu yet → sample skipped, verdict unchanged).
        Returns the PUBLISHED (verdict, confidence)."""
        day = now_et.date().isoformat()
        if day != self._day:
            self._day = day
            self._samples.clear()
            self._set(DayVerdict.UNCLEAR, 0.0, breadth, counters, quiet=True)
            self._pending_raw = DayVerdict.UNCLEAR
            self._pending_streak = 0
        if breadth is None:
            return self.verdict, self.confidence
        breadth = min(1.0, max(0.0, float(breadth)))
        self._samples.append((now_et, breadth))

        raw, conf = self._raw_verdict(breadth)
        # hysteresis streak (accrues even during warmup, publish waits)
        if raw == self._pending_raw:
            self._pending_streak += 1
        else:
            self._pending_raw = raw
            self._pending_streak = 1

        warmup = (now_et.hour, now_et.minute) < WARMUP_END_ET
        if warmup:
            if self.verdict is not DayVerdict.UNCLEAR:  # defensive; day reset covers it
                self._set(DayVerdict.UNCLEAR, 0.0, breadth, counters)
            return self.verdict, self.confidence

        if raw == self.verdict:
            self.confidence = conf  # same verdict — confidence tracks the tape
        elif self._pending_streak >= HYSTERESIS_MIN:
            self._set(raw, conf, breadth, counters, median_abs_day_pct)
        return self.verdict, self.confidence

    def snapshot(self) -> dict:
        """What travels to the Scanner tab badge and the System snapshot."""
        return {"verdict": self.verdict.value, "confidence": round(self.confidence, 2)}

    # -- rules ----------------------------------------------------------------

    def _raw_verdict(self, breadth: float) -> tuple[DayVerdict, float]:
        down_streak = self._trailing_streak(lambda b: b >= TREND_BREADTH_DOWN)
        up_streak = self._trailing_streak(lambda b: b <= TREND_BREADTH_UP)
        band_streak = self._trailing_streak(lambda b: CHOP_BAND[0] <= b <= CHOP_BAND[1])
        drift = self._drift()

        if down_streak >= TREND_PERSIST_MIN and (drift is None or drift >= -TREND_DRIFT_TOL):
            extremity = min(1.0, (breadth - TREND_BREADTH_DOWN) / (1.0 - TREND_BREADTH_DOWN))
            return DayVerdict.TREND_DOWN, self._trend_conf(extremity, down_streak)
        if up_streak >= TREND_PERSIST_MIN and (drift is None or drift <= TREND_DRIFT_TOL):
            extremity = min(1.0, (TREND_BREADTH_UP - breadth) / TREND_BREADTH_UP)
            return DayVerdict.TREND_UP, self._trend_conf(extremity, up_streak)
        if band_streak >= CHOP_PERSIST_MIN and drift is not None and abs(drift) <= CHOP_MAX_DRIFT:
            settled = min(1.0, (band_streak - CHOP_PERSIST_MIN) / 60.0)
            drift_calm = (CHOP_MAX_DRIFT - abs(drift)) / CHOP_MAX_DRIFT
            return DayVerdict.CHOP, round(min(1.0, 0.5 + 0.3 * settled + 0.2 * drift_calm), 2)
        return DayVerdict.UNCLEAR, 0.0

    @staticmethod
    def _trend_conf(extremity: float, streak: int) -> float:
        persistence = min(1.0, (streak - TREND_PERSIST_MIN) / 60.0)
        return round(min(1.0, 0.5 + 0.3 * max(0.0, extremity) + 0.2 * persistence), 2)

    def _trailing_streak(self, keep: Callable[[float], bool]) -> int:
        streak = 0
        for _ts, b in reversed(self._samples):
            if not keep(b):
                break
            streak += 1
        return streak

    def _drift(self) -> float | None:
        """breadth now minus breadth ~DRIFT_LOOKBACK_MIN samples ago (samples
        arrive once per minute at the scanner2 boundary; missed minutes make
        the lookback conservatively longer, never shorter)."""
        if len(self._samples) <= DRIFT_LOOKBACK_MIN:
            return None
        return self._samples[-1][1] - self._samples[-1 - DRIFT_LOOKBACK_MIN][1]

    # -- journal --------------------------------------------------------------

    def _set(
        self,
        verdict: DayVerdict,
        confidence: float,
        breadth: float | None,
        counters: dict | None = None,
        median_abs_day_pct: float | None = None,
        quiet: bool = False,
    ) -> None:
        changed = verdict is not self.verdict
        self.verdict = verdict
        self.confidence = confidence
        if quiet or not changed:
            return
        why = self._why(verdict, breadth)
        logger.info("DAY JUDGE: %s (conf %.1f) — %s", verdict.value, confidence, why)
        if self._journal_db_cb is None:
            return
        try:
            from waveapp.persistence.db import utc_now

            evidence: dict[str, Any] = {
                "why": why,
                "drift": self._drift(),
                "n_samples": len(self._samples),
            }
            if median_abs_day_pct is not None:
                evidence["median_abs_day_pct"] = round(float(median_abs_day_pct), 3)
            if counters:
                evidence["counters"] = dict(counters)  # v2 roadmap socket
            self._journal_db_cb(
                "day_regime",
                {
                    "ts": utc_now(),
                    "verdict": verdict.value,
                    "confidence": round(confidence, 3),
                    "breadth": round(breadth, 3) if breadth is not None else None,
                    "evidence": json.dumps(evidence),
                },
            )
        except Exception:  # a broken journal must never wound the judge
            logger.debug("day_regime journal failed", exc_info=True)

    def _why(self, verdict: DayVerdict, breadth: float | None) -> str:
        b = f"{breadth:.2f}" if breadth is not None else "?"
        drift = self._drift()
        d = f"{drift:+.2f}" if drift is not None else "n/a"
        if verdict is DayVerdict.TREND_DOWN:
            streak = self._trailing_streak(lambda x: x >= TREND_BREADTH_DOWN)
            return f"breadth {b} below-open for {streak} min, drift {d}"
        if verdict is DayVerdict.TREND_UP:
            streak = self._trailing_streak(lambda x: x <= TREND_BREADTH_UP)
            return f"breadth {b} (menu above open) for {streak} min, drift {d}"
        if verdict is DayVerdict.CHOP:
            return f"breadth {b} oscillating in {CHOP_BAND[0]:.2f}-{CHOP_BAND[1]:.2f}, drift {d}"
        return f"breadth {b} — no regime holds"
