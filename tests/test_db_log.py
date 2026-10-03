"""Phase 3: logging pipeline → SQLite log table."""

import logging

from waveapp.persistence.db import Database
from waveapp.persistence.db_log import DBLogHandler, category_for


def test_category_mapping():
    assert category_for("wave.broker.alpaca", logging.INFO) == "ORDER"
    assert category_for("wave.scanner.heuristic", logging.INFO) == "SCANNER"
    assert category_for("wave.risk", logging.WARNING) == "RISK"
    assert category_for("wave.trade", logging.INFO) == "TRADE"
    assert category_for("wave.training.sweep", logging.INFO) == "TRAINING"
    assert category_for("wave.ui", logging.INFO) == "SYSTEM"
    # ERROR level wins regardless of source
    assert category_for("wave.scanner", logging.ERROR) == "ERROR"


def test_handler_writes_rows_via_worker(tmp_path):
    database = Database(tmp_path / "test.db")
    handler = DBLogHandler(database)
    test_logger = logging.getLogger("wave.risk.test_handler")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)

    test_logger.info("kill switch armed")
    test_logger.error("something broke")
    test_logger.removeHandler(handler)
    handler.stop()  # drains the queue before returning

    rows = database.query("SELECT category, level, message FROM log ORDER BY id")
    messages = [(r["category"], r["level"], r["message"]) for r in rows]
    assert ("RISK", "INFO", "kill switch armed") in messages
    assert ("ERROR", "ERROR", "something broke") in messages
    database.close()


def test_stop_reports_deep_queue_and_still_drains(capsys):
    """A4-12 (audit 2026-09-22): stop() used to give a 10k-deep queue 3s and
    say nothing — the tail of a busy session silently vanished at quit. Now a
    deep queue is reported on stderr (with the pending count), the join is
    10s by default, and an expired join reports the rows at risk."""
    import inspect
    import threading

    from waveapp.persistence.db_log import DEEP_QUEUE_NOTE

    # the default join is now 10s, not 3s
    assert inspect.signature(DBLogHandler.stop).parameters["timeout"].default == 10.0

    started = threading.Event()
    gate = threading.Event()
    written = []

    class BlockingDB:
        def write_log(self, **kwargs):
            started.set()
            gate.wait(5.0)
            written.append(kwargs["message"])

    handler = DBLogHandler(BlockingDB())

    def _record(i: int) -> logging.LogRecord:
        return logging.LogRecord(
            "wave.risk.tail", logging.INFO, __file__, 1, "tail row %d", (i,), None
        )

    # park the worker inside the first write, then pile up a deep queue
    handler.emit(_record(0))
    assert started.wait(2.0)
    backlog = DEEP_QUEUE_NOTE + 49
    for i in range(1, backlog + 1):
        handler.emit(_record(i))

    handler.stop(timeout=0.2)  # worker still gated — the join must expire
    err = capsys.readouterr().err
    assert f"{backlog} log rows still queued at stop" in err
    assert "log rows lost at quit" in err

    # release the gate: nothing was dropped, the sentinel sits BEHIND the
    # backlog so every queued row still lands
    gate.set()
    handler._worker.join(5.0)
    assert not handler._worker.is_alive()
    assert len(written) == backlog + 1


def test_persistence_logger_is_never_recorded(tmp_path):
    database = Database(tmp_path / "test.db")
    handler = DBLogHandler(database)
    loop_logger = logging.getLogger("wave.persistence.db")
    loop_logger.addHandler(handler)
    loop_logger.warning("this must not loop into the DB")
    loop_logger.removeHandler(handler)
    handler.stop()
    assert database.query("SELECT * FROM log") == []
    database.close()
