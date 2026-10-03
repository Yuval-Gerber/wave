"""Phase 4: TelegramBridge logic — auth lockdown, confirmation codes,
command handlers. No network, no real bot."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from waveapp.broker.base import PositionInfo
from waveapp.telegram.bridge import (
    DANGEROUS_COMMANDS,
    ConfirmationGate,
    TelegramBridge,
)


def _bridge(adapter=None, sink=None, gate=None) -> TelegramBridge:
    return TelegramBridge(
        allowed_user_id=777,
        adapter_provider=lambda: adapter,
        command_sink=sink,
        gate=gate,
    )


# -- security ----------------------------------------------------------------


def test_rejects_non_positive_user_id():
    with pytest.raises(ValueError):
        TelegramBridge(allowed_user_id=0, adapter_provider=lambda: None)


def test_only_yuvals_id_is_authorized():
    bridge = _bridge()
    assert bridge.is_authorized(777)
    assert not bridge.is_authorized(778)
    assert not bridge.is_authorized(None)


def test_no_live_paper_switching_exists():
    """SPEC.md §4/§13: mode switching must not exist over Telegram."""
    for name in dir(TelegramBridge):
        assert "live" not in name.lower()
        assert "mode" not in name.lower()
    assert "live" not in " ".join(DANGEROUS_COMMANDS)


# -- confirmation gate -------------------------------------------------------


def test_confirmation_roundtrip():
    clock = {"now": 100.0}
    gate = ConfirmationGate(time_fn=lambda: clock["now"])
    code = gate.issue("kill")
    assert len(code) == 6 and code.isdigit()
    assert gate.has_pending
    assert gate.confirm("wrong") is None
    assert gate.confirm(code) == "kill"
    assert not gate.has_pending
    assert gate.confirm(code) is None  # single use


def test_confirmation_expires():
    clock = {"now": 100.0}
    gate = ConfirmationGate(ttl=60, time_fn=lambda: clock["now"])
    code = gate.issue("stop")
    clock["now"] = 161.0
    assert gate.confirm(code) is None
    assert not gate.has_pending


def test_new_code_replaces_old():
    gate = ConfirmationGate()
    first = gate.issue("pause")
    second = gate.issue("kill")
    assert gate.confirm(first) is None or first == second
    # re-issue because the failed attempt above may have consumed nothing
    third = gate.issue("kill")
    assert gate.confirm(third) == "kill"


# -- handlers ----------------------------------------------------------------


def _fake_adapter(positions=(), equity=100_000.0, is_open=True):
    adapter = SimpleNamespace(is_connected=True)
    adapter.get_account = AsyncMock(
        return_value=SimpleNamespace(equity=equity, cash=equity, buying_power=equity * 4)
    )
    adapter.get_clock = AsyncMock(return_value=SimpleNamespace(is_open=is_open))
    adapter.get_positions = AsyncMock(return_value=list(positions))
    return adapter


async def test_status_disconnected():
    bridge = _bridge(adapter=None)
    assert "not connected" in await bridge.handle_status()


async def test_status_connected():
    bridge = _bridge(adapter=_fake_adapter())
    text = await bridge.handle_status()
    assert "$100,000.00" in text and "open" in text


async def test_positions_and_pnl():
    positions = [
        PositionInfo("AAPL", 10, 190.0, 1900.0, 25.0),
        PositionInfo("TSLA", -5, 250.0, -1250.0, -10.0),
    ]
    bridge = _bridge(adapter=_fake_adapter(positions=positions))
    text = await bridge.handle_positions()
    assert "🟢 AAPL +25.00 $ (10 @ $190.00)" in text
    assert "🔴 TSLA (short) -10.00 $" in text
    pnl = await bridge.handle_pnl()
    assert "+15.00 $" in pnl  # 25 - 10


async def test_dangerous_flow_end_to_end():
    sink = SimpleNamespace(
        pause=AsyncMock(return_value="paused"),
        resume=AsyncMock(return_value="resumed"),
        stop=AsyncMock(return_value="stopped"),
        kill=AsyncMock(return_value="killed"),
    )
    gate = ConfirmationGate()
    bridge = _bridge(sink=sink, gate=gate)

    prompt = await bridge.handle_dangerous("kill")
    code = __import__("re").search(r": (\d{6})", prompt).group(1)
    assert "confirm" in prompt

    # wrong code: nothing happens
    assert await bridge.handle_text("000000" if code != "000000" else "111111") is None
    sink.kill.assert_not_awaited()

    # correct code: the sink runs — but the code above already consumed the
    # pending confirmation if it matched; re-issue to be safe
    prompt = await bridge.handle_dangerous("kill")
    code = __import__("re").search(r": (\d{6})", prompt).group(1)
    result = await bridge.handle_text(code)
    assert result == "killed"
    sink.kill.assert_awaited_once()


async def test_random_text_is_ignored():
    bridge = _bridge()
    assert await bridge.handle_text("hello bot") is None


def test_help_covers_every_command():
    bridge = _bridge()
    for name, _menu, _detail in TelegramBridge.COMMANDS:
        assert f"/{name}" in bridge.HELP
    # dangerous commands are marked and the code flow is explained
    assert "6-digit code" in bridge.HELP
    for dangerous in DANGEROUS_COMMANDS:
        assert f"/{dangerous}" in bridge.HELP
