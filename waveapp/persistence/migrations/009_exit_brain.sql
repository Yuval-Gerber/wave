-- Blueprint §II.11 finding #2 (the meta-labeled EXIT gate) + kitchen plan
-- stage 5: every open-position MINUTE of every closed trade becomes one
-- training row for the future ML exit gate ("is there more profit ahead,
-- or is this the peak?"). Features come from the reconstructed minute path;
-- the label is a FORWARD triple-barrier from that minute: 1 if the position
-- later gains >= +0.25R more before retracing an additional 0.5R from the
-- current level, else 0. DATASET ONLY — no model reads this table yet and
-- nothing here gates a trade.
CREATE TABLE IF NOT EXISTS exit_brain_rows (
    position_uuid      TEXT NOT NULL,     -- positions.position_uuid
    symbol             TEXT NOT NULL,
    strategy           TEXT,              -- ORB / VWAP / GAP / ...
    minute_index       INTEGER NOT NULL,  -- minutes since entry (0 = entry bar)
    ts                 TEXT NOT NULL,     -- bar timestamp (UTC ISO-8601)
    unrealized_r       REAL,              -- close-based unrealized P&L, R units
    peak_r             REAL,              -- running max favorable excursion (R)
    retrace_frac       REAL,              -- 1 - unrealized/peak when peak>0 else 0
    minutes_since_peak INTEGER,           -- minutes since the peak was last extended
    vwap_dist_r        REAL,              -- price - session VWAP, in R units
    vol_ratio          REAL,              -- this bar's volume / entry-bar volume
    label_more_ahead   INTEGER,           -- forward triple-barrier label (1/0)
    created_at         TEXT NOT NULL,     -- row write time (UTC ISO-8601)
    PRIMARY KEY (position_uuid, minute_index)
);
CREATE INDEX IF NOT EXISTS idx_exit_brain_symbol ON exit_brain_rows(symbol);
CREATE INDEX IF NOT EXISTS idx_exit_brain_strategy ON exit_brain_rows(strategy);
