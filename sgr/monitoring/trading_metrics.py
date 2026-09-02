"""
SGR Trading Metrics & Observability
====================================

Prometheus Metriken für Trading-spezifische Observability.

Metrics Categories:

1. ORDER METRICS (Execution)
   - orders_submitted_total
   - orders_filled_total
   - orders_rejected_total
   - orders_duplicate_blocked_total
   - orders_unknown_total
   - order_latency_seconds

2. EXECUTION METRICS
   - execution_latency_seconds
   - exchange_latency_seconds
   - exchange_timeout_total

3. RISK METRICS
   - kill_switch_active
   - risk_checks_total
   - risk_rejected_total

4. RECONCILIATION METRICS
   - reconciliation_runs_total
   - reconciliation_failures_total
   - reconciliation_discrepancies_found

5. TRADING CYCLE METRICS
   - trading_cycles_total
   - trading_cycles_failed_total
   - trading_cycles_duration_seconds
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

from sgr.core.logging import get_logger

log = get_logger(__name__)


# =============================================================================
# ORDER METRICS
# =============================================================================

# Counter: Total Orders submitted
orders_submitted_total = Counter(
    "sgr_orders_submitted_total",
    "Total orders submitted to exchange",
    ["exchange", "symbol", "side", "trading_mode"],
)

# Counter: Total Orders filled
orders_filled_total = Counter(
    "sgr_orders_filled_total",
    "Total orders filled",
    ["exchange", "symbol", "side", "trading_mode"],
)

# Counter: Total Orders rejected
orders_rejected_total = Counter(
    "sgr_orders_rejected_total",
    "Total orders rejected",
    ["exchange", "symbol", "reason"],
)

# Counter: Total Orders blocked (Duplicate)
orders_duplicate_blocked_total = Counter(
    "sgr_orders_duplicate_blocked_total",
    "Total duplicate orders blocked by idempotency check",
    ["exchange", "reason"],
)

# Counter: Orders with Unknown status
orders_unknown_total = Counter(
    "sgr_orders_unknown_total",
    "Orders with unknown submission status (needs reconciliation)",
    ["exchange", "symbol"],
)

# Histogram: Order Latency (submission to fill)
order_latency_seconds = Histogram(
    "sgr_order_latency_seconds",
    "Time from order submission to fill",
    ["exchange", "symbol", "order_type"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, float("inf")),
)


# =============================================================================
# EXECUTION METRICS
# =============================================================================

# Histogram: Execution Engine latency
execution_latency_seconds = Histogram(
    "sgr_execution_latency_seconds",
    "Time to execute order from request to result",
    ["exchange", "order_type"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, float("inf")),
)

# Histogram: Exchange response latency
exchange_latency_seconds = Histogram(
    "sgr_exchange_latency_seconds",
    "Exchange response time for order operations",
    ["exchange", "operation"],
    buckets=(0.05, 0.1, 0.5, 1.0, 2.0, 5.0, float("inf")),
)

# Counter: Exchange timeouts
exchange_timeout_total = Counter(
    "sgr_exchange_timeout_total",
    "Total exchange operation timeouts",
    ["exchange", "operation"],
)


# =============================================================================
# RISK METRICS
# =============================================================================

# Gauge: Kill Switch state (1 = active, 0 = inactive)
kill_switch_active = Gauge(
    "sgr_kill_switch_active",
    "Kill switch activation state (1 = active, 0 = inactive)",
    ["trading_mode"],
)

# Counter: Risk checks performed
risk_checks_total = Counter(
    "sgr_risk_checks_total",
    "Total risk assessments performed",
    ["trading_mode", "symbol"],
)

# Counter: Risk rejections
risk_rejected_total = Counter(
    "sgr_risk_rejected_total",
    "Total risk rejections",
    ["trading_mode", "reason"],
)

# Counter: Risk reductions (position sizing down due to soft limits)
risk_reduced_total = Counter(
    "sgr_risk_reduced_total",
    "Total position reductions due to risk limits",
    ["trading_mode", "limit_type"],
)

# Gauge: Portfolio drawdown
portfolio_drawdown = Gauge(
    "sgr_portfolio_drawdown",
    "Current portfolio drawdown from peak",
    ["trading_mode"],
)

# Gauge: Portfolio heat (notional exposure / portfolio value)
portfolio_heat = Gauge(
    "sgr_portfolio_heat",
    "Portfolio heat (total notional / portfolio value)",
    ["trading_mode"],
)

# Gauge: Active positions count
active_positions_count = Gauge(
    "sgr_active_positions_count",
    "Current number of open positions",
    ["trading_mode"],
)


# =============================================================================
# RECONCILIATION METRICS
# =============================================================================

# Counter: Reconciliation runs
reconciliation_runs_total = Counter(
    "sgr_reconciliation_runs_total",
    "Total reconciliation runs",
    ["trading_mode", "status"],
)

# Counter: Reconciliation failures
reconciliation_failures_total = Counter(
    "sgr_reconciliation_failures_total",
    "Total reconciliation failures",
    ["trading_mode", "reason"],
)

# Counter: Discrepancies found
reconciliation_discrepancies_found = Counter(
    "sgr_reconciliation_discrepancies_found",
    "Discrepancies found during reconciliation",
    ["trading_mode", "type"],  # Werte fuer 'type': order_mismatch, position_mismatch, etc.
)


# =============================================================================
# TRADING CYCLE METRICS
# =============================================================================

# Counter: Trading cycles started
trading_cycles_total = Counter(
    "sgr_trading_cycles_total",
    "Total trading cycles executed",
    ["status", "symbol"],  # status: completed, failed, no_signal, etc.
)

# Counter: Trading cycles failed
trading_cycles_failed_total = Counter(
    "sgr_trading_cycles_failed_total",
    "Total trading cycles that failed",
    ["symbol", "reason"],
)

# Histogram: Trading cycle duration
trading_cycles_duration_seconds = Histogram(
    "sgr_trading_cycles_duration_seconds",
    "Time to complete trading cycle",
    ["symbol"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, float("inf")),
)


# =============================================================================
# CONTAINER / PROCESS METRICS (indirekt via Prometheus Node Exporter)
# =============================================================================

# Info: SGR Version & Build Info
sgr_build_info = Info(
    "sgr_build_info",
    "SGR build information",
    ["version", "environment", "trading_mode"],
)


# =============================================================================
# Helper Functions für Metrik-Recording
# =============================================================================

def record_order_submitted(
    exchange: str,
    symbol: str,
    side: str,
    trading_mode: str,
) -> None:
    """Record order submission."""
    orders_submitted_total.labels(
        exchange=exchange,
        symbol=symbol,
        side=side,
        trading_mode=trading_mode,
    ).inc()


def record_order_filled(
    exchange: str,
    symbol: str,
    side: str,
    trading_mode: str,
    latency_seconds: float,
) -> None:
    """Record order fill."""
    orders_filled_total.labels(
        exchange=exchange,
        symbol=symbol,
        side=side,
        trading_mode=trading_mode,
    ).inc()

    order_latency_seconds.labels(
        exchange=exchange,
        symbol=symbol,
        order_type="market",  # Simplified
    ).observe(latency_seconds)


def record_order_rejected(
    exchange: str,
    symbol: str,
    reason: str,
) -> None:
    """Record order rejection."""
    orders_rejected_total.labels(
        exchange=exchange,
        symbol=symbol,
        reason=reason,
    ).inc()


def record_duplicate_blocked(
    exchange: str,
    reason: str,
) -> None:
    """Record duplicate order block."""
    orders_duplicate_blocked_total.labels(
        exchange=exchange,
        reason=reason,
    ).inc()


def record_kill_switch_activation(trading_mode: str, active: bool) -> None:
    """Record kill switch state change."""
    kill_switch_active.labels(trading_mode=trading_mode).set(1 if active else 0)


def record_risk_rejection(trading_mode: str, reason: str) -> None:
    """Record risk-based rejection."""
    risk_rejected_total.labels(
        trading_mode=trading_mode,
        reason=reason,
    ).inc()


def record_trading_cycle_complete(
    symbol: str,
    status: str,
    duration_seconds: float,
) -> None:
    """Record completed trading cycle."""
    trading_cycles_total.labels(
        status=status,
        symbol=symbol,
    ).inc()

    trading_cycles_duration_seconds.labels(
        symbol=symbol,
    ).observe(duration_seconds)
