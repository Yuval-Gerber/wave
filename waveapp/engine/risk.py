"""RiskEngine (§12) — global, above all actors.

Hard rule 2: nothing here may ever be weakened, bypassed or commented out.
Tests mock AROUND these checks, never remove them.

Responsibilities in Phase 5:
- fixed-fractional position sizing (risk-to-stop ≤ 1% equity; overnight 0.5%;
  short book capped at 50% of long limits),
- max concurrent positions,
- daily (3%) and weekly (6%) loss halts with distinct re-arm rules,
- kill switch state,
- SSR (Rule 201) tracking: triggered symbols block downtick-dependent short
  entries for the rest of the day + the next trading day,
- LULD halt tracking: halted symbols block entries entirely,
- entry freezes for consistency mismatches and stale data.

`can_enter` is the single gate every entry must pass; it returns the reason
so every rejection is loggable (feeds the journal in Phase 7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from waveapp.broker.base import OrderSide

logger = logging.getLogger("wave.risk")


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: float = 1.0  # of equity, to the hard stop
    overnight_risk_pct: float = 0.5
    short_size_factor: float = 0.5  # short book ≤ 50% of long sizing
    max_positions: int = 3
    max_daily_loss_pct: float = 3.0
    max_weekly_loss_pct: float = 6.0
    # notional cap (2026-08-18): the simulator exposed that pure
    # risk-based sizing turns a hair-thin stop distance into six-figure
    # notional — no position may exceed this fraction of equity
    max_notional_pct: float = 25.0
    # market-impact participation cap (2026-08-21): per-trade shares ≤ this
    # % of the symbol's average daily volume — Wave must never be the move
    impact_participation_pct: float = 0.5
    # WAVE 2 item 11 (adopted 2026-09-16): position count follows
    # RISK, not a fixed number. Beyond max_positions, entries are allowed
    # while total open risk-to-stop stays inside this ceiling — positions
    # whose stops are locked above entry consume ZERO budget, so a book of
    # floor-locked winners leaves room to hunt. Hard count cap for ops
    # sanity. Defaults reproduce the old behavior exactly
    # (6 positions × 1% = 6%).
    max_total_risk_pct: float = 6.0
    max_positions_hard: int = 12
    # THE SHORT SIDE (S1, 2026-09-23) — the MASTER gate: False refuses every
    # side=SELL entry in can_enter outright, so no short order can ever reach
    # the broker until the owner flips config.shorts_enabled (S5). Wired from
    # config in the monitor's _make_engine, like the limits above.
    shorts_enabled: bool = False
    # short book risk share (§12: short book capped at ≤50% of long limits):
    # shorts' TOTAL open risk-to-stop may use at most this fraction of
    # max_total_risk_pct. Tightening-only (hard rule 2).
    short_risk_share: float = 0.5


class HaltState(Enum):
    NONE = "none"
    DAILY_LOSS = "daily_loss"
    WEEKLY_LOSS = "weekly_loss"
    KILLED = "killed"


@dataclass
class EntryDecision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class RiskEngine:
    limits: RiskLimits = field(default_factory=RiskLimits)

    _halt: HaltState = HaltState.NONE
    _halt_day: date | None = None  # day the daily halt fired (auto re-arm next day)
    _freeze_reasons: set[str] = field(default_factory=set)
    _day_start_equity: float | None = None
    _week_start_equity: float | None = None
    _current_day: date | None = None
    _ssr: dict[str, date] = field(default_factory=dict)  # symbol -> trigger date
    _luld_halted: set[str] = field(default_factory=set)

    # -- sizing -------------------------------------------------------------

    def position_size(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        side: OrderSide,
        overnight: bool = False,
        avg_daily_volume: float | None = None,
        risk_mult: float = 1.0,
    ) -> int:
        """Whole-share size such that (entry→stop) loss ≤ the risk budget.

        R2 SNIPER jewel sizing (2026-09-24, VALIDATION.md): ``risk_mult``
        scales the RISK BUDGET only — deliberately applied BEFORE the
        notional and impact caps below, so every cap binds on the SCALED
        qty (the caps can only shrink a jewel, never be bypassed by one).
        Clamped to [0, JEWEL max 2×] so no caller can inflate it further."""
        distance = abs(entry_price - stop_price)
        if distance <= 0 or equity <= 0:
            return 0
        pct = self.limits.overnight_risk_pct if overnight else self.limits.risk_per_trade_pct
        budget = equity * pct / 100.0 * min(max(risk_mult, 0.0), 2.0)
        qty = int(budget / distance)
        # notional cap: risk math alone must never size past what the
        # account can actually carry (2026-08-18)
        if entry_price > 0:
            qty = min(qty, int(equity * self.limits.max_notional_pct / 100.0 / entry_price))
        # market-impact cap (2026-08-21, the scale question): stay under
        # a small fraction of the symbol's average daily volume so Wave's own
        # orders never move the price against it. Square-root impact law:
        # at 0.5% of ADV the footprint is ~2-4 bps — inside the cost model's
        # slippage buffer. Rarely binds at current equity; the guardrail that
        # lets sizing scale safely. Tightening-only (§12 hard rule 2).
        if avg_daily_volume and avg_daily_volume > 0:
            qty = min(qty, int(avg_daily_volume * self.limits.impact_participation_pct / 100.0))
        if side is OrderSide.SELL:
            qty = int(qty * self.limits.short_size_factor)
        return max(qty, 0)

    # -- equity tracking & loss halts ---------------------------------------

    def on_session_start(self, equity: float, today: date) -> None:
        self._current_day = today
        self._day_start_equity = equity
        if self._week_start_equity is None or today.weekday() == 0:
            self._week_start_equity = equity
        # daily halt auto re-arms on a new session day
        if self._halt is HaltState.DAILY_LOSS and self._halt_day != today:
            logger.info("daily loss halt auto re-armed for new session")
            self._halt = HaltState.NONE
            self._halt_day = None

    def maybe_roll_session(self, equity: float | None, today: date) -> bool:
        """Audit A3-7: in a continuous 24/5 run on_session_start's only
        caller was engine start — the daily/weekly loss baselines never
        rolled, so 'down 3% today' was measured against a days-old equity
        and Monday never rebased the week. The engine calls this cheaply
        (per poll); on an ET session-date change with KNOWN equity it runs
        the EXISTING on_session_start (which also owns the daily-halt
        auto-re-arm on a new day — behavior preserved exactly, weekly halt
        and kill state untouched). Never rebases on unknown equity."""
        if self._current_day is None or today == self._current_day:
            return False
        if not equity or equity <= 0:
            logger.warning(
                "session date rolled %s → %s but equity is unknown — baselines "
                "NOT rebased yet (will retry when equity is known)",
                self._current_day,
                today,
            )
            return False
        logger.info(
            "session date rolled %s → %s — rebasing risk day/week baselines at equity %.2f",
            self._current_day,
            today,
            equity,
        )
        self.on_session_start(equity, today)
        return True

    def update_equity(self, equity: float) -> None:
        # live-prep (2026-09-23): the freshest known equity feeds the
        # $2,000 short-margin floor in can_enter — tracked even while killed
        self._last_equity = equity
        if self._halt is HaltState.KILLED:
            return
        if self._day_start_equity:
            daily_loss_pct = (self._day_start_equity - equity) / self._day_start_equity * 100
            if daily_loss_pct >= self.limits.max_daily_loss_pct and self._halt is HaltState.NONE:
                self._halt = HaltState.DAILY_LOSS
                self._halt_day = self._current_day
                logger.error(
                    "DAILY LOSS HALT: down %.2f%% (limit %.1f%%) — entries halted for the day",
                    daily_loss_pct,
                    self.limits.max_daily_loss_pct,
                )
        if self._week_start_equity:
            weekly_loss_pct = (self._week_start_equity - equity) / self._week_start_equity * 100
            if weekly_loss_pct >= self.limits.max_weekly_loss_pct and self._halt in (
                HaltState.NONE,
                HaltState.DAILY_LOSS,
            ):
                self._halt = HaltState.WEEKLY_LOSS
                logger.error(
                    "WEEKLY LOSS HALT: down %.2f%% (limit %.1f%%) — manual re-arm required",
                    weekly_loss_pct,
                    self.limits.max_weekly_loss_pct,
                )

    def re_arm_weekly(self) -> None:
        """Only callable from the Touch-ID-gated UI path (§4)."""
        if self._halt is HaltState.WEEKLY_LOSS:
            logger.warning("weekly loss halt re-armed manually")
            self._halt = HaltState.NONE
            self._week_start_equity = None

    # -- kill switch --------------------------------------------------------

    def kill(self) -> None:
        logger.error("KILL SWITCH: engine halted")
        self._halt = HaltState.KILLED

    def re_arm_kill(self) -> None:
        """Only callable from the Touch-ID-gated Start/Re-arm path (§4).
        Clears ONLY a kill halt — loss halts keep their own re-arm rules."""
        if self._halt is HaltState.KILLED:
            logger.warning("kill switch re-armed manually")
            self._halt = HaltState.NONE

    @property
    def halt_state(self) -> HaltState:
        return self._halt

    # -- freezes ------------------------------------------------------------

    def freeze_entries(self, reason: str) -> None:
        if reason not in self._freeze_reasons:
            logger.error("entries frozen: %s", reason)
        self._freeze_reasons.add(reason)

    def unfreeze_entries(self, reason: str) -> None:
        if reason in self._freeze_reasons:
            logger.info("entry freeze lifted: %s", reason)
        self._freeze_reasons.discard(reason)

    # -- SSR & LULD ---------------------------------------------------------

    def record_ssr_trigger(self, symbol: str, on_day: date) -> None:
        logger.warning("SSR triggered on %s (%s): short entries restricted", symbol, on_day)
        self._ssr[symbol.upper()] = on_day

    def ssr_trigger_date(self, symbol: str) -> date | None:
        """Most recent recorded trigger day (S0: the detection sweep uses it
        to record a trigger ONCE per day, not on every minute it holds; a
        fresh ≤−10% day on a still-active symbol re-records with the new
        date — Rule 201's window restarts on a re-trigger)."""
        return self._ssr.get(symbol.upper())

    def ssr_active(self, symbol: str, today: date) -> bool:
        """Active for the trigger day and the next trading day (Mon after Fri)."""
        triggered = self._ssr.get(symbol.upper())
        if triggered is None:
            return False
        next_trading = triggered + timedelta(days=3 if triggered.weekday() == 4 else 1)
        return triggered <= today <= next_trading

    def set_luld_halted(self, symbol: str, halted: bool) -> None:
        if halted:
            logger.warning("LULD halt: %s", symbol)
            self._luld_halted.add(symbol.upper())
        else:
            self._luld_halted.discard(symbol.upper())

    def is_luld_halted(self, symbol: str) -> bool:
        return symbol.upper() in self._luld_halted

    # -- THE gate -----------------------------------------------------------

    def can_enter(
        self,
        symbol: str,
        side: OrderSide,
        open_positions: int,
        today: date,
        open_risk_pct: float | None = None,
        short_open_risk_pct: float | None = None,
    ) -> EntryDecision:
        if self._halt is not HaltState.NONE:
            return EntryDecision(False, f"halted: {self._halt.value}")
        if self._freeze_reasons:
            reasons = ", ".join(sorted(self._freeze_reasons))
            return EntryDecision(False, f"entries frozen: {reasons}")
        if self.limits.max_positions <= 0:
            # unlimited position COUNT (2026-09-21: "the judge proved
            # himself — I want unlimited positions held at the same time").
            # The book is governed by RISK alone: total open risk-to-stop
            # must fit the ceiling. Floor-locked winners risk zero, so a
            # green book always leaves room to hunt.
            if open_risk_pct is None:
                # audit A3-6: with the risk read unavailable (equity unknown)
                # the unlimited book used to fail OPEN — no governor at all.
                # Fall back to the hard count cap until risk is measurable.
                if open_positions >= self.limits.max_positions_hard:
                    return EntryDecision(
                        False,
                        "open risk unknown — hard position cap "
                        f"({self.limits.max_positions_hard}) is the fallback governor",
                    )
            elif open_risk_pct + self.limits.risk_per_trade_pct > self.limits.max_total_risk_pct:
                return EntryDecision(
                    False,
                    f"total open risk ceiling ({self.limits.max_total_risk_pct:g}%) reached",
                )
        elif open_positions >= self.limits.max_positions:
            # WAVE 2 item 11: risk headroom may admit MORE positions than
            # the base count — but only when the caller proves total open
            # risk (locked-green positions count zero) plus this trade's
            # budget fits the ceiling, and never past the hard count cap.
            headroom = (
                open_risk_pct is not None
                and open_positions < self.limits.max_positions_hard
                and open_risk_pct + self.limits.risk_per_trade_pct <= self.limits.max_total_risk_pct
            )
            if not headroom:
                return EntryDecision(
                    False, f"max concurrent positions ({self.limits.max_positions}) reached"
                )
        if self.is_luld_halted(symbol):
            return EntryDecision(False, f"{symbol} is in a LULD halt")
        if side is OrderSide.SELL:
            # S1 MASTER GATE (before the SSR check by design): with the flag
            # off, no SELL entry passes this gate, period — the guarantee
            # that keeps the live long-only app bit-identical until the owner
            # flips config.shorts_enabled at S5.
            if not self.limits.shorts_enabled:
                return EntryDecision(False, "shorts are disabled (shorts_enabled=false)")
            # AUTOMATIC $2,000 floor (2026-09-23 — live-trading prep):
            # US margin regulation allows shorting only at >= $2,000 equity.
            # This gates NEW short entries only; a short already held when
            # equity dips below stays fully managed (exits, judge, stops
            # never pass through can_enter). Paper mirrors live behavior so
            # the ramp holds no surprises.
            equity_now = getattr(self, "_last_equity", None) or self._day_start_equity
            if equity_now is not None and equity_now < 2000.0:
                return EntryDecision(
                    False,
                    f"shorts need $2,000 equity (margin regulation) — have ${equity_now:,.0f}",
                )
            # S1 short-book risk share (§12: short book ≤ 50% of long
            # limits): shorts' total open risk-to-stop, PLUS this trade's
            # budget (shorts are sized at short_size_factor of the per-trade
            # risk), must fit inside short_risk_share of the total ceiling.
            # None = equity unknown; the A3-6 hard-cap fallback above is
            # then the governor, same as for the long book.
            if short_open_risk_pct is not None:
                short_ceiling = self.limits.max_total_risk_pct * self.limits.short_risk_share
                incoming = self.limits.risk_per_trade_pct * self.limits.short_size_factor
                if short_open_risk_pct + incoming > short_ceiling:
                    return EntryDecision(
                        False,
                        f"short book risk share ({short_ceiling:g}% of equity) reached",
                    )
            # Shorts never overnight (v1, §6/§8.1): overnight ENTRIES do not
            # exist in code today — the scanner stands down for the whole
            # OVERNIGHT regime ("overnight session (exit-only)", monitor) and
            # TradeGate refuses is_overnight SELLs outright. If overnight
            # entries ever land, the SELL refusal must be enforced here too.
        # (S1: the A3-8 "SSR detection not wired" tripwire is retired — S0
        # wired real detection: connection_monitor._ssr_sweep records
        # triggers every minute boundary regardless of shorts_enabled.)
        if side is OrderSide.SELL and self.ssr_active(symbol, today):
            return EntryDecision(False, f"SSR active on {symbol} — short entry blocked")
        return EntryDecision(True, "ok")
