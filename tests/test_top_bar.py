"""Phase 8.3: the rebuilt top bar — slide toggle, flip board, balance card,
transport buttons, and the actor→flip-board trade ticker."""

import asyncio
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, Qt

from waveapp.broker.base import OrderSide, OrderType, TradingMode
from waveapp.engine.actor import PositionActor, PositionSpec
from waveapp.ui.top_bar import FlipBoard, ModeToggle, TopBar

# -- flip board ---------------------------------------------------------------


def _chars(cells) -> str:
    return "".join(value for kind, value, _ in cells if kind == "char")


def test_flip_board_cells_entries_carry_direction_glyphs():
    long_cells = FlipBoard.cells_for_trade({"action": "entry", "symbol": "AAPL", "long": True})
    assert long_cells[0][0] == "graph_up"  # small monochrome rising graph
    assert _chars(long_cells) == "BUY AAPL"

    short_cells = FlipBoard.cells_for_trade({"action": "entry", "symbol": "TSLA", "long": False})
    assert short_cells[0][0] == "graph_down"


def test_flip_board_cells_exits_carry_pnl_triangles():
    loss = FlipBoard.cells_for_trade(
        {"action": "close", "symbol": "TSLA", "long": True, "pnl": -3.5}
    )
    assert loss[0][0] == "tri_down"  # red upside-down triangle
    assert _chars(loss) == "SELL TSLA -3.50"

    win = FlipBoard.cells_for_trade({"action": "close", "symbol": "NVDA", "long": True, "pnl": 12})
    assert win[0][0] == "tri_up"  # green triangle
    assert _chars(win) == "SELL NVDA +12.00"


def test_flip_board_timer_starts_after_the_flip_settles(qtbot):
    board = FlipBoard()
    qtbot.addWidget(board)
    board.resize(360, 34)
    board._flip.setDuration(60)  # shrink animations for the test
    board.show_trade({"action": "entry", "symbol": "AAPL", "long": True})
    assert board._cells
    assert not board._hide_timer.isActive()  # round 4: clock waits for the flap
    assert not board.grab().isNull()
    qtbot.waitUntil(board._hide_timer.isActive, timeout=2000)  # flip settled
    board._start_fade()  # what the timer fires
    qtbot.waitUntil(lambda: board._cells == [], timeout=2000)
    assert not board._busy


def test_flip_board_messages_queue_and_never_interrupt(qtbot):
    board = FlipBoard()
    qtbot.addWidget(board)
    board.resize(360, 34)
    board._flip.setDuration(50)
    board._fade.setDuration(30)
    board.HOLD_MS = 60
    board.show_trade({"action": "entry", "symbol": "AAPL", "long": True})
    first = list(board._cells)
    board.show_trade({"action": "close", "symbol": "TSLA", "long": True, "pnl": 5.0})
    board.show_message("data feed stale")
    assert board._cells == first  # still the first message — no interruption
    assert len(board._queue) == 2  # the others wait their turn
    qtbot.waitUntil(lambda: bool(board._cells) and board._cells != first, timeout=4000)
    assert _chars(board._cells).startswith("SELL")  # second message, in order
    qtbot.waitUntil(
        lambda: bool(board._cells) and board._cells[0][1] == "!", timeout=4000
    )  # the error flag rides the same board, last
    qtbot.waitUntil(lambda: board._cells == [] and not board._busy, timeout=4000)


# -- mode toggle --------------------------------------------------------------


def test_toggle_live_is_locked(qtbot):
    toggle = ModeToggle()
    qtbot.addWidget(toggle)
    toggle.show()
    assert not toggle.live_enabled
    qtbot.mouseClick(
        toggle, Qt.MouseButton.LeftButton, pos=QPoint(toggle.width() - 12, toggle.height() // 2)
    )
    assert toggle.mode == "paper"  # locked: the knob bounces, nothing switches


def test_toggle_knob_slides_on_mode_change(qtbot):
    toggle = ModeToggle()
    qtbot.addWidget(toggle)
    toggle.set_mode("live")
    qtbot.waitUntil(lambda: toggle._knob > 0.95, timeout=2000)
    toggle.set_mode("paper")
    qtbot.waitUntil(lambda: toggle._knob < 0.05, timeout=2000)


# -- balance ------------------------------------------------------------------


def test_balance_caption_follows_display_mode(qtbot):
    bar = TopBar()
    qtbot.addWidget(bar)
    bar.set_balance("paper", 12345.6)
    assert bar.balance.caption.text() == "PAPER BALANCE"
    assert bar.balance.amount.text() == "$12,345.60"
    bar.set_balance("live", 88.0)  # background update — display stays paper
    assert bar.balance.caption.text() == "PAPER BALANCE"
    bar.set_display_mode("live")
    assert bar.balance.caption.text() == "LIVE BALANCE"
    assert bar.balance.amount.text() == "$88.00"


def test_balance_card_is_the_alpaca_logo_per_mode(qtbot):
    """Round 2: the card is just the (monochrome) Alpaca logo button — paper
    opens the paper dashboard, live opens the banking page."""
    from waveapp.ui.top_bar import ALPACA_LIVE_BANKING, ALPACA_PAPER_DASHBOARD

    bar = TopBar()
    qtbot.addWidget(bar)
    card = bar.balance_card
    card.set_mode("paper")
    assert card._url == ALPACA_PAPER_DASHBOARD
    assert not card.logo_button.grab().isNull()  # the logomark renders
    card.set_mode("live")
    assert card._url == ALPACA_LIVE_BANKING
    assert "bank" in card.note.text()


def test_live_display_not_clobbered_by_paper_polls(qtbot):
    """While the LIVE balance is shown, the paper poll keeps flowing but must
    not overwrite the display; switching back restores the paper number."""
    bar = TopBar()
    qtbot.addWidget(bar)
    bar.set_balance("paper", 100_000.0)
    bar.set_display_mode("live")
    bar.set_balance("live", 42.0)
    assert bar.balance.amount.text() == "$42.00"
    bar.set_balance("paper", 100_100.0)  # poll arrives while live shows
    assert bar.balance.amount.text() == "$42.00"
    bar.set_display_mode("paper")
    assert bar.balance.amount.text() == "$100,100.00"


def test_toggle_connection_fill_reflects_the_wait(qtbot):
    toggle = ModeToggle()
    qtbot.addWidget(toggle)
    toggle.begin_connect("live")
    qtbot.waitUntil(lambda: 0.0 < toggle._fill["live"] < 1.0, timeout=2000)  # creeping
    toggle.finish_connect("live")
    qtbot.waitUntil(lambda: toggle._fill["live"] > 0.99, timeout=2000)  # snapped to blue
    toggle.finish_connect("live", ok=False)
    qtbot.waitUntil(lambda: toggle._fill["live"] < 0.01, timeout=2000)  # failure drains


# -- transport ----------------------------------------------------------------


def test_play_button_fires_start(qtbot):
    bar = TopBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.set_engine_state("idle")
    with qtbot.waitSignal(bar.start_clicked, timeout=1000):
        qtbot.mouseClick(bar.start_button, Qt.MouseButton.LeftButton)


# -- actor → flip board ticker ------------------------------------------------


def _update(event, order):
    return SimpleNamespace(event=event, order=order)


def test_actor_fires_trade_events_with_pnl():
    events = []
    spec = PositionSpec(symbol="AAPL", side=OrderSide.BUY, qty=10, stop_price=95.0)
    actor = PositionActor(spec, adapter=object(), mode=TradingMode.PAPER, on_trade=events.append)

    actor.entry_order_id = "E1"
    entry = SimpleNamespace(
        order_id="E1",
        client_order_id="manual",
        filled_qty=10.0,
        filled_avg_price=100.0,
        order_type=None,
    )
    asyncio.run(actor._handle(_update("fill", entry)))
    assert events[-1]["action"] == "entry"
    assert events[-1]["long"] is True
    assert events[-1]["pnl"] is None

    stop_leg = SimpleNamespace(
        order_id="S1",
        client_order_id="broker-leg",
        filled_qty=10.0,
        filled_avg_price=96.0,
        order_type=OrderType.STOP,
    )
    asyncio.run(actor._handle(_update("fill", stop_leg)))
    assert events[-1]["action"] == "close"
    assert events[-1]["pnl"] == pytest.approx(-40.0)  # (96-100) × 10, long


def test_actor_on_trade_failure_never_breaks_trading():
    def boom(payload) -> None:
        raise RuntimeError("UI died")

    spec = PositionSpec(symbol="AAPL", side=OrderSide.SELL, qty=5, stop_price=105.0)
    actor = PositionActor(spec, adapter=object(), mode=TradingMode.PAPER, on_trade=boom)
    actor.entry_order_id = "E1"
    entry = SimpleNamespace(
        order_id="E1",
        client_order_id="manual",
        filled_qty=5.0,
        filled_avg_price=100.0,
        order_type=None,
    )
    asyncio.run(actor._handle(_update("fill", entry)))  # must not raise
    assert actor.avg_entry_price == 100.0
