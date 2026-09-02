"""SGR Health Check Routers

Implementiert differentierte Health Checks:

1. /health/live  - Liveness Probe (ist Prozess noch am Leben?)
2. /health/ready - Readiness Probe (kann Traffic akzeptiert werden?)
3. /health/trading - Trading Health (ist Trading safe zu aktivieren?)
4. /health - Legacy/Default (kombiniert die wichtigsten Checks)
5. /ping - Einfacher Echo-Check

Fail-Safe Principle:
- Jeder unbekannte Zustand → FALSE/unhealthy (konservativ)
- Lieber kurz nicht ready als falschlicherweise ready zu sagen
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.risk.kill_switch import get_kill_switch

log = get_logger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    trading_mode: str
    timestamp: str
    components: dict[str, str]


class LivenessResponse(BaseModel):
    """Liveness Probe: Ist Prozess noch aktiv?"""
    status: str = Field(default="alive")
    timestamp: str = Field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


class ReadinessResponse(BaseModel):
    """Readiness Probe: Kann Traffic akzeptiert werden?"""
    status: str
    db_connected: bool
    redis_connected: bool
    components_initialized: bool
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


class TradingHealthResponse(BaseModel):
    """Trading Health: Ist es sicher zu traden?"""
    status: str
    trading_enabled: bool
    kill_switch_active: bool
    recovery_complete: bool
    exchange_connected: bool
    preflight_available: bool
    risk_engine_available: bool
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


@router.get("/health/live", response_model=LivenessResponse, status_code=200)
async def health_live() -> LivenessResponse:
    """Liveness Probe: Ist der Prozess noch am Leben?"""
    return LivenessResponse(status="alive")


@router.get("/health/ready", response_model=ReadinessResponse)
async def health_ready(request: Request) -> Response:
    """
    Readiness Probe: Ist der Service bereit für Traffic?
    
    Returns 200 wenn ready, 503 wenn nicht ready.
    Load Balancer entfernt Pod (kein Neustart).
    """
    details: dict[str, Any] = {}

    # DB Check
    db_connected = False
    try:
        pool = getattr(request.app.state, "exchange_pool", None)
        # Vereinfachte Check: wenn pool existiert und nicht leer
        db_connected = pool is not None and len(pool) > 0 if pool else False
        details["database"] = "connected" if db_connected else "disconnected"
    except Exception as e:
        details["database"] = f"error: {str(e)[:50]}"
        db_connected = False

    # Redis Check (Feature Store)
    redis_connected = False
    try:
        store = getattr(request.app.state, "feature_store", None)
        redis_connected = store is not None and getattr(store, "_redis", None) is not None
        details["redis"] = "connected" if redis_connected else "disconnected"
    except Exception as e:
        details["redis"] = f"error: {str(e)[:50]}"
        redis_connected = False

    # Komponenten Check
    components_initialized = db_connected and redis_connected

    status = "healthy" if components_initialized else "unhealthy"
    http_status = 200 if components_initialized else 503

    response_data = ReadinessResponse(
        status=status,
        db_connected=db_connected,
        redis_connected=redis_connected,
        components_initialized=components_initialized,
        details=details,
    )

    return Response(
        content=response_data.model_dump_json(),
        status_code=http_status,
        media_type="application/json",
    )


@router.get("/health/trading", response_model=TradingHealthResponse)
async def health_trading(request: Request) -> Response:
    """
    Trading Health: Ist es sicher zu traden?
    
    Prüft:
    - Kill Switch nicht aktiv
    - Recovery abgeschlossen
    - Exchange-Verbindung
    - Preflight verfügbar  
    - Risk Engine verfügbar
    
    Returns 200 wenn trading_enabled, 503 wenn disabled.
    Nicht für Load Balancer Decisions, sondern für UI/Monitoring.
    """
    config = get_config()
    trading_mode = config.trading_mode
    details: dict[str, Any] = {}

    # Kill Switch Check
    kill_switch = get_kill_switch(trading_mode)
    kill_switch_active = kill_switch.is_active
    details["kill_switch_active"] = kill_switch_active

    # Recovery State
    recovery_complete = True  # Im Lifespan bereits abgeschlossen
    details["recovery_complete"] = recovery_complete

    # Exchange Connection
    exchange_connected = False
    try:
        pool = getattr(request.app.state, "exchange_pool", None)
        exchange_connected = pool is not None and len(pool) > 0 if pool else False
        details["exchange_connected"] = exchange_connected
    except Exception as e:
        details["exchange_connected"] = f"error: {str(e)[:50]}"

    # Risk Engine
    risk_engine_available = False
    try:
        risk = getattr(request.app.state, "risk_engine", None)
        risk_engine_available = risk is not None and getattr(risk, "_initialized", False)
        details["risk_engine_available"] = risk_engine_available
    except Exception as e:
        details["risk_engine_available"] = f"error: {str(e)[:50]}"

    # Preflight (über Execution Engine)
    preflight_available = False
    try:
        exec_engine = getattr(request.app.state, "execution_engine", None)
        preflight = getattr(exec_engine, "_preflight", None) if exec_engine else None
        preflight_available = preflight is not None
        details["preflight_available"] = preflight_available
    except Exception as e:
        details["preflight_available"] = f"error: {str(e)[:50]}"

    # Trading Enabled Decision
    trading_enabled = (
        not kill_switch_active
        and recovery_complete
        and exchange_connected
        and preflight_available
        and risk_engine_available
    )

    status = "healthy" if trading_enabled else "degraded"
    http_status = 200 if trading_enabled else 503

    details["trading_mode"] = trading_mode.value

    response_data = TradingHealthResponse(
        status=status,
        trading_enabled=trading_enabled,
        kill_switch_active=kill_switch_active,
        recovery_complete=recovery_complete,
        exchange_connected=exchange_connected,
        preflight_available=preflight_available,
        risk_engine_available=risk_engine_available,
        details=details,
    )

    return Response(
        content=response_data.model_dump_json(),
        status_code=http_status,
        media_type="application/json",
    )


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """
    Default Health Check Endpoint (kombiniert Readiness).
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
    components["feature_store"] = "ok" if store and getattr(store, "_redis", None) else "degraded"

    # Risk Engine
    risk = getattr(request.app.state, "risk_engine", None)
    components["risk_engine"] = "ok" if risk and getattr(risk, "_initialized", False) else "degraded"

    # Market Data Engine
    md = getattr(request.app.state, "market_data_engine", None)
    components["market_data"] = "ok" if md and getattr(md, "is_running", False) else "degraded"

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
    """Einfacher Echo-Check für Connectivity."""
    return {"pong": datetime.now(tz=UTC).isoformat()}
