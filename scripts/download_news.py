#!/usr/bin/env python
"""Phase 10.10: download news-mention pairs (symbol, published time) for the
full research range into the research store. Feeds the catalyst filter."""

from __future__ import annotations

import sys
from datetime import date, timedelta

from waveapp.research.history import POLYGON_BASE, HistoryStore, PolygonClient
from waveapp.research.progress import finish, report

START = date(2025, 8, 15)
END = date(2026, 8, 14)


def main() -> int:
    store = HistoryStore()
    store._conn.execute(
        "CREATE TABLE IF NOT EXISTS news_mentions ("
        " symbol TEXT NOT NULL, published_ms INTEGER NOT NULL,"
        " PRIMARY KEY (symbol, published_ms))"
    )
    store._conn.execute("CREATE TABLE IF NOT EXISTS news_coverage (day TEXT PRIMARY KEY)")
    store._conn.commit()
    client = PolygonClient()
    covered = {r[0] for r in store._conn.execute("SELECT day FROM news_coverage").fetchall()}

    days = [(START + timedelta(days=i)) for i in range((END - START).days + 1)]
    saved = 0
    for index, day in enumerate(days):
        key = day.isoformat()
        if key in covered:
            continue
        if index % 5 == 0:
            report("news", index, len(days), f"News download: {index}/{len(days)} days")
        url = (
            f"{POLYGON_BASE}/v2/reference/news?published_utc.gte={key}"
            f"&published_utc.lt={(day + timedelta(days=1)).isoformat()}&limit=1000&order=asc"
        )
        pairs = set()
        while url:
            payload = client._get(url)
            for article in payload.get("results") or []:
                ts = article.get("published_utc", "")
                try:
                    import datetime as dt

                    ms = int(
                        dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
                    )
                except Exception:  # noqa: S112 — malformed timestamp: skip article
                    continue
                for ticker in article.get("tickers") or []:
                    if ticker.isalpha() and len(ticker) <= 5:
                        pairs.add((ticker, ms))
            url = payload.get("next_url")
        store._conn.executemany("INSERT OR IGNORE INTO news_mentions VALUES (?,?)", sorted(pairs))
        store._conn.execute("INSERT OR IGNORE INTO news_coverage VALUES (?)", (key,))
        store._conn.commit()
        saved += len(pairs)
    finish(f"News download DONE — {saved:,} mention pairs")
    print(f"saved {saved:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
