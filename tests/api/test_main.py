"""
Tests für sgr.api.main (FastAPI Application Factory + Lifespan).

Coverage-Ziel: ~37% -> hoch.

Teststrategie (zwei Teile, analog zur Modul-Struktur):

1. create_app() + Middleware-Stack + globaler Exception-Handler:
   Getestet über einen echten `TestClient(app)` OHNE `with`-Block.
   Starlettes TestClient betritt den lifespan-Kontextmanager nur
   innerhalb eines `with`-Statements; ein einfacher `TestClient(app)`
   umgeht lifespan vollständig (keine DB/Redis/Exchange-Verbindung
   nötig) und deckt trotzdem create_app() selbst, CORS-Setup,
   Router-Mounting, sowie die drei Middleware/Handler-Closures ab
   (add_request_id, add_timing, global_exception_handler), da diese
   bei jedem Request durchlaufen werden.

2. lifespan(): Vollständig isoliert getestet durch Monkeypatchen
   jedes einzelnen Startup-Schritts (DB, Event Bus, Feature Store,
   Exchange Pool, Risk/Portfolio/Strategy/Execution/Reconciliation
   Engines, Recovery Manager, Market Data Engine) auf AsyncMock/
   MagicMock-Ebene - dieselbe Technik wie in tests/exchanges/
   test_ccxt_base.py und tests/unit/test_event_bus.py für Module,
   die echte Infrastruktur voraussetzen. Wir rufen den lifespan-
   Generator direkt auf (nicht über TestClient), um sowohl den
   Startup- als auch den Shutdown-Pfad explizit zu triggern und zu
   assertieren, dass app.state korrekt befüllt wird.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sgr.api.main import AppState, create_app, lifespan
from sgr.core.types import Environment, ExchangeID, TradingMode

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------
# create_app() / Middleware / Exception Handler (via TestClient, ohne lifespan)
# ---------------------------------------------------------------------


class TestCreateApp:
    def test_create_app_returns_fastapi_instance(self) -> None:
        app = create_app()
        assert isinstance(app, FastAPI)
        assert app.title == "Project SGR"

    def test_create_app_sets_app_state(self) -> None:
        app = create_app()
        assert isinstance(app.state, AppState)

    def test_health_route_mounted_and_reachable(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.get("/health")

        assert response.status_code == 200

    def test_request_id_header_is_set(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.get("/health")

        assert "x-request-id" in response.headers
        # Muss eine gültige UUID4-Darstellung sein (36 Zeichen inkl. Bindestriche).
        assert len(response.headers["x-request-id"]) == 36

    def test_response_time_header_is_set(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.get("/health")

        assert "x-response-time-ms" in response.headers
        # Muss als float mit einer Nachkommastelle formatiert sein, z.B. "2.1".
        float(response.headers["x-response-time-ms"])

    def test_unhandled_exception_returns_500_with_request_id(self) -> None:
        app = create_app()

        @app.get("/__boom__")
        async def boom() -> None:
            raise RuntimeError("kaboom")

        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/__boom__")

        assert response.status_code == 500
        body = response.json()
        assert body["detail"] == "Internal server error"
        assert body["request_id"] is not None
        assert len(body["request_id"]) == 36

    def test_docs_disabled_in_production(self) -> None:
        fake_config = MagicMock()
        fake_config.version = "0.0.0-test"
        fake_config.environment = Environment.PRODUCTION
        fake_config.api.cors_origins = ["*"]

        with patch("sgr.api.main.get_config", return_value=fake_config):
            app = create_app()

        assert app.docs_url is None
        assert app.redoc_url is None


# ---------------------------------------------------------------------
# lifespan() - Startup + Shutdown, vollständig gemockt
# ---------------------------------------------------------------------


def _patch_lifespan_dependencies(
    paper_mode: bool = True,
    has_adapters: bool = True,
    primary_exchange: ExchangeID = ExchangeID.PIONEX,
    tenant_id: str | None = None,
):
    """
    Baut den vollständigen Satz an Patches, um lifespan() ohne echte
    Infrastruktur durchlaufen zu lassen. Gibt (patchers, mocks) zurück,
    damit Tests gezielt Assertions auf einzelne Komponenten machen können.

    tenant_id: default None (Single-Tenant-Verhalten, unveraendert).
        Gesetzt aktiviert den Multi-Tenant-Zweig in lifespan() Schritt 5
        (Commit 5) - siehe TestLifespanTenantId weiter unten fuer die
        zugehoerigen load_tenant_credentials()-Patches.
    """
    config = MagicMock()
    config.monitoring.log_level = "INFO"
    config.environment = Environment.DEVELOPMENT
    config.trading_mode = TradingMode.PAPER if paper_mode else TradingMode.LIVE
    config.version = "0.0.0-test"
    config.api.host = "127.0.0.1"
    config.api.port = 8000
    config.primary_exchange = primary_exchange
    # MagicMock()-Attribute sind standardmaessig truthy/nicht-None - ohne
    # diese explizite Zeile wuerde config.tenant_id ein MagicMock-Objekt
    # sein und den "if config.tenant_id is not None:"-Zweig in lifespan()
    # unbeabsichtigt in JEDEM bestehenden Test aktivieren.
    config.tenant_id = tenant_id
    if paper_mode:
        config.credentials.pionex_live_api_key = None
        config.credentials.pionex_live_secret = None
    else:
        config.credentials.pionex_live_api_key = "key"
        config.credentials.pionex_live_secret = "secret"

    # get_credentials() wird nur fuer Nicht-Pionex-Exchanges (z.B. Binance)
    # aufgerufen (siehe lifespan()-Zweig fuer primary_exchange != PIONEX).
    # Default: liefert gueltige Dummy-Credentials. Einzelne Tests
    # ueberschreiben mocks["config"].credentials.get_credentials mit einem
    # side_effect, um den ValueError-Pfad (fehlende Keys) zu testen.
    config.credentials.get_credentials = MagicMock(
        return_value={"apiKey": "test_key", "secret": "test_secret", "testnet": True}
    )

    repos = MagicMock()

    bus = AsyncMock()

    feature_store = AsyncMock()

    pool = MagicMock()
    pool.initialize = AsyncMock()
    pool.close_all = AsyncMock()
    pool._adapters = {"pionex": object()} if has_adapters else {}

    risk_engine = MagicMock()
    risk_engine.initialize = AsyncMock()

    portfolio_engine = MagicMock()

    registry = MagicMock()
    registry.inject_repository = MagicMock()
    registry.sync_registrations_to_db = AsyncMock()

    strategy_engine = MagicMock()
    strategy_engine.start = AsyncMock()
    strategy_engine.stop = AsyncMock()

    execution_engine = MagicMock()
    execution_engine.shutdown = AsyncMock()

    orchestrator = MagicMock()

    reconciliation_engine = MagicMock()

    recovery_manager = MagicMock()
    recovery_manager.recover_after_crash = AsyncMock()

    md_engine = MagicMock()
    md_engine.subscribe = MagicMock()
    md_engine.start = AsyncMock()
    md_engine.stop = AsyncMock()
    md_engine.is_running = True

    mocks = {
        "config": config,
        "repos": repos,
        "bus": bus,
        "feature_store": feature_store,
        "pool": pool,
        "risk_engine": risk_engine,
        "portfolio_engine": portfolio_engine,
        "registry": registry,
        "strategy_engine": strategy_engine,
        "execution_engine": execution_engine,
        "orchestrator": orchestrator,
        "reconciliation_engine": reconciliation_engine,
        "recovery_manager": recovery_manager,
        "md_engine": md_engine,
    }

    patchers = [
        patch("sgr.api.main.get_config", return_value=config),
        patch("sgr.api.main.setup_logging"),
        patch("sgr.monitoring.observability.setup_observability"),
        patch("sgr.core.database.init_db", AsyncMock()),
        patch("sgr.core.database.close_db", AsyncMock()),
        patch("sgr.core.repositories.get_repositories", return_value=repos),
        patch("sgr.core.event_bus.get_event_bus", return_value=bus),
        patch("sgr.market_data.feature_store.FeatureStore", return_value=feature_store),
        patch("sgr.exchanges.factory.ExchangePool", return_value=pool),
        patch("sgr.risk.engine.RiskEngine", return_value=risk_engine),
        patch("sgr.portfolio.engine.PortfolioEngine", return_value=portfolio_engine),
        patch("sgr.strategy.registry.StrategyRegistry.get", return_value=registry),
        patch("sgr.strategy.engine.StrategyEngine", return_value=strategy_engine),
        patch("sgr.execution.engine.ExecutionEngine", return_value=execution_engine),
        patch("sgr.orchestrator.engine.TradingOrchestrator", return_value=orchestrator),
        patch(
            "sgr.reconciliation.engine.ReconciliationEngine",
            return_value=reconciliation_engine,
        ),
        patch("sgr.core.resilience.RecoveryManager", return_value=recovery_manager),
        patch("sgr.market_data.engine.MarketDataEngine", return_value=md_engine),
        # StartupSafetyChecker erwartet ein echtes SGRConfig-Objekt (reale
        # Decimal/float-Vergleiche), nicht den hier verwendeten MagicMock.
        # Diese Tests decken die Lifespan-Infrastruktur-Verdrahtung ab, nicht
        # die Safety-Check-Logik selbst (siehe dediziert
        # tests/unit/test_startup_checks.py) - daher hier bewusst als
        # No-Op gepatcht.
        patch("sgr.core.startup_checks.StartupSafetyChecker.run_or_raise", return_value=None),
    ]

    return patchers, mocks


class TestLifespanStartupShutdownPaperMode:
    async def test_full_lifecycle_paper_mode_with_adapters(self) -> None:
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=True)

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                # --------- Startup-Assertions ---------
                mocks["bus"].connect.assert_awaited_once()
                mocks["feature_store"].connect.assert_awaited_once()
                mocks["pool"].initialize.assert_awaited_once()
                mocks["risk_engine"].initialize.assert_awaited_once()
                mocks["registry"].inject_repository.assert_called_once_with(
                    mocks["repos"].strategies
                )
                mocks["registry"].sync_registrations_to_db.assert_awaited_once()
                mocks["strategy_engine"].start.assert_awaited_once()
                mocks["recovery_manager"].recover_after_crash.assert_awaited_once()
                mocks["md_engine"].subscribe.assert_called()
                mocks["md_engine"].start.assert_awaited_once()

                assert app.state.exchange_pool is mocks["pool"]
                assert app.state.risk_engine is mocks["risk_engine"]
                assert app.state.portfolio_engine is mocks["portfolio_engine"]
                assert app.state.strategy_engine is mocks["strategy_engine"]
                assert app.state.execution_engine is mocks["execution_engine"]
                assert app.state.orchestrator is mocks["orchestrator"]
                assert app.state.reconciliation_engine is mocks["reconciliation_engine"]
                assert app.state.feature_store is mocks["feature_store"]
                assert app.state.market_data_engine is mocks["md_engine"]
                assert app.state.repositories is mocks["repos"]

            # --------- Shutdown-Assertions ---------
            mocks["strategy_engine"].stop.assert_awaited_once()
            mocks["md_engine"].stop.assert_awaited_once()
            mocks["pool"].close_all.assert_awaited_once()
            mocks["feature_store"].close.assert_awaited_once()
            mocks["bus"].close.assert_awaited_once()
        finally:
            for p in patchers:
                p.stop()

    async def test_lifecycle_without_adapters_skips_market_data_start(self) -> None:
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=False)

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                mocks["md_engine"].subscribe.assert_not_called()
                mocks["md_engine"].start.assert_not_awaited()

            # is_running=True mock default -> stop() still called in shutdown
            # regardless of whether start() ran, matching the `if
            # md_engine.is_running` shutdown guard.
            mocks["md_engine"].stop.assert_awaited_once()
        finally:
            for p in patchers:
                p.stop()

    async def test_exchange_init_failure_is_swallowed(self) -> None:
        """Ein Fehler bei pool.initialize() darf den Start nicht verhindern
        (fail-safe für Exchange-Verbindungsprobleme)."""
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=True)
        mocks["pool"].initialize = AsyncMock(side_effect=RuntimeError("exchange down"))

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                assert app.state.exchange_pool is mocks["pool"]
        finally:
            for p in patchers:
                p.stop()

    async def test_observability_setup_failure_is_swallowed(self) -> None:
        """Observability-Fehler duerfen den Start nicht verhindern
        (fail-safe, siehe Docstring in lifespan())."""
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=True)

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            with patch(
                "sgr.monitoring.observability.setup_observability",
                side_effect=RuntimeError("otel broken"),
            ):
                async with lifespan(app):
                    assert app.state.risk_engine is mocks["risk_engine"]
        finally:
            for p in patchers:
                p.stop()


class TestLifespanLiveModeWithoutAdapters:
    async def test_live_mode_without_credentials_skips_exchange_init(self) -> None:
        """Ohne Live-Credentials und außerhalb PAPER wird pool.initialize()
        gar nicht erst aufgerufen (Bedingung in lifespan())."""
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=False, has_adapters=False)
        mocks["config"].credentials.pionex_live_api_key = None
        mocks["config"].credentials.pionex_live_secret = None

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                mocks["pool"].initialize.assert_not_awaited()
        finally:
            for p in patchers:
                p.stop()

    async def test_live_mode_with_credentials_initializes_exchange(self) -> None:
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=False, has_adapters=True)

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                mocks["pool"].initialize.assert_awaited_once()
        finally:
            for p in patchers:
                p.stop()


class TestLifespanPrimaryExchangeConfigurable:
    """
    Deckt config.primary_exchange als Umschalter zwischen Pionex (kein
    Testnet, Paper braucht keine Keys) und anderen ccxt-unterstuetzten
    Exchanges wie Binance (echtes Testnet, Paper UND Live brauchen
    konfigurierte Keys ueber get_credentials()) ab. Hintergrund: Pionex
    wird von der oeffentlichen ccxt-Bibliothek aktuell nicht unterstuetzt
    (verifiziert ueber ccxt 3.1.60 bis 4.5.77 hinweg), primary_exchange
    erlaubt daher ein Umschalten ohne Code-Aenderung.
    """

    async def test_binance_paper_mode_with_configured_credentials_initializes(self) -> None:
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=True, primary_exchange=ExchangeID.BINANCE
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                mocks["config"].credentials.get_credentials.assert_called_once_with(
                    "binance", TradingMode.PAPER
                )
                mocks["pool"].initialize.assert_awaited_once_with(
                    [ExchangeID.BINANCE], TradingMode.PAPER
                )
        finally:
            for p in patchers:
                p.stop()

    async def test_binance_without_configured_credentials_skips_init_and_does_not_raise(
        self,
    ) -> None:
        """get_credentials() wirft ValueError, wenn BINANCE_PAPER_API_KEY/
        SECRET nicht gesetzt sind. lifespan() darf dabei nicht crashen -
        Verhalten muss identisch zum bestehenden 'Pionex ohne Keys'-Fall
        sein (fail-safe, nur geloggt)."""
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=False, primary_exchange=ExchangeID.BINANCE
        )
        mocks["config"].credentials.get_credentials = MagicMock(
            side_effect=ValueError(
                "Credentials not configured for binance in paper mode. "
                "Set BINANCE_PAPER_API_KEY and BINANCE_PAPER_SECRET env vars."
            )
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                mocks["pool"].initialize.assert_not_awaited()
        finally:
            for p in patchers:
                p.stop()

    async def test_pionex_still_uses_legacy_no_testnet_path(self) -> None:
        """Regressionstest: primary_exchange=PIONEX (Default) darf weiterhin
        NICHT ueber get_credentials() laufen, sondern ueber den bestehenden
        Pionex-Sonderpfad (kein Testnet, Paper Mode braucht keine Keys)."""
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=True, primary_exchange=ExchangeID.PIONEX
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                mocks["config"].credentials.get_credentials.assert_not_called()
                mocks["pool"].initialize.assert_awaited_once_with(
                    [ExchangeID.PIONEX], TradingMode.PAPER
                )
        finally:
            for p in patchers:
                p.stop()

    async def test_market_data_subscriptions_use_configured_primary_exchange(self) -> None:
        """Standard-Subscriptions (BTC/USDT, ETH/USDT) muessen auf die
        konfigurierte primary_exchange zeigen, nicht hartcodiert auf
        Pionex."""
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=True, primary_exchange=ExchangeID.BINANCE
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                calls = mocks["md_engine"].subscribe.call_args_list
                assert len(calls) == 2
                for call in calls:
                    assert call.args[1] == ExchangeID.BINANCE
        finally:
            for p in patchers:
                p.stop()


class TestLifespanRoleApi:
    """
    role="api" (Commit 4): startet nur die Infrastruktur, die die
    Read-Only-API-Router brauchen (DB, Redis, Event Bus, Feature Store).
    Kein Exchange Pool, keine Trading Engines, kein Orchestrator, kein
    Market Data Polling - die entsprechenden app.state.*-Attribute
    bleiben auf ihrem AppState-Klassendefault None (siehe
    sgr/api/dependencies.py Modul-Docstring).
    """

    async def test_only_infrastructure_started_no_trading_engines(self) -> None:
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=True)

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app, role="api"):
                # --------- Infrastruktur MUSS starten ---------
                mocks["bus"].connect.assert_awaited_once()
                mocks["feature_store"].connect.assert_awaited_once()
                assert app.state.repositories is mocks["repos"]
                assert app.state.feature_store is mocks["feature_store"]

                # --------- Trading Lifecycle DARF NICHT starten ---------
                mocks["pool"].initialize.assert_not_awaited()
                mocks["risk_engine"].initialize.assert_not_awaited()
                mocks["strategy_engine"].start.assert_not_awaited()
                mocks["recovery_manager"].recover_after_crash.assert_not_awaited()
                mocks["md_engine"].start.assert_not_awaited()

                # app.state Engine-Attribute bleiben auf AppState-Default None
                assert app.state.exchange_pool is None
                assert app.state.risk_engine is None
                assert app.state.portfolio_engine is None
                assert app.state.strategy_engine is None
                assert app.state.execution_engine is None
                assert app.state.orchestrator is None
                assert app.state.reconciliation_engine is None
                assert app.state.market_data_engine is None
        finally:
            for p in patchers:
                p.stop()

    async def test_shutdown_only_closes_infrastructure(self) -> None:
        """Shutdown darf pool.close_all()/strategy_engine.stop()/
        execution_engine.shutdown() nicht aufrufen - diese Objekte
        existieren im role="api"-Pfad gar nicht (blieben lokale
        None-Variablen, siehe lifespan())."""
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=True)

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app, role="api"):
                pass

            # Infrastruktur wird trotzdem sauber heruntergefahren.
            mocks["feature_store"].close.assert_awaited_once()
            mocks["bus"].close.assert_awaited_once()

            # Trading-Engine-Shutdown-Pfade werden NICHT aufgerufen.
            mocks["strategy_engine"].stop.assert_not_awaited()
            mocks["md_engine"].stop.assert_not_awaited()
            mocks["execution_engine"].shutdown.assert_not_awaited()
            mocks["pool"].close_all.assert_not_awaited()
        finally:
            for p in patchers:
                p.stop()

    async def test_default_role_is_worker_for_backwards_compatibility(self) -> None:
        """lifespan(app) ohne role-Argument muss weiterhin den vollen
        Trading Lifecycle starten (Default role="worker") - das
        bestehende Verhalten vor der role-Aufteilung, auf das sich
        TestLifespanStartupShutdownPaperMode weiterhin verlaesst."""
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=True)

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):  # kein role= Argument
                mocks["pool"].initialize.assert_awaited_once()
                mocks["risk_engine"].initialize.assert_awaited_once()
                assert app.state.exchange_pool is mocks["pool"]
        finally:
            for p in patchers:
                p.stop()


class TestCreateAppUsesApiRole:
    """create_app() (sgr-api) muss role="api" fest verdrahten, ueber den
    _api_lifespan()-Wrapper - sgr-worker/main.py ruft lifespan() dagegen
    direkt mit role="worker" auf (siehe sgr/worker/main.py)."""

    async def test_api_lifespan_wrapper_forces_role_api(self) -> None:
        patchers, mocks = _patch_lifespan_dependencies(paper_mode=True, has_adapters=True)

        from sgr.api.main import _api_lifespan

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with _api_lifespan(app):
                # role="api"-Verhalten: kein Exchange-Pool-Init.
                mocks["pool"].initialize.assert_not_awaited()
                assert app.state.exchange_pool is None
        finally:
            for p in patchers:
                p.stop()


class TestLifespanTenantId:
    """
    Commit 5 (Option A): config.tenant_id gesetzt -> lifespan() Schritt 5
    laedt Exchange-Credentials aus der DB (load_tenant_credentials())
    statt aus config.credentials (.env). Der bestehende Pionex/Binance-
    .env-Verzweigungscode (siehe TestLifespanStartupShutdownPaperMode)
    bleibt fuer tenant_id=None unangetastet und wird hier nicht erneut
    getestet.
    """

    async def test_tenant_id_none_does_not_call_load_tenant_credentials(self) -> None:
        """Regressionsschutz: der bestehende Single-Tenant-Pfad darf durch
        die neue Verzweigung nicht beeinflusst werden."""
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=True, tenant_id=None
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            with patch(
                "sgr.core.tenant_credentials.load_tenant_credentials",
                new=AsyncMock(),
            ) as mock_load:
                async with lifespan(app):
                    mock_load.assert_not_awaited()
                    mocks["pool"].initialize.assert_awaited_once_with(
                        [mocks["config"].primary_exchange], TradingMode.PAPER
                    )
        finally:
            for p in patchers:
                p.stop()

    async def test_tenant_id_set_loads_credentials_from_db_and_initializes_pool(self) -> None:
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=True, tenant_id="gordon"
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        tenant_creds = {"apiKey": "gordon-key", "secret": "gordon-secret"}

        for p in patchers:
            p.start()
        try:
            with patch(
                "sgr.core.tenant_credentials.load_tenant_credentials",
                new=AsyncMock(return_value=tenant_creds),
            ) as mock_load:
                async with lifespan(app):
                    mock_load.assert_awaited_once_with(
                        "gordon", ExchangeID.PIONEX, TradingMode.PAPER
                    )
                    mocks["pool"].initialize.assert_awaited_once_with(
                        [ExchangeID.PIONEX],
                        TradingMode.PAPER,
                        credentials=tenant_creds,
                    )
                    # .env-Pfad (config.credentials.get_credentials) darf im
                    # Multi-Tenant-Zweig nicht aufgerufen werden.
                    mocks["config"].credentials.get_credentials.assert_not_called()
        finally:
            for p in patchers:
                p.stop()

    async def test_tenant_id_set_but_no_db_credentials_logs_warning_and_continues(self) -> None:
        """Analog zum bestehenden .env-ValueError-Pfad: ein Tenant ohne
        konfigurierte DB-Keys darf den Worker-Start nicht blockieren -
        nur der Exchange Pool bleibt fuer diesen Prozess leer."""
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=False, tenant_id="sumo"
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            with patch(
                "sgr.core.tenant_credentials.load_tenant_credentials",
                new=AsyncMock(side_effect=ValueError("No API keys configured")),
            ):
                # Darf nicht raisen - lifespan() muss trotzdem sauber
                # durchlaufen (fail-safe, nicht fail-fast, siehe
                # bestehendes Verhalten fuer den .env-Pfad).
                async with lifespan(app):
                    mocks["pool"].initialize.assert_not_awaited()
        finally:
            for p in patchers:
                p.stop()

    async def test_candle_event_consumer_group_scoped_by_tenant_id(self) -> None:
        """Audit-Fund (nach Commit 5): Gordon und Sumo duerfen sich
        niemals dieselbe Redis-Consumer-Group teilen, sonst verteilt
        Redis Streams CandleEvents per Round-Robin auf beide, statt dass
        jeder JEDES Event erhaelt."""
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=True, tenant_id="a47d994d-gordon"
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            with patch(
                "sgr.core.tenant_credentials.load_tenant_credentials",
                new=AsyncMock(return_value={"apiKey": "k", "secret": "s"}),
            ):
                async with lifespan(app):
                    mocks["bus"].subscribe.assert_called_once()
                    call = mocks["bus"].subscribe.call_args
                    assert call.kwargs["consumer_group"] == "orchestrator:a47d994d-gordon"
                    assert call.kwargs["consumer_name"] == "orchestrator-a47d994d-gordon-1"
        finally:
            for p in patchers:
                p.stop()

    async def test_candle_event_consumer_group_defaults_for_single_tenant(self) -> None:
        """tenant_id=None (Single-Tenant-Deployment ohne TENANT_ID) nutzt
        weiterhin eine feste, vorhersagbare Consumer-Group statt des
        vorherigen "orchestrator" (Breaking Change gegenueber dem
        Vor-Audit-Zustand ist hier bewusst in Kauf genommen - ein
        stehender Redis-Consumer-Group-Name ohne Tenant-Bezug waere
        inkonsistent mit dem neuen Muster, siehe Commit-Beschreibung)."""
        patchers, mocks = _patch_lifespan_dependencies(
            paper_mode=True, has_adapters=True, tenant_id=None
        )

        app = FastAPI()
        app.state = AppState()  # type: ignore[assignment]

        for p in patchers:
            p.start()
        try:
            async with lifespan(app):
                call = mocks["bus"].subscribe.call_args
                assert call.kwargs["consumer_group"] == "orchestrator:default"
                assert call.kwargs["consumer_name"] == "orchestrator-default-1"
        finally:
            for p in patchers:
                p.stop()
