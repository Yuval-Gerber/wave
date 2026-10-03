-- R0 — the DAY JUDGE's journal (after the −$854 chop day,
-- 2026-09-23: "Wave must know, live, what KIND of day it is").
-- OBSERVATION ONLY: one row per published verdict CHANGE (post-hysteresis),
-- written by waveapp/engine/day_judge.py via the monitor's db closure —
-- the same journal_db_cb pattern as migration 012's judge_transitions.

CREATE TABLE IF NOT EXISTS day_regime (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,      -- UTC ISO at the verdict change
    verdict     TEXT NOT NULL,      -- TREND_UP / TREND_DOWN / CHOP / UNCLEAR
    confidence  REAL NOT NULL,      -- 0..1
    breadth     REAL,               -- frac of today's menu below its own open
    evidence    TEXT                -- JSON: streaks, drift, counters, medians
);
CREATE INDEX IF NOT EXISTS idx_day_regime_ts ON day_regime(ts);
