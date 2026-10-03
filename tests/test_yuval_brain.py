"""WAVE 2 item 13 Tier A — the Yuval-brain shadow advisor. Everything
offline (no live API calls): what's tested is the prompt (the card rendered
as text), the strict verdict parse, the armor (timeout/error/garbage →
abstain, lifetime+daily budget → skip), and the fake-client end-to-end
journal write. Shadow only: nothing here places or gates a trade."""

from __future__ import annotations

import time

from waveapp.engine.llmintel import DAILY_SPEND_CAP, LLMIntel
from waveapp.engine.yuval_brain import (
    ACTIONS,
    BARS_IN_PROMPT,
    AdvisorEngine,
    AdvisorMoment,
    AdvisorVerdict,
    MinuteBar,
    NewsFlags,
    build_prompt,
)


def _api_payload(text: str, tokens_in=900, tokens_out=60) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }


def _moment(kind: str = "runner_quiet", n_bars: int = 20) -> AdvisorMoment:
    bars = [
        MinuteBar(
            o=10.0 + i * 0.01,
            h=10.1 + i * 0.01,
            low=9.95 + i * 0.01,
            c=10.05 + i * 0.01,
            v=1000 + i,
        )
        for i in range(n_bars)
    ]
    return AdvisorMoment(
        kind=kind,
        symbol="LITX",
        current_price=10.24,
        bars=bars,
        entry_price=10.00,
        peak_price=10.31,
        qty=300,
        minutes_held=42,
        vwap=10.11,
        day_open=9.90,
        strategy="ORB",
        news=NewsFlags(catalyst=True, direction=1, age_min=35),
        position_uuid="pos-litx-1",
    )


class _FakeTransport:
    """Records the request; returns a canned payload (or raises/sleeps)."""

    def __init__(self, payload=None, exc=None, delay=0.0):
        self.payload = payload
        self.exc = exc
        self.delay = delay
        self.calls: list[tuple[dict, dict]] = []

    def __call__(self, body: dict, headers: dict) -> dict:
        self.calls.append((body, headers))
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.payload


# -- prompt construction ------------------------------------------------------


def test_prompt_is_the_card_in_text():
    prompt = build_prompt(_moment())
    assert "MOMENT: runner_quiet" in prompt
    assert "SYMBOL: LITX (long)" in prompt
    assert "STRATEGY: ORB" in prompt
    assert "entry 10" in prompt and "peak-while-held 10.31" in prompt
    assert "VWAP 10.11" in prompt and "day-open 9.9" in prompt
    assert "held 42 min" in prompt
    assert "OPEN P&L: +72 dollars" in prompt  # (10.24-10.00)*300
    assert "catalyst=yes" in prompt and "direction=+1" in prompt and "35 min old" in prompt
    assert f"LAST {BARS_IN_PROMPT} MINUTE-BARS" in prompt
    assert prompt.count("/") >= BARS_IN_PROMPT * 4  # o/h/l/c per bar line
    assert prompt.rstrip().endswith("JSON only.")


def test_prompt_caps_bars_and_survives_a_bare_candidate():
    prompt = build_prompt(_moment(n_bars=60))
    assert f"LAST {BARS_IN_PROMPT} MINUTE-BARS" in prompt  # never more than ~20
    bare = AdvisorMoment(kind="entry_candidate", symbol="DLLL", current_price=37.42)
    prompt = build_prompt(bare)
    assert "MOMENT: entry_candidate" in prompt
    assert "NEWS: none on file" in prompt
    assert "MINUTE-BARS: none available" in prompt
    assert "entry" not in prompt.split("PRICES: ")[1].splitlines()[0]


def test_system_prompt_carries_the_charter_and_the_three_real_calls():
    from waveapp.engine.yuval_brain import _SYSTEM

    for anchor in ("LITX", "TENB", "DLLL", "91%", "+$239", "CUT WRONGNESS", "BANK CLIMAXES"):
        assert anchor in _SYSTEM
    assert '"action":"bank|hold|cut|skip|take"' in _SYSTEM


# -- parsing ------------------------------------------------------------------


async def test_valid_json_verdict_parses_and_accounts_spend():
    llm = LLMIntel("test-key")
    transport = _FakeTransport(
        _api_payload('{"action":"bank","confidence":0.82,"reason":"run went quiet at the high"}')
    )
    engine = AdvisorEngine(llm, transport=transport)
    verdict = await engine.advise(_moment())
    assert verdict.action == "bank"
    assert verdict.confidence == 0.82
    assert verdict.reason == "run went quiet at the high"
    assert 0 < verdict.spend < 0.01
    assert llm.calls_today == 1  # shared ledger with the news brain
    assert llm.spend_session == verdict.spend
    body, headers = transport.calls[0]
    assert headers["x-api-key"] == "test-key"
    assert body["messages"][0]["content"].startswith("MOMENT:")


async def test_confidence_clamped_and_reason_truncated():
    llm = LLMIntel("test-key")
    long_reason = "x" * 500
    transport = _FakeTransport(
        _api_payload(f'{{"action":"cut","confidence":7,"reason":"{long_reason}"}}')
    )
    verdict = await AdvisorEngine(llm, transport=transport).advise(_moment(kind="bleeder"))
    assert verdict.action == "cut"
    assert verdict.confidence == 1.0
    assert len(verdict.reason) == 140


async def test_malformed_or_offspec_reply_abstains():
    llm = LLMIntel("test-key")
    for text in ("I would probably sell here because", '{"action":"yolo","confidence":0.9}'):
        verdict = await AdvisorEngine(llm, transport=_FakeTransport(_api_payload(text))).advise(
            _moment()
        )
        assert verdict.action == "abstain"
    assert "abstain" not in ACTIONS  # abstain is internal, never a model action


async def test_transport_error_abstains_without_spend():
    llm = LLMIntel("test-key")
    engine = AdvisorEngine(llm, transport=_FakeTransport(exc=OSError("network down")))
    verdict = await engine.advise(_moment())
    assert verdict.action == "abstain"
    assert llm.spend_session == 0.0  # failures never count as spend


async def test_timeout_abstains():
    import threading

    llm = LLMIntel("test-key")
    release = threading.Event()  # the "API" hangs until the test releases it

    def _hanging_transport(body: dict, headers: dict) -> dict:
        release.wait(timeout=5.0)
        return _api_payload('{"action":"hold","confidence":1,"reason":"r"}')

    engine = AdvisorEngine(llm, transport=_hanging_transport, timeout_s=0.05)
    try:
        verdict = await engine.advise(_moment())
    finally:
        release.set()  # let the worker thread exit immediately
    assert verdict.action == "abstain"


async def test_unknown_moment_kind_abstains_without_a_call():
    llm = LLMIntel("test-key")
    transport = _FakeTransport(_api_payload('{"action":"hold","confidence":1,"reason":"r"}'))
    verdict = await AdvisorEngine(llm, transport=transport).advise(_moment(kind="vibes"))
    assert verdict.action == "abstain"
    assert transport.calls == []


# -- budget armor -------------------------------------------------------------


async def test_lifetime_budget_cap_skips_the_call_and_journals_budget(tmp_path):
    from waveapp.persistence.db import Database

    db = Database(tmp_path / "t.db")
    llm = LLMIntel("test-key")
    transport = _FakeTransport(_api_payload('{"action":"hold","confidence":1,"reason":"r"}'))
    engine = AdvisorEngine(
        llm, database=db, budget=25.0, spent_baseline=24.999, transport=transport
    )
    verdict = await engine.advise(_moment())
    assert verdict.action == "budget"
    assert transport.calls == []  # the call never happened
    rows = db.query("SELECT * FROM yuval_brain_verdicts")
    assert len(rows) == 1 and rows[0]["verdict"] == "budget" and rows[0]["spend"] == 0.0
    db.close()


async def test_daily_cap_from_llmintel_also_skips():
    llm = LLMIntel("test-key")
    llm._roll_day()
    llm.spend_today = DAILY_SPEND_CAP
    transport = _FakeTransport(_api_payload('{"action":"hold","confidence":1,"reason":"r"}'))
    verdict = await AdvisorEngine(llm, transport=transport).advise(_moment())
    assert verdict.action == "budget"
    assert transport.calls == []


# -- fake-client end-to-end ---------------------------------------------------


async def test_end_to_end_verdict_lands_in_the_journal(tmp_path):
    from waveapp.persistence.db import Database

    db = Database(tmp_path / "t.db")
    llm = LLMIntel("test-key")
    transport = _FakeTransport(
        _api_payload('{"action":"bank","confidence":0.9,"reason":"sell into strength"}')
    )
    engine = AdvisorEngine(llm, database=db, budget=25.0, spent_baseline=1.0, transport=transport)
    verdict = await engine.advise(_moment())
    assert isinstance(verdict, AdvisorVerdict) and verdict.action == "bank"
    rows = db.query("SELECT * FROM yuval_brain_verdicts")
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "LITX"
    assert row["kind"] == "runner_quiet"
    assert row["verdict"] == "bank"
    assert row["confidence"] == 0.9
    assert row["reason"] == "sell into strength"
    assert row["spend"] > 0
    assert row["position_uuid"] == "pos-litx-1"
    assert row["price"] == 10.24 and row["entry_price"] == 10.0 and row["peak_price"] == 10.31
    assert row["qty"] == 300 and row["minutes_held"] == 42
    assert engine.verdicts_session == 1
    db.close()


async def test_journal_failure_never_escapes(tmp_path):
    class _BoomDB:
        def execute(self, *a, **k):
            raise RuntimeError("disk on fire")

    llm = LLMIntel("test-key")
    transport = _FakeTransport(_api_payload('{"action":"hold","confidence":0.5,"reason":"r"}'))
    engine = AdvisorEngine(llm, database=_BoomDB(), transport=transport)
    verdict = await engine.advise(_moment())  # must not raise
    assert verdict.action == "hold"
