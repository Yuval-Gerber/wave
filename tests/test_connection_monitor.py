"""ConnectionMonitor: dot transitions with a faked adapter, no network."""

from unittest.mock import AsyncMock, patch

from waveapp.broker.base import MissingCredentialsError
from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.ui import theme


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, name: str, color: str, tooltip: str) -> None:
        self.calls.append((name, color, tooltip))

    def last_color(self, name: str) -> str | None:
        for n, color, _ in reversed(self.calls):
            if n == name:
                return color
        return None


async def test_missing_keys_shows_grey_dots():
    recorder = _Recorder()
    monitor = ConnectionMonitor(recorder)
    with patch("waveapp.engine.connection_monitor.AlpacaAdapter") as adapter_cls:
        adapter_cls.return_value.connect = AsyncMock(side_effect=MissingCredentialsError("no keys"))
        await monitor._try_connect()
    assert recorder.last_color("Alpaca") == theme.GREY
    assert recorder.last_color("Data feed") == theme.GREY


async def test_connect_failure_shows_red():
    recorder = _Recorder()
    monitor = ConnectionMonitor(recorder)
    with patch("waveapp.engine.connection_monitor.AlpacaAdapter") as adapter_cls:
        adapter_cls.return_value.connect = AsyncMock(side_effect=OSError("boom"))
        await monitor._try_connect()
    assert recorder.last_color("Alpaca") == theme.RED


async def test_poll_failure_tears_down_and_goes_red():
    recorder = _Recorder()
    monitor = ConnectionMonitor(recorder)
    adapter = AsyncMock()
    adapter.get_clock.side_effect = OSError("gone")
    monitor._adapter = adapter
    await monitor._poll()
    assert recorder.last_color("Alpaca") == theme.RED
    assert monitor._adapter is None
    adapter.close.assert_awaited()


async def test_db_dot_green_with_healthy_database(tmp_path):
    from waveapp.persistence.db import Database

    recorder = _Recorder()
    database = Database(tmp_path / "t.db")
    monitor = ConnectionMonitor(recorder, database=database)
    monitor._report_db_health()
    assert recorder.last_color("DB") == theme.GREEN
    database.close()


async def test_db_dot_grey_without_database():
    recorder = _Recorder()
    monitor = ConnectionMonitor(recorder, database=None)
    monitor._report_db_health()
    assert recorder.last_color("DB") == theme.GREY


# -- ML shadow, agenda #3 redo steps 2+3 (2026-08-23) -------------------------


def test_labeler_triple_barrier_math():
    from waveapp.engine.labeler import compute_outcome

    def bar(ts, o, h, lo, c):
        return (ts, o, h, lo, c)

    entry_ts = 100.0
    # win: +1×ATR touched before −1.5×ATR (ATR = $1, entry = next bar open 10.0)
    bars = [bar(90, 9.9, 10.0, 9.8, 9.9)] + [
        bar(100 + i, 10.0, 10.0 + i * 0.3, 9.9, 10.0 + i * 0.2) for i in range(6)
    ]
    out = compute_outcome(bars, entry_ts, atr=1.0)
    assert out["label"] == "win" and out["entry"] == 10.0 and out["mfe_atr"] >= 1.0
    # loss: the stop barrier hits first
    bars = [bar(100 + i, 10.0, 10.05, 10.0 - i * 0.6, 10.0 - i * 0.5) for i in range(6)]
    assert compute_outcome(bars, entry_ts, atr=1.0)["label"] == "loss"
    # both barriers inside one bar → conservative loss
    bars = [bar(100, 10.0, 10.1, 9.9, 10.0), bar(101, 10.0, 11.5, 8.0, 10.0)] + [
        bar(102 + i, 10.0, 10.1, 9.9, 10.0) for i in range(4)
    ]
    assert compute_outcome(bars, entry_ts, atr=1.0)["label"] == "loss"
    # flat: nothing touched by the session's end
    bars = [bar(100 + i, 10.0, 10.2, 9.8, 10.0) for i in range(6)]
    assert compute_outcome(bars, entry_ts, atr=1.0)["label"] == "flat"
    # honesty guards: no bars after entry / degenerate ATR → None
    assert compute_outcome(bars, 9_999.0, atr=1.0) is None
    assert compute_outcome(bars, entry_ts, atr=0.0) is None


def test_ml_stats_worker_compute_and_active_refusal(tmp_path):
    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    for i, (decision, outcome) in enumerate(
        [("accepted", '{"label": "win"}'), ("rejected", '{"label": "loss"}'), ("rejected", None)]
    ):
        database.execute(
            "INSERT INTO candidates (symbol, session_date, ts, strategy, features,"
            " decision, outcome) VALUES (?, '2026-08-20', ?, 'ORB', '{}', ?, ?)",
            (f"SYM{i}", f"2026-08-20T13:3{i}:00+00:00", decision, outcome),
        )
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = database
    # the accessor NEVER blocks: it returns the (default) snapshot
    assert monitor._ml_stats()["rows"] == 0
    stats = monitor._ml_stats_compute()  # worker-thread payload
    assert stats["rows"] == 3 and stats["symbol_days"] == 3
    assert stats["labeled"] == 2 and stats["label_wins"] == 1 and stats["label_losses"] == 1
    assert stats["accepted_days"] == 1 and not stats["model_ready"]
    # the labeler's batch picks only unlabeled, finished symbol-days
    batch = monitor._label_batch_rows()
    assert [r["symbol"] for r in batch] == ["SYM2"]
    # ACTIVE is refused while no validated model exists (§9.2)
    assert monitor.apply_ml_mode("active").startswith("refused")
    assert "shadow" in monitor.apply_ml_mode("shadow")
    database.execute(
        "INSERT INTO model_registry (ts, kind, version, active)"
        " VALUES ('2026-09-01', 'scanner_ranker', 'v1', 1)"
    )
    assert monitor._ml_stats_compute()["model_ready"]
    assert monitor.apply_ml_mode("active").startswith("ACTIVE")
    database.close()


def test_ml_mode_display_survives_restart(monkeypatch, tmp_path):
    """(2026-08-23): the switch must show the saved pick from the very
    first push — the default 'off' snapshot was overriding it at relaunch."""
    from waveapp import config as config_module
    from waveapp.engine.connection_monitor import ConnectionMonitor

    saved = config_module.AppConfig(ml_mode="shadow")
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, p=None: saved))
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    assert monitor._ml_stats()["mode"] == "shadow"  # before ANY background refresh


def test_labeler_runs_only_while_the_market_world_sleeps():
    """(2026-08-23): never on open market, never while the scanner is
    working it (PRE/POST included) — OVERNIGHT and CLOSED only."""
    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.engine.session import Regime

    allowed = ConnectionMonitor._labeler_allowed
    assert allowed(Regime.OVERNIGHT) and allowed(Regime.CLOSED)
    for busy in (Regime.PRE, Regime.OPEN_DRIVE, Regime.MIDDAY, Regime.POWER_HOUR, Regime.POST):
        assert not allowed(busy)


def test_scanner_idle_note_tells_the_truth():
    """2026-08-23 (Sun 20:11): 'resumes Sunday 20:00' was when the
    VENUE wakes, not the scanner — the note now names the real resume time
    (next trading day 04:00, holiday-aware) and changes per regime."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.engine.session import Regime

    et = ZoneInfo("America/New_York")
    note = ConnectionMonitor._idle_note(Regime.CLOSED, datetime(2026, 8, 23, 15, 0, tzinfo=et))
    assert note == "market closed — scanning resumes Monday 04:00 ET"
    # Labor Day weekend: Saturday Sep 5 → resumes TUESDAY (Mon is a holiday)
    note = ConnectionMonitor._idle_note(Regime.CLOSED, datetime(2026, 9, 5, 12, 0, tzinfo=et))
    assert note == "market closed — scanning resumes Tuesday 04:00 ET"
    note = ConnectionMonitor._idle_note(Regime.OVERNIGHT, datetime(2026, 8, 23, 21, 0, tzinfo=et))
    assert "overnight session (exit-only)" in note and "04:00" in note


def test_monday_wakeup_timeline():
    """2026-08-23 (night before the first full Monday): every
    automatic gate flips at the right moment while he sleeps — proven
    against the REAL scheduling functions with Monday 2026-08-24 times."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from waveapp.engine.connection_monitor import (
        ENTRY_CUTOFF_MINUTES,
        ConnectionMonitor,
    )
    from waveapp.engine.session import Regime, SessionScheduler

    et = ZoneInfo("America/New_York")

    def at(hour, minute):
        return datetime(2026, 8, 24, hour, minute, tzinfo=et)

    # Sunday night → Monday pre-market: the scanner sleeps, then hunts
    sun_night = datetime(2026, 8, 23, 22, 0, tzinfo=et)
    assert SessionScheduler.regime(sun_night) is Regime.OVERNIGHT
    assert "exit-only" in ConnectionMonitor._idle_note(Regime.OVERNIGHT, sun_night)
    assert SessionScheduler.regime(at(3, 59)) is Regime.OVERNIGHT  # still asleep
    assert SessionScheduler.regime(at(4, 1)) is Regime.PRE  # scanner wakes
    # the ML labeler yields the moment the market world wakes
    assert ConnectionMonitor._labeler_allowed(Regime.OVERNIGHT)
    assert not ConnectionMonitor._labeler_allowed(Regime.PRE)
    # 09:00 — the week-ahead report fires (once)
    assert not ConnectionMonitor._report_due("week_ahead", at(8, 59), have_report=False)
    assert ConnectionMonitor._report_due("week_ahead", at(9, 1), have_report=False)
    assert not ConnectionMonitor._report_due("week_ahead", at(9, 30), have_report=True)
    # 09:30 — entries unlock (RTH clock starts); before that, no entries
    assert SessionScheduler.minutes_to_rth_close(at(9, 15)) is None  # pre-market: blocked
    assert SessionScheduler.minutes_to_rth_close(at(9, 31)) is not None  # trading
    # 15:45 — entry cutoff; 16:00+ — flat, closed
    assert SessionScheduler.minutes_to_rth_close(at(15, 50)) <= ENTRY_CUTOFF_MINUTES
    assert SessionScheduler.minutes_to_rth_close(at(16, 5)) is None
    # Monday must NOT fire the Friday week report
    assert not ConnectionMonitor._report_due("week_close", at(16, 10), have_report=False)


async def test_auto_start_announcement_waits_for_the_bridge():
    """2026-08-23 ('i only got the wave is on'): auto-start fires
    before the Telegram bridge exists, so the engine's 'Trading started.'
    push vanished — it must be queued and delivered once the bridge is up."""
    from unittest.mock import AsyncMock

    from waveapp.engine.connection_monitor import ConnectionMonitor

    monitor = ConnectionMonitor(on_status=lambda *a: None, telegram_user_id=777)
    monitor._pending_bridge_push = "▶️ Trading started (auto-start)."
    bridge = AsyncMock()
    bridge.is_running = True

    async def fake_ensure():
        # the tail of _ensure_telegram once the bridge is polling
        monitor._bridge = bridge
        await bridge.push("🌊 Wave is on (paper money). Watching the market.")
        pending = getattr(monitor, "_pending_bridge_push", None)
        if pending:
            monitor._pending_bridge_push = None
            await bridge.push(pending)

    await fake_ensure()
    texts = [c.args[0] for c in bridge.push.call_args_list]
    assert texts == [
        "🌊 Wave is on (paper money). Watching the market.",
        "▶️ Trading started (auto-start).",
    ]
    assert monitor._pending_bridge_push is None


async def test_rest_fallback_replays_missed_fills():
    """2026-08-24 (first Monday, live): the trade-update stream died silently
    overnight — BITX/ETHU entries filled at the broker but Wave never heard.
    The engine now REST-polls live actors' orders and replays missed fills;
    the delta accounting makes a late duplicate stream event harmless."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from waveapp.broker.base import OrderSide, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec, PositionState
    from waveapp.engine.core import EngineCore

    spec = PositionSpec(symbol="BITX", side=OrderSide.BUY, qty=1309, stop_price=17.29)
    filled = SimpleNamespace(
        order_id="E1",
        client_order_id="wave-x-entry-1",
        filled_qty=1309.0,
        filled_avg_price=18.08,
        status=SimpleNamespace(value="filled"),
        order_type=None,
    )
    adapter = SimpleNamespace(get_order=AsyncMock(return_value=filled))
    engine = EngineCore(adapter, database=None)
    actor = PositionActor(spec, adapter, TradingMode.PAPER)
    actor.entry_order_id = "E1"
    engine.actors["x"] = actor
    assert actor.state is PositionState.PENDING_ENTRY
    replayed = await engine.poll_missed_fills()
    assert replayed == 1
    assert actor.state is PositionState.OPEN  # the fill finally landed
    # idempotent: a second sweep (or the late stream event) changes nothing
    assert await engine.poll_missed_fills() == 0


# -- 9:30:00-sharp opening-auction queue (PMOM, 2026-08-25) -------------------


def _pm_features(symbol, price, prev_close, volume, atr_pct=4.0):
    from waveapp.engine.scanner import SymbolFeatures

    return SymbolFeatures(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        gap_pct=0.0,
        rvol=3.0,
        atr_pct=atr_pct,
        spread=0.02,
        day_volume=volume,
        avg_daily_volume=2_000_000,
    )


def test_pmom_picks_filters_and_orders_by_ramp():
    watch = {
        # ramped +5% from its 4:00 print, gap vs prior close +1% → PMOM
        "GOOD": {
            "first": 20.0,
            "last": 21.0,
            "features": _pm_features("GOOD", 21.0, 20.8, 500_000),
        },
        # stronger ramp — must sort FIRST
        "BEST": {
            "first": 20.0,
            "last": 21.6,
            "features": _pm_features("BEST", 21.6, 21.4, 500_000),
        },
        # ramp under 3% → out
        "FLAT": {
            "first": 20.0,
            "last": 20.4,
            "features": _pm_features("FLAT", 20.4, 20.2, 500_000),
        },
        # official gap ≥3% → GAPGO's territory, not PMOM's
        "GAPPY": {
            "first": 20.0,
            "last": 21.0,
            "features": _pm_features("GAPPY", 21.0, 20.0, 500_000),
        },
        # thin pre-market volume → out
        "THIN": {"first": 20.0, "last": 21.0, "features": _pm_features("THIN", 21.0, 20.8, 40_000)},
        # under the $15 floor → out
        "CHEAP": {
            "first": 10.0,
            "last": 10.6,
            "features": _pm_features("CHEAP", 10.6, 10.5, 500_000),
        },
    }
    picks = ConnectionMonitor._pmom_picks(watch, min_price=15.0)
    assert [symbol for symbol, _ in picks] == ["BEST", "GOOD"]
    assert picks[0][1] > picks[1][1]


def test_premarket_track_keeps_first_price_and_resets_daily():
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    day1 = datetime(2026, 8, 25, 5, 0, tzinfo=et)
    monitor._premarket_track(
        [SimpleNamespace(features=_pm_features("AAA", 20.0, 19.9, 50_000))], day1
    )
    monitor._premarket_track(
        [SimpleNamespace(features=_pm_features("AAA", 22.0, 19.9, 300_000))], day1
    )
    assert monitor._pm_watch["AAA"]["first"] == 20.0  # first print survives
    assert monitor._pm_watch["AAA"]["last"] == 22.0
    day2 = datetime(2026, 8, 26, 4, 30, tzinfo=et)
    monitor._premarket_track(
        [SimpleNamespace(features=_pm_features("BBB", 30.0, 29.9, 10_000))], day2
    )
    assert "AAA" not in monitor._pm_watch  # fresh watch every morning
    assert monitor._auction_done is False


def test_cycle_interval_bursts_around_the_open():
    from datetime import time

    from waveapp.engine.session import Regime

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._scan_interval = 120
    assert monitor._cycle_interval(Regime.OPEN_DRIVE, time(9, 45)) == 60
    assert monitor._cycle_interval(Regime.PRE, time(9, 5)) == 60  # fresh ramps pre-queue
    assert monitor._cycle_interval(Regime.PRE, time(7, 0)) == 120
    assert monitor._cycle_interval(Regime.MIDDAY, time(13, 0)) == 120


# -- the day list: live stocks-in-play (scanner rework, 2026-08-25) -----------


def _watch_row(price, avg_vol, pm_vol, leveraged=False, symbol="X", atr_pct=4.0):
    from dataclasses import replace

    f = replace(
        _pm_features(symbol, price, price, pm_vol),
        avg_daily_volume=avg_vol,
        leveraged=leveraged,
        atr_pct=atr_pct,
    )
    return {"first": price, "last": price, "features": f}


def test_day_list_ranks_by_premarket_rvol_with_floors():
    watch = {
        "HOT": _watch_row(30.0, 1_000_000, 900_000, symbol="HOT"),  # pm rvol 0.9
        "WARM": _watch_row(30.0, 1_000_000, 300_000, symbol="WARM"),  # 0.3
        "CHEAP": _watch_row(9.0, 1_000_000, 900_000, symbol="CHEAP"),  # under floor
        "THIN": _watch_row(30.0, 1_000_000, 50_000, symbol="THIN"),  # < 100k pm shares
        "ILLIQ": _watch_row(30.0, 300_000, 200_000, symbol="ILLIQ"),  # avg vol < 500k
    }
    picks = ConnectionMonitor._build_day_list(watch, min_price=15.0)
    assert picks == ["HOT", "WARM"]


def test_day_list_caps_leveraged_products():
    watch = {
        f"L{i}": _watch_row(30.0, 1_000_000, 900_000 - i, leveraged=True, symbol=f"L{i}")
        for i in range(8)
    }
    watch["PLAIN"] = _watch_row(30.0, 1_000_000, 200_000, symbol="PLAIN")
    picks = ConnectionMonitor._build_day_list(watch, min_price=15.0, top_n=8)
    leveraged_picked = [s for s in picks if s.startswith("L")]
    assert len(leveraged_picked) == 2  # leveraged_cap(8, 0.25)
    assert "PLAIN" in picks


def test_fallback_day_list_uses_session_rvol():
    from dataclasses import replace
    from types import SimpleNamespace

    def cand(symbol, rvol, price=30.0, avg=1_000_000):
        f = replace(_pm_features(symbol, price, price, 500_000), rvol=rvol)
        f = replace(f, avg_daily_volume=avg)
        return SimpleNamespace(features=f)

    results = [
        cand("BIG", 4.0),
        cand("MID", 2.0),
        cand("DEAD", 1.0),  # rvol < 1.5 → out
        cand("CHEAP", 5.0, price=9.0),  # under floor → out
    ]
    picks = ConnectionMonitor._fallback_day_list(results, min_price=15.0)
    assert picks == ["BIG", "MID"]


def test_pm_watch_first_prices_survive_a_restart(tmp_path, monkeypatch):
    """A 4:00–9:28 restart must not blind PMOM: first-seen pre-market prices
    persist to disk and reload in the new process (edge-case fix, 2026-08-25)."""
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    import waveapp.config as config_module

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    et = ZoneInfo("America/New_York")
    when = datetime(2026, 8, 26, 5, 0, tzinfo=et)

    first_process = ConnectionMonitor(on_status=lambda *a: None)
    first_process._premarket_track(
        [SimpleNamespace(features=_pm_features("AAA", 20.0, 19.9, 50_000))], when
    )
    assert (tmp_path / "pm_watch.json").exists()

    # the restarted process sees AAA at 24.0 — but the 20.0 first print survives
    second_process = ConnectionMonitor(on_status=lambda *a: None)
    second_process._premarket_track(
        [SimpleNamespace(features=_pm_features("AAA", 24.0, 19.9, 400_000))], when
    )
    assert second_process._pm_watch["AAA"]["first"] == 20.0
    assert second_process._pm_watch["AAA"]["last"] == 24.0

    # a NEW day ignores yesterday's seed
    next_day = datetime(2026, 8, 27, 4, 30, tzinfo=et)
    second_process._premarket_track(
        [SimpleNamespace(features=_pm_features("AAA", 30.0, 29.9, 50_000))], next_day
    )
    assert second_process._pm_watch["AAA"]["first"] == 30.0


def test_free_slots_handles_property_style_engine(monkeypatch, tmp_path):
    """Regression for the 2026-08-26 9:20 crash: EngineCore.open_actor_count is
    a @property (int) — the slot math must accept both int and callable."""
    from types import SimpleNamespace

    class FakeEngine:
        @property
        def open_actor_count(self):
            return 2

        risk = SimpleNamespace(limits=SimpleNamespace(max_positions=6))

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor.engine = FakeEngine()
    engine = monitor.engine
    open_count = getattr(engine, "open_actor_count", 0)
    free = max(
        0,
        getattr(engine.risk.limits, "max_positions", 3)
        - (open_count() if callable(open_count) else open_count),
    )
    assert free == 4


# -- DRESS REHEARSAL (2026-08-26, after the 9:20 crash): the REAL monitor
# drives the REAL EngineCore through a fake 9:21 pre-market cycle, all the
# way to a real OPG order on the broker. Shims hid the property-vs-callable
# bug; this path can never be faked again. -----------------------------------


async def test_auction_queue_end_to_end_with_real_engine(tmp_path, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    import waveapp.config as config_module
    from tests.test_engine import FakeBroker
    from waveapp.broker.base import OrderType, TimeInForce
    from waveapp.engine.core import EngineCore

    # config on disk: auto-trade ON, PMOM on, $15 floor
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
    monitor._scanner = SimpleNamespace()  # only checked for not-None

    class FakeHub:
        async def watch(self, symbols):
            return None

        latest_quotes: dict = {}

    monitor._hub = FakeHub()
    pushes = []

    async def fake_push(text, klass=None):
        pushes.append(text)

    monitor._push = fake_push

    et = ZoneInfo("America/New_York")
    when = datetime(2026, 8, 26, 9, 21, tzinfo=et)
    # the pre-market watch saw BOXX ramp 20 → 21 (+5%) on 500k shares
    monitor._premarket_track(
        [SimpleNamespace(features=_pm_features("BOXX", 20.0, 20.8, 500_000))], when
    )
    monitor._premarket_track(
        [SimpleNamespace(features=_pm_features("BOXX", 21.0, 20.8, 500_000))], when
    )
    await monitor._maybe_queue_auction_entries(when)
    import asyncio

    for _ in range(10):  # the actor submits in its own task — let it run
        await asyncio.sleep(0)

    entries = [r for r in broker.submitted if r.symbol == "BOXX"]
    assert entries, "no auction order reached the broker"
    order = entries[0]
    assert order.time_in_force is TimeInForce.OPG
    assert order.order_type is OrderType.LIMIT
    assert order.stop_loss is None  # stop follows the fill, not the bracket
    assert pushes and "PMOM queued" in pushes[0]
    assert monitor._auction_done is True


async def test_entry_pipeline_free_slots_with_real_engine(monkeypatch, tmp_path):
    """The RTH slot math against the real EngineCore property (the second
    place the 9:20 bug lived)."""

    import waveapp.config as config_module
    from tests.test_engine import FakeBroker
    from waveapp.engine.core import EngineCore

    monkeypatch.setattr(config_module, "support_dir", lambda: tmp_path)
    engine = EngineCore(FakeBroker(), database=None)
    await engine.start()
    open_count = getattr(engine, "open_actor_count", 0)
    free = max(
        0,
        getattr(engine.risk.limits, "max_positions", 3)
        - (open_count() if callable(open_count) else open_count),
    )
    assert free == engine.risk.limits.max_positions  # nothing open, all free


async def test_heartbeat_pings_configured_url(monkeypatch, tmp_path):
    """Dead-man's switch (2026-08-26): one loop pass must GET the configured
    heartbeat URL; with no URL configured it must ping nothing."""
    import asyncio
    import urllib.request

    import waveapp.config as config_module

    pinged = []
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, data=None, timeout=None: pinged.append(url)
    )
    config = config_module.AppConfig(heartbeat_url="https://hc-ping.com/fake-uuid")
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, p=None: config))
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    task = asyncio.ensure_future(monitor._heartbeat_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    assert pinged == ["https://hc-ping.com/fake-uuid"]

    pinged.clear()
    config = config_module.AppConfig(heartbeat_url="")
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, p=None: config))
    task = asyncio.ensure_future(monitor._heartbeat_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    assert pinged == []


async def test_auction_preflight_alerts_only_on_problems(monkeypatch):
    """(2026-08-26): a 9:00 ET check with enough time to fix things —
    push an alert when the auction queue would fail, stay silent when not."""
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    import waveapp.config as config_module

    et = ZoneInfo("America/New_York")
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    pushes = []

    async def fake_push(text, klass=None):
        pushes.append(text)

    monitor._push = fake_push

    # broken setup: no engine, auto-trade off, empty watch → alert
    config = config_module.AppConfig(auto_trade=False)
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, p=None: config))
    await monitor._maybe_run_auction_preflight(datetime(2026, 8, 27, 9, 1, tzinfo=et))
    assert pushes and "preflight FAILED" in pushes[0]
    assert "auto-trade is OFF" in pushes[0]
    # once per day, even if still broken
    await monitor._maybe_run_auction_preflight(datetime(2026, 8, 27, 9, 5, tzinfo=et))
    assert len(pushes) == 1

    # healthy setup on a NEW day → silence
    pushes.clear()
    healthy = config_module.AppConfig(auto_trade=True)
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, p=None: healthy))
    monitor.engine = SimpleNamespace(state="running")
    monitor._scanner = SimpleNamespace()
    monitor._adapter = SimpleNamespace(is_connected=True)
    monitor._pm_watch = {"AAA": {}}
    await monitor._maybe_run_auction_preflight(datetime(2026, 8, 28, 9, 1, tzinfo=et))
    assert pushes == []

    # outside the 9:00-9:20 window nothing fires at all
    monitor._preflight_day = None
    await monitor._maybe_run_auction_preflight(datetime(2026, 8, 29, 8, 30, tzinfo=et))
    assert monitor._preflight_day is None


async def test_auction_fallback_reenters_canceled_pmom_at_market(monkeypatch):
    """2026-08-27: Alpaca PAPER runs no opening auction — OPG orders cancel
    unfilled (CAMT/UCO/Q, caps covered the opens). The fallback re-enters at
    market right after the open; filled auction orders are left alone."""
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from waveapp.engine.actor import PositionState

    et = ZoneInfo("America/New_York")
    when = datetime(2026, 8, 27, 9, 31, tzinfo=et)
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    executed = []

    async def fake_execute(signal, avg_volume=None, auction=False):
        executed.append((signal.symbol, signal.entry_price, auction))
        return True

    pushes = []

    async def fake_push(text, klass=None):
        pushes.append(text)

    monitor._execute_signal = fake_execute
    monitor._push = fake_push
    monitor._latest_price = lambda s: {"CAMT": 150.0, "UCO": 41.96}.get(s)
    monitor._signaled = {"CAMT", "UCO"}
    monitor._pm_watch = {
        "CAMT": {
            "first": 140.0,
            "last": 150.81,
            "features": _pm_features("CAMT", 150.81, 149.0, 500_000),
        },
        "UCO": {
            "first": 40.0,
            "last": 42.11,
            "features": _pm_features("UCO", 42.11, 41.8, 500_000),
        },
    }
    monitor._auction_watchlist = {"CAMT": 2_000_000, "UCO": 2_000_000}
    canceled = SimpleNamespace(
        spec=SimpleNamespace(symbol="CAMT", strategy="PMOM"),
        state=PositionState.CLOSED,
        exit_reason="entry canceled",
    )
    filled = SimpleNamespace(
        spec=SimpleNamespace(symbol="UCO", strategy="PMOM"),
        state=PositionState.OPEN,
        exit_reason=None,
    )
    monitor.engine = SimpleNamespace(actors={"a": canceled, "b": filled})

    await monitor._maybe_auction_fallback(when)
    assert executed == [("CAMT", 150.0, False)]  # canceled → market re-entry
    assert pushes and "CAMT" in pushes[0]
    assert monitor._auction_watchlist == {"UCO": 2_000_000}  # filled → untouched

    # outside the window nothing happens
    executed.clear()
    monitor._auction_watchlist = {"CAMT": 2_000_000}
    await monitor._maybe_auction_fallback(datetime(2026, 8, 27, 9, 45, tzinfo=et))
    assert executed == []


def test_pmom_cost_gate_blocks_wide_spreads():
    """URBN (2026-08-28): the PMOM fallback bought into a 2.3% bid/ask at
    market — the whole loss was the spread toll. Both PMOM paths now pass
    the same §8.4 gate as every other entry."""
    from types import SimpleNamespace

    from waveapp.engine.tradegate import TradeGate

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._scanner = SimpleNamespace(
        gate=TradeGate(),
        expected_move_atr_fraction=0.25,
        slippage_buffer_per_share=0.01,
    )
    # URBN that morning: $80 stock, $1.84 spread → refused
    assert monitor._pmom_cost_gate_ok("URBN", 80.0, 1.84, 2.4) is False
    # a tight NVDX-style book: 2-cent spread on $20 → allowed
    assert monitor._pmom_cost_gate_ok("NVDX", 20.0, 0.02, 0.8) is True


def test_ensure_backfill_dedupes_and_survives_no_loop():
    """Day-list backfill (2026-08-28): one task per symbol, never duplicated,
    and a no-event-loop context (tests/startup) is a quiet no-op."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._chart_backfill = {"DONE": [(1, 1.0, 1.0, 1.0, 1.0)]}
    monitor._backfill_pending = {"PENDING"}
    # already-fetched and in-flight symbols are skipped; no loop → no crash
    monitor._ensure_backfill("DONE")
    monitor._ensure_backfill("PENDING")
    monitor._ensure_backfill("FRESH")  # no running loop here → no-op
    assert "FRESH" not in monitor._backfill_pending


# -- A4-1 / A4-2 (audit 2026-09-22): monitor guard-state must die with the hub


async def test_teardown_clears_backfill_guards_and_allows_rescheduling():
    """A4-1: _backfill_pending survived teardown while the BarBuilder didn't —
    after a mid-day reconnect _ensure_backfill no-op'd forever (no bar/VWAP/
    judge-peak re-seed all day). Teardown must cancel outstanding fetches and
    clear both guards so the fresh hub gets re-seeded."""
    import asyncio

    monitor = ConnectionMonitor(on_status=lambda *a: None)

    async def fake_fetch(symbol):  # in-flight forever until cancelled
        await asyncio.sleep(3600)

    monitor._fetch_chart_backfill = fake_fetch
    monitor._chart_backfill["OLD"] = [(1, 1.0, 1.0, 1.0, 1.0)]
    monitor._ensure_backfill("AAPL")
    assert "AAPL" in monitor._backfill_pending
    assert len(monitor._backfill_tasks) == 1

    await monitor._teardown()
    assert monitor._backfill_pending == set()
    assert monitor._backfill_tasks == set()
    assert monitor._chart_backfill == {}

    # the reconnect path schedules again — both the previously-fetched and
    # the previously-in-flight symbol are eligible on the new (empty) hub
    monitor._ensure_backfill("OLD")
    monitor._ensure_backfill("AAPL")
    assert monitor._backfill_pending == {"OLD", "AAPL"}
    for task in monitor._backfill_tasks:
        task.cancel()
    await asyncio.gather(*monitor._backfill_tasks, return_exceptions=True)


async def test_fetch_backfill_always_releases_the_pending_guard(monkeypatch):
    """A4-1: the fetch's finally must discard the symbol from
    _backfill_pending on every path — early return (no keys) and failure."""
    from waveapp.security import secrets as _secrets

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    # early-return path: no keys in the keychain
    monkeypatch.setattr(_secrets, "get_secret", lambda name: None)
    monitor._backfill_pending.add("MSFT")
    await monitor._fetch_chart_backfill("MSFT")
    assert "MSFT" not in monitor._backfill_pending

    # failure path: keychain blows up → logged, refetch-loop guard set,
    # pending still released
    def _boom(name):
        raise OSError("keychain unavailable")

    monkeypatch.setattr(_secrets, "get_secret", _boom)
    monitor._backfill_pending.add("TSLA")
    await monitor._fetch_chart_backfill("TSLA")
    assert "TSLA" not in monitor._backfill_pending
    assert monitor._chart_backfill["TSLA"] == []


async def test_teardown_resets_watch_guards_and_day_list_rewatches():
    """A4-2: _s2_watched/_climber_watched/day-list short-circuit survived
    teardown while the hub's subscriptions died with it → entry starvation.
    Simulating a full reconnect is too heavy here, so this asserts the
    cleared guard state directly, then drives _ensure_day_list one pass to
    prove the CACHED list is re-watched on the fresh hub (not rebuilt)."""
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    now_et = datetime(2026, 9, 22, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    today = now_et.date().isoformat()
    monitor._s2_watched = {"AAA"}
    monitor._s2_watched_day = today
    monitor._climber_watched = {"BBB"}
    monitor._day_list_day = today
    monitor._day_list = ["CCC", "DDD"]

    await monitor._teardown()
    assert monitor._s2_watched == set()
    assert monitor._climber_watched == set()
    assert monitor._day_list == ["CCC", "DDD"]  # cached list survives
    assert monitor._day_list_day == today  # NOT forced into a rebuild
    assert monitor._day_list_rewatch is True

    # next pass (scanner2 menu empty → fallback path): the same-day
    # short-circuit now re-issues hub.watch + backfill for the cached list
    hub = SimpleNamespace(watch=AsyncMock())
    monitor._hub = hub
    scheduled = []
    monitor._ensure_backfill = scheduled.append
    result = await monitor._ensure_day_list([], now_et, 15.0)
    assert result == ["CCC", "DDD"]
    hub.watch.assert_awaited_once_with(["CCC", "DDD"])
    assert scheduled == ["CCC", "DDD"]

    # once re-watched, the short-circuit is quiet again
    result = await monitor._ensure_day_list([], now_et, 15.0)
    assert result == ["CCC", "DDD"]
    assert hub.watch.await_count == 1


# -- the minute-tick entry clock (2026-09-04: "it needs to be faster") --


async def test_minute_tick_runs_pipeline_on_fresh_scan(monkeypatch):
    from datetime import UTC, datetime

    monitor = ConnectionMonitor(lambda *a, **k: None)
    calls = []

    async def fake_pipeline(results):
        calls.append(results)

    monkeypatch.setattr(monitor, "_entry_pipeline", fake_pipeline)
    monitor._last_scan_results = ["candidate"]
    monitor._last_scan_monotonic = datetime.now(UTC).timestamp()
    monitor._schedule_entry_tick(datetime.now(UTC))
    await monitor._entry_tick_task
    assert calls == [["candidate"]]


async def test_minute_tick_refuses_stale_scan_and_reentry(monkeypatch):
    import asyncio as aio
    from datetime import UTC, datetime

    monitor = ConnectionMonitor(lambda *a, **k: None)
    calls = []

    async def slow_pipeline(results):
        calls.append(1)
        await aio.sleep(0.05)

    monkeypatch.setattr(monitor, "_entry_pipeline", slow_pipeline)
    # stale scan → refused outright
    monitor._last_scan_results = ["c"]
    monitor._last_scan_monotonic = datetime.now(UTC).timestamp() - 400
    monitor._schedule_entry_tick(datetime.now(UTC))
    assert getattr(monitor, "_entry_tick_task", None) is None
    # fresh scan → one tick; a second schedule while in flight is refused
    monitor._last_scan_monotonic = datetime.now(UTC).timestamp()
    monitor._schedule_entry_tick(datetime.now(UTC))
    first = monitor._entry_tick_task
    monitor._schedule_entry_tick(datetime.now(UTC))
    assert monitor._entry_tick_task is first  # no second task while running
    await first
    assert calls == [1]


class TestChurnGuard:
    """2026-09-09 (TYRA x4): scanner may not re-buy a just-stopped symbol."""

    def _monitor(self):
        from waveapp.engine.connection_monitor import ConnectionMonitor

        return ConnectionMonitor(on_status=lambda *a: None)

    async def test_cooldown_blocks_scanner_reentry(self, monkeypatch):
        import time as _time

        from waveapp.broker.base import OrderSide
        from waveapp.engine.strategies import EntrySignal

        m = self._monitor()
        m._symbol_stop_hit_at["TYRA"] = _time.time() - 60  # stopped 1 min ago
        sig = EntrySignal(
            symbol="TYRA",
            side=OrderSide.BUY,
            confidence=0.6,
            reason="t",
            strategy="ORB",
            entry_price=22.0,
            stop_price=21.5,
        )
        assert await m._execute_signal(sig) is False  # blocked before sizing

    async def test_daily_cap_blocks_fourth_entry(self):
        from waveapp.broker.base import OrderSide
        from waveapp.engine.strategies import EntrySignal

        m = self._monitor()
        m._symbol_entries["TYRA"] = 3
        sig = EntrySignal(
            symbol="TYRA",
            side=OrderSide.BUY,
            confidence=0.6,
            reason="t",
            strategy="ORB",
            entry_price=22.0,
            stop_price=21.5,
        )
        assert await m._execute_signal(sig) is False

    async def test_mk_rebuys_are_exempt(self):
        import time as _time

        from waveapp.broker.base import OrderSide
        from waveapp.engine.strategies import EntrySignal

        m = self._monitor()
        m._symbol_stop_hit_at["TYRA"] = _time.time() - 60
        sig = EntrySignal(
            symbol="TYRA",
            side=OrderSide.BUY,
            confidence=0.6,
            reason="t",
            strategy="MKRB",
            entry_price=22.0,
            stop_price=21.5,
        )
        # exempt from the guard — falls through to sizing, which fails on the
        # missing adapter (None), proving the guard did NOT block it
        result = await m._execute_signal(sig)
        assert result is False  # fails later (no adapter), not at the guard


class TestInverseEtfBan:
    """the inverse-ETF ban (2026-09-14): the 9/4 poison — no long entries on
    inverse/bear ETFs. 13-day referee: +$827, trend days untouched."""

    def _monitor_with_asset(self, name):
        from types import SimpleNamespace

        from waveapp.engine.connection_monitor import ConnectionMonitor

        m = ConnectionMonitor(on_status=lambda *a: None)

        class FakeAdapter:
            async def get_asset(self, mode, symbol):
                return SimpleNamespace(name=name)

        m._adapter = FakeAdapter()
        return m

    async def test_bear_etf_long_is_refused(self):
        from waveapp.broker.base import OrderSide
        from waveapp.engine.strategies import EntrySignal

        m = self._monitor_with_asset("MicroSectors Gold -3X Inverse Leveraged ETN")
        sig = EntrySignal(
            symbol="GDXD",
            side=OrderSide.BUY,
            confidence=0.6,
            reason="t",
            strategy="ORB",
            entry_price=10.0,
            stop_price=9.5,
        )
        assert await m._execute_signal(sig) is False

    async def test_ordinary_stock_passes_the_ban(self):
        from waveapp.broker.base import OrderSide
        from waveapp.engine.strategies import EntrySignal

        m = self._monitor_with_asset("Rubrik, Inc. Class A Common Stock")
        sig = EntrySignal(
            symbol="RBRK",
            side=OrderSide.BUY,
            confidence=0.6,
            reason="t",
            strategy="ORB",
            entry_price=98.0,
            stop_price=95.0,
        )
        # passes the ban, then fails later at sizing (no account) — proving
        # the refusal above came from the ban, not the missing adapter parts
        assert await m._execute_signal(sig) is False
        assert m._inverse_cache.get("RBRK") is False

    async def test_detector_caches(self):
        m = self._monitor_with_asset("ProShares UltraShort QQQ")
        assert await m._is_inverse_etf("QID") is True
        assert m._inverse_cache["QID"] is True


# -- A3-2 (audit 2026-09-22): the day-mark burns only on a real open ----------


def _entry_pipeline_monitor(monkeypatch, tmp_path, auto_trade=True):
    """One hot GAP candidate with warm bars and a tight live quote through
    the real Scanner/RiskEngine/TradeGate (the test_strategies shim, local),
    so _entry_pipeline produces a signal and reaches _execute_signal."""
    from datetime import datetime
    from types import SimpleNamespace

    from waveapp.config import AppConfig
    from waveapp.data.hub import DataHub
    from waveapp.engine.risk import RiskEngine
    from waveapp.engine.scanner import Candidate, Scanner, SymbolFeatures
    from waveapp.engine.session import ET, Regime, SessionInfo
    from waveapp.engine.tradegate import TradeGate

    config_path = tmp_path / "config.toml"
    AppConfig(auto_trade=auto_trade).save(config_path)
    monkeypatch.setattr("waveapp.config.config_path", lambda: config_path)
    monkeypatch.setattr(
        "waveapp.engine.session.SessionScheduler.info",
        staticmethod(
            lambda now=None: SessionInfo(
                regime=Regime.OPEN_DRIVE,
                is_lull=False,
                et_time=datetime(2026, 8, 14, 9, 46, tzinfo=ET),
            )
        ),
    )
    monitor = ConnectionMonitor(lambda *a: None)
    monitor.engine = SimpleNamespace(
        risk=RiskEngine(),
        open_position=AsyncMock(return_value=SimpleNamespace(position_key="k")),
        state=SimpleNamespace(value="running"),
    )
    monitor._adapter = SimpleNamespace(
        get_account=AsyncMock(return_value=SimpleNamespace(equity=100_000.0, cash=100_000.0))
    )
    monitor._hub = DataHub(["SPY"])
    monitor._scanner = Scanner(provider=None, gate=TradeGate(), database=None)
    monitor._push = AsyncMock()
    features = SymbolFeatures(
        symbol="HOT",
        price=50.0,
        prev_close=50.0,
        gap_pct=4.5,
        rvol=4.0,
        atr_pct=2.0,
        spread=0.02,
        day_volume=2_000_000,
        avg_daily_volume=2_000_000,
        shortable=True,
    )
    for px, vol, minute in ((51.5, 3000, 44), (52.0, 3000, 45), (52.0, 10, 46)):
        monitor._hub.bar_builder.on_trade(
            "HOT", px, vol, datetime(2026, 8, 14, 9, minute, tzinfo=ET)
        )
    # A4-7: the deferral now requires a tz-aware timestamp within 15s —
    # a warm fixture quote must carry one
    from datetime import UTC as _UTC

    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=51.99, ask_price=52.01, timestamp=datetime.now(_UTC)
    )
    candidate = Candidate(
        features=features,
        score=6.0,
        strategy_scores={"GAP": 6.0},
        best_strategy="GAP",
        accepted=True,
    )
    return monitor, [candidate]


async def test_rejected_signal_refires_next_cycle_opened_burns(monkeypatch, tmp_path):
    """A3-2 (audit 2026-09-22): the one-trade-per-symbol-per-day mark used to
    burn BEFORE _execute_signal, so a transient refusal (risk ceiling
    momentarily full, pause, freeze, judge veto, zero size) blackballed the
    symbol for the whole session and starved the risk-governed book. A
    rejected signal must re-fire next cycle; an OPENED one stays burned."""
    monitor, candidates = _entry_pipeline_monitor(monkeypatch, tmp_path)
    calls = []
    fate = {"opened": False}

    async def fake_execute(signal, avg_volume=None, auction=False, max_qty=None):
        calls.append(signal.symbol)
        return fate["opened"]

    monitor._execute_signal = fake_execute

    # cycle 1: the engine refuses ("total open risk ceiling reached")
    await monitor._entry_pipeline(candidates)
    assert calls == ["HOT"]
    assert "HOT" not in monitor._signaled  # NOT blackballed for the day
    assert "HOT" not in monitor._signal_inflight  # in-flight mark cleaned up

    # cycle 2: the ceiling freed up — the same signal re-fires and opens
    fate["opened"] = True
    await monitor._entry_pipeline(candidates)
    assert calls == ["HOT", "HOT"]
    assert "HOT" in monitor._signaled  # burned on the real open

    # cycle 3: one trade per symbol per day holds
    await monitor._entry_pipeline(candidates)
    assert calls == ["HOT", "HOT"]


async def test_auto_trade_off_push_still_burns_the_day_mark(monkeypatch, tmp_path):
    """The auto-trade-off notification is the one legitimate pre-execution
    burn — without it the signal push would repeat every cycle."""
    monitor, candidates = _entry_pipeline_monitor(monkeypatch, tmp_path, auto_trade=False)
    await monitor._entry_pipeline(candidates)
    assert "HOT" in monitor._signaled
    monitor._push.assert_awaited_once()
    await monitor._entry_pipeline(candidates)
    monitor._push.assert_awaited_once()  # no repeat push on the next cycle


async def test_inflight_symbol_is_not_double_signaled(monkeypatch, tmp_path):
    """The check-then-add span now crosses the _execute_signal await — an
    overlapping pipeline pass (slow-path loop vs minute-tick task) must skip
    a symbol whose entry is still in flight."""
    import asyncio

    monitor, candidates = _entry_pipeline_monitor(monkeypatch, tmp_path)
    release = asyncio.Event()
    calls = []

    async def fake_execute(signal, avg_volume=None, auction=False, max_qty=None):
        calls.append(signal.symbol)
        await release.wait()
        return True

    monitor._execute_signal = fake_execute
    first = asyncio.ensure_future(monitor._entry_pipeline(candidates))
    for _ in range(5):  # let the first pass park inside _execute_signal
        await asyncio.sleep(0)
    assert calls == ["HOT"] and "HOT" in monitor._signal_inflight
    await monitor._entry_pipeline(candidates)  # overlapping pass
    release.set()
    await first
    assert calls == ["HOT"]  # the overlap never reached _execute_signal
    assert "HOT" in monitor._signaled and "HOT" not in monitor._signal_inflight


# -- A5-2 (audit 2026-09-22): telegram MINIMUM mode vs TRANSLATED lines -------
# _push receives lines AFTER core.py's telegram_trade_line translation; the
# old markers matched the RAW log strings, so in minimum mode every fill,
# close, halt, day summary and error silently dropped (no pushes arrived).
# These tests feed _push the REAL translated producer strings, copied
# verbatim from core.py / error_alerts.py / connection_monitor.py.


class _PushRecorderBridge:
    is_running = True

    def __init__(self):
        self.sent: list[str] = []

    async def push(self, text: str) -> None:
        self.sent.append(text)


def _mode_monitor(monkeypatch, mode: str) -> ConnectionMonitor:
    import waveapp.config as config_module

    config = config_module.AppConfig(telegram_mode=mode)
    monkeypatch.setattr(config_module.AppConfig, "load", classmethod(lambda cls, path=None: config))
    monitor = ConnectionMonitor(lambda *a: None)
    monitor._bridge = _PushRecorderBridge()
    return monitor


# the MINIMUM contract: fills, banks, closes, halts, risk, day summary —
# exactly as they leave core.py's telegram_trade_line / _notify today
_MINIMUM_CONTRACT_LINES = [
    "✅ Bought 12 shares of NVDA at $181.25. Protected at $179.10. Slippage: none.",
    "🟢 NVDA sold: WON 12.00 $ (sold at $182.10).",
    "🔴 NVDA sold: LOST 8.40 $ (safety stop did its job).",
    # S4 short book: the mirror lines must survive minimum mode too
    "🔻 Shorted 15 shares of PLTR at $30.10. Protected at $31.20. Slippage: none.",
    "🟢 PLTR covered: WON 7.50 $ (covered at $29.60).",
    "🔴 PLTR covered: LOST 16.50 $ (safety stop did its job).",
    "💰 NVDA — banked part (6 shares at $182.00). The rest keeps riding.",
    "⏸️ GRML is frozen by the exchange (too fast a move). "
    "Wave holds and waits — the safety stop stays at the broker.",
    "▶️ GRML unfrozen — managing again.",
    "🌙 Market closed. Today: +12.00 $ across 3 trades (2 won).",
    "⚠️ Numbers don't match the broker (2 issue(s)). New buys blocked until it's safe.",
    "🛑 KILL SWITCH. Selling everything now (2 positions), "
    "canceled 3 orders. Trading is halted until you re-arm.",
]

# routine chatter that must stay OUT of minimum
_MINIMUM_CHATTER_LINES = [
    "🕐 Wave hourly: NVDA — engine running, watching.",
    "🔼 NVDA safety raised to $181.90.",  # routine stop ratchet
    "🔽 PLTR safety tightened to $30.80.",  # S4: the short ratchet is chatter too
]


async def test_minimum_mode_passes_translated_contract_lines(monkeypatch):
    monitor = _mode_monitor(monkeypatch, "minimum")
    for line in _MINIMUM_CONTRACT_LINES:
        await monitor._push(line)
    assert monitor._bridge.sent == _MINIMUM_CONTRACT_LINES
    for line in _MINIMUM_CHATTER_LINES:
        await monitor._push(line)
    assert monitor._bridge.sent == _MINIMUM_CONTRACT_LINES  # chatter dropped


async def test_minimum_mode_errors_always_page(monkeypatch):
    monitor = _mode_monitor(monkeypatch, "minimum")
    # error_alerts.py fan-out arrives classified…
    await monitor._push("⚠️ ERROR — wave.engine.core\nsomething broke", klass="error")
    # …but even an unclassified error-shaped line must page (belt-and-braces)
    await monitor._push("⚠️ ERROR — wave.data.hub\nfeed died")
    await monitor._push("⚠️ CONNECTION NOT HEALING — 12 reconnect errors in 340s and still failing")
    await monitor._push(
        "🚨 9:00 preflight FAILED — Wave is not ready for the open:\n• scanner is not running",
        klass="error",
    )
    assert len(monitor._bridge.sent) == 4


async def test_minimum_mode_klass_classification(monkeypatch):
    monitor = _mode_monitor(monkeypatch, "minimum")
    # classified contract classes pass…
    await monitor._push(
        "🌙 Market closed. Today: +1.00 $ across 1 trades (1 won).", klass="summary"
    )
    # …classified chatter drops even though nothing marker-matches either way
    await monitor._push("🔔 Market is open — Wave is hunting.", klass="status")
    await monitor._push(
        "📡 GAP signal (BUY) HOT @ 52.00 (auto-trade OFF — signal only)", klass="signal"
    )
    await monitor._push("📊 Weekly report: flat week.", klass="report")
    # a classified klass must NOT fall through to marker matching:
    # "status" chatter that happens to contain a marker word stays dropped
    await monitor._push("↩️ HOT: auction order canceled by the paper venue — filled", klass="status")
    assert monitor._bridge.sent == ["🌙 Market closed. Today: +1.00 $ across 1 trades (1 won)."]


async def test_off_mode_drops_every_push_even_errors(monkeypatch):
    monitor = _mode_monitor(monkeypatch, "off")
    for line in _MINIMUM_CONTRACT_LINES:
        await monitor._push(line)
    await monitor._push("⚠️ ERROR — wave.engine.core\nboom", klass="error")
    await monitor._push("🚨 9:00 preflight FAILED", klass="error")
    assert monitor._bridge.sent == []  # OFF = silence; commands still answer


async def test_on_mode_passes_everything(monkeypatch):
    monitor = _mode_monitor(monkeypatch, "on")
    lines = [*_MINIMUM_CONTRACT_LINES, *_MINIMUM_CHATTER_LINES]
    for line in lines:
        await monitor._push(line)
    await monitor._push("🔔 Market is open — Wave is hunting.", klass="status")
    assert monitor._bridge.sent == [*lines, "🔔 Market is open — Wave is hunting."]


# -- A5-3 / A5-4 (audit 2026-09-22): monitor lifecycle robustness -------------


def _fake_bridge_class(created, start_delay=0.05, stop_delay=0.0):
    """Instrumented TelegramBridge stand-in — start() is deliberately several
    awaits long, reproducing the window the A5-3 race lived in."""
    import asyncio as _asyncio

    class FakeBridge:
        def __init__(self, **kwargs):
            created.append(self)
            self.is_running = False
            self.pushes = []

        @staticmethod
        def token_from_keychain():
            return "fake-token"

        async def start(self):
            await _asyncio.sleep(start_delay)
            self.is_running = True

        async def stop(self):
            await _asyncio.sleep(stop_delay)
            self.is_running = False

        async def push(self, text):
            self.pushes.append(text)

    return FakeBridge


async def test_concurrent_ensure_telegram_builds_exactly_one_bridge(monkeypatch):
    """A5-3: two overlapping _ensure_telegram calls used to BOTH pass the
    `_bridge is None` guard during the slow start and leave two pollers
    (Telegram Conflict churn, split ConfirmationGate). The lock must leave
    exactly one bridge."""
    import asyncio

    created = []
    monkeypatch.setattr("waveapp.telegram.bridge.TelegramBridge", _fake_bridge_class(created))
    monitor = ConnectionMonitor(lambda *a: None, telegram_user_id=777)
    await asyncio.gather(monitor._ensure_telegram(), monitor._ensure_telegram())
    assert len(created) == 1
    assert monitor._bridge is created[0]
    assert monitor._bridge.is_running


async def test_apply_user_id_vs_run_loop_leaves_one_polling_bridge(monkeypatch):
    """A5-3, the reported shape: Settings restarts the bridge (slow stop →
    slow start) while the run loop's own _ensure_telegram fires mid-window.
    Exactly one bridge may poll afterwards, and the old one must be stopped."""
    import asyncio

    created = []
    monkeypatch.setattr(
        "waveapp.telegram.bridge.TelegramBridge",
        _fake_bridge_class(created, start_delay=0.05, stop_delay=0.02),
    )
    monitor = ConnectionMonitor(lambda *a: None, telegram_user_id=777)
    await monitor._ensure_telegram()  # bridge #1 polling
    assert len(created) == 1
    await asyncio.gather(
        monitor.apply_telegram_user_id(888),  # stop #1 → start #2 (locked)
        monitor._ensure_telegram(),  # must NOT build a rival mid-restart
    )
    running = [b for b in created if b.is_running]
    assert len(running) == 1, f"{len(created)} bridges created, {len(running)} running"
    assert monitor._bridge is running[0]
    assert created[0].is_running is False  # the old poller really stopped


async def test_run_loop_survives_poll_exception(monkeypatch):
    """A5-4: an exception in the cycle body (EngineCore ctor, corrupt-config
    DataHub build, …) used to end run() SILENTLY — dots frozen green, no
    liveness polls, no bridge resurrection. The loop must log, sleep and run
    the next cycle; healthy cycles must resume once the fault clears."""
    import asyncio
    import contextlib

    import waveapp.engine.connection_monitor as cm

    monkeypatch.setattr(cm, "RETRY_SECONDS", 0.01)
    monitor = ConnectionMonitor(lambda *a: None)
    monitor._adapter = object()  # take the _poll branch of the cycle

    async def _noop():
        pass

    # pre-fill the sidecar-task slots so run() spawns none of them here
    for attr in (
        "_polygon_task",
        "_ml_stats_task",
        "_ml_task",
        "_nightly_ml_task",
        "_reports_task",
        "_heartbeat_task",
    ):
        setattr(monitor, attr, asyncio.ensure_future(_noop()))
    await asyncio.sleep(0)  # let the no-op tasks finish
    monkeypatch.setattr(monitor, "_install_error_alerts", lambda: None)
    monkeypatch.setattr(monitor, "_poll_interval", lambda: 0.01)
    monkeypatch.setattr(monitor, "_report_db_health", lambda: None)

    calls = {"poll": 0, "telegram": 0}

    async def poll():
        calls["poll"] += 1
        if calls["poll"] <= 3:
            raise RuntimeError("corrupt config")  # the A5-4 killer, thrice

    async def ensure_telegram():
        calls["telegram"] += 1

    monkeypatch.setattr(monitor, "_poll", poll)
    monkeypatch.setattr(monitor, "_ensure_telegram", ensure_telegram)

    task = asyncio.ensure_future(monitor.run())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if calls["telegram"] >= 2:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert calls["poll"] > 3, "loop died on the raising cycles"
    assert calls["telegram"] >= 2, "healthy cycles never resumed"
    # the rate-limit bookkeeping reset once a cycle went clean
    assert monitor._cycle_error_count == 0
    assert monitor._last_cycle_error == ""


def test_cycle_error_rate_limit_counts_identical_and_resets_on_change():
    """A5-4: identical consecutive cycle errors collapse into a counter (no
    log spam); a DIFFERENT error restarts the count with a full traceback."""
    monitor = ConnectionMonitor(lambda *a: None)
    for _ in range(6):
        monitor._log_cycle_error(RuntimeError("same fault"))
    assert monitor._cycle_error_count == 6
    assert monitor._last_cycle_error == "RuntimeError: same fault"
    monitor._log_cycle_error(ValueError("new fault"))
    assert monitor._cycle_error_count == 1
    assert monitor._last_cycle_error == "ValueError: new fault"


# -- A4-7 (audit 2026-09-22): one shared quote-age gate ------------------------
# latest_quotes entries are never invalidated — a halted/dropped symbol serves
# its last quote indefinitely. The 15s age check used to live ONLY in the
# pipeline deferral, and even there contextlib.suppress(TypeError) let a NAIVE
# timestamp pass as "fresh". _fresh_quote now owns the check for every
# decision-pricing consumer, and naive/missing timestamps are STALE.


def _quote_monitor():
    from waveapp.data.hub import DataHub

    monitor = ConnectionMonitor(lambda *a: None)
    monitor._hub = DataHub(["SPY"])
    return monitor


def test_fresh_quote_naive_timestamp_is_stale():
    """The old suppress(TypeError) inverted: a tz-NAIVE timestamp used to
    pass as fresh (the subtraction raised, _fresh stayed True). Now it is
    STALE — a quote that cannot prove its age prices nothing."""
    from datetime import datetime
    from types import SimpleNamespace

    monitor = _quote_monitor()
    monitor._hub.latest_quotes["CUE"] = SimpleNamespace(
        bid_price=30.90,
        ask_price=30.95,
        timestamp=datetime.now(),  # noqa: DTZ005 — naive on purpose
    )
    assert monitor._fresh_quote("CUE") is None


def test_fresh_quote_missing_or_untyped_timestamp_is_stale():
    from types import SimpleNamespace

    monitor = _quote_monitor()
    monitor._hub.latest_quotes["NOTS"] = SimpleNamespace(bid_price=10.0, ask_price=10.02)
    assert monitor._fresh_quote("NOTS") is None  # no timestamp at all
    monitor._hub.latest_quotes["STRT"] = SimpleNamespace(
        bid_price=10.0, ask_price=10.02, timestamp="2026-09-22T14:00:00Z"
    )
    assert monitor._fresh_quote("STRT") is None  # untyped (string) timestamp
    assert monitor._fresh_quote("GONE") is None  # never quoted
    monitor._hub = None
    assert monitor._fresh_quote("NOTS") is None  # no hub


def test_fresh_quote_age_boundary():
    """20s-old is stale, 5s-old passes; max_age_s is a real parameter."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    monitor = _quote_monitor()
    now = datetime.now(UTC)
    monitor._hub.latest_quotes["OLD"] = SimpleNamespace(
        bid_price=20.0, ask_price=20.02, timestamp=now - timedelta(seconds=20)
    )
    monitor._hub.latest_quotes["WARM"] = SimpleNamespace(
        bid_price=20.0, ask_price=20.02, timestamp=now - timedelta(seconds=5)
    )
    assert monitor._fresh_quote("OLD") is None
    q = monitor._fresh_quote("WARM")
    assert q is not None and q.bid_price == 20.0
    # widened threshold flows through the parameter (the helper carries the
    # knob so a caller with a documented wider window can thread it in)
    assert monitor._fresh_quote("OLD", max_age_s=120.0) is not None


async def test_pipeline_defers_on_naive_timestamp_quote(monkeypatch, tmp_path):
    """End-to-end inversion proof: the deferral used to wave a naive-stamped
    quote through (suppress(TypeError) kept _fresh=True). Now it defers —
    and defers WITHOUT burning the one-signal-per-day mark."""
    from datetime import datetime
    from types import SimpleNamespace

    monitor, candidates = _entry_pipeline_monitor(monkeypatch, tmp_path)
    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=51.99,
        ask_price=52.01,
        timestamp=datetime.now(),  # noqa: DTZ005 — naive on purpose
    )
    calls = []

    async def fake_execute(signal, avg_volume=None, auction=False, max_qty=None):
        calls.append(signal.symbol)
        return True

    monitor._execute_signal = fake_execute
    await monitor._entry_pipeline(candidates)
    assert calls == []  # deferred before execution
    assert "HOT" not in monitor._signaled  # not blackballed — re-fires later


async def test_execute_signal_stale_quote_never_reaches_the_judge(monkeypatch, tmp_path):
    """A stale quote's DEAD bid must not be compared to the decision price
    (the door judge would wave a rebuy into a halt-resume), and the ladder
    must not post at a phantom mid — it takes the existing no-quote branch
    (MARKET entry for non-climbers)."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from waveapp.broker.base import OrderSide, OrderType
    from waveapp.engine.strategies import EntrySignal

    monitor, _candidates = _entry_pipeline_monitor(monkeypatch, tmp_path)
    # the frozen book: a plausible two-sided quote, 20 minutes dead
    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=51.99,
        ask_price=52.01,
        timestamp=datetime.now(UTC) - timedelta(minutes=20),
    )
    judged = []

    def fake_judge(is_buy, decision_px, bid, ask, symbol=""):
        judged.append((is_buy, decision_px, bid, ask, symbol))
        return True, ""

    monkeypatch.setattr("waveapp.engine.position_judge.judge_entry", fake_judge)
    sig = EntrySignal(
        symbol="HOT",
        side=OrderSide.BUY,
        confidence=0.8,
        reason="t",
        strategy="GAP",
        entry_price=52.0,
        stop_price=51.5,
    )
    assert await monitor._execute_signal(sig) is True
    # the judge saw ZEROS, never the dead 51.99 bid — it declines to judge
    # and the pipeline's no-fresh-quote deferral upstream owns the delay
    assert judged == [(True, 52.0, 0.0, 0.0, "HOT")]
    spec = monitor.engine.open_position.await_args.args[0]
    # ladder fell back to the no-quote behavior: MARKET, no phantom mid
    assert spec.entry_type is OrderType.MARKET
    assert spec.limit_price is None
    assert spec.ladder is False


async def test_mk_rebuy_defers_on_stale_quote_and_logs_once(monkeypatch, tmp_path, caplog):
    """_mk_rebuy reaches _execute_signal without ever passing the pipeline
    deferral — stale quote must defer the rebuy THIS tick (caller rolls the
    brain ledger back and retries), logging once per staleness episode."""
    import logging
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    monitor, _candidates = _entry_pipeline_monitor(monkeypatch, tmp_path)
    calls = []

    async def fake_execute(signal, avg_volume=None, auction=False, max_qty=None):
        calls.append(signal.symbol)
        return True

    monitor._execute_signal = fake_execute
    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=51.99,
        ask_price=52.01,
        timestamp=datetime.now(UTC) - timedelta(minutes=20),
    )
    with caplog.at_level(logging.WARNING, logger="wave.engine.connections"):
        assert await monitor._mk_rebuy("HOT", 52.0, 51.5, "rebuyA") is False
        assert await monitor._mk_rebuy("HOT", 52.0, 51.5, "rebuyA") is False
    assert calls == []  # never reached the entry path on a dead book
    deferrals = [r for r in caplog.records if "MK rebuy HOT deferred" in r.getMessage()]
    assert len(deferrals) == 1  # once per staleness episode, not every tick

    # the stream warms — the rebuy goes through and the log latch re-arms
    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=51.99, ask_price=52.01, timestamp=datetime.now(UTC)
    )
    assert await monitor._mk_rebuy("HOT", 52.0, 51.5, "rebuyA") is True
    assert calls == ["HOT"]
    assert "HOT" not in monitor._mk_rebuy_stale_logged


# -- Lane M final pass (audit 2026-09-22): A4-8, A4-11, A5-6, A5-8, A5-10, A4-12


async def test_open_flip_freezes_channels_stale_since_before_open(caplog):
    """A4-8: the staleness freeze was edge-triggered — a channel that went
    stale BEFORE the open ("feed quiet … normal") never froze entries after
    the market flipped open. The closed→open transition in _poll must
    re-evaluate watchdog state and apply the freeze for still-stale channels."""
    import logging

    monitor = ConnectionMonitor(on_status=lambda *a: None)

    class FakeWatchdog:
        def check(self):
            return {"trades": True, "quotes": False, "bars": True}

    class FakeHub:
        watchdog = FakeWatchdog()
        is_running = False

    monitor._hub = FakeHub()

    frozen: list[str] = []

    class FakeRisk:
        def freeze_entries(self, reason):
            frozen.append(reason)

        def unfreeze_entries(self, reason):
            raise AssertionError("nothing should unfreeze here")

    class FakeEngine:
        risk = FakeRisk()

        async def poll_missed_fills(self):
            return 0

    monitor.engine = FakeEngine()

    class FakeClock:
        is_open = True

    class FakeAdapter:
        async def get_clock(self, mode):
            return FakeClock()

    monitor._adapter = FakeAdapter()
    monitor._market_open = False
    monitor._market_open_known = True  # a real closed→open FLIP, not first poll

    pushed: list[str] = []

    async def fake_push(text, klass=None):
        pushed.append(text)

    monitor._push = fake_push

    with caplog.at_level(logging.WARNING, logger="wave.engine.connections"):
        await monitor._poll()

    # trades was stale before the bell → frozen at the flip; quotes (fresh)
    # and bars (informational channel) trigger nothing
    assert frozen == ["stale market data"]
    assert any("ALREADY stale" in r.getMessage() for r in caplog.records)
    # cleanup: let the spawned open push settle
    import asyncio

    for task in list(monitor._oneshot_tasks):
        await asyncio.gather(task, return_exceptions=True)


async def test_backfill_judge_seed_failure_keeps_fetched_bars(monkeypatch):
    """A4-11: a throwing judge-seeding tail used to hit the shared except-arm
    and reset _chart_backfill[symbol] = [] — destroying successfully-fetched
    bars AND blocking any refetch. The tail now has its own per-actor guard
    and never touches the fetched bars."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import alpaca.data.historical as _hist

    from waveapp.config import AppConfig
    from waveapp.security import secrets as _secrets

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monkeypatch.setattr(_secrets, "get_secret", lambda name: "k")
    monkeypatch.setattr(
        AppConfig, "load", classmethod(lambda cls, path=None: SimpleNamespace(data_feed="iex"))
    )

    bar = SimpleNamespace(
        timestamp=datetime(2026, 9, 22, 14, 0, tzinfo=UTC),
        open=10.0,
        high=11.0,
        low=9.5,
        close=10.5,
        volume=1000,
        vwap=10.4,
    )

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_stock_bars(self, request):
            return SimpleNamespace(data={"XYZ": [bar]})

    monkeypatch.setattr(_hist, "StockHistoricalDataClient", FakeClient)

    bad = SimpleNamespace(  # comparison b.timestamp >= object() → TypeError
        spec=SimpleNamespace(symbol="XYZ"), adopted_entry_time=object()
    )
    good = SimpleNamespace(
        spec=SimpleNamespace(symbol="XYZ"),
        adopted_entry_time=datetime(2026, 9, 22, 13, 0, tzinfo=UTC),
    )

    class FakeEngine:
        actors = {"bad": bad, "good": good}

    monitor.engine = FakeEngine()
    monitor._backfill_pending.add("XYZ")

    await monitor._fetch_chart_backfill("XYZ")

    # the fetched bars SURVIVE the throwing tail (no [] reset)…
    assert monitor._chart_backfill["XYZ"] == [(bar.timestamp, 10.0, 11.0, 9.5, 10.5)]
    # …the per-actor guard let the healthy actor still get its seed…
    assert good.judge_seed_peak == 11.0
    assert good.judge_seed_peak_vol == 1000.0
    # …and the A4-1 pending guard released as always
    assert "XYZ" not in monitor._backfill_pending


async def test_spawn_logs_oneshot_exception(caplog):
    """A5-6: a fire-and-forget task that raises must not vanish silently —
    _spawn's done-callback logs the exception and drops the strong ref."""
    import asyncio
    import logging

    monitor = ConnectionMonitor(on_status=lambda *a: None)

    async def boom():
        raise RuntimeError("kaput")

    with caplog.at_level(logging.ERROR, logger="wave.engine.connections"):
        task = monitor._spawn(boom(), "test boom")
        assert task in monitor._oneshot_tasks  # strongly held while pending
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run

    assert task not in monitor._oneshot_tasks
    logged = [r for r in caplog.records if "oneshot task" in r.getMessage()]
    assert logged and "kaput" in logged[0].getMessage()
    assert "test boom" in logged[0].getMessage()


async def test_sell_position_task_strongly_referenced_until_done():
    """A5-6: the manual SELL's task lives in _oneshot_tasks (a strong ref —
    asyncio only keeps weak ones) until close_now finishes, so GC can never
    reap a pending manual sell (the 2026-08-19 segfault class)."""
    import asyncio
    import gc
    from types import SimpleNamespace

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    release = asyncio.Event()
    ran: list[str] = []

    class FakeActor:
        state = "OPEN"  # not a PositionState → never in TERMINAL_STATES
        spec = SimpleNamespace(symbol="AAPL")

        async def close_now(self, reason):
            await release.wait()
            ran.append(reason)

    class FakeEngine:
        actors = {"k": FakeActor()}

    monitor.engine = FakeEngine()
    monitor.sell_position("k")

    assert len(monitor._oneshot_tasks) == 1
    task = next(iter(monitor._oneshot_tasks))
    gc.collect()  # the weak-ref hazard: without the set this could reap it
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    await task
    await asyncio.sleep(0)  # done-callback
    assert ran == ["manual sell (positions card)"]
    assert monitor._oneshot_tasks == set()


def test_roll_signal_day_reseeds_on_et_day_at_2100_et():
    """A4-12 (monitor leg): a 20:00–24:00 ET restart used the UTC date —
    already TOMORROW — so the reseed query found nothing and re-armed every
    symbol traded today. The reseed now covers the ET day via a UTC lower
    bound at ET midnight."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    queries: list[tuple] = []

    class FakeDB:
        def query(self, sql, params):
            queries.append((sql, params))
            return [{"symbol": "NVDA"}]

    monitor = ConnectionMonitor(on_status=lambda *a: None, database=FakeDB())
    now_et = datetime(2026, 9, 22, 21, 0, tzinfo=ZoneInfo("America/New_York"))
    monitor._roll_signal_day(now_et)

    assert monitor._signal_day == "2026-09-22"  # keyed to the ET day
    assert monitor._signaled == {"NVDA"}
    ((sql, params),) = queries
    assert "opened_at >= ?" in sql
    # ET midnight of the ET day, expressed in UTC (EDT → 04:00Z), NOT the
    # UTC date of "now" (which is already Sep 23 at 21:00 ET)
    assert params[0] == "2026-09-22T04:00:00+00:00"


async def test_teardown_folds_scanner2_baselines_once_and_reruns_clean():
    """A5-8: fold_baselines()/scanner2-null sat INSIDE the news/feeds task
    loop — the second iteration called fold_baselines() on None (suppressed).
    Now it folds exactly once, and a second teardown is a clean no-op."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    calls: list[int] = []

    class FakeS2:
        def fold_baselines(self):
            calls.append(1)

    monitor._scanner2 = FakeS2()
    await monitor._teardown()
    assert calls == [1]
    assert monitor._scanner2 is None
    await monitor._teardown()  # second pass: no None.fold, no crash
    assert calls == [1]


async def test_confirm_telegram_mode_returns_status():
    """A5-10: no silent no-op — the caller gets a status string it can show,
    so a down bridge can't masquerade as phone-proof."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)

    # bridge down → honest "config only"
    assert (
        await monitor.confirm_telegram_mode("minimum")
        == "bridge not running — saved to config only"
    )

    # bridge up → confirmed, and the push actually went out
    bridge = _PushRecorderBridge()
    monitor._bridge = bridge
    assert await monitor.confirm_telegram_mode("minimum") == "confirmed on the phone"
    assert any("MINIMUM" in text for text in bridge.sent)

    # bridge up but push blows → failure surfaced, not swallowed
    class BoomBridge:
        is_running = True

        async def push(self, text):
            raise OSError("telegram down")

    monitor._bridge = BoomBridge()
    assert (
        await monitor.confirm_telegram_mode("off")
        == "confirmation push failed — saved to config only"
    )


def test_igniter_guest_ttl_and_dedup():
    """Catch-the-climb (2026-09-23): event-promoted igniters get entry-pool
    guest seats — TTL-bounded, deduped against pool/day-list."""
    import time as _time

    from waveapp.engine.connection_monitor import ConnectionMonitor

    monitor = ConnectionMonitor.__new__(ConnectionMonitor)
    monitor._igniter_guests = {"OLD": _time.monotonic() - 2000, "HOT": _time.monotonic()}
    now_mono = _time.monotonic()
    for sym in [s for s, t in monitor._igniter_guests.items() if now_mono - t > 1800]:
        monitor._igniter_guests.pop(sym, None)
    assert "OLD" not in monitor._igniter_guests  # 30-min seat expired
    assert "HOT" in monitor._igniter_guests


# -- S0 SSR sweep (audit A3-8: record_ssr_trigger finally has a caller) -------


def test_ssr_sweep_records_10pct_drop_once_and_blocks_the_short():
    """A watched symbol trading ≤ −10% vs prior close is recorded ONCE per
    day (not every minute), covers held positions too, and risk then blocks
    the short entry per Rule 201 while longs stay open for business."""
    from datetime import date
    from types import SimpleNamespace

    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    monitor = ConnectionMonitor(lambda *a: None)
    risk = RiskEngine(RiskLimits(shorts_enabled=True))  # isolate SSR (S1)
    held = SimpleNamespace(spec=SimpleNamespace(symbol="HELD"))
    monitor.engine = SimpleNamespace(risk=risk, actors={"k": held})
    monitor._scanner2 = SimpleNamespace(
        _index={"DROP": 0, "OK": 1, "HELD": 2, "NODATA": 3},
        last=[44.9, 49.0, 88.0, 30.0],  # DROP −10.2%, OK −2%, HELD −12%
        prev_close_live=[50.0, 50.0, 100.0, 0.0],  # NODATA: no prior close → skip
    )
    monitor._day_list = ["DROP", "OK", "NODATA", "GHOST"]  # GHOST: not in s2 → skip
    today = date(2026, 9, 23)

    recorded: list[str] = []
    original = risk.record_ssr_trigger

    def counting(symbol, on_day):
        recorded.append(symbol)
        original(symbol, on_day)

    risk.record_ssr_trigger = counting
    monitor._ssr_sweep(today)
    monitor._ssr_sweep(today)  # second minute boundary — no re-record
    assert sorted(recorded) == ["DROP", "HELD"]
    assert risk.ssr_active("DROP", today) and risk.ssr_active("HELD", today)
    assert not risk.ssr_active("OK", today)
    assert not risk.can_enter("DROP", OrderSide.SELL, 0, date(2026, 9, 24))  # next day too
    assert risk.can_enter("DROP", OrderSide.BUY, 0, today)  # longs untouched


def test_ssr_sweep_is_inert_without_engine_or_scanner2():
    """Before connect (or with scanner2 down) the sweep is a silent no-op."""
    from datetime import date

    monitor = ConnectionMonitor(lambda *a: None)
    monitor._ssr_sweep(date(2026, 9, 23))  # engine is None — must not raise


# -- M0 telemetry (ML master plan, 2026-09-23) --------------------------------


async def test_entry_lag_row_written_on_first_filled_sight(tmp_path):
    """A pipeline entry (watch → AUTO ENTRY stamp → fill) writes ONE
    entry_lag row when the monitor first sees the actor open; a later sight
    and an adopted position (no signal stamp) write nothing."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from waveapp.broker.base import OrderSide
    from waveapp.persistence.db import Database, utc_now

    database = Database(tmp_path / "t.db")
    monitor = ConnectionMonitor(lambda *a: None, database=database)
    monitor._note_first_watch(["AAPL"])
    monitor._note_first_watch(["AAPL"])  # later sighting never overwrites
    monitor._entry_signal_at["AAPL"] = utc_now()
    actor = SimpleNamespace(
        spec=SimpleNamespace(symbol="AAPL", side=OrderSide.BUY, strategy="VWAP"),
        state=SimpleNamespace(value="open"),
        avg_entry_price=101.0,
        filled_qty=10.0,
        entry_filled_at=datetime.now(UTC),
        realized_pnl=0.0,
    )
    monitor.engine = SimpleNamespace(actors={"k1": actor})
    monitor._track_closed_trades()
    rows = database.query("SELECT * FROM entry_lag")
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "AAPL" and row["side"] == "long" and row["strategy"] == "VWAP"
    assert row["ts_first_watch"] is not None and row["ts_signal"] is not None
    assert row["lag_watch_to_signal_s"] is not None and row["lag_watch_to_signal_s"] >= 0
    assert row["lag_signal_to_fill_s"] is not None
    # same actor seen again → no duplicate; the signal stamp was consumed
    monitor._track_closed_trades()
    assert "AAPL" not in monitor._entry_signal_at
    # an ADOPTED position (no signal stamp) writes nothing
    adopted = SimpleNamespace(
        spec=SimpleNamespace(symbol="MSFT", side=OrderSide.SELL, strategy="GAP"),
        state=SimpleNamespace(value="open"),
        avg_entry_price=200.0,
        filled_qty=5.0,
        entry_filled_at=datetime.now(UTC),
        realized_pnl=0.0,
    )
    monitor.engine.actors["k2"] = adopted
    monitor._track_closed_trades()
    assert len(database.query("SELECT * FROM entry_lag")) == 1
    database.close()


async def test_ml_daily_rollup_summarizes_the_day(tmp_path):
    """The nightly rollup reads the M0 tables into one ml_daily row + one
    TRAINING log line, medians included."""
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "t.db")
    day = "2026-09-23"
    for dwell, stance in ((1000, "RIDE"), (3000, "BANK"), (2000, "ACT:judge BANK stale profit")):
        database.execute(
            "INSERT INTO judge_transitions (ts, symbol, position_key, side, from_stance,"
            " to_stance, dwell_ms) VALUES (?,?,?,?,?,?,?)",
            (day + "T14:00:00.000+00:00", "AAPL", "k1", "long", "WAIT", stance, dwell),
        )
    database.execute(
        "INSERT INTO entry_lag (symbol, side, strategy, ts_fill, lag_watch_to_signal_s)"
        " VALUES (?,?,?,?,?)",
        ("AAPL", "long", "VWAP", day + "T14:00:05.000+00:00", 12.5),
    )
    database.execute(
        "INSERT INTO candidates (symbol, session_date, ts, strategy, features, decision)"
        " VALUES (?,?,?,?,?,?)",
        ("DWN", day, day + "T14:00:00.000+00:00", "VWAP", '{"side": "short"}', "rejected"),
    )
    database.execute(  # legacy row without side — must not count as short
        "INSERT INTO candidates (symbol, session_date, ts, strategy, features, decision)"
        " VALUES (?,?,?,?,?,?)",
        ("OLD", day, day + "T14:01:00.000+00:00", "VWAP", '{"price": 5.0}', "rejected"),
    )
    monitor = ConnectionMonitor(lambda *a: None, database=database)
    monitor._ml_daily_rollup(day)
    row = database.query("SELECT * FROM ml_daily")[0]
    assert row["date"] == day
    assert row["n_transitions"] == 2 and row["n_acts"] == 1
    assert row["median_dwell_ms"] == 3000  # upper median of [1000, 3000]
    assert row["n_entries"] == 1 and row["median_watch_to_signal_s"] == 12.5
    assert row["n_short_candidates"] == 1
    log_rows = database.query("SELECT message FROM log WHERE category='TRAINING'")
    assert any("M0 rollup" in r["message"] for r in log_rows)
    # rerun replaces, never duplicates
    monitor._ml_daily_rollup(day)
    assert len(database.query("SELECT * FROM ml_daily")) == 1
    database.close()


async def test_ml_journal_write_inserts_and_updates(tmp_path):
    """The kitchen's db closure: 'transition' returns the row id; the
    outcome ops fill the forward columns; no database → None, no raise."""
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "t.db")
    monitor = ConnectionMonitor(lambda *a: None, database=database)
    row_id = monitor._ml_journal_write(
        "transition",
        {
            "ts": "2026-09-23T14:00:00.000+00:00",
            "symbol": "AAPL",
            "position_key": "k1",
            "side": "long",
            "from_stance": "WAIT",
            "to_stance": "RIDE",
            "reason": "+0.8A building",
            "profit_atr": 0.8,
            "peak_profit_atr": 0.8,
            "giveback_atr": 0.0,
            "signs": 0,
            "failed_highs": 0,
            "peak_age_s": 1.0,
            "vol_ratio": 1.0,
            "atr_px": 0.5,
            "px": 100.4,
            "entry_px": 100.0,
            "close_guard": 0,
            "dwell_ms": 20000,
            "regime": "midday",
        },
    )
    assert row_id is not None
    monitor._ml_journal_write("px30", {"id": row_id, "v": 100.5})
    monitor._ml_journal_write("px2m", {"id": row_id, "v": 100.6, "mfe": 0.4, "mae": -0.1})
    monitor._ml_journal_write("px5m", {"id": row_id, "v": 100.2})
    row = database.query("SELECT * FROM judge_transitions WHERE id=?", (row_id,))[0]
    assert row["px_30s"] == 100.5 and row["px_2m"] == 100.6 and row["px_5m"] == 100.2
    assert row["mfe_2m_atr"] == 0.4 and row["mae_2m_atr"] == -0.1
    # no DB attached → inert
    bare = ConnectionMonitor(lambda *a: None, database=None)
    assert bare._ml_journal_write("transition", {}) is None
    database.close()
