"""Phase 8.11: the §4 Touch ID hard gates — kill switch, weekly re-arm,
key rotation. Tests patch the gate to pass instantly; the gate itself is
exercised via its no-Touch-ID fallback path."""

from waveapp.engine.risk import HaltState, RiskEngine


def _pass_gate(monkeypatch):
    """Make require_gate call through synchronously and record the reasons."""
    reasons: list[str] = []

    def fake_gate(reason, on_success):
        reasons.append(reason)
        on_success()

    from waveapp.security import gate

    monkeypatch.setattr(gate, "require_gate", fake_gate)
    return reasons


def test_gate_falls_through_without_touch_id(monkeypatch):
    from waveapp.security import auth, gate

    monkeypatch.setattr(auth, "touch_id_available", lambda: False)
    called = []
    gate.require_gate("anything", lambda: called.append(True))
    assert called == [True]


def test_gate_success_reaches_the_qt_thread(qtbot, monkeypatch):
    """Regression (kill re-arm): the auth worker runs off-thread; its
    success must actually arrive back on the Qt thread. QTimer.singleShot
    from a worker thread silently never fires — the queued signal must."""
    import threading

    from waveapp.security import auth, gate

    monkeypatch.setattr(auth, "touch_id_available", lambda: True)
    monkeypatch.setattr(auth, "authenticate_touch_id", lambda reason: True)
    ran_on = []
    gate.require_gate("test gate", lambda: ran_on.append(threading.current_thread()))
    qtbot.waitUntil(lambda: len(ran_on) == 1, timeout=3000)
    assert ran_on[0] is threading.main_thread()


def test_kill_switch_gates_then_confirms_then_fires(qtbot, monkeypatch):
    from waveapp.ui.system_page import SystemPage

    reasons = _pass_gate(monkeypatch)
    page = SystemPage()
    qtbot.addWidget(page)
    page.setFixedSize(900, 600)
    page.show()
    fired = []
    page.kill_confirmed.connect(lambda: fired.append(True))

    page.kill_button.click()
    assert reasons == ["engage the Wave kill switch"]
    assert fired == []  # gate passed but the confirm card still stands between
    assert page._confirm is not None
    page._confirm.confirm_button.click()
    assert fired == [True]


def test_kill_confirm_cancel_fires_nothing(qtbot, monkeypatch):
    from waveapp.ui.system_page import SystemPage

    _pass_gate(monkeypatch)
    page = SystemPage()
    qtbot.addWidget(page)
    page.setFixedSize(900, 600)
    page.show()
    fired = []
    page.kill_confirmed.connect(lambda: fired.append(True))
    page.kill_button.click()
    page._confirm.cancel_button.click()
    assert fired == []


def test_rearm_appears_only_on_weekly_halt(qtbot, monkeypatch):
    from waveapp.ui.system_page import SystemPage

    reasons = _pass_gate(monkeypatch)
    page = SystemPage()
    qtbot.addWidget(page)
    page.show()
    assert page.rearm_button.isHidden()
    page.update_system({"risk": {"halt": "weekly_loss"}})
    assert not page.rearm_button.isHidden()
    fired = []
    page.rearm_requested.connect(lambda: fired.append(True))
    page.rearm_button.click()
    assert fired == [True]
    assert "weekly-loss halt" in reasons[0]
    # any other halt state hides it again
    page.update_system({"risk": {"halt": "daily_loss"}})
    assert page.rearm_button.isHidden()


def test_monitor_rearm_clears_only_weekly_halt():
    from types import SimpleNamespace

    from waveapp.engine.connection_monitor import ConnectionMonitor

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    risk = RiskEngine()
    monitor.engine = SimpleNamespace(risk=risk)

    risk._halt = HaltState.WEEKLY_LOSS
    monitor.re_arm_weekly()
    assert risk.halt_state is HaltState.NONE

    risk._halt = HaltState.KILLED  # a kill halt must never be re-armed this way
    monitor.re_arm_weekly()
    assert risk.halt_state is HaltState.KILLED


def test_risk_rearm_kill_clears_only_kill_halt():
    risk = RiskEngine()
    risk._halt = HaltState.KILLED
    risk.re_arm_kill()
    assert risk.halt_state is HaltState.NONE

    risk._halt = HaltState.WEEKLY_LOSS  # loss halts keep their own re-arm rules
    risk.re_arm_kill()
    assert risk.halt_state is HaltState.WEEKLY_LOSS


def test_monitor_rearm_kill_and_start_goes_through_normal_start():
    import asyncio
    from types import SimpleNamespace

    from waveapp.engine.connection_monitor import ConnectionMonitor

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    risk = RiskEngine()
    risk._halt = HaltState.KILLED
    calls = []

    async def fake_start():
        calls.append("start")
        return "engine running"

    monitor.engine = SimpleNamespace(
        risk=risk, start=fake_start, state=SimpleNamespace(value="killed")
    )
    result = asyncio.run(monitor.re_arm_kill_and_start())
    assert risk.halt_state is HaltState.NONE
    assert calls == ["start"]  # reconcile-first start path, never a bypass
    assert result == "engine running"


def test_top_bar_killed_state_offers_gated_rearm(qtbot):
    from waveapp.ui.top_bar import TopBar

    bar = TopBar()
    qtbot.addWidget(bar)
    bar.set_engine_state("killed")
    assert bar.start_button.isEnabled()
    assert bar.start_button.action_hint == "Re-arm"
    assert not bar.pause_stop.isEnabled()


def test_key_rotation_is_gated(qtbot, tmp_path, monkeypatch, fake_keychain):
    from waveapp.config import AppConfig
    from waveapp.ui.settings_page import SettingsPage

    config_path = tmp_path / "config.toml"
    AppConfig().save(config_path)
    reasons = _pass_gate(monkeypatch)
    page = SettingsPage(config_path=config_path)
    qtbot.addWidget(page)

    key_edit, secret_edit = page.key_fields["paper"]
    page._save_keys_requested("paper")  # empty fields: refused BEFORE the gate
    assert reasons == []

    key_edit.setText("fake-key-id-for-test")
    secret_edit.setText("fake-secret-for-test")
    page._save_keys_requested("paper")
    assert reasons == ["save paper Alpaca keys"]
    assert _vault(fake_keychain)["alpaca_paper_key_id"] == "fake-key-id-for-test"
    assert key_edit.text() == ""  # wiped after the gated save


def test_position_size_notional_cap():
    """2026-08-18: risk-only sizing turned a hair-thin stop into
    six-figure notional. The cap holds regardless of stop distance."""
    from waveapp.broker.base import OrderSide
    from waveapp.engine.risk import RiskEngine, RiskLimits

    risk = RiskEngine(limits=RiskLimits())
    # $0.05 stop distance on a $8 stock, $100k equity: risk math says 20,000
    # shares ($160k) — the cap holds it to 25% of equity ($25k → 3,125 sh)
    qty = risk.position_size(100_000, entry_price=8.0, stop_price=7.95, side=OrderSide.BUY)
    assert qty == int(100_000 * 0.25 / 8.0)
    # normal case unaffected: $1 distance on a $100 stock → 1000 by risk,
    # 250 by notional? 25%/100 = 250 → still capped... use wider equity
    qty = risk.position_size(100_000, entry_price=20.0, stop_price=19.0, side=OrderSide.BUY)
    assert qty == 1000  # risk-based (notional cap would allow 1250)
    # tighter cap via limits swap
    tight = RiskEngine(limits=RiskLimits(max_notional_pct=10.0))
    qty = tight.position_size(100_000, entry_price=8.0, stop_price=7.95, side=OrderSide.BUY)
    assert qty == int(100_000 * 0.10 / 8.0)


def test_dynamic_expected_move_seam():
    """6.1: baseline rules on calm tape; live realized vol lifts the input
    (never lowers it); thin history → baseline unchanged."""
    from waveapp.engine.tradegate import dynamic_expected_move

    calm = [100.0 + 0.01 * i for i in range(15)]
    assert dynamic_expected_move(0.50, calm) == 0.50  # calm: baseline rules
    # a live vertical: ~1%/min swings → dynamic estimate must exceed baseline
    wild = [100.0 * (1.015**i) if i % 2 else 100.0 * (1.015**i) * 0.99 for i in range(15)]
    assert dynamic_expected_move(0.50, wild) > 0.50
    assert dynamic_expected_move(0.50, [100.0, 100.1]) == 0.50  # too thin


def _vault(fake_keychain: dict) -> dict:
    """Read the single-item VAULT (2026-09-02) the way the app stores it."""
    import json

    return json.loads(fake_keychain.get(("Wave", "vault"), "{}"))
