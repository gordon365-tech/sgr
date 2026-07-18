"""SGR System Router"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from sgr.api.dependencies import TokenData, require_admin, require_auth
from sgr.core.config import get_config

router = APIRouter()


class SystemStatusResponse(BaseModel):
    version: str
    environment: str
    trading_mode: str
    uptime_seconds: float
    timestamp: str
    components: dict[str, str]


_startup_time = datetime.now(tz=UTC)


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(
    request: Request,
    user: Annotated[TokenData, Depends(require_auth)],
) -> SystemStatusResponse:
    """Systemstatus aller Komponenten."""
    config = get_config()
    uptime = (datetime.now(tz=UTC) - _startup_time).total_seconds()

    components: dict[str, str] = {}
    pool = getattr(request.app.state, "exchange_pool", None)
    components["exchange_pool"] = "ok" if pool and len(pool) > 0 else "degraded"

    md = getattr(request.app.state, "market_data_engine", None)
    components["market_data"] = "running" if md and md.is_running else "stopped"

    risk = getattr(request.app.state, "risk_engine", None)
    components["risk_engine"] = "ok" if risk else "unavailable"

    from sgr.risk.kill_switch import get_kill_switch

    ks = get_kill_switch(config.trading_mode)
    components["kill_switch"] = "ACTIVE" if ks.is_active else "standby"

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
