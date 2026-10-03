"""Phase 2 broker-layer tests. No network — the Alpaca SDK is never actually
called; hard rules 1 and 7 are what's under test, plus model mapping."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from waveapp.broker.alpaca import (
    KEYCHAIN_PAPER_KEY_ID,
    KEYCHAIN_PAPER_SECRET,
    AlpacaAdapter,
    map_asset,
    map_order,
    map_position,
)
from waveapp.broker.base import (
    BrokerAdapter,
    DormantAdapterError,
    LiveTradingLockedError,
    MissingCredentialsError,
    ModeMismatchError,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from waveapp.broker.ibkr import IBKRAdapter

# -- hard rule 1: LIVE is locked until Phase 11 ------------------------------


def test_live_mode_is_locked_for_every_adapter():
    with pytest.raises(LiveTradingLockedError):
        AlpacaAdapter(TradingMode.LIVE)
    with pytest.raises(LiveTradingLockedError):
        IBKRAdapter(TradingMode.LIVE)


# -- hard rule 7: mode threaded through every call ---------------------------


async def test_mode_mismatch_is_rejected_before_any_client_use():
    adapter = AlpacaAdapter(TradingMode.PAPER)
    for call in (
        adapter.get_account,
        adapter.get_clock,
        adapter.get_assets,
        adapter.get_open_orders,
        adapter.get_positions,
    ):
        with pytest.raises(ModeMismatchError):
            await call(TradingMode.LIVE)


async def test_missing_keys_is_a_clear_error(fake_keychain):
    adapter = AlpacaAdapter(TradingMode.PAPER)
    with pytest.raises(MissingCredentialsError) as excinfo:
        await adapter.connect()
    assert KEYCHAIN_PAPER_KEY_ID in str(excinfo.value)
    assert KEYCHAIN_PAPER_SECRET in str(excinfo.value)


# -- IBKR: built, compiling, dormant -----------------------------------------


async def test_ibkr_is_dormant_but_conforms():
    adapter = IBKRAdapter(TradingMode.PAPER)
    assert isinstance(adapter, BrokerAdapter)
    assert adapter.is_connected is False
    with pytest.raises(DormantAdapterError):
        await adapter.connect()
    with pytest.raises(DormantAdapterError):
        await adapter.get_account(TradingMode.PAPER)
    await adapter.close()  # closing dormant adapter must be harmless


# -- model mapping -----------------------------------------------------------


def _fake_raw_order(**overrides):
    base = dict(
        id="abc-123",
        client_order_id="wave-uuid-entry-1",
        symbol="AAPL",
        side="buy",
        qty="10",
        filled_qty="4",
        order_type="limit",
        status="partially_filled",
        limit_price="189.5",
        stop_price=None,
        filled_avg_price="189.4",
        submitted_at=datetime(2026, 8, 4, 14, 0, tzinfo=UTC),
        legs=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_map_order_basics():
    info = map_order(_fake_raw_order())
    assert info.order_id == "abc-123"
    assert info.client_order_id == "wave-uuid-entry-1"
    assert info.side is OrderSide.BUY
    assert info.order_type is OrderType.LIMIT
    assert info.status is OrderStatus.PARTIALLY_FILLED
    assert info.qty == 10 and info.filled_qty == 4
    assert info.limit_price == 189.5


def test_map_order_with_bracket_legs():
    stop_leg = _fake_raw_order(id="leg-1", order_type="stop", side="sell", status="new")
    parent = _fake_raw_order(legs=[stop_leg])
    info = map_order(parent)
    assert len(info.legs) == 1
    assert info.legs[0].order_type is OrderType.STOP
    assert info.legs[0].side is OrderSide.SELL


def test_map_order_unknown_status_is_other():
    info = map_order(_fake_raw_order(status="held_for_review"))
    assert info.status is OrderStatus.OTHER


def test_map_asset_overnight_attribute():
    raw = SimpleNamespace(
        symbol="SPY",
        name="SPDR S&P 500",
        exchange="ARCA",
        tradable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=True,
        attributes=["overnight_tradable"],
    )
    asset = map_asset(raw)
    assert asset.overnight_eligible is True
    raw.attributes = []
    assert map_asset(raw).overnight_eligible is False


def test_map_position_short_is_negative_qty():
    raw = SimpleNamespace(
        symbol="TSLA",
        qty="5",
        side="PositionSide.SHORT",
        avg_entry_price="250.0",
        market_value="-1250.0",
        unrealized_pl="12.5",
        current_price="248.0",
    )
    position = map_position(raw)
    assert position.qty == -5
    assert position.unrealized_pl == 12.5


# -- order request building --------------------------------------------------


def test_build_bracket_market_order():
    from waveapp.broker.base import OrderRequest, StopLoss, TakeProfit, TimeInForce

    request = OrderRequest(
        symbol="AAPL",
        qty=10,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        client_order_id="wave-x",
        stop_loss=StopLoss(stop_price=180.0),
        take_profit=TakeProfit(limit_price=200.0),
    )
    payload = AlpacaAdapter._build_order_request(request)
    assert payload.symbol == "AAPL"
    assert payload.client_order_id == "wave-x"
    assert payload.stop_loss.stop_price == 180.0
    assert payload.take_profit.limit_price == 200.0
    assert str(payload.order_class).lower().endswith("bracket")


# -- S0 shortability gate (short-side plan, 2026-09-23) -----------------------


def _asset_info(symbol: str, shortable: bool, etb: bool):
    from waveapp.broker.base import AssetInfo

    return AssetInfo(
        symbol=symbol,
        name=symbol,
        exchange="NASDAQ",
        tradable=True,
        shortable=shortable,
        easy_to_borrow=etb,
        fractionable=True,
    )


async def test_asset_shortable_cache_first_and_fail_closed():
    """shortable AND easy_to_borrow, from the metadata cache; unknown
    symbols (cache miss, no connection) answer False — fail closed."""
    adapter = AlpacaAdapter(TradingMode.PAPER)
    adapter._assets_cache["PLTR"] = _asset_info("PLTR", shortable=True, etb=True)
    adapter._assets_cache["XHTB"] = _asset_info("XHTB", shortable=True, etb=False)
    adapter._assets_cache["GRML"] = _asset_info("GRML", shortable=False, etb=False)
    assert await adapter.asset_shortable(TradingMode.PAPER, "PLTR") is True
    assert await adapter.asset_shortable(TradingMode.PAPER, "pltr") is True  # case-safe
    assert await adapter.asset_shortable(TradingMode.PAPER, "XHTB") is False  # ETB required
    assert await adapter.asset_shortable(TradingMode.PAPER, "GRML") is False
    assert await adapter.asset_shortable(TradingMode.PAPER, "ZZZQ") is False  # unknown → no


async def test_asset_shortable_enforces_mode_binding():
    adapter = AlpacaAdapter(TradingMode.PAPER)
    with pytest.raises(ModeMismatchError):
        await adapter.asset_shortable(TradingMode.LIVE, "PLTR")


async def test_get_asset_fills_the_shortability_cache():
    adapter = AlpacaAdapter(TradingMode.PAPER)
    raw = SimpleNamespace(
        symbol="MRNA",
        name="Moderna",
        exchange="NASDAQ",
        tradable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=True,
        attributes=[],
    )
    adapter._client = SimpleNamespace(get_asset=lambda symbol: raw)
    asset = await adapter.get_asset(TradingMode.PAPER, "MRNA")
    assert asset is not None and asset.easy_to_borrow is True
    assert adapter._assets_cache["MRNA"].shortable is True
    assert await adapter.asset_shortable(TradingMode.PAPER, "MRNA") is True


async def test_base_adapter_default_asset_shortable_uses_get_asset():
    """Adapters without a cache still answer through get_asset; adapters
    without asset metadata at all (base default get_asset → None) say False."""

    class Bare(BrokerAdapter):
        async def connect(self): ...
        async def close(self): ...

        @property
        def is_connected(self):
            return True

        async def get_account(self, mode): ...
        async def get_clock(self, mode): ...

        async def get_assets(self, mode):
            return []

        async def submit_order(self, mode, request): ...
        async def cancel_order(self, mode, order_id): ...

        async def replace_order(
            self, mode, order_id, qty=None, limit_price=None, stop_price=None
        ): ...

        async def get_open_orders(self, mode):
            return []

        async def get_positions(self, mode):
            return []

        def trade_updates(self): ...

    bare = Bare(TradingMode.PAPER)
    assert await bare.asset_shortable(TradingMode.PAPER, "AAPL") is False  # no metadata

    class WithMeta(Bare):
        async def get_asset(self, mode, symbol):
            return _asset_info("AAPL", shortable=True, etb=True) if symbol == "AAPL" else None

    meta = WithMeta(TradingMode.PAPER)
    assert await meta.asset_shortable(TradingMode.PAPER, "AAPL") is True
    assert await meta.asset_shortable(TradingMode.PAPER, "GRML") is False
