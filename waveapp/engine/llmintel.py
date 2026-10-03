"""LLM market understanding — master blueprint layer 7 (funded the
key 2026-09-01: "$25, it will never expire, make sure it uses smartly").

The triage cascade (7.1): the FREE keyword classifier bins everything
first; the LLM is consulted ONLY for a headline that is (a) a true first
print (novelty 1.0 — repeats are answered from the 24h cache or skipped),
and (b) about a symbol Wave actually cares about right now (menu / day
list). One headline = ONE call even when Benzinga tags ten tickers.

Money armor (7.7), all hard-coded on purpose:
- DAILY_CALL_CAP calls/day and DAILY_SPEND_CAP dollars/day (measured from
  the API's own usage numbers) — at the cap the layer goes keyword-only
  for the rest of the day and says so once in the log;
- strict timeout → keyword fallback; any error → keyword fallback;
- the key lives ONLY in Keychain ('anthropic_api_key'); no key → layer off.
At ~$0.0006/headline the $0.25/day worst case is ~$7.5/month; the $25
covers 3+ months at full burn, far longer at real volume.

Consumption (7.6, hard rule 8): the verdict is a JOURNALED FEATURE and a
log line — never a standalone trade trigger. The two killer fields:
`rpu` (resolves_prior_uncertainty — the EIX signature: resolution news
produces moves that HOLD) and `peers` (who else is affected, before their
tape shows it). v2 learns their weights via the weekly feature protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger("wave.llm")

KEYCHAIN_LLM_KEY = "anthropic_api_key"
KEYCHAIN_LLM_WORKSPACE = "anthropic_workspace_id"  # identity-linked keys (2026-08-27+)
MODEL = "claude-haiku-4-5-20251001"
API_URL = "https://api.anthropic.com/v1/messages"
TIMEOUT_SECONDS = 8.0
DAILY_CALL_CAP = 400
DAILY_SPEND_CAP = 0.25  # dollars — hard stop, keyword-only past it
PRICE_IN = 1.00 / 1_000_000  # Haiku 4.5 $/input token
PRICE_OUT = 5.00 / 1_000_000  # $/output token
CACHE_LIMIT = 800  # 24h of headlines, bounded

EVENT_ID = {
    "earnings": 1,
    "guidance": 2,
    "fda": 3,
    "ma": 4,
    "offering": 5,
    "contract": 6,
    "analyst": 7,
    "legal": 8,
    "disaster": 9,
    "macro": 10,
    "other": 0,
}

_SYSTEM = (
    "You classify one US-stock news headline for a trading system. Reply with"
    ' ONLY compact JSON, nothing else: {"event":"earnings|guidance|fda|ma|'
    'offering|contract|analyst|legal|disaster|macro|other","dir":-1 or 0 or 1,'
    '"mag":"minor|major","rpu":true|false,"peers":["TICK"]}. dir = likely'
    " direction for the SUBJECT stock. rpu = true only if this RESOLVES a"
    " previously hanging uncertainty (settlement reached, ruling issued,"
    " approval decided, deal closed, guidance finally given). peers = other"
    " US-listed tickers materially affected (max 5, [] if none)."
)


@dataclass
class LLMVerdict:
    event: str = "other"
    direction: int = 0
    major: bool = False
    rpu: bool = False  # resolves_prior_uncertainty — the EIX signature
    peers: list[str] = field(default_factory=list)


class LLMIntel:
    """One instance per app run. classify() is BLOCKING (worker thread only
    — the monitor wraps it in asyncio.to_thread); everything else is cheap."""

    def __init__(self, api_key: str, workspace_id: str | None = None) -> None:
        self._key = api_key
        self._workspace = workspace_id
        self._lock = threading.Lock()
        self._day: str | None = None
        self.calls_today = 0
        self.spend_today = 0.0
        self.spend_session = 0.0  # since app start — feeds the balance line
        self._capped_logged = False
        self._cache: dict[str, LLMVerdict] = {}  # headline-hash → verdict

    # -- budget --------------------------------------------------------------

    def _roll_day(self) -> None:
        today = datetime.now(UTC).date().isoformat()
        if self._day != today:
            self._day = today
            self.calls_today = 0
            self.spend_today = 0.0
            self._capped_logged = False
            self._cache.clear()

    def budget_ok(self) -> bool:
        with self._lock:
            self._roll_day()
            if self.calls_today >= DAILY_CALL_CAP or self.spend_today >= DAILY_SPEND_CAP:
                if not self._capped_logged:
                    self._capped_logged = True
                    logger.warning(
                        "LLM daily budget reached (%d calls, $%.3f) — keyword-only until tomorrow",
                        self.calls_today,
                        self.spend_today,
                    )
                return False
            return True

    @staticmethod
    def _hash(headline: str) -> str:
        return hashlib.sha256((headline or "").strip().lower().encode()).hexdigest()[:24]

    def cached(self, headline: str) -> LLMVerdict | None:
        return self._cache.get(self._hash(headline))

    # -- the call ------------------------------------------------------------

    def classify(self, symbol: str, headline: str) -> LLMVerdict | None:
        """BLOCKING HTTP → verdict. None on any doubt (caller keeps the
        keyword tags — the fallback is always armed)."""
        key = self._hash(headline)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        if not self.budget_ok():
            return None
        body = json.dumps(
            {
                "model": MODEL,
                "max_tokens": 150,
                "system": _SYSTEM,
                "messages": [
                    {"role": "user", "content": f"SYMBOL: {symbol}\nHEADLINE: {headline[:300]}"}
                ],
            }
        ).encode()
        headers = {
            "content-type": "application/json",
            "x-api-key": self._key,
            "anthropic-version": "2023-06-01",
        }
        if self._workspace:
            # identity-linked personal keys (console change 2026-08-27) must
            # name the workspace each request acts in
            headers["anthropic-workspace-id"] = self._workspace
        request = urllib.request.Request(  # noqa: S310 — fixed https endpoint
            API_URL,
            data=body,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                payload = json.loads(response.read())
        except Exception as exc:
            logger.info("LLM call failed (%s) — keyword tags stand", type(exc).__name__)
            return None
        usage = payload.get("usage") or {}
        cost = (
            float(usage.get("input_tokens", 0)) * PRICE_IN
            + float(usage.get("output_tokens", 0)) * PRICE_OUT
        )
        with self._lock:
            self._roll_day()
            self.calls_today += 1
            self.spend_today += cost
            self.spend_session += cost
        verdict = self._parse(payload)
        if verdict is not None:
            with self._lock:
                if len(self._cache) >= CACHE_LIMIT:
                    for stale in list(self._cache)[: CACHE_LIMIT // 4]:
                        del self._cache[stale]
                self._cache[key] = verdict
        return verdict

    @staticmethod
    def _parse(payload: dict) -> LLMVerdict | None:
        try:
            blocks = payload.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return None
            data = json.loads(text[start : end + 1])
            event = str(data.get("event", "other")).lower()
            if event not in EVENT_ID:
                event = "other"
            direction = int(data.get("dir", 0))
            direction = max(-1, min(1, direction))
            peers = [
                str(p).upper()
                for p in (data.get("peers") or [])[:5]
                if isinstance(p, str) and 0 < len(p) <= 5 and str(p).isalpha()
            ]
            return LLMVerdict(
                event=event,
                direction=direction,
                major=str(data.get("mag", "minor")).lower() == "major",
                rpu=bool(data.get("rpu", False)),
                peers=peers,
            )
        except Exception:
            logger.debug("LLM verdict unparseable — keyword tags stand", exc_info=True)
            return None


def load_llm() -> LLMIntel | None:
    """Key in Keychain → armed; absent → the layer simply doesn't exist."""
    try:
        from waveapp.security import secrets

        key = secrets.get_secret(KEYCHAIN_LLM_KEY)
        if not key:
            logger.info("LLM layer off — no '%s' Keychain entry", KEYCHAIN_LLM_KEY)
            return None
        workspace = secrets.get_secret(KEYCHAIN_LLM_WORKSPACE)
        return LLMIntel(key, workspace_id=workspace)
    except Exception:
        logger.exception("LLM layer failed to load — keyword tags stand")
        return None
