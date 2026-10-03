-- Brain Stage M1 (2026-09-01): live SHADOW scoring journal.
-- Every entry signal gets a calibrated p_win the moment it fires; outcomes
-- resolve nightly. This table IS the live precision evidence for the
-- win-rate ladder and the veto threshold (Stage M3).
CREATE TABLE IF NOT EXISTS brain_scores (
    ts          TEXT NOT NULL,          -- UTC, moment the signal fired
    symbol      TEXT NOT NULL,
    strategy    TEXT,
    p_win       REAL NOT NULL,          -- calibrated probability
    approved    INTEGER NOT NULL,       -- p_win >= shadow threshold
    features    TEXT NOT NULL,          -- exact vector scored (parity audit)
    executed    INTEGER NOT NULL DEFAULT 0,  -- did Wave actually enter
    resolved_at TEXT,                   -- nightly resolution stamp
    won         INTEGER,                -- realized outcome (NULL = unresolved)
    outcome_pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_brain_scores_ts ON brain_scores(ts);
CREATE INDEX IF NOT EXISTS idx_brain_scores_symbol ON brain_scores(symbol);
