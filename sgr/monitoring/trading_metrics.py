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

6. STRATEGY SYMBOL VALIDATION METRICS (Autonomous-Strategy-Universe-Rollout)
   - strategy_validation_total
   - strategy_active_total
   - strategy_avg_score / strategy_avg_sharpe / strategy_avg_return_pct /
     strategy_avg_drawdown_pct
   - strategy_universe_symbols_total
   - strategy_validation_duration_seconds
"""

from __future__ import annotations

from decimal import Decimal

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

# Realized Slippage (Live-Verification-Anweisung Abschnitt 4,
# "Observability"): ExecutionEngine._on_fill()'s eigener Docstring
# versprach seit jeher "1. Slippage berechnen + loggen", tat das aber
# nie tatsaechlich (siehe dortige Aufrufstelle) - reine interne
# Risk-Gating-Kennzahlen (paper_slippage_pct in GridRiskEngine)
# existierten, aber keine Produktions-Metrik aus einem echten Fill.
# Nur berechenbar, wenn ein Referenzpreis vorliegt (order.limit_price -
# bei einer reinen MARKET-Order ohne RiskEngine-erzwungenes Limit gibt
# es keinen sinnvollen Referenzpreis, dann wird nichts aufgezeichnet,
# statt einen erfundenen Wert zu melden).
order_slippage_pct = Histogram(
    "sgr_order_slippage_pct",
    "Realized slippage: abs(average_fill_price - reference_price) / reference_price * 100, "
    "only recorded when a reference price (order.limit_price) was available",
    ["exchange", "symbol", "side", "trading_mode", "tenant"],
    buckets=(0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0, float("inf")),
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

# LiveVerificationGate state (Live-Verification-Anweisung Abschnitt 4,
# "Observability"): vorher nur sichtbar ueber die generische Order-
# Rejection-Metrik (reason="live_verification_gate"), keine kontinuierliche
# Sicht auf verbleibendes Budget/verbleibende Orders. "approved_by" (die
# Pflichtangabe aus LiveVerificationProfile, z.B. "operator:gordon")
# dient als Account-Label - KEINE Secrets, nur eine Operator-Kennung, die
# der Operator selbst beim Erstellen des Profils frei waehlt.
live_verification_active = Gauge(
    "sgr_live_verification_active",
    "1 solange dieses LiveVerificationGate Orders zulassen kann, 0 wenn deaktiviert",
    ["approved_by", "tenant"],
)
live_verification_orders_submitted = Gauge(
    "sgr_live_verification_orders_submitted",
    "Bereits ueber dieses Gate abgesetzte Orders",
    ["approved_by", "tenant"],
)
live_verification_max_orders = Gauge(
    "sgr_live_verification_max_orders",
    "Vom Operator konfiguriertes max_orders-Limit dieses Verifikationslaufs",
    ["approved_by", "tenant"],
)
live_verification_loss_budget_remaining_usd = Gauge(
    "sgr_live_verification_loss_budget_remaining_usd",
    "max_loss_usd minus bereits realisiertem Verlust, floored bei 0",
    ["approved_by", "tenant"],
)
live_verification_daily_loss_budget_remaining_usd = Gauge(
    "sgr_live_verification_daily_loss_budget_remaining_usd",
    "max_daily_loss_usd minus heute realisiertem Verlust, floored bei 0",
    ["approved_by", "tenant"],
)


def record_live_verification_state(
    approved_by: str,
    active: bool,
    orders_submitted: int,
    max_orders: int,
    loss_budget_remaining_usd: float,
    daily_loss_budget_remaining_usd: float,
) -> None:
    """Aufrufer: LiveVerificationGate selbst (siehe live_verification_profile.py
    _emit_metrics()) - nach jeder Zustandsaenderung (Order zugelassen/
    abgelehnt, Verlust gebucht, deaktiviert)."""
    tenant_id = _resolve_tenant_id()
    live_verification_active.labels(approved_by=approved_by, tenant=tenant_id).set(
        1 if active else 0
    )
    live_verification_orders_submitted.labels(approved_by=approved_by, tenant=tenant_id).set(
        orders_submitted
    )
    live_verification_max_orders.labels(approved_by=approved_by, tenant=tenant_id).set(max_orders)
    live_verification_loss_budget_remaining_usd.labels(
        approved_by=approved_by, tenant=tenant_id
    ).set(loss_budget_remaining_usd)
    live_verification_daily_loss_budget_remaining_usd.labels(
        approved_by=approved_by, tenant=tenant_id
    ).set(daily_loss_budget_remaining_usd)


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
# STRATEGY SYMBOL VALIDATION METRICS
# =============================================================================
# Autonomous-Strategy-Universe-Rollout (sgr/strategy/symbol_validation_runner.py):
# das Ergebnis der Backtest+Walk-Forward-Validierung fuer JEDES Symbol im
# dynamisch entdeckten Asset Universe (bis zu ~900+ Symbole). BEWUSST
# KEIN "symbol"-Label auf irgendeiner dieser Metriken - das waere
# genau die in der Task-Vorgabe explizit verbotene Kardinalitaets-
# Explosion (900+ Symbole x 5 Strategien x mehrere Metriken = zehn-
# tausende Zeitreihen). "strategy" (< 10 registrierte Strategien) und
# "status" (6 feste Werte, siehe SymbolValidationStatus) bleiben
# niedrig-kardinal. Detaillierte Pro-Symbol-Ergebnisse liegen
# ausschliesslich in strategy_symbol_validations (Postgres, siehe
# StrategySymbolValidationRepository) - dafuer siehe die
# /api/v1/strategy-validation/* Read-Endpoints.

# Counter: Validierungslaeufe pro (Symbol, Strategie)-Paar
strategy_validation_total = Counter(
    "sgr_strategy_validation_total",
    "Total per-symbol strategy validation attempts",
    ["status", "strategy", "tenant"],
)

# Gauge: aktuell ACTIVE Symbole pro Strategie (letzter abgeschlossener Batch)
strategy_active_total = Gauge(
    "sgr_strategy_active_total",
    "Number of symbols where this strategy is the validated, active choice",
    ["strategy", "tenant"],
)

# Gauge: Durchschnittswerte ueber alle ACTIVE Symbole dieser Strategie
strategy_avg_score = Gauge(
    "sgr_strategy_avg_score",
    "Average composite score across ACTIVE symbols for this strategy",
    ["strategy", "tenant"],
)
strategy_avg_sharpe = Gauge(
    "sgr_strategy_avg_sharpe",
    "Average Sharpe ratio across ACTIVE symbols for this strategy",
    ["strategy", "tenant"],
)
strategy_avg_return_pct = Gauge(
    "sgr_strategy_avg_return_pct",
    "Average total return percent across ACTIVE symbols for this strategy",
    ["strategy", "tenant"],
)
strategy_avg_drawdown_pct = Gauge(
    "sgr_strategy_avg_drawdown_pct",
    "Average max drawdown percent across ACTIVE symbols for this strategy",
    ["strategy", "tenant"],
)

# Gauge: Gesamtzusammenfassung des letzten Batches (fuer Grafana-
# Uebersichts-Panels - Phase 16 "Total/Validated/Active/... Symbols")
strategy_universe_symbols_total = Gauge(
    "sgr_strategy_universe_symbols_total",
    "Number of symbols in the last completed universe validation batch, by outcome status",
    ["status", "tenant"],
)

# Histogram: Dauer eines einzelnen (Symbol, Strategie)-Validierungslaufs
strategy_validation_duration_seconds = Histogram(
    "sgr_strategy_validation_duration_seconds",
    "Time to run backtest + walk-forward validation for one (symbol, strategy) pair",
    ["tenant"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, float("inf")),
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


def record_order_slippage(
    exchange: str,
    symbol: str,
    side: str,
    trading_mode: str,
    reference_price: Decimal,
    fill_price: Decimal,
) -> None:
    """Record realized slippage for one fill - siehe order_slippage_pct
    Docstring. Aufrufer muss bereits sichergestellt haben, dass
    reference_price > 0 ist (siehe ExecutionEngine._on_fill())."""
    slippage_pct = abs(fill_price - reference_price) / reference_price * Decimal("100")
    order_slippage_pct.labels(
        exchange=exchange,
        symbol=symbol,
        side=side,
        trading_mode=trading_mode,
        tenant=_resolve_tenant_id(),
    ).observe(float(slippage_pct))


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


def record_strategy_validation(status: str, strategy: str) -> None:
    """Record one per-symbol strategy validation outcome. Called once
    per (symbol, strategy) attempt from
    SymbolStrategyValidationRunner - siehe Modul-Docstring oben fuer
    die bewusste Kardinalitaets-Begrenzung (kein Symbol-Label)."""
    strategy_validation_total.labels(
        status=status,
        strategy=strategy,
        tenant=_resolve_tenant_id(),
    ).inc()


def record_strategy_validation_duration(duration_seconds: float) -> None:
    """Record wall-clock time for one (symbol, strategy) backtest +
    walk-forward validation run."""
    strategy_validation_duration_seconds.labels(tenant=_resolve_tenant_id()).observe(
        duration_seconds
    )


def record_strategy_universe_summary(
    status_counts: dict[str, int],
    strategy_distribution: dict[str, int],
    avg_metrics_by_strategy: dict[str, dict[str, float]],
) -> None:
    """
    Setzt die Aggregat-Gauges fuer den zuletzt abgeschlossenen Batch
    (siehe sgr/strategy/symbol_validation_runner.py BatchSummary).
    Ersetzt den vorherigen Batch-Stand vollstaendig (set(), nicht inc())
    - ein neuer Batch-Lauf soll die vorherigen Zahlen ueberschreiben,
    nicht aufaddieren.

    status_counts: {status_value: count} ueber ALLE Symbole des Batches.
    strategy_distribution: {strategy_name: count} unter den ACTIVE-Symbolen.
    avg_metrics_by_strategy: {strategy_name: {"score":, "sharpe":,
        "return_pct":, "drawdown_pct":}} - Durchschnitt ueber die
        ACTIVE-Symbole dieser Strategie.
    """
    tenant_id = _resolve_tenant_id()
    for status, count in status_counts.items():
        strategy_universe_symbols_total.labels(status=status, tenant=tenant_id).set(count)

    for strategy, count in strategy_distribution.items():
        strategy_active_total.labels(strategy=strategy, tenant=tenant_id).set(count)

    for strategy, metrics in avg_metrics_by_strategy.items():
        strategy_avg_score.labels(strategy=strategy, tenant=tenant_id).set(
            metrics.get("score", 0.0)
        )
        strategy_avg_sharpe.labels(strategy=strategy, tenant=tenant_id).set(
            metrics.get("sharpe", 0.0)
        )
        strategy_avg_return_pct.labels(strategy=strategy, tenant=tenant_id).set(
            metrics.get("return_pct", 0.0)
        )
        strategy_avg_drawdown_pct.labels(strategy=strategy, tenant=tenant_id).set(
            metrics.get("drawdown_pct", 0.0)
        )
