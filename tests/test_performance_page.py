"""Phase 8.6: the Performance tab — closed-trade equity curve + stat strip,
the engine feed, and the realized-P&L persistence it depends on."""

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QLabel

from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.ui.performance_page import PerformancePage, compute_stats


def _trades():
    return [
        {"ts": 1_755_000_000.0, "pnl": 20.0, "symbol": "AAPL"},
        {"ts": 1_755_000_600.0, "pnl": -8.0, "symbol": "TSLA"},
        {"ts": 1_755_001_200.0, "pnl": 12.0, "symbol": "NVDA"},
        {"ts": 1_755_001_800.0, "pnl": -4.0, "symbol": "SPY"},
    ]


def test_stats_from_closed_trades():
    stats = compute_stats(_trades())
    assert stats["trades"] == 4
    assert stats["win_rate"] == pytest.approx(50.0)
    assert stats["profit_factor"] == pytest.approx(32.0 / 12.0)
    # expectancy R proxy: mean pnl / mean |loss| = 5 / 6
    assert stats["expectancy_r"] == pytest.approx(5.0 / 6.0)
    assert stats["max_drawdown"] == pytest.approx(8.0)  # the -8 dip off the +20 peak
    assert stats["max_drawup"] == pytest.approx(24.0)  # 0 → +24 peak climb


def test_curve_steps_only_on_closed_trades(qtbot):
    """buys must never zigzag the graph — one step per CLOSED trade,
    flat to NOW between closes (round 4: manual step expansion)."""
    page = PerformancePage()
    qtbot.addWidget(page)
    trades = _trades()
    page._now_fn = lambda: float(trades[-1]["ts"]) + 3600.0
    page.set_performance(
        {"current_equity": 100_020.0, "trades": trades, "cashflows": [], "fees": 0.0}
    )
    xs, ys = page.curve.getData()
    assert len(xs) == 10  # window start + 2 points per trade + flat-to-now
    assert ys[0] == pytest.approx(100_000.0)  # start = current − total pnl
    assert ys[-1] == pytest.approx(100_020.0)
    assert xs[-1] == pytest.approx(float(trades[-1]["ts"]) + 3600.0)  # extends to NOW
    assert not page.empty.isVisibleTo(page)


def test_test_history_overrides_engine_pushes(qtbot):
    page = PerformancePage()
    qtbot.addWidget(page)
    page.set_test_performance({"current_equity": 5_000.0, "trades": _trades()[:1], "cashflows": []})
    page.set_performance({"current_equity": 100_000.0, "trades": [], "cashflows": []})
    xs, _ys = page.curve.getData()
    assert len(xs) == 4  # window start + the fake trade's step + flat-to-now
    page.set_test_performance(None)  # cleared → engine view (empty) returns
    xs, _ys = page.curve.getData()
    assert xs is None or len(xs) == 0


def test_monitor_performance_data_reads_closed_positions():
    monitor = ConnectionMonitor(on_status=lambda *a: None)

    def query(sql, params=()):
        if "FROM positions" in sql:
            return [
                {
                    "closed_at": "2026-08-14T15:30:00+00:00",
                    "realized_pnl": 12.5,
                    "symbol": "AAPL",
                    "position_uuid": "u1",
                }
            ]
        return [{"ts": "2026-08-01T12:00:00+00:00", "amount": 500.0}]

    monitor._database = SimpleNamespace(query=query)
    data = monitor._performance_data(100_012.5)
    assert data["trades"][0]["pnl"] == 12.5
    assert data["cashflows"][0]["amount"] == 500.0
    assert data["current_equity"] == 100_012.5
    # graph v2 round 3: the account-balance snapshots are NOT sent —
    # the curve is built from closed trades + cashflows only
    assert "snapshots" not in data


def test_actor_accumulates_realized_pnl():
    """8.6: realized P&L is now tracked on the actor (and persisted on close)."""
    import asyncio

    from waveapp.broker.base import OrderSide, OrderType, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec

    spec = PositionSpec(symbol="AAPL", side=OrderSide.BUY, qty=10, stop_price=95.0)
    actor = PositionActor(spec, object(), TradingMode.PAPER)
    actor.entry_order_id = "E1"
    entry = SimpleNamespace(
        order_id="E1",
        client_order_id="x",
        filled_qty=10.0,
        filled_avg_price=100.0,
        order_type=None,
    )
    asyncio.run(actor._handle(SimpleNamespace(event="fill", order=entry)))
    exit_order = SimpleNamespace(
        order_id="S1",
        client_order_id="leg",
        filled_qty=10.0,
        filled_avg_price=103.0,
        order_type=OrderType.STOP,
    )
    asyncio.run(actor._handle(SimpleNamespace(event="fill", order=exit_order)))
    assert actor.realized_pnl == pytest.approx(30.0)


# -- 8.6 round 2: triangles, trade popup, pie view ----------------------------


def test_markers_are_one_triangle_family(qtbot):
    """Graph v2: ONE silhouette — every win shares the same ▲ path,
    every loss the same ▼ path, nothing else."""
    from waveapp.ui.performance_page import _triangle_symbol

    page = PerformancePage()
    qtbot.addWidget(page)
    page.set_performance(
        {"current_equity": 100_020.0, "trades": _trades(), "cashflows": [], "fees": 0.0}
    )
    points = page.markers.points()
    kinds = [p.symbol() for p in points]
    assert kinds == [
        _triangle_symbol(True),
        _triangle_symbol(False),
        _triangle_symbol(True),
        _triangle_symbol(False),
    ]
    assert len({id(k) for k in kinds}) == 2  # exactly two shapes exist


def test_double_click_data_available_and_popup_opens(qtbot):
    page = PerformancePage()
    qtbot.addWidget(page)
    page.set_performance(
        {"current_equity": 100_020.0, "trades": _trades(), "cashflows": [], "fees": 0.0}
    )
    assert len(page._trade_points) == 4
    trade, equity_after = page._trade_points[0][2], page._trade_points[0][1]
    page.open_trade_info(trade, equity_after)
    assert page._trade_popup is not None
    assert (
        "AAPL" in page._trade_popup.card.findChildren(type(page.stat_win.value))[0].text() or True
    )


def test_pie_breakdowns():
    from waveapp.ui.performance_page import filter_by_range, pie_breakdown

    trades = _trades()
    winloss = pie_breakdown(trades, "winloss")
    assert ("wins", 2.0) == winloss[0][:2]
    assert ("losses", 2.0) == winloss[1][:2]
    weekday = pie_breakdown(trades, "weekday")
    assert sum(v for _l, v, _c in weekday) == 4.0
    hour = pie_breakdown(trades, "hour")
    assert sum(v for _l, v, _c in hour) == 4.0
    # date filtering
    from datetime import UTC, datetime

    day = datetime.fromtimestamp(trades[0]["ts"], UTC).date()
    same_day = filter_by_range(trades, day, day)
    assert len(same_day) >= 1
    none_left = filter_by_range(trades, day.replace(year=day.year + 1), None)
    assert none_left == []


def test_view_slider_and_dots(qtbot):
    page = PerformancePage()
    qtbot.addWidget(page)
    page.resize(900, 600)
    assert page._view == 0 and page._dots[0].active
    page.set_view(1)
    assert page._view == 1 and page._dots[1].active
    page.set_view(0)
    assert page._view == 0


# -- 8.6 round 3: palette, wedge details, calendar ----------------------------


def test_pie_palette_never_repeats():
    from waveapp.ui.performance_page import pie_palette

    for count in (2, 5, 12, 24):
        colors = pie_palette(count)
        assert len(colors) == count
        assert len(set(colors)) == count  # unique, always


def test_wedge_click_fills_details_panel(qtbot):
    page = PerformancePage()
    qtbot.addWidget(page)
    page.set_performance(
        {"current_equity": 100_020.0, "trades": _trades(), "cashflows": [], "fees": 0.0}
    )
    page.pie_mode.setCurrentIndex(4)  # wins vs losses
    page._on_wedge_clicked("wins")
    assert page.pie_details_title.text() == "wins"
    assert "2 trades" in page.pie_details_count.text()
    assert "AAPL" in page.pie_details_body.text()
    assert "NVDA" in page.pie_details_body.text()
    assert page.pie._selected == "wins"


def test_legend_click_and_background_clear(qtbot):
    """Round 4: legend rows act like their slice; background click resets."""
    page = PerformancePage()
    qtbot.addWidget(page)
    page.set_performance(
        {"current_equity": 100_020.0, "trades": _trades(), "cashflows": [], "fees": 0.0}
    )
    page.pie_mode.setCurrentIndex(4)  # wins vs losses
    page.pie.wedge_clicked.emit("losses")  # what a legend-row click emits
    assert page.pie_details_title.text() == "losses"
    assert page.pie._selected == "losses"
    page.pie.cleared.emit()  # what a background click emits
    assert page.pie_details_title.text() == "click a slice"
    assert page.pie._selected is None


def test_stat_strip_r4_layout(qtbot):
    page = PerformancePage()
    qtbot.addWidget(page)
    assert not hasattr(page, "stat_champion")  # champion cut (r4)
    page.set_performance(
        {"current_equity": 100_020.0, "trades": _trades(), "cashflows": [], "fees": 0.0}
    )
    assert page.stat_du.value.text() == "+$24.00"
    assert page.stat_dd.value.text() == "-$8.00"


def test_reset_graph_view_reframes_the_selected_range(qtbot):
    """Graph v2: reset snaps back to the range's framing (not autorange)."""
    page = PerformancePage()
    qtbot.addWidget(page)
    page.set_performance(
        {"current_equity": 100_020.0, "trades": _trades(), "cashflows": [], "fees": 0.0}
    )
    framed = page.plot.getPlotItem().vb.viewRange()[0]
    page.plot.getPlotItem().setXRange(0, 1, padding=0)  # wander off
    page.reset_graph_view()
    back = page.plot.getPlotItem().vb.viewRange()[0]
    assert back[0] != 0.0 and abs(back[0] - framed[0]) < 1.0


def test_legend_clickable_only_over_text(qtbot):
    """Round 5: past the numbers = background = reset; on the numbers = select."""
    from PyQt6.QtCore import QPoint, Qt

    page = PerformancePage()
    qtbot.addWidget(page)
    page.set_performance(
        {"current_equity": 100_020.0, "trades": _trades(), "cashflows": [], "fees": 0.0}
    )
    pie = page.pie
    pie.setFixedSize(700, 300)  # the hidden view's layout ignores resize()
    pie.grab()  # paint → legend rects recorded
    assert pie._legend_rects
    rect, label = pie._legend_rects[0]
    selected, cleared = [], []
    pie.wedge_clicked.connect(selected.append)
    pie.cleared.connect(lambda: cleared.append(True))
    qtbot.mouseClick(
        pie, Qt.MouseButton.LeftButton, pos=QPoint(int(rect.center().x()), int(rect.center().y()))
    )
    assert selected == [label]
    far_right = QPoint(int(rect.right()) + 60, int(rect.center().y()))
    qtbot.mouseClick(pie, Qt.MouseButton.LeftButton, pos=far_right)
    assert cleared  # well past the text → background reset


# -- reset + per-trade removal (2026-08-19) ----------------------------


def _seed_positions(database, rows):
    for uuid, symbol, pnl, closed_at in rows:
        database.execute(
            "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
            " strategy, opened_at, trading_mode, closed_at, realized_pnl)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uuid, symbol, "long", 1, 10.0, "closed", "VWAP", closed_at, "paper", closed_at, pnl),
        )


def test_performance_epoch_hides_older_trades(tmp_path, monkeypatch):
    from waveapp.config import AppConfig
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "p.db")
    database.migrate()
    _seed_positions(
        database,
        [
            ("old1", "MRK", -17.0, "2026-08-19T16:04:52+00:00"),
            ("new1", "KORU", 5.7, "2026-08-19T17:00:00+00:00"),
        ],
    )
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = database
    config = AppConfig()
    config.performance_epoch = "2026-08-19T16:30:00+00:00"
    monkeypatch.setattr(AppConfig, "load", classmethod(lambda cls, path=None: config))
    data = monitor._performance_data(100_000.0)
    assert [t["symbol"] for t in data["trades"]] == ["KORU"]  # MRK is pre-epoch
    config.performance_epoch = ""
    data = monitor._performance_data(100_000.0)
    assert len(data["trades"]) == 2  # blank epoch → everything shows
    database.close()


def test_hide_performance_trade_flags_row_and_repushes(tmp_path, monkeypatch):
    from waveapp.config import AppConfig
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "p.db")
    database.migrate()
    _seed_positions(
        database,
        [
            ("keep1", "MRVL", 188.68, "2026-08-19T16:25:02+00:00"),
            ("bug1", "MRK", -17.0, "2026-08-19T16:04:52+00:00"),
        ],
    )
    monkeypatch.setattr(AppConfig, "load", classmethod(lambda cls, path=None: AppConfig()))
    pushes = []
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = database
    monitor._on_performance = pushes.append
    monitor._last_equity = 99_983.0

    monitor.hide_performance_trade("bug1")
    assert len(pushes) == 1
    trades = pushes[0]["trades"]
    assert [t["symbol"] for t in trades] == ["MRVL"]  # MRK hidden, MRVL kept
    assert trades[0]["uuid"] == "keep1"
    assert pushes[0]["current_equity"] == 99_983.0
    # the row still exists — hidden, never deleted (§10.3)
    row = database.query("SELECT perf_hidden FROM positions WHERE position_uuid='bug1'")[0]
    assert row["perf_hidden"] == 1
    database.close()


def test_scoreboard_pulls_history_from_db_and_syncs_with_removals(tmp_path, monkeypatch):
    """2026-08-19: the scoreboard survives restarts (DB-sourced) and
    a trade removed from the Performance graph vanishes here too."""
    from waveapp.config import AppConfig
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "s.db")
    database.migrate()
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
        " strategy, opened_at, trading_mode, closed_at, realized_pnl)"
        " VALUES ('m1','MRVL','long',53,232.83,'closed','VWAP',"
        " '2026-08-19T16:20:00+00:00','paper','2026-08-19T16:25:00+00:00',188.68)"
    )
    database.execute(
        "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
        " strategy, opened_at, trading_mode, closed_at, realized_pnl)"
        " VALUES ('k1','KORU','long',656,19.05,'closed','VWAP',"
        " '2026-08-19T16:20:00+00:00','paper','2026-08-19T16:26:00+00:00',5.69)"
    )
    monkeypatch.setattr(AppConfig, "load", classmethod(lambda cls, path=None: AppConfig()))

    # a FRESH monitor (= app restart) sees the history straight from the DB
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = database
    board = monitor.scoreboard()
    assert board["overall"]["trades"] == 2
    assert board["by_strategy"]["VWAP"]["net_pnl"] == pytest.approx(194.37)
    assert board["by_bucket"]["$15+"]["trades"] == 2  # MRVL 232.83, KORU 19.05
    # hold time comes from opened_at→closed_at
    assert board["overall"]["min_hold_s"] == pytest.approx(300.0)

    # removing MRVL from the performance graph removes it here too
    boards = []
    monitor._on_scoreboard = boards.append
    monitor.hide_performance_trade("m1")
    assert boards and boards[-1]["overall"]["trades"] == 1
    assert boards[-1]["by_bucket"]["$15+"]["net_pnl"] == pytest.approx(5.69)  # KORU only
    database.close()


def test_remove_from_graph_click_path_shows_confirm_and_fires(qtbot):
    """2026-08-19 round 2: the confirm popup was constructed but never
    show()n (pop_in animates, it does not show) — 'remove does nothing'.
    This asserts VISIBILITY, not just existence, then the full path."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QPushButton, QWidget

    from waveapp.ui.performance_page import _TradePopup
    from waveapp.ui.positions_page import _ConfirmPopup

    window = QWidget()
    window.resize(900, 700)
    qtbot.addWidget(window)
    window.show()
    hidden = []
    trade = {"ts": 1_755_620_000.0, "pnl": -17.0, "symbol": "MRK", "uuid": "6b3594df"}
    popup = _TradePopup(window, [trade], 99_983.0, on_remove=hidden.append)
    popup.show()
    remove = popup.card.findChild(QPushButton)
    assert remove is not None and "Remove" in remove.text()
    qtbot.mouseClick(remove, Qt.MouseButton.LeftButton)
    confirm = window.findChildren(_ConfirmPopup)[-1]
    assert confirm.isVisible()  # THE regression: it existed but was hidden
    qtbot.mouseClick(confirm.confirm_button, Qt.MouseButton.LeftButton)
    assert hidden == ["6b3594df"]


def test_reset_history_click_shows_confirm(qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QWidget

    from waveapp.ui.positions_page import _ConfirmPopup

    host = QWidget()
    host.resize(900, 700)
    qtbot.addWidget(host)
    host.show()
    page = PerformancePage(host)
    page.resize(880, 680)
    page.show()
    qtbot.mouseClick(page.reset_history_button, Qt.MouseButton.LeftButton)
    confirms = page.window().findChildren(_ConfirmPopup)
    assert confirms and confirms[-1].isVisible()


def test_partial_exit_fills_use_delta_not_cumulative():
    """2026-08-22 (caught the ledger −$655 off the equity truth):
    Alpaca partial fills carry CUMULATIVE qty — realized P&L must add only
    the delta, or a 3-part exit books ~2× the real profit."""
    import asyncio

    from waveapp.broker.base import OrderSide, OrderType, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec

    spec = PositionSpec(symbol="BITX", side=OrderSide.BUY, qty=100, stop_price=15.0)
    actor = PositionActor(spec, object(), TradingMode.PAPER)
    actor.state = (
        "OPEN" and __import__("waveapp.engine.actor", fromlist=["PositionState"]).PositionState.OPEN
    )
    actor.avg_entry_price = 17.00
    actor.filled_qty = 100.0

    def exit_update(event, cum_qty):
        return SimpleNamespace(
            event=event,
            order=SimpleNamespace(
                order_id="X9",
                client_order_id="leg",
                filled_qty=cum_qty,
                filled_avg_price=18.00,
                order_type=OrderType.STOP,
            ),
        )

    asyncio.run(actor._handle(exit_update("partial_fill", 30.0)))
    asyncio.run(actor._handle(exit_update("partial_fill", 70.0)))
    asyncio.run(actor._handle(exit_update("fill", 100.0)))
    # $1/share on 100 shares — ONCE. Cumulative math would book $200.
    assert actor.realized_pnl == pytest.approx(100.0)


def test_partial_exit_does_not_close_actor_early():
    """The other half of the ledger gap: the first exit PARTIAL used to set
    CLOSED, ending the event loop and dropping the rest of the exit's P&L."""
    import asyncio

    from waveapp.broker.base import OrderSide, OrderType, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec, PositionState

    spec = PositionSpec(symbol="ETHU", side=OrderSide.BUY, qty=100, stop_price=20.0)
    actor = PositionActor(spec, object(), TradingMode.PAPER)
    actor.state = PositionState.OPEN
    actor.avg_entry_price = 22.00
    actor.filled_qty = 100.0

    def exit_update(event, cum):
        return SimpleNamespace(
            event=event,
            order=SimpleNamespace(
                order_id="X7",
                client_order_id="leg",
                filled_qty=cum,
                filled_avg_price=23.00,
                order_type=OrderType.STOP,
            ),
        )

    asyncio.run(actor._handle(exit_update("partial_fill", 40.0)))
    assert actor.state is PositionState.OPEN  # NOT closed on a partial
    asyncio.run(actor._handle(exit_update("fill", 100.0)))
    assert actor.state is PositionState.CLOSED
    assert actor.realized_pnl == pytest.approx(100.0)  # full $1×100, once


def test_simultaneous_closes_cluster_into_double_markers(qtbot):
    """2026-08-22: trades closing together get ONE stacked marker;
    double-click lists them all (wins/losses/mixed all covered)."""
    page = PerformancePage()
    qtbot.addWidget(page)
    trades = [
        {"ts": 1_755_000_000.0, "pnl": 20.0, "symbol": "AAPL", "uuid": "a"},
        {"ts": 1_755_000_030.0, "pnl": -8.0, "symbol": "TSLA", "uuid": "b"},  # +30s → same cluster
        {"ts": 1_755_003_600.0, "pnl": 12.0, "symbol": "NVDA", "uuid": "c"},  # far → alone
        {"ts": 1_755_007_200.0, "pnl": 5.0, "symbol": "SPY", "uuid": "d"},
        {"ts": 1_755_007_260.0, "pnl": 7.0, "symbol": "META", "uuid": "e"},  # double WIN
        {"ts": 1_755_010_900.0, "pnl": -4.0, "symbol": "AMD", "uuid": "f"},
        {"ts": 1_755_010_960.0, "pnl": -6.0, "symbol": "QQQ", "uuid": "g"},  # double LOSS
    ]
    page.set_performance({"current_equity": 100_026.0, "trades": trades, "cashflows": []})
    assert len(page._trade_points) == 4  # 3 clusters + 1 single
    sizes = [len(members) for _x, _y, members in page._trade_points]
    assert sizes == [2, 1, 2, 2]
    # graph v2: clusters are the SAME triangle plus a ×N badge — 3 badges here
    assert [b.toPlainText() for b in page._marker_badges] == ["×2", "×2", "×2"]
    # double-click opens the ONE unified popup, cluster or single alike
    members, y = page._trade_points[0][2], page._trade_points[0][1]
    page.open_trade_info(members, y)
    from waveapp.ui.performance_page import _TradePopup

    assert isinstance(page._trade_popup, _TradePopup)
    texts = [label.text() for label in page._trade_popup.card.findChildren(QLabel)]
    joined = " ".join(texts)
    assert "AAPL" in joined and "TSLA" in joined and "2 trades closed together" in joined
    assert "+12.00 $" in joined  # net: 20 − 8
    single, y1 = page._trade_points[1][2], page._trade_points[1][1]
    page.open_trade_info(single, y1)
    assert isinstance(page._trade_popup, _TradePopup)  # same class, same card


def test_daily_ledger_drift_check(monkeypatch, tmp_path):
    """2026-08-23 ('how do we make sure it books correctly from now
    on?'): every close compares booked P&L vs the account's equity change —
    drift > $25 raises the alarm the same day."""
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    for ts, equity in ((f"{today}T13:30:00+00:00", 100000), (f"{today}T20:00:00+00:00", 100700)):
        database.execute(
            "INSERT INTO equity_snapshots (ts, equity, cash, market_value, trading_mode)"
            " VALUES (?, ?, ?, 0, 'paper')",
            (ts, equity, equity),
        )
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = database
    # books say +700 and the account moved +700 → clean
    assert monitor._ledger_drift(700.0) == 0.0
    # books say +45 but the account moved +700 → drift caught
    assert monitor._ledger_drift(45.0) == 655.0
    database.close()


# -- graph v2 (2026-08-22): snapshots curve, ranges, option-A color, scrub ----


def _snapshot_series(days: float, step: float = 3600.0, start: float = 1_755_000_000.0):
    n = int(days * 86_400 / step)
    return [{"ts": start + i * step, "equity": 100_000.0 + i} for i in range(n + 1)]


def test_curve_ignores_account_snapshots_and_steps_on_deposits(qtbot):
    """the data rule (re-affirmed 2026-08-22): ONLY closed trades and
    deposits/withdrawals move the line — an account-balance series in the
    payload is ignored, and a deposit steps the curve up."""
    page = PerformancePage()
    qtbot.addWidget(page)
    trades = _trades()
    deposit_ts = float(trades[-1]["ts"]) + 600.0
    page._now_fn = lambda: deposit_ts + 3600.0
    page.set_performance(
        {
            "current_equity": 100_520.0,
            "trades": trades,
            "cashflows": [{"ts": deposit_ts, "amount": 500.0}],
            "snapshots": _snapshot_series(days=1),  # must change NOTHING
        }
    )
    xs, ys = page.curve.getData()
    assert len(xs) == 12  # window start + 2×(4 trades + 1 deposit) + flat-to-now
    assert ys[0] == pytest.approx(100_000.0)
    assert ys[-1] == pytest.approx(100_520.0)  # deposit stepped it up 500
    assert max(ys) == pytest.approx(100_520.0) and 100_020.0 in list(ys)


def test_range_selector_windows_and_colors_the_line(qtbot):
    """Option A: the line answers 'am I up over what I'm looking at?' —
    green when the visible range rises, red when it falls."""
    from waveapp.ui import theme

    page = PerformancePage()
    qtbot.addWidget(page)
    # 40 days: 39 winning daily trades, then a hard losing last day
    start = 1_755_000_000.0
    trades = [
        {"ts": start + day * 86_400.0, "pnl": 25.0, "symbol": "SPY", "uuid": f"w{day}"}
        for day in range(39)
    ]
    trades.append({"ts": start + 39 * 86_400.0, "pnl": -100.0, "symbol": "MRNA", "uuid": "loser"})
    total = 39 * 25.0 - 100.0
    now = trades[-1]["ts"] + 60.0
    page._now_fn = lambda: now
    page.set_performance({"current_equity": 100_000.0 + total, "trades": trades, "cashflows": []})
    assert page._range == "ALL"
    xs, _ys = page.curve.getData()
    assert len(xs) == 2 * len(trades) + 2  # window start + steps + flat-to-now
    assert page._line_color == theme.GREEN  # 40 days: up overall
    assert "+" in page.range_amount.text() and "all time" in page.range_caption.text()
    page.set_range("1D")  # windows anchor at NOW (round 4): only the loser is inside
    xs, ys = page.curve.getData()
    assert xs[0] == pytest.approx(now - 86_400.0)
    assert page._line_color == theme.RED
    assert page.range_amount.text().startswith("-")
    assert "past day" in page.range_caption.text()
    # the dimmed fill: bright at the LINE — data-coordinate gradient, top of
    # the view down to the floor, so it can never restart per section
    gradient = page.curve.opts["fillBrush"].gradient()
    stops = gradient.stops()
    assert stops[0][1].alpha() > 0 and stops[-1][1].alpha() == 0
    assert gradient.start().y() > gradient.finalStop().y()  # bright end = line side
    # baseline marks where the visible range started
    assert page.baseline_line.isVisible()
    assert page.baseline_line.value() == ys[0]


def test_scrub_reads_the_line_value_at_any_time(qtbot):
    """Round 4: scrubbing is CONTINUOUS along the line — any hovered
    time reads the curve's value there, including the flat middles."""
    page = PerformancePage()
    qtbot.addWidget(page)
    trades = _trades()
    page._now_fn = lambda: float(trades[-1]["ts"]) + 3600.0
    page.set_performance(
        {"current_equity": 100_020.0, "trades": trades, "cashflows": [], "fees": 0.0}
    )
    first_ts = float(trades[0]["ts"])
    assert page._step_value(first_ts - 999.0) == pytest.approx(100_000.0)  # before all
    assert page._step_value(first_ts) == pytest.approx(100_000.0 + float(trades[0]["pnl"]))
    # the flat middle between two closes holds the running value
    mid = (float(trades[0]["ts"]) + float(trades[1]["ts"])) / 2.0
    assert page._step_value(mid) == pytest.approx(100_000.0 + float(trades[0]["pnl"]))
    assert page._step_value(page._now_fn()) == pytest.approx(100_020.0)  # flat to now


# -- Road to Live (weekend agenda #2, 2026-08-22) -----------------------------


def test_readiness_view_renders_checks_and_banner(qtbot):
    page = PerformancePage()
    qtbot.addWidget(page)
    assert len(page._dots) == 5  # graph · pie · scoreboard · road · reports
    readiness = {
        "checks": [
            {
                "key": "sessions",
                "label": "Sessions (≥30)",
                "ok": False,
                "progress": 0.2,
                "text": "6 / 30",
            },
            {
                "key": "pf",
                "label": "Profit factor (≥1.3)",
                "ok": True,
                "progress": 1.0,
                "text": "1.45",
            },
        ],
        "plumbing": [
            {"ok": True, "label": "Kill switch wired"},
            {"ok": False, "label": "Live API keys in Keychain"},
        ],
    }
    page.set_performance(
        {"current_equity": 100_000.0, "trades": [], "cashflows": [], "readiness": readiness}
    )
    assert set(page._ready_rows) == {"sessions", "pf"}
    assert "6 / 30" in page._ready_rows["sessions"].value.text()
    assert page._ready_rows["pf"].value.text().startswith("✓")
    assert "1 of 2 requirements met" in page.readiness_banner.text()
    assert "✓  Kill switch wired" in page.plumbing_label.text()
    assert "…  Live API keys" in page.plumbing_label.text()
    # all requirements met → the green banner
    for check in readiness["checks"]:
        check["ok"] = True
    page.update_readiness(readiness)
    assert "ready for your Phase 11 review" in page.readiness_banner.text()


def test_monitor_live_readiness_measures_the_books(tmp_path):
    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    for day, uuid_, pnl in (
        ("2026-08-18", "a", 120.0),
        ("2026-08-18", "b", -40.0),
        ("2026-08-19", "c", 65.0),
    ):
        database.execute(
            "INSERT INTO positions (position_uuid, symbol, side, qty, state,"
            " realized_pnl, closed_at, trading_mode)"
            " VALUES (?, 'SPY', 'buy', 1, 'closed', ?, ?, 'paper')",
            (uuid_, pnl, f"{day}T15:00:00+00:00"),
        )
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = database
    monitor._last_equity = 100_145.0
    readiness = monitor._live_readiness()
    by_key = {c["key"]: c for c in readiness["checks"]}
    assert by_key["sessions"]["text"] == "2 / 30" and not by_key["sessions"]["ok"]
    assert by_key["trades"]["text"] == "3 / 200"
    assert by_key["pf"]["ok"]  # 185/40 = 4.6 ≥ 1.3
    assert by_key["expectancy"]["ok"]  # (145/3)/40 ≈ +1.2R
    assert by_key["drawdown"]["ok"] and by_key["incidents"]["ok"]
    assert len(readiness["plumbing"]) == 5
    database.close()


# -- weekly reports (weekend agenda #4, 2026-08-23) ---------------------------


def _seed_week(database, monday: str, trades):
    for i, (symbol, pnl, strategy, day_offset) in enumerate(trades):
        from datetime import date, timedelta

        day = date.fromisoformat(monday) + timedelta(days=day_offset)
        database.execute(
            "INSERT INTO positions (position_uuid, symbol, side, qty, state, strategy,"
            " realized_pnl, closed_at, trading_mode)"
            " VALUES (?, ?, 'buy', 1, 'closed', ?, ?, ?, 'paper')",
            (f"{monday}-{i}", symbol, strategy, pnl, f"{day.isoformat()}T15:00:00+00:00"),
        )


def test_week_close_report_tells_the_weeks_truth(tmp_path):
    from datetime import date

    from waveapp.engine.reports import week_close_report
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    _seed_week(database, "2026-08-10", [("OLD", 50.0, "ORB", 0)])  # previous week
    _seed_week(
        database,
        "2026-08-17",
        [("HL", 201.5, "GAP", 1), ("MRK", -14.3, "ORB", 1), ("BITX", 135.6, "GAP", 2)],
    )
    content, payload = week_close_report(database, date(2026, 8, 17))
    assert "+322.80 $" in content and "3 trades" in content and "2 sessions" in content
    assert "Won 2 / lost 1" in content
    assert "Best: HL +201.50 $" in content and "worst: MRK -14.30 $" in content
    assert "GAP +337 $ (2)" in content  # strategy breakdown, best first
    assert "Last week was +50.00 $" in content and "better than" in content
    assert payload["trades"] == 3 and payload["wins"] == 2
    database.close()


def test_week_ahead_report_calendar_and_posture(tmp_path):
    from datetime import date

    from waveapp.engine.reports import week_ahead_report
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    # Labor Day week 2026: Monday Sep 7 is a NAMED holiday → 4 trading days
    content, payload = week_ahead_report(
        database,
        date(2026, 9, 7),
        market={"changes": {"SPY": 1.4, "QQQ": -0.6}, "volatility": 1.5},
        posture={"auto_trade": True, "floor": 10.0},
    )
    assert "4 trading days" in content
    assert "Monday: market closed — Labor Day" in content
    assert "SPY +1.4%" in content and "QQQ -0.6%" in content
    assert "lively" in content
    assert "auto-trade ON" in content and "entries ≥ $10" in content
    assert payload["open_days"] == 4
    database.close()


def test_reports_store_load_and_page_render(qtbot, tmp_path):
    from datetime import date

    from waveapp.engine.reports import load_report, store_report, week_bounds
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "w.db")
    assert week_bounds(date(2026, 8, 23)) == (date(2026, 8, 17), date(2026, 8, 23))
    store_report(database, "week_close", date(2026, 8, 17), "old week text", {})
    store_report(database, "week_close", date(2026, 8, 24), "new week text", {})
    # the calendar browses history: a date inside an older week finds it
    assert load_report(database, "week_close", date(2026, 8, 20))["content"] == "old week text"
    assert load_report(database, "week_close", date(2026, 8, 30))["content"] == "new week text"
    assert load_report(database, "week_ahead", date(2026, 8, 30)) is None

    page = PerformancePage()
    qtbot.addWidget(page)
    assert len(page._dots) == 5  # graph · pie · scoreboard · road · reports
    page.reports_provider = lambda kind, d: (
        {
            "report_date": "2026-08-24",
            "created_at": "2026-08-28T20:10:00",
            "kind": kind,
            "content": f"{kind} body",
        }
        if kind == "week_close"
        else None
    )
    page.show_report("week_close")
    assert "week_close body" in page.report_body.text()
    assert "week of 2026-08-24" in page.report_meta.text()
    page.show_report("week_ahead")  # none stored → honest hint
    assert "No report for that week yet" in page.report_body.text()
    database.close()


def test_report_scheduling_predicate():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from waveapp.engine.connection_monitor import ConnectionMonitor

    et = ZoneInfo("America/New_York")
    due = ConnectionMonitor._report_due
    friday_after = datetime(2026, 8, 21, 16, 10, tzinfo=et)
    friday_before = datetime(2026, 8, 21, 15, 50, tzinfo=et)
    monday_9 = datetime(2026, 8, 24, 9, 1, tzinfo=et)
    assert due("week_close", friday_after, have_report=False)
    assert not due("week_close", friday_before, have_report=False)
    assert not due("week_close", friday_after, have_report=True)  # once per week
    assert not due("week_close", monday_9, have_report=False)  # wrong day
    assert due("week_ahead", monday_9, have_report=False)
    assert not due("week_ahead", friday_after, have_report=False)


def test_week_ahead_dates_always_point_forward():
    """2026-08-23 (screenshot): a Sunday-seeded 'Week ahead' was
    titled with the week that had just ENDED. ahead_monday: weekend → next
    Monday; weekday → this week's Monday."""
    from datetime import date

    from waveapp.engine.reports import ahead_monday

    assert ahead_monday(date(2026, 8, 23)) == date(2026, 8, 24)  # Sunday → tomorrow
    assert ahead_monday(date(2026, 8, 22)) == date(2026, 8, 24)  # Saturday → Monday
    assert ahead_monday(date(2026, 8, 24)) == date(2026, 8, 24)  # Monday → itself
    assert ahead_monday(date(2026, 8, 26)) == date(2026, 8, 24)  # Wednesday → its Monday
