"""SQLite persistence (SPEC.md §13): WAL mode, one file per environment
(`wave_paper.db` / `wave_live.db`), numbered SQL migrations applied
automatically in order — never destructive without asking.

sqlite3 is used directly (no ORM). The connection allows cross-thread use and
serializes writes with a lock; heavier async wrapping arrives with the engine
phases if profiling demands it.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waveapp.broker.base import TradingMode
from waveapp.config import support_dir

logger = logging.getLogger("wave.persistence")

_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_RE = re.compile(r"^(\d{3})_.+\.sql$")

# audit 2026-09-22 (A4-6): errors that mean "this statement already ran" on a
# legacy half-applied DB (old executescript() committed the DDL but crashed
# before the schema_version INSERT). duplicate column / already exists come
# from re-run ALTER/CREATE; UNIQUE constraint failed from re-run seed INSERTs
# (001's fee_schedule rows). Tolerating them heals such a DB instead of
# bricking startup with "duplicate column name" forever.
_ALREADY_APPLIED_MARKERS = (
    "duplicate column name",
    "already exists",
    "unique constraint failed",
)


def _statement_already_applied(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _ALREADY_APPLIED_MARKERS)


def _split_statements(script: str) -> list[str]:
    """Split a migration script into individual statements.

    sqlite3.complete_statement respects semicolons inside string literals
    (001's fee_schedule notes contain ';') and comments, unlike a naive
    split(';'). Leading comment lines ride along with their statement, which
    SQLite happily ignores.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    tail = buffer.strip()
    # a tail is only real SQL if any line is more than whitespace/-- comment
    if tail and any(
        stripped and not stripped.startswith("--")
        for stripped in (line.strip() for line in tail.splitlines())
    ):
        statements.append(tail)
    return statements


def db_path(mode: TradingMode) -> Path:
    return support_dir() / f"wave_{mode.value}.db"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # audit 2026-09-16: repair/research scripts open this SAME live DB
        # read-write; without a busy_timeout a held write lock made money-
        # row writes (orders/fills/positions) fail after the 5s default —
        # and those failures were swallowed with a log line only.
        self._conn.execute("PRAGMA busy_timeout=15000")
        self.migrate()

    @classmethod
    def for_mode(cls, mode: TradingMode) -> Database:
        return cls(db_path(mode))

    # -- migrations ---------------------------------------------------------

    def schema_version(self) -> int:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            return int(row["v"] or 0)

    def migrate(self) -> None:
        applied = self.schema_version()
        migrations: list[tuple[int, Path]] = []
        for file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            match = _MIGRATION_RE.match(file.name)
            if match:
                migrations.append((int(match.group(1)), file))
        for number, file in migrations:
            if number <= applied:
                continue
            logger.info("applying migration %s", file.name)
            # audit 2026-09-22 (A4-6): executescript() issues an implicit
            # COMMIT before running, so the DDL used to commit independently
            # of the schema_version INSERT — a crash between the two left the
            # next launch re-running the script into "duplicate column name"
            # and Database.__init__ raising (app could not open its DB).
            # Now: individual statements + the version INSERT inside ONE
            # explicit transaction (DDL is transactional in SQLite), with
            # already-applied statements tolerated to heal legacy DBs.
            statements = _split_statements(file.read_text())
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    for statement in statements:
                        try:
                            self._conn.execute(statement)
                        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
                            if not _statement_already_applied(exc):
                                raise
                            logger.info(
                                "migration %s: statement already applied, skipping (%s)",
                                file.name,
                                exc,
                            )
                    self._conn.execute("INSERT INTO schema_version VALUES (?)", (number,))
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
        final = self.schema_version()
        if final != applied:
            logger.info("database at schema version %d (%s)", final, self.path.name)

    # -- generic access -----------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """Batched insert in ONE transaction — scanner2 journals a whole
        minute at once (per-row autocommit is fsync-bound and 100× slower)."""
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(sql, rows)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()

    # -- log ----------------------------------------------------------------

    def write_log(
        self,
        category: str,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO log (ts, category, level, message, json_payload) VALUES (?,?,?,?,?)",
            (
                ts or utc_now(),
                category,
                level,
                message,
                json.dumps(payload) if payload else None,
            ),
        )

    # -- fees ---------------------------------------------------------------

    def active_fee(self, fee_name: str, on_date: str | None = None) -> sqlite3.Row | None:
        """The fee row in force on `on_date` (default: today, UTC)."""
        on_date = on_date or utc_now()[:10]
        rows = self.query(
            "SELECT * FROM fee_schedule WHERE fee_name = ? AND effective_date <= ? "
            "ORDER BY effective_date DESC LIMIT 1",
            (fee_name, on_date),
        )
        return rows[0] if rows else None
