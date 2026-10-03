"""EngineCore (SPEC.md §3, Phase 5): owns the actors, routes broker events,
implements the Pause/Stop/Kill semantics and reconciliation. The UI and the
TelegramBridge both command it through the same public methods (the
CommandBus); neither ever touches the broker directly.

Semantics (§3):
- Pause  = no new entries; actors keep managing exits; connections stay up.
- Stop   = no new entries; actors run until every position is flat; then IDLE.
- Kill   = flatten everything now, cancel all Wave orders, then HALT.
Idempotency: on start/reconnect, reconcile broker state against the DB before
doing anything; unknown Wave-tagged orders freeze entries (never re-send blindly).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum

from waveapp.broker.base import (
    BrokerAdapter,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    TradingMode,
)
from waveapp.engine.actor import (
    TERMINAL_STATES,
    PositionActor,
    PositionSpec,
    PositionState,
    marketable_exit_fields,
)
from waveapp.engine.orders import parse_client_order_id
from waveapp.engine.risk import RiskEngine
from waveapp.persistence.db import Database, utc_now

logger = logging.getLogger("wave.engine.core")

# exit reasons → plain English for Telegram (adopted 2026-08-22)
_REASON_WORDS = {
    "VWAP recross": "the push is over, taking the profit",
    "volume death": "the push is over, taking the profit",
    "time stop (flat-ish)": "went nowhere too long",
    "recovered after drawdown (bank the bounce)": "bounced back — banking it",
    "stop hit": "safety stop did its job",
    "session boundary (flatten)": "closing before the market closes",
    "manual sell (positions card)": "you pressed SELL",
    "manual close": "you pressed SELL",
}


def _plain_reason(reason: str) -> str:
    return _REASON_WORDS.get(reason.strip(), reason)


def telegram_trade_line(actor, text: str) -> str:
    """The log stays technical; Telegram speaks plain English (approved
    message set, 2026-08-22). Unknown lines fall through unchanged."""
    import re

    symbol = actor.spec.symbol
    # S4 (2026-09-23): shorts read naturally — "Shorted/covered", never
    # "Bought/sold". Long wording stays byte-identical.
    short = actor.spec.side is OrderSide.SELL
    m = re.match(r"bracket entry submitted: (buy|sell) ([\d.]+) @ .*stop ([\d.]+)", text)
    if m:
        verb = "Buying" if m.group(1) == "buy" else "Shorting"
        # "{symbol} — 1309 shares" read as MINUS 1309 (2026-08-24)
        return f"🟦 {verb} {float(m.group(2)):g} shares of {symbol}. Safety stop at ${m.group(3)}."
    m = re.match(r"entry filled: ([\d.]+) @ ([\d.]+) — server-side stop resting at ([\d.]+)", text)
    if m:
        fill = float(m.group(2))
        if short:  # the stop in "Protected at" sits ABOVE the entry
            line = (
                f"🔻 Shorted {float(m.group(1)):g} shares of {symbol} at ${fill:,.2f}. "
                f"Protected at ${m.group(3)}."
            )
        else:
            line = (
                f"✅ Bought {float(m.group(1)):g} shares of {symbol} at ${fill:,.2f}. "
                f"Protected at ${m.group(3)}."
            )
        decision = getattr(actor.spec, "decision_price", None)
        if decision:  # option A (2026-08-24): the realized entry slippage
            # direction-aware: a short SELLS the entry, so filling BELOW the
            # decision price is the paid slippage (mirror of the long)
            cents = (fill - decision) * (-100.0 if short else 100.0)
            line += (
                f" Slippage: paid {cents:+.0f}¢/share vs the decision price."
                if abs(cents) >= 0.5
                else " Slippage: none."
            )
        return line
    m = re.match(r"server-side stop ratcheted to ([\d.]+)", text)
    if m:
        new_stop = float(m.group(1))
        entry = actor.avg_entry_price or 0
        long_side = actor.spec.side is OrderSide.BUY
        locked = entry and (new_stop >= entry if long_side else new_stop <= entry)
        if locked:
            verb = "tightened" if short else "raised"  # a short's stop moves DOWN
            return f"🔒 {symbol} safety {verb} to ${new_stop:,.2f} — profit is now locked in."
        if short:
            return f"🔽 {symbol} safety tightened to ${new_stop:,.2f}."
        return f"🔼 {symbol} safety raised to ${new_stop:,.2f}."
    m = re.match(r"exit submitted \((.+)\)", text)
    if m:
        verb = "Covering" if short else "Selling"
        return f"💰 {verb} {symbol} — {_plain_reason(m.group(1))}."
    m = re.match(r"closed \((.+)\) @ ([\d.]+) — (WIN|LOSS) (.+) \$", text)
    if m:
        reason, price, verdict, amount = m.groups()
        closed_verb = "covered" if short else "sold"
        if verdict == "WIN":
            return (
                f"🟢 {symbol} {closed_verb}: WON {amount} $ "
                f"({closed_verb} at ${float(price):,.2f})."
            )
        return f"🔴 {symbol} {closed_verb}: LOST {amount} $ ({_plain_reason(reason)})."
    m = re.match(r"scale-out filled: ([\d.]+) @ ([\d.]+)", text)
    if m:
        return (
            f"💰 {symbol} — banked part ({float(m.group(1)):g} shares at "
            f"${float(m.group(2)):,.2f}). The rest keeps riding."
        )
    if "symbol halted" in text:
        return (
            f"⏸️ {symbol} is frozen by the exchange (too fast a move). "
            "Wave holds and waits — the safety stop stays at the broker."
        )
    if "halt lifted" in text:
        return f"▶️ {symbol} unfrozen — managing again."
    return f"{symbol}: {text}"


class EngineState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    KILLED = "killed"


class EngineCore:
    def __init__(
        self,
        adapter: BrokerAdapter,
        database: Database | None,
        risk: RiskEngine | None = None,
        mode: TradingMode = TradingMode.PAPER,
        on_state: Callable[[EngineState], None] | None = None,
        push: Callable[[str], Awaitable[None]] | None = None,
        on_trade: Callable[[dict], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        exit_params_fn=None,  # Callable[[Regime], ExitParams] — training seam (§8.3)
    ) -> None:
        # clock seam (Phase 10.5): the simulator replays history through this
        # SAME engine; every wall-clock read goes through now_fn so replayed
        # bars see their own time. Live behavior is unchanged (default now).
        self._now = now_fn or (lambda: datetime.now(UTC))
        # §8.3: exit parameters change ONLY through the training pipeline —
        # this seam is how the pipeline sweeps candidates; live default is
        # the adopted params_for table, unchanged
        from waveapp.engine.exits import params_for as _params_for

        self._exit_params_fn = exit_params_fn or _params_for
        self.adapter = adapter
        self.db = database
        self.risk = risk or RiskEngine()
        self.mode = mode
        self.state = EngineState.IDLE
        self.actors: dict[str, PositionActor] = {}
        self._on_state = on_state
        self._push = push
        self._on_trade = on_trade
        self._router_task: asyncio.Task | None = None
        # 2026-08-21: two CONCURRENT start() calls both passed the state
        # check (state flips only AFTER the slow reconcile await) → reconcile
        # ran twice → every position adopted TWICE (duplicate actors, double
        # amends, double-exit risk). One lock, one start.
        self._start_lock = asyncio.Lock()

    def _exit_params_fn_for(self, regime, strategy: str, symbol: str | None = None):
        """Strategy/symbol-aware exit params when the seam supports it
        (research: momentum-only layers; Kitchen 2.0 volatility-class routing
        needs the SYMBOL to look up its daily NATR); plain lookup otherwise."""
        try:
            return self._exit_params_fn(regime, strategy, symbol)
        except TypeError:
            pass
        try:
            return self._exit_params_fn(regime, strategy)
        except TypeError:
            return self._exit_params_fn(regime)

    # -- state plumbing ------------------------------------------------------

    def _set_state(self, state: EngineState) -> None:
        if state is not self.state:
            self.state = state
            logger.info("engine state → %s", state.value)
            if self._on_state is not None:
                self._on_state(state)

    async def _notify(self, text: str) -> None:
        if self._push is not None:
            with contextlib.suppress(Exception):
                await self._push(text)

    @property
    def open_actor_count(self) -> int:
        return sum(1 for a in self.actors.values() if a.state not in TERMINAL_STATES)

    def _open_risk_pct(self, side: OrderSide | None = None) -> float | None:
        """Total open risk-to-stop as % of session-start equity (WAVE 2
        item 11). A position whose stop is ratcheted above entry risks
        NOTHING — floor-locked winners leave headroom to hunt. None when
        equity is unknown (then only the plain count gate applies).

        side=None (the default, every pre-S1 caller) keeps the historical
        behavior: the whole book. side=SELL/BUY returns only that side's
        portion — S1's short-book risk-share gate (§12: shorts ≤ 50% of
        long limits) consumes the SELL breakdown."""
        equity = getattr(self.risk, "_day_start_equity", None)
        if not equity:
            return None
        at_risk = 0.0
        for a in self.actors.values():
            if a.state in TERMINAL_STATES:
                continue
            if side is not None and a.spec.side is not side:
                continue
            # decision_price in the chain (torture sim 2026-09-16: a PENDING
            # MARKET entry has no avg/limit price, fell through to
            # stop_price and counted ZERO risk — 12 simultaneous market
            # signals were admitted at 12% committed risk vs the 6% ceiling)
            entry = (
                a.avg_entry_price
                or a.spec.limit_price
                or a.spec.decision_price
                or a.spec.stop_price
            )
            stop = a.exit_engine.current_stop if a.exit_engine is not None else a.spec.stop_price
            # audit A3-3: while the ENTRY order is still WORKING, a ladder
            # partial fill made `filled_qty or spec.qty` count only the
            # filled SLIVER — but the rest of the order can still fill, so
            # the committed risk is the FULL spec size (the 6% ceiling was
            # breachable ~2× across ladder partials). Once the entry is done
            # (OPEN and beyond), filled_qty is the truth as before.
            if a.state is PositionState.PENDING_ENTRY:
                qty = max(a.spec.qty, a.filled_qty or 0.0)
            else:
                qty = a.filled_qty or a.spec.qty
            direction = 1.0 if a.spec.side is OrderSide.BUY else -1.0
            at_risk += max(0.0, (entry - stop) * direction) * qty
        return at_risk / equity * 100.0

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> str:
        async with self._start_lock:  # concurrent starts serialize here
            return await self._start_locked()

    async def _start_locked(self) -> str:
        if self.state in (EngineState.RUNNING, EngineState.PAUSED, EngineState.STOPPING):
            return f"engine already {self.state.value}"
        mismatches = await self.reconcile()
        account = await self.adapter.get_account(self.mode)
        # A3-7: baselines are keyed by the ET session date (the old UTC date
        # stamped "tomorrow" for any 20:00–24:00 ET start, so the real
        # midnight roll then never fired)
        from waveapp.engine.session import SessionScheduler

        self.risk.on_session_start(account.equity, SessionScheduler.session_date(self._now()))
        if self._router_task is None or self._router_task.done():
            self._router_task = asyncio.ensure_future(self._route_updates())
        self._set_state(EngineState.RUNNING)
        note = f" ({mismatches} reconcile finding(s) — entries frozen)" if mismatches else ""
        await self._notify(
            "▶️ Trading started."
            + (
                f"\n⚠️ Found {mismatches} thing(s) that don't match the broker — "
                "new buys are blocked until it's checked."
                if mismatches
                else ""
            )
        )
        return f"engine running{note}"

    async def pause(self) -> str:
        if self.state is not EngineState.RUNNING:
            return f"cannot pause while {self.state.value}"
        self._set_state(EngineState.PAUSED)
        await self._notify(
            "⏸ Paused. No new buys. Open positions are still protected"
            " and will still sell on their own."
        )
        return "paused: no new entries; open positions still managed"

    async def resume(self) -> str:
        if self.state is not EngineState.PAUSED:
            return f"cannot resume while {self.state.value}"
        self._set_state(EngineState.RUNNING)
        await self._notify("▶️ Back to trading.")
        return "resumed"

    async def stop(self) -> str:
        if self.state not in (EngineState.RUNNING, EngineState.PAUSED):
            return f"cannot stop while {self.state.value}"
        self._set_state(EngineState.STOPPING)
        await self._notify("⏹ Stopping. No new buys. Selling open positions, then Wave rests.")
        # ACTIVE stop (2026-09-18: "pause and stop don't do what they
        # are supposed to" — the old stop WAITED for positions to close
        # themselves, which can take hours, while the UI locked both
        # buttons; he force-quit the app instead). Stop now asks every
        # actor to close through its managed exit path (marketable limit,
        # broker-truth clamped, re-protects itself on a failed submit) —
        # graceful wind-down, not the kill hammer.
        await self._issue_stop_closes()
        asyncio.ensure_future(self._finish_stop_when_flat())
        return "stopping: closing all positions; will idle once flat"

    async def _issue_stop_closes(self) -> list[str]:
        """Ask every live actor to close for the engine stop. Returns the
        symbols skipped because they are HALTED (§12: no order traffic into
        a halt — the wait loop keeps waiting for their resume instead).

        Audit A1-11: this used to fire ONCE per actor from stop() — an
        actor inside its close backoff, a HALTED actor, or one that
        reopened from a partial fill after the first ask simply DROPPED the
        close and STOPPING hung forever. Now the wait loop re-issues it
        every iteration: close_now's CLOSING/terminal idempotency and NVDG
        backoff make the repeat safe and self-paced."""
        halted: list[str] = []
        for actor in list(self.actors.values()):
            if actor.state in TERMINAL_STATES or actor.state is PositionState.CLOSING:
                continue
            if actor.state is PositionState.HALTED:
                halted.append(actor.spec.symbol)
                continue
            with contextlib.suppress(Exception):
                await actor.close_now("engine stop")
        return halted

    async def _finish_stop_when_flat(self) -> None:
        waited = 0
        halted: list[str] = []
        while self.open_actor_count > 0 and self.state is EngineState.STOPPING:
            await asyncio.sleep(1)
            waited += 1
            # audit A1-11: re-ask every iteration (idempotent + backoff-paced)
            halted = await self._issue_stop_closes()
            if waited == 120:  # the stop must never look hung (2026-09-18)
                halted_note = (
                    " Halted symbol(s) waiting for their resume: "
                    + ", ".join(sorted(set(halted)))
                    + "."
                    if halted
                    else ""
                )
                await self._notify(
                    "⏹ Still closing after 2 minutes — some position's exit is "
                    "slow. It stays protected; the kill switch remains available." + halted_note
                )
        if self.state is EngineState.STOPPING:
            self._set_state(EngineState.IDLE)
            await self._notify("⏹ Done. Everything is sold. Wave is resting.")

    CANCEL_SETTLE_SECONDS = 1.0  # let cancels release held shares (brackets)

    async def kill(self) -> str:
        """Flatten everything now, cancel everything, halt (hard rule: the
        kill switch is never weakened).

        Operates on BROKER TRUTH, not engine memory: positions that survived a
        restart (no live actor) are flattened too.

        Audit A1-1 rework — the old shape (cancel EVERY open order including
        every protective stop → wait 1s → submit MARKET/DAY flattens) had two
        holes: outside RTH the market orders QUEUED until the next 09:30 open
        (hours of naked positions created by the safety mechanism itself),
        and a HALTED symbol got a market order INTO the resume. Both violate
        §3 ("marketable limits; market orders allowed only in RTH and never
        on a halt resume"). Now:
        - per §3, the flatten is MARKET inside RTH and a marketable LIMIT
          with extended_hours outside (shared marketable_exit_fields);
        - a position's stop legs are canceled only IMMEDIATELY before that
          symbol's own flatten submit (Alpaca holds the shares against the
          legs, so the cancel must land first — the naked window is one
          cancel-settle per symbol, not the whole flatten pass), and a
          FAILED flatten submit re-places the protective stop (A1-5 pattern:
          never strand a position naked);
        - a HALTED symbol gets NO order traffic at all: its stop stays
          resting and the halt-resume machinery keeps owning it.
        """
        from waveapp.engine.orders import OrderAction, make_client_order_id, new_position_uuid

        self.risk.kill()
        self._set_state(EngineState.KILLED)

        live_actors: dict[str, PositionActor] = {
            a.spec.symbol: a for a in self.actors.values() if a.state not in TERMINAL_STATES
        }

        def _halted(symbol: str) -> bool:
            actor = live_actors.get(symbol)
            if actor is not None and actor.state is PositionState.HALTED:
                return True
            return self.risk.is_luld_halted(symbol)

        # audit A1-3 companion: for every symbol whose stop WE will cancel,
        # the actors' dead-stop watchdog must not fight the kill switch by
        # re-protecting the deliberate cancel while the flatten is in flight.
        # A HALTED symbol is left untouched (stop stays resting), so its
        # actor keeps its normal protection machinery armed.
        for actor in live_actors.values():
            if not _halted(actor.spec.symbol):
                actor._kill_flatten_active = True
            if actor.state is PositionState.PENDING_ENTRY:
                # A1-2 machinery: our cancel below must never let the ladder
                # re-peg or MARKET-fall-back a fresh entry after the kill
                actor._abandon_entry()

        canceled = flattened = 0
        open_orders: list = []
        with contextlib.suppress(Exception):
            open_orders = list(await self.adapter.get_open_orders(self.mode))
        positions: list = []
        with contextlib.suppress(Exception):
            positions = [p for p in await self.adapter.get_positions(self.mode) if p.qty]
        held_symbols = {p.symbol for p in positions}

        # phase 1: cancel every order NOT tied to held shares (pending
        # entries, exits, scales, strays). Orders on HELD symbols wait — each
        # is canceled right before its own symbol's flatten below, so no stop
        # dies before its replacement exit is on its way. Orders on HALTED
        # symbols are not touched at all (§12: the stop keeps protecting).
        for order in open_orders:
            if order.symbol in held_symbols or _halted(order.symbol):
                continue
            with contextlib.suppress(Exception):
                await self.adapter.cancel_order(self.mode, order.order_id)
                canceled += 1

        halted_skipped: list[str] = []
        for position in positions:
            symbol = position.symbol
            if _halted(symbol):
                halted_skipped.append(symbol)
                logger.warning(
                    "kill: %s is HALTED — never an order into the halt/resume (§3); "
                    "its stop stays resting and the halt-resume handling owns it",
                    symbol,
                )
                continue
            # cancel THIS symbol's resting orders (its protective stop legs
            # included) only now, immediately before its replacement flatten
            symbol_canceled = False
            for order in open_orders:
                if order.symbol != symbol:
                    continue
                with contextlib.suppress(Exception):
                    await self.adapter.cancel_order(self.mode, order.order_id)
                    canceled += 1
                    symbol_canceled = True
            if symbol_canceled:
                # Alpaca holds shares against resting bracket legs — the
                # flatten would be rejected until the cancel settles. This
                # settle IS the naked window, per symbol.
                await asyncio.sleep(self.CANCEL_SETTLE_SECONDS)

            exit_side = OrderSide.SELL if position.qty > 0 else OrderSide.BUY
            # A1-1: clock-aware flatten. Fallback reference is the broker
            # position payload (current_price = last trade; entry as last
            # resort) — the helper prefers the live quote's far touch.
            fields = marketable_exit_fields(
                exit_side,
                symbol,
                position.current_price or position.avg_entry_price,
                now_utc=self._now(),
            )
            request = OrderRequest(
                symbol=symbol,
                qty=abs(position.qty),
                side=exit_side,
                order_type=fields["order_type"],
                time_in_force=TimeInForce.DAY,
                limit_price=fields["limit_price"],
                extended_hours=fields["extended_hours"],
                client_order_id=make_client_order_id(new_position_uuid(), OrderAction.KILL, 1),
            )
            try:
                await self.adapter.submit_order(self.mode, request)
                flattened += 1
            except Exception:
                logger.exception(
                    "kill: flatten submit FAILED for %s — its stop was just "
                    "canceled; re-protecting now (hard rule 3)",
                    symbol,
                )
                await self._reprotect_after_failed_kill_flatten(
                    symbol, position, live_actors.get(symbol)
                )

        # actors learn their fate from the fill events; unfilled entries were
        # canceled above — mark them so they don't wait forever
        for actor in list(self.actors.values()):
            if actor.state is PositionState.PENDING_ENTRY:
                actor.state = PositionState.CLOSED
                actor.exit_reason = "KILL SWITCH"

        halted_note = (
            (
                f"\n⏸️ {len(halted_skipped)} halted symbol(s) untouched (never into a "
                f"resume): {', '.join(sorted(halted_skipped))} — stop(s) still resting; "
                "close manually after the resume."
            )
            if halted_skipped
            else ""
        )
        await self._notify(
            f"🛑 KILL SWITCH. Selling everything now ({flattened} positions), "
            f"canceled {canceled} orders. Trading is halted until you re-arm." + halted_note
        )
        return (
            f"kill switch executed: {canceled} order(s) canceled, "
            f"{flattened} position(s) flattening"
            + (f", {len(halted_skipped)} halted symbol(s) left protected" if halted_skipped else "")
            + ", engine halted"
        )

    async def _reprotect_after_failed_kill_flatten(
        self, symbol: str, position, actor: PositionActor | None
    ) -> None:
        """A1-1 failure path: this symbol's stop was canceled for the kill
        flatten and the flatten submit was refused — the position must never
        strand naked (hard rule 3). With a live actor, hand it back its own
        protection machinery (kill flag off so the watchdog, protective
        checks and exit cadence work again) and re-place through the A1-5
        path (remaining qty, tightest confirmed level, rule-3 flatten if
        unplaceable). Without one, submit a raw protective stop the way
        reconcile's adoption does."""
        if actor is not None:
            actor._kill_flatten_active = False
            actor.stop_leg_order_id = None
            with contextlib.suppress(Exception):
                await actor._place_protective_stop()
            return
        from waveapp.engine.orders import OrderAction, make_client_order_id, new_position_uuid

        long_side = position.qty > 0
        reference = position.current_price or position.avg_entry_price
        factor = 1 - self.ADOPT_FALLBACK_STOP_PCT / 100.0
        stop_price = round(reference * (factor if long_side else 2 - factor), 2)
        request = OrderRequest(
            symbol=symbol,
            qty=abs(position.qty),
            side=OrderSide.SELL if long_side else OrderSide.BUY,
            order_type=OrderType.STOP,
            time_in_force=TimeInForce.DAY,
            stop_price=stop_price,
            client_order_id=make_client_order_id(new_position_uuid(), OrderAction.STOP, 1),
        )
        try:
            await self.adapter.submit_order(self.mode, request)
            logger.warning(
                "kill: protective stop re-placed for actorless %s at %s", symbol, stop_price
            )
        except Exception:
            logger.critical(
                "kill: %s is NAKED — the flatten was refused AND the protective "
                "stop could not be placed",
                symbol,
            )
            await self._notify(
                f"🚨 {symbol} could not be sold OR re-protected during the kill — "
                "check the broker NOW."
            )

    # -- entries -------------------------------------------------------------

    async def open_position(self, spec: PositionSpec) -> PositionActor | str:
        """The single entry path. Returns the actor, or a rejection reason."""
        if self.state is not EngineState.RUNNING:
            return f"engine not running ({self.state.value})"
        # one position per symbol (2026-08-19: GDX was bought twice after a
        # restart — the signal dedupe is in-memory, this guard is not).
        # NAMED INVARIANT (2026-09-03, timing hunt II.27): NEVER ADD SIZE TO A
        # POSITION BELOW COST. Every credible source converges on this (prop
        # hard-breach lists, Odean's loser-holding data). Any future add-to-
        # position/pyramiding feature MUST route through RiskEngine with an
        # explicit above-cost check — weakening this guard is a rule-2 event.
        held = {a.spec.symbol for a in self.actors.values() if a.state not in TERMINAL_STATES}
        if spec.symbol in held:
            logger.warning("entry rejected: already holding %s", spec.symbol)
            return f"already holding {spec.symbol}"
        decision = self.risk.can_enter(
            symbol=spec.symbol,
            side=spec.side,
            open_positions=self.open_actor_count,
            today=self._now().date(),
            open_risk_pct=self._open_risk_pct(),
            # S1 short-book share (§12): a SELL entry also proves its own
            # book's committed risk; longs pass None (gate not applicable)
            short_open_risk_pct=(
                self._open_risk_pct(side=OrderSide.SELL) if spec.side is OrderSide.SELL else None
            ),
        )
        if not decision:
            logger.warning("entry rejected: %s", decision.reason)
            return decision.reason
        actor = PositionActor(
            spec, self.adapter, self.mode, on_event=self._on_actor_event, on_trade=self._on_trade
        )
        self.actors[actor.position_key] = actor
        self._persist_position(actor)
        actor.start()
        return actor

    def _on_actor_event(self, actor: PositionActor, text: str) -> None:
        self._persist_position(actor)
        if self._push is not None:
            asyncio.ensure_future(self._notify(telegram_trade_line(actor, text)))

    # -- event routing -------------------------------------------------------

    async def _route_updates(self) -> None:
        try:
            async for update in self.adapter.trade_updates():
                self._persist_update(update)
                parsed = parse_client_order_id(update.order.client_order_id)
                target = None
                if parsed is not None:
                    target = self.actors.get(parsed.position_key)
                if target is None:
                    # bracket legs carry broker-generated ids. Route by the
                    # actor's KNOWN stop-leg id first (rehearsal 2026-09-20
                    # B3: under the Duel, first-match-by-symbol delivered the
                    # judge twin's stop fill to the KITCHEN actor — wrong
                    # cohort booked, the real holder zombied); symbol match
                    # is the fallback for legs no actor has learned yet.
                    for actor in self.actors.values():
                        if (
                            getattr(actor, "stop_leg_order_id", None) == update.order.order_id
                            and actor.state not in TERMINAL_STATES
                        ):
                            target = actor
                            break
                if target is None:
                    for actor in self.actors.values():
                        if (
                            actor.spec.symbol == update.order.symbol
                            and actor.state not in TERMINAL_STATES
                        ):
                            target = actor
                            break
                if target is not None:
                    target.deliver(update)
                else:
                    logger.warning(
                        "trade update for unknown order %s (%s)",
                        update.order.client_order_id,
                        update.order.symbol,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("update router died")

    # -- reconciliation ------------------------------------------------------

    async def poll_missed_fills(self) -> int:
        """Deafness insurance (2026-08-24): even with the trade-update stream
        down, fills are discovered by REST-polling each live actor's known
        orders (entry + stop leg) and replaying them through the normal
        handler. The actors' cumulative-delta accounting makes a later
        duplicate stream event harmless. Returns how many updates replayed."""
        from types import SimpleNamespace

        from waveapp.engine.actor import TERMINAL_STATES

        # audit A3-7: this is the engine's periodic (per-poll) hook — the
        # cheap day-roll check rides it so a continuous 24/5 run rebases the
        # risk baselines at each new ET session date
        await self._maybe_roll_risk_day()
        replayed = 0
        from waveapp.engine.actor import PositionState

        for actor in list(self.actors.values()):
            if actor.state in TERMINAL_STATES:
                continue
            # the entry order matters only while we WAIT for it (the entry
            # path has no cumulative-delta map — state is its dedupe)
            candidates = []
            if actor.state is PositionState.PENDING_ENTRY and actor.entry_order_id:
                candidates.append(actor.entry_order_id)
            if actor.state is not PositionState.PENDING_ENTRY and actor.stop_leg_order_id:
                candidates.append(actor.stop_leg_order_id)
            # audit A1-9: a CLOSING actor waits on its exit order — with the
            # stream deaf, a missed exit fill left it stuck in CLOSING
            if actor.state is PositionState.CLOSING and getattr(actor, "exit_order_id", None):
                candidates.append(actor.exit_order_id)
            for order_id in candidates:
                try:
                    info = await self.adapter.get_order(self.mode, order_id)
                except Exception:  # noqa: S112 — REST probe; silence is fine
                    continue
                status = str(getattr(info.status, "value", info.status)).lower()
                if status == "filled" and float(info.filled_qty or 0) > 0:
                    already = getattr(actor, "_filled_seen", {}).get(order_id, 0.0)
                    if float(info.filled_qty) > already:
                        logger.warning(
                            "[%s] REST fallback replaying missed fill for %s",
                            actor.spec.symbol,
                            order_id,
                        )
                        await actor._handle(SimpleNamespace(event="fill", order=info))
                        replayed += 1
        return replayed

    async def _maybe_roll_risk_day(self) -> None:
        """Audit A3-7: on_session_start's only caller was engine start, so a
        continuous 24/5 run never rolled the day/week loss baselines (and a
        daily halt never auto-re-armed) until the next restart. Cheap date
        compare every poll; only an actual ET-date change fetches equity.
        The roll itself is the EXISTING on_session_start — daily-halt
        auto-re-arm preserved exactly, weekly/kill halts untouched — and it
        never fires on unknown equity (maybe_roll_session refuses)."""
        try:
            from waveapp.engine.session import SessionScheduler

            today = SessionScheduler.session_date(self._now())
            current = getattr(self.risk, "_current_day", None)
            if current is None or today == current:
                return  # never started (start() seeds) or same session day
            equity = None
            with contextlib.suppress(Exception):
                account = await self.adapter.get_account(self.mode)
                equity = account.equity
            self.risk.maybe_roll_session(equity, today)
        except Exception:
            logger.exception("risk day-roll check failed")

    async def reconcile(self) -> int:
        """Broker state vs Wave state BEFORE trading (§3). Positions that
        survived an app restart are ADOPTED (2026-08-19, after KORU/MRVL sat
        frozen and unmanaged): a new actor re-attaches to the broker position
        and its resting server-side stop. Only what can't be adopted or
        protected freezes entries."""
        from waveapp.engine.orders import OrderAction

        findings = 0
        open_orders = await self.adapter.get_open_orders(self.mode)
        # Stale Wave ENTRY orders from a dead run are CANCELED first
        # (2026-08-20: MRNA's entry was in flight during a restart — it
        # filled 16s after reconcile froze entries and the position sat
        # invisible/unmanaged). Cancel-then-refetch: whatever fills before
        # the cancel lands shows up in get_positions below and gets adopted.
        canceled_ids: set[str] = set()
        for order in open_orders:
            parsed = parse_client_order_id(order.client_order_id)
            if (
                parsed is not None
                and parsed.position_key not in self.actors
                and parsed.action == OrderAction.ENTRY
            ):
                with contextlib.suppress(Exception):
                    await self.adapter.cancel_order(self.mode, order.order_id)
                    canceled_ids.add(order.order_id)
                    logger.warning(
                        "reconcile: canceled stale entry %s (%s) from a previous run",
                        order.client_order_id,
                        order.symbol,
                    )
        if canceled_ids:
            await asyncio.sleep(self.CANCEL_SETTLE_SECONDS)
            open_orders = await self.adapter.get_open_orders(self.mode)
        positions = await self.adapter.get_positions(self.mode)
        known_symbols = {
            a.spec.symbol for a in self.actors.values() if a.state not in TERMINAL_STATES
        }
        adopted_stop_ids: set[str] = set()
        for position in positions:
            live_now = {
                a.spec.symbol for a in self.actors.values() if a.state not in TERMINAL_STATES
            }
            if position.symbol in known_symbols or position.symbol in live_now:
                continue
            if not position.qty:
                continue
            actor = await self._adopt_position(position, open_orders)
            if actor is not None:
                if actor.stop_leg_order_id:
                    adopted_stop_ids.add(actor.stop_leg_order_id)
                logger.info(
                    "reconcile: adopted %s (%g @ %.4f, stop %s) from a previous run",
                    position.symbol,
                    position.qty,
                    position.avg_entry_price,
                    actor.spec.stop_price,
                )
                await self._notify(
                    f"♻️ Picked up {position.symbol} again after the restart "
                    f"({abs(position.qty):g} shares, safety stop at ${actor.spec.stop_price})"
                )
            else:
                findings += 1
                logger.error(
                    "reconcile: broker position %s (%g) could not be adopted",
                    position.symbol,
                    position.qty,
                )
        live_symbols = {
            a.spec.symbol for a in self.actors.values() if a.state not in TERMINAL_STATES
        }
        for order in open_orders:
            parsed = parse_client_order_id(order.client_order_id)
            if parsed is None or parsed.position_key in self.actors:
                continue
            if order.order_id in adopted_stop_ids or order.symbol in live_symbols:
                continue  # protecting/serving an adopted position — accounted for
            if order.order_id in canceled_ids:
                continue  # canceled above — broker listings may lag the cancel
            findings += 1
            logger.error(
                "reconcile: Wave order %s at broker with no live actor",
                order.client_order_id,
            )
        # book trades the broker closed while Wave was OFFLINE (2026-08-20:
        # a stop filled during a restart and the trade never reached the
        # Performance tab, scoreboard or log)
        with contextlib.suppress(Exception):
            await self._recover_offline_closes({p.symbol for p in positions if p.qty})
        if findings:
            self.risk.freeze_entries("reconcile mismatch")
            await self._notify(
                f"⚠️ Numbers don't match the broker ({findings} issue(s)). "
                "New buys blocked until it's safe."
            )
        else:
            self.risk.unfreeze_entries("reconcile mismatch")
        return findings

    async def _recover_offline_closes(self, held_symbols: set[str]) -> None:
        """DB rows still marked open whose position is GONE at the broker:
        the exit filled while no Wave was running. Recover the fill from
        broker order history and book the close (state/closed_at/pnl) so
        Performance, scoreboard and the log stay truthful. Older duplicate
        rows for the same symbol (crashed-run re-persists) are marked
        'superseded' — never fabricated into trades."""
        if self.db is None:
            return
        rows = self.db.query(
            "SELECT position_uuid, symbol, side, qty, avg_entry, opened_at FROM positions"
            " WHERE trading_mode=? AND state NOT IN ('closed','error','superseded')"
            " ORDER BY opened_at DESC",
            (self.mode.value,),
        )
        live_uuids = {a.uuid for a in self.actors.values()}
        seen_symbols: set[str] = set()
        trade_log = logging.getLogger("wave.trade.recovery")
        for row in rows:
            if row["position_uuid"] in live_uuids:
                seen_symbols.add(row["symbol"])
                continue
            symbol = row["symbol"]
            # 2026-08-20 double-count bug: if ANOTHER row for this symbol
            # already closed after this one opened, the economics are
            # captured — recovering this row would FABRICATE a second trade
            # (WMT/ETHA duplicates on day 2). Supersede instead. A row that
            # never filled (NULL avg_entry) has nothing to recover either.
            already_booked = self.db.query(
                "SELECT 1 FROM positions WHERE symbol=? AND trading_mode=?"
                " AND state='closed' AND closed_at >= ? LIMIT 1",
                (symbol, self.mode.value, row["opened_at"]),
            )
            recoverable = (
                symbol not in held_symbols
                and symbol not in seen_symbols
                and not already_booked
                and row["avg_entry"] is not None
            )
            seen_symbols.add(symbol)
            booked = False
            if recoverable:
                try:
                    booked = await self._book_offline_close(row, trade_log)
                except Exception:  # one bad row must never abort the sweep
                    logger.exception("offline-close recovery failed for %s", symbol)
            if not booked:
                self.db.execute(
                    "UPDATE positions SET state='superseded' WHERE position_uuid=?",
                    (row["position_uuid"],),
                )

    async def _book_offline_close(self, row, trade_log) -> bool:
        try:
            opened_at = datetime.fromisoformat(str(row["opened_at"]))
        except (ValueError, TypeError):
            return False
        try:
            history = await self.adapter.get_closed_orders(self.mode, row["symbol"], opened_at)
        except Exception:
            logger.exception("offline-close recovery: order history failed (%s)", row["symbol"])
            return False
        exit_side = "sell" if row["side"] == "long" else "buy"
        fills = [
            o for o in history if o.side.value == exit_side and o.filled_avg_price and o.filled_qty
        ]
        if not fills:
            return False
        last = max(fills, key=lambda o: o.filled_at or o.submitted_at or opened_at)
        direction = 1.0 if row["side"] == "long" else -1.0
        pnl = (last.filled_avg_price - float(row["avg_entry"])) * float(row["qty"]) * direction
        closed_at = (last.filled_at or last.submitted_at or datetime.now(UTC)).isoformat()
        self.db.execute(
            "UPDATE positions SET state='closed', closed_at=?, realized_pnl=?,"
            " exit_path='broker close (Wave offline)' WHERE position_uuid=?",
            (closed_at, round(pnl, 2), row["position_uuid"]),
        )
        verdict = "WIN" if pnl > 0 else "LOSS"
        trade_log.info(
            "[%s] closed while Wave was offline @ %.2f — %s %s $ (recovered from broker history)",
            row["symbol"],
            last.filled_avg_price,
            verdict,
            f"{pnl:+,.2f}",
        )
        return True

    ADOPT_FALLBACK_STOP_PCT = 3.0  # naked adopted position: protective stop 3% away

    async def _adopt_position(self, position, open_orders) -> PositionActor | None:
        """Build an OPEN actor around a broker position from a previous run.
        Hard rule 3: a resting server-side stop is located first; if none
        rests, a protective stop is submitted before adopting. Returns None
        only when the position cannot be protected."""
        from waveapp.broker.base import OrderRequest, OrderType, TimeInForce
        from waveapp.engine.orders import OrderAction, make_client_order_id

        symbol = position.symbol
        long_side = position.qty > 0
        qty = abs(position.qty)

        # audit A1-10: collect ALL resting stop legs, not just the first —
        # the merged-twin restart shape leaves TWO legs on one position, and
        # a second surviving leg firing after the (re-protected) first is a
        # stop CASCADE that flips the position short.
        stop_legs: list = []
        seen_leg_ids: set[str] = set()
        for order in open_orders:
            for leg in (order, *order.legs):
                if (
                    leg.symbol == symbol
                    and leg.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
                    and leg.stop_price
                    and leg.order_id not in seen_leg_ids
                ):
                    seen_leg_ids.add(leg.order_id)
                    stop_legs.append(leg)
        stop_order = stop_legs[0] if stop_legs else None

        strategy = "ADOPTED"
        manager = "kitchen"  # DUEL: twins keep their manager across restarts
        original_opened_at = None  # keeps the time-stop clock honest (quirk fix)
        if self.db is not None:
            with contextlib.suppress(Exception):
                rows = self.db.query(
                    "SELECT strategy, opened_at, manager FROM positions WHERE symbol=?"
                    " AND trading_mode=? ORDER BY opened_at DESC LIMIT 1",
                    (symbol, self.mode.value),
                )
                if rows:
                    strategy = rows[0]["strategy"] or strategy
                    manager = rows[0]["manager"] or manager
                    with contextlib.suppress(ValueError, TypeError):
                        original_opened_at = datetime.fromisoformat(str(rows[0]["opened_at"]))
        spec_side = OrderSide.BUY if long_side else OrderSide.SELL

        # COVERAGE NET (duel rehearsal 2026-09-20 S6: a mid-duel restart
        # merges twins into one 2×qty position whose inherited stop leg
        # covers HALF the shares — a rule-3 hole). Audit A1-10 rework: the
        # old net canceled only the FIRST undersized leg (a second merged-
        # twin leg survived → stop cascade) and did it inside a blanket
        # suppress AFTER the qty read, so a failed cancel silently kept the
        # undersized leg trusted as "protection". Now: an undersized first
        # leg OR any extra leg triggers a full re-protect — belief is
        # dropped BEFORE the awaited cancels, and EVERY leg is canceled in
        # its own try/except, loudly.
        if stop_order is not None:
            leg_qty = 0.0
            with contextlib.suppress(Exception):
                leg_qty = abs(float(getattr(stop_order, "qty", 0) or 0))
            undersized = bool(leg_qty) and leg_qty < qty * 0.99
            if undersized or len(stop_legs) > 1:
                logger.warning(
                    "adopted %s: %d resting stop leg(s), first covers %.6g of %.6g "
                    "shares — canceling ALL and replacing with full-size protection",
                    symbol,
                    len(stop_legs),
                    leg_qty,
                    qty,
                )
                # belief first (per the auditor): if a cancel below fails,
                # the fallback still places FULL-size protection instead of
                # trusting an undersized/duplicated leg
                stop_order = None
                for leg in stop_legs:
                    try:
                        await self.adapter.cancel_order(self.mode, leg.order_id)
                    except Exception:
                        logger.warning(
                            "adopted %s: cancel of resting stop %s FAILED — it may "
                            "still rest beside the full-size replacement; check the "
                            "broker",
                            symbol,
                            leg.order_id,
                            exc_info=True,
                        )
        if stop_order is not None:
            stop_price = float(stop_order.stop_price)
            stop_leg_id = stop_order.order_id
        else:
            # no resting stop → submit protection BEFORE adopting (hard rule 3)
            reference = position.current_price or position.avg_entry_price
            factor = 1 - self.ADOPT_FALLBACK_STOP_PCT / 100.0
            stop_price = round(reference * (factor if long_side else 2 - factor), 2)
            from waveapp.engine.orders import new_position_uuid

            request = OrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL if long_side else OrderSide.BUY,
                order_type=OrderType.STOP,
                time_in_force=TimeInForce.DAY,
                stop_price=stop_price,
                client_order_id=make_client_order_id(new_position_uuid(), OrderAction.EXIT, 1),
            )
            try:
                info = await self.adapter.submit_order(self.mode, request)
                stop_leg_id = info.order_id
                logger.warning(
                    "reconcile: %s had NO resting stop — protective stop placed at %s",
                    symbol,
                    stop_price,
                )
            except Exception:
                logger.exception("reconcile: protective stop for %s failed", symbol)
                return None

        spec = PositionSpec(
            symbol=symbol,
            side=spec_side,
            qty=qty,
            stop_price=stop_price,
            strategy=strategy,
            manager=manager,
        )
        actor = PositionActor.adopt(
            spec,
            self.adapter,
            self.mode,
            filled_qty=qty,
            avg_entry_price=position.avg_entry_price,
            stop_leg_order_id=stop_leg_id,
            on_event=self._on_actor_event,
            on_trade=self._on_trade,
            adopted_entry_time=original_opened_at,
        )
        self.actors[actor.position_key] = actor
        self._persist_position(actor)
        actor.start()
        return actor

    # -- persistence ---------------------------------------------------------

    def _persist_position(self, actor: PositionActor) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
                " strategy, opened_at, trading_mode, decision_price, manager)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(position_uuid) DO UPDATE SET"
                " qty=excluded.qty, avg_entry=excluded.avg_entry, state=excluded.state,"
                " decision_price=COALESCE(positions.decision_price, excluded.decision_price)",
                (
                    actor.uuid,
                    actor.spec.symbol,
                    "long" if actor.spec.side is OrderSide.BUY else "short",
                    actor.filled_qty or actor.spec.qty,
                    actor.avg_entry_price,
                    actor.state.value,
                    actor.spec.strategy,
                    # adopted positions keep their ORIGINAL open time — each
                    # restart used to stamp adoption-time, and the NEXT
                    # restart read that as the original: the entry crept
                    # 9:35→9:42→9:59 across restarts (2026-08-24)
                    (
                        actor.adopted_entry_time.isoformat()
                        if getattr(actor, "adopted_entry_time", None) is not None
                        else utc_now()
                    ),
                    self.mode.value,
                    actor.spec.decision_price,
                    getattr(actor.spec, "manager", "kitchen"),
                ),
            )
            if actor.state is PositionState.CLOSED:
                # a canceled entry that never filled a share is not a trade —
                # perf_hidden keeps it OFF the Performance graph (the owner was
                # deleting the +0 phantom auction cancels by hand all week)
                never_filled = 1 if (actor.filled_qty or 0) == 0 else 0
                self.db.execute(
                    "UPDATE positions SET closed_at=?, exit_path=?, realized_pnl=?,"
                    " perf_hidden=MAX(COALESCE(perf_hidden,0), ?)"
                    " WHERE position_uuid=?",
                    (utc_now(), actor.exit_reason, actor.realized_pnl, never_filled, actor.uuid),
                )
        except Exception:
            logger.exception("persist position failed")

    def _persist_update(self, update) -> None:
        if self.db is None:
            return
        try:
            order = update.order
            parsed = parse_client_order_id(order.client_order_id)
            self.db.execute(
                "INSERT INTO orders (order_id, client_order_id, position_uuid, symbol, side,"
                " qty, order_type, time_in_force, limit_price, stop_price, status, filled_qty,"
                " filled_avg_price, trading_mode, submitted_at, updated_at, raw_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,"
                " filled_qty=excluded.filled_qty, filled_avg_price=excluded.filled_avg_price,"
                " updated_at=excluded.updated_at",
                (
                    order.order_id,
                    order.client_order_id or f"foreign-{order.order_id}",
                    parsed.position_key if parsed else None,
                    order.symbol,
                    order.side.value,
                    order.qty,
                    order.order_type.value,
                    "day",
                    order.limit_price,
                    order.stop_price,
                    order.status.value,
                    order.filled_qty,
                    order.filled_avg_price,
                    self.mode.value,
                    str(order.submitted_at) if order.submitted_at else None,
                    utc_now(),
                    None,
                ),
            )
            if update.event in ("fill", "partial_fill") and order.filled_avg_price:
                self.db.execute(
                    "INSERT INTO fills (order_id, symbol, side, qty, price, ts, raw_json)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        order.order_id,
                        order.symbol,
                        order.side.value,
                        order.filled_qty,
                        order.filled_avg_price,
                        utc_now(),
                        json.dumps({"event": update.event}),
                    ),
                )
        except Exception:
            logger.exception("persist update failed")

    # -- adaptive exits: live bar routing (§8.2, Phase 6) --------------------

    def on_market_bar(self, bar, hub) -> None:
        """A closed 1-minute bar from the DataHub: initialize/feed the exit
        brain of every actor holding this symbol. HALTED actors are skipped
        entirely (LULD: hold, stop stays server-side, no order traffic)."""
        from waveapp.engine.exits import ExitEngine, compute_atr
        from waveapp.engine.session import SessionScheduler

        for actor in self.actors.values():
            if actor.spec.symbol != bar.symbol:
                continue
            if actor.state not in (PositionState.OPEN, PositionState.SCALING_OUT):
                continue
            atr = compute_atr(hub.bar_builder.bars(bar.symbol))
            if actor.exit_engine is None:
                if actor.avg_entry_price is None:
                    continue
                info = SessionScheduler.info(self._now())
                seed_atr = atr or abs(actor.avg_entry_price - actor.spec.stop_price) / 1.5
                # adopted positions keep their ORIGINAL entry clock (2026-08-19
                # quirk: GDX's 90-min time stop restarted at adoption)
                entry_time = getattr(actor, "adopted_entry_time", None) or bar.start
                actor.exit_engine = ExitEngine(
                    side=actor.spec.side,
                    entry_price=actor.avg_entry_price,
                    qty=actor.filled_qty or actor.spec.qty,
                    atr_at_entry=seed_atr,
                    params=self._exit_params_fn_for(
                        info.regime, actor.spec.strategy, actor.spec.symbol
                    ),
                    entry_time=entry_time,
                    entry_bar_volume=bar.volume,
                    initial_stop=actor.spec.stop_price,
                )
                continue  # first bar seeds the brain; decisions start next bar
            boundary = SessionScheduler.next_closed_boundary(self._now())
            minutes_to_boundary = (boundary - self._now()).total_seconds() / 60.0
            # daily-close flatten (2026-08-19): DAY bracket stops EXPIRE at
            # 16:00 ET, and overnight holding is unvalidated (§6) — the daily
            # close is a flatten boundary exactly like the weekend one
            rth_minutes = SessionScheduler.minutes_to_rth_close(self._now())
            if rth_minutes is not None:
                minutes_to_boundary = min(minutes_to_boundary, rth_minutes)
            decision = actor.exit_engine.on_bar(
                bar,
                atr=atr,
                minutes_to_close_boundary=minutes_to_boundary,
                session_vwap=hub.session_vwap(bar.symbol),
            )
            if decision.action.value != "none":
                asyncio.ensure_future(actor.apply_exit_decision(decision))

    # -- live safety hooks ---------------------------------------------------

    def on_symbol_halt(self, symbol: str, halted: bool) -> None:
        """LULD trading-status wiring (§12): update the risk gate and put any
        actor holding the symbol into/out of HALTED."""
        self.risk.set_luld_halted(symbol, halted)
        for actor in self.actors.values():
            if actor.spec.symbol.upper() == symbol.upper():
                if halted:
                    actor.enter_halt()
                else:
                    actor.exit_halt()

    async def open_test_position(self, symbol: str, last_price: float) -> str:
        """Phase 5 demo: 1 share, market entry, server-side stop 1% below.
        Goes through the exact same gate and actor path as real strategies."""
        stop = round(last_price * 0.99, 2)
        spec = PositionSpec(
            symbol=symbol.upper(),
            side=OrderSide.BUY,
            qty=1,
            stop_price=stop,
            strategy="TEST",
        )
        result = await self.open_position(spec)
        if isinstance(result, str):
            return f"test entry rejected: {result}"
        return f"test entry submitted: {symbol.upper()} 1 share @ market, server-side stop {stop}"

    # -- Telegram CommandSink ------------------------------------------------

    def command_sink(self):
        """Adapter for the TelegramBridge's CommandSink protocol."""
        engine = self

        class _Sink:
            async def pause(self) -> str:
                return await engine.pause()

            async def resume(self) -> str:
                return await engine.resume()

            async def stop(self) -> str:
                return await engine.stop()

            async def kill(self) -> str:
                return await engine.kill()

        return _Sink()

    async def shutdown(self) -> None:
        if self._router_task is not None:
            self._router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._router_task
            self._router_task = None
