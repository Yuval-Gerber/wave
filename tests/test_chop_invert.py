"""CHOP-DAY SIGNAL INVERSION (2026-09-23, the −$854 chop day):
on a DayJudge CHOP verdict Wave flips its own momentum signals at the same
trigger — shorting the failed breakout, buying the failed breakdown.

Covers: the pure transform math (side/stop mirror/half_size/reason prefix),
the routing (applies ONLY at CHOP + confidence + config flag, each condition
tested alone), the trend-gate skip for inverted signals only, the per-day
opened-entries cap, the defensive no-detector behavior, and the flag-off
negative (pipeline output identical to baseline).
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from waveapp.broker.base import OrderSide
from waveapp.engine.connection_monitor import CHOP_INVERT_MIN_CONF, ConnectionMonitor
from waveapp.engine.strategies import (
    CHOP_INVERT_PREFIX,
    EntrySignal,
    invert_signal,
    is_inverted,
)

# -- 1. transform math --------------------------------------------------------


def _sig(side=OrderSide.BUY, entry=52.0, stop=50.0, half=False, reason="gap-and-go break"):
    return EntrySignal(
        symbol="HOT",
        side=side,
        confidence=0.8,
        reason=reason,
        strategy="GAP",
        entry_price=entry,
        stop_price=stop,
        half_size=half,
    )


def test_invert_buy_becomes_short_with_mirrored_stop():
    flipped = invert_signal(_sig(side=OrderSide.BUY, entry=52.0, stop=50.0))
    assert flipped is not None
    assert flipped.side is OrderSide.SELL
    # stop mirrors around the trigger: 2*52 − 50 = 54, same $2 distance above
    assert flipped.stop_price == 54.0
    assert flipped.half_size is True
    assert flipped.reason == CHOP_INVERT_PREFIX + "gap-and-go break"
    # everything else untouched — strategy tag stays for the journal
    assert flipped.strategy == "GAP"
    assert flipped.entry_price == 52.0
    assert flipped.symbol == "HOT"


def test_invert_sell_becomes_plain_long():
    """A SELL inverted is a plain BUY — no borrow/ETB logic needed (safety
    rule 2: this can never manufacture a naked short)."""
    flipped = invert_signal(_sig(side=OrderSide.SELL, entry=40.0, stop=41.5))
    assert flipped is not None
    assert flipped.side is OrderSide.BUY
    assert flipped.stop_price == 38.5  # 2*40 − 41.5, mirrored below
    assert flipped.half_size is True
    assert is_inverted(flipped)


def test_invert_rounds_the_mirrored_stop_to_cents():
    flipped = invert_signal(_sig(entry=52.336, stop=51.111))
    assert flipped is not None
    assert flipped.stop_price == round(2 * 52.336 - 51.111, 2) == 53.56


def test_invert_refuses_degenerate_mirrors():
    # stop at the entry mirrors onto the entry — protective nonsense
    assert invert_signal(_sig(entry=50.0, stop=50.0)) is None
    # malformed long (stop above entry) mirrors to a short stop below entry
    assert invert_signal(_sig(entry=50.0, stop=51.0)) is None


def test_is_inverted_only_matches_the_prefix():
    assert not is_inverted(_sig())
    assert is_inverted(invert_signal(_sig()))
    assert not is_inverted(SimpleNamespace(reason=None))
    assert not is_inverted(SimpleNamespace())


# -- pipeline fixture (mirrors test_connection_monitor's, kept local so the
# -- parallel day-judge lane can reshape that file freely) --------------------


def _pipeline_monitor(monkeypatch, tmp_path, **config_kwargs):
    """One hot GAP candidate with warm bars and a tight live quote through the
    real Scanner/RiskEngine/TradeGate so _entry_pipeline produces a BUY signal
    and reaches _execute_signal. Returns (monitor, candidates, executed) where
    executed collects the signals _execute_signal received."""
    from waveapp.config import AppConfig
    from waveapp.data.hub import DataHub
    from waveapp.engine.risk import RiskEngine
    from waveapp.engine.scanner import Candidate, Scanner, SymbolFeatures
    from waveapp.engine.session import ET, Regime, SessionInfo
    from waveapp.engine.tradegate import TradeGate

    config_path = tmp_path / "config.toml"
    config_kwargs.setdefault("auto_trade", True)
    AppConfig(**config_kwargs).save(config_path)
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
        actors={},
        open_actor_count=0,
    )
    monitor._adapter = SimpleNamespace(
        get_account=AsyncMock(return_value=SimpleNamespace(equity=100_000.0, cash=100_000.0)),
        asset_shortable=AsyncMock(return_value=True),
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
    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=51.99, ask_price=52.01, timestamp=datetime.now(UTC)
    )
    candidate = Candidate(
        features=features,
        score=6.0,
        strategy_scores={"GAP": 6.0},
        best_strategy="GAP",
        accepted=True,
    )
    executed: list = []

    async def fake_execute(signal, avg_volume=None, auction=False, max_qty=None):
        executed.append(signal)
        return True

    monitor._execute_signal = fake_execute
    return monitor, [candidate], executed


def _judge(verdict, confidence):
    """A detector stand-in shaped exactly like the routing contract."""
    return SimpleNamespace(verdict=verdict, confidence=confidence)


# -- 2. routing: applies only at CHOP + confidence + flag ---------------------


async def test_chop_plus_confidence_plus_flag_inverts(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    monitor._day_judge = _judge("CHOP", 0.9)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    signal = executed[0]
    assert signal.side is OrderSide.SELL  # the failed breakout, shorted
    assert signal.half_size is True
    assert signal.reason.startswith(CHOP_INVERT_PREFIX)
    assert signal.strategy == "GAP"  # journal tag untouched
    # stop mirrored above the trigger
    assert signal.stop_price > signal.entry_price


async def test_flag_off_is_byte_identical_to_baseline(monkeypatch, tmp_path):
    """Negative test: chop_invert=False (the default) under a screaming CHOP
    verdict produces the exact signal the baseline pipeline produces."""
    base_monitor, base_candidates, base_exec = _pipeline_monitor(
        monkeypatch, tmp_path, shorts_enabled=True
    )
    await base_monitor._entry_pipeline(base_candidates)

    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch,
        tmp_path,
        shorts_enabled=True,  # chop_invert stays default False
    )
    monitor._day_judge = _judge("CHOP", 0.99)
    await monitor._entry_pipeline(candidates)

    assert len(base_exec) == len(executed) == 1
    assert executed[0] == base_exec[0]  # frozen dataclass equality: every field
    assert executed[0].side is OrderSide.BUY
    assert not is_inverted(executed[0])


async def test_trend_and_unclear_verdicts_change_nothing(monkeypatch, tmp_path):
    for verdict in ("TREND_UP", "TREND_DOWN", "UNCLEAR"):
        monitor, candidates, executed = _pipeline_monitor(
            monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
        )
        monitor._day_judge = _judge(verdict, 0.95)
        await monitor._entry_pipeline(candidates)
        assert len(executed) == 1, verdict
        assert executed[0].side is OrderSide.BUY, verdict
        assert not is_inverted(executed[0]), verdict


async def test_low_confidence_chop_does_not_invert(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    monitor._day_judge = _judge("CHOP", CHOP_INVERT_MIN_CONF - 0.01)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.BUY
    assert not is_inverted(executed[0])


async def test_missing_detector_is_unclear(monkeypatch, tmp_path):
    """Defensive contract: no _day_judge attribute at all (or one without the
    attributes) must behave exactly like UNCLEAR — never crash, never flip."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    if hasattr(monitor, "_day_judge"):
        del monitor._day_judge
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.BUY

    # attribute-less object in the slot: same story
    monitor2, candidates2, executed2 = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    monitor2._day_judge = object()
    await monitor2._entry_pipeline(candidates2)
    assert len(executed2) == 1
    assert executed2[0].side is OrderSide.BUY


def test_day_judge_reading_normalizes_enums_and_garbage():
    monitor = ConnectionMonitor(lambda *a: None)
    from waveapp.engine.day_judge import DayVerdict

    monitor._day_judge = SimpleNamespace(verdict=DayVerdict.CHOP, confidence=0.7)
    assert monitor._day_judge_reading() == ("CHOP", 0.7)
    monitor._day_judge = SimpleNamespace(verdict="chop", confidence="0.8")
    assert monitor._day_judge_reading() == ("CHOP", 0.8)
    monitor._day_judge = SimpleNamespace(verdict=None, confidence=None)
    assert monitor._day_judge_reading() == ("UNCLEAR", 0.0)
    monitor._day_judge = SimpleNamespace(verdict="CHOP", confidence="not-a-number")
    assert monitor._day_judge_reading() == ("UNCLEAR", 0.0)


# -- 3. trend-gate skip: inverted signals only --------------------------------


def test_trend_gate_vetoes_normal_signals_but_skips_inverted():
    monitor = ConnectionMonitor(lambda *a: None)
    features = SimpleNamespace(symbol="HOT", price=50.0, day_open=50.0)
    # ordinary short at/above the open → vetoed (never fade a lifting tape)
    assert monitor._trend_gate_vetoes(_sig(side=OrderSide.SELL, entry=50.0, stop=51.0), features)
    # ordinary long below the open → vetoed
    below = SimpleNamespace(symbol="HOT", price=49.0, day_open=50.0)
    assert monitor._trend_gate_vetoes(_sig(side=OrderSide.BUY), below)
    # the SAME shapes, CHOP-inverted → the gate stands aside (both sides)
    inv_short = invert_signal(_sig(side=OrderSide.BUY, entry=50.0, stop=49.0))
    assert inv_short.side is OrderSide.SELL
    assert not monitor._trend_gate_vetoes(inv_short, features)
    inv_long = invert_signal(_sig(side=OrderSide.SELL, entry=49.0, stop=50.0))
    assert inv_long.side is OrderSide.BUY
    assert not monitor._trend_gate_vetoes(inv_long, below)
    # no day open on record → the gate never fires at all
    assert not monitor._trend_gate_vetoes(
        _sig(side=OrderSide.SELL), SimpleNamespace(symbol="HOT", price=50.0, day_open=0.0)
    )


async def test_inverted_short_above_open_survives_the_pipeline_gate(monkeypatch, tmp_path):
    """End-to-end: with day_open on record and price above it, the inverted
    SELL would be exactly what the trend gate exists to veto — the skip must
    carry it through to execution."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    import dataclasses

    candidates[0] = dataclasses.replace(
        candidates[0], features=dataclasses.replace(candidates[0].features, day_open=49.0)
    )
    monitor._day_judge = _judge("CHOP", 0.8)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.SELL
    assert is_inverted(executed[0])


# -- 4. the per-day cap -------------------------------------------------------


async def test_daily_cap_stops_inverting_but_not_trading(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True, chop_invert_max_per_day=2
    )
    monitor._day_judge = _judge("CHOP", 0.9)
    monitor._signal_day = "2026-08-14"  # match the fixture clock: no day roll
    monitor._signaled = set()
    monitor._chop_inverted_today = 2  # cap already spent
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.BUY  # passes through uninverted
    assert not is_inverted(executed[0])
    assert monitor._chop_inverted_today == 2  # an uninverted open never counts


async def test_opened_inverted_entry_counts_toward_the_cap(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    monitor._day_judge = _judge("CHOP", 0.9)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1 and is_inverted(executed[0])
    assert monitor._chop_inverted_today == 1


async def test_gated_out_inversion_does_not_burn_the_cap(monkeypatch, tmp_path):
    """A flip that never opens (engine refusal) must not consume the cap."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    monitor._day_judge = _judge("CHOP", 0.9)

    async def refuse(signal, avg_volume=None, auction=False, max_qty=None):
        executed.append(signal)
        return False

    monitor._execute_signal = refuse
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1 and is_inverted(executed[0])
    assert monitor._chop_inverted_today == 0


def test_cap_counter_resets_with_the_et_day():
    from waveapp.engine.session import ET

    monitor = ConnectionMonitor(lambda *a: None)
    monitor._chop_inverted_today = 4
    monitor._roll_signal_day(datetime(2026, 9, 23, 9, 30, tzinfo=ET))
    assert monitor._chop_inverted_today == 0


# -- 5. lanes that never invert (pullback logic, not breakouts) ---------------


class _FakeStrategy:
    def __init__(self, name, strategy_tag):
        self.name = name
        self._tag = strategy_tag

    def evaluate(self, features, bars, vwap, regime, is_lull=False, now=None):
        return EntrySignal(
            symbol=features.symbol,
            side=OrderSide.BUY,
            confidence=0.9,
            reason="pullback hold",
            strategy=self._tag,
            entry_price=52.0,
            stop_price=50.0,
        )


async def test_fpb_signals_are_never_inverted(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    monitor._day_judge = _judge("CHOP", 0.9)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("FPB", "FPB")],
    )
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.BUY  # untouched despite CHOP
    assert not is_inverted(executed[0])


async def test_climber_lane_signals_are_never_inverted(monkeypatch, tmp_path):
    import dataclasses

    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, chop_invert=True, shorts_enabled=True
    )
    monitor._day_judge = _judge("CHOP", 0.9)
    # the sole candidate arrives through the climber lane, not the scan pool
    climber = dataclasses.replace(
        candidates[0], features=dataclasses.replace(candidates[0].features, symbol="CLMB")
    )
    monitor._hub.latest_quotes["CLMB"] = SimpleNamespace(
        bid_price=51.99, ask_price=52.01, timestamp=datetime.now(UTC)
    )

    async def fake_climbers(existing):
        return [climber]

    monitor._climber_candidates = fake_climbers
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("VWAP", "VWAP")],
    )
    await monitor._entry_pipeline([])  # empty scan pool — climber lane only
    assert [s.symbol for s in executed] == ["CLMB"]
    assert executed[0].side is OrderSide.BUY  # untouched despite CHOP
    assert not is_inverted(executed[0])
