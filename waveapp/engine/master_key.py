"""THE MASTER KEY (v14) — the three-gear VWAP-stretch exit brain.

Born in the GRIDY lab (research/gridy/master_key14.py) from the $2,108
wound day (2026-09-02) and hardened on all 12 recorded live days:
lab scoreboard +10,695 vs the installed kitchen's +3,200 (leave-one-day-out
+10,345). This module is the PRODUCTION port: the exact same state machine,
plus the input trackers (session VWAP, volume burst, trend persistence) that
the live shadow and the research harness BOTH use — so parity between lab
and app is enforced by construction, and verified by
research/gridy/mk_prod_check.py which must reproduce the lab numbers to the
dollar before any install.

Long positions only (all recorded evidence is long; shorts log a skip).
This engine never places orders itself — it emits decisions. In shadow mode
the decisions are logged and scored; only a approved future phase may
route them to a broker.

All thresholds are in "stretch" units: percent distance from session VWAP,
scaled by the stock's own pre-entry 1-minute ATR% (floored at a_min).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MasterKeyParams:
    """The frozen v14 champion (chosen config: persist 30m/70%, grinder 8A).

    Every value was fixed by the 12-day lab sweep on 2026-09-08. Changing any
    of them invalidates the recorded evidence — do it only through a new lab
    campaign + parity check.
    """

    a: float = 1.0  # arm at anchor + a*A
    keep_r: float = 0.6  # wavy-gear trail keep fraction (runner)
    keep_s: float = 0.4  # chop-gear trail keep fraction
    w: float = 1.5  # wrong-stop: entry_stretch - w*A
    g: float = 2.5  # giveback cap, not riding (A units)
    g_ride: float = 6.0  # giveback cap while riding (A units)
    g_grind: float = 8.0  # grinder-gear wide leash (A units)
    bb: float = 0.6  # breakout-rebuy buffer (A units)
    n_entries: int = 6  # total entry budget (first + rebuys)
    cooldown_s: int = 180  # seconds after an exit before any rebuy
    slope_min: int = 20  # VWAP slope lookback (minutes)
    slope_eps: float = 0.05  # VWAP rise (%) to call "runner" this second
    frac: float = 0.7  # wavy-gear trail slice
    strikes_max: int = 3  # wrong/dead exits allowed before no rebuys
    dead_min: int = 30  # dead-cut: unarmed & under VWAP this long
    a_min: float = 0.45  # floor for the ATR%-unit (quiet stocks)
    ratchet: float = 2.0  # banked slice lifts anchor to S - ratchet*A
    burst_k: float = 3.0  # climax: 15s volume >= burst_k x 5m pace
    confirm_s: int = 30  # climax: no new high within this window
    near_eps: float = 0.6  # climax: within near_eps*A of the max
    climax_min: float = 3.0  # climax only on a big fish (A units)
    frac_c: float = 0.5  # climax banks this slice, rides the rest
    persist_min: int = 30  # persistence window (minutes)
    persist_frac: float = 0.7  # runner-time fraction to call "proven"
    cost_pct: float = 0.03  # modeled cost per fill side (%)
    # v16 POP LOCK (2026-09-09, the rule, referee +11,314 vs +10,694):
    # grinder gear only — a fast pop whose volume DIES gets its floor lifted
    # to just under the top; a living pop (EIX) is untouched.
    pop_atr: float = 2.5  # POP: max_stretch rose this many A within the window
    pop_window_s: int = 300
    die_frac: float = 0.3  # DIED: last-60s volume < die_frac x volume at the max
    lock_atr: float = 1.0  # LOCK: anchor lifts to max_stretch - lock_atr*A
    # DEMAND 21 fail-safe (2026-09-16, referee15 SHIP-CANDIDATE at −$81/15d,
    # worst day unchanged, TENB rider survives): a stretched entry's wrong-cut
    # is STRETCH-anchored, so when VWAP falls WITH price (TEM, −$661) the
    # stretch cushion holds while dollars bleed. This cut is PRICE-anchored:
    # entered >= failsafe_x*A above VWAP, still above VWAP, and within the
    # first failsafe_n_min minutes price closes failsafe_k*A% below entry ->
    # exit at the close. failsafe_x=0 disarms.
    failsafe_x: float = 3.0
    failsafe_n_min: int = 30
    failsafe_k: float = 1.5


DEFAULT_PARAMS = MasterKeyParams()


class VwapTracker:
    """Session VWAP from the 09:30 ET open, typical-price weighted.

    Seed with minute bars for time before per-second data begins (exactly the
    lab's construction), then feed per-second bars. Bars before rth_open_ms
    are ignored.
    """

    def __init__(self, rth_open_ms: int) -> None:
        self.rth_open_ms = rth_open_ms
        self._pv = 0.0
        self._vv = 0.0

    def feed(self, t_ms: int, high: float, low: float, close: float, volume: float) -> float:
        if t_ms >= self.rth_open_ms:
            tp = (high + low + close) / 3
            self._pv += tp * volume
            self._vv += volume
        return self._pv / self._vv if self._vv else close


class BurstTracker:
    """15s volume vs the trailing 5-minute pace (excluding the 15s window)."""

    def __init__(self) -> None:
        self._w5: list[tuple[int, float]] = []
        self._w15: list[tuple[int, float]] = []
        self._sum5 = 0.0
        self._sum15 = 0.0

    def feed(self, t_ms: int, volume: float) -> float:
        self._w5.append((t_ms, volume))
        self._w15.append((t_ms, volume))
        self._sum5 += volume
        self._sum15 += volume
        while self._w5 and self._w5[0][0] < t_ms - 300_000:
            self._sum5 -= self._w5.pop(0)[1]
        while self._w15 and self._w15[0][0] < t_ms - 15_000:
            self._sum15 -= self._w15.pop(0)[1]
        span5 = max((t_ms - self._w5[0][0]) / 1000, 1)
        pace = (self._sum5 - self._sum15) / max(span5 - 15, 30)
        return (self._sum15 / 15) / pace if pace > 0 else 0.0


class Vol60Tracker:
    """Rolling last-60s volume (inclusive of the current second) — the lab's
    exact construction (research/gridy/master_key16.py)."""

    def __init__(self) -> None:
        self._w: list[tuple[int, float]] = []
        self._sum = 0.0

    def feed(self, t_ms: int, volume: float) -> float:
        self._w.append((t_ms, volume))
        self._sum += volume
        while self._w and self._w[0][0] < t_ms - 60_000:
            self._sum -= self._w.pop(0)[1]
        return self._sum


class RegimeTracker:
    """Runner flag (VWAP slope over slope_min) + rolling persistence fraction."""

    def __init__(self, params: MasterKeyParams = DEFAULT_PARAMS) -> None:
        self.p = params
        self._vwap_hist: list[tuple[int, float]] = []
        self._flags: list[tuple[int, int]] = []
        self._flag_sum = 0

    def feed(self, t_ms: int, vwap: float) -> tuple[bool, bool]:
        """Returns (runner_now, proven_trend)."""
        self._vwap_hist.append((t_ms, vwap))
        cutoff = t_ms - self.p.slope_min * 60_000
        v_then = None
        while len(self._vwap_hist) > 1 and self._vwap_hist[1][0] <= cutoff:
            self._vwap_hist.pop(0)
        if self._vwap_hist[0][0] <= cutoff:
            v_then = self._vwap_hist[0][1]
        runner = v_then is not None and (vwap - v_then) / v_then * 100 >= self.p.slope_eps
        flag = 1 if runner else 0
        self._flags.append((t_ms, flag))
        self._flag_sum += flag
        while self._flags and self._flags[0][0] < t_ms - self.p.persist_min * 60_000:
            self._flag_sum -= self._flags.pop(0)[1]
        n = len(self._flags)
        proven = (self._flag_sum / n if n else 0.0) >= self.p.persist_frac
        return runner, proven


@dataclass
class MasterKeyFill:
    t_ms: int
    action: str  # entry | rebuyB | rebuyD | wrong | dead | floor | trail
    # | half | giveback | climax | flatten
    price: float
    qty: float
    gear: str  # chop | wavy | grinder


@dataclass
class MasterKeyState:
    held: float = 0.0
    realized_pnl: float = 0.0
    entries_used: int = 0
    strikes: int = 0
    fills: list[MasterKeyFill] = field(default_factory=list)


class MasterKeyEngine:
    """Per-position v14 state machine. Feed one second-bar at a time.

    The math and ORDER OF OPERATIONS mirror research/gridy/master_key14.py
    replay14() exactly — mk_prod_check.py proves it to the dollar.
    """

    def __init__(
        self,
        entry_price: float,
        qty: float,
        atr_pct: float,
        opened_ms: int,
        last_reentry_ms: int,
        params: MasterKeyParams = DEFAULT_PARAMS,
    ) -> None:
        self.p = params
        self.entry_price = entry_price
        self.qty0 = qty
        self.A = max(atr_pct, params.a_min)
        self.opened_ms = opened_ms
        self.last_reentry_ms = last_reentry_ms
        self.s = MasterKeyState()
        self._entered_once = False
        self._entries_left = params.n_entries
        self._was_below = False
        self._last_exit_px: float | None = None
        self._last_exit_t = 0
        self._px_in = 0.0
        self._fs_hot = False  # DEMAND 21: armed once at a FRESH entry only
        # THE JUDGE'S WAIT (2026-09-20): while the Position Judge
        # reads "losing but the story is intact", the character-blind
        # wrong-cut and fail-safe HOLD THEIR FIRE (the 09-17 INTW cut,
        # −$583, fired 47¢ above the strategy's own stop on a weak-volume
        # dip). The BROKER HARD STOP and the session flatten are untouched
        # — hard rule 3 is the floor under every stance.
        self.hold_cuts = False
        self._entry_stretch = 0.0
        self._anchor = 0.0
        self._max_stretch = 0.0
        self._armed = False
        self._riding = False
        self._entered_t = 0
        self._watch_t: int | None = None
        self._watch_max = 0.0
        self._hist: list[tuple[int, float]] = []  # (t_ms, max_stretch), per holding
        self._hp = 0
        self._vol_at_max = 0.0

    # -- internals ----------------------------------------------------------
    def _sell(self, t_ms: int, px: float, frac: float, tag: str, gear: str) -> None:
        q = self.s.held if frac >= 1.0 else max(self.s.held * frac, 0.0)
        self.s.realized_pnl += (px - self._px_in) * q - px * q * self.p.cost_pct / 100
        self.s.held -= q
        self.s.fills.append(MasterKeyFill(t_ms, tag, px, q, gear))

    def _buy(self, t_ms: int, px: float, tag: str, gear: str, first: bool) -> None:
        if not first:
            self.s.realized_pnl -= px * self.qty0 * self.p.cost_pct / 100
        self.s.held = self.qty0
        self._entered_once = True
        self._entries_left -= 1
        self.s.entries_used += 1
        self._px_in = self.entry_price if first else px
        self.s.fills.append(
            MasterKeyFill(t_ms, "entry" if first else tag, self._px_in, self.qty0, gear)
        )

    @staticmethod
    def _level_px(vwap: float, trig_stretch: float, close: float, high: float) -> float:
        lp = vwap * (1 + trig_stretch / 100)
        return min(max(lp, close), high)

    # -- the state machine --------------------------------------------------
    def on_second(
        self,
        t_ms: int,
        close: float,
        high: float,
        vwap: float,
        burst_ratio: float,
        runner: bool,
        proven: bool,
        vol60: float = 0.0,
    ) -> list[MasterKeyFill]:
        """Advance one second. Returns fills emitted THIS second."""
        if t_ms < self.opened_ms:
            return []
        p = self.p
        A = self.A
        n_before = len(self.s.fills)
        stretch = (close - vwap) / vwap * 100
        gear = "grinder" if proven else ("wavy" if runner else "chop")

        if self.s.held <= 0:
            first = not self._entered_once
            cooled = t_ms >= self._last_exit_t + p.cooldown_s * 1000
            rebuy_d = self._was_below and stretch >= 0.0
            rebuy_b = self._last_exit_px is not None and close > self._last_exit_px * (
                1 + p.bb * A / 100
            )
            if first or (
                runner
                and proven
                and self.s.strikes < p.strikes_max
                and self._entries_left > 0
                and cooled
                and t_ms <= self.last_reentry_ms
                and (rebuy_d or rebuy_b)
            ):
                self._buy(t_ms, close, "rebuyB" if rebuy_b else "rebuyD", gear, first)
                self._entry_stretch = stretch
                self._anchor = max(self._entry_stretch, 0.0)
                self._max_stretch = stretch
                self._armed = False
                self._riding = False
                self._watch_t = None
                self._entered_t = t_ms
                self._hist = [(t_ms, self._max_stretch)]
                self._hp = 0
                self._vol_at_max = vol60
                # DEMAND 21 arming (lab: night_studies_0915 replay_n line
                # 163 — per-entry flag). The freshness clause is the
                # adoption guard (audit 2026-09-16): a restart-ADOPTED
                # first buy fires long after the true open and must not
                # open a fresh 30-min cut against a stale entry price.
                fresh = (not first) or (t_ms - self.opened_ms <= 120_000)
                self._fs_hot = (
                    p.failsafe_x > 0 and fresh and self._entry_stretch >= p.failsafe_x * A
                )
            else:
                if stretch < 0.0:
                    self._was_below = True
            return self.s.fills[n_before:]

        new_max = stretch > self._max_stretch
        self._max_stretch = max(self._max_stretch, stretch)
        if new_max:
            self._vol_at_max = vol60
        self._hist.append((t_ms, self._max_stretch))
        while self._hp + 1 < len(self._hist) and (
            self._hist[self._hp + 1][0] <= t_ms - p.pop_window_s * 1000
        ):
            self._hp += 1
        big_fish = (self._max_stretch - self._anchor) >= p.climax_min * A
        if new_max:
            self._watch_t = None
        if (
            self._armed
            and big_fish
            and self._watch_t is None
            and burst_ratio >= p.burst_k
            and stretch >= self._max_stretch - p.near_eps * A
        ):
            self._watch_t = t_ms
            self._watch_max = self._max_stretch
        if (
            self._watch_t is not None
            and t_ms - self._watch_t >= p.confirm_s * 1000
            and self._max_stretch <= self._watch_max
        ):
            self._sell(t_ms, close, p.frac_c, "climax", gear)
            self._riding = True
            self._anchor = max(self._anchor, stretch - p.ratchet * A)
            self._watch_t = None
            return self.s.fills[n_before:]

        if stretch <= self._entry_stretch - p.w * A and not self.hold_cuts:
            px = self._level_px(vwap, self._entry_stretch - p.w * A, close, high)
            self._sell(t_ms, px, 1.0, "wrong", gear)
            self.s.strikes += 1
            self._riding = False
            self._watch_t = None
            self._was_below = False
            # lab parity: the rebuy-breakout reference is the bar CLOSE, not
            # the fill level (research/gridy/master_key14.py behavior)
            self._last_exit_px, self._last_exit_t = close, t_ms
            return self.s.fills[n_before:]

        # DEMAND 21 price-anchored fail-safe — LAB ORDER (night_studies_0915
        # replay_n: AFTER the wrong-cut; the 2026-09-16 audit caught prod
        # running it first and booking worse exits on both-true ticks). The
        # TEM blind zone: VWAP falling WITH price keeps the stretch cushion
        # while dollars bleed and the wrong-cut above never fires.
        if (
            self._fs_hot
            and not self.hold_cuts
            and t_ms - self._entered_t <= p.failsafe_n_min * 60_000
            and stretch > 0.0
            and close <= self._px_in * (1 - p.failsafe_k * A / 100)
        ):
            self._sell(t_ms, close, 1.0, "failsafe", gear)
            self.s.strikes += 1
            self._riding = False
            self._watch_t = None
            self._was_below = False
            self._last_exit_px, self._last_exit_t = close, t_ms
            return self.s.fills[n_before:]

        if not self._armed:
            if self._max_stretch >= self._anchor + p.a * A:
                self._armed = True
            elif stretch < 0 and t_ms - self._entered_t >= p.dead_min * 60_000:
                self._sell(t_ms, close, 1.0, "dead", gear)
                self.s.strikes += 1
                self._riding = False
                self._was_below = False
                self._last_exit_px, self._last_exit_t = close, t_ms
                return self.s.fills[n_before:]

        if self._armed:
            full_exit = partial = False
            trig: float | None = None
            tag = ""
            grind = proven
            # v16 POP LOCK: fast pop + volume death -> lift the floor now
            if grind and self._vol_at_max > 0:
                old_max = self._hist[self._hp][1]
                popped = (self._max_stretch - old_max) >= p.pop_atr * A
                died = vol60 < p.die_frac * self._vol_at_max
                if popped and died:
                    self._anchor = max(self._anchor, self._max_stretch - p.lock_atr * A)
            g_eff = p.g_grind if grind else (p.g_ride if self._riding else p.g)
            if stretch <= self._anchor:
                full_exit, tag, trig = True, "floor", self._anchor
            elif stretch <= self._max_stretch - g_eff * A:
                full_exit, tag, trig = True, "giveback", self._max_stretch - g_eff * A
            elif not grind:
                tl = self._anchor + (self._max_stretch - self._anchor) * (
                    p.keep_r if runner else p.keep_s
                )
                if not self._riding and stretch <= tl:
                    if runner:
                        partial, tag, trig = True, "half", tl
                    else:
                        full_exit, tag, trig = True, "trail", tl
            if partial:
                px = self._level_px(vwap, trig, close, high)
                self._sell(t_ms, px, p.frac, tag, gear)
                self._riding = True
                self._anchor = max(self._anchor, stretch - p.ratchet * A)
            elif full_exit:
                px = self._level_px(vwap, trig, close, high)
                self._sell(t_ms, px, 1.0, tag, gear)
                self._riding = False
                self._watch_t = None
                self._was_below = False
                # lab parity: rebuy reference = bar close (see wrong-stop note)
                self._last_exit_px, self._last_exit_t = close, t_ms
        return self.s.fills[n_before:]

    def undo_last_entry(self) -> None:
        """Drive mode: reality refused a rebuy (risk halt, pause, sizing) —
        reverse the ledger's last entry so brain and reality stay in sync.
        The entry budget is refunded; strikes are untouched."""
        if not self.s.fills or self.s.fills[-1].action not in ("entry", "rebuyB", "rebuyD"):
            return
        fill = self.s.fills.pop()
        if fill.action != "entry":
            self.s.realized_pnl += fill.price * self.qty0 * self.p.cost_pct / 100
        self.s.held = 0.0
        self._entries_left += 1
        self.s.entries_used -= 1
        if self.s.entries_used == 0:
            self._entered_once = False

    def force_flat(self, t_ms: int, close: float, why: str = "external") -> list[MasterKeyFill]:
        """Drive mode: something outside the brain flattened reality (halt
        exit, manual close, kill switch) — mirror it in the ledger. Rebuy
        rules stay live; whether a rebuy can execute is reality's call."""
        n_before = len(self.s.fills)
        if self.s.held > 0:
            self._sell(t_ms, close, 1.0, f"ext:{why}", "chop")
            self._riding = False
            self._watch_t = None
            self._was_below = False
            self._last_exit_px, self._last_exit_t = close, t_ms
        return self.s.fills[n_before:]

    def flatten(self, t_ms: int, close: float) -> list[MasterKeyFill]:
        """Session-end flatten of whatever is still held."""
        n_before = len(self.s.fills)
        if self.s.held > 0:
            self._sell(t_ms, close, 1.0, "flatten", "chop")
        return self.s.fills[n_before:]

    @property
    def realized_pnl(self) -> float:
        return self.s.realized_pnl

    @property
    def held(self) -> float:
        return self.s.held


def atr1m_pct(minute_bars: list[dict], opened_ms: int, rth_open_ms: int) -> float:
    """Pre-entry 1-min ATR% — the lab's exact construction (mean 30-min TR%)."""
    bars = [
        b
        for b in minute_bars
        if opened_ms - 30 * 60_000 <= b["t"] < opened_ms and b["t"] >= rth_open_ms
    ]
    if not bars:
        return 0.5
    trs = [(b["h"] - b["l"]) / b["c"] * 100 for b in bars if b["c"]]
    return max(sum(trs) / len(trs), 0.05) if trs else 0.5
