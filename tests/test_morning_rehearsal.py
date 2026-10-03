"""THE MORNING REHEARSAL (the MANDATE, 2026-08-31).

"Every change to wave should make you do a rehearsal to make sure wave will
perform." — after the PMOM gate misfire (a Friday fix silently starved the
Monday 9:20 queue), every install is gated on this file: the REAL
ConnectionMonitor drives the REAL EngineCore through the complete morning
sequence — pre-market watch → 9:00 preflight → 9:20 auction queue → venue
cancel → 9:31 fallback (§8.4 gate on LIVE spread) → entry-pipeline runway
guard → the exit layers on synthetic bars. Fake broker, fake clock, zero
shims around the engine. scripts/build_app.sh refuses to install unless
this passes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from waveapp.engine.connection_monitor import ConnectionMonitor

ET = ZoneInfo("America/New_York")


def _features(symbol, price, prev_close, volume, spread, atr_pct=4.0):
    from waveapp.engine.scanner import SymbolFeatures

    return SymbolFeatures(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        gap_pct=0.0,
        rvol=3.0,
        atr_pct=atr_pct,
        spread=spread,
        day_volume=volume,
        avg_daily_volume=2_000_000,
    )


class _RehearsalHub:
    """Just enough DataHub for the auction paths: watch() and live quotes."""

    def __init__(self) -> None:
        self.latest_quotes: dict = {}
        self.watched: list[str] = []

    async def watch(self, symbols):
        self.watched.extend(symbols)


@pytest.mark.asyncio
async def test_morning_rehearsal_full_sequence(tmp_path, monkeypatch):
    import waveapp.config as config_module
    from tests.test_engine import FakeBroker
    from waveapp.broker.base import OrderType, TimeInForce
    from waveapp.engine.actor import PositionState
    from waveapp.engine.core import EngineCore
    from waveapp.engine.tradegate import TradeGate

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    config = config_module.AppConfig(auto_trade=True)
    config.save(tmp_path / "config.toml")
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, path=None: config))

    broker = FakeBroker()
    engine = EngineCore(broker, database=None)
    await engine.start()

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor.engine = engine
    monitor._adapter = broker
    monitor._scanner = SimpleNamespace(
        gate=TradeGate(),
        expected_move_atr_fraction=0.25,
        slippage_buffer_per_share=0.01,
    )
    hub = _RehearsalHub()
    monitor._hub = hub
    pushes: list[str] = []

    async def fake_push(text, klass=None):
        pushes.append(text)

    monitor._push = fake_push

    # ---- 4:00→9:19 ET: the pre-market watch sees two ramped names ---------
    # BOTH carry WIDE pre-market spreads — the 2026-08-31 fix: pre-market
    # spreads are structurally wide and the auction pays no spread, so the
    # 9:20 queue must NOT gate on them (TS/RRC/KGS were falsely rejected).
    early = datetime(2026, 9, 1, 8, 0, tzinfo=ET)
    later = datetime(2026, 9, 1, 9, 19, tzinfo=ET)
    # ramp ≥3% from the 4:00 print, gap vs prior close <3% (GAPGO's line)
    monitor._premarket_track(
        [
            SimpleNamespace(features=_features("TGHT", 20.0, 20.8, 500_000, spread=0.60)),
            SimpleNamespace(features=_features("WIDE", 80.0, 83.0, 500_000, spread=1.84)),
        ],
        early,
    )
    monitor._premarket_track(
        [
            SimpleNamespace(features=_features("TGHT", 21.0, 20.8, 500_000, spread=0.60)),
            SimpleNamespace(features=_features("WIDE", 84.0, 83.0, 500_000, spread=1.84)),
        ],
        later,
    )

    # ---- 9:01 ET: preflight on a healthy setup stays silent ---------------
    monitor._pm_watch_day = None  # preflight guards read live state only
    await monitor._maybe_run_auction_preflight(datetime(2026, 9, 1, 9, 1, tzinfo=ET))
    assert pushes == [], f"preflight alerted on a healthy morning: {pushes}"

    # ---- 9:20 ET: the auction queue files OPG for BOTH wide-pm names ------
    await monitor._maybe_queue_auction_entries(datetime(2026, 9, 1, 9, 20, tzinfo=ET))
    for _ in range(10):  # actors submit in their own tasks
        await asyncio.sleep(0)
    opg = [r for r in broker.submitted if r.time_in_force is TimeInForce.OPG]
    assert {r.symbol for r in opg} == {"TGHT", "WIDE"}, (
        f"9:20 queue must file every ramped name regardless of pre-market "
        f"spread — got {[r.symbol for r in opg]}"
    )
    for request in opg:
        assert request.order_type is OrderType.LIMIT
        assert request.stop_loss is None  # OPG carries no bracket; stop follows the fill
    assert pushes and "PMOM queued" in pushes[0]
    pushes.clear()

    # ---- the paper venue runs no auction: both OPG orders cancel ----------
    for actor in engine.actors.values():
        if actor.spec.strategy == "PMOM":
            actor.state = PositionState.CLOSED
            actor.exit_reason = "entry canceled"

    # ---- 9:31 ET: fallback re-enters at market, §8.4 gate on LIVE spread --
    # TGHT trades on a 2-cent book → re-enter. WIDE shows its true $1.84
    # spread → the URBN gate refuses it (that toll is REAL at market).
    # A4-7: the entry ladder's mid now requires a FRESH tz-aware quote
    hub.latest_quotes = {
        "TGHT": SimpleNamespace(bid_price=20.99, ask_price=21.01, timestamp=datetime.now(UTC)),
        "WIDE": SimpleNamespace(bid_price=83.08, ask_price=84.92, timestamp=datetime.now(UTC)),
    }
    submitted_before = len(broker.submitted)
    await monitor._maybe_auction_fallback(datetime(2026, 9, 1, 9, 31, tzinfo=ET))
    for _ in range(10):
        await asyncio.sleep(0)
    fallback_orders = broker.submitted[submitted_before:]
    fallback_symbols = {r.symbol for r in fallback_orders}
    assert "TGHT" in fallback_symbols, "tight-spread fallback re-entry never reached the broker"
    assert "WIDE" not in fallback_symbols, (
        "the §8.4 gate must refuse a $1.84 live spread at the fallback (URBN class)"
    )
    tght = next(r for r in fallback_orders if r.symbol == "TGHT")
    # 6.5 entry ladder (ADOPTED 2026-09-02, 4/4 both fill models): the entry
    # posts as a LIMIT at the quote mid (bid 20.99 / ask 21.01 → 21.00); the
    # actor falls back to MARKET on its own timeout if the mid never fills
    assert tght.order_type is OrderType.LIMIT
    assert tght.limit_price == 21.0
    assert tght.stop_loss is not None, (
        "fallback entry must carry the server-side stop (hard rule 3)"
    )

    await engine.stop()


@pytest.mark.asyncio
async def test_entry_pipeline_respects_the_runway_cutoff(tmp_path, monkeypatch):
    """ESI 2026-08-31: bought at 15:43, force-flattened at 15:50, −$43. The
    pipeline must refuse entries with less than ENTRY_CUTOFF_MINUTES to the
    close (70 = 60 min of runway before the 15:50 flatten) and proceed when
    the runway exists."""
    import waveapp.config as config_module
    import waveapp.engine.session as session_module
    from waveapp.engine.connection_monitor import ENTRY_CUTOFF_MINUTES
    from waveapp.engine.session import Regime

    assert ENTRY_CUTOFF_MINUTES >= 70.0, "runway guard weakened below the adopted 60-min evidence"

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    config = config_module.AppConfig(auto_trade=True)
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, path=None: config))

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor.engine = SimpleNamespace(risk=SimpleNamespace(limits=SimpleNamespace(max_positions=6)))
    monitor._hub = _RehearsalHub()
    monitor._scanner = SimpleNamespace()

    fake_info = SimpleNamespace(
        regime=Regime.MIDDAY, et_time=datetime(2026, 9, 1, 15, 5, tzinfo=ET)
    )
    monkeypatch.setattr(session_module.SessionScheduler, "info", staticmethod(lambda: fake_info))

    reached_day_list = []

    async def sentinel_day_list(results, now_et, min_price):
        reached_day_list.append(now_et)
        return []

    monitor._ensure_day_list = sentinel_day_list

    # 15:05 ET → 55 min to close < 70 → paused BEFORE the day list
    monkeypatch.setattr(
        session_module.SessionScheduler, "minutes_to_rth_close", staticmethod(lambda et: 55.0)
    )
    await monitor._entry_pipeline([])
    assert reached_day_list == [], "entry pipeline ran inside the no-runway window"

    # 11:00 ET → 300 min to close → proceeds to the day list
    monkeypatch.setattr(
        session_module.SessionScheduler, "minutes_to_rth_close", staticmethod(lambda et: 300.0)
    )
    await monitor._entry_pipeline([])
    assert reached_day_list, "entry pipeline never proceeded with a full runway"


def test_exit_layers_on_synthetic_bars():
    """The afternoon half of the rehearsal: a REAL ExitEngine walks synthetic
    bars through the layers that fired (and misfired) on 2026-08-31."""
    from waveapp.broker.base import OrderSide
    from waveapp.data.hub import Bar
    from waveapp.engine.exits import ExitAction, ExitEngine, params_for
    from waveapp.engine.session import Regime

    def bar(ts, price, volume=10_000):
        return Bar(
            symbol="RHRSL",
            start=ts,
            open=price,
            high=price + 0.002,
            low=price - 0.002,
            close=price,
            volume=volume,
        )

    start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    params = params_for(Regime.MIDDAY)

    # 1. HDB truth fix: a micro-ATR trail must ratchet in WHOLE CENTS so the
    #    engine, the card and the broker order all hold the same number.
    engine = ExitEngine(
        side=OrderSide.BUY,
        entry_price=22.64,
        qty=1136,
        initial_stop=22.40,
        entry_time=start,
        params=params,
        atr_at_entry=0.011,
    )
    price = 22.64
    for i in range(1, 30):
        price += 0.004  # slow grind up — the trail hugs behind
        decision = engine.on_bar(bar(start + timedelta(minutes=i), price), atr=0.011)
        if decision.action is ExitAction.AMEND_STOP:
            cents = decision.new_stop * 100.0
            assert abs(cents - round(cents)) < 1e-9, (
                f"stop amendment {decision.new_stop} is not a whole cent — "
                f"the card would promise money the resting order cannot lock"
            )
            assert engine.current_stop == decision.new_stop, (
                "engine must keep the SAME rounded stop the broker order gets"
            )

    # 2. Session boundary: the flatten decision fires inside the lead window
    #    no matter what the position looks like.
    engine2 = ExitEngine(
        side=OrderSide.BUY,
        entry_price=50.0,
        qty=100,
        initial_stop=49.0,
        entry_time=start,
        params=params,
        atr_at_entry=0.10,
    )
    decision = engine2.on_bar(
        bar(start + timedelta(minutes=5), 50.2), minutes_to_close_boundary=9.0
    )
    assert decision.action is ExitAction.EXIT_NOW
    assert "flatten" in decision.reason

    # 3. The ACGL/PYPL crack, documented: a bleeder at −0.6% past t_max does
    #    NOT exit today (t_max_cut_losers ships OFF until §11 adopts it) —
    #    this assertion flips the day the dead-hold sweep result is adopted.
    assert params.t_max_cut_losers is False
    engine3 = ExitEngine(
        side=OrderSide.BUY,
        entry_price=100.0,
        qty=100,
        initial_stop=98.5,
        entry_time=start,
        params=params,
        atr_at_entry=0.50,
    )
    decision = engine3.on_bar(bar(start + timedelta(minutes=95), 99.40), atr=0.50)
    assert decision.action in (ExitAction.NONE, ExitAction.AMEND_STOP), (
        "the one-sided time stop must stay OFF until the §11 sweep adopts it"
    )


@pytest.mark.asyncio
async def test_scanner2_shadow_is_isolated_from_trading(tmp_path, monkeypatch):
    """Scanner 2.0 S1 (2026-08-31): the shadow scanner trades NOTHING and a
    failure inside it must never reach the live pipeline. A step() that
    explodes on every sweep leaves the entry pipeline fully functional."""
    import waveapp.config as config_module
    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)

    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe(
        [SimpleNamespace(symbol="ISO", name="", tradable=True, asset_class="AssetClass.US_EQUITY")]
    )

    async def exploding_step(now_et):
        raise RuntimeError("scanner2 sweep blew up")

    scanner.step = exploding_step
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._scanner2 = scanner
    task = asyncio.ensure_future(monitor._scanner2_loop())
    await asyncio.sleep(0)  # the loop only sleeps/catches — nothing propagates
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # trading side unaffected: the runway-guard pipeline still runs cleanly
    config = config_module.AppConfig(auto_trade=True)
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, path=None: config))
    monitor.engine = SimpleNamespace(risk=SimpleNamespace(limits=SimpleNamespace(max_positions=6)))
    monitor._hub = _RehearsalHub()
    monitor._scanner = SimpleNamespace()
    reached = []

    async def sentinel(results, now_et, min_price):
        reached.append(True)
        return []

    monitor._ensure_day_list = sentinel
    import waveapp.engine.session as session_module
    from waveapp.engine.session import Regime

    fake_info = SimpleNamespace(
        regime=Regime.MIDDAY, et_time=datetime(2026, 9, 1, 11, 0, tzinfo=ET)
    )
    monkeypatch.setattr(session_module.SessionScheduler, "info", staticmethod(lambda: fake_info))
    monkeypatch.setattr(
        session_module.SessionScheduler, "minutes_to_rth_close", staticmethod(lambda et: 300.0)
    )
    await monitor._entry_pipeline([])
    assert reached, "entry pipeline must run regardless of scanner2's state"


@pytest.mark.asyncio
async def test_scanner2_live_menu_feeds_the_day_list(tmp_path, monkeypatch):
    """decision (2026-08-31 night): Scanner 2.0 IS the menu. A fresh
    scanner2 menu must become the day list; a stale one must fall back to
    the old path automatically."""
    import waveapp.config as config_module

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    config = config_module.AppConfig(auto_trade=True)
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, path=None: config))

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._hub = _RehearsalHub()
    monitor._chart_backfill = {"HOTA": [], "HOTB": []}  # backfill no-op
    now_et = datetime(2026, 9, 1, 10, 47, tzinfo=ET)

    fresh_scanner = SimpleNamespace(
        last_menu=[{"symbol": "HOTA"}, {"symbol": "HOTB"}],
        last_step_ts=now_et.timestamp() - 30.0,
    )
    monitor._scanner2 = fresh_scanner
    day_list = await monitor._ensure_day_list([], now_et, 15.0)
    assert day_list == ["HOTA", "HOTB"], "fresh scanner2 menu must BE the day list"
    assert set(monitor._hub.watched) == {"HOTA", "HOTB"}  # hub watches the menu

    # stale scanner2 (no sweep for >5 min) → old path takes over, loudly
    monitor._scanner2 = SimpleNamespace(
        last_menu=[{"symbol": "STALE"}], last_step_ts=now_et.timestamp() - 600.0
    )
    monitor._day_list_day = now_et.date().isoformat()
    monitor._day_list = ["OLDWAY"]
    day_list = await monitor._ensure_day_list([], now_et, 15.0)
    assert day_list == ["OLDWAY"], "stale scanner2 must never drive entries"

    # scanner2_live=False → old path even with a fresh menu (the off switch)
    config2 = config_module.AppConfig(auto_trade=True, scanner2_live=False)
    monkeypatch.setattr(
        config_module.AppConfig, "load", classmethod(lambda cls, path=None: config2)
    )
    monitor._scanner2 = fresh_scanner
    day_list = await monitor._ensure_day_list([], now_et, 15.0)
    assert day_list == ["OLDWAY"]
