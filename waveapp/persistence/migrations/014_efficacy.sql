-- R1.5 — THE EFFICACY GUARD's journal (after the −$3,928 day,
-- 2026-09-24, the worst ever: the first two trades lost $1,500 at full size
-- within 25 minutes of the open and nothing watched whether Wave's OWN
-- entries were following through). One row per mode flip
-- (MOMENTUM <-> INVERTED, plus the quiet day-rollover reset), written by
-- waveapp/engine/efficacy.py via the monitor's db closure — the same
-- journal_db_cb pattern as migration 013's day_regime.

CREATE TABLE IF NOT EXISTS efficacy_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,      -- UTC ISO at the flip
    day               TEXT NOT NULL,      -- ET session day
    from_mode         TEXT NOT NULL,      -- MOMENTUM / INVERTED
    to_mode           TEXT NOT NULL,
    reason            TEXT,               -- "2 consecutive fails" / "signals failing 4/5" / "day rollover"
    consecutive_fails INTEGER,            -- the flipping mode's stats at the flip
    n_pass            INTEGER,
    n_fail            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_efficacy_events_ts ON efficacy_events(ts);
