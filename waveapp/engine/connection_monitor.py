"""ConnectionMonitor (Phases 2–3): owns the broker connection AND the market
DataHub, reporting health to the UI as (dot name, color, tooltip) updates.
The UI never touches the adapter or the hub — it only observes (§3).

Behavior:
- connects the AlpacaAdapter (paper) at startup; missing Keychain keys is a
  normal state (grey dot), retried periodically so adding keys "just works";
- while connected, proves liveness with a clock poll every POLL_SECONDS;
  a failure turns the dot red, tears everything down and starts over;
- the "Data feed" dot reflects the DataHub market-data stream (Phase 3);
  its tooltip includes the last-message age from the heartbeat watchdog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from waveapp.broker.alpaca import AlpacaAdapter
from waveapp.broker.base import MissingCredentialsError, OrderSide, TradingMode
from waveapp.data.hub import DataHub
from waveapp.ui import theme

logger = logging.getLogger("wave.engine.connections")

# no new auto entries this close to the daily flatten — the exit system needs
# runway, and the flatten itself starts 10 min out (§8.2 layer 7). Raised
# 15 → 70 min-to-close (= 60 min before the 15:50 flatten) 2026-08-31 after
# ESI entered at 15:43 and was force-flattened at 15:50 for −$43: across all
# four research windows (3 years), entries with <30 min runway ran 37.9% WR
# / −$1,811 and <60 min runway was negative or flat in every window (net
# −$2,491); entries with ≥2 h runway carry the whole edge.
ENTRY_CUTOFF_MINUTES = 70.0

# CHOP-DAY SIGNAL INVERSION (2026-09-23): minimum DayJudge confidence
# before a CHOP verdict may flip momentum signals. Config gates the feature
# (chop_invert, default False); this floor is deliberately code-fixed.
CHOP_INVERT_MIN_CONF = 0.6

POLL_SECONDS = 30.0
RETRY_SECONDS = 15.0
# weekend/holiday keep-alive (2026-08-22: no API churn while CLOSED):
# liveness+equity poll drops from every 30s to every 10 min — connections
# stay up (§3), calls drop ~95% until Sunday 20:00 ET
CLOSED_POLL_SECONDS = 600.0
DEFAULT_WATCHLIST = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA"]

StatusCallback = Callable[[str, str, str], None]  # (dot name, color, tooltip)


class ConnectionMonitor:
    def __init__(
        self,
        on_status: StatusCallback,
        watchlist: list[str] | None = None,
        telegram_user_id: int = 0,
        database=None,
        on_engine_state: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_candidates: Callable[[list[dict]], None] | None = None,
        on_scan_status: Callable[[str], None] | None = None,
        scan_universe_size: int = 150,
        scan_interval_seconds: int = 120,
        on_balance: Callable[[str, float], None] | None = None,
        on_trade: Callable[[dict], None] | None = None,
        on_positions: Callable[[list[dict]], None] | None = None,
        on_marks: Callable[[dict], None] | None = None,
        on_performance: Callable[[dict], None] | None = None,
        on_system: Callable[[dict], None] | None = None,
        on_universe_progress: Callable[[int, int, str], None] | None = None,
        on_scanner2_event: Callable[[str, str], None] | None = None,
        on_scanner2_menu: Callable[[list], None] | None = None,
        on_scanner2_news: Callable[[dict], None] | None = None,
        on_market_regime: Callable[[dict], None] | None = None,
        on_scoreboard: Callable[[dict], None] | None = None,
    ) -> None:
        self._on_status = on_status
        self._watchlist = watchlist or DEFAULT_WATCHLIST
        self._telegram_user_id = telegram_user_id
        self._database = database
        self._on_engine_state = on_engine_state
        self._on_error = on_error
        self._adapter: AlpacaAdapter | None = None
        self._hub: DataHub | None = None
        self._bridge = None
        # A5-3 (audit 2026-09-22): ONE lock serializes every bridge
        # stop/start. bridge.start() is several awaits long, so without it
        # apply_telegram_user_id's stop→start window let the run loop's own
        # _ensure_telegram slip past the `_bridge is None` guard and start a
        # SECOND poller — never stopped, Telegram Conflict churn, and a split
        # ConfirmationGate that could eat a /kill confirmation code.
        self._tg_lock = asyncio.Lock()
        # A5-4: rate-limit identical consecutive run-cycle errors
        self._last_cycle_error = ""
        self._cycle_error_count = 0
        self._error_handler = None
        self._market_open = False
        self._market_open_known = False  # open/close pushes need a baseline
        self._on_candidates = on_candidates
        self._on_scan_status = on_scan_status
        self._scan_universe_size = scan_universe_size
        self._scan_interval = scan_interval_seconds
        self._scanner = None
        self._scan_task = None
        self._scanner2 = None  # Scanner 2.0 (Architecture B, live 2026-09-01)
        self._scanner2_task = None
        self._scanner2_news_task = None
        self._scanner2_feeds_task = None
        self._brain = None  # Brain Stage 1 artifact (loaded at scanner start)
        self._brain2 = None  # v2 nightly live-features challenger (8.2/8.13)
        self._nightly_ml_task = None
        self._llm = None  # layer 7 news brain (loaded at scanner start)
        self._llm_tasks: set = set()
        self._llm_baseline = 0.0  # lifetime spend loaded from config
        self._llm_saved_total = 0.0
        self._llm_budget = 25.0
        # 6.5 ladder observability (fallback must be visible)
        self._ladder_stats = {"posted": 0, "captured": 0, "fallback": 0}
        self._brain_shadow = {
            "scored": 0,
            "approved": 0,
            "rejected": 0,
            "approved_new": 0,
            "rejected_new": 0,
        }
        self._on_balance = on_balance
        self._on_trade = on_trade
        self._on_positions = on_positions
        self._on_marks = on_marks
        self._on_performance = on_performance
        self._on_system = on_system
        self._on_universe_progress = on_universe_progress
        self._on_scanner2_event = on_scanner2_event
        self._on_scanner2_menu = on_scanner2_menu
        self._on_scanner2_news = on_scanner2_news
        self._on_market_regime = on_market_regime
        self._on_scoreboard = on_scoreboard
        self._positions_task = None
        # Master Key shadow kitchen (2026-09-08, adopted) — logs the v14
        # brain's would-be exits beside the real kitchen; never places orders
        self._shadow_kitchen = None
        self._shadow_kitchen_task = None
        # session scoreboard (2026-08-19): closed-trade records since
        # app start — per strategy and per price-floor bucket
        self._trade_records: list[dict] = []
        self._closed_seen: set[str] = set()
        # churn guard (2026-09-09): per-symbol scanner-entry count + last
        # stop-out time, session-local (resets on app restart)
        self._symbol_entries: dict[str, int] = {}
        self._symbol_stop_hit_at: dict[str, float] = {}
        # A3-2 (audit 2026-09-22): the one-trade-per-day mark now burns
        # AFTER _execute_signal succeeds, so this set carries the
        # no-double-signal guarantee across the await — an overlapping
        # pipeline pass (slow-path loop vs minute-tick task) or the PMOM
        # fallback can never double-enter a symbol whose entry is in flight
        self._signal_inflight: set[str] = set()
        # CHOP-INVERT (2026-09-23): opened inverted entries today (ET-day
        # cap, chop_invert_max_per_day); reset by _roll_signal_day
        self._chop_inverted_today: int = 0
        # A4-7 (audit 2026-09-22): per-symbol once-latch for the MK-rebuy
        # stale-quote deferral log — armed on the first deferred tick,
        # cleared the moment the symbol serves a fresh quote again
        self._mk_rebuy_stale_logged: set[str] = set()
        self._actor_opened_at: dict[str, float] = {}
        # M0 entry telemetry (ML master plan, 2026-09-23) — observation only:
        # first watch/promotion stamp per symbol (day-keyed) and the AUTO
        # ENTRY signal stamp per symbol; consumed by _record_entry_lag when
        # the fill is first observed.
        self._first_watch_day: str | None = None
        self._first_watch_at: dict[str, str] = {}
        self._entry_signal_at: dict[str, str] = {}
        # DAY JUDGE (R0, 2026-09-23, the −$854 chop day): live day-type
        # detector, fed once per minute at the scanner2 boundary with the
        # menu-breadth recorder's numbers. Observation + badge only; the
        # entry-routing consumer reads .verdict/.confidence attributes.
        from waveapp.engine.day_judge import DayJudge

        self._day_judge = DayJudge(journal_db_cb=self._day_regime_write)
        # THE EFFICACY GUARD (R1.5, 2026-09-24, the −$3,928 morning): scores
        # whether Wave's OWN fills follow through (+1A before −1A within 10
        # min) and flips the signal book MOMENTUM↔INVERTED on that evidence.
        # Fed by the kitchen's judge path (efficacy_cb) and the closed-trade
        # tracker; consumed by the entry pipeline; flips journal to
        # efficacy_events (migration 014) and page the phone (klass="risk").
        from waveapp.engine.efficacy import EfficacyTracker

        self._efficacy = EfficacyTracker(
            journal_db_cb=self._efficacy_write, notify_cb=self._efficacy_notify
        )
        # PRE-OPEN BIAS BRAIN (R0.5, 2026-09-24): one verdict ~9:25 ET from
        # pre-market evidence (SPY/QQQ gap + the watched-universe gap map),
        # governing entry strictness 9:30 → the Day Judge's first real
        # verdict, and seeding the efficacy tracker's bias_hint. Journals
        # through the SAME day_regime pen, verdict prefixed PREOPEN_.
        from waveapp.engine.preopen import PreOpenBias

        self._preopen = PreOpenBias(journal_db_cb=self._day_regime_write)
        # THE SNIPER BOOK (R2, 2026-09-24, the validation
        # study): FPB pullback watches for extended momentum
        # signals, jewel sizing, pocket starves. State + math live in
        # engine/fpb.py; the routing is in _entry_pipeline and
        # _sniper_watch_pass; day-rolled with the one-trade-per-day mark.
        from waveapp.engine.fpb import SniperBook

        self._sniper = SniperBook()
        # chart backfill (2026-08-19): a freshly-subscribed position
        # symbol has no stream history — REST-fetch the last ~2h of 1-min
        # bars so the detail popup's candle chart is never empty
        self._last_equity = 0.0  # latest account equity (performance re-push)
        self._chart_backfill: dict[str, list] = {}
        self._backfill_pending: set[str] = set()
        # tasks need STRONG refs — asyncio keeps only weak ones, and a GC'd
        # pending task finalizing mid-flight segfaulted the app (2026-08-19)
        self._backfill_tasks: set[asyncio.Task] = set()
        # A5-6 (audit 2026-09-22): manual SELL/tighten and fire-and-forget
        # pushes were bare ensure_future calls — same weak-ref GC class as
        # above, so a manual SELL could silently never submit. _spawn holds
        # a strong ref here until done and logs any swallowed exception.
        self._oneshot_tasks: set[asyncio.Task] = set()
        self._asset_names: dict[str, str] = {}  # symbol → full company name
        import time as _time

        self._started_monotonic = _time.monotonic()
        self._last_scan_at: str | None = None
        self._polygon_status = "checking…"  # background health loop (10.1)
        self._polygon_task = None
        # ML shadow (agenda #3 redo, steps 2+3): stats snapshot refreshed
        # OFF the UI thread; the labeler runs only in shadow mode outside
        # regular hours. Never a DB query on the event loop.
        self._ml_stats_task = None
        self._ml_task = None
        self._reports_task = None  # weekly reports (agenda #4)
        self._ml_stats_value: dict = self._ml_stats_default()
        # the persisted switch position, from the very first push — the
        # default "off" snapshot briefly overrode the saved choice at
        # relaunch (2026-08-23: sticky across relaunches)
        self._ml_stats_value["mode"] = self._ml_mode()
        self._ml_labeled_session = 0
        self.engine = None  # EngineCore, created when the broker connects

    # -- fire-and-forget with a strong reference (A5-6) ---------------------

    def _spawn(self, coro, what: str = "oneshot"):
        """A5-6 (audit 2026-09-22): asyncio holds only WEAK references to
        tasks, so a bare ``ensure_future(actor.close_now(...))`` could be
        garbage-collected before it ran — a manual SELL that silently never
        submits (the 2026-08-19 GC segfault class). Every fire-and-forget in
        this file routes through here: the task is strongly held in
        ``_oneshot_tasks`` until done, and a raised exception is LOGGED
        instead of vanishing with the task object."""
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:  # no running loop (tests/startup) — quiet no-op
            coro.close()
            return None
        self._oneshot_tasks.add(task)

        def _done(t) -> None:
            self._oneshot_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error("oneshot task %r failed: %r", what, exc, exc_info=exc)

        task.add_done_callback(_done)
        return task

    # -- engine commands (the CommandBus for UI and Telegram alike) ---------

    async def command(self, name: str) -> str:
        """start/pause/resume/stop/kill from the UI; same path Telegram uses."""
        if self.engine is None:
            return "engine offline — broker not connected yet"
        # the Start button doubles as Resume while paused (§5 top bar)
        if name == "start" and self.engine.state.value == "paused":
            name = "resume"
        action = getattr(self.engine, name, None)
        if action is None:
            return f"unknown command: {name}"
        result = await action()
        logger.info("engine command %s → %s", name, result)
        return result

    async def test_entry(self) -> str:
        """Phase 5 demo: tiny bracket entry via the full engine path."""
        if self.engine is None:
            return "engine offline — broker not connected yet"
        if self._hub is None:
            return "no market data — cannot price the stop"
        symbol = self._watchlist[0]
        quote = self._hub.latest_quotes.get(symbol)
        price = None
        if quote is not None:
            price = float(getattr(quote, "ask_price", 0) or getattr(quote, "bid_price", 0) or 0)
        if not price:
            return f"no live quote for {symbol} yet — try again in a moment"
        return await self.engine.open_test_position(symbol, price)

    def _hourly_status_text(self) -> str:
        """WAVE 2 item 10: the book in one Telegram line, hourly."""
        try:
            core = self.engine
            parts = []
            day = ""
            if core is not None:
                for a in core.actors.values():
                    if a.state.value in ("open", "scaling_out") and a.avg_entry_price:
                        parts.append(a.spec.symbol)
            book = ", ".join(parts) if parts else "no open positions"
            state = getattr(core, "state", None)
            eng = state.value if state is not None else "off"
            return f"🕐 Wave hourly: {book}{day} — engine {eng}, watching."
        except Exception:
            return "🕐 Wave hourly: status unavailable"

    # message classes that survive MINIMUM mode (2026-09-20: "way less
    # notification but i could still send messages and ask for info") —
    # fills/banks, risk halts, and the day summary — plus ERRORS, which must
    # ALWAYS page. Everything else (signals, climax countdowns, routine stop
    # ratchets, hourly/status chatter, reports) is ON-mode only.
    #
    # A5-2 (audit 2026-09-22, the dropped-pushes bug): _push receives
    # TRANSLATED lines — core.py's telegram_trade_line runs BEFORE push — but
    # these markers were written against the RAW log strings ("entry filled",
    # "WIN", "day summary"…), so in minimum mode every fill, close, halt,
    # summary and error silently dropped. Two-part fix:
    #   1) _push grew an optional `klass` argument; every _push call site
    #      inside this file classifies itself (see _MINIMUM_KLASSES);
    #   2) unclassified lines (producers outside this file: core.py _notify /
    #      telegram_trade_line, error_alerts.py) match markers copied
    #      VERBATIM from the actual translated strings, cited below.
    _MINIMUM_MARKERS = (
        # -- translated trade lines (core.py telegram_trade_line) ----------
        "Bought",  # "✅ Bought {n} shares of {sym} at ${px}. Protected at ${stop}."
        "sold:",  # "🟢 {sym} sold: WON {amt} $ …" / "🔴 {sym} sold: LOST {amt} $ …"
        "Shorted",  # S4: "🔻 Shorted {n} shares of {sym} at ${px}. Protected at ${stop}."
        "covered:",  # S4: "🟢 {sym} covered: WON …" / "🔴 {sym} covered: LOST …"
        "banked",  # "💰 {sym} — banked part ({n} shares at ${px}). The rest keeps riding."
        "frozen by the exchange",  # "⏸️ {sym} is frozen by the exchange (too fast a move). …"
        "unfrozen",  # "▶️ {sym} unfrozen — managing again." (LULD halt resolution)
        # -- engine lifecycle / risk (core.py _notify) ----------------------
        "Market closed",  # "🌙 Market closed. Today: …" (also klass="summary" at its call site)
        "don't match",  # "⚠️ Numbers don't match the broker ({n} issue(s)). …" and the
        #                 "▶️ Trading started.\n⚠️ Found {n} thing(s) that don't match…" variant
        "KILL SWITCH",  # "🛑 KILL SWITCH. Selling everything now …"
        "Stopping",  # "⏹ Stopping. No new buys. Selling open positions, then Wave rests."
        "Paused",  # "⏸ Paused. No new buys. Open positions are still protected …"
        # -- legacy raw-string markers, kept for back-compat: raw (never-
        # translated) lines can still reach _push from tests and the
        # telegram_trade_line "{sym}: {text}" fallthrough ---------------------
        "filled",
        "WIN",
        "LOSS",
        "halt",
        "HALT",
        "kill",
        "day summary",
        "Daily summary",
    )
    # klass values that survive minimum; anything else classified is ON-only.
    # Decided OUT of minimum (the contract is fills/banks/halts/summary +
    # errors, nothing more): "status" (market-open bell, PMOM auction queue &
    # fallback notes, brain graduation/veto), "signal" (auto-trade-OFF signal
    # pushes), "report" (weekly reports), hourly watch line, stop ratchets.
    _MINIMUM_KLASSES = frozenset({"fill", "bank", "halt", "risk", "summary"})
    # error texts that ALWAYS page in minimum even when they arrive
    # unclassified — verbatim from error_alerts.py ("⚠️ ERROR — {name}\n{msg}",
    # "⚠️ ERROR (still happening — …", "⚠️ CONNECTION NOT HEALING — …") and
    # this file's preflight alarms ("🚨 9:00 preflight …").
    _ALWAYS_PAGE_PREFIXES = ("⚠️ ERROR", "⚠️ CONNECTION NOT HEALING", "🚨")

    async def _push(self, text: str, klass: str | None = None) -> None:
        # TELEGRAM MODE (2026-09-20): on / minimum / off — commands
        # always answer regardless; only PUSHES are filtered here.
        # OFF drops EVERY push, errors included (silence is that mode's
        # contract; commands still answer). MINIMUM passes the classes above
        # plus errors, which bypass the filter entirely (A5-2).
        try:
            from waveapp.config import AppConfig as _MuteCfg

            _cfg = _MuteCfg.load()
            mode = str(getattr(_cfg, "telegram_mode", "minimum") or "minimum").lower()
            if mode == "off":
                return
            if mode == "minimum":
                is_error = klass == "error" or text.startswith(self._ALWAYS_PAGE_PREFIXES)
                if not is_error:
                    if klass is not None:
                        if klass not in self._MINIMUM_KLASSES:
                            return
                    elif not any(m in text for m in self._MINIMUM_MARKERS):
                        return
        except Exception:  # noqa: S110 — config unreadable, default to pushing
            pass
        if self._bridge is not None:
            await self._bridge.push(text)

    def _make_engine(self) -> None:
        from waveapp.config import AppConfig
        from waveapp.engine.core import EngineCore
        from waveapp.engine.risk import RiskEngine, RiskLimits

        def state_cb(state) -> None:
            if self._on_engine_state is not None:
                self._on_engine_state(state.value)

        # risk limits come from config (8.10 Settings edits them, Touch ID
        # gated); defaults match §12
        limits = RiskLimits()
        try:
            config = AppConfig.load()
            limits = RiskLimits(
                risk_per_trade_pct=config.risk_per_trade_pct,
                max_daily_loss_pct=config.max_daily_loss_pct,
                max_weekly_loss_pct=config.max_weekly_loss_pct,
                max_positions=config.max_positions,
                max_notional_pct=config.max_notional_pct,
                impact_participation_pct=config.impact_participation_pct,
                max_total_risk_pct=getattr(config, "max_total_risk_pct", 6.0),
                max_positions_hard=getattr(config, "max_positions_hard", 12),
                # S1: the short-side MASTER gate — can_enter refuses every
                # SELL entry while this is False (the config default)
                shorts_enabled=bool(getattr(config, "shorts_enabled", False)),
            )
        except Exception:
            logger.exception("risk limits config load failed — using §12 defaults")
        # Master Key mode (promoted to drive 2026-09-09): the
        # classic per-minute layers stand down and the v14 brain drives exits
        # per second. Hard rule 3 (server-side stop) is untouched either way.
        exit_params_fn = None
        try:
            if getattr(AppConfig.load(), "kitchen", "master_key") == "master_key":
                from waveapp.engine.exits import mk_delegated_params_for

                exit_params_fn = mk_delegated_params_for
                logger.info("kitchen = MASTER KEY (classic layers delegated)")
        except Exception:
            logger.exception("kitchen config read failed — classic kitchen")
        self.engine = EngineCore(
            self._adapter,
            database=self._database,
            risk=RiskEngine(limits=limits),
            on_state=state_cb,
            push=self._push,
            on_trade=self._on_trade,
            exit_params_fn=exit_params_fn,
        )
        if self._on_engine_state is not None:
            self._on_engine_state("idle")  # connected: chrome leaves DISCONNECTED

    def _ladder_event(self, kind: str, symbol: str) -> None:
        """Actor hook: count every ladder posting/capture/fallback for the
        System tab (the fallback is Wave 'falling to the old one')."""
        if kind in self._ladder_stats:
            self._ladder_stats[kind] += 1

    async def run(self) -> None:
        self._install_error_alerts()
        from waveapp.engine.actor import PositionActor as _actor_cls

        _actor_cls.ladder_event_hook = self._ladder_event
        # RE-PEG quote source (2026-09-16): the actor asks for a fresh mid
        # before its market fallback; hub-less contexts stay old-behavior.
        _actor_cls.quote_source = lambda sym: (
            self._hub.latest_quotes.get(sym) if self._hub is not None else None
        )
        if self._polygon_task is None:
            self._polygon_task = asyncio.ensure_future(self._polygon_loop())
        if self._ml_stats_task is None:
            self._ml_stats_task = asyncio.ensure_future(self._ml_stats_loop())
        if self._ml_task is None:
            self._ml_task = asyncio.ensure_future(self._ml_labeler_loop())
        if self._nightly_ml_task is None:
            self._nightly_ml_task = asyncio.ensure_future(self._nightly_ml_loop())
        if self._reports_task is None:
            self._reports_task = asyncio.ensure_future(self._reports_loop())
        if getattr(self, "_heartbeat_task", None) is None:
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        # A5-4 (audit 2026-09-22): the cycle body used to run bare — any
        # exception besides CancelledError (EngineCore ctor inside
        # _make_engine, DataHub(... AppConfig.load()) on a corrupt config, a
        # status callback throwing) ended the monitor SILENTLY: dots frozen
        # green, no liveness polls, no missed-fill replay, no bridge
        # resurrection. Every iteration is now guarded and the loop retries;
        # CancelledError still tears down exactly as before.
        try:
            while True:
                try:
                    if self._adapter is None:
                        await self._try_connect()
                    else:
                        await self._poll()
                    await self._ensure_telegram()
                    self._report_db_health()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log_cycle_error(exc)
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                if self._cycle_error_count:
                    logger.info(
                        "monitor cycle healthy again after %d failed cycle(s)",
                        self._cycle_error_count,
                    )
                    self._last_cycle_error = ""
                    self._cycle_error_count = 0
                await asyncio.sleep(self._poll_interval())
        except asyncio.CancelledError:
            await self._teardown()
            raise

    def _log_cycle_error(self, exc: BaseException) -> None:
        """A5-4: full traceback on the first hit and whenever the error
        CHANGES; identical consecutive failures collapse to a counted
        one-liner every 20th repeat (no log spam on a stuck config/broker)."""
        error = f"{type(exc).__name__}: {exc}"
        if error == self._last_cycle_error:
            self._cycle_error_count += 1
            if self._cycle_error_count % 20 == 0:
                logger.error(
                    "monitor cycle failed %d× in a row (%s) — still retrying",
                    self._cycle_error_count,
                    error,
                )
        else:
            self._last_cycle_error = error
            self._cycle_error_count = 1
            logger.exception("monitor cycle failed — retrying")

    def _poll_interval(self) -> float:
        """Slow WAY down while the market is CLOSED — no weekend API churn."""
        if self._adapter is None:
            return RETRY_SECONDS
        try:
            from waveapp.engine.session import Regime, SessionScheduler

            if SessionScheduler.regime() is Regime.CLOSED:
                return CLOSED_POLL_SECONDS
        except Exception:
            logger.exception("regime check failed — normal poll cadence")
        return POLL_SECONDS

    async def _polygon_loop(self) -> None:
        """Polygon subscription health for the System tab (Phase 10.1):
        one cheap reference call, hourly when healthy, 5-min retry when not."""
        import httpx

        from waveapp.research.history import KEYCHAIN_POLYGON_KEY, POLYGON_BASE
        from waveapp.security import secrets as _secrets

        while True:
            key = _secrets.get_secret(KEYCHAIN_POLYGON_KEY)
            if not key:
                self._polygon_status = "no key in Keychain"
            else:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as http:
                        response = await http.get(
                            f"{POLYGON_BASE}/v3/reference/tickers",
                            params={"limit": 1, "apiKey": key},
                        )
                    if response.status_code == 200:
                        self._polygon_status = "connected"
                    elif response.status_code in (401, 403):
                        self._polygon_status = "key rejected"
                    else:
                        self._polygon_status = f"HTTP {response.status_code}"
                except Exception:
                    self._polygon_status = "unreachable"
            await asyncio.sleep(3600 if self._polygon_status == "connected" else 300)

    async def _ensure_telegram(self) -> None:
        # A5-3 (audit 2026-09-22): mutual exclusion only — the check→start
        # sequence must never interleave with apply_telegram_user_id's
        # check→stop→start (or with teardown's stop). No behavior change
        # inside the lock.
        async with self._tg_lock:
            await self._ensure_telegram_locked()

    async def _ensure_telegram_locked(self) -> None:
        """The real body — caller MUST hold self._tg_lock (A5-3)."""
        from waveapp.telegram.bridge import TelegramBridge

        if self._bridge is not None and self._bridge.is_running:
            return
        if self._telegram_user_id <= 0:
            self._on_status(
                "Telegram", theme.GREY, "Telegram: not configured (set telegram_user_id)"
            )
            return
        if TelegramBridge.token_from_keychain() is None:
            self._on_status("Telegram", theme.GREY, "Telegram: bot token missing from Keychain")
            # the status dot alone hid a dead bridge for hours (2026-09-21)
            # — say it in the Log tab too, at most once per 10 minutes
            now = time.monotonic()
            if now - getattr(self, "_tg_token_warn_mono", -1e9) > 600:
                self._tg_token_warn_mono = now
                logger.error(
                    "Telegram bot token unreadable from Keychain — bridge cannot"
                    " start (after a rebuild, macOS may prompt: choose Always Allow)"
                )
            return
        try:
            monitor = self

            class _EngineSink:
                async def pause(self) -> str:
                    return await monitor.command("pause")

                async def resume(self) -> str:
                    return await monitor.command("resume")

                async def stop(self) -> str:
                    return await monitor.command("stop")

                async def kill(self) -> str:
                    return await monitor.command("kill")

            bridge = TelegramBridge(
                allowed_user_id=self._telegram_user_id,
                adapter_provider=lambda: self._adapter,
                command_sink=_EngineSink(),
                test_entry=self.test_entry,
                stats_provider=self.telegram_stats,
            )
            await bridge.start()
            self._bridge = bridge
            self._on_status("Telegram", theme.GREEN, "Telegram: bridge polling")
            # request (2026-08-14): confirm liveness on every start
            await bridge.push("🌊 Wave is on (paper money). Watching the market.")
            pending = getattr(self, "_pending_bridge_push", None)
            if pending:
                self._pending_bridge_push = None
                await bridge.push(pending)
        except Exception as exc:
            logger.warning("Telegram bridge start failed: %s", exc)
            self._on_status("Telegram", theme.RED, f"Telegram: failed — {exc}")

    def _close_summary_text(self) -> str:
        stats = self.telegram_stats()
        pnl = stats.get("today_pnl")
        if pnl is None:
            return "🌙 Market closed."
        text = (
            f"🌙 Market closed. Today: {pnl:+,.2f} $ across "
            f"{stats.get('today_trades', 0)} trades ({stats.get('today_wins', 0)} won)."
        )
        drift = self._ledger_drift(pnl)
        if drift is not None and abs(drift) > 25.0:
            logger.error("DAILY BOOKKEEPING CHECK FAILED: ledger vs account drift %+.2f $", drift)
            text += (
                f"\n⚠️ Bookkeeping check: the trade ledger differs from the account"
                f" by {drift:+,.2f} $ today — flagged for review."
            )
        elif drift is not None:
            text += "\n✅ Books check out against the account."
        # R2 SNIPER: the day's counter line, only when the book did anything
        try:
            sn = getattr(self, "_sniper", None)
            if sn is not None and any(sn.stats.values()):
                s = sn.stats
                text += (
                    f"\n🎯 Sniper: {s['converted']} converted, {s['triggered']} FPB "
                    f"entries, {s['expired']} skips, {s['starved']} starved, "
                    f"{s['jewel']} jewel-sized."
                )
        except Exception:  # noqa: S110 — a summary line never breaks the summary
            pass
        return text

    def _ledger_drift(self, today_booked: float) -> float | None:
        """Daily proof the books are honest (2026-08-23, after the −$655
        week-1 undercount): at the close (positions flat by the daily
        flatten), account-equity change minus cashflows MUST equal the
        booked trade P&L. Any gap > $25 raises an alert the same day."""
        if self._database is None:
            return None
        try:
            from datetime import UTC, datetime

            today = datetime.now(UTC).date().isoformat()
            first = self._database.query(
                "SELECT equity FROM equity_snapshots WHERE ts LIKE ? ORDER BY ts LIMIT 1",
                (f"{today}%",),
            )
            last = self._database.query(
                "SELECT equity FROM equity_snapshots WHERE ts LIKE ? ORDER BY ts DESC LIMIT 1",
                (f"{today}%",),
            )
            if not first or not last:
                return None
            flows = self._database.query(
                "SELECT COALESCE(SUM(amount),0) AS f FROM cashflows WHERE ts LIKE ?",
                (f"{today}%",),
            )
            account_change = float(last[0]["equity"]) - float(first[0]["equity"])
            account_change -= float(flows[0]["f"]) if flows else 0.0
            return round(account_change - today_booked, 2)
        except Exception:
            logger.exception("ledger drift check failed")
            return None

    def telegram_stats(self) -> dict:
        """Plain numbers for /status /pnl /today /week /scanner (2026-08-22)."""
        out: dict = {"positions": 0, "scanner": getattr(self, "_last_scan_summary", None)}
        if self.engine is not None:
            from waveapp.engine.actor import TERMINAL_STATES

            out["positions"] = sum(
                1 for a in self.engine.actors.values() if a.state not in TERMINAL_STATES
            )
        if self._database is None:
            return out
        try:
            from datetime import UTC, datetime, timedelta

            today = datetime.now(UTC).date().isoformat()
            rows = self._database.query(
                "SELECT symbol, realized_pnl FROM positions WHERE state='closed'"
                " AND realized_pnl IS NOT NULL AND closed_at LIKE ? ORDER BY closed_at",
                (f"{today}%",),
            )
            out["today_list"] = [(r["symbol"], float(r["realized_pnl"])) for r in rows]
            out["today_pnl"] = round(sum(p for _s, p in out["today_list"]), 2)
            out["today_trades"] = len(out["today_list"])
            out["today_wins"] = sum(1 for _s, p in out["today_list"] if p > 0)
            monday = (
                datetime.now(UTC).date() - timedelta(days=datetime.now(UTC).weekday())
            ).isoformat()
            rows = self._database.query(
                "SELECT substr(closed_at,1,10) day, symbol, realized_pnl FROM positions"
                " WHERE state='closed' AND realized_pnl IS NOT NULL AND closed_at >= ?"
                " ORDER BY closed_at",
                (monday,),
            )
            per_day: dict[str, list[float]] = {}
            best = worst = None
            for r in rows:
                per_day.setdefault(r["day"], []).append(float(r["realized_pnl"]))
                pnl = float(r["realized_pnl"])
                if best is None or pnl > best[1]:
                    best = (r["symbol"], pnl)
                if worst is None or pnl < worst[1]:
                    worst = (r["symbol"], pnl)
            out["week_days"] = [
                (day, round(sum(vals), 2), len(vals)) for day, vals in sorted(per_day.items())
            ]
            out["week_pnl"] = round(sum(p for _d, p, _n in out["week_days"]), 2)
            out["week_best"], out["week_worst"] = best, worst
        except Exception:
            logger.exception("telegram stats failed")
        return out

    def _report_db_health(self) -> None:
        if self._database is None:
            self._on_status("DB", theme.GREY, "DB: not opened")
            return
        try:
            version = self._database.schema_version()
            size_mb = self._database.path.stat().st_size / 1_048_576
            self._on_status(
                "DB",
                theme.GREEN,
                f"DB: {self._database.path.name} — schema v{version}, {size_mb:.1f} MB",
            )
        except Exception as exc:
            logger.warning("DB health check failed: %s", exc)
            self._on_status("DB", theme.RED, f"DB: health check failed — {exc}")

    async def _try_connect(self) -> None:
        adapter = AlpacaAdapter(TradingMode.PAPER)
        try:
            await adapter.connect()
        except MissingCredentialsError:
            self._on_status("Alpaca", theme.GREY, "Alpaca: no paper keys in Keychain yet")
            self._on_status("Data feed", theme.GREY, "Data feed: waiting for Alpaca keys")
            return
        except Exception as exc:
            logger.warning("Alpaca connect failed: %s", exc)
            self._on_status("Alpaca", theme.RED, f"Alpaca: connection failed — {exc}")
            self._on_status("Data feed", theme.RED, "Data feed: down")
            return
        self._adapter = adapter
        self._on_status("Alpaca", theme.GREEN, "Alpaca: connected (paper)")
        self._make_engine()
        if self._on_positions is not None and self._positions_task is None:
            self._positions_task = asyncio.ensure_future(self._positions_loop())

        from waveapp.config import AppConfig

        hub = DataHub(
            self._watchlist,
            on_bar=self._on_closed_bar,
            on_trading_status=self._on_trading_status,
            feed=AppConfig.load().data_feed,
        )
        hub.watchdog.on_stale_change = self._on_feed_stale
        await self._start_scanner(adapter)
        # auto-start (2026-08-23: "everything needs to be automatic —
        # make sure it works while I sleep"): with auto-trade armed, the
        # engine must never wait for a human to press Start after a launch
        try:
            from waveapp.config import AppConfig as _AutoCfg

            if (
                _AutoCfg.load().auto_trade
                and self.engine is not None
                and self.engine.state.value == "idle"
            ):
                logger.info("auto-trade is ON — starting the engine automatically")
                result = await self.command("start")
                logger.info("engine auto-start → %s", result)
                # the engine's own "▶️ Trading started." push happened BEFORE
                # the Telegram bridge existed and vanished (2026-08-23:
                # "im not sure trading started") — repeat it once the bridge
                # is up so the phone always gets both messages
                if "running" in result:
                    self._pending_bridge_push = "▶️ Trading started (auto-start)."
        except Exception:
            logger.exception("engine auto-start failed — press Start manually")
        try:
            await hub.start()
            self._hub = hub
        except Exception as exc:
            logger.warning("DataHub start failed: %s", exc)
            self._on_status("Data feed", theme.RED, f"Data feed: failed to start — {exc}")
            return
        self._report_feed_health()
        # Master Key kitchen (promoted to drive 2026-09-09):
        # in "master_key" mode the v14 brain DRIVES exits and rebuys through
        # the standard actor/entry machinery; in "classic" mode it shadows.
        try:
            from waveapp.config import AppConfig as _MKCfg
            from waveapp.config import support_dir

            _mk_cfg = _MKCfg.load()
            if _mk_cfg.shadow_kitchen and self._shadow_kitchen_task is None:
                from waveapp.engine.shadow_kitchen import ShadowKitchen

                mk_mode = (
                    "drive"
                    if getattr(_mk_cfg, "kitchen", "master_key") == "master_key"
                    else "shadow"
                )
                # DEMAND 21 knobs honored at construction (audit 2026-09-16:
                # the kitchen froze DEFAULT_PARAMS, making "failsafe_x = 0
                # disarms" a false claim). Config wins; restart applies it.
                import dataclasses as _mk_dc

                from waveapp.engine.master_key import DEFAULT_PARAMS as _MK_DEF

                mk_params = _mk_dc.replace(
                    _MK_DEF,
                    failsafe_x=float(getattr(_mk_cfg, "failsafe_x", _MK_DEF.failsafe_x)),
                    failsafe_n_min=int(getattr(_mk_cfg, "failsafe_n_min", _MK_DEF.failsafe_n_min)),
                    failsafe_k=float(getattr(_mk_cfg, "failsafe_k", _MK_DEF.failsafe_k)),
                )
                self._shadow_kitchen = ShadowKitchen(
                    hub_getter=lambda: self._hub,
                    actors_getter=lambda: self.engine.actors if self.engine else {},
                    journal_dir=support_dir() / "research",
                    mode=mk_mode,
                    rebuy_cb=self._mk_rebuy if mk_mode == "drive" else None,
                    notify_cb=lambda text: self._spawn(self._push(text), "kitchen notify push"),
                    params=mk_params,
                    # M0: the kitchen's DB pen — judge_transitions rows and
                    # their forward outcomes through this monitor's Database
                    journal_db_cb=self._ml_journal_write,
                    # R1.5: per-judged-second (key, profit_atr, age_ms) into
                    # the efficacy tracker's first-touch scorer
                    efficacy_cb=self._efficacy_feed,
                )
                self._shadow_kitchen_task = asyncio.ensure_future(self._shadow_kitchen.run())
                logger.info("Master Key kitchen task started (%s mode)", mk_mode)
        except Exception:
            logger.exception("Master Key kitchen failed to start (trading unaffected)")
        await self._snapshot_equity()  # balance shows immediately, not next poll
        if self._on_system is not None:  # …and so does the System tab
            try:
                self._on_system(self._system_data())
            except Exception:
                logger.exception("system snapshot push failed")

    async def _poll_missed_fills(self) -> None:
        """REST deafness insurance (2026-08-24): every poll, live actors'
        orders are checked against broker truth — a dead stream can no
        longer hide fills."""
        if self.engine is None:
            return
        try:
            replayed = await self.engine.poll_missed_fills()
            if replayed:
                logger.warning("REST fallback replayed %d missed fill(s)", replayed)
        except Exception:
            logger.exception("missed-fill poll failed")

    async def _poll(self) -> None:
        if self._adapter is None:
            return
        await self._poll_missed_fills()
        try:
            clock = await self._adapter.get_clock(TradingMode.PAPER)
        except Exception as exc:
            logger.warning("Alpaca health check failed: %s", exc)
            self._on_status("Alpaca", theme.RED, f"Alpaca: health check failed — {exc}")
            self._on_status("Data feed", theme.RED, "Data feed: down")
            await self._teardown()
            return
        was_open = self._market_open if self._market_open_known else None
        self._market_open = bool(clock.is_open)
        self._market_open_known = True
        # automatic open/close messages (2026-08-22)
        if was_open is not None and was_open != self._market_open:
            if self._market_open:
                self._spawn(
                    self._push("🔔 Market is open — Wave is hunting.", klass="status"),
                    "market-open push",
                )
                # A4-8 (audit 2026-09-22): the staleness freeze is edge-
                # triggered — a channel that went stale BEFORE the open was
                # logged "feed quiet … while market closed — normal" and the
                # watchdog never re-fires an edge, so a dead feed sailed into
                # the open unfrozen. Re-evaluate on the closed→open flip.
                self._refreeze_stale_channels_at_open()
            else:
                # the day summary is part of MINIMUM's contract (A5-2)
                self._spawn(
                    self._push(self._close_summary_text(), klass="summary"),
                    "close-summary push",
                )
        market = "market open" if clock.is_open else "market closed"
        # hourly Telegram push REMOVED (2026-09-16 13:20: "not
        # something i want to go to telegram") — the log keeps the line.
        if self._market_open:
            import time as _hb_time

            now_hb = _hb_time.time()
            if now_hb - getattr(self, "_last_status_push", 0.0) >= 3600.0:
                self._last_status_push = now_hb
                logger.info(self._hourly_status_text())
        self._on_status("Alpaca", theme.GREEN, f"Alpaca: connected (paper) — {market}")
        self._report_feed_health()
        await self._snapshot_equity()
        if self._on_system is not None:
            try:
                self._on_system(self._system_data())
            except Exception:
                logger.exception("system snapshot push failed")

    def _on_trading_status(self, symbol: str, halted: bool) -> None:
        if self.engine is not None:
            self.engine.on_symbol_halt(symbol, halted)

    def _on_closed_bar(self, bar) -> None:
        """Closed 1-min bars feed the adaptive exit system (§8.2)."""
        if self.engine is not None and self._hub is not None:
            self.engine.on_market_bar(bar, self._hub)

    def _ssr_sweep(self, today) -> None:
        """S0 (audit A3-8): SSR / Rule 201 detection, wired for real —
        record_ssr_trigger finally has a caller. A symbol trading ≤ −10%
        vs its PRIOR session close triggers the restriction; risk.ssr_active
        owns the window (rest of day + next trading day). Scope is what
        Wave actually watches — the day list, open positions and
        event-promoted names — read from scanner2's live arrays (last price
        + prev_close_live), never all 13k. Runs every minute boundary
        REGARDLESS of shorts_enabled: SSR state is factual; only the short
        entry gate in risk.can_enter consumes it."""
        engine = self.engine
        s2 = self._scanner2
        if engine is None or s2 is None:
            return
        try:
            watch: set[str] = set(getattr(self, "_day_list", None) or [])
            watch |= getattr(self, "_promoted_today", None) or set()
            watch |= {a.spec.symbol for a in engine.actors.values()}
            for symbol in watch:
                if engine.risk.ssr_trigger_date(symbol) == today:
                    continue  # today's trigger already on record — once, not per minute
                row = s2._index.get(symbol)
                if row is None:
                    continue
                px = float(s2.last[row])
                prev = float(s2.prev_close_live[row])
                if px > 0 and prev > 0 and px <= prev * 0.90:
                    engine.risk.record_ssr_trigger(symbol, today)
        except Exception:
            logger.exception("SSR sweep failed — next minute retries")

    # -- DAY JUDGE (R0, 2026-09-23) — observation + badge only ---------------

    def _day_judge_breadth(self) -> tuple[float | None, float | None]:
        """(frac_below_open, median |day %|) over today's menu — the SAME
        population the menu-breadth recorder reads from the log ("scanner2
        focus +" ∪ "day list (scanner2 LIVE): +") and the same comparison
        (last print vs the symbol's own day open), computed from scanner2's
        live arrays instead of REST snapshots. None before any menu exists."""
        s2 = self._scanner2
        if s2 is None:
            return None, None
        try:
            menu: set[str] = set(s2.focus)
            menu |= getattr(self, "_s2_watched", None) or set()
            below = ok = 0
            day_pcts: list[float] = []
            for symbol in menu:
                row = s2._index.get(symbol)
                if row is None:
                    continue
                px = float(s2.last[row])
                day_open = float(s2.day_open[row])
                if px <= 0 or day_open <= 0:
                    continue
                ok += 1
                if px < day_open:
                    below += 1
                day_pcts.append(abs(px / day_open - 1.0) * 100.0)
            if ok == 0:
                return None, None
            day_pcts.sort()
            median = day_pcts[len(day_pcts) // 2]
            return below / ok, median
        except Exception:
            logger.debug("day judge breadth failed", exc_info=True)
            return None, None

    def _day_regime_write(self, op: str, payload: dict):
        """The Day Judge's DB pen (it holds no Database by design — the M0
        journal_db_cb pattern): one day_regime row per verdict change."""
        db = self._database
        if db is None or op != "day_regime":
            return None
        try:
            cur = db.execute(
                "INSERT INTO day_regime (ts, verdict, confidence, breadth, evidence)"
                " VALUES (:ts, :verdict, :confidence, :breadth, :evidence)",
                payload,
            )
            return cur.lastrowid
        except Exception:
            logger.debug("day_regime write failed", exc_info=True)
            return None

    # -- EFFICACY GUARD (R1.5, 2026-09-24 — the −$3,928 morning) -------------

    def _efficacy_write(self, op: str, payload: dict):
        """The tracker's DB pen (it holds no Database by design — the M0
        journal_db_cb pattern): one efficacy_events row per mode flip."""
        db = self._database
        if db is None or op != "efficacy_event":
            return None
        try:
            cur = db.execute(
                "INSERT INTO efficacy_events (ts, day, from_mode, to_mode, reason,"
                " consecutive_fails, n_pass, n_fail) VALUES (:ts, :day, :from_mode,"
                " :to_mode, :reason, :consecutive_fails, :n_pass, :n_fail)",
                payload,
            )
            return cur.lastrowid
        except Exception:
            logger.debug("efficacy_events write failed", exc_info=True)
            return None

    def _efficacy_notify(self, text: str) -> None:
        """A mode flip pages the phone — klass="risk" survives Telegram's
        minimum mode by contract (fills/banks/halts/risk/summary)."""
        try:
            self._spawn(self._push(text, klass="risk"), "efficacy flip push")
        except Exception:
            logger.debug("efficacy notify push failed", exc_info=True)

    def _efficacy_feed(self, key: str, profit_atr: float, age_ms: int) -> None:
        """The kitchen's per-judged-second early-outcome reading → first-touch
        scoring in the tracker. Never allowed to wound the caller."""
        try:
            self._efficacy.record_tick(key, float(profit_atr), int(age_ms))
        except Exception:
            logger.debug("efficacy tick feed failed", exc_info=True)

    def _record_efficacy_entry(self, key: str, actor) -> None:
        """Register a FILLED entry with the tracker at first sight of the
        open actor. An adopted (restart) position whose fill is already
        older than the scoring window has nothing scoreable — skipped."""
        try:
            import time as _time

            from waveapp.engine.efficacy import EFFICACY_WINDOW_MIN

            now_ms = int(_time.time() * 1000)
            filled = getattr(actor, "entry_filled_at", None) or getattr(
                actor, "adopted_entry_time", None
            )
            t_ms = int(filled.timestamp() * 1000) if filled is not None else now_ms
            if now_ms - t_ms > EFFICACY_WINDOW_MIN * 60_000:
                return
            side = str(getattr(actor.spec.side, "value", actor.spec.side) or "")
            self._efficacy.record_entry(key, actor.spec.symbol, side, t_ms)
        except Exception:
            logger.debug("efficacy entry record failed", exc_info=True)

    # -- PRE-OPEN BIAS (R0.5, 2026-09-24) ------------------------------------

    def _preopen_tick(self, now_et) -> None:
        """Runs at every scanner2 minute boundary: (a) the Day Judge handoff
        — its first non-UNCLEAR verdict retires the pre-open bias and clears
        the efficacy seed; (b) the one 9:25 computation, from scanner2's
        live pre-market arrays, with the loud log + phone push + day_regime
        journal. Flag off (config preopen_bias=false) = nothing happens."""
        import contextlib as _ctx

        po = self._preopen
        verdict, _conf = self._day_judge_reading()
        if po.note_day_judge(verdict):
            # the live verdict + efficacy own the day from here
            with _ctx.suppress(Exception):
                self._efficacy.set_bias_hint(None)
        if not po.should_compute(now_et):
            return
        from waveapp.config import AppConfig

        try:
            if not bool(getattr(AppConfig.load(), "preopen_bias", True)):
                return
        except Exception:
            logger.debug("preopen config read failed — computing (default ON)")
        spy_gap = self._s2_gap_pct("SPY")
        qqq_gap = self._s2_gap_pct("QQQ")
        frac_down, n_map = self._premarket_gap_map()
        bias, conf = po.compute(now_et, spy_gap, qqq_gap, frac_down, n_map)
        with _ctx.suppress(Exception):
            # day-stamped so the tracker rolls onto TODAY first — otherwise
            # the day's first record_entry would wipe the fresh seed
            self._efficacy.set_bias_hint(po.hint(), day=po.computed_day)
        text = po.headline()
        logger.warning(text)  # the loud 9:25 line, WARNING so it never drowns
        self._spawn(self._push(text, klass="risk"), "preopen bias push")

    def _s2_gap_pct(self, symbol: str) -> float | None:
        """Pre-market gap % for one symbol from scanner2's live arrays: last
        print vs prior session close. None when either side is unknown —
        the bias never guesses."""
        s2 = self._scanner2
        try:
            row = s2._index.get(symbol) if s2 is not None else None
            if row is None:
                return None
            px = float(s2.last[row])
            prev = float(s2.prev_close_live[row])
            if px <= 0 or prev <= 0:
                return None
            return (px / prev - 1.0) * 100.0
        except Exception:
            logger.debug("preopen gap read failed for %s", symbol, exc_info=True)
            return None

    def _premarket_gap_map(self) -> tuple[float | None, int]:
        """(fraction of the watched menu gapping DOWN vs prior close, n).
        Population = the day-judge breadth population (scanner2 focus ∪ the
        announced day list) ∪ the current scanner2 menu — the names Wave is
        actually watching pre-market, compared last-print vs
        prev_close_live. (None, n) below MIN_MAP_N names."""
        from waveapp.engine.preopen import MIN_MAP_N

        s2 = self._scanner2
        if s2 is None:
            return None, 0
        try:
            menu: set[str] = set(s2.focus)
            menu |= getattr(self, "_s2_watched", None) or set()
            with contextlib.suppress(Exception):
                menu |= {e["symbol"] for e in (s2.last_menu or [])}
            down = ok = 0
            for symbol in menu:
                row = s2._index.get(symbol)
                if row is None:
                    continue
                px = float(s2.last[row])
                prev = float(s2.prev_close_live[row])
                if px <= 0 or prev <= 0:
                    continue
                ok += 1
                if px < prev:
                    down += 1
            if ok < MIN_MAP_N:
                return None, ok
            return down / ok, ok
        except Exception:
            logger.debug("preopen gap map failed", exc_info=True)
            return None, 0

    def _preopen_reading(self, now_et) -> str:
        """Defensive read for the entry pipeline: the bias string while the
        verdict GOVERNS (computed for this ET day, directional, Day Judge
        has not spoken), else NEUTRAL. Missing/garbage detector = NEUTRAL —
        the pipeline must work identically in builds without the brain."""
        try:
            po = getattr(self, "_preopen", None)
            if po is None or not po.governs(now_et.date().isoformat()):
                return "NEUTRAL"
            return str(po.bias)
        except Exception:
            return "NEUTRAL"

    # -- scanner (Phase 7) ---------------------------------------------------

    async def _start_scanner(self, adapter) -> None:
        from waveapp.data.features import AlpacaFeatureProvider
        from waveapp.engine.scanner import Scanner
        from waveapp.engine.tradegate import TradeGate

        try:
            assets = await adapter.get_assets(TradingMode.PAPER)
            self._asset_names = {
                a.symbol: (getattr(a, "name", "") or "") for a in assets
            }  # full names for the position detail popup (8.4 r5)
            from waveapp.config import AppConfig as _AppConfig

            provider = AlpacaFeatureProvider(
                assets,
                universe_size=self._scan_universe_size,
                progress_cb=self._on_scan_status,
                progress_units_cb=self._on_universe_progress,
                feed=_AppConfig.load().data_feed,
            )
            self._scanner = Scanner(
                provider=provider,
                gate=TradeGate(database=self._database),
                database=self._database,
                ssr_check=self._scanner_ssr_check,  # M0: journal-only SSR flag
            )
            self._scan_task = asyncio.ensure_future(self._scan_loop())
            logger.info("scanner started (%d-symbol scan set)", self._scan_universe_size)
            self._start_scanner2(assets, provider)
            # WAVE 2 item 1 (approved 2026-09-16): every loop supervised —
            # a crashed OR hung loop resurrects within a minute, loudly.
            from waveapp.engine.session import Regime, SessionScheduler
            from waveapp.engine.supervisor import Supervisor

            def _awake() -> bool:
                return SessionScheduler.regime() not in (Regime.CLOSED, Regime.OVERNIGHT)

            old_sup = getattr(self, "_supervisor", None)
            if old_sup is not None:  # reconnects must not leak a rival supervisor
                self._spawn(old_sup.stop(), "old supervisor stop")
            self._supervisor = Supervisor()
            self._supervisor.register(
                "scan_loop",
                self._scan_task,
                lambda: self._restart_loop("_scan_task", self._scan_loop),
                beat=lambda: getattr(self, "_last_scan_beat", None),
                stale_after=300.0,
                active=_awake,
            )
            for attr, factory in (
                ("_scanner2_task", self._scanner2_loop),
                ("_scanner2_news_task", self._scanner2_news_loop),
                ("_scanner2_feeds_task", self._scanner2_feeds_loop),
                ("_nightly_ml_task", self._nightly_ml_loop),
            ):
                task = getattr(self, attr, None)
                if task is not None:
                    self._supervisor.register(
                        attr.strip("_"),
                        task,
                        lambda a=attr, f=factory: self._restart_loop(a, f),
                    )
            self._supervisor.start()
        except Exception:
            logger.exception("scanner failed to start")

    def _start_scanner2(self, assets, provider) -> None:
        """Scanner 2.0 S1 SHADOW (2026-08-31): full-market
        awareness next to the old scanner. Trades nothing; a failure here
        must never touch the live pipeline."""
        try:
            from waveapp.config import AppConfig
            from waveapp.engine.scanner2 import Scanner2

            if not AppConfig.load().scanner2_shadow:
                logger.info("scanner2 shadow disabled in config")
                return
            self._scanner2 = Scanner2(provider._client, database=self._database, feed=provider.feed)
            # S3 (the short side): weak-side triggers (lod_break, symmetric
            # boosts) arm only behind the master flag — wired here exactly
            # like risk.limits.shorts_enabled in _make_engine. Default False.
            with contextlib.suppress(Exception):
                self._scanner2.shorts_enabled = bool(AppConfig.load().shorts_enabled)
            # Architecture B: the hub's bars* wildcard feeds the scanner,
            # halts/resumes become events, live quotes give focus spreads
            self._s2_bar_tap = self._scanner2.on_bar_msg  # cache the bound
            self._s2_status_tap = self._scanner2.on_status  # methods ONCE
            if self._hub is not None:
                with contextlib.suppress(Exception):
                    self._hub.bar_tap = self._s2_bar_tap
                    self._hub.status_tap = self._s2_status_tap
                    self._scanner2.latest_quotes = self._hub.latest_quotes
            self._scanner2_news_task = asyncio.ensure_future(self._scanner2_news_loop())
            self._scanner2_feeds_task = asyncio.ensure_future(self._scanner2_feeds_loop())
            # Brain Stage 1: load the trained artifact with checksum +
            # golden-vector self-test at startup. ANY doubt → rules-only,
            # loudly (the loader logs it). Shadow-only until the stages.
            try:
                from waveapp.config import AppConfig as _BrainCfg
                from waveapp.engine.brain import load_shadow_brain

                # M1 (2026-09-23): the shadow slot's artifact is picked by
                # config.ml_model ("v1" default = unchanged). Still SHADOW.
                self._brain = load_shadow_brain(_BrainCfg.load().ml_model)
            except Exception:
                logger.exception("brain load crashed — rules-only")
                self._brain = None
            self._load_brain_v2()
            try:
                from waveapp.engine.llmintel import DAILY_SPEND_CAP, load_llm

                self._llm = load_llm()
                if self._llm is not None:
                    from waveapp.config import AppConfig as _cfg

                    with contextlib.suppress(Exception):
                        config = _cfg.load()
                        self._llm_baseline = float(config.llm_spent_total)
                        self._llm_saved_total = self._llm_baseline
                        self._llm_budget = float(config.llm_budget)
                    logger.info(
                        "LLM news brain armed (Haiku, capped $%.2f/day, est. balance $%.2f)",
                        DAILY_SPEND_CAP,
                        self._llm_budget - self._llm_baseline,
                    )
            except Exception:
                logger.exception("llm load crashed — keyword tags stand")
                self._llm = None
            # WAVE 2 item 13 Tier A (approved 2026-09-16): the Yuval-brain
            # advisor — shadow-only, budget-armored, graded daily. No
            # trading path ever reads its verdicts.
            self._yuval_brain = None
            if self._llm is not None:
                try:
                    from waveapp.engine.yuval_brain import AdvisorEngine

                    self._yuval_brain = AdvisorEngine(
                        self._llm,
                        database=self._database,
                        budget=self._llm_budget,
                        spent_baseline=self._llm_baseline,
                    )
                    logger.info("Yuval-brain advisor armed (shadow, Tier A)")
                except Exception:
                    logger.exception("yuval-brain load failed — advisor absent")
            added, dropped = self._scanner2.set_universe(assets)
            if added or dropped:
                logger.info(
                    "scanner2 universe changes: +%d added, -%d dropped (%s%s)",
                    len(added),
                    len(dropped),
                    ", ".join((added + dropped)[:8]),
                    "…" if len(added) + len(dropped) > 8 else "",
                )
            self._scanner2_task = asyncio.ensure_future(self._scanner2_loop())
            logger.info("scanner2 SHADOW started (%d symbols watched)", len(self._scanner2.symbols))
        except Exception:
            logger.exception("scanner2 failed to start — live pipeline unaffected")

    async def _scanner2_loop(self) -> None:
        """Architecture B cadence (sign-off-by-word order, 2026-09-01):
        1-second re-rank from STREAM state; minute tick journals + folds;
        REST snapshots demoted to drift-repair — every 300s while bars flow,
        every 60s when the stream is stale (automatic degradation, menu
        never dies); focus promotions get tick-level subscriptions."""
        import contextlib as _ctx
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _zi

        from waveapp.engine.scanner2 import minute_index

        et = _zi("America/New_York")
        last_status = 0.0
        last_fetch = 0.0
        last_minute = None
        universe_day = None
        while True:
            try:
                now_et = _dt.now(et)
                # SELF-HEALING TAP (2026-09-01 day-1 bug: scanner2 started
                # ~100ms before the DataHub existed → bar/status taps never
                # attached → zero stream bars all day). Attach whenever the
                # hub exists and isn't wired to THIS scanner.
                hub = self._hub
                tap = getattr(self, "_s2_bar_tap", None)
                if tap is None:
                    tap = self._s2_bar_tap = self._scanner2.on_bar_msg
                    self._s2_status_tap = self._scanner2.on_status
                if hub is not None and getattr(hub, "bar_tap", None) is not tap:
                    with _ctx.suppress(Exception):
                        hub.bar_tap = tap
                        hub.status_tap = self._s2_status_tap
                        self._scanner2.latest_quotes = hub.latest_quotes
                        logger.info("scanner2 attached to the data stream (bars tap live)")
                if now_et.weekday() >= 5 or minute_index(now_et) is None:
                    await asyncio.sleep(30.0)
                    continue
                # daily universe refresh (know every add/drop) at ~7:5x ET
                if (
                    universe_day != now_et.date()
                    and now_et.hour == 7
                    and now_et.minute >= 50
                    and self._adapter is not None
                ):
                    universe_day = now_et.date()
                    await asyncio.to_thread(self._resolve_brain_scores)  # yesterday's ladder line
                    with _ctx.suppress(Exception):
                        from waveapp.broker.base import TradingMode as _tm

                        assets = await self._adapter.get_assets(_tm.PAPER)
                        added, dropped = self._scanner2.set_universe(assets)
                        if added or dropped:
                            logger.info(
                                "scanner2 universe refresh: +%d / -%d", len(added), len(dropped)
                            )
                now_ts = now_et.timestamp()
                # minute boundary: journal the closing minute BEFORE re-ranking
                if now_et.minute != last_minute:
                    last_minute = now_et.minute
                    self._scanner2.minute_tick(now_et)
                    # MINUTE-TICK ENTRY CLOCK (2026-09-04 09:33,
                    # 'it needs to be faster'): entries re-evaluate seconds
                    # after every bar close instead of waiting for the
                    # 60/120s scan cycle. Strategy triggers (bars, VWAP,
                    # breakout highs) come live from the hub; candidate
                    # features from the last scan, freshness-guarded.
                    self._schedule_entry_tick(now_et)
                    self._ssr_sweep(now_et.date())  # S0: Rule-201 detection (A3-8)
                    # DAY JUDGE (R0): one minute of evidence — the SAME
                    # breadth the menu-breadth recorder writes to the CSVs
                    with _ctx.suppress(Exception):
                        dj_breadth, dj_med = self._day_judge_breadth()
                        self._day_judge.update(now_et, dj_breadth, median_abs_day_pct=dj_med)
                    # PRE-OPEN BIAS (R0.5): the one 9:25 verdict + the Day
                    # Judge handoff, right after the judge's own minute
                    with _ctx.suppress(Exception):
                        self._preopen_tick(now_et)
                    if self._on_market_regime is not None:  # 4.1 chip
                        with _ctx.suppress(Exception):
                            regime_payload = dict(self._scanner2.market_regime)
                            # DAY JUDGE (R0): rides the same refresh to the
                            # Scanner tab's badge
                            regime_payload["day_regime"] = self._day_judge.snapshot()
                            self._on_market_regime(regime_payload)
                    self._brain_score_menu(now_et)  # the Brain drinks nonstop (shadow)
                    if self._on_scanner2_menu is not None and self._scanner2.last_menu:
                        with _ctx.suppress(Exception):
                            self._on_scanner2_menu(self._scanner2.last_menu)
                # REST snapshots: seed/repair only while the stream is healthy
                stream_fresh = now_ts - self._scanner2.stream_bars_ts < 90.0
                interval = 300.0 if stream_fresh else 60.0
                if now_ts - last_fetch >= interval:
                    last_fetch = now_ts
                    await self._scanner2._fetch_snapshots()
                    if not stream_fresh and now_ts - last_status > 900:
                        logger.warning(
                            "scanner2: bars stream quiet — REST polling carries the menu"
                        )  # noqa: E501
                menu = self._scanner2.rerank_only(now_et)
                promoted = self._scanner2.update_focus()
                if promoted and self._hub is not None:
                    with _ctx.suppress(Exception):
                        await self._hub.watch(promoted)
                        logger.info("scanner2 focus +%s", ", ".join(promoted))
                        self._note_first_watch(promoted)  # M0 entry telemetry
                # EVENT PROMOTER (adopted 2026-09-16): drain the event
                # queue into IMMEDIATE watch + guest scan — the same-minute
                # "look here now". Gates unchanged; A/B tag in the log.
                try:
                    from waveapp.config import AppConfig as _EpCfg

                    if getattr(_EpCfg.load(), "event_promoter", True):
                        queued = self._scanner2.promotion_queue
                        if queued:
                            batch = sorted(queued - getattr(self, "_promoted_today", set()))
                            queued.clear()
                            if batch:
                                promoted_set = getattr(self, "_promoted_today", None)
                                if promoted_set is None:
                                    promoted_set = self._promoted_today = set()
                                promoted_set.update(batch)
                                if self._hub is not None:
                                    await self._hub.watch(batch)
                                scanner = getattr(self, "_scanner", None)
                                provider = (
                                    getattr(scanner, "provider", None)
                                    if scanner is not None
                                    else None
                                )
                                if provider is not None and hasattr(provider, "add_guests"):
                                    provider.add_guests(batch)
                                logger.info(
                                    "EVENT-PROMOTED to watch (same-minute): %s",
                                    ", ".join(batch),
                                )
                                self._note_first_watch(batch)  # M0 entry telemetry
                                # CATCH-THE-CLIMB (#1 priority, 2026-09-23): an
                                # event-promoted igniter used to wait median
                                # 2-19 min more to crack the top-20 menu
                                # before entries were even POSSIBLE (PLTR:
                                # promoted 9:41, tradable 9:44). Give it a
                                # guest seat in the entry pool directly —
                                # every entry gate still stands downstream.
                                guests = getattr(self, "_igniter_guests", None)
                                if guests is None:
                                    guests = self._igniter_guests = {}
                                import time as _time

                                now_mono = _time.monotonic()
                                for sym in batch:
                                    guests[sym] = now_mono
                except Exception:
                    logger.exception("event promoter drain failed — next cycle")
                # NYSE tape: fresh headlines scroll on the Scanner tab
                if self._scanner2.news_feed and self._on_scanner2_news is not None:
                    items, self._scanner2.news_feed = (
                        self._scanner2.news_feed[:8],
                        self._scanner2.news_feed[8:],
                    )  # noqa: E501
                    for item in items:
                        with _ctx.suppress(Exception):
                            self._on_scanner2_news(item)
                # Scanner-tab mind: real scanner2 events light the canvas
                if self._scanner2.ui_events and self._on_scanner2_event is not None:
                    drained, self._scanner2.ui_events = self._scanner2.ui_events[:10], []
                    for symbol, kind in drained:
                        with _ctx.suppress(Exception):
                            self._on_scanner2_event(symbol, kind)
                if menu and now_ts - last_status > 900:  # status line every 15 min
                    last_status = now_ts
                    top = ", ".join(f"{m['symbol']} rvol {m['rvol']:g}" for m in menu[:5])
                    logger.info(
                        "scanner2 menu: %s | events today: %d", top, self._scanner2._events_seen
                    )  # noqa: E501
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scanner2 tick failed — retrying")
                await asyncio.sleep(5.0)
            await asyncio.sleep(1.0)

    async def _scanner2_news_loop(self) -> None:
        """Benzinga real-time news websocket (included in Algo Trader Plus):
        every article's symbols become catalyst flags. Fully optional — if
        the stream is unavailable the scanner runs without news boosts."""
        import time as _time

        try:
            from alpaca.data.live.news import NewsDataStream

            from waveapp.broker.alpaca import KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET
            from waveapp.security import secrets as _secrets

            key_id = _secrets.get_secret(KEYCHAIN_PAPER_KEY_ID)
            secret = _secrets.get_secret(KEYCHAIN_PAPER_SECRET)
            if not key_id or not secret:
                return
        except Exception:
            logger.info("news stream unavailable — scanner2 runs without catalyst flags")
            return

        async def on_article(article) -> None:
            with contextlib.suppress(Exception):
                from waveapp.engine.newsintel import classify

                symbols = [str(s).upper() for s in (getattr(article, "symbols", None) or [])]
                if symbols and self._scanner2 is not None:
                    headline = str(getattr(article, "headline", "") or "")
                    category, direction = classify(headline)
                    self._scanner2.on_news(
                        symbols,
                        _time.time(),
                        headline=headline,
                        category=category,
                        direction=direction,
                    )
                    self._maybe_llm(symbols, headline)

        backoff = 5.0
        while True:
            stream = None
            try:
                stream = NewsDataStream(key_id, secret)
                stream.subscribe_news(on_article, "*")
                logger.info("scanner2 news stream connected (Benzinga)")
                await stream._run_forever()
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    if stream is not None:
                        await stream.stop_ws()
                raise
            except Exception:
                logger.warning("news stream dropped — reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 300.0)

    # -- layer 7: LLM market understanding (the key, 2026-09-01) ---------

    def _maybe_llm(self, symbols: list[str], headline: str) -> None:
        """The triage gate (7.1): consult the LLM only for a FIRST-PRINT
        headline about a symbol on the menu or the day list. Fire-and-forget
        — the news path never waits on the network."""
        llm = self._llm
        if llm is None or self._scanner2 is None or not headline:
            return
        try:
            fresh = any(self._scanner2.news_novelty.get(s, 0.0) >= 1.0 for s in symbols)
            if not fresh and llm.cached(headline) is None:
                return  # repeats ride the cache or stay keyword-tagged
            watched = {m["symbol"] for m in (self._scanner2.last_menu or [])[:20]}
            watched |= set(getattr(self, "_day_list", None) or [])
            hot = [s for s in symbols if s in watched]
            if not hot:
                return
            if llm.cached(headline) is None and not llm.budget_ok():
                return
            task = asyncio.ensure_future(self._llm_task(hot, headline))
            self._llm_tasks.add(task)
            task.add_done_callback(self._llm_tasks.discard)
        except Exception:
            logger.debug("llm triage failed — keyword tags stand", exc_info=True)

    async def _llm_task(self, hot: list[str], headline: str) -> None:
        import time as _time

        try:
            from waveapp.engine.llmintel import EVENT_ID

            verdict = await asyncio.to_thread(self._llm.classify, hot[0], headline)
            if verdict is None or self._scanner2 is None:
                return
            tags = {
                "evt_id": EVENT_ID.get(verdict.event, 0),
                "event": verdict.event,
                "dir": verdict.direction,
                "mag": verdict.major,
                "rpu": verdict.rpu,
                "peers": verdict.peers,
            }
            self._scanner2.on_llm(hot, tags, _time.time())
            logger.info(
                "LLM verdict: %s → %s dir%+d%s%s peers=%s ($%.3f today)",
                ",".join(hot[:3]),
                verdict.event,
                verdict.direction,
                " MAJOR" if verdict.major else "",
                " RESOLVES-UNCERTAINTY" if verdict.rpu else "",
                ",".join(verdict.peers) if verdict.peers else "-",
                self._llm.spend_today,
            )
        except Exception:
            logger.exception("llm task failed — keyword tags stand")

    async def _scanner2_feeds_loop(self) -> None:
        """The free supplementary feeds (data-agent plan, all $0):
        - SEC EDGAR latest 8-K/S-3/424B5 filings → catalyst events (60s,
          10 req/s cap respected, declared User-Agent)
        - ApeWisdom retail-mention leaders → attention set (5 min)
        - Finnhub earnings-today calendar → scheduled-catalyst set (nightly;
          runs ONLY if a 'finnhub_api_key' Keychain entry exists)
        Every feed is optional: a failure logs once and retries later."""
        import json as _json
        import time as _time
        import urllib.request

        def _get_json(url: str, timeout: float = 15.0):
            req = urllib.request.Request(url, headers={"User-Agent": "WaveScanner yuval@wave.app"})  # noqa: S310, E501
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                return _json.loads(r.read())

        cik_map: dict[str, str] = {}  # CIK (int str) → ticker
        cik_day = None
        seen_filings: set[str] = set()
        last_ape = 0.0
        earnings_day = None
        while True:
            await asyncio.sleep(60.0)
            if self._scanner2 is None:
                continue
            now = _time.time()
            # -- EDGAR: latest catalyst/dilution filings ---------------------
            try:
                from datetime import date as _date

                if cik_day != _date.today():
                    data = await asyncio.to_thread(
                        _get_json, "https://www.sec.gov/files/company_tickers.json"
                    )
                    cik_map = {
                        str(row["cik_str"]): str(row["ticker"]).upper() for row in data.values()
                    }
                    cik_day = _date.today()
                    logger.info("scanner2 EDGAR map loaded: %d companies", len(cik_map))
                result = await asyncio.to_thread(
                    _get_json,
                    "https://efts.sec.gov/LATEST/search-index?q="
                    "&forms=8-K,S-3,424B5,S-1,SC%2013D,SC%2013G"
                    "&dateRange=custom&startdt={d}&enddt={d}".format(d=cik_day.isoformat()),
                )
                for hit in (result.get("hits", {}).get("hits") or [])[:50]:
                    source = hit.get("_source", {})
                    filing_id = str(hit.get("_id", ""))
                    if filing_id in seen_filings:
                        continue
                    seen_filings.add(filing_id)
                    for cik in source.get("ciks") or []:
                        ticker = cik_map.get(str(int(cik)) if str(cik).isdigit() else str(cik))
                        if ticker and ticker in self._scanner2._index:
                            # EFTS carries the form type in root_forms /
                            # file_type — "forms" is always None (bug found
                            # 2026-09-15: dilution/activist flags had fired
                            # ZERO times in 391k journal rows).
                            forms = ",".join(
                                source.get("root_forms")
                                or ([source["file_type"]] if source.get("file_type") else [])
                            )
                            # 4.8: dilution/activist semantics journaled
                            self._scanner2.flag_filing(ticker, forms, now)
                            # 4.8b: 8-K item codes carry the WHAT — 1.01
                            # deal (bullish), 2.02 results, 5.02 exec exit
                            items = [str(i) for i in (source.get("items") or [])]
                            category, direction = "filing", 0
                            if "1.01" in items:
                                category, direction = "deal", 1
                            elif "2.02" in items:
                                category = "earnings"
                            elif "5.02" in items:
                                direction = -1  # executive departure
                            detail = f" items {','.join(items)}" if items else ""
                            self._scanner2.on_news(
                                [ticker],
                                now,
                                headline=f"SEC filing {forms}{detail}",
                                category=category,
                                direction=direction,
                                tape=False,
                            )
                if len(seen_filings) > 5000:
                    seen_filings = set(list(seen_filings)[-2500:])
            except Exception:
                logger.debug("EDGAR poll failed (optional feed)", exc_info=True)
            # -- Bitcoin anchor (spec): free Alpaca crypto data ------
            try:
                d = await asyncio.to_thread(
                    _get_json,
                    "https://data.alpaca.markets/v1beta3/crypto/us/snapshots?symbols=BTC%2FUSD",
                )
                snap = (d.get("snapshots") or {}).get("BTC/USD") or {}
                daily_bar = snap.get("dailyBar") or {}
                open_px, close_px = float(daily_bar.get("o") or 0), float(daily_bar.get("c") or 0)
                if open_px > 0 and close_px > 0:
                    self._scanner2.btc_day_pct = (close_px / open_px - 1.0) * 100.0
            except Exception:
                logger.debug("BTC anchor poll failed (optional)", exc_info=True)
            # -- VIX term structure (4.x, CBOE delayed — free, every 10 min):
            # the daily risk state; ratio > 1 = backwardation panic tape.
            # Journaled into the _MARKET internals row, never a gate.
            if now - getattr(self, "_last_vix_poll", 0.0) >= 600.0:
                self._last_vix_poll = now
                try:
                    for cboe_symbol, attr in (("_VIX", "vix"), ("_VIX3M", "vix3m")):
                        d = await asyncio.to_thread(
                            _get_json,
                            "https://cdn.cboe.com/api/global/delayed_quotes/quotes/"
                            f"{cboe_symbol}.json",
                        )
                        price = float(((d or {}).get("data") or {}).get("current_price") or 0.0)
                        if price > 0:
                            setattr(self._scanner2, attr, price)
                except Exception:
                    logger.debug("VIX poll failed (optional)", exc_info=True)
            # -- ApeWisdom: retail attention every 5 min ---------------------
            if now - last_ape >= 300.0:
                last_ape = now
                try:
                    data = await asyncio.to_thread(
                        _get_json, "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
                    )
                    rows = (data.get("results") or [])[:50]
                    mentions = {str(r.get("ticker", "")).upper() for r in rows[:30]}
                    self._scanner2.attention = {s for s in mentions if s in self._scanner2._index}
                    # 4.7 upgrade (data agent: VELOCITY beats level): mentions
                    # vs 24h ago — acceleration is the squeeze precursor
                    velocity: dict[str, float] = {}
                    for r in rows:
                        ticker = str(r.get("ticker", "")).upper()
                        now_m = float(r.get("mentions") or 0.0)
                        ago_m = float(r.get("mentions_24h_ago") or 0.0)
                        if ticker in self._scanner2._index and now_m > 0:
                            velocity[ticker] = round(now_m / max(ago_m, 1.0), 2)
                    self._scanner2.mention_vel = velocity
                except Exception:
                    logger.debug("ApeWisdom poll failed (optional feed)", exc_info=True)
            # -- Finnhub earnings calendar: once per day, key optional -------
            try:
                from datetime import date as _date

                if earnings_day != _date.today():
                    from waveapp.security import secrets as _secrets

                    key = _secrets.get_secret("finnhub_api_key")
                    earnings_day = _date.today()
                    if key:
                        cal = await asyncio.to_thread(
                            _get_json,
                            f"https://finnhub.io/api/v1/calendar/earnings?from={earnings_day}"
                            f"&to={earnings_day}&token={key}",
                        )
                        self._scanner2.earnings_today = {
                            str(r.get("symbol", "")).upper()
                            for r in (cal.get("earningsCalendar") or [])
                        } & set(self._scanner2._index)
                        logger.info(
                            "scanner2 earnings-today: %d names",
                            len(self._scanner2.earnings_today),
                        )
                    else:
                        logger.info(
                            "scanner2 earnings feed off — add a 'finnhub_api_key' Keychain"
                            " entry (free finnhub.io account) to enable"
                        )
            except Exception:
                logger.debug("earnings poll failed (optional feed)", exc_info=True)

    @staticmethod
    def _idle_note(regime, now_et) -> str:
        """The TRUTH about when scanning resumes (2026-08-23, Sun
        20:11 saw 'resumes Sunday 20:00 ET' — that is when the VENUE wakes;
        the scanner, by design, sleeps until pre-market 04:00 of the next
        trading day: overnight is exit-only, §6)."""
        from datetime import timedelta

        from waveapp.engine.session import Regime, day_info

        if regime is Regime.OVERNIGHT:
            return "overnight session (exit-only) — scanning resumes 04:00 ET"
        probe = now_et.date()
        for _ in range(10):
            probe = probe + timedelta(days=1)
            trading, _early = day_info(probe)
            if trading and probe.weekday() < 5:
                return f"market closed — scanning resumes {probe:%A} 04:00 ET"
        return "market closed"

    def _restart_loop(self, attr: str, factory):
        """Supervisor restart hook: recreate the loop task and store it."""
        import asyncio as _aio

        task = _aio.ensure_future(factory())
        setattr(self, attr, task)
        if attr == "_scan_task":
            import time as _t

            self._last_scan_beat = _t.time()
        return task

    async def _scan_loop(self) -> None:
        import time as _time
        from datetime import UTC as _idle_utc
        from datetime import datetime as _idle_dt
        from datetime import time as _pre_time
        from zoneinfo import ZoneInfo as _ZoneInfo

        from waveapp.engine.session import Regime, SessionScheduler

        idle_note_shown: str | None = None
        et_zone = _ZoneInfo("America/New_York")
        while True:
            try:
                self._last_scan_beat = _time.time()  # watchdog heartbeat
                regime = SessionScheduler.regime()
                if self._scanner is not None and regime in (Regime.CLOSED, Regime.OVERNIGHT):
                    # 2026-08-20: say WHY it idles — and KEEP it true
                    # across regime flips (the old one-shot flag froze the
                    # weekend message into Sunday night)
                    note = self._idle_note(regime, _idle_dt.now(_idle_utc).astimezone(et_zone))
                    if note != idle_note_shown:
                        idle_note_shown = note
                        if self._on_scan_status is not None:
                            self._on_scan_status(note)
                        if self._on_universe_progress is not None:
                            self._on_universe_progress(0, 0, note)
                elif self._scanner is not None:
                    idle_note_shown = None
                    results = await self._scanner.scan_once()
                    from datetime import UTC as _utc
                    from datetime import datetime as _dt

                    # FPB GAPPER WATCH (§11 challenger, 2026-09-16 study:
                    # a third of FPB's paydays — +$195/day — happened on
                    # symbols Wave never watched). Once per day at 9:38+:
                    # the top-5 pre-market ramp names join the watch so the
                    # first-pullback engine can SEE its own setups. Gates
                    # unchanged; kill switch fpb_gapper_watch.
                    try:
                        now_et_g = _dt.now(_utc).astimezone(et_zone)
                        if (
                            regime is not Regime.PRE
                            and now_et_g.time() >= _pre_time(9, 38)
                            and getattr(self, "_gapper_watch_day", None)
                            != now_et_g.date().isoformat()
                            and getattr(self, "_pm_watch", None)
                        ):
                            from waveapp.config import AppConfig as _GwCfg

                            self._gapper_watch_day = now_et_g.date().isoformat()
                            if getattr(_GwCfg.load(), "fpb_gapper_watch", True):
                                ramps = sorted(
                                    (
                                        (w["last"] / w["first"] - 1.0, sym)
                                        for sym, w in self._pm_watch.items()
                                        if w.get("first") and w.get("last", 0) >= 5.0
                                    ),
                                    reverse=True,
                                )[:5]
                                top5 = [sym for _, sym in ramps]
                                if top5 and self._hub is not None:
                                    await self._hub.watch(top5)
                                    # SEAT, not just eyes (opening rehearsal
                                    # 2026-09-16: watch-only meant a perfect
                                    # FPB setup off-menu got ZERO strategy
                                    # evaluations — the +$195/day reach was
                                    # never wired). Same pattern as the
                                    # climber lane: guests + backfill + a
                                    # pool seat via _fpb_watch_symbols.
                                    provider = getattr(
                                        getattr(self, "_scanner", None), "provider", None
                                    )
                                    if provider is not None and hasattr(provider, "add_guests"):
                                        provider.add_guests(top5)
                                    for _gw_sym in top5:
                                        await self._fetch_chart_backfill(_gw_sym)
                                    self._fpb_watch_symbols = set(top5)
                                    logger.info(
                                        "FPB-GAPPER-WATCH: top-5 pre-market ramps seated: %s",
                                        ", ".join(top5),
                                    )
                    except Exception:
                        logger.exception("gapper watch seeding failed — next cycle")

                    if regime is Regime.PRE:
                        # 9:30:00-sharp entries (2026-08-25): remember
                        # every symbol's pre-market path so the auction queue
                        # knows the ramp names BEFORE the bell
                        now_et = _dt.now(_utc).astimezone(et_zone)
                        self._premarket_track(results, now_et)
                        await self._maybe_run_auction_preflight(now_et)
                        await self._maybe_queue_auction_entries(now_et)
                        if now_et.time() >= _pre_time(9, 28):
                            # freeze today's stocks-in-play list from the
                            # pre-market watch before the bell (idempotent)
                            try:
                                from waveapp.config import AppConfig as _cfg

                                floor = _cfg.load().min_entry_price
                            except Exception:
                                floor = 15.0
                            await self._ensure_day_list(results, now_et, floor)
                    elif regime is Regime.OPEN_DRIVE:
                        # paper venue runs no auction: canceled OPG orders
                        # re-enter at market right after the open (2026-08-27)
                        now_et = _dt.now(_utc).astimezone(et_zone)
                        await self._maybe_auction_fallback(now_et)

                    self._last_scan_results = results  # minute-tick entry clock
                    self._last_scan_monotonic = _dt.now(_utc).timestamp()
                    self._last_scan_at = _dt.now(_utc).astimezone().strftime("%H:%M:%S")
                    accepted_syms = [c.features.symbol for c in results if c.accepted]
                    self._last_scan_summary = {
                        "at": self._last_scan_at,
                        "candidates": len(results),
                        "accepted": len(accepted_syms),
                        "top": accepted_syms[:5],
                    }
                    await self._entry_pipeline(results)
                    if self._on_candidates is not None:
                        self._on_candidates(
                            [
                                {
                                    "symbol": c.features.symbol,
                                    "score": round(c.score, 2),
                                    "strategy": c.best_strategy,
                                    "rvol": round(c.features.rvol, 2),
                                    "gap_pct": round(c.features.gap_pct, 2),
                                    "atr_pct": round(c.features.atr_pct, 2),
                                    "spread": round(c.features.spread, 3),
                                    # radar table (2026-09-03): Price·Day %
                                    # cell + spread-as-%-of-price column
                                    "price": round(c.features.price, 2),
                                    "day_pct": round(
                                        (c.features.price / c.features.prev_close - 1.0) * 100.0,
                                        2,
                                    )
                                    if c.features.prev_close > 0
                                    else 0.0,
                                    "decision": "✓ accepted"
                                    if c.accepted
                                    else (c.reject_reason or "rejected"),
                                }
                                for c in results  # the full scan set, no cap
                            ]
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scan cycle failed")
            try:
                from datetime import UTC as _s_utc
                from datetime import datetime as _s_dt

                interval = self._cycle_interval(
                    SessionScheduler.regime(), _s_dt.now(_s_utc).astimezone(et_zone).time()
                )
            except Exception:
                interval = self._scan_interval
            await asyncio.sleep(interval)

    def _cycle_interval(self, regime, et_time) -> float:
        """Regime-aware scan cadence (2026-08-25 — 'yesterday is going
        to happen today': 2-min cycles were too slow at the open). 60s through
        the open drive, and from 9:00 ET so the auction queue reads fresh
        pre-market ramps; the base interval everywhere else."""
        from datetime import time as _time

        from waveapp.engine.session import Regime

        if regime is Regime.OPEN_DRIVE:
            return min(self._scan_interval, 60)
        if regime is Regime.PRE and et_time >= _time(9, 0):
            return min(self._scan_interval, 60)
        return self._scan_interval

    # -- 9:00 auction preflight (2026-08-26: "give me an error at 9am
    # if it doesnt work so i have time to fix it before the open") -----------

    def _auction_preflight_problems(self, config) -> list[str]:
        """Everything the 9:20 auction queue needs, checked at 9:00."""
        problems = []
        if self.engine is None or getattr(self.engine, "state", None) is None:
            problems.append("engine is not running")
        if not config.auto_trade:
            problems.append("auto-trade is OFF")
        # pmom_auction OFF is standing rule (PMOM retired), not a
        # failure — for a WEEK this bullet made the preflight "fail" and the
        # old minimum filter swallowed the page; the 2026-09-23 morning alarm
        # was this zombie finally getting delivered. The preflight is a
        # general 9:00 readiness check now.
        if not getattr(self, "_pm_watch", {}):
            problems.append("pre-market watch is EMPTY (no scan data since 4:00)")
        if self._scanner is None:
            problems.append("scanner is not running")
        if self._adapter is None or not self._adapter.is_connected:
            problems.append("Alpaca connection is down")
        return problems

    async def _maybe_run_auction_preflight(self, now_et) -> None:
        from datetime import time as _time

        from waveapp.config import AppConfig

        today = now_et.date().isoformat()
        if getattr(self, "_preflight_day", None) == today:
            return
        if not (_time(9, 0) <= now_et.time() < _time(9, 20)):
            return
        self._preflight_day = today
        try:
            config = AppConfig.load()
        except Exception:
            await self._push(
                "🚨 9:00 preflight: config file is UNREADABLE — fix before 9:20!",
                klass="error",
            )
            return
        problems = self._auction_preflight_problems(config)
        if problems:
            await self._push(
                "🚨 9:00 preflight FAILED — Wave is not ready for the open:\n• "
                + "\n• ".join(problems),
                klass="error",
            )
        else:
            logger.info(
                "9:00 auction preflight: all clear (%d symbols watched)", len(self._pm_watch)
            )

    # -- dead-man's heartbeat (2026-08-26) -------------------

    async def _heartbeat_loop(self) -> None:
        """Ping config.heartbeat_url every 60s. The alert lives OUTSIDE the
        Mac (healthchecks.io → Telegram): when Wave loses wifi/sleeps, the
        pings stop and the watchdog fires — a wifi-less Wave cannot message
        anyone itself, so the silence IS the signal (the silent clock)."""
        import urllib.request

        from waveapp.config import AppConfig

        while True:
            try:
                url = AppConfig.load().heartbeat_url.strip()
            except Exception:
                url = ""
            if url:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(urllib.request.urlopen, url, None, 10),
                        timeout=15.0,
                    )
                except Exception:
                    logger.debug("heartbeat ping failed (offline?) — watchdog will notice")
            await asyncio.sleep(60.0)

    # -- 9:30:00-sharp opening-auction entries (PMOM, 2026-08-25) -----------

    def _pm_seed_path(self):
        from waveapp.config import support_dir

        return support_dir() / "pm_watch.json"

    def _load_pm_seed(self, today: str) -> dict[str, float]:
        """First-seen pre-market prices persisted by an earlier process of the
        SAME day — a 4:00–9:28 restart must not blind PMOM (edge case fixed
        2026-08-25 per directive)."""
        import json

        try:
            payload = json.loads(self._pm_seed_path().read_text())
            if payload.get("day") == today:
                return {str(k): float(v) for k, v in payload.get("first", {}).items()}
        except Exception:  # noqa: S110 — no file / stale file is the normal case
            logger.debug("no pm-watch seed for %s", today)
        return {}

    def _save_pm_seed(self, today: str) -> None:
        import json

        try:
            self._pm_seed_path().write_text(
                json.dumps(
                    {"day": today, "first": {s: w["first"] for s, w in self._pm_watch.items()}}
                )
            )
        except Exception:
            logger.debug("pm watch persist failed (non-fatal)")

    def _premarket_track(self, results, now_et) -> None:
        """Remember each scanned symbol's first-seen pre-market price and its
        latest price/volume. Ramp = last vs first — the same 04:00→09:30
        measurement the PMOM adoption evidence used. First prices persist to
        disk so a restart mid-pre-market doesn't blind the auction queue."""
        today = now_et.date().isoformat()
        if getattr(self, "_pm_day", None) != today:
            self._pm_day = today
            self._pm_watch: dict[str, dict] = {}
            self._auction_done = False
            self._pm_seed = self._load_pm_seed(today)
        dirty = False
        for candidate in results:
            f = candidate.features
            if f.price <= 0:
                continue
            watch = self._pm_watch.get(f.symbol)
            if watch is None:
                first = self._pm_seed.get(f.symbol, f.price)
                self._pm_watch[f.symbol] = {"first": first, "last": f.price, "features": f}
                dirty = True
            else:
                watch["last"] = f.price
                watch["features"] = f  # freshest volume/spread/atr snapshot
        if dirty:
            self._save_pm_seed(today)

    @staticmethod
    def _pmom_picks(
        pm_watch: dict, min_price: float, min_ramp_pct: float = 3.0
    ) -> list[tuple[str, float]]:
        """(symbol, ramp%) picks, strongest ramp first — the PMOM conditions
        exactly as validated: pre-market ramp ≥3% on ≥100k pre-market shares,
        price ≥ the floor, and gap vs prior close under GAPGO's 3% trigger
        (bigger gaps are GAPGO's territory; PMOM covers its blind spot)."""
        picks = []
        for symbol, watch in pm_watch.items():
            first, last = watch["first"], watch["last"]
            f = watch["features"]
            if first <= 0 or last < min_price:
                continue
            ramp = (last / first - 1.0) * 100.0
            if ramp < min_ramp_pct:
                continue
            if f.day_volume < 100_000:  # pre-market volume during PRE
                continue
            gap = (last / f.prev_close - 1.0) * 100.0 if f.prev_close > 0 else 0.0
            if gap >= 3.0:
                continue
            picks.append((symbol, ramp))
        picks.sort(key=lambda p: p[1], reverse=True)
        return picks

    async def _maybe_queue_auction_entries(self, now_et) -> None:
        """Between 9:20 and 9:28 ET, once per day: turn the pre-market watch
        into limit-on-open orders so the fills ARE the 9:30:00 opening print
        (Alpaca tif=opg; queue closes at ~9:28)."""
        from datetime import time as _time
        from types import SimpleNamespace

        from waveapp.broker.base import OrderSide
        from waveapp.config import AppConfig

        if getattr(self, "_auction_done", True):
            return
        if not (_time(9, 20) <= now_et.time() < _time(9, 28)):
            return
        if self.engine is None or self._scanner is None:
            return
        self._auction_done = True  # one shot per day, qualified names or not
        try:
            config = AppConfig.load()
        except Exception:
            logger.exception("auction queue skipped — config unreadable")
            return
        if not (config.auto_trade and config.pmom_auction):
            return
        picks = self._pmom_picks(getattr(self, "_pm_watch", {}), config.min_entry_price)
        if not picks:
            logger.info("auction queue: no PMOM names this morning")
            return
        if self._hub is not None:
            # the exit engine needs live bars/quotes from the first RTH second
            await self._hub.watch([symbol for symbol, _ in picks])
        self._roll_signal_day(now_et)
        limits = getattr(self.engine.risk, "limits", None)
        max_positions = getattr(limits, "max_positions", 3)
        open_count = getattr(self.engine, "open_actor_count", 0)
        # EngineCore exposes a @property (int); test shims may expose a callable
        _open = open_count() if callable(open_count) else open_count
        # max_positions <= 0 = unlimited count (2026-09-21) — risk governs
        free = 10**9 if max_positions <= 0 else max(0, max_positions - _open)
        queued = []
        for symbol, ramp in picks:
            if free <= 0:
                break
            if symbol in self._signaled:
                continue
            f = self._pm_watch[symbol]["features"]
            price = self._pm_watch[symbol]["last"]
            daily_atr = max(f.atr_pct / 100.0 * price, 0.01)
            # NO cost gate here (2026-08-31, the fix order): the opening
            # auction is a single-price cross — nobody pays a spread — and
            # pre-market spreads are structurally wide, so gating the 9:20
            # queue on them falsely rejected TS/RRC/KGS this morning. The
            # §8.4 gate stands guard at the 9:31 fallback (_maybe_auction_
            # fallback), where a REAL live spread is actually paid.
            signal = SimpleNamespace(
                symbol=symbol,
                side=OrderSide.BUY,
                entry_price=price,
                stop_price=round(price - daily_atr, 2),
                strategy="PMOM",
                half_size=False,
                reason=f"PMOM: pre-market ramp {ramp:+.1f}% — queued for the opening auction",
            )
            opened = await self._execute_signal(signal, f.avg_daily_volume, auction=True)
            if opened:
                self._signaled.add(symbol)
                queued.append(f"{symbol} ({ramp:+.1f}%)")
                # remembered for the post-open fallback: Alpaca PAPER runs no
                # opening auction and cancels OPG unfilled (2026-08-27 —
                # CAMT/UCO/Q all canceled although the caps covered the opens)
                self._auction_watchlist = getattr(self, "_auction_watchlist", {})
                self._auction_watchlist[symbol] = f.avg_daily_volume
                free -= 1
        if queued:
            await self._push(
                "⏰ PMOM queued for the 9:30:00 opening auction: " + ", ".join(queued),
                klass="status",
            )

    def _pmom_cost_gate_ok(
        self, symbol: str, price: float, spread: float, daily_atr: float
    ) -> bool:
        """The §8.4 cost gate every other strategy passes — PMOM's auction
        queue and fallback skipped it until URBN (2026-08-28) paid a 2.3%
        spread at market. Same math as the entry pipeline."""
        from waveapp.broker.base import OrderSide
        from waveapp.engine.tradegate import GateInputs

        if self._scanner is None or getattr(self._scanner, "gate", None) is None:
            return True  # no gate wired (tests) — do not block
        decision = self._scanner.gate.evaluate(
            GateInputs(
                symbol=symbol,
                side=OrderSide.BUY,
                price=price,
                expected_move=daily_atr * self._scanner.expected_move_atr_fraction,
                profit_target=max(price * 0.004, 0.01),
                spread=spread,
                slippage_buffer=self._scanner.slippage_buffer_per_share,
            )
        )
        if not decision:
            logger.info("PMOM gated out: %s — %s", symbol, decision.reason)
        return bool(decision)

    async def _maybe_auction_fallback(self, now_et) -> None:
        """9:30:45–9:40: any PMOM auction order the venue CANCELED unfilled
        (the paper engine simulates no auction) re-enters at market — which is
        the validated sim entry anyway (first RTH bar, ~9:31)."""
        from datetime import time as _time
        from types import SimpleNamespace

        from waveapp.broker.base import OrderSide

        watchlist = getattr(self, "_auction_watchlist", None)
        if not watchlist or self.engine is None:
            return
        if not (_time(9, 30, 45) <= now_et.time() < _time(9, 40)):
            return
        from waveapp.engine.actor import PositionState

        for symbol in list(watchlist):
            avg_volume = watchlist[symbol]
            actor = next(
                (
                    a
                    for a in self.engine.actors.values()
                    if a.spec.symbol == symbol and a.spec.strategy == "PMOM"
                ),
                None,
            )
            if actor is None or actor.state is not PositionState.CLOSED:
                continue  # filled/pending — the auction worked, nothing to do
            if actor.exit_reason != "entry canceled":
                del watchlist[symbol]
                continue
            del watchlist[symbol]
            price = self._latest_price(symbol) or 0.0
            watch = getattr(self, "_pm_watch", {}).get(symbol)
            if price <= 0 or watch is None:
                continue
            f = watch["features"]
            daily_atr = max(f.atr_pct / 100.0 * price, 0.01)
            # §8.4 cost gate with the LIVE spread (URBN 2026-08-28: the
            # fallback bought into a 2.3% bid/ask at market — the whole −$58
            # "loss" was the spread toll; this gate was skipped on PMOM paths)
            # A3-9 (audit 2026-09-22): this was the last raw latest_quotes
            # read on a PMOM path — a stale/halted book priced the §8.4
            # gate. _fresh_quote (RTH default age — it is 9:31 by now) →
            # stale/missing falls back to the snapshot spread, exactly like
            # the pipeline's live-spread recompute.
            quote = self._fresh_quote(symbol)
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            live_spread = (ask - bid) if bid > 0 and ask > bid else f.spread
            if not self._pmom_cost_gate_ok(symbol, price, live_spread, daily_atr):
                # surface WHY the name died (2026-09-02: five silent
                # refusals read as "Wave canceled my buys" — every queued
                # name now reports its fate)
                await self._push(
                    f"🚧 {symbol}: auction order expired (paper simulates no"
                    f" auction) — fallback refused by the §8.4 gate: live"
                    f" spread ${live_spread:.2f} at {price:.2f}. Not chased.",
                    klass="status",
                )
                continue
            self._signaled.discard(symbol)
            signal = SimpleNamespace(
                symbol=symbol,
                side=OrderSide.BUY,
                entry_price=price,
                stop_price=round(price - daily_atr, 2),
                strategy="PMOM",
                half_size=False,
                reason=(
                    f"PMOM fallback: auction order canceled by the venue — "
                    f"market entry @ {price:.2f}"
                ),
            )
            # A3-2: with the day-mark discarded above, this await is exactly
            # the window an overlapping entry-pipeline pass could double-enter
            # the symbol — mark it in flight for the duration
            self._signal_inflight.add(symbol)
            try:
                opened = await self._execute_signal(signal, avg_volume)
            finally:
                self._signal_inflight.discard(symbol)
            if opened:
                self._signaled.add(symbol)
                # klass="status": the entry FILL line ("✅ Bought …") follows
                # from the actor path and carries the minimum-mode duty
                await self._push(
                    f"↩️ {symbol}: auction order canceled by the paper venue — "
                    f"re-entered at market {price:.2f} (PMOM fallback)",
                    klass="status",
                )

    # -- the day list: live stocks_in_play_premarket (scanner rework, 8-25) --

    @staticmethod
    def _build_day_list(
        pm_watch: dict,
        min_price: float,
        top_n: int = 20,
        max_leveraged_fraction: float = 0.25,
    ) -> list[str]:
        """The sim's pre-market picks, live: rank by pre-market RVOL
        (pre-market shares ÷ trailing average daily volume) over the §7
        floors — the exact selection the +$53.9k OOS evidence traded."""
        from waveapp.instruments import leveraged_cap

        ranked = []
        for symbol, watch in pm_watch.items():
            f = watch["features"]
            if watch["last"] < min_price or f.avg_daily_volume < 500_000:
                continue
            if f.day_volume < 100_000:  # PREMARKET_MIN_SHARES (§8.1)
                continue
            if f.atr_pct < 1.0:
                # a stock in play MOVES. Bond/treasury ETFs (GOVT, VGSH…)
                # rank high on steady round-the-clock volume but can never
                # clear a profit target (first live day list, 2026-08-26)
                continue
            ranked.append((f.day_volume / max(f.avg_daily_volume, 1.0), symbol, f.leveraged))
        ranked.sort(key=lambda r: r[0], reverse=True)
        budget = leveraged_cap(top_n, max_leveraged_fraction)
        picks: list[str] = []
        admitted_leveraged = 0
        for _score, symbol, leveraged in ranked:
            if len(picks) >= top_n:
                break
            if leveraged:
                if admitted_leveraged >= budget:
                    continue
                admitted_leveraged += 1
            picks.append(symbol)
        return picks

    @staticmethod
    def _fallback_day_list(results, min_price: float, top_n: int = 20) -> list[str]:
        """No pre-market watch (mid-day restart): rank today's scan by
        session-adjusted RVOL over the same floors. Weaker than the true
        pre-market list but far better than a rotating top-5."""
        ranked = [
            (c.features.rvol, c.features.symbol)
            for c in results
            if c.features.price >= min_price
            and c.features.avg_daily_volume >= 500_000
            and c.features.rvol >= 1.5
            and c.features.atr_pct >= 1.0  # movers only — no bond funds
        ]
        ranked.sort(reverse=True)
        return [symbol for _r, symbol in ranked[:top_n]]

    async def _ensure_day_list(self, results, now_et, min_price: float) -> list[str]:
        """Today's stocks-in-play list. SCANNER 2.0 IS LIVE (decision,
        2026-08-31 night: "the new scanner should be in place"): when the
        full-market scanner has a fresh menu, THAT is the day list — dynamic,
        re-ranked every minute, never frozen. The kitchen downstream (trend
        gate, $15 floor, strategies, TradeGate, risk, exits, runway cutoff)
        is untouched; only the menu source changed. The old 9:28 list stays
        as the automatic fallback (scanner2 dead/stale/cold) and as the
        journaled shadow for the daily old-vs-new scoreboard."""
        menu = self._scanner2_menu_symbols(now_et)
        if menu:
            # SCANNER UNLEASHED (2026-09-02: "give the scanner an
            # option to be the master piece that it is"): menu names outside
            # the trailing-volume scan set become same-day GUESTS of the
            # feature scan, so the entry pipeline can actually trade them
            # (TARS/MLYS/VRNS sat on the menu all afternoon untouchable).
            scanner = getattr(self, "_scanner", None)
            provider = getattr(scanner, "provider", None) if scanner is not None else None
            if provider is not None and hasattr(provider, "add_guests"):
                with contextlib.suppress(Exception):
                    provider.add_guests(menu)
            if getattr(self, "_s2_watched_day", None) != now_et.date().isoformat():
                self._s2_watched_day = now_et.date().isoformat()
                self._s2_watched = set()
            fresh = [s for s in menu if s not in getattr(self, "_s2_watched", set())]
            if fresh:
                self._s2_watched = getattr(self, "_s2_watched", set()) | set(fresh)
                logger.info("day list (scanner2 LIVE): +%s", ", ".join(fresh))
                if self._hub is not None:
                    await self._hub.watch(fresh)
                for symbol in fresh:
                    self._ensure_backfill(symbol)
            return menu
        today = now_et.date().isoformat()
        if getattr(self, "_day_list_day", None) == today:
            # A4-2 (audit 2026-09-22): after a teardown the reconnect's hub
            # has NONE of the old subscriptions — re-issue hub.watch (and
            # backfill, whose guards teardown also cleared) for the CACHED
            # list instead of silently short-circuiting; never rebuild it.
            if (
                getattr(self, "_day_list_rewatch", False)
                and self._day_list
                and self._hub is not None
            ):
                self._day_list_rewatch = False
                logger.info("day list re-watched after reconnect: %s", ", ".join(self._day_list))
                await self._hub.watch(self._day_list)
                for symbol in self._day_list:
                    self._ensure_backfill(symbol)
            return self._day_list
        pm_watch = getattr(self, "_pm_watch", {}) if getattr(self, "_pm_day", None) == today else {}
        day_list = self._build_day_list(pm_watch, min_price)
        source = "pre-market watch"
        if not day_list:
            day_list = self._fallback_day_list(results, min_price)
            source = "scan fallback"
        self._day_list_day = today
        self._day_list = day_list
        self._day_list_rewatch = False  # a fresh build watches right below
        if day_list:
            logger.info("day list (%s): %s", source, ", ".join(day_list))
            if self._hub is not None:
                await self._hub.watch(day_list)
            # give every day-list name its full session history NOW — the
            # opening ranges for ORB and a true session VWAP survive restarts
            for symbol in day_list:
                self._ensure_backfill(symbol)
        else:
            logger.info("day list empty — entry pipeline falls back to scanner top-5")
        return day_list

    def _scanner2_menu_symbols(self, now_et) -> list[str]:
        """Scanner 2.0's current menu — only when it is running, enabled for
        LIVE duty, and FRESH (a sweep within 5 min). Anything less falls
        back to the old day-list path automatically."""
        scanner2 = getattr(self, "_scanner2", None)
        if scanner2 is None or not getattr(scanner2, "last_menu", None):
            return []
        try:
            from waveapp.config import AppConfig

            if not AppConfig.load().scanner2_live:
                return []
        except Exception:
            return []
        last_step = getattr(scanner2, "last_step_ts", 0.0)
        if now_et.timestamp() - last_step > 300.0:
            logger.warning("scanner2 menu stale (>5 min) — falling back to the old day list")
            return []
        return [m["symbol"] for m in scanner2.last_menu]

    def _roll_signal_day(self, now_et) -> None:
        """One trade per symbol per day — shared by the scan pipeline and the
        auction queue, restart-safe (reseeds from today's positions)."""
        today = now_et.date().isoformat()
        if getattr(self, "_signal_day", None) == today:
            return
        self._signal_day = today
        self._signaled = set()
        # CHOP-INVERT day cap rollover (2026-09-23): opened inverted entries
        # reset with the ET day, same clock as the one-trade-per-day mark.
        self._chop_inverted_today = 0
        # R1.5: the efficacy guard resets to MOMENTUM with the ET day too
        with contextlib.suppress(Exception):
            self._efficacy.roll_day(today)
        # R2 SNIPER: watches, the expired-skip set and the day counters are
        # ET-day state — a watch must never survive into another session
        with contextlib.suppress(Exception):
            self._sniper.roll_day(today)
        # churn guard rollover (audit 2026-09-16: _symbol_entries had NO day
        # reset — in a 24/5 run a symbol traded 3x Monday stayed blocked
        # forever, silent entry starvation growing with uptime)
        self._symbol_entries = {}
        if self._database is not None:
            from datetime import UTC as _utc

            # A4-12 (audit 2026-09-22): the reseed matched the UTC date of
            # "now" — during 20:00–24:00 ET the UTC date is already TOMORROW,
            # so an evening restart queried tomorrow's positions, found
            # nothing, and re-armed every symbol already traded today. The
            # one-trade-per-day key is the ET day (same as _signal_day), so
            # reseed everything opened since ET midnight, expressed as a UTC
            # lower bound (opened_at is stored as UTC ISO text — same-format
            # lexicographic >= is a correct time comparison).
            day_start_utc = (
                now_et.replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(_utc)
                .isoformat()
            )
            with contextlib.suppress(Exception):
                for row in self._database.query(
                    "SELECT DISTINCT symbol FROM positions WHERE opened_at >= ?"
                    " AND state != 'superseded'",
                    (day_start_utc,),
                ):
                    self._signaled.add(row["symbol"])

    def _day_judge_reading(self) -> tuple[str, float]:
        """Defensive read of the DayJudge for the CHOP-INVERT router: a
        missing detector, missing attribute or bad value reads as
        ("UNCLEAR", 0.0) — this code must work (as a no-op) even in builds
        or test fakes where the detector never landed. Verdict may be the
        DayVerdict str-enum or a plain string; both normalize the same."""
        try:
            dj = getattr(self, "_day_judge", None)
            raw = getattr(dj, "verdict", None)
            raw = getattr(raw, "value", raw)  # enum member → its string value
            verdict = str(raw or "").strip().upper() or "UNCLEAR"
            confidence = float(getattr(dj, "confidence", 0.0) or 0.0)
        except Exception:
            return "UNCLEAR", 0.0
        return verdict, confidence

    def _trend_gate_vetoes(self, signal, features) -> bool:
        """Per-signal TREND GATE (S3 form; adopted 2026-08-24, mirrored for
        shorts 2026-09-23): longs only ABOVE today's open, shorts only BELOW
        it — the anti-fade guard for trend days. Returns True to veto.

        CHOP-INVERT exception (2026-09-23): an inverted signal SKIPS
        this gate, both sides. The gate's whole purpose is to stop Wave
        fading a trending tape; an inverted signal exists ONLY because the
        DayJudge called the day CHOP — the fade IS the strategy, and by
        construction the inverted side sits on the gate's "wrong" side of
        the open (a shorted failed breakout triggers above it). Every other
        gate still applies to inverted signals."""
        if features.day_open <= 0:
            return False
        from waveapp.engine.strategies import is_inverted

        if is_inverted(signal):
            # both flavors (CHOP-INVERTED and R1.5 EFFICACY-INVERTED): an
            # inverted signal exists only because momentum is failing —
            # the fade is the strategy, and it sits on the gate's "wrong"
            # side of the open by construction.
            logger.info(
                "trend gate skipped for inverted %s %s — the fade is today's strategy",
                signal.side.value.upper(),
                signal.symbol,
            )
            return False
        if signal.side is OrderSide.BUY and features.price < features.day_open:
            logger.info(
                "trend gate: %s skipped — %.2f below today's open %.2f",
                features.symbol,
                features.price,
                features.day_open,
            )
            return True
        if signal.side is OrderSide.SELL and features.price >= features.day_open:
            logger.info(
                "trend gate (short): %s skipped — %.2f above today's open %.2f",
                features.symbol,
                features.price,
                features.day_open,
            )
            return True
        return False

    # -- entry pipeline (Phase 7.2): candidate → strategy → gate → entry ----

    def _schedule_entry_tick(self, now_et) -> None:
        """Minute-tick entry clock (2026-09-04): run the entry pipeline off
        the scanner2 minute boundary. Guards: last full scan fresh (<=180s),
        never two ticks in flight; the pipeline itself re-checks regime,
        auto_trade and every gate exactly as on the slow path."""
        import contextlib as _ctx
        from datetime import UTC as _utc
        from datetime import datetime as _dt
        from datetime import time as _time_cls

        results = getattr(self, "_last_scan_results", None)
        if not results:
            return
        # OPENING TICK (2026-09-18: entries belong at 9:30, not
        # 9:31-9:34): at the bell the scan loop is busy with its first
        # RTH sweep (~2 min over 1500 names) and the last COMPLETED scan
        # is the ~9:28 pre-market one — the 180s bound let the 9:30 and
        # 9:31 ticks die and the first entries waited for the 9:32-9:34
        # scan. Pre-market features (gap%, PM rvol) are exactly what the
        # GAP strategy needs, so in the first 3 minutes of RTH the bound
        # relaxes to 420s. Every downstream gate still applies.
        stale_bound = 180.0
        et_now = now_et.time() if hasattr(now_et, "time") else None
        if et_now is not None and et_now >= _time_cls(9, 30) and et_now < _time_cls(9, 33):
            stale_bound = 420.0
        if _dt.now(_utc).timestamp() - getattr(self, "_last_scan_monotonic", 0) > stale_bound:
            return  # scan too stale — the full cycle will handle it
        task = getattr(self, "_entry_tick_task", None)
        if task is not None and not task.done():
            return  # one tick at a time
        with _ctx.suppress(Exception):
            self._entry_tick_task = asyncio.ensure_future(self._entry_tick(results))

    async def _entry_tick(self, results) -> None:
        try:
            await self._entry_pipeline(results)
        except Exception:
            logger.exception("minute-tick entry pass failed")

    def _fresh_quote(self, symbol: str, max_age_s: float = 15.0):
        """A4-7 (audit 2026-09-22): THE quote-age gate. latest_quotes entries
        are never invalidated — a halted/dropped symbol serves its last quote
        indefinitely — so every decision-pricing consumer (pipeline deferral,
        ladder mid, door judge, climber/guest cost gates, MK rebuy) must come
        through here instead of trusting a raw ``latest_quotes`` read.

        Returns the quote only when its timestamp EXISTS, is tz-aware-
        comparable, and is at most ``max_age_s`` old; anything else — no hub,
        no quote, no timestamp, a naive/untyped timestamp (the old
        ``suppress(TypeError)`` let those pass as "fresh"; inverted here on
        purpose), or an aged one — returns None and the caller treats the
        symbol as having NO quote. The torture-sim lesson stands: a frozen
        30-min-old quote read as warm posts a dead-mid ladder then
        market-falls-back into a halt resume (the CUE class).
        """
        if self._hub is None:
            return None
        quote = self._hub.latest_quotes.get(symbol)
        if quote is None:
            return None
        ts = getattr(quote, "timestamp", None)
        if ts is None:
            return None  # a quote that can't prove its age is STALE
        from datetime import UTC as _qutc
        from datetime import datetime as _qdt

        try:
            age = (_qdt.now(_qutc) - ts).total_seconds()
        except TypeError:
            return None  # naive/untyped timestamp — STALE, never fresh
        if age > max_age_s:
            return None
        return quote

    async def _climber_candidates(self, exclude: set[str]) -> list:
        """CLIMBER LANE (2026-09-16, the ranker starvation find): the scan's
        top-N is heat-ranked, so quiet climbers never reach the strategies.
        Sweep scanner2's FULL universe for names climbing off their open on
        real volume and hand them to the pipeline as candidates — every
        downstream gate (cost, churn, veto, risk) still applies. Band and
        volume bar live-read from config (the midday-supply order)."""
        out: list = []
        try:
            import numpy as _np

            from waveapp.config import AppConfig as _ClCfg
            from waveapp.engine.scanner import Candidate, SymbolFeatures

            s2 = self._scanner2
            if s2 is None:
                return out
            cfg = _ClCfg.load()
            band_lo = float(getattr(cfg, "climber_day_pct", 1.2))
            band_hi = float(getattr(cfg, "climber_max_day_pct", 4.0))
            rvol_min = float(getattr(cfg, "climber_rvol", 1.2))
            limit = int(getattr(cfg, "climber_limit", 8))
            shortlist: list[tuple[float, str, int]] = []
            for symbol, row in s2._index.items():
                if symbol in exclude or "." in symbol:
                    continue
                do, px = float(s2.day_open[row]), float(s2.last[row])
                if not do or not px or px < 5.0:
                    continue
                day_pct = (px / do - 1.0) * 100.0
                if day_pct < band_lo or day_pct >= band_hi:  # >=band_hi is veto land
                    continue
                base = s2.baselines.index.get(symbol)
                if base is None:
                    continue
                from datetime import datetime as _cdt
                from zoneinfo import ZoneInfo as _cz

                from waveapp.engine.scanner2 import minute_index as _s2_minute

                # CANONICAL AXIS (audit 2026-09-16 F1, verified ×10-26
                # inflation): expected_cum's columns run on the 4:00-ET
                # axis (9:30 = index 330) — the old minutes-since-9:30
                # math read the pre-market portion of the curve and made
                # the rvol gate a dead letter.
                minute = _s2_minute(_cdt.now(tz=_cz("America/New_York")))
                if minute is None:
                    continue
                expected = max(float(s2.baselines.expected_cum(_np.array([base]), minute)[0]), 1.0)
                rvol = float(s2.cum_vol[row]) / expected
                if rvol < rvol_min:
                    continue
                shortlist.append((day_pct, symbol, row, rvol))
            shortlist.sort(reverse=True)
            picks = shortlist[:limit]
            # A climber candidate is useless barless (2026-09-16, the
            # FIFTH door): the VWAP strategy needs hub bars + session
            # VWAP, and lane symbols never made the day list. Subscribe
            # + backfill NEW picks FIRST (event-promotion path, once per
            # symbol per session) — a fresh name has no live quote yet
            # and becomes a candidate on the next sweep with real numbers.
            watched = getattr(self, "_climber_watched", None)
            if watched is None:
                watched = self._climber_watched = set()
            fresh = [sym for _d, sym, _r, _v in picks if sym not in watched]
            if fresh:
                watched.update(fresh)
                if self._hub is not None:
                    await self._hub.watch(fresh)
                provider = getattr(getattr(self, "_scanner", None), "provider", None)
                if provider is not None and hasattr(provider, "add_guests"):
                    provider.add_guests(fresh)
                for sym in fresh:
                    await self._fetch_chart_backfill(sym)
                logger.info("CLIMBER LANE watching: %s", ", ".join(fresh))
                self._note_first_watch(fresh)  # M0 entry telemetry
            for _day_pct, symbol, row, rvol in picks:
                # SEVENTH door (14:37, GYLD — the first climber signal ever,
                # killed by a fictional spread): the cost gate uses THIS
                # number. Feed it the live quote; a placeholder auto-blocks
                # every sub-$33 name and flatters expensive ones. No quote
                # yet OR a stale one (A4-7: a dead book is just as fictional
                # as no book) → not a candidate this sweep (honest > fast).
                quote = self._fresh_quote(symbol)
                bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
                ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
                if not ask or not bid or ask <= bid:
                    continue
                spread = ask - bid
                px = float(s2.last[row])
                prev = float(s2.prev_close_live[row]) or px
                hod = float(s2.hod[row]) or px
                do = float(s2.day_open[row])
                atr_pct = max((hod - do) / px * 100.0, 0.5)  # day-range proxy
                out.append(
                    Candidate(
                        features=SymbolFeatures(
                            symbol=symbol,
                            price=px,
                            prev_close=prev,
                            gap_pct=(do / prev - 1.0) * 100.0 if prev else 0.0,
                            rvol=rvol,
                            atr_pct=atr_pct,
                            spread=spread,
                            day_volume=float(s2.cum_vol[row]),
                            avg_daily_volume=float(s2.cum_vol[row]),
                            day_open=do,
                        ),
                        score=1.0,
                        strategy_scores={"VWAP": 1.0},
                        best_strategy="VWAP",
                    )
                )
            if out:
                logger.info(
                    "CLIMBER LANE: %s join the entry pipeline",
                    ", ".join(c.features.symbol for c in out),
                )
        except Exception:
            logger.exception("climber sweep failed — heat lane unaffected")
        return out

    async def _guest_candidates(self, symbols: set[str]) -> list:
        """Pool seats for already-watched guest symbols (FPB gapper watch):
        Candidate objects from scanner2's live arrays with REAL quote
        spreads — same honesty rules as the climber lane, no band filter
        (the guest list is already curated). No quote yet -> no seat this
        sweep."""
        out: list = []
        if not symbols:
            return out
        try:
            from waveapp.engine.scanner import Candidate, SymbolFeatures

            s2 = self._scanner2
            if s2 is None or self._hub is None:
                return out
            for symbol in sorted(symbols):
                row = s2._index.get(symbol)
                if row is None:
                    continue
                # A4-7: stale quote == no quote — no seat this sweep
                quote = self._fresh_quote(symbol)
                bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
                ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
                if not ask or not bid or ask <= bid:
                    continue
                px = float(s2.last[row])
                do = float(s2.day_open[row]) or px
                prev = float(s2.prev_close_live[row]) or px
                hod = float(s2.hod[row]) or px
                if not px or px < 5.0:
                    continue
                out.append(
                    Candidate(
                        features=SymbolFeatures(
                            symbol=symbol,
                            price=px,
                            prev_close=prev,
                            gap_pct=(do / prev - 1.0) * 100.0 if prev else 0.0,
                            rvol=1.0,
                            atr_pct=max((hod - do) / px * 100.0, 0.5),
                            spread=ask - bid,
                            day_volume=float(s2.cum_vol[row]),
                            avg_daily_volume=float(s2.cum_vol[row]),
                            day_open=do,
                        ),
                        score=1.0,
                        strategy_scores={"FPB": 1.0},
                        best_strategy="FPB",
                    )
                )
        except Exception:
            logger.debug("guest candidate build skipped")
        return out

    async def _entry_pipeline(self, results) -> None:
        """Gate-accepted candidates meet the armed strategies. Entries fire
        ONLY when config.auto_trade is true (Phase 10 flips it); until then a
        would-be entry pushes a signal notification instead."""
        from waveapp.config import AppConfig
        from waveapp.engine.session import SessionScheduler
        from waveapp.engine.strategies import armed_strategies
        from waveapp.engine.tradegate import GateInputs

        if self.engine is None or self._hub is None or self._scanner is None:
            return
        info = SessionScheduler.info()
        strategies = armed_strategies(info.regime)
        if not strategies:
            return
        # CLIMBER LANE joins the heat lane (2026-09-16); test fakes may
        # lack .actors — the lane is best-effort, the heat lane untouched.
        # Kept SEPARATE from results: the pool below filters results to the
        # day list, and climbers are by definition not on it (the SIXTH
        # door, found 14:32) — they are appended after that filter.
        climber_pool: list = []
        try:
            held = {
                a.spec.symbol
                for a in self.engine.actors.values()
                if a.state.value in ("open", "scaling_out", "pending_entry")
            }
            # CATCH-THE-CLIMB fix (2026-09-23): excluding EVERY scan result
            # made the lane structurally blind to any mover that was a scan
            # candidate below the top-N cutoff (PLTR sat there from 9:32).
            # Exclude only what already has a seat: the day list and holds.
            existing = set(getattr(self, "_day_list", []) or []) | held
            climber_pool = await self._climber_candidates(existing)
        except Exception:
            logger.debug("climber lane skipped this cycle")

        auto_trade = False
        min_entry_price = 10.0
        orb_late_gate_pct = 6.0
        shorts_enabled = False  # S3: fail closed — a config miss trades long-only
        chop_invert = False  # CHOP-INVERT: fail closed — a config miss never flips
        chop_invert_max_per_day = 6
        # R1.5 EFFICACY GUARD: fail ON — the guard is protective and ships
        # default-armed; a config miss never disarms it.
        efficacy_guard = True
        efficacy_full = True
        # R0.5 PRE-OPEN BIAS: default ON per decision; fail ON like the
        # efficacy guard (a NEUTRAL/absent verdict is already a no-op).
        preopen_bias_cfg = True
        # R2 SNIPER BOOK: default ON per decision (2026-09-24); fail ON
        # like the efficacy guard — a config miss never disarms the book.
        sniper_cfg = True
        try:
            config = AppConfig.load()
            auto_trade = config.auto_trade
            min_entry_price = config.min_entry_price
            orb_late_gate_pct = float(getattr(config, "orb_late_gate_pct", 6.0) or 0.0)
            shorts_enabled = bool(getattr(config, "shorts_enabled", False))
            chop_invert = bool(getattr(config, "chop_invert", False))
            chop_invert_max_per_day = int(getattr(config, "chop_invert_max_per_day", 6) or 0)
            efficacy_guard = bool(getattr(config, "efficacy_guard", True))
            efficacy_full = bool(getattr(config, "efficacy_invert_full_size", True))
            preopen_bias_cfg = bool(getattr(config, "preopen_bias", True))
            sniper_cfg = bool(getattr(config, "sniper_book", True))
        except Exception:
            logger.debug("config reload failed — auto_trade stays off this cycle")

        # RTH-only entries (2026-08-19, Wave runs 24/7): the champion evidence
        # is regular-hours only — a PRE-market GAPGO signal would queue an
        # unvalidated market-on-open order. Scanning/analysis continues all
        # day; ENTRIES fire 09:30–16:00 ET only. Late-day guard: everything
        # flattens at the daily close (DAY stops expire at the bell), so no
        # new entries in the final minutes either.
        rth_minutes = SessionScheduler.minutes_to_rth_close(info.et_time)
        if rth_minutes is None:
            logger.debug("entries paused — outside regular hours (validated window)")
            return
        if rth_minutes <= ENTRY_CUTOFF_MINUTES:
            logger.info(
                "entries paused — %.0f min to the daily close (flatten window)", rth_minutes
            )
            return

        # Scanner rework (2026-08-25 "make the scanner send more stock
        # to Wave"): entries come from the DAY LIST — the sim's fixed 20-name
        # stocks-in-play selection, worked ALL DAY — not a rotating top-5.
        day_list = await self._ensure_day_list(results, info.et_time, min_entry_price)
        members_all: set[str] = set(day_list or [])
        if day_list:
            members = set(day_list)
            pool = [c for c in results if c.features.symbol in members]
        else:
            pool = [c for c in results if c.accepted][:5]
        # FPB GAPPER SEAT (opening rehearsal 2026-09-16: watch-only left the
        # +$195/day cohort with zero strategy evaluations off-menu). The
        # 9:38 top-5 get pool seats restricted to the FPB strategy below.
        fpb_symbols: set[str] = set(getattr(self, "_fpb_watch_symbols", ()) or ())
        if fpb_symbols:
            try:
                seen0 = {c.features.symbol for c in pool} | {
                    c.features.symbol for c in climber_pool
                }
                fpb_pool = await self._guest_candidates(fpb_symbols - seen0)
                pool = pool + fpb_pool
            except Exception:
                logger.debug("fpb guest seats skipped this cycle")
        # IGNITER GUEST SEATS (catch-the-climb, #1 priority 2026-09-23): fresh
        # event-promoted movers enter the pool NOW instead of waiting to
        # crack the top-20 menu (the stage that made PLTR tradable at 9:44
        # instead of 9:38). TTL-bounded, capped, dedup'd; TradeGate, trend
        # gate, brain veto, churn and risk all still judge each one.
        igniters = getattr(self, "_igniter_guests", None)
        if igniters:
            try:
                import time as _time

                now_mono = _time.monotonic()
                for sym in [s for s, t in igniters.items() if now_mono - t > 1800]:
                    igniters.pop(sym, None)  # 30-min seat
                seen_i = {c.features.symbol for c in pool} | {
                    c.features.symbol for c in climber_pool
                }
                fresh_igniters = set(list(igniters.keys())[:8]) - seen_i - members_all
                if fresh_igniters:
                    pool = pool + await self._guest_candidates(fresh_igniters)
            except Exception:
                logger.debug("igniter guest seats skipped this cycle")
        climber_symbols: set[str] = set()
        if climber_pool:
            seen = {c.features.symbol for c in pool}
            fresh_climbers = [c for c in climber_pool if c.features.symbol not in seen]
            pool = pool + fresh_climbers
            # SCOREBOARD FIND (2026-09-16 evening): the −$882 cohort entries
            # were ORB-5 BREAKOUT signals firing on lane candidates — buying
            # range-breaks at 14:38 midday (fills at 132–281% of the prior
            # 10-min range). The lane exists to feed the PATIENT pullback
            # strategy only; remember its symbols so the strategy loop below
            # lets climbers meet nothing but VWAP.
            climber_symbols = {c.features.symbol for c in fresh_climbers}
        # R2 SNIPER: sniper transforms exist only where entries exist —
        # with auto_trade off the signal push is the day's action and the
        # pipeline stays byte-identical (documented, tested).
        sniper_on = sniper_cfg and auto_trade
        if not pool and not (sniper_on and self._sniper.active):
            # an empty pool must still service pending FPB watches — a
            # watched symbol that fell off the menu keeps its 20-min claim
            return
        await self._hub.watch([c.features.symbol for c in pool])
        # one trade per SYMBOL per day, like the validated sim — restart-safe
        # (2026-08-21: a restart wiped it and Wave re-chased HOOD at the top)
        self._roll_signal_day(info.et_time)
        # R1.5 EFFICACY GUARD: one mode reading per pass (the roll above just
        # reset a stale day). MOMENTUM = pipeline untouched; INVERTED = every
        # momentum signal below rides the shared invert transform.
        efficacy_inverted = False
        if efficacy_guard:
            try:
                from waveapp.engine.efficacy import MODE_INVERTED

                efficacy_inverted = self._efficacy.mode() == MODE_INVERTED
            except Exception:
                logger.debug("efficacy mode read failed — MOMENTUM assumed")
        # R0.5 PRE-OPEN BIAS: one reading per pass. NEUTRAL (the common
        # case: no directional verdict, flag off, retired by the Day Judge,
        # or a build without the brain) = pipeline byte-identical.
        po_bias = "NEUTRAL"
        if preopen_bias_cfg:
            po_bias = self._preopen_reading(info.et_time)

        # multiple entries per cycle up to the free slots — the sim fills its
        # book as fast as signals arrive; the old one-per-cycle throttle made
        # a 6-slot book take 6+ minutes to fill at the open
        limits = getattr(getattr(self.engine, "risk", None), "limits", None)
        open_count = getattr(self.engine, "open_actor_count", 0)
        # EngineCore exposes a @property (int); test shims may expose a callable
        # WAVE 2 item 11 made real (audit 2026-09-16 door NINE: the old
        # max_positions hard-return meant risk.can_enter's 6→12 headroom
        # logic — approved risk-based position count — was DEAD CODE
        # for the pipeline). The pipeline now runs to the HARD cap; every
        # entry still passes risk.can_enter, which enforces the 6% total-
        # risk ceiling and admits beyond max_positions only when floor-
        # locked winners have freed the risk (tightening-only: can_enter
        # was always the stricter judge, it just never got asked).
        _base_cap = getattr(limits, "max_positions", 3)
        _hard_cap = getattr(limits, "max_positions_hard", _base_cap) or _base_cap
        if _base_cap <= 0:
            # unlimited count (2026-09-21): risk.can_enter's total-
            # open-risk ceiling is the only book-size governor.
            # A3-10 (audit 2026-09-22): max_positions_hard is intentionally
            # NOT a backstop here — the pipeline defers wholly to
            # risk.can_enter in unlimited mode; the None-open-risk hole in
            # that path is A3-6's max_positions_hard fallback in risk.py
            # (Lane A is wiring it), not this counter.
            free_slots = 10**9
        else:
            free_slots = max(
                0,
                max(_base_cap, _hard_cap) - (open_count() if callable(open_count) else open_count),
            )

        # R2 SNIPER (VALIDATION.md): one Day-Judge reading per pass for the
        # pocket starves, then the watch pass — prune dead watches (window
        # expiry, day-mark burn, INVERTED flip, close guard) and fire any
        # watch whose tested pullback rule just completed. Triggered entries
        # ride the same gates (trend gate, cost gate, judge_entry door,
        # sizing) and consume free_slots like any other entry.
        sniper_verdict = ""
        if sniper_on:
            sniper_verdict, _sn_conf = self._day_judge_reading()
            free_slots = await self._sniper_watch_pass(
                info, efficacy_inverted, shorts_enabled, orb_late_gate_pct, free_slots
            )

        for candidate in pool:
            if auto_trade and free_slots <= 0:
                return  # book full — signals resume when a slot frees up
            features = candidate.features
            if features.price < min_entry_price:
                # champion price floor ($15 since 2026-08-24 — the call,
                # confirmed by sim, live losses AND the labeled dataset)
                continue
            # TREND GATE (adopted 2026-08-24, the red-day directive via
            # scripts/research_smarter.py): NEVER buy a long while the price
            # is below today's open — the tape is against it. OOS +$42,361
            # vs champion's +$26,614, PF 1.81 vs 1.42, wins both windows.
            # S3: with shorts_enabled a below-open name is exactly what the
            # SHORT strategies need to see, so the candidate-level skip
            # moves to the per-SIGNAL check below (same outcome for longs,
            # same log wording). Flag off = this pre-strategy skip verbatim.
            if not shorts_enabled and features.day_open > 0 and features.price < features.day_open:
                logger.info(
                    "trend gate: %s skipped — %.2f below today's open %.2f",
                    features.symbol,
                    features.price,
                    features.day_open,
                )
                continue
            bars = self._hub.bar_builder.bars(features.symbol)
            vwap = self._hub.session_vwap(features.symbol)
            candidate_strategies = strategies
            if features.symbol in climber_symbols:
                candidate_strategies = [s for s in strategies if "VWAP" in s.name.upper()]
            elif features.symbol in fpb_symbols and features.symbol not in members_all:
                # a gapper-watch guest earned its seat for the first-pullback
                # engine only — never fresh breakout fuel for ORB (the exact
                # mistake the climber cohort paid −$761 to teach)
                candidate_strategies = [s for s in strategies if "FPB" in s.name.upper()]
            for strategy in candidate_strategies:
                try:
                    signal = strategy.evaluate(
                        features, bars, vwap, info.regime, is_lull=info.is_lull, now=info.et_time
                    )
                except Exception:
                    logger.exception("strategy %s failed on %s", strategy.name, features.symbol)
                    continue
                if signal is None:
                    continue
                # R1.5: the strategy's OWN size truth, captured before any
                # inversion — the efficacy full-size override restores THIS
                # (a midday-lull half stays half; a full-size entry stays full)
                base_half = signal.half_size
                # CHOP-DAY SIGNAL INVERSION (2026-09-23, the
                # −$854 chop day: "if today we bought the longs as shorts and
                # the shorts as longs we would have made money"). On a
                # DayJudge CHOP verdict at conf >= CHOP_INVERT_MIN_CONF the
                # momentum trigger IS the fade entry: flip the side at the
                # same trigger (short the failed breakout / buy the failed
                # breakdown), stop mirrored, half size. Applied AFTER signal
                # generation and BEFORE every gate below, so the flipped
                # signal faces the full gauntlet for its NEW side —
                # shorts_enabled (a flipped BUY is a plain short; with shorts
                # off it dies right below, by design), SSR, ETB/borrow fee,
                # $2k short floor, cost gate, door judge, brain veto, churn,
                # risk — EXCEPT the trend gate (see _trend_gate_vetoes: on a
                # called chop day the fade is the strategy). Never applies to
                # climber-lane or FPB-guest/FPB signals: those are pullback
                # logic, not breakouts — there is no "failed breakout" to
                # fade, and the lanes carry their own never-chase contracts.
                # UNCLEAR/TREND verdicts, flag off, cap reached, or a
                # degenerate mirror = the signal passes through untouched.
                if (
                    chop_invert
                    and features.symbol not in climber_symbols
                    and features.symbol not in fpb_symbols
                    and "FPB" not in signal.strategy.upper()
                ):
                    verdict, dj_conf = self._day_judge_reading()
                    if verdict == "CHOP" and dj_conf >= CHOP_INVERT_MIN_CONF:
                        inverted_today = getattr(self, "_chop_inverted_today", 0)
                        if inverted_today >= chop_invert_max_per_day:
                            logger.info(
                                "CHOP-INVERT cap reached (%d/day) — %s %s passes uninverted",
                                chop_invert_max_per_day,
                                signal.strategy,
                                signal.symbol,
                            )
                        else:
                            from waveapp.engine.strategies import invert_signal

                            flipped = invert_signal(signal)
                            if flipped is not None:
                                logger.info(
                                    "CHOP-INVERT: %s %s %s→%s @ %.2f, stop %.2f→%.2f "
                                    "(CHOP conf %.2f, half size, %d/%d today)",
                                    signal.strategy,
                                    signal.symbol,
                                    signal.side.value.upper(),
                                    flipped.side.value.upper(),
                                    flipped.entry_price,
                                    signal.stop_price,
                                    flipped.stop_price,
                                    dj_conf,
                                    inverted_today,
                                    chop_invert_max_per_day,
                                )
                                signal = flipped
                # THE EFFICACY GUARD (R1.5, after the −$3,928
                # morning, 2026-09-24): nothing watched whether Wave's OWN
                # entries were following through — two full-size losers
                # inside 25 minutes of the open, 18 losers total, zero
                # feedback. The tracker scores every fill (+1A before −1A
                # within 10 min) and flips the book between MOMENTUM (as-is)
                # and INVERTED (every momentum signal rides the SHARED
                # invert_signal transform — mirrored stop, trend gate stands
                # aside via is_inverted, every other gate judged for the NEW
                # side). FULL size in both modes ("Wave must MAKE
                # money, not trade smaller") — the transform's forced half is
                # overridden back to the strategy's own size; CHOP-triggered
                # flips keep their half-size rule. Same lane exemptions as
                # CHOP-INVERT (climber/FPB are pullback logic — no breakout
                # to fade). A CHOP-inverted signal is never inverted twice —
                # efficacy only upgrades it to full size. An uninvertible
                # signal (degenerate mirror, or a would-be short with shorts
                # off) is SKIPPED, never executed as-is while the book is
                # failing; the MODE stays INVERTED regardless. The hard 3%
                # daily halt (§12) sits untouched above all of this.
                if (
                    efficacy_inverted
                    and features.symbol not in climber_symbols
                    and features.symbol not in fpb_symbols
                    and "FPB" not in signal.strategy.upper()
                ):
                    import dataclasses as _dc

                    from waveapp.engine.strategies import (
                        EFFICACY_INVERT_PREFIX,
                        invert_signal,
                        is_inverted,
                    )

                    if is_inverted(signal):
                        # CHOP already flipped it this pass — invert ONCE;
                        # efficacy's full-size directive wins over chop's half
                        if efficacy_full and signal.half_size and not base_half:
                            signal = _dc.replace(signal, half_size=False)
                            logger.info(
                                "EFFICACY: %s already CHOP-inverted — riding it FULL size",
                                signal.symbol,
                            )
                    else:
                        flipped = invert_signal(signal, prefix=EFFICACY_INVERT_PREFIX)
                        if flipped is not None and efficacy_full:
                            flipped = _dc.replace(flipped, half_size=base_half)
                        if flipped is None or (
                            flipped.side is OrderSide.SELL and not shorts_enabled
                        ):
                            logger.info(
                                "EFFICACY: %s %s uninvertible (%s) — entry skipped,"
                                " mode stays INVERTED",
                                signal.strategy,
                                signal.symbol,
                                "degenerate mirror" if flipped is None else "shorts disabled",
                            )
                            continue
                        logger.info(
                            "EFFICACY-INVERT: %s %s %s→%s @ %.2f, stop %.2f→%.2f (full size: %s)",
                            signal.strategy,
                            signal.symbol,
                            signal.side.value.upper(),
                            flipped.side.value.upper(),
                            flipped.entry_price,
                            signal.stop_price,
                            flipped.stop_price,
                            not flipped.half_size,
                        )
                        signal = flipped
                # PRE-OPEN BIAS (R0.5, the demand after −$854/−$3,928:
                # know what to trade FROM THE FIRST TRADE). While the 9:25
                # verdict governs (9:30 → the Day Judge's first real
                # verdict), the biased-WITH side trades unchanged and the
                # biased-AGAINST side is held to a stricter standard —
                # never hard-blocked: under SHORT bias a momentum BUY must
                # be GREEN vs its OWN prior close (entry above prev_close =
                # real relative strength; a monster gapper bucking the tape
                # is exactly that and passes), mirrored for LONG bias.
                # Chosen over a cost-gate multiple because it is a direct,
                # observable evidence bar with zero TradeGate coupling.
                # Inverted signals are exempt (the chop/efficacy flows own
                # their side), as are the climber/FPB pullback lanes —
                # same lane contract as CHOP-INVERT and the efficacy guard.
                # An unknown prior close FAILS STRICT under bias: with the
                # tape called against the trade, no evidence = no entry.
                if (
                    po_bias in ("SHORT_BIAS", "LONG_BIAS")
                    and features.symbol not in climber_symbols
                    and features.symbol not in fpb_symbols
                    and "FPB" not in signal.strategy.upper()
                ):
                    from waveapp.engine.strategies import is_inverted as _is_inv

                    _pc = float(getattr(features, "prev_close", 0.0) or 0.0)
                    if not _is_inv(signal):
                        if (
                            po_bias == "SHORT_BIAS"
                            and signal.side is OrderSide.BUY
                            and (_pc <= 0 or signal.entry_price <= _pc)
                        ):
                            logger.info(
                                "PRE-OPEN SHORT bias: BUY %s needs green vs prior close"
                                " (entry %.2f vs prev %.2f) — skipped",
                                signal.symbol,
                                signal.entry_price,
                                _pc,
                            )
                            continue
                        if (
                            po_bias == "LONG_BIAS"
                            and signal.side is OrderSide.SELL
                            and (_pc <= 0 or signal.entry_price >= _pc)
                        ):
                            logger.info(
                                "PRE-OPEN LONG bias: SELL %s needs red vs prior close"
                                " (entry %.2f vs prev %.2f) — skipped",
                                signal.symbol,
                                signal.entry_price,
                                _pc,
                            )
                            continue
                if signal.side is OrderSide.SELL:
                    # S3 belt-and-braces: strategies already gate SELL
                    # emission on shorts_enabled and risk.can_enter is the
                    # master refusal — this keeps a rogue SELL from even
                    # reaching the churn/quote/gate machinery (no useless
                    # churn or logs while the flag is off).
                    if not shorts_enabled:
                        logger.debug(
                            "SELL signal dropped — shorts_enabled=false (%s)", signal.symbol
                        )
                        continue
                    # §12 SSR: while active (trigger day + next), downtick-
                    # dependent short ENTRIES are disabled on the symbol.
                    # risk.can_enter blocks too; skipping here saves the
                    # gate/judge/brain churn on a refusal foretold.
                    if self.engine.risk.ssr_active(signal.symbol, info.et_time.date()):
                        logger.info("SSR active on %s — short signal skipped (§12)", signal.symbol)
                        continue
                if shorts_enabled and self._trend_gate_vetoes(signal, features):
                    # TREND GATE, per-signal form (S3) — the logic moved to
                    # _trend_gate_vetoes verbatim (same logs, same outcomes)
                    # so the CHOP-INVERT skip (2026-09-23) has one home:
                    # inverted signals bypass this gate only, both sides.
                    continue
                if signal.symbol in self._signaled or signal.symbol in self._signal_inflight:
                    # one trade per symbol per day (sim fidelity). S3 note:
                    # the mark is SYMBOL-keyed, shared across sides by design
                    # — a long today precludes a short today (and vice
                    # versa); no same-day flip-flopping on one name.
                    continue
                # THE SNIPER BOOK (R2, 2026-09-24 — the validation
                # study): starves, jewel sizing and the
                # FPB conversion, for ORDINARY momentum signals only.
                # Exempt by contract (documented + tested): inverted signals
                # of any kind (chop/efficacy — the fade book has no chase to
                # convert), the climber lane and the FPB-guest/FPB-strategy
                # lanes (pullback logic already; their own never-chase
                # contracts). Runs AFTER the day-mark check so a traded
                # symbol never gets a watch, and BEFORE the ORB late gate —
                # a converted signal skips that gate here; the trigger path
                # re-applies it at the actual FPB fill level (parity).
                if (
                    sniper_on
                    and features.day_open > 0
                    and features.symbol not in climber_symbols
                    and features.symbol not in fpb_symbols
                    and "FPB" not in signal.strategy.upper()
                ):
                    from waveapp.engine import fpb as _fpb
                    from waveapp.engine.strategies import is_inverted as _sn_inv

                    if not _sn_inv(signal):
                        ext = _fpb.extension_pct(signal.entry_price, features.day_open)
                        # starve (b): the 14:00-14:59 ET graveyard. FPB
                        # watches created before 14:00 may still trigger
                        # (the trigger path never checks the hour — and
                        # conversion is starved here first, so no watch is
                        # ever born inside the hour); inverted-book entries
                        # never reach this block at all.
                        if info.et_time.hour == _fpb.STARVE_HOUR_ET:
                            logger.info(
                                "SNIPER starve: %s %s at 14h ET — 19%% WR band (n=27, −$1,334)",
                                signal.strategy,
                                signal.symbol,
                            )
                            self._sniper.stats["starved"] += 1
                            continue
                        if "ORB" in signal.strategy.upper() and signal.side is OrderSide.BUY:
                            # starve (a): ORB longs fight a called downtrend
                            if sniper_verdict == "TREND_DOWN":
                                logger.info(
                                    "SNIPER starve: ORB long on TREND_DOWN — 14%% WR band "
                                    "(n=21, −$2,274): %s",
                                    signal.symbol,
                                )
                                self._sniper.stats["starved"] += 1
                                continue
                            # starve (c): the open-chop break — ORB <1%-ext
                            # while the day is called CHOP (long book; the
                            # pocket was measured before shorts armed)
                            if sniper_verdict == "CHOP" and ext < _fpb.JEWEL_EXT_PCT:
                                logger.info(
                                    "SNIPER starve: ORB <1%%-extension on CHOP — 31%% WR band "
                                    "(n=13, −$1,221): %s at %+.2f%%",
                                    signal.symbol,
                                    ext,
                                )
                                self._sniper.stats["starved"] += 1
                                continue
                        # leg 2 — JEWEL SIZING: GAP BUY <1% above open (WR
                        # 85%, +$4,709 on n=33) rides 1.5× risk; the
                        # notional/impact caps bind on the scaled qty
                        # inside position_size (risk.py).
                        if _fpb.is_jewel(signal, features.day_open):
                            import dataclasses as _dc

                            signal = _dc.replace(signal, size_mult=_fpb.JEWEL_SIZE_MULT)
                            self._sniper.stats["jewel"] += 1
                            logger.info(
                                "SNIPER jewel: GAP %s %+.2f%% vs open — size ×%.1f (88%% WR band)",
                                signal.symbol,
                                ext,
                                _fpb.JEWEL_SIZE_MULT,
                            )
                        # POCKET FEEDS (2026-09-25 "make money now" —
                        # the two measured winners the pocket hunt found and
                        # nobody fed): 13:00-ET-hour ORB = 73% WR +$1,766;
                        # GAP 1-2% band = 73% WR +$3,029. Both ride 1.25×
                        # risk (modest — below the jewel's validated 1.5×);
                        # caps still bind on the scaled qty.
                        elif (
                            signal.strategy == "ORB"
                            and info.et_time.hour == 13
                            and float(getattr(signal, "size_mult", 1.0) or 1.0) == 1.0
                        ):
                            import dataclasses as _dc

                            signal = _dc.replace(signal, size_mult=1.25)
                            self._sniper.stats["pocket13"] = (
                                self._sniper.stats.get("pocket13", 0) + 1
                            )
                            logger.info(
                                "SNIPER pocket: 13h ORB %s — size ×1.25 (73%% WR band)",
                                signal.symbol,
                            )
                        elif (
                            signal.strategy == "GAP"
                            and signal.side is OrderSide.BUY
                            and features.day_open > 0
                            and 1.0 <= ext < 2.0
                        ):
                            import dataclasses as _dc

                            signal = _dc.replace(signal, size_mult=1.25)
                            self._sniper.stats["pocket12"] = (
                                self._sniper.stats.get("pocket12", 0) + 1
                            )
                            logger.info(
                                "SNIPER pocket: GAP 1-2%% band %s %+.2f%% — size ×1.25 (73%% WR)",
                                signal.symbol,
                                ext,
                            )
                        # leg 1 — FPB CONVERSION: ≥2% extended in the
                        # signal's direction is never chased; it becomes a
                        # pullback watch (or dies at the cap/close guard —
                        # the extended entry is NOT taken as-is either way).
                        if _fpb.is_extended(signal, features.day_open):
                            from datetime import UTC as _sn_utc
                            from datetime import datetime as _sn_dt

                            outcome = self._sniper.convert(
                                signal, features, bars, _sn_dt.now(_sn_utc), info.et_time
                            )
                            if outcome == "watch":
                                logger.info(
                                    "SNIPER convert: %s %s %s @ %.2f is %+.2f%% vs open %.2f "
                                    "— pullback watch armed (%d min window, %d watching)",
                                    signal.strategy,
                                    signal.side.value.upper(),
                                    signal.symbol,
                                    signal.entry_price,
                                    ext,
                                    features.day_open,
                                    _fpb.FPB_WINDOW_MIN,
                                    len(self._sniper.watches),
                                )
                            elif outcome == "cap":
                                logger.info(
                                    "SNIPER watch cap (%d) — extended %s %s not taken, not watched",
                                    _fpb.FPB_MAX_WATCHES,
                                    signal.strategy,
                                    signal.symbol,
                                )
                            elif outcome == "late":
                                logger.info(
                                    "SNIPER close guard — no new watches at/after 15:20 ET "
                                    "(%s skipped)",
                                    signal.symbol,
                                )
                            else:  # dup / spent — the watch already spoke
                                logger.debug(
                                    "SNIPER: %s extended signal ignored (%s)",
                                    signal.symbol,
                                    outcome,
                                )
                            if outcome in ("watch", "dup"):
                                break  # the watch owns this symbol's play
                            continue  # cap/late/spent: skip the chase
                # NO-QUOTE DEFERRAL (2026-09-16 forensics: the 14:37 restart
                # burst fired on COLD quotes — "no sane quote: bid 0.0000" →
                # MARKET fills at bounce-tops, $749 of the cohort's $882 loss
                # paid at the fill; morning ladders EARNED +$89). A dead
                # quote defers the entry WITHOUT burning the one-signal-per-
                # day mark — it re-fires next cycle once the stream warms.
                if auto_trade:
                    # torture sim 2026-09-16: a FROZEN 30-min-old quote
                    # passed as warm → dead-mid ladder → late MARKET chase.
                    # A4-7: the 15s age check (and the naive-timestamp =
                    # STALE inversion) now lives in _fresh_quote, shared by
                    # every decision-pricing consumer of latest_quotes.
                    _q = self._fresh_quote(signal.symbol)
                    _bid = float(getattr(_q, "bid_price", 0) or 0) if _q else 0.0
                    _ask = float(getattr(_q, "ask_price", 0) or 0) if _q else 0.0
                    if not _bid or not _ask or _ask <= _bid:
                        logger.info(
                            "entry deferred — no fresh quote for %s (warm-up or frozen feed)",
                            signal.symbol,
                        )
                        continue
                # DEMAND 23 (referee15 SHIP-CANDIDATE; DLLL was +7.4% above
                # open): a late ORB break rides half size. Config-live.
                # S3 mirror: a SELL already ≥ late_pct BELOW the open is the
                # same late chase reflected — same knob, half size.
                late_pct = orb_late_gate_pct
                if (
                    late_pct > 0
                    and "ORB" in signal.strategy.upper()
                    and not signal.half_size
                    and features.day_open > 0
                ):
                    import dataclasses as _dc

                    if signal.side is OrderSide.BUY and signal.entry_price >= (
                        features.day_open * (1 + late_pct / 100.0)
                    ):
                        signal = _dc.replace(signal, half_size=True)
                        logger.info(
                            "ORB late gate: %s entry %.2f is >=%.1f%% above open %.2f — HALF size",
                            signal.symbol,
                            signal.entry_price,
                            late_pct,
                            features.day_open,
                        )
                    elif signal.side is OrderSide.SELL and signal.entry_price <= (
                        features.day_open * (1 - late_pct / 100.0)
                    ):
                        signal = _dc.replace(signal, half_size=True)
                        logger.info(
                            "ORB late gate (short): %s entry %.2f is >=%.1f%% below open %.2f"
                            " — HALF size",
                            signal.symbol,
                            signal.entry_price,
                            late_pct,
                            features.day_open,
                        )

                # direction-aware cost gate (§8.4) at signal time.
                # LIVE SPREAD (audit 2026-09-16 F2): features.spread comes
                # from a snapshot up to 180s old and is silently $0.00 for
                # missing/one-sided quotes — a fictional zero auto-PASSES
                # (the exact inverse of the GYLD fictional-2¢ auto-block).
                # The deferral above already proved a sane live quote
                # exists — hand the gate that truth. (A4-7: fresh-checked
                # again here; a stale read falls back to the snapshot
                # spread rather than pricing the gate off a dead book.)
                live_spread = features.spread
                _gq = self._fresh_quote(signal.symbol)
                if _gq is not None:
                    _gb = float(getattr(_gq, "bid_price", 0) or 0)
                    _ga = float(getattr(_gq, "ask_price", 0) or 0)
                    if _ga > _gb > 0:
                        live_spread = _ga - _gb
                daily_atr = features.atr_pct / 100.0 * features.price
                # S3: per-asset borrow status for SELLs — S0's cache-first
                # adapter.asset_shortable (shortable AND easy_to_borrow from
                # broker metadata). GateInputs defaults FAIL CLOSED, so a
                # lookup failure refuses the short rather than guessing;
                # is_short also charges the modeled ETB borrow fee (§8.4).
                # Longs never make this call.
                _etb_ok = False
                if signal.side is OrderSide.SELL:
                    try:
                        _etb_ok = bool(
                            await self._adapter.asset_shortable(TradingMode.PAPER, signal.symbol)
                        )
                    except Exception:
                        logger.exception(
                            "shortability lookup failed for %s — short fails closed",
                            signal.symbol,
                        )
                decision = self._scanner.gate.evaluate(
                    GateInputs(
                        symbol=signal.symbol,
                        side=signal.side,
                        price=signal.entry_price,
                        expected_move=daily_atr * self._scanner.expected_move_atr_fraction,
                        profit_target=max(signal.entry_price * 0.004, 0.01),
                        spread=live_spread,
                        slippage_buffer=self._scanner.slippage_buffer_per_share,
                        shortable=_etb_ok,
                        easy_to_borrow=_etb_ok,
                    )
                )
                if not decision:
                    # audit 2026-09-16: a gate rejection no longer burns the
                    # one-trade-per-day mark — a transient spread blip used
                    # to blackball the symbol for the whole session. The
                    # signal simply re-fires next cycle against fresh costs.
                    logger.info("signal gated out: %s — %s", signal.reason, decision.reason)
                    continue

                if not auto_trade:
                    # auto-trade OFF: the Telegram push IS the day's action
                    # for this symbol — the burn stays HERE (the one
                    # legitimate pre-execution burn) so the notification
                    # doesn't repeat every cycle
                    self._signaled.add(signal.symbol)
                    logger.info("SIGNAL (auto-trade OFF): %s", signal.reason)
                    await self._push(
                        f"📡 {signal.strategy} signal ({signal.side.value.upper()}) "
                        f"{signal.symbol} @ {signal.entry_price:.2f}, "
                        f"stop {signal.stop_price:.2f}\n{signal.reason}\n"
                        f"(auto-trade OFF — signal only)",
                        klass="signal",  # ON-mode only, per MINIMUM's contract
                    )
                    continue

                # A3-2 (audit 2026-09-22): the day-mark used to burn BEFORE
                # _execute_signal — every downstream refusal blackballed the
                # symbol for the whole session and starved the risk-governed
                # unlimited book (9:40 ceiling-full burns meant nothing could
                # re-signal at 10:30 when the winners were floor-locked).
                # Every _execute_signal False path was read and judged
                # TRANSIENT — none burns the day-mark:
                #   - engine rejection (risk ceiling temporarily full, paused,
                #     entries frozen by a reconcile blip): frees up in minutes
                #   - judge_entry drift veto: a 1% dip/spike that can recover
                #   - yuval-brain veto: fresh verdict each attempt, context
                #     moves (its own journal already dampens re-asks)
                #   - churn guard: keeps its OWN permanent state
                #     (_symbol_entries count, stop-cooldown clock) — a burn
                #     here would only shadow it
                #   - inverse-ETF ban: permanent in fact, but the check is
                #     cached and a re-refusal is a no-op log line
                #   - zero size / failed account snapshot: equity-dependent
                #   - climber no-sane-quote skip: feed warms up
                # The burn now happens ONLY when the entry actually opened
                # (one trade per symbol per day, restart-safe: _roll_signal_day
                # reseeds from today's DB positions). _signal_inflight keeps
                # the check-then-add span race-free across this await.
                self._signal_inflight.add(signal.symbol)
                try:
                    opened = await self._execute_signal(signal, features.avg_daily_volume)
                    if opened:
                        self._signaled.add(signal.symbol)
                        free_slots -= 1
                        # CHOP-INVERT day cap counts OPENED CHOP-inverted
                        # entries only (a gated-out flip must not burn the
                        # cap, and R1.5 EFFICACY inversions self-correct via
                        # their own flip-back rule — they never burn the
                        # detector's damage cap); reset by _roll_signal_day.
                        from waveapp.engine.strategies import is_chop_inverted

                        if is_chop_inverted(signal):
                            self._chop_inverted_today = getattr(self, "_chop_inverted_today", 0) + 1
                finally:
                    self._signal_inflight.discard(signal.symbol)
                break  # next candidate — one strategy per symbol

    async def _sniper_watch_pass(
        self,
        info,
        efficacy_inverted: bool,
        shorts_enabled: bool,
        orb_late_gate_pct: float,
        free_slots: int,
    ) -> int:
        """R2 SNIPER: watch upkeep + triggers, once per pipeline pass (the
        study used minute bars, so the pipeline's minute cadence is the
        contract — per-second monitoring buys nothing).

        Deaths first (journaled): window expiry (the proven skip), day-mark
        burn, the INVERTED flip (an anti-momentum book has no pullback-buys)
        and the 15:20 close guard. Then each surviving watch is checked
        against the tested rule (fpb.check_trigger); a trigger runs the SAME
        gauntlet an ordinary entry runs — trend gate, fresh-quote deferral,
        ORB late gate re-applied at the FPB fill level, cost gate, then
        _execute_signal (churn guard, brain veto, inverse-ETF ban, sizing,
        ladder, judge_entry door all inside). Transient refusals keep the
        watch (A3-2 philosophy: the window bounds the retries); an OPEN
        burns the day-mark and retires the watch. Returns updated
        free_slots."""
        from datetime import UTC as _utc
        from datetime import datetime as _dt

        from waveapp.engine import fpb as _fpb
        from waveapp.engine.tradegate import GateInputs

        book = self._sniper
        now_utc = _dt.now(_utc)
        burned = set(getattr(self, "_signaled", ()) or ()) | set(self._signal_inflight)
        for watch, why in book.prune(now_utc, burned, efficacy_inverted, info.et_time):
            logger.info(
                "SNIPER watch died: %s (%s %s from %.2f) — %s",
                watch.symbol,
                watch.signal.strategy,
                watch.signal.side.value.upper(),
                watch.trigger_price,
                why,
            )
        if self._hub is None or self._scanner is None or not book.watches:
            return free_slots
        for watch in sorted(book.watches.values(), key=lambda w: w.created_at):
            if free_slots <= 0:
                return free_slots  # book full — watches keep their window
            trig = _fpb.check_trigger(
                watch,
                self._hub.bar_builder.bars(watch.symbol),
                self._hub.session_vwap(watch.symbol),
                now_utc,
            )
            if trig is None:
                continue  # no holding pullback yet — keep watching
            feats = watch.features
            if trig.side is OrderSide.SELL:
                # the FPB-short mirror faces the short book's own gates
                if not shorts_enabled:
                    logger.info("SNIPER FPB-short watch killed: %s — shorts disabled", trig.symbol)
                    book.remove(watch.symbol)
                    book.stats["killed"] += 1
                    continue
                if self.engine.risk.ssr_active(trig.symbol, info.et_time.date()):
                    logger.info(
                        "SNIPER FPB-short watch killed: SSR active on %s (§12)", trig.symbol
                    )
                    book.remove(watch.symbol)
                    book.stats["killed"] += 1
                    continue
            from types import SimpleNamespace as _NS

            # trend gate judged at the FPB fill level, not the stale scan px
            gate_feats = _NS(symbol=trig.symbol, price=trig.entry_price, day_open=feats.day_open)
            if self._trend_gate_vetoes(trig, gate_feats):
                continue  # transient — the tape may lift back over the open
            _q = self._fresh_quote(trig.symbol)
            _bid = float(getattr(_q, "bid_price", 0) or 0) if _q else 0.0
            _ask = float(getattr(_q, "ask_price", 0) or 0) if _q else 0.0
            if not _bid or not _ask or _ask <= _bid:
                logger.info("SNIPER trigger deferred — no fresh quote for %s", trig.symbol)
                continue
            # ORB late gate, re-applied at the FPB entry level (parity with
            # the main loop — conversion happened before that gate ran)
            if (
                orb_late_gate_pct > 0
                and "ORB" in trig.strategy.upper()
                and not trig.half_size
                and feats.day_open > 0
            ):
                import dataclasses as _dc

                late_long = trig.side is OrderSide.BUY and trig.entry_price >= (
                    feats.day_open * (1 + orb_late_gate_pct / 100.0)
                )
                late_short = trig.side is OrderSide.SELL and trig.entry_price <= (
                    feats.day_open * (1 - orb_late_gate_pct / 100.0)
                )
                if late_long or late_short:
                    trig = _dc.replace(trig, half_size=True)
                    logger.info("SNIPER: ORB late gate on FPB fill — %s HALF size", trig.symbol)
            _etb_ok = False
            if trig.side is OrderSide.SELL:
                try:
                    _etb_ok = bool(
                        await self._adapter.asset_shortable(TradingMode.PAPER, trig.symbol)
                    )
                except Exception:
                    logger.exception(
                        "shortability lookup failed for %s — FPB-short fails closed",
                        trig.symbol,
                    )
            daily_atr = feats.atr_pct / 100.0 * feats.price
            decision = self._scanner.gate.evaluate(
                GateInputs(
                    symbol=trig.symbol,
                    side=trig.side,
                    price=trig.entry_price,
                    expected_move=daily_atr * self._scanner.expected_move_atr_fraction,
                    profit_target=max(trig.entry_price * 0.004, 0.01),
                    spread=_ask - _bid,
                    slippage_buffer=self._scanner.slippage_buffer_per_share,
                    shortable=_etb_ok,
                    easy_to_borrow=_etb_ok,
                )
            )
            if not decision:
                logger.info("SNIPER trigger gated out: %s — %s", trig.reason, decision.reason)
                continue  # transient costs — the watch window bounds retries
            self._signal_inflight.add(trig.symbol)
            opened = False
            try:
                opened = await self._execute_signal(trig, watch.avg_daily_volume)
                if opened:
                    self._signaled.add(trig.symbol)
                    free_slots -= 1
                    book.remove(trig.symbol)
                    book.stats["triggered"] += 1
                    age_min = (now_utc - watch.created_at).total_seconds() / 60.0
                    logger.info(
                        "SNIPER FPB entry: %s %s %s @ %.2f stop %.2f — pullback held "
                        "%.0f min after trigger %.2f",
                        trig.strategy,
                        trig.side.value.upper(),
                        trig.symbol,
                        trig.entry_price,
                        trig.stop_price,
                        age_min,
                        watch.trigger_price,
                    )
            finally:
                self._signal_inflight.discard(trig.symbol)
        return free_slots

    def _brain_score_menu(self, now_et) -> None:
        """Stage M1+ ('the water is flowing so it should drink
        nonstop'): every menu candidate gets a shadow verdict — journaled as
        executed=0 (pure learning), at most once per symbol per 15 min."""
        if self._brain is None or self._scanner2 is None or not self._scanner2.last_menu:
            return
        from types import SimpleNamespace as _ns

        recent = getattr(self, "_brain_menu_scored", None)
        if recent is None:
            recent = self._brain_menu_scored = {}
        now_ts = now_et.timestamp()
        for entry in self._scanner2.last_menu[:20]:
            symbol = entry["symbol"]
            if now_ts - recent.get(symbol, 0.0) < 300.0:  # re-judge every 5 min
                continue
            recent[symbol] = now_ts
            price = float(entry.get("last") or 0.0)
            row = self._scanner2.baselines.index.get(symbol)
            atr_pct = float(self._scanner2.baselines.atr_pct[row]) if row is not None else 0.0
            atr_dollar = max(atr_pct / 100.0 * price, 0.01)
            if price <= 0:
                continue
            self._brain_shadow_score(
                _ns(
                    symbol=symbol, strategy="SCAN", entry_price=price, stop_price=price - atr_dollar
                ),
                executed=False,
            )
            # 8.13: the v2 challenger judges the SAME name at the same moment
            self._brain_score_menu_v2(symbol, now_ts)
        if len(recent) > 500:
            cutoff = now_ts - 1800.0
            self._brain_menu_scored = {s: t for s, t in recent.items() if t > cutoff}

    def _brain_shadow_score(self, signal, executed: bool = True) -> None:
        """Stage M1: the Brain scores every entry signal the moment it fires
        — SHADOW ONLY. Journals score+vector, feeds the ML-tab dots and the
        nightly precision line. Can NEVER block or delay an entry."""
        if self._brain is None:
            return
        try:
            from datetime import UTC as _utc
            from datetime import datetime as _dt

            from waveapp.engine.brain_features import build_features

            now = _dt.now(_utc)
            features = build_features(
                signal,
                now,
                pm_watch=getattr(self, "_pm_watch", None),
                scanner2=self._scanner2,
            )
            p_win = self._brain.score(features)
            approved = p_win >= 0.55  # v1 curve: ≈60% WR at 34% coverage
            if executed:  # M2 advisory: the entry-time read shows on the card
                scores = getattr(self, "_brain_entry_scores", None)
                if scores is None:
                    scores = self._brain_entry_scores = {}
                scores[signal.symbol] = round(float(p_win), 3)
            shadow = self._brain_shadow
            shadow["scored"] += 1
            shadow["approved" if approved else "rejected"] += 1
            shadow["approved_new" if approved else "rejected_new"] += 1
            logger.info(
                "Brain (shadow): %s %s → p_win %.3f → %s",
                signal.symbol,
                signal.strategy,
                p_win,
                "APPROVE" if approved else "reject",
            )
            if self._database is not None:
                import json as _json

                self._database.execute(
                    "INSERT INTO brain_scores (ts, symbol, strategy, p_win, approved,"
                    " features, executed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        now.isoformat(timespec="seconds"),
                        signal.symbol,
                        str(getattr(signal, "strategy", "")),
                        round(float(p_win), 4),
                        int(approved),
                        _json.dumps(features, separators=(",", ":")),
                        int(executed),
                    ),
                )
            # Yuval-brain (item 13): the advisor judges the SAME moment the
            # ML brain scores — the journals get compared day by day.
            self._advise_entry_candidate(signal)
        except Exception:
            logger.exception("brain shadow scoring failed — entries unaffected")

    def _advise(self, moment) -> None:
        """Fire-and-forget Yuval-brain call — shadow, never awaited inline."""
        brain = getattr(self, "_yuval_brain", None)
        if brain is None:
            return
        task = asyncio.ensure_future(brain.advise(moment))
        self._llm_tasks.add(task)
        task.add_done_callback(self._llm_tasks.discard)

    def _last_bars_for(self, symbol: str, n: int = 20) -> list:
        from waveapp.engine.yuval_brain import MinuteBar

        out = []
        with contextlib.suppress(Exception):  # no bars = empty card, fine
            for b in self._hub.bar_builder.bars(symbol)[-n:]:
                out.append(MinuteBar(o=b.open, h=b.high, low=b.low, c=b.close, v=b.volume))
        return out

    def _moment_context(self, symbol: str) -> dict:
        """rvol + breadth + clock for the advisor's card (same-day judgment
        needs the same numbers a human glance uses)."""
        ctx: dict = {}
        try:
            row = self._scanner2._index.get(symbol)
            base = self._scanner2.baselines.index.get(symbol)
            if row is not None and base is not None:
                from datetime import datetime as _rdt
                from zoneinfo import ZoneInfo as _rz

                import numpy as _np

                from waveapp.engine.scanner2 import minute_index as _s2_minute

                now_r = _rdt.now(tz=_rz("America/New_York"))
                # canonical 4:00-ET axis (audit 2026-09-16 F1 — same wrong-
                # axis math as the climber lane, inflating the advisor's
                # rvol context ×10-26)
                minute = _s2_minute(now_r)
                if minute is not None:
                    expected = max(
                        float(self._scanner2.baselines.expected_cum(_np.array([base]), minute)[0]),
                        1.0,
                    )
                    ctx["rvol"] = float(self._scanner2.cum_vol[row]) / expected
        except Exception:
            logger.debug("moment rvol lookup skipped")
        try:
            from datetime import datetime as _dt
            from pathlib import Path as _P
            from zoneinfo import ZoneInfo as _Z

            et = _Z("America/New_York")
            now = _dt.now(tz=et)
            ctx["minutes_since_open"] = max(
                0.0, (now - now.replace(hour=9, minute=30, second=0)).total_seconds() / 60
            )
            bpath = (
                _P(__file__).resolve().parent.parent.parent
                / "research"
                / "menu_breadth"
                / f"{now:%Y-%m-%d}.csv"
            )
            last = bpath.read_text().strip().rsplit("\n", 1)[-1].split(",")
            ctx["breadth_below_open"] = float(last[3])
        except Exception:
            logger.debug("moment breadth lookup skipped")
        return ctx

    def _advise_entry_candidate(self, signal) -> None:
        if getattr(self, "_yuval_brain", None) is None:
            return
        try:
            from waveapp.engine.yuval_brain import AdvisorMoment, NewsFlags

            bars = self._last_bars_for(signal.symbol)
            news = None
            tags = getattr(self._scanner2, "llm_tags", {}).get(signal.symbol)
            if tags:
                news = NewsFlags(
                    catalyst=True,
                    direction=int(tags.get("dir", 0)) if isinstance(tags, dict) else 0,
                    age_min=0.0,
                )
            self._advise(
                AdvisorMoment(
                    kind="entry_candidate",
                    symbol=signal.symbol,
                    current_price=float(getattr(signal, "entry_price", 0.0) or 0.0),
                    bars=bars,
                    day_open=self._day_open_for(signal.symbol, bars),
                    strategy=str(getattr(signal, "strategy", "")),
                    news=news,
                    **self._moment_context(signal.symbol),
                )
            )
        except Exception:
            logger.debug("yuval-brain entry moment build failed — skipped")

    def _resolve_brain_scores(self) -> None:
        """Nightly: match unresolved scores to closed positions → the live
        precision line (the ladder's evidence). Runs in a worker thread."""
        if self._database is None:
            return
        try:
            from datetime import UTC as _utc
            from datetime import datetime as _dt

            rows = self._database.query(
                "SELECT rowid, ts, symbol, p_win, approved FROM brain_scores"
                " WHERE won IS NULL AND executed = 1"
                " AND datetime(ts) < datetime('now', '-4 hours')"
            )
            if not rows:
                return
            stamp = _dt.now(_utc).isoformat(timespec="seconds")
            resolved = wins_approved = n_approved = wins_all = n_all = 0
            for row in rows:
                match = self._database.query(
                    "SELECT realized_pnl FROM positions WHERE symbol = ?"
                    " AND state = 'closed'"
                    " AND datetime(opened_at) >= datetime(?, '-15 minutes')"
                    " AND datetime(opened_at) <= datetime(?, '+15 minutes')"
                    " ORDER BY opened_at LIMIT 1",
                    (row["symbol"], row["ts"], row["ts"]),
                )
                if not match or match[0]["realized_pnl"] is None:
                    continue
                pnl = float(match[0]["realized_pnl"])
                won = int(pnl > 0)
                self._database.execute(
                    "UPDATE brain_scores SET resolved_at = ?, won = ?, outcome_pnl = ?"
                    " WHERE rowid = ?",
                    (stamp, won, round(pnl, 2), row["rowid"]),
                )
                resolved += 1
                n_all += 1
                wins_all += won
                if row["approved"]:
                    n_approved += 1
                    wins_approved += won
            if resolved:
                brain_wr = wins_approved / n_approved * 100 if n_approved else 0.0
                all_wr = wins_all / n_all * 100 if n_all else 0.0
                logger.info(
                    "Brain scoreboard: resolved %d — its approvals won %.0f%% (%d/%d)"
                    " vs all signals %.0f%% (%d/%d)",
                    resolved,
                    brain_wr,
                    wins_approved,
                    n_approved,
                    all_wr,
                    wins_all,
                    n_all,
                )
        except Exception:
            logger.exception("brain score resolution failed")

    # -- v2 nightly challenger (blueprint 8.1/8.2/8.4/8.13) ------------------

    def _load_brain_v2(self) -> None:
        """Load the nightly v2 artifact (same checksum + golden-vector armor
        as v1). Absent or doubted → v2 simply doesn't score; v1 and rules
        are untouched."""
        try:
            from waveapp.config import support_dir
            from waveapp.engine.brain import load_brain
            from waveapp.engine.brain_nightly import V2_ARTIFACT

            self._brain2 = load_brain(path=support_dir() / V2_ARTIFACT)
            if self._brain2 is not None:
                logger.info("brain v2 (nightly live-features) loaded — shadow challenger armed")
        except Exception:
            logger.exception("brain v2 load crashed — v1/rules unaffected")
            self._brain2 = None

    def _brain_score_menu_v2(self, symbol: str, now_ts: float) -> None:
        """Score one menu name with the v2 challenger on the SAME stream v1
        just scored (8.13's head-to-head). Features come from scanner2's
        live_feature_row — the identical code path the journal writes."""
        if self._brain2 is None or self._scanner2 is None:
            return
        try:
            import json as _json
            from datetime import UTC as _utc
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _zi

            from waveapp.engine.brain_features import v2_vector
            from waveapp.engine.brain_nightly import NOISE_PROBES, noise_probe

            feats = self._scanner2.live_feature_row(symbol)
            if feats is None:
                return
            now = _dt.now(_utc)
            vec = v2_vector(feats, now.astimezone(_zi("America/New_York")))
            # probes ride along live exactly as in training (same hash draw)
            ts_key = now.isoformat(timespec="seconds")
            vec[NOISE_PROBES[0]] = noise_probe(symbol, ts_key, "a")
            vec[NOISE_PROBES[1]] = noise_probe(symbol, ts_key, "b")
            p_win = self._brain2.score(vec)
            approved = p_win >= 0.55
            if self._database is not None:
                self._database.execute(
                    "INSERT INTO brain_scores (ts, symbol, strategy, p_win, approved,"
                    " features, executed, model) VALUES (?, ?, ?, ?, ?, ?, 0, 'v2')",
                    (
                        now.isoformat(timespec="seconds"),
                        symbol,
                        "SCAN",
                        round(float(p_win), 4),
                        int(approved),
                        _json.dumps(vec, separators=(",", ":")),
                    ),
                )
        except Exception:
            logger.exception("brain v2 shadow scoring failed — nothing affected")

    def _resolve_shadow_scores(self) -> None:
        """Nightly: resolve the executed=0 menu-shadow rows (both models)
        against snapshot_labels — the 8.13 head-to-head evidence. A shadow
        score 'won' when the 60-min triple-barrier from (about) that minute
        hit the profit barrier first."""
        if self._database is None:
            return
        try:
            from datetime import UTC as _utc
            from datetime import datetime as _dt

            rows = self._database.query(
                "SELECT rowid, ts, symbol, model, approved FROM brain_scores"
                " WHERE won IS NULL AND executed = 0"
                " AND datetime(ts) < datetime('now', '-20 hours') LIMIT 4000"
            )
            if not rows:
                return
            stamp = _dt.now(_utc).isoformat(timespec="seconds")
            per_model: dict[str, list[int]] = {}
            resolved = 0
            for row in rows:
                match = self._database.query(
                    "SELECT label_60m FROM snapshot_labels WHERE symbol = ?"
                    " AND label_60m IS NOT NULL"
                    " AND datetime(ts) >= datetime(?, '-5 minutes')"
                    " AND datetime(ts) <= datetime(?, '+5 minutes')"
                    " ORDER BY ABS(julianday(ts) - julianday(?)) LIMIT 1",
                    (row["symbol"], row["ts"], row["ts"], row["ts"]),
                )
                if not match:
                    continue
                won = int(str(match[0]["label_60m"]) == "win")
                self._database.execute(
                    "UPDATE brain_scores SET resolved_at = ?, won = ? WHERE rowid = ?",
                    (stamp, won, row["rowid"]),
                )
                resolved += 1
                model = str(row["model"] or "v1")
                if row["approved"]:
                    per_model.setdefault(model, []).append(won)
            if resolved:
                parts = []
                for model, outcomes in sorted(per_model.items()):
                    if outcomes:
                        parts.append(
                            f"{model} approvals {sum(outcomes)}/{len(outcomes)}"
                            f" ({sum(outcomes) / len(outcomes) * 100:.0f}%)"
                        )
                logger.info(
                    "Brain shadow head-to-head: resolved %d menu scores — %s",
                    resolved,
                    "; ".join(parts) if parts else "no approvals in batch",
                )
        except Exception:
            logger.exception("shadow score resolution failed")

    async def _nightly_ml_loop(self) -> None:
        """Blueprint 8.1 + 8.2, on one clock: while the market world is
        asleep — label pending snapshot days, resolve shadow scores, then
        (at most once per day, guarded) the full v2 retrain."""
        from waveapp.engine.session import SessionScheduler

        await asyncio.sleep(120.0)  # never compete with startup
        trained_day = None
        while True:
            try:
                regime = SessionScheduler.regime()
                if not self._labeler_allowed(regime) or self._database is None:
                    await asyncio.sleep(600)
                    continue
                # Broker-truth per-trade P&L: once per day
                # after the close, rewrite positions.realized_pnl from the
                # broker's own FILL activities — hand fills and lineage
                # splits land in the right trade's books.
                from datetime import datetime as _dt

                # ET, not UTC (audit 2026-09-16 build-before-open: this pass
                # only runs in OVERNIGHT/CLOSED, which starts 20:00 ET =
                # 00:00 UTC — the UTC date had ALWAYS rolled, so every
                # nightly pass reconciled a day with zero fills, logged
                # "0 corrected" and stamped the real day consumed. Zero
                # true pnl-truth lines existed in the live DB.)
                from zoneinfo import ZoneInfo as _pt_zone

                from waveapp.engine import brain_nightly

                today = _dt.now(tz=_pt_zone("America/New_York")).date().isoformat()
                if getattr(self, "_pnl_truth_day", None) != today and self._adapter is not None:
                    try:
                        from waveapp.engine.pnl_truth import reconcile_day

                        changed = await reconcile_day(
                            self._adapter, TradingMode.PAPER, self._database, today
                        )
                        self._pnl_truth_day = today
                        # always log — a silent pass is indistinguishable
                        # from a pass that never ran (2026-09-16 morning)
                        logger.info(
                            "pnl truth: pass complete — %d lineage(s) corrected", len(changed)
                        )
                    except Exception:
                        logger.exception("pnl truth pass failed — retrying next cycle")

                # M0 rollup (ML master plan, 2026-09-23): one ml_daily summary
                # row per session day, once per nightly window — failure-safe
                if getattr(self, "_ml_rollup_day", None) != today:
                    self._ml_rollup_day = today
                    self._ml_daily_rollup(today)

                batch = await asyncio.to_thread(brain_nightly.pending_symbol_days, self._database)
                labeled = 0
                for symbol, session_date, snaps in batch:
                    if not self._labeler_allowed(SessionScheduler.regime()):
                        break  # market woke up mid-batch
                    bars = await self._fetch_day_bars(symbol, session_date)
                    labeled += await asyncio.to_thread(
                        brain_nightly.label_symbol_day,
                        self._database,
                        symbol,
                        session_date,
                        snaps,
                        bars,
                    )
                    await asyncio.sleep(0.25)  # gentle on the data API
                if labeled:
                    logger.info(
                        "snapshot labeler: +%d lessons from %d symbol-days", labeled, len(batch)
                    )
                await asyncio.to_thread(self._resolve_shadow_scores)
                if not batch:  # caught up → retrain window (once per day)
                    from datetime import UTC as _utc
                    from datetime import datetime as _dt

                    today = _dt.now(_utc).date()
                    if trained_day != today:
                        trained_day = today
                        from waveapp.config import support_dir

                        summary = await asyncio.to_thread(
                            brain_nightly.train_nightly, self._database, support_dir()
                        )
                        if summary is not None:
                            first = self._brain2 is None
                            self._load_brain_v2()
                            if first and self._brain2 is not None:
                                # the P3 timer (2026-09-02): the first
                                # graduation IS the wake-up call
                                await self._push(
                                    "🎓 The v2 Brain graduated school — first"
                                    f" nightly model trained ({summary['rows']:,}"
                                    f" lessons, {summary['days']} days, AUC"
                                    f" {summary['auc']:.2f}). Time to"
                                    " start the P3 evaluation.",
                                    klass="status",
                                )
                    await asyncio.sleep(3600)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("nightly ml cycle failed")
                await asyncio.sleep(600)
            await asyncio.sleep(60)

    _INVERSE_NAME_RE = None
    _inverse_cache: dict[str, bool] = {}

    async def _is_inverse_etf(self, symbol: str) -> bool:
        """Inverse/bear-product detector. Checks the broker's
        asset NAME for bear/inverse/ultrashort markers; cached per symbol.
        Unknown or unreachable -> False (never blocks ordinary stocks)."""
        import re

        if symbol in self._inverse_cache:
            return self._inverse_cache[symbol]
        if ConnectionMonitor._INVERSE_NAME_RE is None:
            ConnectionMonitor._INVERSE_NAME_RE = re.compile(
                r"(inverse|\bbear\b|ultrashort|\bshort\b|-1x|-2x|-3x)", re.IGNORECASE
            )
        verdict = False
        try:
            if self._adapter is not None:
                from waveapp.broker.base import TradingMode as _TM

                asset = await self._adapter.get_asset(_TM.PAPER, symbol)
                name = str(getattr(asset, "name", "") or "")
                verdict = bool(ConnectionMonitor._INVERSE_NAME_RE.search(name))
        except Exception:
            logger.debug("inverse-ETF check failed for %s — allowing", symbol)
        self._inverse_cache[symbol] = verdict
        return verdict

    async def _mk_rebuy(
        self, symbol: str, px: float, stop_px: float, tag: str, qty_cap: float | None = None
    ) -> bool:
        """Master Key rebuy — a NEW position through the standard entry path.

        Everything that guards a scanner entry guards this: engine must be
        RUNNING (pause/stop/kill all refuse), auto_trade must be armed,
        RiskEngine sizes it fixed-fractionally and can reject it, the entry
        ladder posts it, and the bracket's server-side stop rides along
        (hard rule 3). Returns False on any refusal — the brain's ledger is
        rolled back by the caller. Note core.py's named invariant: this is a
        NEW position after a full exit, never an add below cost.
        """
        from waveapp.broker.base import OrderSide
        from waveapp.config import AppConfig
        from waveapp.engine.strategies import EntrySignal

        try:
            if not AppConfig.load().auto_trade:
                logger.info("MK rebuy %s refused: auto_trade is off", symbol)
                return False
        except Exception:
            logger.exception("MK rebuy %s refused: config unreadable", symbol)
            return False
        if self.engine is None or self.engine.state.value != "running":
            logger.info("MK rebuy %s refused: engine not running", symbol)
            return False
        # A4-7 (audit 2026-09-22): this path reaches _execute_signal WITHOUT
        # ever passing the pipeline's no-fresh-quote deferral — and a halted
        # symbol serves its last quote indefinitely, so a dead bid could
        # wave a rebuy straight into a halt-resume. Stale/missing quote →
        # defer THIS tick (caller rolls the brain ledger back and may
        # retry); logged once per staleness episode, not every tick.
        if self._fresh_quote(symbol) is None:
            if symbol not in self._mk_rebuy_stale_logged:
                self._mk_rebuy_stale_logged.add(symbol)
                logger.warning(
                    "MK rebuy %s deferred: no fresh quote (stale/halted feed) — "
                    "will retry once the stream warms",
                    symbol,
                )
            return False
        self._mk_rebuy_stale_logged.discard(symbol)
        # A2-6 leg 2 RESOLVED (audit 2026-09-22) — decision: the MKRB spec
        # deliberately KEEPS PositionSpec's default manager="judge", it is
        # NOT set to "kitchen". Verified against the code as it stands:
        # the judge is the sole manager (2026-09-21) — with
        # manager="judge" the kitchen is bookkeeper-only on this leg
        # (shadow_kitchen._process: drive requires mgr=="kitchen") and
        # _judge_second sets engine.hold_cuts=True so the MK ledger's cut
        # fire is held for the judge to enforce; the A2-6 rebind fix resets
        # sp.judge/judge_acted and re-seeds entry from the NEW actor's fill
        # precisely so the judge manages the rebought leg cleanly. Stamping
        # manager="kitchen" here would flip the kitchen back into driving
        # exits/stop-ratchet on MKRB legs and demote the judge to
        # watch-only — reintroducing the two-manager regime the duel
        # retired. The MK brain keeps its ShadowPosition lineage for
        # ledger/rebuy bookkeeping only.
        signal = EntrySignal(
            symbol=symbol,
            side=OrderSide.BUY,
            confidence=0.6,
            reason=f"Master Key {tag}",
            strategy="MKRB",
            entry_price=px,
            stop_price=round(stop_px, 2),
        )
        try:
            return bool(await self._execute_signal(signal, max_qty=qty_cap))
        except Exception:
            logger.exception("MK rebuy %s failed at the entry path", symbol)
            return False

    def _day_open_for(self, symbol: str, bars) -> float | None:
        """TRUE session open — scanner2's day_open first (the advisor's
        2026-09-16 'up 0.3% from open' bug: bars[0] of a 20-bar window is
        NOT the open); last-20 first bar only as the fallback."""
        try:
            row = self._scanner2._index.get(symbol)
            if row is not None:
                do = float(self._scanner2.day_open[row])
                if do > 0:
                    return do
        except Exception:
            logger.debug("day-open lookup fell back to bars[0]")
        return bars[0].o if bars else None

    async def _yuval_brain_gate_ok(self, signal) -> bool:
        """Advisor veto on EXTENDED entries only (armed 2026-09-16).
        True = entry proceeds. Any doubt — no advisor, no bars, timeout,
        low confidence, non-skip verdict — proceeds: the veto can only
        tighten, and only inside its proven pattern."""
        brain = getattr(self, "_yuval_brain", None)
        if brain is None:
            return True
        try:
            from waveapp.config import AppConfig as _YbCfg

            cfg = _YbCfg.load()
            if not getattr(cfg, "yuval_brain_gate", True):
                return True
            stretch_min = float(getattr(cfg, "yb_gate_stretch_pct", 4.0))
            conf_min = float(getattr(cfg, "yb_gate_confidence", 0.9))
            bars = self._last_bars_for(signal.symbol)
            if not bars:
                return True
            day_open = self._day_open_for(signal.symbol, bars)
            price = float(getattr(signal, "entry_price", 0.0) or 0.0)
            # S3 (the short side): the veto's mandate is DIRECTIONAL
            # extension — a short entered 20% BELOW the open is the mirror
            # of the finished-move top-buy. is_short only ever holds for a
            # SELL signal, which itself exists only behind shorts_enabled,
            # so the long-only math below is bit-identical with the flag off.
            is_short = getattr(signal, "side", None) is OrderSide.SELL
            stretch_pct = 0.0
            if day_open and price:
                stretch_pct = (price / day_open - 1.0) * 100.0
                if is_short:
                    stretch_pct = -stretch_pct  # extension DOWN, in the trade's favor
            if not day_open or not price or stretch_pct < stretch_min:
                return True  # not an extended entry — outside the veto's mandate
            from waveapp.engine.yuval_brain import AdvisorMoment

            moment = AdvisorMoment(
                kind="entry_candidate",
                symbol=signal.symbol,
                current_price=price,
                bars=bars,
                day_open=day_open,
                # the advisor prompt is long-framed; the SHORT tag is the
                # minimal honest way to hand it the direction (S3)
                strategy=str(getattr(signal, "strategy", "")) + (" SHORT" if is_short else ""),
                **self._moment_context(signal.symbol),
            )
            timeout_s = float(getattr(cfg, "yb_gate_timeout_s", 10.0))
            verdict = await asyncio.wait_for(brain.advise(moment), timeout=timeout_s)
            if verdict.action == "skip" and verdict.confidence >= conf_min:
                logger.warning(
                    "YUVAL-BRAIN VETO: %s refused (extended %.1f%% %s open; conf %.2f): %s",
                    signal.symbol,
                    stretch_pct,
                    "below" if is_short else "above",
                    verdict.confidence,
                    verdict.reason,
                )
                self._spawn(
                    self._push(
                        f"🧠 {signal.symbol} entry refused by your brain "
                        f"({verdict.confidence:.0%}): {verdict.reason}",
                        klass="status",
                    ),
                    "brain-veto push",
                )
                return False
        except TimeoutError:
            # SMTC 13:16 lesson: the verdict lost the race by 300ms while a
            # fresh journaled skip already existed. Consult the journal
            # before proceeding blind.
            try:
                if self._database is not None:
                    row = self._database.query(
                        # datetime(ts) normalizes the ISO 'T' separator —
                        # audit 2026-09-16 (empirically reproduced): the raw
                        # TEXT compare let EVERY same-day verdict pass the
                        # 10-minute filter ('T' > ' '), so a stale morning
                        # skip could veto a good afternoon entry.
                        "SELECT verdict, confidence FROM yuval_brain_verdicts "
                        "WHERE symbol=? AND kind='entry_candidate' "
                        "AND datetime(ts) >= datetime('now', '-10 minutes') "
                        "ORDER BY ts DESC LIMIT 1",
                        (signal.symbol,),
                    )
                    if row and row[0]["verdict"] == "skip" and row[0]["confidence"] >= conf_min:
                        logger.warning(
                            "YUVAL-BRAIN VETO (journal, post-timeout): %s refused "
                            "(fresh skip at %.2f conf)",
                            signal.symbol,
                            row[0]["confidence"],
                        )
                        return False
            except Exception:
                logger.debug("journal fallback failed — entry proceeds")
            logger.info("yuval-brain gate timed out, no fresh journal skip — entry proceeds")
        except Exception:
            logger.exception("yuval-brain gate failed — entry proceeds")
        return True

    async def _execute_signal(
        self,
        signal,
        avg_daily_volume: float | None = None,
        auction: bool = False,
        max_qty: float | None = None,
    ) -> bool:
        from waveapp.broker.base import OrderType
        from waveapp.engine.actor import PositionSpec

        self._brain_shadow_score(signal)  # M1 shadow — never blocks

        # YUVAL-BRAIN VETO (armed by 2026-09-16 midday):
        # the advisor may REFUSE extended entries — the finished-move
        # top-buy it called 5-for-5 (DLLL/TEM/LITX/AXTI/SPAL). Scope:
        # entries >= yb_gate_stretch_pct above the day's open, skip verdict
        # at confidence >= yb_gate_confidence, bounded 4s wait, fallback =
        # proceed. Tightening-only (it can only block, never add); kill
        # switch yuval_brain_gate=false disarms without a rebuild.
        if not await self._yuval_brain_gate_ok(signal):
            return False

        # INVERSE-ETF BAN (2026-09-14): long entries on
        # inverse/bear ETFs are the 9/4 poison — refused outright.
        try:
            from waveapp.broker.base import OrderSide as _OS
            from waveapp.config import AppConfig as _BanCfg

            if (
                signal.side is _OS.BUY
                and getattr(_BanCfg.load(), "inverse_etf_ban", True)
                and await self._is_inverse_etf(signal.symbol)
            ):
                logger.info("INVERSE-ETF BAN: %s long refused", signal.symbol)
                return False
        except Exception:
            logger.exception("inverse-ETF ban check failed — entry proceeds")

        # CHURN GUARD (2026-09-09, the TYRA lesson): scanner entries only
        if signal.strategy != "MKRB":
            import time as _time

            from waveapp.config import AppConfig as _CGCfg

            try:
                _cg = _CGCfg.load()
                if _cg.churn_guard:
                    hit = self._symbol_stop_hit_at.get(signal.symbol)
                    cool = _cg.symbol_reentry_cooldown_min * 60
                    if hit is not None and _time.time() - hit < cool:
                        logger.info(
                            "CHURN GUARD: %s blocked — stopped %.0f min ago (< %d min cooldown)",
                            signal.symbol,
                            (_time.time() - hit) / 60,
                            _cg.symbol_reentry_cooldown_min,
                        )
                        return False
                    if self._symbol_entries.get(signal.symbol, 0) >= _cg.symbol_max_entries_per_day:
                        logger.info(
                            "CHURN GUARD: %s blocked — already %d scanner entries today",
                            signal.symbol,
                            self._symbol_entries[signal.symbol],
                        )
                        return False
            except Exception:
                logger.exception("churn guard check failed — entry proceeds")

        try:
            # §0.7 LANDMINE (A3-10, audit 2026-09-22): TradingMode.PAPER is
            # hardcoded — the monitor is paper-pinned by design today, but
            # position SIZING reads this account's equity, so live mode
            # (Phase 11) must thread the real mode through here or live
            # trades get sized off the paper account. Type-level separation
            # per hard rule 7; do not "fix" with a string flag.
            account = await self._adapter.get_account(TradingMode.PAPER)
        except Exception:
            logger.exception("sizing failed — no account snapshot")
            return False
        # R2 SNIPER jewel sizing: the signal's size_mult scales the RISK
        # BUDGET inside position_size so the notional/impact caps bind on
        # the SCALED qty. Passed only when it deviates from 1.0 — the
        # ordinary path (and every test shim of risk) stays byte-identical.
        _size_mult = float(getattr(signal, "size_mult", 1.0) or 1.0)
        _size_kwargs = {"avg_daily_volume": avg_daily_volume}
        if _size_mult != 1.0:
            _size_kwargs["risk_mult"] = _size_mult
        qty = self.engine.risk.position_size(
            account.equity,
            signal.entry_price,
            signal.stop_price,
            signal.side,
            **_size_kwargs,
        )
        if signal.half_size:
            qty //= 2
        if max_qty is not None and qty > max_qty:
            # size parity (2026-09-09, the MVLL 834-vs-254 rebuy): never bet
            # bigger than the size the recorded evidence was collected on
            logger.info(
                "%s sized %d by risk but capped to %d (evidence parity)",
                signal.symbol,
                qty,
                int(max_qty),
            )
            qty = int(max_qty)
        if qty < 1:
            logger.info("signal sized to zero — skipped (%s)", signal.reason)
            return False
        # 6.5 ENTRY LADDER (adopted 2026-09-02, 4/4 both fill models): post
        # at the quote mid, actor falls back to MARKET after the timeout.
        # Requires a sane two-sided quote; anything odd → the old MARKET way.
        # A4-7: fresh-checked — a stale quote is treated exactly like NO
        # quote (never a phantom mid), so it takes the existing no-quote
        # branches below: climber → skipped ("never chases"), else MARKET.
        quote_now = self._fresh_quote(signal.symbol)
        bid_now = float(getattr(quote_now, "bid_price", 0) or 0)
        ask_now = float(getattr(quote_now, "ask_price", 0) or 0)
        ladder_on = True
        try:
            from waveapp.config import AppConfig as _cfg_ladder

            ladder_on = bool(getattr(_cfg_ladder.load(), "entry_ladder", True))
        except Exception:
            logger.debug("ladder config read failed — ladder stays on")
        use_ladder = (
            ladder_on
            and not auction
            and bid_now > 0
            and ask_now > bid_now
            and (ask_now - bid_now) < 0.02 * signal.entry_price  # busted-quote guard
        )
        if use_ladder:
            import math as _math

            mid_raw = (bid_now + ask_now) / 2.0
            # round toward passivity: floor for buys, ceil for shorts
            ladder_mid = (
                _math.floor(mid_raw * 100.0) / 100.0
                if signal.side is OrderSide.BUY
                else _math.ceil(mid_raw * 100.0) / 100.0
            )
        elif ladder_on and not auction:
            if "climber" in (signal.reason or ""):
                # audit 2026-09-16: the never-chase invariant's last hole —
                # a busted/wide quote disabled the ladder and sent the
                # climber to MARKET. A climber that cannot ladder does not
                # enter, period.
                logger.info(
                    "CLIMBER skipped for %s (no sane quote: bid %.4f ask %.4f) — never chases",
                    signal.symbol,
                    bid_now,
                    ask_now,
                )
                return False
            logger.info(
                "LADDER skipped for %s (no sane quote: bid %.4f ask %.4f) — MARKET entry",
                signal.symbol,
                bid_now,
                ask_now,
            )
        spec = PositionSpec(
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            stop_price=signal.stop_price,
            strategy=signal.strategy,
            decision_price=signal.entry_price,
            # auction entries are limit-on-open: capped 1% above the last
            # pre-market print so a wild auction spike can't chase Wave in
            entry_type=OrderType.LIMIT if (auction or use_ladder) else OrderType.MARKET,
            limit_price=round(signal.entry_price * 1.01, 2)
            if auction
            else (ladder_mid if use_ladder else None),
            ladder=use_ladder,
            # climbers never chase: unfilled ladder = abandoned entry
            # (2026-09-16 autopsy — the chase cost $495 of a $633 cohort loss)
            ladder_skip_ok=use_ladder and "climber" in (signal.reason or ""),
            auction_open=auction,
        )
        # A4-7: the door judge must never compare the decision price to a
        # DEAD bid (a halted symbol's last quote could wave an entry into
        # the halt-resume — the CUE class again). Stale/missing quote →
        # zeros, and judge_entry declines to judge (returns True, ""); the
        # pipeline's no-fresh-quote deferral upstream owns the delay.
        # A3-9 (audit 2026-09-22): the 9:20 PMOM auction queue has NO
        # upstream deferral and judges on PRE-MARKET quotes, where prints
        # are sparse — the RTH 15s window read nearly every name as
        # quote-less, and a lingering wide 9:20 spread could false-veto.
        # The auction path gets a generous 120s window instead.
        quote = self._fresh_quote(signal.symbol, max_age_s=120.0 if auction else 15.0)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        # the judge at the door (2026-09-22, the CUE lesson): a signal
        # whose live quote already broke away from the decision price is
        # judged before any order goes out — a small dip buys, a broken
        # spike does not
        from waveapp.engine.position_judge import judge_entry

        if auction and quote is None:
            # A3-9: no fresh pre-market quote → skip the door judge outright
            # rather than feed it zeros. The auction entry is limit-on-open
            # capped +1% above the last pre-market print (spec above) —
            # THAT cap is the chase guard here, not the judge. pmom_auction
            # is config-off today; this path stays minimal on purpose.
            buy_ok, jwhy = True, ""
        else:
            buy_ok, jwhy = judge_entry(
                signal.side is OrderSide.BUY, float(signal.entry_price), bid, ask, signal.symbol
            )
        if not buy_ok:
            logger.info("JUDGE entry veto: %s", jwhy)
            return False
        if jwhy:
            logger.info("JUDGE entry: %s", jwhy)
        result = await self.engine.open_position(spec)
        if isinstance(result, str):
            logger.warning("entry rejected by engine: %s", result)
            return False
        # decision price + NBBO at submit → fill-quality calibration
        # (§11.2: the paper campaign's primary measurement)
        if signal.strategy != "MKRB":  # churn guard bookkeeping
            self._symbol_entries[signal.symbol] = self._symbol_entries.get(signal.symbol, 0) + 1
        # M0: the AUTO ENTRY moment — entry_lag's ts_signal, consumed when
        # the fill is first observed (observation only)
        try:
            from waveapp.persistence.db import utc_now as _m0_now

            self._entry_signal_at[signal.symbol] = _m0_now()
        except Exception:
            logger.debug("entry signal stamp failed", exc_info=True)
        logger.info(
            "AUTO ENTRY: %s %s x%d (%s) decision=%.4f bid=%.4f ask=%.4f",
            signal.side.value,
            signal.symbol,
            qty,
            signal.strategy,
            signal.entry_price,
            bid,
            ask,
        )
        # 6.4: remember the submit-time quote — the symbol_costs row is
        # written when the position closes (entry fill known by then)
        quotes = getattr(self, "_entry_quotes", None)
        if quotes is None:
            quotes = self._entry_quotes = {}
        quotes[signal.symbol] = (bid, ask, float(signal.entry_price))
        return True

    # -- error surfacing (every logged ERROR reaches the phone) -----------------

    def _install_error_alerts(self) -> None:
        import asyncio as aio

        from waveapp.engine.error_alerts import ErrorAlertHandler

        loop = aio.get_running_loop()

        def alert(text: str) -> None:  # may fire from any thread
            # klass="error": errors bypass the MINIMUM filter entirely (A5-2)
            # _spawn, not bare ensure_future (A5-6): the alert must survive GC
            loop.call_soon_threadsafe(
                lambda: self._spawn(self._push(text, klass="error"), "error-alert push")
            )
            if self._on_error is not None:
                loop.call_soon_threadsafe(self._on_error, text)

        self._error_handler = ErrorAlertHandler(alert)
        logging.getLogger().addHandler(self._error_handler)

    # -- stale data → freeze entries (§12) ----------------------------------

    # Only the continuous channels are safety-relevant: bars DERIVE from
    # trades, so a quiet bars channel with live trades is not an outage.
    _FREEZE_CHANNELS = ("trades", "quotes")

    def _on_feed_stale(self, channel: str, stale: bool) -> None:
        if channel not in self._FREEZE_CHANNELS:
            logger.warning("feed channel %s stale=%s (informational only)", channel, stale)
            return
        if stale:
            if self._market_open:
                if self.engine is not None:
                    self.engine.risk.freeze_entries("stale market data")
                logger.error(
                    "market data STALE (%s) during open market — entries frozen, "
                    "server-side stops protect open positions",
                    channel,
                )
                self._on_status("Data feed", theme.RED, f"Data feed: STALE ({channel})")
            else:
                logger.info("feed quiet (%s) while market closed — normal", channel)
        else:
            if self.engine is not None:
                self.engine.risk.unfreeze_entries("stale market data")
            self._report_feed_health()

    def _refreeze_stale_channels_at_open(self) -> None:
        """A4-8 (audit 2026-09-22): the watchdog reports fresh→stale
        transitions exactly ONCE — a channel that died while the market was
        closed fired its edge into the "feed quiet — normal" branch and never
        fired again, so the open started with a dead feed and no entry
        freeze. Called from _poll on the closed→open flip: re-read current
        staleness and apply the freeze for any safety channel still stale
        (a fresh channel that beats later lifts it via the normal edge)."""
        if self._hub is None:
            return
        try:
            stale_map = self._hub.watchdog.check()
        except Exception:
            logger.exception("open-flip staleness re-check failed")
            return
        for channel in self._FREEZE_CHANNELS:
            if stale_map.get(channel):
                logger.warning(
                    "market opened with %s channel ALREADY stale — applying the "
                    "entry freeze the closed-market edge suppressed (A4-8)",
                    channel,
                )
                self._on_feed_stale(channel, True)

    # -- positions feed (Phase 8.4: the cards observe, 1s cadence) -----------

    def _latest_price(self, symbol: str) -> float | None:
        if self._hub is None:
            return None
        quote = self._hub.latest_quotes.get(symbol)
        if quote is not None:
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid and ask:
                return (bid + ask) / 2
            if bid or ask:
                return bid or ask
        bars = self._hub.bar_builder.bars(symbol)
        if bars:
            return bars[-1].close
        return None

    def _sellable_price(self, symbol: str, side: OrderSide) -> float | None:
        """The exit-side quote (bid for longs, ask for shorts) — the 9.1
        truth, shown on the card's detail line since the big number went
        lively (2026-09-16)."""
        if self._hub is None:
            return None
        quote = self._hub.latest_quotes.get(symbol)
        if quote is None:
            return None
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        return (bid if side is OrderSide.BUY else ask) or None

    def _mark_price(self, symbol: str, side: OrderSide) -> float | None:
        """The SELLABLE truth (blueprint 9.1 — the DE +$2→−$5 lie): a long
        exits at the BID, a short covers at the ASK. Cards mark there, never
        at mid. Falls back to mid/last bar close when the book is one-sided."""
        if self._hub is not None:
            # LIVELY CARDS (2026-09-16, lively but real):
            # the big number is the freshest real market event — the last
            # PRINT, unless the BOOK has moved since (thin names print
            # seconds apart while the sellable quote keeps ticking; the
            # cards looked a beat behind the tape (14:43). Both are real numbers.
            last = self._hub.latest_trade_px.get(symbol)
            quote = self._hub.latest_quotes.get(symbol)
            if quote is not None:
                bid = float(getattr(quote, "bid_price", 0) or 0)
                ask = float(getattr(quote, "ask_price", 0) or 0)
                sellable = bid if side is OrderSide.BUY else ask
                if sellable and last:
                    trade_ts = self._hub.latest_trade_ts.get(symbol)
                    quote_ts = getattr(quote, "timestamp", None)
                    try:
                        if trade_ts is not None and quote_ts is not None and quote_ts > trade_ts:
                            return sellable
                    except TypeError:
                        pass  # mixed naive/aware timestamps — keep the print
                    return float(last)
                if sellable:
                    return sellable
            if last:
                return float(last)
        return self._latest_price(symbol)

    def _ensure_backfill(self, symbol: str) -> None:
        """Schedule a one-time REST bar backfill (dedup-guarded). Seeds the
        bar builder's history AND the session VWAP — after a restart this is
        what gives ORB its opening ranges back (2026-08-28: a 10:14 restart
        left ORB blind for the rest of the day on every unheld symbol)."""
        # Refetch-vs-double-fold invariant (audit 2026-09-22): a refetch would
        # seed_history the SAME BarBuilder twice and double-fold its session
        # VWAP. That can't happen: only _teardown clears _chart_backfill, and
        # it destroys the hub in the same pass — so any refetch always seeds
        # the reconnect's NEW, empty builder. If anything else ever clears
        # _chart_backfill without tearing the hub down, it must not.
        if symbol in self._chart_backfill or symbol in self._backfill_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (tests)
        self._backfill_pending.add(symbol)
        task = loop.create_task(self._fetch_chart_backfill(symbol))
        self._backfill_tasks.add(task)
        task.add_done_callback(self._backfill_tasks.discard)

    def _merged_candles(self, symbol: str) -> list[tuple]:
        """(ts, open, high, low, close) — REST backfill under live stream."""
        stream = self._hub.bar_builder.bars(symbol) if self._hub is not None else []
        if len(stream) < 30:
            self._ensure_backfill(symbol)
        merged: dict = {}
        for ts, o, h, low, c in self._chart_backfill.get(symbol, []):
            merged[ts] = (o, h, low, c)
        for b in stream:
            merged[b.start] = (b.open, b.high, b.low, b.close)
        return [(k, *merged[k]) for k in sorted(merged)][-120:]

    def _recent_candles(self, symbol: str) -> list[tuple]:
        """(ts, open, high, low, close) of the last 120 one-minute bars — the
        detail popup's candlestick chart (8.4 r6; ts added 2026-08-24 for
        the hover tooltip)."""
        return list(self._merged_candles(symbol))

    def _entry_candle_index(self, symbol: str, actor) -> int | None:
        """Which candle Wave BOUGHT on (2026-08-21: mark the entry
        on the detail chart). None when the fill predates the window."""
        entry_time = getattr(actor, "adopted_entry_time", None) or getattr(
            actor, "entry_filled_at", None
        )
        if entry_time is None:
            return None
        index = None
        try:
            for i, row in enumerate(self._merged_candles(symbol)):
                if row[0] <= entry_time:
                    index = i
                else:
                    break
        except TypeError:  # mixed naive/aware timestamps — no marker
            return None
        return index

    async def _fetch_chart_backfill(self, symbol: str) -> None:
        # A4-11 (audit 2026-09-22): the fetch and the judge-seeding tail get
        # SEPARATE guards. One shared except-arm used to reset
        # _chart_backfill[symbol] = [] when the TAIL threw — destroying the
        # successfully-fetched bars (empty detail chart, dead VWAP seed, and
        # the [] marker blocked any refetch for the rest of the session).
        # After a successful fetch, _chart_backfill is NEVER reset here.
        try:
            series = await self._fetch_chart_backfill_bars(symbol)
            if series:
                self._seed_judge_peaks(symbol, series)
        finally:
            # A4-1 (audit 2026-09-22): the in-flight guard must never outlive
            # the fetch. It was added in _ensure_backfill and removed NOWHERE,
            # so after a mid-day teardown (which clears _chart_backfill) every
            # previously-backfilled symbol stayed stuck in _backfill_pending
            # and _ensure_backfill no-op'd forever — no bar-history, session-
            # VWAP or judge-peak re-seed on the reconnect's empty hub. Dedup
            # of COMPLETED fetches is _chart_backfill's job (set on both
            # success and failure); pending only guards the flight itself.
            self._backfill_pending.discard(symbol)

    async def _fetch_chart_backfill_bars(self, symbol: str) -> list:
        """The fetch itself (A4-11 split): REST bars → _chart_backfill +
        bar-builder/VWAP seed. Never raises; a failure stores the [] marker
        (don't refetch-loop) and returns []. Returns the raw series so the
        judge-seeding tail can run under its OWN guard."""
        try:
            from datetime import UTC, datetime, timedelta

            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            from waveapp.broker.alpaca import KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET
            from waveapp.config import AppConfig
            from waveapp.security import secrets as _secrets

            key_id = _secrets.get_secret(KEYCHAIN_PAPER_KEY_ID)
            secret = _secrets.get_secret(KEYCHAIN_PAPER_SECRET)
            if not key_id or not secret:
                return []
            feed = DataFeed.SIP if AppConfig.load().data_feed == "sip" else DataFeed.IEX
            client = StockHistoricalDataClient(key_id, secret)
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                # full session (04:00 ET ≈ 08:00 UTC): the seeded bars rebuild
                # the session VWAP after a restart (2026-08-26 fix), so the
                # window must reach back to the session start, not 3 hours
                start=datetime.now(UTC) - timedelta(hours=12),
                feed=feed,
            )
            bars = await asyncio.wait_for(
                asyncio.to_thread(client.get_stock_bars, request), timeout=15.0
            )
            series = bars.data.get(symbol, [])
            self._chart_backfill[symbol] = [
                (b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close))
                for b in series
            ]
            # seed the LIVE bar history too (2026-08-20): without ~15 bars
            # compute_atr returns None and the exit brain runs on a bloated
            # fallback ATR — no trail ratchets for a position's first minutes
            seeded = 0
            if self._hub is not None and series:
                from waveapp.data.hub import Bar as _Bar

                seeded = self._hub.bar_builder.seed_history(
                    symbol,
                    [
                        _Bar(
                            symbol=symbol,
                            start=b.timestamp,
                            open=float(b.open),
                            high=float(b.high),
                            low=float(b.low),
                            close=float(b.close),
                            volume=float(b.volume or 0),
                            vwap_notional=float(b.vwap or b.close) * float(b.volume or 0),
                        )
                        for b in series
                    ],
                )
            logger.info("chart backfill: %s (%d bars, %d seeded)", symbol, len(series), seeded)
            return list(series)
        except Exception:
            logger.exception("chart backfill failed for %s", symbol)
            self._chart_backfill[symbol] = []  # don't refetch-loop on failure
            return []

    def _seed_judge_peaks(self, symbol: str, series: list) -> None:
        """JUDGE ADOPTION (2026-09-22): an adopted position's judge
        must know the high since the ORIGINAL entry, or a restart blinds the
        give-back protection. The backfill bars carry it. A4-11: per-actor
        guard — one bad actor/timestamp must not kill the other seeds, and
        NOTHING here may touch _chart_backfill (the bars are already safely
        stored by the fetch)."""
        if self.engine is None:
            return
        for actor in self.engine.actors.values():
            try:
                opened = getattr(actor, "adopted_entry_time", None)
                if actor.spec.symbol != symbol or opened is None:
                    continue
                since = [b for b in series if b.timestamp >= opened]
                # getattr: adopted-shim actors may lack .side — anything
                # that isn't explicitly SELL takes the long peak path
                if since and getattr(actor.spec, "side", None) is OrderSide.SELL:
                    # S3 (the S2 handoff): a SELL actor's "peak" is the LOW
                    # since entry — seed the trough attrs the kitchen's
                    # side-aware ratchet already reads. Longs keep the
                    # existing judge_seed_peak path untouched below.
                    bot = min(since, key=lambda b: float(b.low))
                    actor.judge_seed_trough = float(bot.low)
                    actor.judge_seed_trough_t_ms = int(bot.timestamp.timestamp() * 1000)
                    actor.judge_seed_trough_vol = float(bot.volume or 0)
                    logger.info(
                        "judge adoption: %s trough since entry seeded @ %.2f (%s)",
                        symbol,
                        actor.judge_seed_trough,
                        bot.timestamp.strftime("%H:%M"),
                    )
                elif since:
                    top = max(since, key=lambda b: float(b.high))
                    actor.judge_seed_peak = float(top.high)
                    # the peak's TIME and VOLUME travel with it (close
                    # bug 2026-09-22: price alone aged the peak from the
                    # adoption moment and left no volume evidence, so
                    # the death detectors were blind all afternoon)
                    actor.judge_seed_peak_t_ms = int(top.timestamp.timestamp() * 1000)
                    actor.judge_seed_peak_vol = float(top.volume or 0)
                    logger.info(
                        "judge adoption: %s peak since entry seeded @ %.2f (%s)",
                        symbol,
                        actor.judge_seed_peak,
                        top.timestamp.strftime("%H:%M"),
                    )
            except Exception:
                logger.exception("judge-peak seed failed for %s (backfill bars kept)", symbol)

    def _position_snapshots(self) -> list[dict]:
        from datetime import UTC, datetime

        from waveapp.broker.base import OrderSide
        from waveapp.engine.actor import TERMINAL_STATES, PositionState
        from waveapp.engine.session import Regime, SessionScheduler

        if self.engine is None:
            return []
        today = datetime.now(UTC).date()
        try:
            market_open = SessionScheduler.regime() is not Regime.CLOSED
        except Exception:
            market_open = True
        snaps: list[dict] = []
        for actor in self.engine.actors.values():
            if actor.state in TERMINAL_STATES or actor.state is PositionState.PENDING_ENTRY:
                continue
            symbol = actor.spec.symbol
            entry = actor.avg_entry_price or actor.spec.limit_price or 0.0
            brain = actor.exit_engine
            stop = brain.current_stop if brain is not None else actor.spec.stop_price
            if brain is not None and brain.scaled_out:
                stage = "SCALED"
            elif brain is not None and brain.breakeven_done:
                stage = "BE"
            elif brain is not None and brain.current_stop != actor.spec.stop_price:
                stage = "TRAIL"
            else:
                stage = ""
            # after a scale-out the card shows the REMAINING shares
            if brain is not None:
                qty = brain.remaining_qty
            else:
                qty = actor.filled_qty or actor.spec.qty
            # first profit target (k_t1 × ATR) until it's been taken.
            # k_t1 ≥ 900 is the champion's "scale-out disabled" sentinel — a
            # 999×ATR line blew the chart's y-scale so far the candles
            # flattened into a hairline (screenshot, 2026-08-19)
            target = None
            if brain is not None and not brain.scaled_out and brain.params.k_t1 < 900:
                target = entry + brain.direction * brain.params.k_t1 * brain.atr
            if target is None and brain is not None:
                # goal ladder (2026-08-21): the card's goal CLIMBS — the next
                # uncrossed rung (None while the ladder is disabled)
                target = getattr(brain, "next_goal", None)
            snaps.append(
                {
                    "key": actor.position_key,
                    "symbol": symbol,
                    "side": "long" if actor.spec.side is OrderSide.BUY else "short",
                    "qty": qty,
                    "entry": entry,
                    "last": self._mark_price(symbol, actor.spec.side) or entry,
                    "sellable": self._sellable_price(symbol, actor.spec.side),
                    # the Position Judge's live stance (2026-09-20:
                    # "the card shows the stance — you SEE it thinking")
                    "stance": (getattr(self._shadow_kitchen, "judge_stances", {}) or {}).get(
                        actor.position_key, (None, None)
                    )[0]
                    if self._shadow_kitchen is not None
                    else None,
                    "stop": stop,
                    "stage": stage,
                    "strategy": actor.spec.strategy,
                    "manager": getattr(actor.spec, "manager", "kitchen"),  # DUEL [K]/[J]
                    "halted": actor.state is PositionState.HALTED,
                    "ssr": self.engine.risk.ssr_active(symbol, today),
                    "name": self._asset_names.get(symbol, ""),
                    "bars": self._recent_candles(symbol),
                    "entry_index": self._entry_candle_index(symbol, actor),
                    "market_open": market_open,
                    "target": target,
                    # M2 advisory: entry-time Brain read (None = not scored)
                    "brain": getattr(self, "_brain_entry_scores", {}).get(symbol),
                }
            )
        return snaps

    def _advise_position_events(self) -> None:
        """Yuval-brain moments on open positions (item 13): the watcher
        thresholds — GIVEBACK (peak >= $150, >= 40% draining), BLEEDER
        (<= -$150 and falling), RUNNER gone quiet (+2%/15min). Per-(symbol,
        kind) step-dedupe so a moment fires once and refires as it worsens."""
        if getattr(self, "_yuval_brain", None) is None or self.engine is None:
            return
        from waveapp.engine.actor import TERMINAL_STATES as _TS
        from waveapp.engine.yuval_brain import AdvisorMoment

        rings = getattr(self, "_advise_rings", None)
        if rings is None:
            rings = self._advise_rings = {}
        fired = getattr(self, "_advise_fired", None)
        if fired is None:
            fired = self._advise_fired = {}
        live = set()
        for a in self.engine.actors.values():
            if a.state in _TS or not a.avg_entry_price or not a.filled_qty:
                continue
            symbol = a.spec.symbol
            live.add(symbol)
            bars = self._last_bars_for(symbol)
            if not bars:
                continue
            px = bars[-1].c
            st = rings.setdefault(symbol, {"peak": px, "ring": []})
            st["peak"] = max(st["peak"], px)
            st["ring"].append(px)
            if len(st["ring"]) > 900:
                del st["ring"][0]
            qty, entry = a.filled_qty, a.avg_entry_price
            open_usd = (px - entry) * qty
            peak_usd = (st["peak"] - entry) * qty
            kind = None
            step = 0
            if peak_usd >= 150 and (peak_usd - open_usd) >= max(120.0, 0.4 * peak_usd):
                kind, step = "giveback", int((peak_usd - open_usd) // 100)
            elif open_usd <= -150 and len(st["ring"]) >= 3 and px <= min(st["ring"][-3:]):
                kind, step = "bleeder", int(-open_usd // 100)
            elif len(st["ring"]) >= 4 and st["ring"][0] and (px / st["ring"][0] - 1) >= 0.02:
                kind, step = "runner_quiet", 1
            if kind is None:
                continue
            key = f"{symbol}:{kind}"
            if step <= fired.get(key, 0):
                continue
            fired[key] = step
            minutes = 0.0
            if a.entry_filled_at is not None:
                from datetime import UTC as _u
                from datetime import datetime as _d

                minutes = (_d.now(_u) - a.entry_filled_at).total_seconds() / 60.0
            self._advise(
                AdvisorMoment(
                    kind=kind,
                    symbol=symbol,
                    current_price=px,
                    bars=bars,
                    entry_price=entry,
                    peak_price=st["peak"],
                    qty=qty,
                    minutes_held=minutes,
                    day_open=bars[0].o if bars else None,
                    strategy=str(a.spec.strategy),
                    position_uuid=a.uuid,
                    **self._moment_context(symbol),
                )
            )
        for symbol in list(rings):
            if symbol not in live:
                del rings[symbol]
                for k in list(fired):
                    if k.startswith(f"{symbol}:"):
                        del fired[k]

    async def _positions_loop(self) -> None:
        """1s full snapshots + a light mark tick between them. Was 250ms
        marks (4 updates/s); 2026-09-18: "a tiny bit too fast — slow
        it down just a bit" → one mark tick at the half-second (2 updates/s
        with the snapshot). The light tick sends only {key: mark}; the full
        snapshot (bars, stops, stages) stays at 1s."""
        tick = 0
        while True:
            await asyncio.sleep(0.25)
            tick += 1
            if tick % 4 in (1, 3):
                continue  # rest beats — the card breathes at 500ms now
            if tick % 4 == 2:  # the light mark tick at the half-second
                if self._on_marks is not None and self.engine is not None:
                    try:
                        marks = self._position_marks()
                        if marks:
                            self._on_marks(marks)
                    except Exception:
                        logger.exception("marks tick failed")
                continue
            try:
                self._track_closed_trades()
            except Exception:
                logger.exception("scoreboard tracking failed")
            try:
                self._advise_position_events()
            except Exception:
                logger.exception("yuval-brain position events failed")
            try:
                await self._ensure_position_symbols_watched()
            except Exception:
                logger.exception("position watch check failed")
            if self._on_positions is None:
                continue
            try:
                self._on_positions(self._position_snapshots())
            except Exception:
                logger.exception("positions snapshot failed")

    def _position_marks(self) -> dict[str, float]:
        """Cheap {position_key: sellable mark} for the 250ms card refresh."""
        marks: dict[str, float] = {}
        if self.engine is None:
            return marks
        for actor in self.engine.actors.values():
            if actor.state.value not in ("open", "scaling_out", "closing", "halted"):
                continue
            mark = self._mark_price(actor.spec.symbol, actor.spec.side)
            if mark:
                marks[actor.position_key] = float(mark)
        return marks

    async def _ensure_position_symbols_watched(self) -> None:
        """Adopted positions (restart survivors) may hold symbols the stream
        isn't watching — without bars their exit brains never seed. Subscribe
        any open-actor symbol the hub doesn't know yet (2026-08-19)."""
        if self.engine is None or self._hub is None:
            return
        open_symbols = {
            a.spec.symbol
            for a in self.engine.actors.values()
            if a.state.value in ("open", "scaling_out", "closing", "halted")
        }
        missing = sorted(open_symbols - self._hub.watched)
        if missing:
            await self._hub.watch(missing)

    # -- session scoreboard (2026-08-19) ------------------------------------

    def _track_closed_trades(self) -> None:
        """Detect actors reaching CLOSED and record the round trip; pushes
        the scoreboard immediately after every closed trade."""
        import time as _time

        if self.engine is None:
            return
        changed = False
        for key, actor in list(self.engine.actors.items()):
            state = actor.state.value
            if state in ("open", "scaling_out") and key not in self._actor_opened_at:
                self._actor_opened_at[key] = _time.time()
                # M0: first sight of the FILLED entry → entry_lag telemetry
                # (observation only; adopted positions write nothing)
                self._record_entry_lag(actor)
                # R1.5: the efficacy guard starts this entry's 10-min clock
                self._record_efficacy_entry(key, actor)
            elif state == "closed" and key not in self._closed_seen:
                self._closed_seen.add(key)
                # R1.5: a close inside the window closes out any unscored
                # entry as FAIL (cut/stopped before proving itself); past
                # the window it's LATE. Already-scored keys are untouched.
                with contextlib.suppress(Exception):
                    self._efficacy.record_close(key, int(_time.time() * 1000))
                opened = self._actor_opened_at.get(key, _time.time())
                exit_reason = str(getattr(actor, "exit_reason", "") or "")
                # DEMAND 20 (2026-09-16, DLLL −$398 then re-bought in 3
                # min): a brain WRONG-cut is a stronger rejection than a
                # stop-out — the name flipped against us hard enough for
                # the wrong-way logic to fire. Both now arm the 15-min
                # symbol lockout. Tightening-only (hard rule 2 intact).
                # "failsafe" (DEMAND 21) is the same class of rejection —
                # a stretched entry that bled — audit 2026-09-16: it must
                # arm the lockout too or the name gets re-chased in minutes.
                if (
                    "stop hit" in exit_reason
                    or "MK wrong" in exit_reason
                    or "failsafe" in exit_reason
                ):
                    self._symbol_stop_hit_at[actor.spec.symbol] = _time.time()
                entry_price = actor.avg_entry_price or actor.spec.stop_price
                self._record_symbol_costs(actor)  # 6.4 per-symbol cost ledger
                self._trade_records.append(
                    {
                        "strategy": actor.spec.strategy,
                        "symbol": actor.spec.symbol,
                        "entry_price": float(entry_price),
                        "pnl": float(actor.realized_pnl or 0.0),
                        "hold_seconds": max(0.0, _time.time() - opened),
                    }
                )
                changed = True
        if changed and self._on_scoreboard is not None:
            self._on_scoreboard(self.scoreboard())

    def _record_symbol_costs(self, actor) -> None:
        """6.4 (blueprint) — the §7 per-symbol cost ledger finally gets fed:
        submit-time quoted spread + realized entry slippage vs the decision
        price, one row per closed round trip. This is the DATA the future
        effective-spread gate needs (§11 decides if it ever replaces the
        quoted spread; paper fills flatter everything — rule 10 noted in
        every consumer)."""
        if self._database is None:
            return
        try:
            from datetime import UTC as _utc
            from datetime import datetime as _dt

            from waveapp.engine.session import ET as _et
            from waveapp.engine.session import SessionScheduler

            symbol = actor.spec.symbol
            quote = getattr(self, "_entry_quotes", {}).pop(symbol, None)
            if quote is None:
                return
            bid, ask, decision = quote
            fill = float(actor.avg_entry_price or 0.0)
            if fill <= 0 or decision <= 0:
                return
            spread = max(ask - bid, 0.0) if (bid > 0 and ask > bid) else 0.0
            direction = 1.0 if actor.spec.side is OrderSide.BUY else -1.0
            slippage = (fill - decision) * direction  # + = paid worse than decided
            self._database.execute(
                "INSERT OR REPLACE INTO symbol_costs (symbol, session_date, regime,"
                " measured_spread, spread_pct, realized_slippage, fill_quality,"
                " samples) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    symbol,
                    _dt.now(_utc).astimezone(_et).date().isoformat(),
                    SessionScheduler.regime().value,
                    round(spread, 4),
                    round(spread / fill * 100.0, 4),
                    round(slippage, 4),
                    round(slippage / max(spread, 0.01), 3),
                ),
            )
        except Exception:
            logger.exception("symbol_costs record failed")

    # -- M0 telemetry (ML master plan, 2026-09-23) — observation only --------

    def _ml_journal_write(self, op: str, payload: dict):
        """The shadow kitchen's DB pen (it holds no Database by design):
        insert a judge_transitions row ('transition' → returns the row id)
        or fill its forward outcomes ('px30'/'px2m'/'px5m'). Runs on the
        event loop like every other monitor write — Database serializes
        internally. Failures return None and never wound the caller."""
        db = self._database
        if db is None:
            return None
        try:
            if op == "transition":
                cur = db.execute(
                    "INSERT INTO judge_transitions (ts, symbol, position_key, side,"
                    " from_stance, to_stance, reason, profit_atr, peak_profit_atr,"
                    " giveback_atr, signs, failed_highs, peak_age_s, vol_ratio, atr_px,"
                    " px, entry_px, close_guard, dwell_ms, regime) VALUES (:ts, :symbol,"
                    " :position_key, :side, :from_stance, :to_stance, :reason,"
                    " :profit_atr, :peak_profit_atr, :giveback_atr, :signs,"
                    " :failed_highs, :peak_age_s, :vol_ratio, :atr_px, :px, :entry_px,"
                    " :close_guard, :dwell_ms, :regime)",
                    payload,
                )
                return cur.lastrowid
            if op == "px30":
                db.execute("UPDATE judge_transitions SET px_30s = :v WHERE id = :id", payload)
            elif op == "px2m":
                db.execute(
                    "UPDATE judge_transitions SET px_2m = :v, mfe_2m_atr = :mfe,"
                    " mae_2m_atr = :mae WHERE id = :id",
                    payload,
                )
            elif op == "px5m":
                db.execute("UPDATE judge_transitions SET px_5m = :v WHERE id = :id", payload)
            return None
        except Exception:
            logger.debug("ml journal write failed (%s)", op, exc_info=True)
            return None

    def _scanner_ssr_check(self, symbol: str) -> bool | None:
        """M0: the candidates journal's SSR probe — RiskEngine's live Rule-201
        state when the engine exists, None (unknown) before it does."""
        try:
            if self.engine is None:
                return None
            from datetime import UTC as _utc
            from datetime import datetime as _dt

            return bool(self.engine.risk.ssr_active(symbol, _dt.now(_utc).date()))
        except Exception:
            return None

    def _note_first_watch(self, symbols) -> None:
        """Stamp the FIRST moment each symbol reached the live watch today
        (focus promotion, event promotion, climber lane) — entry_lag's
        detection anchor. Day-keyed; later sightings never overwrite."""
        try:
            from waveapp.persistence.db import utc_now

            ts = utc_now()
            day = ts[:10]
            if self._first_watch_day != day:
                self._first_watch_day = day
                self._first_watch_at = {}
            for sym in symbols:
                self._first_watch_at.setdefault(str(sym), ts)
        except Exception:
            logger.debug("first-watch stamp failed", exc_info=True)

    def _record_entry_lag(self, actor) -> None:
        """One entry_lag row per FILLED pipeline entry: detection→signal→fill
        latency + how far past the session open the fill paid. Adopted
        (restart) positions and fills whose signal stamp is stale (>30 min —
        an abandoned earlier entry) write no row. Observation only."""
        if self._database is None:
            return
        try:
            from datetime import datetime as _dt

            from waveapp.persistence.db import utc_now

            symbol = actor.spec.symbol
            ts_signal = self._entry_signal_at.pop(symbol, None)
            if ts_signal is None:
                return  # no live signal stamp — an adoption, not a pipeline entry
            filled_at = getattr(actor, "entry_filled_at", None)
            ts_fill = filled_at.isoformat(timespec="milliseconds") if filled_at else utc_now()

            def _secs(a: str | None, b: str | None) -> float | None:
                try:
                    if not a or not b:
                        return None
                    return round((_dt.fromisoformat(b) - _dt.fromisoformat(a)).total_seconds(), 3)
                except Exception:
                    return None

            sig_to_fill = _secs(ts_signal, ts_fill)
            if sig_to_fill is not None and (sig_to_fill > 1800.0 or sig_to_fill < 0):
                return  # stale stamp from an entry that never filled — not this fill's
            ts_watch = self._first_watch_at.get(symbol)
            pct_above_open = None
            try:
                fill_px = float(actor.avg_entry_price or 0.0)
                day_open = self._day_open_for(symbol, self._last_bars_for(symbol))
                if day_open and fill_px > 0:
                    pct_above_open = round((fill_px / day_open - 1.0) * 100.0, 4)
            except Exception:
                pct_above_open = None
            self._database.execute(
                "INSERT INTO entry_lag (symbol, side, strategy, ts_first_watch, ts_signal,"
                " ts_fill, pct_above_open_at_fill, lag_watch_to_signal_s,"
                " lag_signal_to_fill_s) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    "long" if actor.spec.side is OrderSide.BUY else "short",
                    str(getattr(actor.spec, "strategy", "") or "") or None,
                    ts_watch,
                    ts_signal,
                    ts_fill,
                    pct_above_open,
                    _secs(ts_watch, ts_signal),
                    sig_to_fill,
                ),
            )
        except Exception:
            logger.debug("entry lag record failed", exc_info=True)

    def _ml_daily_rollup(self, day: str) -> None:
        """Nightly M0 rollup: one ml_daily row + one TRAINING log line per
        session day, read from the new observation tables. Failure-safe."""
        db = self._database
        if db is None:
            return
        try:
            like = day + "%"
            flips = db.query(
                "SELECT dwell_ms FROM judge_transitions WHERE ts LIKE ?"
                " AND to_stance NOT LIKE 'ACT:%'",
                (like,),
            )
            acts = db.query(
                "SELECT COUNT(*) AS n FROM judge_transitions WHERE ts LIKE ?"
                " AND to_stance LIKE 'ACT:%'",
                (like,),
            )[0]["n"]
            entries = db.query(
                "SELECT lag_watch_to_signal_s FROM entry_lag WHERE ts_fill LIKE ?", (like,)
            )
            shorts = db.query(
                "SELECT COUNT(*) AS n FROM candidates WHERE session_date = ?"
                " AND json_extract(features, '$.side') = 'short'",
                (day,),
            )[0]["n"]

            def _median(values):
                values = sorted(v for v in values if v is not None)
                return values[len(values) // 2] if values else None

            med_dwell = _median([r["dwell_ms"] for r in flips])
            med_watch = _median([r["lag_watch_to_signal_s"] for r in entries])
            db.execute(
                "INSERT OR REPLACE INTO ml_daily (date, n_transitions, n_acts,"
                " median_dwell_ms, n_entries, median_watch_to_signal_s,"
                " n_short_candidates) VALUES (?,?,?,?,?,?,?)",
                (day, len(flips), int(acts), med_dwell, len(entries), med_watch, int(shorts)),
            )
            db.write_log(
                "TRAINING",
                "INFO",
                f"M0 rollup {day}: {len(flips)} flips, {acts} acts,"
                f" median dwell {med_dwell} ms, {len(entries)} entries,"
                f" median watch→signal {med_watch} s, {shorts} short candidates",
            )
        except Exception:
            logger.exception("ml daily rollup failed")

    @staticmethod
    def _bucket(entry_price: float) -> str:
        if entry_price < 10:
            return "<$10"
        if entry_price < 15:
            return "$10-15"
        return "$15+"

    @staticmethod
    def _performance_epoch() -> str:
        """Last performance-reset stamp; blank = show everything."""
        try:
            from waveapp.config import AppConfig

            return AppConfig.load().performance_epoch
        except Exception:
            logger.exception("performance epoch read failed — showing all history")
            return ""

    def _scoreboard_records(self) -> list[dict]:
        """Closed round trips for the scoreboard. Sourced from the DB so a
        restart keeps history (2026-08-19); honors the performance
        reset epoch and per-trade removals so both views always agree.
        Falls back to the in-memory records when no DB is attached."""
        if self._database is None:
            return self._trade_records
        from datetime import datetime

        records: list[dict] = []
        try:
            rows = self._database.query(
                "SELECT strategy, symbol, avg_entry, realized_pnl, opened_at, closed_at"
                " FROM positions WHERE state='closed' AND realized_pnl IS NOT NULL"
                " AND closed_at IS NOT NULL AND closed_at >= ?"
                " AND COALESCE(perf_hidden, 0) = 0 ORDER BY closed_at",
                (self._performance_epoch(),),
            )
        except Exception:
            logger.exception("scoreboard query failed")
            return self._trade_records
        for row in rows:
            hold = 0.0
            try:
                opened = datetime.fromisoformat(str(row["opened_at"]))
                closed = datetime.fromisoformat(str(row["closed_at"]))
                hold = max(0.0, (closed - opened).total_seconds())
            except (ValueError, TypeError):
                pass
            records.append(
                {
                    "strategy": row["strategy"] or "?",
                    "symbol": row["symbol"],
                    "entry_price": float(row["avg_entry"] or 0.0),
                    "pnl": float(row["realized_pnl"]),
                    "hold_seconds": hold,
                }
            )
        return records

    def scoreboard(self) -> dict:
        """Aggregates per strategy and per price-floor bucket — the live
        answer to 'which floor is leading' (2026-08-19)."""

        def _agg(records: list[dict]) -> dict:
            if not records:
                return {"trades": 0}
            pnls = [r["pnl"] for r in records]
            holds = [r["hold_seconds"] for r in records]
            wins = [p for p in pnls if p > 0]
            return {
                "trades": len(records),
                "wins": len(wins),
                "win_rate": len(wins) / len(records),
                "net_pnl": sum(pnls),
                "avg_pnl": sum(pnls) / len(records),
                "best": max(pnls),
                "worst": min(pnls),
                "avg_hold_s": sum(holds) / len(holds),
                "max_hold_s": max(holds),
                "min_hold_s": min(holds),
            }

        records = self._scoreboard_records()
        by_strategy: dict[str, list] = {}
        by_bucket: dict[str, list] = {}
        for record in records:
            by_strategy.setdefault(record["strategy"], []).append(record)
            by_bucket.setdefault(self._bucket(record["entry_price"]), []).append(record)
        return {
            "overall": _agg(records),
            "by_strategy": {k: _agg(v) for k, v in sorted(by_strategy.items())},
            "by_bucket": {k: _agg(v) for k, v in sorted(by_bucket.items())},
        }

    def _performance_data(self, current_equity: float) -> dict:
        """Closed trades + cashflows for the Performance tab (§5). The curve
        steps only on CLOSED trades — buys never move it (8.6)."""
        from datetime import datetime

        trades: list[dict] = []
        cashflows: list[dict] = []
        # reset epoch (2026-08-19): hide — never delete — anything
        # recorded before the last performance reset, so false/buggy rows
        # can't corrupt the curve while the audit trail stays whole
        epoch = self._performance_epoch()
        if self._database is not None:
            rows = self._database.query(
                "SELECT position_uuid, closed_at, realized_pnl, symbol FROM positions"
                " WHERE state='closed' AND realized_pnl IS NOT NULL"
                " AND closed_at IS NOT NULL AND closed_at >= ?"
                " AND COALESCE(perf_hidden, 0) = 0 ORDER BY closed_at",
                (epoch,),
            )
            for row in rows:
                try:
                    stamp = datetime.fromisoformat(str(row["closed_at"])).timestamp()
                except ValueError:
                    continue
                trades.append(
                    {
                        "ts": stamp,
                        "pnl": float(row["realized_pnl"]),
                        "symbol": row["symbol"],
                        "uuid": row["position_uuid"],
                    }
                )
            for row in self._database.query(
                "SELECT ts, amount FROM cashflows WHERE ts >= ? ORDER BY ts", (epoch,)
            ):
                try:
                    stamp = datetime.fromisoformat(str(row["ts"])).timestamp()
                except ValueError:
                    continue
                cashflows.append({"ts": stamp, "amount": float(row["amount"])})
        return {
            "current_equity": current_equity,
            "trades": trades,
            "cashflows": cashflows,
            "fees": 0.0,  # measured from live fills starting Phase 11
            "champion": "v0",  # heuristic baseline — Phase 10 promotes via §11
            "readiness": self._live_readiness(),
        }

    def _live_readiness(self) -> dict:
        """Road to Live (weekend agenda #2, 2026-08-22): the §11.2 evidence
        bar measured from the actual books, plus the live-plumbing checklist.
        Paper P&L is never the FINAL evidence (hard rule 10) — this page
        tracks progress; the gate itself stays Phase 11 + Touch ID + owner approval."""
        pnls: list[float] = []
        sessions = 0
        incidents = 0
        if self._database is not None:
            try:
                rows = self._database.query(
                    "SELECT realized_pnl, substr(closed_at, 1, 10) AS day FROM positions"
                    " WHERE state='closed' AND realized_pnl IS NOT NULL"
                    " AND closed_at IS NOT NULL AND COALESCE(perf_hidden, 0) = 0"
                    " ORDER BY closed_at"
                )
                pnls = [float(r["realized_pnl"]) for r in rows]
                sessions = len({r["day"] for r in rows})
                from datetime import UTC, datetime, timedelta

                floor_ts = (datetime.now(UTC) - timedelta(days=14)).isoformat()
                bad = self._database.query(
                    "SELECT COUNT(*) AS n FROM log WHERE level='ERROR' AND ts >= ? AND ("
                    " message LIKE '%mismatch%' OR message LIKE '%unreconciled%'"
                    " OR message LIKE '%BOOKKEEPING%' OR message LIKE '%reconcil%')",
                    (floor_ts,),
                )
                incidents = int(bad[0]["n"]) if bad else 0
            except Exception:
                logger.exception("readiness query failed")
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        profit_factor = (
            (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
        )
        expectancy_r = (
            (sum(pnls) / len(pnls)) / abs(sum(losses) / len(losses)) if losses and pnls else 0.0
        )
        peak = equity = drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        base_equity = (self._last_equity - sum(pnls)) or 100_000.0
        drawdown_pct = drawdown / base_equity * 100.0
        pf_text = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
        checks = [
            {
                "key": "sessions",
                "label": "Sessions with closed trades (≥30)",
                "ok": sessions >= 30,
                "progress": sessions / 30.0,
                "text": f"{sessions} / 30",
            },
            {
                "key": "trades",
                "label": "Closed trades (≥200)",
                "ok": len(pnls) >= 200,
                "progress": len(pnls) / 200.0,
                "text": f"{len(pnls)} / 200",
            },
            {
                "key": "pf",
                "label": "Profit factor (≥1.3)",
                "ok": bool(pnls) and profit_factor >= 1.3,
                "progress": min(1.0, profit_factor / 1.3) if pnls else 0.0,
                "text": pf_text,
            },
            {
                "key": "expectancy",
                "label": "Expectancy (≥+0.10R)",
                "ok": bool(pnls) and expectancy_r >= 0.1,
                "progress": max(0.0, expectancy_r / 0.1) if pnls else 0.0,
                "text": f"{expectancy_r:+.2f}R",
            },
            {
                "key": "drawdown",
                "label": "Max drawdown (<10% of equity)",
                "ok": bool(pnls) and drawdown_pct < 10.0,
                "progress": max(0.0, 1.0 - drawdown_pct / 10.0) if pnls else 0.0,
                "text": f"{drawdown_pct:.1f}%",
            },
            {
                "key": "incidents",
                "label": "Unreconciled incidents, last 14 days (0)",
                "ok": incidents == 0,
                "progress": 1.0 if incidents == 0 else 0.0,
                "text": str(incidents),
            },
        ]
        from waveapp.broker.live_reader import LiveAccountReader

        try:
            live_keys = LiveAccountReader.has_keys()
        except Exception:
            live_keys = False
        touch_id = True
        try:
            from waveapp.config import AppConfig as _Cfg

            touch_id = _Cfg.load().touch_id_enabled
        except Exception:
            logger.debug("config load failed for plumbing check", exc_info=True)
        plumbing = [
            {"ok": live_keys, "label": "Live API keys in Keychain (test them in Settings)"},
            {"ok": touch_id, "label": "Touch ID gate for the paper→live switch"},
            {"ok": True, "label": "Paper/live separated at the type level (wave_live.db ready)"},
            {"ok": True, "label": "Kill switch + weekly-loss halt + re-arm wired"},
            {"ok": True, "label": "Server-side stop on every position from entry"},
        ]
        return {"checks": checks, "plumbing": plumbing}

    def hide_performance_trade(self, position_uuid: str) -> None:
        """Remove ONE trade from the Performance graph (2026-08-19).
        Sets a flag — the row itself stays in the DB (§10.3 dataset)."""
        if self._database is None or not position_uuid:
            return
        try:
            self._database.execute(
                "UPDATE positions SET perf_hidden=1 WHERE position_uuid=?", (position_uuid,)
            )
            logger.info("performance: trade %s hidden from the graph", position_uuid[:12])
        except Exception:
            logger.exception("hide trade failed")
            return
        if self._on_performance is not None:
            with contextlib.suppress(Exception):
                self._on_performance(self._performance_data(self._last_equity))
        if self._on_scoreboard is not None:  # both views must agree
            with contextlib.suppress(Exception):
                self._on_scoreboard(self.scoreboard())

    def _llm_stats(self) -> dict:
        """Layer-7 ops line for the System tab — spend + BALANCE visibility
        (7.7 + 2026-09-02). Anthropic has no balance API for regular
        keys, so the balance is Wave's own ledger: funded budget − every
        measured token cost, persisted to config so restarts keep counting."""
        if self._llm is None:
            return {"enabled": False}
        from waveapp.engine.llmintel import DAILY_SPEND_CAP

        total = self._llm_baseline + self._llm.spend_session
        if total - self._llm_saved_total >= 0.002:  # throttled persistence
            try:
                from waveapp.config import AppConfig

                config = AppConfig.load()
                config.llm_spent_total = round(total, 4)
                config.save()
                self._llm_saved_total = total
                self._llm_budget = config.llm_budget
            except Exception:
                logger.debug("llm spend persistence failed", exc_info=True)
        return {
            "enabled": True,
            "calls_today": self._llm.calls_today,
            "spend_today": round(self._llm.spend_today, 4),
            "cap": DAILY_SPEND_CAP,
            "budget": self._llm_budget,
            "balance": round(max(self._llm_budget - total, 0.0), 4),
        }

    def _system_data(self) -> dict:
        """The System tab's snapshot (§5 tab 4)."""
        from waveapp import __version__

        feed_ages: dict[str, float | None] = {}
        if self._hub is not None:
            for channel in ("trades", "quotes", "bars"):
                feed_ages[channel] = self._hub.watchdog.age(channel)
        db: dict = {}
        if self._database is not None:
            try:
                db = {
                    "name": self._database.path.name,
                    "schema": self._database.schema_version(),
                    "size_mb": self._database.path.stat().st_size / 1_048_576,
                }
            except Exception:
                logger.exception("db stats failed")
        fees: list[dict] = []
        if self._database is not None:
            try:
                fees = [
                    dict(row)
                    for row in self._database.query(
                        "SELECT fee_name, effective_date, rate, unit, cap"
                        " FROM fee_schedule ORDER BY fee_name, effective_date DESC"
                    )
                ]
            except Exception:
                logger.exception("fee schedule read failed")
        import time as _time

        risk: dict = {}
        if self.engine is not None:
            # halt_state is a PROPERTY — calling it was the r3 bug that
            # silently blanked the whole System tab every poll
            try:
                engine_risk = self.engine.risk
                halt = engine_risk.halt_state
                risk = {
                    "halt": halt.value if hasattr(halt, "value") else str(halt),
                    "freezes": sorted(engine_risk._freeze_reasons),
                    "limits": {
                        "risk_per_trade_pct": engine_risk.limits.risk_per_trade_pct,
                        "max_daily_loss_pct": engine_risk.limits.max_daily_loss_pct,
                        "max_weekly_loss_pct": engine_risk.limits.max_weekly_loss_pct,
                        "max_positions": engine_risk.limits.max_positions,
                        "max_notional_pct": engine_risk.limits.max_notional_pct,
                        "impact_participation_pct": engine_risk.limits.impact_participation_pct,
                        # S4: the System card says when the short book is armed
                        "shorts_enabled": bool(engine_risk.limits.shorts_enabled),
                        "short_risk_share": engine_risk.limits.short_risk_share,
                    },
                }
            except Exception:
                logger.exception("risk snapshot failed")
        else:
            # engine idle: show the CONFIGURED limits, never a stale default
            # (2026-08-20: max_positions raised 3→10 and the System tab
            # kept the old number until Start)
            try:
                from waveapp.config import AppConfig as _Cfg

                config_now = _Cfg.load()
                risk = {
                    "halt": "idle",
                    "freezes": [],
                    "limits": {
                        "risk_per_trade_pct": config_now.risk_per_trade_pct,
                        "max_daily_loss_pct": config_now.max_daily_loss_pct,
                        "max_weekly_loss_pct": config_now.max_weekly_loss_pct,
                        "max_positions": config_now.max_positions,
                        "max_notional_pct": config_now.max_notional_pct,
                        "impact_participation_pct": config_now.impact_participation_pct,
                        # S4: config truth while idle (RiskLimits default share)
                        "shorts_enabled": bool(getattr(config_now, "shorts_enabled", False)),
                        "short_risk_share": 0.5,
                    },
                }
            except Exception:
                logger.exception("idle risk snapshot failed")
        auto_trade = False
        configured_feed = "iex"
        min_price_now = 15.0
        try:
            from waveapp.config import AppConfig

            config = AppConfig.load()
            auto_trade = config.auto_trade
            configured_feed = config.data_feed
            min_price_now = config.min_entry_price
        except Exception:
            logger.debug("config reload failed for system snapshot")
        market_data = {
            "configured_feed": configured_feed,
            "feed": self._hub.feed if self._hub is not None else None,
            "stream_live": bool(self._hub is not None and self._hub.is_running),
            "watched": len(self._hub.watched) if self._hub is not None else 0,
            "budget": self._hub.subscription_limit if self._hub is not None else None,
            "polygon": self._polygon_status,
        }
        errors_today = None
        if self._database is not None:
            try:
                from datetime import UTC as _utc
                from datetime import datetime as _dt

                today = _dt.now(_utc).date().isoformat()
                rows = self._database.query(
                    "SELECT COUNT(*) AS n FROM log WHERE level='ERROR' AND ts >= ?",
                    (today,),
                )
                errors_today = int(rows[0]["n"]) if rows else 0
            except Exception:
                logger.debug("error count failed", exc_info=True)
        return {
            "engine_state": self.engine.state.value if self.engine is not None else "offline",
            "open_positions": self.engine.open_actor_count if self.engine is not None else None,
            "feed_ages": feed_ages,
            "db": db,
            "fees": fees,
            "version": __version__,
            "uptime_seconds": _time.monotonic() - self._started_monotonic,
            "errors_today": errors_today,
            "risk": {
                **risk,
                # the ADOPTED behavior gates (2026-09-02: the card must
                # tell the whole truth, not only the §12 limits)
                "gates": {
                    "min_entry_price": min_price_now,
                    "cutoff_minutes": ENTRY_CUTOFF_MINUTES,
                    "trend_gate": True,
                    "one_per_symbol_day": True,
                },
            },
            "market_data": market_data,
            "market_regime": dict(self._scanner2.market_regime)
            if self._scanner2 is not None
            else {},
            "day_regime": self._day_judge.snapshot(),  # DAY JUDGE (R0)
            "llm": self._llm_stats(),
            "ml": self._ml_stats(),
            "trading": {
                "auto_trade": auto_trade,
                "scan_universe": self._scan_universe_size,
                "scan_interval": self._scan_interval,
                "last_scan": self._last_scan_at,
                # Scanner 2.0 truth (2026-09-02: "those cards are not
                # accurate anymore") — the card shows THIS when it exists
                "scanner2_symbols": len(self._scanner2.symbols)
                if self._scanner2 is not None
                else 0,
                "scanner2_stream_fresh": (
                    __import__("time").time() - self._scanner2.stream_bars_ts < 90.0
                )
                if self._scanner2 is not None
                else False,
                "ladder": dict(self._ladder_stats),
            },
        }

    def position_history(self, key: str) -> list[str]:
        """The popup's order history: every Wave-tagged order this position
        placed, straight from the DB (entry, scale-outs, exits)."""
        if self._database is None:
            return []
        try:
            rows = self._database.query(
                "SELECT submitted_at, side, order_type, status, filled_qty, filled_avg_price"
                " FROM orders WHERE client_order_id LIKE ? ORDER BY submitted_at",
                (f"wave-{key}-%",),
            )
        except Exception:
            logger.exception("position history query failed")
            return []
        lines: list[str] = []
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        for row in rows:
            # DB timestamps are UTC — show ET (2026-08-24: the popup
            # said 13:46 for a 9:46 trade)
            raw = str(row["submitted_at"] or "")
            try:
                stamp = datetime.fromisoformat(raw).astimezone(et).strftime("%H:%M:%S")
            except ValueError:
                stamp = raw[11:19]
            line = f"{stamp}  {row['side']} {row['order_type']} — {row['status']}"
            if row["filled_avg_price"]:
                line += f"  {row['filled_qty']:g} @ {row['filled_avg_price']:.2f}"
            lines.append(line)
        return lines

    def tighten_stop(self, key: str) -> None:
        """Manual override from the popup (confirmed): stop moves halfway
        from its current level toward the live price. The actor refuses any
        request that would loosen."""
        from waveapp.engine.actor import TERMINAL_STATES

        if self.engine is None:
            return
        actor = self.engine.actors.get(key)
        if actor is None or actor.state in TERMINAL_STATES:
            return
        last = self._latest_price(actor.spec.symbol) or actor.avg_entry_price
        if not last:
            return
        current = (
            actor.exit_engine.current_stop
            if actor.exit_engine is not None
            else actor.spec.stop_price
        )
        new_stop = round((current + last) / 2, 2)
        self._spawn(actor.tighten_stop_manual(new_stop), f"manual tighten {actor.spec.symbol}")
        logger.info("manual tighten requested for %s → %.2f", actor.spec.symbol, new_stop)

    async def re_arm_kill_and_start(self) -> str:
        """§4 gate + confirm already passed in the UI: clear the kill halt and
        restart through the NORMAL start path (reconcile first, as always)."""
        if self.engine is None:
            return "engine offline — broker not connected yet"
        self.engine.risk.re_arm_kill()
        return await self.command("start")

    def re_arm_weekly(self) -> None:
        """§4 hard gate (Touch ID) already passed in the UI; clears only a
        WEEKLY_LOSS halt — every other halt state is untouched."""
        if self.engine is not None:
            self.engine.risk.re_arm_weekly()

    # -- weekly reports (agenda #4, 2026-08-23) -----------------------------

    @staticmethod
    def _report_due(kind: str, now_et, have_report: bool) -> bool:
        """Pure scheduling truth: week_close fires Friday from 16:05 ET,
        week_ahead fires Monday from 09:00 ET — each once per week."""
        if have_report:
            return False
        from datetime import time as _time

        if kind == "week_close":
            return now_et.weekday() == 4 and now_et.time() >= _time(16, 5)
        if kind == "week_ahead":
            return now_et.weekday() == 0 and now_et.time() >= _time(9, 0)
        return False

    def report_for(self, kind: str, on_or_before) -> dict | None:
        """The Reports page's provider (tiny table — millisecond query)."""
        if self._database is None:
            return None
        try:
            from waveapp.engine.reports import ahead_monday, load_report

            if kind == "week_ahead":
                # browsing a weekend date means the COMING week's preview
                on_or_before = ahead_monday(on_or_before)
            return load_report(self._database, kind, on_or_before)
        except Exception:
            logger.exception("report lookup failed")
            return None

    async def _reports_loop(self) -> None:
        from datetime import UTC, datetime
        from zoneinfo import ZoneInfo

        from waveapp.engine import reports as reports_module

        et = ZoneInfo("America/New_York")
        await asyncio.sleep(30.0)
        first_pass = True
        while True:
            try:
                if self._database is not None:
                    now_et = datetime.now(UTC).astimezone(et)
                    monday, _sunday = reports_module.week_bounds(now_et.date())
                    if first_pass:
                        first_pass = False
                        # first run ever: seed both reports quietly so the
                        # Reports page is never empty (no Telegram spam)
                        for kind in ("week_close", "week_ahead"):
                            if (
                                reports_module.load_report(self._database, kind, now_et.date())
                                is None
                            ):
                                await self._make_report(kind, monday, push=False)
                    for kind in ("week_close", "week_ahead"):
                        have = (reports_module.load_report(self._database, kind, monday) or {}).get(
                            "report_date"
                        ) == monday.isoformat()
                        if self._report_due(kind, now_et, have):
                            await self._make_report(kind, monday, push=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reports cycle failed")
            await asyncio.sleep(300.0)

    async def _make_report(self, kind: str, monday, push: bool) -> None:
        from waveapp.engine import reports as reports_module

        if kind == "week_close":
            content, payload = await asyncio.to_thread(
                reports_module.week_close_report, self._database, monday
            )
        else:
            market = await self._market_week_snapshot()
            posture = {"auto_trade": False, "floor": 10.0}
            try:
                from waveapp.config import AppConfig

                config = AppConfig.load()
                posture = {
                    "auto_trade": config.auto_trade,
                    "floor": config.min_entry_price,
                }
            except Exception:
                logger.debug("posture config load failed", exc_info=True)
            content, payload = await asyncio.to_thread(
                reports_module.week_ahead_report, self._database, monday, market, posture
            )
        await asyncio.to_thread(
            reports_module.store_report, self._database, kind, monday, content, payload
        )
        logger.info("weekly report stored: %s %s", kind, monday)
        if push:
            await self._push(content, klass="report")  # ON-mode only

    async def _market_week_snapshot(self) -> dict | None:
        """SPY/QQQ, measured not predicted: last week's change and how big
        SPY's daily swings have been. None when bars are unavailable."""
        try:
            from datetime import UTC, datetime, timedelta

            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            from waveapp.broker.alpaca import KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET
            from waveapp.config import AppConfig
            from waveapp.security import secrets as _secrets

            key_id = _secrets.get_secret(KEYCHAIN_PAPER_KEY_ID)
            secret = _secrets.get_secret(KEYCHAIN_PAPER_SECRET)
            if not key_id or not secret:
                return None
            feed = DataFeed.SIP if AppConfig.load().data_feed == "sip" else DataFeed.IEX
            client = StockHistoricalDataClient(key_id, secret)
            request = StockBarsRequest(
                symbol_or_symbols=["SPY", "QQQ"],
                timeframe=TimeFrame.Day,
                start=datetime.now(UTC) - timedelta(days=14),
                feed=feed,
            )
            bars = await asyncio.wait_for(
                asyncio.to_thread(client.get_stock_bars, request), timeout=20.0
            )
            changes: dict[str, float] = {}
            volatility = None
            for symbol in ("SPY", "QQQ"):
                series = bars.data.get(symbol, [])
                closes = [float(b.close) for b in series]
                if len(closes) >= 6:
                    week = closes[-6:]
                    changes[symbol] = (week[-1] / week[0] - 1.0) * 100.0
                    if symbol == "SPY":
                        moves = [
                            abs(week[i] / week[i - 1] - 1.0) * 100.0 for i in range(1, len(week))
                        ]
                        volatility = sum(moves) / len(moves)
            if not changes:
                return None
            return {"changes": changes, "volatility": volatility}
        except Exception:
            logger.debug("market snapshot failed", exc_info=True)
            return None

    # -- ML shadow mode (§9.2, agenda #3 redo — manual switch) -----

    @staticmethod
    def _ml_mode() -> str:
        try:
            from waveapp.config import AppConfig

            return AppConfig.load().ml_mode
        except Exception:
            return "off"

    def apply_ml_mode(self, mode: str) -> str:
        """The System tab's switch. "active" is REFUSED until a scanner
        model has passed the §9.2/§11 bar and sits validated in the
        registry — ML must never gate a trade before that."""
        if mode == "active":
            ready = False
            if self._database is not None:
                try:
                    rows = self._database.query(
                        "SELECT COUNT(*) AS n FROM model_registry"
                        " WHERE kind='scanner_ranker' AND active=1"
                    )
                    ready = bool(rows and rows[0]["n"])
                except Exception:
                    logger.exception("model registry check failed")
            if not ready:
                return "refused — no validated model exists yet (§9.2)."
        self._ml_stats_value["mode"] = mode
        logger.info("ML mode set to %s", mode)
        if mode == "off":
            return (
                "OFF — nothing runs. Shadow gathers data for the ML decision;"
                " it never touches trading."
            )
        if mode == "shadow":
            return "shadow — gathering data; outcome labeling runs while the market is closed"
        return "ACTIVE — the validated model now gates entries"

    @staticmethod
    def _ml_stats_default() -> dict:
        return {
            "mode": "off",
            "rows": 0,
            "symbol_days": 0,
            "accepted_days": 0,
            "labeled": 0,
            "label_wins": 0,
            "label_losses": 0,
            "today_rows": 0,
            "models": 0,
            "model_ready": False,
            "labeled_session": 0,
            "target": 3000,
        }

    def _ml_stats(self) -> dict:
        """NEVER blocks: returns the snapshot the background loop computed
        (the first #3 attempt ran these scans on the UI thread — the app
        froze at launch; measured 2026-08-23 on the deduped DB: every query
        0.1–5ms, but the worker-thread rule stands regardless)."""
        value = dict(self._ml_stats_value)
        value["labeled_session"] = self._ml_labeled_session
        # Stage M1 live shadow: cumulative counters + one-shot deltas that
        # become the ML tab's REAL green/red dots (consumed on read)
        shadow = self._brain_shadow
        value["shadow_scored"] = shadow["scored"]
        value["shadow_approved"] = shadow["approved"]
        value["shadow_rejected"] = shadow["rejected"]
        value["shadow_approve_ratio"] = (
            round(shadow["approved"] / shadow["scored"], 3) if shadow["scored"] else 0.0
        )
        value["shadow_approved_new"] = shadow["approved_new"]
        value["shadow_rejected_new"] = shadow["rejected_new"]
        shadow["approved_new"] = 0
        shadow["rejected_new"] = 0
        value["brain_loaded"] = self._brain is not None
        value["brain2_loaded"] = self._brain2 is not None
        return value

    async def _ml_stats_loop(self) -> None:
        await asyncio.sleep(20.0)  # let the app settle first
        while True:
            try:
                self._ml_stats_value = await asyncio.to_thread(self._ml_stats_compute)
                # push the fresh numbers NOW — the regular system push runs
                # only every 10 min while the market is CLOSED (weekend
                # throttle), which left the ML page stale for minutes
                # (2026-08-23: "the data never arrived")
                if self._on_system is not None:
                    try:
                        self._on_system(self._system_data())
                    except Exception:
                        logger.debug("ml stats push failed", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ml stats refresh failed")
            await asyncio.sleep(300.0)

    def _ml_stats_compute(self) -> dict:
        """The counting queries — WORKER THREAD ONLY."""
        stats = self._ml_stats_default()
        stats["mode"] = self._ml_mode()
        stats["labeled_session"] = self._ml_labeled_session
        if self._database is None:
            return stats
        try:
            from datetime import UTC as _utc
            from datetime import datetime as _dt

            q = self._database.query
            stats["rows"] = int(q("SELECT COUNT(*) AS n FROM candidates")[0]["n"])
            stats["symbol_days"] = int(
                q("SELECT COUNT(DISTINCT symbol || session_date) AS n FROM candidates")[0]["n"]
            )
            stats["accepted_days"] = int(
                q(
                    "SELECT COUNT(DISTINCT symbol || session_date) AS n FROM candidates"
                    " WHERE decision='accepted'"
                )[0]["n"]
            )
            stats["labeled"] = int(
                q("SELECT COUNT(*) AS n FROM candidates WHERE outcome IS NOT NULL")[0]["n"]
            )
            stats["label_wins"] = int(
                q(
                    "SELECT COUNT(*) AS n FROM candidates"
                    """ WHERE outcome LIKE '%"label": "win"%'"""
                )[0]["n"]
            )
            stats["label_losses"] = int(
                q(
                    "SELECT COUNT(*) AS n FROM candidates"
                    """ WHERE outcome LIKE '%"label": "loss"%'"""
                )[0]["n"]
            )
            today = _dt.now(_utc).date().isoformat()
            stats["today_rows"] = int(
                q("SELECT COUNT(*) AS n FROM candidates WHERE session_date = ?", (today,))[0]["n"]
            )
            stats["models"] = int(q("SELECT COUNT(*) AS n FROM model_registry")[0]["n"])
            # v2 school progress vs the retrain guards (2026-09-02:
            # the ML sub-tabs must show the LIVE story)
            school = q(
                "SELECT COUNT(DISTINCT session_date) AS days, COUNT(*) AS rows_n,"
                " SUM(CASE WHEN label_60m='win' THEN 1 ELSE 0 END) AS wins"
                " FROM snapshot_labels WHERE label_60m IS NOT NULL"
            )[0]
            stats["school_days"] = int(school["days"] or 0)
            stats["school_rows"] = int(school["rows_n"] or 0)
            stats["school_wins"] = int(school["wins"] or 0)
            # live shadow head-to-head (8.13): resolved menu judgments per model
            stats["shadow_menu"] = {
                str(r["model"] or "v1"): {
                    "n": int(r["n"]),
                    "wins": int(r["w"] or 0),
                    "approved_n": int(r["na"] or 0),
                    "approved_wins": int(r["wa"] or 0),
                }
                for r in q(
                    "SELECT model, COUNT(*) AS n, SUM(won) AS w,"
                    " SUM(CASE WHEN approved=1 THEN 1 ELSE 0 END) AS na,"
                    " SUM(CASE WHEN approved=1 THEN won ELSE 0 END) AS wa"
                    " FROM brain_scores WHERE executed=0 AND won IS NOT NULL"
                    " GROUP BY model"
                )
            }
            # M2b: the daily precision curve (Brain approvals, resolved)
            stats["precision_series"] = [
                {
                    "day": str(r["day"]),
                    "n": int(r["n"]),
                    "wr": round(int(r["w"] or 0) / int(r["n"]) * 100.0, 1),
                }
                for r in reversed(
                    q(
                        "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n, SUM(won) AS w"
                        " FROM brain_scores WHERE won IS NOT NULL AND approved=1"
                        " GROUP BY substr(ts, 1, 10) ORDER BY day DESC LIMIT 20"
                    )
                )
                if int(r["n"]) >= 5  # under 5 judgments a day is noise
            ]
            trade_rows = q(
                "SELECT COUNT(*) AS n, SUM(won) AS w,"
                " SUM(CASE WHEN approved=1 THEN 1 ELSE 0 END) AS na,"
                " SUM(CASE WHEN approved=1 THEN won ELSE 0 END) AS wa"
                " FROM brain_scores WHERE executed=1 AND won IS NOT NULL"
            )[0]
            stats["shadow_trades"] = {
                "n": int(trade_rows["n"] or 0),
                "wins": int(trade_rows["w"] or 0),
                "approved_n": int(trade_rows["na"] or 0),
                "approved_wins": int(trade_rows["wa"] or 0),
            }
            # blueprint 9.4: the ML tab reloads its ladder/report card when
            # the OOF curve file changes (nightly retrain), not only at launch
            try:
                import os

                from waveapp.config import support_dir

                curve = os.path.join(support_dir(), "research", "judge_v1_oof.csv")
                stats["curve_stamp"] = os.stat(curve).st_mtime if os.path.exists(curve) else 0.0
            except Exception:
                stats["curve_stamp"] = 0.0
            stats["model_ready"] = bool(
                q(
                    "SELECT COUNT(*) AS n FROM model_registry"
                    " WHERE kind='scanner_ranker' AND active=1"
                )[0]["n"]
            )
        except Exception:
            logger.exception("ml stats query failed")
        return stats

    async def _ml_labeler_loop(self) -> None:
        """Fills candidate OUTCOMES (triple-barrier, §9.2) from historical
        1-min bars. Runs ONLY while the switch says shadow, ONLY outside
        regular hours, one symbol-day at a time, gently."""
        from waveapp.engine.session import SessionScheduler

        await asyncio.sleep(60.0)  # never compete with app startup
        while True:
            try:
                regime = SessionScheduler.regime()
                if (
                    self._ml_mode() != "shadow"
                    or not self._labeler_allowed(regime)
                    or self._database is None
                ):
                    await asyncio.sleep(600)
                    continue
                batch = await asyncio.to_thread(self._label_batch_rows)
                if not batch:
                    await asyncio.sleep(3600)  # caught up — idle
                    continue
                for row in batch:
                    if self._ml_mode() != "shadow":  # flipped off mid-batch
                        break
                    await self._label_one(row)
                    await asyncio.sleep(0.25)  # gentle on the data API
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ml labeler cycle failed")
                await asyncio.sleep(300)

    @staticmethod
    def _labeler_allowed(regime) -> bool:
        """Labeling runs ONLY while the market world is asleep (decision,
        2026-08-23): OVERNIGHT and CLOSED. Never regular hours, and never
        PRE/POST either — the scanner is already working the market then."""
        from waveapp.engine.session import Regime

        return regime in (Regime.OVERNIGHT, Regime.CLOSED)

    def _label_batch_rows(self) -> list[dict]:
        """The next unlabeled symbol-days (first journal row each), finished
        sessions only — today's outcome isn't knowable yet. WORKER THREAD."""
        from datetime import UTC as _utc
        from datetime import datetime as _dt

        today = _dt.now(_utc).date().isoformat()
        try:
            return [
                dict(r)
                for r in self._database.query(
                    "SELECT MIN(id) AS id, symbol, session_date FROM candidates"
                    " WHERE session_date < ?"
                    " GROUP BY symbol, session_date"
                    " HAVING SUM(outcome IS NOT NULL) = 0"
                    " ORDER BY session_date LIMIT 25",
                    (today,),
                )
            ]
        except Exception:
            logger.exception("label batch query failed")
            return []

    async def _label_one(self, row: dict) -> None:
        import json
        from datetime import datetime

        from waveapp.engine.labeler import compute_outcome

        detail = self._database.query(
            "SELECT ts, features FROM candidates WHERE id = ?", (row["id"],)
        )
        if not detail:
            return
        try:
            features = json.loads(detail[0]["features"])
            entry_ts = datetime.fromisoformat(str(detail[0]["ts"])).timestamp()
            atr = float(features.get("price", 0)) * float(features.get("atr_pct", 0)) / 100.0
        except (ValueError, TypeError, KeyError):
            atr = 0.0
            entry_ts = 0.0
        outcome = None
        if atr > 0:
            bars = await self._fetch_day_bars(row["symbol"], row["session_date"])
            if bars:
                outcome = compute_outcome(bars, entry_ts, atr)
        # unlabelable rows are stamped too — never retried forever
        payload = json.dumps(outcome if outcome is not None else {"label": "no_data"})
        self._database.execute(
            "UPDATE candidates SET outcome = ? WHERE id = ?", (payload, row["id"])
        )
        if outcome is not None:
            self._ml_labeled_session += 1

    async def _fetch_day_bars(
        self, symbol: str, session_date: str
    ) -> list[tuple[float, float, float, float, float]]:
        """One session's 1-minute bars (same REST client as chart backfill)."""
        try:
            from datetime import UTC, datetime, timedelta

            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            from waveapp.broker.alpaca import KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET
            from waveapp.config import AppConfig
            from waveapp.security import secrets as _secrets

            key_id = _secrets.get_secret(KEYCHAIN_PAPER_KEY_ID)
            secret = _secrets.get_secret(KEYCHAIN_PAPER_SECRET)
            if not key_id or not secret:
                return []
            feed = DataFeed.SIP if AppConfig.load().data_feed == "sip" else DataFeed.IEX
            client = StockHistoricalDataClient(key_id, secret)
            day = datetime.fromisoformat(session_date).replace(tzinfo=UTC)
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=day,
                end=day + timedelta(days=1),
                feed=feed,
            )
            bars = await asyncio.wait_for(
                asyncio.to_thread(client.get_stock_bars, request), timeout=20.0
            )
            return [
                (
                    b.timestamp.timestamp(),
                    float(b.open),
                    float(b.high),
                    float(b.low),
                    float(b.close),
                )
                for b in bars.data.get(symbol, [])
            ]
        except Exception:
            logger.debug("day bars fetch failed for %s %s", symbol, session_date, exc_info=True)
            return []

    def apply_risk_limits(self, values: dict) -> None:
        """Settings (Touch ID gated) changed the risk limits — apply to the
        RUNNING engine immediately. Values only replace the §12 fields; the
        checks themselves are untouchable (hard rule 2)."""
        if self.engine is None:
            return
        from dataclasses import replace

        allowed = {
            key: values[key]
            for key in (
                "risk_per_trade_pct",
                "max_daily_loss_pct",
                "max_weekly_loss_pct",
                "max_positions",
                "max_notional_pct",
                "impact_participation_pct",
                # A3-10 (audit 2026-09-22): the WAVE-2 risk-based book
                # fields were missing here, so a Settings edit of the 6%
                # total-risk ceiling or the hard count cap silently
                # no-opped until the next engine start. Both are §12
                # limits like the rest — replaceable, never removable.
                "max_total_risk_pct",
                "max_positions_hard",
            )
            if key in values
        }
        # RiskLimits is frozen — swap in a new instance, checks untouched
        self.engine.risk.limits = replace(self.engine.risk.limits, **allowed)
        logger.info("risk limits live-applied: %s", allowed)

    def apply_scanner_settings(self, universe_size: int, interval_seconds: int) -> None:
        """Interval applies from the next cycle; universe size from the next
        universe build."""
        self._scan_universe_size = universe_size
        self._scan_interval = interval_seconds
        if self._scanner is not None:
            provider = getattr(self._scanner, "provider", None)
            if provider is not None and hasattr(provider, "universe_size"):
                provider.universe_size = universe_size
        logger.info(
            "scanner settings live-applied: %d symbols / %ds", universe_size, interval_seconds
        )

    async def apply_telegram_user_id(self, user_id: int) -> str:
        """Settings changed the Telegram user id — restart the bridge NOW so
        the ✓ in Settings means 'the bridge is really running on the new id',
        not 'saved to a file' (Settings rework, 2026-08-21)."""
        # A5-3 (audit 2026-09-22): the whole check→stop→start runs under the
        # bridge lock. Unlocked, the old-bridge stop() (seconds) plus start()
        # left a window where the run loop's _ensure_telegram passed the
        # `_bridge is None` guard and built a SECOND bridge — the loser
        # polled forever (Conflict churn) with its own ConfirmationGate.
        async with self._tg_lock:
            if (
                int(user_id) == self._telegram_user_id
                and self._bridge is not None
                and self._bridge.is_running
            ):
                # Save with an unchanged id used to restart a HEALTHY bridge —
                # and when the restart's Keychain read failed, the click itself
                # killed Telegram (the repro, 2026-09-21). Leave it alone.
                return "bridge already running on this id — nothing to restart"
            self._telegram_user_id = int(user_id)
            if self._bridge is not None:
                try:
                    await self._bridge.stop()
                except Exception:
                    logger.debug("old bridge stop failed", exc_info=True)
                self._bridge = None
            if self._telegram_user_id <= 0:
                self._on_status("Telegram", theme.GREY, "Telegram: not configured")
                return "bridge off — no user id set"
            await self._ensure_telegram_locked()
        if self._bridge is not None and self._bridge.is_running:
            return "bridge reconnected — check Telegram for the hello message"
        return "bridge could NOT start — check the bot token and the log"

    async def send_telegram_test(self) -> str:
        if self._bridge is None:
            return "Telegram bridge not running (set the user id + token first)"
        await self._bridge.push("🔧 Wave settings test — the bridge works")
        return "test message sent"

    async def confirm_telegram_mode(self, mode: str) -> str:
        """The Settings toggle changed the notification mode — confirm it on
        the phone (2026-09-21). Pushed through the bridge DIRECTLY, past
        the mode gate, so even switching to OFF says goodbye.

        A5-10 (audit 2026-09-22): no silent no-op — returns a status string
        the Settings page can show, so a down bridge never masquerades as
        phone-proof. (Displaying it is app.py's side — see HANDOFFS.)"""
        if self._bridge is None or not self._bridge.is_running:
            status = "bridge not running — saved to config only"
            logger.info("telegram mode '%s': %s", mode, status)
            return status
        texts = {
            "on": "🔔 Notifications ON — everything gets pushed.",
            "minimum": (
                "🔕 Notifications MINIMUM — fills, banks, risk halts and the day summary only."
            ),
            "off": "🔇 Notifications OFF — no more pushes. Commands still answer.",
        }
        try:
            await self._bridge.push(texts.get(mode, f"Notifications: {mode}"))
        except Exception:
            logger.exception("telegram mode confirmation push failed")
            status = "confirmation push failed — saved to config only"
            logger.info("telegram mode '%s': %s", mode, status)
            return status
        status = "confirmed on the phone"
        logger.info("telegram mode '%s': %s", mode, status)
        return status

    def sell_position(self, key: str) -> None:
        """The card's SELL button. Every click spawns ITS OWN task — actors
        are independent, so one position's exit never waits for another's
        (8.4 round 3)."""
        from waveapp.engine.actor import TERMINAL_STATES

        if self.engine is None:
            return
        actor = self.engine.actors.get(key)
        if actor is None or actor.state in TERMINAL_STATES:
            return  # Test-tab fake keys and already-closed positions land here
        self._spawn(
            actor.close_now("manual sell (positions card)"),
            f"manual sell {actor.spec.symbol}",
        )
        logger.info("manual sell requested for %s (%s)", actor.spec.symbol, key)

    async def _snapshot_equity(self) -> None:
        """Feed the loss halts + the Performance tab's equity curve (§5)."""
        if self._adapter is None:
            return
        try:
            # §0.7 LANDMINE (A3-10, audit 2026-09-22): TradingMode.PAPER
            # hardcoded — this equity snapshot feeds risk.update_equity, the
            # base of every fixed-fractional size and loss-halt threshold.
            # The monitor is paper-pinned by design today; Phase 11 must
            # thread the real TradingMode through here (hard rule 7), never
            # a string flag.
            account = await self._adapter.get_account(TradingMode.PAPER)
        except Exception:
            return
        if self.engine is not None:
            self.engine.risk.update_equity(account.equity)
        self._last_equity = account.equity
        if self._on_balance is not None:
            self._on_balance(TradingMode.PAPER.value, account.equity)
        if self._on_performance is not None:
            try:
                self._on_performance(self._performance_data(account.equity))
            except Exception:
                logger.exception("performance data push failed")
        # scoreboard rides the same cadence: covers restart restore, resets
        # and per-trade removals without waiting for the next closed trade
        if self._on_scoreboard is not None and self._database is not None:
            try:
                self._on_scoreboard(self.scoreboard())
            except Exception:
                logger.exception("scoreboard push failed")
        if self._database is not None:
            try:
                from waveapp.persistence.db import utc_now

                self._database.execute(
                    "INSERT OR REPLACE INTO equity_snapshots"
                    " (ts, equity, cash, market_value, trading_mode) VALUES (?,?,?,?,?)",
                    (
                        utc_now(),
                        account.equity,
                        account.cash,
                        account.equity - account.cash,
                        TradingMode.PAPER.value,
                    ),
                )
            except Exception:
                logger.exception("equity snapshot failed")

    def _report_feed_health(self) -> None:
        if self._hub is None or not self._hub.is_running:
            self._on_status("Data feed", theme.RED, "Data feed: market-data stream not running")
            return
        age = self._hub.last_message_age()
        detail = "connected, no messages yet" if age is None else f"last message {age:.0f}s ago"
        self._on_status(
            "Data feed",
            theme.GREEN,
            f"Data feed: {self._hub.feed.upper()} stream live"
            f" ({', '.join(self._hub.watchlist)}) — {detail}",
        )

    async def _teardown(self) -> None:
        # SUPERVISOR FIRST (audit 2026-09-16 build-before-open: a teardown
        # that leaves the supervisor alive lets it resurrect scan loops
        # against a nulled monitor within 60s — and a later reconnect then
        # runs TWO concurrent entry pipelines, a real double-position race).
        sup = getattr(self, "_supervisor", None)
        if sup is not None:
            self._supervisor = None
            with contextlib.suppress(Exception):
                await sup.stop()
        # backfill dedupe must die with the hub (audit 2026-09-16: it
        # survived teardown while the BarBuilder didn't, so every mid-day
        # reconnect resurrected the premature-recross and bloated-fallback-
        # ATR bugs on symbols whose bars were never re-seeded)
        with contextlib.suppress(Exception):
            self._chart_backfill.clear()
        # A4-1 (audit 2026-09-22): the in-flight guard and its tasks die with
        # the hub too. Cancel outstanding fetches FIRST (a straggler finishing
        # after this point would seed the dead hub / repopulate the maps),
        # then clear the pending set so _ensure_backfill schedules fresh
        # fetches against the reconnect's new, empty BarBuilder.
        backfills = list(self._backfill_tasks)
        for task in backfills:
            task.cancel()
        if backfills:
            with contextlib.suppress(Exception):
                await asyncio.gather(*backfills, return_exceptions=True)
        self._backfill_tasks.clear()
        self._backfill_pending.clear()
        # A4-2 (audit 2026-09-22): the "already watched" guards survived
        # teardown while the hub's real subscriptions died with it — after a
        # reconnect the new hub streamed only base watchlist + positions, so
        # every menu signal hit the no-fresh-quote deferral each cycle:
        # silent entry starvation for the rest of the day. Clear them so the
        # scanner2-menu and climber paths re-issue hub.watch (+ backfill) on
        # the fresh hub; the cached day LIST itself is kept — the rewatch
        # flag makes _ensure_day_list re-watch it without rebuilding.
        self._s2_watched = set()
        self._climber_watched = set()
        self._day_list_rewatch = True
        if self._shadow_kitchen_task is not None:
            self._shadow_kitchen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._shadow_kitchen_task
            self._shadow_kitchen_task = None
            self._shadow_kitchen = None
        if self._polygon_task is not None:
            self._polygon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polygon_task
            self._polygon_task = None
        if self._positions_task is not None:
            self._positions_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._positions_task
            self._positions_task = None
        if self._scan_task is not None:
            self._scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scan_task
            self._scan_task = None
            self._scanner = None
        if self._scanner2_task is not None:
            self._scanner2_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scanner2_task
            self._scanner2_task = None
        for attr in ("_scanner2_news_task", "_scanner2_feeds_task"):
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, attr, None)
        # A5-8/A4-12 (audit 2026-09-22): these two lines sat INSIDE the loop
        # above — the second iteration called fold_baselines() on None
        # (suppressed, silently). Once, after both tasks are down.
        if self._scanner2 is not None:
            with contextlib.suppress(Exception):
                self._scanner2.fold_baselines()  # keep today's observed curves
        self._scanner2 = None
        if self.engine is not None:
            engine, self.engine = self.engine, None
            try:
                await engine.shutdown()
            except Exception:
                logger.exception("error shutting down engine")
        if self._bridge is not None:
            # A5-3: same lock as _ensure_telegram/apply_telegram_user_id —
            # never stop a bridge that is mid-start (re-check under the lock).
            async with self._tg_lock:
                if self._bridge is not None:
                    bridge, self._bridge = self._bridge, None
                    try:
                        await bridge.stop()
                    except Exception:
                        logger.exception("error stopping Telegram bridge")
        if self._hub is not None:
            hub, self._hub = self._hub, None
            try:
                await hub.stop()
            except Exception:
                logger.exception("error stopping DataHub")
        if self._adapter is not None:
            adapter, self._adapter = self._adapter, None
            try:
                await adapter.close()
            except Exception:
                logger.exception("error closing adapter")
