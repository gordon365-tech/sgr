"""
SGR Mock Exchange Adapter
=========================
Vollständige Mock-Implementierung des ExchangeAdapter Protocols.
Für Unit- und Integrationstests: kein echter API-Call, deterministisches Verhalten.

Features:
- Konfigurierbare Responses (was soll fetch_ticker zurückgeben?)
- Fehler-Injection (simuliere RateLimitError, InsufficientFunds, etc.)
- Call-Tracking (wurde place_order aufgerufen? Mit welchen Args?)
- Latenz-Simulation (für Performance-Tests)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
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
    TradingMode,
)
from sgr.exchanges.base import (
    Balance,
    ExchangeError,
    ExchangeInfo,
    OpenInterest,
    TickerData,
)
from sgr.exchanges.ccxt_base import CCXTBaseAdapter


class MockExchangeAdapter(CCXTBaseAdapter):
    """
    Mock adapter for testing.
    Does not require CCXT or real exchange credentials.
    """

    exchange_id = ExchangeID.BINANCE
    _ccxt_id = "binance"

    def __init__(
        self,
        trading_mode: TradingMode = TradingMode.PAPER,
        simulated_latency_ms: float = 0.0,
    ) -> None:
        # Don't call super().__init__() – we bypass CCXT entirely
        self.trading_mode = trading_mode
        self._connected = False
        self._simulated_latency_ms = simulated_latency_ms

        # Call tracking
        self.calls: dict[str, list[dict[str, Any]]] = {
            "connect": [],
            "close": [],
            "ping": [],
            "get_ticker": [],
            "get_orderbook": [],
            "get_ohlcv": [],
            "get_balance": [],
            "get_positions": [],
            "place_order": [],
            "cancel_order": [],
            "cancel_all_orders": [],
        }

        # Configurable responses
        self.ticker_price: Decimal = Decimal("50000")
        self.balance_usdt: Decimal = Decimal("10000")
        self.positions: list[Position] = []
        self.order_fill_status: OrderStatus = OrderStatus.FILLED

        # Error injection
        self._next_error: ExchangeError | None = None

    # ------------------------------------------------------------------
    # Error injection
    # ------------------------------------------------------------------

    def inject_error(self, error: ExchangeError) -> None:
        """Next call to any exchange method will raise this error."""
        self._next_error = error

    async def _maybe_raise(self) -> None:
        if self._simulated_latency_ms > 0:
            await asyncio.sleep(self._simulated_latency_ms / 1000)
        if self._next_error is not None:
            err = self._next_error
            self._next_error = None
            raise err

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self.calls["connect"].append({})
        await self._maybe_raise()
        self._connected = True

    async def close(self) -> None:
        self.calls["close"].append({})
        self._connected = False

    async def ping(self) -> float:
        await self._maybe_raise()
        return self._simulated_latency_ms or 5.0

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    async def get_exchange_info(self) -> ExchangeInfo:
        await self._maybe_raise()
        return ExchangeInfo(
            exchange_id=self.exchange_id,
            symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            timeframes=["1m", "5m", "15m", "1h", "4h", "1d"],
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.001"),
            fetched_at=datetime.now(tz=UTC),
        )

    async def get_ticker(self, symbol: str) -> TickerData:
        self.calls["get_ticker"].append({"symbol": symbol})
        await self._maybe_raise()
        spread = self.ticker_price * Decimal("0.0001")
        return TickerData(
            symbol=symbol,
            bid=self.ticker_price - spread,
            ask=self.ticker_price + spread,
            last=self.ticker_price,
            volume_24h=Decimal("1234567"),
            change_24h_pct=0.5,
            timestamp=datetime.now(tz=UTC),
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        self.calls["get_orderbook"].append({"symbol": symbol, "depth": depth})
        await self._maybe_raise()

        from sgr.core.types import Symbol

        sym = Symbol(base="BTC", quote="USDT", exchange=self.exchange_id)

        bids = [
            OrderBookLevel(
                price=self.ticker_price - Decimal(i),
                size=Decimal("1.0"),
            )
            for i in range(1, depth + 1)
        ]
        asks = [
            OrderBookLevel(
                price=self.ticker_price + Decimal(i),
                size=Decimal("1.0"),
            )
            for i in range(1, depth + 1)
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
        self.calls["get_ohlcv"].append({"symbol": symbol, "timeframe": timeframe, "limit": limit})
        await self._maybe_raise()

        from sgr.core.types import Symbol

        sym = Symbol(base="BTC", quote="USDT", exchange=self.exchange_id)

        now = datetime.now(tz=UTC)
        candles = []
        for i in range(limit):
            price = self.ticker_price + Decimal(i - limit // 2)
            candles.append(
                Candle(
                    symbol=sym,
                    timestamp=now,
                    timeframe=timeframe,
                    open=price,
                    high=price + Decimal("100"),
                    low=price - Decimal("100"),
                    close=price + Decimal("50"),
                    volume=Decimal("1000"),
                )
            )
        return candles

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        await self._maybe_raise()
        from sgr.core.types import Symbol

        sym = Symbol(
            base="BTC", quote="USDT", exchange=self.exchange_id, asset_class=AssetClass.FUTURES
        )
        now = datetime.now(tz=UTC)
        return FundingRate(
            symbol=sym,
            timestamp=now,
            rate=Decimal("0.0001"),
            next_funding_time=now,
        )

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        await self._maybe_raise()
        return OpenInterest(
            symbol=symbol,
            open_interest=Decimal("50000"),
            open_interest_value=Decimal("2500000000"),
            timestamp=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        self.calls["get_balance"].append({})
        await self._maybe_raise()
        return Balance(
            total=self.balance_usdt,
            free=self.balance_usdt * Decimal("0.9"),
            used=self.balance_usdt * Decimal("0.1"),
            assets={"USDT": self.balance_usdt, "BTC": Decimal("0.1")},
            timestamp=datetime.now(tz=UTC),
        )

    async def get_positions(self) -> list[Position]:
        self.calls["get_positions"].append({})
        await self._maybe_raise()
        return self.positions

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResult:
        self.calls["place_order"].append({"order": order})
        await self._maybe_raise()

        fill_price = self.ticker_price
        fees = order.quantity * fill_price * Decimal("0.001")
        now = datetime.now(tz=UTC)

        return OrderResult(
            request_id=order.id,
            exchange_order_id=f"MOCK-{uuid4().hex[:8].upper()}",
            symbol=order.symbol,
            status=self.order_fill_status,
            filled_quantity=order.quantity
            if self.order_fill_status == OrderStatus.FILLED
            else Decimal(0),
            average_fill_price=fill_price,
            fees=fees,
            fee_currency="USDT",
            submitted_at=now,
            filled_at=now if self.order_fill_status == OrderStatus.FILLED else None,
            trading_mode=order.trading_mode,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        self.calls["cancel_order"].append({"order_id": order_id, "symbol": symbol})
        await self._maybe_raise()
        return True

    async def get_order(self, order_id: str, symbol: str) -> OrderResult:
        await self._maybe_raise()
        from sgr.core.types import Symbol

        sym = Symbol(base="BTC", quote="USDT", exchange=self.exchange_id)
        now = datetime.now(tz=UTC)
        return OrderResult(
            request_id=uuid4(),
            exchange_order_id=order_id,
            symbol=sym,
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("1.0"),
            average_fill_price=self.ticker_price,
            fees=Decimal("0.05"),
            submitted_at=now,
            filled_at=now,
            trading_mode=self.trading_mode,
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        await self._maybe_raise()
        return []

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        self.calls["cancel_all_orders"].append({"symbol": symbol})
        await self._maybe_raise()
        return 0

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def call_count(self, method: str) -> int:
        return len(self.calls.get(method, []))

    def last_call(self, method: str) -> dict[str, Any] | None:
        calls = self.calls.get(method, [])
        return calls[-1] if calls else None

    def reset_calls(self) -> None:
        for key in self.calls:
            self.calls[key] = []
