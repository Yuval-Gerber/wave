# SPEC.md — Wave

**Project:** Wave — an autonomous US-equity daytrading bot with a native-feeling PyQt6 desktop dashboard for macOS (Apple Silicon).
**This file is the design contract.** If code and this file disagree, this file wins. Section numbers (§) are referenced throughout the codebase and never renumber.

---

## 0. Non-negotiable hard rules

1. **No live order without the Phase 11 gate.** Until the live gate is explicitly approved (Touch ID), every broker call runs against paper endpoints only. Live API keys must never appear in code, logs, or commits.
2. **Never weaken, bypass, or comment out a risk check** (stops, daily loss halt, kill switch, position limits, TradeGate), not even "temporarily for testing." Tests mock around them, never remove them.
3. **Every position must always have a server-side stop-loss order resting at the broker** from the moment the entry fills. If the app crashes, the broker still protects the position.
4. **The build log (memory.md, kept outside this repo) is append-only.** History is never rewritten.
5. **One phase at a time.** Complete a phase, verify it, commit, then start the next.
6. **Never store secrets in files.** All keys and passwords live in macOS Keychain via `keyring`. Config files reference Keychain entries by name only.
7. **Paper and live are separated at the type level** (distinct `TradingMode` enum threaded through every broker call), never by a string flag that can silently default.
8. **The scanner is a ranker, never a price predictor.** No component may be framed as "predicting where price goes." ML predicts *tradability* (will this symbol produce a clean, tradable move today); rules decide direction. See §9.
9. **No parameter set or model reaches live trading without passing the statistical adoption bar in §11.** No exceptions, including "it looks obviously better."
10. **Paper P&L is never evidence.** Alpaca's paper engine fills against NBBO without checking size, gives random partial fills ~10% of the time, and charges no borrow/margin costs. Paper validates logic and plumbing only. Live evidence starts at minimum size in Phase 11.
11. **If a command or migration could lose data or money, stop and confirm first.**

---

## 1. What Wave is

Wave trades US-listed stocks and ETFs through Alpaca's Trading API, nearly around the clock (24/5: Sunday 20:00 ET through Friday 20:00 ET including pre-market, post-market and overnight), long and short, fully automatically. It hunts many small wins (0.3–0.5% gross target zone, ATR-scaled per symbol) using evidence-backed entry strategies, an adaptive multi-layer exit system, and a cost gate that refuses any trade whose expected move doesn't clear measured costs. It runs in paper mode until it statistically proves itself, then unlocks live mode behind a Touch ID gate and a staged capital ramp.

Global exposure comes through US-listed ADRs and ETFs of foreign companies, not foreign exchanges. An IBKR adapter is built into v1 behind the broker abstraction but stays dormant until capital justifies foreign-exchange fees; enabling it later must require zero engine changes.

**Why Alpaca first:** commission-free US stocks via API, accepts non-US persons (W-8BEN), 24/5 trading and 24/5 paper with identical APIs. A small account cannot survive per-order commission minimums anywhere else.

**The honest premise Wave is built on:** peer-reviewed evidence (Barber & Odean 2000; Chague et al. 2020 — 97% of persistent day traders lost money) shows the small profitable minority differ in three designable ways: ruthless cost discipline, extreme selectivity, and risk asymmetry (cut losers fast, let winners run). Wave encodes exactly these as the TradeGate (§8.4), the scanner ranker (§9), and the adaptive exit system (§8.2).

---

## 2. Tech stack (locked)

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| GUI | PyQt6 + qasync (asyncio loop integrated with Qt) |
| Broker (primary) | `alpaca-py` (trading + market data streams) |
| Broker (dormant v1 adapter) | `ib_async` + IB Gateway + IBC |
| Historical data | Polygon.io (US intraday bars + news); `yfinance` only in research notebooks, never in the engine |
| ML (scanner ranker) | LightGBM (fallback XGBoost), scikit-learn for CV tooling |
| Fast research backtests | vectorbt (parameter sweeps / "is there any edge at all") |
| High-fidelity simulation | Wave's own event-driven simulator (§10.2) sharing the live engine's code paths |
| Database | SQLite (WAL mode), one file per environment: `wave_paper.db`, `wave_live.db` |
| Charts | `finplot` (pyqtgraph-based) for live candles; pyqtgraph for performance graph and scanner canvas |
| Secrets | `keyring` → macOS Keychain |
| Touch ID | `pyobjc-framework-LocalAuthentication` (`LAContext`), password fallback |
| Telegram | `python-telegram-bot` |
| Packaging | PyInstaller (onedir BUNDLE), ad-hoc codesigned, installed locally as a .app — personal use on one Mac, so no Developer ID, notarization or DMG |
| Testing | pytest + pytest-asyncio; every phase ships with tests |

No heavyweight trading framework for the live engine. Wave's engine is custom and thin — deliberate, for control, license cleanliness, and fit with the custom UI. vectorbt is research-only tooling.

---

## 3. Architecture

```
┌─────────────────────────── Wave.app ───────────────────────────┐
│  UI (PyQt6)  ←Qt signals→  Engine Core (asyncio)               │
│                                                                 │
│  Engine Core                                                    │
│   ├── SessionScheduler      (session regimes incl. midday lull  │
│   │                          and power hour, holidays, DST,     │
│   │                          the Fri–Sun dead zone)             │
│   ├── Scanner               (heuristic ranker → ML ranker, §9)  │
│   ├── TradeGate             (cost gate, §8.4)                   │
│   ├── RiskEngine            (global limits, kill switch, SSR &  │
│   │                          LULD handlers, §12)                │
│   ├── PositionActor × N     (one independent task per position, │
│   │                          owns its exits, §8)                │
│   ├── BrokerAbstraction                                          │
│   │     ├── AlpacaAdapter   (active)                            │
│   │     └── IBKRAdapter     (built, dormant)                    │
│   ├── DataHub               (websocket feeds, bar building,     │
│   │                          heartbeat/staleness watchdog)      │
│   ├── Persistence           (SQLite: orders, fills, positions,  │
│   │                          equity curve, log, settings, and   │
│   │                          the per-trade feature+outcome      │
│   │                          training dataset)                  │
│   └── TelegramBridge        (status + safe remote commands)     │
└─────────────────────────────────────────────────────────────────┘
```

Rules:
- The UI never talks to brokers directly; it observes engine state via Qt signals and issues commands through a single `CommandBus`.
- Every `PositionActor` is an independent asyncio task with its own state machine: `PENDING_ENTRY → OPEN → SCALING_OUT → CLOSING → CLOSED | HALTED | ERROR`. One actor failing must never affect another. An actor crash triggers an immediate protective check that the server-side stop still exists.
- **Pause vs Stop:** Pause = no new entries; existing PositionActors keep managing exits; connections stay up. Stop = no new entries, actors run until every position is flat, then the engine idles; connections stay up so open positions are never abandoned. Kill switch = flatten everything now (marketable limits; market orders allowed only in RTH and never on a halt resume), then halt.
- Idempotency: every order carries a client order ID derived from `(position_uuid, action, attempt)`. On reconnect, reconcile broker state against the DB before doing anything else; never re-send blindly.
- Clock discipline: all timestamps UTC in the DB, exchange-local only at the UI edge. Assert NTP sanity at startup.

---

## 4. Security spec

- **Login:** Touch ID (LAContext) or password (argon2 hash in Keychain). Failed attempts are logged; no lockout (removed deliberately — single-user machine).
- **Touch ID hard gates** (biometric or password re-prompt, no session carry-over): switching paper→live, editing risk settings, revealing/rotating API keys, kill switch from the UI, approving Phase 11, re-arming after a weekly-loss halt.
- Secrets only in Keychain. `~/Library/Application Support/Wave/config.toml` holds settings and Keychain entry names, never secrets.
- Telegram: bot token in Keychain; commands accepted only from the configured Telegram user ID; dangerous commands (pause/stop/kill) require replying to a one-time confirmation code; live/paper switching is **not** available over Telegram at all.
- All broker traffic over TLS (SDK default); certificate errors are fatal, never bypassed.
- The app is **ad-hoc codesigned** (`codesign -s -`) — Developer ID + notarization deliberately skipped (personal use on one Mac, decision 2026-08-03). Known consequence: the ad-hoc signature changes on each rebuild, so macOS may re-prompt for Keychain access after rebuilds; the password fallback always exists if Touch ID is unavailable in a given build form.

---

## 5. UI spec

**General — Apple design language (decision 2026-08-14):** Wave must look and feel like a native Apple/macOS app (HIG), light appearance, while keeping every animation and behavior already specced. Concretely:
- **Palette:** Apple HIG light-mode approximations — accent systemBlue `#007AFF` (pressed `#0071E3`), systemOrange `#FF9500` (LIVE), systemRed `#FF3B30`, systemGreen `#28CD41`, systemGray `#8E8E93`, label `#1D1D1F`, secondary label `#6E6E73`, window `#F5F5F7`, content/controls white, sidebar source-list gray `#EDEDF2`, hairline separators `rgba(0,0,0,0.10)`. Brand mark (icon wave + triangle) stays ocean blue `#0077BE`.
- **Type:** system font (SF Pro via `-apple-system`), 13px base.
- **Controls:** macOS idioms — segmented control for paper/live, filled-accent primary buttons (radius 7), bordered secondary buttons, focus rings in accent blue, source-list sidebar with selection wash `rgba(0,122,255,0.14)`.
- **Tooltips:** iOS-notification style — translucent gray `rgba(235,235,240,0.97)`, radius 10, dark text.
- **Materials:** real Apple glass/vibrancy via **`pyqt-liquidglass`** (MIT, PyQt6 — native `NSGlassEffectView` (Liquid Glass, macOS 26+) with `NSVisualEffectView` fallback). Applied to sidebar, tooltips/popovers, modal cards.
Generous whitespace, slick and minimal. Window fully resizable with a sensible minimum size; layouts reflow, never overlap and never scroll — if content can't fit, it becomes another tab or a paginated view. All modals are centered popup cards with dimmed backdrop and subtle scale/fade animation.

**Top bar:** animated mono-line wave logo (left) — a single stroked path with states: calm slow wave (idle/connected), energetic wave (trades in progress), flatline slow pulse (paused), red jagged wave (error), grey dotted wave (no connection). Custom QWidget painting the path with QPainter driven by QVariantAnimation (no GIFs). Next to it: paper/live toggle (live locked behind Touch ID; paper = blue, live = deep orange with persistent "LIVE" badge). Right: Start button, and a Pause/Stop button (first click = Pause with icon morph animation, second click = Stop; semantics per §3).

**Left sidebar:** icon-only rail (~56px) of monochrome line icons; expands on hover (~200px, animated) showing labels and connection status dots (Alpaca, data feed, Telegram, DB).

**Tabs:**
1. **Positions** — grid of position cards: symbol, side, qty, entry, live P&L ($ and %), current stop level, exit-stage badge (BE / TRAIL / SCALED), strategy tag (ORB / VWAP / GAP), SSR/halt badge if active. Double-click opens a large centered popup: live candlestick chart (finplot) with entry/stop/target lines, order history, manual overrides (close now, tighten stop) each behind a confirm dialog. Pagination dots when cards exceed the grid.
2. **Performance** — equity curve of **account value (cash + market value of positions)** so buys never dip the graph; markers for sells (green profit / red loss); deposits and withdrawals as vertical reference lines; stat strip: win rate, profit factor, expectancy (R), max drawdown, fees paid, trades today, and the current champion parameter-set version.
3. **Scanner** — animated "Wave's mind": particle/graph canvas (pyqtgraph) where nodes light up on real scanner events (universe refresh, candidate found, candidate rejected by TradeGate with reason, promoted to entry). Below: live candidate table with per-strategy scores, spread%, RVOL, and the accept/reject reason string; scanner controls (universe filters, min relative volume, max spread% of target, session toggles, heuristic/ML mode indicator).
4. **System** — connection health, feed latency, heartbeat ages, engine task states, DB size, version, session countdowns, fee-schedule constants currently in force.
5. **Log** — virtualized table over the SQLite log: full-text search (word or fragment), category filter (TRADE / ORDER / SCANNER / RISK / SYSTEM / ERROR / TRAINING), date-range filter with a themed QCalendarWidget popup, multi-select rows (and select-all-filtered) to delete or export; export selected or all to a standalone `.sqlite` file.
6. **Settings** — nested sub-tabs: Trading (per-strategy, per-session parameters), Risk (Touch ID gated), Scanner, Connections (Keychain-backed keys, test buttons), Telegram, Appearance, Backup/Restore (export/import config + DB snapshot for migrating to a new Mac).

---

## 6. Session model

`SessionScheduler` exposes the current regime as an enum:
`PRE` (04:00–09:30 ET) → `OPEN_DRIVE` (09:30–10:30) → `MIDDAY` (10:30–15:00, with the 11:30–13:30 lull flagged) → `POWER_HOUR` (15:00–16:00) → `POST` (16:00–20:00) → `OVERNIGHT` (20:00–04:00; Sun 20:00 opens the week) → `CLOSED` (Fri 20:00 → Sun 20:00, holidays).

Volume and volatility are U-shaped across the day (highest at open and close, dead midday); every strategy, exit and risk parameter set is keyed by regime, and strategies arm/disarm by regime (§8.1). Overnight facts baked in: limit orders only, structurally wide spreads (~7¢/share wider than regular hours on the overnight venue), thin liquidity, overnight-eligible symbols only. **Overnight is exit/risk-management only in v1 — never an alpha source — until live evidence proves otherwise.** The scheduler also drives maintenance windows (nightly reconcile, DB checkpoint, model/drift checks) inside `CLOSED`, and handles DST shifts explicitly.

---

## 7. Universe & costs

- **Universe:** US common stocks + liquid ETFs/ADRs above price and average-volume floors; overnight-eligibility tracked from broker asset metadata. Default bias: liquid mid/large-caps, where spreads run ~1–5 bps — a round trip consumes only ~10–15% of a 30–50 bps profit target. Small/low-float names (spreads ~36–50+ bps, which alone can exceed the whole target) are admitted **only** when the expected move is large enough to clear their spread (e.g., a strong gapper), at reduced size.
- **Fees as data, not literals:** regulatory fees are configurable constants with effective dates in the DB: SEC Section 31 ($20.60 per $1M sold, effective 2026-04-04; variable — re-verify each cycle) and FINRA TAF ($0.000195/share sold, cap $9.79, exempt ≤50 shares, scheduled annual step-ups). Trivial per trade at Wave's size but they belong in the cost model.
- **Per-symbol cost ledger:** Wave continuously records measured spread, realized slippage and fill quality per symbol per session in `symbol_costs`, updating tradability daily.

---

## 8. Strategy & adaptive exit framework (the heart)

### 8.1 Entry families (evidence-backed, session-gated)
Each is a pluggable `Strategy` class producing `EntrySignal(symbol, side, confidence, reason)`. Direction always comes from rules; ML only gates and sizes (§9). Parameters per (strategy, session regime).

1. **ORB-5 on Stocks-in-Play** — arms in `OPEN_DRIVE` only. Candidates: top names by opening relative volume (RVOL ≥ 2–3× normal), ideally with a catalyst. After the first 5-minute bar, long on a break above the range high, short below the range low, initial stop at the opposite extreme (then the exit system takes over). The 5-minute variant with the relative-volume filter is the documented sweet spot (Zarattini/Barbon/Aziz 2024: portfolio of top-20 stocks-in-play, 2016–2023, Sharpe ~2.8 — treat as an upper bound, not an expectation). The stock-selection component carries the edge; never run ORB on ordinary low-volume names. Shorts only when SSR is not active on the symbol.
2. **VWAP regime-switch** — arms all day, half size in the midday lull. A regime detector (opening-range size, overnight gap magnitude, opening volume vs rolling baseline) classifies the day: *trend day* → trade with VWAP (long above / short below, pullback entries toward VWAP); *range day* → fade stretched extensions back toward VWAP with fading volume. Wrong-regime trading is the documented failure mode, so when the detector is low-confidence, the strategy stands down.
3. **Gap-and-Go momentum** — arms in `PRE`→`OPEN_DRIVE`, re-arms in `POWER_HOUR`. Requires: gap ≥ 3%, pre-market volume ≥ 100k shares, RVOL ≥ 3×, a real catalyst flag (news feed), spread cleared by expected move. Power-hour continuation additionally leans on the documented market intraday momentum effect (strong first half-hour predicts the last half-hour, strongest on high-volume/high-volatility days — Gao/Han/Li/Zhou 2018).

**Short book:** structurally smaller and stricter than longs — SSR-aware (§12), borrow-cost-aware (live borrow fees exist; paper shows none), squeeze-risk capped by tighter size limits. New strategies must be addable without touching the engine.

### 8.2 The adaptive exit system (per PositionActor)
Layers, all active simultaneously; the binding constraint wins. All stop math is ATR-based (fast intraday ATR, e.g. ATR(14) on 1-min bars, parameterized) so it adapts to each stock's volatility.

1. **Hard stop (server-side, always):** part of a bracket at entry, distance `k_stop × ATR` (start ~1.5×). The disaster floor; lives at the broker.
2. **Breakeven ratchet:** at unrealized profit ≥ `k_be × ATR`, amend the server-side stop to entry ± fees buffer. A winner can no longer become a loser.
3. **Volatility trailing stop (chandelier-style):** trails `k_trail × ATR` below the highest price since entry (mirror for shorts), ratcheting one direction only. Intraday baseline `k_trail ≈ 2.0` on short bars, per-regime tuned. The engine computes the trail and amends the server-side stop upward on meaningful moves (throttled), so the broker's protective order always reflects the trail.
4. **Momentum exit:** exit immediately (marketable limit) when the push dies — bar volume collapses below a fraction of entry-time volume, price closes back through VWAP against the position, or an opposing signal fires. This is the "bank +0.3% before it falls" behavior.
5. **Scale-out:** at the first target (`k_t1 × ATR`, roughly the 0.5% zone), sell a configurable fraction (default 50%), move stop to breakeven on the rest, let the trail hunt the bigger move.
6. **Time stop:** flat-ish after `T_max` minutes (regime-keyed) → exit; capital must not sit dead.
7. **Session-boundary rule:** flatten before `CLOSED` (weekend/holiday). Holding over the weekend is forbidden in v1.

### 8.3 Parameter discipline
Every `k_*`, `T_*`, threshold and filter lives in one dataclass per (strategy, regime), stored in Settings, versioned in `settings_versions`. Parameters change only through the training pipeline (§10–§11). Every adoption is logged with the evidence that justified it.

### 8.4 TradeGate (the cost gate — first-class, checked before every entry)
Refuse the trade unless:
`expected_move (from signal stats, ATR-scaled) ≥ (measured spread + live-calibrated slippage buffer + regulatory fees + borrow fee if short) × safety_multiple` (default 3×).
Additional gates: symbol's spread% must be < ~15% of the profit target for the default universe (wide-spread names only via the big-expected-move exception); overnight requires overnight-eligibility, limit-only, and reduced size caps. Every rejection is logged with its reason (feeds the Scanner tab and the training dataset).

---

## 9. Scanner: heuristic first, then an ML ranker

### 9.1 Heuristic ranker (ships first)
Score = f(RVOL, gap%, ATR%, catalyst flag, prior-day pattern, sector momentum), penalized by spread% and float risk. Top-N per strategy pass to TradeGate. Every symbol-day's features and outcome are logged regardless of whether it was traded — this log **is** the future training set.

### 9.2 ML ranker (activates only after ≥ a few thousand labeled symbol-days exist)
- **Model:** LightGBM (gradient-boosted trees dominate tabular financial data at solo-dev scale; no deep learning).
- **Task:** learning-to-rank / probability of **tradability** — "will this symbol, today, produce a clean move with adequate range and tight enough spread to hit the target under the exit system?" Never direction.
- **Labels:** triple-barrier (López de Prado) applied intraday — upper barrier = ATR-scaled profit target, lower = stop, vertical = time stop/session end; first barrier touched sets the label.
- **Meta-labeling:** the rule-based strategy fires side; the model outputs P(clean tradable move) → trade/skip decision and size scaling. This is the highest-leverage ML pattern for Wave: it buys selectivity.
- **Features:** relative volume (pre-market and opening), gap%, ATR%, spread% (the cost feature), float, short interest (slow, bimonthly — treat accordingly), catalyst/news flags (Polygon news), pre-market volume and range, sector/peer momentum, prior-day pattern, price bucket, session regime. Nothing requiring order-book depth or institutional tick data.
- **Validation:** purged k-fold CV with embargo (mandatory — plain k-fold leaks on overlapping intraday labels), sample-uniqueness weighting, and the §11 adoption bar. The ML ranker replaces the heuristic **only** if it beats it on out-of-sample precision@top-N.
- **Drift & retraining:** rolling-window retrain (starting cadence: monthly on trailing 6–12 months, tuned by walk-forward); a rolling precision/AUC monitor on live scanner hit-rate; material drops below the validation band trigger investigation and possible off-cycle retrain. Do not overfit the retrain cadence itself.

---

## 10. Training & backtesting infrastructure

### 10.1 Fast research layer (vectorbt)
Parameter sweeps over Polygon intraday history to answer "is there any edge at all" cheaply. Anything that survives moves to 10.2. vectorbt results are never adoption evidence by themselves.

### 10.2 High-fidelity simulator (Wave's own, sharing live code paths)
Event-driven replay through the same PositionActor/RiskEngine/TradeGate code that trades live. Models: spread crossing (half-spread for marketable limits, full for market), slippage as a function of symbol class, session and participation, limit-order queue realism (a limit at the touch is not a guaranteed fill), partial fills, LULD halts and SSR days as state changes, and the overnight limit-only regime. The cost model's slippage numbers are calibrated from live fills once they exist (§11.2) and re-calibrated continuously.

### 10.3 The trade journal is the dataset
For every trade AND every rejected candidate: full feature vector, context (regime, spread, RVOL...), decision, outcome, exit path taken, realized costs. Stored in SQLite, exportable. This powers the ML ranker, the slippage model, and every future improvement.

---

## 11. The adoption bar & the paper→live protocol

### 11.1 Statistical adoption bar (auto-applied; all must pass before ANY parameter set, strategy or model is promoted)
- Rolling (not anchored) walk-forward, ~3:1 in-sample:out-of-sample, ≥30 trades per free parameter in-sample per window.
- ≥100 out-of-sample trades total for the candidate.
- Deflated Sharpe Ratio > 0 at 95% confidence, adjusted for the number of configurations tried.
- Probability of Backtest Overfitting (PBO) < 0.5.
- Monte Carlo resampling of the trade sequence: 5th-percentile outcome still acceptable (positive expectancy, drawdown within limits).
- Parameter stability: chosen values sit on plateaus (±10% across windows), never isolated peaks.
- **Champion/challenger:** the incumbent (champion) runs most of the allocation; a challenger runs a small capped allocation; promotion only after beating the champion on this bar over a minimum sample. Failing any single criterion = no adoption.

### 11.2 Live gate and staged ramp (Phase 11)
Live stays locked until ALL hold on the high-fidelity simulator + 24/5 paper (paper for logic only, simulator for economics):
- ≥30 distinct sessions and ≥200 closed trades through the full pipeline,
- profit factor ≥ 1.3 after modeled costs, expectancy ≥ +0.1R, max drawdown < 10%,
- zero unreconciled order/position incidents in the last 10 sessions.
Then: review the evidence → Touch ID approval → live keys entered into Keychain manually → **Stage L1:** minimum-size live orders for ≥5 sessions ("plumbing proof") while fitting the slippage model per symbol-class/session from the first ~30–50 real fills → **Stage L2:** normal small-account sizing only if live net expectancy, hit rate and drawdown match the validated band within tolerance → further capital tranches each gated the same way. If live slippage exceeds 2× the modeled values on the core universe, shrink the universe and re-calibrate before continuing.

---

## 12. Risk engine (global, above all actors)

- Position size: fixed-fractional — risk per trade (distance to hard stop) ≤ **1% of equity**; overnight ≤ 0.5%; short book additionally capped (e.g., ≤50% of long-book limits).
- Max concurrent positions (scales with equity, setting-gated).
- Max daily loss **3% of equity** → entries halted for the day (open positions still managed to exit), Telegram alert, manual re-arm next session. Max weekly loss 6% → halted until re-armed in-app (Touch ID).
- Kill switch (UI + Telegram-with-confirmation): flatten all, cancel all, halt.
- **SSR handler:** detect Rule 201 trigger (−10% intraday from prior close); while active (rest of day + next day), disable downtick-dependent short entries on that symbol and require short executions above the national best bid.
- **LULD halt handler:** track Limit States and 5-minute pauses (bands double 09:30–09:45 and 15:35–16:00). A held position that halts → actor enters `HALTED`; never send market orders into a resume; exit via pre-sized limit orders on the reopening auction; never chase a price approaching a band; pauses in the last 10 minutes resolve at the closing auction.
- Consistency checks each minute: DB positions == broker positions; mismatch → freeze entries, alert, reconcile.
- Data staleness: feed heartbeat over threshold → freeze entries, rely on server-side stops until healthy.

---

## 13. Database & Telegram

SQLite schema (numbered SQL migrations, applied automatically, never destructive without confirmation): `orders`, `fills`, `positions`, `equity_snapshots`, `cashflows`, `log` (ts, category, level, message, json_payload), `settings_versions`, `symbol_costs`, `candidates` (the per-symbol-day feature+outcome journal, §10.3), `fee_schedule` (constants with effective dates, §7), `model_registry` (ML versions + validation evidence).

TelegramBridge: `/status`, `/positions`, `/pnl`, `/pause`, `/resume`, `/stop`, `/kill` (the last four require the confirmation-code reply), push alerts for fills, halts, SSR/LULD events, risk halts, errors, daily summary. No live/paper switching via Telegram.

---

## 14. Build log

A separate append-only build log (memory.md, kept out of the public repo) records every meaningful unit of work: what was done, how it was tested, problems hit and how they were solved, and the overall state after the change. If anything goes south, the log is the map back.

---

## 15. Commit & rollback discipline

1. `main` only; every completed step is one commit, message `phase-<n>.<step>: <summary>`.
2. Finish a step → run its tests → log it → commit. Phase completions are tagged `phase-<n>-done`.
3. Rollback is `git revert` (history-preserving) by default; `git reset --hard` only as a deliberate, logged exception. Never force-push, never rebase published history.
4. The full test suite runs before any new work after a rollback.

---

## 16. Build phases

Each phase = deliverable + tests + demo instructions + build-log entries + commit(s).

- **Phase 0 — Repo & protocol bootstrap:** git init, skeleton, build log created, pre-commit hooks, this SPEC.md committed, test harness green.
- **Phase 1 — App shell & security:** PyQt6 shell (ad-hoc signed, runnable both as dev script and as .app), login (Touch ID + password), Keychain wiring, top bar with animated logo states, hover sidebar, empty tabs, resize behavior proven.
- **Phase 2 — Broker abstraction & paper connection:** `BrokerAbstraction`, AlpacaAdapter against paper (account, clock, assets, orders, streams), IBKRAdapter skeleton compiling behind the same interface (dormant), TradingMode type separation, status dots live.
- **Phase 3 — DataHub & database:** websocket data, bar building, heartbeat watchdog, full schema + migrations (including `candidates` and `fee_schedule`), logging pipeline.
- **Phase 4 — Telegram bridge:** per §13.
- **Phase 5 — Engine core, PositionActors & RiskEngine:** state machines, bracket entry with server-side stop, pause/stop semantics, kill switch, SSR + LULD handlers, reconciliation, idempotent orders. Paper demo with tiny test entries.
- **Phase 6 — Adaptive exit system:** all seven layers with regime-keyed parameters, stop-amendment throttling, unit tests on synthetic paths (must include: reversal at +0.3% banks profit; runner reaches trail; volume-death exit; breakeven holds; halt during hold).
- **Phase 7 — Scanner (heuristic) & TradeGate:** §9.1 + §8.4, candidate journal logging every accept/reject, candidate table live.
- **Phase 8 — Full UI:** Positions cards + chart popup, Performance graph, Scanner mind animation, System, Log (search/filter/calendar/multi-select/export), Settings with nested sub-tabs and Touch ID gates.
- **Phase 9 — Packaging & installer:** PyInstaller BUNDLE, ad-hoc codesign, install locally as a .app; Backup/Restore migration; fresh-Mac install checklist.
- **Phase 10 — Training campaign:** build the 10.1 research layer and 10.2 simulator; sweep the three strategy families on Polygon history; walk-forward per §11.1; explicitly sweep the exit-decision cadence (1-min baseline vs 30s/15s bars) as a parameter — adopt faster cadence only if it beats 1-min out-of-sample after whipsaw costs (the hard stop is tick-level at the broker regardless, so cadence is an optimization knob, not a safety one); run 24/5 paper continuously for logic validation while the simulator produces the economic evidence; once the candidate journal is large enough, train the ML ranker (§9.2) and test it against the heuristic; weekly report of §11.2 metrics. No engine feature changes during the campaign except bug fixes, each logged.
- **Phase 11 — Live gate & staged ramp:** verify §11.2, present evidence, Touch ID approval, Stage L1 minimum-size plumbing proof + slippage calibration, Stage L2 ramp, tranche-gated growth.
- **Phase 12 — Profit optimization campaign:** once live evidence exists, continuously optimize Wave toward maximum net profitability — parameter re-tuning, strategy expansion, universe widening, cost reduction (slippage/spread work), and trade-frequency scaling — but ONLY through the §11 adoption bar and champion/challenger process. "Most profitable" always means most profitable *on out-of-sample evidence after costs*; any optimization that can't beat the champion statistically is discarded, no matter how good it looks in-sample.

---

## 17. Key evidence behind this spec

Zarattini, Barbon & Aziz (SSRN 4729284) — ORB on stocks-in-play; Zarattini & Aziz (SSRN 4631351) — VWAP day-trading systems; Gao, Han, Li & Zhou, *Market Intraday Momentum*, JFE 2018; Barber & Odean, JoF 2000 and Chague et al. (SSRN 3423101) — why cost discipline, selectivity and risk asymmetry are the design pillars; López de Prado, *Advances in Financial ML* — triple-barrier, meta-labeling, purged CV, DSR/PBO; Lim (SSRN 6610883) — overnight venue spread premium; Nasdaq economist spread data by cap tier; FINRA/SEC fee advisories. Headline strategy returns in these papers are upper bounds from the authors' own backtests — Wave's bar is §11, not those numbers.
