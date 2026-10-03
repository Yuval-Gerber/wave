"""Phase 8.8: the System tab + sidebar brand."""

from types import SimpleNamespace

from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.ui.sidebar import BrandBadge, BrandWord
from waveapp.ui.system_page import SystemPage


def test_system_page_renders_snapshot(qtbot):
    page = SystemPage()
    qtbot.addWidget(page)
    page.update_system(
        {
            "engine_state": "running",
            "open_positions": 2,
            "feed_ages": {"trades": 3.0, "quotes": 5.0, "bars": 42.0},
            "db": {"name": "wave_paper.db", "schema": 1, "size_mb": 4.2},
            "fees": [
                {
                    "fee_name": "sec_section31",
                    "effective_date": "2026-04-04",
                    "rate": 20.6,
                    "unit": "usd_per_million_sold",
                    "cap": None,
                }
            ],
            "version": "0.0.1",
        }
    )
    assert page.engine_state_label.text() == "running"
    page.set_open_positions(2)  # r5: the Positions tab display drives this
    assert "2 open positions" in page.engine_positions_label.text()
    assert "wave_paper.db" in page.db_label.text()
    assert "Wave 0.0.1" in page.version_label.text()
    assert "sec_section31" in page.fees_label.text()
    assert "s ago" in page.feed_rows["trades"].text()
    assert page.regime_label.text() != "—"  # session ticks from the real clock
    assert not page.grab().isNull()


def test_system_page_risk_and_trading_cards(qtbot):
    page = SystemPage()
    qtbot.addWidget(page)
    page.update_system(
        {
            "engine_state": "running",
            "risk": {
                "halt": "none",
                "freezes": [],
                "limits": {
                    "risk_per_trade_pct": 1.0,
                    "max_daily_loss_pct": 3.0,
                    "max_weekly_loss_pct": 6.0,
                    "max_positions": 3,
                },
            },
            "trading": {
                "auto_trade": False,
                "scan_universe": 400,
                "scan_interval": 120,
                "last_scan": "10:16:48",
            },
            "uptime_seconds": 3720,
            "errors_today": 0,
        }
    )
    assert page.halt_label.text() == "no halts"
    assert "entries flowing" in page.freeze_label.text()
    assert (
        "1% / trade" in page.limits_label.text() and "max 3 positions" in page.limits_label.text()
    )
    assert page.auto_trade_label.text() == "AUTO-TRADE OFF"
    assert "signals only until Phase 10" in page.scanner_label.text()
    assert "last scan 10:16:48" in page.last_scan_label.text()
    assert "up 1h 02m" in page.uptime_label.text()
    # a halt + freeze shows red and the reasons
    page.update_system(
        {"risk": {"halt": "daily_loss", "freezes": ["stale market data"], "limits": {}}}
    )
    assert "daily loss" in page.halt_label.text()
    assert "stale market data" in page.freeze_label.text()


def test_system_page_shorts_line_only_when_enabled(qtbot):
    """S4: the risk card appends '· shorts ON (≤50% book)' only when the
    shorts_enabled flag reaches the limits snapshot; invisible when off."""
    page = SystemPage()
    qtbot.addWidget(page)
    limits = {
        "risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 3.0,
        "max_weekly_loss_pct": 6.0,
        "max_positions": 3,
    }
    page.update_system({"risk": {"halt": "none", "freezes": [], "limits": dict(limits)}})
    assert "shorts" not in page.limits_label.text()  # flag off/absent: no trace
    page.update_system(
        {
            "risk": {
                "halt": "none",
                "freezes": [],
                "limits": {**limits, "shorts_enabled": True, "short_risk_share": 0.5},
            }
        }
    )
    assert "· shorts ON (≤50% book)" in page.limits_label.text()


def test_system_page_rides_the_status_signal(qtbot):
    from waveapp.ui.sidebar import ConnectionStatus

    page = SystemPage()
    qtbot.addWidget(page)
    status = ConnectionStatus()
    page.attach_status(status)
    status.set_status("Alpaca", "#28CD41", "Alpaca: connected (paper) — market open")
    assert "connected (paper)" in page.status_rows["Alpaca"].detail.text()


def test_monitor_system_data_shapes():
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = SimpleNamespace(
        path=SimpleNamespace(name="wave_paper.db", stat=lambda: SimpleNamespace(st_size=1048576)),
        schema_version=lambda: 1,
        query=lambda sql: [
            {
                "fee_name": "finra_taf",
                "effective_date": "2026-01-01",
                "rate": 0.000195,
                "unit": "usd_per_share_sold",
                "cap": 9.79,
            }
        ],
    )
    data = monitor._system_data()
    assert data["engine_state"] == "offline"
    assert data["db"]["size_mb"] == 1.0
    assert data["fees"][0]["fee_name"] == "finra_taf"
    assert "version" in data


def test_brand_badge_and_word_render(qtbot):
    badge = BrandBadge()
    qtbot.addWidget(badge)
    assert not badge.grab().isNull()
    word = BrandWord()
    qtbot.addWidget(word)
    word.resize(140, 34)
    word._phase = 0.5
    assert not word.grab().isNull()
    word.resize(30, 34)  # collapsed rail: paints nothing, still valid
    assert not word.grab().isNull()


# -- 8.8 r3: market clock + week close ----------------------------------------


def test_boundary_is_minute_aligned_so_countdowns_tick():
    """r3 bug: the boundary drifted with the caller's seconds ('…59s'
    forever). It must be a FIXED wall-clock minute."""
    from datetime import UTC, datetime

    from waveapp.engine.session import SessionScheduler

    now_a = datetime(2026, 8, 12, 15, 0, 17, tzinfo=UTC)  # a Wednesday
    now_b = datetime(2026, 8, 12, 15, 0, 43, tzinfo=UTC)  # 26s later
    assert SessionScheduler.next_closed_boundary(now_a) == SessionScheduler.next_closed_boundary(
        now_b
    )
    assert SessionScheduler.next_closed_boundary(now_a).second == 0


def test_market_clock_modes():
    from datetime import UTC, datetime

    from waveapp.engine.session import SessionScheduler

    # Wednesday 14:00 UTC = 10:00 ET, inside RTH → counting to the close
    rth = SessionScheduler.market_clock(datetime(2026, 8, 12, 14, 0, tzinfo=UTC))
    assert rth["mode"] == "closes"
    assert rth["target"].astimezone(UTC).hour == 20  # 16:00 ET in UTC (EDT)
    assert rth["anchor"] < rth["target"]

    # Saturday → counting to Monday's open
    weekend = SessionScheduler.market_clock(datetime(2026, 8, 15, 14, 0, tzinfo=UTC))
    assert weekend["mode"] == "opens"
    assert weekend["target"].astimezone(UTC).weekday() == 0  # Monday
    assert weekend["anchor"] < weekend["target"]


def test_next_closed_info_names_the_holiday():
    from datetime import UTC, datetime

    from waveapp.engine.session import SessionScheduler

    # the Friday before Labor Day 2026 (Mon Sep 7): the closure includes it
    boundary, name = SessionScheduler.next_closed_info(
        datetime(2026, 9, 3, 15, 0, tzinfo=UTC)  # Thursday before
    )
    assert boundary.astimezone(UTC).weekday() in (4, 5)  # closes Fri night ET
    assert name is not None and "Labor" in name

    # a plain mid-August weekend has no holiday name
    _boundary, plain = SessionScheduler.next_closed_info(datetime(2026, 8, 12, 15, 0, tzinfo=UTC))
    assert plain is None


def test_clock_cards_tick(qtbot):
    page = SystemPage()
    qtbot.addWidget(page)
    page._tick()
    assert page.clock_caption.text() in ("MARKET OPENS IN", "MARKET CLOSES IN")
    assert ":" in page.clock_ring.text
    assert page.week_countdown_label.text() != "—"
    reason = page.week_reason_label.text()
    # trading week → counts DOWN to the close; weekend/holiday → counts UP
    # to the OPEN (2026-08-22 fix: the old card counted to "the next closed
    # minute" all weekend)
    # 2026-08-23: Sun 20:00 wakes the SCANNER, not the market —
    # the market-clock card beside it owns the real open
    assert "weekend" in reason or "holiday" in reason or "trading week opens" in reason
    first = page.week_countdown_label.text()
    import time as _t

    # it COUNTS DOWN: wait until the displayed second actually rolls over
    # (a fixed 1.1s sleep flaked under full-suite load)
    deadline = _t.monotonic() + 5.0
    while _t.monotonic() < deadline:
        _t.sleep(0.4)
        page._tick()
        if page.week_countdown_label.text() != first:
            break
    assert page.week_countdown_label.text() != first


def test_system_data_works_with_the_REAL_engine():
    """r4 regression: halt_state is a property — calling it blanked the whole
    System tab. Build the snapshot against a real EngineCore/RiskEngine."""
    from waveapp.engine.core import EngineCore

    class _Adapter:
        async def submit_order(self, *a, **k): ...

        def trade_updates(self): ...

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor.engine = EngineCore(_Adapter(), database=None)
    data = monitor._system_data()
    assert data["engine_state"] == "idle"
    assert data["risk"]["halt"] == "none"
    assert data["risk"]["limits"]["max_positions"] == 3
    assert data["trading"]["scan_universe"] == monitor._scan_universe_size


def test_engine_card_live_wiring(qtbot):
    """r5: engine state updates instantly; the open-positions count
    follows what the Positions tab displays — including Test-tab fakes."""
    from waveapp.config import AppConfig
    from waveapp.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    window.on_engine_state("running")
    assert window.system_page.engine_state_label.text() == "running"
    window.positions_page.set_test_positions(
        [
            {
                "key": "f1",
                "symbol": "AAPL",
                "side": "long",
                "qty": 5,
                "entry": 100.0,
                "last": 101.0,
                "stop": 98.0,
                "stage": "",
                "strategy": "ORB",
                "halted": False,
                "ssr": False,
            }
        ]
    )
    assert "1 open position" in window.system_page.engine_positions_label.text()
    window.positions_page.set_test_positions([])
    assert "0 open positions" in window.system_page.engine_positions_label.text()


def test_stream_budget_counts_channel_subscriptions():
    """r5: Alpaca Basic counts CHANNEL-symbol pairs (~30) — the old 28-symbol
    cap caused 'symbol limit exceeded (405)' spam (24 in the log)."""
    import asyncio

    from waveapp.data.hub import DYNAMIC_CHANNELS, SUBSCRIPTION_LIMIT, DataHub

    hub = DataHub(["SPY", "QQQ", "AAPL", "TSLA", "NVDA"])  # 5 × 4 = 20 slots
    added = asyncio.run(hub.watch(["AMD", "META", "NFLX", "CRM", "ORCL", "PLTR"]))
    used = 5 * 4 + len(added) * DYNAMIC_CHANNELS
    assert used <= SUBSCRIPTION_LIMIT  # never over the real quota
    assert added == ["AMD", "META", "NFLX"]  # 20 + 3×3 = 29 ≤ 30; 4th would break it


# -- ML page, agenda #3 redo step 1 (2026-08-23): UI only, zero DB work ------


def test_ml_page_step1_switch_only(qtbot):
    """RETARGETED 2026-08-30: the switch lives on MLPage now (own tab)."""
    from waveapp.ui.ml_page import MLPage

    page = MLPage()
    qtbot.addWidget(page)
    assert page.ml_mode_buttons["off"].isChecked()
    assert not page.ml_mode_buttons["active"].isEnabled()
    emitted = []
    page.ml_mode_changed.connect(emitted.append)
    page._ml_mode_clicked("shadow")
    assert emitted == ["shadow"] and page.ml_mode_buttons["shadow"].isChecked()
    page.set_ml_mode_display("off")
    assert page.ml_mode_buttons["off"].isChecked()


def test_ml_page_step2_renders_the_dataset(qtbot):
    """RETARGETED 2026-08-30: dataset stats render on MLPage now."""
    from waveapp.ui.ml_page import MLPage

    page = MLPage()
    qtbot.addWidget(page)
    page.update_ml(
        {
            "labeled": 3000,
            "mode": "shadow",
            "label_wins": 100,
            "label_losses": 40,
            "symbol_days": 2900,
            "model_ready": False,
        }
    )
    assert page.vital_lessons.value.text() == "3,000"
    assert not page.ml_mode_buttons["active"].isEnabled()


def test_ml_page_moved_to_its_own_tab(qtbot):
    """2026-08-30: the Judge got a top-level ML tab; the System
    sub-page is a signpost and the mode contract lives on MLPage now."""
    from waveapp.ui.ml_page import MLPage

    page = MLPage()
    qtbot.addWidget(page)
    assert page.ml_mode_buttons["off"].isChecked()  # DEFAULT: off
    assert not page.ml_mode_buttons["active"].isEnabled()  # earned, never given
    emitted = []
    page.ml_mode_changed.connect(emitted.append)
    page._ml_mode_clicked("shadow")
    assert emitted == ["shadow"] and page.ml_mode_buttons["shadow"].isChecked()
    page.set_ml_mode_display("off")
    assert page.ml_mode_buttons["off"].isChecked()
    # stats payload renders; model_ready unlocks ACTIVE
    page.update_ml(
        {
            "labeled": 12240,
            "mode": "shadow",
            "label_wins": 844,
            "label_losses": 223,
            "symbol_days": 12000,
            "model_ready": True,
        }
    )
    assert page.vital_lessons.value.text() == "12,240"
    assert page.ml_mode_buttons["active"].isEnabled()
    # a SECOND snapshot with more lessons queues REAL galaxy events (no loop)
    before = len(page.canvas._queued)
    page.update_ml({"labeled": 12250, "mode": "shadow", "label_wins": 850, "label_losses": 224})
    assert len(page.canvas._queued) > before  # 10 new lessons entered the network
    # sections switch
    page.select_section(1)
    assert page.stack.currentIndex() == 1


def test_ml_mode_status_path_cannot_raise(qtbot):
    """Audit A5-1 (2026-09-22): clicking an ML mode pill routed status text to
    SystemPage.set_ml_status, whose ml_status_label died with the sub-page
    move on 2026-08-30 → AttributeError across the Qt signal boundary →
    qFatal abort. Status now lives on MLPage; SystemPage stays a safe no-op."""
    from waveapp.ui.ml_page import MLPage

    ml = MLPage()
    qtbot.addWidget(ml)
    ml.set_ml_status("refused — no validated model exists yet (§9.2).")
    assert "refused" in ml.status_line.text()
    # mode display rewrites the line (why app.py orders display THEN refusal)
    ml.set_ml_mode_display("off")
    assert "refused" not in ml.status_line.text()

    sys_page = SystemPage()
    qtbot.addWidget(sys_page)
    assert not hasattr(sys_page, "ml_status_label")  # the label is truly gone
    sys_page.set_ml_status("any text")  # must never raise


def test_scanner_page_live_menu_strip(qtbot):
    """Scanner 2.0 menu strip ('the Scanner tab becomes a live feed')."""
    from waveapp.ui.scanner_page import ScannerPage

    page = ScannerPage()
    qtbot.addWidget(page)
    page.update_menu(
        [
            {"symbol": "RBLX", "rvol": 4.2, "day_pct": 5.1},
            {"symbol": "CRCL", "rvol": 2.1, "day_pct": 9.0},
        ]
    )
    text = page.menu_label.text()
    assert "RBLX" in text and "CRCL" in text and "4.2×" in text
    page.update_menu([])
    assert page.menu_label.text() == ""
