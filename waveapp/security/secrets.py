"""Keychain access (§0.6): every secret lives in macOS Keychain.
Nothing here ever writes a secret to disk.

THE VAULT (2026-09-02 — after macOS's hidden partition-list
rules kept re-prompting for every one of Wave's 10 items despite two rounds
of signing fixes): all secrets live in ONE Keychain item — service "Wave",
account "vault", value = a JSON object of {entry: secret}. One item means
AT MOST ONE password prompt per app launch (and only after a rebuild), and
the in-process cache means the Keychain is touched once per run.

Migration is self-healing: a get for an entry the vault doesn't know falls
back to the legacy per-entry item and absorbs it into the vault. The
scripts/migrate_keychain_vault.py one-shot does the whole move up front and
deletes the legacy items so the app never touches them again.

Hardening (audit 2026-09-22, A4-9):
- writes MERGE against a fresh Keychain read, so a second process (e.g. a
  rotation script) that added entries after this process loaded its cache
  never gets them silently dropped;
- a write is refused when the fresh pre-write read comes back empty although
  this process has seen a non-empty vault — a denied prompt surfacing as
  None must never let a one-entry vault overwrite a full one;
- degraded-cache retries are rate-limited (each retry can pop a macOS
  password prompt; pre-fix EVERY get_secret retried → prompt storm).
"""

from __future__ import annotations

import json
import time

import keyring

from waveapp.config import KEYCHAIN_SERVICE

VAULT_ENTRY = "vault"
_cache: dict[str, str] | None = None


_cache_degraded = False  # a Keychain READ failed — the cache may be a lie
_saw_nonempty = False  # this process has, at some point, held a non-empty vault
_degraded_retry_at = 0.0  # time.monotonic() of the last failed Keychain read
DEGRADED_RETRY_SECONDS = 30.0  # min gap between degraded retries (tests override)


def _load_vault() -> dict[str, str]:
    global _cache, _cache_degraded, _saw_nonempty, _degraded_retry_at
    if _cache_degraded:
        # the comment below PROMISED "reads may retry" but the {} cache made
        # every later read return None for the life of the process — one
        # denied Keychain prompt silently killed the Telegram bridge all
        # session (2026-09-21). A degraded cache is no cache: re-read.
        #
        # A4-9(c): but each retry can pop a macOS password prompt, and with
        # get_secret called all over the engine an unthrottled retry became a
        # prompt storm during a denial streak. Retry at most once per
        # DEGRADED_RETRY_SECONDS; between retries serve the (empty) degraded
        # cache without touching the Keychain.
        if time.monotonic() - _degraded_retry_at >= DEGRADED_RETRY_SECONDS:
            _cache = None  # window elapsed — allow the re-read below
    if _cache is None:
        try:
            raw = keyring.get_password(KEYCHAIN_SERVICE, VAULT_ENTRY)
            _cache = json.loads(raw) if raw else {}
            if not isinstance(_cache, dict):
                _cache = {}
            _cache_degraded = False
            if _cache:
                _saw_nonempty = True  # arms the wipe guard in _save_vault
        except Exception:
            # audit 2026-09-16 (vault-wipe scenario): caching {} after a
            # DENIED Keychain prompt (expected after ad-hoc resigns) and
            # then letting set_secret WRITE that {} back would destroy
            # every stored key incl. the password hash. Mark degraded —
            # reads may retry (rate-limited above), writes are refused
            # until a clean read.
            _cache = {}
            _cache_degraded = True
            _degraded_retry_at = time.monotonic()
    return _cache


def _save_vault(vault: dict[str, str], changed_keys: set[str]) -> None:
    """Write the vault, merging against a fresh Keychain read.

    `changed_keys` are the entries THIS caller deliberately changed (set or
    deleted); for those, `vault`'s view wins. For every other key the fresh
    Keychain read wins, so entries another process (rotation script) added
    after this process loaded its cache are never dropped (A4-9(a)).
    """
    global _cache, _cache_degraded, _saw_nonempty
    if _cache_degraded:
        raise RuntimeError(
            "refusing to write the secrets vault: the last Keychain read "
            "failed, so this write could wipe every stored key"
        )
    # Ordering matters: the fresh read happens IMMEDIATELY before the write
    # to shrink the lost-update window to microseconds (the Keychain has no
    # compare-and-swap, so this is the best available). The merge below is
    # then built on the freshest view of the store.
    try:
        raw = keyring.get_password(KEYCHAIN_SERVICE, VAULT_ENTRY)
        fresh = json.loads(raw) if raw else {}
        if not isinstance(fresh, dict):
            fresh = {}
    except Exception as exc:
        raise RuntimeError(
            "refusing to write the secrets vault: the pre-write Keychain "
            "read failed, so this write could wipe every stored key"
        ) from exc
    # A4-9(b): a denial surfacing as None (instead of raising) reads as an
    # empty vault. If this process has EVER seen a non-empty vault, an empty
    # fresh read is a wipe hazard, not a fact — refuse the write. A genuine
    # fresh install (never saw entries) still writes its first entry.
    if not fresh and _saw_nonempty:
        raise RuntimeError(
            "refusing to write the secrets vault: the Keychain read back "
            "EMPTY although this process has seen a populated vault — a "
            "denied prompt may be masquerading as an empty store"
        )
    merged = dict(fresh)
    for key in changed_keys:
        if key in vault:
            merged[key] = vault[key]  # our explicit set wins for our key
        else:
            merged.pop(key, None)  # our explicit delete wins for our key
    keyring.set_password(KEYCHAIN_SERVICE, VAULT_ENTRY, json.dumps(merged))
    _cache = merged
    # after WE deliberately wrote this content, its emptiness (or not) is a
    # known fact, so the wipe guard tracks it rather than the past
    _saw_nonempty = bool(merged)


def reset_cache() -> None:
    """Testing/rotation hook: force the next read to hit the Keychain.

    Also clears the degraded/wipe-guard bookkeeping so tests start cold; a
    rotation script calling this simply gets a clean re-read next access.
    """
    global _cache, _cache_degraded, _saw_nonempty, _degraded_retry_at
    _cache = None
    _cache_degraded = False
    _saw_nonempty = False
    _degraded_retry_at = 0.0


def get_secret(entry: str) -> str | None:
    vault = _load_vault()
    if entry in vault:
        return vault[entry] or None
    if _cache_degraded:
        # the Keychain is denying us right now — the legacy per-entry read
        # below would only add another prompt to the storm (A4-9(c)); miss
        # quietly and let the rate-limited vault retry heal us later
        return None
    # legacy per-entry item (pre-vault) — absorb it so the next launch
    # touches only the vault
    try:
        legacy = keyring.get_password(KEYCHAIN_SERVICE, entry)
    except Exception:
        # a denied Keychain prompt must degrade to "not found", never
        # propagate — an uncaught raise here killed callers silently
        # (Telegram bridge, 2026-09-21)
        return None
    if legacy is not None:
        import contextlib

        vault[entry] = legacy  # in-process: this session never re-prompts
        with contextlib.suppress(Exception):  # absorption is best-effort
            set_secret(entry, legacy)
    return legacy


def set_secret(entry: str, value: str) -> None:
    vault = dict(_load_vault())
    vault[entry] = value
    _save_vault(vault, changed_keys={entry})


def delete_secret(entry: str) -> None:
    vault = dict(_load_vault())
    if entry in vault:
        del vault[entry]
        _save_vault(vault, changed_keys={entry})
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, entry)  # legacy cleanup
    except keyring.errors.PasswordDeleteError:
        pass
