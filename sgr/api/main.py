"""
SGR FastAPI Application
=======================
Haupteinstiegspunkt der REST + WebSocket API.

Startup-Sequenz (via lifespan):
    1. Config validieren
    1a. Startup Safety Checks (fail-fast, siehe core/startup_checks.py)
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
    /api/v1/trading  → Manueller Trading-Cycle-Trigger (Orchestrator)
    /api/v1/reconciliation → Exchange- vs. lokaler State-Abgleich (Phase 7B)
    /ws              → WebSocket Streams
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sgr.core.config import get_config
from sgr.core.logging import get_logger, setup_logging
from sgr.core.types import Environment, TradingMode

log = get_logger(__name__)

# Rolle, mit der lifespan() aufgerufen wird. Steuert, ob der volle Trading
# Lifecycle (Exchange Pool, Strategy Engine, Execution Engine, Orchestrator,
# Market Data Engine) gestartet wird, oder nur die Read-Pfad-Infrastruktur
# (DB, Redis, Event Bus, Feature Store), die die Read-Only-API-Router
# brauchen (siehe sgr/api/dependencies.py Modul-Docstring).
#
# "worker" ist bewusst der Default: er entspricht dem Verhalten VOR dieser
# Aufteilung und ist das, was die bestehende Test-Suite (tests/api/
# test_main.py) bereits durchgehend prueft (voller Lifecycle inkl.
# strategy_engine.start(), md_engine.start() etc.). sgr-worker/main.py
# ruft explizit role="worker" auf; create_app() (fuer sgr-api) uebergibt
# explizit role="api".
LifespanRole = Literal["api", "worker"]


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
    reconciliation_engine: Any = None
    feature_store: Any = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(
    app: FastAPI, role: LifespanRole = "worker"
) -> AsyncIterator[None]:
    """
    Startup + Shutdown aller Systemkomponenten.
    Fehler beim Startup → Server startet nicht (fail fast).

    role="worker" (Default): startet den vollstaendigen Trading Lifecycle
        (Exchange Pool, Risk/Portfolio/Strategy/Execution Engines,
        Orchestrator, Reconciliation Engine, Crash Recovery, Market Data
        Engine mit Candle-Event-Subscription). sgr-worker ist alleiniger
        Owner dieser Komponenten (siehe sgr/worker/main.py).
    role="api": startet ausschliesslich die Infrastruktur, die die
        Read-Only-API-Router brauchen (DB, Redis, Event Bus, Feature
        Store als Redis-Fassade). Kein Exchange Pool, keine Trading
        Engines, kein Orchestrator, kein Market Data Polling - die API
        besitzt seit der sgr-api/sgr-worker-Trennung keinen eigenen
        Trading Lifecycle mehr (siehe sgr/api/dependencies.py
        Modul-Docstring). app.state.* Engine-Attribute bleiben dabei
        auf ihrem AppState-Klassendefault None stehen.
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

    # 1a. Startup Safety Checks
    # Laufen bewusst VOR jeder Verbindung (DB, Redis, Exchange) und VOR
    # Observability-Setup. Fail-Fast: eine unsichere LIVE-Konfiguration
    # (fehlende Credentials, deaktivierte Fat-Finger-Caps, zu lasche Hard
    # Limits, bereits aktiver Kill Switch) darf den Server nicht mit
    # scheinbar funktionierenden, aber ungeschützten Trading-Pfaden
    # hochfahren lassen. Anders als Crash Recovery (Schritt 8d, fail-safe)
    # ist dieser Schritt absichtlich fail-fast - siehe
    # sgr/core/startup_checks.py Modul-Docstring.
    from sgr.core.startup_checks import StartupSafetyChecker

    StartupSafetyChecker(config).run_or_raise()

    # 1b. Observability (OpenTelemetry Metrics + Auto-Instrumentation)
    # Wurde zuvor nirgends aufgerufen (deferred finding: monitoring/
    # observability.py war reiner Dead Code, 0% Coverage). Jetzt verdrahtet:
    # setup_metrics() registriert den Prometheus-MeterProvider, ohne den
    # sgr.monitoring.metrics.SGRMetrics-Meter (bereits an anderer Stelle
    # verwendet) stillschweigend gegen den OpenTelemetry-No-Op-Meter fällt.
    # Tracing bleibt bewusst No-Op (siehe setup_tracing()-Docstring), bis
    # die OTLP-Migration explizit angegangen wird.
    from sgr.monitoring.observability import setup_observability

    try:
        setup_observability(app)
    except Exception as e:
        # Observability-Fehler sollen den Start nicht verhindern
        # (fail-safe, nicht fail-fast - analog zu Crash Recovery unten).
        log.warning("sgr.api.observability_setup_failed", error=str(e))

    # 2. Database
    from sgr.core.database import close_db, init_db

    await init_db()

    # 2b. Repositories (Persistenz-Schicht)
    # Vorher NIRGENDS im Lifespan instanziiert - PositionRepository,
    # OrderRepository, StrategyRepository etc. existierten isoliert von
    # der laufenden App, obwohl vollstaendig implementiert und getestet.
    # Ohne diesen Schritt ist echte Crash-Recovery unmoeglich (keine
    # Injektion in PortfolioEngine/ExecutionEngine/StrategyRegistry).
    from sgr.core.repositories import get_repositories

    repos = get_repositories()
    app.state.repositories = repos

    # 3. Event Bus
    from sgr.core.event_bus import get_event_bus

    bus = get_event_bus()
    await bus.connect()

    # 4. Feature Store
    from sgr.market_data.feature_store import FeatureStore

    feature_store = FeatureStore()
    await feature_store.connect()
    app.state.feature_store = feature_store

    # ------------------------------------------------------------------
    # Ab hier: nur role="worker" startet den vollstaendigen Trading
    # Lifecycle (Exchange Pool, Trading Engines, Orchestrator, Market
    # Data Engine). role="api" ueberspringt diesen gesamten Block - die
    # entsprechenden app.state.*-Attribute bleiben auf ihrem
    # AppState-Klassendefault None (siehe Klassendefinition oben).
    # ------------------------------------------------------------------
    pool: Any = None
    strategy_engine: Any = None
    execution_engine: Any = None
    md_engine: Any = None

    if role == "worker":
        # 5. Exchange Pool (nur bei konfigurierten Keys)
        from sgr.core.types import ExchangeID
        from sgr.exchanges.factory import ExchangePool

        pool = ExchangePool()
        primary_exchange = config.primary_exchange

        try:
            if config.tenant_id is not None:
                # Multi-Tenant-Worker (Commit 5, Option A): Credentials
                # kommen aus der DB (APIKeyModel) statt aus config.credentials
                # (.env). Kein Pionex/Testnet-Sonderfall hier - Paper-Mode-
                # Credentials muessen fuer Tenants genauso explizit in der DB
                # hinterlegt sein wie Live-Credentials (siehe
                # sgr.core.tenant_credentials.load_tenant_credentials()
                # Docstring: Tenant ohne konfigurierte Keys faehrt den
                # Worker trotzdem hoch, nur ohne Exchange Pool - kein
                # fail-fast, analog zum bestehenden .env-ValueError-Pfad
                # unten).
                from sgr.core.tenant_credentials import load_tenant_credentials

                try:
                    tenant_credentials = await load_tenant_credentials(
                        config.tenant_id, primary_exchange, config.trading_mode
                    )
                    await pool.initialize(
                        [primary_exchange], config.trading_mode, credentials=tenant_credentials
                    )
                except ValueError:
                    log.warning(
                        "sgr.api.tenant_credentials_missing",
                        tenant_id=config.tenant_id,
                        exchange=primary_exchange.value,
                        trading_mode=config.trading_mode.value,
                    )
            elif primary_exchange == ExchangeID.PIONEX:
                # Pionex hat kein Testnet: Paper Mode braucht keine echten Keys
                # (PionexAdapter.connect() simuliert lokal, siehe pionex.py)
                if config.trading_mode == TradingMode.PAPER or (
                    config.credentials.pionex_live_api_key
                    and config.credentials.pionex_live_secret
                ):
                    await pool.initialize([primary_exchange], config.trading_mode)
            else:
                # Andere Exchanges (z.B. Binance) haben ein echtes Testnet und
                # brauchen dafuer konfigurierte Paper-Testnet-Keys, bzw. echte
                # Live-Keys im LIVE-Modus. get_credentials() wirft ValueError,
                # wenn die entsprechenden Env-Vars nicht gesetzt sind.
                try:
                    config.credentials.get_credentials(
                        primary_exchange.value, config.trading_mode
                    )
                    await pool.initialize([primary_exchange], config.trading_mode)
                except ValueError:
                    log.warning(
                        "sgr.api.exchange_credentials_missing",
                        exchange=primary_exchange.value,
                        trading_mode=config.trading_mode.value,
                    )
        except Exception as e:
            log.warning(
                "sgr.api.exchange_init_failed", exchange=primary_exchange.value, error=str(e)
            )

        app.state.exchange_pool = pool

        # 6. Risk Engine
        from sgr.risk.engine import RiskEngine

        risk_engine = RiskEngine(config.trading_mode)
        await risk_engine.initialize()
        app.state.risk_engine = risk_engine

        # 7. Portfolio Engine
        from sgr.portfolio.engine import PortfolioEngine

        portfolio_engine = PortfolioEngine(
            config.trading_mode, position_repository=repos.positions
        )
        app.state.portfolio_engine = portfolio_engine

        # 8. Strategy Engine
        # Strategien registrieren (Import triggert @register Decorator)
        import sgr.strategy.mean_reversion  # noqa: F401
        import sgr.strategy.trend_following  # noqa: F401
        from sgr.strategy.engine import StrategyEngine
        from sgr.strategy.registry import StrategyRegistry

        registry = StrategyRegistry.get()
        registry.inject_repository(repos.strategies)
        await registry.sync_registrations_to_db()

        # Aktiviere alle validierten Strategien für das Paper Trading.
        # Default: Strategien starten deaktiviert, müssen explizit aktiviert werden.
        # Hier aktivieren wir nur die, die bereits validiert sind (is_validated=True
        # nach erfolgreichem Backtest). Weitere Strategien können durch Management-APIs
        # später aktiviert werden.
        for entry in registry.get_all().values():
            if entry.is_validated:
                await registry.activate(entry.strategy.name)
                log.info(
                    "sgr.api.strategy_activated",
                    name=entry.strategy.name,
                    version=entry.strategy.version,
                )

        strategy_engine = StrategyEngine(config.trading_mode, feature_store)
        await strategy_engine.start()
        app.state.strategy_engine = strategy_engine

        # 8b. Execution Engine + Trading Orchestrator
        # Verdrahtet den zuvor nicht verbundenen Pfad Signal -> Risk -> Order ->
        # Portfolio. Siehe sgr/orchestrator/engine.py für die Architekturbegründung.
        from sgr.execution.engine import ExecutionEngine
        from sgr.orchestrator.engine import TradingOrchestrator

        execution_engine = ExecutionEngine(
            pool, config.trading_mode, order_repository=repos.orders
        )
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

        # 8c. Reconciliation Engine (Phase 7B)
        # Nur in LIVE aussagekräftig (siehe sgr/reconciliation/engine.py
        # Modul-Docstring) - wird trotzdem immer instanziiert, damit
        # get_reconciliation_engine() nicht je nach Modus fehlschlägt.
        # reconcile() selbst gibt in PAPER/DRY_RUN fail-safe SKIPPED_NOT_LIVE
        # zurück, statt einen Fehler zu werfen.
        from sgr.reconciliation.engine import ReconciliationEngine

        reconciliation_engine = ReconciliationEngine(
            exchange_pool=pool,
            portfolio_engine=portfolio_engine,
            trading_mode=config.trading_mode,
        )
        app.state.reconciliation_engine = reconciliation_engine

        # 8d. Crash Recovery
        # Frueher reiner Pseudo-Code (RecoveryManager._restore_*() taten
        # nichts). Jetzt echter Delegat an die gerade injizierten Komponenten.
        # Laeuft VOR dem Market-Data-Start (Schritt 9), damit Recovery
        # abgeschlossen ist, bevor Live-Candle-Events den Orchestrator ausloesen.
        # Fehler hier stoppen den Start NICHT (fail-safe, nicht fail-fast) -
        # ein unvollstaendiges Recovery ist besser als ein Server, der gar
        # nicht hochkommt; die naechste ReconciliationEngine (Phase 7B)
        # deckt verbleibende Diskrepanzen ohnehin auf.
        from sgr.core.resilience import RecoveryManager

        recovery_manager = RecoveryManager(
            portfolio_engine=portfolio_engine,
            order_repository=repos.orders,
            strategy_registry=registry,
            trading_mode=config.trading_mode,
        )
        await recovery_manager.recover_after_crash()

        # 9. Market Data Engine
        from sgr.market_data.engine import MarketDataEngine

        md_engine = MarketDataEngine(pool, config.trading_mode, feature_store)
        # Standard-Subscriptions
        if pool._adapters:
            md_engine.subscribe("BTC/USDT", primary_exchange, ["1h", "4h"])
            md_engine.subscribe("ETH/USDT", primary_exchange, ["1h"])
            await md_engine.start()

            # Orchestrator automatisch bei jedem neuen Candle auslösen
            # (additiver Event-Trigger; run_cycle() bleibt auch direkt aufrufbar,
            # z.B. für manuelle Trigger oder Tests, ohne Redis-Abhängigkeit)
            #
            # Tenant-Scoping (siehe Audit nach Commit 5): consumer_group
            # und consumer_name enthalten die tenant_id (Default "default"
            # fuer Single-Tenant-Deployments ohne TENANT_ID). Redis Streams
            # verteilen Nachrichten INNERHALB einer Consumer-Group per
            # Round-Robin auf ihre Consumer - mit dem vorherigen, fuer
            # ALLE Tenants identischen "orchestrator"/"orchestrator-1"
            # haetten sich Gordon und Sumo CandleEvents gegenseitig
            # weggenommen, statt dass beide JEDES Event erhalten. Getrennte
            # Consumer-Groups pro Tenant sind bei Redis Streams die
            # korrekte Loesung fuer "mehrere unabhaengige Konsumenten
            # desselben Streams" - jede Gruppe sieht den vollen Stream.
            from sgr.core.types import CandleEvent

            tenant_suffix = config.tenant_id or "default"
            bus.subscribe(
                CandleEvent,
                orchestrator.on_candle_event,
                consumer_group=f"orchestrator:{tenant_suffix}",
                consumer_name=f"orchestrator-{tenant_suffix}-1",
            )
        app.state.market_data_engine = md_engine

        # 10. Monitoring Engine
        # Liest periodisch (alle 10s) State aus RiskEngine/PortfolioEngine/
        # StrategyRegistry und schreibt ihn ueber die OTel-Metrik-API
        # (sgr/monitoring/metrics.py SGRMetrics) - dieselbe REGISTRY, die
        # setup_observability() bereits als PrometheusMetricReader-Ziel
        # konfiguriert hat (siehe oben), landet also automatisch im
        # bestehenden /metrics-Endpoint ohne weitere Verdrahtung.
        # Bisher (dokumentierter Bestandsbefund) wurde diese Klasse nie
        # instanziiert - Portfolio/Risk/Strategy-Gauges blieben dauerhaft
        # auf ihrem Initialwert 0, unabhaengig vom tatsaechlichen State.
        from sgr.monitoring.engine import MonitoringEngine

        monitoring_engine = MonitoringEngine(
            risk_engine=risk_engine,
            portfolio_engine=portfolio_engine,
            strategy_registry=registry,
            trading_mode=config.trading_mode.value,
        )
        await monitoring_engine.start()
        app.state.monitoring_engine = monitoring_engine

        # 11. Worker Metrics Publisher (siehe sgr/monitoring/
        # worker_metrics_bridge.py Modul-Docstring): macht die gerade
        # aktivierten Metriken (MonitoringEngine + trading_metrics.py)
        # fuer Prometheus sichtbar, OHNE dass der Worker einen eigenen
        # HTTP-Port oeffnen muss (bewusstes Architekturprinzip, siehe
        # sgr/worker/main.py). Push statt Pull, analog zum Redis-Ticker-
        # Cache-Muster aus Schritt 6.
        from sgr.monitoring.worker_metrics_bridge import WorkerMetricsPublisher

        worker_metrics_publisher = WorkerMetricsPublisher(
            redis_client=feature_store.redis_client,
            tenant_id=config.tenant_id or "default",
            trading_mode=config.trading_mode.value,
        )
        await worker_metrics_publisher.start()
        app.state.worker_metrics_publisher = worker_metrics_publisher

    log.info(
        "sgr.api.ready",
        role=role,
        tenant_id=config.tenant_id,
        host=config.api.host,
        port=config.api.port,
    )

    yield

    # --------------- Shutdown ---------------
    log.info("sgr.api.shutting_down", role=role)

    if role == "worker":
        await worker_metrics_publisher.stop()
        await monitoring_engine.stop()
        await strategy_engine.stop()
        if md_engine.is_running:
            await md_engine.stop()
        # Baustein 7 (Shutdown Safety): noch offene/im Fill-Monitoring
        # befindliche Orders best-effort cancelln, BEVOR die
        # Exchange-Verbindungen geschlossen werden - danach waere kein
        # Cancel mehr moeglich.
        await execution_engine.shutdown()
        await pool.close_all()

    await feature_store.close()
    await bus.close()

    await close_db()

    log.info("sgr.api.stopped", role=role)


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _api_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Wrapper um lifespan(), der role="api" fest verdrahtet.

    FastAPI ruft den lifespan-Contextmanager immer mit genau einem
    Positionsargument (der App-Instanz) auf - ein zusaetzlicher Parameter
    wie role kann daher nicht direkt als lifespan= uebergeben werden.
    sgr-worker/main.py ruft lifespan() dagegen direkt mit role="worker"
    auf (siehe dort), ohne diesen Wrapper.
    """
    async with lifespan(app, role="api"):
        yield


def create_app() -> FastAPI:
    config = get_config()

    app = FastAPI(
        title="Project SGR",
        description="Institutional AI-powered Multi-Asset Trading System",
        version=config.version,
        docs_url="/docs" if config.environment != Environment.PRODUCTION else None,
        redoc_url="/redoc" if config.environment != Environment.PRODUCTION else None,
        lifespan=_api_lifespan,
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
        reconciliation,
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
    app.include_router(
        reconciliation.router, prefix="/api/v1/reconciliation", tags=["reconciliation"]
    )
    app.include_router(websocket.router, prefix="/ws", tags=["websocket"])

    # SaaS Layer
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(apikey_router, prefix="/api/v1")
    app.include_router(billing_router, prefix="/api/v1")

    # Prometheus Metrics Endpoint
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Any:
        """
        Prometheus metrics endpoint.

        Kombiniert die lokale REGISTRY dieses Prozesses (in role="api"
        praktisch leer, da die API seit der Rollentrennung keine
        Trading-Engines mehr besitzt) mit den per Redis gepushten
        Snapshots aller sgr-worker-Prozesse (siehe sgr/monitoring/
        worker_metrics_bridge.py) - Prometheus muss dadurch nur diesen
        einen Endpunkt scrapen, obwohl die eigentlichen Trading-
        Metriken im Worker-Prozess entstehen, der selbst keinen HTTP-
        Port besitzt.
        """
        from prometheus_client import REGISTRY, generate_latest

        body = generate_latest(REGISTRY)

        redis_client = getattr(app.state.feature_store, "redis_client", None)
        if redis_client is not None:
            from sgr.monitoring.worker_metrics_bridge import collect_worker_metrics

            worker_body = await collect_worker_metrics(redis_client)
            if worker_body:
                body = body + b"\n" + worker_body

        return Response(body, media_type="text/plain; charset=utf-8")

    return app


# Singleton App-Instanz
app = create_app()
