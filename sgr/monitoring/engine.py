"""
SGR Monitoring Engine
=====================
Sammelt periodisch Metriken aus allen Engines über die OTel-basierte
SGRMetrics API.

Warum separater Monitoring Loop?
    Engines selbst sollen keine Monitoring-Abhängigkeit haben.
    Monitoring Engine ist ein Beobachter – kein Teil der Trading-Logik.
    Kann deaktiviert werden ohne Trading zu beeinflussen.

Zusätzlich: FastAPI Middleware für API-Metriken.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from prometheus_client import make_asgi_app

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.monitoring.metrics import (
    get_metrics,
    record_portfolio_snapshot,
    record_position_snapshot,
    record_risk_snapshot,
)

log = get_logger(__name__)


class MonitoringEngine:
    """
    Liest periodisch State aus allen Engines und schreibt ihn über die
    zentrale OTel-Metrik-API.
    Läuft als separater asyncio Task.
    """

    def __init__(
        self,
        risk_engine: Any = None,
        portfolio_engine: Any = None,
        strategy_registry: Any = None,
        trading_mode: str = "paper",
        interval_seconds: float = 10.0,
    ) -> None:
        self._risk_engine = risk_engine
        self._portfolio_engine = portfolio_engine
        self._strategy_registry = strategy_registry
        self._trading_mode = trading_mode
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False
        # Merkt sich die Label-Kombination (symbol, side, exchange) aller
        # im letzten Zyklus gemeldeten offenen Positionen. Eine Position,
        # die im aktuellen Zyklus nicht mehr auftaucht (geschlossen), wird
        # explizit auf 0 gesetzt statt einfach nicht mehr geschrieben zu
        # werden - sonst wuerde ihr letzter (offener) Gauge-Wert in
        # Prometheus/Grafana unveraendert stehen bleiben und eine laengst
        # geschlossene Position faelschlich als weiterhin offen anzeigen.
        self._last_position_keys: set[tuple[str, str, str]] = set()

    async def start(self) -> None:
        config = get_config()
        get_metrics()

        self._running = True
        self._task = asyncio.create_task(
            self._collect_loop(),
            name="monitoring_engine",
        )
        log.info(
            "monitoring_engine.started",
            interval=self._interval,
            environment=config.environment.value,
            trading_mode=config.trading_mode.value,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("monitoring_engine.stopped")

    async def _collect_loop(self) -> None:
        while self._running:
            try:
                await self._collect()
            except Exception as e:
                log.error("monitoring_engine.collect_error", error=str(e))
            await asyncio.sleep(self._interval)

    async def _collect(self) -> None:
        """Sammelt alle verfügbaren Metriken in einem Durchlauf."""
        portfolio_value = None
        cash = None

        if self._portfolio_engine:
            try:
                portfolio_value = self._portfolio_engine.portfolio_value
                positions = self._portfolio_engine.positions
                cash = getattr(self._portfolio_engine, "cash", None)

                if cash is None:
                    cash = getattr(self._portfolio_engine, "cash_balance", 0)

                # Portfolio snapshot wird nach Möglichkeit mit den Risk-Daten
                # ergänzt. Ohne Risk Engine werden nur die verfügbaren Werte
                # geschrieben.
                if self._risk_engine:
                    try:
                        risk_metrics = self._risk_engine._compute_metrics(
                            portfolio_value=portfolio_value,
                            positions=positions,
                        )
                        record_portfolio_snapshot(
                            portfolio_value=portfolio_value,
                            cash=cash,
                            daily_pnl=getattr(risk_metrics, "daily_pnl", 0),
                            daily_pnl_pct=float(getattr(risk_metrics, "daily_pnl_pct", 0)) * 100,
                        )
                    except Exception as e:
                        log.debug(
                            "monitoring.portfolio_risk_snapshot_error",
                            error=str(e),
                        )
                else:
                    record_portfolio_snapshot(
                        portfolio_value=portfolio_value,
                        cash=cash,
                        daily_pnl=0,
                        daily_pnl_pct=0,
                    )
            except Exception as e:
                log.debug("monitoring.portfolio_error", error=str(e))

        if self._risk_engine and self._portfolio_engine:
            try:
                risk_metrics = self._risk_engine._compute_metrics(
                    portfolio_value=self._portfolio_engine.portfolio_value,
                    positions=self._portfolio_engine.positions,
                )

                record_risk_snapshot(
                    portfolio_heat=float(risk_metrics.portfolio_heat),
                    max_drawdown_pct=float(risk_metrics.drawdown_from_peak) * 100,
                    # Bugfix (Grafana-Observability-Audit): RiskMetrics
                    # (sgr/core/types.py) hat kein Feld "leverage" - das
                    # tatsaechliche Feld heisst "gross_leverage". Die alte
                    # getattr(..., "leverage", 0)-Zeile griff daher NIE,
                    # sgr_risk_leverage stand dauerhaft fest auf 0.0, egal
                    # wie hoch der echte Hebel war (server-verifiziert).
                    leverage=float(getattr(risk_metrics, "gross_leverage", 0)),
                    open_positions=len(self._portfolio_engine.positions),
                    var_95_pct=float(risk_metrics.var_95) * 100,
                )
            except Exception as e:
                log.debug("monitoring.risk_error", error=str(e))

        if self._portfolio_engine:
            try:
                self._collect_position_metrics()
            except Exception as e:
                log.debug("monitoring.position_error", error=str(e))

        if self._strategy_registry:
            try:
                active = self._strategy_registry.get_active()
                sgr_metrics = get_metrics()

                # Punkt "active_strategies" - global sichtbar statt nur
                # geloggt (siehe Modul-Docstring/Server-Verifikation:
                # active_strategies=0 muss ein reales, beobachtbares
                # Signal in Grafana sein, kein Log-only-Wert).
                sgr_metrics.active_strategies_count.set(
                    len(active), {"trading_mode": self._trading_mode}
                )

                for name, entry in self._strategy_registry.get_all().items():
                    labels = {"strategy": name, "trading_mode": self._trading_mode}

                    if entry.performance:
                        p = entry.performance
                        sgr_metrics.strategy_win_rate.set(p.hit_rate * 100, labels)

                    # Go-Live-Gate-Ergebnis (can_go_live) als 0/1-Gauge -
                    # macht sichtbar, WARUM active_strategies bei 0 bleibt,
                    # ohne die Logs durchsuchen zu muessen.
                    sgr_metrics.strategy_validation_status.set(
                        1 if entry.validation_status.can_go_live else 0, labels
                    )

                    # Rohwerte aus dem letzten Backtest-Validierungslauf
                    # (StrategyEntry.last_validation_result, siehe
                    # StrategyValidationRunner). None vor dem ersten
                    # Validierungslauf - dann werden hier bewusst keine
                    # Gauges geschrieben, statt eine Null vorzutaeuschen,
                    # die faelschlich wie ein echtes Backtest-Ergebnis
                    # aussehen wuerde.
                    result = entry.last_validation_result
                    if result is not None:
                        sgr_metrics.strategy_sharpe_ratio.set(result.sharpe_ratio, labels)
                        sgr_metrics.strategy_total_return.set(result.total_return_pct, labels)
                        sgr_metrics.strategy_max_drawdown.set(result.max_drawdown_pct, labels)
                        sgr_metrics.strategy_backtest_trades.set(result.total_trades, labels)

                    log.debug(
                        "monitoring.strategy_performance",
                        strategy=name,
                        backtest_sharpe=result.sharpe_ratio if result else None,
                        live_sharpe=(entry.performance.sharpe_ratio if entry.performance else None),
                        hit_rate=entry.performance.hit_rate if entry.performance else None,
                        can_go_live=entry.validation_status.can_go_live,
                    )
            except Exception as e:
                log.debug("monitoring.strategy_error", error=str(e))

    def _collect_position_metrics(self) -> None:
        """Schreibt eine Gauge-Zeile pro aktuell offener Position.

        Fuettert das Asset/Position-Breakdown-Panel im Grafana-Dashboard
        (Symbol, Side, Groesse, Exposure, Leverage, Unrealized PnL).
        Positionen, die seit dem letzten Zyklus geschlossen wurden, werden
        explizit auf 0 gesetzt (siehe _last_position_keys Docstring in
        __init__) statt einfach nicht mehr aktualisiert zu werden.
        """
        positions = self._portfolio_engine.positions
        current_keys: set[tuple[str, str, str]] = set()

        for position in positions:
            # position.symbol.exchange (nicht die globale Config-
            # primary_exchange) - das ist das tatsaechliche Exchange DIESER
            # Position (siehe Symbol.exchange in sgr/core/types.py), korrekt
            # auch sobald ein Tenant gleichzeitig auf mehreren Exchanges
            # handelt.
            symbol = position.symbol.ccxt_symbol
            exchange = position.symbol.exchange.value
            side = position.side.value
            current_keys.add((symbol, side, exchange))
            record_position_snapshot(
                symbol=symbol,
                side=side,
                trading_mode=self._trading_mode,
                exchange=exchange,
                size=float(position.quantity),
                exposure_usd=float(position.notional_value),
                leverage=float(position.leverage),
                unrealized_pnl_usd=float(position.unrealized_pnl),
                entry_price_usd=float(position.entry_price),
                current_price_usd=float(position.current_price),
            )

        closed_keys = self._last_position_keys - current_keys
        for symbol, side, exch in closed_keys:
            record_position_snapshot(
                symbol=symbol,
                side=side,
                trading_mode=self._trading_mode,
                exchange=exch,
                size=0.0,
                exposure_usd=0.0,
                leverage=0.0,
                unrealized_pnl_usd=0.0,
            )

        self._last_position_keys = current_keys


def create_metrics_app():
    """
    Erstellt die bestehende Prometheus ASGI-App.

    Hinweis: Die eigentlichen SGR Custom Metrics sind inzwischen OTel-basiert.
    Die OTel-Prometheus-Exporter-Anbindung wird separat in observability.py
    hergestellt.
    """
    return make_asgi_app()


def add_metrics_middleware(app: Any) -> None:
    """Fügt Request-Tracking über die zentrale OTel-Metrics-API hinzu."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            start = time.monotonic()
            response = await call_next(request)
            duration = time.monotonic() - start

            path = request.url.path
            if path.startswith("/api/") or path in ("/health",):
                sgr_metrics = get_metrics()
                attrs = {
                    "method": request.method,
                    "path": path,
                    "status_code": str(response.status_code),
                }
                if response.status_code >= 400:
                    sgr_metrics.api_errors_total.add(1, attrs)
                else:
                    sgr_metrics.api_requests_total.add(1, attrs)
                log.debug("monitoring.api_request", duration_s=duration, **attrs)

            return response

    app.add_middleware(MetricsMiddleware)
