"""Phase 3: bar building and the heartbeat watchdog (pure, no sockets)."""

from datetime import UTC, datetime

from waveapp.data.hub import BarBuilder, HeartbeatWatchdog


def _ts(minute: int, second: int) -> datetime:
    return datetime(2026, 8, 4, 14, minute, second, tzinfo=UTC)


def test_bar_aggregation_and_close():
    closed: list = []
    builder = BarBuilder(on_bar=closed.append)

    builder.on_trade("AAPL", 190.00, 100, _ts(30, 5))
    builder.on_trade("AAPL", 190.50, 50, _ts(30, 20))
    builder.on_trade("AAPL", 189.80, 200, _ts(30, 55))
    assert closed == []  # bar still open

    builder.on_trade("AAPL", 190.10, 10, _ts(31, 1))  # first trade of next minute
    assert len(closed) == 1
    bar = closed[0]
    assert bar.start == _ts(30, 0)
    assert bar.open == 190.00
    assert bar.high == 190.50
    assert bar.low == 189.80
    assert bar.close == 189.80
    assert bar.volume == 350
    expected_vwap = (190.00 * 100 + 190.50 * 50 + 189.80 * 200) / 350
    assert abs(bar.vwap - expected_vwap) < 1e-9
    assert builder.bars("AAPL") == [bar]


def test_symbols_are_independent():
    builder = BarBuilder()
    builder.on_trade("AAPL", 190, 100, _ts(30, 5))
    builder.on_trade("TSLA", 250, 10, _ts(30, 6))
    builder.on_trade("AAPL", 191, 100, _ts(31, 0))  # closes AAPL only
    assert len(builder.bars("AAPL")) == 1
    assert builder.bars("TSLA") == []


def test_watchdog_stale_transitions_fire_once():
    clock = {"now": 100.0}
    events: list = []
    watchdog = HeartbeatWatchdog(
        threshold_seconds=30,
        time_fn=lambda: clock["now"],
        on_stale_change=lambda ch, stale: events.append((ch, stale)),
    )
    watchdog.beat("trades")
    assert watchdog.check() == {"trades": False}

    clock["now"] = 131.0  # over threshold
    assert watchdog.check() == {"trades": True}
    watchdog.check()  # repeated checks must not re-fire
    assert events == [("trades", True)]

    watchdog.beat("trades")  # recovery
    assert events == [("trades", True), ("trades", False)]
    assert watchdog.check() == {"trades": False}


def test_watchdog_age():
    clock = {"now": 50.0}
    watchdog = HeartbeatWatchdog(time_fn=lambda: clock["now"])
    assert watchdog.age("quotes") is None
    watchdog.beat("quotes")
    clock["now"] = 62.5
    assert watchdog.age("quotes") == 12.5


def test_seed_history_prepends_older_bars_only():
    """2026-08-20: REST backfill seeds the live bar history so compute_atr
    has ≥15 bars immediately — the exit brain's trail was frozen for every
    position's first ~15 minutes on a bloated fallback ATR."""
    from datetime import UTC, datetime, timedelta

    from waveapp.data.hub import Bar, BarBuilder
    from waveapp.engine.exits import compute_atr

    builder = BarBuilder()
    base = datetime(2026, 8, 20, 13, 30, tzinfo=UTC)

    def bar(i, price=100.0):
        return Bar(
            symbol="WMT",
            start=base + timedelta(minutes=i),
            open=price,
            high=price + 0.1,
            low=price - 0.1,
            close=price,
            volume=1000.0,
        )

    # two live bars exist (minutes 20, 21)
    builder.history["WMT"] = __import__("collections").deque([bar(20), bar(21)])
    assert compute_atr(builder.bars("WMT")) is None  # the old blind spot
    # REST backfill: minutes 0..21 — overlap (20, 21) must NOT duplicate
    added = builder.seed_history("WMT", [bar(i) for i in range(22)])
    assert added == 20
    bars = builder.bars("WMT")
    assert len(bars) == 22
    assert [b.start for b in bars] == sorted(b.start for b in bars)
    assert compute_atr(bars) is not None  # ATR available immediately
    # idempotent: seeding again adds nothing
    assert builder.seed_history("WMT", [bar(i) for i in range(22)]) == 0


def test_seed_history_rebuilds_session_vwap():
    """2026-08-26 (AIQ +$3 / DKS +$393 premature momentum exits): a restart
    zeroed the session-VWAP accumulator, so 'the day's VWAP' was a 3-minute
    average and the recross exit hair-triggered. Seeding history must fold
    the pre-restart bars into the VWAP too."""
    from datetime import UTC, datetime

    from waveapp.data.hub import Bar, BarBuilder

    builder = BarBuilder()
    late = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
    # the restarted process saw only one late trade at 125.0
    builder.on_trade("DKS", 125.0, 100, late)
    vwap_before = builder.session_vwap("DKS")
    assert vwap_before == 125.0
    # the seeded morning: heavy volume around 120 — the TRUE day VWAP is low
    morning = [
        Bar(
            symbol="DKS",
            start=datetime(2026, 8, 26, 13, 30 + i, tzinfo=UTC),
            open=120.0,
            high=120.5,
            low=119.5,
            close=120.0,
            volume=10_000,
            vwap_notional=0.0,
        )
        for i in range(5)
    ]
    added = builder.seed_history("DKS", morning)
    assert added == 5
    vwap_after = builder.session_vwap("DKS")
    assert vwap_after is not None and vwap_after < 121.0  # dominated by the morning
    # a fresh trade keeps accumulating on top of the seeded base
    builder.on_trade("DKS", 125.0, 100, late)
    assert builder.session_vwap("DKS") < 121.5


def test_session_vwap_anchors_at_the_930_open():
    """Fidelity fix (2026-08-28): the adoption evidence was earned with an
    RTH-anchored VWAP (the sim replays RTH bars only) — live pre-market
    trades must NOT move the engine's session VWAP."""
    from datetime import UTC, datetime

    from waveapp.data.hub import BarBuilder

    builder = BarBuilder()
    pre = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)  # 8:00 ET — pre-market
    rth = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)  # 10:00 ET
    builder.on_trade("CRM", 240.0, 100_000, pre)  # crash volume, pre-market
    assert builder.session_vwap("CRM") is None  # anchor not reached yet
    builder.on_trade("CRM", 260.0, 100, rth)
    assert abs(builder.session_vwap("CRM") - 260.0) < 0.01  # RTH-only


def test_session_vwap_survives_winter_post_market_utc_midnight():
    """A4-4 (audit 2026-09-22): the accumulator was keyed by the UTC date, so
    in winter (EST) a 19:30-ET post-market print crossed midnight UTC, flipped
    the key and RESET the session VWAP mid-POST — the recross exit then read a
    minutes-old 'day VWAP'. The key must be the ET session date."""
    from datetime import UTC, datetime

    from waveapp.data.hub import BarBuilder

    builder = BarBuilder()
    # 2026-12-15 is EST (UTC-5): 10:00 ET == 15:00 UTC, 19:30 ET == 00:30 UTC Dec 16
    rth = datetime(2026, 12, 15, 15, 0, tzinfo=UTC)
    post = datetime(2026, 12, 16, 0, 30, tzinfo=UTC)
    builder.on_trade("NVDA", 100.0, 1000, rth)
    assert abs(builder.session_vwap("NVDA") - 100.0) < 1e-9
    builder.on_trade("NVDA", 110.0, 1000, post)  # same ET session, next UTC date
    # must ACCUMULATE (→105), not reset to a fresh accumulator (→110)
    assert abs(builder.session_vwap("NVDA") - 105.0) < 1e-9


def test_seed_history_yesterday_evening_does_not_pollute_today():
    """A4-4: an early-morning restart's 12-hour backfill reaches past 20:00 ET
    of YESTERDAY. Those evening bars pass the >=9:30 wall-clock gate and, under
    UTC keying, carried TODAY's date — folding into today's session VWAP. They
    belong to yesterday's ET session and must never touch today's."""
    from datetime import UTC, datetime

    from waveapp.data.hub import Bar, BarBuilder

    def bar(start, price, volume=1000.0):
        return Bar(
            symbol="TSLA",
            start=start,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
        )

    builder = BarBuilder()
    # live: today's (2026-12-16, EST) first RTH trade at 9:31 ET == 14:31 UTC
    builder.on_trade("TSLA", 200.0, 1000, datetime(2026, 12, 16, 14, 31, tzinfo=UTC))
    # backfill: yesterday's post-market 19:00-19:05 ET == 00:00-00:05 UTC TODAY,
    # at a wildly different price, plus today's 9:30 ET bar the stream missed
    evening = [
        bar(datetime(2026, 12, 16, 0, i, tzinfo=UTC), 150.0, volume=50_000) for i in range(5)
    ]
    todays_open = [bar(datetime(2026, 12, 16, 14, 30, tzinfo=UTC), 202.0)]
    added = builder.seed_history("TSLA", evening + todays_open)
    assert added == 6  # all six bars still enter the BAR history
    vwap = builder.session_vwap("TSLA")
    # session VWAP = today's prints only: (200 + 202) / 2 — yesterday's heavy
    # 150.0 evening volume must not drag it
    assert vwap is not None and abs(vwap - 201.0) < 1e-9


def test_seed_history_rebuilds_todays_rth_vwap_in_winter():
    """A4-4 companion: the 2026-08-26 restart-reseed behavior must survive the
    ET-date rekey — seeding TODAY's earlier RTH bars still rebuilds the true
    day VWAP under a live accumulator, in winter (EST) too."""
    from datetime import UTC, datetime

    from waveapp.data.hub import Bar, BarBuilder

    builder = BarBuilder()
    # restarted process saw only one late trade: 11:00 ET Dec 16 == 16:00 UTC
    builder.on_trade("DKS", 125.0, 100, datetime(2026, 12, 16, 16, 0, tzinfo=UTC))
    assert builder.session_vwap("DKS") == 125.0
    # seeded morning: 9:30+ ET == 14:30+ UTC, heavy volume around 120
    morning = [
        Bar(
            symbol="DKS",
            start=datetime(2026, 12, 16, 14, 30 + i, tzinfo=UTC),
            open=120.0,
            high=120.5,
            low=119.5,
            close=120.0,
            volume=10_000,
        )
        for i in range(5)
    ]
    assert builder.seed_history("DKS", morning) == 5
    vwap = builder.session_vwap("DKS")
    assert vwap is not None and vwap < 121.0  # dominated by the seeded morning
    # and a fresh trade keeps accumulating on top of the seeded base
    builder.on_trade("DKS", 125.0, 100, datetime(2026, 12, 16, 16, 1, tzinfo=UTC))
    assert builder.session_vwap("DKS") < 121.5


def test_out_of_order_late_tick_folds_instead_of_destroying_the_bar():
    """A4-10: a trade stamped an EARLIER minute than the in-progress bar used
    to replace it (newer partial lost, stale bar closed out of order). The
    late tick now folds into the current bar; history stays monotone."""
    from datetime import UTC, datetime

    from waveapp.data.hub import BarBuilder

    b = BarBuilder()
    t0 = datetime(2026, 9, 22, 14, 30, 10, tzinfo=UTC)  # 10:30:10 ET
    b.on_trade("SPY", 100.0, 50, t0)
    late = t0.replace(minute=29, second=59)  # previous minute, out of order
    b.on_trade("SPY", 99.5, 25, late)
    cur = b._current["SPY"]
    assert cur.start == t0.replace(second=0, microsecond=0)  # bar survived
    assert cur.low == 99.5 and cur.volume == 75  # late tick folded in
    b.on_trade("SPY", 100.2, 10, t0.replace(minute=31))  # next minute closes it
    hist = list(b.history["SPY"])
    assert [bar.start for bar in hist] == sorted(bar.start for bar in hist)
