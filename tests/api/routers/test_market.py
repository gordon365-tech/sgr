"""
Tests für sgr.api.routers.market.get_ticker.

Der Endpoint liest ausschließlich aus dem Redis-Ticker-Cache (siehe
sgr/market_data/ticker_cache.py) - kein Live-Exchange-Call aus dem
API-Prozess. Getestet auf einer minimalen FastAPI-App via TestClient
(identisches Muster wie test_risk.py), require_auth und get_redis_client
werden per app.dependency_overrides ersetzt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sgr.api.dependencies import TokenData, get_redis_client, require_auth
from sgr.api.routers.market import router as market_router
from sgr.core.types import TradingMode


@pytest.fixture
def token_data() -> TokenData:
    return TokenData(user_id="user-1", trading_mode=TradingMode.PAPER, is_admin=False)


@pytest.fixture
def app(token_data: TokenData) -> FastAPI:
    app = FastAPI()
    app.include_router(market_router, prefix="/api/v1/market")
    app.dependency_overrides[require_auth] = lambda: token_data
    app.dependency_overrides[get_redis_client] = lambda: object()
    return app


class TestGetTicker:
    def test_ticker_found_returns_cached_payload(self, app: FastAPI) -> None:
        cached_ticker = {
            "symbol": "BTC/USDT",
            "bid": "50000",
            "ask": "50010",
            "last": "50005",
            "volume_24h": "1234.5",
            "change_24h_pct": 1.2,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        client = TestClient(app)

        with patch(
            "sgr.api.routers.market.read_ticker_from_redis",
            new=AsyncMock(return_value=cached_ticker),
        ):
            response = client.get("/api/v1/market/ticker/BTC-USDT")

        assert response.status_code == 200
        assert response.json() == cached_ticker

    def test_symbol_normalized_before_cache_lookup(self, app: FastAPI) -> None:
        """'btc-usdt' muss als 'BTC/USDT' im Cache nachgeschlagen werden -
        identisches Normalisierungsmuster wie /features/{symbol}."""
        client = TestClient(app)
        mock_read = AsyncMock(return_value=None)

        with patch("sgr.api.routers.market.read_ticker_from_redis", new=mock_read):
            client.get("/api/v1/market/ticker/btc-usdt")

        assert mock_read.await_args is not None
        called_symbol = mock_read.await_args.args[1]
        assert called_symbol == "BTC/USDT"

    def test_no_cached_ticker_returns_404(self, app: FastAPI) -> None:
        client = TestClient(app)

        with patch(
            "sgr.api.routers.market.read_ticker_from_redis",
            new=AsyncMock(return_value=None),
        ):
            response = client.get("/api/v1/market/ticker/ETH-USDT")

        assert response.status_code == 404

    def test_no_redis_connection_returns_503(self, token_data: TokenData) -> None:
        """get_redis_client wirft 503, wenn keine Verbindung besteht -
        hier NICHT ueberschrieben, um das reale Dependency-Verhalten zu
        pruefen (siehe sgr/api/dependencies.py get_redis_client)."""
        app = FastAPI()
        app.include_router(market_router, prefix="/api/v1/market")
        app.dependency_overrides[require_auth] = lambda: token_data

        client = TestClient(app)
        response = client.get("/api/v1/market/ticker/BTC-USDT")

        assert response.status_code == 503
