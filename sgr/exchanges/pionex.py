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

PIONEX PRIVATE API INTEGRATION (READ ONLY) - Folge-Erweiterung:
    sgr.exchanges.pionex_client.PionexClient signiert jetzt zusätzlich
    private (authentifizierte) Pionex-Endpunkte (siehe dortiger Modul-
    Docstring: HMAC-SHA256, PIONEX-KEY/PIONEX-SIGNATURE-Header, verifiziert
    gegen das offizielle Pionex-OpenAPI-Repository und den dokumentierten
    Signing-Beispielrechner). Dieser Adapter nutzt das jetzt, um LIVE
    (!) eine echte, aber AUSSCHLIESSLICH LESENDE Verbindung aufzubauen:
    connect() akzeptiert LIVE jetzt auch ohne ccxt-Pionex-ID (native
    Fallback, wie PAPER bereits zuvor) und verifiziert dabei sofort echte
    Credentials per authentifiziertem Balance-Abruf (fail-fast bei
    falschem Key/Secret/IP-Whitelist).

    KEINE ÄNDERUNG AN DER ORDER-SUBMISSION-SPERRE: place_order() prüft
    JETZT EXPLIZIT (statt implizit durch einen fehlenden self._ccxt
    auszufallen) und blockiert JEDE LIVE-Order sofort, bevor irgendein
    Signing-/Netzwerk-Code erreicht wird - unabhängig davon, dass die
    Verbindung selbst jetzt technisch für LIVE zustande kommt.
    cancel_order()/cancel_all_orders() sind aus demselben Grund für LIVE
    ebenfalls weiterhin blockiert (Schreib-Endpunkte, nicht Teil dieser
    Read-Only-Erweiterung) - sie geben NICHT mehr stillschweigend
    "erfolgreich" zurück, das wäre bei einem echten Account irreführend
    (siehe dortiger Kommentar).

    sgr.exchanges.capabilities/sgr.compliance/sgr.risk bleiben von dieser
    Erweiterung komplett unberührt - kein Grid-Scheduler, kein
    automatisches Order-Placement, kein Kill-Switch-Bypass. Diese
    Erweiterung macht ausschließlich sichtbar, was auf einem echten
    Pionex-Account tatsächlich vorhanden ist (Balance, Positionen, offene
    Orders, Order-Status, Funding, Leverage/Margin) - sie handelt nicht.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sgr.core.types import (
    AssetClass,
    Candle,
    ExchangeID,
    FundingRate,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    PositionSide,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import (
    AdapterFeatureNotImplementedError,
    Balance,
    ExchangeAuthenticationError,
    ExchangeConnectionError,
    ExchangeError,
    ExchangeInfo,
    MarketStatus,
    NotSupportedFeatureError,
    OpenInterest,
    OrderNotFoundError,
    PositionModeInfo,
    RateLimitError,
    SymbolLimits,
    SymbolNotFoundError,
    TickerData,
)
from sgr.exchanges.ccxt_base import CCXTBaseAdapter
from sgr.exchanges.pionex_client import PionexAPIError, PionexAuthenticationError, PionexHTTPError

# Pionex-Fehlercodes, die eine Authentifizierungs-/Berechtigungs-Ablehnung
# darstellen (siehe sgr/exchanges/pionex_client.py Modul-Docstring und die
# "Error Code"-Liste der offiziellen Pionex-Dokumentation). INVALIE_APIKEY
# ist ein dokumentierter Tippfehler in Pionex' eigener Error-Code-Liste
# (an anderer Stelle in derselben Dokumentation korrekt als INVALID_APIKEY
# geschrieben) - beide Schreibweisen werden defensiv erkannt.
_AUTH_ERROR_CODES = frozenset(
    {
        "APIKEY_LOST",
        "SIGNATURE_LOST",
        "IP_NOT_WHITELISTED",
        "INVALID_APIKEY",
        "INVALIE_APIKEY",
        "INVALID_SIGNATURE",
        "APIKEY_EXPIRED",
        "INVALID_TIMESTAMP",
        "PERMISSION_DENIED",
    }
)


_FUTURES_SYMBOL_SUFFIX = "_PERP"


def _to_pionex_symbol(ccxt_symbol: str, futures: bool = False) -> str:
    """
    "BTC/USDT" -> "BTC_USDT" (Spot) oder "BTC_USDT_PERP" (Futures/
    Perpetual). Das "_PERP"-Suffix ist Pionex' verifiziertes natives
    Symbolformat fuer Perpetual-Kontrakte - bestaetigt sowohl im
    `symbols`-Parameter der oeffentlichen Futures-Symbols-Endpunkte
    ("Comma-separated symbol list, e.g. BTC_USDT_PERP,ETH_USDT_PERP")
    als auch durchgaengig in JEDEM WebSocket-Beispiel der Futures-API
    (siehe pionex.py Modul-Docstring "Faktenpruefung..." fuer die
    Quelle). futures=False (Default) erhaelt das bisherige Spot-
    Verhalten fuer alle bestehenden Aufrufstellen unveraendert bei.
    """
    base = ccxt_symbol.split(":")[0].replace("/", "_")
    if futures and not base.endswith(_FUTURES_SYMBOL_SUFFIX):
        base += _FUTURES_SYMBOL_SUFFIX
    return base


def _from_pionex_symbol(
    pionex_symbol: str, exchange_id: ExchangeID, asset_class: AssetClass = AssetClass.SPOT
) -> Symbol:
    """ "BTC_USDT" -> Symbol(base="BTC", quote="USDT") bzw. "BTC_USDT_PERP"
    -> dieselbe Symbol (das "_PERP"-Suffix wird vor dem Aufteilen in
    base/quote entfernt - siehe _to_pionex_symbol() Docstring). asset_
    class=FUTURES fuer Futures-Symbole (Positionen/Futures-Orders) -
    Default SPOT bleibt fuer alle bestehenden Aufrufstellen unveraendert.
    """
    cleaned = pionex_symbol
    if cleaned.endswith(_FUTURES_SYMBOL_SUFFIX):
        cleaned = cleaned[: -len(_FUTURES_SYMBOL_SUFFIX)]
    base, _, quote = cleaned.partition("_")
    return Symbol(base=base, quote=quote, exchange=exchange_id, asset_class=asset_class)


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

        LIVE verbindet sich jetzt (Private-API-Integration) auch ohne
        ccxt-Pionex-ID ueber den nativen, signierten Fallback - siehe
        _connect_native_fallback(). Das ist AUSSCHLIESSLICH eine
        Lese-Verbindung: place_order()/cancel_order()/cancel_all_orders()
        bleiben fuer LIVE weiterhin explizit blockiert (siehe dortige
        Overrides), unabhaengig davon, dass connect() selbst jetzt
        gelingt.
        """
        try:
            import ccxt.async_support as ccxt
        except ImportError:
            raise RuntimeError("ccxt not installed. Run: pip install ccxt") from None

        ccxt_has_pionex = hasattr(ccxt, self._ccxt_id)

        if not ccxt_has_pionex:
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
        Fallback ueber PionexClient, wenn ccxt keine "pionex"-ID kennt
        (siehe Modul-Docstring). Fuer PAPER unveraendert: nur oeffentliche
        Marktdaten, keine Credentials an den Client uebergeben (auch wenn
        config.credentials.pionex_paper_* gesetzt waeren - Paper-Handel
        bleibt strikt von jedem echten Account getrennt, siehe
        get_balance()/get_positions() Docstrings unten).

        Fuer LIVE (Private-API-Integration): der native Client bekommt
        die echten Credentials und die Verbindungspruefung wird um einen
        AUTHENTIFIZIERTEN Aufruf erweitert (Account-Balance) - ein
        falscher/abgelaufener Key oder eine fehlende IP-Whitelist-
        Eintragung faellt so SOFORT bei connect() auf (fail-fast), nicht
        erst beim ersten tatsaechlichen Lesezugriff spaeter. Das ist
        bewusst die Balance zwischen "Authentication Tests" (Punkt 1) und
        "Connectivity Tests" (Punkt 10) der Aufgabenstellung - beide
        werden durch denselben Aufruf abgedeckt, ohne zwei separate
        Netzwerk-Roundtrips beim Verbindungsaufbau zu brauchen.
        """
        from sgr.core.logging import get_logger
        from sgr.exchanges.pionex_client import PionexClient

        is_live = self.trading_mode == TradingMode.LIVE
        self._native_client = PionexClient(
            api_key=self._api_key if is_live else None,
            api_secret=self._secret if is_live else None,
        )
        try:
            # Oeffentliche Erreichbarkeitspruefung - unabhaengig vom Modus
            # immer zuerst, damit ein reiner Netzwerk-/DNS-Ausfall nicht
            # als Authentifizierungsfehler missklassifiziert wird.
            await asyncio.to_thread(self._native_client.get_symbols)

            if is_live:
                # Authentifizierte Zusatzpruefung (siehe Docstring oben).
                # futures_mode entscheidet, welcher Account-Typ gelesen
                # wird - beide Endpunkte sind reine GET/Read-Aufrufe.
                if self.futures_mode:
                    await asyncio.to_thread(self._native_client.get_futures_account_balances)
                else:
                    await asyncio.to_thread(self._native_client.get_account_balances)
        except Exception as e:
            self._native_client = None
            raise self._map_pionex_error(e) from e

        self._native_fallback = True
        self._connected = True
        get_logger(__name__).info(
            "exchange.connected",
            exchange=self.exchange_id.value,
            trading_mode=self.trading_mode.value,
            futures_mode=self.futures_mode,
            note=(
                "native_fallback_authenticated_read_only"
                if is_live
                else "native_fallback_public_market_data_only_no_ccxt_pionex"
            ),
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
    # Order submission - EXPLICITLY BLOCKED for LIVE (native fallback).
    #
    # Diese Ueberschreibung ist der zentrale Sicherheitsmechanismus
    # dieser Erweiterung (siehe Modul-Docstring "PIONEX PRIVATE API
    # INTEGRATION"): seit connect() jetzt auch fuer LIVE ohne ccxt-
    # Pionex-ID gelingt (native Fallback), wuerde OHNE dieses Override
    # ein LIVE place_order()-Aufruf in CCXTBaseAdapter.place_order()
    # laufen und dort bei `self._ccxt.create_order(...)` mit einem
    # rohen AttributeError abstuerzen (self._ccxt ist im native Fallback
    # immer None) - kein kontrollierter, aussagekraeftiger Fehler. Diese
    # Methode faengt das VOR jeglichem Signing-/Netzwerk-Code ab, mit
    # derselben AdapterFeatureNotImplementedError wie zuvor beim
    # (jetzt entfernten) fail-fast in connect().
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if not self._native_fallback:
            return await super().place_order(order)

        self._require_connected()

        if order.trading_mode == TradingMode.PAPER:
            # Unveraendert: Paper-Orders fuellen weiterhin ausschliesslich
            # ueber CCXTBaseAdapter._simulate_order() (ruft self.get_ticker()
            # auf, das fuer den nativen Fallback bereits korrekt auf
            # PionexClient umgeleitet ist - siehe dortiger Override).
            return await self._simulate_order(order)

        # LIVE: bewusst und explizit blockiert (siehe Modul-Docstring und
        # Klassen-Kommentar oben) - unabhaengig davon, dass connect() fuer
        # LIVE jetzt gelingt. Diese Sperre bleibt bestehen, bis ein
        # verifizierter, gegen einen echten (oder offiziellen Sandbox-)
        # Account getesteter Order-Submission-Pfad existiert.
        raise AdapterFeatureNotImplementedError(
            self.exchange_id.value,
            "live_order_submission",
            detail=(
                "Pionex LIVE order submission remains intentionally blocked - "
                "this session only implements verified READ-ONLY private API "
                "access (balances, positions, orders, leverage, margin, "
                "funding). See docs/SGR_STRATEGY_REPORT.md for the validation "
                "path required before LIVE order submission can be enabled."
            ),
        )

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
        pionex_symbol = _to_pionex_symbol(symbol, futures=self.futures_mode)
        market_type = "PERP" if self.futures_mode else "SPOT"
        raw = await asyncio.to_thread(self._native_client.get_ticker, pionex_symbol)
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
        pionex_symbol = _to_pionex_symbol(symbol, futures=self.futures_mode)
        raw = await asyncio.to_thread(self._native_client.get_orderbook, pionex_symbol, depth)
        sym = _from_pionex_symbol(
            pionex_symbol,
            self.exchange_id,
            AssetClass.FUTURES if self.futures_mode else AssetClass.SPOT,
        )

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
        pionex_symbol = _to_pionex_symbol(symbol, futures=self.futures_mode)
        sym = _from_pionex_symbol(
            pionex_symbol,
            self.exchange_id,
            AssetClass.FUTURES if self.futures_mode else AssetClass.SPOT,
        )
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
        """
        Oeffentlicher Endpunkt (kein Auth noetig, funktioniert in PAPER
        UND LIVE) - jetzt ueber PionexClient.get_funding_rates() verifiziert
        implementiert (Aufgabenstellung Punkt 8), ersetzt die vorherige
        AdapterFeatureNotImplementedError.
        """
        if not self._native_fallback:
            return await super().get_funding_rate(symbol)
        self._require_connected()
        # Funding Rates existieren per Definition nur fuer Perpetual-
        # Kontrakte - futures=True unabhaengig von self.futures_mode
        # (ein Spot-Adapter kann trotzdem gezielt nach einem Perpetual-
        # Funding-Satz fragen, siehe Protocol-Docstring in base.py).
        pionex_symbol = _to_pionex_symbol(symbol, futures=True)
        try:
            raw = await asyncio.to_thread(
                self._native_client.get_funding_rates, pionex_symbol, None, 1
            )
        except Exception as e:
            raise self._map_pionex_error(e) from e

        rates = raw.get("rates") or []
        if not rates:
            raise SymbolNotFoundError(self.exchange_id.value, symbol)
        latest = rates[0]
        sym = _from_pionex_symbol(pionex_symbol, self.exchange_id, AssetClass.FUTURES)
        return FundingRate(
            symbol=sym,
            timestamp=self._parse_ts(latest.get("fundingTime")),
            rate=self._safe_decimal(latest.get("fundingRate")) or Decimal("0"),
            # Pionex meldet den naechsten Funding-Zeitpunkt hier nicht
            # separat - fundingTime ist der Zeitpunkt DIESER (bereits
            # abgerechneten) Rate. next_funding_time bleibt deshalb best-
            # effort identisch zu fundingTime statt einen ungeprueften
            # Wert zu erfinden (siehe Modul-Docstring "keine Annahmen").
            next_funding_time=self._parse_ts(latest.get("fundingTime")),
        )

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        """Oeffentlicher Endpunkt - jetzt ueber
        PionexClient.get_open_interests() verifiziert implementiert."""
        if not self._native_fallback:
            return await super().get_open_interest(symbol)
        self._require_connected()
        # Open Interest existiert per Definition nur fuer Futures-
        # Kontrakte - futures=True analog zu get_funding_rate().
        pionex_symbol = _to_pionex_symbol(symbol, futures=True)
        try:
            rows = await asyncio.to_thread(self._native_client.get_open_interests)
        except Exception as e:
            raise self._map_pionex_error(e) from e

        for row in rows:
            if row.get("symbol") == pionex_symbol:
                oi = self._safe_decimal(row.get("openInterest")) or Decimal("0")
                return OpenInterest(
                    symbol=symbol,
                    open_interest=oi,
                    # Pionex liefert keinen separaten USD-Wert - best-
                    # effort 0 statt eines erfundenen Umrechnungskurses
                    # (siehe Modul-Docstring "keine Annahmen").
                    open_interest_value=Decimal("0"),
                    timestamp=datetime.now(tz=UTC),
                )
        raise SymbolNotFoundError(self.exchange_id.value, symbol)

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        # Schreib-Endpunkt (POST /uapi/v1/account/leverage) - bewusst
        # NICHT Teil dieser Read-Only-Erweiterung, siehe get_futures_
        # leverage() fuer den Lese-Zugriff auf denselben Wert.
        if not self._native_fallback:
            return await super().set_leverage(symbol, leverage)
        raise AdapterFeatureNotImplementedError(
            self.exchange_id.value,
            "set_leverage",
            detail="Write endpoint - out of scope for the read-only Private API integration.",
        )

    async def get_positions(self) -> list[Position]:
        """
        PAPER: unveraendert leer (siehe bisheriger Kommentar - Paper-
        Positionen werden ausschliesslich lokal von PortfolioEngine
        gefuehrt, nie von einer Exchange abgefragt, auch nicht von einem
        echten Account).

        LIVE + futures_mode: echte, signierte Positionsabfrage
        (Aufgabenstellung Punkt 5). LIVE + Spot (futures_mode=False):
        bleibt [] - Spot kennt konzeptionell keine Positionen (siehe
        Protocol-Docstring), unabhaengig vom Account.
        """
        if not self._native_fallback:
            return await super().get_positions()
        if self.trading_mode != TradingMode.LIVE or not self.futures_mode:
            return []

        self._require_connected()
        try:
            raw_positions = await asyncio.to_thread(self._native_client.get_futures_positions)
        except Exception as e:
            raise self._map_pionex_error(e) from e

        positions: list[Position] = []
        for raw in raw_positions:
            net_size = self._safe_decimal(raw.get("netSize")) or Decimal("0")
            if net_size == 0:
                continue
            side = PositionSide.LONG if raw.get("positionSide") == "LONG" else PositionSide.SHORT
            sym = _from_pionex_symbol(
                str(raw.get("symbol", "")), self.exchange_id, AssetClass.FUTURES
            )
            positions.append(
                Position(
                    symbol=sym,
                    side=side,
                    quantity=abs(net_size),
                    entry_price=self._safe_decimal(raw.get("avgPrice")) or Decimal("0"),
                    current_price=self._safe_decimal(raw.get("markPrice")) or Decimal("0"),
                    leverage=self._safe_decimal(raw.get("leverage")) or Decimal("1"),
                    unrealized_pnl=self._safe_decimal(raw.get("unrealizedPnL")) or Decimal("0"),
                    # Pionex' Position-Endpunkt liefert kein realisiertes
                    # PnL-Feld (nur unrealizedPnL) - 0 ist hier "nicht von
                    # diesem Endpunkt gemeldet", nicht "kein Gewinn/Verlust
                    # realisiert" (siehe Modul-Docstring "keine Annahmen";
                    # realisiertes PnL bleibt Aufgabe von TradeRepository/
                    # PortfolioEngine, die aus tatsaechlichen Fills rechnen).
                    realized_pnl=Decimal("0"),
                    opened_at=self._parse_ts(raw.get("createTime")),
                    strategy_name="unknown",  # von PortfolioEngine angereichert
                    trading_mode=self.trading_mode,
                )
            )
        return positions

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        if not self._native_fallback:
            return await super().cancel_all_orders(symbol)
        if self.trading_mode == TradingMode.LIVE:
            # Schreib-Endpunkt (DELETE /.../allOrders) - bewusst NICHT
            # implementiert (siehe Modul-Docstring). WICHTIG: gibt NICHT
            # mehr stillschweigend 0 zurueck wie zuvor - das waere bei
            # einem ECHTEN Account irrefuehrend (koennte als "keine
            # offenen Orders vorhanden" statt "Stornierung nicht
            # unterstuetzt" missverstanden werden, z.B. waehrend eines
            # Kill-Switch-Ereignisses).
            raise AdapterFeatureNotImplementedError(
                self.exchange_id.value,
                "cancel_all_orders",
                detail="Write endpoint - out of scope for the read-only Private API integration.",
            )
        # PAPER-Grid-Orders fuellen ueber CCXTBaseAdapter._simulate_order()
        # sofort und hinterlassen nie eine offene Exchange-seitige Order
        # (siehe sgr/execution/grid_controller.py Modul-Docstring) - "0
        # storniert" ist hier faktisch korrekt, kein Blindflug-No-Op wie
        # bei einer echten LIVE-Exchange.
        return 0

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        """
        PAPER: unveraendert leer (keine resting Orders in der Simulation).

        LIVE: echte, signierte Abfrage (Aufgabenstellung Punkt 6). Spot
        verlangt laut Pionex-API zwingend ein `symbol` (kein "alle
        Symbole"-Endpunkt existiert dafuer) - ohne symbol wird das als
        klare, dokumentierte API-Einschraenkung gemeldet statt eine
        Annahme zu raten. Futures erlaubt symbol=None (alle Symbole).
        """
        if not self._native_fallback:
            return await super().get_open_orders(symbol)
        if self.trading_mode != TradingMode.LIVE:
            return []

        self._require_connected()
        try:
            if self.futures_mode:
                pionex_symbol = _to_pionex_symbol(symbol, futures=True) if symbol else None
                raw_orders = await asyncio.to_thread(
                    self._native_client.get_futures_open_orders, pionex_symbol
                )
            else:
                if not symbol:
                    raise AdapterFeatureNotImplementedError(
                        self.exchange_id.value,
                        "get_open_orders_without_symbol",
                        detail=(
                            "Pionex' Spot-openOrders-Endpunkt verlangt zwingend ein "
                            "symbol-Parameter - es existiert kein dokumentierter "
                            "'alle Symbole'-Endpunkt dafuer."
                        ),
                    )
                pionex_symbol = _to_pionex_symbol(symbol)
                raw_orders = await asyncio.to_thread(
                    self._native_client.get_spot_open_orders, pionex_symbol
                )
        except AdapterFeatureNotImplementedError:
            raise
        except Exception as e:
            raise self._map_pionex_error(e) from e

        asset_class = AssetClass.FUTURES if self.futures_mode else AssetClass.SPOT
        return [self._parse_native_order(raw, asset_class) for raw in raw_orders]

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        if not self._native_fallback:
            return await super().cancel_order(order_id, symbol)
        if self.trading_mode == TradingMode.LIVE:
            # Wie cancel_all_orders(): Schreib-Endpunkt, bewusst nicht
            # implementiert, gibt NICHT mehr stillschweigend True zurueck.
            raise AdapterFeatureNotImplementedError(
                self.exchange_id.value,
                "cancel_order",
                detail="Write endpoint - out of scope for the read-only Private API integration.",
            )
        return True

    async def get_order(self, order_id: str, symbol: str) -> OrderResult:
        """
        PAPER: unveraendert OrderNotFoundError (keine resting Orders).

        LIVE: echte, signierte Order-Status-Abfrage (Aufgabenstellung
        Punkt 7). Spot benoetigt nur orderId (kein symbol), Futures
        benoetigt beides (siehe PionexClient-Methoden-Docstrings).
        """
        if not self._native_fallback:
            return await super().get_order(order_id, symbol)
        if self.trading_mode != TradingMode.LIVE:
            raise OrderNotFoundError(self.exchange_id.value, order_id)

        self._require_connected()
        try:
            numeric_id = int(order_id)
        except (TypeError, ValueError) as e:
            raise OrderNotFoundError(self.exchange_id.value, order_id) from e

        try:
            if self.futures_mode:
                raw = await asyncio.to_thread(
                    self._native_client.get_futures_order,
                    _to_pionex_symbol(symbol, futures=True),
                    numeric_id,
                )
            else:
                raw = await asyncio.to_thread(self._native_client.get_spot_order, numeric_id)
        except Exception as e:
            raise self._map_pionex_error(e) from e

        asset_class = AssetClass.FUTURES if self.futures_mode else AssetClass.SPOT
        return self._parse_native_order(raw, asset_class)

    def _parse_native_order(self, raw: dict[str, Any], asset_class: AssetClass) -> OrderResult:
        """Uebersetzt eine rohe Pionex-Order (Spot oder Futures, beide
        Schemas sind kompatibel genug fuer eine gemeinsame Abbildung) in
        ein SGR OrderResult."""
        status_map = {"OPEN": OrderStatus.SUBMITTED, "CLOSED": OrderStatus.FILLED}
        filled_size = self._safe_decimal(raw.get("filledSize")) or Decimal("0")
        size = self._safe_decimal(raw.get("size")) or Decimal("0")
        status = status_map.get(str(raw.get("status")), OrderStatus.SUBMITTED)
        if status == OrderStatus.SUBMITTED and 0 < filled_size < size:
            status = OrderStatus.PARTIALLY_FILLED

        sym = _from_pionex_symbol(str(raw.get("symbol", "")), self.exchange_id, asset_class)
        avg_price = None
        if filled_size > 0:
            filled_amount = self._safe_decimal(raw.get("filledAmount"))
            if filled_amount is not None:
                avg_price = filled_amount / filled_size

        return OrderResult(
            # Kein SGR-seitiger OrderRequest existiert fuer eine von der
            # Exchange abgefragte (nicht von SGR selbst ausgeloeste)
            # Order - analoges Muster wie CCXTBaseAdapter.get_order()/
            # get_open_orders() (mock_request mit frischer uuid4()).
            request_id=uuid4(),
            exchange_order_id=str(raw.get("orderId", "")),
            symbol=sym,
            status=status,
            filled_quantity=filled_size,
            average_fill_price=avg_price or self._safe_decimal(raw.get("price")),
            fees=self._safe_decimal(raw.get("fee")) or Decimal("0"),
            fee_currency=str(raw.get("feeCoin") or "USDT"),
            submitted_at=self._parse_ts(raw.get("createTime")),
            filled_at=self._parse_ts(raw.get("updateTime")) if filled_size > 0 else None,
            trading_mode=self.trading_mode,
            raw_response=raw,
        )

    async def get_balance(self) -> Balance:
        """
        PAPER: synthetische leere Balance (Decimal(0) ueberall) - Paper-
        Kapital ist virtuell und wird ausschliesslich von PortfolioEngine
        gefuehrt (siehe sgr.core.config.paper_initial_capital), NIE von
        einer Exchange abgefragt. Ein echter Account-Aufruf mit den
        Dummy-Paper-Credentials waere entweder sinnlos (keine echten
        Credentials) oder - schlimmer - wuerde bei zufaellig echten,
        aber falsch konfigurierten Paper-Credentials ein reales Konto
        exponieren. Kein Client wird dafuer ueberhaupt mit Keys gebaut
        (siehe _connect_native_fallback()).

        LIVE: echte, signierte Balance-Abfrage (Aufgabenstellung Punkt
        2/3) - Spot oder Futures je nach futures_mode.
        """
        if not self._native_fallback:
            return await super().get_balance()

        if self.trading_mode != TradingMode.LIVE:
            return Balance(
                total=Decimal("0"),
                free=Decimal("0"),
                used=Decimal("0"),
                assets={},
                timestamp=datetime.now(tz=UTC),
            )

        self._require_connected()
        try:
            if self.futures_mode:
                raw = await asyncio.to_thread(self._native_client.get_futures_account_balances)
                rows = raw.get("balances", [])
            else:
                rows = await asyncio.to_thread(self._native_client.get_account_balances)
        except Exception as e:
            raise self._map_pionex_error(e) from e

        assets: dict[str, Decimal] = {}
        total_usdt = free_usdt = used_usdt = Decimal("0")
        for row in rows:
            coin = str(row.get("coin", ""))
            free = self._safe_decimal(row.get("free")) or Decimal("0")
            frozen = self._safe_decimal(row.get("frozen")) or Decimal("0")
            coin_total = free + frozen
            if coin_total > 0:
                assets[coin] = coin_total
            if coin == "USDT":
                total_usdt, free_usdt, used_usdt = coin_total, free, frozen

        return Balance(
            total=total_usdt,
            free=free_usdt,
            used=used_usdt,
            assets=assets,
            timestamp=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # Pionex-spezifische Lese-Erweiterungen (KEIN Teil des gemeinsamen
    # ExchangeAdapter-Protocols - siehe Aufgabenstellung "Keine Pionex
    # Sonderlogik in Strategy Engine oder Risk Engine": diese Methoden
    # werden ausschliesslich von diesem Adapter selbst, von Tests und von
    # kuenftigen, noch zu bauenden Diagnose-/Reconciliation-Werkzeugen
    # aufgerufen - niemals von Strategy/Risk Engine.
    # ------------------------------------------------------------------

    async def get_futures_leverage(self, symbol: str) -> Decimal:
        """Aktuell auf dem Account gesetzte Leverage fuer ein Futures-
        Symbol (Aufgabenstellung Punkt 9). LIVE + futures_mode only."""
        self._require_live_futures_native("get_futures_leverage")
        try:
            raw = await asyncio.to_thread(
                self._native_client.get_futures_leverage,
                _to_pionex_symbol(symbol, futures=True),
            )
        except Exception as e:
            raise self._map_pionex_error(e) from e
        return self._safe_decimal(raw.get("leverage")) or Decimal("0")

    async def get_futures_margin_mode(self, symbol: str) -> str:
        """Aktueller Cross/Isolated-Margin-Modus fuer ein Futures-Symbol
        (Aufgabenstellung Punkt 9). LIVE + futures_mode only."""
        self._require_live_futures_native("get_futures_margin_mode")
        try:
            raw = await asyncio.to_thread(
                self._native_client.get_futures_margin_mode,
                _to_pionex_symbol(symbol, futures=True),
            )
        except Exception as e:
            raise self._map_pionex_error(e) from e
        return str(raw.get("isolatedMode", "UNKNOWN"))

    async def get_futures_funding_fee_history(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Tatsaechlich auf dem Account belastete/gutgeschriebene Funding-
        Zahlungen (Aufgabenstellung Punkt 8) - im Unterschied zu
        get_funding_rate() (oeffentlicher Markt-Satz, kontounabhaengig).
        LIVE + futures_mode only."""
        self._require_live_futures_native("get_futures_funding_fee_history")
        pionex_symbol = _to_pionex_symbol(symbol, futures=True) if symbol else None
        try:
            return await asyncio.to_thread(
                self._native_client.get_futures_funding_fees, pionex_symbol, None, None, limit
            )
        except Exception as e:
            raise self._map_pionex_error(e) from e

    def _require_live_futures_native(self, feature: str) -> None:
        self._require_connected()
        if not self._native_fallback:
            raise AdapterFeatureNotImplementedError(
                self.exchange_id.value,
                feature,
                detail="Only available via the native (non-ccxt) fallback.",
            )
        if self.trading_mode != TradingMode.LIVE or not self.futures_mode:
            raise AdapterFeatureNotImplementedError(
                self.exchange_id.value,
                feature,
                detail="Requires LIVE trading_mode and futures_mode=True.",
            )

    async def detect_capabilities(self) -> dict[str, bool]:
        """
        Laufzeit-Capability-Detection auf Basis TATSAECHLICHER Pionex-
        API-Antworten fuer DIESE konkrete Verbindung (Aufgabenstellung
        Punkt 12) - ergaenzt sgr.exchanges.capabilities (die statische
        "kann diese Exchange/dieses Produkt technisch existieren"-
        Tabelle) um eine Bestaetigung "funktioniert das fuer DIESEN
        Account tatsaechlich". Rein lesend, wirft keine Exceptions -
        jeder Teilcheck wird einzeln best-effort versucht und als
        True/False im Ergebnis-Dict abgebildet, niemals als Fehler nach
        aussen propagiert (Diagnose-Werkzeug, kein Preflight-Gate).
        """
        result = {
            "connected": self._connected,
            "public_market_data": False,
            "authenticated_account_read": False,
            "futures_account_read": False,
            "futures_positions_read": False,
            "futures_open_orders_read": False,
        }
        if not self._native_fallback or self._native_client is None:
            return result

        try:
            await asyncio.to_thread(self._native_client.get_symbols)
            result["public_market_data"] = True
        except Exception:
            pass

        if self.trading_mode != TradingMode.LIVE:
            return result

        try:
            await self.get_balance()
            result["authenticated_account_read"] = True
        except Exception:
            pass

        if self.futures_mode:
            try:
                await asyncio.to_thread(self._native_client.get_futures_account_balances)
                result["futures_account_read"] = True
            except Exception:
                pass
            try:
                await self.get_positions()
                result["futures_positions_read"] = True
            except Exception:
                pass
            try:
                await asyncio.to_thread(self._native_client.get_futures_open_orders)
                result["futures_open_orders_read"] = True
            except Exception:
                pass

        return result

    def _map_pionex_error(self, exc: Exception) -> ExchangeError:
        """
        Uebersetzt PionexClient-Fehler (PionexAPIError/PionexHTTPError/
        PionexAuthenticationError) in klassifizierte SGR-Exchange-Fehler -
        analog zu CCXTBaseAdapter._map_error() fuer den ccxt-Pfad. Jede
        native Fallback-Methode, die den PionexClient aufruft, faengt
        dessen Exceptions ab und reicht sie durch diese Methode weiter,
        statt eine rohe Client-Exception nach oben durchsickern zu lassen.
        """
        if isinstance(exc, PionexAuthenticationError):
            return ExchangeAuthenticationError(self.exchange_id.value, str(exc))

        if isinstance(exc, PionexHTTPError):
            if exc.status_code == 429:
                return RateLimitError(self.exchange_id.value)
            if exc.status_code == 401:
                return ExchangeAuthenticationError(self.exchange_id.value, str(exc))
            if exc.status_code == 0 or exc.status_code >= 500:
                return ExchangeConnectionError(self.exchange_id.value, str(exc))
            return ExchangeError(str(exc), self.exchange_id.value, retryable=False)

        if isinstance(exc, PionexAPIError):
            code = str(exc.code) if exc.code is not None else ""
            if code in _AUTH_ERROR_CODES:
                return ExchangeAuthenticationError(self.exchange_id.value, str(exc), code=code)
            if code == "TRADE_ORDER_NOT_FOUND":
                return OrderNotFoundError(self.exchange_id.value, "unknown")
            if code == "TRADE_INVALID_SYMBOL":
                return SymbolNotFoundError(self.exchange_id.value, "unknown")
            if "RATE" in code or "LIMIT" in code:
                # Kein dokumentierter Rate-Limit-Fehlercode gefunden (siehe
                # sgr/exchanges/pionex_client.py Modul-Docstring "Verified
                # against..."), aber Pionex kann Rate-Limit-Verletzungen
                # theoretisch auch ueber einen ERROR-CODE statt (nur) HTTP
                # 429 signalisieren - defensiv erkannt statt ignoriert.
                return RateLimitError(self.exchange_id.value)
            return ExchangeError(str(exc), self.exchange_id.value, retryable=False)

        return ExchangeConnectionError(self.exchange_id.value, str(exc))

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
