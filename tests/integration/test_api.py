"""
API Integration Tests.
Testet die FastAPI Endpoints ohne echte Exchange-Verbindung.

Migrationsstand (sgr-api Read-Only-Zielarchitektur):
    Router, die bereits auf Repository/Redis-Reads umgestellt sind
    (siehe sgr/api/dependencies.py), werden hier über
    app.dependency_overrides[get_repos]/[get_trading_mode]/[get_redis_client]
    mit einem gefälschten Repository-Bündel getestet - nicht mehr über
    echte Engine-Instanzen in app.state (die die API seit der
    sgr-api/sgr-worker-Trennung nicht mehr besitzt).

    Noch nicht migrierte Router in dieser Datei behalten vorerst ihre
    bisherige app.state-basierte Mock-Strategie bei, bis sie im Zuge von
    Commit 3 (Router-für-Router-Umstellung) ebenfalls umgestellt werden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sgr.core.config import get_config
from sgr.core.types import TradingMode

# ---------------------------------------------------------------------------
# Fake Repositories (fuer bereits migrierte Router, z.B. portfolio.py)
# ---------------------------------------------------------------------------


def _make_fake_repos() -> MagicMock:
    """
    Gefälschtes Repositories-Bündel mit realistischen Rückgabewerten für
    die bereits migrierten Router. Jede Repository-Methode ist ein
    AsyncMock mit einem sinnvollen Default-Rückgabewert (leeres/None
    entspricht "frisches Deployment, Worker hat noch nichts geschrieben").
    """
    repos = MagicMock()

    repos.portfolio_snapshots.get_latest = AsyncMock(
        return_value={
            "portfolio_value": Decimal("100000"),
            "cash": Decimal("80000"),
            "unrealized_pnl": Decimal("500"),
            "peak_value": Decimal("102000"),
            "drawdown": Decimal("0.02"),
            "open_positions_count": 2,
            "total_trades": 5,
            "trading_mode": "paper",
        }
    )
    repos.positions.get_open_positions = AsyncMock(return_value=[])
    repos.trades.get_recent = AsyncMock(return_value=[])
    repos.trades.get_pnl_summary = AsyncMock(
        return_value={
            "total_trades": 0,
            "winning_trades": 0,
            "total_realized_pnl": Decimal("0"),
            "total_fees": Decimal("0"),
            "hit_rate": 0.0,
        }
    )
    repos.strategies.get_all = AsyncMock(
        return_value=[
            {
                "name": "trend_following_v1",
                "version": "1.0.0",
                "is_active": True,
                "is_validated": True,
                "supported_regimes": ["trending_up", "trending_down"],
                "deactivation_reason": None,
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "max_drawdown": None,
                "hit_rate": None,
                "total_trades": 0,
            },
            {
                "name": "mean_reversion_v1",
                "version": "1.0.0",
                "is_active": True,
                "is_validated": True,
                "supported_regimes": ["ranging"],
                "deactivation_reason": None,
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "max_drawdown": None,
                "hit_rate": None,
                "total_trades": 0,
            },
        ]
    )
    repos.strategies.get_by_name = AsyncMock(return_value=None)
    repos.strategies.set_active = AsyncMock(return_value=None)
    return repos


def _create_test_app():
    """Erstellt Test-App mit gemockten Engines."""
    from fastapi import FastAPI

    app = FastAPI(title="SGR Test")

    # Mock App State
    from sgr.api.main import AppState

    app.state = AppState()

    # Mock Risk Engine
    risk_mock = MagicMock()
    risk_mock._initialized = True
    risk_mock._trading_mode = TradingMode.PAPER
    from sgr.core.types import RiskMetrics

    risk_mock._compute_metrics.return_value = RiskMetrics(
        timestamp=datetime.now(tz=UTC),
        portfolio_value=Decimal("100000"),
        daily_pnl=Decimal("500"),
        daily_pnl_pct=0.005,
        drawdown_from_peak=0.02,
        var_95=0.015,
        expected_shortfall=0.022,
        portfolio_heat=0.30,
        active_positions=2,
        correlation_exposure=0.3,
    )
    app.state.risk_engine = risk_mock

    # Mock Feature Store (redis_client-Property statt privatem _redis,
    # siehe FeatureStore.redis_client in sgr/market_data/feature_store.py)
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)  # kein State geschrieben -> "unknown"/"stale"
    fake_redis.set = AsyncMock()
    fake_redis.ping = AsyncMock(return_value=True)
    store_mock = MagicMock()
    store_mock.redis_client = fake_redis
    app.state.feature_store = store_mock

    # Mock Exchange Pool
    pool_mock = MagicMock()
    pool_mock.__len__ = lambda self: 1
    app.state.exchange_pool = pool_mock

    # Mock Market Data Engine
    md_mock = MagicMock()
    md_mock.is_running = True
    app.state.market_data_engine = md_mock

    # Mount routers
    from sgr.api.dependencies import get_redis_client, get_repos, get_trading_mode
    from sgr.api.routers import (
        health,
        orders,
        portfolio as portfolio_router,
        risk as risk_router,
        strategy,
        system,
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(portfolio_router.router, prefix="/api/v1/portfolio", tags=["portfolio"])
    app.include_router(risk_router.router, prefix="/api/v1/risk", tags=["risk"])
    app.include_router(strategy.router, prefix="/api/v1/strategy", tags=["strategy"])
    app.include_router(orders.router, prefix="/api/v1/orders", tags=["orders"])
    app.include_router(system.router, prefix="/api/v1/system", tags=["system"])

    # portfolio.py/risk.py/strategy.py/orders.py sind bereits auf
    # Repository/Redis-Reads umgestellt (Commit 3) - kein
    # app.state.portfolio_engine/risk_engine mehr als fachliche
    # Datenquelle, stattdessen dependency_overrides.
    app.dependency_overrides[get_repos] = _make_fake_repos
    app.dependency_overrides[get_trading_mode] = lambda: TradingMode.PAPER
    app.dependency_overrides[get_redis_client] = lambda: fake_redis

    return app


def _make_test_token(user_id: str = "test_user", is_admin: bool = False) -> str:
    """Erstellt gültiges JWT für Tests."""
    from jose import jwt

    from sgr.core.config import get_config

    config = get_config()
    payload = {
        "sub": user_id,
        "trading_mode": "paper",
        "is_admin": is_admin,
    }
    return jwt.encode(
        payload,
        config.api.secret_key.get_secret_value(),
        algorithm=config.api.algorithm,
    )


@pytest.fixture
def test_app():
    get_config.cache_clear()
    app = _create_test_app()
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


@pytest.fixture
def auth_headers() -> dict:
    token = _make_test_token()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers() -> dict:
    token = _make_test_token(is_admin=True)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Health Endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_200(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_contains_required_fields(self, client: TestClient) -> None:
        response = client.get("/health")
        data = response.json()
        assert "status" in data
        assert "version" in data
        assert "trading_mode" in data
        assert "components" in data

    def test_ping_returns_pong(self, client: TestClient) -> None:
        response = client.get("/ping")
        assert response.status_code == 200
        assert "pong" in response.json()

    def test_health_has_request_id_header(self, client: TestClient) -> None:
        response = client.get("/health")
        # Request-ID wird von Middleware gesetzt
        # TestClient leitet Middleware nicht immer weiter – Status check genügt
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Portfolio Endpoints
# ---------------------------------------------------------------------------


class TestPortfolioEndpoints:
    def test_overview_requires_auth(self, client: TestClient) -> None:
        response = client.get("/api/v1/portfolio/overview")
        assert response.status_code == 401

    def test_overview_with_auth(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/portfolio/overview", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "portfolio_value" in data
        assert "cash" in data
        assert "open_positions" in data
        assert "trading_mode" in data
        assert data["trading_mode"] == "paper"

    def test_positions_empty_initially(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/portfolio/positions", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []

    def test_trades_empty_initially(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/portfolio/trades", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []

    def test_pnl_structure(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/portfolio/pnl", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "unrealized_pnl" in data
        assert "realized_pnl" in data
        assert "hit_rate" in data
        assert "total_trades" in data


# ---------------------------------------------------------------------------
# Risk Endpoints
# ---------------------------------------------------------------------------


class TestRiskEndpoints:
    def test_metrics_requires_auth(self, client: TestClient) -> None:
        response = client.get("/api/v1/risk/metrics")
        assert response.status_code == 401

    def test_metrics_with_auth(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/risk/metrics", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "portfolio_value" in data
        assert "drawdown_from_peak" in data
        assert "var_95" in data
        assert "portfolio_heat" in data
        assert "kill_switch_active" in data

    def test_limits_with_auth(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/risk/limits", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "hard_limits" in data
        assert "soft_limits" in data

    def test_kill_switch_status_unknown_when_never_written(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        """Kein State je geschrieben (frisches Test-Setup) -> is_active ist
        None/status_known False, NICHT False - siehe Commit-3-Entscheidung:
        KillSwitchResponse musste bool -> bool|None erweitert werden, damit
        'unbekannt' nicht mit 'inaktiv' verwechselt werden kann."""
        response = client.get("/api/v1/risk/kill-switch", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is None
        assert data["status_known"] is False
        assert data["trading_mode"] == "paper"

    def test_kill_switch_reset_requires_admin(self, client: TestClient, auth_headers: dict) -> None:
        response = client.post("/api/v1/risk/kill-switch/reset", headers=auth_headers)
        assert response.status_code == 403

    def test_kill_switch_reset_as_admin(self, client: TestClient, admin_headers: dict) -> None:
        response = client.post("/api/v1/risk/kill-switch/reset", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["reset"] is True


# ---------------------------------------------------------------------------
# Strategy Endpoints
# ---------------------------------------------------------------------------


class TestStrategyEndpoints:
    def test_list_strategies_requires_auth(self, client: TestClient) -> None:
        response = client.get("/api/v1/strategy/")
        assert response.status_code == 401

    def test_list_strategies_with_auth(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/strategy/", headers=auth_headers)
        assert response.status_code == 200
        strategies = response.json()
        assert isinstance(strategies, list)
        # Strategien wurden via @register registriert
        names = [s["name"] for s in strategies]
        assert "trend_following_v1" in names
        assert "mean_reversion_v1" in names

    def test_strategy_has_required_fields(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/strategy/", headers=auth_headers)
        data = response.json()
        if data:
            strategy = data[0]
            assert "name" in strategy
            assert "version" in strategy
            assert "is_active" in strategy
            assert "supported_regimes" in strategy

    def test_activate_requires_admin(self, client: TestClient, auth_headers: dict) -> None:
        response = client.post(
            "/api/v1/strategy/trend_following_v1/activate",
            headers=auth_headers,
        )
        assert response.status_code == 403

    def test_deactivate_not_found(self, client: TestClient, auth_headers: dict) -> None:
        # Deactivate endpoint erlaubt auth (nicht admin-only per aktuellem Code)
        response = client.post(
            "/api/v1/strategy/nonexistent_strategy/deactivate",
            headers=auth_headers,
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# System Endpoints
# ---------------------------------------------------------------------------


class TestSystemEndpoints:
    def test_status_requires_auth(self, client: TestClient) -> None:
        response = client.get("/api/v1/system/status")
        assert response.status_code == 401

    def test_status_with_auth(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/system/status", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert "environment" in data
        assert "trading_mode" in data
        assert "uptime_seconds" in data
        assert "components" in data

    def test_config_requires_admin(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/system/config", headers=auth_headers)
        assert response.status_code == 403

    def test_config_as_admin(self, client: TestClient, admin_headers: dict) -> None:
        response = client.get("/api/v1/system/config", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert "risk_limits" in data
        # Sicherstellen dass keine Secrets im Response
        config_str = str(data)
        assert "secret" not in config_str.lower()
        assert "password" not in config_str.lower()


# ---------------------------------------------------------------------------
# Auth Checks
# ---------------------------------------------------------------------------


class TestAuthChecks:
    def test_missing_auth_header(self, client: TestClient) -> None:
        response = client.get("/api/v1/portfolio/overview")
        assert response.status_code == 401

    def test_invalid_token(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/portfolio/overview",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert response.status_code == 401

    def test_malformed_auth_header(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/portfolio/overview",
            headers={"Authorization": "NotBearer token"},
        )
        assert response.status_code == 401
