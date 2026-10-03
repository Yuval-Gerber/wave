-- WAVE 2 spec item 13, Tier A (approved 2026-09-16): the Yuval-brain shadow
-- verdict journal. Every judgment the advisor makes at a decision moment —
-- entry_candidate / runner_quiet / bleeder / giveback — lands here with the
-- price anchors the grader needs. SHADOW ONLY: nothing in the engine reads
-- this table to trade; scripts/yuval_brain_grade.py grades it daily against
-- the positions table.
CREATE TABLE IF NOT EXISTS yuval_brain_verdicts (
    ts            TEXT NOT NULL,      -- UTC ISO-8601, moment of the verdict
    symbol        TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- entry_candidate|runner_quiet|bleeder|giveback
    verdict       TEXT NOT NULL,      -- bank|hold|cut|skip|take|abstain|budget
    confidence    REAL,               -- 0..1 (0 for abstain/budget)
    reason        TEXT,               -- <=140 chars, the model's plain words
    spend         REAL,               -- dollars this call cost (0 = no call made)
    position_uuid TEXT,               -- positions.position_uuid when held ('' else)
    strategy      TEXT,
    price         REAL,               -- current price at verdict time (grading anchor)
    entry_price   REAL,
    peak_price    REAL,
    qty           REAL,
    minutes_held  REAL
);
CREATE INDEX IF NOT EXISTS idx_yuval_brain_ts ON yuval_brain_verdicts(ts);
CREATE INDEX IF NOT EXISTS idx_yuval_brain_symbol ON yuval_brain_verdicts(symbol);
