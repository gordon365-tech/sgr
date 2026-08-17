"""
SGR Core Types
==============
All shared domain types. No business logic here – pure data contracts.
Every module imports from here; never the other way around.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TradingMode(StrEnum):
    """Strict separation: paper and live never share execution paths."""

    PAPER = "paper"
    LIVE = "live"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class ExchangeID(StrEnum):
    BINANCE = "binance"
    PIONEX = "pionex"
    BYBIT = "bybit"
    OKX = "okx"
    KRAKEN = "kraken"


class AssetClass(StrEnum):
    SPOT = "spot"
    FUTURES = "futures"
    OPTIONS = "options"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"
    TWAP = "twap"


class OrderStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class MarketRegime(StrEnum):
    """ML-detected market regime. Drives strategy selection."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    BREAKOUT = "breakout"
    HIGH_VOLATILITY = "high_volatility"
    CRISIS = "crisis"
    UNKNOWN = "unknown"


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    CLOSE = "close"
    NEUTRAL = "neutral"


class RiskDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REDUCED = "reduced"  # Approved but size reduced


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    KILL_SWITCH = "kill_switch"


# ---------------------------------------------------------------------------
# Value Objects (immutable, validated)
# ---------------------------------------------------------------------------


class Symbol(BaseModel):
    """Normalized trading pair: base/quote on an exchange."""

    model_config = {"frozen": True}

    base: str
    quote: str
    exchange: ExchangeID
    asset_class: AssetClass = AssetClass.SPOT

    @field_validator("base", "quote")
    @classmethod
    def uppercase(cls, v: str) -> str:
        return v.upper()

    @property
    def ccxt_symbol(self) -> str:
        return f"{self.base}/{self.quote}"

    def __str__(self) -> str:
        return f"{self.base}/{self.quote}:{self.exchange.value}"


class Price(BaseModel):
    """Decimal price with precision guard."""

    model_config = {"frozen": True}

    value: Decimal
    currency: str = "USDT"

    def __mul__(self, other: Decimal | float | int) -> Price:
        return Price(value=self.value * Decimal(str(other)), currency=self.currency)

    def __add__(self, other: Price) -> Price:
        if self.currency != other.currency:
            msg = (
                f"Cannot add prices with different currencies: {self.currency} vs {other.currency}"
            )
            raise ValueError(msg)
        return Price(value=self.value + other.value, currency=self.currency)

    def __gt__(self, other: Price) -> bool:
        return self.value > other.value

    def __lt__(self, other: Price) -> bool:
        return self.value < other.value


# ---------------------------------------------------------------------------
# Market Data
# ---------------------------------------------------------------------------


class Candle(BaseModel):
    """OHLCV candle. Immutable once created."""

    model_config = {"frozen": True}

    symbol: Symbol
    timestamp: datetime
    timeframe: str  # "1m", "5m", "1h", "4h", "1d"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @model_validator(mode="after")
    def high_gte_low(self) -> Candle:
        if self.high < self.low:
            raise ValueError("high must be >= low")
        return self


class OrderBookLevel(BaseModel):
    model_config = {"frozen": True}
    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    model_config = {"frozen": True}

    symbol: Symbol
    timestamp: datetime
    bids: list[OrderBookLevel]  # sorted descending
    asks: list[OrderBookLevel]  # sorted ascending

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price if self.bids else Decimal(0)

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price if self.asks else Decimal(0)

    @property
    def spread(self) -> Decimal:
        return self.best_ask - self.best_bid

    @property
    def mid_price(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2


class FundingRate(BaseModel):
    model_config = {"frozen": True}

    symbol: Symbol
    timestamp: datetime
    rate: Decimal
    next_funding_time: datetime


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """Trading signal from a strategy. Immutable."""

    model_config = {"frozen": True}

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime
    strategy_name: str
    symbol: Symbol
    direction: SignalDirection
    confidence: float = Field(ge=0.0, le=1.0)
    regime: MarketRegime
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Optional: suggested sizing hint (0.0–1.0 fraction of allowed position)
    size_hint: float = Field(default=1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Orders & Positions
# ---------------------------------------------------------------------------


class OrderRequest(BaseModel):
    """What the execution engine receives from risk engine."""

    id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    symbol: Symbol
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trading_mode: TradingMode
    reduce_only: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrderResult(BaseModel):
    """Result after order submission to exchange."""

    request_id: UUID
    exchange_order_id: str
    symbol: Symbol
    status: OrderStatus
    filled_quantity: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None
    fees: Decimal = Decimal(0)
    fee_currency: str = "USDT"
    submitted_at: datetime
    filled_at: datetime | None = None
    trading_mode: TradingMode
    raw_response: dict[str, Any] = Field(default_factory=dict)


class Position(BaseModel):
    """Live position state."""

    id: UUID = Field(default_factory=uuid4)
    symbol: Symbol
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal
    leverage: Decimal = Decimal(1)
    unrealized_pnl: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    opened_at: datetime
    strategy_name: str
    trading_mode: TradingMode

    @property
    def notional_value(self) -> Decimal:
        return self.quantity * self.current_price

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return float((self.current_price - self.entry_price) / self.entry_price)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class RiskMetrics(BaseModel):
    """Snapshot of current risk state. Computed by Risk Engine."""

    timestamp: datetime
    portfolio_value: Decimal
    daily_pnl: Decimal
    daily_pnl_pct: float
    drawdown_from_peak: float
    var_95: float  # Value at Risk 95% (as fraction)
    expected_shortfall: float
    portfolio_heat: float  # 0.0–1.0 (sum of risk units / max)
    active_positions: int
    correlation_exposure: float


class RiskAssessment(BaseModel):
    """Result of risk engine evaluation for a signal."""

    signal_id: UUID
    decision: RiskDecision
    approved_quantity: Decimal
    rejection_reason: str | None = None
    risk_metrics_snapshot: RiskMetrics
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Events (Event Bus contracts)
# ---------------------------------------------------------------------------


class BaseEvent(BaseModel):
    """All events on the bus extend this."""

    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime
    source: str  # module that emitted the event


class CandleEvent(BaseEvent):
    source: str = "market_data"
    candle: Candle


class SignalEvent(BaseEvent):
    source: str = "strategy_engine"
    signal: Signal


class RiskApprovedEvent(BaseEvent):
    source: str = "risk_engine"
    assessment: RiskAssessment
    order_request: OrderRequest


class RiskRejectedEvent(BaseEvent):
    source: str = "risk_engine"
    assessment: RiskAssessment


class OrderFilledEvent(BaseEvent):
    source: str = "execution_engine"
    result: OrderResult


class KillSwitchEvent(BaseEvent):
    source: str = "risk_engine"
    reason: str
    severity: AlertSeverity = AlertSeverity.KILL_SWITCH
    trading_mode: TradingMode


class AlertEvent(BaseEvent):
    source: str
    severity: AlertSeverity
    title: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Trading Orchestrator: Cycle Result & Events
# ---------------------------------------------------------------------------


class TradingCycleStatus(StrEnum):
    """Terminal-Status eines Trading-Zyklus (Orchestrator-Ebene)."""

    NO_SIGNAL = "no_signal"
    SIGNAL_REJECTED = "signal_rejected"
    RISK_REJECTED = "risk_rejected"
    ORDER_FILLED = "order_filled"
    ORDER_NOT_FILLED = "order_not_filled"  # submitted, aber (noch) nicht FILLED
    FAILED = "failed"


class TradingCycleResult(BaseModel):
    """
    Ergebnis eines vollständigen Orchestrator-Zyklus.
    Auditierbarer Endpunkt: enthält Referenzen auf jeden Zwischenschritt,
    damit ein Zyklus im Nachhinein vollständig nachvollzogen werden kann.
    """

    id: UUID = Field(default_factory=uuid4)
    started_at: datetime
    completed_at: datetime
    status: TradingCycleStatus
    symbol_key: str
    timeframe: str
    signal: Signal | None = None
    assessment: RiskAssessment | None = None
    order_request: OrderRequest | None = None
    order_result: OrderResult | None = None
    error: str | None = None


class TradingCycleCompletedEvent(BaseEvent):
    source: str = "orchestrator"
    result: TradingCycleResult


class TradingCycleFailedEvent(BaseEvent):
    source: str = "orchestrator"
    symbol_key: str
    timeframe: str
    error: str
