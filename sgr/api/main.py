"""
SGR FastAPI Application
=======================
Haupteinstiegspunkt der REST + WebSocket API.

Startup-Sequenz (via lifespan):
    1. Config validieren
    2. Logging initialisieren
    3. DB + Redis verbinden
    4. Exchange Pool initialisieren
    5. Market Data Engine starten
    6. Strategy Engine starten
    7. Risk Engine initialisieren
    8. API bereit (Traffic annehmen)

Middleware-Stack (außen → innen):
    CORS → RequestID → RateLimit → Auth → Handler

Routers:
    /health          → Health Check (kein Auth)
    /api/v1/market   → Market Data
    /api/v1/portfolio→ Positionen + PnL
    /api/v1/risk     → Risk Metriken + Limits
    /api/v1/strategy → Strategy Engine Status
    /api/v1/orders   → Order History
    /api/v1/system   → Kill Switch, Status
    /ws              → WebSocket Streams
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sgr.core.config import get_config
from sgr.core.logging import get_logger, setup_logging
from sgr.core.types import Environment, TradingMode

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Application State (shared across requests)
# ---------------------------------------------------------------------------


class AppState:
    """
    Singleton App-State: alle Engines und Pools.
    Wird im lifespan initialisiert und via request.app.state zugegriffen.
    """

    exchange_pool: Any = None
    market_data_engine: Any = None
    strategy_engine: Any = None
    risk_engine: Any = None
    portfolio_engine: Any = None
    execution_engine: Any = None
    orchestrator: Any = None
    feature_store: Any = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Startup + Shutdown aller Systemkomponenten.
    Fehler beim Startup → Server startet nicht (fail fast).
    """
    config = get_config()

    # 1. Logging
    setup_logging(
        log_level=config.monitoring.log_level,
        json_output=config.environment == Environment.PRODUCTION,
        trading_mode=config.trading_mode,
    )

    log.info(
        "sgr.api.starting",
        version=config.version,
        environment=config.environment.value,
        trading_mode=config.trading_mode.value,
    )

    # 2. Database
    from sgr.core.database import close_db, init_db

    await init_db()

    # 3. Event Bus
    from sgr.core.event_bus import get_event_bus

    bus = get_event_bus()
    await bus.connect()

    # 4. Feature Store
    from sgr.market_data.feature_store import FeatureStore

    feature_store = FeatureStore()
    await feature_store.connect()
    app.state.feature_store = feature_store

    # 5. Exchange Pool (nur bei konfigurierten Keys)
    from sgr.core.types import ExchangeID
    from sgr.exchanges.factory import ExchangePool

    pool = ExchangePool()

    try:
        # Pionex hat kein Testnet: Paper Mode braucht keine echten Keys
        # (PionexAdapter.connect() simuliert lokal, siehe pionex.py)
        if config.trading_mode == TradingMode.PAPER or (
            config.credentials.pionex_live_api_key and config.credentials.pionex_live_secret
        ):
            await pool.initialize([ExchangeID.PIONEX], config.trading_mode)
    except Exception as e:
        log.warning("sgr.api.exchange_init_failed", exchange="pionex", error=str(e))

    app.state.exchange_pool = pool

    # 6. Risk Engine
    from sgr.risk.engine import RiskEngine

    risk_engine = RiskEngine(config.trading_mode)
    await risk_engine.initialize()
    app.state.risk_engine = risk_engine

    # 7. Portfolio Engine
    from sgr.portfolio.engine import PortfolioEngine

    portfolio_engine = PortfolioEngine(config.trading_mode)
    app.state.portfolio_engine = portfolio_engine

    # 8. Strategy Engine
    # Strategien registrieren (Import triggert @register Decorator)
    import sgr.strategy.mean_reversion  # noqa: F401
    import sgr.strategy.trend_following  # noqa: F401
    from sgr.strategy.engine import StrategyEngine

    strategy_engine = StrategyEngine(config.trading_mode, feature_store)
    await strategy_engine.start()
    app.state.strategy_engine = strategy_engine

    # 8b. Execution Engine + Trading Orchestrator
    # Verdrahtet den zuvor nicht verbundenen Pfad Signal -> Risk -> Order ->
    # Portfolio. Siehe sgr/orchestrator/engine.py für die Architekturbegründung.
    from sgr.execution.engine import ExecutionEngine
    from sgr.orchestrator.engine import TradingOrchestrator

    execution_engine = ExecutionEngine(pool, config.trading_mode)
    app.state.execution_engine = execution_engine

    orchestrator = TradingOrchestrator(
        strategy_engine=strategy_engine,
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        portfolio_engine=portfolio_engine,
        feature_store=feature_store,
        trading_mode=config.trading_mode,
    )
    app.state.orchestrator = orchestrator

    # 9. Market Data Engine
    from sgr.market_data.engine import MarketDataEngine

    md_engine = MarketDataEngine(pool, config.trading_mode, feature_store)
    # Standard-Subscriptions
    if pool._adapters:
        md_engine.subscribe("BTC/USDT", ExchangeID.PIONEX, ["1h", "4h"])
        md_engine.subscribe("ETH/USDT", ExchangeID.PIONEX, ["1h"])
        await md_engine.start()

        # Orchestrator automatisch bei jedem neuen Candle auslösen
        # (additiver Event-Trigger; run_cycle() bleibt auch direkt aufrufbar,
        # z.B. für manuelle Trigger oder Tests, ohne Redis-Abhängigkeit)
        from sgr.core.types import CandleEvent

        bus.subscribe(
            CandleEvent,
            orchestrator.on_candle_event,
            consumer_group="orchestrator",
            consumer_name="orchestrator-1",
        )
    app.state.market_data_engine = md_engine

    log.info("sgr.api.ready", host=config.api.host, port=config.api.port)

    yield

    # --------------- Shutdown ---------------
    log.info("sgr.api.shutting_down")

    await strategy_engine.stop()
    if md_engine.is_running:
        await md_engine.stop()
    await pool.close_all()
    await feature_store.close()
    await bus.close()

    await close_db()

    log.info("sgr.api.stopped")


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    config = get_config()

    app = FastAPI(
        title="Project SGR",
        description="Institutional AI-powered Multi-Asset Trading System",
        version=config.version,
        docs_url="/docs" if config.environment != Environment.PRODUCTION else None,
        redoc_url="/redoc" if config.environment != Environment.PRODUCTION else None,
        lifespan=lifespan,
    )

    app.state = AppState()  # type: ignore[assignment]

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request ID Middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Any) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # Request Timing
    @app.middleware("http")
    async def add_timing(request: Request, call_next: Any) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        return response

    # Global Exception Handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "api.unhandled_exception",
            path=request.url.path,
            method=request.method,
            error=str(exc),
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    # Mount Routers
    from sgr.api.routers import (
        health,
        market,
        orders,
        portfolio,
        risk,
        strategy,
        system,
        trading,
        websocket,
    )
    from sgr.saas.routers import apikey_router, auth_router, billing_router

    app.include_router(health.router, tags=["health"])
    app.include_router(market.router, prefix="/api/v1/market", tags=["market"])
    app.include_router(portfolio.router, prefix="/api/v1/portfolio", tags=["portfolio"])
    app.include_router(risk.router, prefix="/api/v1/risk", tags=["risk"])
    app.include_router(strategy.router, prefix="/api/v1/strategy", tags=["strategy"])
    app.include_router(orders.router, prefix="/api/v1/orders", tags=["orders"])
    app.include_router(system.router, prefix="/api/v1/system", tags=["system"])
    app.include_router(trading.router, prefix="/api/v1/trading", tags=["trading"])
    app.include_router(websocket.router, prefix="/ws", tags=["websocket"])

    # SaaS Layer
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(apikey_router, prefix="/api/v1")
    app.include_router(billing_router, prefix="/api/v1")

    return app


# Singleton App-Instanz
app = create_app()
