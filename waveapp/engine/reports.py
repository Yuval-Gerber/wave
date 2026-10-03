"""Weekly reports (weekend agenda #4, 2026-08-23 — spec).

Two report kinds, both stored in the `reports` table so the Performance
tab's Reports page can browse history by calendar:

- week_close — Friday after the bell: what the week actually did, from the
  real ledger (closed trades only, removed trades respected).
- week_ahead — Monday 09:00 ET: what THIS week looks like — the trading
  calendar (holidays/early closes by name, from Wave's own calendar), a
  market snapshot computed from real bars when available, and Wave's own
  posture. No predictions are invented: everything here is either a
  calendar fact, a measured number, or Wave's configuration.

Generation is pure given its inputs — the monitor owns scheduling and the
Telegram pushes.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

logger = logging.getLogger("wave.engine.reports")


def week_bounds(day: date) -> tuple[date, date]:
    """(Monday, Sunday) of the ISO week containing `day`."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def ahead_monday(day: date) -> date:
    """The Monday a WEEK-AHEAD report should describe, seen from `day`:
    on a weekend that is NEXT week's Monday (2026-08-23, screenshot:
    a Sunday-seeded report titled with the week that had just ENDED);
    during the week it is the current week's Monday."""
    if day.weekday() >= 5:  # Saturday/Sunday → look forward
        return day + timedelta(days=7 - day.weekday())
    return day - timedelta(days=day.weekday())


def _week_trades(database, monday: date, sunday: date) -> list[dict]:
    rows = database.query(
        "SELECT symbol, realized_pnl, strategy, closed_at, avg_entry, qty,"
        " decision_price FROM positions"
        " WHERE state='closed' AND realized_pnl IS NOT NULL"
        " AND COALESCE(perf_hidden, 0) = 0"
        " AND substr(closed_at, 1, 10) BETWEEN ? AND ?"
        " ORDER BY closed_at",
        (monday.isoformat(), sunday.isoformat()),
    )
    return [dict(r) for r in rows]


def week_close_report(database, monday: date) -> tuple[str, dict]:
    """The Friday report: the week's truth from the ledger."""
    sunday = monday + timedelta(days=6)
    trades = _week_trades(database, monday, sunday)
    prev_trades = _week_trades(database, monday - timedelta(days=7), monday - timedelta(days=1))
    pnls = [float(t["realized_pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    lines = [f"📒 Week report — {monday:%b %d} to {(monday + timedelta(days=4)):%b %d}"]
    if not trades:
        lines.append("No closed trades this week.")
    else:
        win_rate = len(wins) / len(pnls) * 100.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses else float("inf")
        pf_text = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
        days = len({str(t["closed_at"])[:10] for t in trades})
        lines.append(
            f"Result: {total:+,.2f} $ across {len(trades)} trades"
            f" in {days} session{'s' if days != 1 else ''}."
        )
        lines.append(
            f"Won {len(wins)} / lost {len(losses)}"
            f" ({win_rate:.0f}% win rate, profit factor {pf_text})."
        )
        best = max(trades, key=lambda t: float(t["realized_pnl"]))
        worst = min(trades, key=lambda t: float(t["realized_pnl"]))
        lines.append(
            f"Best: {best['symbol']} {float(best['realized_pnl']):+,.2f} $ ·"
            f" worst: {worst['symbol']} {float(worst['realized_pnl']):+,.2f} $."
        )
        by_strategy: dict[str, list[float]] = {}
        for t in trades:
            by_strategy.setdefault(str(t["strategy"] or "adopted"), []).append(
                float(t["realized_pnl"])
            )
        strategy_bits = [
            f"{name} {sum(vals):+,.0f} $ ({len(vals)})"
            for name, vals in sorted(by_strategy.items(), key=lambda kv: -sum(kv[1]))
        ]
        lines.append("By strategy: " + " · ".join(strategy_bits) + ".")
        # option A (2026-08-24): what entry slippage COST this week
        slips = [
            (float(t["avg_entry"]) - float(t["decision_price"])) * float(t["qty"])
            for t in trades
            if t.get("decision_price") and t.get("avg_entry")
        ]
        if slips:
            per_share = [
                (float(t["avg_entry"]) - float(t["decision_price"])) * 100.0
                for t in trades
                if t.get("decision_price") and t.get("avg_entry")
            ]
            lines.append(
                f"Entry slippage: {sum(slips):+,.2f} $ total"
                f" (avg {sum(per_share) / len(per_share):+.1f}¢/share,"
                f" {len(slips)} measured trades)."
            )
    if prev_trades:
        prev_total = sum(float(t["realized_pnl"]) for t in prev_trades)
        arrow = (
            "better than"
            if total > prev_total
            else ("behind" if total < prev_total else "even with")
        )
        lines.append(f"Last week was {prev_total:+,.2f} $ — this week came in {arrow} it.")
    payload = {
        "total": round(total, 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
    }
    return "\n".join(lines), payload


def week_ahead_report(
    database,
    monday: date,
    market: dict | None = None,
    posture: dict | None = None,
) -> tuple[str, dict]:
    """The Monday-morning report: the week's calendar facts, a measured
    market snapshot (when bars were available), and Wave's posture."""
    from waveapp.engine.session import day_info

    lines = [f"🗓️ Week ahead — {monday:%b %d} to {(monday + timedelta(days=4)):%b %d}"]
    open_days = 0
    specials: list[str] = []
    for offset in range(5):
        day = monday + timedelta(days=offset)
        trading, early = day_info(day)
        if trading:
            open_days += 1
            if early:
                specials.append(f"{day:%A}: early close {early:%H:%M} ET")
        else:
            name = _holiday_name(day)
            specials.append(f"{day:%A}: market closed" + (f" — {name}" if name else ""))
    lines.append(
        f"{open_days} trading day{'s' if open_days != 1 else ''} this week"
        + ("." if not specials else "; " + "; ".join(specials) + ".")
    )
    if market:
        bits = []
        for symbol, change in market.get("changes", {}).items():
            bits.append(f"{symbol} {change:+.1f}%")
        if bits:
            lines.append("Last week: " + " · ".join(bits) + ".")
        vol = market.get("volatility")
        if vol is not None:
            mood = "lively" if vol > 1.2 else ("quiet" if vol < 0.7 else "normal")
            lines.append(f"Market movement has been {mood} (daily swings ≈ {vol:.1f}% on SPY).")
    if posture:
        lines.append(
            f"Wave's plan: {posture.get('strategies', 'ORB · VWAP · GAPGO')}, long only,"
            f" entries ≥ ${posture.get('floor', 10):g}, auto-trade"
            f" {'ON' if posture.get('auto_trade') else 'OFF'}."
        )
    counters = _evidence_counters(database)
    if counters:
        lines.append(
            f"Road to live: {counters['sessions']} of 30 sessions,"
            f" {counters['trades']} of 200 trades banked."
        )
    payload = {"open_days": open_days, "specials": specials}
    return "\n".join(lines), payload


def _holiday_name(day: date) -> str | None:
    try:
        from waveapp.engine.session import _get_calendar

        names = _get_calendar().regular_holidays.holidays(
            day.isoformat(), day.isoformat(), return_name=True
        )
        for stamp, name in names.items():
            if stamp.date() == day:
                return str(name)
    except Exception:
        logger.debug("holiday name lookup failed", exc_info=True)
    return None


def _evidence_counters(database) -> dict | None:
    try:
        rows = database.query(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT substr(closed_at,1,10)) AS d"
            " FROM positions WHERE state='closed' AND realized_pnl IS NOT NULL"
            " AND COALESCE(perf_hidden, 0) = 0"
        )
        return {"trades": int(rows[0]["n"]), "sessions": int(rows[0]["d"])}
    except Exception:
        logger.debug("evidence counters failed", exc_info=True)
        return None


def store_report(database, kind: str, monday: date, content: str, payload: dict) -> None:
    from waveapp.persistence.db import utc_now

    database.execute(
        "INSERT OR REPLACE INTO reports (report_date, kind, created_at, content, json_payload)"
        " VALUES (?,?,?,?,?)",
        (monday.isoformat(), kind, utc_now(), content, json.dumps(payload)),
    )


def load_report(database, kind: str, on_or_before: date) -> dict | None:
    """The newest stored report of `kind` covering a week at or before the
    given date — how the calendar browses history."""
    rows = database.query(
        "SELECT report_date, kind, created_at, content FROM reports"
        " WHERE kind = ? AND report_date <= ? ORDER BY report_date DESC LIMIT 1",
        (kind, on_or_before.isoformat()),
    )
    return dict(rows[0]) if rows else None
