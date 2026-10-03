# Wave — fresh-Mac install checklist (Phase 9)

Personal-use install on a single Mac (Apple Silicon). No Developer ID,
no notarization, no DMG — the app is ad-hoc signed and installed locally
(decision 2026-08-03).

## Prerequisites

- Apple Silicon Mac, macOS 13+ (glass materials look best on macOS 26+).
- Python 3.12 via Homebrew or python.org (only needed to BUILD, not to run).
- The Wave repo (this folder), or just a built `Wave.app` copied from the
  old Mac (both work — the .app is self-contained).

## A. Build & install from the repo

```bash
cd Wave
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
bash scripts/build_app.sh          # builds dist/Wave.app, ad-hoc signs,
                                   # installs to ~/Desktop/Wave.app
```

Run the test suite first if anything looks off:
`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest`

## B. First launch on a new Mac

Secrets live ONLY in the macOS Keychain, so a new Mac starts with none —
the backup zip deliberately contains no keys or passwords.

1. Launch Wave. Create the app password when prompted (Touch ID enrolls
   automatically after the first successful password login).
2. Settings → Connections: paste the Alpaca **paper** key id + secret →
   "Save to Keychain" (Touch ID) → "Test".
3. Settings → Connections: same for the **live** keys (read-only balance
   view until Phase 11; live trading stays locked).
4. Settings → Telegram: your numeric user id → Save; paste the bot token →
   "Save to Keychain" (Touch ID) → "Send test message".
5. Because the ad-hoc signature changes per build, macOS may ask for
   Keychain access again after a rebuild — approve with "Always Allow".

## C. Migrating data from the old Mac

1. Old Mac: Settings → Backup → "Export backup…" → produces a zip with
   `config.toml` + a database snapshot (NO secrets — see B).
2. New Mac: install per A, first-launch per B (password + keys re-entered
   by hand — this is by design).
3. Settings → Backup → "Restore backup…" → pick the zip. The config
   applies immediately; the database is STAGED and swaps in on the next
   launch (never overwritten while open — WAL corruption risk).
4. Quit and relaunch Wave. Performance history, log, and the candidate
   journal are back.

## D. Sanity checks after install

- Sidebar dots: Alpaca + Data feed green after Start; Telegram green if
  configured; DB green.
- System tab: market clock ticking, DB size > 0, fee schedule listed.
- Log tab: no ERROR rows after a fresh session (kill-switch tests aside).
