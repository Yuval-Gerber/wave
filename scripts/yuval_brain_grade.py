#!/usr/bin/env python
"""Daily grade of the Yuval-brain's shadow verdicts (WAVE 2 item 13 Tier A).

Usage: .venv/bin/python scripts/yuval_brain_grade.py [YYYY-MM-DD]

Reads yuval_brain_verdicts + positions from the paper DB (read-only) and
grades each verdict against what actually happened next, in dollars:

  bank / cut  : (verdict-time price - actual exit price) x qty x side
                -> positive = obeying the verdict would have kept that money
                   (for cut: the loss avoided)
  hold        : (actual exit price - verdict-time price) x qty x side
                -> positive = holding past the verdict moment paid
  take        : realized P&L of the position opened on that symbol within
                30 min after the verdict -> positive = right call
  skip        : minus that realized P&L (Wave traded it anyway)
                -> positive = the skip would have saved money;
                   not traded -> ungraded (needs tape, not the book)

abstain/budget rows are listed but never graded. Purely a report: this
script writes nothing anywhere.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from waveapp.config import support_dir  # noqa: E402

TAKE_WINDOW_MIN = 30  # a take/skip is judged by an entry within this window
GRADED = ("bank", "hold", "cut", "take", "skip")


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _side_sign(side: str | None) -> float:
    side = (side or "long").lower()
    return 1.0 if side.startswith(("l", "b")) else -1.0


def _exit_price(pos: sqlite3.Row) -> float | None:
    """Reconstruct the average exit from the book: avg_entry + pnl/qty (signed)."""
    qty = float(pos["qty"] or 0.0)
    entry = pos["avg_entry"]
    pnl = pos["realized_pnl"]
    if qty <= 0 or entry is None or pnl is None:
        return None
    return float(entry) + _side_sign(pos["side"]) * float(pnl) / qty


def _match_held(verdict: sqlite3.Row, positions: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The position the verdict was about: exact uuid, else the symbol's
    position whose holding window covers the verdict moment."""
    uuid = verdict["position_uuid"] or ""
    if uuid:
        for pos in positions:
            if pos["position_uuid"] == uuid:
                return pos
    ts = _parse_ts(verdict["ts"])
    if ts is None:
        return None
    for pos in positions:
        if pos["symbol"] != verdict["symbol"]:
            continue
        opened, closed = _parse_ts(pos["opened_at"] or ""), _parse_ts(pos["closed_at"] or "")
        if opened and opened <= ts and (closed is None or closed >= ts):
            return pos
    return None


def _match_entry_after(verdict: sqlite3.Row, positions: list[sqlite3.Row]) -> sqlite3.Row | None:
    ts = _parse_ts(verdict["ts"])
    if ts is None:
        return None
    best = None
    for pos in positions:
        if pos["symbol"] != verdict["symbol"]:
            continue
        opened = _parse_ts(pos["opened_at"] or "")
        if opened and ts <= opened <= ts + timedelta(minutes=TAKE_WINDOW_MIN):
            if best is None or opened < _parse_ts(best["opened_at"]):
                best = pos
    return best


def grade_one(verdict: sqlite3.Row, positions: list[sqlite3.Row]) -> tuple[float | None, str]:
    """(grade dollars, note). None = ungradable (open, unmatched, no tape)."""
    action = verdict["verdict"]
    if action not in GRADED:
        return None, "not graded"
    if action in ("take", "skip"):
        pos = _match_entry_after(verdict, positions)
        if pos is None:
            return None, "not traded — needs tape"
        if pos["realized_pnl"] is None:
            return None, "position still open"
        pnl = float(pos["realized_pnl"])
        return (pnl, "entry followed") if action == "take" else (-pnl, "traded anyway")
    pos = _match_held(verdict, positions)
    if pos is None:
        return None, "no matching position"
    if pos["closed_at"] is None or pos["realized_pnl"] is None:
        return None, "position still open"
    exit_px = _exit_price(pos)
    price = verdict["price"]
    if exit_px is None or price is None:
        return None, "book incomplete"
    qty = float(verdict["qty"] or pos["qty"] or 0.0)
    edge = (float(price) - exit_px) * qty * _side_sign(pos["side"])
    if action == "bank":
        return edge, "vs actual exit"
    if action == "cut":
        return edge, "loss avoided vs actual exit"
    return -edge, "holding paid" if -edge >= 0 else "holding cost"


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(tz=UTC).date().isoformat()
    db_file = support_dir() / "wave_paper.db"
    if not db_file.exists():
        print(f"no paper DB at {db_file}")
        return 1
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        try:
            verdicts = con.execute(
                "SELECT rowid, * FROM yuval_brain_verdicts WHERE ts LIKE ? ORDER BY ts",
                (f"{day}%",),
            ).fetchall()
        except sqlite3.OperationalError:
            print("yuval_brain_verdicts table missing — run the app once to migrate")
            return 1
        positions = con.execute(
            "SELECT position_uuid, symbol, side, qty, avg_entry, realized_pnl,"
            " opened_at, closed_at FROM positions WHERE opened_at LIKE ?"
            " OR closed_at LIKE ? OR closed_at IS NULL",
            (f"{day}%", f"{day}%"),
        ).fetchall()
    finally:
        con.close()
    if not verdicts:
        print(f"no Yuval-brain verdicts for {day}")
        return 0

    print(f"YUVAL-BRAIN SHADOW GRADES — {day} (shadow only; nothing was acted on)")
    header = (
        f"{'ts (UTC)':<20} {'symbol':<6} {'kind':<15} {'verdict':<8}"
        f" {'conf':>4} {'price':>8} {'grade $':>9}  note / reason"
    )
    print(header)
    print("-" * len(header))
    totals: dict[str, list[float]] = {}
    spend = 0.0
    for verdict in verdicts:
        spend += float(verdict["spend"] or 0.0)
        grade, note = grade_one(verdict, positions)
        if grade is not None:
            totals.setdefault(verdict["verdict"], []).append(grade)
        grade_s = f"{grade:+9.0f}" if grade is not None else f"{'—':>9}"
        conf = verdict["confidence"]
        conf_s = f"{conf:.2f}" if conf is not None else "  — "
        reason = (verdict["reason"] or "")[:48]
        print(
            f"{(verdict['ts'] or '')[:19]:<20} {verdict['symbol']:<6}"
            f" {verdict['kind']:<15} {verdict['verdict']:<8} {conf_s:>4}"
            f" {float(verdict['price'] or 0):>8.2f} {grade_s}  {note}; {reason}"
        )
    print("-" * len(header))
    for action in GRADED:
        grades = totals.get(action)
        if not grades:
            continue
        right = sum(1 for g in grades if g > 0)
        print(
            f"{action:<8} graded {len(grades):>3}  right {right:>3}"
            f"  ({100.0 * right / len(grades):.0f}%)  net {sum(grades):+,.0f} $"
        )
    graded_n = sum(len(v) for v in totals.values())
    print(
        f"total: {len(verdicts)} verdicts, {graded_n} graded,"
        f" spend ${spend:.4f} — grades are counterfactual dollars, not P&L"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
