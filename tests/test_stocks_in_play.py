"""Phase 10.3: stocks-in-play ranking over grouped dailies."""

from datetime import date, timedelta

from waveapp.research.history import HistoryStore
from waveapp.research.stocks_in_play import (
    download_grouped_daily,
    ensure_daily_schema,
    stocks_in_play,
    trading_days,
)


def _seed_daily(store, symbol, day, volume, close=10.0, open_=None):
    ensure_daily_schema(store)
    store._conn.execute(
        "INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?)",
        (symbol, day.isoformat(), open_ or close, close + 0.5, close - 0.5, close, volume),
    )
    store._conn.commit()


def _seed_history(store, symbol, target_day, base_volume, sessions=15, close=10.0):
    for i in range(1, sessions + 1):
        day = target_day - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        _seed_daily(store, symbol, day, base_volume, close=close)


def test_rvol_ranking_floors_and_gap(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    target = date(2026, 3, 10)  # a Tuesday
    # BURST: 3x volume today + gap up from 10.0 → 11.0 open
    _seed_history(store, "BURST", target, base_volume=1_000_000)
    _seed_daily(store, "BURST", target, volume=3_000_000, close=11.5, open_=11.0)
    # SLEEPY: normal volume → excluded by min_rvol
    _seed_history(store, "SLEEPY", target, base_volume=1_000_000)
    _seed_daily(store, "SLEEPY", target, volume=1_100_000)
    # CHEAP: huge rvol but $1 stock → price floor
    _seed_history(store, "CHEAP", target, base_volume=1_000_000, close=1.0)
    _seed_daily(store, "CHEAP", target, volume=9_000_000, close=1.0)
    # THIN: huge rvol but tiny average volume → volume floor
    _seed_history(store, "THIN", target, base_volume=50_000)
    _seed_daily(store, "THIN", target, volume=500_000)

    names = stocks_in_play(store, target, top_n=5)
    assert [n.symbol for n in names] == ["BURST"]
    burst = names[0]
    assert 2.9 < burst.rvol < 3.1
    assert 9.0 < burst.gap_pct < 11.0  # 10.0 → 11.0 open


def test_top_n_orders_by_rvol(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    target = date(2026, 3, 10)
    for symbol, mult in (("AAA", 4.0), ("BBB", 6.0), ("CCC", 2.5)):
        _seed_history(store, symbol, target, base_volume=1_000_000)
        _seed_daily(store, symbol, target, volume=int(1_000_000 * mult))
    names = stocks_in_play(store, target, top_n=2)
    assert [n.symbol for n in names] == ["BBB", "AAA"]


def test_insufficient_history_excluded(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    target = date(2026, 3, 10)
    _seed_history(store, "NEWIPO", target, base_volume=1_000_000, sessions=5)  # < 10 days
    _seed_daily(store, "NEWIPO", target, volume=8_000_000)
    assert stocks_in_play(store, target) == []


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, results_by_day):
        self.results_by_day = results_by_day
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        day = url.split("/stocks/")[1].split("?")[0]
        return _FakeResponse({"results": self.results_by_day.get(day, [])})


def test_grouped_download_incremental_and_skips_weekends(tmp_path):
    from waveapp.research.history import PolygonClient

    store = HistoryStore(tmp_path / "h.db")
    monday, friday = date(2026, 3, 9), date(2026, 3, 13)
    http = _FakeHttp(
        {
            "2026-03-09": [{"T": "AAPL", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 5000}],
            "2026-03-10": [
                {"T": "AAPL", "o": 10, "h": 11, "l": 9, "c": 10.6, "v": 6000},
                {"T": "TOOLONGNAME", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
            ],
        }
    )
    client = PolygonClient(api_key="fake-polygon-key", http=http)
    fetched = download_grouped_daily(monday, friday + timedelta(days=2), store, client)
    assert fetched == 5  # Mon-Fri requested; Sat+Sun never hit the API
    assert all("/stocks/2026-03-1" in u or "/stocks/2026-03-09" in u for u in http.urls)
    assert trading_days(store, monday, friday) == [monday, date(2026, 3, 10)]
    # junk tickers filtered
    row = store._conn.execute(
        "SELECT COUNT(*) FROM daily_bars WHERE symbol='TOOLONGNAME'"
    ).fetchone()
    assert row[0] == 0
    # second run: everything covered → zero new requests
    before = len(http.urls)
    assert download_grouped_daily(monday, friday, store, client) == 0
    assert len(http.urls) == before


def test_progress_file_roundtrip_and_staleness(tmp_path, monkeypatch):
    import waveapp.research.progress as progress

    monkeypatch.setattr(progress, "progress_path", lambda: tmp_path / "progress.json")
    assert progress.read() is None
    progress.report("minutes", 40, 200, "minute bars 40/200")
    data = progress.read()
    assert data["done"] == 40 and data["total"] == 200
    # stale unfinished report disappears…
    assert progress.read(now=data["ts"] + 301) is None
    # …but a FINISHED marker stays visible forever
    progress.finish("ORB in-play DONE — best +0.12R")
    stale_later = progress.read(now=progress.read()["ts"] + 10_000)
    assert stale_later is not None and stale_later["finished"] is True


def test_research_card_states(qtbot, tmp_path, monkeypatch):
    import waveapp.research.progress as progress

    from waveapp.ui.system_page import SystemPage

    monkeypatch.setattr(progress, "progress_path", lambda: tmp_path / "progress.json")
    page = SystemPage()
    qtbot.addWidget(page)
    page._refresh_research()
    assert "no research running" in page.research_label.text()
    progress.report("minutes", 50, 200, "ORB in-play: minute bars 50/200 symbols")
    page._refresh_research()
    assert page.research_bar.value() == 25
    assert "50/200" in page.research_label.text()
    # a stage reporting 1/1 while RUNNING must never fill the bar
    progress.report("select", 1, 1, "ORB in-play: selecting names…")
    page._refresh_research()
    assert page.research_bar.value() == 99
    progress.finish("ORB in-play DONE — best +0.10R over 900 trades")
    page._refresh_research()
    assert page.research_bar.value() == 100
    assert "DONE" in page.research_label.text()


def test_premarket_volume_window(tmp_path):
    from datetime import date as _date

    import pandas as pd
    from waveapp.research.history import MinuteBar
    from waveapp.research.stocks_in_play import premarket_volume

    store = HistoryStore(tmp_path / "h.db")
    day = _date(2026, 3, 10)
    # bars at 08:00 (pre-market), 09:29 (pre-market), 09:30 (RTH — excluded)
    stamps = [
        pd.Timestamp(f"2026-03-10 {t}", tz="America/New_York") for t in ("08:00", "09:29", "09:30")
    ]
    store.save_bars(
        [
            MinuteBar("GAPR", int(ts.timestamp() * 1000), 10, 10, 10, 10, vol, None, 1)
            for ts, vol in zip(stamps, (40_000, 70_000, 999_999), strict=True)
        ]
    )
    assert premarket_volume(store, "GAPR", day) == 110_000


def test_premarket_rerank_drops_quiet_names(tmp_path):
    from datetime import date as _date

    import pandas as pd
    from waveapp.research.history import MinuteBar
    from waveapp.research.stocks_in_play import InPlayName, stocks_in_play_premarket

    store = HistoryStore(tmp_path / "h.db")
    day = _date(2026, 3, 10)
    ts = int(pd.Timestamp("2026-03-10 08:30", tz="America/New_York").timestamp() * 1000)
    # LOUD: 600k pre-market shares; QUIET: 20k (below the 100k floor)
    store.save_bars([MinuteBar("LOUD", ts, 10, 10, 10, 10, 600_000, None, 1)])
    store.save_bars([MinuteBar("QUIET", ts, 10, 10, 10, 10, 20_000, None, 1)])
    pool = [
        InPlayName("QUIET", rvol=9.0, gap_pct=5.0, price=10.0, avg_volume=1_000_000),
        InPlayName("LOUD", rvol=2.0, gap_pct=3.0, price=10.0, avg_volume=1_000_000),
    ]
    names = stocks_in_play_premarket(store, day, pool, top_n=5)
    assert [n.symbol for n in names] == ["LOUD"]  # QUIET's full-day rank is irrelevant
    assert names[0].rvol == 0.6  # 600k / 1M avg daily — the rank actually used


def test_leveraged_classifier():
    from waveapp.instruments import is_leveraged_name, leveraged_cap

    assert is_leveraged_name("ProShares UltraPro QQQ")
    assert is_leveraged_name("Direxion Daily TSLA Bull 2X Shares")
    assert is_leveraged_name("GraniteShares 2x Long PLTR Daily ETF")
    assert is_leveraged_name("T-Rex 2X Inverse NVIDIA Daily Target ETF")
    assert is_leveraged_name("MicroSectors FANG+ 3X Leveraged ETN")
    assert not is_leveraged_name("Apple Inc.")
    assert not is_leveraged_name("SPDR S&P 500 ETF Trust")
    assert not is_leveraged_name("Vanguard Total Bond Market ETF")
    assert not is_leveraged_name(None)
    assert leveraged_cap(20) == 5
    assert leveraged_cap(4) == 1
    assert leveraged_cap(1) == 1


def test_premarket_selection_caps_leveraged(tmp_path):
    from datetime import date as _date

    import pandas as pd
    from waveapp.research.history import MinuteBar
    from waveapp.research.stocks_in_play import (
        InPlayName,
        ensure_meta_schema,
        stocks_in_play_premarket,
    )

    store = HistoryStore(tmp_path / "h.db")
    ensure_meta_schema(store)
    day = _date(2026, 3, 10)
    ts = int(pd.Timestamp("2026-03-10 08:30", tz="America/New_York").timestamp() * 1000)
    pool = []
    for i, symbol in enumerate(("LEV1", "LEV2", "LEV3", "PLAIN1", "PLAIN2")):
        # all loud pre-market; leveraged ones loudest (they'd sweep the top)
        store.save_bars([MinuteBar(symbol, ts, 10, 10, 10, 10, 900_000 - i * 100_000, None, 1)])
        pool.append(InPlayName(symbol, rvol=3.0, gap_pct=2.0, price=10.0, avg_volume=1_000_000))
        is_lev = 1 if symbol.startswith("LEV") else 0
        store._conn.execute(
            "INSERT OR REPLACE INTO symbols_meta VALUES (?,?,?,?)",
            (symbol, f"{symbol} fund", "ETF", is_lev),
        )
    store._conn.commit()

    picks = stocks_in_play_premarket(store, day, pool, top_n=4)
    symbols = [p.symbol for p in picks]
    # pool has only 2 ordinary names: 1 leveraged (cap) + both plains = 3
    assert sum(1 for s in symbols if s.startswith("LEV")) == 1  # cap = 25% of 4
    assert "PLAIN1" in symbols and "PLAIN2" in symbols
    assert len(symbols) == 3


def test_scanner_caps_leveraged_products():
    import asyncio

    from waveapp.engine.scanner import Scanner, SymbolFeatures
    from waveapp.engine.tradegate import TradeGate

    def feature(symbol, leveraged, rvol=5.0):
        return SymbolFeatures(
            symbol=symbol,
            price=50.0,
            prev_close=48.0,
            gap_pct=4.0,
            rvol=rvol,
            atr_pct=4.0,
            spread=0.01,
            day_volume=5e6,
            avg_daily_volume=1e6,
            leveraged=leveraged,
        )

    class _Provider:
        async def fetch(self):
            # leveraged names rank loudest — without the cap they'd sweep
            return [
                feature("LEVA", True, rvol=9.0),
                feature("LEVB", True, rvol=8.5),
                feature("LEVC", True, rvol=8.0),
                feature("AAA", False, rvol=7.0),
                feature("BBB", False, rvol=6.0),
            ]

    scanner = Scanner(provider=_Provider(), gate=TradeGate(), top_n=4)
    results = asyncio.run(scanner.scan_once())
    accepted = [c.features.symbol for c in results if c.accepted]
    capped = [c for c in results if c.reject_reason == "leveraged-product concentration cap"]
    assert sum(1 for s in accepted if s.startswith("LEV")) == 1  # cap = 25% of 4
    assert len(capped) == 2  # LEVB, LEVC pushed out
    assert "AAA" in accepted and "BBB" in accepted
