"""SCANNER UNLEASHED (2026-09-02): scanner2 menu names outside the
trailing-volume scan set become same-day guests of the feature scan —
with real daily stats, never garbage zeros, resetting each trading day."""

from types import SimpleNamespace

import keyring
import pytest

from waveapp.broker.base import AssetInfo


def _provider(tmp_path, fake_keychain, symbols=("SPY", "TARS", "MLYS")):
    keyring.set_password("Wave", "alpaca_paper_key_id", "test-key-id")
    keyring.set_password("Wave", "alpaca_paper_secret", "x" * 30)
    from waveapp.data.features import AlpacaFeatureProvider

    assets = [AssetInfo(s, s, "ARCA", True, True, True, True) for s in symbols]
    provider = AlpacaFeatureProvider(assets, universe_size=150, cache_path=tmp_path / "c.json")
    provider._scan_set = ["SPY"]
    return provider


def _daily_series(closes, volume=900_000.0):
    return [SimpleNamespace(close=c, high=c + 1.0, low=c - 1.0, volume=volume) for c in closes]


@pytest.mark.asyncio
async def test_menu_guest_is_admitted_with_real_stats(tmp_path, fake_keychain):
    provider = _provider(tmp_path, fake_keychain)
    provider._client = SimpleNamespace(
        get_stock_bars=lambda request: SimpleNamespace(data={"TARS": _daily_series([70.0] * 10)})
    )
    provider.add_guests(["TARS", "SPY", "UNKNOWN"])  # SPY = scan set, UNKNOWN = not an asset
    assert provider._guest_pending == {"TARS"}
    await provider._guest_task
    assert "TARS" in provider._guests
    assert provider._avg_volume["TARS"] == 900_000.0
    assert provider._daily_atr_pct["TARS"] > 0  # real ATR, not a garbage zero


@pytest.mark.asyncio
async def test_junk_guests_are_refused(tmp_path, fake_keychain):
    """Below the guest floors (price ≥ $3, 10% of the venue volume floor) —
    the guest door is not a junk door."""
    provider = _provider(tmp_path, fake_keychain)
    provider._client = SimpleNamespace(
        get_stock_bars=lambda request: SimpleNamespace(
            data={
                "TARS": _daily_series([2.0] * 10),  # sub-$3
                "MLYS": _daily_series([50.0] * 10, volume=100.0),  # no volume
            }
        )
    )
    provider.add_guests(["TARS", "MLYS"])
    await provider._guest_task
    assert provider._guests == set()


@pytest.mark.asyncio
async def test_guests_reset_with_the_trading_day(tmp_path, fake_keychain):
    provider = _provider(tmp_path, fake_keychain)
    provider._guest_day = "2026-09-01"  # yesterday's party
    provider._guests = {"OLD"}
    provider._client = SimpleNamespace(get_stock_bars=lambda request: SimpleNamespace(data={}))
    provider.add_guests(["TARS"])
    assert "OLD" not in provider._guests
    assert provider._guest_day == provider._trading_day()
    if provider._guest_task is not None:
        await provider._guest_task


def test_guest_cap_holds(tmp_path, fake_keychain):
    provider = _provider(tmp_path, fake_keychain)
    provider._guest_day = provider._trading_day()
    provider._guests = {f"G{i}" for i in range(provider.MAX_GUESTS_PER_DAY)}
    provider.add_guests(["TARS"])
    assert provider._guest_pending == set()  # door closed at the cap


@pytest.mark.asyncio
async def test_fetch_scans_guests_too(tmp_path, fake_keychain):
    provider = _provider(tmp_path, fake_keychain)
    provider._universe_day = provider._trading_day()  # A4-12: ET day key
    provider._guest_day = provider._trading_day()
    provider._guests = {"TARS"}
    seen_chunks = []

    def fake_snapshot(request):
        seen_chunks.append(list(request.symbol_or_symbols))
        return {}

    provider._client = SimpleNamespace(get_stock_snapshot=fake_snapshot)
    await provider.fetch()
    scanned = {s for chunk in seen_chunks for s in chunk}
    assert scanned == {"SPY", "TARS"}
