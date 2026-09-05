"""
Tests für /health/live, /health/ready, /health/trading (3-Tier Model).

Migrationsstand (sgr-api Read-Only-Zielarchitektur, Commit 3):
    Alle Checks laufen jetzt über echte Konnektivitäts-Prüfungen
    (gemockte DB-Session, gemockter Redis-Client über
    app.state.feature_store.redis_client) und Redis-gelesene Zustände
    (Kill Switch über read_kill_switch_state_from_redis), NICHT mehr
    über die Anwesenheit von In-Memory-Engines in app.state
    (exchange_pool, risk_engine, execution_engine, market_data_engine).

Testziele:
1. /health/live = pure liveness (200 always)
2. /health/ready = 200 if DB+Redis ready, else 503
3. /health/trading = 200 nur wenn Kill Switch nachweislich inaktiv ist
   UND die (aktuell noch "unknown") Worker-Signale bekannt sind -
   siehe sgr/api/routers/health.py Modul-Docstring: diese drei Signale
   sind bis zu einem Folge-Commit durchgehend "unknown", daher liefert
   /health/trading in diesem Zwischenstand IMMER 503/trading_enabled=False.
4. Backward compatibility: /health endpoint works
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from sgr.api.main import create_app


def _mock_feature_store(redis_client: MagicMock | None) -> MagicMock:
    """FeatureStore-Mock mit redis_client-Property wie im echten Objekt."""
    store = MagicMock()
    store.redis_client = redis_client
    return store


def _mock_redis_ok() -> AsyncMock:
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    return redis


class TestHealthLive:
    """GET /health/live - Pure process liveness."""

    def test_health_live_returns_200_always(self) -> None:
        """Liveness probe never depends on external systems."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/health/live")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "alive"
        assert "timestamp" in body

    def test_health_live_no_dependencies(self) -> None:
        """No external dependencies can be checked in liveness."""
        app = create_app()
        app.state.exchange_pool = None
        app.state.feature_store = None

        client = TestClient(app)
        response = client.get("/health/live")

        assert response.status_code == 200


class TestHealthReady:
    """GET /health/ready - Readiness for traffic."""

    def test_health_ready_returns_200_when_dependencies_ready(self) -> None:
        """Ready probe returns 200 when DB and Redis are connected."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(True, "connected")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["db_connected"] is True
        assert body["redis_connected"] is True
        assert body["components_initialized"] is True

    def test_health_ready_returns_503_when_db_missing(self) -> None:
        """Ready probe returns 503 when database is not connected."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(False, "error: connection refused")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["db_connected"] is False
        assert body["redis_connected"] is True
        assert body["components_initialized"] is False

    def test_health_ready_returns_503_when_redis_missing(self) -> None:
        """Ready probe returns 503 when Redis is not connected."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(None)

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(True, "connected")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["db_connected"] is True
        assert body["redis_connected"] is False
        assert body["components_initialized"] is False

    def test_health_ready_returns_503_when_both_missing(self) -> None:
        """Ready probe returns 503 when both DB and Redis are down."""
        app = create_app()
        app.state.feature_store = None

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(False, "error: connection refused")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["components_initialized"] is False


class TestHealthTrading:
    """
    GET /health/trading - Trading pipeline operational.

    Wichtig: exchange_connected/preflight_available/risk_engine_available
    sind im aktuellen Zwischenstand (Commit 3) durchgehend "unknown" -
    siehe Modul-Docstring von sgr/api/routers/health.py. Der Endpoint
    liefert daher fail-safe IMMER trading_enabled=False/503, bis ein
    Folge-Commit diese drei Signale verlässlich aus Redis liefert. Das
    ist kein Test-Bug, sondern die dokumentierte, bewusste Konsequenz
    der Read-Only-Migration.
    """

    def test_health_trading_reports_unknown_worker_signals(self) -> None:
        """Solange kein Folge-Commit die Worker-Signale nach Redis
        published, meldet der Endpoint sie fail-safe als 'unknown' und
        bleibt konservativ bei trading_enabled=False."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with patch(
            "sgr.api.routers.health.read_kill_switch_state_from_redis",
            AsyncMock(return_value={"is_active": False, "reason": None}),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["exchange_connected"] == "unknown"
        assert body["risk_engine_available"] == "unknown"
        assert body["preflight_available"] == "unknown"
        assert body["kill_switch_active"] is False

    def test_health_trading_returns_503_when_kill_switch_active(self) -> None:
        """Trading probe returns 503 when kill switch is active."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with patch(
            "sgr.api.routers.health.read_kill_switch_state_from_redis",
            AsyncMock(return_value={"is_active": True, "reason": "manual"}),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["trading_enabled"] is False
        assert body["kill_switch_active"] is True

    def test_health_trading_kill_switch_unknown_when_redis_unavailable(self) -> None:
        """Kein Redis -> Kill-Switch-Status ist 'unknown', nicht 'inaktiv'
        (Fail-Safe-Prinzip: unbekannt darf niemals als sicher gelten)."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(None)

        client = TestClient(app)
        response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["kill_switch_active"] is None
        assert body["details"]["kill_switch_active"] == "unknown"

    def test_health_trading_kill_switch_unknown_when_never_written(self) -> None:
        """Redis erreichbar, aber noch nie ein State geschrieben (frisches
        Deployment) -> ebenfalls 'unknown', kein falsches 'inaktiv'."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with patch(
            "sgr.api.routers.health.read_kill_switch_state_from_redis",
            AsyncMock(return_value=None),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["kill_switch_active"] is None
        assert body["trading_enabled"] is False


class TestHealthBackwardCompatibility:
    """GET /health - Backward compatible alias."""

    def test_health_endpoint_accessible(self) -> None:
        """Default /health endpoint still works, now backed by real
        DB/Redis connectivity checks instead of app.state engine presence."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(True, "connected")),
        ):
            client = TestClient(app)
            response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "components" in body
        assert body["components"]["database"] == "ok"
        assert body["components"]["redis"] == "ok"

    def test_health_endpoint_degraded_when_db_down(self) -> None:
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(False, "error: down")),
        ):
            client = TestClient(app)
            response = client.get("/health")

        body = response.json()
        assert body["status"] == "degraded"
        assert body["components"]["database"] == "degraded"

    def test_ping_endpoint_works(self) -> None:
        """Simple ping endpoint for connectivity checks."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/ping")

        assert response.status_code == 200
        body = response.json()
        assert "pong" in body
