"""Layer 7 — LLM market understanding (blueprint 7.x). Everything offline:
the HTTP call is mocked; what's tested is the armor — triage, caps, cache,
parse-or-fallback — and that verdicts land as journaled features only."""

from __future__ import annotations

import json
from types import SimpleNamespace

from waveapp.engine.llmintel import (
    DAILY_CALL_CAP,
    DAILY_SPEND_CAP,
    EVENT_ID,
    LLMIntel,
    LLMVerdict,
)


def _api_payload(text: str, tokens_in=250, tokens_out=60) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }


def _patch_http(monkeypatch, payload: dict):
    class _Resp:
        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import waveapp.engine.llmintel as mod

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: _Resp(payload))


def test_classify_parses_a_clean_verdict(monkeypatch):
    llm = LLMIntel("test-key")
    _patch_http(
        monkeypatch,
        _api_payload('{"event":"legal","dir":1,"mag":"major","rpu":true,"peers":["PCG","SRE"]}'),
    )
    verdict = llm.classify("EIX", "Edison settles wildfire claims, removing overhang")
    assert verdict is not None
    assert verdict.event == "legal"
    assert verdict.direction == 1
    assert verdict.major is True
    assert verdict.rpu is True  # the EIX signature field
    assert verdict.peers == ["PCG", "SRE"]
    assert llm.calls_today == 1
    assert 0 < llm.spend_today < 0.001  # ~$0.0006 per headline


def test_same_headline_is_one_call_forever_today(monkeypatch):
    llm = LLMIntel("test-key")
    _patch_http(
        monkeypatch,
        _api_payload('{"event":"earnings","dir":1,"mag":"minor","rpu":false,"peers":[]}'),
    )
    first = llm.classify("AAPL", "Apple beats on earnings")
    second = llm.classify("AAPL", "Apple beats on earnings")  # cache hit
    assert first is not None and second is not None
    assert llm.calls_today == 1  # the re-hash cost nothing
    assert llm.cached("Apple beats on earnings") is not None


def test_budget_caps_are_hard(monkeypatch):
    llm = LLMIntel("test-key")
    _patch_http(
        monkeypatch, _api_payload('{"event":"other","dir":0,"mag":"minor","rpu":false,"peers":[]}')
    )
    llm._roll_day()
    llm.spend_today = DAILY_SPEND_CAP  # burned the day's budget
    assert llm.budget_ok() is False
    assert llm.classify("XYZ", "some new headline") is None  # keyword-only now
    llm.spend_today = 0.0
    llm.calls_today = DAILY_CALL_CAP
    llm._capped_logged = False
    assert llm.budget_ok() is False


def test_garbage_response_falls_back_to_keywords(monkeypatch):
    llm = LLMIntel("test-key")
    _patch_http(monkeypatch, _api_payload("I think this headline is interesting because"))
    assert llm.classify("XYZ", "whatever") is None  # unparseable → None
    _patch_http(
        monkeypatch,
        _api_payload(
            '{"event":"nonsense","dir":7,"mag":"huge","rpu":"x","peers":["TOOLONGG",123]}'
        ),
    )
    verdict = llm.classify("XYZ", "another headline")
    assert verdict.event == "other"  # unknown category normalized
    assert verdict.direction == 1  # clamped to [-1, 1]
    assert verdict.peers == []  # invalid peers dropped


def test_http_failure_is_silent_fallback(monkeypatch):
    import waveapp.engine.llmintel as mod

    llm = LLMIntel("test-key")

    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)
    assert llm.classify("XYZ", "headline") is None
    assert llm.spend_today == 0.0  # failures never count as spend


def test_load_llm_off_without_key(monkeypatch):
    from waveapp.engine import llmintel
    from waveapp.security import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    assert llmintel.load_llm() is None


def test_scanner2_journals_the_verdict_feature_only(tmp_path, monkeypatch):
    """on_llm lands as journaled features + event annotations — no score
    boost, no trigger (hard rule 8)."""
    import numpy as np

    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe(
        [SimpleNamespace(symbol=s, name="", tradable=True) for s in ("EIX", "PCG", "SRE")]
    )
    before = np.array(scanner.event_score, copy=True)
    tags = {
        "evt_id": EVENT_ID["legal"],
        "event": "legal",
        "dir": 1,
        "mag": True,
        "rpu": True,
        "peers": ["PCG"],
    }
    scanner.on_llm(["EIX"], tags, 1_760_000_000.0)
    feats = scanner.live_feature_row("EIX")
    assert feats["llm_evt"] == EVENT_ID["legal"]
    assert feats["llm_dir"] == 1
    assert feats["llm_mag"] == 1
    assert feats["llm_rpu"] == 1
    assert scanner.llm_tags["PCG"]["peer_of"] == "EIX"  # sympathy link journaled
    kinds = [e[2] for e in scanner._pending_events]
    assert "llm" in kinds and "llm_peer" in kinds
    assert (scanner.event_score == before).all()  # NEVER a score boost


def test_verdict_defaults_are_neutral():
    verdict = LLMVerdict()
    assert verdict.direction == 0 and verdict.rpu is False and verdict.peers == []


def test_workspace_header_sent_for_identity_linked_keys(monkeypatch):
    """Console change 2026-08-27: identity-linked personal keys must name
    the workspace each request acts in."""
    import waveapp.engine.llmintel as mod

    seen = {}

    class _Resp:
        def read(self):
            return json.dumps(
                _api_payload('{"event":"other","dir":0,"mag":"minor","rpu":false,"peers":[]}')
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _capture(request, timeout=None):
        seen.update({k.lower(): v for k, v in request.header_items()})
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", _capture)
    LLMIntel("k", workspace_id="wrkspc_test123").classify("XYZ", "headline one")
    assert seen.get("anthropic-workspace-id") == "wrkspc_test123"
    seen.clear()
    LLMIntel("k").classify("XYZ", "headline two")  # legacy workspace key: no header
    assert "anthropic-workspace-id" not in seen
