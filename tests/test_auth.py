import pytest

from waveapp.config import AppConfig
from waveapp.security.auth import PasswordAuth


@pytest.fixture
def password_auth(fake_keychain):
    return PasswordAuth(config=AppConfig())


def test_no_password_initially(password_auth):
    assert not password_auth.is_password_set()
    assert not password_auth.verify("anything")


def test_set_and_verify(password_auth):
    password_auth.set_password("correct horse battery")
    assert password_auth.is_password_set()
    assert password_auth.verify("correct horse battery")
    assert not password_auth.verify("wrong")


def test_minimum_length_enforced(password_auth):
    with pytest.raises(ValueError):
        password_auth.set_password("short")
    assert not password_auth.is_password_set()


def test_hash_stored_in_keychain_not_plaintext(fake_keychain):
    auth = PasswordAuth(config=AppConfig())
    auth.set_password("supersecretpw")
    # secrets live in the single VAULT item now (2026-09-02) — one Keychain
    # entry, at most one macOS prompt
    import json

    vault = json.loads(fake_keychain[("Wave", "vault")])
    stored = vault["app_password_hash"]
    assert "supersecretpw" not in stored
    assert stored.startswith("$argon2")
    assert list(fake_keychain) == [("Wave", "vault")]  # nothing outside the vault


def test_no_lockout_many_failures_then_right_password_works(password_auth):
    """No lockout (Phase 8.2 round 5): failures only log a warning,
    and the correct password always gets in."""
    password_auth.set_password("correct horse battery")
    for _ in range(10):
        assert not password_auth.verify("wrong")
    assert password_auth.verify("correct horse battery")
