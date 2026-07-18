"""
SGR Binance Adapter
===================
Concrete adapter for Binance (Spot + Futures).

Binance-specific:
- Testnet URLs für Paper Trading (testnet.binance.vision)
- Futures: separate Endpoint (fapi.binance.com)
- Funding Rates, Open Interest: nur Futures
- UM Futures vs CM Futures: wir nutzen USDT-Margined (UM)

Paper Mode:
    Nutzt Binance Testnet (echte API, simuliertes Kapital).
    Testnet-Keys unter: https://testnet.binance.vision/
    BINANCE_PAPER_API_KEY + BINANCE_PAPER_SECRET in .env setzen.

Live Mode:
    Nur nach bestandenen Go-Live Gates aktivieren.
    BINANCE_LIVE_API_KEY + BINANCE_LIVE_SECRET in .env setzen.
"""

from __future__ import annotations

from typing import Any

from sgr.core.types import ExchangeID, TradingMode
from sgr.exchanges.ccxt_base import CCXTBaseAdapter


class BinanceAdapter(CCXTBaseAdapter):
    """
    Binance exchange adapter (Spot + UM Futures).
    Inherits all CCXT logic from CCXTBaseAdapter.
    Only Binance-specific config lives here.
    """

    exchange_id = ExchangeID.BINANCE
    _ccxt_id = "binance"

    # Binance Testnet endpoints (paper trading)
    _testnet_urls = {
        "api": {
            "public": "https://testnet.binance.vision/api",
            "private": "https://testnet.binance.vision/api",
        },
        "fapiPublic": "https://testnet.binancefuture.com/fapi/v1",
        "fapiPrivate": "https://testnet.binancefuture.com/fapi/v1",
    }

    def __init__(
        self,
        api_key: str,
        secret: str,
        trading_mode: TradingMode,
        futures_mode: bool = False,
    ) -> None:
        """
        Args:
            api_key: Binance API key (testnet or live depending on trading_mode)
            secret: Binance API secret
            trading_mode: PAPER (testnet) or LIVE
            futures_mode: True = UM Futures, False = Spot
        """
        extra_options: dict[str, Any] = {}

        if futures_mode:
            # Switch CCXT to Futures API
            extra_options["defaultType"] = "future"
            extra_options["options"] = {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            }
        else:
            extra_options["options"] = {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
            }

        super().__init__(
            api_key=api_key,
            secret=secret,
            trading_mode=trading_mode,
            extra_options=extra_options,
        )

        self.futures_mode = futures_mode

    @classmethod
    def from_config(
        cls,
        trading_mode: TradingMode,
        futures_mode: bool = False,
    ) -> BinanceAdapter:
        """
        Factory method: loads credentials from SGR config.
        Use this in production code instead of direct constructor.

        Example:
            adapter = BinanceAdapter.from_config(TradingMode.PAPER)
            await adapter.connect()
        """
        from sgr.core.config import get_config

        config = get_config()
        credentials = config.credentials.get_credentials("binance", trading_mode)

        return cls(
            api_key=credentials["apiKey"],
            secret=credentials["secret"],
            trading_mode=trading_mode,
            futures_mode=futures_mode,
        )
