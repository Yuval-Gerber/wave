"""PRE-OPEN BIAS BRAIN (R0.5, 2026-09-24): the front half of the efficacy
guard — one verdict ~9:25 ET from pre-market evidence (SPY/QQQ gap + the
watched-universe gap map), steering entry strictness from the FIRST trade
until the Day Judge's first real verdict, and seeding the efficacy tracker
so the first counter-bias fail flips the book after ONE fail instead of two.

Covers: the verdict rules (short/long/neutral, conflicts, thin evidence),
the one-shot 9:25 window, the day_regime journal (PREOPEN_ prefix), the Day
Judge handoff, the efficacy bias_hint seed, the monitor tick from scanner2
arrays (row + risk-klass push + seed), and the entry pipeline (SELL
unchanged under SHORT bias, BUY held to green-vs-prior-close — never
hard-blocked; NEUTRAL and flag-off byte-identical; inverted/FPB lanes
exempt) — ending with the 09-24 morning replayed WITH the brain."""

import asyncio
import dataclasses
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.test_efficacy import _FakeStrategy, _force_inverted, _pipeline_monitor
from waveapp.broker.base import OrderSide
from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.engine.efficacy import FAIL, MODE_INVERTED, MODE_MOMENTUM, EfficacyTracker
from waveapp.engine.preopen import LONG_BIAS, NEUTRAL, SHORT_BIAS, PreOpenBias
from waveapp.engine.session import ET
from waveapp.engine.strategies import EntrySignal


def _et(hour: int, minute: int, day: int = 24) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=ET)


DAY = "2026-09-24"


# -- 1. the verdict rules -----------------------------------------------------


def test_short_bias_from_red_premarket():
    po = PreOpenBias()
    bias, conf = po.compute(_et(9, 25), -0.5, -0.6, 0.71, 80)
    assert bias == SHORT_BIAS and po.bias == SHORT_BIAS
    assert 0.5 <= conf <= 0.9  # never certain — the study went 0/4
    assert po.governs(DAY) and not po.governs("2026-09-25")
    assert po.hint() == "SHORT"
    head = po.headline()
    assert "PRE-OPEN: SHORT bias" in head
    assert "SPY -0.5%" in head and "71% of names gapping down" in head
    assert head.endswith("shorts full, longs strict.")


def test_long_bias_is_the_mirror():
    po = PreOpenBias()
    bias, conf = po.compute(_et(9, 25), +0.5, +0.4, 0.25, 60)
    assert bias == LONG_BIAS and conf >= 0.5
    assert po.hint() == "LONG"
    head = po.headline()
    assert "LONG bias" in head and "75% of names gapping up" in head
    assert head.endswith("longs full, shorts strict.")


def test_neutral_on_small_gap_conflict_or_thin_evidence():
    # small gap, even with a red map — NEUTRAL
    po = PreOpenBias()
    assert po.compute(_et(9, 25), -0.1, -0.05, 0.75, 80)[0] == NEUTRAL
    assert po.confidence == 0.0 and po.hint() is None
    assert not po.governs(DAY)  # NEUTRAL never governs
    assert "efficacy discovers" in po.headline()
    # gap and map disagree (red gap, green map) — NEUTRAL
    assert PreOpenBias().compute(_et(9, 25), -0.5, -0.5, 0.30, 80)[0] == NEUTRAL
    # thin map (n < MIN_MAP_N) — NEUTRAL, and the reason says so
    po3 = PreOpenBias()
    assert po3.compute(_et(9, 25), -0.5, -0.6, 0.90, 5)[0] == NEUTRAL
    assert "gap map unknown" in po3.reasons
    # no index prints at all — NEUTRAL
    assert PreOpenBias().compute(_et(9, 25), None, None, 0.75, 80)[0] == NEUTRAL


def test_one_index_missing_still_reads_the_other():
    po = PreOpenBias()
    assert po.compute(_et(9, 25), -0.4, None, 0.70, 40)[0] == SHORT_BIAS


def test_compute_window_is_925_to_930_once_per_day():
    po = PreOpenBias()
    assert not po.should_compute(_et(9, 24))
    assert po.should_compute(_et(9, 25))
    assert po.should_compute(_et(9, 29))
    assert not po.should_compute(_et(9, 30))  # a late start computes nothing
    po.compute(_et(9, 25), -0.5, -0.6, 0.71, 80)
    assert not po.should_compute(_et(9, 26))  # one verdict per day
    assert po.should_compute(_et(9, 25, day=25))  # tomorrow is a new day


def test_journal_writes_one_day_regime_row_with_preopen_prefix():
    calls = []
    po = PreOpenBias(journal_db_cb=lambda op, payload: calls.append((op, payload)))
    po.compute(_et(9, 25), -0.5, -0.6, 0.71, 80)
    assert len(calls) == 1
    op, payload = calls[0]
    assert op == "day_regime"
    assert payload["verdict"] == "PREOPEN_SHORT_BIAS"
    assert payload["breadth"] == 0.71
    assert payload["confidence"] == po.confidence
    evidence = json.loads(payload["evidence"])
    assert evidence["spy_gap_pct"] == -0.5 and evidence["n_map"] == 80
    # NEUTRAL journals too — the day's read is on record either way
    calls.clear()
    PreOpenBias(journal_db_cb=lambda op, p: calls.append((op, p))).compute(
        _et(9, 25), 0.0, 0.0, 0.5, 80
    )
    assert calls[0][1]["verdict"] == "PREOPEN_NEUTRAL"


def test_broken_journal_never_wounds_the_verdict():
    def boom(op, payload):
        raise RuntimeError("dead pen")

    po = PreOpenBias(journal_db_cb=boom)
    assert po.compute(_et(9, 25), -0.5, -0.6, 0.71, 80)[0] == SHORT_BIAS


# -- 2. the Day Judge handoff -------------------------------------------------


def test_first_real_day_judge_verdict_retires_the_bias():
    po = PreOpenBias()
    assert not po.note_day_judge("TREND_DOWN")  # nothing computed yet — no-op
    po.compute(_et(9, 25), -0.5, -0.6, 0.71, 80)
    assert not po.note_day_judge("UNCLEAR")  # UNCLEAR is not a verdict
    assert po.governs(DAY)
    assert po.note_day_judge("TREND_DOWN")  # the handoff, exactly once
    assert not po.governs(DAY) and po.retired
    assert not po.note_day_judge("CHOP")  # already handed off
    # a new day's compute resets the retirement
    po.compute(_et(9, 25, day=25), -0.5, -0.6, 0.71, 80)
    assert po.governs("2026-09-25")


# -- 3. the efficacy seed (bias_hint) -----------------------------------------


def _entry(tr, key, side, t=None):
    t = t or int(_et(9, 32).timestamp() * 1000)
    tr.record_entry(key, key.upper(), side, t)


def test_first_counter_bias_fail_flips_after_one():
    events = []
    tr = EfficacyTracker(journal_db_cb=lambda op, p: events.append(p))
    tr.set_bias_hint("SHORT", day=DAY)
    _entry(tr, "k1", "buy")  # a long fighting the called-short tape
    tr.record_outcome("k1", FAIL)
    assert tr.mode() == MODE_INVERTED  # ONE fail, not two
    assert events[0]["reason"] == "first counter-bias fail (pre-open SHORT bias)"


def test_aligned_side_fail_keeps_the_normal_two_fail_ladder():
    tr = EfficacyTracker()
    tr.set_bias_hint("SHORT", day=DAY)
    _entry(tr, "s1", "sell")  # a short WITH the bias — its fail is ordinary
    tr.record_outcome("s1", FAIL)
    assert tr.mode() == MODE_MOMENTUM
    _entry(tr, "s2", "sell")
    tr.record_outcome("s2", FAIL)
    assert tr.mode() == MODE_INVERTED  # the normal second fail


def test_long_hint_mirrors_for_shorts():
    tr = EfficacyTracker()
    tr.set_bias_hint("LONG", day=DAY)
    _entry(tr, "s1", "sell")
    tr.record_outcome("s1", FAIL)
    assert tr.mode() == MODE_INVERTED


def test_without_a_hint_one_fail_never_flips():
    tr = EfficacyTracker()
    _entry(tr, "k1", "buy")
    tr.record_outcome("k1", FAIL)
    assert tr.mode() == MODE_MOMENTUM


def test_hint_clears_on_day_rollover():
    tr = EfficacyTracker()
    tr.set_bias_hint("SHORT", day=DAY)
    tr.roll_day("2026-09-25")
    assert tr.bias_hint is None
    _entry(tr, "k1", "buy", int(_et(9, 32, day=25).timestamp() * 1000))
    tr.record_outcome("k1", FAIL)
    assert tr.mode() == MODE_MOMENTUM  # back to the normal ladder


def test_hint_validation_normalizes_and_refuses_garbage():
    tr = EfficacyTracker()
    tr.set_bias_hint("short", day=DAY)
    assert tr.bias_hint == "SHORT"
    tr.set_bias_hint("sideways?!")
    assert tr.bias_hint is None


# -- 4. the monitor tick: scanner2 arrays → verdict + row + push + seed -------


def _fake_s2(spy_gap=-0.5, qqq_gap=-0.6, n_down=9, n_up=3):
    symbols = ["SPY", "QQQ"] + [f"N{i}" for i in range(n_down + n_up)]
    last, prev = [], []
    for i, _sym in enumerate(symbols):
        if i == 0:
            prev.append(100.0), last.append(100.0 * (1 + spy_gap / 100))
        elif i == 1:
            prev.append(100.0), last.append(100.0 * (1 + qqq_gap / 100))
        elif i - 2 < n_down:
            prev.append(50.0), last.append(49.0)  # gapping down
        else:
            prev.append(50.0), last.append(51.0)  # gapping up
    return SimpleNamespace(
        _index={s: i for i, s in enumerate(symbols)},
        last=last,
        prev_close_live=prev,
        focus=set(symbols[2:]),
        last_menu=[],
    )


async def test_preopen_tick_computes_journals_pushes_and_seeds(tmp_path):
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "test.db")
    monitor = ConnectionMonitor(lambda *a: None, database=database)
    monitor._scanner2 = _fake_s2()
    monitor._push = AsyncMock()
    monitor._preopen_tick(_et(9, 25))
    await asyncio.sleep(0)  # let the spawned push run
    assert monitor._preopen.bias == SHORT_BIAS
    rows = database.query("SELECT verdict, confidence, breadth FROM day_regime")
    assert len(rows) == 1 and rows[0]["verdict"] == "PREOPEN_SHORT_BIAS"
    assert rows[0]["breadth"] == 0.75  # 9 of 12 watched names gapping down
    monitor._push.assert_awaited_once()
    args, kwargs = monitor._push.await_args
    assert args[0].startswith("PRE-OPEN: SHORT bias")
    assert kwargs.get("klass") == "risk"  # survives Telegram MINIMUM mode
    assert monitor._efficacy.bias_hint == "SHORT"  # the seed landed
    # same minute again: one verdict per day — no second row, no second push
    monitor._preopen_tick(_et(9, 26))
    await asyncio.sleep(0)
    assert len(database.query("SELECT 1 FROM day_regime")) == 1
    monitor._push.assert_awaited_once()
    database.close()


async def test_preopen_tick_flag_off_does_nothing(monkeypatch, tmp_path):
    from waveapp.config import AppConfig

    config_path = tmp_path / "config.toml"
    AppConfig(preopen_bias=False).save(config_path)
    monkeypatch.setattr("waveapp.config.config_path", lambda: config_path)
    monitor = ConnectionMonitor(lambda *a: None)
    monitor._scanner2 = _fake_s2()
    monitor._push = AsyncMock()
    monitor._preopen_tick(_et(9, 25))
    await asyncio.sleep(0)
    assert monitor._preopen.computed_day is None  # never computed
    monitor._push.assert_not_awaited()
    assert monitor._efficacy.bias_hint is None


def test_preopen_tick_handoff_clears_the_efficacy_seed():
    monitor = ConnectionMonitor(lambda *a: None)
    monitor._scanner2 = _fake_s2()
    monitor._preopen_tick(_et(9, 25))
    assert monitor._preopen.governs(DAY) and monitor._efficacy.bias_hint == "SHORT"
    # 10:07 — the Day Judge publishes its first real verdict
    monitor._day_judge = SimpleNamespace(verdict="TREND_DOWN", confidence=0.8)
    monitor._preopen_tick(_et(10, 7))
    assert monitor._preopen.retired and not monitor._preopen.governs(DAY)
    assert monitor._efficacy.bias_hint is None  # the live verdict owns the day


def test_preopen_tick_survives_a_missing_scanner2():
    monitor = ConnectionMonitor(lambda *a: None)
    monitor._scanner2 = None
    monitor._preopen_tick(_et(9, 25))  # no arrays → NEUTRAL, never a raise
    assert monitor._preopen.bias == NEUTRAL


# -- 5. the entry pipeline ----------------------------------------------------


def _bias(monitor, which):
    if which == SHORT_BIAS:
        monitor._preopen.compute(_et(9, 25), -0.5, -0.6, 0.75, 80)
    elif which == LONG_BIAS:
        monitor._preopen.compute(_et(9, 25), +0.5, +0.6, 0.25, 80)
    else:
        monitor._preopen.compute(_et(9, 25), 0.0, 0.0, 0.5, 80)
    assert monitor._preopen.bias == which


def _with_prev_close(candidates, prev):
    c = candidates[0]
    return [dataclasses.replace(c, features=dataclasses.replace(c.features, prev_close=prev))]


class _FakeSellStrategy:
    name = "SHORTMOM"

    def evaluate(self, features, bars, vwap, regime, is_lull=False, now=None):
        return EntrySignal(
            symbol=features.symbol,
            side=OrderSide.SELL,
            confidence=0.9,
            reason="breakdown",
            strategy="GAP",
            entry_price=52.0,
            stop_price=54.0,
        )


async def test_short_bias_green_long_passes_red_long_skipped(monkeypatch, tmp_path):
    """Never a hard block: a monster gapper bucking the tape (GREEN vs its
    own prior close) trades; a red name chasing longs into a called-short
    tape does not."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies", lambda regime: [_FakeStrategy("GAP", "GAP")]
    )
    _bias(monitor, SHORT_BIAS)
    # entry 52.0 vs prior close 50.0 — green, real relative strength
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    assert executed[0].side is OrderSide.BUY and executed[0].reason == "momentum break"
    # entry 52.0 vs prior close 60.0 — red vs yesterday: held back
    monitor2, candidates2, executed2 = _pipeline_monitor(monkeypatch, tmp_path)
    _bias(monitor2, SHORT_BIAS)
    await monitor2._entry_pipeline(_with_prev_close(candidates2, 60.0))
    assert executed2 == []
    assert "HOT" not in monitor2._signaled  # not burned — re-fires next cycle


async def test_short_bias_sell_momentum_unchanged(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies", lambda regime: [_FakeSellStrategy()]
    )
    _bias(monitor, SHORT_BIAS)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1
    signal = executed[0]
    assert signal.side is OrderSide.SELL
    assert signal.reason == "breakdown" and signal.half_size is False  # untouched


async def test_long_bias_mirror_for_shorts(monkeypatch, tmp_path):
    """LONG bias: a short must be RED vs its own prior close; a red-tape
    breakdown on a genuinely weak name still trades."""
    monkey_strats = lambda regime: [_FakeSellStrategy()]  # noqa: E731
    # entry 52.0 vs prior close 60.0 — red name, breakdown is real: passes
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    monkeypatch.setattr("waveapp.engine.strategies.armed_strategies", monkey_strats)
    _bias(monitor, LONG_BIAS)
    await monitor._entry_pipeline(_with_prev_close(candidates, 60.0))
    assert len(executed) == 1 and executed[0].side is OrderSide.SELL
    # entry 52.0 vs prior close 50.0 — shorting a green name into LONG bias: held
    monitor2, candidates2, executed2 = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    _bias(monitor2, LONG_BIAS)
    await monitor2._entry_pipeline(candidates2)
    assert executed2 == []


async def test_neutral_bias_is_byte_identical(monkeypatch, tmp_path):
    base_monitor, base_candidates, base_exec = _pipeline_monitor(monkeypatch, tmp_path)
    await base_monitor._entry_pipeline(base_candidates)
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path)
    _bias(monitor, NEUTRAL)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == len(base_exec) == 1
    assert executed[0] == base_exec[0]  # frozen dataclass equality: every field


async def test_flag_off_is_byte_identical_even_with_a_verdict(monkeypatch, tmp_path):
    base_monitor, base_candidates, base_exec = _pipeline_monitor(monkeypatch, tmp_path)
    await base_monitor._entry_pipeline(_with_prev_close(base_candidates, 60.0))
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, preopen_bias=False)
    _bias(monitor, SHORT_BIAS)  # a verdict exists, but the flag is OFF
    await monitor._entry_pipeline(_with_prev_close(candidates, 60.0))
    assert len(executed) == len(base_exec) == 1
    assert executed[0] == base_exec[0]


async def test_retired_bias_no_longer_gates(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path)
    _bias(monitor, SHORT_BIAS)
    monitor._preopen.note_day_judge("CHOP")  # the Day Judge has spoken
    await monitor._entry_pipeline(_with_prev_close(candidates, 60.0))
    assert len(executed) == 1  # the red BUY trades again — the bias retired


async def test_efficacy_inverted_signals_are_exempt(monkeypatch, tmp_path):
    """LONG bias + INVERTED book: the momentum BUY flips to SELL at entry
    52 >= prior close 50 (not red) — the strict check would kill it, but
    inverted signals belong to the efficacy flow and are exempt."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    _bias(monitor, LONG_BIAS)
    _force_inverted(monitor)
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1 and executed[0].side is OrderSide.SELL


async def test_fpb_lane_is_exempt(monkeypatch, tmp_path):
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "waveapp.engine.strategies.armed_strategies", lambda regime: [_FakeStrategy("FPB", "FPB")]
    )
    _bias(monitor, SHORT_BIAS)
    await monitor._entry_pipeline(_with_prev_close(candidates, 60.0))
    assert len(executed) == 1 and executed[0].side is OrderSide.BUY  # pullback lane untouched


# -- 6. the 09-24 morning, replayed WITH the brain ----------------------------


async def test_replay_0924_with_the_preopen_brain(monkeypatch, tmp_path):
    """The −$3,928 morning's actual pre-open evidence (SPY −0.44%, 74% of
    the menu gapping down — STUDY.md row 09-24) reads SHORT bias at 9:25.
    The first full-size long FAIL then flips the book after ONE fail — the
    R1.5 replay flipped at the second; the brain buys back one full-size
    loser (−$700+ that morning)."""
    monitor, candidates, executed = _pipeline_monitor(monkeypatch, tmp_path, shorts_enabled=True)
    monitor._scanner2 = _fake_s2(spy_gap=-0.44, qqq_gap=-0.79, n_down=89, n_up=31)
    monitor._preopen_tick(_et(9, 25))
    assert monitor._preopen.bias == SHORT_BIAS
    assert monitor._efficacy.bias_hint == "SHORT"

    # 9:32 — the first long fills anyway (green vs prior close) and dies
    tr = monitor._efficacy
    t1 = int(_et(9, 32).timestamp() * 1000)
    tr.record_entry("pos1", "LOSER1", "buy", t1)
    monitor._efficacy_feed("pos1", -1.1, 150_000)  # first touch of −1A: FAIL
    assert tr.mode() == MODE_INVERTED  # ONE counter-bias fail — not two

    # the next momentum trigger executes inverted, full size
    await monitor._entry_pipeline(candidates)
    assert len(executed) == 1 and executed[0].side is OrderSide.SELL
    assert executed[0].half_size is False
