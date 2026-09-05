"""
SGR Health Check Routers

Implementiert differentierte Health Checks:

1. /health/live  - Liveness Probe (ist Prozess noch am Leben?)
2. /health/ready - Readiness Probe (kann Traffic akzeptiert werden?)
3. /health/trading - Trading Health (ist Trading safe zu aktivieren?)
4. /health - Legacy/Default (kombiniert die wichtigsten Checks)
5. /ping - Einfacher Echo-Check

Fail-Safe Principle:
- Jeder unbekannte Zustand → FALSE/unhealthy (konservativ)
- Lieber kurz nicht ready als falschlicherweise ready zu sagen

Migrationsstand (sgr-api Read-Only-Zielarchitektur):
    Alle Checks laufen jetzt über echte Konnektivitäts-Pruefungen (DB,
    Redis) und Redis-gelesene Zustände (Kill Switch), nicht mehr über
    die Anwesenheit von In-Memory-Engines in app.state.

    /health/trading: exchange_connected, preflight_available und
    risk_engine_available sind Worker-interne Zustände ohne aktuelle
    Redis/DB-Repräsentation (kein Push-Mechanismus vom Worker analog
    zum Kill Switch). Sie werden bewusst als "unknown" statt "ok"/
    "degraded" gemeldet, bis ein Folge-Commit den Worker dazu bringt,
    diese Zustände nach Redis zu publizieren. trading_enabled bleibt
    dabei konservativ False, solange irgendein Signal unknown ist
    (Fail-Safe-Prinzip dieses Moduls).

    WICHTIGER FUND (bereits vor der Migration bestehender Bug): die
    alte /health/ready-Implementierung prüfte "db_connected" fälschlich
    über die Anwesenheit von exchange_pool, nicht über einen echten
    Datenbank-Zugriff. Hier korrigiert (echter SELECT 1 Ping).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from sgr.api.dependencies import get_redis_client_or_none
from sgr.core.config import get_config
from sgr.core.database import get_session
from sgr.core.logging import get_logger
from sgr.risk.kill_switch import read_kill_switch_state_from_redis

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
    kill_switch_active: bool | None
    recovery_complete: bool
    exchange_connected: str
    preflight_available: str
    risk_engine_available: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


async def _check_database() -> tuple[bool, str]:
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return True, "connected"
    except Exception as e:
        return False, f"error: {str(e)[:50]}"


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

    db_connected, db_detail = await _check_database()
    details["database"] = db_detail

    redis_client = get_redis_client_or_none(request)
    redis_connected = False
    if redis_client is None:
        details["redis"] = "disconnected"
    else:
        try:
            await redis_client.ping()
            redis_connected = True
            details["redis"] = "connected"
        except Exception as e:
            details["redis"] = f"error: {str(e)[:50]}"

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
    - Kill Switch nicht aktiv (aus Redis, vom Worker geschrieben)
    - Recovery abgeschlossen
    - Exchange-Verbindung, Preflight, Risk Engine: siehe Modul-Docstring
      - diese drei sind aktuell "unknown" (Worker-interne Zustände ohne
        Redis-Repräsentation, Folge-Commit geplant).

    Returns 200 wenn trading_enabled, 503 wenn disabled.
    Nicht für Load Balancer Decisions, sondern für UI/Monitoring.
    """
    config = get_config()
    trading_mode = config.trading_mode
    details: dict[str, Any] = {}

    redis_client = get_redis_client_or_none(request)
    kill_switch_active: bool | None
    if redis_client is None:
        kill_switch_active = None
        details["kill_switch_active"] = "unknown"
    else:
        ks_state = await read_kill_switch_state_from_redis(redis_client, trading_mode)
        kill_switch_active = bool(ks_state["is_active"]) if ks_state is not None else None
        details["kill_switch_active"] = (
            "unknown" if kill_switch_active is None else kill_switch_active
        )

    recovery_complete = True  # Im Lifespan bereits abgeschlossen
    details["recovery_complete"] = recovery_complete

    # Worker-interne Zustände ohne aktuelle Redis-Repraesentation - siehe
    # Modul-Docstring. Bewusst "unknown" statt erfunden/geraten.
    exchange_connected = "unknown"
    preflight_available = "unknown"
    risk_engine_available = "unknown"
    details["exchange_connected"] = exchange_connected
    details["preflight_available"] = preflight_available
    details["risk_engine_available"] = risk_engine_available

    # Fail-safe (siehe Modul-Docstring-Prinzip: unbekannt -> False).
    # exchange_connected/preflight_available/risk_engine_available sind
    # aktuell durchgehend "unknown" (siehe oben) - trading_enabled kann
    # daher derzeit nie True werden, bis der Folge-Commit diese Signale
    # verlaesslich aus Redis liefert. Das ist eine bewusste, dokumentierte
    # Konsequenz des Zwischenstands, keine Regression: der alte Code
    # konnte diese Signale zwar auf True setzen, aber nur ueber
    # In-Memory-Engines, die im Read-Only-API-Prozess nicht mehr existieren
    # wuerden - "immer optimistisch True" waere die eigentliche Regression.
    signals_known = (
        exchange_connected != "unknown"
        and preflight_available != "unknown"
        and risk_engine_available != "unknown"
    )
    trading_enabled = (
        kill_switch_active is False and recovery_complete and signals_known
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

    db_connected, _ = await _check_database()
    components["database"] = "ok" if db_connected else "degraded"

    redis_client = get_redis_client_or_none(request)
    if redis_client is None:
        components["redis"] = "degraded"
    else:
        try:
            await redis_client.ping()
            components["redis"] = "ok"
        except Exception:
            components["redis"] = "degraded"

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
