"""
SGR System Router
===================
Read-only: Systemstatus wird ueber echte Konnektivitaets-Checks (DB,
Redis) und den Redis-Kill-Switch-State ermittelt, nicht mehr ueber die
Anwesenheit von In-Memory-Engines in app.state (exchange_pool,
market_data_engine, risk_engine sagen nichts darueber aus, ob der
tatsaechlich Trades ausfuehrende sgr-worker-Prozess laeuft oder
verbunden ist - diese Engines existieren im API-Prozess nach der
sgr-api/sgr-worker-Trennung ohnehin nicht mehr sinnvoll befuellt).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from sgr.api.dependencies import (
    TokenData,
    get_redis_client_or_none,
    get_trading_mode,
    require_admin,
    require_auth,
)
from sgr.core.config import get_config
from sgr.core.database import get_session
from sgr.core.types import TradingMode
from sgr.risk.kill_switch import read_kill_switch_state_from_redis

router = APIRouter()


class SystemStatusResponse(BaseModel):
    version: str
    environment: str
    trading_mode: str
    uptime_seconds: float
    timestamp: str
    components: dict[str, str]


_startup_time = datetime.now(tz=UTC)


async def _check_database() -> str:
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "unavailable"


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    redis_client: Annotated[Redis | None, Depends(get_redis_client_or_none)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> SystemStatusResponse:
    """Systemstatus aller Komponenten (echte Konnektivitaets-Checks)."""
    config = get_config()
    uptime = (datetime.now(tz=UTC) - _startup_time).total_seconds()

    components: dict[str, str] = {}
    components["database"] = await _check_database()

    if redis_client is None:
        components["redis"] = "unavailable"
        components["kill_switch"] = "unknown"
    else:
        try:
            await redis_client.ping()
            components["redis"] = "ok"
        except Exception:
            components["redis"] = "unavailable"

        ks_state = await read_kill_switch_state_from_redis(redis_client, trading_mode)
        components["kill_switch"] = (
            "unknown"
            if ks_state is None
            else ("ACTIVE" if ks_state["is_active"] else "standby")
        )

    return SystemStatusResponse(
        version=config.version,
        environment=config.environment.value,
        trading_mode=config.trading_mode.value,
        uptime_seconds=round(uptime, 1),
        timestamp=datetime.now(tz=UTC).isoformat(),
        components=components,
    )


@router.get("/config")
async def get_safe_config(
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict:
    """
    Nicht-sensitive Config-Parameter.
    Nur für Admins. Secrets werden NIEMALS zurückgegeben.
    """
    config = get_config()
    limits = config.risk_limits
    return {
        "trading_mode": config.trading_mode.value,
        "environment": config.environment.value,
        "risk_limits": {
            "max_portfolio_drawdown_pct": limits.max_portfolio_drawdown * 100,
            "daily_loss_limit_pct": limits.daily_loss_limit * 100,
            "max_single_position_pct": limits.max_single_position_pct * 100,
            "var_95_limit_pct": limits.var_95_limit * 100,
            "portfolio_heat_limit_pct": limits.portfolio_heat_limit * 100,
            "max_leverage": str(limits.max_leverage),
            "max_open_positions": limits.max_open_positions,
        },
    }
