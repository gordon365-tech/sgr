"""
Tests für /health/live, /health/ready, /health/trading (3-Tier Model).

Testziele:
1. /health/live = pure liveness (200 always)
2. /health/ready = 200 if DB+Redis ready, else 503
3. /health/trading = 200 if trading operational, else 503
4. Backward compatibility: /health endpoint works
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from sgr.api.main import create_app


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
        # Simulate broken state
        app.state.exchange_pool = None
        app.state.feature_store = None

        client = TestClient(app)
        response = client.get("/health/live")

        # Still 200
        assert response.status_code == 200


class TestHealthReady:
    """GET /health/ready - Readiness for traffic."""

    def test_health_ready_returns_200_when_dependencies_ready(self) -> None:
        """Ready probe returns 200 when DB and Redis are connected."""
        app = create_app()

        # Mock healthy dependencies
        mock_pool = MagicMock()
        mock_pool.__len__ = MagicMock(return_value=1)
        app.state.exchange_pool = mock_pool

        mock_store = MagicMock()
        mock_store._redis = MagicMock()
        app.state.feature_store = mock_store

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

        # DB missing
        app.state.exchange_pool = None

        # Redis ok
        mock_store = MagicMock()
        mock_store._redis = MagicMock()
        app.state.feature_store = mock_store

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

        # DB ok
        mock_pool = MagicMock()
        mock_pool.__len__ = MagicMock(return_value=1)
        app.state.exchange_pool = mock_pool

        # Redis missing
        app.state.feature_store = None

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

        app.state.exchange_pool = None
        app.state.feature_store = None

        client = TestClient(app)
        response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["components_initialized"] is False


class TestHealthTrading:
    """GET /health/trading - Trading pipeline operational."""

    def test_health_trading_returns_200_when_trading_operational(self) -> None:
        """Trading probe returns 200 when all trading systems operational."""
        app = create_app()

        # Mock all trading components
        mock_pool = MagicMock()
        mock_pool.__len__ = MagicMock(return_value=1)
        app.state.exchange_pool = mock_pool

        mock_risk = MagicMock()
        mock_risk._initialized = True
        app.state.risk_engine = mock_risk

        mock_exec = MagicMock()
        mock_exec._preflight = MagicMock()
        app.state.execution_engine = mock_exec

        # Mock kill switch as inactive
        with patch("sgr.api.routers.health.get_kill_switch") as mock_ks:
            mock_kill_switch = MagicMock()
            mock_kill_switch.is_active = False
            mock_ks.return_value = mock_kill_switch

            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["trading_enabled"] is True
        assert body["kill_switch_active"] is False
        assert body["exchange_connected"] is True
        assert body["risk_engine_available"] is True
        assert body["preflight_available"] is True

    def test_health_trading_returns_503_when_kill_switch_active(self) -> None:
        """Trading probe returns 503 when kill switch is active."""
        app = create_app()

        # Setup operational components
        mock_pool = MagicMock()
        mock_pool.__len__ = MagicMock(return_value=1)
        app.state.exchange_pool = mock_pool

        mock_risk = MagicMock()
        mock_risk._initialized = True
        app.state.risk_engine = mock_risk

        mock_exec = MagicMock()
        mock_exec._preflight = MagicMock()
        app.state.execution_engine = mock_exec

        # Kill switch ACTIVE
        with patch("sgr.api.routers.health.get_kill_switch") as mock_ks:
            mock_kill_switch = MagicMock()
            mock_kill_switch.is_active = True
            mock_ks.return_value = mock_kill_switch

            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["trading_enabled"] is False
        assert body["kill_switch_active"] is True

    def test_health_trading_returns_503_when_exchange_disconnected(self) -> None:
        """Trading probe returns 503 when exchange is not connected."""
        app = create_app()

        # Exchange disconnected
        app.state.exchange_pool = None

        mock_risk = MagicMock()
        mock_risk._initialized = True
        app.state.risk_engine = mock_risk

        mock_exec = MagicMock()
        mock_exec._preflight = MagicMock()
        app.state.execution_engine = mock_exec

        with patch("sgr.api.routers.health.get_kill_switch") as mock_ks:
            mock_kill_switch = MagicMock()
            mock_kill_switch.is_active = False
            mock_ks.return_value = mock_kill_switch

            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["exchange_connected"] is False

    def test_health_trading_returns_503_when_risk_engine_unavailable(self) -> None:
        """Trading probe returns 503 when risk engine is not initialized."""
        app = create_app()

        mock_pool = MagicMock()
        mock_pool.__len__ = MagicMock(return_value=1)
        app.state.exchange_pool = mock_pool

        # Risk engine NOT initialized
        app.state.risk_engine = None

        mock_exec = MagicMock()
        mock_exec._preflight = MagicMock()
        app.state.execution_engine = mock_exec

        with patch("sgr.api.routers.health.get_kill_switch") as mock_ks:
            mock_kill_switch = MagicMock()
            mock_kill_switch.is_active = False
            mock_ks.return_value = mock_kill_switch

            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["risk_engine_available"] is False

    def test_health_trading_returns_503_when_preflight_unavailable(self) -> None:
        """Trading probe returns 503 when preflight is not available."""
        app = create_app()

        mock_pool = MagicMock()
        mock_pool.__len__ = MagicMock(return_value=1)
        app.state.exchange_pool = mock_pool

        mock_risk = MagicMock()
        mock_risk._initialized = True
        app.state.risk_engine = mock_risk

        # Preflight NOT available
        mock_exec = MagicMock()
        mock_exec._preflight = None
        app.state.execution_engine = mock_exec

        with patch("sgr.api.routers.health.get_kill_switch") as mock_ks:
            mock_kill_switch = MagicMock()
            mock_kill_switch.is_active = False
            mock_ks.return_value = mock_kill_switch

            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["preflight_available"] is False


class TestHealthBackwardCompatibility:
    """GET /health - Backward compatible alias."""

    def test_health_endpoint_accessible(self) -> None:
        """Default /health endpoint still works."""
        app = create_app()

        # Setup mock components
        mock_pool = MagicMock()
        mock_pool.__len__ = MagicMock(return_value=1)
        app.state.exchange_pool = mock_pool

        mock_store = MagicMock()
        mock_store._redis = MagicMock()
        app.state.feature_store = mock_store

        mock_risk = MagicMock()
        mock_risk._initialized = True
        app.state.risk_engine = mock_risk

        mock_md = MagicMock()
        mock_md.is_running = True
        app.state.market_data_engine = mock_md

        client = TestClient(app)
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "components" in body
        assert body["components"]["exchange_pool"] == "ok"
        assert body["components"]["feature_store"] == "ok"
        assert body["components"]["risk_engine"] == "ok"
        assert body["components"]["market_data"] == "ok"

    def test_ping_endpoint_works(self) -> None:
        """Simple ping endpoint for connectivity checks."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/ping")

        assert response.status_code == 200
        body = response.json()
        assert "pong" in body
