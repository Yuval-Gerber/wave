"""THE EFFICACY GUARD (R1.5, 2026-09-24 — the −$3,928 morning): Wave's own
entries get scored in real time (+1A before −1A within 10 min) and the signal
book flips MOMENTUM↔INVERTED on that evidence, FULL size in both modes
(Wave must MAKE money, not trade smaller).

Covers: the tracker state machine (flip ladder both directions, hysteresis,
LATE neutrality, per-mode tagging, day rollover), the first-touch scoring
rules (ticks and closes), the kitchen's per-second feed, the flip journal
(log + efficacy_events row + klass="risk" push), and the entry pipeline
(INVERTED inverts at full size through the shared transform, uninvertible
signals are skipped, flag-off is byte-identical, CHOP composition inverts
once with efficacy's full size winning) — ending with the replay of the
2026-09-24 morning itself.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from waveapp.broker.base import OrderSide
from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.engine.efficacy import (
    EFFICACY_WINDOW_MIN,
    FAIL,
    HYSTERESIS_MIN_SCORED,
    LATE,
    MODE_INVERTED,
    MODE_MOMENTUM,
    PASS,
    EfficacyTracker,
)
from waveapp.engine.strategies import (
    CHOP_INVERT_PREFIX,
    EFFICACY_INVERT_PREFIX,
    EntrySignal,
    invert_signal,
    is_chop_inverted,
    is_inverted,
)

ET_DAY_MS = int(datetime(2026, 9, 24, 9, 32, tzinfo=UTC).timestamp() * 1000)
WINDOW_MS = EFFICACY_WINDOW_MIN * 60_000


def _tracker(**kw) -> EfficacyTracker:
    return EfficacyTracker(**kw)


def _entered(tr: EfficacyTracker, key: str, t_ms: int = ET_DAY_MS) -> None:
    tr.record_entry(key, key.upper(), "buy", t_ms)


def _fails(tr: EfficacyTracker, *keys: str) -> None:
    for key in keys:
        _entered(tr, key)
        tr.record_outcome(key, FAIL)


# -- 1. tracker state machine -------------------------------------------------


def test_day_starts_momentum_with_clean_stats():
    tr = _tracker()
    _entered(tr, "k0")
    assert tr.mode() == MODE_MOMENTUM
    assert tr.consecutive_fails == 0
    assert tr.n_scored == 0
    assert tr.fail_rate() is None  # below min_n — no rate yet


def test_two_consecutive_fails_flip_to_inverted():
    tr = _tracker()
    _entered(tr, "k1")
    _entered(tr, "k2")
    tr.record_outcome("k1", FAIL)
    assert tr.mode() == MODE_MOMENTUM  # one fail is noise
    tr.record_outcome("k2", FAIL)
    assert tr.mode() == MODE_INVERTED  # the reflex the −$3,928 day lacked


def test_fail_rate_path_flips_without_a_streak():
    tr = _tracker()
    for key, outcome in (("a", FAIL), ("b", PASS), ("c", FAIL)):
        _entered(tr, key)
        tr.record_outcome(key, outcome)
    # streak is only 1, but 2/3 failed >= 0.6 with n >= 3
    assert tr.mode() == MODE_INVERTED


def test_passes_reset_the_streak_and_low_rate_stays_momentum():
    tr = _tracker()
    for key, outcome in (("a", FAIL), ("b", PASS), ("c", PASS), ("d", FAIL)):
        _entered(tr, key)
        tr.record_outcome(key, outcome)
    assert tr.mode() == MODE_MOMENTUM  # rate 2/4 < 0.6, streak broken by PASSes
    assert tr.consecutive_fails == 1  # only the trailing fail — PASSes reset it


def test_late_is_neutral_everywhere():
    tr = _tracker()
    for key, outcome in (("a", FAIL), ("b", LATE), ("c", FAIL)):
        _entered(tr, key)
        tr.record_outcome(key, outcome)
    # LATE neither broke the fail streak nor entered the rates (the flip
    # reset the CURRENT-mode view, so read MOMENTUM's book directly)
    assert tr._stats[MODE_MOMENTUM].n_scored == 2
    assert tr.mode() == MODE_INVERTED


def test_flip_back_is_symmetric_with_hysteresis():
    tr = _tracker()
    _fails(tr, "m1", "m2")
    assert tr.mode() == MODE_INVERTED
    # the INVERTED stint judges its own trades: two fails would qualify,
    # but the hysteresis demands HYSTERESIS_MIN_SCORED outcomes first
    _fails(tr, "i1", "i2")
    assert tr.mode() == MODE_INVERTED
    assert tr.n_scored == 2 < HYSTERESIS_MIN_SCORED
    _fails(tr, "i3")
    assert tr.mode() == MODE_MOMENTUM  # inverted entries failing too — back out


def test_outcomes_score_the_mode_that_placed_them():
    tr = _tracker()
    _entered(tr, "old")  # placed in MOMENTUM, still open at the flip
    _fails(tr, "m1", "m2")
    assert tr.mode() == MODE_INVERTED
    tr.record_outcome("old", FAIL)  # the momentum leftover finally dies
    # it scored MOMENTUM's book, not the fresh INVERTED stint
    assert tr.n_scored == 0
    assert tr.mode() == MODE_INVERTED


def test_one_outcome_per_position_first_touch_wins():
    tr = _tracker()
    _entered(tr, "k1")
    assert tr.record_tick("k1", 1.2, 60_000) == PASS
    assert tr.record_tick("k1", -5.0, 90_000) is None  # already scored
    assert tr.record_close("k1", ET_DAY_MS + 120_000) is None
    assert tr.n_scored == 1 and tr.consecutive_fails == 0


def test_day_rollover_resets_to_momentum():
    tr = _tracker()
    _fails(tr, "m1", "m2")
    assert tr.mode() == MODE_INVERTED
    tr.roll_day("2026-09-25")
    assert tr.mode() == MODE_MOMENTUM
    assert tr.n_scored == 0 and tr.n_entries_today == 0
    # and a next-day entry rolls the day by itself too (defensive path)
    tr2 = _tracker()
    _fails(tr2, "m1", "m2")
    tr2.record_entry("n1", "N1", "buy", ET_DAY_MS + 24 * 3600_000)
    assert tr2.mode() == MODE_MOMENTUM


# -- 2. first-touch scoring rules ---------------------------------------------


def test_tick_scoring_first_touch_rules():
    tr = _tracker()
    _entered(tr, "a")
    assert tr.record_tick("a", 0.5, 10_000) is None  # nothing touched yet
    assert tr.record_tick("a", 1.0, 20_000) == PASS  # +1A inside the window
    _entered(tr, "b")
    assert tr.record_tick("b", -1.0, 20_000) == FAIL  # −1A first
    _entered(tr, "c")
    assert tr.record_tick("c", 0.2, WINDOW_MS + 1000) == LATE  # window expired flat
    assert tr.record_tick("ghost", 5.0, 1000) is None  # unknown key — no crash


def test_close_inside_window_fails_after_window_is_late():
    tr = _tracker()
    _entered(tr, "cut")
    assert tr.record_close("cut", ET_DAY_MS + WINDOW_MS - 1000) == FAIL
    _entered(tr, "slow")
    assert tr.record_close("slow", ET_DAY_MS + WINDOW_MS + 1000) == LATE
    assert tr.n_scored == 1  # only the FAIL counted


# -- 3. flip journal: log line + db row + phone push --------------------------


def test_mode_flip_journals_and_notifies(caplog):
    events: list = []
    pushes: list = []
    tr = _tracker(journal_db_cb=lambda op, p: events.append((op, p)), notify_cb=pushes.append)
    import logging

    with caplog.at_level(logging.WARNING, logger="wave.engine.efficacy"):
        _fails(tr, "k1", "k2")
    assert any(
        "EFFICACY: flipping to INVERTED — 2 consecutive fails" in r.message for r in caplog.records
    )
    assert len(events) == 1
    op, payload = events[0]
    assert op == "efficacy_event"
    assert payload["from_mode"] == MODE_MOMENTUM and payload["to_mode"] == MODE_INVERTED
    assert payload["consecutive_fails"] == 2 and payload["n_fail"] == 2
    assert pushes and "EFFICACY: flipping to INVERTED" in pushes[0]


def test_rate_flip_reason_reads_like_the_spec():
    pushes: list = []
    tr = _tracker(notify_cb=pushes.append)
    for key, outcome in (("a", FAIL), ("b", PASS), ("c", FAIL)):
        _entered(tr, key)
        tr.record_outcome(key, outcome)
    # streak never reached 2; the 2/3 rate did the flipping
    assert tr.mode() == MODE_INVERTED
    assert "signals failing 2/3" in pushes[0]


def test_efficacy_events_row_written_through_the_monitor(tmp_path):
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "test.db")
    monitor = ConnectionMonitor(lambda *a: None, database=database)
    tr = EfficacyTracker(journal_db_cb=monitor._efficacy_write)
    _fails(tr, "k1", "k2")
    rows = database.query("SELECT day, from_mode, to_mode, reason, n_fail FROM efficacy_events")
    assert len(rows) == 1
    assert rows[0]["from_mode"] == "MOMENTUM" and rows[0]["to_mode"] == "INVERTED"
    assert rows[0]["reason"] == "2 consecutive fails" and rows[0]["n_fail"] == 2
    assert rows[0]["day"] == "2026-09-24"
    # wrong op / no database answer None, never raise
    assert monitor._efficacy_write("something_else", {}) is None
    database.close()
    assert ConnectionMonitor(lambda *a: None)._efficacy_write("efficacy_event", {}) is None


async def test_flip_push_reaches_telegram_as_risk_klass():
    monitor = ConnectionMonitor(lambda *a: None)
    monitor._push = AsyncMock()
    monitor._efficacy.record_entry("k1", "AAA", "buy", ET_DAY_MS)
    monitor._efficacy.record_entry("k2", "BBB", "buy", ET_DAY_MS)
    monitor._efficacy.record_outcome("k1", FAIL)
    monitor._efficacy.record_outcome("k2", FAIL)
    await asyncio.sleep(0)  # let the spawned push task run
    monitor._push.assert_awaited_once()
    args, kwargs = monitor._push.await_args
    assert "EFFICACY: flipping to INVERTED" in args[0]
    assert kwargs.get("klass") == "risk"  # survives Telegram MINIMUM mode


# -- 4. the kitchen feed ------------------------------------------------------


def _kitchen_actor(symbol="AAPL", entry=100.0):
    return SimpleNamespace(
        spec=SimpleNamespace(symbol=symbol, side=SimpleNamespace(value="buy"), qty=100.0),
        state=SimpleNamespace(value="open"),
        filled_qty=100.0,
        avg_entry_price=entry,
        entry_filled_at=datetime.now(tz=UTC),
        adopted_entry_time=None,
    )


async def test_kitchen_feeds_profit_atr_and_age_per_judged_second(tmp_path):
    from waveapp.engine.shadow_kitchen import ShadowKitchen

    feed: list = []
    actor = _kitchen_actor()
    sk = ShadowKitchen(
        hub_getter=lambda: SimpleNamespace(
            trade_tap=None, bar_builder=SimpleNamespace(bars=lambda s: []), watch=None
        ),
        actors_getter=lambda: {"key1": actor},
        journal_dir=tmp_path,
        efficacy_cb=lambda key, profit_atr, age_ms: feed.append((key, profit_atr, age_ms)),
    )
    sk._judge_mode = "shadow"  # hermetic: never read the live config
    await sk.tick()  # adopts
    sp = sk.positions["key1"]
    assert sp.entry_ms > 0  # the age anchor landed at adoption
    t0 = ((sp.entry_ms // 1000) + 2) * 1000
    sk.on_trade_tick("AAPL", 100.0, 10, t0)
    sk.on_trade_tick("AAPL", 103.0, 10, t0 + 1000)
    sk.on_trade_tick("AAPL", 103.0, 10, t0 + 2000)  # closes the 103.0 second
    await sk.tick()
    assert len(feed) >= 2
    key, profit0, age0 = feed[0]
    assert key == "key1" and age0 == t0 - sp.entry_ms
    assert abs(profit0) < 0.5  # flat at entry price
    assert feed[1][1] >= 1.0  # +3$ on a sub-$1 ATR — a clean PASS reading


async def test_kitchen_without_cb_is_untouched(tmp_path):
    from waveapp.engine.shadow_kitchen import ShadowKitchen

    actor = _kitchen_actor()
    sk = ShadowKitchen(
        hub_getter=lambda: SimpleNamespace(
            trade_tap=None, bar_builder=SimpleNamespace(bars=lambda s: []), watch=None
        ),
        actors_getter=lambda: {"key1": actor},
        journal_dir=tmp_path,
    )
    sk._judge_mode = "shadow"
    await sk.tick()
    t0 = ((sk.positions["key1"].entry_ms // 1000) + 2) * 1000
    sk.on_trade_tick("AAPL", 100.0, 10, t0)
    sk.on_trade_tick("AAPL", 100.5, 10, t0 + 1000)
    await sk.tick()  # no cb, no crash — the judge just judges


# -- 5. monitor entry/close recording ----------------------------------------


def _tracked_actor(symbol="HOT", state="open", filled_at=None):
    return SimpleNamespace(
        spec=SimpleNamespace(
            symbol=symbol,
            side=OrderSide.BUY,
            strategy="GAP",
            stop_price=50.0,
        ),
        state=SimpleNamespace(value=state),
        avg_entry_price=52.0,
        realized_pnl=-100.0,
        exit_reason="stop hit",
        entry_filled_at=filled_at or datetime.now(tz=UTC),
        adopted_entry_time=None,
    )


def test_track_closed_trades_records_entry_then_scores_close_as_fail():
    monitor = ConnectionMonitor(lambda *a: None)
    actor = _tracked_actor()
    monitor.engine = SimpleNamespace(actors={"key9": actor})
    monitor._track_closed_trades()  # first sight of the open fill
    assert "key9" in monitor._efficacy._entries
    actor.state = SimpleNamespace(value="closed")
    monitor._track_closed_trades()  # stop-close 2s later — inside the window
    assert monitor._efficacy.consecutive_fails == 1


def test_adopted_position_older_than_the_window_is_not_scored():
    from datetime import timedelta

    monitor = ConnectionMonitor(lambda *a: None)
    stale = _tracked_actor(filled_at=datetime.now(tz=UTC) - timedelta(minutes=25))
    monitor.engine = SimpleNamespace(actors={"old1": stale})
    monitor._track_closed_trades()
    assert "old1" not in monitor._efficacy._entries  # nothing scoreable
    stale.state = SimpleNamespace(value="closed")
    monitor._track_closed_trades()
    assert monitor._efficacy.n_scored == 0


# -- 6. the entry pipeline (fixture mirrors test_chop_invert's) ---------------


def _pipeline_monitor(monkeypatch, tmp_path, **config_kwargs):
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
                et_time=datetime(2026, 9, 24, 9, 46, tzinfo=ET),
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
            "HOT", px, vol, datetime(2026, 9, 24, 9, minute, tzinfo=ET)
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


FIXTURE_T_MS = int(datetime(2026, 9, 24, 9, 35, tzinfo=UTC).timestamp() * 1000)


def _force_inverted(monitor) -> None:
    """Two instant fails — the guard's flip — via the public tracker API,
    stamped on the fixture's ET day so the pipeline's day roll keeps it."""
    from waveapp.engine.session import ET

    t_ms = int(datetime(2026, 9, 24, 9, 33, tzinfo=ET).timestamp() * 1000)
    tr = monitor._efficacy
    tr.record_entry("f1", "AAA", "buy", t_ms)
    tr.record_entry("f2", "BBB", "buy", t_ms + 1000)
    tr.record_outcome("f1", FAIL)
    tr.record_outcome("f2", FAIL)
    assert tr.mode() == MODE_INVERTED


async def test_inverted_mode_inverts_at_full_size(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    _force_inverted(monitor)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    signal = executed[0]
    assert signal.side is OrderSide.SELL  # the failing breakout, shorted
    assert signal.reason.startswith(EFFICACY_INVERT_PREFIX)
    assert signal.half_size is False  # FULL size — never shrink
    assert signal.stop_price > signal.entry_price  # stop mirrored above
    assert signal.strategy == "GAP"  # journal tag untouched
    assert is_inverted(signal) and not is_chop_inverted(signal)
    assert monitor._chop_inverted_today == 0  # never burns the CHOP cap


async def test_uninvertible_signal_is_skipped_and_mode_stays(monkeypatch, tmp_path):
    """shorts off → the flipped BUY would be an impossible short: skip that
    ONE signal, loudly, and stay INVERTED (never execute momentum as-is)."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path)
    _force_inverted(monitor)
    await monitor._entry_pipeline(candidates)
    assert executed == []
    assert monitor._efficacy.mode() == MODE_INVERTED


async def test_flag_off_is_byte_identical_to_baseline(monkeypatch, tmp_path):
    base_monitor, base_candidates, base_exec = _pipeline_monitor(
        monkeypatch, tmp_path, shorts_enabled=True
    )
    await base_monitor._entry_pipeline(base_candidates)

    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, shorts_enabled=True, efficacy_guard=False
    )
    _force_inverted(monitor)  # a screaming INVERTED verdict the flag mutes
    await monitor._entry_pipeline(candidates)

    assert len(base_exec) == len(executed) == 1
    assert executed[0] == base_exec[0]  # frozen dataclass equality: every field
    assert executed[0].side is OrderSide.BUY
    assert not is_inverted(executed[0])


async def test_momentum_mode_leaves_the_pipeline_untouched(monkeypatch, tmp_path):
    """Guard ON (the default) in MOMENTUM — the normal state — must equal
    the baseline exactly."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    assert monitor._efficacy.mode() == MODE_MOMENTUM
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.BUY
    assert not is_inverted(executed[0]) and executed[0].half_size is False


async def test_chop_and_efficacy_compose_to_one_inversion_full_size(monkeypatch, tmp_path):
    """CHOP verdict + INVERTED mode both want the flip: invert ONCE (the
    shared transform, CHOP's application), and efficacy's full-size wins
    over chop's half-size."""
    monitor, candidates, executed = _pipeline_monitor(
        monkeypatch, tmp_path, shorts_enabled=True, chop_invert=True
    )
    monitor._day_judge = SimpleNamespace(verdict="CHOP", confidence=0.9)
    _force_inverted(monitor)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    signal = executed[0]
    assert signal.side is OrderSide.SELL
    assert signal.reason.startswith(CHOP_INVERT_PREFIX)
    assert signal.reason.count("INVERTED: ") == 1  # once, not twice
    assert signal.half_size is False  # efficacy's full size wins
    assert monitor._chop_inverted_today == 1  # the CHOP flip still counts


class _FakeStrategy:
    def __init__(self, name, strategy_tag, half=False):
        self.name = name
        self._tag = strategy_tag
        self._half = half

    def evaluate(self, features, bars, vwap, regime, is_lull=False, now=None):
        return EntrySignal(
            symbol=features.symbol,
            side=OrderSide.BUY,
            confidence=0.9,
            reason="pullback hold" if self._tag == "FPB" else "momentum break",
            strategy=self._tag,
            entry_price=52.0,
            stop_price=50.0,
            half_size=self._half,
        )


async def test_fpb_lane_is_exempt_in_inverted_mode(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    _force_inverted(monitor)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("FPB", "FPB")],
    )
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.BUY  # pullback logic — never inverted
    assert not is_inverted(executed[0])


async def test_strategy_half_size_survives_the_full_size_override(monkeypatch, tmp_path):
    """FULL size means the strategy's OWN size, not a blanket un-halving: a
    signal the strategy itself sized half (midday lull) stays half when
    efficacy inverts it."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    _force_inverted(monitor)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies",
        lambda regime: [_FakeStrategy("GAP", "GAP", half=True)],
    )
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert is_inverted(executed[0])
    assert executed[0].half_size is True  # the strategy's truth, preserved


def test_trend_gate_stands_aside_for_efficacy_inverted_signals():
    monitor = ConnectionMonitor(lambda *a: None)
    signal = EntrySignal(
        symbol="HOT",
        side=OrderSide.BUY,
        confidence=0.8,
        reason="gap break",
        strategy="GAP",
        entry_price=50.0,
        stop_price=49.0,
    )
    flipped = invert_signal(signal, prefix=EFFICACY_INVERT_PREFIX)
    assert flipped is not None and flipped.side is OrderSide.SELL
    features = SimpleNamespace(symbol="HOT", price=50.0, day_open=49.5)
    # an ordinary short above the open is vetoed; the inverted one is not
    assert monitor._trend_gate_vetoes(
        EntrySignal("HOT", OrderSide.SELL, 0.8, "raw", "GAP", 50.0, 51.0), features
    )
    assert not monitor._trend_gate_vetoes(flipped, features)


# -- 7. the replay: the −$3,928 morning, with the guard in place --------------


async def test_replay_of_the_3928_morning(monkeypatch, tmp_path):
    """2026-09-24 as it actually ran: two full-size losers inside 25 minutes
    of the open, then more of the same with zero feedback. With the guard:
    the two instant fails flip the book to INVERTED, and entry 3 onward
    executes the SAME momentum triggers inverted, at FULL size."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    from waveapp.engine.session import ET

    tr = monitor._efficacy

    # 09:32 — entries 1 and 2 fill at full size (the real morning's opener)
    t1 = int(datetime(2026, 9, 24, 9, 32, tzinfo=ET).timestamp() * 1000)
    tr.record_entry("pos1", "LOSER1", "buy", t1)
    tr.record_entry("pos2", "LOSER2", "buy", t1 + 30_000)
    assert tr.mode() == MODE_MOMENTUM

    # the kitchen's per-second feed watches them die: −1A inside minutes
    monitor._efficacy_feed("pos1", -0.4, 60_000)  # bleeding, not scored yet
    assert tr.mode() == MODE_MOMENTUM
    monitor._efficacy_feed("pos1", -1.1, 150_000)  # first touch of −1A: FAIL
    monitor._efficacy_feed("pos2", -1.0, 180_000)  # FAIL #2
    assert tr.mode() == MODE_INVERTED  # the reflex fired ~09:35, not 11:12

    # 09:46 — the NEXT momentum trigger (entry 3) executes INVERTED, full size
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    entry3 = executed[0]
    assert entry3.side is OrderSide.SELL
    assert entry3.reason.startswith(EFFICACY_INVERT_PREFIX)
    assert entry3.half_size is False  # FULL size — Wave makes money, not smaller bets

    # the inverted book now judges itself: if IT starts failing, the guard
    # flips back after its hysteresis sample (symmetry, tested above)
    tr.record_entry("pos3", "HOT", "sell", t1 + 840_000)
    assert tr._entries["pos3"].mode == MODE_INVERTED
