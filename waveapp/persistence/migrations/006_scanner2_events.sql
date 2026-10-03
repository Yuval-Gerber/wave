-- Scanner 2.0 S2 — Architecture B event stream (sign-off-by-word order,
-- 2026-09-01): ingest-time edge triggers journaled for the Scanner tab,
-- the trigger-count leaderboard, and the ML dataset.
CREATE TABLE IF NOT EXISTS scanner2_events (
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- hod_break / rvol_cross / vol_spike / news / halt / resume
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_scanner2_events_symbol ON scanner2_events(symbol);
CREATE INDEX IF NOT EXISTS idx_scanner2_events_ts ON scanner2_events(ts);
