"""The Yuval-brain — WAVE 2 spec item 13, Tier A (approved 2026-09-16).

A SHADOW-ONLY AI advisor that looks at a decision moment the way Yuval
does — chart, news, tape, the numbers on the position card — and journals
a judgment. Its verdicts are WRITTEN AND GRADED (scripts/yuval_brain_grade.py),
never acted on. No order, no gate, no size change reads this module.

Plumbing: the SAME Anthropic account the layer-7 news brain uses
(waveapp/engine/llmintel.py) — same Haiku model, same key, and the SPEND IS
SHARED: every advisor call is added to the LLMIntel counters under its own
lock, so the monitor's existing throttled persistence rolls it into
config.llm_spent_total and the $25 lifetime budget without new wiring.

Money armor (inherits llmintel's, plus the lifetime hard cap):
- lifetime: if llm_spent_total (baseline + this session) would exceed
  llm_budget, the call is SKIPPED and the verdict journaled as "budget";
- daily: llmintel's DAILY_CALL_CAP / DAILY_SPEND_CAP also gate the call;
- strict timeout / any error / unparseable reply → verdict "abstain",
  never an exception outward.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from waveapp.engine.llmintel import API_URL, MODEL, PRICE_IN, PRICE_OUT, LLMIntel

logger = logging.getLogger("wave.yuval_brain")

TIMEOUT_SECONDS = 10.0
MAX_TOKENS = 160
BARS_IN_PROMPT = 20  # last ~20 minute-bars — what a glance at the card sees
EST_CALL_COST = 0.005  # conservative headroom the lifetime check reserves

MOMENT_KINDS = ("entry_candidate", "runner_quiet", "bleeder", "giveback")
ACTIONS = ("bank", "hold", "cut", "skip", "take")
REASON_MAX = 140

# The trading charter's judgment rules distilled, plus
# three few-shot examples from the REAL calls: the LITX hand-sell at 91%
# of peak, the TENB +$239 ride, the DLLL top-buy mistake.
_SYSTEM = (
    "You are Yuval, a hungry day trader judging ONE decision moment on a US"
    " stock from chart + news + tape. Your rules, in order:"
    " (1) HUNGER FOR MONEY — every look ends in the move that makes the most"
    " dollars, never in comfort."
    " (2) CUT WRONGNESS FAST — a trade that goes wrong right away is cut, not"
    " debated; a bleeder never gets to bleed quietly."
    " (3) BANK CLIMAXES — when a hard run goes QUIET (volume dies, highs stop"
    " extending), sell into the remaining strength; do not hand back a peak."
    " (4) RESPECT RUNNERS — while volume holds and higher lows keep printing,"
    " a proven runner keeps its seat; never scalp real fuel."
    " (5) NEVER BUY A FINISHED MOVE — a signal firing into the last 1% of a"
    " run that already happened is a skip, whatever the indicator says."
    " Real calls that define you:"
    " LITX: it ran hard, the run went quiet near the high — you sold by hand"
    " at 91% of the peak. Verdict was bank, and it was right."
    " TENB: a runner with volume still alive — you let it ride and banked"
    " +$239 only when the push actually died. Verdict until then was hold."
    " DLLL: +8.3% off the open already, breakout signal 1% below the day"
    " high — buying that top lost $450 on the day's best stock. Verdict"
    " should have been skip."
    " Verdict meanings: bank = sell the winner now into strength; hold = keep"
    " the position; cut = exit the loser now; take = enter this candidate;"
    " skip = do not enter."
    ' Reply with ONLY compact JSON, nothing else: {"action":"bank|hold|cut|'
    'skip|take","confidence":0.0-1.0,"reason":"<=140 chars, plain words,'
    ' dollars over jargon"}.'
)


@dataclass
class MinuteBar:
    """One minute-bar as the card shows it (o/h/l/c/v)."""

    o: float
    h: float
    low: float
    c: float
    v: float


@dataclass
class NewsFlags:
    """What the news column of the card says about the symbol right now."""

    catalyst: bool = False
    direction: int = 0  # -1 / 0 / +1, from the news brain's verdict
    age_min: float | None = None  # minutes since the headline; None = unknown


@dataclass
class AdvisorMoment:
    """Everything a human eye sees on the position card at a decision moment."""

    kind: str  # one of MOMENT_KINDS
    symbol: str
    current_price: float
    bars: list[MinuteBar] = field(default_factory=list)  # oldest → newest
    entry_price: float | None = None  # None for entry_candidate
    peak_price: float | None = None  # best price while held
    qty: float = 0.0
    minutes_held: float = 0.0
    vwap: float | None = None
    day_open: float | None = None
    side: str = "long"
    strategy: str = ""
    news: NewsFlags | None = None
    rvol: float | None = None  # day relative volume (the #1 discriminator)
    breadth_below_open: float | None = None  # menu fraction fading (fade-day context)
    minutes_since_open: float | None = None
    position_uuid: str = ""  # lets the grader join positions exactly


@dataclass
class AdvisorVerdict:
    action: str = "abstain"  # ACTIONS | "abstain" | "budget"
    confidence: float = 0.0
    reason: str = ""
    spend: float = 0.0  # dollars this call cost (0 when no call was made)


def build_prompt(moment: AdvisorMoment) -> str:
    """The compact user message — the card, rendered as text."""
    lines = [f"MOMENT: {moment.kind}", f"SYMBOL: {moment.symbol} ({moment.side})"]
    if moment.strategy:
        lines.append(f"STRATEGY: {moment.strategy}")
    px = [f"now {moment.current_price:.4g}"]
    if moment.entry_price is not None:
        px.append(f"entry {moment.entry_price:.4g}")
    if moment.peak_price is not None:
        px.append(f"peak-while-held {moment.peak_price:.4g}")
    if moment.day_open is not None:
        px.append(f"day-open {moment.day_open:.4g}")
    if moment.vwap is not None:
        px.append(f"VWAP {moment.vwap:.4g}")
    lines.append("PRICES: " + ", ".join(px))
    ctx = []
    if moment.rvol is not None:
        ctx.append(f"rvol {moment.rvol:.1f}x")
    if moment.breadth_below_open is not None:
        ctx.append(f"market breadth: {moment.breadth_below_open:.0%} of menu below open")
    if moment.minutes_since_open is not None:
        ctx.append(f"{moment.minutes_since_open:.0f} min since open")
    if ctx:
        lines.append("CONTEXT: " + ", ".join(ctx))
    if moment.qty:
        lines.append(f"POSITION: {moment.qty:g} sh, held {moment.minutes_held:.0f} min")
        if moment.entry_price:
            open_usd = (moment.current_price - moment.entry_price) * moment.qty
            if moment.side != "long":
                open_usd = -open_usd
            lines.append(f"OPEN P&L: {open_usd:+.0f} dollars")
    news = moment.news
    if news is not None:
        age = "age unknown" if news.age_min is None else f"{news.age_min:.0f} min old"
        lines.append(
            f"NEWS: catalyst={'yes' if news.catalyst else 'no'},"
            f" direction={news.direction:+d}, {age}"
        )
    else:
        lines.append("NEWS: none on file")
    bars = moment.bars[-BARS_IN_PROMPT:]
    if bars:
        lines.append(f"LAST {len(bars)} MINUTE-BARS (oldest first, o/h/l/c/v):")
        lines.extend(f"{b.o:.4g}/{b.h:.4g}/{b.low:.4g}/{b.c:.4g}/{b.v:.0f}" for b in bars)
    else:
        lines.append("MINUTE-BARS: none available")
    lessons = _recent_lessons()
    if lessons:
        lines.append("YOUR RECENT GRADED CALLS (learn from these):")
        lines.extend(f"- {les}" for les in lessons)
    lines.append(
        "RULE REFINEMENT (2026-09-16 graded evidence): a finished move = "
        "extension WITH DYING volume. Extension with BUILDING volume is a "
        "runner — judge the fuel, not just the altitude."
    )
    lines.append("Your verdict, JSON only.")
    return "\n".join(lines)


_LESSONS_PATH = None  # set lazily; module stays import-cheap


def _recent_lessons(limit: int = 5) -> list[str]:
    """The tracker's graded verdicts, freshest first — same-day feedback."""
    global _LESSONS_PATH
    try:
        import json as _json
        from pathlib import Path as _P

        if _LESSONS_PATH is None:
            _LESSONS_PATH = (
                _P(__file__).resolve().parent.parent.parent / "research" / "veto_lessons.json"
            )
        if not _LESSONS_PATH.exists():
            return []
        rows = _json.loads(_LESSONS_PATH.read_text())
        return [str(r) for r in rows[-limit:]][::-1]
    except Exception:
        return []


# transport signature: (request_body: dict, headers: dict) -> raw API payload dict
Transport = Callable[[dict, dict], dict]


class AdvisorEngine:
    """Shadow advisor. advise() never raises and never touches trading.

    `llm` is the SAME LLMIntel the news brain uses — key and spend ledger are
    shared. `spent_baseline` / `budget` mirror the monitor's lifetime
    accounting (config.llm_spent_total at load / config.llm_budget).
    `transport` is injectable for tests; default is the real HTTPS call.
    """

    def __init__(
        self,
        llm: LLMIntel,
        database=None,
        *,
        budget: float = 25.0,
        spent_baseline: float = 0.0,
        transport: Transport | None = None,
        timeout_s: float = TIMEOUT_SECONDS,
    ) -> None:
        self._llm = llm
        self._db = database
        self._budget = float(budget)
        self._baseline = float(spent_baseline)
        self._transport = transport or self._http_transport
        self._timeout = timeout_s
        self.verdicts_session = 0

    # -- budget ---------------------------------------------------------------

    def budget_ok(self) -> bool:
        """Lifetime hard cap first (WAVE 2 item 13), then llmintel's daily caps."""
        lifetime = self._baseline + self._llm.spend_session
        if lifetime + EST_CALL_COST > self._budget:
            return False
        return self._llm.budget_ok()

    # -- the advisor ----------------------------------------------------------

    async def advise(self, moment: AdvisorMoment) -> AdvisorVerdict:
        """One judgment. Journals every verdict; errors become 'abstain'."""
        try:
            if moment.kind not in MOMENT_KINDS:
                verdict = AdvisorVerdict(action="abstain", reason=f"unknown kind {moment.kind}")
            elif not self.budget_ok():
                verdict = AdvisorVerdict(action="budget", reason="budget cap — call skipped")
            else:
                try:
                    verdict = await asyncio.wait_for(
                        asyncio.to_thread(self._call, moment), timeout=self._timeout
                    )
                except Exception as exc:
                    logger.info(
                        "Yuval-brain call failed on %s/%s (%s) — abstain",
                        moment.symbol,
                        moment.kind,
                        type(exc).__name__,
                    )
                    verdict = AdvisorVerdict(action="abstain", reason=type(exc).__name__[:60])
        except Exception:  # belt and braces: NOTHING escapes to the caller
            logger.exception("Yuval-brain advise crashed — abstain")
            verdict = AdvisorVerdict(action="abstain", reason="internal error")
        self.verdicts_session += 1
        self._journal(moment, verdict)
        return verdict

    # -- blocking internals (worker thread) -----------------------------------

    def _call(self, moment: AdvisorMoment) -> AdvisorVerdict:
        body = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": build_prompt(moment)}],
        }
        # shared key + workspace: same account, same headers as the news brain
        headers = {
            "content-type": "application/json",
            "x-api-key": self._llm._key,
            "anthropic-version": "2023-06-01",
        }
        if self._llm._workspace:
            headers["anthropic-workspace-id"] = self._llm._workspace
        payload = self._transport(body, headers)
        usage = payload.get("usage") or {}
        cost = (
            float(usage.get("input_tokens", 0)) * PRICE_IN
            + float(usage.get("output_tokens", 0)) * PRICE_OUT
        )
        # spend lands in the news brain's ledger — the monitor's throttled
        # persistence rolls it into config.llm_spent_total automatically
        with self._llm._lock:
            self._llm._roll_day()
            self._llm.calls_today += 1
            self._llm.spend_today += cost
            self._llm.spend_session += cost
        verdict = self._parse(payload)
        if verdict is None:
            return AdvisorVerdict(action="abstain", reason="unparseable reply", spend=cost)
        verdict.spend = cost
        return verdict

    def _http_transport(self, body: dict, headers: dict) -> dict:
        request = urllib.request.Request(  # noqa: S310 — fixed https endpoint
            API_URL, data=json.dumps(body).encode(), headers=headers
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            return json.loads(response.read())

    @staticmethod
    def _parse(payload: dict) -> AdvisorVerdict | None:
        """Strict verdict parse; None on anything off-spec."""
        try:
            blocks = payload.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return None
            data = json.loads(text[start : end + 1])
            action = str(data.get("action", "")).strip().lower()
            if action not in ACTIONS:
                return None
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
            reason = str(data.get("reason", ""))[:REASON_MAX]
            return AdvisorVerdict(action=action, confidence=confidence, reason=reason)
        except Exception:
            logger.debug("Yuval-brain verdict unparseable", exc_info=True)
            return None

    # -- journal --------------------------------------------------------------

    def _journal(self, moment: AdvisorMoment, verdict: AdvisorVerdict) -> None:
        """Every verdict becomes a row — the grader's raw material. Never raises."""
        if self._db is None:
            return
        try:
            from waveapp.persistence.db import utc_now

            self._db.execute(
                "INSERT INTO yuval_brain_verdicts (ts, symbol, kind, verdict, confidence,"
                " reason, spend, position_uuid, strategy, price, entry_price, peak_price,"
                " qty, minutes_held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    utc_now(),
                    moment.symbol,
                    moment.kind,
                    verdict.action,
                    round(float(verdict.confidence), 4),
                    verdict.reason,
                    round(float(verdict.spend), 6),
                    moment.position_uuid,
                    moment.strategy,
                    float(moment.current_price),
                    moment.entry_price,
                    moment.peak_price,
                    float(moment.qty),
                    float(moment.minutes_held),
                ),
            )
        except Exception:
            logger.exception("Yuval-brain journal write failed (verdict lost, trading unaffected)")
