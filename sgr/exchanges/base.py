"""
SGR Exchange Layer – Abstract Protocol
=======================================
Defines the contract every exchange adapter must implement.

Design principles:
- Protocol (structural subtyping) statt ABC: mypy-kompatibel, kein Inheritance-Zwang
- Alle Methoden async: passt in AsyncIO-Architektur, kein Blocking erlaubt
- Einheitliches Return-Format: eigene Domain-Types, niemals CCXT-Rohdaten nach oben
- Fehlerklassifizierung: transient (retry sinnvoll) vs. permanent (sofort eskalieren)
- Rate Limit Awareness: jeder Adapter kennt seine Limits und tracked Requests

Warum Protocol statt ABC?
  ABCs erzwingen Inheritance – das koppelt Module an Klassen-Hierarchie.
  Protocol erlaubt Duck Typing: jeder der das Interface erfüllt ist kompatibel,
  ohne von ExchangeAdapter zu erben. Besser für Plugin-Systeme und Testing.

Adapter Verantwortlichkeiten:
  ✓ CCXT-Responses in Domain-Types übersetzen
  ✓ Exchange-spezifische Fehler klassifizieren
  ✓ Rate Limits tracken und respektieren
  ✓ Retry-Logik für transiente Fehler
  ✗ Kein Business Logic (keine Signale, keine Risk-Checks)
  ✗ Keine Persistenz (das ist Aufgabe der darüberliegenden Schichten)
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from sgr.core.types import (
    Candle,
    ExchangeID,
    FundingRate,
    OrderBook,
    OrderRequest,
    OrderResult,
    Position,
    TradingMode,
)

# ---------------------------------------------------------------------------
# Exchange-specific domain types (not in core to avoid circular deps)
# ---------------------------------------------------------------------------


class Balance:
    """
    Portfolio balance snapshot from an exchange.
    All values in the quote currency (usually USDT).
    """

    __slots__ = ("total", "free", "used", "assets", "timestamp")

    def __init__(
        self,
        total: Decimal,
        free: Decimal,
        used: Decimal,
        assets: dict[str, Decimal],
        timestamp: datetime,
    ) -> None:
        self.total = total
        self.free = free
        self.used = used
        self.assets = assets  # {"BTC": Decimal("0.5"), "USDT": Decimal("10000")}
        self.timestamp = timestamp

    def __repr__(self) -> str:
        return f"Balance(total={self.total}, free={self.free}, assets={len(self.assets)})"


class TickerData:
    """Real-time ticker snapshot for a symbol."""

    __slots__ = ("symbol", "bid", "ask", "last", "volume_24h", "change_24h_pct", "timestamp")

    def __init__(
        self,
        symbol: str,
        bid: Decimal,
        ask: Decimal,
        last: Decimal,
        volume_24h: Decimal,
        change_24h_pct: float,
        timestamp: datetime,
    ) -> None:
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.last = last
        self.volume_24h = volume_24h
        self.change_24h_pct = change_24h_pct
        self.timestamp = timestamp


class ExchangeInfo:
    """Static exchange metadata (symbols, limits, fees)."""

    __slots__ = ("exchange_id", "symbols", "timeframes", "maker_fee", "taker_fee", "fetched_at")

    def __init__(
        self,
        exchange_id: ExchangeID,
        symbols: list[str],
        timeframes: list[str],
        maker_fee: Decimal,
        taker_fee: Decimal,
        fetched_at: datetime,
    ) -> None:
        self.exchange_id = exchange_id
        self.symbols = symbols
        self.timeframes = timeframes
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.fetched_at = fetched_at


class OpenInterest:
    """Futures open interest for a symbol."""

    __slots__ = ("symbol", "open_interest", "open_interest_value", "timestamp")

    def __init__(
        self,
        symbol: str,
        open_interest: Decimal,
        open_interest_value: Decimal,
        timestamp: datetime,
    ) -> None:
        self.symbol = symbol
        self.open_interest = open_interest
        self.open_interest_value = open_interest_value
        self.timestamp = timestamp


# ---------------------------------------------------------------------------
# Exchange Errors (classified)
# ---------------------------------------------------------------------------


class ExchangeError(Exception):
    """Base class for all exchange errors."""

    def __init__(self, message: str, exchange: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.exchange = exchange
        self.retryable = retryable


class RateLimitError(ExchangeError):
    """Rate limit hit. Always retryable with backoff."""

    def __init__(self, exchange: str, retry_after_seconds: float = 1.0) -> None:
        super().__init__(
            f"Rate limit exceeded on {exchange}. Retry after {retry_after_seconds}s",
            exchange=exchange,
            retryable=True,
        )
        self.retry_after_seconds = retry_after_seconds


class InsufficientFundsError(ExchangeError):
    """Not enough balance. Not retryable."""

    def __init__(self, exchange: str, required: Decimal, available: Decimal) -> None:
        super().__init__(
            f"Insufficient funds on {exchange}: need {required}, have {available}",
            exchange=exchange,
            retryable=False,
        )
        self.required = required
        self.available = available


class OrderNotFoundError(ExchangeError):
    """Order ID not found on exchange. Not retryable."""

    def __init__(self, exchange: str, order_id: str) -> None:
        super().__init__(
            f"Order {order_id} not found on {exchange}",
            exchange=exchange,
            retryable=False,
        )
        self.order_id = order_id


class SymbolNotFoundError(ExchangeError):
    """Symbol not supported on exchange."""

    def __init__(self, exchange: str, symbol: str) -> None:
        super().__init__(
            f"Symbol {symbol} not supported on {exchange}",
            exchange=exchange,
            retryable=False,
        )


class ExchangeConnectionError(ExchangeError):
    """Network/connection issue. Always retryable."""

    def __init__(self, exchange: str, detail: str) -> None:
        super().__init__(
            f"Connection error on {exchange}: {detail}",
            exchange=exchange,
            retryable=True,
        )


class ExchangeMaintenanceError(ExchangeError):
    """Exchange under maintenance. Retryable after delay."""

    def __init__(self, exchange: str) -> None:
        super().__init__(
            f"{exchange} is under maintenance",
            exchange=exchange,
            retryable=True,
        )


class NotSupportedFeatureError(ExchangeError):
    """
    Endpoint/feature not supported by this exchange (e.g. Futures-Endpunkte
    wie fetchPositions/fetchFundingRate/fetchOpenInterest bei primär
    Spot-Exchanges wie Pionex). Nicht retryable – ein erneuter Versuch
    würde am gleichen Ergebnis scheitern.
    """

    def __init__(self, exchange: str, feature: str) -> None:
        super().__init__(
            f"{feature} wird von {exchange} nicht unterstützt "
            f"(vermutlich Spot-only Exchange ohne Futures-API).",
            exchange=exchange,
            retryable=False,
        )
        self.feature = feature


# ---------------------------------------------------------------------------
# The Protocol (Contract)
# ---------------------------------------------------------------------------


@runtime_checkable
class ExchangeAdapter(Protocol):
    """
    Contract for all exchange adapters.

    Every method must:
    - Be async (non-blocking)
    - Return typed domain objects (not CCXT raw dicts)
    - Raise classified ExchangeError subclasses on failure
    - Handle rate limiting internally (retry or raise RateLimitError)

    trading_mode is set at construction time and never changes.
    An adapter instance is always either paper OR live – never both.
    """

    exchange_id: ExchangeID
    trading_mode: TradingMode

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """
        Initialize connection to exchange (load markets, verify credentials).
        Must be called before any other method.
        Raises ExchangeConnectionError if unreachable.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and clean up resources. Idempotent."""
        ...

    @abstractmethod
    async def ping(self) -> float:
        """
        Health check. Returns round-trip latency in milliseconds.
        Raises ExchangeConnectionError if unreachable.
        """
        ...

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_exchange_info(self) -> ExchangeInfo:
        """
        Fetch supported symbols, timeframes, fee structure.
        Cached internally – not fetched on every call.
        """
        ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> TickerData:
        """Current bid/ask/last/volume for a symbol."""
        ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        """
        Current order book snapshot.
        depth: number of levels per side (max varies by exchange).
        """
        ...

    @abstractmethod
    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        """
        Historical OHLCV candles.
        since: fetch from this timestamp (UTC). None = most recent.
        limit: max candles per request (exchange-dependent max).
        Returns list ordered by timestamp ascending.
        """
        ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """
        Current funding rate for a futures symbol.
        Raises SymbolNotFoundError for spot-only symbols.
        """
        ...

    @abstractmethod
    async def get_open_interest(self, symbol: str) -> OpenInterest:
        """
        Current open interest for a futures symbol.
        Raises SymbolNotFoundError for spot-only symbols.
        """
        ...

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_balance(self) -> Balance:
        """
        Current account balance.
        Requires valid API credentials.
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """
        All open positions (futures).
        Returns empty list for spot accounts with no open positions.
        """
        ...

    # ------------------------------------------------------------------
    # Order Management
    # ------------------------------------------------------------------

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Submit an order to the exchange.
        Paper mode: simulates fill, never touches real exchange.
        Live mode: submits to real exchange API.

        Raises:
            InsufficientFundsError: not enough balance
            SymbolNotFoundError: symbol not on this exchange
            ExchangeError: other exchange-side rejection
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """
        Cancel an open order.
        Returns True if cancelled, False if already filled/cancelled.
        Raises OrderNotFoundError if order_id doesn't exist.
        """
        ...

    @abstractmethod
    async def get_order(self, order_id: str, symbol: str) -> OrderResult:
        """
        Fetch current status of an order.
        Use for fill monitoring after place_order.
        Raises OrderNotFoundError if not found.
        """
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        """
        All currently open (unfilled) orders.
        symbol: filter by symbol, None = all symbols.
        """
        ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """
        Cancel all open orders.
        symbol: limit to one symbol, None = all symbols.
        Returns number of orders cancelled.
        Used by Kill Switch.
        """
        ...
