#!/usr/bin/env python
"""One-shot Keychain → VAULT migration (2026-09-02).

Reads every legacy per-entry Wave secret, writes them all into the single
'vault' item, verifies the round trip, then DELETES the legacy items so the
app only ever touches ONE Keychain entry (= at most one password prompt).

Run from the terminal (the terminal created the legacy items, so it reads
them without prompts):  .venv/bin/python scripts/migrate_keychain_vault.py
"""

from __future__ import annotations

import json

import keyring

from waveapp.config import KEYCHAIN_SERVICE

LEGACY_ENTRIES = (
    "alpaca_live_key_id",
    "alpaca_live_secret",
    "alpaca_paper_key_id",
    "alpaca_paper_secret",
    "anthropic_api_key",
    "anthropic_workspace_id",
    "app_password_hash",
    "finnhub_api_key",
    "polygon_api_key",
    "telegram_bot_token",
)
VAULT_ENTRY = "vault"


def main() -> int:
    existing_raw = keyring.get_password(KEYCHAIN_SERVICE, VAULT_ENTRY)
    vault: dict[str, str] = json.loads(existing_raw) if existing_raw else {}
    moved, missing = [], []
    for entry in LEGACY_ENTRIES:
        value = keyring.get_password(KEYCHAIN_SERVICE, entry)
        if value is None:
            missing.append(entry)
            continue
        vault[entry] = value
        moved.append(entry)
    keyring.set_password(KEYCHAIN_SERVICE, VAULT_ENTRY, json.dumps(vault))

    # verify the round trip BEFORE deleting anything (never lose a secret)
    check = json.loads(keyring.get_password(KEYCHAIN_SERVICE, VAULT_ENTRY) or "{}")
    for entry in moved:
        if check.get(entry) != vault[entry]:
            print(f"VERIFY FAILED for {entry} — legacy items NOT deleted")
            return 1

    for entry in moved:
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, entry)
        except keyring.errors.PasswordDeleteError:
            pass
    print(
        f"vault holds {len(check)} secrets · migrated {len(moved)} · "
        f"absent (never set): {', '.join(missing) if missing else 'none'}"
    )
    print("legacy items deleted — Wave now touches ONE Keychain entry only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
