"""App configuration (SPEC.md §4).

`~/Library/Application Support/Wave/config.toml` holds settings and Keychain
entry *names* only — never secrets. Reading is tolerant: a missing file yields
defaults; unknown keys are preserved on save.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("wave.config")

APP_NAME = "Wave"
BUNDLE_ID = "com.yuval.wave"
KEYCHAIN_SERVICE = "Wave"


def support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_NAME


def config_path() -> Path:
    return support_dir() / "config.toml"


def log_dir() -> Path:
    return support_dir() / "logs"


def _toml_string(value: str) -> str:
    """Render a Python str as a valid TOML basic string.

    A4-5(b): the old serializer emitted f'"{value}"' verbatim, so a value
    containing '"' or '\\' saved fine but made every LATER load raise
    TOMLDecodeError. Escape backslash, quote and control characters per the
    TOML basic-string grammar (no new dependency — this is the whole set)."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


@dataclass
class AppConfig:
    """Phase 1 settings. Later phases extend this; secrets never live here."""

    # Keychain entry names (the entries themselves live in macOS Keychain)
    keychain_password_hash_entry: str = "app_password_hash"  # noqa: S105 — entry NAME, not a secret

    # Login behavior (no lockout — removed Phase 8.2)
    touch_id_enabled: bool = True

    # UI
    window_width: int = 1280
    window_height: int = 800

    # Data (Phase 3)
    watchlist: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "AAPL", "TSLA", "NVDA"])

    # Appearance: Apple liquid-glass materials (set false to force solid theme)
    glass_enabled: bool = True

    # Telegram (Phase 4): the owner's numeric user id; 0 = bridge disabled.
    # The bot token lives in Keychain (entry `telegram_bot_token`), never here.
    telegram_user_id: int = 0

    # Market data tier (Phase 10.1): "iex" (free, ~30 channel subs) or "sip"
    # (paid Algo Trader Plus — full feed, big subscription budget). Wave
    # falls back to IEX behavior if the account lacks the subscription.
    data_feed: str = "iex"

    # Scanner (Phase 7). 400 → 1500 on 2026-08-20 (a way larger pool
    # finds more of the day's opportunities) — 15 snapshot batches per scan
    # cycle, well inside the 120s interval; TradeGate selectivity unchanged.
    scan_universe_size: int = 1500
    scan_interval_seconds: int = 120
    auto_trade: bool = False  # entries from strategies — enabled in Phase 10

    # Master Key kitchen (promoted from shadow to drive, 2026-09-09):
    # which brain manages exits.
    #   "master_key" — the v14 three-gear VWAP brain drives real exits and
    #                  rebuys (rebuys still pass RiskEngine sizing + the
    #                  running/auto_trade gates; server-side stop always on)
    #   "classic"    — the old 7-layer kitchen drives; the Master Key only
    #                  shadows (logs would-be decisions)
    # Lab evidence for master_key: +10,695 vs classic's +3,200 across all 12
    # recorded days (parity-proven port, research/gridy/mk_prod_check.py).
    kitchen: str = "master_key"

    # The Master Key observer itself (journal + wave.trade.shadow log lines);
    # in "classic" mode it only watches, in "master_key" mode it drives.
    shadow_kitchen: bool = True

    # CHURN GUARD (2026-09-09, TYRA x4 = -$279: the scanner's re-signaling
    # assumed the old 90-minute kitchen; the Master Key cuts in seconds and
    # the scanner re-bought the same knife four times in an hour). Scanner
    # entries only — the brain's own rebuys keep their stricter gates.
    churn_guard: bool = True
    # INVERSE-ETF BAN (2026-09-14 — the 9/4 autopsy: the worst
    # day's poison was long entries on inverse/bear ETFs; the 13-day referee
    # scores the ban +$827 with trend days untouched). Long entries on
    # inverse/bear products are refused at the entry gate.
    inverse_etf_ban: bool = True
    symbol_reentry_cooldown_min: int = 15  # no scanner re-entry this long after a stop-out
    symbol_max_entries_per_day: int = 3  # scanner entries per symbol per day

    # Scanner 2.0 (2026-08-31, adopted): full-market scanner — watches
    # every tradable asset, re-ranks every minute, journals everything.
    scanner2_shadow: bool = True  # run the full-market scanner at all
    # LIVE menu duty (2026-08-31 night): scanner2's fresh menu IS the day list; the old
    # 9:28 list is the automatic fallback and the journaled shadow.
    scanner2_live: bool = True

    # Risk limits (Phase 8.10 — editable behind Touch ID; §12 defaults).
    # The engine builds its RiskLimits from these at startup and live-applies
    # edits; hard rule 2 still holds — checks can be TIGHTENED, never removed.
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_weekly_loss_pct: float = 6.0
    # 3 → 6 (2026-08-25, "apply all of this"): the exit-style × book
    # sweep — 6 slots OOS +$53,877 / DD −$5,010 vs 3 slots' +$42,361 / −$7,343
    # (more, smaller positions = no single loser can wreck the day); 9 slots
    # added nothing over 6. Full stack with PMOM: OOS +$58,454 / PF 1.86.
    max_positions: int = 6
    # WAVE 2 item 11 (adopted 2026-09-16): beyond max_positions,
    # entries are admitted while total open risk-to-stop stays under this
    # ceiling (floor-locked winners risk zero); hard count cap for sanity.
    max_total_risk_pct: float = 6.0
    max_positions_hard: int = 12
    max_notional_pct: float = 25.0  # per-position notional cap (% of equity)
    # Champion price floor. $10 (2026-08-19) → $15 (decision,
    # 2026-08-24, after the first red day): the $15 sim ran OOS +$58.5k vs
    # $10's +$19.4k, and the live $10–15 bucket kept underdelivering.
    min_entry_price: float = 15.0
    # market-impact participation cap (2026-08-21): per-trade shares ≤ this
    # % of the symbol's average daily volume — Wave must never be the move
    impact_participation_pct: float = 0.5

    # PMOM opening-auction entries (the strategy, 2026-08-25): pre-market
    # ramp names queued as limit-on-open orders before the bell so the fill
    # IS the 9:30:00 opening print. Kill switch, not a tuning knob.
    pmom_auction: bool = True

    # Dead-man's switch (2026-08-26, for school days): Wave
    # pings this URL every 60s; an external watchdog (healthchecks.io with a
    # Telegram integration) alerts when the pings STOP — the only way to hear
    # "Wave has no wifi", because a wifi-less Wave can't message anyone.
    # Empty = off.
    heartbeat_url: str = ""

    # Backup bookkeeping (Settings rework 2026-08-21): ISO-UTC stamp of the
    # last successful export, shown in the Backup section. Blank = never.
    last_backup_at: str = ""

    # ML mode (agenda #3 redo, 2026-08-23 — manual switch):
    # "off" (DEFAULT — nothing runs), "shadow" = gather/label data only,
    # never touching trading; "active" = ML gates entries — refused in code
    # until a model passes the §9.2/§11 bar AND flipped manually.
    ml_mode: str = "off"
    # Which artifact the SHADOW brain slot loads (ML MASTER PLAN M1,
    # 2026-09-23). "v1" (DEFAULT) → brain_v1.joblib, unchanged behavior.
    # Any other value "<name>" → brain_<name>.joblib in the support dir
    # (e.g. "v2.0-2026-09-23" for the M1 door-brain artifact); a missing or
    # refused artifact falls back to v1 with a warning. Whatever loads is
    # STILL shadow-only — this field never makes ML touch trading.
    ml_model: str = "v1"

    # Performance graph epoch (2026-08-19): the Reset button stamps
    # this ISO-UTC moment; trades/cashflows before it are HIDDEN from the
    # Performance tab, never deleted (orders/fills/positions stay in the DB —
    # they're the §10.3 dataset). Blank = show everything.
    performance_epoch: str = ""
    # layer 7 news brain (2026-09-02): the LLM provider exposes no balance
    # API for regular keys, so Wave tracks its OWN spend against the funded
    # budget and shuts the layer off at the cap.
    llm_budget: float = 25.0  # funded budget for the news-brain account
    llm_spent_total: float = 0.0  # lifetime spend, persisted across restarts
    # 6.5 entry ladder (ADOPTED 2026-09-02, 4/4 both fill models): entries
    # post at the quote mid and fall back to MARKET on timeout. The
    # kill switch — False restores pre-adoption behavior without a rebuild.
    entry_ladder: bool = True

    # EVENT PROMOTER (adopted 2026-09-16, "build the promoter"): a
    # vol_spike/hod_break on a tradable unwatched name puts it under watch
    # + guest scan the SAME MINUTE (study: late-admitted names +$35.5/case
    # vs menu +$25.2; median lag was 35-48 min). Gates unchanged. False
    # disarms without a rebuild.
    event_promoter: bool = True
    # FPB GAPPER WATCH challenger (2026-09-16 study: a third of FPB paydays
    # were on unwatched symbols, +$195/day): top-5 pre-market ramps join
    # the watch at 9:38 so the first-pullback engine sees its own setups.
    fpb_gapper_watch: bool = True

    # YUVAL-BRAIN GATE (armed 2026-09-16): the advisor may veto
    # EXTENDED entries (>= yb_gate_stretch_pct above the day's open) on a
    # skip verdict at >= yb_gate_confidence. Tightening-only; False disarms.
    yuval_brain_gate: bool = True
    yb_gate_stretch_pct: float = 4.0
    yb_gate_confidence: float = 0.8
    yb_gate_timeout_s: float = 10.0

    # CLIMBER LANE knobs (2026-09-16: "aim for lower profit midday —
    # with 13000 stocks we can still make profit"; not holding positions IS
    # losing money). The lane sweeps the full scanner2 universe for names
    # climbing off their OPEN, below yb_gate veto land. Live-read on every
    # sweep AND by the VWAP climber gate — tune without a restart.
    climber_day_pct: float = 1.2
    climber_max_day_pct: float = 4.0
    climber_rvol: float = 1.2
    climber_limit: int = 8

    # DEMAND 23 late-ORB gate (referee15: Y=6 half −$110/15d and Y=8 half
    # −$48/15d are both SHIP-CANDIDATEs; DLLL — the motivating −$398 — was
    # 7.4% above open, so 6.0 is the setting that catches its class). An ORB
    # signal fired >= this % above the day's open rides HALF size. 0 disarms.
    orb_late_gate_pct: float = 6.0

    # DEMAND 21 fail-safe knobs (kitchen reads them at construction — a
    # change needs a Wave restart, not a rebuild). failsafe_x = 0 disarms.
    failsafe_x: float = 3.0
    failsafe_n_min: int = 30
    failsafe_k: float = 1.5

    # THE POSITION JUDGE (2026-09-20): every position
    # re-judged EVERY SECOND into a whole-position stance (RIDE / READY /
    # WAIT / BANK / CUT). "shadow" = judge + card + journal, act on nothing;
    # "drive" = stances act (arm ONLY after the referee replay); "off".
    judge_mode: str = "shadow"

    # THE SHORT SIDE — S0 master flag (2026-09-23: "slowly build the
    # short without destroying the long"). EVERY short-side behavior gates
    # on this; False (the default) keeps Wave bit-identical to the long-only
    # app. flipped manually at S5, after the full mirror + §11
    # evidence — never earlier, never in code.
    shorts_enabled: bool = False

    # CHOP-DAY SIGNAL INVERSION (2026-09-23, after the −$854
    # chop day: "if today we bought the longs as shorts and the shorts as
    # longs we would have made money"). On a DayJudge CHOP verdict at
    # confidence >= CHOP_INVERT_MIN_CONF (0.6, connection_monitor), Wave
    # flips its OWN momentum signals at the same trigger — shorts the failed
    # breakout, buys the failed breakdown — half size, stop mirrored around
    # the entry. Every gate still applies for the NEW side (shorts_enabled,
    # SSR, ETB/borrow, cost gate, judge, brain, churn, risk) EXCEPT the
    # trend gate: on a called chop day the fade IS the strategy. Never
    # touches climber-lane or FPB pullback entries. False (the default)
    # keeps the pipeline byte-identical; flipped manually.
    chop_invert: bool = False
    # Bad-detector-day damage cap: at most this many OPENED inverted entries
    # per ET day; past the cap signals pass through uninverted (baseline).
    chop_invert_max_per_day: int = 6

    # THE EFFICACY GUARD (R1.5, the #1 priority after the −$3,928 morning of
    # 2026-09-24 — the worst day ever: two full-size losers inside 25 min of
    # the open, 18 losers total, and NOTHING watched whether Wave's own
    # entries were following through). Every fill is scored (+1A before −1A
    # within 10 min = PASS, mirror/cut = FAIL) and the whole signal book
    # flips between MOMENTUM (as-is) and INVERTED (every momentum signal
    # rides the shared invert transform) on that evidence — 2 consecutive
    # fails or a 60% fail rate flips, symmetrically both ways, day-reset to
    # MOMENTUM. Default ON; False restores the
    # byte-identical baseline pipeline. The hard 3% daily halt (§12) sits
    # untouched above all of this.
    efficacy_guard: bool = True
    # directive (2026-09-24): Wave must MAKE money, not trade
    # smaller — efficacy-triggered inversions ride FULL size (the invert
    # transform's forced half-size stays for CHOP-triggered flips only).
    efficacy_invert_full_size: bool = True

    # PRE-OPEN BIAS BRAIN (R0.5, 2026-09-24 — the front half the efficacy
    # guard was missing: know what to trade FROM THE FIRST TRADE). One
    # verdict ~9:25 ET from pre-market evidence (SPY/QQQ gap vs prior close
    # + the watched-universe gap map): LONG_BIAS / SHORT_BIAS / NEUTRAL.
    # While it governs (9:30 until the Day Judge's first real verdict), the
    # biased-against side is held to a stricter standard — under SHORT bias
    # a momentum LONG must be GREEN vs its own prior close (never a hard
    # block: a monster gapper bucking the tape passes); the bias also seeds
    # the efficacy tracker (first counter-bias fail flips after 1, not 2).
    # HONESTY (pre-open study, 15 sessions): the rule went
    # 0/4 on directional calls — it ships CONSERVATIVE (strict AND rule,
    # silent most days) and the seed is the main teeth. False = the 9:25
    # computation, push, journal and routing all vanish (byte-identical).
    preopen_bias: bool = True

    # THE SNIPER BOOK (R2, 2026-09-24 — the validation
    # study: the two proven edges replayed over 310 real trades turn
    # +$853 into +$5,995; green days 11/22 → 16/22; worst day −$3,933 →
    # −$2,497). Four legs, ordinary momentum signals ONLY (inverted signals,
    # climber and FPB lanes exempt — their own contracts):
    #   1. FPB conversion — a signal ≥2% extended from the open becomes a
    #      20-min pullback watch (the study's tested rule) instead of a
    #      chase; no holding pullback = no trade (the proven skip).
    #   2. Jewel sizing — GAP BUY <1% above open rides 1.5× risk (caps
    #      still bind on the scaled qty).
    #   3. Pocket starves — ORB long on TREND_DOWN (14% WR), any ordinary
    #      momentum entry in the 14:00-14:59 ET hour (19% WR), ORB <1%-ext
    #      on CHOP (31% WR).
    #   4. Journaling — every conversion/trigger/expiry/starve logs; FPB
    #      entries reason-prefixed "FPB:" / "FPB-short:".
    # Default ON per decision; False restores the byte-identical
    # baseline pipeline. Honesty: the replay is an in-sample counterfactual
    # — this ships as the live challenger book, §11 still owns adoption.
    sniper_book: bool = True

    # TELEGRAM MODE (2026-09-20): "on" = everything; "minimum" =
    # only fills, banks, risk halts and the day summary; "off" = no pushes.
    # Commands (/status /positions /pnl) ALWAYS answer in every mode.
    # Replaces the older binary mute.
    telegram_mode: str = "minimum"

    # Anything from the file we don't model yet — preserved on save.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        path = path or config_path()
        if not path.exists():
            return cls()
        try:
            with path.open("rb") as f:
                raw = tomllib.load(f)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            # A4-5(c): a torn/corrupt config.toml (crash mid-write before the
            # atomic-save fix, or external corruption) must never brick startup
            # — the app.py call site is unguarded, so load() itself recovers.
            # Recovery order: a valid `.tmp` sibling if one exists (an
            # interrupted atomic save holds the freshest complete snapshot),
            # else pure defaults. The broken file is deliberately NOT
            # overwritten here — it stays on disk for forensics; the next
            # legitimate save() replaces it atomically.
            logger.error("config.toml is corrupt, recovering (%s): %s", path, exc)
            raw = None
            tmp = path.with_name(path.name + ".tmp")
            if tmp.exists():
                try:
                    with tmp.open("rb") as f:
                        raw = tomllib.load(f)
                    logger.error("config recovered from atomic-save sibling %s", tmp)
                except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
                    raw = None
            if raw is None:
                logger.error("no valid .tmp sibling — falling back to defaults")
                return cls()
        known = {f_.name for f_ in cls.__dataclass_fields__.values()} - {"extra"}
        kwargs = {k: v for k, v in raw.items() if k in known}
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(**kwargs, extra=extra)

    def save_window_geometry(self, width: int, height: int, path: Path | None = None) -> None:
        """Persist ONLY the window size: re-reads the file first so settings
        changed outside this process (CLI edits, other tools) are never
        clobbered by a stale in-memory snapshot on app close."""
        fresh = AppConfig.load(path)
        fresh.window_width = width
        fresh.window_height = height
        fresh.save(path)

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        items: dict[str, Any] = {
            k: getattr(self, k) for k in self.__dataclass_fields__ if k != "extra"
        }
        items.update(self.extra)
        lines = []
        for key, value in items.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int | float):
                rendered = str(value)
            elif isinstance(value, list | tuple):
                rendered = "[" + ", ".join(_toml_string(str(item)) for item in value) + "]"
            else:
                rendered = _toml_string(str(value))
            lines.append(f"{key} = {rendered}")
        # A4-5(a): atomic write. The app saves as often as every 30s; a plain
        # write_text() truncates in place, and a crash/power-cut mid-write
        # leaves torn TOML — an external fresh-load→save then persists DEFAULTS
        # for every missing field (the exact telegram_mode→"minimum" revert
        # signature). Write the full text to a sibling and os.replace() it in:
        # atomic on APFS, so config.toml is always either the old or the new
        # complete file, never a torn one.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
