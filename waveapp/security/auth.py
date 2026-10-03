"""Login authentication (§4).

- Password: argon2id hash stored in Keychain (never on disk). Failed attempts
  are logged; there is NO lockout — removed at the instruction (Phase 8.2
  round 5: "if someone doesn't know the password it will not know it").
- Touch ID: LAContext (LocalAuthentication framework). The completion handler
  fires on a system thread, so `authenticate_touch_id` bridges it back with a
  threading.Event and must be called off the UI thread (the login dialog runs
  it via asyncio's default executor).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from waveapp.config import AppConfig
from waveapp.security import secrets

logger = logging.getLogger("wave.security")

_hasher = PasswordHasher()


@dataclass
class PasswordAuth:
    """Argon2 password verification. Failures are logged, never locked out."""

    config: AppConfig = field(default_factory=AppConfig)
    _failed_attempts: int = 0

    @property
    def entry(self) -> str:
        return self.config.keychain_password_hash_entry

    def is_password_set(self) -> bool:
        return secrets.get_secret(self.entry) is not None

    def set_password(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        secrets.set_secret(self.entry, _hasher.hash(password))
        logger.info("App password set/updated")

    def verify(self, password: str) -> bool:
        stored = secrets.get_secret(self.entry)
        if stored is None:
            return False
        try:
            _hasher.verify(stored, password)
        except VerifyMismatchError:
            self._failed_attempts += 1
            logger.warning("Failed password attempt %d", self._failed_attempts)
            return False
        self._failed_attempts = 0
        if _hasher.check_needs_rehash(stored):
            secrets.set_secret(self.entry, _hasher.hash(password))
        logger.info("Password login OK")
        return True


def touch_id_available() -> bool:
    try:
        import LocalAuthentication
    except ImportError:
        return False
    context = LocalAuthentication.LAContext.new()
    ok, _error = context.canEvaluatePolicy_error_(
        LocalAuthentication.LAPolicyDeviceOwnerAuthenticationWithBiometrics, None
    )
    return bool(ok)


def authenticate_touch_id(reason: str = "unlock Wave", timeout: float = 120.0) -> bool:
    """Blocking Touch ID prompt. Call from a worker thread, never the UI thread."""
    import LocalAuthentication

    context = LocalAuthentication.LAContext.new()
    done = threading.Event()
    result: dict[str, bool] = {"ok": False}

    def _completion(success: bool, error: object) -> None:
        result["ok"] = bool(success)
        if not success:
            logger.warning("Touch ID failed/cancelled: %s", error)
        done.set()

    context.evaluatePolicy_localizedReason_reply_(
        LocalAuthentication.LAPolicyDeviceOwnerAuthenticationWithBiometrics,
        reason,
        _completion,
    )
    done.wait(timeout)
    if result["ok"]:
        logger.info("Touch ID login OK")
    return result["ok"]
