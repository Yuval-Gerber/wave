"""Phase 5 step 5.1: engine core logic — RiskEngine, PositionActor, EngineCore
with a scripted fake broker. No network."""

import asyncio
import contextlib
from datetime import UTC, date, datetime, timedelta

import pytest

from waveapp.broker.base import (
    BrokerAdapter,
    OrderInfo,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    TradeUpdate,
    TradingMode,
)
from waveapp.engine.actor import PositionActor, PositionSpec, PositionState
from waveapp.engine.core import EngineCore, EngineState
from waveapp.engine.orders import make_client_order_id, parse_client_order_id
from waveapp.engine.risk import HaltState, RiskEngine, RiskLimits

TODAY = date(2026, 8, 14)


# -- fake broker -------------------------------------------------------------


class FakeBroker(BrokerAdapter):
    """Records orders; fills are pushed manually by tests."""

    def __init__(self) -> None:
        super().__init__(TradingMode.PAPER)
        self.submitted: list = []
        self.canceled: list[str] = []
        self.replaced: list = []
        self.open_orders: list[OrderInfo] = []
        self.positions: list = []
        self._queue: asyncio.Queue[TradeUpdate] = asyncio.Queue()
        self._counter = 0
        # A1-1 order-of-operations audit trail: cancels and submits in the
        # exact sequence the broker saw them
        self.call_log: list[tuple] = []

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    @property
    def is_connected(self) -> bool:
        return True

    async def get_account(self, mode):
        from waveapp.broker.base import AccountSnapshot

        self._check_mode(mode)
        return AccountSnapshot("FAKE", 100_000.0, 100_000.0, 400_000.0)

    async def get_clock(self, mode):
        raise NotImplementedError

    async def get_assets(self, mode):
        return []

    async def submit_order(self, mode, request):
        self._check_mode(mode)
        self._counter += 1
        info = OrderInfo(
            order_id=f"o{self._counter}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            filled_qty=0,
            order_type=request.order_type,
            status=OrderStatus.NEW,
        )
        self.submitted.append(request)
        self.call_log.append(
            ("submit", request.symbol, request.order_type, request.client_order_id)
        )
        return info

    async def cancel_order(self, mode, order_id):
        self.canceled.append(order_id)
        self.call_log.append(("cancel", order_id))

    async def replace_order(self, mode, order_id, qty=None, limit_price=None, stop_price=None):
        self._check_mode(mode)
        self._counter += 1
        self.replaced.append((order_id, qty, stop_price))
        return OrderInfo(
            order_id=f"r{self._counter}",
            client_order_id="",
            symbol="SPY",
            side=OrderSide.SELL,
            qty=qty or 1,
            filled_qty=0,
            order_type=OrderType.STOP,
            status=OrderStatus.NEW,
            stop_price=stop_price,
        )

    async def get_open_orders(self, mode):
        return list(self.open_orders)

    async def get_positions(self, mode):
        return list(self.positions)

    async def trade_updates(self):
        while True:
            yield await self._queue.get()

    # test helpers
    def push_update(
        self,
        event,
        *,
        order_id,
        client_order_id,
        symbol,
        side,
        order_type=OrderType.MARKET,
        qty=1,
        filled_qty=1,
        price=100.0,
    ):
        self._queue.put_nowait(
            TradeUpdate(
                event=event,
                order=OrderInfo(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    filled_qty=filled_qty,
                    order_type=order_type,
                    status=OrderStatus.FILLED if event == "fill" else OrderStatus.NEW,
                    filled_avg_price=price,
                ),
                timestamp=datetime.now(UTC),
            )
        )


def _spec(symbol="SPY", side=OrderSide.BUY, qty=1, stop=95.0):
    return PositionSpec(symbol=symbol, side=side, qty=qty, stop_price=stop)


async def _drain():
    for _ in range(10):
        await asyncio.sleep(0)


# -- order ids ---------------------------------------------------------------


def test_client_order_id_roundtrip():
    cid = make_client_order_id("abcdef0123456789", "entry", 2)
    assert cid == "wave-abcdef012345-entry-2"
    parsed = parse_client_order_id(cid)
    assert parsed.position_key == "abcdef012345"
    assert parsed.action == "entry" and parsed.attempt == 2
    assert parse_client_order_id("manual-order-1") is None
    assert parse_client_order_id("wave-x-y-z") is None


# -- risk engine -------------------------------------------------------------


def test_position_sizing_long_short_overnight():
    risk = RiskEngine()
    # $100k, 1% risk = $1000 budget; $2 to stop → 500 by risk, but 500×$100
    # is HALF the account — the notional cap (25%, 2026-08-18) trims to 250
    assert risk.position_size(100_000, 100.0, 98.0, OrderSide.BUY) == 250
    # $4 to stop → 250 by risk, $25k notional: exactly at the cap
    assert risk.position_size(100_000, 100.0, 96.0, OrderSide.BUY) == 250
    # $8 to stop → 125 by risk, well under the cap: risk math rules
    assert risk.position_size(100_000, 100.0, 92.0, OrderSide.BUY) == 125
    # short book halved (after the cap)
    assert risk.position_size(100_000, 100.0, 102.0, OrderSide.SELL) == 125
    # overnight halved budget
    assert risk.position_size(100_000, 100.0, 98.0, OrderSide.BUY, overnight=True) == 250
    # degenerate stop distance
    assert risk.position_size(100_000, 100.0, 100.0, OrderSide.BUY) == 0


def test_daily_loss_halt_and_auto_rearm():
    risk = RiskEngine()
    risk.on_session_start(100_000, TODAY)
    risk.update_equity(97_500)  # -2.5% — fine
    assert risk.halt_state is HaltState.NONE
    risk.update_equity(96_900)  # -3.1% — halt
    assert risk.halt_state is HaltState.DAILY_LOSS
    assert not risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    # next session day: auto re-arm
    risk.on_session_start(96_900, date(2026, 8, 17))
    assert risk.halt_state is HaltState.NONE


def test_weekly_loss_halt_requires_manual_rearm():
    risk = RiskEngine()
    risk.on_session_start(100_000, date(2026, 8, 10))  # Monday
    risk.update_equity(93_900)  # -6.1%
    assert risk.halt_state is HaltState.WEEKLY_LOSS
    risk.on_session_start(93_900, date(2026, 8, 11))  # new day does NOT re-arm
    assert risk.halt_state is HaltState.WEEKLY_LOSS
    risk.re_arm_weekly()
    assert risk.halt_state is HaltState.NONE


def test_max_positions_and_freezes():
    risk = RiskEngine(limits=RiskLimits(max_positions=2))
    assert risk.can_enter("SPY", OrderSide.BUY, 1, TODAY)
    assert not risk.can_enter("SPY", OrderSide.BUY, 2, TODAY)
    risk.freeze_entries("stale data")
    decision = risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    assert not decision and "stale data" in decision.reason
    risk.unfreeze_entries("stale data")
    assert risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)


def test_ssr_blocks_shorts_until_next_trading_day():
    risk = RiskEngine(limits=RiskLimits(shorts_enabled=True))  # isolate SSR (S1)
    risk.record_ssr_trigger("TSLA", date(2026, 8, 14))  # Friday
    assert not risk.can_enter("TSLA", OrderSide.SELL, 0, date(2026, 8, 14))
    assert not risk.can_enter("TSLA", OrderSide.SELL, 0, date(2026, 8, 17))  # Monday
    assert risk.can_enter("TSLA", OrderSide.SELL, 0, date(2026, 8, 18))  # Tuesday ok
    assert risk.can_enter("TSLA", OrderSide.BUY, 0, date(2026, 8, 14))  # longs fine


def test_luld_blocks_entries_both_sides():
    risk = RiskEngine()
    risk.set_luld_halted("NVDA", True)
    assert not risk.can_enter("NVDA", OrderSide.BUY, 0, TODAY)
    assert not risk.can_enter("NVDA", OrderSide.SELL, 0, TODAY)
    risk.set_luld_halted("NVDA", False)
    assert risk.can_enter("NVDA", OrderSide.BUY, 0, TODAY)


def test_kill_is_terminal_for_entries():
    risk = RiskEngine()
    risk.kill()
    assert risk.halt_state is HaltState.KILLED
    assert not risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    risk.update_equity(200_000)  # nothing un-kills
    assert risk.halt_state is HaltState.KILLED


# -- actor -------------------------------------------------------------------


def test_actor_requires_stop():
    """Hard rule 3: a position without a server-side stop cannot exist."""
    spec = PositionSpec(symbol="SPY", side=OrderSide.BUY, qty=1, stop_price=0)
    with pytest.raises(ValueError):
        PositionActor(spec, FakeBroker(), TradingMode.PAPER)


async def test_actor_entry_fill_and_stop_close():
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    assert actor.state is PositionState.PENDING_ENTRY
    entry_request = broker.submitted[0]
    assert entry_request.stop_loss is not None  # bracket carries the stop
    assert entry_request.client_order_id.startswith(f"wave-{actor.position_key}-entry")

    actor.deliver_fill = None  # noqa - clarity only
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry_request.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        price=100.0,
    )
    # updates are delivered by the engine router normally; call directly here
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN
    assert actor.avg_entry_price == 100.0

    # stop leg fills (broker-generated id, routed by symbol)
    broker.push_update(
        "fill",
        order_id="o99",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        price=95.0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.CLOSED
    assert actor.exit_reason == "stop hit"


async def test_actor_close_now_and_halt():
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()

    actor.enter_halt()
    assert actor.state is PositionState.HALTED
    actor.exit_halt()
    assert actor.state is PositionState.OPEN

    from waveapp.broker.base import PositionInfo

    broker.positions = [PositionInfo("SPY", 1, 100.0, 100.0, 0.0)]  # broker truth
    await actor.close_now("test close")
    assert actor.state is PositionState.CLOSING
    exit_request = broker.submitted[-1]
    assert exit_request.side is OrderSide.SELL
    assert "exit" in exit_request.client_order_id


async def test_actor_cancel_unfilled_entry():
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await actor.close_now("abort")
    assert broker.canceled == ["o1"]


async def test_protective_check_detects_missing_stop():
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.state = PositionState.OPEN
    # broker says: position exists, but no stop order anywhere
    from waveapp.broker.base import PositionInfo

    broker.positions = [PositionInfo("SPY", 1, 100.0, 100.0, 0.0)]
    assert await actor.protective_check() is False
    # add a resting stop → protected
    broker.open_orders = [
        OrderInfo("s1", "", "SPY", OrderSide.SELL, 1, 0, OrderType.STOP, OrderStatus.NEW)
    ]
    assert await actor.protective_check() is True
    # flat position → protected by definition
    broker.open_orders = []
    broker.positions = []
    assert await actor.protective_check() is True


# -- engine core -------------------------------------------------------------


async def test_engine_full_cycle():
    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    assert "running" in await engine.start()
    assert engine.state is EngineState.RUNNING

    actor = await engine.open_position(_spec())
    assert isinstance(actor, PositionActor)
    await _drain()

    # router delivers the entry fill by client id
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
    )
    await _drain()
    assert actor.state is PositionState.OPEN

    # pause blocks new entries but keeps the open actor
    await engine.pause()
    rejected = await engine.open_position(_spec(symbol="AAPL"))
    assert isinstance(rejected, str)
    await engine.resume()

    # stop → waits for flat → idle
    await engine.stop()
    assert engine.state is EngineState.STOPPING
    broker.push_update(
        "fill",
        order_id="o77",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
    )
    await _drain()
    assert actor.state is PositionState.CLOSED
    await asyncio.sleep(1.1)
    assert engine.state is EngineState.IDLE
    await engine.shutdown()


async def test_engine_kill_flattens_broker_truth():
    """Kill cancels ALL orders then market-flattens ALL broker positions —
    including ones no live actor owns (e.g. after an app restart)."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    engine.CANCEL_SETTLE_SECONDS = 0
    await engine.start()
    actor = await engine.open_position(_spec())
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
    )
    await _drain()

    # broker truth: our SPY position + its resting stop leg (broker client id),
    # PLUS an orphaned TSLA short from a previous session with no actor
    broker.positions = [
        PositionInfo("SPY", 1, 100.0, 100.0, 0.0),
        PositionInfo("TSLA", -2, 250.0, -500.0, 0.0),
    ]
    broker.open_orders = [
        OrderInfo(
            "s1", "broker-uuid-1", "SPY", OrderSide.SELL, 1, 0, OrderType.STOP, OrderStatus.NEW
        )
    ]

    result = await engine.kill()
    assert "halted" in result
    assert engine.state is EngineState.KILLED
    assert engine.risk.halt_state is HaltState.KILLED
    assert broker.canceled == ["s1"]  # ALL orders canceled, wave-tagged or not
    kill_orders = [r for r in broker.submitted if "-kill-" in r.client_order_id]
    assert {(r.symbol, r.side, r.qty) for r in kill_orders} == {
        ("SPY", OrderSide.SELL, 1),
        ("TSLA", OrderSide.BUY, 2),  # short is bought back
    }
    # inside RTH (conftest pins is_rth True): plain MARKET flattens, no
    # extended-hours flag — the pre-A1-1 behavior, unchanged
    for request in kill_orders:
        assert request.order_type is OrderType.MARKET
        assert request.extended_hours is False
    # the SPY exit fill reaches the actor by symbol routing → CLOSED
    spy_kill = next(r for r in kill_orders if r.symbol == "SPY")
    broker.push_update(
        "fill",
        order_id="k1",
        client_order_id=spy_kill.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
    )
    await _drain()
    assert actor.state is PositionState.CLOSED
    # engine refuses new entries after kill
    rejection = await engine.open_position(_spec(symbol="AAPL"))
    assert isinstance(rejection, str)
    await engine.shutdown()


async def test_close_now_cancels_stop_leg_first():
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.CANCEL_SETTLE_SECONDS = 0
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()

    # a resting stop exists at the broker (broker-generated client id)
    broker.open_orders = [
        OrderInfo("s7", "broker-leg", "SPY", OrderSide.SELL, 1, 0, OrderType.STOP, OrderStatus.NEW)
    ]
    await actor.close_now("test")
    assert broker.canceled == ["s7"]  # stop canceled BEFORE the exit
    assert broker.submitted[-1].order_type is OrderType.MARKET


# -- audit A1-1 / A1-12: market orders outside RTH ---------------------------


def _pin_outside_rth(monkeypatch):
    from waveapp.engine.session import SessionScheduler

    monkeypatch.setattr(SessionScheduler, "is_rth", staticmethod(lambda now_utc=None: False))


async def _open_filled_spy(engine, broker):
    actor = await engine.open_position(_spec())
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
    )
    await _drain()
    return actor


async def test_kill_outside_rth_marketable_limits_and_per_symbol_stop_cancel(monkeypatch):
    """Audit A1-1: a 07:30 kill used to cancel EVERY protective stop, then
    queue MARKET/DAY flattens that could not execute until 09:30 — hours of
    naked positions created by the safety mechanism itself. Outside RTH the
    flattens must be marketable LIMITs with extended_hours, and a position's
    stop must die only immediately before its OWN flatten submit."""
    from waveapp.broker.base import PositionInfo

    _pin_outside_rth(monkeypatch)
    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    engine.CANCEL_SETTLE_SECONDS = 0
    await engine.start()
    await _open_filled_spy(engine, broker)

    broker.positions = [
        PositionInfo("SPY", 1, 100.0, 100.0, 0.0, current_price=100.0),
        PositionInfo("TSLA", -2, 250.0, -500.0, 0.0, current_price=250.0),
    ]
    broker.open_orders = [
        OrderInfo(
            "s1", "broker-uuid-1", "SPY", OrderSide.SELL, 1, 0, OrderType.STOP, OrderStatus.NEW
        ),
        # a stray order on a NON-held symbol: phase-1 cancel, before any stop
        OrderInfo(
            "x9",
            "wave-ffffffffffff-entry-1",
            "MSFT",
            OrderSide.BUY,
            1,
            0,
            OrderType.LIMIT,
            OrderStatus.NEW,
        ),
    ]
    broker.call_log.clear()
    await engine.kill()

    kill_orders = [r for r in broker.submitted if "-kill-" in r.client_order_id]
    assert {r.symbol for r in kill_orders} == {"SPY", "TSLA"}
    for request in kill_orders:
        assert request.order_type is OrderType.LIMIT  # never MARKET outside RTH
        assert request.extended_hours is True
        assert request.time_in_force is TimeInForce.DAY
        assert request.limit_price is not None
    spy = next(r for r in kill_orders if r.symbol == "SPY")
    tsla = next(r for r in kill_orders if r.symbol == "TSLA")
    # marketable: through the far side by the 0.5% buffer (cent-rounded)
    assert spy.side is OrderSide.SELL
    assert spy.limit_price == pytest.approx(100.0 * 0.995, abs=0.02)
    assert spy.limit_price < 100.0
    assert tsla.side is OrderSide.BUY
    assert tsla.limit_price == pytest.approx(250.0 * 1.005, abs=0.02)
    assert tsla.limit_price > 250.0

    # order-of-operations on the broker's own call log: the stray MSFT order
    # dies in phase 1, but SPY's protective stop is canceled only IMMEDIATELY
    # before SPY's flatten submit (naked window = one settle, per symbol)
    log = broker.call_log
    stray_idx = log.index(("cancel", "x9"))
    stop_idx = log.index(("cancel", "s1"))
    spy_submit_idx = next(
        i for i, e in enumerate(log) if e[0] == "submit" and e[1] == "SPY" and "-kill-" in e[3]
    )
    assert stray_idx < stop_idx
    assert spy_submit_idx == stop_idx + 1
    await engine.shutdown()


async def test_kill_leaves_halted_symbol_protected():
    """Audit A1-1: never an order into a halt/resume (§3). A HALTED symbol is
    skipped entirely — its stop keeps resting and the halt-resume machinery
    keeps owning it (the actor's kill flag stays off)."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    engine.CANCEL_SETTLE_SECONDS = 0
    await engine.start()
    actor = await _open_filled_spy(engine, broker)
    engine.on_symbol_halt("SPY", True)
    assert actor.state is PositionState.HALTED

    broker.positions = [
        PositionInfo("SPY", 1, 100.0, 100.0, 0.0, current_price=100.0),
        PositionInfo("TSLA", -2, 250.0, -500.0, 0.0, current_price=250.0),
    ]
    broker.open_orders = [
        OrderInfo("s1", "b1", "SPY", OrderSide.SELL, 1, 0, OrderType.STOP, OrderStatus.NEW),
        OrderInfo("s2", "b2", "TSLA", OrderSide.BUY, 2, 0, OrderType.STOP, OrderStatus.NEW),
    ]
    result = await engine.kill()

    kill_orders = [r for r in broker.submitted if "-kill-" in r.client_order_id]
    assert {r.symbol for r in kill_orders} == {"TSLA"}  # nothing into SPY's halt
    assert "s1" not in broker.canceled  # SPY's stop keeps resting (rule 3)
    assert "s2" in broker.canceled  # TSLA proceeds normally
    assert actor.state is PositionState.HALTED
    assert actor._kill_flatten_active is False  # halt-resume handling owns SPY
    assert "halted symbol(s) left protected" in result
    await engine.shutdown()


async def test_kill_flatten_failure_replaces_stop():
    """Audit A1-1 failure path: the stop was canceled for the flatten; when
    the flatten submit is refused the position must be re-protected
    immediately — never stranded naked (hard rule 3)."""
    from waveapp.broker.base import PositionInfo

    class RefusingBroker(FakeBroker):
        async def submit_order(self, mode, request):
            if "-kill-" in request.client_order_id:
                raise RuntimeError("refused")
            return await super().submit_order(mode, request)

    broker = RefusingBroker()
    engine = EngineCore(broker, database=None)
    engine.CANCEL_SETTLE_SECONDS = 0
    await engine.start()
    actor = await _open_filled_spy(engine, broker)
    broker.positions = [PositionInfo("SPY", 1, 100.0, 100.0, 0.0, current_price=100.0)]
    broker.open_orders = [
        OrderInfo("s1", "b1", "SPY", OrderSide.SELL, 1, 0, OrderType.STOP, OrderStatus.NEW)
    ]
    await engine.kill()

    assert "s1" in broker.canceled  # the stop WAS canceled for the flatten
    stops = [
        r
        for r in broker.submitted
        if r.order_type is OrderType.STOP and "-stop-" in r.client_order_id
    ]
    assert stops, "a replacement protective stop must be submitted"
    assert stops[-1].symbol == "SPY" and stops[-1].qty == 1
    # the actor owns its protection again — watchdog and checks re-armed
    assert actor._kill_flatten_active is False
    await engine.shutdown()


async def test_close_now_outside_rth_submits_marketable_limit(monkeypatch):
    """Audit A1-12: close_now always sent MARKET/DAY without extended_hours —
    every PRE/POST exit (session flatten, judge close, momentum exit) queued
    until 09:30. Outside RTH it must go as a marketable LIMIT with
    extended_hours, priced off the broker position's current price."""
    from waveapp.broker.base import PositionInfo

    _pin_outside_rth(monkeypatch)
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.CANCEL_SETTLE_SECONDS = 0
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    broker.positions = [PositionInfo("SPY", 1, 100.0, 100.0, 0.0, current_price=104.0)]

    await actor.close_now("post-market judge close")
    exit_request = broker.submitted[-1]
    assert exit_request.order_type is OrderType.LIMIT
    assert exit_request.extended_hours is True
    assert exit_request.time_in_force is TimeInForce.DAY
    assert exit_request.limit_price == pytest.approx(104.0 * 0.995, abs=0.02)
    assert exit_request.limit_price < 104.0
    assert actor.state is PositionState.CLOSING
    assert actor.exit_order_id is not None  # A1-9 machinery still wired


def test_marketable_exit_fields_price_sources(monkeypatch):
    """The shared helper: quote far-touch first, caller fallback second;
    plain MARKET inside RTH (the historical behavior, untouched)."""
    from types import SimpleNamespace

    from waveapp.engine.actor import marketable_exit_fields
    from waveapp.engine.session import SessionScheduler

    _pin_outside_rth(monkeypatch)
    monkeypatch.setattr(
        PositionActor,
        "quote_source",
        staticmethod(lambda s: SimpleNamespace(bid_price=99.0, ask_price=101.0)),
    )
    sell = marketable_exit_fields(OrderSide.SELL, "SPY", 105.0)
    assert sell["order_type"] is OrderType.LIMIT and sell["extended_hours"] is True
    assert sell["limit_price"] == pytest.approx(99.0 * 0.995, abs=0.02)  # bid, not fallback
    buy = marketable_exit_fields(OrderSide.BUY, "SPY", 105.0)
    assert buy["limit_price"] == pytest.approx(101.0 * 1.005, abs=0.02)  # ask, not fallback

    monkeypatch.setattr(PositionActor, "quote_source", None)
    fallback = marketable_exit_fields(OrderSide.SELL, "SPY", 50.0)
    assert fallback["limit_price"] == pytest.approx(50.0 * 0.995, abs=0.02)

    monkeypatch.setattr(SessionScheduler, "is_rth", staticmethod(lambda now_utc=None: True))
    rth = marketable_exit_fields(OrderSide.SELL, "SPY", 50.0)
    assert rth["order_type"] is OrderType.MARKET
    assert rth["extended_hours"] is False and rth["limit_price"] is None


async def test_engine_reconcile_freezes_on_unknown_wave_order():
    """A Wave-tagged order that is NOT a stale entry (2026-08-20: those get
    canceled now) and serves no position still freezes entries."""
    broker = FakeBroker()
    broker.open_orders = [
        OrderInfo(
            "z9",
            "wave-deadbeef0000-exit-1",  # an EXIT with no actor and no position
            "TSLA",
            OrderSide.SELL,
            1,
            0,
            OrderType.LIMIT,
            OrderStatus.NEW,
        )
    ]
    engine = EngineCore(broker, database=None)
    await engine.start()
    decision = engine.risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    assert not decision and "reconcile" in decision.reason
    assert broker.canceled == []  # exits are never blind-canceled
    # once the broker is clean, the next reconcile lifts the freeze
    broker.open_orders = []
    await engine.reconcile()
    assert engine.risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    await engine.shutdown()


async def test_engine_persists_positions_and_fills(tmp_path):
    from waveapp.persistence.db import Database

    broker = FakeBroker()
    database = Database(tmp_path / "t.db")
    engine = EngineCore(broker, database=database)
    await engine.start()
    actor = await engine.open_position(_spec())
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        price=101.5,
    )
    await _drain()

    rows = database.query("SELECT * FROM positions")
    assert len(rows) == 1 and rows[0]["state"] == "open"
    fills = database.query("SELECT * FROM fills")
    assert len(fills) == 1 and fills[0]["price"] == 101.5
    orders = database.query("SELECT * FROM orders")
    assert len(orders) == 1 and orders[0]["position_uuid"] == actor.position_key
    await engine.shutdown()
    database.close()


# -- restart adoption (2026-08-19: KORU/MRVL sat frozen after a restart) ------


async def test_reconcile_adopts_restart_survivor_with_resting_stop():
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    broker.positions = [PositionInfo("KORU", 656, 19.05, 12497.0, 0.0, current_price=19.10)]
    broker.open_orders = [
        OrderInfo(
            "s42",
            "broker-leg",
            "KORU",
            OrderSide.SELL,
            656,
            0,
            OrderType.STOP,
            OrderStatus.NEW,
            stop_price=18.8,
        )
    ]
    engine = EngineCore(broker, database=None)
    await engine.start()
    # adopted, not frozen
    assert engine.risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    adopted = next(a for a in engine.actors.values() if a.spec.symbol == "KORU")
    assert adopted.state is PositionState.OPEN
    assert adopted.filled_qty == 656 and adopted.avg_entry_price == 19.05
    assert adopted.spec.stop_price == 18.8
    assert adopted.stop_leg_order_id == "s42"
    assert adopted.spec.side is OrderSide.BUY
    # no new entry order was sent for the adopted position
    assert broker.submitted == []
    await engine.shutdown()


async def test_reconcile_protects_naked_survivor_before_adopting():
    """Hard rule 3: a restart survivor with NO resting stop gets a protective
    stop submitted before the actor attaches."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    broker.positions = [PositionInfo("MRVL", 53, 232.83, 12340.0, 0.0, current_price=233.00)]
    engine = EngineCore(broker, database=None)
    await engine.start()
    assert engine.risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    adopted = next(a for a in engine.actors.values() if a.spec.symbol == "MRVL")
    assert adopted.state is PositionState.OPEN
    protective = broker.submitted[-1]
    assert protective.order_type is OrderType.STOP and protective.side is OrderSide.SELL
    assert protective.qty == 53
    assert protective.stop_price == round(233.00 * 0.97, 2)
    assert adopted.spec.stop_price == protective.stop_price
    await engine.shutdown()


async def test_open_position_rejects_symbol_already_held():
    """2026-08-19: GDX was bought twice after a restart — the engine now
    refuses a second entry on any symbol with a live actor."""
    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    await engine.start()
    first = await engine.open_position(_spec(symbol="GDX"))
    assert not isinstance(first, str)
    second = await engine.open_position(_spec(symbol="GDX"))
    assert second == "already holding GDX"
    assert sum(1 for a in engine.actors.values() if a.spec.symbol == "GDX") == 1
    await engine.shutdown()


async def test_cancelled_actor_skips_protective_check_during_app_shutdown():
    """2026-08-19: quitting with open positions ran protective_check against
    a half-torn-down connection → 3 false ERROR alerts. During app shutdown
    the check is skipped (the broker-side stop is untouched by quitting)."""
    broker = FakeBroker()
    checks = []

    async def _fake_check():
        checks.append(1)
        return True

    # normal cancel (crash-style): check RUNS
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.protective_check = _fake_check
    task = actor.start()
    await _drain()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert checks == [1]

    # app-shutdown cancel: check SKIPPED
    PositionActor.app_shutting_down = True
    try:
        actor2 = PositionActor(_spec(symbol="MRVL"), broker, TradingMode.PAPER)
        actor2.protective_check = _fake_check
        task2 = actor2.start()
        await _drain()
        task2.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task2
        assert checks == [1]  # unchanged
    finally:
        PositionActor.app_shutting_down = False


# -- daily-close flatten + adoption clock (2026-08-19 post-session fixes) -----


def test_minutes_to_rth_close_only_inside_rth():
    from datetime import UTC, datetime

    from waveapp.engine.session import SessionScheduler

    # Tue 2026-08-18 15:50 ET = 19:50 UTC → 10 minutes to the bell
    inside = datetime(2026, 8, 18, 19, 50, tzinfo=UTC)
    assert SessionScheduler.minutes_to_rth_close(inside) == pytest.approx(10.0)
    # 16:05 ET (POST) and 09:00 ET (PRE) → None
    assert SessionScheduler.minutes_to_rth_close(datetime(2026, 8, 18, 20, 5, tzinfo=UTC)) is None
    assert SessionScheduler.minutes_to_rth_close(datetime(2026, 8, 18, 13, 0, tzinfo=UTC)) is None
    # Saturday → None
    assert SessionScheduler.minutes_to_rth_close(datetime(2026, 8, 15, 19, 50, tzinfo=UTC)) is None


def test_exit_engine_flattens_at_daily_close_boundary():
    """The daily 16:00 close acts exactly like the weekend boundary: within
    flatten_before_close_minutes the binding decision is EXIT_NOW."""
    from datetime import UTC, datetime

    from waveapp.broker.base import OrderSide
    from waveapp.data.hub import Bar
    from waveapp.engine.exits import DEFAULT_PARAMS, ExitEngine
    from waveapp.engine.session import Regime

    brain = ExitEngine(
        side=OrderSide.BUY,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=DEFAULT_PARAMS[Regime.POWER_HOUR],
        entry_time=datetime(2026, 8, 18, 19, 0, tzinfo=UTC),
    )
    bar = Bar(
        symbol="HL",
        start=datetime(2026, 8, 18, 19, 52, tzinfo=UTC),
        open=100.2,
        high=100.3,
        low=100.1,
        close=100.2,
        volume=1000.0,
        vwap_notional=100_200.0,
    )
    decision = brain.on_bar(bar, minutes_to_close_boundary=8.0)
    assert decision.action.value == "exit_now"
    assert "boundary" in decision.reason


async def test_adopted_actor_keeps_original_entry_clock(tmp_path):
    """GDX quirk: adoption must recover opened_at from the DB so t_max
    doesn't restart at adoption."""
    from waveapp.broker.base import PositionInfo
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "a.db")
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
        " strategy, opened_at, trading_mode)"
        " VALUES ('orig1','GDX','long',225,96.86,'open','VWAP',"
        " '2026-08-19T16:32:39+00:00','paper')"
    )
    broker = FakeBroker()
    broker.positions = [PositionInfo("GDX", 225, 96.86, 21793.0, 0.0, current_price=96.9)]
    broker.open_orders = [
        OrderInfo(
            "s9",
            "leg",
            "GDX",
            OrderSide.SELL,
            225,
            0,
            OrderType.STOP,
            OrderStatus.NEW,
            stop_price=92.35,
        )
    ]
    engine = EngineCore(broker, database=database)
    await engine.start()
    adopted = next(a for a in engine.actors.values() if a.spec.symbol == "GDX")
    assert adopted.adopted_entry_time is not None
    assert adopted.adopted_entry_time.isoformat().startswith("2026-08-19T16:32:39")
    assert adopted.spec.strategy == "VWAP"
    await engine.shutdown()
    database.close()


async def test_reconcile_cancels_stale_inflight_entry_order():
    """2026-08-20 MRNA incident: an entry order in flight during a restart
    froze entries, then filled unmanaged. Stale Wave ENTRY orders are now
    CANCELED during reconcile; nothing freezes."""
    broker = FakeBroker()
    broker.open_orders = [
        OrderInfo(
            "e77",
            "wave-2d64c7a5d307-entry-1",
            "MRNA",
            OrderSide.BUY,
            83,
            0,
            OrderType.MARKET,
            OrderStatus.NEW,
        )
    ]
    engine = EngineCore(broker, database=None)
    engine.CANCEL_SETTLE_SECONDS = 0
    await engine.start()
    assert "e77" in broker.canceled  # stale entry canceled, not frozen over
    assert engine.risk.can_enter("SPY", OrderSide.BUY, 0, TODAY)
    await engine.shutdown()


async def test_close_log_says_win_or_loss():
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.state = PositionState.OPEN
    actor.entry_order_id = "E1"
    actor.avg_entry_price = 100.0
    actor.filled_qty = 10.0
    from types import SimpleNamespace as NS

    events = []
    actor._on_event = lambda a, text: events.append(text)
    exit_order = NS(
        order_id="X1",
        client_order_id="leg",
        filled_qty=10.0,
        filled_avg_price=103.0,
        order_type=OrderType.STOP,
    )
    await actor._handle(NS(event="fill", order=exit_order))
    assert actor.state is PositionState.CLOSED
    assert any("WIN +30.00 $" in e for e in events)


async def test_reconcile_books_trades_closed_while_offline(tmp_path):
    """2026-08-20: a stop filled while the app was closed — the trade
    never reached Performance/log. Reconcile now recovers the exit from
    broker order history; duplicate stale rows become 'superseded'."""
    from datetime import UTC, datetime

    from waveapp.persistence.db import Database

    database = Database(tmp_path / "r.db")
    for uuid, opened in (
        ("newr1", "2026-08-20T15:00:00+00:00"),
        ("oldr1", "2026-08-20T14:00:00+00:00"),
    ):
        database.execute(
            "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
            " strategy, opened_at, trading_mode) VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid, "HL", "long", 100, 20.00, "open", "VWAP", opened, "paper"),
        )
    broker = FakeBroker()  # broker holds NOTHING — the stop already filled

    async def fake_history(mode, symbol, after):
        assert symbol == "HL"
        return [
            OrderInfo(
                "x1",
                "leg",
                "HL",
                OrderSide.SELL,
                100,
                100,
                OrderType.STOP,
                OrderStatus.FILLED,
                filled_avg_price=20.42,
                filled_at=datetime(2026, 8, 20, 15, 40, tzinfo=UTC),
            )
        ]

    broker.get_closed_orders = fake_history
    engine = EngineCore(broker, database=database)
    await engine.start()
    newest = database.query("SELECT * FROM positions WHERE position_uuid='newr1'")[0]
    assert newest["state"] == "closed"
    assert newest["realized_pnl"] == pytest.approx(42.0)  # (20.42-20.00)×100
    assert newest["exit_path"] == "broker close (Wave offline)"
    assert newest["closed_at"].startswith("2026-08-20T15:40")
    older = database.query("SELECT state FROM positions WHERE position_uuid='oldr1'")[0]
    assert older["state"] == "superseded"  # duplicate — never a fabricated trade
    await engine.shutdown()
    database.close()


async def test_offline_recovery_never_double_counts(tmp_path):
    """Day-2 bug: WMT/ETHA got BOTH a real close and a fabricated 'offline
    close' for the same position. If another same-symbol row already closed
    after this one opened, the stale row is superseded — never recovered."""
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "d.db")
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
        " strategy, opened_at, trading_mode) VALUES"
        " ('stale1','WMT','short',120,104.24,'open','GAP','2026-08-20T13:48:00+00:00','paper')"
    )
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
        " strategy, opened_at, trading_mode, closed_at, realized_pnl) VALUES"
        " ('real1','WMT','short',120,104.24,'closed','GAP','2026-08-20T13:52:00+00:00',"
        " 'paper','2026-08-20T13:57:00+00:00',2.25)"
    )
    # a never-filled entry row (NULL avg_entry) must supersede, not crash
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, state,"
        " strategy, opened_at, trading_mode) VALUES"
        " ('ghost1','MRNA','short',64,'pending_entry','GAP','2026-08-20T13:31:00+00:00','paper')"
    )
    broker = FakeBroker()

    async def fail_history(mode, symbol, after):  # must never be consulted
        raise AssertionError("recovery consulted history despite a booked close")

    broker.get_closed_orders = fail_history
    engine = EngineCore(broker, database=database)
    await engine.start()
    assert (
        database.query("SELECT state FROM positions WHERE position_uuid='stale1'")[0]["state"]
        == "superseded"
    )
    assert (
        database.query("SELECT state FROM positions WHERE position_uuid='ghost1'")[0]["state"]
        == "superseded"
    )
    assert database.query("SELECT realized_pnl FROM positions WHERE position_uuid='real1'")[0][
        "realized_pnl"
    ] == pytest.approx(2.25)  # the one true trade, untouched
    await engine.shutdown()
    database.close()


async def test_concurrent_starts_adopt_each_position_once():
    """2026-08-21: two simultaneous start() calls double-adopted every
    position (duplicate actors → double-exit risk). Serialized now."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    broker.positions = [PositionInfo("HOOD", 182, 104.21, 18966.0, 0.0, current_price=106.9)]
    broker.open_orders = [
        OrderInfo(
            "s1",
            "leg",
            "HOOD",
            OrderSide.SELL,
            182,
            0,
            OrderType.STOP,
            OrderStatus.NEW,
            stop_price=98.77,
        )
    ]
    engine = EngineCore(broker, database=None)
    results = await asyncio.gather(engine.start(), engine.start())
    assert any("running" in r for r in results)
    hood_actors = [a for a in engine.actors.values() if a.spec.symbol == "HOOD"]
    assert len(hood_actors) == 1  # ONE actor, not twins
    await engine.shutdown()


def test_position_size_capped_by_market_impact_participation():
    """2026-08-21 (the scale question): per-trade size ≤ 0.5% of the
    symbol's ADV so Wave's own orders never move the price against it."""
    risk = RiskEngine()
    # thin name: ADV 200k shares → cap = 1,000 shares even though risk math
    # on a $10M account would want far more
    qty = risk.position_size(10_000_000, 20.0, 19.5, OrderSide.BUY, avg_daily_volume=200_000)
    assert qty == 1_000
    # liquid name: ADV 80M → the cap never binds at this equity
    qty = risk.position_size(100_000, 20.0, 19.5, OrderSide.BUY, avg_daily_volume=80_000_000)
    assert qty == 1_250  # notional cap (25% of 100k / $20) rules, not impact


async def test_decision_price_persists_and_measures_slippage(tmp_path):
    """Option A (2026-08-24, the first-Monday fill delay): every position
    remembers its DECISION price; fill − decision = realized entry slippage,
    stored for the §11.2 calibration and the weekly report."""

    from waveapp.broker.base import OrderSide, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec
    from waveapp.engine.core import EngineCore, telegram_trade_line
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    spec = PositionSpec(
        symbol="BITX", side=OrderSide.BUY, qty=100, stop_price=17.29, decision_price=18.07
    )
    engine = EngineCore(object(), database=database)
    actor = PositionActor(spec, object(), TradingMode.PAPER)
    actor.filled_qty = 100.0
    actor.avg_entry_price = 18.31  # filled 24¢ worse than decided
    engine._persist_position(actor)
    row = database.query("SELECT decision_price, avg_entry FROM positions")[0]
    assert row["decision_price"] == 18.07 and row["avg_entry"] == 18.31
    # Telegram tells the slippage truth on the fill message
    line = telegram_trade_line(
        actor, "entry filled: 100 @ 18.31 — server-side stop resting at 17.29"
    )
    assert "Slippage: paid +24¢/share" in line
    database.close()


def test_telegram_short_lines_read_naturally():
    """S4: shorts speak short — 'Shorted/covered', stop 'tightened' (it moves
    DOWN), slippage sign mirrored (a short entry SELLS: filling below the
    decision price is the paid slippage). Long wording is pinned unchanged by
    the tests around this one."""
    from types import SimpleNamespace

    from waveapp.broker.base import OrderSide
    from waveapp.engine.core import telegram_trade_line

    actor = SimpleNamespace(
        spec=SimpleNamespace(symbol="PLTR", side=OrderSide.SELL, decision_price=30.20),
        avg_entry_price=30.10,
    )
    line = telegram_trade_line(
        actor, "entry filled: 15 @ 30.10 — server-side stop resting at 31.20"
    )
    assert line.startswith("🔻 Shorted 15 shares of PLTR at $30.10. Protected at $31.20.")
    assert "Slippage: paid +10¢/share" in line  # sold 10¢ BELOW the decision
    # ratchet DOWN through the entry = profit locked; above entry = tightened
    assert telegram_trade_line(actor, "server-side stop ratcheted to 29.90") == (
        "🔒 PLTR safety tightened to $29.90 — profit is now locked in."
    )
    assert telegram_trade_line(actor, "server-side stop ratcheted to 30.80") == (
        "🔽 PLTR safety tightened to $30.80."
    )
    assert telegram_trade_line(actor, "exit submitted (VWAP recross)") == (
        "💰 Covering PLTR — the push is over, taking the profit."
    )
    assert telegram_trade_line(actor, "closed (trail) @ 29.60 — WIN 7.50 $") == (
        "🟢 PLTR covered: WON 7.50 $ (covered at $29.60)."
    )
    assert telegram_trade_line(actor, "closed (stop hit) @ 31.20 — LOSS 16.50 $") == (
        "🔴 PLTR covered: LOST 16.50 $ (safety stop did its job)."
    )


def test_week_report_includes_the_slippage_bill(tmp_path):
    from datetime import date

    from waveapp.engine.reports import week_close_report
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, state, strategy,"
        " realized_pnl, closed_at, trading_mode, avg_entry, decision_price)"
        " VALUES ('a', 'BITX', 'long', 100, 'closed', 'GAP', 50.0,"
        " '2026-08-24T15:00:00+00:00', 'paper', 18.31, 18.07)",
    )
    content, _payload = week_close_report(database, date(2026, 8, 24))
    assert "Entry slippage: +24.00 $ total" in content
    assert "avg +24.0¢/share" in content
    database.close()


def test_adopted_positions_keep_their_original_open_time(tmp_path):
    """2026-08-24 ('the buy indicator is wrong — I bought around
    9:35'): every restart re-persisted the position stamped with ADOPTION
    time, and the next restart read that as the original — the entry time
    crept forward. Adopted rows now keep the true open time forever."""
    from datetime import UTC, datetime

    from waveapp.broker.base import OrderSide, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec
    from waveapp.engine.core import EngineCore
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    original = datetime(2026, 8, 24, 13, 35, 12, tzinfo=UTC)
    spec = PositionSpec(symbol="ETHU", side=OrderSide.BUY, qty=797, stop_price=26.01)
    actor = PositionActor.adopt(
        spec,
        object(),
        TradingMode.PAPER,
        filled_qty=797.0,
        avg_entry_price=27.21,
        stop_leg_order_id="S1",
        adopted_entry_time=original,
    )
    engine = EngineCore(object(), database=database)
    engine._persist_position(actor)
    row = database.query("SELECT opened_at FROM positions")[0]
    assert str(row["opened_at"]).startswith("2026-08-24T13:35:12")  # not "now"
    database.close()


# -- opening-auction entries (PMOM, 2026-08-25) -------------------------------


def _auction_spec(qty=10):
    return PositionSpec(
        symbol="MVLL",
        side=OrderSide.BUY,
        qty=qty,
        stop_price=30.0,
        entry_type=OrderType.LIMIT,
        limit_price=33.0,
        auction_open=True,
        strategy="PMOM",
    )


async def test_auction_entry_opg_no_bracket_then_stop_after_fill():
    """An auction entry goes up tif=OPG with NO bracket leg (Alpaca refuses
    them), and the server-side stop is submitted the instant the fill lands."""
    from waveapp.broker.base import TimeInForce

    broker = FakeBroker()
    actor = PositionActor(_auction_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    assert entry.time_in_force is TimeInForce.OPG
    assert entry.stop_loss is None
    assert actor.stop_leg_order_id is None

    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="MVLL",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=10,
        price=32.5,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN
    stop_request = broker.submitted[1]
    assert stop_request.order_type is OrderType.STOP
    assert stop_request.side is OrderSide.SELL
    assert stop_request.qty == 10
    assert stop_request.stop_price == 30.0
    assert "-stop-" in stop_request.client_order_id
    assert actor.stop_leg_order_id == "o2"


async def test_auction_entry_flattens_if_stop_cannot_be_placed():
    """Hard rule 3: if the post-auction protective stop fails twice, the
    position must not exist — the actor flattens itself immediately."""

    class NoStopBroker(FakeBroker):
        async def submit_order(self, mode, request):
            if request.order_type is OrderType.STOP:
                raise RuntimeError("stop rejected")
            return await super().submit_order(mode, request)

    broker = NoStopBroker()
    from waveapp.broker.base import PositionInfo

    broker.positions = [PositionInfo("MVLL", 10, 32.5, 325.0, 0.0)]  # broker truth
    actor = PositionActor(_auction_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="MVLL",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=10,
        price=32.5,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    exits = [
        r for r in broker.submitted if r.order_type is OrderType.MARKET and r.side is OrderSide.SELL
    ]
    assert exits, "no flatten order after the stop failed"
    assert actor.state is PositionState.CLOSING
    assert actor.exit_reason == "protective stop could not be placed"


async def test_day_entry_still_brackets():
    """Regression: normal (non-auction) entries keep the bracket stop leg."""
    from waveapp.broker.base import TimeInForce

    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    assert entry.time_in_force is TimeInForce.DAY
    assert entry.stop_loss is not None


async def test_partial_fill_then_cancel_opens_and_protects():
    """CAMT 2026-08-27: the paper venue filled 5 of 90 auction shares then
    canceled the rest — the old actor closed and orphaned the shares with no
    stop. Now: partial+cancel → OPEN on the filled shares, stop placed."""
    broker = FakeBroker()
    actor = PositionActor(_auction_spec(qty=90), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "partial_fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="MVLL",
        side=OrderSide.BUY,
        qty=90,
        filled_qty=5,
        price=150.78,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="MVLL",
        side=OrderSide.BUY,
        qty=90,
        filled_qty=5,
        price=150.78,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN
    assert actor.filled_qty == 5
    stops = [r for r in broker.submitted if r.order_type is OrderType.STOP]
    assert stops and stops[0].qty == 5  # protects exactly what filled


async def test_halt_before_fill_cancels_the_entry():
    """GRML 2026-09-22 (−$843): a LULD pause before the entry filled used to
    leave the buy order resting through the halt — it filled the resume dump
    of a parabolic runner. A pre-fill halt now cancels the entry."""
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    assert actor.state is PositionState.PENDING_ENTRY
    actor.enter_halt()
    await _drain()
    assert actor.state is not PositionState.HALTED  # not "holding" — canceling
    assert broker.canceled, "the entry order must be canceled on a pre-fill halt"


# -- re-protect correctness after scale-out / ratchet (audit A1-5) ------------


def _open_actor_with_exit_engine(broker, qty=10, fill_price=32.5):
    """Fill an auction entry to OPEN and attach a real ExitEngine."""
    from waveapp.engine.exits import DEFAULT_PARAMS, ExitEngine

    actor = PositionActor(_auction_spec(qty=qty), broker, TradingMode.PAPER)
    actor.start()
    return actor, lambda: ExitEngine(
        side=OrderSide.BUY,
        entry_price=fill_price,
        qty=qty,
        atr_at_entry=0.5,
        params=DEFAULT_PARAMS,
        entry_time=datetime(2026, 8, 25, 13, 31, tzinfo=UTC),
        initial_stop=actor.spec.stop_price,
    )


async def _fill_entry(broker, actor, qty=10, price=32.5):
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol=actor.spec.symbol,
        side=OrderSide.BUY,
        qty=qty,
        filled_qty=qty,
        price=price,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()


async def test_reprotect_after_scale_out_uses_remaining_qty():
    """Audit A1-5(a): after a 50% scale-out, filled_qty still holds the FULL
    entry fill — a re-protect for the full size would flip the position and
    the broker rejects it. The re-protect must submit the REMAINING qty."""
    broker = FakeBroker()
    actor, make_engine = _open_actor_with_exit_engine(broker, qty=10)
    await _drain()
    await _fill_entry(broker, actor, qty=10)
    assert actor.state is PositionState.OPEN
    engine = make_engine()
    actor.exit_engine = engine
    engine.remaining_qty = 5  # 50% scale-out banked; ledger truth
    actor.stop_leg_order_id = None  # stop leg died (cancel/expire) — re-protect

    await actor._place_protective_stop()
    stop = broker.submitted[-1]
    assert stop.order_type is OrderType.STOP
    assert stop.qty == 5, "re-protect must cover the REMAINING shares, not the original fill"
    assert actor.stop_leg_order_id is not None


async def test_reprotect_after_ratchet_uses_confirmed_level_not_entry_stop():
    """Audit A1-5(b): re-protecting a breakeven-locked winner at the ORIGINAL
    entry-time stop silently regresses it to full initial risk. The
    re-protect must use the tightest broker-confirmed level (direction-aware)."""
    broker = FakeBroker()
    actor, make_engine = _open_actor_with_exit_engine(broker, qty=10)
    await _drain()
    await _fill_entry(broker, actor, qty=10)
    actor.exit_engine = make_engine()
    # breakeven ratchet confirmed at the broker earlier (long: 32.6 > 30.0)
    actor._confirmed_stop = 32.6
    actor.stop_leg_order_id = None

    await actor._place_protective_stop()
    stop = broker.submitted[-1]
    assert stop.stop_price == 32.6, "re-protect regressed a ratcheted stop to the entry level"

    # direction-aware: a stale/looser confirmed level must never WIDEN the
    # stop below the spec level for a long
    actor._confirmed_stop = 28.0
    actor.stop_leg_order_id = None
    await actor._place_protective_stop()
    assert broker.submitted[-1].stop_price == 30.0


async def test_rule3_flatten_fires_through_fresh_close_backoff():
    """Audit A1-5(c): the exit-submit failure that precedes a re-protect sets
    _close_backoff_mono = now+5; when the re-protect then fails twice, the
    'PROTECTIVE STOP FAILED TWICE — flattening (hard rule 3)' close_now used
    to silently return on that fresh backoff. The rule-3 emergency flatten
    must bypass ONLY the backoff gate."""
    import time as _time

    from waveapp.broker.base import PositionInfo

    class NoStopBroker(FakeBroker):
        async def submit_order(self, mode, request):
            if request.order_type is OrderType.STOP:
                raise RuntimeError("stop rejected")
            return await super().submit_order(mode, request)

    broker = NoStopBroker()
    broker.positions = [PositionInfo("MVLL", 10, 32.5, 325.0, 0.0)]  # broker truth
    actor = PositionActor(_auction_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10)
    # OPEN despite the failed auction-fill stop attempt: force the exact
    # defect-(c) shape — backoff JUST set by a failed exit submit
    actor.state = PositionState.OPEN
    actor.stop_leg_order_id = None
    actor._close_backoff_mono = _time.monotonic() + 5.0

    def _exits():
        return [
            r
            for r in broker.submitted
            if r.order_type is OrderType.MARKET and r.side is OrderSide.SELL
        ]

    before = len(_exits())  # the fill-time rule-3 flatten (pre-backoff) doesn't count
    await actor._place_protective_stop()
    assert len(_exits()) > before, "rule-3 flatten was suppressed by the NVDG backoff"
    assert actor.state is PositionState.CLOSING
    assert actor.exit_reason == "protective stop could not be placed"


async def test_close_now_backoff_still_paces_ordinary_retries():
    """The NVDG pacing must survive the force gate: an ordinary (non-forced)
    close_now inside the backoff window submits nothing."""
    import time as _time

    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    assert actor.state is PositionState.OPEN
    before = len(broker.submitted)
    actor._close_backoff_mono = _time.monotonic() + 5.0
    await actor.close_now("judge EXIT_NOW retry")
    assert len(broker.submitted) == before  # paced, no order traffic
    assert actor.state is PositionState.OPEN


# -- dead stop/exit orders on a live position (audit A1-3 + A1-9) -------------


def _stops(broker):
    return [r for r in broker.submitted if r.order_type is OrderType.STOP]


async def test_stop_leg_canceled_on_open_position_reprotects():
    """Audit A1-3: a canceled/expired STOP leg on an OPEN position was
    silently dropped (the whole handler lived under PENDING_ENTRY) — the
    position sat naked while the trail amended a dead order id forever.
    The event must trigger an immediate re-protect."""
    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    assert actor.state is PositionState.OPEN
    actor.stop_leg_order_id = "stop1"  # the bracket's resting leg

    before = len(_stops(broker))
    broker.push_update(
        "canceled",  # manual dashboard cancel; "expired" = the 16:00 DAY death
        order_id="stop1",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN
    new_stops = _stops(broker)[before:]
    assert new_stops, "canceled stop leg on an OPEN position must be re-protected"
    assert new_stops[-1].qty == 10
    assert actor.stop_leg_order_id is not None
    assert actor.stop_leg_order_id != "stop1"


async def test_stop_expired_during_halt_reprotects_on_resume():
    """Audit A1-3 (HALTED flavor): the DAY stop dies at 16:00 while the
    symbol is LULD-halted. §12 forbids submitting into the halt — no order
    may go out until the halt lifts, then the re-protect fires."""
    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    actor.stop_leg_order_id = "stop1"
    actor.enter_halt()
    assert actor.state is PositionState.HALTED

    before = len(broker.submitted)
    broker.push_update(
        "expired",
        order_id="stop1",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert len(broker.submitted) == before, "no order traffic into a halt (§12)"
    assert actor.state is PositionState.HALTED
    assert actor.stop_leg_order_id is None

    actor.exit_halt()
    await _drain()
    assert actor.state is PositionState.OPEN
    stop = broker.submitted[-1]
    assert stop.order_type is OrderType.STOP, "resume must re-place the dead stop"
    assert stop.qty == 10
    assert actor.stop_leg_order_id is not None


async def test_exit_order_canceled_returns_closing_actor_to_open():
    """Audit A1-9: close_now discarded the exit order's id, and a canceled/
    expired EXIT order stranded the actor in CLOSING forever (close_now
    returns on CLOSING, apply_exit_decision requires OPEN). The actor must
    fall back to OPEN with a fresh protective stop so the close retries."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    broker.positions = [PositionInfo("SPY", 10, 100.0, 1000.0, 0.0)]  # broker truth

    await actor.close_now("test close")
    assert actor.state is PositionState.CLOSING
    assert actor.exit_order_id is not None  # the id is kept now
    exit_request = broker.submitted[-1]
    assert "exit" in exit_request.client_order_id

    broker.push_update(
        "canceled",
        order_id=actor.exit_order_id,
        client_order_id=exit_request.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN, "dead exit order must not strand CLOSING"
    assert actor.exit_order_id is None
    stop = broker.submitted[-1]
    assert stop.order_type is OrderType.STOP, "a fresh protective stop must be resting"
    assert stop.qty == 10
    assert actor.stop_leg_order_id is not None


# -- scale-out stop accounting (audit A1-4 + A1-6) -----------------------------


def _exit_bar(close, *, minute=0, high=None, low=None, volume=900.0):
    from waveapp.data.hub import Bar

    start = datetime(2026, 8, 25, 14, 30, tzinfo=UTC) + timedelta(minutes=minute)
    return Bar(
        symbol="MVLL",
        start=start,
        open=close,
        high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


def _real_exit_engine(qty=10, entry=32.5, stop=30.0):
    from waveapp.engine.exits import ExitEngine, ExitParams

    return ExitEngine(
        side=OrderSide.BUY,
        entry_price=entry,
        qty=qty,
        atr_at_entry=0.5,
        params=ExitParams(),
        entry_time=datetime(2026, 8, 25, 14, 29, tzinfo=UTC),
        entry_bar_volume=1000.0,
        initial_stop=stop,
    )


async def _open_scaleout_actor(broker, qty=10):
    """Auction entry filled at 32.5 with the protective stop resting; a real
    ExitEngine (ATR 0.5, k_t1 default 1.2 -> first target +0.60) attached."""
    actor = PositionActor(_auction_spec(qty=qty), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=qty)
    assert actor.state is PositionState.OPEN
    assert actor.stop_leg_order_id is not None  # auction fill placed the stop
    actor.exit_engine = _real_exit_engine(qty=qty)
    return actor


async def test_scale_out_success_updates_confirmed_stop_anchor():
    """Audit A1-4: the scale-out's replace-success must anchor _confirmed_stop
    at the new breakeven level. Otherwise a later transient amend failure
    rolls current_stop back to the STALE entry-time level, and the next
    chandelier proposal physically LOWERS the broker stop below breakeven."""
    from waveapp.engine.exits import ExitAction

    class FlakyReplaceBroker(FakeBroker):
        fail_replace = False

        async def replace_order(self, mode, order_id, qty=None, limit_price=None, stop_price=None):
            if self.fail_replace:
                raise RuntimeError("transient 500")
            return await super().replace_order(
                mode, order_id, qty=qty, limit_price=limit_price, stop_price=stop_price
            )

    broker = FlakyReplaceBroker()
    actor = await _open_scaleout_actor(broker)
    engine = actor.exit_engine

    decision = engine.on_bar(_exit_bar(33.15, high=33.2))
    assert decision.action is ExitAction.SCALE_OUT
    await actor.apply_exit_decision(decision)
    assert engine.scaled_out is True
    be_level = decision.new_stop
    assert be_level >= 32.51
    assert actor._confirmed_stop == be_level, "replace success must move the rollback anchor"

    # a later ratchet amend fails transiently: the rollback must land on the
    # scale-out breakeven level, NEVER back on the entry-time 30.0
    engine.current_stop = 33.0  # the engine's optimistic pre-commit
    broker.fail_replace = True
    await actor._amend_stop(33.0, "chandelier ratchet")
    assert engine.current_stop == be_level, (
        "rollback regressed below the broker-confirmed breakeven stop"
    )
    assert actor._confirmed_stop == be_level


async def test_scale_sale_submit_failure_rolls_back_and_reprotects_full_qty():
    """Audit A1-6: a failed scale-sale submit must leave remaining_qty
    truthful, re-arm the one-shot latch (the layer retries), and re-protect
    the FULL remaining position (the stop was already resized down)."""
    from waveapp.engine.exits import ExitAction

    class NoSaleBroker(FakeBroker):
        async def submit_order(self, mode, request):
            if request.order_type is OrderType.MARKET and request.side is OrderSide.SELL:
                raise RuntimeError("sale rejected")
            return await super().submit_order(mode, request)

    broker = NoSaleBroker()
    actor = await _open_scaleout_actor(broker)
    engine = actor.exit_engine

    decision = engine.on_bar(_exit_bar(33.15, high=33.2))
    assert decision.action is ExitAction.SCALE_OUT
    await actor.apply_exit_decision(decision)

    assert engine.remaining_qty == 10, "ledger must stay truthful on a failed submit"
    assert engine.scaled_out is False, "latch must re-arm so the layer retries"
    assert engine._pending_scale is None
    assert actor.state is PositionState.OPEN
    stop = _stops(broker)[-1]
    assert stop.qty == 10, "re-protect must cover the FULL remaining position"
    assert stop.stop_price == decision.new_stop  # tightest confirmed level (BE)
    assert actor.stop_leg_order_id is not None

    # the layer genuinely retries on a later bar
    retry = engine.on_bar(_exit_bar(33.16, minute=1, high=33.2))
    assert retry.action is ExitAction.SCALE_OUT


async def test_scale_out_happy_path_end_to_end():
    """Happy path unchanged: stop resized to the remainder at breakeven, the
    scale portion sold, ledger committed, latch set, anchors in sync."""
    from waveapp.engine.exits import ExitAction

    broker = FakeBroker()
    actor = await _open_scaleout_actor(broker)
    engine = actor.exit_engine

    decision = engine.on_bar(_exit_bar(33.15, high=33.2))
    assert decision.action is ExitAction.SCALE_OUT
    assert decision.qty == 5
    await actor.apply_exit_decision(decision)

    replaced_id, replaced_qty, replaced_stop = broker.replaced[-1]
    assert replaced_qty == 5, "resting stop must shrink to the POST-scale remainder"
    assert replaced_stop == round(decision.new_stop, 2)
    sale = broker.submitted[-1]
    assert sale.order_type is OrderType.MARKET and sale.side is OrderSide.SELL
    assert sale.qty == 5
    assert actor.state is PositionState.SCALING_OUT
    assert engine.remaining_qty == 5
    assert engine.scaled_out is True and engine.breakeven_done is True
    assert engine.pending_stop is None and engine._pending_scale is None
    assert engine.current_stop == decision.new_stop
    assert actor._confirmed_stop == decision.new_stop


# -- rejected events scoped to the order that died (audit A1-7) ----------------


async def test_rejected_entry_still_lands_error():
    """A rejected ENTRY opened nothing — terminal ERROR stays the honest
    verdict, and the actor task finishes cleanly (nothing held to sweep)."""
    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    task = actor.start()
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "rejected",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await asyncio.wait_for(task, timeout=1)
    assert actor.state is PositionState.ERROR
    assert actor.exit_reason == "order rejected"


async def test_rejected_exit_order_returns_to_open_with_fresh_stop():
    """Audit A1-7(a): an async-rejected EXIT order used to land terminal
    ERROR with the shares still held (and the stop already canceled by
    close_now) — a clean loop exit that skipped the protective check and
    made the symbol re-enterable. The actor must instead re-protect and
    return to OPEN so the close retries."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    broker.positions = [PositionInfo("SPY", 10, 100.0, 1000.0, 0.0)]  # broker truth

    await actor.close_now("test close")
    assert actor.state is PositionState.CLOSING
    exit_request = broker.submitted[-1]
    assert "exit" in exit_request.client_order_id

    broker.push_update(
        "rejected",
        order_id=actor.exit_order_id,
        client_order_id=exit_request.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN, "rejected exit must NOT be terminal ERROR"
    assert actor.exit_order_id is None
    stop = broker.submitted[-1]
    assert stop.order_type is OrderType.STOP, "a fresh protective stop must be resting"
    assert stop.qty == 10
    assert actor.stop_leg_order_id is not None


async def test_rejected_scale_sale_restores_ledger_and_reprotects_full():
    """Audit A1-7(b): the broker accepted the scale sale's submit then
    async-rejected it — confirm_scale_out had already committed, so the
    ledger under-counted and the resting stop was resized DOWN. The
    rejection must restore remaining_qty, re-protect the FULL remainder at
    the confirmed breakeven level, and return the actor to OPEN."""
    from waveapp.engine.exits import ExitAction

    broker = FakeBroker()
    actor = await _open_scaleout_actor(broker)
    actor.CANCEL_SETTLE_SECONDS = 0
    engine = actor.exit_engine

    decision = engine.on_bar(_exit_bar(33.15, high=33.2))
    assert decision.action is ExitAction.SCALE_OUT
    await actor.apply_exit_decision(decision)
    assert actor.state is PositionState.SCALING_OUT
    assert engine.remaining_qty == 5  # commit landed on the accepted submit
    assert actor.scale_order_id is not None
    sale_request = broker.submitted[-1]

    broker.push_update(
        "rejected",
        order_id=actor.scale_order_id,
        client_order_id=sale_request.client_order_id,
        symbol="MVLL",
        side=OrderSide.SELL,
        qty=5,
        filled_qty=0,
    )
    rejected_id = actor.scale_order_id
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN, "rejected scale must NOT be terminal ERROR"
    assert engine.remaining_qty == 10, "the unsold shares must return to the ledger"
    assert actor.scale_order_id is None
    stop = _stops(broker)[-1]
    assert stop.qty == 10, "re-protect must cover the FULL remaining position"
    assert stop.stop_price == round(decision.new_stop, 2)  # tightest confirmed (BE)
    assert actor.stop_leg_order_id is not None

    # a duplicate rejection echo must not inflate the ledger or double-protect
    stops_before = len(_stops(broker))
    broker.push_update(
        "rejected",
        order_id=rejected_id,
        client_order_id=sale_request.client_order_id,
        symbol="MVLL",
        side=OrderSide.SELL,
        qty=5,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert engine.remaining_qty == 10
    assert len(_stops(broker)) == stops_before


async def test_clean_terminal_exit_with_shares_runs_protective_check():
    """Audit A1-7(c): _run only ran protective_check on CancelledError or a
    crash — a CLEAN event-loop exit (terminal state landed by an event)
    with shares held skipped verification entirely. Now every path out with
    filled_qty > 0 and a non-CLOSED state sweeps, and protective_check on
    an ERROR actor with shares does real work (it used to no-op on any
    terminal state)."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    results = []
    orig = actor.protective_check

    async def _spy():
        results.append(await orig())
        return results[-1]

    actor.protective_check = _spy
    task = actor.start()
    await _drain()
    entry = broker.submitted[0]
    # partial fill lands 4 real shares, THEN the entry is rejected: terminal
    # ERROR via a clean loop exit, with a naked position at the broker
    broker.positions = [PositionInfo("SPY", 4, 100.0, 400.0, 0.0)]
    broker.push_update(
        "partial_fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=4,
        price=100.0,
    )
    actor.deliver(broker._queue.get_nowait())
    broker.push_update(
        "rejected",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        filled_qty=4,
    )
    actor.deliver(broker._queue.get_nowait())
    await asyncio.wait_for(task, timeout=1)
    assert actor.state is PositionState.ERROR
    assert results == [False], (
        "the clean-exit path must run the protective check, and it must "
        "detect the naked shares (no resting stop) instead of no-opping"
    )


# -- halt vs CLOSING (audit A1-8) ---------------------------------------------


async def test_halt_during_closing_keeps_exit_order_as_manager():
    """Audit A1-8: enter_halt used to overwrite CLOSING with HALTED — the
    in-flight exit order was orphaned (its cancel/reject handlers only fire
    on CLOSING) and the resume flipped to OPEN believing a dead stop id.
    Now a CLOSING actor stays CLOSING through the halt, so a dying exit
    order still lands in the A1-9 handler and re-protects."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    broker.positions = [PositionInfo("SPY", 10, 100.0, 1000.0, 0.0)]

    await actor.close_now("momentum exit")
    assert actor.state is PositionState.CLOSING
    exit_id = actor.exit_order_id
    exit_request = broker.submitted[-1]

    actor.enter_halt()
    assert actor.state is PositionState.CLOSING, "CLOSING must survive the halt"
    actor.exit_halt()  # a resume is a no-op for a non-HALTED actor
    assert actor.state is PositionState.CLOSING

    # the in-flight exit dies unfilled → the CLOSING handler (A1-9) catches
    # it: back to OPEN with a FRESH protective stop, never a dead stop id
    broker.push_update(
        "canceled",
        order_id=exit_id,
        client_order_id=exit_request.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN
    assert actor.exit_order_id is None
    fresh = _stops(broker)[-1]
    assert fresh.qty == 10
    assert actor.stop_leg_order_id is not None


async def test_halt_resume_reverifies_protection_without_death_event():
    """Audit A1-8 (resume half): the stop died during the halt but the
    stream was DEAF — no canceled/expired event ever arrived, so the A1-3
    _reprotect_after_halt flag never armed. The resume must verify against
    broker truth instead of trusting the pre-halt stop id, and re-protect."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    actor.stop_leg_order_id = "stop-dead"  # believed, but nothing rests
    broker.positions = [PositionInfo("SPY", 10, 100.0, 1000.0, 0.0)]
    broker.open_orders = []  # broker truth: NO resting stop survived

    actor.enter_halt()
    assert actor.state is PositionState.HALTED
    assert actor._reprotect_after_halt is False  # the death event never came

    before = len(_stops(broker))
    actor.exit_halt()
    await _drain()
    assert actor.state is PositionState.OPEN
    new_stops = _stops(broker)[before:]
    assert new_stops, "resume must re-protect when broker truth shows no stop"
    assert new_stops[-1].qty == 10
    assert actor.stop_leg_order_id not in (None, "stop-dead")


async def test_halt_resume_with_intact_stop_places_nothing():
    """A1-8 companion: when the resting stop DID survive the halt, the
    resume verification must be a read-only pass — no duplicate stop."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    actor = PositionActor(_spec(qty=10), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    actor.stop_leg_order_id = "s-alive"
    broker.positions = [PositionInfo("SPY", 10, 100.0, 1000.0, 0.0)]
    broker.open_orders = [
        OrderInfo("s-alive", "", "SPY", OrderSide.SELL, 10, 0, OrderType.STOP, OrderStatus.NEW)
    ]
    actor.enter_halt()
    submitted_before = len(broker.submitted)
    actor.exit_halt()
    await _drain()
    assert actor.state is PositionState.OPEN
    assert len(broker.submitted) == submitted_before, "intact stop → no new orders"
    assert broker.canceled == []


# -- adoption coverage net cancels ALL stop legs (audit A1-10) -----------------


async def test_adopt_cancels_every_stop_leg_on_merged_twin():
    """Audit A1-10: the coverage net canceled only the FIRST resting stop
    leg — the merged-twin restart shape (one 2×qty position, two half-size
    legs) left the second leg resting beside the full-size replacement, and
    that stop cascade can flip the position short. ALL legs must go."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    broker.positions = [PositionInfo("KORU", 20, 19.05, 381.0, 0.0, current_price=19.10)]
    broker.open_orders = [
        OrderInfo(
            "s1",
            "",
            "KORU",
            OrderSide.SELL,
            10,
            0,
            OrderType.STOP,
            OrderStatus.NEW,
            stop_price=18.8,
        ),
        OrderInfo(
            "s2",
            "",
            "KORU",
            OrderSide.SELL,
            10,
            0,
            OrderType.STOP,
            OrderStatus.NEW,
            stop_price=18.7,
        ),
    ]
    engine = EngineCore(broker, database=None)
    await engine.start()
    assert {"s1", "s2"} <= set(broker.canceled), "BOTH resting legs must be canceled"
    protective = [r for r in broker.submitted if r.order_type is OrderType.STOP]
    assert protective and protective[-1].qty == 20, "full-size replacement protection"
    adopted = next(a for a in engine.actors.values() if a.spec.symbol == "KORU")
    assert adopted.state is PositionState.OPEN
    assert adopted.filled_qty == 20
    await engine.shutdown()


async def test_adopt_stop_cancel_failure_is_loud_and_still_reprotects_full(caplog):
    """Audit A1-10 (suppressed-cancel half): a failed cancel of the
    undersized leg used to be swallowed by a blanket suppress WITH the
    belief kept — the half-size leg stayed trusted as 'protection'. Now the
    belief is dropped BEFORE the cancel, every cancel failure is LOGGED,
    and a full-size stop goes up regardless."""
    import logging

    from waveapp.broker.base import PositionInfo

    class RefusingCancel(FakeBroker):
        async def cancel_order(self, mode, order_id):
            self.canceled.append(order_id)
            raise RuntimeError("cancel refused")

    broker = RefusingCancel()
    broker.positions = [PositionInfo("KORU", 20, 19.05, 381.0, 0.0, current_price=19.10)]
    broker.open_orders = [
        OrderInfo(
            "s1",
            "",
            "KORU",
            OrderSide.SELL,
            10,
            0,
            OrderType.STOP,
            OrderStatus.NEW,
            stop_price=18.8,
        )
    ]
    engine = EngineCore(broker, database=None)
    with caplog.at_level(logging.WARNING, logger="wave.engine.core"):
        await engine.start()
    assert any("cancel of resting stop s1 FAILED" in r.message for r in caplog.records)
    protective = [r for r in broker.submitted if r.order_type is OrderType.STOP]
    assert protective and protective[-1].qty == 20, (
        "the undersized leg must never be kept as the only protection"
    )
    adopted = next(a for a in engine.actors.values() if a.spec.symbol == "KORU")
    assert adopted.state is PositionState.OPEN
    await engine.shutdown()


# -- engine stop re-asks every wait iteration (audit A1-11) --------------------


async def test_engine_stop_reissues_close_after_backoff():
    """Audit A1-11: stop() fired close_now ONCE per actor — an actor inside
    its NVDG close backoff silently dropped the ask and STOPPING hung
    forever. The wait loop must re-issue the close each iteration."""
    import time as _time

    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    await engine.start()
    actor = await engine.open_position(_spec(symbol="SPY", qty=10))
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    broker.positions = [PositionInfo("SPY", 10, 100.0, 1000.0, 0.0)]
    # a fresh failure backoff paces away the FIRST ask (the old bug's shape)
    actor._close_backoff_mono = _time.monotonic() + 1.2

    await engine.stop()
    assert engine.state is EngineState.STOPPING
    assert actor.state is PositionState.OPEN, "first ask paced away by the backoff"

    await asyncio.sleep(2.5)  # wait-loop iterations re-ask once the backoff expires
    assert actor.state is PositionState.CLOSING, "the stop must RE-ASK, not hang"
    assert actor.exit_order_id is not None

    exit_request = broker.submitted[-1]
    broker.push_update(
        "fill",
        order_id=actor.exit_order_id,
        client_order_id=exit_request.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        qty=10,
        filled_qty=10,
        price=100.5,
    )
    await _drain()
    await asyncio.sleep(1.2)
    assert actor.state is PositionState.CLOSED
    assert engine.state is EngineState.IDLE
    await engine.shutdown()


async def test_engine_stop_waits_for_halted_actor_without_order_traffic():
    """Audit A1-11 (HALTED shape): a halted actor must get NO close traffic
    (§12 — never an order into a halt/resume) while the stop keeps waiting
    for it, and _issue_stop_closes names it for the 2-minute notify."""
    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    await engine.start()
    actor = await engine.open_position(_spec(symbol="SPY", qty=10))
    await _drain()
    await _fill_entry(broker, actor, qty=10, price=100.0)
    actor.stop_leg_order_id = "stop1"
    actor.enter_halt()
    assert actor.state is PositionState.HALTED

    submitted_before = len(broker.submitted)
    await engine.stop()
    assert await engine._issue_stop_closes() == ["SPY"]  # named for the notify
    await asyncio.sleep(2.2)  # two wait-loop re-ask iterations
    assert engine.state is EngineState.STOPPING, "must keep waiting for the resume"
    assert actor.state is PositionState.HALTED
    assert len(broker.submitted) == submitted_before, "no order traffic into a halt"
    assert broker.canceled == []

    # resume + close (simulated) → the stop finishes
    actor.state = PositionState.CLOSED
    await asyncio.sleep(1.2)
    assert engine.state is EngineState.IDLE
    await engine.shutdown()


# -- open-risk counts a working entry at full size (audit A3-3) ----------------


async def test_open_risk_counts_pending_entry_at_full_spec_qty():
    """Audit A3-3: a PARTIALLY-filled working entry was counted at the
    filled sliver (`filled_qty or spec.qty`) — the rest of the ladder order
    can still fill, so the 6% ceiling was breachable ~2×. While the entry
    works, committed risk is the FULL spec size."""
    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    await engine.start()  # day-start equity 100k
    spec = PositionSpec(
        symbol="SPY",
        side=OrderSide.BUY,
        qty=100,
        stop_price=99.0,
        entry_type=OrderType.LIMIT,
        limit_price=100.0,
    )
    actor = await engine.open_position(spec)
    assert isinstance(actor, PositionActor)
    await _drain()
    assert actor.state is PositionState.PENDING_ENTRY
    # even before any fill: full spec size counts
    assert engine._open_risk_pct() == pytest.approx(0.1)  # 100 sh × $1 / 100k

    entry = broker.submitted[0]
    broker.push_update(
        "partial_fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        qty=100,
        filled_qty=5,
        price=100.0,
    )
    await _drain()
    assert actor.state is PositionState.PENDING_ENTRY
    assert actor.filled_qty == 5
    assert engine._open_risk_pct() == pytest.approx(0.1), (
        "a ladder partial must count the FULL working size, not the sliver"
    )
    # entry completes → filled truth takes over as before
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        qty=100,
        filled_qty=100,
        price=100.0,
    )
    await _drain()
    assert actor.state is PositionState.OPEN
    assert engine._open_risk_pct() == pytest.approx(0.1)
    await engine.shutdown()


# -- risk day baselines roll on the ET date (audit A3-7) -----------------------


async def test_engine_rolls_risk_day_on_et_date_change():
    """Audit A3-7 (engine wiring): the per-poll hook rebases the day/week
    baselines when the ET session date flips — a continuous 24/5 run used
    to keep day-one baselines (and a stuck daily halt) until a restart."""
    from waveapp.engine.risk import HaltState

    clock = {"now": datetime(2026, 9, 22, 18, 0, tzinfo=UTC)}  # 14:00 ET Tue
    broker = FakeBroker()
    engine = EngineCore(broker, database=None, now_fn=lambda: clock["now"])
    await engine.start()
    assert engine.risk._current_day == date(2026, 9, 22)  # ET session date
    engine.risk.update_equity(96_900.0)  # -3.1% → daily halt
    assert engine.risk.halt_state is HaltState.DAILY_LOSS

    # same ET day: the poll hook is a no-op
    await engine.poll_missed_fills()
    assert engine.risk._current_day == date(2026, 9, 22)
    assert engine.risk.halt_state is HaltState.DAILY_LOSS

    # 20:30 ET the same evening is Sep 23 in UTC — the old UTC keying would
    # roll here, 3.5 hours early; the ET keying holds the day
    clock["now"] = datetime(2026, 9, 23, 0, 30, tzinfo=UTC)
    await engine.poll_missed_fills()
    assert engine.risk._current_day == date(2026, 9, 22)
    assert engine.risk.halt_state is HaltState.DAILY_LOSS

    # past ET midnight: the roll fires — fresh account equity becomes the
    # day baseline and the daily halt auto-re-arms (existing behavior)
    clock["now"] = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)  # 05:00 ET Wed
    await engine.poll_missed_fills()
    assert engine.risk._current_day == date(2026, 9, 23)
    assert engine.risk._day_start_equity == 100_000.0
    assert engine.risk.halt_state is HaltState.NONE
    await engine.shutdown()


# -- S1: the short-side actor mirror (2026-09-23) ------------------------------
# Every actor path exercised with side=SELL: bracket entry with the BUY stop
# ABOVE, partials, close_now = buy-to-cover, outside-RTH marketable limits
# through the ASK, direction-aware re-protect, loosen refusal, halt paths.


def _short_spec(symbol="SPY", qty=10, stop=105.0):
    return PositionSpec(symbol=symbol, side=OrderSide.SELL, qty=qty, stop_price=stop)


async def _fill_short_entry(broker, actor, qty=10, price=100.0):
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol=actor.spec.symbol,
        side=OrderSide.SELL,
        qty=qty,
        filled_qty=qty,
        price=price,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()


async def test_short_bracket_entry_carries_stop_above_and_stop_fill_is_a_loss():
    """(a) A SELL bracket entry rests its protective stop ABOVE the entry
    (the broker's leg is a BUY STOP); the stop filling books a LOSS with the
    short-direction P&L sign."""
    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    assert entry.side is OrderSide.SELL
    assert entry.stop_loss is not None and entry.stop_loss.stop_price == 105.0
    await _fill_short_entry(broker, actor, qty=10, price=100.0)
    assert actor.state is PositionState.OPEN
    assert actor.avg_entry_price == 100.0
    assert actor.spec.stop_price > actor.avg_entry_price  # stop ABOVE a short's entry

    # the stop leg fires ABOVE entry: buy-to-cover at 105 → −$50, a LOSS
    broker.push_update(
        "fill",
        order_id="o99",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.STOP,
        qty=10,
        filled_qty=10,
        price=105.0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.CLOSED
    assert actor.exit_reason == "stop hit"
    assert actor.realized_pnl == pytest.approx(-50.0)  # (105−100)×10×(−1)


async def test_short_winning_close_books_positive_pnl():
    """Price falling is a WIN for a short: the realized-P&L direction math."""
    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_short_entry(broker, actor, qty=10, price=100.0)
    broker.push_update(
        "fill",
        order_id="o98",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.STOP,
        qty=10,
        filled_qty=10,
        price=99.0,  # covered LOWER than entry
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.CLOSED
    assert actor.realized_pnl == pytest.approx(10.0)  # (99−100)×10×(−1)


async def test_short_partial_fill_then_cancel_protects_with_buy_stop():
    """(b) CAMT mirror: a partial SELL fill then cancel opens the actor on
    the filled shares and the protective stop is a BUY order at the
    above-entry level."""
    broker = FakeBroker()
    actor = PositionActor(_short_spec(qty=90), broker, TradingMode.PAPER)
    # standalone protective path (no bracket leg learned): drop the leg id
    actor.start()
    await _drain()
    entry = broker.submitted[0]
    broker.push_update(
        "partial_fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        qty=90,
        filled_qty=5,
        price=100.0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        qty=90,
        filled_qty=5,
        price=100.0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.OPEN
    assert actor.filled_qty == 5
    stops = [r for r in broker.submitted if r.order_type is OrderType.STOP]
    assert stops, "the partial short must be protected"
    assert stops[0].side is OrderSide.BUY  # buy-to-cover stop
    assert stops[0].qty == 5
    assert stops[0].stop_price == 105.0  # ABOVE the entry


async def test_short_close_now_is_a_buy_to_cover():
    """(b) close_now on a short submits a BUY for the broker-truth quantity
    (broker reports short qty NEGATIVE — the abs() clamp)."""
    from waveapp.broker.base import PositionInfo

    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.CANCEL_SETTLE_SECONDS = 0
    actor.start()
    await _drain()
    await _fill_short_entry(broker, actor, qty=10, price=100.0)
    broker.positions = [PositionInfo("SPY", -10, 100.0, -1000.0, 0.0)]  # negative = short

    await actor.close_now("test cover")
    assert actor.state is PositionState.CLOSING
    exit_request = broker.submitted[-1]
    assert exit_request.side is OrderSide.BUY
    assert exit_request.qty == 10  # abs of the broker's −10
    assert "exit" in exit_request.client_order_id


async def test_short_close_now_outside_rth_prices_through_the_ask(monkeypatch):
    """(c) A1-12 mirror: covering a short outside RTH goes as a marketable
    LIMIT with extended_hours, priced THROUGH the ask (up), never a queued
    MARKET buy."""
    from types import SimpleNamespace

    from waveapp.broker.base import PositionInfo

    _pin_outside_rth(monkeypatch)
    monkeypatch.setattr(
        PositionActor,
        "quote_source",
        staticmethod(lambda s: SimpleNamespace(bid_price=98.0, ask_price=98.10)),
    )
    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.CANCEL_SETTLE_SECONDS = 0
    actor.start()
    await _drain()
    await _fill_short_entry(broker, actor, qty=10, price=100.0)
    broker.positions = [PositionInfo("SPY", -10, 100.0, -980.0, 0.0, current_price=98.0)]

    await actor.close_now("post-market cover")
    exit_request = broker.submitted[-1]
    assert exit_request.side is OrderSide.BUY
    assert exit_request.order_type is OrderType.LIMIT
    assert exit_request.extended_hours is True
    # limit = ask × (1 + 0.5% buffer), ceil-rounded: ABOVE the ask
    assert exit_request.limit_price == pytest.approx(98.10 * 1.005, abs=0.02)
    assert exit_request.limit_price > 98.10


async def test_short_reprotect_uses_tightest_confirmed_level_direction_aware():
    """(d) A1-5(b) mirror: for a short, the tightest confirmed level is the
    LOWEST — a ratcheted-down stop must survive the re-protect, a looser
    (higher) stale level must never widen past the spec stop."""
    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_short_entry(broker, actor, qty=10, price=100.0)
    # a breakeven ratchet was confirmed at 99.4 (BELOW entry — locked profit)
    actor._confirmed_stop = 99.4
    actor.stop_leg_order_id = None
    await actor._place_protective_stop()
    stop = broker.submitted[-1]
    assert stop.side is OrderSide.BUY
    assert stop.stop_price == 99.4, "re-protect regressed a ratcheted short stop"

    # a stale HIGHER confirmed level (looser for a short) must lose to spec
    actor._confirmed_stop = 108.0
    actor.stop_leg_order_id = None
    await actor._place_protective_stop()
    assert broker.submitted[-1].stop_price == 105.0


async def test_short_failed_exit_reprotects_and_returns_to_open():
    """(d) the naked-window mirror: a refused buy-to-cover submit re-places
    the BUY protective stop and falls back to OPEN so the close retries."""
    from waveapp.broker.base import PositionInfo

    class NoExitBroker(FakeBroker):
        async def submit_order(self, mode, request):
            if request.side is OrderSide.BUY and request.order_type is not OrderType.STOP:
                raise RuntimeError("cover refused")
            return await super().submit_order(mode, request)

    broker = NoExitBroker()
    broker.positions = [PositionInfo("SPY", -10, 100.0, -1000.0, 0.0)]
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.CANCEL_SETTLE_SECONDS = 0
    actor.start()
    await _drain()
    await _fill_short_entry(broker, actor, qty=10, price=100.0)

    await actor.close_now("judge cover")
    assert actor.state is PositionState.OPEN, "failed cover must fall back to OPEN"
    stop = broker.submitted[-1]
    assert stop.order_type is OrderType.STOP and stop.side is OrderSide.BUY
    assert stop.qty == 10 and stop.stop_price == 105.0


async def test_short_manual_tighten_refuses_loosening_upward():
    """(e) tighten_stop_manual mirror: tightening a short's stop means
    moving it DOWN; a request to move it UP (loosen) is refused."""
    from waveapp.engine.exits import ExitEngine, ExitParams

    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_short_entry(broker, actor, qty=10, price=100.0)
    actor.stop_leg_order_id = "stop1"
    actor.exit_engine = ExitEngine(
        side=OrderSide.SELL,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=ExitParams(),
        entry_time=datetime(2026, 9, 23, 14, 0, tzinfo=UTC),
        initial_stop=105.0,
    )
    await actor.tighten_stop_manual(107.0)  # UP = loosening a short — refused
    assert not broker.replaced
    assert actor.exit_engine.current_stop == 105.0
    await actor.tighten_stop_manual(103.0)  # DOWN = tighten — allowed
    assert broker.replaced and broker.replaced[-1][2] == 103.0
    assert actor.exit_engine.current_stop == 103.0


async def test_short_stop_death_during_halt_reprotects_on_resume():
    """(f) halt mirror: a short's stop dying during a LULD halt sends no
    order into the halt; the resume re-places the BUY stop at the spec
    level ABOVE entry."""
    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    await _fill_short_entry(broker, actor, qty=10, price=100.0)
    actor.stop_leg_order_id = "stop1"
    actor.enter_halt()
    assert actor.state is PositionState.HALTED

    before = len(broker.submitted)
    broker.push_update(
        "expired",
        order_id="stop1",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.STOP,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert len(broker.submitted) == before, "no order traffic into a halt (§12)"
    assert actor.state is PositionState.HALTED

    actor.exit_halt()
    await _drain()
    assert actor.state is PositionState.OPEN
    stop = broker.submitted[-1]
    assert stop.order_type is OrderType.STOP and stop.side is OrderSide.BUY
    assert stop.qty == 10 and stop.stop_price == 105.0


async def test_short_halt_before_fill_abandons_the_entry():
    """(f) GRML mirror for shorts: a pre-fill halt cancels the SELL entry and
    latches the abandon flag — no resume chase, no re-peg."""
    broker = FakeBroker()
    actor = PositionActor(_short_spec(), broker, TradingMode.PAPER)
    actor.start()
    await _drain()
    assert actor.state is PositionState.PENDING_ENTRY
    actor.enter_halt()
    await _drain()
    assert actor._entry_abandoned
    assert broker.canceled, "the short entry must be canceled on a pre-fill halt"
    entry = broker.submitted[0]
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
        filled_qty=0,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.CLOSED
    assert actor.exit_reason == "entry abandoned"


async def test_engine_refuses_short_entry_while_shorts_disabled():
    """S1 MASTER GATE end-to-end: EngineCore.open_position with a SELL spec
    is refused by risk.can_enter under the default limits — no order, no
    actor, the reason names the flag."""
    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    await engine.start()
    result = await engine.open_position(_short_spec())
    assert isinstance(result, str)
    assert result == "shorts are disabled (shorts_enabled=false)"
    assert not broker.submitted, "no short order may ever reach the broker"
    assert not engine.actors
    await engine.shutdown()


async def test_engine_admits_short_when_enabled_and_splits_open_risk_by_side():
    """S1: with shorts_enabled=True the SELL entry passes the master gate,
    and _open_risk_pct's side breakdown feeds the short-book share cap."""
    broker = FakeBroker()
    engine = EngineCore(
        broker,
        database=None,
        risk=RiskEngine(limits=RiskLimits(shorts_enabled=True)),
    )
    await engine.start()
    long_actor = await engine.open_position(_spec(symbol="AAPL", qty=10, stop=95.0))
    assert isinstance(long_actor, PositionActor)
    short_actor = await engine.open_position(_short_spec(symbol="MRNA"))
    assert isinstance(short_actor, PositionActor), short_actor
    await _drain()
    # long: (100−95)×10 = riskless until filled — fill both at 100
    for actor in (long_actor, short_actor):
        entry = [r for r in broker.submitted if r.symbol == actor.spec.symbol][0]
        broker.push_update(
            "fill",
            order_id=f"f-{actor.spec.symbol}",
            client_order_id=entry.client_order_id,
            symbol=actor.spec.symbol,
            side=entry.side,
            qty=10,
            filled_qty=10,
            price=100.0,
        )
    await _drain()
    total = engine._open_risk_pct()
    short_only = engine._open_risk_pct(side=OrderSide.SELL)
    # long risk: 5×10/100k = 0.05%; short risk: 5×10/100k = 0.05%
    assert total == pytest.approx(0.10)
    assert short_only == pytest.approx(0.05)
    assert engine._open_risk_pct(side=OrderSide.BUY) == pytest.approx(0.05)
    await engine.shutdown()
