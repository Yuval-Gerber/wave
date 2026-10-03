"""The Master Key SHADOW kitchen — proof rung 2 (2026-09-08, adopted).

Runs the v14 three-gear VWAP exit brain (waveapp/engine/master_key.py)
beside the real kitchen. For every OPEN position it consumes live trade
ticks, builds one-second bars, feeds the same trackers the lab used, and
logs every decision the Master Key WOULD have made — entries, slices,
stops, climaxes — with a running would-be P&L.

IT NEVER PLACES ORDERS. It holds no broker reference at all. Its output is
a daily JSONL journal (support_dir()/research/shadow_kitchen_<day>.jsonl)
plus wave.trade.shadow log lines, scored each evening against what the real
kitchen actually did (scripts/shadow_scoreboard.py). Only if the shadow's
live decisions keep matching the lab's replay of the same days does the
Master Key earn a §11 adoption discussion.

Known, documented divergences from the lab replay (both conservative):
 - the lab tape starts 120s before entry; live tracking starts at the entry
   fill, and the regime tracker is warm-started from today's 1-minute bars
   (minute-cadence VWAP points for slope; trend persistence accumulates
   from real seconds only, so the first minutes lean toward the cautious
   chop gear);
 - seconds with zero trades produce no bar, exactly like the lab's tapes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from waveapp.engine.master_key import (
    DEFAULT_PARAMS,
    BurstTracker,
    MasterKeyEngine,
    MasterKeyParams,
    RegimeTracker,
    Vol60Tracker,
    VwapTracker,
    atr1m_pct,
)

logger = logging.getLogger("wave.trade.shadow")

ET = ZoneInfo("America/New_York")
ACTIVE_ACTOR_STATES = ("open", "scaling_out", "closing", "halted")

# A2-2 (2026-09-22): how long after a judge act the actor may stay alive
# before the act is declared not-stuck and the judge re-arms. A marketable
# exit normally fills in 1-3s and close_now's NVDG failure backoff is 5s —
# 10s clears both with margin, and the broker hard stop floors the position
# throughout the wait, so a slow retry costs at most 10s of exposure the
# stop already bounds.
JUDGE_ACT_GRACE_MS = 10_000

# A5-5 leg 3 (audit 2026-09-22): a bug inside the per-second judge used to
# present as a silently stuck stance chip — the outer swallow logged at debug
# only. Failures now surface at exception level, rate-limited to once per
# this window so a fault that fires every second stays bounded in the log.
JUDGE_ERR_LOG_SECONDS = 60.0


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def session_marks(now_utc: datetime) -> tuple[int, int, int]:
    """(rth_open_ms, last_reentry_ms, flatten_ms) for today's ET session."""
    et_now = now_utc.astimezone(ET)
    rth_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    last_reentry = et_now.replace(hour=15, minute=30, second=0, microsecond=0)
    flatten = et_now.replace(hour=15, minute=50, second=0, microsecond=0)
    return _ms(rth_open), _ms(last_reentry), _ms(flatten)


class SecondBarAggregator:
    """Trade ticks -> one-second OHLCV bars (closed when the next second's
    first tick arrives, mirroring the minute BarBuilder's roll rule)."""

    def __init__(self) -> None:
        self._current: dict | None = None
        self.closed: deque[dict] = deque(maxlen=4096)

    def on_trade(self, price: float, size: float, t_ms: int) -> None:
        sec = (t_ms // 1000) * 1000
        cur = self._current
        if cur is None or sec > cur["t"]:
            if cur is not None:
                self.closed.append(cur)
            self._current = {"t": sec, "o": price, "h": price, "l": price, "c": price, "v": size}
            return
        if sec < cur["t"]:  # late tick — fold into the open second
            sec = cur["t"]
        cur["h"] = max(cur["h"], price)
        cur["l"] = min(cur["l"], price)
        cur["c"] = price
        cur["v"] += size

    def drain(self) -> list[dict]:
        out = list(self.closed)
        self.closed.clear()
        return out


@dataclass
class ShadowPosition:
    symbol: str
    position_key: str
    engine: MasterKeyEngine | None
    vwap: VwapTracker | None
    burst: BurstTracker | None
    regime: RegimeTracker | None
    vol60: Vol60Tracker | None
    day: str
    flatten_ms: int
    # R1.5 EFFICACY (2026-09-24): the REAL entry-fill moment (epoch ms) —
    # the age anchor for the guard's 10-minute scoring window. 0 = unknown
    # (no feed for this position).
    entry_ms: int = 0
    last_price: float = 0.0
    last_t_ms: int = 0
    finished: bool = False
    actor_gone: bool = False
    # drive mode: server-side stop ratchet + rebuy rebinding
    last_stop_px: float = 0.0
    last_amend_ms: int = 0
    pending_rebind: bool = False
    # judge drive (A2-2): act latch + when it fired. The latch prevents
    # multi-fire while an exit is in flight; _judge_second re-arms it when
    # the exit did not stick (actor still alive past JUDGE_ACT_GRACE_MS).
    judge_acted: bool = False
    judge_acted_t_ms: int = 0
    # per-position JudgeState, created lazily by _judge_second on the first
    # judged second; reset to None on an MK rebuy rebind (A2-6) so the new
    # leg is never judged against the dead leg's entry/peak
    judge: Any = None
    # A5-5 leg 2: wall-clock ms of the last SYNTHETIC judge second (judged
    # from the hub's freshest print because no trade-second bar drained) —
    # throttles the thin-tape path to at most once per second per position
    judge_synth_ms: int = 0


class ShadowKitchen:
    """The runner. attach() taps the hub; loop() ticks at 1 Hz.

    hub_getter/actors_getter are callables so reconnects (a new DataHub) and
    engine restarts are picked up automatically each tick.
    """

    def __init__(
        self,
        hub_getter: Callable[[], Any],
        actors_getter: Callable[[], dict[str, Any]],
        journal_dir: Path,
        params: MasterKeyParams = DEFAULT_PARAMS,
        clock: Callable[[], datetime] | None = None,
        mode: str = "shadow",
        rebuy_cb: Callable[..., Any] | None = None,
        notify_cb: Callable[[str], Any] | None = None,
        journal_db_cb: Callable[..., Any] | None = None,
        efficacy_cb: Callable[..., Any] | None = None,
    ) -> None:
        """mode="shadow": log would-be decisions only (never touches reality).
        mode="drive" (promoted 2026-09-09): the brain
        RUNS the kitchen — exits go through actor.apply_exit_decision (the
        same broker machinery, idempotent ids, LULD guards), the server-side
        stop is ratcheted to the brain's protect level (tighten-only), and
        rebuys go through rebuy_cb → the standard entry path (RiskEngine
        sizing, engine-running + auto_trade gates, bracket stop)."""
        self._hub_getter = hub_getter
        self._actors_getter = actors_getter
        self._journal_dir = journal_dir
        # M0 (ML master plan, 2026-09-23): the kitchen has no DB handle by
        # design — the monitor passes a small writer closure that inserts
        # judge_transitions rows / fills their forward outcomes through the
        # existing Database layer. None = telemetry off; every use is
        # try/except'd so a DB fault can never wound the judge.
        self._journal_db_cb = journal_db_cb
        self._ml_pending: list[dict] = []  # transitions awaiting forward outcomes
        # R1.5 EFFICACY GUARD (2026-09-24): a per-judged-second (key,
        # profit_atr, age_ms) reading back to the monitor's tracker — the
        # guard's early-outcome feed. None = guard off for this kitchen;
        # every call is try/except'd so a feed fault never wounds the judge.
        self._efficacy_cb = efficacy_cb
        self.params = params
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._notify = notify_cb  # climax-watch alert to the phone (item 3/10)
        self.mode = mode
        self._rebuy_cb = rebuy_cb
        self._aggs: dict[str, SecondBarAggregator] = {}
        self.positions: dict[str, ShadowPosition] = {}
        # A5-6 handoff (audit 2026-09-22): strong refs for the kitchen's own
        # fire-and-forget tasks — a bare ensure_future is only weakly held by
        # the loop (the 2026-08-19 GC segfault class) and a judge exit could
        # silently never submit. Done-callback discards and logs failures.
        self._oneshot_tasks: set[Any] = set()
        self._tap = self.on_trade_tick  # cached: bound methods are per-access

    # -- hot path (called from the stream) ----------------------------------
    def on_trade_tick(self, symbol: str, price: float, size: float, ts: Any) -> None:
        agg = self._aggs.get(symbol)
        if agg is None:
            return
        t_ms = _ms(ts) if isinstance(ts, datetime) else int(ts)
        agg.on_trade(price, size, t_ms)

    # -- lifecycle ----------------------------------------------------------
    def ensure_tap(self) -> None:
        hub = self._hub_getter()
        if hub is not None and hub.trade_tap is not self._tap:
            hub.trade_tap = self._tap
            logger.info("Master Key shadow attached to the trade stream")

    def _minute_bars_as_dicts(self, symbol: str) -> list[dict]:
        hub = self._hub_getter()
        if hub is None:
            return []
        try:
            bars = hub.bar_builder.bars(symbol)
        except Exception:
            return []
        return [
            {"t": _ms(b.start), "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in bars
        ]

    def _adopt_actor(self, key: str, actor: Any) -> None:
        symbol = actor.spec.symbol
        side = str(getattr(actor.spec.side, "value", actor.spec.side)).lower()
        if "buy" not in side:
            # S2 (short-side plan, 2026-09-23) — the "second judge": a SHORT
            # is adopted LIVE (not finished) but WITHOUT an MK engine — the
            # MK brain stays long-only bookkeeping and shorts stay excluded
            # from its ledger (S0 decision). engine=None routes _process
            # into the judge-only slice (the A5-5 path), where judge_second's
            # normalized frame mirrors every long rule exactly; the broker
            # bracket stop stays the floor underneath (hard rule 3), and the
            # core's session flatten owns the boundary (flatten_ms=0 — the
            # kitchen-ledger flatten is MK bookkeeping a short never has).
            qty = abs(float(actor.filled_qty or 0.0))
            entry = float(actor.avg_entry_price or 0.0)
            if entry <= 0 or qty <= 0:
                return  # not filled yet — try again next tick
            now = self._clock()
            short_opened = (
                actor.entry_filled_at or getattr(actor, "adopted_entry_time", None) or now
            )
            self._aggs[symbol] = SecondBarAggregator()
            self.positions[key] = ShadowPosition(
                symbol=symbol,
                position_key=key,
                engine=None,
                vwap=None,
                burst=None,
                regime=None,
                vol60=Vol60Tracker(),  # the judge's fuel gauge needs it
                day=now.astimezone(ET).date().isoformat(),
                flatten_ms=0,
                entry_ms=_ms(short_opened),  # R1.5: the efficacy age anchor
                finished=False,
            )
            logger.info(
                "SHORT %s adopted judge-only: %.6g sh @ %.4f (no MK engine —"
                " the second judge manages, broker stop floors)",
                symbol,
                qty,
                entry,
            )
            self._journal(self.positions[key], {"event": "adopt_short", "qty": qty, "entry": entry})
            return
        qty = float(actor.filled_qty or 0.0)
        entry = float(actor.avg_entry_price or 0.0)
        if entry <= 0 or qty <= 0:
            return  # not filled yet — try again next tick
        opened_dt = (
            actor.entry_filled_at or getattr(actor, "adopted_entry_time", None) or self._clock()
        )
        opened_ms = _ms(opened_dt)
        now = self._clock()
        rth_open_ms, last_reentry_ms, flatten_ms = session_marks(now)
        # A2-8 (audit 2026-09-22): a position adopted AFTER today's flatten
        # mark (POST/OVERNIGHT adoption — e.g. an app restart at 17:00) has
        # flatten_ms in the past, and the kitchen ledger would fire its
        # flatten (in drive: EXIT_NOW) on the very FIRST bar. The core's
        # session-boundary flatten (early-close aware) is the real backstop,
        # so the kitchen's mirror is disabled for this position instead of
        # instantly dumping it. NOTE: session_marks itself is NOT early-close
        # aware (hardcoded 15:30/15:50 ET) — on an early-close day the
        # kitchen-ledger flatten runs late, but the core's flatten still
        # fires on time; the real fix needs the session calendar (HANDOFFS).
        if _ms(now) >= flatten_ms:
            logger.warning(
                "MK %s adopting %s after today's flatten mark — kitchen-ledger"
                " flatten disabled for this position (the core's session"
                " flatten owns the exit)",
                self.mode,
                symbol,
            )
            flatten_ms = 0
        minute_bars = self._minute_bars_as_dicts(symbol)
        vwap = VwapTracker(rth_open_ms)
        regime = RegimeTracker(self.params)
        for b in minute_bars:
            running = vwap.feed(b["t"], b["h"], b["l"], b["c"], b["v"])
            regime.feed(b["t"], running)  # minute-cadence warm start for slope
        engine = MasterKeyEngine(
            entry_price=entry,
            qty=qty,
            atr_pct=atr1m_pct(minute_bars, opened_ms, rth_open_ms),
            opened_ms=opened_ms,
            last_reentry_ms=last_reentry_ms,
            params=self.params,
        )
        self._aggs[symbol] = SecondBarAggregator()
        self.positions[key] = ShadowPosition(
            symbol=symbol,
            position_key=key,
            engine=engine,
            vwap=vwap,
            burst=BurstTracker(),
            regime=regime,
            vol60=Vol60Tracker(),
            day=now.astimezone(ET).date().isoformat(),
            flatten_ms=flatten_ms,
            entry_ms=opened_ms,  # R1.5: the efficacy age anchor
        )
        self.positions[key].last_stop_px = float(getattr(actor.spec, "stop_price", 0.0) or 0.0)
        logger.info(
            "Master Key (%s) adopted %s: %.6g sh @ %.4f (A=%.2f%%)",
            self.mode,
            symbol,
            qty,
            entry,
            engine.A,
        )
        self._journal(
            self.positions[key], {"event": "adopt", "qty": qty, "entry": entry, "atr_pct": engine.A}
        )

    def _journal(self, sp: ShadowPosition, row: dict) -> None:
        try:
            self._journal_dir.mkdir(parents=True, exist_ok=True)
            path = self._journal_dir / f"shadow_kitchen_{sp.day}.jsonl"
            base = {"day": sp.day, "symbol": sp.symbol, "position": sp.position_key}
            with path.open("a") as f:
                f.write(json.dumps(base | row) + "\n")
        except Exception:
            logger.exception("shadow journal write failed")

    def _mins_to_flatten(self) -> float | None:
        """Minutes until the session-boundary flatten fires (the judge's
        closing-guard clock, 2026-09-21). Same math as EngineCore's exit
        boundary: min(CLOSED boundary, daily RTH close) minus the flatten
        lead (exits.ExitParams.flatten_before_close_minutes — A2-7: read
        from the one source of truth, not a second hardcoded 10). Cached
        ~5s — it is asked once per position per second."""
        import time as _time

        cached = getattr(self, "_flatten_clock_cache", None)
        now_mono = _time.monotonic()
        if cached is not None and now_mono - cached[0] < 5.0:
            return cached[1]
        mins: float | None = None
        try:
            # lazy, cycle-safe: exits imports nothing from this module
            from waveapp.engine.exits import ExitParams
            from waveapp.engine.session import SessionScheduler

            # A2-7: the INJECTED clock, so replay/referee runs read the
            # replayed time instead of diverging onto the wall clock
            now = self._clock()
            boundary = SessionScheduler.next_closed_boundary(now)
            mins = (boundary - now).total_seconds() / 60.0
            rth = SessionScheduler.minutes_to_rth_close(now)
            if rth is not None:
                mins = min(mins, rth)
            mins -= float(ExitParams.flatten_before_close_minutes)
        except Exception:
            mins = None  # unknown clock = no guard, prior behavior
        self._flatten_clock_cache = (now_mono, mins)
        return mins

    @staticmethod
    def _atr_pct_for(sp: ShadowPosition, actor: Any) -> float:
        """Position-ATR as a % of price for the judge. The kitchen twin's
        MK engine measured it at adoption (engine.A — the long path,
        unchanged). An engine-less SHORT (S2) has no MK engine, so the
        actor's own exit engine is the source: atr (dollars at entry) over
        the entry price. 0.5% is the same last-resort fallback the long
        path's getattr default always had."""
        if sp.engine is not None:
            return float(getattr(sp.engine, "A", 0.5))
        ee = getattr(actor, "exit_engine", None)
        atr_dollars = float(getattr(ee, "atr", 0.0) or 0.0)
        entry = float(getattr(actor, "avg_entry_price", 0.0) or 0.0)
        if atr_dollars > 0 and entry > 0:
            return atr_dollars / entry * 100.0
        return 0.5

    def _judge_second(
        self, sp: ShadowPosition, actor: Any, t_ms: int, px: float, v60: float
    ) -> None:
        """One second of the Position Judge for one position (see
        waveapp/engine/position_judge.py). Stance lands in self.judge_stances
        for the card; flips are journaled; in judge_mode="drive" the acting
        stances (READY trigger / BANK / CUT) close the WHOLE position via the
        same managed exit path the kitchen drives."""
        try:
            from waveapp.engine import position_judge as PJ

            mode = getattr(self, "_judge_mode", None)
            if mode is None:
                from waveapp.config import AppConfig

                try:
                    mode = str(getattr(AppConfig.load(), "judge_mode", "shadow"))
                except Exception:
                    mode = "shadow"
                self._judge_mode = mode  # per-process; restart re-reads
            if mode == "off":
                return
            if not self._actor_alive(actor):
                # A5-7 (audit 2026-09-22): CLOSING/HALTED is not dead — the
                # card is still displayed. Judged-but-frozen: keep the last
                # stance (annotated) and judge nothing while the exit works.
                state_val = getattr(getattr(actor, "state", None), "value", "")
                if state_val in ("closing", "halted"):
                    self._freeze_stance(sp.position_key, state_val)
                    return
                # GHOST FIX (2026-09-22): the judge kept re-judging positions
                # AFTER their broker stop closed them (−40A flip noise all
                # morning). A dead (terminal) position gets no opinions.
                if hasattr(self, "judge_stances"):
                    self.judge_stances.pop(sp.position_key, None)
                return
            st = getattr(sp, "judge", None)
            if st is None:
                # A2-6: seed from the LIVE actor's fill when it exists — after
                # an MK rebuy rebind sp.engine.entry_price still holds the
                # ORIGINAL leg's entry (MasterKeyEngine._buy tracks rebuy legs
                # in _px_in and never rewrites entry_price), so the actor is
                # the truth. At first adoption both are the same number.
                entry = float(getattr(actor, "avg_entry_price", 0.0) or 0.0)
                if entry <= 0:
                    entry = float(getattr(sp.engine, "entry_price", 0.0) or 0.0)
                stop = float(getattr(actor.spec, "stop_price", 0.0) or 0.0)
                if entry <= 0:
                    return
                # S2: the judge is side-aware via its normalized frame — a
                # SELL actor's state is born side=-1 (JudgeState logs the
                # "short judge armed" line itself); stop stays the REAL
                # buy-stop above entry, the frame transforms it internally.
                side_val = str(
                    getattr(actor.spec.side, "value", getattr(actor.spec, "side", "buy"))
                ).lower()
                st = sp.judge = PJ.JudgeState(
                    entry_px=entry,
                    strategy_stop=stop,
                    atr_px=max(entry * self._atr_pct_for(sp, actor) / 100.0, 0.01),
                    symbol=sp.symbol,
                    side=1 if "buy" in side_val else -1,
                    stance_since_ms=t_ms,
                    # fast-fail age anchor (loss-side study 2026-09-25);
                    # 0 for adoptions older than the R1.5 entry_ms anchor —
                    # fast-fail then simply never applies to them
                    entry_t_ms=int(getattr(sp, "entry_ms", 0) or 0),
                )
            # JUDGE ADOPTION (2026-09-22: the judge adopts everything,
            # manual buys included): an adopted position carries its history —
            # the monitor computes the high since the original entry from the
            # chart backfill and leaves it on the actor (possibly a few
            # seconds AFTER this state was born, so this is a ratchet, not a
            # creation-time read). Without it a restart wiped the peak and
            # the give-back protection went blind.
            # S2: the seed is side-aware. A long's peak is the HIGH since
            # entry (judge_seed_peak); a short's is the LOW since entry
            # (judge_seed_trough — the monitor's mirror, see PLAN.md
            # HANDOFFS), reflected into the frame where it IS the peak. The
            # raw>0 guard matters for shorts: an absent seed (0.0) would
            # otherwise reflect to 2*entry and fake a giant frame peak.
            if st.side == 1:
                seed_raw = float(getattr(actor, "judge_seed_peak", 0.0) or 0.0)
                seed_t = int(getattr(actor, "judge_seed_peak_t_ms", 0) or 0)
                seed_v = float(getattr(actor, "judge_seed_peak_vol", 0.0) or 0.0)
            else:
                seed_raw = float(getattr(actor, "judge_seed_trough", 0.0) or 0.0)
                seed_t = int(getattr(actor, "judge_seed_trough_t_ms", 0) or 0)
                seed_v = float(getattr(actor, "judge_seed_trough_vol", 0.0) or 0.0)
            seed_peak = st._to_frame(seed_raw) if seed_raw > 0 else 0.0
            if seed_peak > st.peak_px:
                st.peak_px = seed_peak
                # the REAL peak time and volume (2026-09-22 close bug: aging
                # from the adoption moment + zero volume evidence kept every
                # adopted position "alive" while it bled into the flatten)
                st.peak_t_ms = seed_t or t_ms
                st.peak_vol60 = max(st.peak_vol60, seed_v)
            st.update_atr(max(px * self._atr_pct_for(sp, actor) / 100.0, 0.01))
            before = st.stance
            stance = PJ.judge_second(st, t_ms, px, v60, mins_to_flatten=self._mins_to_flatten())
            if not hasattr(self, "judge_stances"):
                self.judge_stances = {}
            self.judge_stances[sp.position_key] = (stance, st.last_reason)
            # R1.5 EFFICACY feed (2026-09-24, the −$3,928 morning): one
            # profit-vs-entry reading per judged second, in position-ATR
            # units in the judge's frame (shorts score like longs), plus the
            # age since the REAL fill. First-touch scoring (±1A inside the
            # 10-min window) lives in the monitor's tracker; a feed fault
            # can never wound the judge.
            if self._efficacy_cb is not None and sp.entry_ms > 0:
                try:
                    self._efficacy_cb(
                        sp.position_key,
                        (st._to_frame(px) - st.entry_px) / max(float(st.atr_px), 0.01),
                        t_ms - sp.entry_ms,
                    )
                except Exception:
                    logger.debug("efficacy feed failed", exc_info=True)
            # The judge is the only manager (2026-09-21): its MK ledger
            # holds cut fire permanently — WAIT/RIDE/READY are the judge's
            # to enforce; the broker hard stop stays the floor.
            mgr = str(getattr(getattr(actor, "spec", None), "manager", "judge") or "judge")
            if sp.engine is not None:  # hold_cuts is MK bookkeeping — shorts have no MK
                sp.engine.hold_cuts = mgr == "judge"
            if stance != before:
                self._journal(sp, {"event": "judge", "stance": stance, "why": st.last_reason})
                # M0: every flip becomes a judge_transitions row (+ forward
                # outcomes filled later) — observation only, failure-safe
                self._ml_transition(
                    sp,
                    st,
                    t_ms,
                    px,
                    v60,
                    before,
                    stance,
                    dwell_ms=int(getattr(st, "last_flip_dwell_ms", 0) or 0),
                )
            # A5-5 leg 1 (audit 2026-09-22): `sp.finished` no longer blocks
            # acting. _finish can fire while the REAL actor is still open
            # (brain-ledger flat != reality flat — e.g. the judge's own
            # optimistic force_flat after a close that FAILED, past
            # last_reentry_ms), and the judge is the sole manager of a
            # judge-side position: a live actor with no judge is a
            # management gap. The _actor_alive gates above and in the
            # acting block below already keep a truly dead position inert.
            if mgr != "judge" or mode != "drive":
                return  # kitchen twin: the judge only watches
            # A2-2 (2026-09-22): the act latch must not be a one-way door.
            # close_now can FAIL (the NVDG path: the broker refuses the exit
            # submit, the actor re-protects and returns to OPEN expecting the
            # per-second cadence to retry) — a permanently latched judge_acted
            # left such a position with no manager but the broker stop and
            # the 15:50 flatten. If the actor is still alive well past normal
            # fill latency, the act did not stick: re-arm and retry below.
            if (
                sp.judge_acted
                and t_ms - sp.judge_acted_t_ms > JUDGE_ACT_GRACE_MS
                and self._actor_alive(actor)
            ):
                sp.judge_acted = False
                logger.warning(
                    "JUDGE drive %s: act did not stick (actor still %s %.0fs later) — re-arming",
                    sp.symbol,
                    getattr(getattr(actor, "state", None), "value", "?"),
                    (t_ms - sp.judge_acted_t_ms) / 1000,
                )
            # acting stances (whole position, once per grace window) — the
            # broker hard stop stays resting underneath throughout (rule 3)
            act = None
            if stance == PJ.READY:
                # S2: ready_trigger_px is a REAL price, so the crossing test
                # is SIDE-AWARE — a long takes at/below its trigger (a
                # pullback off the high), a short takes at/above its trigger
                # (a rebound off the low). take_now maps to +inf / −inf
                # respectively: any print crosses, on either side.
                trig = PJ.ready_trigger_px(st)
                if (px <= trig) if st.side == 1 else (px >= trig):
                    act = "judge READY take"
            elif stance == PJ.BANK:
                act = "judge BANK stale profit"
            elif stance == PJ.CUT:
                # carry the REAL reason (2026-09-23: a bleeder cut labeled
                # "story broke" sent the owner hunting a phantom bug)
                act = f"judge CUT — {st.last_reason}" if st.last_reason else "judge CUT"
            if act and not sp.judge_acted and self._actor_alive(actor):
                try:
                    from waveapp.engine.exits import ExitAction, ExitDecision

                    sp.judge_acted = True
                    sp.judge_acted_t_ms = t_ms
                    logger.info("JUDGE drive %s: %s @ %.4f", sp.symbol, act, px)
                    self._journal(sp, {"event": "judge_act", "act": act, "t": t_ms, "px": px})
                    # M0: acts are journaled too — dwell here is flip→act
                    # (stance_since_ms is the moment the acting stance took over)
                    self._ml_transition(
                        sp,
                        st,
                        t_ms,
                        px,
                        v60,
                        stance,
                        f"ACT:{act}",
                        dwell_ms=max(t_ms - int(st.stance_since_ms or t_ms), 0),
                    )
                    # OPTIMISTIC: the MK ledger (bookkeeping only since
                    # 2026-09-21) flattens before reality confirms; if the
                    # exit fails the re-arm above retries, and force_flat is
                    # a no-op on an already-flat ledger. A short has no MK
                    # ledger to flatten (engine=None).
                    if sp.engine is not None:
                        sp.engine.force_flat(t_ms, px, why="judge")
                    # A5-6 handoff: strong-ref the exit task until it is done
                    # and surface its failure — the A2-2 grace re-arm retries
                    # the act, but a silent submit death must still be LOUD.
                    task = asyncio.ensure_future(
                        actor.apply_exit_decision(ExitDecision(ExitAction.EXIT_NOW, reason=act))
                    )
                    self._oneshot_tasks.add(task)

                    def _act_done(t: Any, sym: str = sp.symbol) -> None:
                        self._oneshot_tasks.discard(t)
                        if not t.cancelled() and t.exception() is not None:
                            logger.error("JUDGE drive %s: exit task died: %r", sym, t.exception())

                    task.add_done_callback(_act_done)
                except Exception:
                    # acting-path failures must be VISIBLE (A2-2) — acts are
                    # rare, so no rate limit; the outer swallow below stays
                    # at debug for per-second non-acting noise only.
                    logger.exception("JUDGE drive %s: acting path failed", sp.symbol)
        except Exception:
            # A5-5 leg 3 (audit 2026-09-22): this used to be a bare debug —
            # a judge bug presented as a stuck chip with ZERO log evidence.
            # Surface at exception level, once per JUDGE_ERR_LOG_SECONDS
            # (monotonic stamp on the kitchen); repeats inside the window
            # stay at debug so a per-second fault can't flood the log. The
            # acting block above logs its own failures at exception (A2-2).
            import time as _time

            now_mono = _time.monotonic()
            if now_mono - getattr(self, "_judge_err_mono", 0.0) >= JUDGE_ERR_LOG_SECONDS:
                self._judge_err_mono = now_mono
                logger.exception(
                    "judge second FAILED for %s (further repeats muted for %.0fs)",
                    sp.symbol,
                    JUDGE_ERR_LOG_SECONDS,
                )
            else:
                logger.debug("judge second skipped", exc_info=True)

    def _judge_latest_print(self, sp: ShadowPosition, actor: Any) -> None:
        """A5-5 leg 2 (audit 2026-09-22): a trade-second bar only CLOSES when
        the next second's first print arrives, so on a thin tape the newest
        bar sits open for minutes and the judge (stance, plus all its clocks:
        stall, bleeder, giveback aging) lags the market by exactly that gap —
        the SPAX laggy chip. When a tick drains nothing for this symbol,
        judge once anyway on the hub's freshest print at the CURRENT clock.
        At most once per wall second per position (the run loop polls at 4
        Hz), and never for a wall second a drained bar already judged.
        S2: engine=None (a judge-only short) is judged here too — the old
        engine-None early-return guarded the retired A2-5 short stub."""
        now_ms = _ms(self._clock())
        if now_ms - sp.judge_synth_ms < 1000:
            return
        if sp.last_t_ms and now_ms // 1000 <= sp.last_t_ms // 1000:
            return  # this second was already fed from a real bar
        px = 0.0
        hub = self._hub_getter()
        if hub is not None:
            try:
                px = float(getattr(hub, "latest_trade_px", {}).get(sp.symbol, 0.0) or 0.0)
            except Exception:
                px = 0.0
        if px <= 0:
            px = sp.last_price  # no print known to the hub — age on the last close
        if px <= 0:
            return
        # a zero-volume feed is the honest decayed 60s window; eviction is by
        # timestamp, so the next real bar's feed returns the identical sum it
        # would have anyway — the kitchen's own numbers are untouched. (A bar
        # for an OLDER second can still drain after this and judge with a
        # slightly regressed t_ms; dwell timers read that as a shorter dwell,
        # which only ever delays a flip — conservative, self-correcting.)
        v60 = sp.vol60.feed(now_ms, 0.0) if sp.vol60 is not None else 0.0
        sp.judge_synth_ms = now_ms
        self._judge_second(sp, actor, now_ms, px, v60)

    # -- M0 telemetry (ML master plan, 2026-09-23) — observation only --------

    def _ml_transition(
        self,
        sp: ShadowPosition,
        st: Any,
        t_ms: int,
        px: float,
        v60: float,
        from_stance: str,
        to_stance: str,
        dwell_ms: int | None,
    ) -> None:
        """One judge_transitions row per stance flip / act, through the
        monitor's db closure. profit/peak/giveback are FRAME-space (a short's
        tape reflected around its entry) so shorts compare like longs; px and
        entry_px stay REAL. `signs` is recomputed read-only from the same
        evidence judge_second used this second. Never wounds the judge."""
        cb = self._journal_db_cb
        if cb is None:
            return
        try:
            from waveapp.engine import position_judge as PJ

            atr = max(float(st.atr_px), 0.01)
            frame_px = st._to_frame(px)
            profit_atr = (frame_px - st.entry_px) / atr
            peak_profit_atr = (st.peak_px - st.entry_px) / atr
            signs = int(PJ._push_health(st, t_ms, v60) == "dying")
            signs += int(st.failed_highs >= PJ.FAILED_HIGH_COUNT)
            signs += int(t_ms - st.peak_t_ms >= PJ.PEAK_AGE_DEATH_S * 1000)
            regime = None
            try:
                from waveapp.engine.session import SessionScheduler

                regime = SessionScheduler.regime().value
            except Exception:
                regime = None
            row_id = cb(
                "transition",
                {
                    "ts": datetime.fromtimestamp(t_ms / 1000, tz=UTC).isoformat(
                        timespec="milliseconds"
                    ),
                    "symbol": sp.symbol,
                    "position_key": sp.position_key,
                    "side": "long" if st.side == 1 else "short",
                    "from_stance": from_stance,
                    "to_stance": to_stance,
                    "reason": st.last_reason or None,
                    "profit_atr": round(profit_atr, 4),
                    "peak_profit_atr": round(peak_profit_atr, 4),
                    "giveback_atr": round(peak_profit_atr - profit_atr, 4),
                    "signs": signs,
                    "failed_highs": int(st.failed_highs),
                    "peak_age_s": round((t_ms - st.peak_t_ms) / 1000.0, 1)
                    if st.peak_t_ms
                    else None,
                    "vol_ratio": round(v60 / st.peak_vol60, 4) if st.peak_vol60 > 0 else None,
                    "atr_px": round(atr, 6),
                    "px": px,
                    "entry_px": st.entry_px,
                    "close_guard": int(bool(st.close_guard)),
                    "dwell_ms": int(dwell_ms) if dwell_ms is not None else None,
                    "regime": regime,
                },
            )
            if row_id is not None:
                import time as _time

                self._ml_pending.append(
                    {
                        "row_id": row_id,
                        "symbol": sp.symbol,
                        "t0_ms": t_ms,
                        "side": int(st.side),
                        "entry_px": float(st.entry_px),
                        "atr_px": atr,
                        "px0": frame_px,
                        "mfe": frame_px,
                        "mae": frame_px,
                        "born_mono": _time.monotonic(),
                        "filled": set(),
                    }
                )
        except Exception:
            logger.debug("ml transition journal failed", exc_info=True)

    def _ml_outcomes(self) -> None:
        """Fill px_30s/px_2m/px_5m (+ 2m MFE/MAE) on pending transitions from
        the live tape, batched per tick. The honest rule: the tape must still
        be flowing (a live position's freshest bar, else the hub's freshest
        print at the wall clock) — a symbol that vanished leaves NULLs, and a
        row still incomplete after 20 wall-minutes is dropped as-is. MFE/MAE
        are sampled at tick cadence (~1s), frame-space vs the flip price."""
        if not self._ml_pending or self._journal_db_cb is None:
            return
        import time as _time

        now_mono = _time.monotonic()
        hub = self._hub_getter()
        keep: list[dict] = []
        for p in self._ml_pending:
            now_ms, px = 0, 0.0
            for sp in self.positions.values():
                if sp.symbol == p["symbol"] and sp.last_t_ms > now_ms and sp.last_price > 0:
                    now_ms, px = sp.last_t_ms, sp.last_price
            if px <= 0 and hub is not None:
                try:
                    px = float(getattr(hub, "latest_trade_px", {}).get(p["symbol"], 0.0) or 0.0)
                    now_ms = _ms(self._clock())
                except Exception:
                    px = 0.0
            try:
                if px > 0 and now_ms > p["t0_ms"]:
                    frame = px if p["side"] == 1 else 2.0 * p["entry_px"] - px
                    age = now_ms - p["t0_ms"]
                    if age <= 120_000:
                        p["mfe"] = max(p["mfe"], frame)
                        p["mae"] = min(p["mae"], frame)
                    if "30s" not in p["filled"] and age >= 30_000:
                        p["filled"].add("30s")
                        self._journal_db_cb("px30", {"id": p["row_id"], "v": round(frame, 6)})
                    if "2m" not in p["filled"] and age >= 120_000:
                        p["filled"].add("2m")
                        self._journal_db_cb(
                            "px2m",
                            {
                                "id": p["row_id"],
                                "v": round(frame, 6),
                                "mfe": round((p["mfe"] - p["px0"]) / p["atr_px"], 4),
                                "mae": round((p["mae"] - p["px0"]) / p["atr_px"], 4),
                            },
                        )
                    if "5m" not in p["filled"] and age >= 300_000:
                        p["filled"].add("5m")
                        self._journal_db_cb("px5m", {"id": p["row_id"], "v": round(frame, 6)})
            except Exception:
                logger.debug("ml outcome write failed", exc_info=True)
            if "5m" not in p["filled"] and now_mono - p["born_mono"] < 1200.0:
                keep.append(p)
        self._ml_pending = keep

    async def _process(
        self, sp: ShadowPosition, actor: Any, drained: dict[str, list] | None = None
    ) -> None:
        agg = self._aggs.get(sp.symbol)
        if agg is None:
            return
        if drained is not None:
            bars = drained.setdefault(sp.symbol, agg.drain())
        else:
            bars = agg.drain()
        if sp.finished or sp.engine is None:
            # A5-5 leg 1 (audit 2026-09-22): _finish used to freeze this
            # position entirely, but the brain ledger going flat is not the
            # same as REALITY going flat — the actor can still be open (a
            # judge act whose close failed, or a still-filling 15:50 exit).
            # The judge is the sole manager now, so a live actor keeps
            # being judged (and, in drive, acted on): kitchen ledger, exits
            # and stop-ratchet stay retired; only the judge slice runs.
            # tick() only routes finished positions here while the actor is
            # alive, and _judge_second pops the stance once it dies.
            # S2: an engine-less SHORT lives its WHOLE life in this slice —
            # judge-only from adoption (the MK brain is long-only), with
            # the broker bracket stop as the floor and the core's session
            # flatten as the boundary.
            for bar in bars:
                v60 = sp.vol60.feed(bar["t"], bar["v"]) if sp.vol60 is not None else 0.0
                sp.last_price, sp.last_t_ms = bar["c"], bar["t"]
                self._judge_second(sp, actor, bar["t"], bar["c"], v60)
            if not bars:
                self._judge_latest_print(sp, actor)
            if sp.engine is None and not sp.finished and sp.actor_gone:
                # a short with no actor left has nothing to judge and no
                # ledger to settle — retire it (pops any lingering chip)
                self._finish(sp)
            return
        # THE DUEL (2026-09-20): the kitchen drives ONLY its own twin.
        # A judge-managed position gets kitchen bookkeeping (bars, ledger,
        # journal) but no kitchen exits and no kitchen stop-ratchet — the
        # Position Judge is its sole manager; the broker bracket stop is
        # the floor under both twins.
        mgr = str(getattr(getattr(actor, "spec", None), "manager", "kitchen") or "kitchen")
        drive = self.mode == "drive" and mgr == "kitchen"
        for bar in bars:
            vwap = sp.vwap.feed(bar["t"], bar["h"], bar["l"], bar["c"], bar["v"])
            burst = sp.burst.feed(bar["t"], bar["v"])
            runner, proven = sp.regime.feed(bar["t"], vwap)
            v60 = sp.vol60.feed(bar["t"], bar["v"])
            watch_before = getattr(sp.engine, "_watch_t", None)
            fills = sp.engine.on_second(
                bar["t"], bar["c"], bar["h"], vwap, burst, runner, proven, vol60=v60
            )
            sp.last_price, sp.last_t_ms = bar["c"], bar["t"]
            # THE POSITION JUDGE (2026-09-20): re-judge this
            # position EVERY SECOND into a whole-position stance. Shadow by
            # default — display + journal only; drive arms via judge_mode
            # after the referee replay and the own config flip.
            self._judge_second(sp, actor, bar["t"], bar["c"], v60)
            # CLIMAX-WATCH ALERT (2026-09-16, the LITX autopsy: the hand
            # sell beat the brain's bank by 19s because he couldn't SEE the
            # countdown). The moment the watch arms, say so — log + phone —
            # so the human and the machine stop racing each other.
            watch_after = getattr(sp.engine, "_watch_t", None)
            if drive and watch_after is not None and watch_before is None:
                secs = sp.engine.p.confirm_s
                logger.info(
                    "MK drive %s: CLIMAX WATCH armed — banking into strength "
                    "in ~%ds unless a new high",
                    sp.symbol,
                    secs,
                )
                if self._notify is not None:
                    with contextlib.suppress(Exception):
                        self._notify(
                            f"⏳ {sp.symbol}: Wave sees the climax — banking in "
                            f"~{secs}s unless it keeps running. Hold your fire."
                        )
            for f in fills:
                logger.info(
                    "MK %s %s: %s @ %.4f x%.6g (%s) — brain pnl %+.0f$",
                    self.mode,
                    sp.symbol,
                    f.action,
                    f.price,
                    f.qty,
                    f.gear,
                    sp.engine.realized_pnl,
                )
                self._journal(
                    sp,
                    {
                        "event": "fill",
                        "t": f.t_ms,
                        "action": f.action,
                        "price": f.price,
                        "qty": f.qty,
                        "gear": f.gear,
                        "pnl_after": round(sp.engine.realized_pnl, 2),
                    },
                )
                if drive:
                    await self._drive_fill(sp, f, actor)
            if drive:
                await self._ratchet_protective_stop(sp, actor, vwap, bar["t"])
        if not bars:
            # A5-5 leg 2: thin tape — no trade-second closed this tick, so
            # the stance would otherwise sit frozen until the NEXT print
            self._judge_latest_print(sp, actor)
        # session-end flatten, same rule the lab lived under (in drive mode
        # the classic engine's session-boundary EXIT_NOW is the real backstop;
        # the ledger mirrors it here)
        # A2-8: flatten_ms == 0 = disabled for this position (adopted after
        # the mark — the core's session-boundary flatten owns the exit)
        if (
            not sp.finished
            and sp.flatten_ms > 0
            and sp.last_t_ms >= sp.flatten_ms
            and sp.engine.held > 0
        ):
            for f in sp.engine.flatten(sp.last_t_ms, sp.last_price):
                logger.info(
                    "MK %s %s: flatten @ %.4f — brain pnl %+.0f$",
                    self.mode,
                    sp.symbol,
                    f.price,
                    sp.engine.realized_pnl,
                )
            if drive and actor is not None and self._actor_alive(actor):
                from waveapp.engine.exits import ExitAction, ExitDecision

                await actor.apply_exit_decision(
                    ExitDecision(ExitAction.EXIT_NOW, reason="MK flatten (15:50)")
                )
            self._finish(sp)
        elif (
            not sp.finished
            and sp.engine.held <= 0
            and sp.last_t_ms > 0
            and sp.last_t_ms >= sp.engine.last_reentry_ms
        ):
            self._finish(sp)  # flat and no rebuys possible — the day is decided

    @staticmethod
    def _actor_alive(actor: Any) -> bool:
        return getattr(getattr(actor, "state", None), "value", "") in ("open", "scaling_out")

    def _freeze_stance(self, key: str, state_val: str) -> None:
        """A5-7 (audit 2026-09-22): a CLOSING/HALTED actor's card is still on
        screen — popping the chip there left a displayed position with no
        stance. Keep the last stance, annotated with the actor state, until
        the actor is truly terminal (closed/error), when the ghost-pop takes
        it. Idempotent: re-freezing replaces any earlier annotation."""
        stances = getattr(self, "judge_stances", None)
        cur = stances.get(key) if stances else None
        if cur is None:
            return
        stance, reason = cur
        for tag in (" (closing)", " (halted)"):
            if reason.endswith(tag):
                reason = reason[: -len(tag)]
        stances[key] = (stance, f"{reason} ({state_val})" if reason else f"({state_val})")

    async def _drive_fill(self, sp: ShadowPosition, f: Any, actor: Any) -> None:
        """Translate one brain fill into reality, through the actor/entry
        machinery. Reality can refuse (risk, pause, halt) — then the ledger
        is rolled back or force-synced, never left lying."""
        from waveapp.engine.exits import ExitAction, ExitDecision

        eng = sp.engine
        if f.action == "entry":
            return  # the real entry already exists — ledger bookkeeping only
        if f.action in ("rebuyB", "rebuyD"):
            ok = False
            if self._rebuy_cb is not None:
                stop_px = f.price * (1 - self.params.w * eng.A / 100)
                try:
                    ok = bool(await self._rebuy_cb(sp.symbol, f.price, stop_px, f.action, eng.qty0))
                except Exception:
                    logger.exception("MK rebuy callback failed for %s", sp.symbol)
            if ok:
                sp.pending_rebind = True  # bind to the new actor next tick
                self._journal(sp, {"event": "rebuy_submitted", "t": f.t_ms, "price": f.price})
            else:
                eng.undo_last_entry()
                self._journal(sp, {"event": "rebuy_blocked", "t": f.t_ms, "price": f.price})
                logger.info("MK rebuy %s blocked by reality — ledger rolled back", sp.symbol)
            return
        # exit fills
        if actor is None or not self._actor_alive(actor):
            return  # reality already flat (stop fired / halt) — synced in tick
        if eng.held > 0:  # partial: half, or the climax slice
            ee = actor.exit_engine
            real_held = float(ee.remaining_qty if ee is not None else (actor.filled_qty or 0.0))
            frac = f.qty / (eng.held + f.qty)
            real_qty = round(real_held * frac)
            if real_qty < 1 or real_qty >= real_held:
                # the real position is too small to slice — sell it all and
                # mirror that in the ledger so brain and reality agree
                await actor.apply_exit_decision(
                    ExitDecision(ExitAction.EXIT_NOW, reason=f"MK {f.action} (full, tiny rest)")
                )
                eng.force_flat(f.t_ms, f.price, why="tiny-rest")
                return
            protect = sp.last_stop_px or float(getattr(actor.spec, "stop_price", 0.0) or 0.0)
            if ee is not None:
                ee.remaining_qty = max(real_held - real_qty, 0.0)
                ee.pending_stop = protect
            await actor.apply_exit_decision(
                ExitDecision(
                    ExitAction.SCALE_OUT,
                    qty=float(real_qty),
                    new_stop=protect,
                    reason=f"MK {f.action}",
                )
            )
        else:
            await actor.apply_exit_decision(
                ExitDecision(ExitAction.EXIT_NOW, reason=f"MK {f.action}")
            )

    async def _ratchet_protective_stop(
        self, sp: ShadowPosition, actor: Any, vwap: float, t_ms: int
    ) -> None:
        """Keep the server-side stop just BELOW the brain's protect level.
        TIGHTEN ONLY — the resting stop can never be widened (hard rule 3).

        THE GAP (2026-09-09, the MVLL race): parking the stop exactly AT the
        brain's trigger made the broker stop and the brain's software exit
        fire on the same tick — the stop partially filled while close_now was
        canceling it, and the leftover shares sat stopless. The stop now sits
        stop_gap x A below the trigger: the brain's exit always fires first;
        the resting stop is the disaster net for crashes and dead software.
        """
        eng = sp.engine
        if eng.held <= 0 or actor is None or not self._actor_alive(actor) or vwap <= 0:
            return
        protect_stretch = eng._entry_stretch - self.params.w * eng.A
        if eng._armed:
            protect_stretch = max(protect_stretch, eng._anchor)
        stop_gap = 0.35 * eng.A  # the race-breaking daylight, in wiggle units
        protect_px = vwap * (1 + (protect_stretch - stop_gap) / 100)
        if protect_px <= sp.last_stop_px * 1.0005 or t_ms - sp.last_amend_ms < 20_000:
            return
        from waveapp.engine.exits import ExitAction, ExitDecision

        await actor.apply_exit_decision(
            ExitDecision(ExitAction.AMEND_STOP, new_stop=protect_px, reason="MK protect ratchet")
        )
        if actor.exit_engine is not None:
            actor.exit_engine.current_stop = protect_px
        sp.last_stop_px = protect_px
        sp.last_amend_ms = t_ms

    def _finish(self, sp: ShadowPosition) -> None:
        sp.finished = True
        # A5-5 leg 1 (audit 2026-09-22, the SPAX frozen chip): _process used
        # to stop for finished positions while the stance stayed in
        # judge_stances — the card showed a dead opinion forever. Drop it
        # here: if the real actor is somehow still alive, tick() keeps
        # judging it and a FRESH stance reappears within a second; if
        # reality is flat too, the card shows nothing rather than a lie.
        if hasattr(self, "judge_stances"):
            self.judge_stances.pop(sp.position_key, None)
        if sp.engine is None:  # S2: a judge-only short has no MK ledger to score
            self._journal(sp, {"event": "done"})
            logger.info("short judge %s DONE — position retired", sp.symbol)
            return
        self._journal(sp, {"event": "done", "shadow_pnl": round(sp.engine.realized_pnl, 2)})
        logger.info("MK shadow %s DONE — would-be pnl %+.0f$", sp.symbol, sp.engine.realized_pnl)

    async def tick(self) -> None:
        """One 1 Hz step — split out for tests."""
        self.ensure_tap()
        actors = self._actors_getter() or {}
        bound = {sp.position_key for sp in self.positions.values()}
        # symbols whose lineage is mid-rebuy: the fresh actor belongs to the
        # EXISTING brain — adopting it as a new one forks a duplicate brain
        # (the TH double-brain, 2026-09-09 10:14)
        rebinding = {
            sp.symbol for sp in self.positions.values() if not sp.finished and sp.pending_rebind
        }
        for key, actor in list(actors.items()):
            try:
                state = actor.state.value
            except Exception:  # noqa: S112 — a half-built actor just isn't ready yet
                continue
            if getattr(getattr(actor, "spec", None), "symbol", "") in rebinding:
                continue
            if state in ACTIVE_ACTOR_STATES and key not in self.positions and key not in bound:
                try:
                    self._adopt_actor(key, actor)
                except Exception:
                    logger.exception("MK adopt failed for %s", key)
        live_keys = {
            k
            for k, a in actors.items()
            if getattr(getattr(a, "state", None), "value", "") in ACTIVE_ACTOR_STATES
        }
        # DUEL B1 fix (rehearsal 2026-09-20 FATAL: aggregators are keyed by
        # SYMBOL while positions drain per POSITION — the first twin ate
        # every second-bar and the judge twin was never judged). Each tick
        # drains each symbol ONCE; both twins consume the same bars.
        drained_bars: dict[str, list] = {}
        for _key, sp in list(self.positions.items()):
            if sp.finished:
                # A5-5 leg 1: a finished brain whose REAL actor is still
                # alive (ledger-flat past last_reentry, or a still-working
                # 15:50 exit) keeps its judge — the sole manager now — via
                # _process's judge-only slice. S2: that slice also carries
                # engine-less shorts, so the gate is actor-liveness alone.
                fin_actor = actors.get(sp.position_key)
                fin_state = getattr(getattr(fin_actor, "state", None), "value", "")
                if self._actor_alive(fin_actor):
                    try:
                        await self._process(sp, fin_actor, drained_bars)
                    except Exception:
                        logger.exception("MK processing failed for %s", sp.symbol)
                elif fin_state in ("closing", "halted"):
                    # A5-7: the card is still shown while the exit works —
                    # judged-but-frozen, never a popped chip (see
                    # _freeze_stance); the pop below waits for terminal
                    self._freeze_stance(sp.position_key, fin_state)
                elif hasattr(self, "judge_stances"):
                    # reality flat too — the ghost-pop inside _judge_second
                    # is unreachable from here, so drop any lingering chip
                    self.judge_stances.pop(sp.position_key, None)
                continue
            # a successful rebuy created a fresh actor — rebind the lineage
            if sp.pending_rebind:
                for akey, a in actors.items():
                    if (
                        akey not in bound
                        and getattr(getattr(a, "spec", None), "symbol", "") == sp.symbol
                        and getattr(getattr(a, "state", None), "value", "")
                        in (*ACTIVE_ACTOR_STATES, "pending_entry")
                    ):
                        sp.position_key = akey
                        sp.pending_rebind = False
                        sp.actor_gone = False
                        sp.last_stop_px = float(getattr(a.spec, "stop_price", 0.0) or 0.0)
                        # A2-6 (audit 2026-09-22): the rebind keeps the same
                        # ShadowPosition, but the judge's evidence belongs to
                        # the DEAD leg — its entry/peak would read the rebuy
                        # as an instant deep giveback + aged peak (immediate
                        # take = churn), and a latched judge_acted would leave
                        # the new leg never acted on at all. Drop it all;
                        # _judge_second re-seeds from the NEW actor's fill
                        # price on its next judged second.
                        sp.judge = None
                        sp.judge_acted = False
                        sp.judge_acted_t_ms = 0
                        bound.add(akey)
                        self._journal(sp, {"event": "rebound", "actor": akey})
                        break
            actor = actors.get(sp.position_key)
            if sp.position_key not in live_keys and not sp.actor_gone and not sp.pending_rebind:
                sp.actor_gone = True
                self._journal(sp, {"event": "real_position_closed", "t": sp.last_t_ms})
                if self.mode == "drive" and sp.engine is not None and sp.engine.held > 0:
                    # reality flattened outside the brain (stop fired, manual
                    # close, halt) — mirror it; rebuy rules stay live
                    sp.engine.force_flat(
                        sp.last_t_ms or _ms(self._clock()), sp.last_price, why="reality"
                    )
            try:
                await self._process(sp, actor, drained_bars)
            except Exception:
                logger.exception("MK processing failed for %s", sp.symbol)
        # M0: forward-outcome fills for journaled judge transitions (cheap,
        # batched, observation only — a failure never touches trading)
        try:
            self._ml_outcomes()
        except Exception:
            logger.debug("ml outcomes pass failed", exc_info=True)
        # keep data flowing for brains that outlive their actor (rebuy watch)
        hub = self._hub_getter()
        stale = [sp.symbol for sp in self.positions.values() if not sp.finished and sp.actor_gone]
        if hub is not None and stale:
            try:
                await hub.watch(stale)
            except Exception:  # noqa: S110 — a failed re-watch retries next tick
                pass

    async def run(self) -> None:
        logger.info("Master Key kitchen running in %s mode (v14, %s)", self.mode, self.params)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("shadow kitchen tick failed")
                await asyncio.sleep(5)
            # 1.0 → 0.25 (2026-09-16, sell-latency fix):
            # today's exits measured a constant ~0.95s decision→submit gap —
            # pure poll lag. Bars still close on the same second boundaries
            # (lab parity untouched); decisions just act the moment a bar
            # exists instead of up to a second later. AXON's floor exit paid
            # ~55¢/share for that second on a falling tape.
            await asyncio.sleep(0.25)
