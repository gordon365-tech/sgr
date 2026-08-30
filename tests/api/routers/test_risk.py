"""
Tests für die Symbol-Kill-Switch-Endpunkte in sgr.api.routers.risk.

Neuer Baustein (Phase 2 - Live Trading Safety). Getestet auf einer
minimalen FastAPI-App via TestClient (kein voller lifespan nötig,
siehe tests/api/test_main.py für die Begründung). require_auth/
require_admin werden per app.dependency_overrides ersetzt.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sgr.api.dependencies import TokenData, require_admin, require_auth
from sgr.api.routers.risk import router as risk_router
from sgr.core.types import TradingMode
from sgr.risk.symbol_kill_switch import SymbolKillSwitch, get_symbol_kill_switch


@pytest.fixture(autouse=True)
def _reset_symbol_kill_switch():
    SymbolKillSwitch._instance = None
    yield
    SymbolKillSwitch._instance = None


@pytest.fixture
def token_data() -> TokenData:
    return TokenData(user_id="user-1", trading_mode=TradingMode.PAPER, is_admin=False)


@pytest.fixture
def app(token_data: TokenData) -> FastAPI:
    app = FastAPI()
    app.include_router(risk_router, prefix="/api/v1/risk")
    app.dependency_overrides[require_auth] = lambda: token_data
    app.dependency_overrides[require_admin] = lambda: token_data
    return app


class TestListSymbolKillSwitches:
    def test_list_empty_initially(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/risk/symbol-kill-switch")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_includes_deactivated_symbols(self, app: FastAPI) -> None:
        import asyncio

        asyncio.run(
            get_symbol_kill_switch().deactivate("pionex:BTC/USDT", "manual stop")
        )

        client = TestClient(app)
        response = client.get("/api/v1/risk/symbol-kill-switch")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["symbol_key"] == "pionex:BTC/USDT"
        assert body[0]["is_active"] is False
        assert body[0]["reason"] == "manual stop"


class TestDeactivateSymbol:
    def test_deactivate_symbol_success(self, app: FastAPI) -> None:
        client = TestClient(app)
        response = client.post(
            "/api/v1/risk/symbol-kill-switch/pionex:BTC%2FUSDT/deactivate",
            params={"reason": "anomalous volume spike"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["deactivated"] == "pionex:BTC/USDT"
        assert body["reason"] == "anomalous volume spike"
        assert get_symbol_kill_switch().is_active("pionex:BTC/USDT") is False

    def test_deactivate_symbol_requires_auth(self) -> None:
        app = FastAPI()
        app.include_router(risk_router, prefix="/api/v1/risk")

        client = TestClient(app)
        response = client.post("/api/v1/risk/symbol-kill-switch/pionex:BTC%2FUSDT/deactivate")

        assert response.status_code == 401


class TestActivateSymbol:
    def test_activate_symbol_success(self, app: FastAPI) -> None:
        import asyncio

        asyncio.run(get_symbol_kill_switch().deactivate("pionex:BTC/USDT", "reason"))

        client = TestClient(app)
        response = client.post("/api/v1/risk/symbol-kill-switch/pionex:BTC%2FUSDT/activate")

        assert response.status_code == 200
        assert response.json()["activated"] == "pionex:BTC/USDT"
        assert get_symbol_kill_switch().is_active("pionex:BTC/USDT") is True

    def test_activate_symbol_requires_admin(self, token_data: TokenData) -> None:
        app = FastAPI()
        app.include_router(risk_router, prefix="/api/v1/risk")
        app.dependency_overrides[require_auth] = lambda: token_data
        # require_admin intentionally NOT overridden.

        client = TestClient(app)
        response = client.post("/api/v1/risk/symbol-kill-switch/pionex:BTC%2FUSDT/activate")

        assert response.status_code in (401, 403)
