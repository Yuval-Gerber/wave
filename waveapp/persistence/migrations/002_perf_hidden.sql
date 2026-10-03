-- 2026-08-19: per-trade removal from the Performance graph.
-- A hidden trade disappears from the curve/stats but STAYS in the DB —
-- positions rows are the §10.3 training dataset and are never deleted.
ALTER TABLE positions ADD COLUMN perf_hidden INTEGER NOT NULL DEFAULT 0;
