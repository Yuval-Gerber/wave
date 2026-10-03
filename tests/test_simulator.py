"""Phase 10.5: SimBroker + SessionReplay drive the REAL engine code paths."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from waveapp.research.sim_broker import SimBroker

from waveapp.broker.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    StopLoss,
    TimeInForce,
    TradingMode,
)
from waveapp.data.hub import Bar
from waveapp.engine.actor import PositionSpec

ET = "America/New_York"


def _bar(symbol, ts, o, h, l, c, v=10_000.0):  # noqa: E741
    return Bar(symbol=symbol, start=ts, open=o, high=h, low=l, close=c, volume=v)


T0 = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)  # 09:30 ET


def test_sim_broker_entry_carries_stop_leg_and_crosses_spread():
    import asyncio

    async def run():
        sim = SimBroker(equity=10_000)
        sim.process_bar(_bar("AAPL", T0, 100.0, 100.2, 99.9, 100.0))
        order = await sim.submit_order(
            TradingMode.PAPER,
            OrderRequest(
                symbol="AAPL",
                qty=10,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                client_order_id="wave-abc123def456-entry-1",
                stop_loss=StopLoss(stop_price=99.0),
            ),
        )
        assert order.client_order_id == "wave-abc123def456-entry-1"
        assert order.filled_avg_price > 100.0  # crossed the spread + slip
        assert len(order.legs) == 1 and order.legs[0].order_type == OrderType.STOP
        assert order.legs[0].order_id  # real id the actor can amend
        # replace mints a NEW id (live-broker contract)
        replaced = await sim.replace_order(
            TradingMode.PAPER, order.legs[0].order_id, stop_price=99.5
        )
        assert replaced.order_id != order.legs[0].order_id
        assert (await sim.get_open_orders(TradingMode.PAPER))[0].stop_price == 99.5
        return sim

    asyncio.run(run())


def test_sim_broker_stop_fills_on_bar_path_with_gap_through():
    import asyncio

    async def run():
        sim = SimBroker(equity=10_000)
        sim.process_bar(_bar("TSLA", T0, 100.0, 100.2, 99.9, 100.0))
        await sim.submit_order(
            TradingMode.PAPER,
            OrderRequest(
                symbol="TSLA",
                qty=10,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                client_order_id="wave-abc123def456-entry-1",
                stop_loss=StopLoss(stop_price=99.0),
            ),
        )
        # gap DOWN through the stop: fill at the open (worse), minus slip
        sim.process_bar(_bar("TSLA", T0 + timedelta(minutes=1), 98.5, 98.8, 98.2, 98.4))
        assert not await sim.get_open_orders(TradingMode.PAPER)
        stop_fill = sim.fills[-1]
        assert stop_fill.reason == "stop"
        assert stop_fill.price < 98.5  # open minus slippage, never 99.0
        positions = await sim.get_positions(TradingMode.PAPER)
        assert positions == []  # flat
        # sells paid regulatory fees
        assert sim.fees_paid > 0

    asyncio.run(run())


@pytest.mark.asyncio
async def test_replay_runs_real_engine_stop_out(monkeypatch):
    """A losing long: entry at 09:36, grind down, server-side stop fills.
    The REAL PositionActor/ExitEngine manage it end to end."""
    from waveapp.research.simulator import replay_session

    index = pd.date_range("2026-03-10 09:30", periods=40, freq="1min", tz=ET)
    prices = [100.0] * 6 + [100.0 - 0.05 * i for i in range(1, 35)]
    frame = pd.DataFrame(
        {
            "open": prices[:40],
            "high": [p + 0.03 for p in prices[:40]],
            "low": [p - 0.06 for p in prices[:40]],
            "close": [p - 0.02 for p in prices[:40]],
            "volume": [8_000.0] * 40,
        },
        index=index,
    )

    def signal(symbol, bars):
        if len(bars) == 6:  # enter on the 6th bar
            price = bars[-1].close
            return PositionSpec(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=10,
                stop_price=round(price - 0.6, 2),
                strategy="ORB",
            )
        return None

    result = await replay_session({"SIM1": frame}, signal, equity=50_000)
    reasons = [f.reason for f in result.fills]
    assert "entry" in reasons
    assert "stop" in reasons or "market_exit" in reasons  # protected exit happened
    assert result.net_pnl < 0  # a controlled, stop-sized loss
    assert result.net_pnl > -80  # 10 shares × ~0.6 risk + costs, NOT a blowout
    assert result.fees_paid > 0


@pytest.mark.asyncio
async def test_replay_winner_scales_out_and_trails():
    """A clean runner: the real exit system should bank the scale-out and
    trail the rest — net positive after adverse fills."""
    from waveapp.research.simulator import replay_session

    index = pd.date_range("2026-03-10 09:30", periods=60, freq="1min", tz=ET)
    prices = [100.0] * 6 + [100.0 + 0.08 * i for i in range(1, 45)] + [103.4] * 10
    frame = pd.DataFrame(
        {
            "open": prices[:60],
            "high": [p + 0.05 for p in prices[:60]],
            "low": [p - 0.04 for p in prices[:60]],
            "close": [p + 0.02 for p in prices[:60]],
            "volume": [9_000.0] * 60,
        },
        index=index,
    )

    def signal(symbol, bars):
        if len(bars) == 6:
            price = bars[-1].close
            return PositionSpec(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=20,
                stop_price=round(price - 0.5, 2),
                strategy="ORB",
            )
        return None

    from waveapp.engine.exits import ExitParams

    # explicit scale-friendly params: this test exercises the ENGINE's
    # scale-out mechanics, not the (now trail-only) adopted defaults
    result = await replay_session(
        {"SIM2": frame},
        signal,
        equity=50_000,
        exit_params_fn=lambda regime: ExitParams(k_trail=2.0, t_max_minutes=90),
    )
    assert result.net_pnl > 0
    sells = [f for f in result.fills if f.side == OrderSide.SELL]
    assert len(sells) >= 2  # scale-out + final exit — the 7-layer system acted
    # share conservation: bought == sold, dead flat (the 2026-08-18 oversell
    # bug sold the ORIGINAL qty again after a scale-out → phantom shorts)
    bought = sum(f.qty for f in result.fills if f.side == OrderSide.BUY)
    sold = sum(f.qty for f in result.fills if f.side == OrderSide.SELL)
    assert bought == sold


@pytest.mark.asyncio
async def test_close_after_scale_out_sells_only_the_remainder():
    """Regression (2026-08-18): close_now sold the ORIGINAL entry qty after a
    scale-out — oversell, phantom short, fabricated campaign profits."""
    from waveapp.research.simulator import replay_session

    index = pd.date_range("2026-03-10 09:30", periods=40, freq="1min", tz=ET)
    # rise far enough to scale out, then go DEAD flat → time/momentum exit
    prices = [100.0] * 6 + [100.0 + 0.15 * i for i in range(1, 15)] + [102.1] * 20
    frame = pd.DataFrame(
        {
            "open": prices[:40],
            "high": [p + 0.04 for p in prices[:40]],
            "low": [p - 0.04 for p in prices[:40]],
            "close": [p + 0.01 for p in prices[:40]],
            "volume": [9_000.0] * 40,
        },
        index=index,
    )

    def signal(symbol, bars):
        if len(bars) == 6:
            price = bars[-1].close
            return PositionSpec(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=20,
                stop_price=round(price - 0.5, 2),
                strategy="ORB",
            )
        return None

    result = await replay_session({"SIM3": frame}, signal, equity=50_000)
    bought = sum(f.qty for f in result.fills if f.side == OrderSide.BUY)
    sold = sum(f.qty for f in result.fills if f.side == OrderSide.SELL)
    assert bought == sold  # dead flat — never short
    assert bought == 20


@pytest.mark.asyncio
async def test_stop_fill_racing_momentum_exit_never_double_sells():
    """Regression (2026-08-18, NKTR forensic): the resting stop fills on the
    SAME bar a volume-death EXIT_NOW fires — close_now must notice the
    position closed itself mid-cancel and NOT sell again (live race too)."""
    from waveapp.research.simulator import replay_session

    index = pd.date_range("2026-03-10 09:30", periods=50, freq="1min", tz=ET)
    rows = []
    for i in range(50):
        if i < 6:
            rows.append((100.0, 100.1, 99.9, 100.0, 9000.0))
        elif i < 20:  # strong push: trail ratchets the stop up under price
            p = 100.0 + 0.25 * (i - 5)
            rows.append((p, p + 0.2, p - 0.1, p + 0.15, 9000.0))
        else:  # the killer bar(s): price knifes back through the stop AND
            # volume collapses → stop fill and EXIT_NOW race
            rows.append((101.0, 101.2, 100.2, 100.4, 150.0))
    frame = pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [r[4] for r in rows],
        },
        index=index,
    )

    def signal(symbol, bars):
        if len(bars) == 6:
            price = bars[-1].close
            return PositionSpec(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=30,
                stop_price=round(price - 0.5, 2),
                strategy="ORB",
            )
        return None

    result = await replay_session({"RACE": frame}, signal, equity=50_000)
    bought = sum(f.qty for f in result.fills if f.side == OrderSide.BUY)
    sold = sum(f.qty for f in result.fills if f.side == OrderSide.SELL)
    assert bought == 30
    assert sold == 30  # exactly one exit — never a phantom short


@pytest.mark.asyncio
async def test_stop_entry_fills_at_the_level_not_the_close():
    """10.7: a buy-stop resting AT the breakout level fills at level+slip on
    the trigger bar — not at the bar's close — and the protective leg arms."""
    from waveapp.research.simulator import replay_session

    index = pd.date_range("2026-03-10 09:30", periods=30, freq="1min", tz=ET)
    # flat 100-101 open, then a bar that RIPS through 101 and closes at 102.5
    prices = (
        [(100.2, 101.0, 100.0, 100.8)] * 6
        + [(100.9, 102.6, 100.8, 102.5)]
        + [(102.5, 102.6, 102.3, 102.4)] * 23
    )
    frame = pd.DataFrame(
        {
            "open": [p[0] for p in prices[:30]],
            "high": [p[1] for p in prices[:30]],
            "low": [p[2] for p in prices[:30]],
            "close": [p[3] for p in prices[:30]],
            "volume": [9000.0] * 30,
        },
        index=index,
    )

    from waveapp.broker.base import OrderType

    def signal(symbol, bars):
        if len(bars) == 6:  # at 09:35: place the stop-entry AT the level
            return PositionSpec(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=10,
                stop_price=100.0,  # protective stop at the range low
                entry_type=OrderType.STOP,
                entry_stop_price=101.01,
                strategy="ORB",
            )
        return None

    result = await replay_session({"LVL": frame}, signal, equity=50_000)
    entries = [f for f in result.fills if f.reason == "entry"]
    assert len(entries) == 1
    # filled AT the level (101.01 + slip) — the market-at-close path would
    # have paid ~102.5 on this bar
    assert entries[0].price < 101.1
    bought = sum(f.qty for f in result.fills if f.side == OrderSide.BUY)
    sold = sum(f.qty for f in result.fills if f.side == OrderSide.SELL)
    assert bought == sold == 10  # protective leg armed; flat at end


@pytest.mark.asyncio
async def test_stop_entry_never_triggers_cancels_cleanly():
    from waveapp.research.simulator import replay_session

    from waveapp.broker.base import OrderType

    index = pd.date_range("2026-03-10 09:30", periods=20, freq="1min", tz=ET)
    frame = pd.DataFrame(
        {
            "open": [100.2] * 20,
            "high": [100.9] * 20,
            "low": [100.0] * 20,
            "close": [100.5] * 20,
            "volume": [9000.0] * 20,
        },
        index=index,
    )

    def signal(symbol, bars):
        if len(bars) == 6:
            return PositionSpec(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=10,
                stop_price=99.5,
                entry_type=OrderType.STOP,
                entry_stop_price=101.5,  # never reached
                strategy="ORB",
            )
        return None

    result = await replay_session({"NOPE": frame}, signal, equity=50_000)
    assert result.fills == []  # nothing triggered, nothing phantom
    assert result.net_pnl == 0


def test_entry_ladder_captures_mid_or_pays_the_chase():
    """6.5 seam (P2): with entry_ladder on, an entry posts at MID and
    resolves on the next bar — pullback fills at mid (half-spread saved),
    runaway fills at next-bar open + adverse (the price of patience)."""
    import asyncio

    def request(symbol):
        return OrderRequest(
            symbol=symbol,
            qty=10,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            client_order_id=f"wave-{symbol.lower()}0000000000-entry-1",
            stop_loss=StopLoss(stop_price=99.0),
        )

    async def run():
        sim = SimBroker(equity=100_000, entry_ladder=True)
        for symbol in ("PULL", "RUNS"):
            sim.process_bar(_bar(symbol, T0, 100.0, 100.2, 99.9, 100.0))
        pull = await sim.submit_order(TradingMode.PAPER, request("PULL"))
        runs = await sim.submit_order(TradingMode.PAPER, request("RUNS"))
        # deferred, not filled at submit; protective leg pre-registered
        assert pull.status.value == "accepted" and pull.filled_qty == 0
        assert runs.legs[0].order_type is OrderType.STOP
        # PULL's next bar trades back through the mid → filled AT 100.00
        sim.process_bar(_bar("PULL", T0 + timedelta(minutes=1), 100.1, 100.3, 99.95, 100.2))
        # RUNS gaps and never looks back → chased at next-bar open + adverse
        sim.process_bar(_bar("RUNS", T0 + timedelta(minutes=1), 100.5, 101.0, 100.45, 100.9))
        fills = {f.symbol: f for f in sim.fills}
        assert fills["PULL"].price == 100.0  # the half-spread was captured
        assert fills["RUNS"].price > 100.5  # open + adverse — worse than mid
        # both protective stops armed at the broker (hard rule 3)
        open_stops = await sim.get_open_orders(TradingMode.PAPER)
        assert sum(1 for o in open_stops if o.order_type is OrderType.STOP) == 2
        # ladder never touches exits: a plain market SELL still crosses now
        sell = await sim.submit_order(
            TradingMode.PAPER,
            OrderRequest(
                symbol="PULL",
                qty=10,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                client_order_id="wave-pull0000000000-close-1",
            ),
        )
        assert sell.status.value == "filled"

    asyncio.run(run())
