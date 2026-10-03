"""IBKRAdapter (SPEC.md §1, §3): built into v1 behind the same interface,
deliberately DORMANT until capital justifies foreign-exchange fees. Enabling
it later must require zero engine changes — hence it compiles against the full
BrokerAdapter ABC today, and every operation raises DormantAdapterError.

When activated (a future, owner-gated decision) the implementation will use
`ib_async` + IB Gateway + IBC; the import below proves the dependency wiring
without paying any runtime cost.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from waveapp.broker.base import (
    AccountSnapshot,
    AssetInfo,
    BrokerAdapter,
    DormantAdapterError,
    MarketClock,
    OrderInfo,
    OrderRequest,
    PositionInfo,
    TradeUpdate,
    TradingMode,
)

_DORMANT_MESSAGE = "IBKRAdapter is dormant in v1 (SPEC.md §1) — use AlpacaAdapter"


def _dormant() -> DormantAdapterError:
    return DormantAdapterError(_DORMANT_MESSAGE)


class IBKRAdapter(BrokerAdapter):
    """Compiles behind the BrokerAdapter interface; refuses to operate."""

    def __init__(self, mode: TradingMode) -> None:
        super().__init__(mode)
        # Prove ib_async is importable so activation is a code change, not an
        # environment scramble. Imported lazily; nothing is instantiated.
        import ib_async  # noqa: F401

    async def connect(self) -> None:
        raise _dormant()

    async def close(self) -> None:  # closing a dormant adapter is a no-op
        return None

    @property
    def is_connected(self) -> bool:
        return False

    async def get_account(self, mode: TradingMode) -> AccountSnapshot:
        self._check_mode(mode)
        raise _dormant()

    async def get_clock(self, mode: TradingMode) -> MarketClock:
        self._check_mode(mode)
        raise _dormant()

    async def get_assets(self, mode: TradingMode) -> list[AssetInfo]:
        self._check_mode(mode)
        raise _dormant()

    async def submit_order(self, mode: TradingMode, request: OrderRequest) -> OrderInfo:
        self._check_mode(mode)
        raise _dormant()

    async def cancel_order(self, mode: TradingMode, order_id: str) -> None:
        self._check_mode(mode)
        raise _dormant()

    async def replace_order(
        self,
        mode: TradingMode,
        order_id: str,
        qty: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderInfo:
        self._check_mode(mode)
        raise _dormant()

    async def get_open_orders(self, mode: TradingMode) -> list[OrderInfo]:
        self._check_mode(mode)
        raise _dormant()

    async def get_positions(self, mode: TradingMode) -> list[PositionInfo]:
        self._check_mode(mode)
        raise _dormant()

    async def trade_updates(self) -> AsyncIterator[TradeUpdate]:
        raise _dormant()
        yield  # pragma: no cover — makes this an async generator per the ABC
