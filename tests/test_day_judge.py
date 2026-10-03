"""R0 — the DAY JUDGE (day-type detector, 2026-09-23, the −$854 chop day):
rule verdicts, warmup gate, hysteresis, journal, the monitor's breadth-from-
scanner2-arrays wiring + snapshot field, and the Scanner tab badge."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np

from waveapp.engine.day_judge import (
    CHOP_PERSIST_MIN,
    HYSTERESIS_MIN,
    TREND_PERSIST_MIN,
    DayJudge,
    DayVerdict,
)

ET = ZoneInfo("America/New_York")


def _t(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 23, hour, minute, 5, tzinfo=ET)


def _feed(judge: DayJudge, start: datetime, breadths, **kw) -> datetime:
    """One update per minute from `start`; returns the next free minute."""
    now = start
    for b in breadths:
        judge.update(now, b, **kw)
        now += timedelta(minutes=1)
    return now


# -- rules v1 ------------------------------------------------------------------


def test_before_1000_et_always_unclear():
    judge = DayJudge()
    # a screaming down-tape from the open — still no verdict before 10:00
    _feed(judge, _t(9, 31), [0.80] * 28)  # 9:31 .. 9:58
    assert judge.verdict is DayVerdict.UNCLEAR
    assert judge.confidence == 0.0


def test_trend_down_tape_publishes_after_warmup():
    journal: list[tuple[str, dict]] = []
    judge = DayJudge(journal_db_cb=lambda op, p: journal.append((op, p)))
    # extreme downside breadth 9:31 → 10:04: streak and hysteresis are long
    # satisfied by 10:00, so the verdict lands at the first post-warmup minute
    _feed(judge, _t(9, 31), [0.72] * 34)
    assert judge.verdict is DayVerdict.TREND_DOWN
    assert 0.0 < judge.confidence <= 1.0
    assert [op for op, _ in journal] == ["day_regime"]
    row = journal[0][1]
    assert row["verdict"] == "TREND_DOWN"
    assert row["breadth"] == 0.72
    assert 0.0 < row["confidence"] <= 1.0
    assert "below-open" in row["evidence"]


def test_trend_up_mirror():
    judge = DayJudge()
    _feed(judge, _t(9, 31), [0.25] * 40)
    assert judge.verdict is DayVerdict.TREND_UP


def test_oscillating_band_breadth_is_chop():
    judge = DayJudge()
    # oscillates inside 0.40-0.60 with ~zero net drift (period 4 → the
    # 20-minute drift lookback lands on the same phase)
    wave = [0.48, 0.55, 0.52, 0.45] * 12
    _feed(judge, _t(9, 31), wave)
    assert judge.verdict is DayVerdict.CHOP
    assert 0.0 < judge.confidence <= 1.0


def test_flip_needs_hysteresis_minutes():
    judge = DayJudge()
    end = _feed(judge, _t(9, 31), [0.72] * 45)  # TREND_DOWN, well established
    assert judge.verdict is DayVerdict.TREND_DOWN
    # tape mean-reverts into the band: raw turns UNCLEAR (band streak is
    # still short of CHOP_PERSIST_MIN) but the published verdict must hold
    # for HYSTERESIS_MIN−1 minutes first
    end = _feed(judge, end, [0.50] * (HYSTERESIS_MIN - 1))
    assert judge.verdict is DayVerdict.TREND_DOWN
    _feed(judge, end, [0.50])
    assert judge.verdict is DayVerdict.UNCLEAR
    # …and once the band has held CHOP_PERSIST_MIN minutes, CHOP publishes
    _feed(judge, end + timedelta(minutes=1), [0.50] * CHOP_PERSIST_MIN)
    assert judge.verdict is DayVerdict.CHOP


def test_confidence_sane_and_journal_only_on_changes():
    journal: list[dict] = []
    judge = DayJudge(journal_db_cb=lambda op, p: journal.append(p))
    tape = [0.55] * 10 + [0.70] * 40 + [0.50] * 40 + [0.30] * 40
    now = _t(9, 31)
    for b in tape:
        _, conf = judge.update(now, b, counters={"signals": 1})
        assert 0.0 <= conf <= 1.0
        now += timedelta(minutes=1)
    verdicts = [row["verdict"] for row in journal]
    # journal rows are CHANGES only — consecutive rows never repeat
    assert all(a != b for a, b in zip(verdicts, verdicts[1:], strict=False))
    assert "TREND_DOWN" in verdicts and "TREND_UP" in verdicts
    assert all('"counters"' in row["evidence"] for row in journal)


def test_trend_needs_persistence_not_one_spike():
    judge = DayJudge()
    # post-warmup: extreme breadth but only briefly — no trend call
    _feed(judge, _t(10, 30), [0.50] * 5 + [0.70] * (TREND_PERSIST_MIN - 5))
    assert judge.verdict is DayVerdict.UNCLEAR


def test_day_rollover_resets_without_journaling():
    journal: list[dict] = []
    judge = DayJudge(journal_db_cb=lambda op, p: journal.append(p))
    _feed(judge, _t(9, 31), [0.72] * 40)
    assert judge.verdict is DayVerdict.TREND_DOWN
    rows_before = len(journal)
    judge.update(_t(9, 31) + timedelta(days=1), 0.72)
    assert judge.verdict is DayVerdict.UNCLEAR  # fresh day, fresh eyes
    assert len(journal) == rows_before  # the reset itself is not a verdict


def test_none_breadth_is_skipped():
    judge = DayJudge()
    _feed(judge, _t(9, 31), [0.72] * 40)
    assert judge.verdict is DayVerdict.TREND_DOWN
    judge.update(_t(10, 12), None)
    assert judge.verdict is DayVerdict.TREND_DOWN  # no sample, no change


# -- monitor wiring ------------------------------------------------------------


def _monitor(**kw):
    from waveapp.engine.connection_monitor import ConnectionMonitor

    return ConnectionMonitor(lambda *a: None, **kw)


def test_day_judge_breadth_reads_scanner2_live_arrays():
    monitor = _monitor()
    monitor._scanner2 = SimpleNamespace(
        _index={"AAA": 0, "BBB": 1, "CCC": 2, "DDD": 3},
        last=np.array([9.0, 11.0, 0.0, 8.0], dtype=np.float32),
        day_open=np.array([10.0, 10.0, 10.0, 10.0], dtype=np.float32),
        focus={"AAA", "BBB"},
    )
    monitor._s2_watched = {"CCC", "DDD", "ZZZ"}  # day list ∪ focus, like the CSV
    breadth, median_abs = monitor._day_judge_breadth()
    # CCC has no print yet, ZZZ is unknown → 3 ok symbols, 2 below open
    assert breadth is not None and abs(breadth - 2 / 3) < 1e-9
    assert abs(median_abs - 10.0) < 1e-6  # |−10|, |+10|, |−20| → median 10


def test_day_judge_breadth_none_without_menu():
    monitor = _monitor()
    assert monitor._day_judge_breadth() == (None, None)  # no scanner2 at all
    monitor._scanner2 = SimpleNamespace(
        _index={}, last=np.zeros(0), day_open=np.zeros(0), focus=set()
    )
    assert monitor._day_judge_breadth() == (None, None)


def test_system_snapshot_carries_day_regime():
    monitor = _monitor()
    data = monitor._system_data()
    assert data["day_regime"] == {"verdict": "UNCLEAR", "confidence": 0.0}


def test_day_regime_row_written_through_the_monitor(tmp_path):
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "t.db")
    monitor = _monitor(database=database)
    judge = DayJudge(journal_db_cb=monitor._day_regime_write)
    _feed(judge, _t(9, 31), [0.72] * 40)
    rows = database.query("SELECT verdict, confidence, breadth, evidence FROM day_regime")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "TREND_DOWN"
    assert rows[0]["breadth"] == 0.72
    assert 0.0 < rows[0]["confidence"] <= 1.0
    # wrong op / no db are inert, never raise
    assert monitor._day_regime_write("something_else", {}) is None
    assert _monitor()._day_regime_write("day_regime", {}) is None
    database.close()


# -- Scanner tab badge ---------------------------------------------------------


def test_badge_renders_every_verdict(qtbot):
    from waveapp.ui.scanner_page import ScannerPage

    page = ScannerPage()
    qtbot.addWidget(page)
    assert page.day_badge.text() == "· reading the day…"  # default before data
    page.set_regime({"regime": "WARMUP", "day_regime": {"verdict": "CHOP", "confidence": 0.7}})
    assert page.day_badge.text() == "↔️ CHOP (0.7)"
    assert "#FF9500" in page.day_badge.styleSheet()
    page.set_day_regime({"verdict": "TREND_UP", "confidence": 0.8})
    assert page.day_badge.text() == "📈 TREND ↑ (0.8)"
    assert "#28CD41" in page.day_badge.styleSheet()
    page.set_day_regime({"verdict": "TREND_DOWN", "confidence": 0.55})
    assert page.day_badge.text().startswith("📉 TREND ↓")
    assert "#FF3B30" in page.day_badge.styleSheet()
    page.set_day_regime({"verdict": "UNCLEAR", "confidence": 0.0})
    assert page.day_badge.text() == "· reading the day…"
    assert "#8E8E93" in page.day_badge.styleSheet()
    # garbage payloads degrade to UNCLEAR, never raise
    page.set_day_regime({"verdict": "MARTIAN", "confidence": "nan-ish"})
    assert page.day_badge.text() == "· reading the day…"
