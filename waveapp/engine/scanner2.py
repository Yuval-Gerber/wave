"""Scanner 2.0 — full-market awareness, S1 SHADOW stage (2026-08-31).

The old scanner froze a 20-name day list at 9:28 off a broken volume ruler;
on 2026-08-31 the champion rules made +$902 on the day's real winners and
−$325 on the frozen menu. Scanner 2.0 watches EVERY asset tradable at the
broker (13,478 counted 2026-08-31), re-ranks continuously 4:00–16:00 ET with
a time-anchored RVOL (today's cumulative volume ÷ the symbol's own 20-day
average cumulative volume AT THE SAME MINUTE OF DAY — the SMB/Trade-Ideas
measure; the ORB paper's edge is monotonic in exactly this quantity), and
journals every minute's menu for the §11 evidence comparison.

S1 is SHADOW ONLY: it trades nothing, feeds no entries, and shares no state
with the live entry pipeline. Its outputs are the scanner2_* tables and log
lines — the daily old-menu-vs-new-menu scoreboard is the point.

Memory discipline (8GB Air): one struct-of-arrays numpy table for the whole
universe (~2.6MB), one baseline-curve matrix (~39MB float32), no per-symbol
Python objects in the hot path, batched SQLite writes once per minute.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time as _time_mod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np

from waveapp.config import support_dir
from waveapp.instruments import is_leveraged_name, leveraged_cap

logger = logging.getLogger("wave.scanner2")

ET = ZoneInfo("America/New_York")

# minute-of-day indexing: 0..329 = 04:00–09:29 (pre-market), 330..719 =
# 09:30–15:59 (RTH). SIP bars from 04:00 ET cannot contain overnight-venue
# prints, so cumulative volume on this axis is the HONEST pre-market number.
MINUTES = 720
PM_MINUTES = 330
SNAPSHOT_BATCH = 500
POLL_SECONDS = 60.0
MENU_SIZE = 20
JOURNAL_TOP = 200

# §7 floors — menu eligibility (watching is universal, eating is gated)
MIN_PRICE = 15.0
MIN_ADV = 500_000.0
MIN_ATR_PCT = 1.0

# Architecture B edge triggers (agent 5 diagram + agent 2 recipes 2/4):
# CATCH-THE-CLIMB (the #1 priority, 2026-09-23 — the PLTR 9-minute lag):
# the latency study (the detection-latency study) replayed
# 674 movers over 9 days: lateness cost ~$1,820/day, and these two rulers
# were the blocker (a megacap needs ~6-10 min to triple its cumulative
# volume even during a 34x explosion). 2.0→1.5 / 2.0→1.0 cuts median
# promotion lag 4→1 min for ~+28 junk promotions/day — promotion is cheap,
# every entry gate (TradeGate, brain veto, trend gate, risk) still stands.
RVOL_TRIGGER = 1.5  # SMB "in play" line (was 2.0)
HOD_MIN_DAY_PCT = 1.0  # HOD break counts with real day change (was 2.0)
VOL_SPIKE_MULT = 5.0  # 1-min volume ≥ 5× that minute's expected (Trade Ideas VS1 / TOS)
EVENT_DECAY_MINUTES = 90.0  # trigger-count leaderboard decay (agent 2 recipe 10)
NEWS_BOOST_HOURS = 4.0  # catalyst flag lifetime (Boudoukh: news day ≈ 2× variance)
FOCUS_SIZE = 150  # tick-level attention tier (agent 5: 50-300)
FOCUS_HYSTERESIS = 2  # consecutive ranked appearances before promotion
FOCUS_DAILY_CAP = 400  # hub has no unsubscribe — bound the day's promotions
# lunch-consolidation breakout (agent 2 recipe 8): 11:00–13:30 ET range,
# afternoon break on volume ≥3× minute-expected while in play
LUNCH_START = 420  # minute index of 11:00 ET
LUNCH_END = 570  # minute index of 13:30 ET
CONSOL_VOL_MULT = 3.0

# sympathy clusters (agent 2 recipe 9): static maps, anchor moves boost the
# chain. Members get score boost + a lower effective RVOL bar via the boost.
CLUSTERS: dict[str, dict] = {
    "crypto": {
        "anchors": ["COIN", "MSTR", "IBIT"],
        "members": [
            "COIN",
            "MSTR",
            "MARA",
            "RIOT",
            "CLSK",
            "HOOD",
            "CRCL",
            "IBIT",
            "BITO",
            "GLXY",
            "BMNR",
            "HUT",
            "WULF",
            "CIFR",
            "IREN",
            "BTBT",
        ],
    },
    "ai_semis": {
        "anchors": ["NVDA", "AMD"],
        "members": [
            "NVDA",
            "AMD",
            "AVGO",
            "MU",
            "SMCI",
            "ARM",
            "TSM",
            "MRVL",
            "SNDK",
            "WDC",
            "VRT",
            "ALAB",
            "CRDO",
            "PLTR",
        ],
    },
}


# universal U-shape fallback: fraction of a full day's volume expected by
# each minute, used until a symbol has real observed curves (self-builds
# within days). Shape: ~8% trades pre-market, heavy open, dead midday,
# heavy close — the standard intraday volume profile.
def _fallback_curve() -> np.ndarray:
    frac = np.zeros(MINUTES, dtype=np.float32)
    # pre-market: linear ramp to 8% of the day
    frac[:PM_MINUTES] = np.linspace(0.0, 0.08, PM_MINUTES, dtype=np.float32)
    rth = np.linspace(0.0, 1.0, MINUTES - PM_MINUTES, dtype=np.float32)
    # U-shaped density integrated: fast start, slow middle, fast end
    density = 0.35 * np.exp(-rth * 8.0) + 0.25 + 0.40 * np.exp((rth - 1.0) * 8.0)
    cum = np.cumsum(density)
    cum /= cum[-1]
    frac[PM_MINUTES:] = 0.08 + 0.92 * cum
    return frac


_FALLBACK = _fallback_curve()


def minute_index(now_et: datetime) -> int | None:
    """Index on the 4:00–16:00 ET axis; None outside it."""
    minutes = now_et.hour * 60 + now_et.minute - 4 * 60
    if 0 <= minutes < MINUTES:
        return minutes
    return None


@dataclass
class BaselineStore:
    """Per-symbol expected cumulative-volume curves + daily stats.

    Backed by one .npz in Application Support. Curves update nightly from
    the day's observed cumulative volumes (simple mean over up to 20 days,
    incremental form); symbols without history fall back to ADV × the
    universal U-curve until their own curve exists.
    """

    path: object = None
    symbols: list[str] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)
    curve: np.ndarray | None = None  # N × MINUTES, mean cumulative volume
    days: np.ndarray | None = None  # observations per symbol
    adv: np.ndarray | None = None  # 20d average daily volume
    atr_pct: np.ndarray | None = None
    prev_close: np.ndarray | None = None
    shares_out: np.ndarray | None = None  # float proxy (slow prior, journaled only)
    # 4.2 daily-context (capitulation/washout shape — the EIX signature):
    # N × 5 — pct_off_52w_high, consecutive_red_days, prior_day_volume_ratio,
    # prior_day_close_location, cum_5d_return. Journaled features only.
    daily_ctx: np.ndarray | None = None
    # 4.7 squeeze priors (slow, structural — FINRA via Massive, nightly):
    # N × 3 — si_pct (short interest % of shares out), days_to_cover,
    # short_volume_ratio (latest daily off-exchange %). Journaled features.
    short_ctx: np.ndarray | None = None

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = support_dir() / "scanner2_baselines.npz"

    def load(self) -> bool:
        try:
            if not self.path.exists():
                return False
            data = np.load(self.path, allow_pickle=False)
            self.symbols = [str(s) for s in data["symbols"]]
            self.index = {s: i for i, s in enumerate(self.symbols)}
            self.curve = data["curve"].astype(np.float32)
            self.days = data["days"].astype(np.float32)
            self.adv = data["adv"].astype(np.float32)
            self.atr_pct = data["atr_pct"].astype(np.float32)
            self.prev_close = data["prev_close"].astype(np.float32)
            if "shares_out" in data:
                self.shares_out = data["shares_out"].astype(np.float32)
            else:
                self.shares_out = np.zeros(len(self.symbols), dtype=np.float32)
            if "daily_ctx" in data:
                self.daily_ctx = data["daily_ctx"].astype(np.float32)
            else:
                self.daily_ctx = np.zeros((len(self.symbols), 5), dtype=np.float32)
            if "short_ctx" in data:
                self.short_ctx = data["short_ctx"].astype(np.float32)
            else:
                self.short_ctx = np.zeros((len(self.symbols), 3), dtype=np.float32)
            return True
        except Exception:
            logger.exception("baseline store unreadable — starting cold")
            return False

    def save(self) -> None:
        try:
            np.savez_compressed(
                self.path,
                symbols=np.array(self.symbols),
                curve=self.curve,
                days=self.days,
                adv=self.adv,
                atr_pct=self.atr_pct,
                prev_close=self.prev_close,
                shares_out=self.shares_out
                if self.shares_out is not None
                else np.zeros(len(self.symbols), dtype=np.float32),
                daily_ctx=self.daily_ctx
                if self.daily_ctx is not None
                else np.zeros((len(self.symbols), 5), dtype=np.float32),
                short_ctx=self.short_ctx
                if self.short_ctx is not None
                else np.zeros((len(self.symbols), 3), dtype=np.float32),
            )
        except Exception:
            logger.exception("baseline store save failed")

    def ensure(self, symbols: list[str]) -> None:
        """Grow the store to cover `symbols` (new listings get zero rows)."""
        new = [s for s in symbols if s not in self.index]
        n_old = len(self.symbols)
        n = n_old + len(new)
        if self.curve is None:
            self.curve = np.zeros((n, MINUTES), dtype=np.float32)
            self.days = np.zeros(n, dtype=np.float32)
            self.adv = np.zeros(n, dtype=np.float32)
            self.atr_pct = np.zeros(n, dtype=np.float32)
            self.prev_close = np.zeros(n, dtype=np.float32)
            self.shares_out = np.zeros(n, dtype=np.float32)
            self.daily_ctx = np.zeros((n, 5), dtype=np.float32)
            self.short_ctx = np.zeros((n, 3), dtype=np.float32)
        elif new:
            pad = len(new)
            self.curve = np.vstack([self.curve, np.zeros((pad, MINUTES), dtype=np.float32)])
            self.days = np.concatenate([self.days, np.zeros(pad, dtype=np.float32)])
            self.adv = np.concatenate([self.adv, np.zeros(pad, dtype=np.float32)])
            self.atr_pct = np.concatenate([self.atr_pct, np.zeros(pad, dtype=np.float32)])
            self.prev_close = np.concatenate([self.prev_close, np.zeros(pad, dtype=np.float32)])
            if self.shares_out is None:
                self.shares_out = np.zeros(n_old, dtype=np.float32)
            self.shares_out = np.concatenate([self.shares_out, np.zeros(pad, dtype=np.float32)])
            if self.daily_ctx is None:
                self.daily_ctx = np.zeros((n_old, 5), dtype=np.float32)
            self.daily_ctx = np.vstack([self.daily_ctx, np.zeros((pad, 5), dtype=np.float32)])
            if self.short_ctx is None:
                self.short_ctx = np.zeros((n_old, 3), dtype=np.float32)
            self.short_ctx = np.vstack([self.short_ctx, np.zeros((pad, 3), dtype=np.float32)])
        for i, s in enumerate(new):
            self.index[s] = n_old + i
        self.symbols.extend(new)

    def expected_cum(self, rows: np.ndarray, minute: int) -> np.ndarray:
        """Expected cumulative volume at `minute` for baseline rows `rows`
        (observed curve when it exists, ADV × U-curve fallback otherwise)."""
        observed = self.curve[rows, minute]
        fallback = self.adv[rows] * _FALLBACK[minute]
        have_curve = self.days[rows] >= 3.0  # ≥3 observed days to trust it
        return np.where(have_curve & (observed > 0), observed, fallback)

    def update_from_day(self, symbols: list[str], day_curves: np.ndarray) -> None:
        """Nightly: fold today's observed cumulative-volume curves into the
        running 20-day mean (incremental mean, capped weight at 20)."""
        self.ensure(symbols)
        rows = np.array([self.index[s] for s in symbols])
        counts = np.minimum(self.days[rows] + 1.0, 20.0)
        weight = (1.0 / counts)[:, None]
        self.curve[rows] = (1.0 - weight) * self.curve[rows] + weight * day_curves
        self.days[rows] = counts


class Scanner2:
    """The shadow scanner: poll → table → rank → journal. No trading."""

    def __init__(self, data_client, database=None, feed: str = "sip") -> None:
        self._client = data_client
        self._database = database
        self._feed = feed
        self.baselines = BaselineStore()
        self.baselines.load()
        self.symbols: list[str] = []
        self._index: dict[str, int] = {}
        self._leveraged: np.ndarray | None = None
        self._journal_order: list[int] = []
        self._journal_arrays: tuple | None = None
        self.market_regime: dict = {"regime": "WARMUP", "score": 0.0}  # 4.1
        self._hod_breaks_min = 0
        # hot table columns (float32, one row per symbol)
        self.last = None
        self.cum_vol = None
        self.day_open = None
        self.prev_close_live = None
        self.day_curve = None  # N × MINUTES observed today (for the nightly fold)
        self._day = None
        self.last_menu: list[dict] = []
        self.last_step_ts: float = 0.0  # freshness gate for LIVE menu duty
        self.universe_version: str = ""
        # S3 (the short side, 2026-09-23): weak-side awareness — lod_break
        # events, symmetric news/cluster boosts — exists ONLY behind this
        # flag. False (default) keeps every event, score and menu today
        # bit-identical to the long-only scanner (event_score feeds _rank,
        # so an ungated lod_break would reshuffle the LONG menu). Set from
        # config.shorts_enabled by the monitor at scanner2 startup.
        self.shorts_enabled: bool = False
        # Architecture B state (S2): stream ingest, triggers, focus, boosts
        self.hod = None  # running RTH high per symbol
        self.lod = None  # running RTH low per symbol (Build 3: range_pos)
        self.hod_minute = None  # minute-of-axis when the day high last printed
        self._cur_minute = 0  # latest minute index seen today (Build 3 clock)
        self.vwap_pv = None  # Σ(price×vol) since 9:30
        self.vwap_v = None  # Σ(vol) since 9:30
        self.cum_vol_stream = None  # bars-derived cumulative (reconciled w/ snapshot)
        self.event_score = None  # decaying trigger-count leaderboard
        self._event_decay_ts = 0.0
        self._rvol_armed = None  # per-symbol: rvol currently below trigger?
        self.stream_bars_ts: float = 0.0  # last bar message (stream health)
        self.news_ts: dict[str, float] = {}  # symbol → last FRESH catalyst time
        self.news_dir: dict[str, int] = {}  # symbol → last headline direction
        self.news_novelty: dict[str, float] = {}  # 4.3: 1.0 first print → decays
        self.news_rel: dict[str, int] = {}  # 4.3: ticker named in headline
        self.llm_tags: dict[str, dict] = {}  # 7.x: symbol → LLM verdict dict
        # 4.8 EDGAR form semantics — journaled flags, never gates
        self.dilution_ts: dict[str, float] = {}  # S-1/S-3/424: paper hitting the market
        self.activist_ts: dict[str, float] = {}  # 13D/G: activist stake (bullish prior)
        self.halt_count: dict[str, int] = {}  # 4.9: halts today (exhaustion meter)
        self.vix = 0.0  # 4.x VIX state (CBOE delayed; 0 = feed not seen yet)
        self.vix3m = 0.0
        self.news_feed: list[dict] = []  # ticker tape fuel (drained by the UI)
        from waveapp.engine.newsintel import NoveltyTracker

        self._novelty = NoveltyTracker()
        self.second_day: set[str] = set()  # yesterday's in-play carryover
        self._focus_streak: dict[str, int] = {}
        self.focus: set[str] = set()
        self._focus_promoted_today = 0
        self._pending_events: list[tuple[str, str, str, str]] = []
        self._events_seen = 0
        self.ui_events: list[tuple[str, str]] = []  # (symbol, kind) → Scanner-tab mind
        # EVENT PROMOTER: unwatched tradable names whose events fired —
        # drained each monitor cycle into immediate watch+scan (A/B-tagged)
        self.promotion_queue: set[str] = set()
        self.lunch_hi = None  # 11:00–13:30 consolidation range
        self.lunch_lo = None
        self._consol_fired = None
        self.earnings_today: set[str] = set()  # catalyst calendar (optional feed)
        self.attention: set[str] = set()  # retail-mention leaders (ApeWisdom)
        self.mention_vel: dict[str, float] = {}  # 4.7: mentions ÷ 24h-ago (velocity)
        # 4.6 themes: sector map (step 1) + live co-movement graph (step 2)
        self.sector_id: np.ndarray | None = None  # int8 per row; 10 = OTHER
        self.sector_names: list[str] = []
        self.sector_heat: dict[int, float] = {}  # sector id → median RVOL
        self.hot_sectors: list[str] = []
        self.themes: list[set[str]] = []  # live co-moving groups (≥3 names)
        self._theme_of: dict[str, int] = {}  # symbol → theme size (feature)
        self._ring_tick = 0
        self._ring: dict[str, dict[int, float]] = {}  # symbol → {tick: px}
        self.latest_quotes: dict = {}  # live quote ref (spread for focus names)
        # the REAL Bitcoin anchor (spec: "Bitcoin jumps 3% → the
        # whole crypto family gets bumped") — set by the feeds loop from
        # Alpaca's free crypto data; equity anchors remain as backup
        self.btc_day_pct: float = 0.0

    # -- universe ----------------------------------------------------------

    def set_universe(self, assets) -> tuple[list[str], list[str]]:
        """Adopt the broker's tradable equity list; returns (added, dropped)
        vs the previous persisted universe. Called at startup and daily."""

        # Wave's AssetInfo carries no asset_class (the adapter returns only
        # US equities); raw alpaca-py Assets do. Exclude only an EXPLICIT
        # non-equity class — a missing field means equity. (2026-09-01: the
        # old filter demanded the field and silently emptied the universe.)
        def _is_equity(a) -> bool:
            asset_class = str(getattr(a, "asset_class", "") or "")
            return not asset_class or asset_class.endswith("EQUITY")

        rows = [
            (a.symbol, getattr(a, "name", "") or "")
            for a in assets
            if getattr(a, "tradable", False) and _is_equity(a)
        ]
        symbols = sorted({s for s, _ in rows})
        names = dict(rows)
        previous, previous_names = self._load_universe_file()
        # wipe guard (2026-09-01): a broken feed/filter must never shrink a
        # 13k universe to a stub — keep the previous list and scream.
        if previous and len(previous) >= 1000 and len(symbols) < len(previous) * 0.5:
            logger.error(
                "scanner2 universe REFUSED: new list has %d symbols vs %d known —"
                " keeping the previous universe",
                len(symbols),
                len(previous),
            )
            symbols = previous
            # M3 fix: emptying names here rebuilt _leveraged all-False and
            # silently dropped the leveraged menu cap for the day — rebuild
            # from the persisted names (stub feed's names as fallback).
            names = {**names, **previous_names}
        added = sorted(set(symbols) - set(previous)) if previous else []
        dropped = sorted(set(previous) - set(symbols)) if previous else []
        self.symbols = symbols
        self._index = {s: i for i, s in enumerate(symbols)}
        n = len(symbols)
        self._leveraged = np.array(
            [is_leveraged_name(names.get(s, "")) for s in symbols], dtype=bool
        )
        self.last = np.zeros(n, dtype=np.float32)
        self.cum_vol = np.zeros(n, dtype=np.float32)
        self.day_open = np.zeros(n, dtype=np.float32)
        self.prev_close_live = np.zeros(n, dtype=np.float32)
        self.day_curve = np.zeros((n, MINUTES), dtype=np.float32)
        self.hod = np.zeros(n, dtype=np.float32)
        self.lod = np.zeros(n, dtype=np.float32)
        self.hod_minute = np.full(n, -1, dtype=np.int32)
        self._cur_minute = 0
        self.vwap_pv = np.zeros(n, dtype=np.float64)
        self.vwap_v = np.zeros(n, dtype=np.float64)
        self.cum_vol_stream = np.zeros(n, dtype=np.float32)
        self.event_score = np.zeros(n, dtype=np.float32)
        self._rvol_armed = np.ones(n, dtype=bool)
        self.last_dir = np.zeros(n, dtype=np.int8)  # 4.1 TICK-analog: last bar up/down
        self._hod_breaks_min = 0  # 4.1: new-HOD rate per minute
        self.lunch_hi = np.zeros(n, dtype=np.float32)
        self.lunch_lo = np.zeros(n, dtype=np.float32)
        self._consol_fired = np.zeros(n, dtype=bool)
        self.baselines.ensure(symbols)
        # 2026-09-04 07:50 root fix: the daily universe swap resizes every
        # array, but the rank/journal state computed on the OLD universe
        # survived — 9ms later three passes consumed stale indices against
        # the new arrays (IndexError x3, paged). Invalidate here; the
        # next scan pass rebuilds them on the new shape.
        self._journal_order = []
        self._journal_arrays = None
        self._load_sector_map()
        self._save_universe_file(symbols, names)
        self.universe_version = datetime.now(UTC).date().isoformat()
        if added or dropped:
            logger.info(
                "scanner2 universe: %d symbols (%+d added, %-d dropped)",
                n,
                len(added),
                len(dropped),
            )
            self._journal_universe_changes(added, dropped)
        else:
            logger.info("scanner2 universe: %d symbols (no membership changes)", n)
        return added, dropped

    def _universe_path(self):
        return support_dir() / "scanner2_universe.json"

    def _load_universe_file(self) -> tuple[list[str], dict[str, str]]:
        try:
            if self._universe_path().exists():
                data = json.loads(self._universe_path().read_text())
                # "names" absent in pre-M3 files → {} (heals on next save)
                return data["symbols"], data.get("names", {})
        except Exception:
            logger.exception("scanner2 universe file unreadable")
        return [], {}

    def _save_universe_file(self, symbols: list[str], names: dict[str, str]) -> None:
        try:
            self._universe_path().write_text(
                json.dumps(
                    {
                        "day": datetime.now(UTC).date().isoformat(),
                        "symbols": symbols,
                        # M3: names persist beside symbols so the wipe guard
                        # can rebuild _leveraged (the leveraged menu cap)
                        "names": names,
                    }
                )
            )
        except Exception:
            logger.exception("scanner2 universe file write failed")

    def _journal_universe_changes(self, added: list[str], dropped: list[str]) -> None:
        if self._database is None:
            return
        ts = datetime.now(UTC).isoformat()
        try:
            rows = [(ts, s, "added", "") for s in added] + [(ts, s, "dropped", "") for s in dropped]
            self._database.executemany(
                "INSERT INTO scanner2_universe_log (ts, symbol, change, detail)"
                " VALUES (?, ?, ?, ?)",
                rows,
            )
        except Exception:
            logger.exception("scanner2 universe journal failed")

    # -- Architecture B: stream ingest + edge triggers (agent 5 diagram) ----

    def on_bar_msg(self, bar) -> None:
        """One SIP minute bar from the bars* wildcard — the hot path.
        Scalar writes + edge detection only; no allocation-heavy work, no
        exceptions escape (a bad message must never wound the stream)."""
        try:
            row = self._index.get(str(bar.symbol))
            if row is None:
                return
            ts = bar.timestamp
            now_et = ts.astimezone(ET)
            minute = minute_index(now_et)
            if minute is None:
                return
            self.stream_bars_ts = ts.timestamp()
            # M2 fix: a 4:00-4:01 ET bar can land before the first
            # minute_tick of the new day — roll here too so it never
            # writes into (and is then wiped from) yesterday's arrays.
            self._roll_day(now_et)
            if self._day is not None and now_et.date().isoformat() != self._day:
                return  # straggler bar from a previous day — never contaminate today
            if minute > self._cur_minute:
                self._cur_minute = minute  # Build 3: intraday minute clock
            volume = float(bar.volume or 0.0)
            close = float(bar.close or 0.0)
            high = float(bar.high or 0.0)
            if close <= 0:
                return
            prev_last = float(self.last[row])
            if prev_last > 0 and close != prev_last:  # 4.1 TICK-analog
                self.last_dir[row] = 1 if close > prev_last else -1
            self.last[row] = close
            self.cum_vol_stream[row] += volume
            self.cum_vol[row] = max(self.cum_vol[row], self.cum_vol_stream[row])
            rth = minute >= PM_MINUTES
            low = 0.0
            prior_lod = 0.0
            new_lod = False
            if rth:
                if not self.day_open[row]:
                    self.day_open[row] = float(bar.open or close)
                self.vwap_pv[row] += ((high + float(bar.low or close) + close) / 3.0) * volume
                self.vwap_v[row] += volume
                low = float(bar.low or close)
                prior_lod = float(self.lod[row])
                if low > 0 and (prior_lod == 0.0 or low < prior_lod):
                    self.lod[row] = low  # Build 3: day range low
                    new_lod = True

            # expected volume at this minute (baseline or fallback)
            brow = self.baselines.index.get(str(bar.symbol))
            expected = 1.0
            per_minute = 0.0
            if brow is not None:
                expected = max(float(self.baselines.expected_cum(np.array([brow]), minute)[0]), 1.0)
                prev_minute = max(minute - 1, 0)
                per_minute = max(
                    expected - float(self.baselines.expected_cum(np.array([brow]), prev_minute)[0]),
                    0.0,
                )
            rvol = self.cum_vol[row] / expected
            day_pct = (close / self.day_open[row] - 1.0) * 100.0 if self.day_open[row] > 0 else 0.0

            # trigger 1 — RVOL cross (SMB "in play" line), fires once per arming
            if rvol >= RVOL_TRIGGER and self._rvol_armed[row]:
                self._rvol_armed[row] = False
                self._fire(now_et, row, "rvol_cross", f"rvol {rvol:.1f}")
            elif rvol < RVOL_TRIGGER * 0.75:
                self._rvol_armed[row] = True  # re-arm well below the line

            # trigger 2 — new HOD on participation (Warrior HOD-momo, liquid bands)
            if rth and high > 0:
                prior_hod = self.hod[row]
                if high > prior_hod:
                    self.hod[row] = high
                    self.hod_minute[row] = minute  # Build 3: trend_age_min anchor
                    if prior_hod > 0:
                        self._hod_breaks_min += 1  # 4.1: market-heat internal
                    if prior_hod > 0 and rvol >= RVOL_TRIGGER and day_pct >= HOD_MIN_DAY_PCT:
                        self._fire(
                            now_et,
                            row,
                            "hod_break",
                            f"{high:.2f} rvol {rvol:.1f}",
                            rvol=rvol,
                            day_pct=day_pct,
                        )

            # trigger 2b — S3 weak side (shorts_enabled ONLY): new LOW of day
            # on participation, the exact mirror of the HOD break (same rvol
            # ruler, day change ≤ −HOD_MIN_DAY_PCT). Gated on the flag
            # because event_score feeds the menu rank — an ungated lod_break
            # would promote weak names into today's LONG-only pipeline.
            # detail carries the direction tag (the structure's reason).
            if (
                self.shorts_enabled
                and new_lod
                and prior_lod > 0
                and rvol >= RVOL_TRIGGER
                and day_pct <= -HOD_MIN_DAY_PCT
            ):
                self._fire(
                    now_et,
                    row,
                    "lod_break",
                    f"{low:.2f} rvol {rvol:.1f} dir=short",
                    rvol=rvol,
                    day_pct=day_pct,
                )

            # trigger 3 — 1-min volume spike vs this minute's expected
            if per_minute > 0 and volume >= VOL_SPIKE_MULT * per_minute and volume > 10_000:
                self._fire(
                    now_et,
                    row,
                    "vol_spike",
                    f"{volume:,.0f} vs {per_minute:,.0f}",
                    rvol=rvol,
                    day_pct=day_pct,
                )

            # trigger 4 — lunch-consolidation breakout (agent 2 recipe 8):
            # build the 11:00–13:30 range; afternoon break of its high on
            # 3× minute volume while in play fires once
            if LUNCH_START <= minute < LUNCH_END:
                if self.lunch_hi[row] == 0.0:
                    self.lunch_hi[row] = high
                    self.lunch_lo[row] = float(bar.low or close)
                else:
                    self.lunch_hi[row] = max(self.lunch_hi[row], high)
                    self.lunch_lo[row] = min(self.lunch_lo[row], float(bar.low or close))
            elif (
                minute >= LUNCH_END
                and not self._consol_fired[row]
                and self.lunch_hi[row] > 0
                and close > self.lunch_hi[row]
                and rvol >= RVOL_TRIGGER
                and per_minute > 0
                and volume >= CONSOL_VOL_MULT * per_minute
            ):
                self._consol_fired[row] = True
                self._fire(now_et, row, "consol_break", f"above {self.lunch_hi[row]:.2f}")
        except Exception:  # never let the hot path throw
            logger.debug("scanner2 bar ingest error", exc_info=True)

    def on_status(self, symbol: str, halted: bool) -> None:
        """Halt/resume as scanner events (Trade Ideas HALT/RESUME): a halted
        name is in play for the rest of the day by definition. Score effect
        comes only through the leaderboard; Wave never chases a resume (§12).
        4.9: halt COUNT is journaled — the practitioner exhaustion meter
        (1-2 halts = continuation prior, 3+ = exhaustion-reversal odds)."""
        row = self._index.get(symbol)
        if row is None:
            return
        if halted:
            self.halt_count[symbol] = self.halt_count.get(symbol, 0) + 1
        now = datetime.now(UTC)
        detail = f"#{self.halt_count.get(symbol, 0)} today" if halted else ""
        self._fire(now.astimezone(ET), row, "halt" if halted else "resume", detail)

    def _fire(
        self, now_et, row: int, kind: str, detail: str, rvol: float = 0.0, day_pct: float = 0.0
    ) -> None:
        """An edge trigger: bump the decaying leaderboard + queue journal."""
        self.event_score[row] += 1.0
        self._events_seen += 1
        symbol = self.symbols[row]
        self._pending_events.append(
            (now_et.astimezone(UTC).isoformat(timespec="seconds"), symbol, kind, detail)
        )
        if len(self._pending_events) > 2000:
            del self._pending_events[:1000]
        self.ui_events.append((symbol, kind))  # Scanner-tab mind animation
        if len(self.ui_events) > 200:
            del self.ui_events[:100]
        # EVENT PROMOTER (adopted 2026-09-16, "build the promoter — show
        # me numbers": late-admitted event names graded +$35.5/case vs the
        # menu's +$25.2; median admission lag was 35-48 MINUTES; heat-only
        # selection graded $9/day, so the event only says LOOK — every
        # entry gate still decides). vol_spike/hod_break on a tradable,
        # unwatched name queues it for immediate watch+scan, A/B-tagged.
        # v2 filter (13:11 flood: raw vol_spikes fire constantly on liquid
        # megacaps — the whole market got promoted in 4 minutes). A real
        # IGNITION only: day rvol >= 3 AND the day already moving >= 2%,
        # or an rvol-gated hod_break. The study's junk share was 69%; this
        # is the discriminator that separates ignition from lunch volume.
        # S3: lod_break joins the promoter — it exists only when
        # shorts_enabled (the trigger itself is flag-gated), so promotion
        # of weak names cannot happen on the long-only app. vol_spike
        # ignition already reads |day_pct| — side-blind by construction.
        if kind in ("vol_spike", "hod_break", "lod_break"):
            with contextlib.suppress(Exception):  # best-effort; journal stands
                px = float(self.last[row]) or 0.0
                ignited = kind in ("hod_break", "lod_break") or (
                    rvol >= 3.0 and abs(day_pct) >= 2.0
                )
                plain_stock = "." not in symbol  # no preferreds/warrants (BAC.PRN, 13:30)
                if ignited and plain_stock and px >= 15.0 and float(self.cum_vol[row]) >= 100_000:
                    self.promotion_queue.add(symbol)

    def on_news(
        self,
        symbols: list[str],
        now_ts: float,
        headline: str = "",
        category: str = "other",
        direction: int = 0,
        tape: bool = True,
    ) -> None:
        """Benzinga stream: catalyst flag per symbol, now with CATEGORY and
        DIRECTION tags (Boudoukh: category carries skew) plus NOVELTY +
        RELEVANCE (4.3, Ke/Kelly/Xiu: fresh news moves ~×1.7 more). Tags are
        journaled features and ticker fuel — never a standalone trade
        trigger. A stale re-hash (novelty < 0.5) does NOT re-light the news
        boost window; it only updates the journal."""
        from waveapp.engine.newsintel import relevance as _relevance

        stamp = datetime.fromtimestamp(now_ts, UTC).isoformat(timespec="seconds")
        for symbol in symbols:
            novelty = self._novelty.assess(symbol, headline, now_ts)
            fresh = novelty >= 1.0  # only a true first print re-lights the boost
            if fresh or symbol not in self.news_ts:
                self.news_ts[symbol] = now_ts  # boost anchor: first prints only
            self.news_dir[symbol] = direction
            self.news_novelty[symbol] = novelty
            self.news_rel[symbol] = _relevance(symbol, headline)
            if symbol in self._index:
                self._pending_events.append(
                    (
                        stamp,
                        symbol,
                        "news",
                        f"{category}:{direction:+d} nov={novelty:.2f} {headline[:70]}",
                    )
                )
                if not tape or not fresh:  # repeats never re-enter the tape
                    continue
                self.news_feed.append(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "category": category,
                        "headline": headline[:120],
                    }
                )
        if len(self.news_feed) > 100:
            del self.news_feed[:50]

    def flag_filing(self, symbol: str, forms: str, now_ts: float) -> None:
        """4.8 EDGAR form-type semantics (journaled features only): an
        S-1/S-3/424(b) means new paper can hit the market — the DILUTION
        FADE flag on gappers; a 13D/G means an activist stake (bullish
        prior). The scanner's score is untouched — the Brain learns the
        weights from outcomes."""
        upper = forms.upper()
        if any(f in upper for f in ("S-1", "S-3", "424B")):
            self.dilution_ts[symbol] = now_ts
        if "13D" in upper or "13G" in upper:
            self.activist_ts[symbol] = now_ts

    def on_llm(self, symbols: list[str], verdict: dict, now_ts: float) -> None:
        """Layer 7 verdict lands (monitor's LLM task): journaled feature +
        event-stream annotation — NEVER a score boost or a trigger (hard
        rule 8; features first, gates second). Peers get a llm_peer tag so
        the journal records the sympathy link before their tape moves."""
        stamp = datetime.fromtimestamp(now_ts, UTC).isoformat(timespec="seconds")
        detail = (
            f"{verdict.get('event', 'other')}:{verdict.get('dir', 0):+d}"
            f"{' MAJOR' if verdict.get('mag') else ''}{' RPU' if verdict.get('rpu') else ''}"
        )
        for symbol in symbols:
            self.llm_tags[symbol] = dict(verdict)
            if symbol in self._index:
                self._pending_events.append((stamp, symbol, "llm", detail))
        for peer in verdict.get("peers") or []:
            if peer in self._index and peer not in symbols:
                self.llm_tags.setdefault(peer, {})["peer_of"] = symbols[0] if symbols else ""
                self._pending_events.append((stamp, peer, "llm_peer", f"via {detail}"))
        if len(self.llm_tags) > 600:  # day-bounded memory
            for stale in list(self.llm_tags)[:200]:
                del self.llm_tags[stale]

    def decay_events(self, now_ts: float) -> None:
        """Exponential decay of the trigger leaderboard (~90-min half-life)."""
        if self.event_score is None:
            return
        if self._event_decay_ts == 0.0:
            self._event_decay_ts = now_ts
            return
        dt_min = max(now_ts - self._event_decay_ts, 0.0) / 60.0
        self._event_decay_ts = now_ts
        if dt_min > 0:
            self.event_score *= 0.5 ** (dt_min / EVENT_DECAY_MINUTES)

    def flush_events(self) -> None:
        if self._database is None or not self._pending_events:
            self._pending_events = self._pending_events[-2000:]
            return
        batch, self._pending_events = self._pending_events, []
        try:
            self._database.executemany(
                "INSERT INTO scanner2_events (ts, symbol, kind, detail) VALUES (?, ?, ?, ?)",
                batch,
            )
        except Exception:
            logger.exception("scanner2 events journal failed")

    def seed_second_day(self) -> None:
        """SMB 2nd-day plays: yesterday's in-play names start on today's
        radar with a score boost."""
        if self._database is None:
            return
        try:
            rows = self._database.query(
                "SELECT DISTINCT symbol FROM scanner2_menu"
                " WHERE ts >= datetime('now', '-1 day') AND rank <= 20"
            )
            self.second_day = {r["symbol"] for r in rows}
            if self.second_day:
                logger.info(
                    "scanner2 2nd-day plays seeded: %s", ", ".join(sorted(self.second_day)[:15])
                )
        except Exception:
            logger.exception("scanner2 2nd-day seed failed")

    def update_focus(self) -> list[str]:
        """Focus-list manager (agent 5): symbols holding a top rank for
        FOCUS_HYSTERESIS consecutive menus get tick-level attention.
        Returns newly promoted symbols (the monitor subscribes them)."""
        current = {m["symbol"] for m in self.last_menu}
        promoted: list[str] = []
        for symbol in current:
            self._focus_streak[symbol] = self._focus_streak.get(symbol, 0) + 1
            if (
                symbol not in self.focus
                and self._focus_streak[symbol] >= FOCUS_HYSTERESIS
                and self._focus_promoted_today < FOCUS_DAILY_CAP
                and len(self.focus) < FOCUS_SIZE + FOCUS_DAILY_CAP
            ):
                self.focus.add(symbol)
                self._focus_promoted_today += 1
                promoted.append(symbol)
        for symbol in list(self._focus_streak):
            if symbol not in current:
                self._focus_streak[symbol] = 0
        return promoted

    # -- polling -----------------------------------------------------------

    async def step(self, now_et: datetime) -> list[dict]:
        """One full-market sweep: snapshots → table → rank → journal.
        Returns the current shadow menu (top-20 dicts)."""
        minute = minute_index(now_et)
        if minute is None or not self.symbols:
            return []
        self._roll_day(now_et)
        self._cur_minute = max(self._cur_minute, minute)
        await self._fetch_snapshots()
        self.day_curve[:, minute] = self.cum_vol
        self.decay_events(now_et.timestamp())
        menu = self._rank(minute)
        self.last_menu = menu
        self.last_step_ts = now_et.timestamp()
        self._journal(menu, now_et)
        self.flush_events()
        return menu

    def minute_tick(self, now_et: datetime) -> None:
        """Once per minute (Architecture B): day roll, observed-curve write,
        menu journal, market internals (4.1), event flush — no REST involved."""
        minute = minute_index(now_et)
        if minute is None or not self.symbols:
            return
        self._roll_day(now_et)
        self._cur_minute = max(self._cur_minute, minute)
        self.day_curve[:, minute] = self.cum_vol
        # 4.6 step 2: per-minute price ring for the loudest names + SPY
        self._ring_tick += 1
        try:
            n_sym = len(self.symbols)
            tracked = {self.symbols[i] for i in self._journal_order[:300] if i < n_sym}
            tracked.add("SPY")
            cutoff = self._ring_tick - 120
            for symbol in tracked:
                idx = self._index.get(symbol)
                if idx is None or self.last[idx] <= 0:
                    continue
                book = self._ring.setdefault(symbol, {})
                book[self._ring_tick] = float(self.last[idx])
                if len(book) > 130:
                    for t in [t for t in book if t < cutoff]:
                        del book[t]
            # drop only rings that went fully stale (a name that dips out of
            # the top-300 for a few minutes keeps its history)
            for symbol in [s for s, b in self._ring.items() if not b or max(b) < cutoff]:
                del self._ring[symbol]
        except Exception:
            logger.exception("price ring update failed")
        if self.last_menu:
            self._journal(self.last_menu, now_et)
        self._sector_heat_pass()
        if self._ring_tick % 5 == 0:
            self._theme_graph_pass()
        self.compute_market_internals(now_et)
        self.flush_events()

    # -- 4.1: self-computed market internals → MarketRegime ------------------

    SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC")

    def compute_market_internals(self, now_et: datetime) -> dict:
        """Every classic internal (ADD/VOLD/TICK/breadth/heat) computed from
        our own 13k-symbol tape — no external feed (blueprint 4.1). The
        weighted regime score is a VETO-grade context signal; tonight it is
        a JOURNALED FEATURE only (the multi-filter overfit trap: features
        first, gates second)."""
        out = {
            "breadth": 0.0,
            "vold": 0.0,
            "tick": 0.0,
            "above_vwap": 0.0,
            "agg_rvol": 0.0,
            "dispersion": 0.0,
            "hod_rate": float(self._hod_breaks_min),
            "vix": round(self.vix, 2),
            # VIX/VIX3M > 1 = backwardation (panic tape, ~7.7% of days) —
            # a daily risk state, journaled only (Avellaneda & Li)
            "vix_ratio": round(self.vix / self.vix3m, 3) if self.vix3m > 0 else 0.0,
            # 4.6: what the market is trading today
            "hot_sectors": ",".join(self.hot_sectors),
            "themes_n": len(self.themes),
            "score": 0.0,
            "regime": "WARMUP",
        }
        self._hod_breaks_min = 0
        try:
            valid = (self.last > 0) & (self.prev_close_live > 0)
            n_valid = int(valid.sum())
            if n_valid >= 100:  # a real tape, not a warm-up sliver
                day_ret = np.zeros_like(self.last)
                day_ret[valid] = self.last[valid] / self.prev_close_live[valid] - 1.0
                adv = valid & (day_ret > 0)
                dec = valid & (day_ret < 0)
                moved = int(adv.sum() + dec.sum())
                breadth = (int(adv.sum()) - int(dec.sum())) / max(moved, 1)
                dollar = self.cum_vol * self.last
                adv_d = float(dollar[adv].sum())
                dec_d = float(dollar[dec].sum())
                vold = (adv_d - dec_d) / max(adv_d + dec_d, 1.0)
                has_vwap = valid & (self.vwap_v > 0)
                above = 0.0
                if has_vwap.any():
                    vwap = self.vwap_pv[has_vwap] / self.vwap_v[has_vwap]
                    above = float((self.last[has_vwap] > vwap).mean())
                tick = float(self.last_dir[valid].mean())
                agg_rvol = 0.0
                if self._journal_arrays is not None:
                    rvol_arr = self._journal_arrays[1]
                    live = rvol_arr[valid & (rvol_arr > 0)]
                    if live.size >= 50:
                        agg_rvol = float(np.median(live))
                sector_rets = [
                    float(day_ret[self._index[s]] * 100.0)
                    for s in self.SECTOR_ETFS
                    if s in self._index and valid[self._index[s]]
                ]
                dispersion = (max(sector_rets) - min(sector_rets)) if len(sector_rets) >= 6 else 0.0
                score = 0.5 * vold + 0.3 * breadth + 0.2 * tick
                if score <= -0.5 and agg_rvol >= 1.5:
                    regime = "PANIC"
                elif score >= 0.5:
                    regime = "TREND_UP"
                elif score <= -0.5:
                    regime = "TREND_DOWN"
                elif dispersion >= 1.5:
                    regime = "ROTATION"
                elif agg_rvol and agg_rvol <= 0.8:
                    regime = "DEAD"
                else:
                    regime = "MIXED"
                out.update(
                    breadth=round(breadth, 3),
                    vold=round(vold, 3),
                    tick=round(tick, 3),
                    above_vwap=round(above, 3),
                    agg_rvol=round(agg_rvol, 3),
                    dispersion=round(dispersion, 2),
                    score=round(score, 3),
                    regime=regime,
                )
            self.market_regime = out
            if self._database is not None:
                ts = now_et.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
                self._database.execute(
                    "INSERT OR REPLACE INTO scanner2_snapshots (ts, symbol, features)"
                    " VALUES (?, '_MARKET', ?)",
                    (ts, json.dumps(out, separators=(",", ":"))),
                )
        except Exception:
            logger.exception("market internals failed")
        return out

    def rerank_only(self, now_et: datetime) -> list[dict]:
        """Architecture B fast path: 1-second re-rank from STREAM state —
        no REST, no journal (the minute step still journals)."""
        minute = minute_index(now_et)
        if minute is None or not self.symbols:
            return self.last_menu
        self._roll_day(now_et)  # M2 fix: never re-rank yesterday's arrays
        self._cur_minute = max(self._cur_minute, minute)
        self.decay_events(now_et.timestamp())
        menu = self._rank(minute)
        self.last_menu = menu
        self.last_step_ts = now_et.timestamp()
        return menu

    def _roll_day(self, now_et: datetime) -> None:
        day = now_et.date().isoformat()
        # forward-only (M2 fix): on_bar_msg rolls on BAR timestamps, so a
        # straggler bar from yesterday must never roll "back" and wipe
        # today's arrays. ISO dates compare lexicographically.
        if self._day is not None and day <= self._day:
            return
        if self._day is not None and self.day_curve is not None:
            self.fold_baselines()  # yesterday's curves — fold before reset
        self._day = day
        if self.day_curve is not None:
            self.day_curve[:] = 0.0
            self.cum_vol[:] = 0.0
            self.day_open[:] = 0.0
            self.hod[:] = 0.0
            self.vwap_pv[:] = 0.0
            self.vwap_v[:] = 0.0
            self.cum_vol_stream[:] = 0.0
            self.event_score[:] = 0.0
            self._rvol_armed[:] = True
            self.lunch_hi[:] = 0.0
            self.lunch_lo[:] = 0.0
            self._consol_fired[:] = False
            if self.lod is not None:
                self.lod[:] = 0.0
            if self.hod_minute is not None:
                self.hod_minute[:] = -1
        self._cur_minute = 0
        self._focus_streak.clear()
        self.focus.clear()
        self._focus_promoted_today = 0
        self.halt_count.clear()  # 4.9: the exhaustion meter is per-day
        # M4 fix: yesterday's scheduled-catalyst boosts (1.3x earnings,
        # 1.1x attention) must not leak into today until the feeds refresh.
        self.earnings_today.clear()
        self.attention.clear()
        self.seed_second_day()

    def fold_baselines(self) -> None:
        """Fold the day's observed curves into the 20-day baselines and
        persist. Called on day roll and at engine shutdown."""
        if self.day_curve is None or not self.symbols:
            return
        observed = self.day_curve.max(axis=1) > 0
        if not observed.any():
            return
        symbols = [s for s, o in zip(self.symbols, observed, strict=True) if o]
        # carry forward: cumulative curves are monotone; fill trailing zeros
        curves = np.maximum.accumulate(self.day_curve[observed], axis=1)
        self.baselines.update_from_day(symbols, curves)
        rows = np.array([self.baselines.index[s] for s in symbols])
        prev = self.last[observed]
        self.baselines.prev_close[rows] = np.where(prev > 0, prev, self.baselines.prev_close[rows])
        full_day = curves[:, -1]
        old_adv = self.baselines.adv[rows]
        self.baselines.adv[rows] = np.where(old_adv > 0, 0.95 * old_adv + 0.05 * full_day, full_day)
        self.baselines.save()
        logger.info("scanner2 baselines folded: %d symbols observed today", len(symbols))

    async def _fetch_snapshots(self) -> None:
        from alpaca.data.requests import StockSnapshotRequest

        async def fetch(chunk):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.get_stock_snapshot,
                        StockSnapshotRequest(symbol_or_symbols=chunk, feed=self._feed),
                    ),
                    timeout=25.0,
                )
            except Exception:
                logger.warning("scanner2 snapshot chunk failed (%d symbols)", len(chunk))
                return {}

        chunks = [
            self.symbols[i : i + SNAPSHOT_BATCH]
            for i in range(0, len(self.symbols), SNAPSHOT_BATCH)
        ]
        for start in range(0, len(chunks), 6):  # 6 concurrent, gentle on the loop
            results = await asyncio.gather(*(fetch(c) for c in chunks[start : start + 6]))
            for snapshots in results:
                for symbol, snap in (snapshots or {}).items():
                    row = self._index.get(symbol)
                    if row is None:
                        continue
                    try:
                        daily = snap.daily_bar
                        prev = snap.previous_daily_bar
                        trade = snap.latest_trade
                        # STALE-BAR GUARD (2026-09-15, the AT phantom): a
                        # recycled ticker's snapshot carried a daily bar
                        # from 2021 — phantom +563% gap, phantom 919k
                        # volume, 144 poisoned journal rows. Trust no bar
                        # without a fresh timestamp: today's daily only;
                        # prev-close only from the last ~7 calendar days.
                        now_ts = _time_mod.time()
                        daily_ts = getattr(daily, "timestamp", None)
                        if daily_ts is not None and now_ts - daily_ts.timestamp() > 86_400:
                            daily = None  # not today's bar
                        prev_ts = getattr(prev, "timestamp", None)
                        if prev_ts is not None and now_ts - prev_ts.timestamp() > 7 * 86_400:
                            prev = None  # dead/recycled listing
                        if daily is not None:
                            # reconcile, never regress: the stream may run
                            # ahead of the snapshot and vice versa (agent 5:
                            # snapshot = drift cross-check / gap repair)
                            snap_vol = float(daily.volume or 0.0)
                            self.cum_vol[row] = max(self.cum_vol[row], snap_vol)
                            self.cum_vol_stream[row] = max(self.cum_vol_stream[row], snap_vol)
                            if not self.day_open[row]:
                                self.day_open[row] = float(daily.open or 0.0)
                        if prev is not None and prev.close:
                            self.prev_close_live[row] = float(prev.close)
                        price = float(trade.price) if trade and trade.price else 0.0
                        if not price and daily is not None:
                            price = float(daily.close or 0.0)
                        if price:
                            self.last[row] = price
                    except Exception:  # noqa: PERF203, S112 — one bad snapshot never kills the sweep
                        continue

    # -- ranking -----------------------------------------------------------

    def _rank(self, minute: int) -> list[dict]:
        base_rows = np.array(
            [self.baselines.index.get(s, -1) for s in self.symbols], dtype=np.int64
        )
        valid_base = base_rows >= 0
        expected = np.ones(len(self.symbols), dtype=np.float32)
        expected[valid_base] = np.maximum(
            self.baselines.expected_cum(base_rows[valid_base], minute), 1.0
        )
        rvol = self.cum_vol / expected

        prev_close = np.where(
            self.prev_close_live > 0,
            self.prev_close_live,
            np.where(valid_base, self.baselines.prev_close[base_rows.clip(min=0)], 0.0),
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            gap = np.where(prev_close > 0, (self.last / prev_close - 1.0) * 100.0, 0.0)
            day_pct = np.where(self.day_open > 0, (self.last / self.day_open - 1.0) * 100.0, 0.0)
        adv = np.where(valid_base, self.baselines.adv[base_rows.clip(min=0)], 0.0)
        atr_pct = np.where(valid_base, self.baselines.atr_pct[base_rows.clip(min=0)], 0.0)

        # §7 floors: watching is universal, the MENU is gated
        eligible = (
            (self.last >= MIN_PRICE)
            & (adv >= MIN_ADV)
            & ((atr_pct >= MIN_ATR_PCT) | (atr_pct == 0.0))  # unknown ATR: not banned
            & (self.cum_vol > 0)
        )
        # participation × movement score (research: participation leads,
        # price movement only as an interaction; gap alone fades)
        move = np.clip(np.abs(gap), 0.0, 15.0) + np.clip(np.abs(day_pct), 0.0, 15.0)
        score = np.log1p(np.maximum(rvol, 0.0)) * (1.0 + 0.15 * move)

        # Architecture B boosts —
        # trigger-count leaderboard (decayed): the most event-active names rise
        score *= 1.0 + 0.25 * np.log1p(self.event_score)
        # catalyst flag (~2× variance on real-news days): boost for NEWS_BOOST_HOURS
        if self.news_ts:
            now_ts = self._event_decay_ts or 0.0
            fresh_news = np.zeros(len(self.symbols), dtype=bool)
            horizon = NEWS_BOOST_HOURS * 3600.0
            for symbol, ts in self.news_ts.items():
                srow = self._index.get(symbol)
                if srow is not None and now_ts - ts < horizon:
                    # DIRECTION-AWARE (bug found 2026-09-15: the boost
                    # ignored news_dir, promoting negative-headline names
                    # into a LONG-ONLY entry pipeline). Negative news gets
                    # no boost; neutral/positive keeps the full ×1.5.
                    # S3 (shorts_enabled only): with a short book armed,
                    # weakness is tradable — negative headlines boost too.
                    if self.news_dir.get(symbol, 0) >= 0 or self.shorts_enabled:
                        fresh_news[srow] = True
            score = np.where(fresh_news, score * 1.5, score)
        # 2nd-day plays (SMB): yesterday's in-play names stay on the radar
        if self.second_day:
            for symbol in self.second_day:
                srow = self._index.get(symbol)
                if srow is not None:
                    score[srow] *= 1.2
        # earnings today = scheduled catalyst (Boudoukh: identified-news day)
        if self.earnings_today:
            for symbol in self.earnings_today:
                srow = self._index.get(symbol)
                if srow is not None:
                    score[srow] *= 1.3
        # retail-attention leaders (Barber-Odean attention triple) — weakest
        # prior of the ten, smallest boost
        if self.attention:
            for symbol in self.attention:
                srow = self._index.get(symbol)
                if srow is not None:
                    score[srow] *= 1.1
        # sympathy clusters: a hot anchor lights the chain. The crypto
        # cluster's PRIMARY anchor is Bitcoin itself (|BTC day| ≥ 3%).
        for name, cluster in CLUSTERS.items():
            # S3 (shorts_enabled only): an anchor CRASHING lights the chain
            # too — weakness scores symmetrically. Flag off = the long-only
            # day_pct >= 3.0 test, bit-identical ranking.
            anchor_hot = any(
                (srow := self._index.get(a)) is not None
                and (
                    rvol[srow] >= 3.0
                    or day_pct[srow] >= 3.0
                    or (self.shorts_enabled and day_pct[srow] <= -3.0)
                )
                for a in cluster["anchors"]
            )
            if name == "crypto" and abs(self.btc_day_pct) >= 3.0:
                anchor_hot = True
            if anchor_hot:
                for symbol in cluster["members"]:
                    srow = self._index.get(symbol)
                    if srow is not None:
                        score[srow] *= 1.3

        score = np.where(eligible, score, -1.0)

        order = np.argsort(-score)
        cap = leveraged_cap(MENU_SIZE)
        menu: list[dict] = []
        admitted_leveraged = 0
        for idx in order:
            if len(menu) >= MENU_SIZE or score[idx] <= 0:
                break
            if self._leveraged is not None and self._leveraged[idx]:
                if admitted_leveraged >= cap:
                    continue
                admitted_leveraged += 1
            menu.append(
                {
                    "symbol": self.symbols[idx],
                    "score": round(float(score[idx]), 4),
                    "rvol": round(float(rvol[idx]), 3),
                    "gap_pct": round(float(gap[idx]), 2),
                    "day_pct": round(float(day_pct[idx]), 2),
                    "cum_volume": float(self.cum_vol[idx]),
                    "last": round(float(self.last[idx]), 4),
                }
            )
        self._journal_order = order[:JOURNAL_TOP]
        self._journal_arrays = (score, rvol, gap, day_pct)
        return menu

    # -- journaling --------------------------------------------------------

    def feature_row(
        self,
        idx: int,
        score: float = 0.0,
        rvol: float = 0.0,
        gap: float = 0.0,
        day_pct: float = 0.0,
    ) -> dict:
        """The as-of feature vector for one symbol — the ONE code path
        (blueprint 8.4 / point-in-time rule): the journal writes exactly
        this dict, and live v2 shadow scoring reads exactly this dict, so
        train-time and score-time features can never skew."""
        symbol = self.symbols[idx]
        px = float(self.last[idx])
        vwap = float(self.vwap_pv[idx] / self.vwap_v[idx]) if self.vwap_v[idx] > 0 else 0.0
        quote = self.latest_quotes.get(symbol)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        # Build 3 (2026-09-03) — human-awareness pack, JOURNALED FEATURES
        # only: what a human sees at entry time (the METU lesson: "it bought
        # after the trend was already up"). Missing inputs → 0.0 defaults
        # (range_pos → 0.5); this block must never raise.
        ext_open_atr = ext_vwap_atr = trend_age_min = day2_rep = 0.0
        mins_since_open = 0.0
        range_pos = 0.5
        try:
            mins_since_open = float(max(self._cur_minute - PM_MINUTES, 0))
            if symbol in self.second_day:
                day2_rep = 1.0  # on yesterday's menu too (2-day repeat)
            atr_dollars = 0.0
            brow = self.baselines.index.get(symbol)
            if brow is not None and self.baselines.atr_pct is not None:
                ref = float(self.prev_close_live[idx])
                if ref <= 0 and self.baselines.prev_close is not None:
                    ref = float(self.baselines.prev_close[brow])
                if ref <= 0:
                    ref = px
                atr_dollars = float(self.baselines.atr_pct[brow]) / 100.0 * ref
            if atr_dollars > 0 and px > 0:
                if self.day_open[idx] > 0:  # daily-ATR units already run since open
                    ext_open_atr = (px - float(self.day_open[idx])) / atr_dollars
                if vwap > 0:  # stretch above/below session VWAP
                    ext_vwap_atr = (px - vwap) / atr_dollars
            day_hi = float(self.hod[idx])
            day_lo = float(self.lod[idx]) if self.lod is not None else 0.0
            if px > 0 and day_hi > 0 and day_lo > 0 and day_hi - day_lo > 1e-9:
                range_pos = min(max((px - day_lo) / (day_hi - day_lo), 0.0), 1.0)
            if self.hod_minute is not None:
                hod_min = int(self.hod_minute[idx])
                if hod_min >= 0:  # 0 = new highs NOW; large = the trend is old
                    trend_age_min = float(max(self._cur_minute - hod_min, 0))
        except Exception:
            logger.debug("awareness feature block failed", exc_info=True)
        return {
            "score": round(score, 4),
            "rvol": round(rvol, 3),
            "gap": round(gap, 2),
            "day": round(day_pct, 2),
            "vol": float(self.cum_vol[idx]),
            "px": round(px, 4),
            "hod_dist": round((px / float(self.hod[idx]) - 1.0) * 100.0, 3)
            if self.hod[idx] > 0
            else 0.0,
            "vwap_dist": round((px / vwap - 1.0) * 100.0, 3) if vwap > 0 else 0.0,
            "spread": round(ask - bid, 4) if bid > 0 and ask > bid else 0.0,
            "events": round(float(self.event_score[idx]), 2),
            "news": 1 if symbol in self.news_ts else 0,
            "news_dir": self.news_dir.get(symbol, 0),
            "news_nov": round(self.news_novelty.get(symbol, 0.0), 3),
            "news_rel": self.news_rel.get(symbol, 0),
            # 7.x LLM verdict (journal-only; v2 adopts via the weekly
            # feature protocol, 8.8)
            # 4.8: 48h dilution / 7d activist windows (journal-write time)
            "dilution": 1
            if _time_mod.time() - self.dilution_ts.get(symbol, 0.0) < 48 * 3600
            else 0,
            "activist": 1
            if _time_mod.time() - self.activist_ts.get(symbol, 0.0) < 7 * 86400
            else 0,
            # 4.9: halts today (1-2 continuation prior, 3+ exhaustion)
            "halts": self.halt_count.get(symbol, 0),
            # 4.7 squeeze priors (nightly FINRA via Massive) + composite
            **self._squeeze_feats(symbol),
            # 4.6 themes: the symbol's sector, its sector's live heat, and
            # the size of any co-movement theme it belongs to right now
            "sector": int(self.sector_id[idx]) if self.sector_id is not None else 10,
            "sector_heat": round(
                self.sector_heat.get(
                    int(self.sector_id[idx]) if self.sector_id is not None else 10, 0.0
                ),
                2,
            ),
            "theme_n": self._theme_of.get(symbol, 0),
            # 4.10: flat-day relative-strength leader — a stock going to new
            # highs while SPY sleeps ("strong stock on a flat tape goes first")
            "rs_leader": self._rs_leader_flag(idx),
            "llm_evt": self.llm_tags.get(symbol, {}).get("evt_id", 0),
            "llm_dir": self.llm_tags.get(symbol, {}).get("dir", 0),
            "llm_mag": 1 if self.llm_tags.get(symbol, {}).get("mag") else 0,
            "llm_rpu": 1 if self.llm_tags.get(symbol, {}).get("rpu") else 0,
            "shares_out": float(self.baselines.shares_out[self.baselines.index[symbol]])
            if self.baselines.shares_out is not None and symbol in self.baselines.index
            else 0.0,
            # barrier geometry for the nightly labeler (8.1) — journaled
            # as-of so labels never depend on a later baseline refresh
            "atr_pct": float(self.baselines.atr_pct[self.baselines.index[symbol]])
            if self.baselines.atr_pct is not None and symbol in self.baselines.index
            else 0.0,
            # Build 3 human-awareness pack (journaled only; owner 2026-09-03)
            "ext_open_atr": round(ext_open_atr, 3),
            "ext_vwap_atr": round(ext_vwap_atr, 3),
            "range_pos": round(range_pos, 3),
            "trend_age_min": trend_age_min,
            "mins_since_open": mins_since_open,
            "day2_rep": day2_rep,
            **self._daily_ctx_feats(symbol),
        }

    def _daily_ctx_feats(self, symbol: str) -> dict:
        """4.2 capitulation/washout shape, from the nightly baseline build.
        Zeros until the backfill has run with the daily-context stage."""
        base = self.baselines
        brow = base.index.get(symbol)
        if brow is None or base.daily_ctx is None or brow >= len(base.daily_ctx):
            return dict.fromkeys(
                ("off_52w", "red_days", "pdv_ratio", "pd_close_loc", "ret_5d"), 0.0
            )
        ctx = base.daily_ctx[brow]
        return {
            "off_52w": round(float(ctx[0]), 2),  # % below the 52-week high
            "red_days": float(ctx[1]),  # consecutive red days into today
            "pdv_ratio": round(float(ctx[2]), 2),  # prior-day volume ÷ 20d avg
            "pd_close_loc": round(float(ctx[3]), 3),  # 0 = closed at low, 1 = at high
            "ret_5d": round(float(ctx[4]), 2),  # trailing 5-day return %
        }

    # -- 4.6 themes: sector heat (step 1) + live co-movement graph (step 2) --

    def _load_sector_map(self) -> None:
        """scanner2_sectors.npz (scripts/scanner2_sectors.py, weekly) →
        per-row sector ids. Absent = everyone OTHER until the fetch runs."""
        n = len(self.symbols)
        self.sector_id = np.full(n, 10, dtype=np.int8)
        try:
            path = support_dir() / "scanner2_sectors.npz"
            if not path.exists():
                return
            data = np.load(path, allow_pickle=False)
            self.sector_names = [str(s) for s in data["sector_names"]]
            mapping = dict(zip(data["symbols"], data["sector_ids"], strict=True))
            for i, symbol in enumerate(self.symbols):
                sid = mapping.get(symbol)
                if sid is not None:
                    self.sector_id[i] = int(sid)
            known = int((self.sector_id != 10).sum())
            logger.info("sector map loaded: %d/%d symbols classified", known, n)
        except Exception:
            logger.exception("sector map unreadable — themes run sectorless")

    def _sector_heat_pass(self) -> None:
        """Step 1 (BUILD-FIRST per the agent): per-sector median time-anchored
        RVOL — 'what is the market trading today', zero new math."""
        self.sector_heat = {}
        self.hot_sectors = []
        if self.sector_id is None or self._journal_arrays is None:
            return
        if len(self._journal_arrays[1]) != len(self.last) or len(self.sector_id) != len(self.last):
            return  # resize guard (2026-09-04): stale shapes rebuild next pass
        try:
            rvol = self._journal_arrays[1]
            live = (rvol > 0) & (self.last > 0)
            heats: dict[int, float] = {}
            for sid in range(10):  # OTHER (10) is not a theme
                mask = live & (self.sector_id == sid)
                if int(mask.sum()) >= 5:
                    heats[sid] = float(np.median(rvol[mask]))
            self.sector_heat = heats
            if heats:
                overall = float(np.median(list(heats.values())))
                self.hot_sectors = [
                    self.sector_names[sid] if sid < len(self.sector_names) else str(sid)
                    for sid, heat in sorted(heats.items(), key=lambda kv: -kv[1])
                    if heat >= 1.5 and heat >= 1.5 * overall
                ][:3]
        except Exception:
            logger.exception("sector heat pass failed")

    def _theme_graph_pass(self) -> None:
        """Step 2: residual co-movement components on the top-RVOL slice
        (Physica A 2025: industry structure recovers best at 4-48min bars;
        1-min prices sampled to 5-min returns over the last hour). β=1 SPY
        strip per the agent's v1; raw-corr threshold 0.7 stands in for the
        shrunk-matrix 0.6 until a daily-corr prior exists. Journal-only."""
        self.themes = []
        self._theme_of = {}
        try:
            tick = self._ring_tick
            if tick < 61 or "SPY" not in self._ring:
                return
            sample_ticks = list(range(tick - 60, tick + 1, 5))  # 13 samples → 12 returns

            def series(symbol: str) -> np.ndarray | None:
                book = self._ring.get(symbol)
                if not book:
                    return None
                prices = []
                for t in sample_ticks:
                    px = book.get(t) or book.get(t - 1) or book.get(t - 2)
                    if not px:
                        return None
                    prices.append(px)
                arr = np.asarray(prices, dtype=np.float64)
                return np.diff(np.log(arr)) if (arr > 0).all() else None

            spy_ret = series("SPY")
            if spy_ret is None:
                return
            rvol = self._journal_arrays[1] if self._journal_arrays is not None else None
            members: list[str] = []
            rets: list[np.ndarray] = []
            for symbol in list(self._ring.keys()):
                if symbol == "SPY":
                    continue
                r = series(symbol)
                if r is not None and float(np.std(r)) > 1e-6:
                    members.append(symbol)
                    rets.append(r - spy_ret)  # residualize: strip the market
            if len(members) < 3:
                return
            matrix = np.corrcoef(np.vstack(rets))
            adjacency = np.abs(matrix) > 0.7
            np.fill_diagonal(adjacency, False)
            # connected components (numpy BFS — no graph deps)
            unvisited = set(range(len(members)))
            while unvisited:
                seed = unvisited.pop()
                component = {seed}
                frontier = [seed]
                while frontier:
                    node = frontier.pop()
                    for neighbor in np.nonzero(adjacency[node])[0]:
                        if neighbor in unvisited:
                            unvisited.discard(int(neighbor))
                            component.add(int(neighbor))
                            frontier.append(int(neighbor))
                if len(component) >= 3:
                    names = {members[i] for i in component}
                    avg_rvol = 0.0
                    if rvol is not None:
                        rows = [self._index[s] for s in names if s in self._index]
                        avg_rvol = float(np.mean(rvol[rows])) if rows else 0.0
                    if avg_rvol >= 1.5:  # themes live where the volume is
                        self.themes.append(names)
                        for s in names:
                            self._theme_of[s] = len(names)
        except Exception:
            logger.exception("theme graph pass failed")

    def _squeeze_feats(self, symbol: str) -> dict:
        """4.7 squeeze-anticipation (Svoboda et al.: squeezes ARE
        anticipatable — elevated SI + attention spikes are precursors).
        Journaled features + a 0-4 precursor COUNT, never a gate. Borrow-fee
        was in the spec but its free source (iBorrowDesk) is dead — the
        composite runs on the four live precursors until a source exists."""
        base = self.baselines
        brow = base.index.get(symbol)
        si_pct = dtc = sv_ratio = 0.0
        if brow is not None and base.short_ctx is not None and brow < len(base.short_ctx):
            si_pct = float(base.short_ctx[brow][0])
            dtc = float(base.short_ctx[brow][1])
            sv_ratio = float(base.short_ctx[brow][2])
        vel = float(self.mention_vel.get(symbol, 0.0))
        capitulation = 0
        if brow is not None and base.daily_ctx is not None and brow < len(base.daily_ctx):
            ctx = base.daily_ctx[brow]
            # washed-out shape: a red streak, or deep off-high on a flush
            capitulation = 1 if (ctx[1] >= 3 or (ctx[0] >= 30.0 and ctx[2] >= 2.0)) else 0
        score = (
            (1 if si_pct >= 15.0 else 0)
            + (1 if dtc >= 5.0 else 0)
            + (1 if vel >= 2.0 else 0)
            + capitulation
        )
        return {
            "si_pct": round(si_pct, 1),
            "dtc": round(dtc, 1),
            "sv_ratio": round(sv_ratio, 1),
            "mention_vel": round(vel, 2),
            "squeeze": score,
        }

    def _rs_leader_flag(self, idx: int) -> int:
        """4.10 (journaled feature only): |SPY| ≤ 0.25% on the day while this
        symbol is +2%+, within 1% of its HOD and above VWAP — the flat-day
        alpha shape (index flatness says nothing about single names)."""
        try:
            spy = self._index.get("SPY")
            if spy is None:
                return 0
            spy_prev = float(self.prev_close_live[spy])
            spy_last = float(self.last[spy])
            if spy_prev <= 0 or spy_last <= 0:
                return 0
            if abs(spy_last / spy_prev - 1.0) * 100.0 > 0.25:
                return 0  # the index is moving — not a flat-day setup
            prev = float(self.prev_close_live[idx])
            px = float(self.last[idx])
            hod = float(self.hod[idx])
            if prev <= 0 or px <= 0 or hod <= 0:
                return 0
            if (px / prev - 1.0) * 100.0 < 2.0:
                return 0
            if px < hod * 0.99:
                return 0  # not pressing its high
            vwap = float(self.vwap_pv[idx] / self.vwap_v[idx]) if self.vwap_v[idx] > 0 else 0.0
            return 1 if (vwap > 0 and px > vwap) else 0
        except Exception:
            return 0

    def live_feature_row(self, symbol: str) -> dict | None:
        """feature_row for one symbol RIGHT NOW (live v2 shadow scoring) —
        score/rvol/gap/day pulled from the same state the re-rank uses."""
        idx = self._index.get(symbol)
        if idx is None:
            return None
        rvol = gap = day_pct = score = 0.0
        # arrays may be absent pre-open — zeros are honest then
        with contextlib.suppress(Exception):
            if self._journal_arrays is not None:
                s, r, g, d = self._journal_arrays
                score, rvol, gap, day_pct = (
                    float(s[idx]),
                    float(r[idx]),
                    float(g[idx]),
                    float(d[idx]),
                )
        return self.feature_row(idx, score=score, rvol=rvol, gap=gap, day_pct=day_pct)

    def _journal(self, menu: list[dict], now_et: datetime) -> None:
        if self._database is None:
            return
        ts = now_et.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
        try:
            self._database.executemany(
                "INSERT OR REPLACE INTO scanner2_menu"
                " (ts, rank, symbol, score, rvol, gap_pct, day_pct, cum_volume, last_price)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        ts,
                        rank + 1,
                        m["symbol"],
                        m["score"],
                        m["rvol"],
                        m["gap_pct"],
                        m["day_pct"],
                        m["cum_volume"],
                        m["last"],
                    )
                    for rank, m in enumerate(menu)
                ],
            )
            arrays = self._journal_arrays
            if arrays is None:
                # 2026-09-23 07:50 race: a universe refresh reset the arrays
                # in the same second the journal writer ran — skip this tick
                # (research journaling only; the next tick has fresh arrays)
                return
            score, rvol, gap, day_pct = arrays
            rows = []
            n_sym = min(len(self.symbols), len(score))
            for idx in self._journal_order:
                if idx >= n_sym:
                    continue  # resize guard (2026-09-04)
                if score[idx] <= 0:
                    break
                symbol = self.symbols[idx]
                feats = self.feature_row(
                    idx,
                    score=float(score[idx]),
                    rvol=float(rvol[idx]),
                    gap=float(gap[idx]),
                    day_pct=float(day_pct[idx]),
                )
                rows.append((ts, symbol, json.dumps(feats, separators=(",", ":"))))
            if rows:
                self._database.executemany(
                    "INSERT OR REPLACE INTO scanner2_snapshots (ts, symbol, features)"
                    " VALUES (?, ?, ?)",
                    rows,
                )
        except Exception:
            logger.exception("scanner2 journal write failed")
