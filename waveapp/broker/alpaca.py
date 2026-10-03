"""AlpacaAdapter (§3, Phase 2) — the active broker, paper endpoints
only until the Phase 11 gate (the base class enforces this at construction).

alpaca-py's TradingClient is synchronous, so calls run in a worker thread via
asyncio.to_thread; TradingStream is async and feeds `trade_updates()` through
an internal queue. Keys come from Keychain (entry names in config), never from
files or environment variables.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from waveapp.broker.base import (
    AccountSnapshot,
    AssetInfo,
    BrokerAdapter,
    BrokerError,
    MarketClock,
    MissingCredentialsError,
    OrderInfo,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionInfo,
    TradeUpdate,
    TradingMode,
)
from waveapp.security import secrets

logger = logging.getLogger("wave.broker.alpaca")

KEYCHAIN_PAPER_KEY_ID = "alpaca_paper_key_id"
KEYCHAIN_PAPER_SECRET = "alpaca_paper_secret"  # noqa: S105 — Keychain entry NAME


def _to_order_side(value: Any) -> OrderSide:
    raw = str(getattr(value, "value", value)).lower()
    return OrderSide.SELL if raw == "sell" else OrderSide.BUY


def _to_order_type(value: Any) -> OrderType:
    raw = str(getattr(value, "value", value)).lower()
    try:
        return OrderType(raw)
    except ValueError:
        return OrderType.MARKET


def _to_status(value: Any) -> OrderStatus:
    raw = str(getattr(value, "value", value)).lower()
    try:
        return OrderStatus(raw)
    except ValueError:
        return OrderStatus.OTHER


def map_order(raw: Any) -> OrderInfo:
    """Map an alpaca-py order object to Wave's OrderInfo."""
    legs = tuple(map_order(leg) for leg in (getattr(raw, "legs", None) or []))
    return OrderInfo(
        order_id=str(raw.id),
        client_order_id=str(getattr(raw, "client_order_id", "") or ""),
        symbol=str(raw.symbol),
        side=_to_order_side(raw.side),
        qty=float(raw.qty or 0),
        filled_qty=float(getattr(raw, "filled_qty", 0) or 0),
        order_type=_to_order_type(getattr(raw, "order_type", None) or getattr(raw, "type", "")),
        status=_to_status(raw.status),
        limit_price=float(raw.limit_price) if getattr(raw, "limit_price", None) else None,
        stop_price=float(raw.stop_price) if getattr(raw, "stop_price", None) else None,
        filled_avg_price=(
            float(raw.filled_avg_price) if getattr(raw, "filled_avg_price", None) else None
        ),
        submitted_at=getattr(raw, "submitted_at", None),
        filled_at=getattr(raw, "filled_at", None),
        legs=legs,
    )


def map_asset(raw: Any) -> AssetInfo:
    attributes = [str(a) for a in (getattr(raw, "attributes", None) or [])]
    return AssetInfo(
        symbol=str(raw.symbol),
        name=str(getattr(raw, "name", "") or ""),
        exchange=str(getattr(getattr(raw, "exchange", ""), "value", raw.exchange or "")),
        tradable=bool(raw.tradable),
        shortable=bool(getattr(raw, "shortable", False)),
        easy_to_borrow=bool(getattr(raw, "easy_to_borrow", False)),
        fractionable=bool(getattr(raw, "fractionable", False)),
        # Alpaca marks overnight-eligible assets via an attribute flag; treat
        # anything containing "overnight" as eligible, absence as False.
        overnight_eligible=any("overnight" in a.lower() for a in attributes),
    )


def map_position(raw: Any) -> PositionInfo:
    qty = float(raw.qty)
    if str(getattr(raw, "side", "long")).lower().endswith("short"):
        qty = -abs(qty)
    return PositionInfo(
        symbol=str(raw.symbol),
        qty=qty,
        avg_entry_price=float(raw.avg_entry_price),
        market_value=float(getattr(raw, "market_value", 0) or 0),
        unrealized_pl=float(getattr(raw, "unrealized_pl", 0) or 0),
        current_price=(float(raw.current_price) if getattr(raw, "current_price", None) else None),
    )


class AlpacaAdapter(BrokerAdapter):
    def __init__(self, mode: TradingMode) -> None:
        super().__init__(mode)  # LIVE raises here (hard rule 1)
        self._client: Any = None
        self._stream: Any = None
        self._stream_task: asyncio.Task | None = None
        self._updates: asyncio.Queue[TradeUpdate] = asyncio.Queue(maxsize=1000)
        self._connected = False
        # S0 asset-metadata cache (same pattern as overnight-eligibility, §7):
        # filled by get_asset/get_assets; asset_shortable reads it first so
        # the entry path never burns a REST call per candidate.
        self._assets_cache: dict[str, AssetInfo] = {}

    # -- credentials --------------------------------------------------------

    @staticmethod
    def _credentials() -> tuple[str, str]:
        key_id = secrets.get_secret(KEYCHAIN_PAPER_KEY_ID)
        secret = secrets.get_secret(KEYCHAIN_PAPER_SECRET)
        if not key_id or not secret:
            raise MissingCredentialsError(
                f"Keychain entries '{KEYCHAIN_PAPER_KEY_ID}' / '{KEYCHAIN_PAPER_SECRET}' "
                "are missing — store your Alpaca PAPER keys first"
            )
        return key_id, secret

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.stream import TradingStream

        key_id, secret = self._credentials()
        self._client = TradingClient(key_id, secret, paper=True)
        # prove the credentials with one REST call before declaring connected
        await asyncio.to_thread(self._client.get_clock)
        self._stream = TradingStream(key_id, secret, paper=True)
        self._stream.subscribe_trade_updates(self._on_trade_update)
        self._stream_task = asyncio.ensure_future(self._run_stream())
        self._connected = True
        logger.info("Alpaca paper connection established")

    async def _run_stream(self) -> None:
        """The trade-update stream with a LIFE INSURANCE policy (2026-08-24:
        it died silently overnight — the first Monday's fills were never
        heard and positions sat invisible/unmanaged). Any exit, clean or
        crashed, is logged LOUDLY and the stream is rebuilt after 5s."""
        from alpaca.trading.stream import TradingStream

        while self._connected or self._stream is not None:
            try:
                await self._stream._run_forever()  # alpaca-py's async core loop
                logger.error("Alpaca trade-update stream ENDED — reconnecting in 5s")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Alpaca trade-update stream DIED — reconnecting in 5s")
            if not self._connected:
                return
            await asyncio.sleep(5.0)
            try:
                with contextlib.suppress(Exception):
                    await self._stream.stop_ws()
                key_id, secret = self._credentials()
                self._stream = TradingStream(key_id, secret, paper=True)
                self._stream.subscribe_trade_updates(self._on_trade_update)
                logger.info("Alpaca trade-update stream rebuilt")
            except Exception:
                logger.exception("stream rebuild failed — retrying next loop")
                await asyncio.sleep(10.0)

    async def _on_trade_update(self, data: Any) -> None:
        try:
            update = TradeUpdate(
                event=str(getattr(data, "event", "")),
                order=map_order(data.order),
                timestamp=getattr(data, "timestamp", None),
            )
            self._updates.put_nowait(update)
        except asyncio.QueueFull:
            logger.error("trade-update queue full — dropping event")
        except Exception:
            logger.exception("failed to map trade update")

    async def close(self) -> None:
        self._connected = False
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        if self._stream is not None:
            with contextlib.suppress(Exception):
                await self._stream.stop_ws()
            self._stream = None
        self._client = None
        logger.info("Alpaca connection closed")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _require_client(self) -> Any:
        if self._client is None:
            raise BrokerError("not connected — call connect() first")
        return self._client

    # -- account & market meta ---------------------------------------------

    async def get_account(self, mode: TradingMode) -> AccountSnapshot:
        self._check_mode(mode)
        raw = await asyncio.to_thread(self._require_client().get_account)
        return AccountSnapshot(
            account_id=str(raw.account_number),
            equity=float(raw.equity),
            cash=float(raw.cash),
            buying_power=float(raw.buying_power),
            currency=str(getattr(raw, "currency", "USD") or "USD"),
        )

    async def get_clock(self, mode: TradingMode) -> MarketClock:
        self._check_mode(mode)
        raw = await asyncio.to_thread(self._require_client().get_clock)
        return MarketClock(
            timestamp=raw.timestamp,
            is_open=bool(raw.is_open),
            next_open=raw.next_open,
            next_close=raw.next_close,
        )

    async def get_asset(self, mode: TradingMode, symbol: str) -> AssetInfo | None:
        self._check_mode(mode)
        try:
            raw = await asyncio.to_thread(self._require_client().get_asset, symbol)
        except Exception:
            return None
        asset = map_asset(raw)
        self._assets_cache[asset.symbol.upper()] = asset
        return asset

    async def get_assets(self, mode: TradingMode) -> list[AssetInfo]:
        self._check_mode(mode)
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        raw = await asyncio.to_thread(self._require_client().get_all_assets, request)
        assets = [map_asset(a) for a in raw]
        # keep the S0 shortability cache warm off the daily universe refresh
        self._assets_cache.update({a.symbol.upper(): a for a in assets})
        return assets

    async def asset_shortable(self, mode: TradingMode, symbol: str) -> bool:
        """Cache-first shortable AND easy_to_borrow (S0). The daily
        get_assets refresh keeps the cache warm; a miss falls back to one
        REST lookup via get_asset (which also fills the cache). Unknown
        metadata answers False — fail closed, never short blind."""
        self._check_mode(mode)
        asset = self._assets_cache.get(symbol.upper())
        if asset is None:
            asset = await self.get_asset(mode, symbol)
        return bool(asset is not None and asset.shortable and asset.easy_to_borrow)

    # -- orders & positions -------------------------------------------------

    async def submit_order(self, mode: TradingMode, request: OrderRequest) -> OrderInfo:
        self._check_mode(mode)
        payload = self._build_order_request(request)
        raw = await asyncio.to_thread(self._require_client().submit_order, payload)
        info = map_order(raw)
        logger.info(
            "order submitted: %s %s %s x%s (client id %s)",
            info.symbol,
            info.side.value,
            info.order_type.value,
            info.qty,
            info.client_order_id,
        )
        return info

    @staticmethod
    def _build_order_request(request: OrderRequest) -> Any:
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.enums import OrderSide as ASide
        from alpaca.trading.enums import TimeInForce as ATif
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLimitOrderRequest,
            StopLossRequest,
            StopOrderRequest,
            TakeProfitRequest,
        )

        common: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": request.qty,
            "side": ASide.BUY if request.side is OrderSide.BUY else ASide.SELL,
            "time_in_force": ATif(request.time_in_force.value),
            "client_order_id": request.client_order_id,
            "extended_hours": request.extended_hours,
        }
        if request.stop_loss is not None or request.take_profit is not None:
            common["order_class"] = (
                OrderClass.BRACKET
                if (request.stop_loss and request.take_profit)
                else OrderClass.OTO
            )
            if request.stop_loss is not None:
                common["stop_loss"] = StopLossRequest(
                    stop_price=request.stop_loss.stop_price,
                    limit_price=request.stop_loss.limit_price,
                )
            if request.take_profit is not None:
                common["take_profit"] = TakeProfitRequest(
                    limit_price=request.take_profit.limit_price
                )

        if request.order_type is OrderType.MARKET:
            return MarketOrderRequest(**common)
        if request.order_type is OrderType.LIMIT:
            return LimitOrderRequest(limit_price=request.limit_price, **common)
        if request.order_type is OrderType.STOP:
            return StopOrderRequest(stop_price=request.stop_price, **common)
        return StopLimitOrderRequest(
            limit_price=request.limit_price, stop_price=request.stop_price, **common
        )

    async def cancel_order(self, mode: TradingMode, order_id: str) -> None:
        self._check_mode(mode)
        await asyncio.to_thread(self._require_client().cancel_order_by_id, order_id)

    async def replace_order(
        self,
        mode: TradingMode,
        order_id: str,
        qty: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderInfo:
        self._check_mode(mode)
        from alpaca.trading.requests import ReplaceOrderRequest

        request = ReplaceOrderRequest(
            qty=int(qty) if qty is not None else None,
            limit_price=limit_price,
            stop_price=stop_price,
        )
        raw = await asyncio.to_thread(self._require_client().replace_order_by_id, order_id, request)
        return map_order(raw)

    async def get_open_orders(self, mode: TradingMode) -> list[OrderInfo]:
        self._check_mode(mode)
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        raw = await asyncio.to_thread(self._require_client().get_orders, request)
        return [map_order(o) for o in raw]

    async def get_order(self, mode: TradingMode, order_id: str) -> OrderInfo:
        self._check_mode(mode)
        client = self._require_client()
        raw = await asyncio.to_thread(client.get_order_by_id, order_id)
        return map_order(raw)

    async def get_closed_orders(self, mode, symbol, after):
        """Order history for ONE symbol since `after` — lets reconcile book
        exits that filled while Wave was offline (2026-08-20)."""
        self._check_mode(mode)
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=[symbol], after=after, limit=100
        )
        raw = await asyncio.to_thread(self._require_client().get_orders, request)
        return [map_order(o) for o in raw]

    async def get_positions(self, mode: TradingMode) -> list[PositionInfo]:
        self._check_mode(mode)
        raw = await asyncio.to_thread(self._require_client().get_all_positions)
        return [map_position(p) for p in raw]

    async def get_fill_activities(self, mode: TradingMode, after: str) -> list[dict]:
        self._check_mode(mode)
        client = self._require_client()

        def _fetch() -> list[dict]:
            # alpaca-py's TradingClient exposes no activities wrapper; use
            # the underlying RESTClient GET (same auth/session/retries).
            out: list[dict] = []
            page_token = None
            for _ in range(20):  # hard cap: 2000 rows is far beyond a day
                params: dict = {"after": after, "page_size": 100}
                if page_token:
                    params["page_token"] = page_token
                rows = client.get("/account/activities/FILL", params)
                if not isinstance(rows, list) or not rows:
                    break
                out.extend(rows)
                if len(rows) < 100:
                    break
                page_token = rows[-1].get("id")
            return out

        return await asyncio.to_thread(_fetch)

    # -- streams ------------------------------------------------------------

    async def trade_updates(self) -> AsyncIterator[TradeUpdate]:
        while True:
            yield await self._updates.get()
