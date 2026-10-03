-- THE DUEL (2026-09-20): which brain manages each position —
-- 'kitchen' (Master Key) or 'judge' (the Position Judge). Scores the
-- head-to-head on the Duel tab and scripts/ab_scoreboard.py.
ALTER TABLE positions ADD COLUMN manager TEXT NOT NULL DEFAULT 'kitchen';
