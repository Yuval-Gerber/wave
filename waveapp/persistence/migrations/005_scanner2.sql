-- Scanner 2.0 shadow tables (2026-08-31 evening).
-- SHADOW ONLY: nothing here feeds entries until the S3 evidence switch.

-- every-minute top-of-market menu the new scanner WOULD serve
CREATE TABLE IF NOT EXISTS scanner2_menu (
    ts          TEXT NOT NULL,          -- UTC minute stamp
    rank        INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    score       REAL NOT NULL,
    rvol        REAL,                   -- time-anchored RVOL at this minute
    gap_pct     REAL,
    day_pct     REAL,                   -- vs today's open
    cum_volume  REAL,
    last_price  REAL,
    PRIMARY KEY (ts, rank)
);
CREATE INDEX IF NOT EXISTS idx_scanner2_menu_symbol ON scanner2_menu(symbol);

-- wider per-minute feature snapshots (top slice) — the ML training dataset
CREATE TABLE IF NOT EXISTS scanner2_snapshots (
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    features    TEXT NOT NULL,          -- compact JSON feature vector
    PRIMARY KEY (ts, symbol)
);

-- universe membership changes (2026-08-31: "make sure the scanner
-- knows if one stock is being added to alpaca or dropped")
CREATE TABLE IF NOT EXISTS scanner2_universe_log (
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    change      TEXT NOT NULL,          -- added / dropped
    detail      TEXT
);
