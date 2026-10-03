"""DataHub (§3, Phase 3): live market data over Alpaca's websocket
(IEX feed), 1-minute bar building from raw trades, and the heartbeat/staleness
watchdog the RiskEngine will consume in Phase 5 (§12: stale feed → freeze
entries, rely on server-side stops).

BarBuilder and HeartbeatWatchdog are pure (injectable clocks, no sockets) so
they are fully unit-testable; DataHub wires them to the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from waveapp.security import secrets

logger = logging.getLogger("wave.data")

_ET_ZONE = ZoneInfo("America/New_York")  # the 9:30 VWAP anchor (fidelity fix)

BAR_HISTORY = 500  # ~1 trading day of 1-min bars per symbol
# Alpaca Basic (IEX) counts CHANNEL-symbol pairs, ~30 total — not symbols.
# The old 28-SYMBOL cap blew the real quota at ~7 symbols and spammed
# "symbol limit exceeded (405)" errors (found in the log, 8.8 r5).
# channel-subscription budgets by feed tier (Phase 10.1): free IEX enforces
# ~30 channel-symbol pairs (the 405 bug of 8.8); paid SIP is effectively
# unlimited — capped generously so a runaway scanner still can't grow forever
SUBSCRIPTION_LIMITS = {"iex": 30, "sip": 2000}
SUBSCRIPTION_LIMIT = SUBSCRIPTION_LIMITS["iex"]  # back-compat default
BASE_CHANNELS = 4  # watchlist: trades + quotes + bars + trading statuses
DYNAMIC_CHANNELS = 3  # scanner candidates skip statuses until a position opens


@dataclass
class Bar:
    symbol: str
    start: datetime  # UTC, floored to the minute
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap_notional: float = 0.0  # running sum(price*size) for bar VWAP

    @property
    def vwap(self) -> float:
        return self.vwap_notional / self.volume if self.volume else self.close


class BarBuilder:
    """Aggregates trades into 1-minute bars. `on_bar` fires when a bar closes
    (i.e. when the first trade of the NEXT minute arrives)."""

    def __init__(self, on_bar: Callable[[Bar], None] | None = None) -> None:
        self._on_bar = on_bar
        self._current: dict[str, Bar] = {}
        self.history: dict[str, deque[Bar]] = {}
        self._session_vwap: dict[str, tuple[Any, float, float]] = {}

    def session_vwap(self, symbol: str) -> float | None:
        state = self._session_vwap.get(symbol)
        if state is None or not state[2]:
            return None
        return state[1] / state[2]

    def _fold_session_vwap(self, symbol: str, et_ts: datetime, price: float, size: float) -> None:
        """Fold one print (or a seeded bar's typical price) into the
        9:30-anchored session-VWAP accumulator.

        A4-4 (audit 2026-09-22): the accumulator is keyed by the ET session
        date, NOT the UTC date. Under UTC keying, a post-market print after
        the UTC-midnight boundary (19:00 ET in winter/EST, 20:00 ET in
        summer/EDT) flipped the key and RESET the accumulator mid-POST — a
        position held into the evening read a minutes-old "session VWAP" in
        the momentum/recross exit — and, because such evening prints pass the
        >=9:30 wall-clock gate, they polluted the NEXT day's session VWAP
        (live via on_trade, and via seed_history's early-morning 12-hour
        backfill reaching past 20:00 ET). ET keying pins every print to its
        own trading day, and prints from a session OLDER than the
        accumulator's are dropped, so a stale/backfilled evening print can
        never reset or contaminate today's VWAP.
        """
        if (et_ts.hour, et_ts.minute) < (9, 30):
            return  # 9:30 anchor (fidelity fix 2026-08-28): pre-open prints don't count
        session_date = et_ts.date()  # ET date == the trading session's date
        state = self._session_vwap.get(symbol)
        if state is not None and state[0] > session_date:
            return  # print from a PRIOR session — never reset backward
        if state is None or state[0] != session_date:
            state = (session_date, 0.0, 0.0)  # a new session opens the accumulator
        self._session_vwap[symbol] = (
            session_date,
            state[1] + price * size,
            state[2] + size,
        )

    @staticmethod
    def _floor_minute(ts: datetime) -> datetime:
        return ts.astimezone(UTC).replace(second=0, microsecond=0)

    def on_trade(self, symbol: str, price: float, size: float, ts: datetime) -> None:
        # running SESSION vwap per symbol — ANCHORED AT THE 9:30 OPEN
        # (fidelity fix 2026-08-28: the Phase 10 simulator replays RTH bars
        # only, so ALL the adoption evidence was earned with a 9:30-anchored
        # VWAP. Live accumulated from 04:00 pre-market and drifted from the
        # validated design — on gap days the poisoned anchor blinded the
        # recross exit for hours, the CRM shape). Pre-9:30 trades still build
        # bars; they just don't move the engine's VWAP. Keying/anchor rules
        # live in _fold_session_vwap (A4-4: ET-date key, not UTC-date).
        self._fold_session_vwap(symbol, ts.astimezone(_ET_ZONE), price, size)
        minute = self._floor_minute(ts)
        bar = self._current.get(symbol)
        if bar is not None and minute > bar.start:
            self._close_bar(bar)
            bar = None
        elif bar is not None and minute < bar.start:
            # A4-10 (audit 2026-09-22): an out-of-order EARLIER-minute trade
            # used to REPLACE the in-progress bar (newer partial lost, stale
            # bar closed out of order into history — one corrupted ATR
            # input). Fold the late tick into the current bar instead, the
            # same rule the kitchen's SecondBarAggregator uses.
            minute = bar.start
        if bar is None or minute != bar.start:
            self._current[symbol] = Bar(
                symbol=symbol,
                start=minute,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=size,
                vwap_notional=price * size,
            )
            return
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        bar.volume += size
        bar.vwap_notional += price * size

    def _close_bar(self, bar: Bar) -> None:
        self.history.setdefault(bar.symbol, deque(maxlen=BAR_HISTORY)).append(bar)
        if self._on_bar is not None:
            self._on_bar(bar)

    def bars(self, symbol: str) -> list[Bar]:
        return list(self.history.get(symbol, ()))

    def seed_history(self, symbol: str, bars: list[Bar]) -> int:
        """Prepend REST-fetched history OLDER than what the stream has built
        (2026-08-20): a freshly-subscribed position symbol has <15 live bars,
        so compute_atr returned None and the exit brain seeded a stop-distance
        fallback ATR 10–20× too big — the trail sat above the stop and never
        ratcheted for the first ~15 minutes of EVERY position. Live bars win
        on overlap. Returns how many bars were added."""
        history = self.history.setdefault(symbol, deque(maxlen=BAR_HISTORY))
        cutoff = history[0].start if history else None
        older = sorted(
            (b for b in bars if cutoff is None or b.start < cutoff),
            key=lambda b: b.start,
        )
        if not older:
            return 0
        # 2026-08-26 (AIQ/DKS premature "VWAP recross" after a restart): the
        # session-VWAP accumulator also restarts at zero, so 3 minutes later
        # "the day's VWAP" was really a 3-minute average sitting on the price
        # and the momentum exit hair-triggered. Fold the seeded bars in —
        # they are exactly the pre-restart span on_trade never saw. Same
        # anchor/keying rules as on_trade (_fold_session_vwap, A4-4): bars
        # from a prior ET session in the backfill never touch today's VWAP.
        for bar in older:
            typical = (bar.high + bar.low + bar.close) / 3.0
            self._fold_session_vwap(symbol, bar.start.astimezone(_ET_ZONE), typical, bar.volume)
        merged = older + list(history)
        history.clear()
        history.extend(merged[-BAR_HISTORY:])
        return len(older)


@dataclass
class HeartbeatWatchdog:
    """Tracks the age of the last message per channel; reports staleness
    transitions exactly once per state change.

    Thresholds are PER CHANNEL: 1-minute bars naturally arrive ~60s apart, so
    they get a much longer threshold than the continuous trades/quotes flow
    (a uniform 30s threshold false-alarmed every minute — 2026-08-14)."""

    threshold_seconds: float = 30.0  # default for unlisted channels
    channel_thresholds: dict[str, float] = field(
        default_factory=lambda: {"trades": 30.0, "quotes": 30.0, "bars": 180.0}
    )
    time_fn: Callable[[], float] = time.monotonic
    on_stale_change: Callable[[str, bool], None] | None = None
    _last_beat: dict[str, float] = field(default_factory=dict)
    _stale: dict[str, bool] = field(default_factory=dict)

    def _threshold(self, channel: str) -> float:
        return self.channel_thresholds.get(channel, self.threshold_seconds)

    def beat(self, channel: str) -> None:
        self._last_beat[channel] = self.time_fn()
        if self._stale.get(channel):
            self._stale[channel] = False
            if self.on_stale_change:
                self.on_stale_change(channel, False)

    def age(self, channel: str) -> float | None:
        beat = self._last_beat.get(channel)
        return None if beat is None else self.time_fn() - beat

    def check(self) -> dict[str, bool]:
        """Evaluate all channels; fire callbacks on fresh→stale transitions."""
        result: dict[str, bool] = {}
        for channel in self._last_beat:
            age = self.age(channel)
            stale = age is not None and age > self._threshold(channel)
            result[channel] = stale
            if stale and not self._stale.get(channel):
                self._stale[channel] = True
                if self.on_stale_change:
                    self.on_stale_change(channel, True)
        return result


class DataHub:
    """Owns the market-data websocket. UI and engine observe via callbacks."""

    def __init__(
        self,
        watchlist: list[str],
        on_bar: Callable[[Bar], None] | None = None,
        heartbeat_threshold: float = 30.0,
        on_trading_status: Callable[[str, bool], None] | None = None,
        feed: str = "iex",
    ) -> None:
        self.watchlist = [s.upper() for s in watchlist]
        self.feed = feed if feed in SUBSCRIPTION_LIMITS else "iex"
        self.subscription_limit = SUBSCRIPTION_LIMITS[self.feed]
        self.bar_builder = BarBuilder(on_bar=on_bar)
        self.watchdog = HeartbeatWatchdog(threshold_seconds=heartbeat_threshold)
        self.latest_quotes: dict[str, Any] = {}
        self.latest_trade_px: dict[str, float] = {}  # freshest print (lively cards)
        self.latest_trade_ts: dict[str, Any] = {}  # its timestamp (print vs quote race)
        self.bar_tap: Any = None  # Scanner 2.0 full-market ingest (Architecture B)
        self.status_tap: Any = None  # Scanner 2.0 halt/resume events
        self.trade_tap: Any = None  # Master Key shadow: per-trade tick feed
        self._stream: Any = None
        self._stream_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._on_trading_status = on_trading_status
        self._watched: set[str] = set(self.watchlist)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream

        from waveapp.broker.alpaca import KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET

        key_id = secrets.get_secret(KEYCHAIN_PAPER_KEY_ID)
        secret = secrets.get_secret(KEYCHAIN_PAPER_SECRET)
        if not key_id or not secret:
            raise RuntimeError("Alpaca keys missing from Keychain — cannot start DataHub")

        stream_feed = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
        self._stream = StockDataStream(key_id, secret, feed=stream_feed)
        # PRIME THE WATCHDOG (audit 2026-09-16: check() only iterates
        # channels that beat at least once — a stream that connects but
        # never delivers showed GREEN forever and never froze entries.
        # Priming makes silence-from-birth count as staleness.)
        for _ch in ("trades", "quotes", "bars"):
            self.watchdog.beat(_ch)
        symbols = self.watchlist
        self._stream.subscribe_trades(self._on_trade, *symbols)
        self._stream.subscribe_quotes(self._on_quote, *symbols)
        # Scanner 2.0 Architecture B (2026-09-01, decision): on SIP the
        # bars channel runs WILDCARD — every symbol's minute bar (~200 msg/s,
        # ~10k burst at each minute close) feeds the full-market scanner via
        # bar_tap. Watched-symbol bars are still built locally from trades,
        # so the wildcard serves the scanner exclusively. IEX (30-sub cap)
        # keeps the per-symbol form.
        if self.feed == "sip":
            self._stream.subscribe_bars(self._on_stream_bar, "*")
            with contextlib.suppress(Exception):
                self._stream.subscribe_updated_bars(self._on_stream_bar, "*")
        else:
            self._stream.subscribe_bars(self._on_stream_bar, *symbols)
        with contextlib.suppress(Exception):  # not all feeds carry statuses
            if self.feed == "sip":
                self._stream.subscribe_trading_statuses(self._on_status_msg, "*")
            else:
                self._stream.subscribe_trading_statuses(self._on_status_msg, *symbols)
        self._stream_task = asyncio.ensure_future(self._run_stream())
        self._watchdog_task = asyncio.ensure_future(self._run_watchdog())
        logger.info("DataHub started for %s", ", ".join(symbols))

    async def _run_stream(self) -> None:
        # LIFE INSURANCE (audit 2026-09-16 build-before-open: the SDK's
        # _run_forever can RETURN cleanly on a terminal condition — e.g.
        # "insufficient subscription" — and nothing rebuilt this task;
        # a silent return meant no live quotes/trades until a full
        # reconnect that only a REST failure could trigger).
        backoff = 1.0
        while True:
            try:
                await self._stream._run_forever()
                logger.error(
                    "market-data stream ENDED (no exception) — rebuilding in %.0fs", backoff
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("market-data stream died — rebuilding in %.0fs", backoff)
            with contextlib.suppress(Exception):
                await self._stream.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _run_watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            self.watchdog.check()

    async def stop(self) -> None:
        for task in (self._stream_task, self._watchdog_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._stream_task = self._watchdog_task = None
        if self._stream is not None:
            with contextlib.suppress(Exception):
                await self._stream.stop_ws()
            self._stream = None
        logger.info("DataHub stopped")

    @property
    def is_running(self) -> bool:
        return self._stream_task is not None and not self._stream_task.done()

    def session_vwap(self, symbol: str) -> float | None:
        return self.bar_builder.session_vwap(symbol)

    @property
    def watched(self) -> set[str]:
        return set(self._watched)

    async def watch(self, symbols: list[str]) -> list[str]:
        """Dynamically subscribe scanner candidates (Phase 7.2). Respects the
        websocket budget; returns the symbols actually added.

        The SDK's subscribe_* on a RUNNING stream blocks until the event loop
        services the change — calling it from the loop itself deadlocks the
        whole app (froze the UI on 2026-08-14). So the blocking calls run on
        a worker thread, with a hard timeout."""
        added: list[str] = []
        base = set(self.watchlist)
        for symbol in symbols:
            symbol = symbol.upper()
            if symbol in self._watched:
                continue
            dynamic_count = len(self._watched - base)  # `added` already counted
            used = len(base) * BASE_CHANNELS + dynamic_count * DYNAMIC_CHANNELS
            if used + DYNAMIC_CHANNELS > self.subscription_limit:
                logger.warning(
                    "stream budget full (%d/%d channel subs) — not watching %s",
                    used,
                    self.subscription_limit,
                    symbol,
                )
                break
            self._watched.add(symbol)
            added.append(symbol)
        if added and self._stream is not None:

            def _subscribe_all() -> None:
                for symbol in added:
                    with contextlib.suppress(Exception):
                        # dynamic candidates: NO trading-statuses channel — it
                        # would blow the Basic plan's subscription quota (405)
                        self._stream.subscribe_trades(self._on_trade, symbol)
                        self._stream.subscribe_quotes(self._on_quote, symbol)
                        self._stream.subscribe_bars(self._on_stream_bar, symbol)

            try:
                await asyncio.wait_for(asyncio.to_thread(_subscribe_all), timeout=15)
            except TimeoutError:
                logger.error("dynamic subscribe timed out — stream may need a restart")
        if added:
            logger.info("now watching %s (total %d)", ", ".join(added), len(self._watched))
        return added

    def last_message_age(self) -> float | None:
        ages = [a for c in ("trades", "quotes", "bars") if (a := self.watchdog.age(c)) is not None]
        return min(ages) if ages else None

    # -- stream handlers ----------------------------------------------------

    async def _on_trade(self, trade: Any) -> None:
        self.watchdog.beat("trades")
        # lively cards (2026-09-16): the freshest real PRINT per symbol
        # + its timestamp, so the card can prefer the QUOTE when the book
        # moved after the last print (thin names print seconds apart)
        with contextlib.suppress(Exception):
            self.latest_trade_px[str(trade.symbol)] = float(trade.price)
            self.latest_trade_ts[str(trade.symbol)] = trade.timestamp
        try:
            self.bar_builder.on_trade(
                symbol=str(trade.symbol),
                price=float(trade.price),
                size=float(trade.size),
                ts=trade.timestamp,
            )
        except Exception:
            logger.exception("bad trade tick")
        # Master Key shadow tap — hot path, swallow everything (same contract
        # as bar_tap: the tap can never wound the stream)
        tap = self.trade_tap
        if tap is not None:
            try:
                tap(str(trade.symbol), float(trade.price), float(trade.size), trade.timestamp)
            except Exception:  # noqa: BLE001, S110 — hot path, swallow
                pass

    async def _on_quote(self, quote: Any) -> None:
        self.watchdog.beat("quotes")
        self.latest_quotes[str(quote.symbol)] = quote

    async def _on_stream_bar(self, bar: Any) -> None:
        self.watchdog.beat("bars")
        # Architecture B: forward EVERY bar to the full-market scanner. The
        # tap must be allocation-light and can never wound the stream.
        tap = self.bar_tap
        if tap is not None:
            try:
                tap(bar)
            except Exception:  # noqa: BLE001, S110 — hot path, swallow
                pass

    async def _on_status_msg(self, status: Any) -> None:
        """Trading-status messages drive the LULD handling (§12)."""
        self.classify_and_forward_status(status)

    def classify_and_forward_status(self, status: Any) -> None:
        symbol = str(getattr(status, "symbol", "") or "").upper()
        if not symbol:
            return
        code = str(getattr(status, "status_code", "") or "").upper()
        message = str(getattr(status, "status_message", "") or "").lower()
        logger.warning("trading status %s: code=%s msg=%s", symbol, code, message)
        halted: bool | None = None
        if code in ("H", "P", "V") or "halt" in message or "pause" in message:
            halted = True
        elif code in ("T", "Q") or "resum" in message:
            halted = False
        if halted is None:
            return
        if self._on_trading_status is not None:
            self._on_trading_status(symbol, halted)
        tap = self.status_tap  # scanner2 halt/resume events (Architecture B)
        if tap is not None:
            with contextlib.suppress(Exception):
                tap(symbol, halted)
