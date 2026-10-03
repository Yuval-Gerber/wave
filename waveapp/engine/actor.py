"""PositionActor (§3, §8): one independent asyncio task per
position, owning its lifecycle. State machine:

    PENDING_ENTRY → OPEN → SCALING_OUT → CLOSING → CLOSED | HALTED | ERROR

Hard rule 3: every position always has a server-side stop resting at the
broker — the entry is a BRACKET (entry + stop-loss leg), so the broker holds
the stop from the moment the entry fills, even if Wave crashes.

Phase 5 scope: bracket entry, fill tracking, protective stop verification,
close-now, and clean terminal states. The adaptive exit layers (§8.2) plug
into this actor in Phase 6.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import Enum

from waveapp.broker.base import (
    BrokerAdapter,
    OrderRequest,
    OrderSide,
    OrderType,
    StopLoss,
    TimeInForce,
    TradeUpdate,
    TradingMode,
)
from waveapp.engine.exits import ExitAction, ExitDecision, ExitEngine
from waveapp.engine.orders import (
    OrderAction,
    make_client_order_id,
    new_position_uuid,
    parse_client_order_id,
)

logger = logging.getLogger("wave.trade.actor")


class PositionState(Enum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    SCALING_OUT = "scaling_out"
    CLOSING = "closing"
    CLOSED = "closed"
    HALTED = "halted"
    ERROR = "error"


TERMINAL_STATES = frozenset({PositionState.CLOSED, PositionState.ERROR})

# audit A1-1 + A1-12: outside RTH a MARKET/DAY order QUEUES at the broker
# until the next 09:30 open — every "flatten now" path (kill switch, session
# flatten before 20:00, judge closes, momentum exits) was silently creating
# HOURS of queued, unmanaged exposure. Outside RTH an immediate exit goes as
# a marketable LIMIT instead: through the far touch by this buffer, with
# extended_hours=True (§3: market orders only in RTH).
FLATTEN_LIMIT_BUFFER_PCT = 0.5


def marketable_exit_fields(
    exit_side: OrderSide,
    symbol: str,
    fallback_price: float | None,
    now_utc=None,
) -> dict:
    """How to flatten RIGHT NOW, per the clock — shared by EngineCore.kill()
    and PositionActor.close_now (audit A1-1 + A1-12), so the two paths can
    never drift apart.

    Inside RTH → plain MARKET (unchanged historical behavior). Outside RTH →
    marketable LIMIT + extended_hours=True: limit through the far touch by
    FLATTEN_LIMIT_BUFFER_PCT so it executes immediately against the resting
    book instead of queuing as a market order until the next open.

    Price source, best first:
    1. the live quote's far touch via PositionActor.quote_source (bid for a
       sell, ask for a buy-to-cover) — the true marketable reference;
    2. the caller's fallback_price (broker position current_price, i.e. the
       last trade, or the entry price) — conservative: it is not the touch,
       so on overnight's structurally wide spreads the buffered limit may
       land inside the spread and REST as an aggressive limit for a moment;
       still executable and still ours to manage, never a queued market.
    Extended-hours eligibility is per asset; if the broker refuses the
    order, the callers' existing failure paths re-place the protective stop
    (never strand naked).

    Returns OrderRequest kwargs: order_type, limit_price, extended_hours.
    """
    from waveapp.engine.session import SessionScheduler

    if SessionScheduler.is_rth(now_utc):
        return {"order_type": OrderType.MARKET, "limit_price": None, "extended_hours": False}
    reference: float | None = None
    source = PositionActor.quote_source
    if source is not None:
        try:
            quote = source(symbol)
            touch = getattr(
                quote, "bid_price" if exit_side is OrderSide.SELL else "ask_price", None
            )
            if touch and float(touch) > 0:
                reference = float(touch)
        except Exception:
            reference = None
    if reference is None and fallback_price and float(fallback_price) > 0:
        reference = float(fallback_price)
    if reference is None:
        # no price at all — should be unreachable (every filled position has
        # an entry price and the broker payload carries current_price), but a
        # limit cannot be priced without one. A queued MARKET at the next
        # open still beats NO exit order at all; say so loudly.
        logger.error(
            "[%s] no reference price for the outside-RTH flatten — "
            "falling back to MARKET (it will QUEUE until the open)",
            symbol,
        )
        return {"order_type": OrderType.MARKET, "limit_price": None, "extended_hours": False}
    import math as _math

    buffer = FLATTEN_LIMIT_BUFFER_PCT / 100.0
    if exit_side is OrderSide.SELL:
        limit = _math.floor(reference * (1.0 - buffer) * 100.0) / 100.0
    else:
        limit = _math.ceil(reference * (1.0 + buffer) * 100.0) / 100.0
    return {
        "order_type": OrderType.LIMIT,
        "limit_price": max(limit, 0.01),
        "extended_hours": True,
    }


@dataclass(frozen=True)
class PositionSpec:
    symbol: str
    side: OrderSide  # BUY = long entry, SELL = short entry
    qty: float
    stop_price: float  # server-side hard stop (mandatory — hard rule 3)
    entry_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    entry_stop_price: float | None = None  # trigger for STOP entries (10.7)
    strategy: str = "TEST"
    extended_hours: bool = False
    # the price at DECISION time (option A, 2026-08-24): fill − decision =
    # realized entry slippage, the §11.2 calibration measurement
    decision_price: float | None = None
    # opening-auction entry (PMOM, 2026-08-25): queue with tif=OPG before the
    # bell so the fill IS the 9:30:00 opening print. Alpaca refuses bracket
    # legs on auction orders, so the server-side stop is submitted the moment
    # the auction fill event arrives (hard rule 3's earliest possible instant)
    auction_open: bool = False
    # ENTRY LADDER (blueprint 6.5, ADOPTED on adopted 2026-09-02 — 4/4
    # windows under BOTH the optimistic and the strict trade-through fill
    # models): the entry posts as a LIMIT at the submit-time mid
    # (limit_price) with the bracket attached; if it hasn't filled within
    # LADDER_TIMEOUT_SECONDS the actor cancels and FALLS BACK to a plain
    # MARKET entry — the pre-adoption behavior, loudly logged. The
    # protective stop rides the bracket in both paths (hard rule 3).
    # Which brain manages the exits (2026-09-21, the duel's verdict): the
    # Position Judge runs every position; "kitchen" survives only for
    # historical rows and the shadow journals.
    manager: str = "judge"
    ladder: bool = False
    # LADDER-SKIP (2026-09-16, the climber chase autopsy: market fallbacks
    # filled 0.3–0.7% above the signal price and opened instantly red —
    # NICE paid $179 of pure chase, MFP $183; the one mid-captured entry
    # lost a quarter of the chased ones): when True, an unfilled ladder
    # NEVER falls back to MARKET — the entry is abandoned. A missed
    # climber costs nothing; a chased one starts −0.6%.
    ladder_skip_ok: bool = False


class PositionActor:
    def __init__(
        self,
        spec: PositionSpec,
        adapter: BrokerAdapter,
        mode: TradingMode,
        on_event=None,  # callable(actor, text) for UI/Telegram fan-out
        on_trade=None,  # callable(dict) — the Phase 8.3 flip-board ticker
    ) -> None:
        if spec.stop_price <= 0:
            raise ValueError("a position without a stop price is forbidden (hard rule 3)")
        self.uuid = new_position_uuid()
        self.spec = spec
        self.adapter = adapter
        self.mode = mode
        self.state = PositionState.PENDING_ENTRY
        self.entry_order_id: str | None = None
        self.stop_leg_order_id: str | None = None
        self.exit_engine: ExitEngine | None = None  # attached by EngineCore (§8.2)
        self.filled_qty = 0.0
        self.avg_entry_price: float | None = None
        self.realized_pnl = 0.0  # accumulated over scale-outs + the close
        self.exit_reason: str | None = None
        self.adopted_entry_time = None  # set only by adopt() — original open time
        self.entry_filled_at = None  # UTC fill moment (chart entry marker)
        # Alpaca trade updates report CUMULATIVE filled_qty per order —
        # realized P&L must use the DELTA since the last update, or partial
        # fills double-count (found 2026-08-22: week ledger −$655 vs equity)
        self._filled_seen: dict[str, float] = {}
        self._updates: asyncio.Queue[TradeUpdate] = asyncio.Queue()
        self._on_event = on_event
        self._on_trade = on_trade
        self._task: asyncio.Task | None = None
        self._attempt = 0
        # entry-ladder state (inert unless spec.ladder)
        self._ladder_task: asyncio.Task | None = None
        self._ladder_repegs = 0  # fresh-mid re-posts used (cap: LADDER_REPEG_MAX)
        # last broker-CONFIRMED stop level — the rollback anchor for failed
        # amends (audit 2026-09-16); seeded by the bracket at entry
        self._confirmed_stop: float | None = spec.stop_price
        # reentrancy latch: rule-3 emergency flatten must not recurse through
        # close_now → re-protect → rule 3 when the broker refuses everything
        self._rule3_flatten_active = False
        self._repeg_limit_price: float | None = None  # spec is frozen — override here
        self._ladder_escalating = False
        self._entry_type_override: OrderType | None = None
        # GRML 2026-09-22 (−$843, the SECOND hole): once a halt or an engine
        # stop abandons a PENDING entry, NO machinery may ever submit a new
        # entry order for this actor — the canceled-event handler checks this
        # flag BEFORE any re-peg or MARKET fallback.
        self._entry_abandoned = False
        # audit A1-9: close_now's exit order id — without it a canceled/
        # expired exit order stranded the actor in CLOSING forever (no code
        # path knew which order to watch)
        self.exit_order_id: str | None = None
        # audit A1-7: the in-flight scale sale's order id (an async-rejected
        # scale must be matched strictly, like the stop/exit ids above)
        self.scale_order_id: str | None = None
        # audit A1-7: rejection events already acted on — a duplicate echo
        # must never rollback the ledger or re-protect a second time
        self._handled_rejects: set[str] = set()
        # audit A1-7: consecutive async stop rejections — after the second,
        # hard rule 3 says the position must not exist (flatten)
        self._stop_reject_count = 0
        # audit A1-3: the stop leg died (canceled/expired) DURING a LULD halt
        # — §12 forbids order traffic into a halt, so exit_halt re-protects
        # the moment the halt lifts. Task ref is strong (GC class 2026-08-19).
        self._reprotect_after_halt = False
        self._reprotect_task: asyncio.Task | None = None
        # set by EngineCore.kill() BEFORE it cancels every open order: the
        # dead-stop watchdog must not fight the kill switch by re-protecting
        # each deliberately-canceled stop while the flatten is in flight
        self._kill_flatten_active = False

    # ladder observability (2026-09-02: fallbacks must be visible)
    # — the monitor installs a callable(kind, symbol) with
    # kind ∈ {"posted", "captured", "fallback"}; counters land on the System
    # tab. A hook failure can never wound the actor.
    ladder_event_hook = None
    LADDER_TIMEOUT_SECONDS: float = 45.0  # ≈ the sim's next-bar resolution
    # LADDER RE-PEG (2026-09-16 + the n=191 study: one fresh-mid
    # re-post before the MARKET chase recovered ~$204 of $219 fallback cost
    # with zero fills lost, worst single case −$42): after the first timeout,
    # re-post ONCE at the current mid for a short window, THEN go market.
    # quote_source is installed by the monitor (symbol → latest quote or
    # None); without it the re-peg silently skips = the old behavior.
    LADDER_REPEG_SECONDS: float = 15.0
    LADDER_REPEG_MAX: int = 1
    quote_source = None

    def _ladder_note(self, kind: str) -> None:
        hook = PositionActor.ladder_event_hook
        if hook is not None:
            with contextlib.suppress(Exception):
                hook(kind, self.spec.symbol)

    # -- identity ------------------------------------------------------------

    @property
    def position_key(self) -> str:
        return self.uuid[:12]

    def owns_order(self, client_order_id: str) -> bool:
        return client_order_id.startswith(f"wave-{self.position_key}-")

    # -- events from the engine's router ------------------------------------

    def deliver(self, update: TradeUpdate) -> None:
        self._updates.put_nowait(update)

    def _emit(self, text: str) -> None:
        logger.info("[%s %s] %s", self.spec.symbol, self.position_key, text)
        if self._on_event is not None:
            self._on_event(self, text)

    def _fill_delta(self, order) -> float:
        """NEW shares in this update: cumulative filled_qty minus what this
        order already reported (partial-fill double-count fix)."""
        seen = self._filled_seen.get(order.order_id, 0.0)
        cumulative = float(order.filled_qty or 0)
        self._filled_seen[order.order_id] = max(seen, cumulative)
        return max(0.0, cumulative - seen)

    def _realized(self, qty: float, price: float | None) -> float | None:
        """P&L of qty shares exiting at price vs the average entry."""
        if not price or not self.avg_entry_price:
            return None
        direction = 1.0 if self.spec.side is OrderSide.BUY else -1.0
        return (price - self.avg_entry_price) * qty * direction

    def _fire_trade(self, action: str, qty: float, price: float | None) -> None:
        """Feed the top-bar flip board. Must NEVER break trading."""
        if self._on_trade is None or not price:
            return
        is_long = self.spec.side is OrderSide.BUY
        pnl = None if action == "entry" else self._realized(qty, price)
        try:
            self._on_trade(
                {
                    "action": action,
                    "symbol": self.spec.symbol,
                    "long": is_long,
                    "qty": qty,
                    "price": price,
                    "pnl": pnl,
                }
            )
        except Exception:
            logger.exception("on_trade callback failed (UI only — trading unaffected)")

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def adopt(
        cls,
        spec: PositionSpec,
        adapter: BrokerAdapter,
        mode: TradingMode,
        *,
        filled_qty: float,
        avg_entry_price: float,
        stop_leg_order_id: str | None,
        on_event=None,
        on_trade=None,
        adopted_entry_time=None,
    ) -> PositionActor:
        """Re-attach to a position that survived an app restart (2026-08-19):
        the broker still holds the shares and the server-side stop; the new
        process builds an actor around them instead of freezing forever.
        No entry is submitted — the actor starts directly in OPEN.

        adopted_entry_time: the ORIGINAL opened_at recovered from the DB —
        keeps the t_max time-stop clock honest across restarts (the GDX
        quirk: the 90-min clock restarted at adoption). Best-effort: after
        chained restarts it points at the previous adoption, still far
        closer to the truth than 'now'."""
        actor = cls(spec, adapter, mode, on_event=on_event, on_trade=on_trade)
        actor.state = PositionState.OPEN
        actor.filled_qty = filled_qty
        actor.avg_entry_price = avg_entry_price
        actor.stop_leg_order_id = stop_leg_order_id
        actor.adopted_entry_time = adopted_entry_time
        return actor

    def start(self) -> asyncio.Task:
        self._task = asyncio.ensure_future(self._run())
        return self._task

    # Set by the app's quit path BEFORE draining tasks (2026-08-19): during
    # shutdown the protective check is meaningless — quitting never touches
    # the broker-side stop (it keeps protecting the position; hard rule 3),
    # and its network call fails mid-teardown, spamming false error alerts.
    app_shutting_down: bool = False

    async def _run(self) -> None:
        checked = False
        try:
            if self.state is PositionState.PENDING_ENTRY:
                await self._submit_entry()
            await self._event_loop()
        except asyncio.CancelledError:
            # crash/cancel → the broker still holds the stop; verify it exists
            if not PositionActor.app_shutting_down:
                await self.protective_check()
                checked = True
            raise
        except Exception:
            logger.exception("[%s] actor crashed", self.spec.symbol)
            self.state = PositionState.ERROR
        finally:
            # audit A1-7: EVERY path out of the actor confirms the broker
            # still holds a stop when shares remain — including the CLEAN
            # event-loop exit (a terminal state landed by an event), which
            # the old code never checked: an async-rejected order that
            # ERRORed the actor with shares held slipped out unverified.
            # A genuinely CLOSED actor is flat; nothing to verify.
            if (
                not checked
                and not PositionActor.app_shutting_down
                and (self.filled_qty or 0) > 0
                and self.state is not PositionState.CLOSED
            ):
                await self.protective_check()

    def _abandon_entry(self) -> None:
        """GRML 2026-09-22 (−$843): a LULD halt (or a close/stop command) on a
        PENDING_ENTRY actor must kill the WHOLE entry attempt, not just the
        resting order. Canceling alone left the ladder armed: the canceled
        event re-pegged a LIMIT at the frozen pre-halt mid, or fell back to
        MARKET — submitting a fresh order INTO the halt that filled the
        resume dump. Latch the abandon flag and disarm the ladder; both
        orderings (halt-then-timeout, timeout-then-halt) end here."""
        self._entry_abandoned = True
        self._ladder_escalating = False
        if self._ladder_task is not None and not self._ladder_task.done():
            self._ladder_task.cancel()

    async def _submit_entry(self) -> None:
        if self._entry_abandoned:
            # belt-and-braces: no path may post a fresh entry after abandon
            if self.state is PositionState.PENDING_ENTRY and (self.filled_qty or 0) == 0:
                self.state = PositionState.CLOSED
                self.exit_reason = self.exit_reason or "entry abandoned"
            self._emit("entry abandoned (halt/stop) — submit suppressed")
            return
        self._attempt += 1
        auction = self.spec.auction_open
        entry_type = self._entry_type_override or self.spec.entry_type
        request = OrderRequest(
            symbol=self.spec.symbol,
            qty=self.spec.qty,
            side=self.spec.side,
            order_type=entry_type,
            time_in_force=TimeInForce.OPG if auction else TimeInForce.DAY,
            client_order_id=make_client_order_id(self.uuid, OrderAction.ENTRY, self._attempt),
            limit_price=(
                (self._repeg_limit_price or self.spec.limit_price)
                if entry_type is OrderType.LIMIT
                else None
            ),
            stop_price=self.spec.entry_stop_price,  # STOP entries: buy AT the level
            # auction orders can't carry a bracket — the stop follows the fill
            stop_loss=None if auction else StopLoss(stop_price=self.spec.stop_price),
            extended_hours=self.spec.extended_hours,
        )
        info = await self.adapter.submit_order(self.mode, request)
        self.entry_order_id = info.order_id
        # remember the bracket's stop leg: exits must cancel it first, because
        # the broker holds the shares against it
        for leg in info.legs:
            if leg.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
                self.stop_leg_order_id = leg.order_id
        self._emit(
            f"{'opening-auction' if auction else 'bracket'} entry submitted: "
            f"{self.spec.side.value} {self.spec.qty:g} "
            f"@ {entry_type.value}, stop {self.spec.stop_price}"
        )
        # 6.5 entry ladder: arm the one-shot escalation timer on the FIRST
        # (mid-posted LIMIT) attempt only
        if self.spec.ladder and self._attempt == 1 and entry_type is OrderType.LIMIT:
            self._ladder_note("posted")
            self._ladder_task = asyncio.ensure_future(
                self._ladder_watch(PositionActor.LADDER_TIMEOUT_SECONDS)
            )

    def _fresh_mid(self) -> float | None:
        """Current quote mid rounded toward passivity, or None when no sane
        quote is available (then the re-peg skips and market fires)."""
        source = PositionActor.quote_source
        if source is None:
            return None
        try:
            quote = source(self.spec.symbol)
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
        except Exception:
            return None
        if bid <= 0 or ask <= bid or (ask - bid) > 0.02 * ask:  # busted-quote guard
            return None
        import math as _math

        mid = (bid + ask) / 2.0
        rounded = (
            _math.floor(mid * 100.0) / 100.0
            if self.spec.side is OrderSide.BUY
            else _math.ceil(mid * 100.0) / 100.0
        )
        return rounded if rounded > 0 else None

    async def _ladder_watch(self, timeout: float) -> None:
        """One-shot: if the posted entry hasn't filled at all within the
        timeout, cancel it — the canceled event re-pegs once at the fresh
        mid, then falls back to a MARKET entry. A partial fill stands down
        (the existing partial-cancel path protects real shares; we never
        chase the remainder)."""
        await asyncio.sleep(timeout)
        if (
            self.state is not PositionState.PENDING_ENTRY
            or (self.filled_qty or 0) > 0
            or self._entry_abandoned
        ):
            return  # filled, dead, or abandoned (halt/stop) — nothing to escalate
        self._ladder_escalating = True
        self._emit(f"LADDER timeout ({timeout:g}s at mid {self.spec.limit_price}) — escalating")
        try:
            await self.adapter.cancel_order(self.mode, self.entry_order_id)
        except Exception:
            # cancel raced a fill — the fill event will land normally
            logger.info("[%s] ladder cancel raced the fill", self.spec.symbol)
            self._ladder_escalating = False

    async def _event_loop(self) -> None:
        while self.state not in TERMINAL_STATES:
            update = await self._updates.get()
            await self._handle(update)

    async def _handle(self, update: TradeUpdate) -> None:
        event = update.event
        order = update.order
        parsed = parse_client_order_id(order.client_order_id)
        if event in ("fill", "partial_fill"):
            is_entry = order.order_id == self.entry_order_id or (
                parsed is not None and parsed.action == OrderAction.ENTRY
            )
            if is_entry:
                self.filled_qty = order.filled_qty
                self.avg_entry_price = order.filled_avg_price
                if event == "fill":
                    from datetime import UTC as _utc
                    from datetime import datetime as _dt

                    self.entry_filled_at = _dt.now(_utc)
                    self.state = PositionState.OPEN
                    if self.spec.ladder:
                        self._ladder_escalating = False
                        if order.order_type is OrderType.LIMIT:
                            self._ladder_note("captured")
                            self._emit(
                                f"LADDER captured the mid: filled @ "
                                f"{order.filled_avg_price} (spread saved)"
                            )
                    if self.spec.auction_open and self.stop_leg_order_id is None:
                        # auction entry (no bracket allowed): the protective
                        # stop goes up NOW, before anything else happens.
                        # Bracket entries NEVER take this path — their stop leg
                        # is the broker's, and a missing leg there is caught by
                        # protective_check, not by double-submitting
                        await self._place_protective_stop()
                    self._emit(
                        f"entry filled: {order.filled_qty:g} @ {order.filled_avg_price} "
                        f"— server-side stop resting at {self.spec.stop_price}"
                    )
                    self._fire_trade("entry", order.filled_qty, order.filled_avg_price)
            elif parsed is not None and parsed.action == OrderAction.SCALE:
                # partial profit banked; the rest keeps running at breakeven+
                if event == "fill":
                    self.scale_order_id = None  # A1-7: sale landed, id retired
                self.realized_pnl += (
                    self._realized(self._fill_delta(order), order.filled_avg_price) or 0
                )
                self._emit(
                    f"scale-out filled: {order.filled_qty:g} @ {order.filled_avg_price} "
                    f"(banked; remainder trailing)"
                )
                self._fire_trade("scale", order.filled_qty, order.filled_avg_price)
            else:  # stop leg or full exit filling
                self.realized_pnl += (
                    self._realized(self._fill_delta(order), order.filled_avg_price) or 0
                )
                # 2026-08-22 (the −$655 ledger gap): a PARTIAL exit fill must
                # NOT close the actor — the event loop would stop and the
                # remaining partials' P&L was silently dropped. Close only on
                # the terminal "fill" event.
                if event != "fill":
                    return
                self.state = PositionState.CLOSED
                self.exit_reason = self.exit_reason or (
                    "stop hit" if order.order_type is OrderType.STOP else "exit filled"
                )
                # WIN/LOSS verdict in the log (2026-08-20)
                verdict = "WIN" if self.realized_pnl > 0 else "LOSS"
                self._emit(
                    f"closed ({self.exit_reason}) @ {order.filled_avg_price} — "
                    f"{verdict} {self.realized_pnl:+,.2f} $"
                )
                self._fire_trade("close", order.filled_qty, order.filled_avg_price)
        elif event in ("canceled", "expired"):
            if self.state is PositionState.PENDING_ENTRY:
                # AXTI 2026-09-02 (opening bell): canceling a bracket makes
                # Alpaca emit TWO cancel events — the entry AND its stop leg.
                # The ladder consumed the first and re-entered at MARKET; the
                # stale leg event arrived 1ms later and CLOSED the actor,
                # orphaning the filled position from exit management. A
                # cancel only counts if it is for the CURRENT entry attempt.
                is_current_entry = order.order_id == self.entry_order_id or (
                    parsed is not None
                    and parsed.action == OrderAction.ENTRY
                    and parsed.attempt == self._attempt
                )
                if not is_current_entry:
                    logger.info(
                        "[%s] ignoring stale %s event for %s (current entry: %s)",
                        self.spec.symbol,
                        event,
                        order.order_id,
                        self.entry_order_id,
                    )
                    return
                if self._entry_abandoned and (self.filled_qty or 0) == 0:
                    # GRML 2026-09-22: the entry was abandoned by a halt or a
                    # close/stop command — the cancel confirmation must NEVER
                    # re-peg or fall back to MARKET into the halt. Checked
                    # BEFORE the ladder branch so both orderings (halt before
                    # or after the ladder timeout's own cancel) land here.
                    # A PARTIAL fill skips this and takes the reopen path
                    # below: real shares get the protective stop (rule 3).
                    self._ladder_escalating = False
                    self.state = PositionState.CLOSED
                    self.exit_reason = self.exit_reason or "entry abandoned"
                    self._emit("entry abandoned (halt/stop) — no re-entry, no resume chase")
                    return
                if self._ladder_escalating and (self.filled_qty or 0) == 0:
                    self._ladder_escalating = False
                    # RE-PEG leg (2026-09-16): one fresh-mid re-post before
                    # the market chase — the studied middle step (+$204 of
                    # $219 fallback cost recovered, zero fills lost).
                    if self._ladder_repegs < PositionActor.LADDER_REPEG_MAX:
                        new_mid = self._fresh_mid()
                        if new_mid is not None:
                            self._ladder_repegs += 1
                            self._repeg_limit_price = new_mid
                            self._ladder_note("repeg")
                            self._emit(
                                f"LADDER re-peg: re-posting at fresh mid {new_mid}"
                                f" for {PositionActor.LADDER_REPEG_SECONDS:g}s"
                            )
                            await self._submit_entry()
                            self._ladder_task = asyncio.ensure_future(
                                self._ladder_watch(PositionActor.LADDER_REPEG_SECONDS)
                            )
                            return
                    if self.spec.ladder_skip_ok:
                        # LADDER-SKIP: this entry style forbids the market
                        # chase — abandon the entry, nothing was filled,
                        # nothing needs protecting. State flips BEFORE the
                        # emit (audit 2026-09-16: the emit persists the row,
                        # and the sibling partial-fill branch orders it this
                        # way — otherwise the DB freezes a pending_entry
                        # ghost until the next restart's sweep).
                        self.state = PositionState.CLOSED
                        self._ladder_note("skipped")
                        self._emit("LADDER skip: unfilled and this entry never chases — abandoned")
                        return
                    # 6.5 fallback: the posted LIMIT died unfilled —
                    # re-enter the OLD way (plain MARKET, fresh attempt id)
                    self._entry_type_override = OrderType.MARKET
                    self._ladder_note("fallback")
                    self._emit("LADDER fallback: re-entering at MARKET")
                    await self._submit_entry()
                    return
                if (self.filled_qty or 0) > 0:
                    # partial fill, THEN cancel (Alpaca paper did exactly this
                    # to CAMT's auction order, 2026-08-27: 5 of 90 filled →
                    # cancel): the filled shares are REAL. Closing the actor
                    # orphaned them stopless until a restart's reconcile. Open
                    # the position on what filled and protect it NOW.
                    from datetime import UTC as _utc
                    from datetime import datetime as _dt

                    self.state = PositionState.OPEN
                    if self.entry_filled_at is None:
                        self.entry_filled_at = _dt.now(_utc)
                    if self.stop_leg_order_id is None:
                        await self._place_protective_stop()
                    self._emit(
                        f"entry canceled after PARTIAL fill — OPEN with "
                        f"{self.filled_qty:g} shares, protective stop enforced"
                    )
                    self._fire_trade("entry", self.filled_qty, self.avg_entry_price)
                else:
                    self.state = PositionState.CLOSED
                    self.exit_reason = "entry canceled"
                    if self.spec.auction_open:
                        # 2026-09-02: "canceled the buy" read as a failure —
                        # this is the paper venue expiring an auction order
                        # it never simulates; the 9:31 fallback re-evaluates
                        self._emit(
                            "auction order expired unfilled (paper simulates no"
                            " auction) — the 9:31 fallback re-evaluates this name"
                        )
                    else:
                        self._emit("entry canceled before fill")
            else:
                # audit A1-3/A1-9: this event used to be swallowed for every
                # post-entry state — a canceled/expired STOP leg (DAY stops
                # ALL die at 16:00; a manual cancel in the Alpaca dashboard
                # does the same) left the position naked with the trail
                # amending a dead order id forever, and a canceled EXIT
                # order stranded CLOSING as a dead end.
                await self._handle_dead_protection(event, order, parsed)
        elif event == "rejected":
            await self._handle_rejected(order, parsed)

    async def _handle_dead_protection(self, event: str, order, parsed) -> None:
        """Audit A1-3 + A1-9: a canceled/expired event on a LIVE position.

        Stop leg dead while OPEN/SCALING_OUT → re-protect immediately (hard
        rule 3: the broker must always hold a stop). While HALTED → arm the
        exit_halt re-protect instead (§12: no order traffic into a halt).
        Exit order dead while CLOSING → back to OPEN with a fresh stop so the
        exit layers keep managing (mirror of the exit-submit-failure path).

        Matching is deliberately strict: primary key is the CURRENT believed
        order id; the client-order-id action is trusted only when we have no
        believed id at all (e.g. the scale-out relocate lost track) — a stale
        cancel echo for a long-replaced order must never spawn a second stop.
        """
        is_our_stop = (
            self.stop_leg_order_id is not None and order.order_id == self.stop_leg_order_id
        ) or (
            self.stop_leg_order_id is None
            and parsed is not None
            and parsed.action == OrderAction.STOP
        )
        is_our_exit = (self.exit_order_id is not None and order.order_id == self.exit_order_id) or (
            self.exit_order_id is None and parsed is not None and parsed.action == OrderAction.EXIT
        )
        if is_our_stop and self.state in (PositionState.OPEN, PositionState.SCALING_OUT):
            self.stop_leg_order_id = None
            if self._kill_flatten_active:
                logger.info(
                    "[%s] stop %s by the kill switch — flatten in flight, not re-protecting",
                    self.spec.symbol,
                    event,
                )
                return
            logger.warning(
                "[%s] resting STOP %s %s while %s — position is NAKED, re-protecting now",
                self.spec.symbol,
                order.order_id,
                event.upper(),
                self.state.value,
            )
            await self._place_protective_stop()
            return
        if is_our_stop and self.state is PositionState.HALTED:
            self.stop_leg_order_id = None
            self._reprotect_after_halt = True
            logger.warning(
                "[%s] resting STOP %s %s during a HALT — §12 forbids submitting "
                "into the halt; re-protect armed for the resume",
                self.spec.symbol,
                order.order_id,
                event.upper(),
            )
            self._emit("stop died during the halt — re-protect armed for the resume")
            return
        if is_our_exit and self.state is PositionState.CLOSING:
            self.exit_order_id = None
            if self._kill_flatten_active:
                logger.info(
                    "[%s] exit order %s by the kill switch — its own flatten closes this",
                    self.spec.symbol,
                    event,
                )
                return
            remaining = (
                self.exit_engine.remaining_qty
                if self.exit_engine is not None
                else (self.filled_qty or self.spec.qty)
            ) - float(order.filled_qty or 0)
            if remaining <= 0:
                self.state = PositionState.CLOSED
                self._emit(f"exit order {event} after filling everything — closed")
                return
            logger.warning(
                "[%s] EXIT order %s %s with %.6g shares still held — CLOSING would "
                "dead-end; re-protecting and returning to OPEN so the close retries",
                self.spec.symbol,
                order.order_id,
                event.upper(),
                remaining,
            )
            self.state = PositionState.OPEN
            self.stop_leg_order_id = None
            with contextlib.suppress(Exception):
                await self._place_protective_stop()
            self._emit(f"exit order {event} — protective stop re-placed, position back to OPEN")
            return
        logger.info(
            "[%s] ignoring %s event for %s while %s (not the live stop/exit order)",
            self.spec.symbol,
            event,
            order.order_id,
            self.state.value,
        )

    async def _handle_rejected(self, order, parsed) -> None:
        """Audit A1-7: 'rejected' used to be unconditionally terminal —
        state = ERROR no matter WHICH order the broker refused. An
        async-rejected scale sale, exit order or protective stop then killed
        the actor with shares still held: the event loop exited CLEANLY (so
        _run never ran a protective check) and ERROR is terminal, so the
        engine's held-symbol guard allowed a SECOND entry on the symbol
        while the broker still held the first position.

        Now only a rejected ENTRY is terminal. A rejection on a live
        position's management order logs at ERROR, restores the ledger,
        re-protects through the A1-5 machinery, and keeps the actor
        managing. Matching mirrors _handle_dead_protection: primary key is
        the CURRENT believed order id; the client-order-id action is
        trusted only when no believed id exists."""
        is_entry = order.order_id == self.entry_order_id or (
            parsed is not None and parsed.action == OrderAction.ENTRY
        )
        if is_entry:
            # a rejected ENTRY opened nothing (a partial fill before the
            # rejection is caught by _run's protective sweep, which now runs
            # on the clean-exit path too) — terminal ERROR stays honest here
            self.state = PositionState.ERROR
            self.exit_reason = "order rejected"
            self._emit(f"order REJECTED: {order.client_order_id}")
            return
        if self.state in TERMINAL_STATES:
            logger.info(
                "[%s] ignoring rejected event for %s — actor already %s",
                self.spec.symbol,
                order.order_id,
                self.state.value,
            )
            return
        is_our_stop = (
            self.stop_leg_order_id is not None and order.order_id == self.stop_leg_order_id
        ) or (
            self.stop_leg_order_id is None
            and parsed is not None
            and parsed.action == OrderAction.STOP
        )
        is_our_scale = (
            self.scale_order_id is not None and order.order_id == self.scale_order_id
        ) or (
            self.scale_order_id is None
            and parsed is not None
            and parsed.action == OrderAction.SCALE
        )
        is_our_exit = (self.exit_order_id is not None and order.order_id == self.exit_order_id) or (
            self.exit_order_id is None and parsed is not None and parsed.action == OrderAction.EXIT
        )
        if not (is_our_stop or is_our_scale or is_our_exit):
            logger.warning(
                "[%s] REJECTED event for %s while %s — not the live stop/scale/exit "
                "order; ignoring",
                self.spec.symbol,
                order.order_id,
                self.state.value,
            )
            return
        if order.order_id in self._handled_rejects:
            logger.info(
                "[%s] duplicate rejected echo for %s — already handled",
                self.spec.symbol,
                order.order_id,
            )
            return
        self._handled_rejects.add(order.order_id)
        if self._kill_flatten_active:
            # the kill switch owns this position right now (A1-3 flag) —
            # never fight its flatten with fresh order traffic; just stop
            # aiming at the dead ids and let the kill machinery recover
            if is_our_stop:
                self.stop_leg_order_id = None
            if is_our_exit:
                self.exit_order_id = None
            if is_our_scale:
                self.scale_order_id = None
            logger.error(
                "[%s] order %s REJECTED during the kill flatten — kill machinery owns recovery",
                self.spec.symbol,
                order.order_id,
            )
            return
        held = (
            self.exit_engine.remaining_qty
            if self.exit_engine is not None
            else (self.filled_qty or 0.0)
        )
        if is_our_scale:
            await self._rejected_scale(order)
            return
        if is_our_exit:
            await self._rejected_exit(order, held)
            return
        await self._rejected_stop(order, held)

    async def _rejected_scale(self, order) -> None:
        """The broker ACCEPTED the scale sale's submit then async-rejected it:
        confirm_scale_out already landed the ledger commit, so remaining_qty
        is a lie (the shares were never sold) and the resting stop was
        already resized DOWN to the post-scale remainder — a rule-3
        half-hole, the async twin of the A1-6 submit-failure path."""
        self.scale_order_id = None
        unfilled = max(0.0, float(order.qty or 0) - float(order.filled_qty or 0))
        if self.exit_engine is not None:
            self.exit_engine.rollback_scale_out(unfilled)
        logger.error(
            "[%s] SCALE sale %s REJECTED after submit — ledger restored (+%.6g), "
            "re-protecting the full remaining position",
            self.spec.symbol,
            order.order_id,
            unfilled,
        )
        if self.state is PositionState.HALTED:
            # §12: no order traffic into a halt. The undersized stop (if any)
            # keeps protecting part of the position; the resume re-protects
            # the full count when no stop is resting at all.
            if self.stop_leg_order_id is None:
                self._reprotect_after_halt = True
            self._emit("scale-out REJECTED during a halt — ledger restored, resume re-protects")
            return
        self.state = PositionState.OPEN
        await self._cancel_resting_stop()
        self.stop_leg_order_id = None
        await self._place_protective_stop()
        self._emit(
            "scale-out REJECTED — staging dropped, full remaining re-protected, back to OPEN"
        )

    async def _rejected_exit(self, order, held: float) -> None:
        """close_now's exit order async-rejected: the resting stop was
        already canceled, so the position is NAKED in CLOSING — the mirror
        of the exit-submit-failure path, one broker round-trip later."""
        self.exit_order_id = None
        remaining = held - float(order.filled_qty or 0)
        if remaining <= 0:
            self.state = PositionState.CLOSED
            self._emit("exit order rejected with nothing left held — closed")
            return
        if self.state is PositionState.HALTED:
            if self.stop_leg_order_id is None:
                self._reprotect_after_halt = True
            self._emit("exit REJECTED during a halt — re-protect armed for the resume")
            return
        if (
            self.state in (PositionState.OPEN, PositionState.SCALING_OUT)
            and self.stop_leg_order_id is not None
        ):
            logger.info(
                "[%s] late exit rejection for %s — already back to %s and protected",
                self.spec.symbol,
                order.order_id,
                self.state.value,
            )
            return
        self._close_backoff_mono = time.monotonic() + 5.0  # NVDG pacing
        logger.error(
            "[%s] EXIT order %s REJECTED with %.6g shares still held — CLOSING would "
            "dead-end; re-protecting and returning to OPEN so the close retries",
            self.spec.symbol,
            order.order_id,
            remaining,
        )
        self.state = PositionState.OPEN
        self.stop_leg_order_id = None
        with contextlib.suppress(Exception):
            await self._place_protective_stop()
        self._emit("exit order REJECTED — protective stop re-placed, position back to OPEN")

    async def _rejected_stop(self, order, held: float) -> None:
        """The protective stop itself async-rejected — the position is NAKED
        (hard rule 3). Re-protect once; a second async rejection means the
        broker will not hold a stop for this position, so it must not exist."""
        self.stop_leg_order_id = None
        if self.state is PositionState.HALTED:
            self._reprotect_after_halt = True
            logger.error(
                "[%s] protective STOP %s REJECTED during a HALT — §12 forbids "
                "submitting into the halt; re-protect armed for the resume",
                self.spec.symbol,
                order.order_id,
            )
            self._emit("stop REJECTED during the halt — re-protect armed for the resume")
            return
        if self.state not in (PositionState.OPEN, PositionState.SCALING_OUT) or held <= 0:
            logger.info(
                "[%s] rejected stop %s while %s with %.6g held — nothing to re-protect",
                self.spec.symbol,
                order.order_id,
                self.state.value,
                held,
            )
            return
        self._stop_reject_count += 1
        logger.error(
            "[%s] protective STOP %s REJECTED while %s — position is NAKED (async reject #%d)",
            self.spec.symbol,
            order.order_id,
            self.state.value,
            self._stop_reject_count,
        )
        if self._stop_reject_count >= 2:
            self._emit("stop REJECTED twice — flattening (hard rule 3)")
            if self._rule3_flatten_active:
                logger.error(
                    "[%s] rule-3 flatten already in flight — not recursing; exit cadence retries",
                    self.spec.symbol,
                )
                return
            self._rule3_flatten_active = True
            try:
                await self.close_now("protective stop rejected", force=True)
            finally:
                self._rule3_flatten_active = False
            return
        await self._place_protective_stop()
        self._emit("stop REJECTED — protective stop re-submitted")

    # -- commands ------------------------------------------------------------

    async def close_now(self, reason: str = "manual close", *, force: bool = False) -> None:
        """Flatten this position immediately: MARKET inside RTH, marketable
        LIMIT + extended_hours outside (audit A1-12 — the old MARKET/DAY
        request QUEUED every PRE/POST exit until 09:30, so session flattens,
        judge closes and momentum exits sat exposed for hours).

        force=True bypasses ONLY the NVDG retry backoff (for the rule-3
        emergency flatten, which must never be paced away) — the
        CLOSING/terminal idempotency guard always applies."""
        if self.state in TERMINAL_STATES or self.state is PositionState.CLOSING:
            return  # idempotent: a second close must never double-sell
        if not force and time.monotonic() < getattr(self, "_close_backoff_mono", 0.0):
            # NVDG 2026-09-21: a broker-refused close used to hot-loop
            # (fail → re-protect → rule-3 flatten → fail …) 6 times in 2s.
            # After a failure, pace the retry — the exit engine re-fires
            # EXIT_NOW every second, so the close IS retried, calmly.
            return
        self.exit_reason = reason
        if self.state is PositionState.PENDING_ENTRY and self.entry_order_id:
            # ABANDON before the cancel (GRML hole, engine-stop flavor): the
            # ladder's canceled-event handler would otherwise re-enter at
            # MARKET while the engine is STOPPING.
            self._abandon_entry()
            await self.adapter.cancel_order(self.mode, self.entry_order_id)
            self._emit(f"cancel requested for unfilled entry ({reason})")
            return
        self.state = PositionState.CLOSING
        # free the shares: cancel the resting stop leg before the exit order
        await self._cancel_resting_stop()
        # RE-CHECK after the await: the resting stop may have FILLED while the
        # cancel was in flight (stop fill racing a momentum exit flipped a
        # position short in the simulator, 2026-08-18 — same race exists live)
        if self.state in TERMINAL_STATES:
            self._emit(f"position closed itself while canceling the stop ({reason})")
            return
        # the REMAINING quantity — after a scale-out, filled_qty still holds
        # the original entry fill; selling it again would flip the position
        # short (oversell caught by the simulator, 2026-08-18)
        remaining = (
            self.exit_engine.remaining_qty
            if self.exit_engine is not None
            else (self.filled_qty or self.spec.qty)
        )
        # BROKER TRUTH CLAMP (2026-09-09, the MVLL race): the resting stop can
        # PARTIALLY FILL in the instant before/while it is canceled, so our
        # ledger over-counts — asking the broker for the whole ledger qty then
        # 403s and strands the leftover shares WITHOUT a stop (rule-3 breach).
        # Ask the broker what actually remains and never sell more than that.
        broker_px: float | None = None  # A1-12: reference for the outside-RTH limit
        try:
            broker_qty = None
            for pos in await self.adapter.get_positions(self.mode):
                if pos.symbol == self.spec.symbol:
                    broker_qty = abs(pos.qty)
                    broker_px = pos.current_price
                    break
            if broker_qty is None:
                self.state = PositionState.CLOSED
                self._emit(f"broker already flat — close is done ({reason})")
                return
            if broker_qty < remaining:
                logger.warning(
                    "[%s] ledger says %.6g but broker holds %.6g — the resting "
                    "stop partially filled; selling broker truth",
                    self.spec.symbol,
                    remaining,
                    broker_qty,
                )
                remaining = broker_qty
        except Exception:
            logger.exception(
                "[%s] broker position check failed — using ledger qty", self.spec.symbol
            )
        if remaining <= 0:
            self._emit(f"nothing left to close ({reason})")
            return
        self._attempt += 1
        exit_side = OrderSide.SELL if self.spec.side is OrderSide.BUY else OrderSide.BUY
        # A1-12: MARKET only inside RTH; outside, a marketable LIMIT with
        # extended_hours. Reference: live quote far touch (helper) → broker
        # position current_price (captured above) → entry price (always set
        # on a filled position; conservative last resort).
        fields = marketable_exit_fields(
            exit_side, self.spec.symbol, broker_px or self.avg_entry_price
        )
        request = OrderRequest(
            symbol=self.spec.symbol,
            qty=remaining,
            side=exit_side,
            order_type=fields["order_type"],
            time_in_force=TimeInForce.DAY,
            limit_price=fields["limit_price"],
            extended_hours=fields["extended_hours"],
            client_order_id=make_client_order_id(self.uuid, OrderAction.EXIT, self._attempt),
        )
        try:
            info = await self.adapter.submit_order(self.mode, request)
            # audit A1-9: keep the exit order's id — the canceled/expired
            # handler and core.poll_missed_fills need it, or a dead exit
            # order strands the actor in CLOSING forever
            self.exit_order_id = info.order_id
        except Exception as exc:
            # THE NAKED WINDOW (torture sim 2026-09-16 CRITICAL): the stop
            # is already canceled; a failed exit submit used to strand the
            # position in CLOSING — no stop, no manager, no retry, invisible
            # until restart. Re-protect NOW and fall back to OPEN so the
            # exit layers keep managing and the close can be retried.
            self._close_backoff_mono = time.monotonic() + 5.0  # NVDG pacing
            logger.exception(
                "[%s] EXIT SUBMIT FAILED after stop cancel — re-protecting (%r)",
                self.spec.symbol,
                exc,
            )
            self.state = PositionState.OPEN
            self.stop_leg_order_id = None
            with contextlib.suppress(Exception):
                await self._place_protective_stop()
            self._emit(
                f"exit submit FAILED ({reason}) — protective stop re-placed, position back to OPEN"
            )
            return
        self._emit(f"exit submitted ({reason})")

    # -- adaptive exit execution (§8.2, Phase 6) -----------------------------

    async def apply_exit_decision(self, decision: ExitDecision) -> None:
        """Execute one ExitEngine decision against the broker. HALTED actors
        never act (LULD: no order traffic into a halt/resume)."""
        if self.state not in (PositionState.OPEN, PositionState.SCALING_OUT):
            # audit A1-6: a SCALE_OUT decision carries a deferred ledger
            # commit in the ExitEngine — dropping the decision (halt/close
            # raced it) must also drop the pending commit, or the scale
            # layers stay gated forever
            if decision.action is ExitAction.SCALE_OUT and hasattr(
                self.exit_engine, "abort_scale_out"
            ):
                self.exit_engine.abort_scale_out()
            return
        try:
            if decision.action is ExitAction.AMEND_STOP:
                await self._amend_stop(decision.new_stop, decision.reason)
            elif decision.action is ExitAction.SCALE_OUT:
                await self._scale_out(decision)
            elif decision.action is ExitAction.EXIT_NOW:
                await self.close_now(decision.reason)
        except Exception:
            logger.exception("[%s] exit decision failed: %s", self.spec.symbol, decision)
            # A1-6 belt-and-braces: never leave a pending scale commit
            # dangling after a failed decision (abort is idempotent and a
            # no-op after a successful confirm)
            if decision.action is ExitAction.SCALE_OUT and hasattr(
                self.exit_engine, "abort_scale_out"
            ):
                with contextlib.suppress(Exception):
                    self.exit_engine.abort_scale_out()

    async def tighten_stop_manual(self, new_stop: float) -> None:
        """Manual override (UI popup, behind a confirm): move the server-side
        stop TOWARD price. Only ever tightens — a loosening request is
        refused, the protective stop can never be widened from the UI."""
        direction = 1.0 if self.spec.side is OrderSide.BUY else -1.0
        current = (
            self.exit_engine.current_stop if self.exit_engine is not None else self.spec.stop_price
        )
        if (new_stop - current) * direction <= 0:
            logger.warning(
                "[%s] manual stop %.2f would LOOSEN the current %.2f — refused",
                self.spec.symbol,
                new_stop,
                current,
            )
            return
        await self._amend_stop(new_stop, "manual tighten (UI)")
        if self.exit_engine is not None:
            self.exit_engine.current_stop = new_stop

    async def _place_protective_stop(self) -> None:
        """Submit the standalone server-side stop after an auction fill.
        Hard rule 3: if the stop cannot be placed, the position must not
        exist — one retry, then flatten immediately."""
        exit_side = OrderSide.SELL if self.spec.side is OrderSide.BUY else OrderSide.BUY
        # REMAINING quantity (audit A1-5): after a scale-out, filled_qty still
        # holds the ORIGINAL entry fill — a full-size stop would flip the
        # position and Alpaca rejects it, so BOTH attempts died. Mirror
        # close_now's remaining logic (broker-truth clamping stays close_now's
        # job; this is the ledger's honest count).
        qty = (
            self.exit_engine.remaining_qty
            if self.exit_engine is not None
            else (self.filled_qty or self.spec.qty)
        )
        if qty <= 0:
            logger.warning(
                "[%s] protective stop skipped — no remaining shares to protect",
                self.spec.symbol,
            )
            return
        # BEST known level (audit A1-5): spec.stop_price is the ENTRY-TIME
        # stop — re-protecting a breakeven-locked winner there silently
        # regresses it to full initial risk. Use the tightest-protective
        # level we ever confirmed at the broker (direction-aware: higher
        # protects a long, lower protects a short).
        direction = 1.0 if self.spec.side is OrderSide.BUY else -1.0
        stop_level = self.spec.stop_price
        if self._confirmed_stop is not None and (self._confirmed_stop - stop_level) * direction > 0:
            stop_level = self._confirmed_stop
        for attempt in (1, 2):
            self._attempt += 1
            request = OrderRequest(
                symbol=self.spec.symbol,
                qty=qty,
                side=exit_side,
                order_type=OrderType.STOP,
                time_in_force=TimeInForce.DAY,
                client_order_id=make_client_order_id(self.uuid, OrderAction.STOP, self._attempt),
                stop_price=round(stop_level, 2),
            )
            try:
                info = await self.adapter.submit_order(self.mode, request)
            except Exception as exc:
                logger.exception(
                    "[%s] protective stop submit failed (attempt %d): %r",
                    self.spec.symbol,
                    attempt,
                    exc,
                )
                continue
            self.stop_leg_order_id = info.order_id
            # audit A1-4: this level is now broker-confirmed — anchor it so
            # a later failed amend's rollback cannot regress below it
            self._confirmed_stop = stop_level
            self._emit(f"protective stop placed @ {round(stop_level, 2)} ({qty:g} shares)")
            return
        self._emit("PROTECTIVE STOP FAILED TWICE — flattening (hard rule 3)")
        # force=True: the exit-submit failure that often leads here just set
        # _close_backoff_mono (NVDG pacing) — the rule-3 emergency flatten
        # must NOT be silently paced away. The reentrancy latch keeps a
        # broker-down cascade (close fails → re-protect fails → rule 3 →
        # close …) from recursing; retries then come from the exit engine's
        # per-second EXIT_NOW cadence, as the NVDG design intends.
        if self._rule3_flatten_active:
            logger.error(
                "[%s] rule-3 flatten already in flight — not recursing; exit cadence retries",
                self.spec.symbol,
            )
            return
        self._rule3_flatten_active = True
        try:
            await self.close_now("protective stop could not be placed", force=True)
        finally:
            self._rule3_flatten_active = False

    async def _amend_stop(self, new_stop: float, reason: str) -> None:
        if self.stop_leg_order_id is None:
            logger.warning("[%s] no stop leg id — cannot amend", self.spec.symbol)
            return
        try:
            info = await self.adapter.replace_order(
                self.mode, self.stop_leg_order_id, stop_price=round(new_stop, 2)
            )
            # STALE-ID FIX (2026-09-15, the TRMD climax 422): every replace
            # births a NEW order id; forgetting it leaves the next replace
            # (ratchet or scale-out) aiming at a dead order.
            self.stop_leg_order_id = info.order_id
        except Exception as exc:
            # benign race (agenda #5a, 2026-08-23): the trail asked to ratchet
            # in the same tick the stop FILLED. But the 2026-09-16 audit
            # found REAL failures hiding at DEBUG — a failed amend means the
            # believed stop is not the resting stop. Say so where it is
            # heard (WARNING) unless the position is genuinely closing.
            if self.state in (PositionState.CLOSING, PositionState.CLOSED):
                logger.debug(
                    "[%s] stop amend skipped — position closing (%s)",
                    self.spec.symbol,
                    reason,
                )
            else:
                logger.warning(
                    "[%s] stop amend FAILED (%s) — believed stop %.2f is NOT "
                    "resting (%r); rolling belief back so the trail retries",
                    self.spec.symbol,
                    reason,
                    new_stop,
                    exc,
                )
                # ROLLBACK (audit 2026-09-16, probe-confirmed): the engine
                # pre-commits current_stop + the throttle clock before the
                # broker call; without this rollback the ratchet gate saw
                # the phantom level and NEVER re-proposed ("retry next
                # tick" was a lie). Restore the last broker-confirmed
                # level and clear the throttle so the retry is immediate.
                if self.exit_engine is not None and self._confirmed_stop is not None:
                    self.exit_engine.current_stop = self._confirmed_stop
                    self.exit_engine.last_amend_time = None
            return
        self.stop_leg_order_id = info.order_id  # replace returns a NEW order id
        # UI TRUTH (2026-09-15: "if you raised the exit, it should
        # show on the UI"): exit_engine.current_stop feeds the position
        # card AND the manual-tighten LOOSEN check. Only the manual path
        # kept it in sync — every MK ratchet raised the broker stop while
        # the card (and the loosen guard) kept the stale entry-time stop.
        self._confirmed_stop = new_stop  # broker-confirmed level (rollback anchor)
        if self.exit_engine is not None:
            self.exit_engine.current_stop = new_stop
        if "MK protect ratchet" in reason:
            # routine brain housekeeping every ~20s — log it, never page the
            # phone with it (2026-09-09: tightening spam does not belong
            # on the phone)
            logger.info(
                "[%s %s] server-side stop ratcheted to %.2f (%s)",
                self.spec.symbol,
                self.position_key,
                new_stop,
                reason,
            )
        else:
            self._emit(f"server-side stop ratcheted to {new_stop:.2f} ({reason})")

    async def _scale_out(self, decision: ExitDecision) -> None:
        if self.exit_engine is None:
            return
        # POST-scale remainder (audit A1-6): the ExitEngine no longer
        # pre-decrements remaining_qty — the commit is deferred until the
        # sale submit succeeds — so the stop resize computes the remainder
        # itself. planned_remaining == remaining_qty on the MK path (the
        # kitchen adjusts its ledger before issuing SCALE_OUT).
        remainder = getattr(self.exit_engine, "planned_remaining", self.exit_engine.remaining_qty)
        # 1) shrink + tighten the resting stop FIRST (shares must be freed,
        #    and the remainder must stay protected at breakeven)
        if self.stop_leg_order_id is not None and decision.new_stop is not None:
            try:
                info = await self.adapter.replace_order(
                    self.mode,
                    self.stop_leg_order_id,
                    qty=remainder,
                    stop_price=round(decision.new_stop, 2),
                )
                self.stop_leg_order_id = info.order_id
                self.exit_engine.current_stop = decision.new_stop  # UI truth
                # audit A1-4: the broker CONFIRMED this level — anchor it, or
                # a later failed ratchet rolls current_stop back to the stale
                # pre-scale level and the next chandelier proposal physically
                # LOWERS the resting stop below breakeven
                self._confirmed_stop = decision.new_stop
            except Exception:
                # STALE-ID RACE (2026-09-15 TRMD): a ratchet replaced the stop
                # moments ago and the id moved. Find the LIVE stop and retry;
                # if none is found, fall through — the sale below still runs
                # against broker truth and close paths re-protect.
                logger.warning(
                    "[%s] scale-out stop-resize hit a stale id — relocating",
                    self.spec.symbol,
                )
                try:
                    from waveapp.broker.base import OrderType as _OT

                    live_stop = None
                    for order in await self.adapter.get_open_orders(self.mode):
                        for leg in (order, *order.legs):
                            if leg.symbol == self.spec.symbol and leg.order_type in (
                                _OT.STOP,
                                _OT.STOP_LIMIT,
                            ):
                                live_stop = leg
                                break
                        if live_stop:
                            break
                    if live_stop is not None:
                        info = await self.adapter.replace_order(
                            self.mode,
                            live_stop.order_id,
                            qty=remainder,
                            stop_price=round(decision.new_stop, 2),
                        )
                        self.stop_leg_order_id = info.order_id
                        self.exit_engine.current_stop = decision.new_stop  # UI truth
                        self._confirmed_stop = decision.new_stop  # A1-4 anchor
                    else:
                        logger.warning(
                            "[%s] no live stop found during scale-out — proceeding",
                            self.spec.symbol,
                        )
                        self.stop_leg_order_id = None
                except Exception:
                    logger.exception(
                        "[%s] stop relocate failed — scale sale proceeds", self.spec.symbol
                    )
        # 2) bank the scale portion
        self._attempt += 1
        exit_side = OrderSide.SELL if self.spec.side is OrderSide.BUY else OrderSide.BUY
        request = OrderRequest(
            symbol=self.spec.symbol,
            qty=decision.qty,
            side=exit_side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            client_order_id=make_client_order_id(self.uuid, OrderAction.SCALE, self._attempt),
        )
        try:
            info = await self.adapter.submit_order(self.mode, request)
            # audit A1-7: keep the sale's id — an async REJECTION of this
            # order must be matched strictly and un-commit the ledger
            self.scale_order_id = info.order_id
        except Exception as exc:
            # audit A1-6: the broker still holds the FULL position but the
            # resting stop was just resized DOWN to the post-scale remainder
            # (rule-3 half-hole). Re-arm the layer so it retries (deferred
            # commit dropped — remaining_qty stays truthful), then cancel
            # the undersized stop and re-protect the FULL remaining position
            # through the A1-5 machinery (remaining qty, tightest confirmed
            # level, rule-3 flatten if it cannot be placed).
            logger.exception(
                "[%s] SCALE-OUT SALE SUBMIT FAILED (%s) — re-arming layer and "
                "re-protecting full size (%r)",
                self.spec.symbol,
                decision.reason,
                exc,
            )
            if hasattr(self.exit_engine, "abort_scale_out"):
                self.exit_engine.abort_scale_out()
            await self._cancel_resting_stop()
            self.stop_leg_order_id = None
            await self._place_protective_stop()
            self._emit(
                f"scale-out submit FAILED ({decision.reason}) — layer re-armed, "
                f"full remaining position re-protected"
            )
            return
        self.state = PositionState.SCALING_OUT
        self.exit_engine.confirm_scale_out()
        self._emit(
            f"scale-out: {decision.qty:g} banked ({decision.reason}); "
            f"remainder {self.exit_engine.remaining_qty:g} protected at {decision.new_stop:.2f}"
        )

    CANCEL_SETTLE_SECONDS = 0.8

    async def _cancel_resting_stop(self) -> None:
        canceled = False
        seen: set[str] = set()
        try:
            if self.stop_leg_order_id is not None:
                await self.adapter.cancel_order(self.mode, self.stop_leg_order_id)
                seen.add(self.stop_leg_order_id)
                canceled = True
            # ALWAYS also sweep every other resting stop on our symbol —
            # a restart double-buy left TWO bracket legs on GDX (2026-08-19);
            # an orphaned stop firing after the close would flip us short.
            # Safe because the engine enforces one position per symbol
            # (the 2026-09-20 duel-twin exception retired with the duel).
            for order in await self.adapter.get_open_orders(self.mode):
                for leg in (order, *order.legs):
                    if (
                        leg.symbol == self.spec.symbol
                        and leg.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
                        and leg.order_id not in seen
                    ):
                        await self.adapter.cancel_order(self.mode, leg.order_id)
                        seen.add(leg.order_id)
                        canceled = True
        except Exception as exc:
            logger.exception("[%s] stop-leg cancel failed: %r", self.spec.symbol, exc)
        if canceled:
            await asyncio.sleep(self.CANCEL_SETTLE_SECONDS)

    def enter_halt(self) -> None:
        """LULD halt on a held symbol (§12): never market into the resume."""
        if self.state is PositionState.PENDING_ENTRY:
            # GRML 2026-09-22 (−$843): the entry order sat through a LULD
            # volatility pause and filled the resume dump of a parabolic
            # runner. A halt BEFORE the fill kills the signal — cancel the
            # entry, never chase the resume. (A fill racing the cancel is
            # fine: the actor then proceeds normally, bracket stop and all.)
            # ABANDON, don't just cancel (the −$843 second hole): the ladder
            # machinery would otherwise consume the canceled event and re-peg
            # or MARKET-fall-back INTO the halt.
            self._abandon_entry()
            if self.entry_order_id:
                asyncio.ensure_future(self._cancel_entry_on_halt())
            self._emit("symbol halted before the entry filled — canceling (no resume chase)")
            return
        if self.state is PositionState.CLOSING:
            # audit A1-8: overwriting CLOSING with HALTED orphaned the
            # in-flight exit order — if it then died unfilled during the
            # halt, the dead-exit handler only fires on CLOSING, so the
            # resume flipped straight to OPEN believing a stop id that no
            # longer rests. The exit order IS this position's manager: leave
            # CLOSING alone (its fill/cancel/reject handlers all keep
            # working) and just say so.
            logger.warning(
                "[%s] halted while CLOSING — the in-flight exit order stays "
                "the manager; not flipping to HALTED",
                self.spec.symbol,
            )
            self._emit("symbol halted (LULD) while closing — exit order stays working")
            return
        if self.state not in TERMINAL_STATES:
            self.state = PositionState.HALTED
            self._emit("symbol halted (LULD) — actor holding, stop stays server-side")

    async def _cancel_entry_on_halt(self) -> None:
        try:
            await self.adapter.cancel_order(self.mode, self.entry_order_id)
        except Exception as exc:
            # cancel refused usually means it already filled — the normal
            # fill path takes over, protected by the bracket stop
            logger.warning(
                "[%s] halted-entry cancel refused (%r) — if it filled, the actor manages it",
                self.spec.symbol,
                exc,
            )

    def exit_halt(self) -> None:
        if self.state is PositionState.HALTED:
            self.state = PositionState.OPEN
            if self._reprotect_after_halt:
                # audit A1-3: the stop leg died (canceled/expired — DAY stops
                # die at 16:00) while the symbol was halted. §12 forbade
                # submitting into the halt; the halt just lifted, so the
                # protective stop goes back up NOW. Strong task ref: a
                # weak-ref ensure_future was the 2026-08-19 GC segfault class.
                self._reprotect_after_halt = False
                self._reprotect_task = asyncio.ensure_future(self._place_protective_stop())
                self._emit("halt lifted — re-placing the stop that died during the halt")
            else:
                # audit A1-8: never TRUST that the stop survived the halt —
                # a stop-death event can be missed entirely while the stream
                # is deaf (the A1-3 flag only arms when the event ARRIVES).
                # Verify against broker truth and re-protect if it is gone.
                self._reprotect_task = asyncio.ensure_future(self._verify_protection_after_resume())
                self._emit("halt lifted — actor resumed")

    async def _verify_protection_after_resume(self) -> None:
        """Audit A1-8: after a halt lifts, confirm the server-side stop is
        actually resting (hard rule 3) instead of assuming the id we believed
        before the halt is still alive. protective_check already alarms when
        no stop is found; here we also FIX it."""
        try:
            if await self.protective_check():
                return
            if (
                self.state in (PositionState.OPEN, PositionState.SCALING_OUT)
                and (self.filled_qty or 0) > 0
            ):
                # the believed id is dead; sweep any stray legs first so a
                # false-negative check (broker hiccup) can never leave TWO
                # stops resting, then re-place through the A1-5 machinery
                self.stop_leg_order_id = None
                await self._cancel_resting_stop()
                await self._place_protective_stop()
        except Exception:
            logger.exception("[%s] post-halt protective re-check failed", self.spec.symbol)

    async def protective_check(self) -> bool:
        """After a crash/cancel: is the server-side stop still resting?
        Returns True when protected (stop found or position already flat)."""
        try:
            # audit A1-7: ERROR is terminal but NOT necessarily flat — an
            # errored actor with shares still needs its stop verified. Only
            # a genuinely CLOSED (or never-filled) actor skips cheaply.
            if self.state is PositionState.CLOSED or self.state is PositionState.PENDING_ENTRY:
                return True
            if self.state is PositionState.ERROR and (self.filled_qty or 0) == 0:
                return True
            if self._kill_flatten_active:
                # the kill switch canceled the stops DELIBERATELY and its
                # flatten is in flight — a naked-position alarm here would
                # fight it (A1-3 flag)
                return True
            open_orders = await self.adapter.get_open_orders(self.mode)
            for order in open_orders:
                legs = (order, *order.legs)
                for leg in legs:
                    if leg.symbol == self.spec.symbol and leg.order_type in (
                        OrderType.STOP,
                        OrderType.STOP_LIMIT,
                    ):
                        return True
            positions = await self.adapter.get_positions(self.mode)
            if not any(p.symbol == self.spec.symbol and p.qty for p in positions):
                return True  # already flat — nothing to protect
            logger.critical(
                "[%s] NO SERVER-SIDE STOP FOUND for an open position — protective alert",
                self.spec.symbol,
            )
            self._emit("⚠️ protective check FAILED: no resting stop found")
            return False
        except Exception:
            logger.exception("protective check errored")
            return False
