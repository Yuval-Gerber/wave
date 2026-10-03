"""Phase 10.2: ORB-5 sweep on engineered synthetic sessions."""

import pandas as pd
import pytest
from waveapp.research.orb_sweep import OrbParams, run_params, session_trade, stats, sweep

ET = "America/New_York"


def _session(bars: list[tuple[float, float, float, float]], day: str = "2026-01-05"):
    """Minute bars from 09:30 ET onward: (open, high, low, close)."""
    index = pd.date_range(f"{day} 09:30", periods=len(bars), freq="1min", tz=ET)
    return pd.DataFrame(
        {
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "volume": [1000.0] * len(bars),
            "vwap": [b[3] for b in bars],
            "trades": [10] * len(bars),
        },
        index=index,
    )


def _flat_open(low=100.0, high=101.0):
    """5 opening-range bars pinned inside [low, high]."""
    return [(low + 0.2, high, low, high - 0.2)] * 5


def test_long_breakout_hits_target():
    # range 100-101 (range=1.0); breakout at 09:37; target 1R = 102 hit later
    bars = _flat_open() + [
        (100.8, 100.9, 100.6, 100.8),  # 09:35 no break
        (100.9, 101.4, 100.9, 101.3),  # 09:36 breaks above 101 → long at 101
        (101.3, 102.2, 101.2, 102.0),  # 09:37 target 102 touched
        (102.0, 102.1, 101.8, 101.9),
    ]
    trade = session_trade(_session(bars), OrbParams(target_r=1.0, cost_bps=0.0))
    assert trade is not None
    assert trade.side == "long"
    assert trade.entry == pytest.approx(101.0)
    assert trade.exit == pytest.approx(102.0)
    assert trade.r_multiple == pytest.approx(1.0, abs=0.01)  # risk = range = 1.0


def test_short_breakout_stopped_out():
    bars = _flat_open() + [
        (100.2, 100.4, 99.8, 99.9),  # breaks below 100 → short at 100
        (99.9, 101.2, 99.9, 101.1),  # rips back through stop 101
    ]
    trade = session_trade(_session(bars), OrbParams(target_r=2.0, cost_bps=0.0))
    assert trade is not None
    assert trade.side == "short"
    assert trade.entry == pytest.approx(100.0)
    assert trade.exit == pytest.approx(101.0)  # stopped at opposite extreme
    assert trade.r_multiple == pytest.approx(-1.0, abs=0.01)


def test_stop_and_target_same_bar_counts_as_stop():
    bars = _flat_open() + [
        (101.1, 101.2, 101.0, 101.1),  # long at 101 (gap-through: entry 101.1)
        (101.1, 102.5, 99.5, 100.0),  # touches BOTH target and stop
    ]
    trade = session_trade(_session(bars), OrbParams(target_r=1.0, cost_bps=0.0))
    assert trade is not None
    assert trade.exit == pytest.approx(100.0)  # conservative: loss first
    assert trade.net_pct < 0


def test_both_sides_in_one_bar_skips_the_day():
    bars = _flat_open() + [
        (100.5, 101.5, 99.5, 100.5),  # breaks BOTH ways — ambiguous
        (100.5, 100.6, 100.4, 100.5),
    ]
    assert session_trade(_session(bars), OrbParams()) is None


def test_no_breakout_no_trade_and_no_lookahead():
    bars = _flat_open() + [(100.5, 100.9, 100.1, 100.5)] * 10  # stays inside
    assert session_trade(_session(bars), OrbParams()) is None
    # lookahead guard: a break INSIDE the opening window must not enter
    inside_break = [(100.2, 103.0, 100.0, 102.0)] + _flat_open()[:4]
    bars = inside_break + [(100.5, 100.9, 100.1, 100.5)] * 6
    trade = session_trade(_session(bars), OrbParams())
    assert trade is None  # the window bar set the range; nothing broke after


def test_time_stop_and_costs():
    bars = (
        _flat_open()
        + [
            (100.9, 101.3, 100.9, 101.2),  # long at 101
        ]
        + [(101.2, 101.3, 101.1, 101.2)] * 20
    )  # drifts, never target/stop
    trade = session_trade(_session(bars), OrbParams(target_r=5.0, time_stop_min=10, cost_bps=8.0))
    assert trade is not None
    assert trade.exit == pytest.approx(101.2)  # closed by the time stop
    assert trade.net_pct == pytest.approx(trade.gross_pct - 0.08, abs=1e-9)


def test_sweep_pools_and_ranks():
    winner_day = _flat_open() + [
        (100.9, 101.4, 100.9, 101.3),
        (101.3, 102.2, 101.2, 102.0),
        (102.0, 102.1, 101.0, 101.05),  # fades: 5R target never fills, close 101.05
    ]
    frames = {
        "AAA": _session(winner_day, day="2026-01-05"),
        "BBB": _session(winner_day, day="2026-01-05"),
    }
    grid = [OrbParams(target_r=1.0, cost_bps=0.0), OrbParams(target_r=5.0, cost_bps=0.0)]
    table = sweep(frames, grid)
    assert len(table) == 2
    assert table.iloc[0]["trades"] == 2  # pooled across symbols
    # 1R target hits on this path; 5R target ends flat-ish at session close
    assert table.iloc[0]["target_r"] == 1.0
    assert table.iloc[0]["expectancy_r"] > table.iloc[1]["expectancy_r"]


def test_stats_empty():
    assert stats([]) == {"trades": 0}
    assert run_params(pd.DataFrame(), OrbParams()) == []
