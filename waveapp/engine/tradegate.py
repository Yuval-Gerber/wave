"""TradeGate (§8.4) — the cost gate, first-class and checked before
every entry. Hard rule 2: never weakened or bypassed.

Refuse the trade unless:
    expected_move ≥ (measured spread + slippage buffer + regulatory fees
                     + borrow fee if short) × safety_multiple   (default 3×)

Additional gates:
- default universe: spread must be < ~15% of the profit target; wide-spread
  names pass only via the big-expected-move exception;
- overnight entries require overnight eligibility (limit-only and reduced
  size are enforced by the engine when overnight entries arrive in v1+).

Every rejection carries its reason — the journal and the Scanner tab feed on
them (§10.3). Regulatory fees come from the fee_schedule table (fees are data,
not literals — §7); conservative hardcoded values back them up if the DB is
unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from waveapp.broker.base import OrderSide
from waveapp.persistence.db import Database

logger = logging.getLogger("wave.scanner.gate")

# conservative fallbacks if the fee_schedule table is unreachable
_FALLBACK_SEC_PER_DOLLAR_SOLD = 22.0 / 1_000_000
_FALLBACK_TAF_PER_SHARE = 0.000195

# S0 borrow-cost model (§8.4 "borrow fee if short" — paper shows NO borrow
# fees, hard rule 10 says model them anyway). ETB names run cheap: Alpaca's
# published typical easy-to-borrow rate is ~0.3%/yr (industry ETB range
# 0.3–1%/yr); fees accrue per calendar day on the short's notional. Wave
# shorts are intraday-only in v1, so ONE day at 0.3%/360 of price is the
# conservative line: ≈ $0.0008 on a $100 share — negligible but real, and
# the gate's ×3 safety multiple applies to it like every other cost.
_ETB_BORROW_RATE_ANNUAL = 0.003


def etb_borrow_fee_per_share(price: float, days_held: float = 1.0) -> float:
    """$ per share of modeled ETB borrow cost for `days_held` calendar days."""
    return max(price, 0.0) * _ETB_BORROW_RATE_ANNUAL / 360.0 * days_held


def dynamic_expected_move(
    baseline_move: float,
    recent_closes: list[float],
    horizon_minutes: float = 60.0,
    k: float = 1.0,
) -> float:
    """Blueprint 6.1 seam (research, not yet wired to live entries): the
    gate formula stays untouched — its INPUT becomes state-aware.

        expected_move = max(baseline_ATR_scaled,
                            k × realized_vol(1-min, last 10-15m) × sqrt(T))

    On a stock up 15% with 1-min vol 5× baseline, yesterday's ATR says
    "refuse" while the actual near-term move has already dwarfed the spread
    — this was the flat-day "found movers, refused them" hole. Callers pass
    the last ~10-15 one-minute closes; fewer than 6 → baseline unchanged.
    Adoption only via the §10.2 simulator + §11 (paper fills flatter this)."""
    import math

    if len(recent_closes) < 6:
        return baseline_move
    rets = []
    for prev, cur in zip(recent_closes[:-1], recent_closes[1:], strict=True):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 5:
        return baseline_move
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    per_minute_vol = math.sqrt(var)
    price = recent_closes[-1]
    dynamic = k * per_minute_vol * math.sqrt(horizon_minutes) * price
    return max(baseline_move, dynamic)


@dataclass(frozen=True)
class GateInputs:
    symbol: str
    side: OrderSide
    price: float
    expected_move: float  # $ per share, ATR-scaled from signal stats
    profit_target: float  # $ per share (the 0.3–0.5% zone, ATR-scaled)
    spread: float  # $ per share, measured
    slippage_buffer: float  # $ per share (live-calibrated from Phase 11)
    borrow_fee: float = 0.0  # $ per share per day, shorts only (0 → modeled ETB rate)
    is_overnight: bool = False
    overnight_eligible: bool = False
    # S0 short side: per-asset borrow status from broker metadata. Defaults
    # FAIL CLOSED — a SELL candidate that doesn't explicitly prove
    # shortable + easy_to_borrow is refused (S3 passes the real flags
    # through via adapter.asset_shortable). Longs never read these.
    shortable: bool = False
    easy_to_borrow: bool = False


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reason: str
    total_cost: float = 0.0
    required_move: float = 0.0

    def __bool__(self) -> bool:
        return self.accepted


class TradeGate:
    def __init__(
        self,
        database: Database | None = None,
        safety_multiple: float = 3.0,
        max_spread_fraction_of_target: float = 0.15,
        wide_spread_exception_ratio: float = 10.0,
    ) -> None:
        self.db = database
        self.safety_multiple = safety_multiple
        self.max_spread_fraction = max_spread_fraction_of_target
        self.wide_spread_exception_ratio = wide_spread_exception_ratio

    # -- fees (data, not literals — §7) -------------------------------------

    def regulatory_fees_per_share(self, price: float, side: OrderSide) -> float:
        """SEC §31 + FINRA TAF apply to SELLS; a round trip always includes
        one sell, so the gate charges them on every trade."""
        sec_rate = _FALLBACK_SEC_PER_DOLLAR_SOLD
        taf = _FALLBACK_TAF_PER_SHARE
        if self.db is not None:
            try:
                sec_row = self.db.active_fee("sec_section31")
                if sec_row is not None:
                    sec_rate = float(sec_row["rate"]) / 1_000_000
                taf_row = self.db.active_fee("finra_taf")
                if taf_row is not None:
                    taf = float(taf_row["rate"])
            except Exception:
                logger.exception("fee lookup failed — using fallback constants")
        return price * sec_rate + taf

    # -- THE gate ------------------------------------------------------------

    def evaluate(self, inputs: GateInputs) -> GateDecision:
        fees = self.regulatory_fees_per_share(inputs.price, inputs.side)
        borrow = 0.0
        if inputs.side is OrderSide.SELL:
            # S0 hard refusal: non-shortable / hard-to-borrow → no trade.
            # HTB rates are unknowable pre-locate and routinely exceed the
            # whole 30–50 bps edge; §8.4 refuses rather than guesses.
            if not (inputs.shortable and inputs.easy_to_borrow):
                return GateDecision(
                    False, f"{inputs.symbol} is not shortable/easy-to-borrow — short refused"
                )
            # ETB: caller-measured fee if given, else the modeled constant
            borrow = inputs.borrow_fee or etb_borrow_fee_per_share(inputs.price)
        total_cost = inputs.spread + inputs.slippage_buffer + fees + borrow
        required = total_cost * self.safety_multiple

        if inputs.is_overnight and inputs.side is OrderSide.SELL:
            # S1 (§6/§8.1): shorts NEVER overnight in v1 — overnight is
            # exit/risk-management only, and the short book is stricter than
            # longs. Note: overnight ENTRIES do not exist in code today (the
            # scanner stands down for the whole OVERNIGHT regime, monitor
            # "overnight session (exit-only)"); this refusal is the backstop
            # for the day is_overnight callers ever appear.
            return GateDecision(False, "shorts never enter overnight (v1)", total_cost, required)
        if inputs.is_overnight and not inputs.overnight_eligible:
            return GateDecision(
                False, "overnight entry on a non-overnight-eligible symbol", total_cost, required
            )

        if inputs.expected_move < required:
            return GateDecision(
                False,
                (
                    f"expected move ${inputs.expected_move:.3f} < required "
                    f"${required:.3f} ({self.safety_multiple:g}x costs "
                    f"${total_cost:.3f})"
                ),
                total_cost,
                required,
            )

        max_spread = self.max_spread_fraction * inputs.profit_target
        if inputs.spread > max_spread:
            # wide-spread names only via the big-expected-move exception
            if inputs.expected_move < self.wide_spread_exception_ratio * inputs.spread:
                return GateDecision(
                    False,
                    (
                        f"spread ${inputs.spread:.3f} > {self.max_spread_fraction:.0%} "
                        f"of target ${inputs.profit_target:.3f} and expected move "
                        f"does not qualify for the wide-spread exception"
                    ),
                    total_cost,
                    required,
                )

        return GateDecision(True, "ok", total_cost, required)
