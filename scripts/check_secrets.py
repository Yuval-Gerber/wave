#!/usr/bin/env python3
"""Pre-commit guard (hard rule 6): no secrets in files, ever.

Scans staged file contents for broker/Telegram key patterns and private keys.
Exits non-zero (blocking the commit) on any hit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Alpaca API key ID", re.compile(r"\b(?:PK|AK)[A-Z0-9]{16,20}\b")),
    (
        "Alpaca secret key assignment",
        re.compile(
            r"(?i)(?:APCA[_-]?API[_-]?SECRET[_-]?KEY|alpaca[_-]?secret)\s*[:=]\s*['\"][^'\"]{20,}['\"]"
        ),
    ),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "Generic secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|api[_-]?secret|secret[_-]?key|password|token)\s*[:=]\s*['\"][A-Za-z0-9+/_-]{20,}['\"]"
        ),
    ),
]

# Files where naming a pattern is legitimate (this guard itself, and docs/specs).
EXEMPT = {"scripts/check_secrets.py"}

FORBIDDEN_FILENAMES = re.compile(r"(^|/)\.env(\..+)?$")


def main(argv: list[str]) -> int:
    failed = False
    for arg in argv:
        rel = Path(arg).as_posix()
        if FORBIDDEN_FILENAMES.search(rel):
            print(f"BLOCKED: {rel}: .env files must not exist — secrets live in Keychain")
            failed = True
            continue
        if rel in EXEMPT:
            continue
        try:
            text = Path(arg).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    print(f"BLOCKED: {rel}:{lineno}: looks like a {label}")
                    failed = True
    if failed:
        print("\nSecrets never go in files (§0.6). Use macOS Keychain via keyring.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
