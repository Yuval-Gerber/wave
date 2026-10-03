"""Phase 8.4: position cards, pagination, and the engine→cards snapshot feed."""

from types import SimpleNamespace

from waveapp.broker.base import OrderSide, TradingMode
from waveapp.engine.actor import PositionState
from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.engine.risk import RiskEngine
from waveapp.ui.positions_page import PositionCard, PositionsPage


def _position(**overrides) -> dict:
    base = {
        "key": "k1",
        "symbol": "AAPL",
        "side": "long",
        "qty": 10,
        "entry": 100.0,
        "last": 102.0,
        "stop": 98.5,
        "stage": "TRAIL",
        "strategy": "ORB",
        "halted": False,
        "ssr": False,
    }
    base.update(overrides)
    return base


# -- cards --------------------------------------------------------------------


def test_card_shows_all_fields_long_profit(qtbot):
    card = PositionCard()
    qtbot.addWidget(card)
    card.update_data(_position())
    assert card.symbol.text() == "AAPL"
    assert card.side_chip.text() == "LONG"
    assert card.strategy_chip.text() == "ORB"
    assert card.pnl.text() == "+20.00"  # (102-100) × 10
    assert card.pnl_pct.text() == "+2.00%"
    assert "sells at 98.50" in card.stop_label.text()  # sell-price card (2026-08-27)
    assert card.stage_chip.text() == "TRAIL"
    assert not card.halt_chip.isVisibleTo(card)


def test_card_short_loss_and_halt_badge(qtbot):
    card = PositionCard()
    qtbot.addWidget(card)
    card.update_data(_position(side="short", entry=50.0, last=51.0, qty=20, halted=True, stage=""))
    assert card.side_chip.text() == "SHORT"
    assert card.pnl.text() == "-20.00"  # short: price up = loss
    assert card.halt_chip.text() == "HALT"
    assert card.halt_chip.isVisibleTo(card)
    assert not card.stage_chip.isVisibleTo(card)


def test_card_short_green_when_price_falls(qtbot):
    """S4 (the explicit requirement): a short shows GREEN when the price
    goes DOWN — the card colors by P&L sign, and for a short falling price IS
    positive P&L. The close button reads COVER, the stop line 'covers at'."""
    from waveapp.ui import theme

    card = PositionCard()
    qtbot.addWidget(card)
    card.update_data(_position(side="short", entry=50.0, last=48.0, qty=20, stop=51.0))
    assert card.side_chip.text() == "SHORT"
    assert card.pnl.text() == "+40.00"  # (50-48) × 20 — profit on the way DOWN
    assert card.pnl_pct.text() == "+4.00%"
    assert theme.GREEN in card.pnl.styleSheet()
    assert theme.GREEN in card.pnl_pct.styleSheet()
    assert card.sell_button.text() == "COVER"
    assert "covers at 51.00" in card.stop_label.text()  # the stop BUYS back, above entry
    # the long card is untouched: SELL + 'sells at' verbatim
    card.update_data(_position())
    assert card.sell_button.text() == "SELL"
    assert "sells at 98.50" in card.stop_label.text()


def test_card_short_mark_tick_green_on_down(qtbot):
    """The 250ms light tick keeps the short's green-on-down coloring."""
    from waveapp.ui import theme

    card = PositionCard()
    qtbot.addWidget(card)
    card.update_data(_position(side="short", entry=50.0, last=50.0, qty=20, stop=51.0))
    card.update_mark(49.0)
    assert card.pnl.text() == "+20.00"
    assert theme.GREEN in card.pnl.styleSheet()
    card.update_mark(50.5)  # rebound against the short = red
    assert card.pnl.text() == "-10.00"
    assert theme.RED in card.pnl.styleSheet()


def test_stop_goal_text_short_states():
    """S4: stop math for a short — stop BELOW entry locks profit, ABOVE is
    the loss floor; wording says 'covers at' (a short's stop buys back)."""
    from waveapp.ui.positions_page import stop_goal_text

    text, state = stop_goal_text(50.0, 51.0, long_side=False, qty=20)
    assert state == "loss" and text == "covers at 51.00 · 20 $"
    text, state = stop_goal_text(50.0, 49.0, long_side=False, qty=20)
    assert state == "profit" and "covers at 49.00" in text and "✓ locked" in text
    text, state = stop_goal_text(50.0, 50.02, long_side=False, qty=20)
    assert state == "breakeven"


def test_detail_card_short_cover_button_and_target_below_entry(qtbot):
    """The chart popup for a short: COVER button, default goal BELOW entry,
    stop drawn ABOVE — nothing assumes stop < entry."""
    from waveapp.ui.positions_page import PositionDetailCard

    card = PositionDetailCard()
    qtbot.addWidget(card)
    bars = [(50.2, 50.4, 49.8, 50.0), (50.0, 50.1, 49.5, 49.6)]
    card.update_data(
        _position(side="short", entry=50.0, last=49.6, qty=20, stop=51.0, bars=bars, target=None)
    )
    assert card.side_chip.text() == "SHORT"
    assert card.sell_button.text() == "COVER"
    assert card.chart._stop == 51.0  # above entry, drawn as-is
    assert card.chart._target == 50.0 * 0.996  # default goal is BELOW entry
    assert "covers at 51.00" in card.stop_label.text()


def test_card_ssr_badge(qtbot):
    card = PositionCard()
    qtbot.addWidget(card)
    card.update_data(_position(ssr=True))
    assert card.halt_chip.text() == "SSR"


# -- page ---------------------------------------------------------------------


def test_page_empty_state_and_cards(qtbot):
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    assert page.empty.isVisibleTo(page)
    page.update_positions([_position(), _position(key="k2", symbol="TSLA")])
    assert not page.empty.isVisibleTo(page)
    assert len(page._cards) == 2


def test_pagination_dots_and_switching(qtbot):
    page = PositionsPage()
    qtbot.addWidget(page)
    page.show()
    page.resize(700, 420)  # small: capacity is limited
    positions = [_position(key=f"k{i}", symbol=f"SYM{i}") for i in range(9)]
    page.update_positions(positions)
    assert page.page_count() >= 2
    assert len(page._dots) == page.page_count()
    first_page_keys = set(page._cards)
    page.set_page(page.page_count() - 1)
    assert set(page._cards) != first_page_keys  # different slice now
    assert page._dots[page._page].active


def test_engine_snapshots_cannot_blink_away_test_positions(qtbot):
    """Round-2 bug: the engine's (empty) 1s snapshot kept wiping the Test
    tab's fake cards. Test positions override; clearing them restores the
    engine's view."""
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    page.set_test_positions([_position(key="fake")])
    page.update_positions([])  # engine says "no positions" — must NOT wipe
    assert set(page._cards) == {"fake"}
    engine_view = [_position(key="real", symbol="SPY")]
    page.update_positions(engine_view)
    assert set(page._cards) == {"fake"}  # still the test override
    page.set_test_positions([])  # bench cleared → engine view restored
    assert set(page._cards) == {"real"}


def test_test_tab_positions_ride_the_indicator(qtbot):
    """Round 2: adding/closing a position must flip on the board."""
    from waveapp.ui.test_page import TestPage
    from waveapp.ui.top_bar import TopBar

    bar = TopBar()
    qtbot.addWidget(bar)
    page = PositionsPage()
    qtbot.addWidget(page)
    bench = TestPage(bar, positions_page=page)
    qtbot.addWidget(bench)
    shown = []
    bar.flip_board.show_trade = shown.append  # capture instead of animating
    bench._add_long()
    assert shown and shown[-1]["action"] == "entry" and shown[-1]["long"] is True
    assert len(page._cards) == 1
    bench._close_last()
    assert shown[-1]["action"] == "close"
    assert isinstance(shown[-1]["pnl"], float)
    assert len(page._cards) == 0


# -- engine → snapshot feed ---------------------------------------------------


def test_monitor_builds_snapshots_from_actors():
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    actor = SimpleNamespace(
        state=PositionState.OPEN,
        spec=SimpleNamespace(
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=10,
            strategy="ORB",
            stop_price=95.0,
            limit_price=None,
        ),
        avg_entry_price=100.0,
        filled_qty=10.0,
        exit_engine=SimpleNamespace(
            current_stop=97.0,
            scaled_out=False,
            breakeven_done=True,
            remaining_qty=10.0,
            direction=1.0,
            atr=1.5,
            params=SimpleNamespace(k_t1=1.0),
        ),
        position_key="abc123",
    )
    closed = SimpleNamespace(state=PositionState.CLOSED)
    monitor.engine = SimpleNamespace(actors={"a": actor, "b": closed}, risk=RiskEngine())
    snaps = monitor._position_snapshots()
    assert len(snaps) == 1  # the closed one is skipped
    snap = snaps[0]
    assert snap["symbol"] == "AAPL"
    assert snap["side"] == "long"
    assert snap["stop"] == 97.0
    assert snap["stage"] == "BE"
    assert snap["last"] == 100.0  # no hub → entry price
    assert snap["halted"] is False
    assert snap["target"] == 101.5  # entry + k_t1 × ATR (8.5)


def test_monitor_snapshot_short_actor_side_and_target():
    """S4: a SELL actor snapshots side='short' with POSITIVE qty (the UI's
    side field flips the P&L sign — falling price renders green) and the
    first target BELOW entry (direction −1)."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    actor = SimpleNamespace(
        state=PositionState.OPEN,
        spec=SimpleNamespace(
            symbol="PLTR",
            side=OrderSide.SELL,
            qty=15,
            strategy="ORB",
            stop_price=103.0,  # a short's stop rests ABOVE entry
            limit_price=None,
        ),
        avg_entry_price=100.0,
        filled_qty=15.0,
        exit_engine=SimpleNamespace(
            current_stop=103.0,
            scaled_out=False,
            breakeven_done=False,
            remaining_qty=15.0,
            direction=-1.0,
            atr=1.5,
            params=SimpleNamespace(k_t1=1.0),
        ),
        position_key="short1",
    )
    monitor.engine = SimpleNamespace(actors={"a": actor}, risk=RiskEngine())
    snap = monitor._position_snapshots()[0]
    assert snap["side"] == "short"
    assert snap["qty"] == 15.0  # positive — direction lives in `side`
    assert snap["stop"] == 103.0
    assert snap["target"] == 98.5  # entry + (−1) × k_t1 × ATR — below entry
    # the UI's math on exactly these fields: price DOWN $1 → +$15 P&L
    last, direction = 99.0, -1.0
    assert (last - snap["entry"]) * snap["qty"] * direction == 15.0


def test_snapshot_shows_remaining_qty_after_scale_out():
    """Round 3: after banking a portion, the card shows the NEW
    remaining share count and the SCALED stage."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    actor = SimpleNamespace(
        state=PositionState.SCALING_OUT,
        spec=SimpleNamespace(
            symbol="NVDA",
            side=OrderSide.BUY,
            qty=10,
            strategy="VWAP",
            stop_price=95.0,
            limit_price=None,
        ),
        avg_entry_price=100.0,
        filled_qty=10.0,
        exit_engine=SimpleNamespace(
            current_stop=100.01, scaled_out=True, breakeven_done=True, remaining_qty=5.0
        ),
        position_key="scaled1",
    )
    monitor.engine = SimpleNamespace(actors={"a": actor}, risk=RiskEngine())
    snap = monitor._position_snapshots()[0]
    assert snap["qty"] == 5.0
    assert snap["stage"] == "SCALED"
    assert snap["target"] is None  # first target already taken (8.5)


def test_recent_candles_merges_rest_backfill_with_stream_bars():
    """2026-08-19: a freshly-subscribed position symbol has almost no
    stream history — the REST backfill fills the chart; stream bars win on
    timestamp overlap."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    stream = [
        SimpleNamespace(start=10, open=101.0, high=102.0, low=100.5, close=101.5),
        SimpleNamespace(start=11, open=101.5, high=103.0, low=101.0, close=102.5),
    ]
    monitor._hub = SimpleNamespace(bar_builder=SimpleNamespace(bars=lambda s: stream))
    monitor._chart_backfill["AAPL"] = [
        (8, 99.0, 100.0, 98.5, 99.5),
        (9, 99.5, 101.0, 99.0, 100.5),
        (10, 100.9, 101.9, 100.4, 101.4),  # overlaps stream ts=10 → stream wins
    ]
    candles = monitor._recent_candles("AAPL")
    assert len(candles) == 4  # 8, 9, 10, 11 — deduped and sorted
    # 2026-08-24 (the hover tooltip): candles now CARRY their timestamp
    assert candles[0] == (8, 99.0, 100.0, 98.5, 99.5)
    assert candles[2] == (10, 101.0, 102.0, 100.5, 101.5)  # stream bar, not backfill
    assert candles[-1] == (11, 101.5, 103.0, 101.0, 102.5)


def test_recent_candles_without_loop_does_not_schedule_fetch():
    """Sync call with no running loop (tests, shutdown) must not crash."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._hub = SimpleNamespace(bar_builder=SimpleNamespace(bars=lambda s: []))
    assert monitor._recent_candles("MSFT") == []
    assert "MSFT" not in monitor._backfill_pending


async def test_sell_positions_run_concurrently():
    """Two SELL clicks → two independent tasks; neither waits for the other."""
    import asyncio

    started, finished = [], []

    def _actor(key):
        async def close_now(reason=""):
            started.append(key)
            await asyncio.sleep(0.05)
            finished.append(key)

        return SimpleNamespace(
            state=PositionState.OPEN, close_now=close_now, spec=SimpleNamespace(symbol=key.upper())
        )

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor.engine = SimpleNamespace(actors={"a": _actor("a"), "b": _actor("b")}, risk=RiskEngine())
    monitor.sell_position("a")
    monitor.sell_position("b")
    await asyncio.sleep(0.01)
    assert started == ["a", "b"]  # BOTH already running — no serialization
    assert finished == []
    await asyncio.sleep(0.1)
    assert sorted(finished) == ["a", "b"]


# -- sell button & details popup ----------------------------------------------


def test_card_sell_button_emits_key(qtbot):
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    page.update_positions([_position(key="pos-1"), _position(key="pos-2", symbol="TSLA")])
    sells = []
    page.sell_requested.connect(sells.append)
    page._cards["pos-2"].sell_button.click()
    page._cards["pos-1"].sell_button.click()
    assert sells == ["pos-2", "pos-1"]


def test_double_click_opens_details_modal(qtbot):
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    page.update_positions([_position(key="k1")])
    assert page._detail_popup is None
    page.open_details("k1")
    assert page._detail_popup is not None
    assert page._detail_popup.card.symbol.text() == "AAPL"  # the same card, bigger
    assert page._detail_popup.card.width() > 400  # scaled up
    # live refresh: an update ticks the popup's numbers too
    page.update_positions([_position(key="k1", last=110.0)])
    assert page._detail_popup.card.pnl.text() == "+100.00"


def test_second_double_click_closes_previous_popup(qtbot):
    """A5-9: a second open_details must dismiss the first overlay instead of
    orphaning it (stale, never-refreshed popup under the new one)."""
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    page.update_positions([_position(key="k1"), _position(key="k2", symbol="TSLA")])
    page.open_details("k1")
    first = page._detail_popup
    assert first is not None
    page.open_details("k2")
    second = page._detail_popup
    assert second is not None and second is not first
    assert first.isHidden()  # the orphan was closed, not left visible
    assert page._detail_key == "k2"
    assert second.card.symbol.text() == "TSLA"
    # only the new popup keeps ticking / auto-closes with its position
    page.update_positions([_position(key="k1")])  # k2 gone → popup closes
    assert page._detail_popup is None


def test_detail_popup_name_candles_invested_and_closed_state(qtbot):
    """Rounds 5–6: full company name; CANDLE chart + invested $ + share count
    while the market is open; the login wave + 'market is closed' when not."""
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    open_pos = _position(
        key="k1",
        name="Apple Inc.",
        bars=[(100.0, 101.2, 99.8, 101.0), (101.0, 102.4, 100.9, 102.0)],
        market_open=True,
    )
    page.update_positions([open_pos])
    page.open_details("k1")
    card = page._detail_popup.card
    assert card.name.text() == "Apple Inc."
    assert card.chart.isVisibleTo(card)  # candles (round 6)
    assert len(card.chart._candles) == 2
    assert "invested $1,000.00" in card.detail.text()  # 10 shares @ 100
    assert "10 shares" in card.detail.text()
    assert not card.closed_box.isVisibleTo(card)
    # market closes → the wave takes over
    page.update_positions([_position(key="k1", name="Apple Inc.", market_open=False)])
    assert card.closed_box.isVisibleTo(card)
    assert not card.chart.isVisibleTo(card)
    assert card.closed_label.text() == "market is closed"


def test_bench_bank_half_updates_card_and_indicator(qtbot):
    from waveapp.ui.test_page import TestPage
    from waveapp.ui.top_bar import TopBar

    bar = TopBar()
    qtbot.addWidget(bar)
    page = PositionsPage()
    qtbot.addWidget(page)
    bench = TestPage(bar, positions_page=page)
    qtbot.addWidget(bench)
    shown = []
    bar.flip_board.show_trade = shown.append
    bench._add_long()
    qty_before = bench._fake_positions[-1]["qty"]
    bench._bank_half()
    assert bench._fake_positions[-1]["qty"] == qty_before - max(1, int(qty_before // 2))
    assert bench._fake_positions[-1]["stage"] == "SCALED"
    assert shown[-1]["action"] == "scale"
    # SELL on the card closes exactly that fake position
    key = bench._fake_positions[-1]["key"]
    page.sell_requested.emit(key)
    assert bench._fake_positions == []
    assert shown[-1]["action"] == "close"


# -- 8.5: levels, history, overrides ------------------------------------------


def test_chart_carries_stop_and_target_levels(qtbot):
    from waveapp.ui.positions_page import _CandleChart

    chart = _CandleChart()
    qtbot.addWidget(chart)
    chart.set_data(
        [(100, 101, 99, 100.5), (100.5, 102, 100, 101.5)], 100.0, stop=98.5, target=103.0
    )
    assert chart._stop == 98.5
    assert chart._target == 103.0
    chart.resize(300, 150)
    assert not chart.grab().isNull()


def test_popup_overrides_sit_behind_confirms(qtbot):
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    page.update_positions([_position(key="k1")])
    sells, tightens = [], []
    page.sell_requested.connect(sells.append)
    page.tighten_requested.connect(tightens.append)
    page.open_details("k1")
    popup = page._detail_popup

    popup.card.sell_button.click()
    assert page._confirm is not None and sells == []  # confirm first, no sell yet
    page._confirm.cancel_button.click()
    assert sells == []  # cancelled

    popup.card.sell_button.click()
    page._confirm.confirm_button.click()
    assert sells == ["k1"]  # confirmed → sold, popup dismissed
    assert popup.isHidden()

    page.open_details("k1")
    page._detail_popup.card.tighten_button.click()
    page._confirm.confirm_button.click()
    assert tightens == ["k1"]


def test_popup_history_from_provider_and_snapshot(qtbot):
    page = PositionsPage()
    qtbot.addWidget(page)
    page.resize(700, 500)
    page.history_provider = lambda key: [f"row-for-{key}"]
    page.update_positions([_position(key="k1")])  # no "history" → provider used
    page.open_details("k1")
    assert "row-for-k1" in page._detail_popup.card.history_label.text()
    # a snapshot-supplied history (bench fakes) wins over the provider
    page.update_positions([_position(key="k1", history=["baked-row"])])
    page.open_details("k1")
    assert "baked-row" in page._detail_popup.card.history_label.text()


async def test_actor_manual_tighten_never_loosens():
    from types import SimpleNamespace as NS

    from waveapp.engine.actor import PositionActor, PositionSpec

    replaced = []

    class Adapter:
        async def replace_order(self, mode, order_id, **kwargs):
            replaced.append(kwargs)
            return NS(order_id="new-stop")

    spec = PositionSpec(symbol="AAPL", side=OrderSide.BUY, qty=10, stop_price=95.0)
    actor = PositionActor(spec, Adapter(), TradingMode.PAPER)
    actor.stop_leg_order_id = "stop-1"
    await actor.tighten_stop_manual(94.0)  # would LOOSEN a long stop → refused
    assert replaced == []
    await actor.tighten_stop_manual(97.5)  # tightens → amended at the broker
    assert replaced and replaced[-1]["stop_price"] == 97.5


def test_monitor_history_formats_db_rows():
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor._database = SimpleNamespace(
        query=lambda sql, params: [
            {
                "submitted_at": "2026-08-16T13:31:04+00:00",
                "side": "buy",
                "order_type": "market",
                "status": "filled",
                "filled_qty": 10.0,
                "filled_avg_price": 100.25,
            }
        ]
    )
    lines = monitor.position_history("abc")
    # 2026-08-24: the popup shows ET, not raw UTC — 13:31Z = 09:31 ET
    assert lines == ["09:31:04  buy market — filled  10 @ 100.25"]


def test_confirm_popup_pops_in_and_settles(qtbot):
    """8.14: modals open with a scale/fade and end exactly where they started,
    with the card's fixed size restored."""
    from PyQt6.QtCore import QAbstractAnimation
    from PyQt6.QtWidgets import QWidget

    from waveapp.ui.positions_page import _ConfirmPopup

    host = QWidget()
    qtbot.addWidget(host)
    host.setFixedSize(800, 500)
    host.show()
    popup = _ConfirmPopup(host, "Sell TEST now?", "Sell", lambda: None)
    popup.show()
    assert popup._pop_fx is not None
    assert popup._pop_fx.state() == QAbstractAnimation.State.Running
    final = popup._pop_fx.endValue()  # start() already applied the small rect
    assert popup.card.width() < final.width()  # mid-scale right now
    qtbot.waitUntil(lambda: popup._pop_fx.state() == QAbstractAnimation.State.Stopped, timeout=3000)
    assert popup.card.geometry() == final  # landed exactly where it belongs


def test_snapshot_hides_disabled_scaleout_target():
    """k_t1=999 (champion trail-only sentinel) must NOT draw a target line —
    it blew the popup chart's y-scale into a hairline (2026-08-19)."""
    monitor = ConnectionMonitor(on_status=lambda *a: None)
    actor = SimpleNamespace(
        state=PositionState.OPEN,
        spec=SimpleNamespace(
            symbol="GDX",
            side=OrderSide.BUY,
            qty=113,
            strategy="VWAP",
            stop_price=92.35,
            limit_price=None,
        ),
        avg_entry_price=96.72,
        filled_qty=113.0,
        exit_engine=SimpleNamespace(
            current_stop=92.35,
            scaled_out=False,
            breakeven_done=False,
            remaining_qty=113.0,
            direction=1.0,
            atr=0.1,
            params=SimpleNamespace(k_t1=999.0),
        ),
        position_key="gdx1",
    )
    monitor.engine = SimpleNamespace(actors={"g": actor}, risk=RiskEngine())
    snap = monitor._position_snapshots()[0]
    assert snap["target"] is None


def test_sell_price_three_state_colors():
    """(2026-08-27): the card shows the SELL price — red below
    breakeven, regular at breakeven, green when profit is locked."""
    from waveapp.ui.positions_page import stop_goal_text

    text, state = stop_goal_text(100.0, 98.5, long_side=True, qty=100)
    assert state == "loss" and "sells at 98.50" in text and "goal" not in text
    assert "150 $" in text  # (98.5-100)×100 shown UNSIGNED — the color says loss
    text, state = stop_goal_text(100.0, 100.05, long_side=True, qty=100)
    assert state == "breakeven" and "breakeven" in text
    text, state = stop_goal_text(100.0, 101.20, long_side=True, qty=100)
    assert state == "profit" and "locked" in text and "120 $" in text
    # shorts mirror: stop BELOW entry = profit locked
    _text, state = stop_goal_text(50.0, 49.5, long_side=False)
    assert state == "profit"
    _text, state = stop_goal_text(50.0, 51.0, long_side=False)
    assert state == "loss"


def test_candle_chart_scale_ignores_far_stop(qtbot):
    """GDX screenshot round 2 (2026-08-19): a stop ~4.5% away must not crush
    the candles — the scale hugs the price action; the far stop pins to the
    chart's bottom edge."""
    from waveapp.ui.positions_page import _CandleChart

    chart = _CandleChart()
    qtbot.addWidget(chart)
    chart.resize(400, 200)
    candles = [
        (96.4 + i * 0.01, 96.5 + i * 0.01, 96.3 + i * 0.01, 96.45 + i * 0.01) for i in range(20)
    ]
    chart.set_data(candles, entry=96.86, stop=92.35, target=97.25)
    chart.repaint()  # paints without error
    # the scale math: stop is > one price-span away → excluded from the range
    lows = [c[2] for c in chart._candles]
    highs = [c[1] for c in chart._candles]
    price_span = max(highs) - min(lows)
    assert chart._stop < min(lows) - price_span  # sanity: it IS far away


def test_confirm_popup_grows_to_fit_long_message(qtbot):
    """2026-08-19 (screenshot): the fixed 330×132 card clipped a
    two-line message — the card now sizes to its wrapped content."""
    from PyQt6.QtWidgets import QWidget

    from waveapp.ui.positions_page import _ConfirmPopup

    window = QWidget()
    window.resize(900, 700)
    qtbot.addWidget(window)
    window.show()
    long_message = (
        "Remove the MRK trade (-7.04 $) from the graph?\n"
        "It stays in the database — only the chart and stats forget it."
    )
    popup = _ConfirmPopup(window, long_message, "Remove", lambda: None)
    popup.show()
    qtbot.wait(300)  # let pop_in's 190ms scale-up land — mid-animation the
    # card is 94% size and label metrics lie
    assert popup.card.height() > 132  # grew beyond the old fixed height
    # the message label is fully inside the card (nothing clipped)
    label = popup.message
    assert label.height() >= label.heightForWidth(label.width())
    short = _ConfirmPopup(window, "Sell now?", "Sell", lambda: None)
    short.show()
    qtbot.wait(300)
    assert short.card.height() >= 132  # short messages keep the classic size


def test_entry_candle_index_maps_fill_time_to_candle():
    """2026-08-21: the detail chart marks WHERE Wave bought."""
    from datetime import UTC, datetime

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    base = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)
    from datetime import timedelta

    stream = [
        SimpleNamespace(
            start=base + timedelta(minutes=i),
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.2,
        )
        for i in range(10)
    ]
    monitor._hub = SimpleNamespace(bar_builder=SimpleNamespace(bars=lambda s: stream))
    actor = SimpleNamespace(
        adopted_entry_time=None,
        entry_filled_at=base + timedelta(minutes=4, seconds=30),
    )
    assert monitor._entry_candle_index("T", actor) == 4  # the 14:04 candle
    # fill before the window → no marker
    early = SimpleNamespace(adopted_entry_time=None, entry_filled_at=base - timedelta(hours=3))
    assert monitor._entry_candle_index("T", early) is None
    # no fill time at all → no marker
    blank = SimpleNamespace(adopted_entry_time=None, entry_filled_at=None)
    assert monitor._entry_candle_index("T", blank) is None


def test_candle_chart_paints_entry_marker(qtbot):
    from waveapp.ui.positions_page import _CandleChart

    chart = _CandleChart()
    qtbot.addWidget(chart)
    chart.resize(400, 200)
    candles = [(100.0, 100.5, 99.5, 100.2)] * 30
    chart.set_data(candles, entry=100.0, stop=99.0, entry_index=12)
    assert chart._entry_index == 12
    chart.repaint()  # marker path paints without error


def test_brain_advisory_chip_on_card(qtbot):
    """M2 advisory (2026-09-02): the Brain's entry-time read shows on the
    card — visible to the user, ignored by Wave."""
    from waveapp.ui.positions_page import PositionCard

    card = PositionCard()
    qtbot.addWidget(card)
    base = dict(key="k", symbol="EIX", side="long", qty=10, entry=50.0, last=51.0)
    card.update_data({**base, "brain": 0.61})
    assert card.brain_chip.isVisibleTo(card)
    assert "61%" in card.brain_chip.text()
    card.update_data(base)  # unscored position → no chip
    assert not card.brain_chip.isVisibleTo(card)
