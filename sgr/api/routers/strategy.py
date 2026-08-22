"""SGR Strategy Router"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sgr.api.dependencies import TokenData, require_admin, require_auth
from sgr.strategy.registry import StrategyRegistry

router = APIRouter()


class StrategyStatusResponse(BaseModel):
    name: str
    version: str
    is_active: bool
    is_validated: bool
    supported_regimes: list[str]
    deactivation_reason: str | None
    performance: dict | None


@router.get("/", response_model=list[StrategyStatusResponse])
async def list_strategies(
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[StrategyStatusResponse]:
    """Alle registrierten Strategien mit Status."""
    registry = StrategyRegistry.get()
    result = []
    for _name, entry in registry.get_all().items():
        perf = None
        if entry.performance:
            p = entry.performance
            perf = {
                "sharpe_ratio": p.sharpe_ratio,
                "sortino_ratio": p.sortino_ratio,
                "max_drawdown_pct": p.max_drawdown * 100,
                "hit_rate_pct": p.hit_rate * 100,
                "profit_factor": p.profit_factor,
                "total_trades": p.total_trades,
            }
        result.append(
            StrategyStatusResponse(
                name=entry.strategy.name,
                version=entry.strategy.version,
                is_active=entry.is_active,
                is_validated=entry.is_validated,
                supported_regimes=[r.value for r in entry.strategy.supported_regimes],
                deactivation_reason=entry.deactivation_reason,
                performance=perf,
            )
        )
    return result


@router.post("/{name}/activate")
async def activate_strategy(
    name: str,
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict:
    """Strategie aktivieren (Admin only)."""
    registry = StrategyRegistry.get()
    entry = registry.get_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")
    if not entry.is_validated:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{name}' has not passed validation. Cannot activate.",
        )
    registry.activate(name)
    return {"activated": name}


@router.post("/{name}/deactivate")
async def deactivate_strategy(
    name: str,
    reason: str = "Manual deactivation",
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """Strategie deaktivieren (Admin only)."""
    registry = StrategyRegistry.get()
    if registry.get_entry(name) is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")
    registry.deactivate(name, reason)
    return {"deactivated": name, "reason": reason}
