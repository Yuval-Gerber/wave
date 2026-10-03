"""Phase 3: database, migrations, fee schedule, log writes."""

import sqlite3

import pytest

from waveapp.broker.base import TradingMode
from waveapp.persistence.db import Database, db_path

EXPECTED_TABLES = {
    "orders",
    "fills",
    "positions",
    "equity_snapshots",
    "cashflows",
    "log",
    "settings_versions",
    "symbol_costs",
    "candidates",
    "fee_schedule",
    "model_registry",
    "reports",
    "exit_brain_rows",
    "yuval_brain_verdicts",
    "judge_transitions",
    "entry_lag",
    "ml_daily",
    "day_regime",
    "efficacy_events",
}


def _tables(database: Database) -> set[str]:
    rows = database.query("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


def test_migrations_create_full_schema(tmp_path):
    database = Database(tmp_path / "test.db")
    assert database.schema_version() == 14  # 014_efficacy (2026-09-24)
    assert EXPECTED_TABLES <= _tables(database)
    database.close()


def test_migrations_are_idempotent(tmp_path):
    database = Database(tmp_path / "test.db")
    database.migrate()  # second run must be a no-op
    assert database.schema_version() == 14  # 014_efficacy (2026-09-24)
    database.close()


# -- audit 2026-09-22 (A4-6): crash between DDL and version row ---------------


def test_half_applied_migration_heals_on_next_launch(tmp_path):
    """Legacy executescript() committed a migration's DDL independently of the
    schema_version INSERT — a crash between the two used to brick startup with
    'duplicate column name'. Simulate that state (DDL present, version rows
    missing) and prove the next launch heals instead of raising."""
    path = tmp_path / "test.db"
    Database(path).close()  # full schema on disk (version 14)

    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM schema_version WHERE version > 1")
    conn.commit()
    conn.close()

    # relaunch: migrations 002..014 re-run against already-applied DDL
    # (duplicate ALTER columns, already-existing CREATEs) — must not raise
    database = Database(path)
    assert database.schema_version() == 14
    assert EXPECTED_TABLES <= _tables(database)
    database.close()


def test_fully_applied_ddl_with_no_version_rows_heals(tmp_path):
    """Worst legacy case: even migration 001's seed INSERT re-runs (UNIQUE
    conflict on fee_schedule) — tolerated, no duplicate rows, right version."""
    path = tmp_path / "test.db"
    Database(path).close()

    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM schema_version")
    conn.commit()
    conn.close()

    database = Database(path)
    assert database.schema_version() == 14
    rows = database.query("SELECT COUNT(*) AS n FROM fee_schedule WHERE fee_name='sec_section31'")
    assert rows[0]["n"] == 1  # seed not duplicated
    database.close()


def test_failed_migration_is_atomic(tmp_path, monkeypatch):
    """A migration that dies mid-script must leave NO trace: neither its
    earlier DDL nor its version row (one explicit transaction, unlike the old
    executescript whose implicit COMMIT split the two)."""
    import waveapp.persistence.db as db_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    real = db_module._MIGRATIONS_DIR
    (migrations / "001_initial.sql").write_text((real / "001_initial.sql").read_text())
    (migrations / "002_bad.sql").write_text(
        "CREATE TABLE half_done (id INTEGER PRIMARY KEY);\nINSERT INTO no_such_table VALUES (1);\n"
    )
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", migrations)

    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        Database(tmp_path / "test.db")

    conn = sqlite3.connect(tmp_path / "test.db")
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == 1  # 001 committed, 002 rolled back whole
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "half_done" not in tables  # the CREATE before the crash is gone too
    finally:
        conn.close()

    # and the same DB reopens cleanly once the bad migration is fixed
    (migrations / "002_bad.sql").write_text("CREATE TABLE half_done (id INTEGER PRIMARY KEY);\n")
    database = Database(tmp_path / "test.db")
    assert database.schema_version() == 2
    database.close()


def test_wal_mode_active(tmp_path):
    database = Database(tmp_path / "test.db")
    mode = database.query("PRAGMA journal_mode")[0][0]
    assert mode == "wal"
    database.close()


def test_db_path_per_mode():
    assert db_path(TradingMode.PAPER).name == "wave_paper.db"
    assert db_path(TradingMode.LIVE).name == "wave_live.db"


def test_fee_schedule_seeded_and_date_aware(tmp_path):
    database = Database(tmp_path / "test.db")
    sec = database.active_fee("sec_section31", "2026-08-04")
    assert sec is not None and sec["rate"] == 20.60
    assert database.active_fee("sec_section31", "2026-01-01") is None  # not yet effective
    taf = database.active_fee("finra_taf", "2026-08-04")
    assert taf["cap"] == 9.79 and taf["exempt_below"] == 50
    database.close()


def test_write_and_read_log(tmp_path):
    database = Database(tmp_path / "test.db")
    database.write_log("RISK", "WARNING", "daily loss 2.1%", {"pct": 2.1})
    rows = database.query("SELECT * FROM log WHERE category='RISK'")
    assert len(rows) == 1
    assert "daily loss" in rows[0]["message"]
    assert '"pct": 2.1' in rows[0]["json_payload"]
    database.close()
