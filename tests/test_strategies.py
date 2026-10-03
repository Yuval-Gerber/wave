"""Phase 7.2: the three §8.1 strategies + the entry pipeline. Pure inputs,
no network."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from waveapp.broker.base import OrderSide
from waveapp.data.hub import Bar, DataHub
from waveapp.engine.scanner import SymbolFeatures
from waveapp.engine.session import ET, Regime
from waveapp.engine.strategies import (
    GapAndGoStrategy,
    ORB5Strategy,
    VWAPRegimeStrategy,
    armed_strategies,
)


def _features(**overrides) -> SymbolFeatures:
    base = dict(
        symbol="HOT",
        price=50.0,
        prev_close=50.0,
        gap_pct=0.0,
        rvol=3.0,
        atr_pct=2.0,
        spread=0.02,
        day_volume=2_000_000,
        avg_daily_volume=2_000_000,
        shortable=True,
    )
    base.update(overrides)
    return SymbolFeatures(**base)


def _bar_at(hour, minute, close, high=None, low=None, volume=5000.0) -> Bar:
    start = datetime(2026, 8, 14, hour, minute, tzinfo=ET)
    return Bar(
        symbol="HOT",
        start=start,
        open=close,
        high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05,
        close=close,
        volume=volume,
        vwap_notional=close * volume,
    )


def _opening_range_bars(low=49.5, high=50.5):
    """09:30–09:34 range bars + later bars appended by tests."""
    return [
        _bar_at(9, 30, 50.0, high=high, low=low),
        _bar_at(9, 31, 50.2, high=high - 0.1, low=low + 0.1),
        _bar_at(9, 32, 49.9, high=high - 0.2, low=low + 0.05),
        _bar_at(9, 33, 50.1),
        _bar_at(9, 34, 50.0),
    ]


# -- ORB-5 -------------------------------------------------------------------


def test_orb_long_breakout():
    bars = _opening_range_bars() + [_bar_at(9, 40, 50.60)]
    signal = ORB5Strategy().evaluate(_features(), bars, 50.0, Regime.OPEN_DRIVE)
    assert signal is not None
    assert signal.side is OrderSide.BUY
    assert signal.stop_price == 49.5  # opposite extreme of the range
    assert "breakout" in signal.reason


def test_orb_short_breakdown_stands_down():
    """2026-08-21 (GBTC short at the bell): the adopted champion is
    LONG-ONLY — breakdowns below the range no longer enter."""
    bars = _opening_range_bars() + [_bar_at(9, 40, 49.30)]
    signal = ORB5Strategy().evaluate(_features(), bars, 50.0, Regime.OPEN_DRIVE)
    assert signal is None


def test_orb_no_signal_inside_range_or_wrong_regime():
    bars = _opening_range_bars() + [_bar_at(9, 40, 50.10)]
    strategy = ORB5Strategy()
    assert strategy.evaluate(_features(), bars, 50.0, Regime.OPEN_DRIVE) is None  # inside
    breakout = _opening_range_bars() + [_bar_at(9, 40, 50.60)]
    # sim fidelity (2026-08-25): no rvol gate at signal time — the day list
    # carries stock selection — and ORB is armed ALL DAY (midday included)
    assert strategy.evaluate(_features(rvol=1.0), breakout, 50.0, Regime.OPEN_DRIVE) is not None
    assert strategy.evaluate(_features(), breakout, 50.0, Regime.MIDDAY) is not None
    assert strategy.evaluate(_features(), breakout, 50.0, Regime.PRE) is None
    assert strategy.evaluate(_features(), breakout, 50.0, Regime.POST) is None


# -- Gap-and-Go --------------------------------------------------------------


def test_gap_and_go_long_above_vwap():
    features = _features(gap_pct=4.5, rvol=4.0)
    bars = [_bar_at(9, 45, 52.0)]
    signal = GapAndGoStrategy().evaluate(features, bars, 51.5, Regime.OPEN_DRIVE)
    assert signal is not None and signal.side is OrderSide.BUY
    assert signal.stop_price < 52.0
    assert "Gap-and-Go" in signal.reason


def test_gap_and_go_requirements():
    strategy = GapAndGoStrategy()
    bars = [_bar_at(9, 45, 52.0)]
    assert (
        strategy.evaluate(_features(gap_pct=1.0, rvol=4.0), bars, 51.5, Regime.OPEN_DRIVE) is None
    )
    assert (
        strategy.evaluate(_features(gap_pct=4.0, rvol=1.0), bars, 51.5, Regime.OPEN_DRIVE) is None
    )
    small = _features(gap_pct=4.0, rvol=4.0, day_volume=50_000)
    assert strategy.evaluate(small, bars, 51.5, Regime.OPEN_DRIVE) is None
    # below VWAP on a gap-up = failed continuation → no entry
    assert (
        strategy.evaluate(_features(gap_pct=4.0, rvol=4.0), bars, 52.5, Regime.OPEN_DRIVE) is None
    )
    # gap-down short: stands down — the adopted champion is LONG-ONLY
    # (2026-08-20; shorts return only via the §11 pipeline)
    down = strategy.evaluate(
        _features(gap_pct=-4.0, rvol=4.0), [_bar_at(9, 45, 48.0)], 48.5, Regime.OPEN_DRIVE
    )
    assert down is None


# -- VWAP regime-switch (Phase 10 champion config, 2026-08-19) ----------------

_AT_11 = datetime(2026, 8, 14, 11, 0, tzinfo=ET)


def test_vwap_trend_day_pullback_long():
    features = _features(gap_pct=3.0, rvol=2.5)  # gap ≥ 2.5% → trend day
    bars = [_bar_at(11, 0, 51.51)]
    signal = VWAPRegimeStrategy().evaluate(features, bars, 51.50, Regime.MIDDAY, now=_AT_11)
    assert signal is not None and signal.side is OrderSide.BUY
    assert "trend-day pullback" in signal.reason
    # champion stop geometry: VWAP − 1.25 × daily ATR (2% of 50 = 1.0)
    assert signal.stop_price == 50.25


def test_vwap_champion_gates():
    strategy = VWAPRegimeStrategy()
    bars = [_bar_at(11, 0, 51.51)]
    # gap below 2.5% → not a trend day → stand down (range fades were never
    # validated and no longer trade)
    assert strategy.evaluate(_features(gap_pct=1.0), bars, 51.50, Regime.MIDDAY, now=_AT_11) is None
    # before 10:00 ET the VWAP hasn't settled → stand down
    early = datetime(2026, 8, 14, 9, 45, tzinfo=ET)
    good = _features(gap_pct=3.0)
    early_bars = [_bar_at(9, 45, 51.51)]
    assert strategy.evaluate(good, early_bars, 51.50, Regime.OPEN_DRIVE, now=early) is None
    # stretched far above VWAP (> 0.6 × daily ATR) → not a pullback
    stretched = [_bar_at(11, 0, 52.3)]
    assert strategy.evaluate(good, stretched, 51.50, Regime.MIDDAY, now=_AT_11) is None


def test_vwap_half_size_in_lull_and_trend_break_stand_down():
    features = _features(gap_pct=3.0, rvol=2.5)
    bars = [_bar_at(12, 0, 51.51)]
    signal = VWAPRegimeStrategy().evaluate(
        features, bars, 51.50, Regime.MIDDAY, is_lull=True, now=_AT_11
    )
    assert signal is not None and signal.half_size is True
    # uptrend day but price BELOW vwap → trend broken → stand down
    below = [_bar_at(12, 0, 51.0)]
    assert VWAPRegimeStrategy().evaluate(features, below, 51.50, Regime.MIDDAY, now=_AT_11) is None


# -- arming ------------------------------------------------------------------


def test_arming_by_regime():
    open_names = {s.name for s in armed_strategies(Regime.OPEN_DRIVE)}
    assert open_names == {"ORB", "GAP", "VWAP", "FPB"}  # FPB adopted 2026-09-01
    midday_names = {s.name for s in armed_strategies(Regime.MIDDAY)}
    assert midday_names == {"ORB", "VWAP", "FPB"}  # ORB all day (sim fidelity, 8-25)
    pre_names = {s.name for s in armed_strategies(Regime.PRE)}
    assert pre_names == {"GAP"}
    assert armed_strategies(Regime.CLOSED) == []


# -- dynamic subscriptions ---------------------------------------------------


async def test_hub_watch_respects_budget():
    """8.8 r5: the budget counts CHANNEL-symbol pairs (Alpaca Basic ~30),
    not symbols — the old symbol cap caused 405 spam."""
    from waveapp.data.hub import BASE_CHANNELS, DYNAMIC_CHANNELS, SUBSCRIPTION_LIMIT

    hub = DataHub(["SPY"])
    added = await hub.watch(["AAPL", "TSLA", "aapl"])  # dedupe + case
    assert added == ["AAPL", "TSLA"]
    many = [f"S{i:03d}" for i in range(40)]
    await hub.watch(many)
    dynamic = len(hub.watched) - 1  # SPY is the base
    assert 1 * BASE_CHANNELS + dynamic * DYNAMIC_CHANNELS <= SUBSCRIPTION_LIMIT
    assert dynamic == (SUBSCRIPTION_LIMIT - BASE_CHANNELS) // DYNAMIC_CHANNELS


# -- entry pipeline ----------------------------------------------------------


def _pipeline_monitor(auto_trade: bool, monkeypatch, tmp_path):
    from waveapp.config import AppConfig
    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.engine.risk import RiskEngine
    from waveapp.engine.scanner import Candidate, Scanner
    from waveapp.engine.tradegate import TradeGate

    config_path = tmp_path / "config.toml"
    config = AppConfig(auto_trade=auto_trade)
    config.save(config_path)
    monkeypatch.setattr("waveapp.config.config_path", lambda: config_path)

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

    features = _features(symbol="HOT", gap_pct=4.5, rvol=4.0)
    # seed live data: price above vwap for a gap-up continuation
    monitor._hub.bar_builder.on_trade("HOT", 51.5, 3000, datetime(2026, 8, 14, 9, 44, tzinfo=ET))
    monitor._hub.bar_builder.on_trade("HOT", 52.0, 3000, datetime(2026, 8, 14, 9, 45, tzinfo=ET))
    monitor._hub.bar_builder.on_trade("HOT", 52.0, 10, datetime(2026, 8, 14, 9, 46, tzinfo=ET))
    # live quote: the no-quote deferral (2026-09-16, the cold-restart chase)
    # holds entries until the stream is warm — the fixture's stream is warm,
    # and the spread is a tight 2¢ (the cost gate now judges the LIVE
    # spread, and 15% of the $0.21 target caps it at ~3¢). A4-7: a warm
    # quote must carry a fresh tz-aware timestamp.
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


async def test_pipeline_signal_only_when_auto_trade_off(monkeypatch, tmp_path):
    from waveapp.engine.session import Regime, SessionInfo

    monitor, candidates = _pipeline_monitor(False, monkeypatch, tmp_path)
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
    await monitor._entry_pipeline(candidates)
    monitor._push.assert_awaited_once()
    assert "auto-trade OFF" in monitor._push.await_args.args[0]
    monitor.engine.open_position.assert_not_awaited()


async def test_pipeline_enters_when_auto_trade_on(monkeypatch, tmp_path):
    from waveapp.engine.session import Regime, SessionInfo

    monitor, candidates = _pipeline_monitor(True, monkeypatch, tmp_path)
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
    await monitor._entry_pipeline(candidates)
    monitor.engine.open_position.assert_awaited_once()
    spec = monitor.engine.open_position.await_args.args[0]
    assert spec.symbol == "HOT" and spec.qty >= 1
    assert spec.stop_price < 52.0
    # dedupe: same signal does not fire twice in a session
    await monitor._entry_pipeline(candidates)
    assert monitor.engine.open_position.await_count == 1


async def test_pipeline_blocks_entries_outside_rth(monkeypatch, tmp_path):
    """24/7 runtime (2026-08-19): scanning never stops, but ENTRIES
    only fire 09:30–16:00 ET — a 08:00 PRE-market GAPGO signal must not
    queue an unvalidated market-on-open order."""
    from waveapp.engine.session import Regime, SessionInfo

    monitor, candidates = _pipeline_monitor(True, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "waveapp.engine.session.SessionScheduler.info",
        staticmethod(
            lambda now=None: SessionInfo(
                regime=Regime.PRE,
                is_lull=False,
                et_time=datetime(2026, 8, 14, 8, 0, tzinfo=ET),
            )
        ),
    )
    await monitor._entry_pipeline(candidates)
    monitor.engine.open_position.assert_not_awaited()
    monitor._push.assert_not_awaited()  # no signal alert either — fully paused


# -- 4. First pullback (blueprint 5.1, adopted on the 4/4 sweep) -------------


def _fpb_bar(minute, open_, close, high=None, low=None):
    start = datetime(2026, 8, 14, 10, minute, tzinfo=ET)
    return Bar(
        symbol="HOT",
        start=start,
        open=open_,
        high=high if high is not None else max(open_, close) + 0.03,
        low=low if low is not None else min(open_, close) - 0.03,
        close=close,
        volume=5000.0,
        vwap_notional=close * 5000.0,
    )


def _fpb_setup(reds=2, deep=False, lose_vwap=False):
    """A leader up ~4% (open 50 → ~52) that pulls back `reds` candles and
    fires a green break. VWAP pinned at 50.8 (below the pullback)."""
    bars = [
        _fpb_bar(0, 50.0, 51.0, high=51.1, low=49.9),
        _fpb_bar(1, 51.0, 52.0, high=52.1, low=50.9),  # HOD 52.1, leg 2.1
    ]
    lows = [51.6, 51.4, 51.2]
    if deep:
        lows = [50.6, 50.5, 50.4]  # gives back > half the leg
    for i in range(reds):
        close = (lows[i] + 0.05) if not lose_vwap else 50.5
        bars.append(_fpb_bar(2 + i, close + 0.2, close, low=lows[i]))
    prev_high = bars[-1].high
    bars.append(_fpb_bar(2 + reds, bars[-1].close, bars[-1].close + 0.5, high=prev_high + 0.2))
    return bars


def test_first_pullback_fires_on_the_textbook_shape():
    from waveapp.engine.strategies import FirstPullbackStrategy

    strategy = FirstPullbackStrategy()
    bars = _fpb_setup(reds=2)
    now = datetime(2026, 8, 14, 10, 5, tzinfo=ET)
    signal = strategy.evaluate(
        _features(day_open=50.0), bars, session_vwap=50.8, regime=Regime.MIDDAY, now=now
    )
    assert signal is not None
    assert signal.strategy == "FPB"
    assert signal.side is OrderSide.BUY
    assert signal.stop_price == 51.4  # the pullback low
    assert "first pullback" in signal.reason


def test_first_pullback_refuses_bad_shapes():
    from waveapp.engine.strategies import FirstPullbackStrategy

    strategy = FirstPullbackStrategy()
    now = datetime(2026, 8, 14, 10, 5, tzinfo=ET)
    feats = _features(day_open=50.0)
    # deep retrace (> half the leg) — not the clean form
    assert strategy.evaluate(feats, _fpb_setup(deep=True), 50.8, Regime.MIDDAY, now=now) is None
    # pullback lost VWAP
    assert (
        strategy.evaluate(feats, _fpb_setup(lose_vwap=True), 50.8, Regime.MIDDAY, now=now) is None
    )
    # outside the 9:45-11:00 window
    late = datetime(2026, 8, 14, 12, 0, tzinfo=ET)
    assert strategy.evaluate(feats, _fpb_setup(), 50.8, Regime.MIDDAY, now=late) is None
    # weak day (< +2%)
    assert (
        strategy.evaluate(_features(day_open=51.9), _fpb_setup(), 50.8, Regime.MIDDAY, now=now)
        is None
    )


def test_first_pullback_needs_one_to_three_reds():
    from waveapp.engine.strategies import FirstPullbackStrategy

    strategy = FirstPullbackStrategy()
    now = datetime(2026, 8, 14, 10, 8, tzinfo=ET)
    feats = _features(day_open=50.0)
    # no pullback at all (green into green) → no chase
    bars = [
        _fpb_bar(0, 50.0, 51.0),
        _fpb_bar(1, 51.0, 52.0, high=52.1),
        _fpb_bar(2, 52.0, 52.5, high=52.6),
    ]
    assert strategy.evaluate(feats, bars, 50.8, Regime.MIDDAY, now=now) is None
    # a 2-red textbook shape does fire
    assert strategy.evaluate(feats, _fpb_setup(reds=2), 50.8, Regime.MIDDAY, now=now) is not None


def test_first_pullback_is_armed_in_the_pipeline():
    from waveapp.engine.strategies import FirstPullbackStrategy

    names = [type(s).__name__ for s in armed_strategies(Regime.MIDDAY)]
    assert "FirstPullbackStrategy" in names
    # champion strategies keep priority — FPB evaluates last
    assert names.index("FirstPullbackStrategy") == len(names) - 1
    assert Regime.POWER_HOUR not in FirstPullbackStrategy.arms_in  # window is the morning


# -- S3: the short side (every SELL gated on shorts_enabled) -----------------


def _shorts_on(monkeypatch, tmp_path):
    """Write shorts_enabled=true to a tmp config — the strategies read the
    flag live via AppConfig.load(), same path as the climber knobs."""
    from waveapp.config import AppConfig

    path = tmp_path / "config.toml"
    AppConfig(shorts_enabled=True).save(path)
    monkeypatch.setattr("waveapp.config.config_path", lambda: path)


def _shorts_off(monkeypatch, tmp_path):
    from waveapp.config import AppConfig

    path = tmp_path / "config.toml"
    AppConfig().save(path)  # defaults: shorts_enabled=False
    monkeypatch.setattr("waveapp.config.config_path", lambda: path)


def test_orb_short_breakdown_emits_sell_when_enabled(monkeypatch, tmp_path):
    """§8.1(1) mirror: break BELOW the 5-min range low → SELL, stop at the
    range HIGH (above entry). Flag off → the GBTC stand-down holds."""
    bars = _opening_range_bars() + [_bar_at(9, 40, 49.30)]
    _shorts_off(monkeypatch, tmp_path)
    assert ORB5Strategy().evaluate(_features(), bars, 50.0, Regime.OPEN_DRIVE) is None
    _shorts_on(monkeypatch, tmp_path)
    signal = ORB5Strategy().evaluate(_features(), bars, 50.0, Regime.OPEN_DRIVE)
    assert signal is not None and signal.side is OrderSide.SELL
    assert signal.stop_price == 50.5  # the opposite extreme — ABOVE entry
    assert signal.stop_price > signal.entry_price
    assert "breakdown (short)" in signal.reason


def test_gap_down_and_go_short_when_enabled(monkeypatch, tmp_path):
    """§8.1(3) mirror: gap ≤ −3%, rvol, premarket volume, continuation
    BELOW VWAP → SELL with the stop ABOVE entry. Flag off → stands down."""
    features = _features(gap_pct=-4.0, rvol=4.0)
    bars = [_bar_at(9, 45, 48.0)]
    _shorts_off(monkeypatch, tmp_path)
    assert GapAndGoStrategy().evaluate(features, bars, 48.5, Regime.OPEN_DRIVE) is None
    _shorts_on(monkeypatch, tmp_path)
    signal = GapAndGoStrategy().evaluate(features, bars, 48.5, Regime.OPEN_DRIVE)
    assert signal is not None and signal.side is OrderSide.SELL
    # stop = price + 1×daily ATR (2% of 50 = 1.0) — ABOVE entry
    assert signal.stop_price == 49.0 and signal.stop_price > signal.entry_price
    assert "below" in signal.reason
    # failed continuation mirror: price back ABOVE VWAP on a gap-down → None
    assert GapAndGoStrategy().evaluate(features, bars, 47.5, Regime.OPEN_DRIVE) is None


def test_vwap_trend_day_pullback_short_when_enabled(monkeypatch, tmp_path):
    """§8.1(2) mirror: gap-DOWN trend day, price below VWAP pulling back
    toward it from below → SELL, stop ABOVE VWAP. Flag off → None."""
    features = _features(gap_pct=-3.0, rvol=2.5)
    bars = [_bar_at(11, 0, 51.49)]
    _shorts_off(monkeypatch, tmp_path)
    assert VWAPRegimeStrategy().evaluate(features, bars, 51.50, Regime.MIDDAY, now=_AT_11) is None
    _shorts_on(monkeypatch, tmp_path)
    signal = VWAPRegimeStrategy().evaluate(features, bars, 51.50, Regime.MIDDAY, now=_AT_11)
    assert signal is not None and signal.side is OrderSide.SELL
    # champion geometry mirrored: VWAP + 1.25 × daily ATR (2% of 50 = 1.0)
    assert signal.stop_price == 52.75 and signal.stop_price > signal.entry_price
    assert "(short)" in signal.reason
    # lull → half size, like the long
    lull = VWAPRegimeStrategy().evaluate(
        features, bars, 51.50, Regime.MIDDAY, is_lull=True, now=_AT_11
    )
    assert lull is not None and lull.half_size is True
    # downtrend broken (price above VWAP) → stand down
    above = [_bar_at(11, 0, 51.60)]
    assert VWAPRegimeStrategy().evaluate(features, above, 51.50, Regime.MIDDAY, now=_AT_11) is None
    # stretched far below VWAP (> 0.6 × daily ATR) → not a pullback
    stretched = [_bar_at(11, 0, 50.70)]
    assert (
        VWAPRegimeStrategy().evaluate(features, stretched, 51.50, Regime.MIDDAY, now=_AT_11) is None
    )


# -- S3 entry-pipeline mirrors ------------------------------------------------


def _short_pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True, price=48.0, day_open=50.5):
    """A gap-DOWN candidate with warm bars, price below session VWAP and a
    tight live quote — the GAP short signal through the real
    Scanner/RiskEngine/TradeGate/_execute_signal path."""
    from waveapp.config import AppConfig
    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.engine.risk import RiskEngine, RiskLimits
    from waveapp.engine.scanner import Candidate, Scanner
    from waveapp.engine.tradegate import TradeGate

    config_path = tmp_path / "config.toml"
    # sniper_book off: these tests exercise the S3 short-mirror mechanics on
    # deliberately EXTENDED tapes (−4.9% / −6.9% vs open) — with the R2
    # sniper book armed those signals correctly become FPB-short watches
    # instead of executing (covered in test_sniper.py); opt out here.
    AppConfig(auto_trade=True, shorts_enabled=shorts_enabled, sniper_book=False).save(config_path)
    monkeypatch.setattr("waveapp.config.config_path", lambda: config_path)
    from waveapp.engine.session import Regime, SessionInfo

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
        risk=RiskEngine(RiskLimits(shorts_enabled=shorts_enabled)),
        open_position=AsyncMock(return_value=SimpleNamespace(position_key="k")),
        state=SimpleNamespace(value="running"),
    )
    monitor._adapter = SimpleNamespace(
        get_account=AsyncMock(return_value=SimpleNamespace(equity=100_000.0, cash=100_000.0)),
        asset_shortable=AsyncMock(return_value=True),  # S0 ETB cache stand-in
    )
    monitor._hub = DataHub(["SPY"])
    monitor._scanner = Scanner(provider=None, gate=TradeGate(), database=None)
    monitor._push = AsyncMock()
    features = _features(symbol="WEAK", price=price, gap_pct=-4.5, rvol=4.0, day_open=day_open)
    # gap-down continuation tape: closes BELOW the session VWAP
    monitor._hub.bar_builder.on_trade("WEAK", 48.6, 3000, datetime(2026, 8, 14, 9, 44, tzinfo=ET))
    monitor._hub.bar_builder.on_trade("WEAK", 48.0, 3000, datetime(2026, 8, 14, 9, 45, tzinfo=ET))
    monitor._hub.bar_builder.on_trade("WEAK", 48.0, 10, datetime(2026, 8, 14, 9, 46, tzinfo=ET))
    from datetime import UTC as _UTC

    monitor._hub.latest_quotes["WEAK"] = SimpleNamespace(
        bid_price=47.99, ask_price=48.01, timestamp=datetime.now(_UTC)
    )
    candidate = Candidate(
        features=features,
        score=6.0,
        strategy_scores={"GAP": 6.0},
        best_strategy="GAP",
        accepted=True,
    )
    return monitor, [candidate]


async def test_pipeline_short_entry_end_to_end_and_gate_gets_etb_flags(monkeypatch, tmp_path):
    """Flag ON: the GAP short flows through every real gate to the engine —
    and the TradeGate call receives shortable/easy_to_borrow from
    adapter.asset_shortable plus the SELL side (borrow fee applies)."""
    from waveapp.broker.base import OrderSide as _OS

    monitor, candidates = _short_pipeline_monitor(monkeypatch, tmp_path)
    seen = []
    gate = monitor._scanner.gate
    orig = gate.evaluate

    def spy(inputs):
        seen.append(inputs)
        return orig(inputs)

    monkeypatch.setattr(gate, "evaluate", spy)
    await monitor._entry_pipeline(candidates)
    monitor.engine.open_position.assert_awaited_once()
    spec = monitor.engine.open_position.await_args.args[0]
    assert spec.symbol == "WEAK" and spec.side is _OS.SELL and spec.qty >= 1
    assert spec.stop_price > 48.0  # stop ABOVE the short entry
    assert len(seen) == 1 and seen[0].side is _OS.SELL
    assert seen[0].shortable is True and seen[0].easy_to_borrow is True
    monitor._adapter.asset_shortable.assert_awaited_once()
    assert monitor._adapter.asset_shortable.await_args.args[1] == "WEAK"


async def test_pipeline_zero_sell_signals_while_flag_off(monkeypatch, tmp_path):
    """THE whole-pipeline negative proof: shorts_enabled=False and the same
    weak tape → no SELL signal is emitted, pushed or executed anywhere."""
    monitor, candidates = _short_pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=False)
    executed = []

    async def spy_execute(signal, avg_volume=None, auction=False, max_qty=None):
        executed.append(signal)
        return True

    monitor._execute_signal = spy_execute
    await monitor._entry_pipeline(candidates)
    assert executed == []  # nothing reached execution
    monitor.engine.open_position.assert_not_awaited()
    monitor._push.assert_not_awaited()  # no signal notification either


async def test_trend_gate_short_refuses_above_open_shorts(monkeypatch, tmp_path):
    """Mirror of the long trend gate: a SELL on a name trading ABOVE today's
    open is refused ('trend gate (short)')."""
    monitor, candidates = _short_pipeline_monitor(monkeypatch, tmp_path, price=51.0, day_open=50.5)
    await monitor._entry_pipeline(candidates)
    monitor.engine.open_position.assert_not_awaited()


async def test_pipeline_short_skipped_while_ssr_active(monkeypatch, tmp_path):
    """§12: SSR on the symbol → the short signal is skipped at the pipeline
    level (risk.can_enter would refuse anyway; no churn)."""
    from datetime import date

    monitor, candidates = _short_pipeline_monitor(monkeypatch, tmp_path)
    monitor.engine.risk.record_ssr_trigger("WEAK", date(2026, 8, 14))
    await monitor._entry_pipeline(candidates)
    monitor.engine.open_position.assert_not_awaited()


async def test_orb_late_gate_short_halves(monkeypatch, tmp_path):
    """DEMAND 23 mirrored: an ORB short ≥ orb_late_gate_pct BELOW the open
    rides half size (same config knob)."""
    from waveapp.broker.base import OrderSide as _OS
    from waveapp.engine.scanner import Candidate

    monitor, _ = _short_pipeline_monitor(monkeypatch, tmp_path)
    # rebuild the tape as an opening range 49.9–50.2 then a 9:40 breakdown
    # to 47.0 — −6.9% below the 50.5 open trips the 6% late gate
    monitor._hub = type(monitor._hub)(["SPY"])
    for minute, px in ((30, 50.0), (31, 50.2), (32, 49.9), (33, 50.1), (34, 50.0), (40, 47.0)):
        monitor._hub.bar_builder.on_trade(
            "WEAK", px, 3000, datetime(2026, 8, 14, 9, minute, tzinfo=ET)
        )
    monitor._hub.bar_builder.on_trade("WEAK", 47.0, 10, datetime(2026, 8, 14, 9, 46, tzinfo=ET))
    from datetime import UTC as _UTC

    monitor._hub.latest_quotes["WEAK"] = SimpleNamespace(
        bid_price=46.99, ask_price=47.01, timestamp=datetime.now(_UTC)
    )
    features = _features(symbol="WEAK", price=47.0, gap_pct=-4.5, rvol=4.0, day_open=50.5)
    candidates = [
        Candidate(
            features=features,
            score=6.0,
            strategy_scores={"ORB": 6.0},
            best_strategy="ORB",
            accepted=True,
        )
    ]
    executed = []

    async def spy_execute(signal, avg_volume=None, auction=False, max_qty=None):
        executed.append(signal)
        return True

    monitor._execute_signal = spy_execute
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    signal = executed[0]
    assert signal.side is _OS.SELL and signal.strategy == "ORB"
    assert signal.half_size is True  # the late-gate (short) haircut


async def test_seed_judge_trough_lands_on_sell_actor(monkeypatch, tmp_path):
    """S2 HANDOFF resolved: judge adoption seeds the LOW since entry (with
    its time and volume) onto a SELL actor; a BUY actor keeps the peak."""
    from datetime import UTC

    from waveapp.engine.connection_monitor import ConnectionMonitor

    monitor = ConnectionMonitor(lambda *a: None)
    series = [
        SimpleNamespace(
            timestamp=datetime(2026, 9, 23, 14, m, tzinfo=UTC),
            open=50.0,
            high=50.0 + h,
            low=50.0 - lo,
            close=50.0,
            volume=v,
        )
        for m, h, lo, v in ((0, 0.5, 0.2, 100), (5, 1.0, 1.4, 900), (10, 0.2, 0.6, 200))
    ]
    short_actor = SimpleNamespace(
        spec=SimpleNamespace(symbol="XYZ", side=OrderSide.SELL),
        adopted_entry_time=datetime(2026, 9, 23, 13, 55, tzinfo=UTC),
    )
    long_actor = SimpleNamespace(
        spec=SimpleNamespace(symbol="XYZ", side=OrderSide.BUY),
        adopted_entry_time=datetime(2026, 9, 23, 13, 55, tzinfo=UTC),
    )
    monitor.engine = SimpleNamespace(actors={"s": short_actor, "l": long_actor})
    monitor._seed_judge_peaks("XYZ", series)
    assert short_actor.judge_seed_trough == 48.6  # min low (50 − 1.4)
    assert short_actor.judge_seed_trough_t_ms == int(series[1].timestamp.timestamp() * 1000)
    assert short_actor.judge_seed_trough_vol == 900.0
    assert not hasattr(short_actor, "judge_seed_peak")  # mirror, not both
    assert long_actor.judge_seed_peak == 51.0  # long path untouched
    assert not hasattr(long_actor, "judge_seed_trough")
