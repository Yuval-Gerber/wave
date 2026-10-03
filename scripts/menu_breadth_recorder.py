#!/usr/bin/env python
"""Menu-breadth recorder (2026-09-15).

Two fade-day ideas (a fade-day gear and fade-day shorts) both died on the
same blind spot: nothing records how the scanner's menu behaves through
the close, so the promising day-gate (fraction of the menu below its own open)
can be neither proven on history nor raised live. This script closes the
hole: every minute of the session it snapshots every symbol the scanner
has focused on today and appends one row of breadth numbers.

Runs standalone (launchd, weekdays 09:31 ET), exits itself after 16:05 ET.
Output: research/menu_breadth/<YYYY-MM-DD>.csv
    ts_et,n_menu,n_below_open,frac_below_open,n_snapshots_ok
Symbols come from today's log lines ("scanner2 focus +…" and "day list"),
re-read each cycle so newly focused names join the menu mid-day.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from waveapp.security.secrets import get_secret  # noqa: E402

ET = ZoneInfo("America/New_York")
LOG = Path.home() / "Library" / "Application Support" / "Wave" / "logs" / "wave.log"
OUT_DIR = Path(__file__).resolve().parent.parent / "research" / "menu_breadth"
H = {
    "APCA-API-KEY-ID": get_secret("alpaca_paper_key_id"),
    "APCA-API-SECRET-KEY": get_secret("alpaca_paper_secret"),
}
DATA = "https://data.alpaca.markets"
SYM = re.compile(r"\b[A-Z][A-Z0-9.]{0,5}\b")


def todays_menu() -> set[str]:
    """Union of every symbol scanner2 focused on today, from the log."""
    today = datetime.now(tz=ET).strftime("%Y-%m-%d")
    symbols: set[str] = set()
    # log rotation (2026-09-16: a midday restart rotated wave.log and the
    # menu view collapsed 112 -> 11): read today's lines from the rotated
    # file too.
    for log_path in (LOG.with_suffix(".log.1"), LOG):
        try:
            with log_path.open(errors="replace") as f:
                for line in f:
                    if not line.startswith(today):
                        continue
                    if "scanner2 focus +" in line:
                        part = line.split("scanner2 focus +", 1)[1]
                    elif "day list (scanner2 LIVE): +" in line:
                        part = line.split("day list (scanner2 LIVE): +", 1)[1]
                    else:
                        continue
                    symbols.update(SYM.findall(part))
        except OSError:
            pass
    return symbols


def breadth_row(symbols: set[str]) -> tuple[int, int, int] | None:
    if not symbols:
        return None
    below = ok = 0
    syms = sorted(symbols)
    for start in range(0, len(syms), 100):
        chunk = ",".join(syms[start : start + 100])
        try:
            r = requests.get(
                f"{DATA}/v2/stocks/snapshots",
                params={"symbols": chunk, "feed": "sip"},
                headers=H,
                timeout=10,
            )
            snaps = r.json() if r.ok else {}
        except Exception:  # noqa: S112 — a failed chunk skips, the row still writes
            continue
        for snap in snaps.values():
            daily = (snap or {}).get("dailyBar") or {}
            trade = (snap or {}).get("latestTrade") or {}
            day_open, price = daily.get("o"), trade.get("p")
            if not day_open or not price:
                continue
            ok += 1
            if price < day_open:
                below += 1
    return (len(symbols), below, ok)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{datetime.now(tz=ET):%Y-%m-%d}.csv"
    if not out.exists():
        out.write_text("ts_et,n_menu,n_below_open,frac_below_open,n_snapshots_ok\n")
    while True:
        now = datetime.now(tz=ET)
        if now.hour == 16 and now.minute >= 5 or now.hour > 16:
            return 0
        if (now.hour, now.minute) >= (9, 30):
            row = breadth_row(todays_menu())
            if row is not None:
                n, below, ok = row
                frac = below / ok if ok else 0.0
                with out.open("a") as f:
                    f.write(f"{now:%H:%M:%S},{n},{below},{frac:.3f},{ok}\n")
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
