"""AlpacaFeatureProvider (Phase 7): builds SymbolFeatures for the scan set
from Alpaca REST — multi-symbol snapshots (latest quote/trade, today's daily
bar, previous daily bar) plus a rolling 20-day daily-bar window for average
volume and daily ATR. Batched to respect rate limits.

The scan set: tradable assets on the main exchanges, narrowed to the most
active names by average dollar volume (config `scan_universe_size`). The full
~14k universe is journaled over time by rotating scan sets in later phases;
websockets stay reserved for positions/watchlist.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

from waveapp.broker.base import AssetInfo
from waveapp.engine.scanner import SymbolFeatures
from waveapp.engine.session import ET
from waveapp.instruments import is_leveraged_name as _is_leveraged
from waveapp.security import secrets

logger = logging.getLogger("wave.scanner.features")

SNAPSHOT_BATCH = 100
DAILY_BARS_BATCH = 200  # 1000-symbol requests hit Alpaca 504s (2026-08-14)
DAILY_BARS_DAYS = 30  # calendar days fetched to get ~20 trading days
REQUEST_TIMEOUT_SECONDS = 60.0  # a hung call must never freeze the scan loop


def session_elapsed_fraction(now_utc: datetime | None = None) -> float:
    """Fraction of the regular session elapsed (for time-adjusted RVOL);
    clamped to [0.05, 1.0] so early-morning volume doesn't divide by ~zero."""
    now = (now_utc or datetime.now(UTC)).astimezone(ET)
    open_t, close_t = time(9, 30), time(16, 0)
    if now.time() <= open_t:
        return 0.05
    if now.time() >= close_t:
        return 1.0
    elapsed = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds()
    return max(0.05, min(1.0, elapsed / (6.5 * 3600)))


class AlpacaFeatureProvider:
    def __init__(
        self,
        assets: list[AssetInfo],
        universe_size: int = 150,
        progress_cb=None,  # Callable[[str], None] — UI status line
        progress_units_cb=None,  # Callable[[int, int, str], None] — (done, total, label)
        cache_path=None,  # same-day universe cache (skips the ~6-min rebuild)
        feed: str = "iex",  # Phase 10.1: "sip" = full-market volumes
    ) -> None:
        from waveapp.broker.alpaca import KEYCHAIN_PAPER_KEY_ID, KEYCHAIN_PAPER_SECRET

        key_id = secrets.get_secret(KEYCHAIN_PAPER_KEY_ID)
        secret = secrets.get_secret(KEYCHAIN_PAPER_SECRET)
        if not key_id or not secret:
            raise RuntimeError("Alpaca keys missing — cannot build feature provider")
        from alpaca.data.historical import StockHistoricalDataClient

        self._client = StockHistoricalDataClient(key_id, secret)
        from alpaca.data.enums import DataFeed

        self.feed = feed if feed in ("iex", "sip") else "iex"
        self._data_feed = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
        # §7 average-volume floor, venue-scaled: IEX prints ~2-3% of market
        # volume (15k IEX ≈ ~500k-1M real); SIP carries REAL volumes.
        self._volume_floor = 500_000 if self.feed == "sip" else 15_000
        self._assets = {
            a.symbol: a for a in assets if a.tradable and a.symbol.isalpha() and len(a.symbol) <= 5
        }
        self.universe_size = universe_size
        self._progress = progress_cb or (lambda text: None)
        self._progress_units = progress_units_cb or (lambda done, total, label: None)
        from waveapp.config import support_dir

        self._cache_path = cache_path or (support_dir() / "universe_cache.json")
        self._scan_set: list[str] = []
        self._rebuild_task: asyncio.Task | None = None  # strong ref (GC!)
        self._avg_volume: dict[str, float] = {}
        self._daily_atr_pct: dict[str, float] = {}
        # A4-12 (audit 2026-09-22): keyed by the ET trading day (same string
        # _trading_day() returns), NEVER by the UTC date — the UTC date rolls
        # at 20:00/19:00 ET, which forced one redundant cache reload every
        # evening and then pre-stamped TOMORROW's day on yesterday's ranking.
        self._universe_day: str | None = None
        # SCANNER UNLEASHED (2026-09-02): same-day GUESTS — scanner2
        # menu names outside the trailing-dollar-volume scan set (§ guest
        # methods below). The entry pipeline can only trade what fetch()
        # features, and the trailing ranking structurally excludes the very
        # names that explode on fresh news (TARS/MLYS/VRNS on the menu all
        # afternoon, never a candidates row).
        self._guests: set[str] = set()
        self._guest_pending: set[str] = set()
        self._guest_day: str | None = None
        self._guest_task: asyncio.Task | None = None  # strong ref (GC!)

    # -- same-day cache ------------------------------------------------------

    @staticmethod
    def _trading_day() -> str:
        """ET calendar date — the UTC date flipped at 20:00 ET and forced a
        full nightly rebuild on every evening restart (2026-08-20)."""
        from zoneinfo import ZoneInfo

        return datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date().isoformat()

    def _load_cache(self, allow_stale: bool = False) -> str | None:
        """Returns "fresh", "stale" (yesterday's ranking, usable NOW while a
        rebuild runs in the background), or None."""
        import json

        try:
            if not self._cache_path.exists():
                return None
            data = json.loads(self._cache_path.read_text())
            if data.get("feed", "iex") != self.feed:
                return None  # universe built on the other venue's volumes
            # A4-12: _save_cache stamps the ET trading day, so freshness is
            # judged against that alone (the old UTC-date alternative was the
            # same evening-rollover confusion this fix removes)
            fresh = data.get("date") == self._trading_day()
            if not fresh and not allow_stale:
                return None
            self._scan_set = list(data["scan_set"])[: self.universe_size]
            self._avg_volume = {k: float(v) for k, v in data["avg_volume"].items()}
            self._daily_atr_pct = {k: float(v) for k, v in data["atr_pct"].items()}
            if fresh:
                self._universe_day = self._trading_day()
                logger.info("universe loaded from same-day cache (%d symbols)", len(self._scan_set))
                return "fresh"
            logger.info(
                "STALE universe cache active (%d symbols) — scanning NOW, rebuilding in background",
                len(self._scan_set),
            )
            return "stale"
        except Exception:
            logger.exception("universe cache load failed — rebuilding")
            return None

    def _save_cache(self) -> None:
        import json

        try:
            self._cache_path.write_text(
                json.dumps(
                    {
                        "date": self._trading_day(),
                        "feed": self.feed,
                        "scan_set": self._scan_set,
                        "avg_volume": self._avg_volume,
                        "atr_pct": self._daily_atr_pct,
                    }
                )
            )
        except Exception:
            logger.exception("universe cache save failed")

    # -- daily universe refresh ---------------------------------------------

    async def _refresh_universe(self) -> None:
        """Rank all assets by average dollar volume using daily bars; keep the
        most active `universe_size` names as today's scan set."""
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if self._load_cache() == "fresh":
            self._progress_units(1, 1, f"scan set ready — {len(self._scan_set)} symbols (cache)")
            return

        symbols = sorted(self._assets)
        start = datetime.now(UTC) - timedelta(days=DAILY_BARS_DAYS)
        volumes: dict[str, float] = {}
        atrs: dict[str, float] = {}

        chunks = [
            symbols[i : i + DAILY_BARS_BATCH] for i in range(0, len(symbols), DAILY_BARS_BATCH)
        ]
        total_chunks = len(chunks)
        failed_chunks = 0
        done_chunks = 0
        semaphore = asyncio.Semaphore(4)  # 4-wide: ~4× faster, well under rate limits

        async def fetch_chunk(chunk: list[str]):
            request = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start,
                adjustment=Adjustment.SPLIT,
                feed=self._data_feed,
            )
            async with semaphore:
                for attempt in (1, 2):  # one retry (transient 5xx / hang)
                    try:
                        # hard timeout: a hung HTTP call must never freeze
                        # the scan loop forever (observed 2026-08-14)
                        return await asyncio.wait_for(
                            asyncio.to_thread(self._client.get_stock_bars, request),
                            timeout=REQUEST_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        if attempt == 2:
                            return None
                        await asyncio.sleep(2)

        tasks = [asyncio.ensure_future(fetch_chunk(chunk)) for chunk in chunks]
        all_series: dict = {}
        for task in asyncio.as_completed(tasks):
            bars = await task
            done_chunks += 1
            self._progress_units(
                done_chunks,
                total_chunks,
                f"building today's scan set — batch {done_chunks}/{total_chunks}",
            )
            if done_chunks % 10 == 1:
                logger.info(
                    "downloading market history — batch %d/%d (%s symbols)",
                    done_chunks,
                    total_chunks,
                    f"{len(symbols):,}",
                )
            if bars is None:
                failed_chunks += 1
                continue
            all_series.update(bars.data)
        for symbol, series in all_series.items():
            if len(series) < 5:
                continue
            closes = [b.close for b in series]
            vols = [b.volume for b in series]
            trs = [
                max(b.high - b.low, abs(b.high - pc), abs(b.low - pc))
                for b, pc in zip(series[1:], closes[:-1], strict=True)
            ]
            avg_volume = sum(vols) / len(vols)
            price = closes[-1]
            # (A 200k floor against IEX volume wrongly cut the pool to
            # ~700 names — 2026-08-14; floor is venue-scaled since 10.1.)
            if price < 3.0 or avg_volume < self._volume_floor:
                continue  # price/volume floors (§7, venue-scaled)
            volumes[symbol] = avg_volume * price  # dollar volume
            self._avg_volume[symbol] = avg_volume
            atrs[symbol] = (sum(trs) / len(trs)) / price * 100 if trs else 0.0

        self._daily_atr_pct.update(atrs)
        new_set = sorted(
            volumes, key=volumes.get, reverse=True
        )[  # type: ignore[arg-type]
            : self.universe_size
        ]
        # NEVER SHRINK TO NOTHING (2026-09-17, the empty-open bug: an
        # overnight rebuild got empty/failed data, replaced the 1500-name
        # scan set with [], and every retry rebuilt empty again — Wave
        # reached 9:06 with ZERO scannable stocks until a manual restart
        # reloaded the cache). A collapsed build keeps the old set and
        # retries; only a plausible build may replace it or touch the cache.
        floor = max(50, self.universe_size // 4)
        if len(new_set) < floor and len(self._scan_set) >= len(new_set):
            logger.error(
                "universe refresh produced only %d names (floor %d) — KEEPING the "
                "previous %d-name scan set; will retry next cycle",
                len(new_set),
                floor,
                len(self._scan_set),
            )
            return
        self._scan_set = new_set
        self._universe_day = self._trading_day()  # A4-12: ET day, never UTC
        self._save_cache()
        self._progress("universe ready — scanning…")
        self._progress_units(1, 1, f"scan set ready — {len(self._scan_set)} symbols")
        if failed_chunks:
            logger.error(
                "universe refresh incomplete: %d chunk(s) failed — scan set may be partial",
                failed_chunks,
            )
        logger.info(
            "universe refreshed: %d assets → scan set of %d", len(volumes), len(self._scan_set)
        )

    # -- FeatureProvider ------------------------------------------------------

    # -- same-day guests (SCANNER UNLEASHED, 2026-09-02) ---------------------

    MAX_GUESTS_PER_DAY = 200

    def add_guests(self, symbols: list[str]) -> None:
        """Admit scanner2 menu names OUTSIDE the scan set as same-day guests.
        Each guest gets a one-off daily-stats fetch (avg volume + daily ATR)
        and joins fetch() only once its stats exist — never with garbage
        zeros. Guests reset with the trading day; the scan set itself is
        untouched (the trailing-volume universe stays the backbone)."""
        today = self._trading_day()
        if self._guest_day != today:
            self._guest_day = today
            self._guests.clear()
            self._guest_pending.clear()
        if len(self._guests) + len(self._guest_pending) >= self.MAX_GUESTS_PER_DAY:
            return
        known = set(self._scan_set) | self._guests | self._guest_pending
        fresh = [s for s in symbols if s not in known and s in self._assets]
        if not fresh:
            return
        self._guest_pending.update(fresh[: self.MAX_GUESTS_PER_DAY])
        if self._guest_task is None or self._guest_task.done():
            self._guest_task = asyncio.ensure_future(self._admit_guests())

    async def _admit_guests(self) -> None:
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        while self._guest_pending:
            batch = sorted(self._guest_pending)[:DAILY_BARS_BATCH]
            self._guest_pending -= set(batch)
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=datetime.now(UTC) - timedelta(days=DAILY_BARS_DAYS),
                adjustment=Adjustment.SPLIT,
                feed=self._data_feed,
            )
            try:
                bars = await asyncio.wait_for(
                    asyncio.to_thread(self._client.get_stock_bars, request),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.exception("guest stats fetch failed (%d names)", len(batch))
                continue
            admitted = []
            for symbol, series in bars.data.items():
                if len(series) < 5:
                    continue
                closes = [b.close for b in series]
                vols = [b.volume for b in series]
                trs = [
                    max(b.high - b.low, abs(b.high - pc), abs(b.low - pc))
                    for b, pc in zip(series[1:], closes[:-1], strict=True)
                ]
                avg_volume = sum(vols) / len(vols)
                price = closes[-1]
                # guests skip the trailing-volume floor ON PURPOSE — today's
                # volume is the whole point; a light 10% floor + the price
                # floor keep total junk out (the $15 entry floor and the
                # TradeGate spread math still stand downstream)
                if price < 3.0 or avg_volume < self._volume_floor * 0.1:
                    continue
                self._avg_volume[symbol] = avg_volume
                self._daily_atr_pct[symbol] = (sum(trs) / len(trs)) / price * 100 if trs else 0.0
                self._guests.add(symbol)
                admitted.append(symbol)
            if admitted:
                logger.info("scan-set guests admitted: %s", ", ".join(admitted))

    async def fetch(self) -> list[SymbolFeatures]:
        # A4-12: the day key is the ET trading day on BOTH sides, so the
        # 20:00-ET UTC rollover no longer triggers an evening reload (and
        # the stale-while-revalidate path below stays exactly as it was)
        if self._universe_day != self._trading_day() or not self._scan_set:
            # stale-while-revalidate (2026-08-20: "5 min and still
            # nothing"): yesterday's ranking is ~99% of today's — scan with
            # it IMMEDIATELY and swap in the fresh build when it lands
            if self._scan_set or self._load_cache(allow_stale=True) == "stale":
                if self._rebuild_task is None or self._rebuild_task.done():
                    self._rebuild_task = asyncio.ensure_future(self._refresh_universe())
            else:
                await self._refresh_universe()  # no cache at all: first run

        from alpaca.data.requests import StockSnapshotRequest

        elapsed = session_elapsed_fraction()
        features: list[SymbolFeatures] = []
        scan_list = self._scan_set + sorted(
            self._guests - set(self._scan_set) if self._guest_day == self._trading_day() else set()
        )
        for i in range(0, len(scan_list), SNAPSHOT_BATCH):
            chunk = scan_list[i : i + SNAPSHOT_BATCH]
            try:
                snapshots = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.get_stock_snapshot,
                        StockSnapshotRequest(symbol_or_symbols=chunk, feed=self._data_feed),
                    ),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.exception("snapshot fetch failed for a chunk")
                continue
            for symbol, snap in snapshots.items():
                feature = self._to_features(symbol, snap, elapsed)
                if feature is not None:
                    features.append(feature)
        return features

    def _to_features(self, symbol: str, snap, elapsed: float) -> SymbolFeatures | None:
        try:
            daily = snap.daily_bar
            prev = snap.previous_daily_bar
            quote = snap.latest_quote
            # STALE-BAR GUARD ported from scanner2 (audit 2026-09-16 F3:
            # this provider feeds ACTUAL TRADES and had no timestamp check —
            # a recycled ticker with live quotes got a phantom gap/rvol and
            # a real GAP entry on fictional features)
            import time as _sb_time

            _sb_now = _sb_time.time()
            _dts = getattr(daily, "timestamp", None) if daily is not None else None
            # 6 days, not 24h/4d (2026-09-21 MONDAY BUG: at the open every
            # daily bar is still FRIDAY'S — the 24h guard nulled the entire
            # scan set and the market opened to 0 features. 4d then failed
            # the HOLIDAY variants — audit 2026-09-22 A4-3/A3-4: daily bars
            # are timestamped midnight ET of their trading date, so at a
            # 9:28 Tuesday scan after a Monday holiday Friday's bar is
            # 4d 9.5h ≈ 4.4d old → 4d nulled every symbol again. Holiday
            # math: plain weekend ≈ 3.4d; long weekend (Mon or Fri holiday)
            # ≈ 4.4d; worst realistic — two weekday closures fused to a
            # weekend (Sandy Mon+Tue 2012 style, Wed-bar → Mon-open) ≈ 5.4d.
            # 6d covers all of those with ~0.6d margin while staying orders
            # of magnitude below the YEARS-stale AT phantom this guards
            # against. A full-week market halt is not worth guarding.)
            if _dts is not None and _sb_now - _dts.timestamp() > 6 * 86_400:
                daily = None  # not a recent session's bar
            _pts = getattr(prev, "timestamp", None) if prev is not None else None
            # prev is TWO sessions back pre-open: long weekend ≈ 5.4d,
            # double-closure worst case ≈ 6.4d — 7d still clears both.
            if _pts is not None and _sb_now - _pts.timestamp() > 7 * 86_400:
                prev = None  # dead/recycled listing
            if daily is None or prev is None or not prev.close:
                return None
            price = float((snap.latest_trade.price if snap.latest_trade else None) or daily.close)
            spread = 0.0
            if quote is not None and quote.ask_price and quote.bid_price:
                spread = max(float(quote.ask_price) - float(quote.bid_price), 0.0)
            avg_volume = self._avg_volume.get(symbol, 0.0)
            rvol = float(daily.volume) / (avg_volume * max(elapsed, 0.05)) if avg_volume else 0.0
            asset = self._assets.get(symbol)
            return SymbolFeatures(
                symbol=symbol,
                price=price,
                prev_close=float(prev.close),
                gap_pct=(float(daily.open) - float(prev.close)) / float(prev.close) * 100,
                day_open=float(daily.open),
                rvol=rvol,
                atr_pct=self._daily_atr_pct.get(symbol, 0.0),
                spread=spread,
                day_volume=float(daily.volume),
                avg_daily_volume=avg_volume,
                shortable=bool(asset.shortable) if asset else False,
                # M0: ETB rides along from the same cached asset — no new call
                easy_to_borrow=bool(asset.easy_to_borrow) if asset else None,
                overnight_eligible=bool(asset.overnight_eligible) if asset else False,
                leveraged=_is_leveraged(getattr(asset, "name", None)),
            )
        except Exception:
            logger.exception("feature build failed for %s", symbol)
            return None
