"""
SGR CCXT Base Adapter
=====================
Shared implementation for all CCXT-backed exchange adapters.

Concrete adapters (Binance, Pionex, ...) extend this class and only override
what's exchange-specific. Common logic lives here once:
  - CCXT initialization + testnet routing
  - Retry logic with exponential backoff (tenacity)
  - Rate limit tracking
  - Domain type conversion (CCXT → SGR types)
  - Error mapping (CCXT exceptions → SGR exceptions)

Why not one class per exchange?
  CCXT already abstracts the HTTP layer. What differs per exchange:
    - Testnet URLs
    - Which endpoints exist (some lack funding rates, OI, etc.)
    - Fee structure quirks
    - Symbol format edge cases
  Everything else is identical → one base class, minimal subclasses.

Threading model:
  CCXT async client (ccxt.pro or ccxt.async_support) is not thread-safe.
  We create one instance per adapter, used only in the AsyncIO event loop.
  Never share adapters across coroutines concurrently without locking.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from sgr.core.logging import get_logger
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
    OrderType,
    Position,
    PositionSide,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import (
    Balance,
    ExchangeConnectionError,
    ExchangeError,
    ExchangeInfo,
    ExchangeMaintenanceError,
    InsufficientFundsError,
    NotSupportedFeatureError,
    OpenInterest,
    OrderNotFoundError,
    RateLimitError,
    SymbolNotFoundError,
    TickerData,
)

log = get_logger(__name__)

# Retry only on transient errors
_RETRYABLE = lambda e: isinstance(e, ExchangeError) and e.retryable  # noqa: E731


def _retryable_exchange_call(max_attempts: int = 3) -> Any:
    """Decorator factory for retrying transient exchange errors."""
    return retry(
        retry=retry_if_exception(_RETRYABLE),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=10),
        reraise=True,
    )


class CCXTBaseAdapter:
    """
    Base adapter for CCXT-backed exchanges.
    Subclasses must set:
        exchange_id: ExchangeID
        _ccxt_id: str  (CCXT exchange string, e.g. "binance")
        _testnet_urls: dict  (optional, for paper mode)
    """

    exchange_id: ExchangeID
    _ccxt_id: str
    _testnet_urls: dict[str, str] = {}

    def __init__(
        self,
        api_key: str,
        secret: str,
        trading_mode: TradingMode,
        extra_options: dict[str, Any] | None = None,
    ) -> None:
        self.trading_mode = trading_mode
        self._api_key = api_key
        self._secret = secret
        self._extra_options = extra_options or {}
        self._ccxt: Any = None  # ccxt.async_support exchange instance
        self._exchange_info: ExchangeInfo | None = None
        self._connected = False

        # Rate limit tracking
        self._request_count = 0
        self._request_window_start = time.monotonic()
        self._rate_limit_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Initialize CCXT instance and load markets.
        Verifies credentials by fetching balance (requires auth).
        """
        try:
            import ccxt.async_support as ccxt
        except ImportError:
            raise RuntimeError("ccxt not installed. Run: pip install ccxt") from None

        exchange_class = getattr(ccxt, self._ccxt_id)

        options: dict[str, Any] = {
            "apiKey": self._api_key,
            "secret": self._secret,
            "enableRateLimit": True,  # CCXT built-in rate limiting (first layer)
            "timeout": 30_000,  # 30s timeout per request
        }

        options.update(self._extra_options)
        self._ccxt = exchange_class(options)

        # Testnet/Sandbox-Routing fuer Paper Mode.
        # WICHTIG: NICHT ueber options["urls"] = {"api": self._testnet_urls}
        # (fruehere Implementierung) - das ueberschreibt nur die explizit
        # gelisteten URL-Namespaces. Bei Binance z.B. nutzt ccxt bereits bei
        # load_markets() intern auch den "sapi"-Namespace (u.a. fuer
        # capital/config/getall), der dabei NICHT auf Testnet zeigen wuerde,
        # sondern weiterhin auf die echte api.binance.com - das fuehrte zu
        # "Invalid Api-Key ID", weil ein echter Testnet-Key gegen die
        # Live-API validiert wurde. set_sandbox_mode(True) ist ccxt's
        # eigener, vollstaendiger Mechanismus, der alle relevanten
        # URL-Namespaces der jeweiligen Exchange konsistent umschaltet.
        # Bei Exchanges ohne konfigurierten Sandbox-Endpoint in ccxt selbst
        # ist dies ein sicheres No-Op (mit ccxt-eigener Warnung).
        if self.trading_mode == TradingMode.PAPER and self._testnet_urls:
            self._ccxt.set_sandbox_mode(True)
            log.info(
                "exchange.testnet_mode",
                exchange=self.exchange_id.value,
                testnet_urls=list(self._testnet_urls.keys()),
            )

        try:
            await self._ccxt.load_markets()
            self._connected = True
            log.info(
                "exchange.connected",
                exchange=self.exchange_id.value,
                trading_mode=self.trading_mode.value,
                symbols_count=len(self._ccxt.symbols),
            )
        except Exception as e:
            raise self._map_error(e) from e

    async def close(self) -> None:
        """Close CCXT session. Safe to call multiple times."""
        if self._ccxt is not None:
            try:
                await self._ccxt.close()
            except Exception:
                pass  # Ignore close errors
            finally:
                self._ccxt = None
                self._connected = False
                log.info("exchange.closed", exchange=self.exchange_id.value)

    async def ping(self) -> float:
        """Measure round-trip latency in milliseconds."""
        self._require_connected()
        start = time.monotonic()
        try:
            await self._ccxt.fetch_time()
            return (time.monotonic() - start) * 1000
        except Exception as e:
            raise ExchangeConnectionError(self.exchange_id.value, str(e)) from e

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    async def get_exchange_info(self) -> ExchangeInfo:
        """Cached exchange info. Refreshes if > 1 hour old."""
        self._require_connected()

        if self._exchange_info is None:
            markets = self._ccxt.markets or {}
            symbols = [s for s in self._ccxt.symbols if "/" in s]
            timeframes = list(getattr(self._ccxt, "timeframes", {}).keys())

            # Extract fees (use BTC/USDT as reference)
            maker_fee = Decimal("0.001")  # default 0.1%
            taker_fee = Decimal("0.001")
            try:
                if "BTC/USDT" in markets:
                    market = markets["BTC/USDT"]
                    maker_fee = Decimal(str(market.get("maker", 0.001)))
                    taker_fee = Decimal(str(market.get("taker", 0.001)))
            except (KeyError, InvalidOperation):
                pass

            self._exchange_info = ExchangeInfo(
                exchange_id=self.exchange_id,
                symbols=symbols,
                timeframes=timeframes,
                maker_fee=maker_fee,
                taker_fee=taker_fee,
                fetched_at=datetime.now(tz=UTC),
            )

        return self._exchange_info

    @_retryable_exchange_call(max_attempts=3)
    async def get_ticker(self, symbol: str) -> TickerData:
        self._require_connected()
        try:
            raw = await self._ccxt.fetch_ticker(symbol)
            return TickerData(
                symbol=symbol,
                bid=Decimal(str(raw.get("bid") or raw.get("last", 0))),
                ask=Decimal(str(raw.get("ask") or raw.get("last", 0))),
                last=Decimal(str(raw.get("last", 0))),
                volume_24h=Decimal(str(raw.get("quoteVolume") or raw.get("baseVolume", 0))),
                change_24h_pct=float(raw.get("percentage") or 0.0),
                timestamp=self._parse_ts(raw.get("timestamp")),
            )
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    @_retryable_exchange_call(max_attempts=3)
    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        self._require_connected()
        try:
            raw = await self._ccxt.fetch_order_book(symbol, limit=depth)
            sym = self._parse_symbol(symbol)

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
                timestamp=self._parse_ts(raw.get("timestamp")),
                bids=bids,
                asks=asks,
            )
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    @_retryable_exchange_call(max_attempts=3)
    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        self._require_connected()
        try:
            since_ms = int(since.timestamp() * 1000) if since else None
            raw = await self._ccxt.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since_ms, limit=limit
            )
            sym = self._parse_symbol(symbol)

            candles = []
            for row in raw:
                ts_ms, open_price, high, low, close, volume = row
                candles.append(
                    Candle(
                        symbol=sym,
                        timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                        timeframe=timeframe,
                        open=Decimal(str(open_price)),
                        high=Decimal(str(high)),
                        low=Decimal(str(low)),
                        close=Decimal(str(close)),
                        volume=Decimal(str(volume)),
                    )
                )

            return sorted(candles, key=lambda c: c.timestamp)

        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    @_retryable_exchange_call(max_attempts=3)
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        self._require_connected()
        self._require_feature("fetchFundingRate")
        try:
            raw = await self._ccxt.fetch_funding_rate(symbol)
            sym = self._parse_symbol(symbol)

            return FundingRate(
                symbol=sym,
                timestamp=self._parse_ts(raw.get("timestamp")),
                rate=Decimal(str(raw.get("fundingRate", 0))),
                next_funding_time=self._parse_ts(raw.get("fundingDatetime")),
            )
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    @_retryable_exchange_call(max_attempts=3)
    async def get_open_interest(self, symbol: str) -> OpenInterest:
        self._require_connected()
        self._require_feature("fetchOpenInterest")
        try:
            raw = await self._ccxt.fetch_open_interest(symbol)
            return OpenInterest(
                symbol=symbol,
                open_interest=Decimal(str(raw.get("openInterest", 0))),
                open_interest_value=Decimal(str(raw.get("openInterestValue", 0))),
                timestamp=self._parse_ts(raw.get("timestamp")),
            )
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    @_retryable_exchange_call(max_attempts=3)
    async def get_balance(self) -> Balance:
        self._require_connected()
        try:
            raw = await self._ccxt.fetch_balance()
            total_usdt = Decimal(str(raw.get("total", {}).get("USDT", 0) or 0))
            free_usdt = Decimal(str(raw.get("free", {}).get("USDT", 0) or 0))
            used_usdt = Decimal(str(raw.get("used", {}).get("USDT", 0) or 0))

            # All non-zero assets
            assets: dict[str, Decimal] = {}
            for currency, amount in (raw.get("total") or {}).items():
                if amount and Decimal(str(amount)) > 0:
                    assets[currency] = Decimal(str(amount))

            return Balance(
                total=total_usdt,
                free=free_usdt,
                used=used_usdt,
                assets=assets,
                timestamp=datetime.now(tz=UTC),
            )
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    @_retryable_exchange_call(max_attempts=3)
    async def get_positions(self) -> list[Position]:
        self._require_connected()
        # Spot-only Exchanges (z.B. Pionex) kennen den Futures-Positionsbegriff
        # nicht. Laut Protokoll-Vertrag ist eine leere Liste hier das korrekte
        # Ergebnis (kein Fehler) – ein Spot-Account hat schlicht nie offene
        # Futures-Positionen.
        has = getattr(self._ccxt, "has", {}) or {}
        if has.get("fetchPositions") is False:
            log.info(
                "exchange.fetch_positions_not_supported",
                exchange=self.exchange_id.value,
                note="spot_only_exchange_returns_empty",
            )
            return []
        try:
            raw_positions = await self._ccxt.fetch_positions()
            positions = []

            for raw in raw_positions:
                contracts = Decimal(str(raw.get("contracts") or 0))
                if contracts <= 0:
                    continue  # Skip empty positions

                side_str = (raw.get("side") or "long").lower()
                sym = self._parse_symbol(raw.get("symbol", "BTC/USDT"))

                positions.append(
                    Position(
                        symbol=sym,
                        side=PositionSide.LONG if side_str == "long" else PositionSide.SHORT,
                        quantity=contracts,
                        entry_price=Decimal(str(raw.get("entryPrice") or 0)),
                        current_price=Decimal(
                            str(raw.get("markPrice") or raw.get("entryPrice") or 0)
                        ),
                        leverage=Decimal(str(raw.get("leverage") or 1)),
                        unrealized_pnl=Decimal(str(raw.get("unrealizedPnl") or 0)),
                        realized_pnl=Decimal(str(raw.get("realizedPnl") or 0)),
                        opened_at=datetime.now(tz=UTC),  # CCXT often lacks this
                        strategy_name="unknown",  # Enriched by Portfolio Engine
                        trading_mode=self.trading_mode,
                    )
                )

            return positions
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    # ------------------------------------------------------------------
    # Order Management
    # ------------------------------------------------------------------

    @_retryable_exchange_call(max_attempts=2)  # Fewer retries for order submission
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Submit order. Paper mode: simulate fill at current price.
        Live mode: submit to exchange API.
        """
        self._require_connected()

        if order.trading_mode == TradingMode.PAPER:
            return await self._simulate_order(order)

        # Live order submission
        try:
            symbol = order.symbol.ccxt_symbol
            side = order.side.value
            order_type = self._map_order_type(order.order_type)
            quantity = float(order.quantity)

            # Idempotency (Baustein 7): order.id ist eine stabile UUID und
            # wird als clientOrderId gesendet. place_order wird bei
            # transienten Fehlern bis zu 1x retryt (max_attempts=2); ohne
            # clientOrderId wuerde ein Retry nach einem Timeout, bei dem die
            # Exchange die erste Order bereits angenommen hatte, zu einer
            # echten Doppel-Order fuehren. Vor jeder Neuerstellung pruefen
            # wir daher zuerst per fetchOrder, ob unter dieser
            # clientOrderId schon eine Order existiert. Siehe auch
            # sgr/execution/order_safety.py Modul-Docstring Punkt 3
            # (Exchange als Quelle der Wahrheit fuer Crash-Resistenz).
            client_order_id = str(order.id)
            params: dict[str, Any] = {"clientOrderId": client_order_id}
            if order.reduce_only:
                params["reduceOnly"] = True

            existing = await self._find_existing_order_by_client_id(
                client_order_id, symbol
            )
            if existing is not None:
                log.info(
                    "exchange.duplicate_order_detected_via_client_id",
                    order_id=client_order_id,
                    exchange_order_id=existing.get("id"),
                )
                return self._parse_order_result(existing, order)

            raw = await self._ccxt.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=quantity,
                price=float(order.limit_price) if order.limit_price else None,
                params=params,
            )

            return self._parse_order_result(raw, order)

        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    async def _find_existing_order_by_client_id(
        self, client_order_id: str, symbol: str
    ) -> dict[str, Any] | None:
        """
        Prueft best-effort, ob unter dieser clientOrderId bereits eine Order
        auf der Exchange existiert (Duplicate-Order-Schutz bei Retries).

        Nur aktiv, wenn der Adapter fetchOrder unterstuetzt (self._ccxt.has).
        Jeder Fehler (nicht gefunden, nicht unterstuetzt, Netzwerkfehler)
        wird als "keine existierende Order gefunden" behandelt - dieser
        Check darf eine Order-Submission niemals blockieren, nur ein
        Duplikat vermeiden.
        """
        if not self._ccxt.has.get("fetchOrder"):
            return None
        try:
            existing = await self._ccxt.fetch_order(
                client_order_id, symbol, params={"clientOrderId": client_order_id}
            )
        except Exception:
            return None
        if not existing:
            return None
        return existing

    async def _simulate_order(self, order: OrderRequest) -> OrderResult:
        """
        Paper trading simulation.
        Simulates a market fill at current mid price + realistic slippage.
        """
        try:
            ticker = await self.get_ticker(order.symbol.ccxt_symbol)
        except Exception:
            # Fallback if ticker fails in simulation
            ticker = None

        # Simulate fill price: market order = ask (buy) or bid (sell) + slippage
        if ticker:
            if order.side == Side.BUY:
                fill_price = ticker.ask * Decimal("1.0005")  # 0.05% slippage
            else:
                fill_price = ticker.bid * Decimal("0.9995")  # 0.05% slippage
        else:
            fill_price = order.limit_price or Decimal("0")

        # Simulate fees (0.1% taker)
        fee_rate = Decimal("0.001")
        fees = order.quantity * fill_price * fee_rate

        now = datetime.now(tz=UTC)

        log.info(
            "exchange.paper_order_simulated",
            symbol=str(order.symbol),
            side=order.side.value,
            quantity=str(order.quantity),
            fill_price=str(fill_price),
            fees=str(fees),
        )

        return OrderResult(
            request_id=order.id,
            exchange_order_id=f"PAPER-{order.id}",
            symbol=order.symbol,
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            average_fill_price=fill_price,
            fees=fees,
            fee_currency="USDT",
            submitted_at=now,
            filled_at=now,
            trading_mode=TradingMode.PAPER,
        )

    @_retryable_exchange_call(max_attempts=3)
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        self._require_connected()
        try:
            await self._ccxt.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            mapped = self._map_error(e)
            if isinstance(mapped, OrderNotFoundError):
                return False
            raise mapped from e

    @_retryable_exchange_call(max_attempts=3)
    async def get_order(self, order_id: str, symbol: str) -> OrderResult:
        self._require_connected()
        try:
            raw = await self._ccxt.fetch_order(order_id, symbol)
            # Parse minimal OrderRequest for context
            from uuid import uuid4

            mock_request = OrderRequest(
                id=raw.get("clientOrderId") or str(uuid4()),  # type: ignore[arg-type]
                signal_id=uuid4(),
                symbol=self._parse_symbol(symbol),
                side=Side(raw.get("side", "buy")),
                order_type=OrderType.LIMIT,
                quantity=Decimal(str(raw.get("amount", 0))),
                trading_mode=self.trading_mode,
            )
            return self._parse_order_result(raw, mock_request)
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    @_retryable_exchange_call(max_attempts=3)
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        self._require_connected()
        try:
            raw_orders = await self._ccxt.fetch_open_orders(symbol)
            results = []
            for raw in raw_orders:
                from uuid import uuid4

                mock_request = OrderRequest(
                    id=uuid4(),
                    signal_id=uuid4(),
                    symbol=self._parse_symbol(raw.get("symbol", "")),
                    side=Side(raw.get("side", "buy")),
                    order_type=OrderType.LIMIT,
                    quantity=Decimal(str(raw.get("amount", 0))),
                    trading_mode=self.trading_mode,
                )
                results.append(self._parse_order_result(raw, mock_request))
            return results
        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    @_retryable_exchange_call(max_attempts=3)
    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """
        Emergency cancel of all orders.
        Called by Kill Switch – must be as reliable as possible.
        """
        self._require_connected()
        try:
            if symbol:
                result = await self._ccxt.cancel_all_orders(symbol)
            else:
                # Cancel per symbol for exchanges that don't support global cancel
                open_orders = await self.get_open_orders()
                symbols_with_orders = list({o.symbol.ccxt_symbol for o in open_orders})
                count = 0
                for sym in symbols_with_orders:
                    try:
                        await self._ccxt.cancel_all_orders(sym)
                        count += len([o for o in open_orders if o.symbol.ccxt_symbol == sym])
                    except Exception as e:
                        log.error(
                            "exchange.cancel_all.symbol_failed",
                            symbol=sym,
                            error=str(e),
                        )
                return count

            if isinstance(result, list):
                return len(result)
            return 0

        except ExchangeError:
            raise
        except Exception as e:
            raise self._map_error(e) from e

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected or self._ccxt is None:
            raise ExchangeConnectionError(
                self.exchange_id.value,
                "Adapter not connected. Call connect() first.",
            )

    def _require_feature(self, ccxt_method: str) -> None:
        """
        Prüft vor Aufruf eines Futures-spezifischen Endpunkts (fetchPositions,
        fetchFundingRate, fetchOpenInterest), ob die Exchange ihn laut CCXT
        `.has`-Property überhaupt unterstützt. Primär-Spot-Exchanges wie
        Pionex unterstützen diese Endpunkte i.d.R. nicht oder nur eingeschränkt.
        Wirft eine klare NotSupportedFeatureError statt einer kryptischen
        ccxt.NotSupported-Exception weiter unten im Stack.
        """
        if self._ccxt is None:
            return
        has = getattr(self._ccxt, "has", {}) or {}
        if has.get(ccxt_method) is False:
            raise NotSupportedFeatureError(self.exchange_id.value, ccxt_method)

    def _parse_symbol(self, ccxt_symbol: str) -> Symbol:
        """
        Parse CCXT symbol string to SGR Symbol.
        "BTC/USDT" → Symbol(base="BTC", quote="USDT")
        "BTC/USDT:USDT" → Symbol(base="BTC", quote="USDT", asset_class=FUTURES)
        """
        # Remove settle currency (futures: "BTC/USDT:USDT")
        symbol_clean = ccxt_symbol.split(":")[0]
        asset_class = AssetClass.FUTURES if ":" in ccxt_symbol else AssetClass.SPOT

        parts = symbol_clean.split("/")
        if len(parts) != 2:
            raise SymbolNotFoundError(self.exchange_id.value, ccxt_symbol)

        return Symbol(
            base=parts[0],
            quote=parts[1],
            exchange=self.exchange_id,
            asset_class=asset_class,
        )

    def _parse_ts(self, ts: Any) -> datetime:
        """Convert CCXT timestamp (ms int or ISO string) to datetime."""
        if ts is None:
            return datetime.now(tz=UTC)
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000, tz=UTC)
        if isinstance(ts, str):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        return datetime.now(tz=UTC)

    def _parse_order_result(self, raw: dict[str, Any], request: OrderRequest) -> OrderResult:
        """Convert CCXT order dict to OrderResult."""
        status_map = {
            "open": OrderStatus.SUBMITTED,
            "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
            "expired": OrderStatus.EXPIRED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
        }

        raw_status = (raw.get("status") or "open").lower()
        status = status_map.get(raw_status, OrderStatus.SUBMITTED)

        # Handle partially filled
        amount = Decimal(str(raw.get("amount") or 0))
        filled = Decimal(str(raw.get("filled") or 0))
        if Decimal(0) < filled < amount:
            status = OrderStatus.PARTIALLY_FILLED

        fees = Decimal(str((raw.get("fee") or {}).get("cost", 0) or 0))
        fee_currency = (raw.get("fee") or {}).get("currency", "USDT") or "USDT"

        avg_price_raw = raw.get("average") or raw.get("price")
        avg_price = Decimal(str(avg_price_raw)) if avg_price_raw else None

        return OrderResult(
            request_id=request.id,
            exchange_order_id=str(raw.get("id", "")),
            symbol=request.symbol,
            status=status,
            filled_quantity=filled,
            average_fill_price=avg_price,
            fees=fees,
            fee_currency=fee_currency,
            submitted_at=self._parse_ts(raw.get("timestamp")),
            filled_at=self._parse_ts(raw.get("lastTradeTimestamp"))
            if raw.get("lastTradeTimestamp")
            else None,
            trading_mode=self.trading_mode,
            raw_response=raw,
        )

    def _map_order_type(self, order_type: OrderType) -> str:
        """Map SGR OrderType to CCXT order type string."""
        mapping = {
            OrderType.MARKET: "market",
            OrderType.LIMIT: "limit",
            OrderType.STOP_MARKET: "stop_market",
            OrderType.STOP_LIMIT: "stop_limit",
            OrderType.TWAP: "limit",  # TWAP split into multiple limits by execution engine
        }
        return mapping.get(order_type, "market")

    def _map_error(self, exc: Exception) -> ExchangeError:
        """
        Map CCXT exceptions to SGR classified exceptions.
        Determines retryability: transient vs permanent errors.
        """
        try:
            import ccxt

            type(exc).__name__
            exc_str = str(exc).lower()

            if isinstance(exc, ccxt.RateLimitExceeded):
                return RateLimitError(self.exchange_id.value)

            # WICHTIG: Spezifischere NetworkError-Subklassen MÜSSEN vor dem
            # generischen NetworkError-Check geprüft werden, da sonst
            # RequestTimeout und OnMaintenance durch die NetworkError-Prüfung
            # abgefangen werden (isinstance-Check trifft auf Basisklasse zu)
            # und nie ihre spezifischere Klassifikation erreichen.
            if isinstance(exc, ccxt.RequestTimeout):
                return ExchangeConnectionError(self.exchange_id.value, f"Timeout: {exc}")

            if isinstance(exc, ccxt.OnMaintenance):
                return ExchangeMaintenanceError(self.exchange_id.value)

            if isinstance(exc, ccxt.NetworkError):
                return ExchangeConnectionError(self.exchange_id.value, str(exc))

            if isinstance(exc, ccxt.InsufficientFunds):
                return InsufficientFundsError(
                    self.exchange_id.value,
                    required=Decimal(0),  # CCXT doesn't always provide these
                    available=Decimal(0),
                )

            if isinstance(exc, ccxt.OrderNotFound):
                return OrderNotFoundError(self.exchange_id.value, "unknown")

            if isinstance(exc, ccxt.BadSymbol):
                return SymbolNotFoundError(self.exchange_id.value, str(exc))

            if isinstance(exc, ccxt.NotSupported):
                return NotSupportedFeatureError(self.exchange_id.value, str(exc))

            # Generic transient if looks like network issue
            if any(kw in exc_str for kw in ["timeout", "connection", "network", "503", "502"]):
                return ExchangeConnectionError(self.exchange_id.value, str(exc))

            # Generic permanent
            return ExchangeError(str(exc), self.exchange_id.value, retryable=False)

        except ImportError:
            return ExchangeError(str(exc), self.exchange_id.value, retryable=False)
