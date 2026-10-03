#!/bin/zsh
# Build Wave.app (onedir, ad-hoc signed — §2: personal use only,
# no Developer ID / notarization). Run from the repo root:
#   scripts/build_app.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Gate discipline (2026-09-03, two load-flake gate failures): the suite is
# Qt-timing-sensitive under full CPU load — a research sweep hammering all
# cores makes animation/timer tests flake and can segfault offscreen Qt.
# Never run the gate during a sweep.
if pgrep -f "scripts/research_" > /dev/null 2>&1; then
    echo "FATAL: a research sweep is running — the build gate needs a quiet machine."
    echo "       Wait for the sweep to finish (or stop it) and rerun."
    exit 1
fi

echo "==> MORNING REHEARSAL (the mandate, 2026-08-31: no rehearsal, no install)"
# The real monitor drives the real EngineCore through the complete morning
# sequence (pre-market watch → preflight → 9:20 queue → cancel → 9:31
# fallback+gate → runway guard → exit layers). If it fails, the build STOPS.
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_morning_rehearsal.py -q \
    || { echo "FATAL: the morning rehearsal FAILED — Wave must not be installed."; exit 1; }

echo "==> Full test suite (two processes: ~600 Qt tests in one heap corrupt"
echo "    QGraphicsScene teardown — 2026-09-15 .ips: itemsBoundingRect on a"
echo "    dead item. Every test still runs; each half gets a fresh Qt heap.)"
HALF_A=$(ls tests/test_*.py | sort | awk '$0 <= "tests/test_login_window.py"')
HALF_B=$(ls tests/test_*.py | sort | awk '$0 > "tests/test_login_window.py"')
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q $HALF_A \
    || { echo "FATAL: test suite (half A) failed — Wave must not be installed."; exit 1; }
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q $HALF_B \
    || { echo "FATAL: test suite (half B) failed — Wave must not be installed."; exit 1; }

echo "==> App icon"
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/make_icon.py

echo "==> PyInstaller build"
.venv/bin/pyinstaller --noconfirm --clean wave.spec

echo "==> Codesign"
# Stable identity (2026-09-02: end the 10× Keychain prompts): when the
# one-time 'Wave Signing' self-signed cert exists (scripts/make_signing_cert.sh)
# every rebuild keeps the SAME identity, so macOS asks "Always Allow" once and
# then never again. Falls back to ad-hoc when the cert is absent.
if security find-identity -p codesigning -v 2>/dev/null | grep -q "Wave Signing"; then
    echo "    signing with the stable 'Wave Signing' identity"
    codesign --force --deep -s "Wave Signing" dist/Wave.app
else
    echo "    ad-hoc (run scripts/make_signing_cert.sh once to stop Keychain prompts)"
    codesign --force --deep -s - dist/Wave.app
fi

echo "==> Verify"
codesign --verify --deep --strict dist/Wave.app
codesign -dv dist/Wave.app 2>&1 | grep -E "Identifier|Signature"
# bundled data assets the app cannot run without
test -n "$(find dist/Wave.app -name wave_stroke.json)" || { echo "FATAL: wave_stroke.json missing from bundle"; exit 1; }
test -n "$(find dist/Wave.app -name '001_initial.sql')" || { echo "FATAL: migrations missing from bundle"; exit 1; }
test -n "$(find dist/Wave.app -name 'word_stroke.json')" || { echo "FATAL: word_stroke.json missing from bundle"; exit 1; }
test -n "$(find dist/Wave.app -name 'check.svg')" || { echo "FATAL: check.svg missing from bundle"; exit 1; }

echo "==> Install to Desktop"
# ATOMIC SWAP (2026-09-14, the RBLX-popup corruption): ditto over a RUNNING
# Wave rewrites files the live process reads lazily (PyInstaller archive,
# certifi) and corrupts them mid-session. Now: if Wave is running, the old
# bundle is MOVED aside (the process keeps its open inodes and stays fully
# healthy) and the new build lands at the canonical path for next launch.
# Still honors the 2026-08-20 "don't wait for me to close the app".
if pgrep -f "Wave.app/Contents/MacOS/Wave" > /dev/null 2>&1; then
    OLD="$HOME/Desktop/.Wave.app.running-$(date +%s)"
    # STAGED SWAP (2026-09-16, the certifi TLS window): the ditto of a
    # ~500MB bundle into the canonical path leaves SECONDS during which the
    # running process resolves per-request resources (certifi cacert) into
    # a half-copied bundle — every REST call (backfills, ORDER EXITS) can
    # fail in that window. Stage the copy on the same volume first, then
    # swap with two renames (millisecond window, atomic per file).
    STAGING="$HOME/Desktop/.Wave.app.staging-$$"
    echo "Wave is RUNNING — staging new bundle, two-rename swap (old kept at $OLD)"
    rm -rf "$STAGING"
    ditto dist/Wave.app "$STAGING"
    mv "$HOME/Desktop/Wave.app" "$OLD"
    mv "$STAGING" "$HOME/Desktop/Wave.app"
    # While Wave RUNS, sweep NOTHING (audit 2026-09-16 + the same-night
    # correction: ps reports the process's ORIGINAL launch path — always
    # Desktop/Wave.app — never the set-aside location its bundle was moved
    # to, so no path check can identify the live bundle. Keeping every
    # .running-* while a Wave process exists is the only safe rule; the
    # ~500MB/build cost is reclaimed by the not-running branch's sweep on
    # the next quiet install.) Staging orphans older than an hour do go.
    find "$HOME/Desktop" -maxdepth 1 -name ".Wave.app.staging-*" -mmin +60 -exec rm -rf {} + 2>/dev/null || true
else
    # not-running path made atomic too (audit: rm+ditto left a broken app
    # on disk-full — silent no-launch at 9:20)
    STAGING="$HOME/Desktop/.Wave.app.staging-$$"
    rm -rf "$STAGING"
    ditto dist/Wave.app "$STAGING"
    rm -rf "$HOME/Desktop/Wave.app"
    mv "$STAGING" "$HOME/Desktop/Wave.app"
    # quiet install = the safe moment to reclaim ALL set-aside bundles
    rm -rf "$HOME"/Desktop/.Wave.app.running-* 2>/dev/null || true
fi

echo ""
echo "Done: ~/Desktop/Wave.app (also at dist/Wave.app)"
