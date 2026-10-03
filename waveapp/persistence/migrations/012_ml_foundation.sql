-- M0 — the ML MASTER PLAN's data foundation (2026-09-23).
-- OBSERVATION ONLY: nothing in this migration is read by trading logic;
-- these tables feed the entry brain (M1) and the judge's advisor (M2).

-- One row per Position Judge stance FLIP (and per ACT, to_stance='ACT:…'),
-- written by the shadow kitchen's _judge_second via the monitor's db closure.
-- FRAME SPACE NOTE: profit/peak/giveback/mfe/mae and the forward px_* columns
-- live in the judge's normalized frame (a short's tape reflected around its
-- entry: px' = 2*entry − px), so "up" always means MORE PROFIT and shorts
-- compare like longs. px and entry_px stay REAL prices.
CREATE TABLE IF NOT EXISTS judge_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,      -- UTC ISO at the flip/act second
    symbol          TEXT NOT NULL,
    position_key    TEXT NOT NULL,
    side            TEXT NOT NULL,      -- 'long' / 'short'
    from_stance     TEXT NOT NULL,
    to_stance       TEXT NOT NULL,      -- stance, or 'ACT:<act label>'
    reason          TEXT,               -- the judge's own why-line
    profit_atr      REAL,               -- frame profit at the flip, ATR units
    peak_profit_atr REAL,
    giveback_atr    REAL,               -- peak_profit_atr − profit_atr
    signs           INTEGER,            -- death signs 0-3 at the flip
    failed_highs    INTEGER,
    peak_age_s      REAL,
    vol_ratio       REAL,               -- vol60 / peak_vol60 (NULL: no peak vol)
    atr_px          REAL,               -- one position-ATR in dollars
    px              REAL,               -- REAL price at the flip
    entry_px        REAL,               -- REAL entry
    close_guard     INTEGER,            -- 0/1 — inside the closing window
    dwell_ms        INTEGER,            -- flip: candidate_since→flip; act: flip→act
    regime          TEXT,               -- SessionScheduler regime (NULL if unknown)
    -- forward outcomes, UPDATEd later from the live tape (FRAME prices; the
    -- honest rule: filled only while the symbol's tape still reaches the
    -- kitchen — a symbol that vanishes first leaves NULLs, never stale fills)
    px_30s          REAL,
    px_2m           REAL,
    px_5m           REAL,
    mfe_2m_atr      REAL,               -- best frame excursion vs flip px, ATRs
    mae_2m_atr      REAL                -- worst frame excursion vs flip px, ATRs
);
CREATE INDEX IF NOT EXISTS idx_judge_transitions_ts ON judge_transitions(ts);
CREATE INDEX IF NOT EXISTS idx_judge_transitions_symbol ON judge_transitions(symbol);

-- Entry telemetry (catch-the-climb as permanent columns): one row per FILLED
-- entry that came through the live signal pipeline — detection→signal→fill.
-- Adopted (restart) positions have no signal stamp and write no row.
CREATE TABLE IF NOT EXISTS entry_lag (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                 TEXT NOT NULL,
    side                   TEXT NOT NULL,   -- 'long' / 'short'
    strategy               TEXT,
    ts_first_watch         TEXT,            -- first watch/promotion today (UTC ISO)
    ts_signal              TEXT,            -- AUTO ENTRY moment (UTC ISO)
    ts_fill                TEXT NOT NULL,   -- entry fill (UTC ISO)
    pct_above_open_at_fill REAL,            -- fill vs the session open (NULL unknown)
    lag_watch_to_signal_s  REAL,
    lag_signal_to_fill_s   REAL
);
CREATE INDEX IF NOT EXISTS idx_entry_lag_fill ON entry_lag(ts_fill);

-- Nightly M0 rollup: one summary row per session day (UTC-date bucketed),
-- written in the CLOSED window by the monitor's nightly loop.
CREATE TABLE IF NOT EXISTS ml_daily (
    date                     TEXT PRIMARY KEY,  -- ET session date
    n_transitions            INTEGER,
    n_acts                   INTEGER,
    median_dwell_ms          INTEGER,
    n_entries                INTEGER,
    median_watch_to_signal_s REAL,
    n_short_candidates       INTEGER
);
