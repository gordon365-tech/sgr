"""
Custom Metrics für SGR Trading System
======================================
Portfolio, Risk, Strategy & Market Data Metrics.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from sgr.core.config import get_config
from sgr.core.logging import get_logger

log = get_logger(__name__)


class _TenantScopedInstrument:
    """
    Transparenter Wrapper um ein OTel-Gauge/Counter-Instrument: injiziert
    automatisch ein "tenant" Label in jeden .set()/.add()-Aufruf.

    Hintergrund (Schritt 6, Server-verifiziert): sgr-worker laeuft als ein
    Prozess PRO Tenant (worker-gordon, worker-sumo - siehe
    docker-compose.prod.yml), aber SGRMetrics ist ein reines In-Prozess-
    Singleton ohne jegliches Tenant-Label. Jeder Worker-Prozess erzeugte
    seine Gauges bisher mit identischen Labels (z.B. {"strategy": "...",
    "trading_mode": "paper"}), die beim Zusammenfuehren in
    worker_metrics_bridge.collect_worker_metrics() (Redis-Snapshots werden
    roh aneinandergehaengt, kein Label-Rewriting) fuer Prometheus
    ununterscheidbar sind - bestaetigt per curl auf dem Server: zwei
    identische sgr_strategy_active_count-Zeilen fuer Gordon und Sumo.

    Dieser Wrapper macht es strukturell unmoeglich, das Tenant-Label zu
    vergessen: jeder Aufrufer (record_*-Funktionen, MonitoringEngine, jeder
    zukuenftige neue Call) muss KEIN zusaetzliches Label mitgeben - es wird
    hier immer automatisch injiziert, auch wenn der Aufrufer selbst kein
    "tenant" im uebergebenen Dict hat. Ein explizit im Aufrufer-Dict
    gesetztes "tenant" wuerde NICHT ueberschrieben (siehe dict-merge-
    Reihenfolge unten) - kommt in der Praxis aber nicht vor, da kein
    Aufrufer aktuell "tenant" selbst setzt.
    """

    __slots__ = ("_instrument", "_tenant_id")

    def __init__(self, instrument: Any, tenant_id: str) -> None:
        self._instrument = instrument
        self._tenant_id = tenant_id

    def _with_tenant(self, attributes: dict[str, str] | None) -> dict[str, str]:
        merged = {"tenant": self._tenant_id}
        if attributes:
            merged.update(attributes)
        return merged

    def set(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self._instrument.set(amount, self._with_tenant(attributes))

    def add(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self._instrument.add(amount, self._with_tenant(attributes))


class SGRMetrics:
    """Zentralisierte Custom Metrics.

    tenant_id: identifiziert den Tenant-Prozess (z.B. "gordon", "sumo"),
    der diese Instanz erzeugt hat - siehe _TenantScopedInstrument
    Docstring fuer den Hintergrund. Default "default" fuer Single-Tenant-
    Deployments/Tests, in denen kein Tenant-Kontext existiert.
    """

    def __init__(self, tenant_id: str | None = None) -> None:
        self._meter: Meter = metrics.get_meter(__name__)
        self._tenant_id = tenant_id or _resolve_tenant_id()

        def gauge(name: str, description: str, unit: str = "") -> _TenantScopedInstrument:
            instrument = self._meter.create_gauge(name=name, description=description, unit=unit)
            return _TenantScopedInstrument(instrument, self._tenant_id)

        def counter(name: str, description: str) -> _TenantScopedInstrument:
            instrument = self._meter.create_counter(name=name, description=description)
            return _TenantScopedInstrument(instrument, self._tenant_id)

        # Portfolio Metrics
        self.portfolio_value = gauge("sgr.portfolio.value_usd", "Current portfolio value", "USD")
        self.portfolio_cash = gauge("sgr.portfolio.cash_usd", "Available cash", "USD")
        self.daily_pnl = gauge("sgr.portfolio.daily_pnl_usd", "Daily profit/loss", "USD")
        self.daily_pnl_pct = gauge(
            "sgr.portfolio.daily_pnl_pct", "Daily profit/loss percentage", "%"
        )

        # Risk Metrics
        self.portfolio_heat = gauge("sgr.risk.portfolio_heat", "Portfolio heat (0-1)")
        self.max_drawdown = gauge("sgr.risk.max_drawdown_pct", "Maximum drawdown", "%")
        self.leverage = gauge("sgr.risk.leverage", "Current leverage ratio")
        self.open_positions_count = gauge(
            "sgr.risk.open_positions", "Number of open positions"
        )
        self.var_95 = gauge("sgr.risk.var_95_pct", "Value at Risk (95% confidence)", "%")

        # Trading Metrics
        self.trades_total = counter("sgr.trades.total", "Total trades executed")
        self.trades_winning = counter("sgr.trades.winning", "Winning trades")
        self.trades_losing = counter("sgr.trades.losing", "Losing trades")

        # Strategy Metrics
        self.strategy_signals = counter(
            "sgr.strategy.signals_generated", "Trading signals generated"
        )
        self.strategy_win_rate = gauge("sgr.strategy.win_rate_pct", "Strategy win rate", "%")

        # Strategy Validation Metrics (Schritt 6: Sharpe/Return/Drawdown aus
        # StrategyValidationRunner duerfen nicht nur geloggt werden - siehe
        # MonitoringEngine._collect(), das diese Werte vorher nur per
        # log.debug ausgab, nie als Metrik. Quelle: StrategyEntry.
        # last_validation_result (sgr/strategy/registry.py), gefuellt von
        # StrategyValidationRunner.mark_validated().
        self.active_strategies_count = gauge(
            "sgr.strategy.active_count",
            "Number of currently activated (validated, is_active) strategies",
        )
        self.strategy_validation_status = gauge(
            "sgr.strategy.validation_status",
            "Go-live gate result per strategy (1 = can_go_live, "
            "0 = not approved for paper/live activation)",
        )
        self.strategy_sharpe_ratio = gauge(
            "sgr.strategy.backtest_sharpe_ratio",
            "Sharpe ratio from the most recent validation backtest",
        )
        self.strategy_total_return = gauge(
            "sgr.strategy.backtest_total_return_pct",
            "Total return from the most recent validation backtest",
            "%",
        )
        self.strategy_max_drawdown = gauge(
            "sgr.strategy.backtest_max_drawdown_pct",
            "Max drawdown from the most recent validation backtest",
            "%",
        )
        self.strategy_backtest_trades = gauge(
            "sgr.strategy.backtest_total_trades",
            "Number of trades in the most recent validation backtest",
        )

        # Market Data Metrics
        self.candles_received = counter(
            "sgr.market_data.candles_received", "OHLCV candles received"
        )

        # System Metrics
        self.api_requests_total = counter("sgr.api.requests_total", "Total API requests")
        self.api_errors_total = counter("sgr.api.errors_total", "API errors")

        log.info("metrics.sgr_metrics_initialized", tenant_id=self._tenant_id)


def _resolve_tenant_id() -> str:
    """Liest tenant_id aus der globalen Config (sgr.core.config.get_config()
    .tenant_id, dasselbe Feld, das WorkerMetricsPublisher bereits fuer den
    Redis-Snapshot-Key nutzt - siehe sgr/api/main.py Instanziierungsstelle).
    Fail-safe: liefert "default", falls Config noch nicht initialisiert ist
    (z.B. in Unit-Tests ohne vollen App-Kontext) oder tenant_id nicht
    gesetzt ist - konsistent mit WorkerMetricsPublisher's eigenem
    "config.tenant_id or 'default'"-Fallback."""
    try:
        config = get_config()
        return config.tenant_id or "default"
    except Exception:
        return "default"


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
