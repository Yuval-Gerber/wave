"""UI test bench (born Phase 8.3; moved into Settings as its own section
2026-08-18 at request — kept for good). Exercises the dashboard's
animations without touching markets, orders, or balances.

- Flip board: mimic every trade scenario (long/short entries, scale-out,
  profitable and losing exits) with fake symbols and amounts.
- Connection load: replay the paper/live gray→blue label fill with a random
  real wait (0.3–5s), exactly how a real connect drives it.
"""

from __future__ import annotations

import random

from PyQt6.QtCore import QDate, Qt, QTimer
from PyQt6.QtWidgets import (
    QDateEdit,
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme

_SYMBOLS = ("AAPL", "TSLA", "NVDA", "SPY", "AMD", "META")
_FAKE_NAMES = {
    "AAPL": "Apple Inc.",
    "TSLA": "Tesla, Inc.",
    "NVDA": "NVIDIA Corporation",
    "SPY": "SPDR S&P 500 ETF Trust",
    "AMD": "Advanced Micro Devices, Inc.",
    "META": "Meta Platforms, Inc.",
}


class TestPage(QWidget):
    """All buttons drive UI-only animations on the given TopBar (and fake
    position cards on the PositionsPage)."""

    def __init__(
        self,
        top_bar,
        positions_page=None,
        performance_page=None,
        scanner_page=None,
        ml_page=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.top_bar = top_bar
        self.positions_page = positions_page
        self.performance_page = performance_page
        self.scanner_page = scanner_page
        self.ml_page = ml_page
        self._rng = random.Random()  # noqa: S311 — fake demo data, not crypto
        self._fake_positions: list[dict] = []
        self._market_closed_demo = False
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick_positions)
        if positions_page is not None:
            # the cards' SELL buttons work on fakes too (real keys are handled
            # by the engine; fake keys only exist here)
            positions_page.sell_requested.connect(self._sell_fake)
            positions_page.tighten_requested.connect(self._tighten_fake)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)  # Settings pads around sections
        card = QFrame()
        card.setProperty("card", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(7)

        title = QLabel("UI test bench")
        title.setProperty("cardTitle", True)
        note = QLabel(
            "Nothing here touches markets or balances — every button drives "
            "UI-only previews with fake data."
        )
        note.setProperty("muted", True)
        note.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(note)

        board = QLabel("Flight-board trades")
        board.setStyleSheet(f"font-weight: 700; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(board)
        grid = QGridLayout()
        grid.setSpacing(8)
        scenarios = (
            ("Long entry", self._long_entry),
            ("Short entry", self._short_entry),
            ("Scale-out (bank half)", self._scale_out),
            ("Close — profit", self._close_profit),
            ("Close — loss", self._close_loss),
            ("Error flag", self._error_flag),
        )
        for index, (label, handler) in enumerate(scenarios):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(grid)

        cards = QLabel("Position cards (fake — Positions tab)")
        cards.setStyleSheet(f"font-weight: 700; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(cards)
        cards_grid = QGridLayout()
        cards_grid.setSpacing(8)
        card_actions = (
            ("Add long position", self._add_long),
            ("Add short position", self._add_short),
            ("Bank half on last", self._bank_half),
            ("Cycle stage on last", self._cycle_stage),
            ("Halt / unhalt last", self._toggle_halt),
            ("Close last position", self._close_last),
            ("Clear fake positions", self._clear_positions),
            ("Market closed on/off (popup demo)", self._toggle_market_closed),
        )
        for index, (label, handler) in enumerate(card_actions):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            cards_grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(cards_grid)

        brain = QLabel("Brain animation (fires the synapse web)")
        brain.setProperty("cardTitle", True)
        layout.addWidget(brain)
        brain_grid = QGridLayout()
        brain_grid.setSpacing(8)
        for i, (label, kind) in enumerate(
            (("10 lessons", "lesson"), ("approve burst", "approve"), ("reject burst", "reject"))
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _c=False, k=kind: self._fire_brain(k))
            brain_grid.addWidget(button, 0, i)
        layout.addLayout(brain_grid)

        tape = QLabel("News tape (Scanner tab NYSE ticker)")
        tape.setProperty("cardTitle", True)
        layout.addWidget(tape)
        tape_grid = QGridLayout()
        tape_grid.setSpacing(8)
        for i, (label, kind) in enumerate((("tape demo", "demo"), ("clear tape", "clear"))):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, k=kind: self._fire_tape(k))
            tape_grid.addWidget(button, 0, i)
        layout.addLayout(tape_grid)

        perf = QLabel("Performance tab (fake history)")
        perf.setStyleSheet(f"font-weight: 700; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(perf)
        perf_grid = QGridLayout()
        perf_grid.setSpacing(8)
        gen = QPushButton("Generate fake trade history")
        clear_perf = QPushButton("Clear fake history")
        for i, button in enumerate((gen, clear_perf)):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            perf_grid.addWidget(button, 0, i)
        gen.clicked.connect(self._generate_performance)
        clear_perf.clicked.connect(self._clear_performance)
        layout.addLayout(perf_grid)

        mind = QLabel("Wave's mind (Scanner tab)")
        mind.setStyleSheet(f"font-weight: 700; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(mind)
        mind_grid = QGridLayout()
        mind_grid.setSpacing(8)
        mind_actions = (
            ("Scan pulse", self._mind_pulse),
            ("Candidate found", self._mind_found),
            ("Reject candidate", self._mind_reject),
            ("Promote candidate", self._mind_promote),
            ("Thought storm", self._mind_storm),
        )
        for index, (label, handler) in enumerate(mind_actions):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            mind_grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(mind_grid)

        # scan-set build loader demo (2026-08-21), with a clear button
        loader = QLabel("Scan-set loading animation (Scanner header)")
        loader.setStyleSheet(f"font-weight: 700; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(loader)
        loader_grid = QGridLayout()
        loader_grid.setSpacing(8)
        play_loader = QPushButton("Play scan-set loading")
        clear_loader = QPushButton("Clear loader")
        for i, button in enumerate((play_loader, clear_loader)):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            loader_grid.addWidget(button, 0, i)
        play_loader.clicked.connect(self._play_scan_loader)
        clear_loader.clicked.connect(self._clear_scan_loader)
        self._loader_timer = QTimer(self)
        self._loader_timer.setInterval(140)
        self._loader_timer.timeout.connect(self._loader_step)
        self._loader_done = 0
        layout.addLayout(loader_grid)

        # calendar probe (2026-08-23): prove Wave labels any DATE
        # correctly — trading day / weekend / named holiday / early close
        calendar_title = QLabel("Calendar check (does Wave know this date?)")
        calendar_title.setStyleSheet(
            f"font-weight: 700; color: {theme.TEXT}; background: transparent;"
        )
        layout.addWidget(calendar_title)
        cal_grid = QGridLayout()
        cal_grid.setSpacing(8)
        self.probe_date = QDateEdit()
        self.probe_date.setCalendarPopup(True)
        self.probe_date.setDate(QDate.currentDate())
        self.probe_date.setDisplayFormat("ddd MMM d yyyy")
        check_date = QPushButton("Check this date")
        random_holiday = QPushButton("Random holiday")
        random_open = QPushButton("Random open day")
        clear_calendar = QPushButton("Clear")
        cal_grid.addWidget(self.probe_date, 0, 0)
        cal_grid.addWidget(check_date, 0, 1)
        cal_grid.addWidget(random_holiday, 0, 2)
        cal_grid.addWidget(random_open, 1, 1)
        cal_grid.addWidget(clear_calendar, 1, 2)
        for button in (check_date, random_holiday, random_open, clear_calendar):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addLayout(cal_grid)
        self.calendar_result = QLabel("pick a date — Wave tells you what it believes about it")
        self.calendar_result.setWordWrap(True)
        self.calendar_result.setStyleSheet(
            f"font-family: Menlo, monospace; font-size: 12px; color: {theme.TEXT_MUTED};"
            f" background: transparent;"
        )
        layout.addWidget(self.calendar_result)
        check_date.clicked.connect(self._check_probe_date)
        random_holiday.clicked.connect(lambda: self._random_probe(want_holiday=True))
        random_open.clicked.connect(lambda: self._random_probe(want_holiday=False))
        clear_calendar.clicked.connect(
            lambda: self.calendar_result.setText(
                "pick a date — Wave tells you what it believes about it"
            )
        )

        connect = QLabel("Connection load (random 0.3–5s real wait)")
        connect.setStyleSheet(f"font-weight: 700; color: {theme.TEXT}; background: transparent;")
        layout.addWidget(connect)
        row = QGridLayout()
        row.setSpacing(8)
        paper = QPushButton("Replay PAPER connect")
        live = QPushButton("Replay LIVE connect")
        fail = QPushButton("Replay LIVE connect — failure")
        for i, button in enumerate((paper, live, fail)):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button, 0, i)
        paper.clicked.connect(lambda: self._replay_connect("paper", ok=True))
        live.clicked.connect(lambda: self._replay_connect("live", ok=True))
        fail.clicked.connect(lambda: self._replay_connect("live", ok=False))
        layout.addLayout(row)

        layout.addStretch(1)
        outer.addWidget(card)
        # rigid heights: a squeezed layout must clip nothing (the bug
        # screenshot — half-cut button labels inside the Settings section)
        for button in card.findChildren(QPushButton):
            button.setMinimumHeight(28)

    # -- calendar probe (real SessionScheduler calls, read-only) ---------------

    def probe_calendar_date(self, year: int, month: int, day: int) -> str:
        """What Wave believes about one date — same code paths the engine
        uses (day_info + regime), nothing simulated."""
        from datetime import UTC, date, datetime, time
        from zoneinfo import ZoneInfo

        from waveapp.engine.session import SessionScheduler, day_info

        probe = date(year, month, day)
        trading, early_close = day_info(probe)
        lines = [probe.strftime("%a %b %d %Y")]
        holiday_name = None
        if not trading and probe.weekday() < 5:
            try:
                from waveapp.engine.session import _get_calendar

                names = _get_calendar().regular_holidays.holidays(
                    probe.isoformat(), probe.isoformat(), return_name=True
                )
                for stamp, name in names.items():
                    if stamp.date() == probe:
                        holiday_name = str(name)
            except Exception:
                holiday_name = None
        if trading:
            close = early_close.strftime("%H:%M") if early_close else "16:00"
            kind = f"TRADING DAY — closes {close} ET"
            if early_close:
                kind += "  (EARLY CLOSE)"
        elif probe.weekday() >= 5:
            kind = "WEEKEND — no day session"
        else:
            kind = f"MARKET HOLIDAY — {holiday_name or 'closed'}"
        lines.append(kind)
        et = ZoneInfo("America/New_York")
        samples = []
        for label, t in (
            ("08:00", time(8, 0)),
            ("10:00", time(10, 0)),
            ("15:30", time(15, 30)),
            ("21:00", time(21, 0)),
        ):
            moment = datetime.combine(probe, t, tzinfo=et).astimezone(UTC)
            samples.append(f"{label} {SessionScheduler.regime(moment).value}")
        lines.append("regimes: " + " · ".join(samples))
        return "\n".join(lines)

    def _check_probe_date(self) -> None:
        qdate = self.probe_date.date()
        try:
            self.calendar_result.setText(
                self.probe_calendar_date(qdate.year(), qdate.month(), qdate.day())
            )
        except Exception as exc:
            self.calendar_result.setText(f"calendar check failed: {exc}")

    def _random_probe(self, want_holiday: bool) -> None:
        from datetime import date, timedelta

        from waveapp.engine.session import day_info

        today = date.today()
        pool = []
        for offset in range(1, 500):
            probe = today + timedelta(days=offset)
            trading, _early = day_info(probe)
            if want_holiday and not trading and probe.weekday() < 5:
                pool.append(probe)
            elif not want_holiday and trading:
                pool.append(probe)
        if not pool:
            self.calendar_result.setText("no matching date found in the next 500 days")
            return
        chosen = self._rng.choice(pool)
        self.probe_date.setDate(QDate(chosen.year, chosen.month, chosen.day))
        self._check_probe_date()

    # -- flip board scenarios (fake data only) --------------------------------

    def _symbol(self) -> str:
        return self._rng.choice(_SYMBOLS)

    def _long_entry(self) -> None:
        self.top_bar.show_trade({"action": "entry", "symbol": self._symbol(), "long": True})

    def _short_entry(self) -> None:
        self.top_bar.show_trade({"action": "entry", "symbol": self._symbol(), "long": False})

    def _scale_out(self) -> None:
        pnl = round(self._rng.uniform(0.5, 9.0), 2)
        self.top_bar.show_trade(
            {"action": "scale", "symbol": self._symbol(), "long": True, "pnl": pnl}
        )

    def _close_profit(self) -> None:
        pnl = round(self._rng.uniform(1.0, 40.0), 2)
        self.top_bar.show_trade(
            {"action": "close", "symbol": self._symbol(), "long": True, "pnl": pnl}
        )

    def _close_loss(self) -> None:
        pnl = -round(self._rng.uniform(1.0, 25.0), 2)
        self.top_bar.show_trade(
            {"action": "close", "symbol": self._symbol(), "long": False, "pnl": pnl}
        )

    def _error_flag(self) -> None:
        self.top_bar.show_alert("data feed stale")

    # -- fake position cards (UI only — ticks live P&L every second) ----------

    def _push_positions(self) -> None:
        if self.positions_page is not None:
            # test override — the engine's empty snapshots can't blink these
            self.positions_page.set_test_positions(list(self._fake_positions))
        if self._fake_positions:
            self._tick_timer.start()
        else:
            self._tick_timer.stop()

    def _add_position(self, long_side: bool) -> None:
        symbol = self._symbol()
        entry = round(self._rng.uniform(20, 400), 2)
        # a plausible intraday candle walk leading up to the entry price
        bars: list[tuple[float, float, float, float]] = []
        price = entry * (1.0 + self._rng.uniform(-0.02, 0.02))
        for _ in range(60):
            open_ = price
            close = round(open_ * (1.0 + self._rng.uniform(-0.003, 0.003)), 2)
            high = round(max(open_, close) * (1.0 + self._rng.uniform(0, 0.0015)), 2)
            low = round(min(open_, close) * (1.0 - self._rng.uniform(0, 0.0015)), 2)
            bars.append((round(open_, 2), high, low, close))
            price = close
        qty = self._rng.choice((5, 10, 25, 50))
        self._fake_positions.append(
            {
                "key": f"fake-{len(self._fake_positions)}-{symbol}",
                "symbol": symbol,
                "name": _FAKE_NAMES.get(symbol, ""),
                "side": "long" if long_side else "short",
                "qty": qty,
                "entry": entry,
                "last": entry,
                "stop": round(entry * (0.985 if long_side else 1.015), 2),
                "target": round(entry * (1.006 if long_side else 0.994), 2),
                "stage": "",
                "strategy": self._rng.choice(("ORB", "VWAP", "GAP")),
                "halted": False,
                "ssr": False,
                "bars": bars,
                "market_open": not self._market_closed_demo,
                "history": [
                    f"09:31:04  {'buy' if long_side else 'sell'} market — filled"
                    f"  {qty:g} @ {entry:.2f}"
                ],
            }
        )
        self._push_positions()
        # positions ride the indicator too — a new one flips in as a BUY
        self.top_bar.show_trade({"action": "entry", "symbol": symbol, "long": long_side})

    def _add_long(self) -> None:
        self._add_position(True)

    def _add_short(self) -> None:
        self._add_position(False)

    def _bank_half(self) -> None:
        """Mimic a scale-out: half the shares banked, card updates to the
        remainder, the banked half's P&L flips on the indicator."""
        if not self._fake_positions:
            return
        position = self._fake_positions[-1]
        banked = max(1, int(position["qty"] // 2))
        if banked >= position["qty"]:
            return  # nothing left to keep running
        direction = 1.0 if position["side"] == "long" else -1.0
        pnl = round((position["last"] - position["entry"]) * banked * direction, 2)
        position["qty"] -= banked
        position["stage"] = "SCALED"
        position.setdefault("history", []).append(
            f"10:02:11  sell limit — filled  {banked:g} @ {position['last']:.2f} (scale-out)"
        )
        position["target"] = None  # first target taken
        self._push_positions()
        self.top_bar.show_trade(
            {
                "action": "scale",
                "symbol": position["symbol"],
                "long": position["side"] == "long",
                "pnl": pnl,
            }
        )

    def _sell_fake(self, key: str) -> None:
        """SELL button on a fake card: close exactly that position, now —
        independent of any other card's sell."""
        for index, position in enumerate(self._fake_positions):
            if position["key"] == key:
                self._fake_positions.pop(index)
                self._push_positions()
                direction = 1.0 if position["side"] == "long" else -1.0
                pnl = round((position["last"] - position["entry"]) * position["qty"] * direction, 2)
                self.top_bar.show_trade(
                    {
                        "action": "close",
                        "symbol": position["symbol"],
                        "long": position["side"] == "long",
                        "pnl": pnl,
                    }
                )
                return

    def _tighten_fake(self, key: str) -> None:
        """Confirmed tighten on a fake: stop moves halfway toward price."""
        for position in self._fake_positions:
            if position["key"] == key:
                position["stop"] = round((position["stop"] + position["last"]) / 2, 2)
                position.setdefault("history", []).append(
                    f"10:15:40  stop amended → {position['stop']:.2f} (manual tighten)"
                )
                self._push_positions()
                return

    def _cycle_stage(self) -> None:
        if not self._fake_positions:
            return
        stages = ("", "BE", "TRAIL", "SCALED")
        position = self._fake_positions[-1]
        position["stage"] = stages[(stages.index(position["stage"]) + 1) % len(stages)]
        self._push_positions()

    def _toggle_halt(self) -> None:
        if not self._fake_positions:
            return
        self._fake_positions[-1]["halted"] = not self._fake_positions[-1]["halted"]
        self._push_positions()

    def _close_last(self) -> None:
        if not self._fake_positions:
            return
        position = self._fake_positions.pop()
        self._push_positions()
        # closing a position flips a SELL with its realized P&L
        direction = 1.0 if position["side"] == "long" else -1.0
        pnl = round((position["last"] - position["entry"]) * position["qty"] * direction, 2)
        self.top_bar.show_trade(
            {
                "action": "close",
                "symbol": position["symbol"],
                "long": position["side"] == "long",
                "pnl": pnl,
            }
        )

    def _clear_positions(self) -> None:
        self._fake_positions.clear()
        self._push_positions()

    def _toggle_market_closed(self) -> None:
        """Preview the popup's closed-market state (wave + note)."""
        self._market_closed_demo = not self._market_closed_demo
        for position in self._fake_positions:
            position["market_open"] = not self._market_closed_demo
        self._push_positions()

    def _tick_positions(self) -> None:
        """Wiggle prices ±0.3% so the P&L (and the candles) live."""
        for position in self._fake_positions:
            previous = position["last"]
            drift = 1.0 + self._rng.uniform(-0.003, 0.003)
            position["last"] = round(previous * drift, 2)
            bars = position.setdefault("bars", [])
            open_, close = previous, position["last"]
            high = round(max(open_, close) * (1.0 + self._rng.uniform(0, 0.001)), 2)
            low = round(min(open_, close) * (1.0 - self._rng.uniform(0, 0.001)), 2)
            bars.append((open_, high, low, close))
            del bars[:-120]
        self._push_positions()

    # -- Wave's mind scenarios (UI only) --------------------------------------

    def _mind(self):
        return None if self.scanner_page is None else self.scanner_page.mind

    def _mind_pulse(self) -> None:
        if self._mind():
            self._mind().pulse_scan()

    def _mind_found(self) -> None:
        if self._mind():
            self._mind().candidate_found(self._symbol())

    def _mind_reject(self) -> None:
        if self._mind():
            reasons = ("spread too wide", "rvol below floor", "expected move < 3× costs")
            self._mind().candidate_rejected(self._symbol(), self._rng.choice(reasons))

    def _mind_promote(self) -> None:
        if self._mind():
            self._mind().candidate_promoted(self._symbol())

    def _mind_storm(self) -> None:
        """A full fake scan cycle: pulse, finds, rejects, a promotion."""
        mind = self._mind()
        if not mind:
            return
        mind.pulse_scan()
        for _ in range(4):
            mind.candidate_found(self._symbol())
        for _ in range(4):
            self._mind_reject()
        mind.candidate_promoted(self._symbol())

    # -- scan-set loader demo (2026-08-21) ------------------------------------

    def _play_scan_loader(self) -> None:
        """Replays the 65-batch universe build on the Scanner header: the
        small wave animates while the numbers count up, then 'ready ✓'."""
        if self.scanner_page is None:
            return
        self._loader_done = 0
        self.scanner_page.set_build_progress(0, 65, "")
        self._loader_timer.start()

    def _loader_step(self) -> None:
        if self.scanner_page is None:
            self._loader_timer.stop()
            return
        self._loader_done += 1
        self.scanner_page.set_build_progress(self._loader_done, 65, "")
        if self._loader_done >= 65:  # ends on "scan set ready ✓"
            self._loader_timer.stop()

    def _clear_scan_loader(self) -> None:
        self._loader_timer.stop()
        self._loader_done = 0
        if self.scanner_page is not None:
            self.scanner_page.restore_status()  # back to the REAL status

    # -- fake performance history (UI only) -----------------------------------

    def _generate_performance(self) -> None:
        """~45 closed trades over the last month + a deposit, all fake."""
        import time

        if self.performance_page is None:
            return
        now = time.time()
        trades = []
        stamp = now - 30 * 86400
        for _ in range(45):
            stamp += self._rng.uniform(0.4, 1.4) * 86400 / 1.5
            win = self._rng.random() < 0.58
            pnl = round(self._rng.uniform(4, 60) if win else -self._rng.uniform(3, 45), 2)
            trades.append({"ts": stamp, "pnl": pnl, "symbol": self._symbol()})
        trades = [t for t in trades if t["ts"] < now]
        self.performance_page.set_test_performance(
            {
                "current_equity": 100_000 + sum(t["pnl"] for t in trades),
                "trades": trades,
                "cashflows": [{"ts": now - 22 * 86400, "amount": 500.0}],
                "fees": 3.42,
                "champion": "v0",
            }
        )

    def _clear_performance(self) -> None:
        if self.performance_page is not None:
            self.performance_page.set_test_performance(None)

    # -- connection fill replay ----------------------------------------------

    def _replay_connect(self, side: str, ok: bool) -> None:
        toggle = self.top_bar.mode_toggle
        toggle.begin_connect(side)
        delay_ms = int(self._rng.uniform(300, 5000))
        QTimer.singleShot(delay_ms, lambda: toggle.finish_connect(side, ok=ok))

    def _fire_tape(self, kind: str) -> None:
        """Test bench: demo headlines on the Scanner-tab NYSE tape, and the
        clear button that stops/empties it (2026-09-01). Demo items
        are obviously fake tickers — the real tape only ever carries the
        live Benzinga/EDGAR stream."""
        if self.scanner_page is None or not hasattr(self.scanner_page, "ticker"):
            return
        ticker = self.scanner_page.ticker
        if kind == "clear":
            ticker.clear()
            return
        for item in (
            {
                "symbol": "DEMO1",
                "direction": 1,
                "headline": "Demo Corp surges on record earnings beat",
            },  # noqa: E501
            {
                "symbol": "DEMO2",
                "direction": -1,
                "headline": "Demo Industries plunges as SEC opens probe",
            },  # noqa: E501
            {"symbol": "DEMO3", "direction": 0, "headline": "Demo Group schedules investor day"},
            {
                "symbol": "DEMO4",
                "direction": 1,
                "headline": "FDA grants Demo Bio approval for Phase 3",
            },  # noqa: E501
            {
                "symbol": "DEMO5",
                "direction": -1,
                "headline": "Demo Energy cuts guidance on weak demand",
            },  # noqa: E501
        ):
            ticker.add_news(item)

    def _fire_brain(self, kind: str) -> None:
        """Test bench: pump synthetic events into the Brain's synapse web."""
        if self.ml_page is None:
            return
        canvas = self.ml_page.canvas
        if kind == "lesson":
            for _ in range(10):
                canvas.candidate_found("")
            canvas.pulse_scan()
        elif kind == "approve":
            for _ in range(6):
                canvas.candidate_promoted("")
        else:
            for _ in range(6):
                canvas.candidate_rejected("")
