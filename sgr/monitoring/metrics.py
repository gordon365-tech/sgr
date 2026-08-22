"""
Custom Metrics für SGR Trading System
======================================
Portfolio, Risk, Strategy & Market Data Metrics.
"""

from __future__ import annotations

from decimal import Decimal
from opentelemetry import metrics
from opentelemetry.metrics import Meter
from sgr.core.logging import get_logger

log = get_logger(__name__)


class SGRMetrics:
    """Zentralisierte Custom Metrics."""

    def __init__(self) -> None:
        self._meter: Meter = metrics.get_meter(__name__)

        # Portfolio Metrics
        self.portfolio_value = self._meter.create_gauge(
            name="sgr.portfolio.value_usd",
            description="Current portfolio value",
            unit="USD",
        )

        self.portfolio_cash = self._meter.create_gauge(
            name="sgr.portfolio.cash_usd",
            description="Available cash",
            unit="USD",
        )

        self.daily_pnl = self._meter.create_gauge(
            name="sgr.portfolio.daily_pnl_usd",
            description="Daily profit/loss",
            unit="USD",
        )

        self.daily_pnl_pct = self._meter.create_gauge(
            name="sgr.portfolio.daily_pnl_pct",
            description="Daily profit/loss percentage",
            unit="%",
        )

        # Risk Metrics
        self.portfolio_heat = self._meter.create_gauge(
            name="sgr.risk.portfolio_heat",
            description="Portfolio heat (0-1)",
        )

        self.max_drawdown = self._meter.create_gauge(
            name="sgr.risk.max_drawdown_pct",
            description="Maximum drawdown",
            unit="%",
        )

        self.leverage = self._meter.create_gauge(
            name="sgr.risk.leverage",
            description="Current leverage ratio",
        )

        self.open_positions_count = self._meter.create_gauge(
            name="sgr.risk.open_positions",
            description="Number of open positions",
        )

        self.var_95 = self._meter.create_gauge(
            name="sgr.risk.var_95_pct",
            description="Value at Risk (95% confidence)",
            unit="%",
        )

        # Trading Metrics
        self.trades_total = self._meter.create_counter(
            name="sgr.trades.total",
            description="Total trades executed",
        )

        self.trades_winning = self._meter.create_counter(
            name="sgr.trades.winning",
            description="Winning trades",
        )

        self.trades_losing = self._meter.create_counter(
            name="sgr.trades.losing",
            description="Losing trades",
        )

        # Strategy Metrics
        self.strategy_signals = self._meter.create_counter(
            name="sgr.strategy.signals_generated",
            description="Trading signals generated",
        )

        self.strategy_win_rate = self._meter.create_gauge(
            name="sgr.strategy.win_rate_pct",
            description="Strategy win rate",
            unit="%",
        )

        # Market Data Metrics
        self.candles_received = self._meter.create_counter(
            name="sgr.market_data.candles_received",
            description="OHLCV candles received",
        )

        # System Metrics
        self.api_requests_total = self._meter.create_counter(
            name="sgr.api.requests_total",
            description="Total API requests",
        )

        self.api_errors_total = self._meter.create_counter(
            name="sgr.api.errors_total",
            description="API errors",
        )

        log.info("metrics.sgr_metrics_initialized")


# Singleton instance
_metrics_instance: SGRMetrics | None = None


def get_metrics() -> SGRMetrics:
    """Returns the global SGR metrics instance."""
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = SGRMetrics()
    return _metrics_instance


def record_portfolio_snapshot(
    portfolio_value: Decimal,
    cash: Decimal,
    daily_pnl: Decimal,
    daily_pnl_pct: float,
) -> None:
    """Records portfolio state."""
    m = get_metrics()
    m.portfolio_value.set(float(portfolio_value), {"status": "live"})
    m.portfolio_cash.set(float(cash), {"status": "live"})
    m.daily_pnl.set(float(daily_pnl), {"status": "live"})
    m.daily_pnl_pct.set(daily_pnl_pct, {"status": "live"})


def record_risk_snapshot(
    portfolio_heat: float,
    max_drawdown_pct: float,
    leverage: float,
    open_positions: int,
    var_95_pct: float,
) -> None:
    """Records risk metrics."""
    m = get_metrics()
    m.portfolio_heat.set(portfolio_heat, {"status": "live"})
    m.max_drawdown.set(max_drawdown_pct, {"status": "live"})
    m.leverage.set(leverage, {"status": "live"})
    m.open_positions_count.set(open_positions, {"status": "live"})
    m.var_95.set(var_95_pct, {"status": "live"})


def record_trade_executed(side: str, pnl: Decimal, winning: bool) -> None:
    """Records trade execution."""
    m = get_metrics()
    m.trades_total.add(1, {"side": side})
    if winning:
        m.trades_winning.add(1, {"side": side})
    else:
        m.trades_losing.add(1, {"side": side})


def record_signal_generated(strategy_name: str, direction: str, confidence: float) -> None:
    """Records signal generation."""
    m = get_metrics()
    m.strategy_signals.add(1, {"strategy": strategy_name, "direction": direction})


def record_candle_received(symbol: str, timeframe: str) -> None:
    """Records candle reception."""
    m = get_metrics()
    m.candles_received.add(1, {"symbol": symbol, "timeframe": timeframe})
