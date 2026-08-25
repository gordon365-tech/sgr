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
    record_candle_received,
    record_portfolio_snapshot,
    record_risk_snapshot,
    record_signal_generated,
    record_trade_executed,
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
                            daily_pnl_pct=float(
                                getattr(risk_metrics, "daily_pnl_pct", 0)
                            ) * 100,
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
                    leverage=float(getattr(risk_metrics, "leverage", 0)),
                    open_positions=len(self._portfolio_engine.positions),
                    var_95_pct=float(risk_metrics.var_95) * 100,
                )
            except Exception as e:
                log.debug("monitoring.risk_error", error=str(e))

        if self._strategy_registry:
            try:
                active = self._strategy_registry.get_active()
                log.debug(
                    "monitoring.strategy_state",
                    active_count=len(active),
                )

                for name, entry in self._strategy_registry.get_all().items():
                    if entry.performance:
                        p = entry.performance
                        # Strategy performance remains observable through the
                        # existing registry data. Signal recording is delegated
                        # to record_signal_generated() at signal creation time.
                        log.debug(
                            "monitoring.strategy_performance",
                            strategy=name,
                            sharpe=p.sharpe_ratio,
                            hit_rate=p.hit_rate,
                        )
            except Exception as e:
                log.debug("monitoring.strategy_error", error=str(e))


# Keep these imports available for callers that use the monitoring engine as
# the central metrics integration point. The actual recording happens through
# the OTel-based metrics module above.
__all__ = [
    "MonitoringEngine",
    "create_metrics_app",
    "add_metrics_middleware",
    "record_candle_received",
    "record_portfolio_snapshot",
    "record_risk_snapshot",
    "record_signal_generated",
    "record_trade_executed",
]


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
                metrics = get_metrics()
                metrics.api_requests_total.add(
                    1,
                    {
                        "method": request.method,
                        "path": path,
                        "status_code": str(response.status_code),
                    },
                )
                log.debug(
                    "monitoring.api_request",
                    method=request.method,
                    path=path,
                    status_code=response.status_code,
                    duration_seconds=duration,
                )

            return response

    app.add_middleware(MetricsMiddleware)
