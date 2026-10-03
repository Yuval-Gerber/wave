-- Master blueprint 8.1 (the P1, 2026-09-01): label EVERY scanned
-- candidate. Each journaled scanner2 snapshot (5-min grid) gets as-of
-- triple-barrier outcomes simulated from score time at three horizons.
-- The features column is the journal's as-of vector COPIED — compute at
-- score time, store forever (the point-in-time rule); never recomputed.
CREATE TABLE IF NOT EXISTS snapshot_labels (
    ts           TEXT NOT NULL,     -- snapshot minute (UTC, = scanner2_snapshots.ts)
    symbol       TEXT NOT NULL,
    session_date TEXT NOT NULL,     -- ET session date (day-grouped CV key)
    features     TEXT NOT NULL,     -- as-of JSON vector from the journal
    entry        REAL,              -- next 1-min bar open after ts (no lookahead)
    atr          REAL,              -- dollar ATR used for the barriers
    label_30m    TEXT,              -- win / loss / flat within 30 minutes
    label_60m    TEXT,              -- win / loss / flat within 60 minutes
    label_sess   TEXT,              -- win / loss / flat by session end
    mfe_atr      REAL,              -- max favorable excursion, ATR units
    mae_atr      REAL,              -- max adverse excursion, ATR units
    n_snapshots  INTEGER,           -- labeled snapshots this symbol-day (uniqueness weight = 1/n)
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_labels_day ON snapshot_labels(session_date);
CREATE INDEX IF NOT EXISTS idx_snapshot_labels_symbol ON snapshot_labels(symbol);

-- 8.13: the cadence champion/challenger — live shadow rows carry which
-- model produced them (v1 = frozen champion, v2 = nightly live-features)
ALTER TABLE brain_scores ADD COLUMN model TEXT NOT NULL DEFAULT 'v1';
