"""Phase 10.1: Polygon history downloader + SQLite store."""

from datetime import date

from waveapp.research.history import (
    HistoryStore,
    MinuteBar,
    PolygonClient,
    download_history,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    """Scripted GET responses; records every URL asked for."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str) -> _FakeResponse:
        self.urls.append(url)
        return self._responses.pop(0)


def _bar_row(ts_ms: int, close: float = 100.0) -> dict:
    return {
        "t": ts_ms,
        "o": 99.5,
        "h": 100.5,
        "l": 99.0,
        "c": close,
        "v": 1200,
        "vw": 99.9,
        "n": 42,
    }


def test_store_roundtrip_and_coverage(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    bars = [
        MinuteBar("AAPL", 1_700_000_000_000 + i * 60_000, 1, 2, 0.5, 1.5, 100, 1.2, 5)
        for i in range(3)
    ]
    assert store.save_bars(bars) == 3
    assert store.bar_count("aapl") == 3  # case-insensitive
    loaded = store.load_bars("AAPL", date(2023, 11, 1), date(2023, 12, 1))
    assert [b.ts_ms for b in loaded] == [b.ts_ms for b in bars]
    store.mark_covered("AAPL", [date(2023, 11, 14), date(2023, 11, 15)])
    assert store.covered_days("AAPL") == {date(2023, 11, 14), date(2023, 11, 15)}
    # idempotent: saving the same bars again doesn't duplicate
    store.save_bars(bars)
    assert store.bar_count("AAPL") == 3
    store.close()


def test_client_paginates_and_backs_off():
    page2_url = "https://api.polygon.io/next-page"
    http = _FakeHttp(
        [
            _FakeResponse(429, {}),  # rate-limited once → retry
            _FakeResponse(
                200, {"results": [_bar_row(1000), _bar_row(61_000)], "next_url": page2_url}
            ),
            _FakeResponse(200, {"results": [_bar_row(121_000)]}),
        ]
    )
    import waveapp.research.history as history

    history.time.sleep = lambda _s: None  # no real waiting in tests
    client = PolygonClient(api_key="fake-polygon-key", http=http)
    bars = client.fetch_minute_bars("nvda", date(2026, 1, 5), date(2026, 1, 5))
    assert [b.ts_ms for b in bars] == [1000, 61_000, 121_000]
    assert all(b.symbol == "NVDA" for b in bars)
    assert "apiKey=fake-polygon-key" in http.urls[0]
    assert http.urls[2].startswith(page2_url)  # pagination followed


def test_download_is_incremental(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    http = _FakeHttp([_FakeResponse(200, {"results": [_bar_row(1_700_000_000_000)]})])
    client = PolygonClient(api_key="fake-polygon-key", http=http)

    saved = download_history(["AAPL"], date(2026, 1, 5), date(2026, 1, 7), store, client)
    assert saved == {"AAPL": 1}
    assert len(http.urls) == 1
    # every requested day (incl. empty weekend days) is now covered
    assert len(store.covered_days("AAPL")) == 3

    # second run: nothing missing → zero requests
    saved = download_history(["AAPL"], date(2026, 1, 5), date(2026, 1, 7), store, client)
    assert saved == {"AAPL": 0}
    assert len(http.urls) == 1
    store.close()


def test_polygon_key_settings_row_gated(qtbot, tmp_path, monkeypatch, fake_keychain):
    from waveapp.config import AppConfig
    from waveapp.ui.settings_page import SettingsPage

    reasons = []

    def fake_gate(reason, on_success):
        reasons.append(reason)
        on_success()

    from waveapp.security import gate

    monkeypatch.setattr(gate, "require_gate", fake_gate)
    config_path = tmp_path / "config.toml"
    AppConfig().save(config_path)
    page = SettingsPage(config_path=config_path)
    qtbot.addWidget(page)
    assert "NOT set" in page.polygon_status.text()
    page._save_polygon_requested()  # empty → refused before the gate
    assert reasons == []
    page.polygon_key_edit.setText("fake-polygon-key")
    page._save_polygon_requested()
    assert reasons == ["save the Polygon API key"]
    assert _vault(fake_keychain)["polygon_api_key"] == "fake-polygon-key"
    assert page.polygon_key_edit.text() == ""
    assert "fake-polygon-key" not in config_path.read_text()


def test_feed_tier_saves_and_hub_budget(qtbot, tmp_path, fake_keychain):
    from waveapp.config import AppConfig
    from waveapp.data.hub import SUBSCRIPTION_LIMITS, DataHub
    from waveapp.ui.settings_page import SettingsPage

    config_path = tmp_path / "config.toml"
    AppConfig().save(config_path)
    page = SettingsPage(config_path=config_path)
    qtbot.addWidget(page)
    page.feed_combo.setCurrentIndex(page.feed_combo.findData("sip"))
    page._save_feed()
    assert AppConfig.load(config_path).data_feed == "sip"

    hub = DataHub(["SPY"], feed="sip")
    assert hub.subscription_limit == SUBSCRIPTION_LIMITS["sip"]
    assert DataHub(["SPY"], feed="iex").subscription_limit == SUBSCRIPTION_LIMITS["iex"]
    assert DataHub(["SPY"], feed="nonsense").subscription_limit == SUBSCRIPTION_LIMITS["iex"]


def test_system_market_data_card(qtbot):
    from waveapp.ui.system_page import SystemPage

    page = SystemPage()
    qtbot.addWidget(page)
    page.update_system(
        {
            "market_data": {
                "configured_feed": "sip",
                "feed": "sip",
                "stream_live": True,
                "watched": 12,
                "budget": 2000,
                "polygon": "connected",
            }
        }
    )
    assert page.feed_tier_label.text() == "SIP — Algo Trader Plus"
    assert "watching 12" in page.feed_detail_label.text()
    assert "relaunch" not in page.feed_detail_label.text()
    assert "connected" in page.polygon_label.text()

    # configured SIP but still connected on IEX → tells him to relaunch
    page.update_system(
        {
            "market_data": {
                "configured_feed": "sip",
                "feed": "iex",
                "stream_live": True,
                "watched": 2,
                "budget": 30,
                "polygon": "no key in Keychain",
            }
        }
    )
    assert page.feed_tier_label.text() == "IEX — free plan"
    assert "SIP configured — relaunch to apply" in page.feed_detail_label.text()


def test_monitor_snapshot_carries_market_data():
    from types import SimpleNamespace

    from waveapp.engine.connection_monitor import ConnectionMonitor

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._hub = SimpleNamespace(
        feed="sip",
        is_running=True,
        watched={"AAPL", "TSLA"},
        subscription_limit=2000,
        watchdog=SimpleNamespace(age=lambda c: 1.0),
    )
    monitor._polygon_status = "connected"
    data = monitor._system_data()
    market = data["market_data"]
    assert market["feed"] == "sip"
    assert market["stream_live"] is True
    assert market["watched"] == 2
    assert market["budget"] == 2000
    assert market["polygon"] == "connected"


def test_scan_set_progress_card(qtbot):
    from waveapp.ui.system_page import SystemPage

    page = SystemPage()
    qtbot.addWidget(page)
    assert "waiting" in page.scanset_label.text()
    page.set_universe_progress(12, 74, "building today's scan set — batch 12/74")
    assert page.scanset_bar.value() == 16
    assert "batch 12/74" in page.scanset_label.text()
    page.set_universe_progress(1, 1, "scan set ready — 400 symbols")
    assert page.scanset_bar.value() == 100
    assert "ready" in page.scanset_label.text()
    page.set_universe_progress(0, 0, "")
    assert page.scanset_bar.value() == 0


def test_provider_emits_progress_units(monkeypatch, fake_keychain, tmp_path):
    """The universe build reports (done, total) per batch and (1,1) when
    ready — including the same-day-cache path."""
    import json
    import json as _json

    fake_keychain[("Wave", "vault")] = _json.dumps(
        {"alpaca_paper_key_id": "fake-key-id", "alpaca_paper_secret": "fake-secret"}
    )

    from types import SimpleNamespace

    from waveapp.data.features import AlpacaFeatureProvider

    cache = tmp_path / "universe_cache.json"
    cache.write_text(
        json.dumps(
            {
                # the cache is stamped with the ET TRADING day (A4-12), not
                # the UTC date — stamping UTC here made this test fail every
                # evening between 20:00 ET and midnight ET (caught 2026-09-23)
                "date": AlpacaFeatureProvider._trading_day(),
                "feed": "iex",
                "scan_set": ["AAPL", "TSLA"],
                "avg_volume": {"AAPL": 1e6},
                "atr_pct": {"AAPL": 2.0},
            }
        )
    )
    events = []
    provider = AlpacaFeatureProvider(
        [SimpleNamespace(symbol="AAPL", tradable=True, name="Apple")],
        universe_size=10,
        cache_path=cache,
        progress_units_cb=lambda done, total, label: events.append((done, total, label)),
    )
    import asyncio

    asyncio.run(provider._refresh_universe())
    assert events == [(1, 1, "scan set ready — 2 symbols (cache)")]


def test_session_scoreboard_tracks_closed_trades(qtbot):
    from types import SimpleNamespace

    from waveapp.engine.connection_monitor import ConnectionMonitor

    boards = []
    monitor = ConnectionMonitor(on_status=lambda *a: None, on_scoreboard=boards.append)

    def actor(state, strategy, entry, pnl):
        return SimpleNamespace(
            state=SimpleNamespace(value=state),
            spec=SimpleNamespace(strategy=strategy, symbol="X", stop_price=entry),
            avg_entry_price=entry,
            realized_pnl=pnl,
        )

    monitor.engine = SimpleNamespace(
        actors={
            "a1": actor("open", "ORB", 12.0, 0.0),
            "a2": actor("closed", "ORB", 12.0, 150.0),
            "a3": actor("closed", "GAPGO", 22.0, -80.0),
        }
    )
    monitor._track_closed_trades()
    assert len(boards) == 1
    board = boards[0]
    assert board["overall"]["trades"] == 2
    assert board["overall"]["wins"] == 1
    assert board["by_strategy"]["ORB"]["net_pnl"] == 150.0
    assert board["by_bucket"]["$10-15"]["trades"] == 1
    assert board["by_bucket"]["$15+"]["worst"] == -80.0
    # second tick: no NEW closes → no re-push
    monitor._track_closed_trades()
    assert len(boards) == 1

    # the table (now a Performance-tab view, 2026-08-22) renders it
    from waveapp.ui.performance_page import PerformancePage

    page = PerformancePage()
    qtbot.addWidget(page)
    page.update_scoreboard(board)
    table = page.scoreboard_table
    groups = [table.item(r, 0).text() for r in range(table.rowCount())]
    assert groups[0] == "ALL" and "ORB" in groups and "$15+" in groups
    all_row = {c: table.item(0, c).text() for c in range(table.columnCount())}
    assert all_row[1] == "2" and all_row[2] == "50%"  # trades / win rate
    worst_row = groups.index("$15+")
    assert table.item(worst_row, 6).text() == "-80.00"
    page.update_scoreboard({"overall": {}})
    assert table.isHidden() and not page.scoreboard_empty.isHidden()


def test_weekend_poll_throttle():
    """2026-08-22: no API churn while CLOSED — poll drops 30s → 10min."""
    from unittest.mock import patch

    from waveapp.engine.connection_monitor import (
        CLOSED_POLL_SECONDS,
        POLL_SECONDS,
        ConnectionMonitor,
    )
    from waveapp.engine.session import Regime

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    assert monitor._poll_interval() > 0  # no adapter → retry cadence
    monitor._adapter = object()
    with patch("waveapp.engine.session.SessionScheduler.regime", return_value=Regime.CLOSED):
        assert monitor._poll_interval() == CLOSED_POLL_SECONDS
    with patch("waveapp.engine.session.SessionScheduler.regime", return_value=Regime.MIDDAY):
        assert monitor._poll_interval() == POLL_SECONDS


def _vault(fake_keychain: dict) -> dict:
    """Read the single-item VAULT (2026-09-02) the way the app stores it."""
    import json

    return json.loads(fake_keychain.get(("Wave", "vault"), "{}"))
