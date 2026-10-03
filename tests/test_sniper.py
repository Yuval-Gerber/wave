"""THE SNIPER BOOK (R2, 2026-09-24 — the sniper validation study).

Covers all four legs and their composition with the existing pipeline:

1. FPB conversion — the tested pullback rule verbatim (undercut the trigger
   while holding above session VWAP and the signal bar's low), the tighter
   pullback stop with the original-stop floor, window expiry = the proven
   skip, watch deaths (INVERTED flip, day-mark burn, 15:20 close guard),
   the concurrent-watch cap, and the FPB-short mirror.
2. Jewel sizing — GAP-only 1.5× via the risk budget, with the notional and
   impact caps binding AFTER the scale.
3. Pocket starves — ORB long on TREND_DOWN, the 14h ET hour, ORB <1%-ext on
   CHOP — each with its exemptions (inverted book, pre-14:00 watches).
4. Composition — inverted signals never convert, lanes exempt, flag-off
   byte-identical, and the 09-24 replay shape.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from waveapp.broker.base import OrderSide
from waveapp.engine import fpb
from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.engine.fpb import FpbWatch, SniperBook, check_trigger
from waveapp.engine.strategies import EntrySignal, is_inverted

ET_DAY = "2026-08-14"


def _sig(
    symbol="HOT",
    side=OrderSide.BUY,
    entry=52.0,
    stop=49.0,
    strategy="GAP",
    reason="gap-and-go break",
    half=False,
):
    return EntrySignal(
        symbol=symbol,
        side=side,
        confidence=0.8,
        reason=reason,
        strategy=strategy,
        entry_price=entry,
        stop_price=stop,
        half_size=half,
    )


def _bar(minute, low, high, close, start_base=None):
    start = (start_base or datetime(2026, 8, 14, 13, 30, tzinfo=UTC)) + timedelta(minutes=minute)
    return SimpleNamespace(start=start, low=low, high=high, close=close)


def _watch(signal=None, trigger=52.0, bar_low=51.7, bar_high=52.1, minute=0, created=None):
    signal = signal or _sig(entry=trigger)
    base = datetime(2026, 8, 14, 13, 30, tzinfo=UTC)
    return FpbWatch(
        symbol=signal.symbol,
        signal=signal,
        features=SimpleNamespace(
            symbol=signal.symbol, price=trigger, day_open=50.0, atr_pct=2.0, avg_daily_volume=2e6
        ),
        trigger_price=trigger,
        signal_bar_low=bar_low,
        signal_bar_high=bar_high,
        signal_bar_start=base + timedelta(minutes=minute),
        created_at=created or datetime.now(UTC),
        avg_daily_volume=2e6,
    )


NOW = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)  # all fixture bars completed


# -- 1a. the tested rule, verbatim (check_trigger) ---------------------------


def test_trigger_fires_on_the_tested_rule_with_tighter_stop():
    """The study's rule: first completed bar whose low undercuts the trigger
    while holding above BOTH session VWAP and the signal bar's low. Fill =
    bar close (≤ trigger), stop = the pullback low, floored at the original
    stop — TIGHTER than the original 49.0."""
    w = _watch(trigger=52.0, bar_low=51.7)
    bars = [_bar(0, 51.7, 52.2, 52.0), _bar(2, 51.85, 52.05, 51.95)]
    trig = check_trigger(w, bars, session_vwap=51.75, now_utc=NOW)
    assert trig is not None
    assert trig.reason == fpb.FPB_PREFIX + w.signal.reason
    assert trig.entry_price == 51.95  # the pullback bar's close
    assert trig.stop_price == 51.85  # the pullback low — tighter than 49.0
    assert trig.stop_price > w.signal.stop_price
    assert trig.side is OrderSide.BUY and trig.strategy == "GAP"


def test_trigger_fill_capped_at_the_trigger_price():
    """A pullback bar that closed back ABOVE the trigger fills at no better
    than the trigger (resting limit assumption, per the study)."""
    w = _watch(trigger=52.0, bar_low=51.7)
    bars = [_bar(1, 51.9, 52.3, 52.25)]
    trig = check_trigger(w, bars, session_vwap=51.75, now_utc=NOW)
    assert trig is not None and trig.entry_price == 52.0
    assert trig.stop_price == 51.9


def test_trigger_requires_the_undercut_and_both_holds():
    w = _watch(trigger=52.0, bar_low=51.7)
    # no undercut (low at/above trigger) — the breakout just ran
    assert check_trigger(w, [_bar(1, 52.0, 52.5, 52.4)], 51.75, NOW) is None
    # undercut but VWAP lost — not the clean form
    assert check_trigger(w, [_bar(1, 51.6, 52.0, 51.9)], 51.75, NOW) is None
    # undercut but broke the signal bar's low — not holding
    assert check_trigger(w, [_bar(1, 51.65, 52.0, 51.9)], 51.5, NOW) is None


def test_trigger_scan_continues_past_nonqualifying_bars():
    """Study semantics: a dumping bar that broke VWAP is not the qualifying
    bar, but the scan continues — a later bar that holds still enters."""
    w = _watch(trigger=52.0, bar_low=51.7)
    bars = [
        _bar(1, 51.6, 52.0, 51.7),  # broke VWAP — skipped
        _bar(3, 51.9, 52.1, 52.0),  # undercut + held both → the entry
    ]
    trig = check_trigger(w, bars, 51.75, NOW)
    assert trig is not None and trig.stop_price == 51.9


def test_trigger_ignores_forming_bars_and_bars_outside_the_window():
    w = _watch(trigger=52.0, bar_low=51.7, minute=0)
    good = _bar(2, 51.85, 52.05, 51.95)
    # forming: bar started 30s ago — not a completed minute
    now = good.start + timedelta(seconds=30)
    assert check_trigger(w, [good], 51.75, now) is None
    # completed one minute later → fires
    assert check_trigger(w, [good], 51.75, good.start + timedelta(seconds=61)) is not None
    # a qualifying bar past the 20-min window never fires
    late = _bar(21, 51.85, 52.05, 51.95)
    assert check_trigger(w, [late], 51.75, NOW) is None
    # the signal bar itself never triggers
    assert check_trigger(w, [_bar(0, 51.7, 52.2, 52.0)], 51.75, NOW) is None


def test_trigger_skips_degenerate_bar_closed_on_its_low():
    w = _watch(trigger=52.0, bar_low=51.7)
    assert check_trigger(w, [_bar(1, 51.9, 52.0, 51.9)], 51.75, NOW) is None


def test_trigger_short_mirror_reflected():
    """FPB-short: bar high pokes above the trigger while holding BELOW both
    VWAP and the signal bar's high; fill floored at the trigger, stop = the
    pullback high capped at the original stop, reason tagged separably."""
    sig = _sig(side=OrderSide.SELL, entry=48.0, stop=49.5)
    w = _watch(signal=sig, trigger=48.0, bar_low=47.8, bar_high=48.3)
    bars = [_bar(2, 48.0, 48.2, 48.1)]
    trig = check_trigger(w, bars, session_vwap=48.3, now_utc=NOW)
    assert trig is not None
    assert trig.reason == fpb.FPB_SHORT_PREFIX + sig.reason
    assert trig.side is OrderSide.SELL
    assert trig.entry_price == 48.1 and trig.stop_price == 48.2
    assert trig.stop_price < sig.stop_price  # tighter than the original 49.5
    # holds must be strict: high at VWAP, or above the signal bar high → no
    assert check_trigger(w, [_bar(3, 48.0, 48.35, 48.1)], 48.3, NOW) is None
    assert check_trigger(w, [_bar(3, 48.0, 48.4, 48.1)], 48.5, NOW) is None


# -- 1b. band helpers ---------------------------------------------------------


def test_extension_and_band_helpers():
    import pytest

    assert fpb.extension_pct(52.0, 50.0) == pytest.approx(4.0)
    assert fpb.is_extended(_sig(entry=51.0, stop=50.0), 50.0)  # +2.0% exactly
    assert not fpb.is_extended(_sig(entry=50.9, stop=50.0), 50.0)
    assert fpb.is_extended(_sig(side=OrderSide.SELL, entry=49.0, stop=50.0), 50.0)
    assert not fpb.is_extended(_sig(side=OrderSide.SELL, entry=49.1, stop=50.0), 50.0)
    # jewel: GAP BUY <1% above open, GAP-only, BUY-only
    assert fpb.is_jewel(_sig(entry=50.4), 50.0)
    assert not fpb.is_jewel(_sig(entry=50.5), 50.0)  # 1.0% is out
    assert not fpb.is_jewel(_sig(entry=50.4, strategy="ORB"), 50.0)
    assert not fpb.is_jewel(_sig(entry=50.4, side=OrderSide.SELL), 50.0)
    assert not fpb.is_jewel(_sig(entry=50.4), 0.0)


# -- 1c. the book: cap, dedupe, close guard, deaths ---------------------------


def _bars_for_convert():
    return [_bar(0, 51.7, 52.2, 52.0)]


def _et(hour, minute):
    from waveapp.engine.session import ET

    return datetime(2026, 8, 14, hour, minute, tzinfo=ET)


def test_book_cap_dedupe_spent_and_close_guard():
    book = SniperBook()
    book.roll_day(ET_DAY)
    now = datetime.now(UTC)
    assert (
        book.convert(_sig(symbol="AAA"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 0))
        == "watch"
    )
    assert (
        book.convert(_sig(symbol="AAA"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 1))
        == "dup"
    )
    for i in range(7):
        assert (
            book.convert(
                _sig(symbol=f"S{i}"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 2)
            )
            == "watch"
        )
    assert len(book.watches) == fpb.FPB_MAX_WATCHES == 8
    assert (
        book.convert(_sig(symbol="NINE"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 3))
        == "cap"
    )
    # close guard: at/after 15:20 ET no new watch is born
    book.remove("AAA")
    assert (
        book.convert(_sig(symbol="LATE"), SimpleNamespace(), _bars_for_convert(), now, _et(15, 20))
        == "late"
    )
    # an expired symbol is spent for the day — the skip is final
    book._spent.add("GONE")
    assert (
        book.convert(_sig(symbol="GONE"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 5))
        == "spent"
    )


def test_book_prune_reasons():
    now = datetime.now(UTC)
    # window expiry — the proven skip; marks the symbol spent
    book = SniperBook()
    book.roll_day(ET_DAY)
    book.convert(_sig(symbol="OLD"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 0))
    book.watches["OLD"].created_at = now - timedelta(minutes=21)
    dead = book.prune(now, set(), False, _et(10, 25))
    assert [(w.symbol, "skip" in why or "pullback" in why) for w, why in dead] == [("OLD", True)]
    assert "OLD" in book._spent and book.stats["expired"] == 1
    # INVERTED flip kills everything, spends nothing
    book2 = SniperBook()
    book2.roll_day(ET_DAY)
    book2.convert(_sig(symbol="AAA"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 0))
    dead2 = book2.prune(now, set(), True, _et(10, 1))
    assert [w.symbol for w, _ in dead2] == ["AAA"] and "INVERTED" in dead2[0][1]
    assert not book2.watches and "AAA" not in book2._spent
    # day-mark burn
    book3 = SniperBook()
    book3.roll_day(ET_DAY)
    book3.convert(_sig(symbol="BBB"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 0))
    dead3 = book3.prune(now, {"BBB"}, False, _et(10, 1))
    assert "day-mark" in dead3[0][1] and not book3.watches
    # close guard kills survivors at 15:20
    book4 = SniperBook()
    book4.roll_day(ET_DAY)
    book4.convert(_sig(symbol="CCC"), SimpleNamespace(), _bars_for_convert(), now, _et(10, 0))
    dead4 = book4.prune(now, set(), False, _et(15, 20))
    assert "close guard" in dead4[0][1] and "CCC" in book4._spent


def test_book_rolls_with_the_day():
    book = SniperBook()
    book.roll_day(ET_DAY)
    book.convert(
        _sig(symbol="AAA"), SimpleNamespace(), _bars_for_convert(), datetime.now(UTC), _et(10, 0)
    )
    book._spent.add("BBB")
    book.stats["starved"] = 3
    book.roll_day(ET_DAY)  # same day: nothing moves
    assert book.watches and book._spent and book.stats["starved"] == 3
    book.roll_day("2026-08-17")
    assert not book.watches and not book._spent and book.stats["starved"] == 0


# -- pipeline fixture (mirrors test_chop_invert's, adapted for day_open) ------


def _pipeline_monitor(
    monkeypatch,
    tmp_path,
    day_open=50.0,
    price=52.0,
    et_time=None,
    strategy="GAP",
    gap_pct=4.5,
    **config_kwargs,
):
    """One hot candidate with warm bars, a real day_open and a tight live
    quote through the real Scanner/RiskEngine/TradeGate. The 9:46 signal bar
    has a real range (low 51.7, close 52.0) so the tested pullback rule has
    honest anchors. Returns (monitor, candidates, executed)."""
    from waveapp.config import AppConfig
    from waveapp.data.hub import DataHub
    from waveapp.engine.risk import RiskEngine, RiskLimits
    from waveapp.engine.scanner import Candidate, Scanner, SymbolFeatures
    from waveapp.engine.session import ET, Regime, SessionInfo
    from waveapp.engine.tradegate import TradeGate

    config_path = tmp_path / "config.toml"
    config_kwargs.setdefault("auto_trade", True)
    AppConfig(**config_kwargs).save(config_path)
    monkeypatch.setattr("waveapp.config.config_path", lambda: config_path)
    et = et_time or datetime(2026, 8, 14, 9, 48, tzinfo=ET)
    monkeypatch.setattr(
        "waveapp.engine.session.SessionScheduler.info",
        staticmethod(
            lambda now=None: SessionInfo(regime=Regime.OPEN_DRIVE, is_lull=False, et_time=et)
        ),
    )
    monitor = ConnectionMonitor(lambda *a: None)
    monitor.engine = SimpleNamespace(
        risk=RiskEngine(RiskLimits(shorts_enabled=bool(config_kwargs.get("shorts_enabled")))),
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
        price=price,
        prev_close=50.0,
        gap_pct=gap_pct,
        rvol=4.0,
        atr_pct=2.0,
        spread=0.02,
        day_volume=2_000_000,
        avg_daily_volume=2_000_000,
        shortable=True,
        day_open=day_open,
    )
    # warm tape: VWAP ≈ 51.75; the SIGNAL bar (last CLOSED bar, 9:45) has a
    # real range (low 51.7 → close 52.0) so the pullback rule has honest
    # anchors; the 9:46 print closes it in the builder
    for px, vol, minute in ((51.5, 3000, 44), (51.7, 200, 45), (52.0, 3000, 45), (52.0, 10, 46)):
        monitor._hub.bar_builder.on_trade(
            "HOT", px, vol, datetime(2026, 8, 14, 9, minute, tzinfo=ET)
        )
    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=51.99, ask_price=52.01, timestamp=datetime.now(UTC)
    )
    candidate = Candidate(
        features=features,
        score=6.0,
        strategy_scores={strategy: 6.0},
        best_strategy=strategy,
        accepted=True,
    )
    executed: list = []

    async def fake_execute(signal, avg_volume=None, auction=False, max_qty=None):
        executed.append(signal)
        return True

    monitor._execute_signal = fake_execute
    return monitor, [candidate], executed


def _judge(verdict, confidence=0.9):
    return SimpleNamespace(verdict=verdict, confidence=confidence)


class _FakeStrategy:
    """Emits per-symbol signals from a table — for ORB-tagged shapes the
    real ORB strategy would need a full opening-range tape to produce."""

    def __init__(self, tag, table):
        self.name = tag
        self._tag = tag
        self._table = table  # symbol -> (side, entry, stop)

    def evaluate(self, features, bars, vwap, regime, is_lull=False, now=None):
        row = self._table.get(features.symbol)
        if row is None:
            return None
        side, entry, stop = row
        return EntrySignal(
            symbol=features.symbol,
            side=side,
            confidence=0.9,
            reason=f"{self._tag} break",
            strategy=self._tag,
            entry_price=entry,
            stop_price=stop,
        )


def _pullback_tape(monitor, symbol="HOT"):
    """Two minutes after the signal bar: a red drift then the holding
    pullback bar (low 51.85 > VWAP ≈ 51.76, > signal bar low 51.7, < trigger
    52.0), closing 51.95 — the study's entry shape."""
    from waveapp.engine.session import ET

    monitor._hub.bar_builder.on_trade(symbol, 51.85, 100, datetime(2026, 8, 14, 9, 48, tzinfo=ET))
    monitor._hub.bar_builder.on_trade(symbol, 51.95, 100, datetime(2026, 8, 14, 9, 48, tzinfo=ET))
    # a 9:49 print completes the 9:48 bar in the builder
    monitor._hub.bar_builder.on_trade(symbol, 51.95, 10, datetime(2026, 8, 14, 9, 49, tzinfo=ET))


# -- 2. leg 1 end-to-end: convert → pullback → FPB entry ----------------------


async def test_extended_signal_converts_then_pullback_triggers_entry(monkeypatch, tmp_path):
    """The study trade shape end-to-end: a GAP BUY 4% above the open is NOT
    executed — it becomes a watch; the next pass, after the holding pullback
    bar, the entry fires through the pipeline with the FPB reason prefix and
    the tighter pullback stop."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    await monitor._entry_pipeline(candidates)
    assert executed == []  # the chase was refused
    assert "HOT" in monitor._sniper.watches
    w = monitor._sniper.watches["HOT"]
    assert w.trigger_price == 52.0 and w.signal_bar_low == 51.7
    assert monitor._sniper.stats["converted"] == 1

    _pullback_tape(monitor)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    trig = executed[0]
    assert trig.reason.startswith(fpb.FPB_PREFIX)
    assert trig.strategy == "GAP" and trig.side is OrderSide.BUY
    assert trig.entry_price == 51.95
    assert trig.stop_price == 51.85  # the pullback low — tighter
    assert trig.size_mult == 1.0  # FPB rides 1.0× (VALIDATION: SNIPER+SIZE)
    assert "HOT" not in monitor._sniper.watches
    assert "HOT" in monitor._signaled  # day-mark burned by the FPB open
    assert monitor._sniper.stats["triggered"] == 1


async def test_window_expiry_is_the_proven_skip(monkeypatch, tmp_path):
    """No holding pullback inside 20 min → the watch dies silently, the
    symbol is spent for the day, and a re-fired extended signal neither
    executes nor re-arms a watch (the +$2,644 skip)."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    await monitor._entry_pipeline(candidates)
    assert "HOT" in monitor._sniper.watches
    monitor._sniper.watches["HOT"].created_at = datetime.now(UTC) - timedelta(minutes=21)
    await monitor._entry_pipeline(candidates)
    assert executed == []
    assert "HOT" not in monitor._sniper.watches
    assert monitor._sniper.stats["expired"] == 1
    # the extended signal keeps firing — and keeps being skipped
    await monitor._entry_pipeline(candidates)
    assert executed == [] and not monitor._sniper.watches


async def test_inversion_flip_kills_watches_and_extended_signals_invert(monkeypatch, tmp_path):
    """Composition: the book flips to INVERTED → every watch dies (the
    anti-momentum book has no pullback-buys) and a ≥2% momentum signal rides
    the efficacy inversion instead of converting (no watch)."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, day_open=50.0, shorts_enabled=True
    )
    await monitor._entry_pipeline(candidates)
    assert "HOT" in monitor._sniper.watches and executed == []
    from waveapp.engine.efficacy import MODE_INVERTED

    monitor._efficacy = SimpleNamespace(mode=lambda: MODE_INVERTED, roll_day=lambda d: None)
    await monitor._entry_pipeline(candidates)
    assert not monitor._sniper.watches  # watch killed by the flip
    assert monitor._sniper.stats["killed"] == 1
    assert len(executed) == 1
    assert is_inverted(executed[0]) and executed[0].side is OrderSide.SELL
    assert monitor._sniper.stats["converted"] == 1  # no second conversion


async def test_chop_inverted_signal_never_converts(monkeypatch, tmp_path):
    """Composition: a CHOP-inverted signal is fade logic — it executes
    inverted and never becomes a watch."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, day_open=50.0, shorts_enabled=True, chop_invert=True
    )
    monitor._day_judge = _judge("CHOP", 0.9)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1 and is_inverted(executed[0])
    assert not monitor._sniper.watches


async def test_watch_dies_when_another_entry_burns_the_day_mark(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    await monitor._entry_pipeline(candidates)
    assert "HOT" in monitor._sniper.watches
    monitor._signaled.add("HOT")  # e.g. an MKRB/auction entry took the seat
    _pullback_tape(monitor)
    await monitor._entry_pipeline(candidates)
    assert executed == [] and not monitor._sniper.watches
    assert monitor._sniper.stats["killed"] == 1


async def test_fpb_short_mirror_end_to_end(monkeypatch, tmp_path):
    """The reflected rule for a SELL momentum signal ≥2% BELOW the open:
    watch, pullback UP that holds below VWAP, entry tagged FPB-short."""
    from waveapp.engine.session import ET

    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, day_open=50.5, price=48.0, gap_pct=-4.5, shorts_enabled=True
    )
    # rebuild the tape as a gap-down: VWAP ≈ 48.3, signal bar (9:45) high
    # 48.3 → close 48.0; the 9:46 print closes it in the builder
    monitor._hub = type(monitor._hub)(["SPY"])
    for px, vol, minute in ((48.6, 3000, 44), (48.3, 200, 45), (48.0, 3000, 45), (48.0, 10, 46)):
        monitor._hub.bar_builder.on_trade(
            "HOT", px, vol, datetime(2026, 8, 14, 9, minute, tzinfo=ET)
        )
    monitor._hub.latest_quotes["HOT"] = SimpleNamespace(
        bid_price=47.99, ask_price=48.01, timestamp=datetime.now(UTC)
    )
    await monitor._entry_pipeline(candidates)
    assert executed == []
    assert "HOT" in monitor._sniper.watches
    w = monitor._sniper.watches["HOT"]
    assert w.signal.side is OrderSide.SELL and w.trigger_price == 48.0
    # pullback UP: 9:48 bar high 48.2 (> trigger, < VWAP, < signal bar 48.3)
    monitor._hub.bar_builder.on_trade("HOT", 48.2, 100, datetime(2026, 8, 14, 9, 48, tzinfo=ET))
    monitor._hub.bar_builder.on_trade("HOT", 48.1, 100, datetime(2026, 8, 14, 9, 48, tzinfo=ET))
    monitor._hub.bar_builder.on_trade("HOT", 48.1, 10, datetime(2026, 8, 14, 9, 49, tzinfo=ET))
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    trig = executed[0]
    assert trig.reason.startswith(fpb.FPB_SHORT_PREFIX)
    assert trig.side is OrderSide.SELL
    assert trig.entry_price == 48.1 and trig.stop_price == 48.2
    assert trig.stop_price < w.signal.stop_price  # tighter than the original


# -- 3. leg 2: jewel sizing ---------------------------------------------------


async def test_jewel_gap_rides_1_5x(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=51.8)
    await monitor._entry_pipeline(candidates)  # ext +0.39% — the jewel band
    assert len(executed) == 1
    assert executed[0].size_mult == fpb.JEWEL_SIZE_MULT == 1.5
    assert monitor._sniper.stats["jewel"] == 1
    assert not monitor._sniper.watches


async def test_jewel_is_gap_only(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=51.8)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("ORB", {"HOT": (OrderSide.BUY, 52.0, 50.0)})],
    )
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].size_mult == 1.0  # ORB <1% gets no preferential size


def test_risk_mult_scales_budget_with_caps_binding_after():
    """position_size: risk_mult scales the budget BEFORE the notional and
    impact caps, so every cap binds on the SCALED qty."""
    from waveapp.engine.risk import RiskEngine

    risk = RiskEngine()
    # uncapped: 1% of 100k = $1,000 budget / $10 stop distance → 100 → ×1.5
    assert risk.position_size(100_000, 100.0, 90.0, OrderSide.BUY) == 100
    assert risk.position_size(100_000, 100.0, 90.0, OrderSide.BUY, risk_mult=1.5) == 150
    # notional cap (25% → 250 shares at $100) binds on the scaled qty
    assert risk.position_size(100_000, 100.0, 98.0, OrderSide.BUY) == 250
    assert risk.position_size(100_000, 100.0, 98.0, OrderSide.BUY, risk_mult=1.5) == 250
    # impact cap (0.5% of ADV 20k = 100 shares) binds on the scaled qty
    assert (
        risk.position_size(
            100_000, 100.0, 90.0, OrderSide.BUY, avg_daily_volume=20_000, risk_mult=1.5
        )
        == 100
    )
    # the mult is clamped — nobody inflates past 2×
    assert risk.position_size(100_000, 100.0, 90.0, OrderSide.BUY, risk_mult=50.0) == 200


async def test_execute_signal_threads_size_mult_into_sizing(monkeypatch, tmp_path):
    """_execute_signal passes the signal's size_mult into position_size —
    the submitted qty is 1.5× the base (caps not binding here)."""
    import dataclasses

    monitor, _c, _e = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    base = _sig(entry=52.0, stop=48.0)  # $4 distance → 250 base shares
    assert await ConnectionMonitor._execute_signal(monitor, base) is True
    jewel = dataclasses.replace(_sig(symbol="GEM", entry=52.0, stop=48.0), size_mult=1.5)
    monitor._hub.latest_quotes["GEM"] = SimpleNamespace(
        bid_price=51.99, ask_price=52.01, timestamp=datetime.now(UTC)
    )
    assert await ConnectionMonitor._execute_signal(monitor, jewel) is True
    specs = [c.args[0] for c in monitor.engine.open_position.await_args_list]
    assert specs[0].qty == 250 and specs[1].qty == 375


# -- 4. leg 3: the pocket starves ---------------------------------------------


async def test_starve_orb_long_on_trend_down(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    monitor._day_judge = _judge("TREND_DOWN")
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("ORB", {"HOT": (OrderSide.BUY, 52.0, 50.0)})],
    )
    await monitor._entry_pipeline(candidates)
    assert executed == [] and not monitor._sniper.watches  # starved, not watched
    assert monitor._sniper.stats["starved"] == 1


async def test_gap_long_on_trend_down_is_not_starved(monkeypatch, tmp_path):
    """The pocket is ORB-shaped — GAP longs pass (jewel band here)."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=51.8)
    monitor._day_judge = _judge("TREND_DOWN")
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1 and executed[0].strategy == "GAP"


async def test_starve_orb_sub1pct_on_chop_but_extended_orb_converts(monkeypatch, tmp_path):
    """CHOP: the open-chop break (<1% ext) is starved; an extended ORB on
    the same verdict still converts to a watch (not the chop pocket)."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=51.8)
    monitor._day_judge = _judge("CHOP", 0.3)  # any published CHOP verdict
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("ORB", {"HOT": (OrderSide.BUY, 52.0, 50.0)})],
    )
    await monitor._entry_pipeline(candidates)  # ext +0.39% → the chop pocket
    assert executed == [] and monitor._sniper.stats["starved"] == 1

    monitor2, candidates2, executed2 = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    monitor2._day_judge = _judge("CHOP", 0.3)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("ORB", {"HOT": (OrderSide.BUY, 52.0, 50.0)})],
    )
    await monitor2._entry_pipeline(candidates2)  # ext +4% → watch, no starve
    assert executed2 == [] and "HOT" in monitor2._sniper.watches
    assert monitor2._sniper.stats["starved"] == 0


async def test_starve_14h_hour_blocks_ordinary_momentum(monkeypatch, tmp_path):
    from waveapp.engine.session import ET

    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, day_open=51.8, et_time=datetime(2026, 8, 14, 14, 10, tzinfo=ET)
    )
    await monitor._entry_pipeline(candidates)  # jewel-band GAP — still starved
    assert executed == []
    # both firing strategies on the tape (GAP, then the VWAP pullback) were
    # refused by the same pocket — one log line each
    assert monitor._sniper.stats["starved"] == 2
    assert not monitor._sniper.watches  # and no watch born inside the hour


async def test_14h_exemptions_watch_trigger_and_inverted_book(monkeypatch, tmp_path):
    """A watch created before 14:00 may still trigger inside the hour, and
    inverted-book entries are a different animal — both execute at 14:10."""
    from waveapp.engine.session import ET

    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch,
        tmp_path,
        day_open=50.0,
        shorts_enabled=True,
        et_time=datetime(2026, 8, 14, 14, 10, tzinfo=ET),
    )
    # pre-armed watch (13:5x birth) with the pullback already printed
    monitor._signal_day = ET_DAY
    monitor._signaled = set()
    monitor._sniper.roll_day(ET_DAY)
    sig = _sig(entry=52.0, stop=49.0, strategy="ORB", reason="ORB break")
    bars = monitor._hub.bar_builder.bars("HOT")
    monitor._sniper.convert(
        sig,
        candidates[0].features,
        bars,
        datetime.now(UTC) - timedelta(minutes=12),
        _et(13, 58),
    )
    monitor._sniper.watches["HOT"].created_at = datetime.now(UTC) - timedelta(minutes=12)
    _pullback_tape(monitor)
    await monitor._entry_pipeline([])  # empty pool: the watch alone acts
    assert len(executed) == 1 and executed[0].reason.startswith(fpb.FPB_PREFIX)

    # inverted book at 14:10: the momentum signal inverts and executes
    from waveapp.engine.efficacy import MODE_INVERTED

    monitor2, candidates2, executed2 = _pipeline_monitor(
        monkeypatch,
        tmp_path,
        day_open=51.8,
        shorts_enabled=True,
        et_time=datetime(2026, 8, 14, 14, 10, tzinfo=ET),
    )
    monitor2._efficacy = SimpleNamespace(mode=lambda: MODE_INVERTED, roll_day=lambda d: None)
    await monitor2._entry_pipeline(candidates2)
    assert len(executed2) == 1 and is_inverted(executed2[0])
    assert monitor2._sniper.stats["starved"] == 0


# -- 5. lanes exempt, flag off byte-identical ---------------------------------


async def test_climber_lane_is_exempt_from_sniper(monkeypatch, tmp_path):
    """A climber-lane candidate 4% above its open must NOT convert — the
    lane has its own never-chase contract."""
    import dataclasses

    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    climber = dataclasses.replace(
        candidates[0], features=dataclasses.replace(candidates[0].features, symbol="CLMB")
    )
    monitor._hub.latest_quotes["CLMB"] = SimpleNamespace(
        bid_price=51.99, ask_price=52.01, timestamp=datetime.now(UTC)
    )
    for px, vol, minute in ((51.5, 3000, 44), (52.0, 3000, 45), (52.0, 10, 46)):
        from waveapp.engine.session import ET

        monitor._hub.bar_builder.on_trade(
            "CLMB", px, vol, datetime(2026, 8, 14, 9, minute, tzinfo=ET)
        )

    async def fake_climbers(existing):
        return [climber]

    monitor._climber_candidates = fake_climbers
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("VWAP", {"CLMB": (OrderSide.BUY, 52.0, 50.0)})],
    )
    await monitor._entry_pipeline([])
    assert [s.symbol for s in executed] == ["CLMB"]  # executed untouched
    assert not monitor._sniper.watches


async def test_fpb_strategy_signals_are_exempt(monkeypatch, tmp_path):
    """The FPB strategy's own pullback entries never re-convert."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("FPB", {"HOT": (OrderSide.BUY, 52.0, 50.0)})],
    )
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1 and executed[0].strategy == "FPB"
    assert not monitor._sniper.watches


async def test_flag_off_is_byte_identical_to_baseline(monkeypatch, tmp_path):
    """Negative test: sniper_book=False → the extended GAP executes exactly
    as the baseline pipeline executed it (frozen-dataclass equality, default
    size_mult), nothing watched, nothing starved, nothing counted."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, day_open=50.0, sniper_book=False
    )
    monitor._day_judge = _judge("TREND_DOWN")  # starves would fire if armed
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    signal = executed[0]
    assert signal.side is OrderSide.BUY and signal.entry_price == 52.0
    assert signal.size_mult == 1.0
    assert not monitor._sniper.watches
    assert not any(monitor._sniper.stats.values())
    # jewel band too: flag off → no preferential size
    monitor2, candidates2, executed2 = _pipeline_monitor(
        monkeypatch, tmp_path, day_open=51.8, sniper_book=False
    )
    await monitor2._entry_pipeline(candidates2)
    assert len(executed2) == 1 and executed2[0].size_mult == 1.0


async def test_auto_trade_off_disarms_sniper(monkeypatch, tmp_path):
    """With auto_trade off the signal push is the day's action — the
    extended signal is pushed, never watched (byte-identical path)."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, day_open=50.0, auto_trade=False
    )
    await monitor._entry_pipeline(candidates)
    assert executed == [] and not monitor._sniper.watches
    monitor._push.assert_awaited_once()  # the auto-trade-OFF signal push


# -- 6. the replay shape: 09-24 first hour ------------------------------------


async def test_replay_shape_0924_first_hour(monkeypatch, tmp_path):
    """The 09-24 disaster-day shape under sniper: extended ORB entries
    become watches (ORCX +2.0%, NBIS +3.7% — the real rows), the jewel-band
    ORB (VKTX +0.3%) trades ordinarily, and once the judge calls
    TREND_DOWN, an ORB long is starved instead of watched."""
    import dataclasses

    from waveapp.engine.session import ET

    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, day_open=50.0)
    monitor._day_judge = _judge("TREND_UP")  # 09-24 opened trend-up
    table = {
        "ORCX": (OrderSide.BUY, 51.0, 49.5),  # +2.0% vs its 50.0 open
        "NBIS": (OrderSide.BUY, 51.85, 50.0),  # +3.7%
        "VKTX": (OrderSide.BUY, 50.15, 49.4),  # +0.3% — ordinary entry
    }
    pool = []
    for sym in table:
        pool.append(
            dataclasses.replace(
                candidates[0], features=dataclasses.replace(candidates[0].features, symbol=sym)
            )
        )
        for px, vol, minute in ((50.4, 3000, 44), (50.8, 3000, 45), (50.6, 200, 46)):
            monitor._hub.bar_builder.on_trade(
                sym, px, vol, datetime(2026, 8, 14, 9, minute, tzinfo=ET)
            )
        monitor._hub.latest_quotes[sym] = SimpleNamespace(
            bid_price=50.59, ask_price=50.61, timestamp=datetime.now(UTC)
        )
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("ORB", table)],
    )
    await monitor._entry_pipeline(pool)
    assert set(monitor._sniper.watches) == {"ORCX", "NBIS"}  # extended → watches
    assert [s.symbol for s in executed] == ["VKTX"]  # the jewel band trades
    assert monitor._sniper.stats["converted"] == 2

    # the judge turns: TREND_DOWN → a fresh ORB long is STARVED, not watched
    monitor._day_judge = _judge("TREND_DOWN")
    table["AMDL"] = (OrderSide.BUY, 52.9, 51.0)  # +5.8%
    amdl = dataclasses.replace(
        pool[0], features=dataclasses.replace(pool[0].features, symbol="AMDL")
    )
    for px, vol, minute in ((52.0, 3000, 44), (52.9, 3000, 45), (52.9, 10, 46)):
        monitor._hub.bar_builder.on_trade(
            "AMDL", px, vol, datetime(2026, 8, 14, 9, minute, tzinfo=ET)
        )
    monitor._hub.latest_quotes["AMDL"] = SimpleNamespace(
        bid_price=52.89, ask_price=52.91, timestamp=datetime.now(UTC)
    )
    monitor._day_list = list(monitor._day_list) + ["AMDL"]  # joins the frozen menu
    await monitor._entry_pipeline([amdl])
    assert "AMDL" not in monitor._sniper.watches
    assert [s.symbol for s in executed] == ["VKTX"]  # nothing new executed
    assert monitor._sniper.stats["starved"] == 1


# -- 7. the nightly counter line ----------------------------------------------


def test_close_summary_carries_the_sniper_line():
    monitor = ConnectionMonitor(lambda *a: None)
    monitor.telegram_stats = lambda: {"today_pnl": 100.0, "today_trades": 2, "today_wins": 1}
    monitor._ledger_drift = lambda pnl: None
    assert "Sniper" not in monitor._close_summary_text()  # quiet day: no line
    monitor._sniper.stats.update(converted=3, triggered=1, expired=2, starved=4, jewel=2)
    text = monitor._close_summary_text()
    assert "🎯 Sniper: 3 converted, 1 FPB entries, 2 skips, 4 starved, 2 jewel-sized." in text
