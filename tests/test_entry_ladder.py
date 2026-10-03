"""6.5 ENTRY LADDER — the LIVE order path (adopted on adopted,
2026-09-02, after 4/4 wins under both fill models). Every branch of the
lifecycle is exercised against the FakeBroker: post at mid, capture, the
timeout fallback to MARKET, the partial-fill stand-down, external cancels,
and the cancel-vs-fill race. The protective stop must exist in EVERY path
(hard rule 3)."""

from __future__ import annotations

import asyncio

import pytest

from tests.test_engine import FakeBroker
from waveapp.broker.base import OrderSide, OrderType
from waveapp.engine.actor import PositionActor, PositionSpec, PositionState


def _spec(**overrides) -> PositionSpec:
    base = dict(
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        stop_price=49.0,
        entry_type=OrderType.LIMIT,
        limit_price=50.05,  # the mid
        ladder=True,
        decision_price=50.10,
    )
    base.update(overrides)
    return PositionSpec(**base)


@pytest.fixture
def fast_ladder(monkeypatch):
    monkeypatch.setattr(PositionActor, "LADDER_TIMEOUT_SECONDS", 0.05)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        PositionActor, "ladder_event_hook", staticmethod(lambda k, s: events.append((k, s)))
    )
    return events


async def _spin(n: int = 6) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def test_ladder_posts_limit_at_mid_with_bracket(fast_ladder):
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    request = broker.submitted[0]
    assert request.order_type is OrderType.LIMIT
    assert request.limit_price == 50.05
    assert request.stop_loss is not None  # hard rule 3: bracket attached
    assert request.stop_loss.stop_price == 49.0
    assert ("posted", "EIX") in fast_ladder
    actor._task.cancel()
    await _spin()


async def test_ladder_capture_fill_before_timeout(fast_ladder):
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=10,
        order_type=OrderType.LIMIT,
        price=50.05,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.OPEN
    assert actor.avg_entry_price == 50.05
    assert ("captured", "EIX") in fast_ladder
    # the watch timer later wakes and must NOT cancel anything
    await asyncio.sleep(0.08)
    await _spin()
    assert broker.canceled == []


async def test_ladder_timeout_falls_back_to_market(fast_ladder):
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    await asyncio.sleep(0.08)  # timeout fires → cancel goes out
    await _spin()
    assert broker.canceled == ["o1"]
    # broker confirms the cancel → the actor re-enters at MARKET
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.PENDING_ENTRY  # still hunting, not dead
    assert len(broker.submitted) == 2
    fallback = broker.submitted[1]
    assert fallback.order_type is OrderType.MARKET
    assert fallback.limit_price is None
    assert fallback.stop_loss.stop_price == 49.0  # protection rides attempt 2 too
    assert fallback.client_order_id != broker.submitted[0].client_order_id  # idempotency
    assert ("fallback", "EIX") in fast_ladder
    # the market entry fills → OPEN
    broker.push_update(
        "fill",
        order_id="o2",
        client_order_id=fallback.client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=10,
        order_type=OrderType.MARKET,
        price=50.12,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.OPEN
    assert ("captured", "EIX") not in fast_ladder  # a fallback is not a capture


async def test_ladder_partial_fill_at_timeout_keeps_shares(fast_ladder):
    """CAMT rule: partially-filled ladder never chases the rest — the cancel
    opens the position on the real shares with the protective stop."""
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    broker.push_update(
        "partial_fill",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=4,
        order_type=OrderType.LIMIT,
        price=50.05,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    await asyncio.sleep(0.08)  # watch fires — must stand down (shares exist)
    await _spin()
    assert broker.canceled == []  # never cancels a partially-filled entry
    broker.push_update(
        "canceled",  # e.g. the BROKER cancels the remainder later
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=4,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.OPEN  # real shares, protected
    assert len(broker.submitted) >= 2  # protective stop went up for them
    assert ("fallback", "EIX") not in fast_ladder


async def test_external_cancel_still_closes_without_escalation(fast_ladder):
    """A cancel that ISN'T the ladder's own (kill switch, reconcile) keeps
    the old semantics: the actor closes, no market chase."""
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.CLOSED
    assert len(broker.submitted) == 1  # no resubmit


async def test_cancel_racing_fill_is_harmless(fast_ladder, monkeypatch):
    """The timeout's cancel hits an already-filled order: the broker errors,
    the fill event lands, the actor opens normally."""

    broker = FakeBroker()

    async def _cancel_boom(mode, order_id):
        raise RuntimeError("order already filled")

    monkeypatch.setattr(broker, "cancel_order", _cancel_boom)
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    await asyncio.sleep(0.08)  # timeout → cancel raises → flag cleared
    await _spin()
    assert actor._ladder_escalating is False
    broker.push_update(
        "fill",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=10,
        order_type=OrderType.LIMIT,
        price=50.05,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.OPEN


async def test_non_ladder_specs_are_untouched(fast_ladder):
    """ladder=False keeps the exact pre-adoption behavior: MARKET, no watch."""
    broker = FakeBroker()
    spec = _spec(ladder=False, entry_type=OrderType.MARKET, limit_price=None)
    actor = PositionActor(spec, broker, broker.mode)
    actor.start()
    await _spin()
    assert broker.submitted[0].order_type is OrderType.MARKET
    assert actor._ladder_task is None
    assert fast_ladder == []  # no ladder events
    actor._task.cancel()
    await _spin()


async def test_stale_leg_cancel_never_kills_the_fallback(fast_ladder):
    """AXTI at the 2026-09-02 open: canceling a bracket emits TWO cancel
    events (entry + stop leg). The first correctly escalates to MARKET; the
    STALE second one must be ignored — it closed the actor in production
    and orphaned a filled position from exit management."""
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    await asyncio.sleep(0.08)  # ladder timeout → cancel sent
    await _spin()
    entry1_cid = broker.submitted[0].client_order_id
    # event 1: the ENTRY's cancel → escalates to MARKET (attempt 2)
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=entry1_cid,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.PENDING_ENTRY
    assert len(broker.submitted) == 2  # the MARKET fallback went out
    # event 2 (the killer): the OLD bracket's STOP LEG cancel — broker id,
    # empty client id, arrives 1ms later
    broker.push_update(
        "canceled",
        order_id="leg-of-o1",
        client_order_id="",
        symbol="EIX",
        side=OrderSide.SELL,
        qty=10,
        filled_qty=0,
        order_type=OrderType.STOP,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.PENDING_ENTRY  # STILL ALIVE
    # the market fill lands → OPEN and managed
    broker.push_update(
        "fill",
        order_id="o2",
        client_order_id=broker.submitted[1].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=10,
        order_type=OrderType.MARKET,
        price=50.12,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.OPEN
    assert actor.avg_entry_price == 50.12


class _Quote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


async def test_ladder_repeg_posts_fresh_mid_before_market(fast_ladder, monkeypatch):
    """RE-PEG (2026-09-16): first timeout re-posts a LIMIT at the fresh
    mid; only the second timeout goes MARKET. Bracket on every leg."""
    monkeypatch.setattr(PositionActor, "LADDER_REPEG_SECONDS", 0.05)
    monkeypatch.setattr(PositionActor, "quote_source", staticmethod(lambda s: _Quote(50.20, 50.24)))
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    await asyncio.sleep(0.08)  # first timeout → cancel
    await _spin()
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    # re-peg leg: a fresh LIMIT at the new mid (floor of 50.22), bracket on
    assert len(broker.submitted) == 2
    repeg = broker.submitted[1]
    assert repeg.order_type is OrderType.LIMIT
    assert repeg.limit_price == 50.22
    assert repeg.stop_loss is not None and repeg.stop_loss.stop_price == 49.0
    assert ("repeg", "EIX") in fast_ladder
    # second timeout → cancel → MARKET fallback (no second re-peg)
    await asyncio.sleep(0.08)
    await _spin()
    broker.push_update(
        "canceled",
        order_id="o2",
        client_order_id=broker.submitted[1].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert len(broker.submitted) == 3
    assert broker.submitted[2].order_type is OrderType.MARKET
    assert ("fallback", "EIX") in fast_ladder
    actor._task.cancel()


async def test_halt_then_ladder_cancel_never_resubmits(fast_ladder, monkeypatch):
    """GRML 2026-09-22 (−$843), ordering 1 (halt-then-cancel-event): a LULD
    halt on a PENDING ladder entry abandons the WHOLE attempt — the canceled
    event must not re-peg at the frozen pre-halt mid nor fall back to MARKET
    into the halt. Quote source is armed so a re-peg WOULD fire absent the fix."""
    monkeypatch.setattr(PositionActor, "quote_source", staticmethod(lambda s: _Quote(50.20, 50.24)))
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    assert len(broker.submitted) == 1
    actor.enter_halt()  # halt lands while the entry rests
    await _spin()
    assert actor._entry_abandoned is True
    assert broker.canceled == ["o1"]  # enter_halt canceled the entry
    assert actor._ladder_task.cancelled() or actor._ladder_task.done()  # ladder disarmed
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.CLOSED
    assert len(broker.submitted) == 1  # NO fresh order into the halt
    # and the ladder timer waking later must not resurrect anything
    await asyncio.sleep(0.08)
    await _spin()
    assert len(broker.submitted) == 1


async def test_ladder_timeout_racing_halt_never_resubmits(fast_ladder, monkeypatch):
    """GRML ordering 2 (timeout-then-halt): the ladder's own timeout already
    sent its cancel (_ladder_escalating=True); the halt arrives BEFORE the
    canceled event drains. The event must land on the abandon path, never on
    the re-peg/MARKET escalation."""
    monkeypatch.setattr(PositionActor, "quote_source", staticmethod(lambda s: _Quote(50.20, 50.24)))
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    await asyncio.sleep(0.08)  # ladder timeout fires → its cancel goes out
    await _spin()
    assert actor._ladder_escalating is True
    assert broker.canceled == ["o1"]
    actor.enter_halt()  # halt wins the race to the canceled event
    await _spin()
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.CLOSED
    assert len(broker.submitted) == 1  # no re-peg, no MARKET fallback
    assert ("repeg", "EIX") not in fast_ladder
    assert ("fallback", "EIX") not in fast_ladder


async def test_close_now_on_pending_ladder_never_market_falls_back(fast_ladder, monkeypatch):
    """close_now on a PENDING ladder actor (engine STOPPING / manual close):
    the cancel's confirmation must close the actor, not hand the ladder a
    fresh MARKET entry while the engine is shutting down."""
    monkeypatch.setattr(PositionActor, "quote_source", staticmethod(lambda s: _Quote(50.20, 50.24)))
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    await actor.close_now("engine stop")
    assert actor._entry_abandoned is True
    assert broker.canceled == ["o1"]
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.CLOSED
    assert actor.exit_reason == "engine stop"
    assert len(broker.submitted) == 1  # nothing new submitted
    # the ladder timer waking later stays dead too
    await asyncio.sleep(0.08)
    await _spin()
    assert len(broker.submitted) == 1


async def test_halted_pending_partial_fill_still_gets_protected(fast_ladder):
    """Abandon must NEVER orphan real shares: a partial fill before the
    halt-cancel confirms keeps the CAMT reopen path — OPEN with a protective
    stop on the filled sliver, no chase of the remainder."""
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    broker.push_update(
        "partial_fill",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=4,
        order_type=OrderType.LIMIT,
        price=50.05,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    actor.enter_halt()  # halt arrives with 4 real shares held
    await _spin()
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=4,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert actor.state is PositionState.OPEN  # real shares, managed
    # a protective stop went up for the filled sliver (hard rule 3)
    stop = broker.submitted[-1]
    assert stop.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
    assert ("fallback", "EIX") not in fast_ladder  # never chased the rest


async def test_ladder_repeg_skips_without_quote_source(fast_ladder, monkeypatch):
    """No quote source (tests, hub-less contexts) = the OLD behavior:
    straight to MARKET on the first timeout."""
    monkeypatch.setattr(PositionActor, "quote_source", None)
    broker = FakeBroker()
    actor = PositionActor(_spec(), broker, broker.mode)
    actor.start()
    await _spin()
    await asyncio.sleep(0.08)
    await _spin()
    broker.push_update(
        "canceled",
        order_id="o1",
        client_order_id=broker.submitted[0].client_order_id,
        symbol="EIX",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        order_type=OrderType.LIMIT,
    )
    actor.deliver(broker._queue.get_nowait())
    await _spin()
    assert len(broker.submitted) == 2
    assert broker.submitted[1].order_type is OrderType.MARKET
    actor._task.cancel()
