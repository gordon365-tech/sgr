"""SGR Risk Router"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sgr.api.dependencies import (
    TokenData,
    get_portfolio_engine,
    get_risk_engine,
    require_admin,
    require_auth,
    require_live_2fa,
)
from sgr.core.config import get_config
from sgr.portfolio.engine import PortfolioEngine
from sgr.risk.engine import RiskEngine
from sgr.risk.kill_switch import get_kill_switch
from sgr.risk.symbol_kill_switch import get_symbol_kill_switch

router = APIRouter()


class RiskMetricsResponse(BaseModel):
    portfolio_value: str
    daily_pnl: str
    daily_pnl_pct: float
    drawdown_from_peak: float
    var_95: float
    expected_shortfall: float
    portfolio_heat: float
    active_positions: int
    kill_switch_active: bool
    kill_switch_reason: str | None


class KillSwitchResponse(BaseModel):
    is_active: bool
    triggered_at: str | None
    reason: str | None
    trading_mode: str


class KillSwitchTriggerRequest(BaseModel):
    reason: str
    close_positions: bool = False


@router.get("/metrics", response_model=RiskMetricsResponse)
async def get_risk_metrics(
    risk: Annotated[RiskEngine, Depends(get_risk_engine)],
    portfolio: Annotated[PortfolioEngine, Depends(get_portfolio_engine)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> RiskMetricsResponse:
    """Aktuelle Risk-Metriken des Portfolios."""
    metrics = risk._compute_metrics(
        portfolio_value=portfolio.portfolio_value,
        positions=portfolio.positions,
    )
    ks = get_kill_switch(risk._trading_mode)

    return RiskMetricsResponse(
        portfolio_value=str(metrics.portfolio_value),
        daily_pnl=str(metrics.daily_pnl),
        daily_pnl_pct=round(metrics.daily_pnl_pct * 100, 2),
        drawdown_from_peak=round(metrics.drawdown_from_peak * 100, 2),
        var_95=round(metrics.var_95 * 100, 4),
        expected_shortfall=round(metrics.expected_shortfall * 100, 4),
        portfolio_heat=round(metrics.portfolio_heat * 100, 2),
        active_positions=metrics.active_positions,
        kill_switch_active=ks.is_active,
        kill_switch_reason=ks.state.reason,
    )


@router.get("/limits")
async def get_limits(
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """Aktuelle Risk-Limit-Konfiguration."""
    config = get_config()
    limits = config.risk_limits
    return {
        "hard_limits": {
            "max_portfolio_drawdown_pct": limits.max_portfolio_drawdown * 100,
            "daily_loss_limit_pct": limits.daily_loss_limit * 100,
            "max_single_position_pct": limits.max_single_position_pct * 100,
            "max_open_positions": limits.max_open_positions,
        },
        "soft_limits": {
            "var_95_limit_pct": limits.var_95_limit * 100,
            "portfolio_heat_limit_pct": limits.portfolio_heat_limit * 100,
            "max_correlation_exposure": limits.max_correlation_exposure,
            "max_slippage_pct": limits.max_slippage_pct * 100,
        },
        "futures": {
            "max_leverage": str(limits.max_leverage),
        },
    }


@router.get("/kill-switch", response_model=KillSwitchResponse)
async def get_kill_switch_status(
    risk: Annotated[RiskEngine, Depends(get_risk_engine)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> KillSwitchResponse:
    """Status des Kill Switch."""
    ks = get_kill_switch(risk._trading_mode)
    state = ks.state
    return KillSwitchResponse(
        is_active=ks.is_active,
        triggered_at=state.triggered_at.isoformat() if state.triggered_at else None,
        reason=state.reason,
        trading_mode=risk._trading_mode.value,
    )


@router.post("/kill-switch/trigger")
async def trigger_kill_switch(
    body: KillSwitchTriggerRequest,
    risk: Annotated[RiskEngine, Depends(get_risk_engine)],
    user: Annotated[TokenData, Depends(require_live_2fa)],
) -> dict:
    """
    Manueller Kill Switch Trigger.
    Erfordert Auth + 2FA (Live Mode).
    """
    ks = get_kill_switch(risk._trading_mode)
    await ks.trigger(
        reason=f"Manual: {body.reason}",
        triggered_by=f"user:{user.user_id}",
        close_positions=body.close_positions,
    )
    return {
        "triggered": True,
        "reason": body.reason,
        "triggered_by": user.user_id,
    }


@router.post("/kill-switch/reset")
async def reset_kill_switch(
    risk: Annotated[RiskEngine, Depends(get_risk_engine)],
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict:
    """
    Kill Switch zurücksetzen.
    Erfordert Admin-Rolle.
    Nur nach manueller Prüfung der Ursache aufrufen.
    """
    ks = get_kill_switch(risk._trading_mode)
    await ks.reset(reset_by=user.user_id)
    return {"reset": True, "reset_by": user.user_id}


# ---------------------------------------------------------------------
# Symbol Kill Switch
# ---------------------------------------------------------------------


class SymbolKillSwitchEntryResponse(BaseModel):
    symbol_key: str
    is_active: bool
    deactivated_at: str | None
    reason: str | None
    deactivated_by: str | None


@router.get("/symbol-kill-switch", response_model=list[SymbolKillSwitchEntryResponse])
async def list_symbol_kill_switches(
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[SymbolKillSwitchEntryResponse]:
    """Alle Symbole mit explizitem Kill-Switch-Eintrag (aktiv + inaktiv)."""
    sks = get_symbol_kill_switch()
    return [
        SymbolKillSwitchEntryResponse(
            symbol_key=entry.symbol_key,
            is_active=entry.is_active,
            deactivated_at=entry.deactivated_at.isoformat() if entry.deactivated_at else None,
            reason=entry.reason,
            deactivated_by=entry.deactivated_by,
        )
        for entry in sks.get_all().values()
    ]


@router.post("/symbol-kill-switch/{symbol_key:path}/deactivate")
async def deactivate_symbol(
    symbol_key: str,
    user: Annotated[TokenData, Depends(require_auth)],
    reason: str = "Manual deactivation",
) -> dict[str, str]:
    """
    Trading für ein einzelnes Symbol deaktivieren, ohne das gesamte
    System zu stoppen (im Gegensatz zum globalen Kill Switch).

    Bewusst require_auth statt require_admin: wie bei
    strategy.deactivate_strategy() ist Deaktivieren die defensive
    Richtung und darf niedrigschwelliger sein als Reaktivierung.
    """
    sks = get_symbol_kill_switch()
    await sks.deactivate(symbol_key, reason, deactivated_by=f"user:{user.user_id}")
    return {"deactivated": symbol_key, "reason": reason}


@router.post("/symbol-kill-switch/{symbol_key:path}/activate")
async def activate_symbol(
    symbol_key: str,
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict[str, str]:
    """
    Trading für ein zuvor deaktiviertes Symbol wieder erlauben.
    Erfordert Admin-Rolle (Re-Aktivierung ist die riskante Richtung).
    """
    sks = get_symbol_kill_switch()
    await sks.activate(symbol_key, activated_by=f"user:{user.user_id}")
    return {"activated": symbol_key}
