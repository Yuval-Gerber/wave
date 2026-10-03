"""Broker abstraction (§3, Phase 2).

Hard rules encoded here, not in call sites:
- Rule 7: paper and live are separated at the type level. `TradingMode` is
  threaded through EVERY broker call and checked against the adapter's bound
  mode — a paper adapter physically refuses a call marked LIVE and vice versa.
- Rule 1: no live orders before the Phase 11 gate. Constructing any adapter
  in LIVE mode raises `LiveTradingLockedError` until that gate ships.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


class BrokerError(Exception):
    """Base for all broker-layer errors."""


class LiveTradingLockedError(BrokerError):
    """Raised when anything tries to touch LIVE before the Phase 11 gate."""


class ModeMismatchError(BrokerError):
    """A call's TradingMode does not match the adapter's bound mode."""


class MissingCredentialsError(BrokerError):
    """Keychain entries for the adapter are absent."""


class DormantAdapterError(BrokerError):
    """The adapter is built but deliberately disabled (IBKR in v1)."""


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    OPG = "opg"
    CLS = "cls"


class OrderStatus(Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    PENDING = "pending"
    OTHER = "other"


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    equity: float
    cash: float
    buying_power: float
    currency: str = "USD"


@dataclass(frozen=True)
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class AssetInfo:
    symbol: str
    name: str
    exchange: str
    tradable: bool
    shortable: bool
    easy_to_borrow: bool
    fractionable: bool
    overnight_eligible: bool | None = None  # None = unknown from this broker


@dataclass(frozen=True)
class StopLoss:
    stop_price: float
    limit_price: float | None = None  # None = plain stop


@dataclass(frozen=True)
class TakeProfit:
    limit_price: float


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    qty: float
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    client_order_id: str  # idempotency key, derived per §3
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: StopLoss | None = None  # bracket leg — server-side stop
    take_profit: TakeProfit | None = None
    extended_hours: bool = False


@dataclass(frozen=True)
class OrderInfo:
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    qty: float
    filled_qty: float
    order_type: OrderType
    status: OrderStatus
    limit_price: float | None = None
    stop_price: float | None = None
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    legs: tuple[OrderInfo, ...] = field(default=())


@dataclass(frozen=True)
class PositionInfo:
    symbol: str
    qty: float  # negative = short
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    current_price: float | None = None


@dataclass(frozen=True)
class TradeUpdate:
    """One event from the broker's order-update stream."""

    event: str  # fill, partial_fill, canceled, rejected, new, ...
    order: OrderInfo
    timestamp: datetime | None = None


class BrokerAdapter(ABC):
    """Every broker Wave talks to implements exactly this interface.

    Adapters are bound to one TradingMode at construction; every call must
    re-state the mode and `_check_mode` enforces agreement (hard rule 7).
    """

    def __init__(self, mode: TradingMode) -> None:
        if mode is TradingMode.LIVE:
            raise LiveTradingLockedError(
                "LIVE trading is locked until the Phase 11 gate (hard rule 1)"
            )
        self._mode = mode

    @property
    def mode(self) -> TradingMode:
        return self._mode

    def _check_mode(self, mode: TradingMode) -> None:
        if mode is not self._mode:
            raise ModeMismatchError(
                f"call marked {mode.value!r} on an adapter bound to {self._mode.value!r}"
            )

    # -- lifecycle ----------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    # -- account & market meta ---------------------------------------------

    @abstractmethod
    async def get_account(self, mode: TradingMode) -> AccountSnapshot: ...

    @abstractmethod
    async def get_clock(self, mode: TradingMode) -> MarketClock: ...

    @abstractmethod
    async def get_assets(self, mode: TradingMode) -> list[AssetInfo]: ...

    async def get_asset(self, mode: TradingMode, symbol: str) -> AssetInfo | None:  # noqa: B027 — optional capability, default None
        return None

    async def asset_shortable(self, mode: TradingMode, symbol: str) -> bool:
        """S0 short-side gate helper: shortable AND easy_to_borrow from the
        broker's asset metadata (verified 2026-09-23: the paper account has
        shorting_enabled, but per-asset flags vary — PLTR/AAPL ETB, GRML
        not shortable — so the per-asset check is mandatory at entry, S3).
        No metadata → False: fail closed, never short blind."""
        asset = await self.get_asset(mode, symbol)
        return bool(asset is not None and asset.shortable and asset.easy_to_borrow)

    async def get_fill_activities(  # noqa: B027 — optional capability, default empty
        self, mode: TradingMode, after: str
    ) -> list[dict]:
        """Broker FILL activity rows (raw dicts: symbol, side, qty, price,
        transaction_time) since `after` (ISO-8601). Optional capability —
        powers broker-truth per-trade P&L; adapters without
        an activities feed return []."""
        return []

    # -- orders & positions -------------------------------------------------

    @abstractmethod
    async def submit_order(self, mode: TradingMode, request: OrderRequest) -> OrderInfo: ...

    @abstractmethod
    async def cancel_order(self, mode: TradingMode, order_id: str) -> None: ...

    @abstractmethod
    async def replace_order(
        self,
        mode: TradingMode,
        order_id: str,
        qty: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderInfo:
        """Atomically amend a resting order (the §8.2 stop ratchet uses this —
        the server-side stop is REPLACED, never left missing mid-amend)."""
        ...

    @abstractmethod
    async def get_open_orders(self, mode: TradingMode) -> list[OrderInfo]: ...

    async def get_order(self, mode: TradingMode, order_id: str) -> OrderInfo:
        """One order by id — the REST truth (2026-08-24 deafness insurance)."""
        raise NotImplementedError

    async def get_closed_orders(
        self, mode: TradingMode, symbol: str, after: datetime
    ) -> list[OrderInfo]:
        """Recently closed orders for one symbol (offline-close recovery,
        2026-08-20). Adapters without order-history support return []."""
        return []

    @abstractmethod
    async def get_positions(self, mode: TradingMode) -> list[PositionInfo]: ...

    # -- streams ------------------------------------------------------------

    @abstractmethod
    def trade_updates(self) -> AsyncIterator[TradeUpdate]:
        """Async iterator of order events; runs until `close()`."""
        ...
