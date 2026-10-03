"""SessionScheduler (SPEC.md §6): the trading week as regimes.

PRE (04:00–09:30 ET) → OPEN_DRIVE (09:30–10:30) → MIDDAY (10:30–15:00, with
the 11:30–13:30 lull flagged) → POWER_HOUR (15:00–16:00) → POST (16:00–20:00)
→ OVERNIGHT (20:00–04:00; Sunday 20:00 opens the week) → CLOSED (Friday 20:00
→ Sunday 20:00, holidays).

DST is handled by computing in America/New_York wall time (zoneinfo). Every
strategy/exit/risk parameter set is keyed by these regimes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

logger = logging.getLogger("wave.engine.session")

ET = ZoneInfo("America/New_York")
_fallback_reported = False

# Primary holiday source: the XNYS calendar from `exchange_calendars` — rules
# computed for all years (holidays AND early closes), no yearly updates needed
# (2026-08-14). The hardcoded set below is only a fallback
# if the library ever fails to load.
_CALENDAR_YEARS_AHEAD = 5
_calendar = None
_day_cache: dict[date, tuple[bool, time | None]] = {}


def _get_calendar():
    global _calendar  # noqa: PLW0603
    if _calendar is None:
        import exchange_calendars as xcals

        today = datetime.now(UTC).date()
        _calendar = xcals.get_calendar(
            "XNYS",
            start="2020-01-01",
            end=f"{today.year + _CALENDAR_YEARS_AHEAD}-12-31",
        )
    return _calendar


def day_info(day: date) -> tuple[bool, time | None]:
    """(is_trading_day, early_close_ET_time or None) from the XNYS calendar,
    cached per date; falls back to the static holiday set on any failure."""
    cached = _day_cache.get(day)
    if cached is not None:
        return cached
    try:
        calendar = _get_calendar()
        iso = day.isoformat()
        if not calendar.is_session(iso):
            result: tuple[bool, time | None] = (False, None)
        else:
            close_et = calendar.session_close(iso).tz_convert(ET)
            early = close_et.time() if close_et.time() < time(16, 0) else None
            result = (True, early)
    except Exception:  # library unavailable/out of range → static fallback
        global _fallback_reported  # noqa: PLW0603
        if not _fallback_reported:
            _fallback_reported = True
            logger.exception(
                "XNYS calendar library FAILED for %s — using the static fallback "
                "holiday list (2026-27 only). Fix before trading near holidays!",
                day,
            )
        trading = day.weekday() < 5 and day not in MARKET_HOLIDAYS
        result = (trading, None)
    _day_cache[day] = result
    return result


# Fallback only — the XNYS calendar above is authoritative.
MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    }
)


class Regime(Enum):
    PRE = "pre"
    OPEN_DRIVE = "open_drive"
    MIDDAY = "midday"
    POWER_HOUR = "power_hour"
    POST = "post"
    OVERNIGHT = "overnight"
    CLOSED = "closed"


@dataclass(frozen=True)
class SessionInfo:
    regime: Regime
    is_lull: bool  # the 11:30–13:30 dead zone inside MIDDAY
    et_time: datetime


class SessionScheduler:
    @staticmethod
    def info(now_utc: datetime | None = None) -> SessionInfo:
        now = (now_utc or datetime.now(UTC)).astimezone(ET)
        regime = SessionScheduler._regime(now)
        lull = regime is Regime.MIDDAY and time(11, 30) <= now.time() < time(13, 30)
        return SessionInfo(regime=regime, is_lull=lull, et_time=now)

    @staticmethod
    def regime(now_utc: datetime | None = None) -> Regime:
        return SessionScheduler.info(now_utc).regime

    @staticmethod
    def _regime(now_et: datetime) -> Regime:
        weekday = now_et.weekday()  # Mon=0 .. Sun=6
        t = now_et.time()

        # the weekend dead zone: Friday 20:00 ET → Sunday 20:00 ET
        if weekday == 5:  # Saturday
            return Regime.CLOSED
        if weekday == 6:  # Sunday
            return Regime.OVERNIGHT if t >= time(20, 0) else Regime.CLOSED
        if weekday == 4 and t >= time(20, 0):  # Friday night
            return Regime.CLOSED

        # calendar-driven: holidays close the day session (04:00–20:00);
        # overnight resumes at 20:00 (Friday handled above)
        trading, early_close = day_info(now_et.date())
        if not trading and time(4, 0) <= t < time(20, 0):
            return Regime.CLOSED

        if t < time(4, 0):
            return Regime.OVERNIGHT
        if t < time(9, 30):
            return Regime.PRE
        if t < time(10, 30):
            return Regime.OPEN_DRIVE

        # early-close days (e.g. Black Friday, 13:00 ET): the last RTH hour is
        # the power hour, and the afternoon after the close is POST
        if early_close is not None:
            power_start = (datetime.combine(now_et.date(), early_close) - timedelta(hours=1)).time()
            if t < power_start:
                return Regime.MIDDAY
            if t < early_close:
                return Regime.POWER_HOUR
            if t < time(20, 0):
                return Regime.POST
            return Regime.OVERNIGHT

        if t < time(15, 0):
            return Regime.MIDDAY
        if t < time(16, 0):
            return Regime.POWER_HOUR
        if t < time(20, 0):
            return Regime.POST
        return Regime.OVERNIGHT

    @staticmethod
    def next_closed_boundary(now_utc: datetime | None = None) -> datetime:
        """The next moment the market enters CLOSED (weekend or holiday day
        session end) — the §8.2 layer-7 flatten deadline. Returned in UTC.

        The probe is floored to whole minutes so the boundary is a FIXED
        wall-clock moment — countdowns tick instead of drifting with the
        caller's seconds (8.8 r3 bug: '…59s' forever)."""
        now = (now_utc or datetime.now(UTC)).astimezone(ET)
        probe = now.replace(second=0, microsecond=0)
        for _ in range(10 * 24 * 60):  # scan forward, minute resolution, ≤10 days
            probe = probe + timedelta(minutes=1)
            if SessionScheduler._regime(probe) is Regime.CLOSED:
                return probe.astimezone(UTC)
        raise RuntimeError("no CLOSED boundary found in 10 days — calendar bug")

    @staticmethod
    def next_open_boundary(now_utc: datetime | None = None) -> datetime:
        """When CLOSED ends — the moment the new week (or post-holiday
        session) opens (2026-08-22: the weekend card counted to 'the next
        closed minute', i.e. forever one minute away)."""
        now = (now_utc or datetime.now(UTC)).astimezone(ET)
        probe = now.replace(second=0, microsecond=0)
        for _ in range(10 * 24 * 60):
            probe = probe + timedelta(minutes=1)
            if SessionScheduler._regime(probe) is not Regime.CLOSED:
                return probe.astimezone(UTC)
        raise RuntimeError("no open boundary found in 10 days — calendar bug")

    @staticmethod
    def minutes_to_rth_close(now_utc: datetime | None = None) -> float | None:
        """Minutes until TODAY's 16:00 ET close (early-close aware), or None
        outside regular hours. Drives the daily flatten (2026-08-19: bracket
        stops are DAY orders — they EXPIRE at the bell, so any position held
        past close sits overnight unprotected; overnight is unvalidated §6)."""
        now = (now_utc or datetime.now(UTC)).astimezone(ET)
        today = now.date()
        trading, early = day_info(today)
        if not trading or today.weekday() >= 5:
            return None
        close = datetime.combine(today, early or time(16, 0), tzinfo=ET)
        open_ = datetime.combine(today, time(9, 30), tzinfo=ET)
        if not (open_ <= now < close):
            return None
        return (close - now).total_seconds() / 60.0

    @staticmethod
    def is_rth(now_utc: datetime | None = None) -> bool:
        """True inside TODAY's regular trading hours (09:30 ET → the close,
        early-close aware). §3: MARKET orders are legal only here — at any
        other moment an immediate flatten must go as a marketable LIMIT with
        extended_hours, or it QUEUES until the next open (audit A1-1/A1-12:
        a 07:30 kill switch left positions naked for hours)."""
        return SessionScheduler.minutes_to_rth_close(now_utc) is not None

    @staticmethod
    def session_date(now_utc: datetime | None = None) -> date:
        """The ET calendar date — what 'today' means for the risk engine's
        day baselines (audit A3-7: in a continuous 24/5 run the baselines
        never rolled because on_session_start's only caller was engine
        start; the roll check keys on THIS date)."""
        return (now_utc or datetime.now(UTC)).astimezone(ET).date()

    # -- the System tab's market clock (8.8 r3) -----------------------------

    @staticmethod
    def market_clock(now_utc: datetime | None = None) -> dict:
        """Daily RTH clock: what the next everyday event is and when.

        Returns {mode: "opens"|"closes", target: UTC datetime, anchor: UTC
        datetime} — anchor is when the current phase began, so a UI can draw
        the elapsed fraction as a ring."""
        now = (now_utc or datetime.now(UTC)).astimezone(ET)

        def rth_open(day: date) -> datetime:
            return datetime.combine(day, time(9, 30), tzinfo=ET)

        def rth_close(day: date) -> datetime:
            _trading, early = day_info(day)
            return datetime.combine(day, early or time(16, 0), tzinfo=ET)

        today = now.date()
        trading_today, _early = day_info(today)
        in_rth = trading_today and today.weekday() < 5 and rth_open(today) <= now < rth_close(today)
        if in_rth:
            return {
                "mode": "closes",
                "target": rth_close(today).astimezone(UTC),
                "anchor": rth_open(today).astimezone(UTC),
            }
        # next open: today if it hasn't happened yet, else walk forward
        probe = today
        if not (trading_today and probe.weekday() < 5 and now < rth_open(probe)):
            probe = probe + timedelta(days=1)
            for _ in range(10):
                trading, _e = day_info(probe)
                if trading and probe.weekday() < 5:
                    break
                probe = probe + timedelta(days=1)
        # anchor: the previous trading day's close
        back = probe - timedelta(days=1)
        for _ in range(10):
            trading, _e = day_info(back)
            if trading and back.weekday() < 5:
                break
            back = back - timedelta(days=1)
        return {
            "mode": "opens",
            "target": rth_open(probe).astimezone(UTC),
            "anchor": rth_close(back).astimezone(UTC),
        }

    @staticmethod
    def next_closed_info(now_utc: datetime | None = None) -> tuple[datetime, str | None]:
        """(next CLOSED boundary UTC, holiday name or None). None = a plain
        weekend; a name means the coming closure includes that market
        holiday (8.8 r3 — 'say holiday and which holiday')."""
        boundary = SessionScheduler.next_closed_boundary(now_utc)
        try:
            calendar = _get_calendar()
            start = boundary.astimezone(ET).date()
            # the boundary's own date is usually still a session (the closure
            # starts that NIGHT) — scan the days inside the closure after it
            window = [start + timedelta(days=offset) for offset in range(1, 8)]
            names = calendar.regular_holidays.holidays(
                start.isoformat(), window[-1].isoformat(), return_name=True
            )
            named = {stamp.date(): str(name) for stamp, name in names.items()}
            for day in window:
                trading, _early = day_info(day)
                if trading:
                    break  # the closure ends at the next session — stop looking
                if day in named:
                    return boundary, named[day]
        except Exception:
            logger.debug("holiday name lookup failed", exc_info=True)
        return boundary, None
