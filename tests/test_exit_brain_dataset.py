"""Exit-brain dataset builder (blueprint §II.11 finding #2): labeler math on
synthetic paths, R-normalization fallback, migration 009, and idempotency.

The forward label from minute i asks: does the position later gain
>= +0.25R more before retracing an additional 0.5R from the current level?
A path that runs +1R then fades must label the early minutes 1 (more profit
was ahead) and the post-peak minutes 0 (that was the peak).
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from waveapp.research.history import HistoryStore, MinuteBar

from waveapp.persistence.db import Database

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "exit_brain_dataset.py"
_spec = importlib.util.spec_from_file_location("exit_brain_dataset", _SCRIPT)
ebd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ebd)

T0 = int(datetime(2026, 9, 2, 14, 30, tzinfo=UTC).timestamp() * 1000)


def _bar(
    i: int, close: float, high: float | None = None, low: float | None = None, volume: float = 500.0
) -> MinuteBar:
    return MinuteBar(
        symbol="TEST",
        ts_ms=T0 + i * 60_000,
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
        vwap=None,
        trades=1,
    )


def _flat_path(closes: list[float], entry_volume: float = 1000.0) -> list[MinuteBar]:
    """Flat bars (o=h=l=c) so the R math is exact in tests."""
    return [_bar(i, c, volume=entry_volume if i == 0 else 500.0) for i, c in enumerate(closes)]


# +1R run-up then fade — the §II.11 canonical shape
RUN_FADE = [100.0, 100.3, 100.6, 101.0, 100.8, 100.5, 100.2, 100.15]


def _rows(closes: list[float], side: str = "long", risk: float = 1.0) -> list[dict]:
    bars = _flat_path(closes)
    return ebd.build_rows(bars, T0, bars[-1].ts_ms, 100.0, side, risk)


# -- forward label -----------------------------------------------------------


def test_run_up_then_fade_labels_early_minutes_1_and_post_peak_0():
    rows = _rows(RUN_FADE)
    labels = [r["label_more_ahead"] for r in rows]
    # minutes 0-2: +0.25R more arrives before a 0.5R giveback from that level
    assert labels[:3] == [1, 1, 1]
    # minute 3 is the +1R peak; from there and beyond only giveback follows
    assert labels[3:] == [0, 0, 0, 0, 0]


def test_short_side_mirror_labels_identically():
    # same R-path for a short: prices fall 1R then bounce back
    closes = [200.0 - (c - 100.0) for c in RUN_FADE]
    rows = ebd.build_rows(_flat_path(closes), T0, T0 + 7 * 60_000, 200.0, "short", 1.0)
    assert [r["label_more_ahead"] for r in rows] == [1, 1, 1, 0, 0, 0, 0, 0]
    assert rows[3]["unrealized_r"] == 1.0  # short profit is positive R


def test_giveback_before_gain_labels_0_even_if_price_recovers_later():
    # dips 0.6R below entry (giveback barrier from minute 0 = -0.5R) before
    # any +0.25R gain — barrier order decides, not the eventual outcome
    rows = _rows([100.0, 99.7, 99.4, 100.0, 100.5, 101.0])
    assert rows[0]["label_more_ahead"] == 0


def test_position_close_is_the_vertical_barrier():
    # neither barrier hit before the exit → 0 (no more profit materialized)
    rows = _rows([100.0, 100.1, 100.05, 100.1])
    assert [r["label_more_ahead"] for r in rows] == [0, 0, 0, 0]


# -- retrace / peak math -----------------------------------------------------


def test_peak_retrace_and_minutes_since_peak_math():
    rows = _rows(RUN_FADE)
    assert rows[3]["peak_r"] == 1.0
    assert rows[3]["minutes_since_peak"] == 0
    assert rows[3]["retrace_frac"] == 0.0
    # minute 5: unrealized 0.5R off a 1.0R peak set 2 minutes ago
    assert rows[5]["unrealized_r"] == 0.5
    assert rows[5]["peak_r"] == 1.0
    assert rows[5]["retrace_frac"] == 0.5  # 1 - 0.5/1.0
    assert rows[5]["minutes_since_peak"] == 2
    assert rows[7]["minutes_since_peak"] == 4


def test_retrace_frac_is_0_while_peak_is_not_positive():
    rows = _rows([100.0, 99.9, 99.8])
    assert all(r["retrace_frac"] == 0.0 for r in rows)
    assert rows[2]["peak_r"] == 0.0  # entry bar's excursion never exceeded


def test_vol_ratio_and_vwap_dist():
    rows = _rows(RUN_FADE)
    assert rows[0]["vol_ratio"] == 1.0  # entry bar vs itself
    assert rows[1]["vol_ratio"] == 0.5  # 500 vs the 1000-share entry bar
    assert rows[0]["vwap_dist_r"] == 0.0  # first bar sits on its own VWAP


# -- R normalization ---------------------------------------------------------


def test_risk_uses_recorded_stop_distance():
    assert ebd.resolve_risk(100.0, 98.5, [], T0) == 1.5


def test_risk_falls_back_to_1_5x_atr14_when_no_stop_recorded():
    """Documented fallback: no stop row → risk = 1.5 x ATR(14) on the 1-min
    bars at/before entry, so R stays comparable with the champion k_stop."""
    pre = [_bar(i, 100.0, high=100.2, low=99.8) for i in range(20)]  # TR = 0.4
    entry_ms = pre[-1].ts_ms
    risk = ebd.resolve_risk(100.0, None, pre, entry_ms)
    assert risk is not None and abs(risk - 1.5 * 0.4) < 1e-9
    # unresolvable risk → no rows rather than garbage R units
    assert ebd.resolve_risk(100.0, None, pre[:1], entry_ms) is None
    assert ebd.build_rows(pre, T0, pre[-1].ts_ms, 100.0, "long", None) == []


# -- migration 009 -----------------------------------------------------------


def test_migration_creates_exit_brain_rows_on_fresh_db(tmp_path):
    database = Database(tmp_path / "fresh.db")
    tables = {
        r["name"] for r in database.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "exit_brain_rows" in tables
    assert database.schema_version() >= 9  # 009_exit_brain (later migrations may follow)
    before = database.schema_version()
    database.migrate()  # idempotent
    assert database.schema_version() == before
    # primary key (position_uuid, minute_index) upserts instead of duplicating
    row = (
        "uuid-1",
        "TEST",
        "ORB",
        0,
        "2026-09-02T14:30:00+00:00",
        0.1,
        0.2,
        0.0,
        0,
        0.0,
        1.0,
        1,
        "2026-09-03T00:00:00",
    )
    database.execute(ebd.INSERT_SQL, row)
    database.execute(ebd.INSERT_SQL, row)
    count = database.query("SELECT COUNT(*) AS n FROM exit_brain_rows")[0]["n"]
    assert count == 1
    database.close()


# -- the builder end-to-end (synthetic DB + synthetic HistoryStore) ----------


def _seed_position(
    database,
    uuid: str,
    closes: list[float],
    *,
    qty: float = 10,
    exit_path: str = "trail",
    stop: float | None = 99.0,
) -> None:
    opened = datetime.fromtimestamp(T0 / 1000, tz=UTC)
    closed = datetime.fromtimestamp((T0 + (len(closes) - 1) * 60_000) / 1000, tz=UTC)
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
        " strategy, opened_at, closed_at, realized_pnl, exit_path, trading_mode)"
        " VALUES (?, 'TEST', 'long', ?, 100.0, 'CLOSED', 'ORB', ?, ?, 1.5, ?, 'paper')",
        (uuid, qty, opened.isoformat(), closed.isoformat(), exit_path),
    )
    if stop is not None:
        database.execute(
            "INSERT INTO orders (order_id, client_order_id, position_uuid, symbol, side,"
            " qty, order_type, time_in_force, stop_price, status, trading_mode, submitted_at)"
            " VALUES (?, ?, ?, 'TEST', 'sell', ?, 'stop', 'day', ?, 'accepted', 'paper', ?)",
            (f"o-{uuid}", f"c-{uuid}", uuid, qty, stop, opened.isoformat()),
        )


def test_run_is_idempotent_and_skips_canceled_and_zero_qty(tmp_path):
    database = Database(tmp_path / "paper.db")
    store = HistoryStore(tmp_path / "history.db")
    store.save_bars(_flat_path(RUN_FADE))
    _seed_position(database, "p-real", RUN_FADE)
    _seed_position(database, "p-canceled", RUN_FADE, exit_path="entry canceled")
    _seed_position(database, "p-zero", RUN_FADE, qty=0)

    summary = ebd.run(database, store)
    assert summary["trades_seen"] == 1  # canceled + zero-qty filtered in SQL
    assert summary["trades_processed"] == 1
    assert summary["rows_written"] == len(RUN_FADE)
    assert summary["rows_per_strategy"] == {"ORB": len(RUN_FADE)}
    assert 0.0 < summary["p_more_ahead"] < 1.0  # 3 of 8 minutes labeled 1

    again = ebd.run(database, store)  # re-run updates in place, no growth
    assert again["rows_written"] == len(RUN_FADE)
    count = database.query("SELECT COUNT(*) AS n FROM exit_brain_rows")[0]["n"]
    assert count == len(RUN_FADE)
    # stored labels match the synthetic-path expectation
    stored = database.query(
        "SELECT label_more_ahead FROM exit_brain_rows WHERE position_uuid='p-real'"
        " ORDER BY minute_index"
    )
    assert [r["label_more_ahead"] for r in stored] == [1, 1, 1, 0, 0, 0, 0, 0]
    database.close()
    store.close()
