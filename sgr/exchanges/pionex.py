"""
SGR Pionex Exchange Adapter.

Pionex-spezifisch:

* Grid Trading Bots werden von SGR nicht verwendet.
* SGR verwendet ausschließlich Standard-Orders.
* Pionex besitzt kein dediziertes Testnet.
* Paper Mode verwendet deshalb echte öffentliche Marktdaten
  über CCXT, simuliert aber sämtliche Orders lokal in SGR.
* CCXT-ID: "pionex"
* Hauptsächlich Spot, begrenzte Futures-Unterstützung.

Paper Mode:
    CCXT wird für öffentliche Marktdaten initialisiert.
    Es werden keine Trading-Orders an Pionex gesendet.
    Orders werden durch CCXTBaseAdapter._simulate_order()
    vollständig lokal simuliert.

Live Mode:
    PIONEX_LIVE_API_KEY + PIONEX_LIVE_SECRET werden benötigt.
"""

from __future__ import annotations

from sgr.core.types import ExchangeID, TradingMode
from sgr.exchanges.ccxt_base import CCXTBaseAdapter


class PionexAdapter(CCXTBaseAdapter):
    """
    Pionex Exchange Adapter.

    Paper Mode:
        Echte öffentliche Marktdaten über CCXT.
        Orders werden lokal durch SGR simuliert.

    Live Mode:
        Normale authentifizierte Pionex Verbindung.
    """

    exchange_id = ExchangeID.PIONEX
    _ccxt_id = "pionex"

    # Pionex besitzt kein dediziertes Testnet.
    _testnet_urls: dict = {}

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
        Initialisiert die Pionex CCXT Verbindung.

        Paper Mode:
            CCXT wird trotzdem initialisiert, weil SGR echte
            öffentliche Marktdaten benötigt. Es werden jedoch
            keine Trading Orders an Pionex gesendet.

        Live Mode:
            Normale authentifizierte Verbindung über die
            Basisklasse.
        """

        if self.trading_mode == TradingMode.PAPER:
            # Wichtig:
            # Paper Mode darf NICHT einfach nur _connected=True setzen.
            #
            # Market Data benötigt eine echte CCXT Instanz.
            # Die Order-Simulation erfolgt später in
            # CCXTBaseAdapter._simulate_order().

            try:
                import ccxt.async_support as ccxt
            except ImportError:
                raise RuntimeError(
                    "ccxt not installed. Run: pip install ccxt"
                ) from None

            exchange_class = getattr(ccxt, self._ccxt_id)

            options = {
                "apiKey": self._api_key,
                "secret": self._secret,
                "enableRateLimit": True,
                "timeout": 30_000,
            }

            options.update(self._extra_options)

            self._ccxt = exchange_class(options)

            try:
                # Lädt öffentliche Märkte und Timeframes.
                # Keine Authentifizierung und kein Trading erforderlich.
                await self._ccxt.load_markets()

                self._connected = True

                from sgr.core.logging import get_logger

                get_logger(__name__).info(
                    "exchange.connected",
                    exchange=self.exchange_id.value,
                    trading_mode=self.trading_mode.value,
                    note="paper_market_data_mode",
                    symbols_count=len(self._ccxt.symbols),
                )

            except Exception:
                # CCXT Session sauber schließen, wenn die
                # Initialisierung fehlschlägt.
                try:
                    await self._ccxt.close()
                except Exception:
                    pass

                self._ccxt = None
                self._connected = False
                raise

            return

        # Live Mode:
        # Basisklasse übernimmt normale authentifizierte Verbindung.
        await super().connect()

    @classmethod
    def from_config(cls, trading_mode: TradingMode) -> PionexAdapter:
        """
        Erstellt den Adapter aus der SGR Konfiguration.
        """

        from sgr.core.config import get_config

        config = get_config()

        if trading_mode == TradingMode.PAPER:
            # Paper Mode benötigt keine echten Pionex Credentials.
            # Dummy Credentials werden nur benötigt, weil die
            # CCXT Exchange Instanz diese Parameter akzeptiert.

            return cls(
                api_key=(
                    config.credentials.pionex_paper_api_key.get_secret_value()
                    if config.credentials.pionex_paper_api_key
                    else "paper_key"
                ),
                secret=(
                    config.credentials.pionex_paper_secret.get_secret_value()
                    if config.credentials.pionex_paper_secret
                    else "paper_secret"
                ),
                trading_mode=trading_mode,
            )

        credentials = config.credentials.get_credentials(
            "pionex",
            trading_mode,
        )

        return cls(
            api_key=credentials["apiKey"],
            secret=credentials["secret"],
            trading_mode=trading_mode,
        )
