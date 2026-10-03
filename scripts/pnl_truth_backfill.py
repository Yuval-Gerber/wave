#!/usr/bin/env python
"""Backfill broker-truth per-trade P&L for one day.

Usage: pnl_truth_backfill.py YYYY-MM-DD [--write]
Dry-run by default: prints every lineage whose realized_pnl differs from
broker-fill truth. --write applies the updates to wave_paper.db.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from waveapp.engine.pnl_truth import _ts, reconcile_symbol_day  # noqa: E402
from waveapp.persistence.db import Database  # noqa: E402
from waveapp.security.secrets import get_secret  # noqa: E402


async def main() -> int:
    day = sys.argv[1]
    write = "--write" in sys.argv
    db_path = Path.home() / "Library" / "Application Support" / "Wave" / "wave_paper.db"
    db = Database(db_path)
    headers = {
        "APCA-API-KEY-ID": get_secret("alpaca_paper_key_id"),
        "APCA-API-SECRET-KEY": get_secret("alpaca_paper_secret"),
    }
    acts: list[dict] = []
    token = None
    for _ in range(20):
        params = {"after": f"{day}T00:00:00Z", "page_size": 100}
        if token:
            params["page_token"] = token
        rows = requests.get(
            "https://paper-api.alpaca.markets/v2/account/activities/FILL",
            params=params,
            headers=headers,
            timeout=10,
        ).json()
        if not isinstance(rows, list) or not rows:
            break
        acts.extend(rows)
        if len(rows) < 100:
            break
        token = rows[-1].get("id")
    by_symbol: dict[str, list[dict]] = {}
    for a in acts:
        by_symbol.setdefault(a["symbol"], []).append(
            {
                "side": a["side"],
                "qty": float(a["qty"]),
                "price": float(a["price"]),
                "t": _ts(a["transaction_time"]),
            }
        )
    rows = db.query(
        "SELECT position_uuid, symbol, qty, opened_at, closed_at, realized_pnl "
        "FROM positions WHERE opened_at LIKE ? AND state='closed'",
        (f"{day}%",),
    )
    lineages_by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        lineages_by_symbol.setdefault(r["symbol"], []).append(
            {
                "uuid": r["position_uuid"],
                "qty": float(r["qty"]),
                "opened": _ts(r["opened_at"]),
                "closed": _ts(r["closed_at"]) if r["closed_at"] else None,
                "old": float(r["realized_pnl"] or 0.0),
            }
        )
    total_old = total_new = 0.0
    for symbol, lineages in sorted(lineages_by_symbol.items()):
        fills = by_symbol.get(symbol, [])
        truth = reconcile_symbol_day(fills, lineages) if fills else {}
        for ln in lineages:
            new = truth.get(ln["uuid"], ln["old"])
            total_old += ln["old"]
            total_new += new
            marker = "  " if abs(new - ln["old"]) <= 0.01 else "->"
            print(f"{marker} {symbol:6} {ln['uuid'][:8]} {ln['old']:+9.2f} -> {new:+9.2f}")
            if write and abs(new - ln["old"]) > 0.01:
                db.execute(
                    "UPDATE positions SET realized_pnl=? WHERE position_uuid=?",
                    (round(new, 2), ln["uuid"]),
                )
    print(f"TOTAL: {total_old:+.2f} -> {total_new:+.2f} ({'WRITTEN' if write else 'dry-run'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
