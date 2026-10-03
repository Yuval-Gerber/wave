"""Phase 6 step 6.2: the exit system EXECUTING against the (fake) broker —
actor decision execution, bar routing through EngineCore, and the mandated
halt-during-hold scenario."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from test_engine import FakeBroker, _drain, _spec

from waveapp.broker.base import OrderSide, OrderType, TradingMode
from waveapp.data.hub import Bar, DataHub
from waveapp.engine.actor import PositionActor, PositionState
from waveapp.engine.core import EngineCore
from waveapp.engine.exits import ExitAction, ExitDecision, ExitEngine, ExitParams

T0 = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _far_from_session_boundary(monkeypatch):
    """These tests exercise trailing/halt behavior, not the session-boundary
    flatten. on_market_bar reads the REAL clock for minutes-to-CLOSED, so on
    weekends (inside the Fri→Sun dead zone) the boundary rule preempted the
    expected trail and the tests failed. Pin the boundary far away."""
    from waveapp.engine.session import SessionScheduler

    monkeypatch.setattr(
        SessionScheduler,
        "next_closed_boundary",
        staticmethod(lambda now_utc=None: datetime.now(UTC) + timedelta(hours=6)),
    )


def _bar(i, close, high=None, low=None, volume=1000.0, symbol="SPY") -> Bar:
    return Bar(
        symbol=symbol,
        start=T0 + timedelta(minutes=i),
        open=close,
        high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


async def _open_actor(broker) -> PositionActor:
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
    assert actor.state is PositionState.OPEN
    return actor


def _brain(actor, **overrides) -> ExitEngine:
    params = ExitParams(amend_throttle_seconds=0, **overrides)
    brain = ExitEngine(
        side=actor.spec.side,
        entry_price=100.0,
        qty=10,
        atr_at_entry=0.5,
        params=params,
        entry_time=T0,
        entry_bar_volume=1000.0,
    )
    actor.exit_engine = brain
    actor.filled_qty = 10
    actor.avg_entry_price = 100.0
    return brain


# -- amend execution ---------------------------------------------------------


async def test_amend_decision_replaces_server_stop():
    broker = FakeBroker()
    actor = await _open_actor(broker)
    actor.stop_leg_order_id = "stop-1"
    _brain(actor)
    await actor.apply_exit_decision(
        ExitDecision(ExitAction.AMEND_STOP, reason="trail", new_stop=101.0)
    )
    assert broker.replaced == [("stop-1", None, 101.0)]
    assert actor.stop_leg_order_id == "r2"  # replace returned a NEW id


# -- scale-out execution -----------------------------------------------------


async def test_scale_out_replaces_stop_then_banks():
    broker = FakeBroker()
    actor = await _open_actor(broker)
    actor.stop_leg_order_id = "stop-1"
    brain = _brain(actor)
    brain.remaining_qty = 5
    brain.pending_stop = 100.01
    await actor.apply_exit_decision(
        ExitDecision(ExitAction.SCALE_OUT, reason="target", qty=5, new_stop=100.01)
    )
    # stop replaced FIRST (qty shrunk + breakeven), then the market sell
    assert broker.replaced == [("stop-1", 5, 100.01)]
    scale_order = broker.submitted[-1]
    assert scale_order.qty == 5 and scale_order.side is OrderSide.SELL
    assert "-scale-" in scale_order.client_order_id
    assert actor.state is PositionState.SCALING_OUT
    # scale fill does NOT close the actor; a later stop fill does
    broker.push_update(
        "fill",
        order_id="s9",
        client_order_id=scale_order.client_order_id,
        symbol="SPY",
        side=OrderSide.SELL,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.SCALING_OUT
    broker.push_update(
        "fill",
        order_id="r2",
        client_order_id="",
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
    )
    actor.deliver(broker._queue.get_nowait())
    await _drain()
    assert actor.state is PositionState.CLOSED


# -- mandated scenario: halt during hold -------------------------------------


async def test_halt_during_hold_suppresses_all_exit_actions():
    """LULD halt on a held symbol: the actor holds, the server-side stop stays,
    and NO orders are sent while halted; management resumes after the halt."""
    broker = FakeBroker()
    engine = EngineCore(
        broker, database=None, now_fn=lambda: datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    )  # mid-RTH pin: the daily-close flatten must not fire in these tests
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
    _brain(actor)
    actor.stop_leg_order_id = "stop-1"
    hub = DataHub(["SPY"])
    orders_before = len(broker.submitted) + len(broker.replaced) + len(broker.canceled)

    engine.on_symbol_halt("SPY", True)
    assert actor.state is PositionState.HALTED
    # a big winning bar arrives during the halt → nothing may happen
    engine.on_market_bar(_bar(1, 103.0, high=103.0), hub)
    await _drain()
    assert len(broker.submitted) + len(broker.replaced) + len(broker.canceled) == orders_before

    engine.on_symbol_halt("SPY", False)
    assert actor.state is PositionState.OPEN
    engine.on_market_bar(_bar(2, 103.0, high=103.0), hub)
    await _drain()
    assert broker.replaced, "management resumes after the halt"
    await engine.shutdown()


# -- bar routing & brain seeding ---------------------------------------------


async def test_engine_seeds_exit_brain_and_routes_decisions():
    broker = FakeBroker()
    engine = EngineCore(
        broker, database=None, now_fn=lambda: datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    )  # mid-RTH pin: the daily-close flatten must not fire in these tests
    await engine.start()
    actor = await engine.open_position(_spec())
    await _drain()
    from waveapp.broker.base import PositionInfo

    broker.positions = [PositionInfo("SPY", 100, 100.0, 10000.0, 0.0)]  # broker truth
    entry = broker.submitted[0]
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=entry.client_order_id,
        symbol="SPY",
        side=OrderSide.BUY,
        price=100.0,
    )
    await _drain()
    actor.stop_leg_order_id = "stop-1"

    hub = DataHub(["SPY"])
    assert actor.exit_engine is None
    engine.on_market_bar(_bar(1, 100.1), hub)  # first bar seeds the brain
    assert actor.exit_engine is not None
    assert actor.exit_engine.entry_price == 100.0
    assert actor.exit_engine.current_stop == 95.0  # from the spec's stop

    # runner: decision flows through to a broker replace
    actor.exit_engine.params = ExitParams(amend_throttle_seconds=0)
    engine.on_market_bar(_bar(2, 103.0, high=103.0), hub)
    await _drain()
    assert broker.replaced and broker.replaced[-1][2] > 95.0
    await engine.shutdown()


async def test_vwap_recross_closes_via_engine():
    broker = FakeBroker()
    engine = EngineCore(
        broker, database=None, now_fn=lambda: datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    )  # mid-RTH pin: the daily-close flatten must not fire in these tests
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
        price=100.0,
    )
    await _drain()
    from waveapp.broker.base import PositionInfo

    broker.positions = [PositionInfo("SPY", 100, 100.0, 10000.0, 0.0)]  # broker truth
    actor.stop_leg_order_id = "stop-1"
    actor.CANCEL_SETTLE_SECONDS = 0
    _brain(actor)

    hub = DataHub(["SPY"])
    # session VWAP at 100.2 via trades; close at 100.05: in profit, entered
    # above VWAP... build: entry 100 < vwap? need profit>0 and close<vwap<...
    now = T0 + timedelta(minutes=1)
    hub.bar_builder.on_trade("SPY", 100.2, 1000, now)  # session VWAP = 100.2

    engine.on_market_bar(_bar(2, 100.05, high=100.4), hub)
    await asyncio.sleep(0)
    await _drain()
    # profit (100.05 > 100 entry), vwap 100.2 above close → recross → exit
    exits = [r for r in broker.submitted if "-exit-" in r.client_order_id]
    assert exits and exits[-1].side is OrderSide.SELL
    await engine.shutdown()


async def test_stop_amend_race_is_quiet_not_fatal():
    """Agenda #5a (2026-08-23): amending a stop that just FILLED is a benign
    race — skip quietly, never raise (the sim spammed RuntimeErrors)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from waveapp.broker.base import OrderSide, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec

    spec = PositionSpec(symbol="HL", side=OrderSide.BUY, qty=5, stop_price=17.0)
    adapter = SimpleNamespace(
        replace_order=AsyncMock(side_effect=RuntimeError("no resting stop sim-42 to replace"))
    )
    actor = PositionActor(spec, adapter, TradingMode.PAPER)
    actor.stop_leg_order_id = "sim-42"
    await actor._amend_stop(17.8, "trail ratchet")  # must NOT raise
    assert actor.stop_leg_order_id == "sim-42"  # unchanged — nothing replaced
