"""Scanner (§9.1, Phase 7): the heuristic ranker.

The scanner is a RANKER, never a price predictor (hard rule 8): it scores
*tradability* — will this symbol produce a clean, tradable move today — and
rules decide direction later (§8.1 strategies, step 7.2).

Score = f(RVOL, gap%, ATR%, catalyst flag, prior-day pattern, sector momentum),
penalized by spread% and float risk. Top-N pass to the TradeGate. EVERY
symbol's features and decision are journaled to `candidates` whether traded or
not — that log IS the future ML training set (§10.3).

Feature acquisition is behind the FeatureProvider protocol so tests inject
synthetic markets and the live app uses Alpaca snapshot batches.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from waveapp.broker.base import OrderSide
from waveapp.engine.tradegate import GateInputs, TradeGate
from waveapp.instruments import leveraged_cap
from waveapp.persistence.db import Database, utc_now

logger = logging.getLogger("wave.scanner")


@dataclass(frozen=True)
class SymbolFeatures:
    symbol: str
    price: float
    prev_close: float
    gap_pct: float  # (open_or_last vs prev close) × 100
    rvol: float  # session-elapsed-adjusted relative volume
    atr_pct: float  # daily ATR as % of price
    spread: float  # $ measured
    day_volume: float
    avg_daily_volume: float
    catalyst: bool = False  # news flag (Polygon wiring later)
    day_open: float = 0.0  # today's official open (trend gate, 2026-08-24)
    shortable: bool = False
    # M0 (2026-09-23): easy-to-borrow from the broker's cached asset metadata
    # (no extra API call). None = the cache had no answer — journaled as-is.
    easy_to_borrow: bool | None = None
    overnight_eligible: bool = False
    leveraged: bool = False  # 2x/3x/inverse product (concentration-capped)


@dataclass(frozen=True)
class Candidate:
    features: SymbolFeatures
    score: float
    strategy_scores: dict[str, float]
    best_strategy: str
    accepted: bool = False
    reject_reason: str = ""


class FeatureProvider(Protocol):
    async def fetch(self) -> Sequence[SymbolFeatures]: ...


# -- scoring (§9.1) ----------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def strategy_scores(f: SymbolFeatures) -> dict[str, float]:
    """Per-strategy tradability scores in [0, ~10]. Direction-free."""
    rvol_term = _clamp(f.rvol / 3.0, 0.0, 2.0)  # 3× normal volume = full marks
    gap_term = _clamp(abs(f.gap_pct) / 3.0, 0.0, 2.0)  # 3% gap = full marks
    atr_term = _clamp(f.atr_pct / 2.0, 0.0, 1.5)  # 2% daily range = full marks
    catalyst_term = 1.0 if f.catalyst else 0.0

    # penalties: spread as a fraction of a 0.4% profit target; illiquidity
    target = max(f.price * 0.004, 0.01)
    spread_penalty = _clamp((f.spread / target) * 2.0, 0.0, 3.0)
    liquidity_penalty = 1.5 if f.avg_daily_volume < 500_000 else 0.0

    orb = 3.0 * rvol_term + 1.0 * gap_term + 1.0 * atr_term + catalyst_term
    gap = 2.0 * gap_term + 2.0 * rvol_term + catalyst_term + 0.5 * atr_term
    vwap = 1.5 * rvol_term + 1.5 * atr_term + 0.5 * gap_term

    penalty = spread_penalty + liquidity_penalty
    return {
        "ORB": max(orb - penalty, 0.0),
        "GAP": max(gap - penalty, 0.0),
        "VWAP": max(vwap - penalty, 0.0),
    }


def rank(features: Sequence[SymbolFeatures]) -> list[Candidate]:
    candidates = []
    for f in features:
        scores = strategy_scores(f)
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        candidates.append(
            Candidate(
                features=f,
                score=scores[best],
                strategy_scores=scores,
                best_strategy=best,
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


# -- orchestration -----------------------------------------------------------


@dataclass
class Scanner:
    provider: FeatureProvider
    gate: TradeGate
    database: Database | None = None
    top_n: int = 10
    max_leveraged_fraction: float = 0.25  # cap on 2x/3x products in the top-N
    expected_move_atr_fraction: float = 0.25  # intraday move ≈ ¼ daily ATR
    slippage_buffer_per_share: float = 0.01  # placeholder until live calibration
    last_results: list[Candidate] = field(default_factory=list)
    # M0 (2026-09-23): optional SSR probe for the candidates journal —
    # returns True/False, or None when unknown (no engine yet). Journaling
    # only; never gates a candidate.
    ssr_check: Callable[[str], bool | None] | None = None
    # one journal row per (symbol, strategy) per day (2026-08-23 fix 1:
    # the per-cycle spam grew the DB 752MB/week and froze the 8GB Air)
    _journal_day: str | None = None
    _journal_seen: dict = field(default_factory=dict)

    async def scan_once(self) -> list[Candidate]:
        features = list(await self.provider.fetch())
        ranked = rank(features)
        results: list[Candidate] = []
        leveraged_admitted = 0
        leveraged_budget = leveraged_cap(self.top_n, self.max_leveraged_fraction)
        considered = 0  # gate-evaluated slots (capped names don't consume one)
        for candidate in ranked:
            f = candidate.features
            if (
                considered < self.top_n
                and candidate.score > 0
                and f.leveraged
                and (leveraged_admitted >= leveraged_budget)
            ):
                # concentration cap (2026-08-18): RVOL rankers love
                # 2x/3x products — admit only a bounded share; the freed slot
                # PROMOTES the next ordinary name
                results.append(
                    replace(
                        candidate,
                        accepted=False,
                        reject_reason="leveraged-product concentration cap",
                    )
                )
                continue
            if considered < self.top_n and candidate.score > 0:
                considered += 1
                daily_atr = f.atr_pct / 100.0 * f.price
                expected_move = daily_atr * self.expected_move_atr_fraction
                target = _clamp(0.4 * daily_atr, f.price * 0.003, f.price * 0.006)
                decision = self.gate.evaluate(
                    GateInputs(
                        symbol=f.symbol,
                        side=OrderSide.BUY,  # direction comes from strategies (7.2)
                        price=f.price,
                        expected_move=expected_move,
                        profit_target=target,
                        spread=f.spread,
                        slippage_buffer=self.slippage_buffer_per_share,
                        overnight_eligible=f.overnight_eligible,
                    )
                )
                candidate = Candidate(
                    features=f,
                    score=candidate.score,
                    strategy_scores=candidate.strategy_scores,
                    best_strategy=candidate.best_strategy,
                    accepted=bool(decision),
                    reject_reason="" if decision else decision.reason,
                )
                if candidate.accepted and f.leveraged:
                    leveraged_admitted += 1
            else:
                candidate = Candidate(
                    features=f,
                    score=candidate.score,
                    strategy_scores=candidate.strategy_scores,
                    best_strategy=candidate.best_strategy,
                    accepted=False,
                    reject_reason="below top-N cutoff",
                )
            results.append(candidate)
        self._journal(results)
        self.last_results = results
        logger.info(
            "scan: %d symbols, %d gate-accepted",
            len(results),
            sum(1 for c in results if c.accepted),
        )
        return results

    def _journal(self, results: Sequence[Candidate]) -> None:
        """§10.3: every symbol-day's features and decision — ONE row per
        (symbol, day, strategy). The first version wrote a row every scan
        CYCLE (~430/symbol/day): 1.58M rows / 752MB in a week, which froze
        the 8GB Air (2026-08-22). A rejected symbol that later clears
        the gate has its row UPGRADED to accepted in place, so the dataset
        keeps the label it deserves. Restart-proof: the per-day seen-map is
        reseeded from the DB on the first scan of each day."""
        if self.database is None:
            return
        now = datetime.now(UTC)
        session_date = now.date().isoformat()
        ts = utc_now()
        if session_date != self._journal_day:
            self._journal_day = session_date
            self._journal_seen = {}
            try:
                for row in self.database.query(
                    "SELECT symbol, strategy, decision FROM candidates WHERE session_date = ?",
                    (session_date,),
                ):
                    key = (str(row["symbol"]), str(row["strategy"]))
                    if self._journal_seen.get(key) != "accepted":
                        self._journal_seen[key] = str(row["decision"])
            except Exception:
                logger.exception("journal seen-map reseed failed")
        try:
            for c in results:
                key = (c.features.symbol, c.best_strategy)
                decision = "accepted" if c.accepted else "rejected"
                previous = self._journal_seen.get(key)
                if previous is not None and not (previous == "rejected" and decision == "accepted"):
                    continue  # already journaled today — no per-cycle spam
                # M0 (2026-09-23): the journal goes side-aware. The scanner
                # stays a direction-free ranker (hard rule 8) — `side` is the
                # side the RULES would fire on this tape (day trend sign),
                # journaled for the training set only, deciding nothing here.
                f = c.features
                day_pct = (f.price / f.prev_close - 1.0) * 100.0 if f.prev_close else 0.0
                ssr = None
                if self.ssr_check is not None:
                    try:
                        probed = self.ssr_check(f.symbol)
                        ssr = None if probed is None else bool(probed)
                    except Exception:
                        ssr = None
                features = json.dumps(
                    {
                        "price": f.price,
                        "gap_pct": f.gap_pct,
                        "rvol": f.rvol,
                        "atr_pct": f.atr_pct,
                        "spread": f.spread,
                        "day_volume": f.day_volume,
                        "avg_daily_volume": f.avg_daily_volume,
                        "catalyst": f.catalyst,
                        "scores": c.strategy_scores,
                        "side": "short" if day_pct < 0 else "long",
                        "day_pct": round(day_pct, 4),
                        "shortable": bool(f.shortable),
                        "etb": f.easy_to_borrow,  # None = cache had no answer
                        "ssr_active": ssr,  # None = unknown at scan time
                    }
                )
                if previous is None:
                    self.database.execute(
                        "INSERT OR IGNORE INTO candidates"
                        " (symbol, session_date, ts, strategy, features, decision,"
                        " reject_reason) VALUES (?,?,?,?,?,?,?)",
                        (
                            c.features.symbol,
                            session_date,
                            ts,
                            c.best_strategy,
                            features,
                            decision,
                            c.reject_reason or None,
                        ),
                    )
                else:  # rejected → accepted upgrade, in place
                    self.database.execute(
                        "UPDATE candidates SET decision='accepted', reject_reason=NULL,"
                        " features=?, ts=? WHERE symbol=? AND session_date=? AND strategy=?",
                        (features, ts, c.features.symbol, session_date, c.best_strategy),
                    )
                self._journal_seen[key] = decision
        except Exception:
            logger.exception("candidate journaling failed")
