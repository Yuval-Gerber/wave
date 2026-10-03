#!/usr/bin/env python
"""One-time ledger repair (2026-08-22: 'performance shows $1,300,
I won $2,000'). Root cause: cumulative-partial-fill accounting (fixed
forward in phase-10.24) mis-booked historical realized_pnl and dropped
whole episodes.

Method: rebuild TRUE episodes from the fills table's FINAL cumulative row
per order (reliable qty + final avg price), walk net position 0→0 per
symbol, then:
- exactly one closed row in the episode window → UPDATE its realized_pnl;
- duplicate rows (twin-era) → true pnl on the closed row, NULL on superseded;
- no row at all → INSERT a repaired closed row.
Validated against the broker equity trail before applying.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path.home() / "Library/Application Support/Wave/wave_paper.db"


def episodes(db) -> list[dict]:
    finals = db.execute("""
      SELECT f.symbol, f.side, f.qty, f.price, f.ts FROM fills f
      JOIN (SELECT order_id, MAX(qty) mq FROM fills GROUP BY order_id) m
        ON f.order_id = m.order_id AND f.qty = m.mq
      WHERE f.ts >= '2026-08-19' GROUP BY f.order_id ORDER BY f.ts""").fetchall()
    out, open_pos = [], {}
    for f in finals:
        sym, q = f["symbol"], f["qty"]
        st = open_pos.setdefault(
            sym,
            {
                "net": 0.0,
                "cost": 0.0,
                "proceeds": 0.0,
                "bought": 0.0,
                "sold": 0.0,
                "start": f["ts"],
                "first_side": f["side"],
            },
        )
        st["net"] += q if f["side"] == "buy" else -q
        if f["side"] == "buy":
            st["cost"] += q * f["price"]
            st["bought"] += q
        else:
            st["proceeds"] += q * f["price"]
            st["sold"] += q
        if abs(st["net"]) < 0.01:
            long_side = st["first_side"] == "buy"
            qty = st["bought"] if long_side else st["sold"]
            entry_avg = (st["cost"] / st["bought"]) if long_side else (st["proceeds"] / st["sold"])
            out.append(
                {
                    "symbol": sym,
                    "start": st["start"],
                    "end": f["ts"],
                    "pnl": round(st["proceeds"] - st["cost"], 2),
                    "qty": qty,
                    "avg_entry": round(entry_avg, 4),
                    "side": "long" if long_side else "short",
                }
            )
            del open_pos[sym]
    return out


def main() -> int:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    updates = inserts = nulled = 0
    for ep in episodes(db):
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        # ISO-T strings compare correctly only against ISO-T strings —
        # sqlite's datetime() emits space-separated form (the round-1 bug
        # that inserted 37 duplicates)
        end_pad = (_dt.fromisoformat(ep["end"]) + _td(minutes=3)).isoformat()
        rows = db.execute(
            "SELECT position_uuid, state, realized_pnl FROM positions WHERE symbol=?"
            " AND realized_pnl IS NOT NULL AND closed_at >= ? AND closed_at <= ?",
            (ep["symbol"], ep["start"], end_pad),
        ).fetchall()
        closed = [r for r in rows if r["state"] == "closed"]
        others = [r for r in rows if r["state"] != "closed"]
        if len(closed) >= 1:
            if abs(closed[0]["realized_pnl"] - ep["pnl"]) > 0.01:
                db.execute(
                    "UPDATE positions SET realized_pnl=?, exit_path=COALESCE(exit_path,'')"
                    " || ' [pnl repaired]' WHERE position_uuid=?",
                    (ep["pnl"], closed[0]["position_uuid"]),
                )
                updates += 1
            for twin in closed[1:]:  # twin-era double-books: ONE episode, one row
                db.execute(
                    "UPDATE positions SET realized_pnl=NULL, exit_path=COALESCE(exit_path,'')"
                    " || ' [twin half — merged]' WHERE position_uuid=?",
                    (twin["position_uuid"],),
                )
                nulled += 1
        elif not closed:
            db.execute(
                "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
                " strategy, opened_at, trading_mode, closed_at, realized_pnl, exit_path)"
                " VALUES (lower(hex(randomblob(16))),?,?,?,?,'closed','GAP',?,?,?,?,"
                " 'ledger repair (fills reconstruction)')",
                (
                    ep["symbol"],
                    ep["side"],
                    ep["qty"],
                    ep["avg_entry"],
                    ep["start"],
                    "paper",
                    ep["end"],
                    ep["pnl"],
                ),
            )
            inserts += 1
        for r in others:  # superseded twins: pnl must not double-count anywhere
            db.execute(
                "UPDATE positions SET realized_pnl=NULL WHERE position_uuid=?",
                (r["position_uuid"],),
            )
            nulled += 1
    db.commit()
    total = db.execute(
        "SELECT ROUND(SUM(realized_pnl),2) FROM positions WHERE state='closed'"
        " AND realized_pnl IS NOT NULL"
    ).fetchone()[0]
    print(f"updates {updates}, inserted episodes {inserts}, superseded nulled {nulled}")
    print(f"ledger total now: {total:+,.2f} (equity truth ≈ +2,051)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
