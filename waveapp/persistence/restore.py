"""Staged backup restore (Phases 8.10/9).

Settings → Backup → Restore never overwrites the live database (a WAL file
open in another connection would corrupt); it stages the restored copy next
to it as `<name>.db.restored`. This module swaps the staged file in at the
NEXT launch, before the Database opens.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("wave.persistence")


def apply_staged_restore(db_path: Path) -> bool:
    """If a staged restore exists for `db_path`, swap it in. Returns True
    when a swap happened. Must run BEFORE the database is opened."""
    staged = db_path.with_suffix(".db.restored")
    if not staged.exists():
        return False
    for stale in (
        db_path,
        db_path.with_suffix(".db-wal"),
        db_path.with_suffix(".db-shm"),
    ):
        stale.unlink(missing_ok=True)
    staged.rename(db_path)
    logger.warning("restored database swapped in from backup")
    return True
