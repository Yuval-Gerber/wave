#!/usr/bin/env python
"""Veto tracker (2026-09-16: "when the brain refuses a buy,
follow this position and make sure not buying was the right order").

Tails the log for YUVAL-BRAIN VETO lines; 45 minutes after each veto,
grades the refusal against the real tape — stop-aware: an entry at the
veto price with a 1.5%-proxy stop that would have STOPPED before any run
counts as a RIGHT veto even if the name later ran. Prints one verdict
line per veto (a Monitor surfaces it on the screen).
"""

from __future__ import annotations

import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from waveapp.security.secrets import get_secret  # noqa: E402

ET = ZoneInfo("America/New_York")
LOG = Path.home() / "Library" / "Application Support" / "Wave" / "logs" / "wave.log"
H = {
    "APCA-API-KEY-ID": get_secret("alpaca_paper_key_id"),
    "APCA-API-SECRET-KEY": get_secret("alpaca_paper_secret"),
}
GRADE_AFTER_S = 45 * 60
STOP_PCT = 1.5  # bracket proxy: stop this % below the refused entry

VETO_RE = re.compile(r"^(2026-\d\d-\d\d \d\d:\d\d:\d\d),\d+ WARNING.*YUVAL-BRAIN VETO: (\w+) ")


def grade(symbol: str, t0: datetime) -> str | None:
    start = t0.astimezone(UTC).isoformat().replace("+00:00", "Z")
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            params={"timeframe": "1Min", "start": start, "limit": 50, "feed": "sip"},
            headers=H,
            timeout=8,
        )
        bars = r.json().get("bars") or []
    except Exception:
        return None
    if len(bars) < 5:
        return None
    entry = bars[0]["o"]
    stop = entry * (1 - STOP_PCT / 100)
    outcome, best = None, entry
    for b in bars:
        if b["l"] <= stop and outcome is None:
            outcome = "stopped"
        best = max(best, b["h"])
    last = bars[-1]["c"]
    run_pct = (best / entry - 1) * 100
    end_pct = (last / entry - 1) * 100
    if outcome == "stopped":
        return (
            f"VETO-CHECK {symbol}: RIGHT — a buy at {entry:.2f} would have "
            f"STOPPED at {stop:.2f} (~-{STOP_PCT:.1f}%) before anything else"
        )
    if end_pct <= 0.3:
        return (
            f"VETO-CHECK {symbol}: RIGHT — went {end_pct:+.1f}% in 45min "
            f"(peaked {run_pct:+.1f}%), nothing missed"
        )
    return (
        f"VETO-CHECK {symbol}: WRONG — ran {end_pct:+.1f}% without stopping "
        f"(missed ~${end_pct / 100 * 10000:+.0f} at size); goes on the veto's bill"
    )


LESSONS = Path(__file__).resolve().parent.parent / "research" / "veto_lessons.json"


def _append_lesson(verdict: str) -> None:
    """Feed the advisor its own graded record (same-day learning loop)."""
    import json

    try:
        rows = json.loads(LESSONS.read_text()) if LESSONS.exists() else []
        rows.append(verdict)
        LESSONS.write_text(json.dumps(rows[-50:], indent=0))
    except Exception as exc:  # noqa: BLE001 — a lesson lost never kills the tracker
        print(f"lesson write failed: {exc}", flush=True)


def main() -> int:
    seen: set[str] = set()
    pending: list[tuple[float, str, datetime]] = []
    pos = LOG.stat().st_size if LOG.exists() else 0
    while True:
        try:
            size = LOG.stat().st_size
            if size < pos:
                pos = 0  # rotated
            with LOG.open(errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            for line in chunk.splitlines():
                m = VETO_RE.match(line)
                if not m:
                    continue
                key = m.group(1) + m.group(2)
                if key in seen:
                    continue
                seen.add(key)
                t0 = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
                pending.append((time.time() + GRADE_AFTER_S, m.group(2), t0))
                print(f"VETO-TRACKING {m.group(2)} — verdict in 45min", flush=True)
            now = time.time()
            due = [p for p in pending if p[0] <= now]
            pending = [p for p in pending if p[0] > now]
            for _, symbol, t0 in due:
                verdict = grade(symbol, t0)
                if verdict:
                    print(verdict, flush=True)
                    _append_lesson(verdict)
        except Exception as exc:  # noqa: BLE001 — the tracker must survive anything
            print(f"veto-watch cycle failed: {exc}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
