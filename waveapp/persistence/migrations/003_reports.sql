-- 2026-08-23 (weekend agenda #4): weekly reports live in the DB so
-- past ones stay browsable from the Performance tab's Reports page.
-- kind: 'week_close' (Friday after the bell) | 'week_ahead' (Monday 09:00 ET)
CREATE TABLE reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date  TEXT NOT NULL,               -- the Monday of the ISO week it covers
    kind         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    content      TEXT NOT NULL,               -- the plain-English report body
    json_payload TEXT,
    UNIQUE (report_date, kind)
);
CREATE INDEX idx_reports_date ON reports(report_date);
