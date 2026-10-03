"""The secrets vault under a hostile Keychain (2026-09-21: one denied macOS
prompt silently killed the Telegram bridge for the rest of the session)."""

import json

import keyring

from waveapp.security import secrets


def test_degraded_cache_retries_instead_of_poisoning(fake_keychain, monkeypatch):
    """A failed vault read must NOT lock every later read to None — the next
    read hits the Keychain again and succeeds. (Retries are rate-limited
    since A4-9; window zeroed here so the back-to-back retry still heals.)"""
    monkeypatch.setattr(secrets, "DEGRADED_RETRY_SECONDS", 0.0)
    calls = {"n": 0}
    real_get = keyring.get_password

    def flaky_get(service, entry):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Keychain prompt denied")
        return real_get(service, entry)

    monkeypatch.setattr(keyring, "get_password", flaky_get)
    fake_keychain[("Wave", "vault")] = json.dumps({"telegram_bot_token": "tok123"})
    assert secrets.get_secret("telegram_bot_token") is None  # denied once
    assert secrets.get_secret("telegram_bot_token") == "tok123"  # retried, healed


def test_degraded_cache_still_refuses_writes(fake_keychain, monkeypatch):
    """The vault-wipe guard survives the retry change: after a failed read,
    a write is refused until a clean read happens."""
    import pytest

    def always_deny(service, entry):
        raise RuntimeError("Keychain prompt denied")

    monkeypatch.setattr(keyring, "get_password", always_deny)
    assert secrets.get_secret("anything") is None
    with pytest.raises(RuntimeError):
        secrets.set_secret("app_password_hash", "value")


def test_legacy_read_denial_returns_none_not_raise(fake_keychain, monkeypatch):
    """The per-entry legacy fallback hits the Keychain fresh on every call —
    a denial there must degrade to 'not found', never propagate."""
    fake_keychain[("Wave", "vault")] = json.dumps({})

    def deny_legacy(service, entry):
        if entry == "vault":
            return fake_keychain.get((service, entry))
        raise RuntimeError("Keychain prompt denied")

    monkeypatch.setattr(keyring, "get_password", deny_legacy)
    assert secrets.get_secret("telegram_bot_token") is None  # no raise


def test_legacy_hit_is_cached_in_process(fake_keychain, monkeypatch):
    """A legacy entry read once never re-prompts this session, even when the
    vault absorption write fails."""
    fake_keychain[("Wave", "legacy_entry")] = "legval"

    def write_denied(service, entry, value):
        raise RuntimeError("Keychain write denied")

    monkeypatch.setattr(keyring, "set_password", write_denied)
    assert secrets.get_secret("legacy_entry") == "legval"
    del fake_keychain[("Wave", "legacy_entry")]  # Keychain would now prompt/miss
    assert secrets.get_secret("legacy_entry") == "legval"  # served from memory


# ---- A4-9 hardening (audit 2026-09-22) --------------------------------------


def test_set_secret_merges_external_process_writes(fake_keychain):
    """(A4-9a) A rotation script adds entry X to the vault AFTER this process
    loaded its cache; our next set_secret of Y must preserve X, not clobber
    the store with our stale cache."""
    fake_keychain[("Wave", "vault")] = json.dumps({"a": "1"})
    assert secrets.get_secret("a") == "1"  # loads + caches {"a": "1"}
    # external process rewrites the vault behind our back
    fake_keychain[("Wave", "vault")] = json.dumps({"a": "1", "x": "external"})
    secrets.set_secret("y", "ours")
    assert json.loads(fake_keychain[("Wave", "vault")]) == {
        "a": "1",
        "x": "external",
        "y": "ours",
    }
    # and the in-process cache absorbed the merge
    assert secrets.get_secret("x") == "external"


def test_set_secret_own_key_wins_over_fresh_read(fake_keychain):
    """(A4-9a) The merge is directional: fresh wins for keys we didn't touch,
    but OUR explicit change wins for our own key, even if another process
    also rewrote it after our load."""
    fake_keychain[("Wave", "vault")] = json.dumps({"a": "1"})
    assert secrets.get_secret("a") == "1"
    fake_keychain[("Wave", "vault")] = json.dumps({"a": "theirs", "x": "external"})
    secrets.set_secret("a", "mine")
    assert json.loads(fake_keychain[("Wave", "vault")]) == {
        "a": "mine",
        "x": "external",
    }


def test_delete_secret_merges_external_process_writes(fake_keychain):
    """(A4-9a) Deletes go through the same merge: our delete of A sticks,
    an externally-added X survives."""
    fake_keychain[("Wave", "vault")] = json.dumps({"a": "1", "b": "2"})
    assert secrets.get_secret("a") == "1"
    fake_keychain[("Wave", "vault")] = json.dumps({"a": "1", "b": "2", "x": "ext"})
    secrets.delete_secret("a")
    assert json.loads(fake_keychain[("Wave", "vault")]) == {"b": "2", "x": "ext"}


def test_empty_fresh_read_refuses_wipe(fake_keychain, monkeypatch):
    """(A4-9b) If a Keychain denial ever surfaces as None instead of raising,
    the pre-write fresh read looks like an empty vault. Once this process has
    seen a populated vault, that write must be refused, store untouched."""
    import pytest

    fake_keychain[("Wave", "vault")] = json.dumps({"a": "1"})
    assert secrets.get_secret("a") == "1"  # process has now seen a non-empty vault
    real_get = keyring.get_password

    def vault_denial_as_none(service, entry):
        if entry == "vault":
            return None  # denial masquerading as "no such item"
        return real_get(service, entry)

    monkeypatch.setattr(keyring, "get_password", vault_denial_as_none)
    with pytest.raises(RuntimeError):
        secrets.set_secret("b", "2")
    # the full vault is still intact in the store
    assert json.loads(fake_keychain[("Wave", "vault")]) == {"a": "1"}


def test_fresh_install_can_write_first_entry(fake_keychain):
    """(A4-9b) The wipe guard must NOT block a genuinely fresh install: no
    vault item exists, this process never saw entries → first write lands."""
    secrets.set_secret("first", "val")
    assert json.loads(fake_keychain[("Wave", "vault")]) == {"first": "val"}


def test_degraded_retries_are_rate_limited(fake_keychain, monkeypatch):
    """(A4-9c) During a denial streak, repeated get_secret calls hit the
    Keychain at most once per DEGRADED_RETRY_SECONDS window — no prompt
    storm — and the next window gets exactly one fresh retry."""
    calls = {"n": 0}

    def always_deny(service, entry):
        calls["n"] += 1
        raise RuntimeError("Keychain prompt denied")

    monkeypatch.setattr(keyring, "get_password", always_deny)
    clock = {"t": 1000.0}
    monkeypatch.setattr(secrets.time, "monotonic", lambda: clock["t"])

    assert secrets.get_secret("a") is None  # first read: hits Keychain, denied
    assert calls["n"] == 1
    for _ in range(5):  # storm inside the window — zero Keychain touches
        assert secrets.get_secret("a") is None
    assert calls["n"] == 1
    clock["t"] += secrets.DEGRADED_RETRY_SECONDS + 0.1
    assert secrets.get_secret("a") is None  # window elapsed → one retry
    assert calls["n"] == 2


def test_degraded_rate_limited_retry_still_heals(fake_keychain, monkeypatch):
    """(A4-9c) After the window elapses, the retry that succeeds fully heals
    the cache — same guarantee as the 2026-09-21 fix, just throttled."""
    real_get = keyring.get_password
    deny = {"on": True}

    def flaky_get(service, entry):
        if deny["on"]:
            raise RuntimeError("Keychain prompt denied")
        return real_get(service, entry)

    monkeypatch.setattr(keyring, "get_password", flaky_get)
    clock = {"t": 1000.0}
    monkeypatch.setattr(secrets.time, "monotonic", lambda: clock["t"])
    fake_keychain[("Wave", "vault")] = json.dumps({"telegram_bot_token": "tok123"})

    assert secrets.get_secret("telegram_bot_token") is None  # denied
    assert secrets.get_secret("telegram_bot_token") is None  # inside window
    deny["on"] = False
    clock["t"] += secrets.DEGRADED_RETRY_SECONDS + 0.1
    assert secrets.get_secret("telegram_bot_token") == "tok123"  # healed
