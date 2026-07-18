"""SGR Health Check Router"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from sgr.core.config import get_config

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    trading_mode: str
    timestamp: str
    components: dict[str, str]


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """
    Health check endpoint.
    Prüft alle kritischen Komponenten.
    Keine Authentifizierung erforderlich (für Load Balancer).
    """
    config = get_config()
    components: dict[str, str] = {}

    # Exchange Pool
    pool = getattr(request.app.state, "exchange_pool", None)
    components["exchange_pool"] = "ok" if pool and len(pool) > 0 else "degraded"

    # Feature Store
    store = getattr(request.app.state, "feature_store", None)
    components["feature_store"] = "ok" if store and store._redis else "degraded"

    # Risk Engine
    risk = getattr(request.app.state, "risk_engine", None)
    components["risk_engine"] = "ok" if risk and risk._initialized else "degraded"

    # Market Data Engine
    md = getattr(request.app.state, "market_data_engine", None)
    components["market_data"] = "ok" if md and md.is_running else "degraded"

    overall = "ok" if all(v == "ok" for v in components.values()) else "degraded"

    return HealthResponse(
        status=overall,
        version=config.version,
        environment=config.environment.value,
        trading_mode=config.trading_mode.value,
        timestamp=datetime.now(tz=UTC).isoformat(),
        components=components,
    )


@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": datetime.now(tz=UTC).isoformat()}
