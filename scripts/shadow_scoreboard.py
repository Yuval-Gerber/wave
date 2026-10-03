#!/usr/bin/env python
"""Daily scoreboard: the Master Key shadow vs what the real kitchen did.

Usage: .venv/bin/python scripts/shadow_scoreboard.py [YYYY-MM-DD]
Reads support_dir()/research/shadow_kitchen_<day>.jsonl (written live by the
shadow) and the paper DB's closed positions for the same day.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from waveapp.config import support_dir  # noqa: E402


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(tz=UTC).date().isoformat()
    path = support_dir() / "research" / f"shadow_kitchen_{day}.jsonl"
    if not path.exists():
        print(f"no shadow journal for {day} ({path})")
        return 1
    shadow: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        sp = shadow.setdefault(row["position"], {"symbol": row["symbol"], "pnl": None, "fills": 0})
        if row["event"] == "fill":
            sp["fills"] += 1
            sp["pnl"] = row["pnl_after"]
        elif row["event"] == "done":
            sp["pnl"] = row["shadow_pnl"]
    actual: dict[str, float] = {}
    db = support_dir() / "wave_paper.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for uuid, pnl in con.execute(
                "SELECT position_uuid, realized_pnl FROM positions "
                "WHERE substr(opened_at, 1, 10) = ?",
                (day,),
            ):
                actual[uuid[:12]] = pnl or 0.0
        finally:
            con.close()
    print(f"MASTER KEY SHADOW vs REAL KITCHEN — {day}")
    print(f"{'symbol':>8} {'shadow':>9} {'actual':>9} {'fills':>6}")
    s_tot = a_tot = 0.0
    for key, sp in shadow.items():
        a = actual.get(key)
        s = sp["pnl"] if sp["pnl"] is not None else 0.0
        s_tot += s
        a_tot += a or 0.0
        a_txt = f"{a:+.0f}" if a is not None else "open"
        print(f"{sp['symbol']:>8} {s:>+9.0f} {a_txt:>9} {sp['fills']:>6}")
    print(f"{'TOTAL':>8} {s_tot:>+9.0f} {a_tot:>+9.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
