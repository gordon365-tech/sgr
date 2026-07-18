"""
SGR Monitoring Engine
=====================
Sammelt periodisch Metriken aus allen Engines und schreibt
sie in Prometheus Gauges.

Warum separater Monitoring Loop?
    Engines selbst sollen keine Prometheus-Abhängigkeit haben.
    Monitoring Engine ist ein Beobachter – kein Teil der Trading-Logik.
    Kann deaktiviert werden ohne Trading zu beeinflussen.

Zusätzlich: FastAPI Middleware für API-Metriken (Request Count, Latenz).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from prometheus_client import make_asgi_app

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.monitoring import metrics as m

log = get_logger(__name__)


class MonitoringEngine:
    """
    Liest periodisch State aus allen Engines und updated Metriken.
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
        m.update_system_info(
            version=config.version,
            environment=config.environment.value,
            trading_mode=config.trading_mode.value,
        )

        self._running = True
        self._task = asyncio.create_task(
            self._collect_loop(),
            name="monitoring_engine",
        )
        log.info("monitoring_engine.started", interval=self._interval)

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
        """Sammelt alle Metriken in einem Durchlauf."""
        mode = self._trading_mode

        # Portfolio Metriken
        if self._portfolio_engine:
            try:
                val = self._portfolio_engine.portfolio_value
                m.portfolio_value.labels(trading_mode=mode).set(float(val))
                m.open_positions.labels(trading_mode=mode).set(
                    len(self._portfolio_engine.positions)
                )
            except Exception as e:
                log.debug("monitoring.portfolio_error", error=str(e))

        # Risk Metriken
        if self._risk_engine and self._portfolio_engine:
            try:
                metrics = self._risk_engine._compute_metrics(
                    portfolio_value=self._portfolio_engine.portfolio_value,
                    positions=self._portfolio_engine.positions,
                )
                m.drawdown_pct.labels(trading_mode=mode).set(metrics.drawdown_from_peak * 100)
                m.var_95.labels(trading_mode=mode).set(metrics.var_95 * 100)
                m.portfolio_heat.labels(trading_mode=mode).set(metrics.portfolio_heat * 100)
                m.portfolio_pnl_daily.labels(trading_mode=mode).set(metrics.daily_pnl_pct * 100)

                # Kill Switch Status
                from sgr.core.types import TradingMode
                from sgr.risk.kill_switch import get_kill_switch

                ks = get_kill_switch(TradingMode(mode))
                m.kill_switch_active.labels(trading_mode=mode).set(1.0 if ks.is_active else 0.0)
            except Exception as e:
                log.debug("monitoring.risk_error", error=str(e))

        # Strategy Metriken
        if self._strategy_registry:
            try:
                active = self._strategy_registry.get_active()
                m.strategy_active_count.set(len(active))

                for name, entry in self._strategy_registry.get_all().items():
                    if entry.performance:
                        p = entry.performance
                        m.strategy_sharpe.labels(strategy=name, trading_mode=mode).set(
                            p.sharpe_ratio
                        )
                        m.strategy_hit_rate.labels(strategy=name, trading_mode=mode).set(
                            p.hit_rate * 100
                        )
            except Exception as e:
                log.debug("monitoring.strategy_error", error=str(e))


def create_metrics_app():
    """
    Erstellt ASGI-App für Prometheus Metrics Endpoint.
    Mounten in FastAPI: app.mount("/metrics", create_metrics_app())
    """
    return make_asgi_app()


def add_metrics_middleware(app: Any) -> None:
    """
    Fügt Request-Tracking Middleware zu FastAPI hinzu.
    Tracked: request count, latency per route.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            start = time.monotonic()
            response = await call_next(request)
            duration = time.monotonic() - start

            # Nur API-Routen tracken (kein /metrics, /health spam)
            path = request.url.path
            if path.startswith("/api/") or path in ("/health",):
                m.api_requests.labels(
                    method=request.method,
                    path=path,
                    status_code=response.status_code,
                ).inc()
                m.api_latency.labels(
                    method=request.method,
                    path=path,
                ).observe(duration)

            return response

    app.add_middleware(MetricsMiddleware)
