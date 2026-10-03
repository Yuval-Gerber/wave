"""Phase 5 step 5.2: monitor↔engine↔telegram wiring (no network)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from waveapp.data.hub import DataHub
from waveapp.engine.connection_monitor import ConnectionMonitor


def _monitor(**kwargs) -> ConnectionMonitor:
    return ConnectionMonitor(lambda *a: None, **kwargs)


# -- command routing ---------------------------------------------------------


async def test_command_offline_without_engine():
    monitor = _monitor()
    assert "offline" in await monitor.command("start")


async def test_command_routes_and_start_becomes_resume_when_paused():
    monitor = _monitor()
    engine = SimpleNamespace(
        state=SimpleNamespace(value="paused"),
        start=AsyncMock(return_value="started"),
        resume=AsyncMock(return_value="resumed"),
        kill=AsyncMock(return_value="killed"),
    )
    monitor.engine = engine
    assert await monitor.command("start") == "resumed"
    engine.resume.assert_awaited_once()
    engine.start.assert_not_awaited()

    engine.state = SimpleNamespace(value="idle")
    assert await monitor.command("start") == "started"
    assert await monitor.command("kill") == "killed"
    assert "unknown" in await monitor.command("frobnicate")


# -- test entry --------------------------------------------------------------


async def test_test_entry_requires_engine_and_quote():
    monitor = _monitor()
    assert "offline" in await monitor.test_entry()

    monitor.engine = SimpleNamespace(
        open_test_position=AsyncMock(return_value="test entry submitted")
    )
    assert "no market data" in await monitor.test_entry()

    hub = DataHub(["SPY"])
    monitor._hub = hub
    assert "no live quote" in await monitor.test_entry()

    hub.latest_quotes["SPY"] = SimpleNamespace(ask_price=500.25, bid_price=500.20)
    result = await monitor.test_entry()
    assert result == "test entry submitted"
    monitor.engine.open_test_position.assert_awaited_once_with("SPY", 500.25)


# -- LULD status classification ---------------------------------------------


def test_trading_status_classification():
    events = []
    hub = DataHub(["NVDA"], on_trading_status=lambda s, h: events.append((s, h)))

    hub.classify_and_forward_status(
        SimpleNamespace(symbol="NVDA", status_code="H", status_message="Trading halt")
    )
    assert events == [("NVDA", True)]

    hub.classify_and_forward_status(
        SimpleNamespace(symbol="NVDA", status_code="T", status_message="Resumption")
    )
    assert events == [("NVDA", True), ("NVDA", False)]

    # unknown codes are ignored, not guessed
    hub.classify_and_forward_status(
        SimpleNamespace(symbol="NVDA", status_code="X", status_message="")
    )
    assert len(events) == 2


def test_monitor_forwards_halt_to_engine():
    monitor = _monitor()
    engine = SimpleNamespace(on_symbol_halt=lambda s, h: calls.append((s, h)))
    calls = []
    monitor.engine = engine
    monitor._on_trading_status("NVDA", True)
    assert calls == [("NVDA", True)]


# -- telegram /test flow -----------------------------------------------------


async def test_telegram_test_command_confirmation_flow():
    from waveapp.telegram.bridge import ConfirmationGate, TelegramBridge

    executed = []

    async def fake_test_entry():
        executed.append(True)
        return "test entry submitted: SPY"

    gate = ConfirmationGate()
    bridge = TelegramBridge(
        allowed_user_id=1,
        adapter_provider=lambda: None,
        test_entry=fake_test_entry,
        gate=gate,
    )
    prompt = await bridge.handle_dangerous("test")
    code = __import__("re").search(r": (\d{6})", prompt).group(1)
    result = await bridge.handle_text(code)
    assert result == "test entry submitted: SPY"
    assert executed == [True]


# -- error surfacing ---------------------------------------------------------


def test_error_alert_handler_dedupes_and_filters():
    import logging

    from waveapp.engine.error_alerts import ErrorAlertHandler

    clock = {"now": 0.0}
    alerts = []
    handler = ErrorAlertHandler(
        alerts.append, min_interval_seconds=600, time_fn=lambda: clock["now"]
    )
    logger = logging.getLogger("wave.test.alerts")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info("just info")  # below ERROR → ignored
    logger.error("stop amend failed")  # → alert
    logger.error("stop amend failed")  # duplicate within window → dropped
    clock["now"] = 700.0
    logger.error("stop amend failed")  # window passed → alert again
    logging.getLogger("wave.telegram").addHandler(handler)
    logging.getLogger("wave.telegram").error("push failed")  # excluded → no loop

    logger.removeHandler(handler)
    assert len(alerts) == 2
    assert "stop amend failed" in alerts[0]


def test_stale_feed_freezes_entries_only_when_market_open():
    from unittest.mock import MagicMock

    monitor = _monitor()
    engine = MagicMock()
    monitor.engine = engine

    monitor._market_open = False
    monitor._on_feed_stale("trades", True)  # closed market: informational only
    engine.risk.freeze_entries.assert_not_called()

    monitor._market_open = True
    monitor._on_feed_stale("trades", True)  # open market: freeze
    engine.risk.freeze_entries.assert_called_once_with("stale market data")

    monitor._on_feed_stale("trades", False)  # recovery: unfreeze
    engine.risk.unfreeze_entries.assert_called_once_with("stale market data")


def test_bars_channel_gets_long_threshold_and_never_freezes():
    from unittest.mock import MagicMock

    from waveapp.data.hub import HeartbeatWatchdog

    clock = {"now": 100.0}
    events = []
    watchdog = HeartbeatWatchdog(
        time_fn=lambda: clock["now"],
        on_stale_change=lambda ch, s: events.append((ch, s)),
    )
    watchdog.beat("trades")
    watchdog.beat("bars")
    clock["now"] = 190.0  # 90s later: bars are naturally minute-spaced
    result = watchdog.check()
    assert result["trades"] is True  # continuous channel IS stale at 90s
    assert result["bars"] is False  # bars threshold is 180s — no false alarm
    clock["now"] = 300.0
    assert watchdog.check()["bars"] is True  # a truly dead bars channel still reports

    # and even a stale bars channel never freezes entries
    monitor = _monitor()
    engine = MagicMock()
    monitor.engine = engine
    monitor._market_open = True
    monitor._on_feed_stale("bars", True)
    engine.risk.freeze_entries.assert_not_called()


async def test_trend_gate_refuses_longs_below_todays_open(monkeypatch):
    """Adopted 2026-08-24 (research_smarter.py, OOS +42,361 vs +26,614 /
    PF 1.81 vs 1.42): no long entries while price < today's open."""
    from types import SimpleNamespace

    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.engine.scanner import Candidate, SymbolFeatures

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    entered = []
    monitor.engine = SimpleNamespace(open_position=lambda spec: entered.append(spec))
    features = SymbolFeatures(
        symbol="PURR",
        price=11.0,
        prev_close=11.5,
        gap_pct=2.6,
        rvol=6.0,
        atr_pct=5.0,
        spread=0.01,
        day_volume=5e6,
        avg_daily_volume=3e6,
        day_open=11.8,  # price 11.0 < open 11.8 → the tape is against longs
    )
    candidate = Candidate(
        features=features, score=5.0, strategy_scores={}, best_strategy="VWAP", accepted=True
    )

    # minimal harness: hub/scanner present, RTH, auto-trade on — the gate
    # must refuse BEFORE any strategy runs
    class Hub:
        async def watch(self, syms): ...
        def session_vwap(self, s):
            return 10.9

        bar_builder = SimpleNamespace(bars=lambda s: [])

    monitor._hub = Hub()
    monitor._scanner = SimpleNamespace(
        gate=None, expected_move_atr_fraction=0.25, slippage_buffer_per_share=0.01
    )
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class FakeSched:
        @staticmethod
        def info():
            from waveapp.engine.session import Regime

            return SimpleNamespace(
                regime=Regime.MIDDAY,
                is_lull=False,
                et_time=datetime(2026, 8, 25, 11, 0, tzinfo=ZoneInfo("America/New_York")),
            )

        @staticmethod
        def minutes_to_rth_close(t=None):
            return 300.0

    monkeypatch.setattr("waveapp.engine.session.SessionScheduler", FakeSched)
    await monitor._entry_pipeline([candidate])
    assert entered == []  # refused at the trend gate — no strategy ever ran


def test_transient_websocket_noise_pages_nobody_unless_it_persists():
    """4:00 AM wake-ups (2026-08-25 + 08-28): self-healing reconnect errors
    stay in the log but never alert; a storm still raging past the grace
    window sends ONE aggregated 'not healing' alert."""
    import logging

    from waveapp.engine.error_alerts import ErrorAlertHandler

    clock = {"now": 0.0}
    alerts: list[str] = []
    handler = ErrorAlertHandler(alerts.append, time_fn=lambda: clock["now"])

    def fire(msg, name="wave.data"):
        handler.emit(logging.LogRecord(name, logging.ERROR, __file__, 1, msg, None, None))

    # a 2-minute self-healing blip: 8 errors, zero alerts
    for i in range(8):
        clock["now"] = i * 15.0
        fire("error during websocket communication: ")
    assert alerts == []

    # a real outage: the storm keeps going past 300s → exactly ONE alert
    for i in range(30):
        clock["now"] = 1200.0 + i * 15.0  # new storm (old one healed)
        fire("data websocket error, restarting connection: no close frame")
    assert len(alerts) == 1 and "NOT HEALING" in alerts[0]

    # ordinary errors still alert immediately
    clock["now"] = 5000.0
    fire("scan cycle failed")
    assert len(alerts) == 2 and "scan cycle failed" in alerts[1]


def test_error_alert_backoff_escalates_not_spams():
    """2026-09-02 (woken by ~33 identical pings): a PERSISTENT error
    escalates its quiet window ×3 (capped) instead of re-alerting every
    10 minutes, and each re-alert carries the suppressed tally."""
    import logging

    from waveapp.engine.error_alerts import ErrorAlertHandler

    clock = {"now": 0.0}
    alerts: list[str] = []
    handler = ErrorAlertHandler(
        alerts.append, min_interval_seconds=600.0, time_fn=lambda: clock["now"]
    )

    def fire():
        handler.emit(
            logging.LogRecord(
                "wave.engine", logging.ERROR, __file__, 1, "scan cycle failed", None, None
            )
        )

    # a stuck error firing every 2 minutes for 8 hours
    for minute in range(0, 8 * 60, 2):
        clock["now"] = minute * 60.0
        fire()
    # old behavior would be ~48 alerts; backoff (10m→30m→90m→180m cap) ≈ 5
    assert 3 <= len(alerts) <= 7, f"got {len(alerts)} alerts: {alerts[:2]}"
    assert "still happening" in alerts[1]
    assert "×" in alerts[1] or "x" in alerts[1].lower()
    # a DIFFERENT error still alerts immediately
    handler.emit(
        logging.LogRecord(
            "wave.engine", logging.ERROR, __file__, 1, "something new broke", None, None
        )
    )
    assert "something new broke" in alerts[-1]
