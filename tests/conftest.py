"""Shared test fixtures. Qt runs offscreen; Keychain is always mocked so tests
never touch the real macOS Keychain (SPEC.md: tests mock around security,
never through it)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import keyring  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def fake_keychain(monkeypatch):
    """In-memory stand-in for macOS Keychain."""
    store: dict[tuple[str, str], str] = {}

    def get_password(service: str, entry: str) -> str | None:
        return store.get((service, entry))

    def set_password(service: str, entry: str, value: str) -> None:
        store[(service, entry)] = value

    def delete_password(service: str, entry: str) -> None:
        if (service, entry) not in store:
            raise keyring.errors.PasswordDeleteError(entry)
        del store[(service, entry)]

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", set_password)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    # the vault caches in-process (2026-09-02) — every test starts cold and
    # leaves nothing cached for the next one
    from waveapp.security import secrets as _secrets

    _secrets.reset_cache()
    yield store
    _secrets.reset_cache()


# -- clock pin (audit A1-1/A1-12): immediate exits are now clock-dependent —
# MARKET inside RTH, marketable LIMIT + extended_hours outside. The suite must
# behave identically at 3 AM and at noon, so every test defaults to RTH (the
# assumption every historical MARKET-order assert was written under); the
# outside-RTH regression tests monkeypatch is_rth back to False explicitly.
@pytest.fixture(autouse=True)
def _pin_rth(monkeypatch):
    from waveapp.engine.session import SessionScheduler

    monkeypatch.setattr(SessionScheduler, "is_rth", staticmethod(lambda now_utc=None: True))
    yield


# -- Qt timing tolerance (2026-09-03: four build-gate refusals from animation
# tests starving under machine load — sweeps, the live app, PyInstaller).
# Scale EVERY qtbot timeout/wait ×5: identical behavior when the machine is
# fast (conditions still return early), tolerant when it is starved. The
# gate's verdict then reflects the CODE, not the CPU scheduler.
@pytest.fixture(autouse=True)
def _qt_timeout_headroom(monkeypatch):
    try:
        from pytestqt.qtbot import QtBot
    except Exception:
        yield
        return
    scale = 5

    def scaled(original):
        def wrapper(self, *args, timeout=None, **kwargs):
            if timeout is not None:
                kwargs["timeout"] = timeout * scale
            return original(self, *args, **kwargs)

        return wrapper

    for name in ("waitUntil", "waitSignal", "waitSignals"):
        if hasattr(QtBot, name):
            monkeypatch.setattr(QtBot, name, scaled(getattr(QtBot, name)))
    original_wait = QtBot.wait

    def wait_scaled(self, ms):
        return original_wait(self, ms * scale)

    monkeypatch.setattr(QtBot, "wait", wait_scaled)
    yield


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch, tmp_path):
    """No test may read the LIVE config.toml (2026-09-23: flipping
    shorts_enabled on the trading machine broke two long-only strategy
    tests that leaked live state). Every test sees a defaults config;
    tests that need specific settings re-patch config_path themselves."""
    path = tmp_path / "hermetic_config.toml"
    monkeypatch.setattr("waveapp.config.config_path", lambda: path)
