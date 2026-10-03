-- Wave schema v1 (SPEC.md §13). All timestamps are UTC ISO-8601 strings.
-- Migrations are applied in numeric order inside one transaction each and
-- are never destructive without asking first (hard rule 11).

CREATE TABLE orders (
    order_id        TEXT PRIMARY KEY,          -- broker order id
    client_order_id TEXT NOT NULL UNIQUE,      -- Wave idempotency key (§3)
    position_uuid   TEXT,                      -- owning PositionActor, if any
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,             -- buy / sell
    qty             REAL NOT NULL,
    order_type      TEXT NOT NULL,             -- market / limit / stop / stop_limit
    time_in_force   TEXT NOT NULL,
    limit_price     REAL,
    stop_price      REAL,
    status          TEXT NOT NULL,
    filled_qty      REAL NOT NULL DEFAULT 0,
    filled_avg_price REAL,
    trading_mode    TEXT NOT NULL,             -- paper / live (audit; type-enforced in code)
    submitted_at    TEXT,
    updated_at      TEXT,
    raw_json        TEXT
);
CREATE INDEX idx_orders_symbol ON orders(symbol);
CREATE INDEX idx_orders_position ON orders(position_uuid);

CREATE TABLE fills (
    fill_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    price         REAL NOT NULL,
    ts            TEXT NOT NULL,
    raw_json      TEXT
);
CREATE INDEX idx_fills_order ON fills(order_id);

CREATE TABLE positions (
    position_uuid  TEXT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,              -- long / short
    qty            REAL NOT NULL,
    avg_entry      REAL,
    state          TEXT NOT NULL,              -- PENDING_ENTRY..CLOSED/HALTED/ERROR (§3)
    strategy       TEXT,                       -- ORB / VWAP / GAP
    opened_at      TEXT,
    closed_at      TEXT,
    realized_pnl   REAL,
    exit_path      TEXT,                       -- which exit layer closed it (§8.2)
    trading_mode   TEXT NOT NULL
);
CREATE INDEX idx_positions_symbol ON positions(symbol);
CREATE INDEX idx_positions_state ON positions(state);

CREATE TABLE equity_snapshots (
    ts            TEXT PRIMARY KEY,
    equity        REAL NOT NULL,               -- cash + market value (§5 Performance)
    cash          REAL NOT NULL,
    market_value  REAL NOT NULL,
    trading_mode  TEXT NOT NULL
);

CREATE TABLE cashflows (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    amount REAL NOT NULL,                      -- + deposit, - withdrawal
    note   TEXT
);

CREATE TABLE log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    category     TEXT NOT NULL,                -- TRADE/ORDER/SCANNER/RISK/SYSTEM/ERROR/TRAINING
    level        TEXT NOT NULL,
    message      TEXT NOT NULL,
    json_payload TEXT
);
CREATE INDEX idx_log_ts ON log(ts);
CREATE INDEX idx_log_category ON log(category);

CREATE TABLE settings_versions (
    version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    scope       TEXT NOT NULL,                 -- e.g. "orb/open_drive"
    settings    TEXT NOT NULL,                 -- JSON of the parameter dataclass
    evidence    TEXT,                          -- why adopted (§8.3, §11)
    active      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE symbol_costs (
    symbol           TEXT NOT NULL,
    session_date     TEXT NOT NULL,            -- YYYY-MM-DD (ET session)
    regime           TEXT NOT NULL,            -- §6 session regime
    measured_spread  REAL,                     -- average, $ per share
    spread_pct       REAL,                     -- of price
    realized_slippage REAL,
    fill_quality     REAL,
    samples          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, session_date, regime)
);

CREATE TABLE candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    session_date  TEXT NOT NULL,
    ts            TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    features      TEXT NOT NULL,               -- JSON feature vector (§10.3)
    decision      TEXT NOT NULL,               -- accepted / rejected / traded
    reject_reason TEXT,
    outcome       TEXT,                        -- JSON outcome (filled in later)
    UNIQUE (symbol, session_date, strategy, ts)
);
CREATE INDEX idx_candidates_session ON candidates(session_date);

CREATE TABLE fee_schedule (
    fee_name       TEXT NOT NULL,
    effective_date TEXT NOT NULL,              -- YYYY-MM-DD
    rate           REAL NOT NULL,
    unit           TEXT NOT NULL,
    cap            REAL,
    exempt_below   REAL,
    note           TEXT,
    PRIMARY KEY (fee_name, effective_date)
);

-- Fees as data, not literals (§7). Re-verify each cycle.
INSERT INTO fee_schedule VALUES
  ('sec_section31', '2026-04-04', 20.60, 'usd_per_million_sold', NULL, NULL,
   'SEC Section 31 — $20.60 per $1M sold; variable, re-verify each cycle'),
  ('finra_taf',     '2026-01-01', 0.000195, 'usd_per_share_sold', 9.79, 50,
   'FINRA TAF — $0.000195/share sold, cap $9.79/trade, exempt <=50 shares; scheduled step-ups');

CREATE TABLE model_registry (
    model_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,                -- scanner_ranker, slippage, ...
    version      TEXT NOT NULL,
    artifact_path TEXT,
    validation   TEXT,                         -- JSON evidence (§9.2, §11.1)
    active       INTEGER NOT NULL DEFAULT 0
);
