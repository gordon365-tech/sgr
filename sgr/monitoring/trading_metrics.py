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


def _resolve_tenant_id() -> str:
    """Liest tenant_id aus der globalen Config, Fallback "default".

    Identisches Muster wie sgr.monitoring.metrics._resolve_tenant_id()
    (dort fuer die OTel-basierten SGRMetrics-Instrumente bereits
    verwendet). Grund fuer die Existenz hier, statt den Wrapper aus
    metrics.py wiederzuverwenden: die Metriken in diesem Modul sind
    rohe prometheus_client-Instrumente (Counter/Gauge/Histogram), nicht
    OTel-Instrumente - _TenantScopedInstrument greift hier nicht.

    Root-Cause-Fund (Grafana-Observability-Audit): KEINE der Metriken in
    diesem Modul trug bisher ein tenant-Label, obwohl sgr-worker als ein
    Prozess PRO Tenant laeuft (worker-gordon, worker-sumo, siehe
    docker-compose.prod.yml) und beide Snapshots ueber
    worker_metrics_bridge.collect_worker_metrics() zu einem einzigen
    /metrics-Textkoerper zusammengefuegt werden (keine Label-Rewriting).
    Solange beide Tenants denselben Wert melden (z.B. kill_switch_active=0
    fuer beide), bleibt das unbemerkt (identische Zeilen). Sobald die
    Werte auseinanderlaufen (z.B. Kill Switch nur bei einem Tenant aktiv),
    entstehen zwei Prometheus-Samples mit IDENTISCHEM Namen+Label-Set aber
    unterschiedlichem Wert - fuer den Prometheus-Textformat-Parser nicht
    eindeutig aufloesbar. Betrifft ausgerechnet die sicherheitskritischste
    Metrik (Kill Switch) sowie alle Order-/Risk-/Reconciliation-Metriken
    dieses Moduls.
    """
    try:
        from sgr.core.config import get_config

        config = get_config()
        return config.tenant_id or "default"
    except Exception:
        return "default"


# =============================================================================
# ORDER METRICS
# =============================================================================

# Counter: Total Orders submitted
orders_submitted_total = Counter(
    "sgr_orders_submitted_total",
    "Total orders submitted to exchange",
    ["exchange", "symbol", "side", "trading_mode", "tenant"],
)

# Counter: Total Orders filled
orders_filled_total = Counter(
    "sgr_orders_filled_total",
    "Total orders filled",
    ["exchange", "symbol", "side", "trading_mode", "tenant"],
)

# Counter: Total Orders rejected
orders_rejected_total = Counter(
    "sgr_orders_rejected_total",
    "Total orders rejected",
    ["exchange", "symbol", "reason", "tenant"],
)

# Counter: Total Orders blocked (Duplicate)
orders_duplicate_blocked_total = Counter(
    "sgr_orders_duplicate_blocked_total",
    "Total duplicate orders blocked by idempotency check",
    ["exchange", "reason", "tenant"],
)

# Counter: Orders with Unknown status
orders_unknown_total = Counter(
    "sgr_orders_unknown_total",
    "Orders with unknown submission status (needs reconciliation)",
    ["exchange", "symbol", "tenant"],
)

# Histogram: Order Latency (submission to fill)
order_latency_seconds = Histogram(
    "sgr_order_latency_seconds",
    "Time from order submission to fill",
    ["exchange", "symbol", "order_type", "tenant"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, float("inf")),
)


# =============================================================================
# EXECUTION METRICS
# =============================================================================

# Histogram: Execution Engine latency
execution_latency_seconds = Histogram(
    "sgr_execution_latency_seconds",
    "Time to execute order from request to result",
    ["exchange", "order_type", "tenant"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, float("inf")),
)

# Histogram: Exchange response latency
exchange_latency_seconds = Histogram(
    "sgr_exchange_latency_seconds",
    "Exchange response time for order operations",
    ["exchange", "operation", "tenant"],
    buckets=(0.05, 0.1, 0.5, 1.0, 2.0, 5.0, float("inf")),
)

# Counter: Exchange timeouts
exchange_timeout_total = Counter(
    "sgr_exchange_timeout_total",
    "Total exchange operation timeouts",
    ["exchange", "operation", "tenant"],
)


# =============================================================================
# RISK METRICS
# =============================================================================

# Gauge: Kill Switch state (1 = active, 0 = inactive)
kill_switch_active = Gauge(
    "sgr_kill_switch_active",
    "Kill switch activation state (1 = active, 0 = inactive)",
    ["trading_mode", "tenant"],
)

# Counter: Risk checks performed
risk_checks_total = Counter(
    "sgr_risk_checks_total",
    "Total risk assessments performed",
    ["trading_mode", "symbol", "tenant"],
)

# Counter: Risk rejections
risk_rejected_total = Counter(
    "sgr_risk_rejected_total",
    "Total risk rejections",
    ["trading_mode", "reason", "tenant"],
)

# Counter: Risk reductions (position sizing down due to soft limits)
risk_reduced_total = Counter(
    "sgr_risk_reduced_total",
    "Total position reductions due to risk limits",
    ["trading_mode", "limit_type", "tenant"],
)

# Gauge: Portfolio drawdown
portfolio_drawdown = Gauge(
    "sgr_portfolio_drawdown",
    "Current portfolio drawdown from peak",
    ["trading_mode", "tenant"],
)

# Gauge: Portfolio heat (notional exposure / portfolio value)
portfolio_heat = Gauge(
    "sgr_portfolio_heat",
    "Portfolio heat (total notional / portfolio value)",
    ["trading_mode", "tenant"],
)

# Gauge: Active positions count
active_positions_count = Gauge(
    "sgr_active_positions_count",
    "Current number of open positions",
    ["trading_mode", "tenant"],
)


# =============================================================================
# RECONCILIATION METRICS
# =============================================================================

# Counter: Reconciliation runs
reconciliation_runs_total = Counter(
    "sgr_reconciliation_runs_total",
    "Total reconciliation runs",
    ["trading_mode", "status", "tenant"],
)

# Counter: Reconciliation failures
reconciliation_failures_total = Counter(
    "sgr_reconciliation_failures_total",
    "Total reconciliation failures",
    ["trading_mode", "reason", "tenant"],
)

# Counter: Discrepancies found
reconciliation_discrepancies_found = Counter(
    "sgr_reconciliation_discrepancies_found",
    "Discrepancies found during reconciliation",
    # Werte fuer 'type': order_mismatch, position_mismatch, etc.
    ["trading_mode", "type", "tenant"],
)


# =============================================================================
# TRADING CYCLE METRICS
# =============================================================================

# Counter: Trading cycles started
trading_cycles_total = Counter(
    "sgr_trading_cycles_total",
    "Total trading cycles executed",
    # status: completed, failed, no_signal, etc.
    ["status", "symbol", "tenant"],
)

# Counter: Trading cycles failed
trading_cycles_failed_total = Counter(
    "sgr_trading_cycles_failed_total",
    "Total trading cycles that failed",
    ["symbol", "reason", "tenant"],
)

# Histogram: Trading cycle duration
trading_cycles_duration_seconds = Histogram(
    "sgr_trading_cycles_duration_seconds",
    "Time to complete trading cycle",
    ["symbol", "tenant"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, float("inf")),
)


# =============================================================================
# WORKER LIVENESS (Grafana-Observability-Audit: "Worker Health / Heartbeat"
# aus dem Dashboard-Anforderungskatalog hatte bisher KEINE Entsprechung im
# Code - sgr-worker exponiert bewusst keinen eigenen HTTP-Port (siehe
# worker_metrics_bridge.py Modul-Docstring), es gab also kein Signal, das
# zwischen "Worker laeuft und published aktuelle Metriken" und "Worker
# haengt/ist abgestuerzt, letzter Redis-Snapshot ist alt" unterscheidet.
# =============================================================================

# Gauge: Zeitstempel des letzten erfolgreichen Metrics-Publish-Zyklus
# (siehe WorkerMetricsPublisher._publish_once in worker_metrics_bridge.py).
# Grafana/PromQL: time() - sgr_worker_heartbeat_timestamp_seconds > 30
# zeigt einen haengenden/toten Worker, auch wenn dessen letzter Snapshot
# (dank Redis-TTL) noch kurz sichtbar ist.
worker_heartbeat_timestamp_seconds = Gauge(
    "sgr_worker_heartbeat_timestamp_seconds",
    "Unix timestamp of the last successful worker metrics publish cycle",
    ["trading_mode", "tenant"],
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
        tenant=_resolve_tenant_id(),
    ).inc()


def record_order_filled(
    exchange: str,
    symbol: str,
    side: str,
    trading_mode: str,
    latency_seconds: float,
) -> None:
    """Record order fill."""
    tenant_id = _resolve_tenant_id()
    orders_filled_total.labels(
        exchange=exchange,
        symbol=symbol,
        side=side,
        trading_mode=trading_mode,
        tenant=tenant_id,
    ).inc()

    order_latency_seconds.labels(
        exchange=exchange,
        symbol=symbol,
        order_type="market",  # Simplified
        tenant=tenant_id,
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
        tenant=_resolve_tenant_id(),
    ).inc()


def record_duplicate_blocked(
    exchange: str,
    reason: str,
) -> None:
    """Record duplicate order block."""
    orders_duplicate_blocked_total.labels(
        exchange=exchange,
        reason=reason,
        tenant=_resolve_tenant_id(),
    ).inc()


def record_kill_switch_activation(trading_mode: str, active: bool) -> None:
    """Record kill switch state change."""
    kill_switch_active.labels(trading_mode=trading_mode, tenant=_resolve_tenant_id()).set(
        1 if active else 0
    )


def record_risk_rejection(trading_mode: str, reason: str) -> None:
    """Record risk-based rejection."""
    risk_rejected_total.labels(
        trading_mode=trading_mode,
        reason=reason,
        tenant=_resolve_tenant_id(),
    ).inc()


def record_trading_cycle_complete(
    symbol: str,
    status: str,
    duration_seconds: float,
) -> None:
    """Record completed trading cycle."""
    tenant_id = _resolve_tenant_id()
    trading_cycles_total.labels(
        status=status,
        symbol=symbol,
        tenant=tenant_id,
    ).inc()

    trading_cycles_duration_seconds.labels(
        symbol=symbol,
        tenant=tenant_id,
    ).observe(duration_seconds)
