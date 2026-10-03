"""Logging pipeline → SQLite `log` table (SPEC.md §13, Phase 3).

A queue + worker thread keeps DB writes off the UI/engine threads. Categories
are derived from logger names via the prefix map below; records at ERROR level
or above land in ERROR regardless of source. Records from the persistence
logger itself are skipped (no feedback loops).
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import UTC, datetime

from waveapp.persistence.db import Database

_CATEGORY_BY_PREFIX = (
    ("wave.trade", "TRADE"),
    ("wave.broker", "ORDER"),
    ("wave.engine.order", "ORDER"),
    ("wave.scanner", "SCANNER"),
    ("wave.risk", "RISK"),
    ("wave.training", "TRAINING"),
)

_SENTINEL = None

# A4-12 (audit 2026-09-22): stop() parks the sentinel BEHIND everything
# already queued, so a deep queue at quit is a real drain, not a formality.
# Above this depth stop() says so on stderr (never through logging — this
# handler is usually on the root logger and a log line here would queue
# behind its own sentinel).
DEEP_QUEUE_NOTE = 100


def category_for(logger_name: str, levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "ERROR"
    for prefix, category in _CATEGORY_BY_PREFIX:
        if logger_name.startswith(prefix):
            return category
    return "SYSTEM"


class DBLogHandler(logging.Handler):
    """Non-blocking handler: emit() only enqueues; a worker thread writes."""

    def __init__(self, database: Database) -> None:
        super().__init__()
        self._db = database
        self._queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._worker = threading.Thread(target=self._drain, name="wave-db-log", daemon=True)
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("wave.persistence"):
            return  # the DB layer must not log into itself
        try:
            self._queue.put_nowait(record)
        except queue.Full:  # drop rather than block the app
            pass

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is _SENTINEL:
                return
            try:
                self._db.write_log(
                    category=category_for(record.name, record.levelno),
                    level=record.levelname,
                    message=record.getMessage(),
                    payload={"logger": record.name},
                    ts=datetime.fromtimestamp(record.created, UTC).isoformat(
                        timespec="milliseconds"
                    ),
                )
            except Exception:  # noqa: S110 — a broken log write must never crash the app
                pass

    def stop(self, timeout: float = 10.0) -> None:
        """A4-12 (audit 2026-09-22): the old 3s join dropped the tail of a
        busy session at quit — up to 10k queued rows sat in front of the
        sentinel. 10s covers any realistic backlog (WAL writes run ~1ms/row);
        a deep queue is reported so a lost tail is at least never silent."""
        import sys

        pending = self._queue.qsize()
        if pending > DEEP_QUEUE_NOTE:
            print(
                f"wave db_log: {pending} log rows still queued at stop — "
                f"draining (up to {timeout:.0f}s)",
                file=sys.stderr,
            )
        self._queue.put(_SENTINEL)
        self._worker.join(timeout)
        if self._worker.is_alive():
            print(
                f"wave db_log: worker did not finish within {timeout:.0f}s — "
                f"~{self._queue.qsize()} log rows lost at quit",
                file=sys.stderr,
            )
