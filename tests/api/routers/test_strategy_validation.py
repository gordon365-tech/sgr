"""
Tests fuer sgr.api.routers.strategy_validation - identisches Muster
wie test_market.py (require_auth/get_repos via app.dependency_overrides).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sgr.api.dependencies import TokenData, get_repos, require_auth
from sgr.api.routers.strategy_validation import router as validation_router
from sgr.core.types import TradingMode


@pytest.fixture
def token_data() -> TokenData:
    return TokenData(user_id="user-1", trading_mode=TradingMode.PAPER, is_admin=False)


@pytest.fixture
def fake_repos():
    repos = AsyncMock()
    repos.strategy_symbol_validations.get_status_counts = AsyncMock(
        return_value={"active": 3, "no_valid_strategy": 2, "insufficient_data": 1}
    )
    repos.strategy_symbol_validations.get_strategy_distribution = AsyncMock(
        return_value={"trend_following_v1": 2, "mean_reversion_v1": 1}
    )
    repos.strategy_symbol_validations.get_by_symbol = AsyncMock(
        return_value=[{"symbol": "BTC/USDT", "strategy": "trend_following_v1", "status": "active"}]
    )
    repos.strategy_symbol_validations.get_best_by_symbol = AsyncMock(
        return_value=[
            {"symbol": "BTC/USDT", "status": "active"},
            {"symbol": "XYZ/USDT", "status": "no_valid_strategy"},
            {"symbol": "NEW/USDT", "status": "insufficient_data"},
        ]
    )
    repos.strategy_symbol_validations.get_active = AsyncMock(
        return_value=[{"symbol": "BTC/USDT", "status": "active"}]
    )
    repos.strategy_symbol_validations.get_top_n = AsyncMock(
        return_value=[{"symbol": "BTC/USDT", "score": 80.0}]
    )
    return repos


@pytest.fixture
def app(token_data: TokenData, fake_repos) -> FastAPI:
    app = FastAPI()
    app.include_router(validation_router, prefix="/api/v1/strategy-validation")
    app.dependency_overrides[require_auth] = lambda: token_data
    app.dependency_overrides[get_repos] = lambda: fake_repos
    return app


class TestOverview:
    def test_returns_status_counts_and_success_rate(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/strategy-validation/overview")

        assert response.status_code == 200
        body = response.json()
        assert body["total_symbols"] == 6
        assert body["status_counts"]["active"] == 3
        assert body["validation_success_rate_pct"] == pytest.approx(50.0)


class TestBySymbol:
    def test_returns_rows_for_symbol(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/strategy-validation/by-symbol/BTC/USDT")

        assert response.status_code == 200
        assert response.json()[0]["symbol"] == "BTC/USDT"


class TestFailedAndDataQuality:
    def test_failed_filters_to_no_valid_strategy_only(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/strategy-validation/failed")

        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "XYZ/USDT"

    def test_data_quality_failures_filters_correctly(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/strategy-validation/data-quality-failures")

        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "NEW/USDT"


class TestActiveAndTop:
    def test_active_endpoint(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/strategy-validation/active")
        assert response.status_code == 200
        assert response.json()[0]["symbol"] == "BTC/USDT"

    def test_top_endpoint_default_order_by_score(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/strategy-validation/top")
        assert response.status_code == 200

    def test_top_endpoint_rejects_invalid_order_by(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/strategy-validation/top?order_by=nonsense")
        assert response.status_code == 422
