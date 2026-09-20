"""
SGR Pionex Exchange Adapter.

Pionex-spezifisch:

* Grid Trading Bots werden von SGR nicht verwendet.
* SGR verwendet ausschließlich Standard-Orders - auch für Futures Grid
  (siehe sgr/strategy/futures_grid.py): SGR verwaltet die Grid-Levels
  selbst, es wird niemals Pionex' eigenes Grid-Bot-Produkt angesprochen.
* Pionex besitzt kein dediziertes Testnet.
* Paper Mode verwendet deshalb echte öffentliche Marktdaten,
  simuliert aber sämtliche Orders lokal in SGR.
* Hauptsächlich Spot, begrenzte Futures/Perpetual-Unterstützung.

WICHTIGER BEFUND (Architektur-Audit für die Futures-Grid-Erweiterung):
    ccxt führt seit mehreren Major-Versionen (verifiziert von 4.1.50 bis
    4.5.81, dem aktuell in pyproject.toml zulässigen Bereich) KEINE
    "pionex"-Exchange-ID mehr. `getattr(ccxt.async_support, "pionex")`
    schlägt deshalb IMMER mit AttributeError fehl - unabhängig von Paper
    oder Live Mode. Das bedeutet: dieser Adapter konnte sich mit der
    zuvor bestehenden, rein ccxt-basierten Implementierung in der Praxis
    nie erfolgreich verbinden (weder für Marktdaten noch für Orders) -
    ein latenter, durch Tests mit vollständig gemocktem ccxt nicht
    aufgedeckter Defekt, der bereits vor dieser Futures-Grid-Erweiterung
    bestand.

    Fix (rein additiv, kein Verhaltensverlust für Binance): dieser Adapter
    versucht weiterhin zuerst den ccxt-Pfad (falls eine zukünftige
    ccxt-Version "pionex" wieder registriert). Ist die ccxt-Exchange-ID
    nicht vorhanden, fällt er für PAPER Mode auf
    sgr.exchanges.pionex_client.PionexClient zurück (bereits vorhandener,
    getesteter synchroner REST-Client für Pionex' ÖFFENTLICHE
    Markt-Endpunkte) und deckt darüber Ticker/Orderbook/OHLCV/
    Symbol-Metadaten ab. Damit funktioniert Paper Trading (inkl. Futures
    Grid Paper Trading) vollständig ohne echte Order-Übermittlung.

    LIVE Order-Submission bleibt für Pionex bewusst NICHT implementiert,
    solange kein verifizierter, signierter Private-REST-Client existiert
    (Order-Signierung ohne Sandbox-Zugang zu raten wäre für ein
    Handelssystem nicht vertretbar) - connect() schlägt im LIVE-Modus
    ohne funktionierenden ccxt-Pfad explizit und sofort fehl
    (AdapterFeatureNotImplementedError), statt eine Order später
    unkontrolliert scheitern zu lassen. Das ist eine bewusste
    Sicherheitsentscheidung, keine Fleißaufgabe für später "vergessen".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sgr.core.types import (
    AssetClass,
    Candle,
    ExchangeID,
    FundingRate,
    OrderBook,
    OrderBookLevel,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import (
    AdapterFeatureNotImplementedError,
    ExchangeConnectionError,
    ExchangeInfo,
    MarketStatus,
    NotSupportedFeatureError,
    OpenInterest,
    PositionModeInfo,
    SymbolLimits,
    TickerData,
)
from sgr.exchanges.ccxt_base import CCXTBaseAdapter


def _to_pionex_symbol(ccxt_symbol: str) -> str:
    """"BTC/USDT" -> "BTC_USDT" (Pionex' natives Symbolformat)."""
    return ccxt_symbol.split(":")[0].replace("/", "_")


def _from_pionex_symbol(pionex_symbol: str, exchange_id: ExchangeID) -> Symbol:
    """"BTC_USDT" -> Symbol(base="BTC", quote="USDT")."""
    base, _, quote = pionex_symbol.partition("_")
    return Symbol(base=base, quote=quote, exchange=exchange_id, asset_class=AssetClass.SPOT)


class PionexAdapter(CCXTBaseAdapter):
    """
    Pionex Exchange Adapter.

    Paper Mode:
        Öffentliche Marktdaten primär via ccxt (falls verfügbar), sonst
        via PionexClient-Fallback (siehe Modul-Docstring). Orders werden
        in beiden Fällen lokal durch SGR simuliert (CCXTBaseAdapter.
        _simulate_order(), unverändert - keine Sonderlogik).

    Live Mode:
        Normale authentifizierte ccxt-Verbindung, FALLS ccxt eine
        "pionex"-ID kennt. Sonst: siehe Modul-Docstring, connect() schlägt
        explizit fehl statt stillschweigend nicht zu funktionieren.
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
        futures_mode: bool = False,
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
        # futures_mode spiegelt BinanceAdapter's Parameter (siehe dortige
        # Klasse) - fuer den PERPETUAL/FUTURES_GRID-Produkttyp (siehe
        # sgr/exchanges/capabilities.py), NICHT fuer eine eigene ccxt
        # defaultType-Option (Pionex/ccxt kennt dieses Konzept nicht in
        # der hier verfuegbaren Version - siehe Modul-Docstring). Wird
        # ausschliesslich fuer die Marktdaten-Endpunkt-Auswahl im
        # PionexClient-Fallback verwendet (market_type=PERP statt SPOT).
        self.futures_mode = futures_mode
        # None solange nicht verbunden; True/False danach fix fuer die
        # Lebensdauer des Adapters (Symmetrie zu CCXTBaseAdapter._connected).
        self._native_fallback: bool = False
        self._native_client: Any = None

    async def connect(self) -> None:
        """
        Initialisiert die Pionex-Verbindung. Siehe Modul-Docstring fuer
        die vollstaendige Begruendung des ccxt-Availability-Checks.
        """
        try:
            import ccxt.async_support as ccxt
        except ImportError:
            raise RuntimeError("ccxt not installed. Run: pip install ccxt") from None

        ccxt_has_pionex = hasattr(ccxt, self._ccxt_id)

        if not ccxt_has_pionex:
            if self.trading_mode == TradingMode.LIVE:
                # Fail-fast (Modul-Docstring): kein stillschweigender
                # Verbindungsversuch, der erst bei der ersten Order
                # ueberraschend scheitert.
                raise AdapterFeatureNotImplementedError(
                    self.exchange_id.value,
                    "live_order_submission",
                    detail=(
                        "ccxt liefert keine 'pionex'-Exchange-ID und SGR hat "
                        "keinen verifizierten Pionex-Private-REST-Client. "
                        "LIVE Trading auf Pionex ist deshalb deaktiviert."
                    ),
                )
            await self._connect_native_fallback()
            return

        if self.trading_mode == TradingMode.PAPER:
            # Wichtig:
            # Paper Mode darf NICHT einfach nur _connected=True setzen.
            #
            # Market Data benötigt eine echte CCXT Instanz.
            # Die Order-Simulation erfolgt später in
            # CCXTBaseAdapter._simulate_order().
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
                try:
                    await self._ccxt.close()
                except Exception:
                    pass
                self._ccxt = None
                self._connected = False
                raise
            return

        # Live Mode, ccxt kennt "pionex": Basisklasse übernimmt normale
        # authentifizierte Verbindung.
        await super().connect()

    async def _connect_native_fallback(self) -> None:
        """
        PAPER-only Fallback ueber PionexClient (nur oeffentliche
        Marktdaten, siehe Modul-Docstring). LIVE wird bereits vor diesem
        Aufruf durch connect() abgewiesen.
        """
        from sgr.core.logging import get_logger
        from sgr.exchanges.pionex_client import PionexClient

        self._native_client = PionexClient()
        try:
            # Verbindungspruefung: ein einzelner leichter Call genuegt,
            # scheitert er, ist der Fallback selbst nicht nutzbar.
            await asyncio.to_thread(self._native_client.get_symbols)
        except Exception as e:
            self._native_client = None
            raise ExchangeConnectionError(
                self.exchange_id.value,
                f"Pionex native fallback (PionexClient) unreachable: {e}",
            ) from e

        self._native_fallback = True
        self._connected = True
        get_logger(__name__).info(
            "exchange.connected",
            exchange=self.exchange_id.value,
            trading_mode=self.trading_mode.value,
            note="native_fallback_public_market_data_only_no_ccxt_pionex",
        )

    def _require_connected(self) -> None:
        """
        Ueberschrieben, weil CCXTBaseAdapter._require_connected() ein
        gesetztes self._ccxt voraussetzt - im nativen Fallback (siehe
        Modul-Docstring) ist self._ccxt bewusst immer None, obwohl der
        Adapter ueber self._native_client verbunden ist.
        """
        if self._native_fallback:
            if not self._connected or self._native_client is None:
                raise ExchangeConnectionError(
                    self.exchange_id.value,
                    "Adapter not connected. Call connect() first.",
                )
            return
        super()._require_connected()

    async def close(self) -> None:
        if self._native_fallback:
            if self._native_client is not None:
                await asyncio.to_thread(self._native_client.close)
            self._native_client = None
            self._native_fallback = False
            self._connected = False
            return
        await super().close()

    async def ping(self) -> float:
        if self._native_fallback:
            import time

            start = time.monotonic()
            try:
                await asyncio.to_thread(self._native_client.get_symbols)
            except Exception as e:
                raise ExchangeConnectionError(self.exchange_id.value, str(e)) from e
            return (time.monotonic() - start) * 1000
        return await super().ping()

    # ------------------------------------------------------------------
    # Market Data (native fallback overrides)
    # ------------------------------------------------------------------

    async def get_exchange_info(self) -> ExchangeInfo:
        if not self._native_fallback:
            return await super().get_exchange_info()

        if self._exchange_info is not None:
            return self._exchange_info

        raw_symbols = await asyncio.to_thread(self._native_client.get_symbols)
        symbols: list[str] = []
        symbol_limits: dict[str, SymbolLimits] = {}
        for entry in raw_symbols:
            try:
                base = entry.get("baseCurrency") or entry.get("base")
                quote = entry.get("quoteCurrency") or entry.get("quote")
                if not base or not quote:
                    continue
                ccxt_symbol = f"{base}/{quote}"
                symbols.append(ccxt_symbol)

                amount_precision = self._safe_int(
                    entry.get("basePrecision") or entry.get("amountPrecision")
                )
                price_precision = self._safe_int(
                    entry.get("quotePrecision") or entry.get("pricePrecision")
                )
                min_amount = self._safe_decimal(entry.get("minAmount") or entry.get("minTradeSize"))
                min_notional = self._safe_decimal(entry.get("minTradeAmount"))
                symbol_limits[ccxt_symbol] = SymbolLimits(
                    amount_precision=amount_precision,
                    price_precision=price_precision,
                    min_amount=min_amount,
                    min_notional=min_notional,
                )
            except (TypeError, AttributeError):
                # Best-effort wie CCXTBaseAdapter._extract_symbol_limits:
                # ein fehlerhafter Eintrag darf die anderen nicht blockieren.
                continue

        self._exchange_info = ExchangeInfo(
            exchange_id=self.exchange_id,
            symbols=symbols,
            timeframes=list(self._native_client.KLINE_INTERVALS.keys()),
            maker_fee=Decimal("0.0005"),
            taker_fee=Decimal("0.0005"),
            fetched_at=datetime.now(tz=UTC),
            symbol_limits=symbol_limits,
        )
        return self._exchange_info

    async def get_market_status(self) -> MarketStatus:
        if not self._native_fallback:
            return await super().get_market_status()
        # PionexClient hat keinen dedizierten Status-Endpunkt - ein
        # erfolgreicher get_symbols()-Call (bereits Teil von connect())
        # ist der einzige verfuegbare Signal fuer "online". Kein
        # NotSupportedFeatureError (das Feature existiert bei Pionex
        # vermutlich, siehe AdapterFeatureNotImplementedError-Unterscheidung
        # im Modul-Docstring), sondern ein konservatives "online wenn
        # erreichbar".
        try:
            await asyncio.to_thread(self._native_client.get_symbols)
            return MarketStatus(
                exchange_id=self.exchange_id,
                is_online=True,
                raw_status="assumed_ok_via_public_endpoint",
                fetched_at=datetime.now(tz=UTC),
            )
        except Exception as e:
            raise ExchangeConnectionError(self.exchange_id.value, str(e)) from e

    async def get_position_mode(self) -> PositionModeInfo:
        if not self._native_fallback:
            return await super().get_position_mode()
        raise NotSupportedFeatureError(self.exchange_id.value, "fetchPositionMode")

    async def get_ticker(self, symbol: str) -> TickerData:
        if not self._native_fallback:
            return await super().get_ticker(symbol)

        self._require_connected()
        pionex_symbol = _to_pionex_symbol(symbol)
        market_type = "PERP" if self.futures_mode else "SPOT"
        raw = await asyncio.to_thread(
            self._native_client.get_ticker, pionex_symbol
        )
        book = await self._safe_book_ticker(pionex_symbol, market_type)

        last = self._safe_decimal(raw.get("close")) or Decimal("0")
        bid = book.get("bidPrice") if book else None
        ask = book.get("askPrice") if book else None

        return TickerData(
            symbol=symbol,
            bid=self._safe_decimal(bid) or last,
            ask=self._safe_decimal(ask) or last,
            last=last,
            volume_24h=self._safe_decimal(raw.get("volume")) or Decimal("0"),
            change_24h_pct=self._safe_change_pct(raw),
            timestamp=self._parse_ts(raw.get("time")),
        )

    async def _safe_book_ticker(
        self, pionex_symbol: str, market_type: str
    ) -> dict[str, Any] | None:
        """Best-effort bid/ask - ein Fehler hier darf get_ticker() nicht
        scheitern lassen, `last` bleibt dann der Fallback fuer bid/ask
        (siehe get_ticker())."""
        try:
            rows = await asyncio.to_thread(
                self._native_client.get_book_tickers, pionex_symbol, market_type
            )
            return rows[0] if rows else None
        except Exception:
            return None

    def _safe_change_pct(self, raw: dict[str, Any]) -> float:
        try:
            open_p = self._safe_decimal(raw.get("open"))
            close_p = self._safe_decimal(raw.get("close"))
            if open_p and close_p and open_p > 0:
                return float((close_p - open_p) / open_p * 100)
        except (InvalidOperation, ZeroDivisionError):
            pass
        return 0.0

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        if not self._native_fallback:
            return await super().get_orderbook(symbol, depth)

        self._require_connected()
        pionex_symbol = _to_pionex_symbol(symbol)
        raw = await asyncio.to_thread(self._native_client.get_orderbook, pionex_symbol, depth)
        sym = _from_pionex_symbol(pionex_symbol, self.exchange_id)

        bids = [
            OrderBookLevel(price=Decimal(str(p)), size=Decimal(str(s)))
            for p, s in (raw.get("bids") or [])[:depth]
        ]
        asks = [
            OrderBookLevel(price=Decimal(str(p)), size=Decimal(str(s)))
            for p, s in (raw.get("asks") or [])[:depth]
        ]
        return OrderBook(
            symbol=sym,
            timestamp=datetime.now(tz=UTC),
            bids=bids,
            asks=asks,
        )

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        if not self._native_fallback:
            return await super().get_ohlcv(symbol, timeframe, since, limit)

        self._require_connected()
        pionex_symbol = _to_pionex_symbol(symbol)
        sym = _from_pionex_symbol(pionex_symbol, self.exchange_id)
        # PionexClient.get_ohlcv erlaubt maximal 500 pro Call (siehe
        # dortige Validierung) - unveraendert weitergereicht, kein
        # Pagination-Loop (analog zum bestehenden Verhalten anderer
        # Adapter fuer eine einzelne get_ohlcv()-Anfrage).
        end_time = int(since.timestamp() * 1000) if since else None
        raw = await asyncio.to_thread(
            self._native_client.get_ohlcv,
            pionex_symbol,
            timeframe,
            min(limit, 500),
            end_time,
        )
        candles = []
        for row in raw:
            try:
                candles.append(
                    Candle(
                        symbol=sym,
                        timestamp=self._parse_ts(row.get("time")),
                        timeframe=timeframe,
                        open=Decimal(str(row["open"])),
                        high=Decimal(str(row["high"])),
                        low=Decimal(str(row["low"])),
                        close=Decimal(str(row["close"])),
                        volume=Decimal(str(row["volume"])),
                    )
                )
            except (KeyError, InvalidOperation):
                continue
        return sorted(candles, key=lambda c: c.timestamp)

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        if not self._native_fallback:
            return await super().get_funding_rate(symbol)
        raise AdapterFeatureNotImplementedError(
            self.exchange_id.value,
            "get_funding_rate",
            detail="Kein verifizierter Pionex-Funding-Rate-Endpunkt in PionexClient.",
        )

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        if not self._native_fallback:
            return await super().get_open_interest(symbol)
        raise AdapterFeatureNotImplementedError(
            self.exchange_id.value,
            "get_open_interest",
            detail="Kein verifizierter Pionex-Open-Interest-Endpunkt in PionexClient.",
        )

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        if not self._native_fallback:
            return await super().set_leverage(symbol, leverage)
        raise AdapterFeatureNotImplementedError(
            self.exchange_id.value,
            "set_leverage",
            detail="Erfordert einen authentifizierten Pionex-Endpunkt (nicht implementiert).",
        )

    async def get_positions(self) -> list[Any]:
        if not self._native_fallback:
            return await super().get_positions()
        # Analog zum bestehenden Spot-only-Vertrag (siehe
        # CCXTBaseAdapter.get_positions() Docstring): kein authentifizierter
        # Positions-Endpunkt verfuegbar -> leere Liste ist das korrekte,
        # nicht-fehlerhafte Ergebnis. PAPER-Positionen werden ohnehin
        # ausschliesslich lokal von PortfolioEngine gefuehrt, nicht von der
        # Exchange abgefragt.
        return []

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        if not self._native_fallback:
            return await super().cancel_all_orders(symbol)
        # PAPER-Grid-Orders fuellen ueber CCXTBaseAdapter._simulate_order()
        # sofort und hinterlassen nie eine offene Exchange-seitige Order
        # (siehe sgr/execution/grid_controller.py Modul-Docstring) - "0
        # storniert" ist hier faktisch korrekt, kein Blindflug-No-Op wie
        # bei einer echten LIVE-Exchange.
        return 0

    async def get_open_orders(self, symbol: str | None = None) -> list[Any]:
        if not self._native_fallback:
            return await super().get_open_orders(symbol)
        return []

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        if not self._native_fallback:
            return await super().cancel_order(order_id, symbol)
        return True

    async def get_order(self, order_id: str, symbol: str) -> Any:
        if not self._native_fallback:
            return await super().get_order(order_id, symbol)
        from sgr.exchanges.base import OrderNotFoundError

        raise OrderNotFoundError(self.exchange_id.value, order_id)

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @classmethod
    def from_config(
        cls,
        trading_mode: TradingMode,
        futures_mode: bool = False,
    ) -> PionexAdapter:
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
                futures_mode=futures_mode,
            )

        credentials = config.credentials.get_credentials(
            "pionex",
            trading_mode,
        )

        return cls(
            api_key=credentials["apiKey"],
            secret=credentials["secret"],
            trading_mode=trading_mode,
            futures_mode=futures_mode,
        )
