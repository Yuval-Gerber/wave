"""Phase 10.9: VWAP regime-switch walker on engineered sessions."""

import pandas as pd
import pytest
from waveapp.research.vwap_sweep import VwapParams, session_trade, stats

ET = "America/New_York"


def _session(bars, day="2026-01-05", start="09:30"):
    index = pd.date_range(f"{day} {start}", periods=len(bars), freq="1min", tz=ET)
    return pd.DataFrame(
        {
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "volume": [b[4] if len(b) > 4 else 1000.0 for b in bars],
        },
        index=index,
    )


def test_trend_day_pullback_entry_rides_to_close():
    # gap +3% → trend long; price rides above VWAP, pulls back to it at
    # 10:00+, then trends up into the close
    bars = []
    for i in range(30):  # 09:30-10:00 drift up (sets VWAP ≈ 101.5-102)
        p = 101.0 + i * 0.05
        bars.append((p, p + 0.05, p - 0.05, p + 0.02))
    bars.append((102.0, 102.05, 101.6, 101.75))  # 10:00 pullback INTO the vwap band
    for i in range(60):  # trend resumes to 105+
        p = 102.1 + i * 0.05
        bars.append((p, p + 0.06, p - 0.04, p + 0.05))
    frame = _session(bars)
    trade = session_trade(frame, VwapParams(cost_bps=0.0), gap_pct=3.0, daily_atr=1.0, symbol="T")
    assert trade is not None
    assert trade.regime == "trend" and trade.side == "long"
    assert trade.exit > trade.entry  # rode the trend into the close
    assert trade.net_pct > 1.5


def test_range_day_fade_hits_vwap_target():
    # flat gap → range day; price flat around 100 then stretches to 102
    # (2×ATR with atr=1), fade short targets VWAP touch
    bars = [(100.0, 100.1, 99.9, 100.0)] * 40
    bars += [
        (100.0 + 0.3 * i, 100.1 + 0.3 * i, 99.9 + 0.3 * i, 100.05 + 0.3 * i) for i in range(1, 8)
    ]
    bars += [(102.1, 102.2, 99.8, 100.0)] * 5  # falls back through vwap
    bars += [(100.0, 100.1, 99.9, 100.0)] * 15  # pad past the 60-bar minimum
    frame = _session(bars)
    trade = session_trade(
        frame, VwapParams(cost_bps=0.0, stretch_atr=1.5), gap_pct=0.1, daily_atr=1.0, symbol="R"
    )
    assert trade is not None
    assert trade.regime == "range" and trade.side == "short"
    assert trade.exit < trade.entry  # faded back toward VWAP
    assert trade.net_pct > 0


def test_ambiguous_gap_stands_down():
    bars = [(100.0, 100.5, 99.5, 100.2)] * 90
    frame = _session(bars)
    # gap 1.0% sits between range_gap_max (0.5) and gap_thr (2.0) → no trade
    trade = session_trade(frame, VwapParams(), gap_pct=1.0, daily_atr=1.0)
    assert trade is None


def test_trend_stop_holds_loss_to_stop_size():
    # gap up → trend long; pullback entry, then a dump straight through the stop
    bars = []
    for i in range(30):
        p = 101.0 + i * 0.05
        bars.append((p, p + 0.05, p - 0.05, p + 0.02))
    bars.append((102.0, 102.05, 101.6, 101.75))  # entry near VWAP
    bars += [(101.5, 101.6, 99.0, 99.2)] * 30  # crash through stop
    frame = _session(bars)
    trade = session_trade(frame, VwapParams(cost_bps=0.0), gap_pct=2.5, daily_atr=1.0, symbol="X")
    assert trade is not None
    assert trade.exit < trade.entry
    # loss bounded near the stop distance (1×ATR below vwap), not the crash low
    assert abs(trade.exit - trade.entry) <= trade.risk + 0.6  # gap-through slack


def test_long_only_skips_short_days():
    bars = [(100.0, 100.1, 99.9, 100.0)] * 90
    frame = _session(bars)
    trade = session_trade(frame, VwapParams(long_only=True), gap_pct=-3.0, daily_atr=1.0)
    assert trade is None  # gap-down trend day is a SHORT — skipped long-only


def test_stats_and_costs():
    frame = _session(
        [(100.0, 100.1, 99.9, 100.0)] * 40
        + [
            (100.0 + 0.3 * i, 100.1 + 0.3 * i, 99.9 + 0.3 * i, 100.05 + 0.3 * i)
            for i in range(1, 8)
        ]
        + [(102.1, 102.2, 99.8, 100.0)] * 5
        + [(100.0, 100.1, 99.9, 100.0)] * 15
    )
    with_costs = session_trade(
        frame, VwapParams(cost_bps=8.0, stretch_atr=1.5), gap_pct=0.1, daily_atr=1.0, symbol="C"
    )
    free = session_trade(
        frame, VwapParams(cost_bps=0.0, stretch_atr=1.5), gap_pct=0.1, daily_atr=1.0, symbol="C"
    )
    assert with_costs.net_pct == pytest.approx(free.net_pct - 0.08, abs=1e-9)
    s = stats([free])
    assert s["trades"] == 1 and s["range_trades"] == 1
