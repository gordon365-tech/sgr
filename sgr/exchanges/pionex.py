"""
SGR Pionex Adapter
==================
Concrete adapter für Pionex.

Pionex-spezifisch:
- Grid Trading Bots (SGR nutzt nur Standard-Orders, keine Bot-Features)
- Kein dediziertes Testnet → Paper Mode simuliert vollständig lokal
- CCXT-ID: "pionex"
- Hauptsächlich Spot, begrenzte Futures-Unterstützung

Paper Mode:
    Pionex hat kein Testnet. Paper-Orders werden vollständig
    in SGR simuliert (kein API-Call). Credentials sind für Paper
    nicht zwingend erforderlich (nur für Live).

Live Mode:
    PIONEX_LIVE_API_KEY + PIONEX_LIVE_SECRET in .env setzen.
    API-Keys unter: https://www.pionex.com/en/account/api
"""

from __future__ import annotations

from sgr.core.types import ExchangeID, TradingMode
from sgr.exchanges.ccxt_base import CCXTBaseAdapter


class PionexAdapter(CCXTBaseAdapter):
    """
    Pionex exchange adapter.
    Paper mode ist vollständig simuliert (kein Testnet verfügbar).
    """

    exchange_id = ExchangeID.PIONEX
    _ccxt_id = "pionex"
    _testnet_urls = {}  # Kein Testnet für Pionex

    def __init__(
        self,
        api_key: str,
        secret: str,
        trading_mode: TradingMode,
    ) -> None:
        super().__init__(
            api_key=api_key,
            secret=secret,
            trading_mode=trading_mode,
            extra_options={
                "options": {
                    "adjustForTimeDifference": True,
                },
            },
        )

    async def connect(self) -> None:
        """
        Für Paper Mode: minimale Initialisierung ohne echten API-Call.
        Für Live Mode: normaler Connect mit Credential-Validierung.
        """
        if self.trading_mode == TradingMode.PAPER:
            # Simulierter Connect – kein echter API-Call
            # Paper-Orders laufen komplett in _simulate_order()
            self._connected = True
            from sgr.core.logging import get_logger

            get_logger(__name__).info(
                "exchange.connected",
                exchange=self.exchange_id.value,
                trading_mode=self.trading_mode.value,
                note="paper_simulation_mode",
            )
            return

        await super().connect()

    @classmethod
    def from_config(cls, trading_mode: TradingMode) -> PionexAdapter:
        """Factory: lädt Credentials aus SGR Config."""
        from sgr.core.config import get_config

        config = get_config()

        if trading_mode == TradingMode.PAPER:
            # Paper braucht keine echten Keys
            return cls(
                api_key=config.credentials.pionex_paper_api_key.get_secret_value()
                if config.credentials.pionex_paper_api_key
                else "paper_key",
                secret=config.credentials.pionex_paper_secret.get_secret_value()
                if config.credentials.pionex_paper_secret
                else "paper_secret",
                trading_mode=trading_mode,
            )

        credentials = config.credentials.get_credentials("pionex", trading_mode)
        return cls(
            api_key=credentials["apiKey"],
            secret=credentials["secret"],
            trading_mode=trading_mode,
        )
