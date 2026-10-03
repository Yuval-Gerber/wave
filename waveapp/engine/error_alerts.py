"""Error surfacing (2026-08-14): ANY error logged anywhere
in Wave — every phase, every module — must reach him visibly.

A logging.Handler on the root logger catches every record at ERROR level or
above and fans it out to Telegram (via a thread-safe scheduler) and the UI
status bar. Deduped per message for `min_interval_seconds` so an error storm
becomes one alert, not a phone full of spam. Telegram's own loggers are
excluded (a failing push must not create an alert loop).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

_EXCLUDED_PREFIXES = ("wave.telegram", "telegram", "httpx", "httpcore")

# Self-healing reconnection noise (woken at 4:00 twice, 2026-08-25 and
# 2026-08-28): these patterns fire while the resurrection loops are ALREADY
# fixing the problem. They stay in the log but page nobody — unless the same
# storm keeps raging past GRACE_SECONDS, which means it is NOT healing and
# ONE aggregated alert goes out.
_TRANSIENT_PATTERNS = (
    "error during websocket communication",
    "websocket error, restarting",
)
GRACE_SECONDS = 300.0


class ErrorAlertHandler(logging.Handler):
    def __init__(
        self,
        alert: Callable[[str], None],  # thread-safe; receives the alert text
        min_interval_seconds: float = 600.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self._alert = alert
        self._min_interval = min_interval_seconds
        self._time_fn = time_fn
        # per-message: [last_alert_ts, current_interval, repeats_since_alert]
        # (2026-09-02: a stuck "scan cycle failed" re-alerted every 10 min all
        # night ≈ 33 pings. Repeats now ESCALATE the quiet window ×3 up to
        # BACKOFF_CAP — a persistent error pages ~4-5 times a night, not 33.)
        self._recent: dict[str, list[float]] = {}
        self._storm_started: float | None = None  # first transient in a storm
        self._storm_count = 0
        self._storm_alerted = False

    BACKOFF_FACTOR = 3.0
    BACKOFF_CAP = 3.0 * 3600.0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_EXCLUDED_PREFIXES):
                return
            message = record.getMessage()[:300]
            now = self._time_fn()
            if any(pattern in message for pattern in _TRANSIENT_PATTERNS):
                if self._storm_started is None or now - self._storm_started > GRACE_SECONDS * 2:
                    # a NEW storm (the previous one healed long ago)
                    self._storm_started = now
                    self._storm_count = 0
                    self._storm_alerted = False
                self._storm_count += 1
                if not self._storm_alerted and now - self._storm_started > GRACE_SECONDS:
                    self._storm_alerted = True
                    self._alert(
                        f"⚠️ CONNECTION NOT HEALING — {self._storm_count} reconnect "
                        f"errors in {int(now - self._storm_started)}s and still failing"
                    )
                return  # healing noise stays in the log, pages nobody
            entry = self._recent.get(message)
            if entry is not None:
                last, interval, repeats = entry
                if now - last < interval:
                    entry[2] += 1  # suppressed — counted for the next alert
                    return
                # still recurring after the quiet window → alert with the
                # tally and TRIPLE the next window (capped)
                new_interval = min(interval * self.BACKOFF_FACTOR, self.BACKOFF_CAP)
                self._recent[message] = [now, new_interval, 0]
                self._alert(
                    f"⚠️ ERROR (still happening — {repeats + 1}× since last alert;"
                    f" next update in ≥{int(new_interval / 60)} min) — "
                    f"{record.name}\n{message}"
                )
                return
            self._recent[message] = [now, self._min_interval, 0]
            if len(self._recent) > 500:  # keep the dedupe map bounded
                cutoff = now - self.BACKOFF_CAP
                self._recent = {m: e for m, e in self._recent.items() if e[0] > cutoff}
            self._alert(f"⚠️ ERROR — {record.name}\n{message}")
        except Exception:  # noqa: S110 — alerting must never crash the app
            pass
