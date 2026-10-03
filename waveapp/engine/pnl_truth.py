"""Broker-truth trade P&L (2026-09-15).

The actor books only fills IT placed. Twice on 2026-09-15 reality diverged:
a hand-completed exit (TRMD: 378 shares sold manually while the old build's
stop bug jammed the actor) and a lineage split (FPS: 330 banked + 141
re-adopted) left the positions table showing +$413 where the broker banked
+$830. The equity curve was always right (it reads the account); the
per-trade rows lied low.

This module rebuilds realized P&L per position lineage from the broker's
own FILL activities, FIFO-matching buys to sells per symbol-day and
allocating each fill to the lineage whose holding window contains it.
It only ever RE-WRITES positions.realized_pnl (display/journal truth);
it never touches orders, fills, or live trading state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("wave.engine.pnl_truth")


@dataclass
class _Lot:
    lineage: str  # position_uuid the shares belong to
    qty: float
    price: float


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def reconcile_symbol_day(
    fills: list[dict],
    lineages: list[dict],
) -> dict[str, float]:
    """FIFO-match one symbol-day of broker fills onto Wave lineages.

    fills: [{"side": "buy"|"sell"|"sell_short", "qty": float, "price":
        float, "t": epoch-seconds}] in time order — EVERY broker fill for
        the symbol that day, Wave-placed or not.
    lineages: [{"uuid": str, "side": "long"|"short" (default "long"),
        "qty": float, "opened": epoch, "closed": epoch|None}] — Wave's
        position rows for the symbol-day.

    Returns {uuid: realized_pnl}. SIDE-AWARE (the 2026-09-25 loss-side
    study: the long-only version zeroed EVERY short's realized_pnl — a
    short's sell_short entry matched no long lot and its cover buy was
    misfiled as an unsold long entry). Two FIFO books, netted the way
    the broker nets the account:

    - buy fills first COVER open short lots (P&L = entry sell px − cover
      buy px, credited to the lot's lineage), and any remainder is split
      across open LONG lineages with remaining entry capacity.
    - sell/sell_short fills first CONSUME open long lots (P&L = sell px −
      entry buy px), and any remainder opens short lots split across open
      SHORT lineages with remaining entry capacity.

    Fills that match no lot and no lineage (flat-book manual trades) are
    ignored — they belong to no card.
    """
    remaining_capacity = {ln["uuid"]: float(ln["qty"]) for ln in lineages}
    long_lots: list[_Lot] = []
    short_lots: list[_Lot] = []
    realized: dict[str, float] = {ln["uuid"]: 0.0 for ln in lineages}

    def open_lineages_at(t: float, side: str) -> list[dict]:
        out = []
        for ln in lineages:
            if ln.get("side", "long") != side:
                continue
            closed = ln["closed"]
            # 90s of slack: entry fills land moments before the DB row's
            # opened_at commit; exits can settle moments after closed_at.
            if ln["opened"] - 90 <= t <= (closed if closed is not None else 4e12) + 90:
                out.append(ln)
        return out

    def consume(lots: list[_Lot], qty: float, pnl_per_share) -> float:
        """Eat lots FIFO, crediting each consumed lot's lineage."""
        while qty > 0 and lots:
            lot = lots[0]
            take = min(lot.qty, qty)
            realized[lot.lineage] = realized.get(lot.lineage, 0.0) + take * pnl_per_share(lot.price)
            lot.qty -= take
            qty -= take
            if lot.qty <= 1e-9:
                lots.pop(0)
        return qty

    def allocate(qty: float, price: float, t: float, side: str, book: list[_Lot]) -> None:
        """Split an entry fill across open same-side lineages with capacity."""
        for ln in open_lineages_at(t, side):
            cap = remaining_capacity.get(ln["uuid"], 0.0)
            if cap <= 0 or qty <= 0:
                continue
            take = min(cap, qty)
            book.append(_Lot(ln["uuid"], take, price))
            remaining_capacity[ln["uuid"]] = cap - take
            qty -= take
        # unmatched remainder: shares Wave never tracked — ignore

    for f in sorted(fills, key=lambda x: x["t"]):
        qty, price, t = float(f["qty"]), float(f["price"]), f["t"]
        if f["side"] == "buy":
            # cover shorts first (short P&L = entry sell px − cover buy px) …
            qty = consume(short_lots, qty, lambda entry_px: entry_px - price)  # noqa: B023
            # … then the remainder is a long entry
            allocate(qty, price, t, "long", long_lots)
        else:  # sell / sell_short
            # exit longs first (long P&L = sell px − entry buy px) …
            qty = consume(long_lots, qty, lambda entry_px: price - entry_px)  # noqa: B023
            # … then the remainder is a short entry
            allocate(qty, price, t, "short", short_lots)
    return realized


async def reconcile_day(adapter, mode, database, day_iso: str) -> list[tuple[str, float, float]]:
    """Rewrite positions.realized_pnl for one day from broker FILL truth.

    Returns [(symbol, old_pnl, new_pnl)] for every lineage that changed
    by more than a cent. Safe to run repeatedly (idempotent).
    """
    activities = await adapter.get_fill_activities(mode, after=f"{day_iso}T00:00:00Z")
    by_symbol: dict[str, list[dict]] = {}
    for a in activities:
        by_symbol.setdefault(a["symbol"], []).append(
            {
                "side": a["side"],
                "qty": float(a["qty"]),
                "price": float(a["price"]),
                "t": _ts(a["transaction_time"]),
            }
        )

    changed: list[tuple[str, float, float]] = []
    rows = database.query(
        "SELECT position_uuid, symbol, side, qty, opened_at, closed_at, realized_pnl "
        "FROM positions WHERE opened_at LIKE ? AND state = 'closed'",
        (f"{day_iso}%",),
    )
    lineages_by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        lineages_by_symbol.setdefault(r["symbol"], []).append(
            {
                "uuid": r["position_uuid"],
                # side-aware pairing (2026-09-25): without it the FIFO
                # matcher zeroed every short's realized_pnl overnight
                "side": (r["side"] or "long"),
                "qty": float(r["qty"]),
                "opened": _ts(r["opened_at"]),
                "closed": _ts(r["closed_at"]) if r["closed_at"] else None,
                "old": float(r["realized_pnl"] or 0.0),
            }
        )

    for symbol, lineages in lineages_by_symbol.items():
        fills = by_symbol.get(symbol)
        if not fills:
            continue
        truth = reconcile_symbol_day(fills, lineages)
        for ln in lineages:
            new = truth.get(ln["uuid"], 0.0)
            if abs(new - ln["old"]) > 0.01:
                database.execute(
                    "UPDATE positions SET realized_pnl = ? WHERE position_uuid = ?",
                    (round(new, 2), ln["uuid"]),
                )
                changed.append((symbol, ln["old"], new))
                logger.info(
                    "pnl truth: %s lineage %s: %+.2f -> %+.2f (broker fills)",
                    symbol,
                    ln["uuid"][:8],
                    ln["old"],
                    new,
                )
    return changed
