"""Read-only LIVE account access (Phase 8.3 — the live balance is
visible behind the toggle before Phase 11).

Hard rule 1 stays fully intact:
- this class exposes ONLY account reads (equity/cash) — it has no order
  methods at all, so no code path through it can ever place a live order;
- the trading engine never receives this object; AlpacaAdapter still refuses
  TradingMode.LIVE at the type level until the Phase 11 gate;
- live keys stay in macOS Keychain (entries below), never in files.
"""

from __future__ import annotations

import asyncio
import logging

from waveapp.security import secrets

logger = logging.getLogger("wave.broker.live_reader")

KEYCHAIN_LIVE_KEY_ID = "alpaca_live_key_id"  # noqa: S105 — Keychain entry NAME
KEYCHAIN_LIVE_SECRET = "alpaca_live_secret"  # noqa: S105 — Keychain entry NAME


class LiveAccountReader:
    """Balance-only view of the live Alpaca account."""

    @staticmethod
    def has_keys() -> bool:
        return (
            secrets.get_secret(KEYCHAIN_LIVE_KEY_ID) is not None
            and secrets.get_secret(KEYCHAIN_LIVE_SECRET) is not None
        )

    async def get_equity(self) -> tuple[float, float]:
        """(equity, cash) of the live account. Raises on any failure."""
        key_id = secrets.get_secret(KEYCHAIN_LIVE_KEY_ID)
        secret = secrets.get_secret(KEYCHAIN_LIVE_SECRET)
        if not key_id or not secret:
            raise RuntimeError(
                f"live keys missing from Keychain "
                f"('{KEYCHAIN_LIVE_KEY_ID}' / '{KEYCHAIN_LIVE_SECRET}')"
            )

        def _fetch() -> tuple[float, float]:
            from alpaca.trading.client import TradingClient

            client = TradingClient(key_id, secret, paper=False)
            account = client.get_account()
            return float(account.equity), float(account.cash)

        equity, cash = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=30)
        logger.info("live account read OK (read-only)")
        return equity, cash
