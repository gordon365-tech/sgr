"""
SGR Trading Router
===================
Manuelles Auslösen eines Trading-Zyklus über den Orchestrator.

Sicherheitsmechanismus für Live Trading:
    Ein Aufruf mit trading_mode == LIVE wird abgelehnt, sofern nicht
    explizit confirm_live=true im Request-Body gesetzt ist. Das verhindert,
    dass ein versehentlicher/automatisierter Client-Call in Live einen
    echten Trade auslöst. Dry Run und Paper Trading benötigen dieses
    Flag nicht - sie haben laut Projektgrundsatz ohnehin Vorrang und
    sind risikofrei.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sgr.api.dependencies import TokenData, get_orchestrator, require_admin
from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.core.types import MarketRegime, TradingCycleResult, TradingMode
from sgr.orchestrator.engine import TradingOrchestrator

log = get_logger(__name__)

router = APIRouter()


class TriggerCycleRequest(BaseModel):
    symbol_key: str
    timeframe: str
    regime: MarketRegime = MarketRegime.UNKNOWN
    confirm_live: bool = False


@router.post("/cycle", response_model=TradingCycleResult)
async def trigger_cycle(
    body: TriggerCycleRequest,
    user: Annotated[TokenData, Depends(require_admin)],
    orchestrator: Annotated[TradingOrchestrator, Depends(get_orchestrator)],
) -> TradingCycleResult:
    """
    Löst manuell einen einzelnen Trading-Zyklus aus.
    Nur für Admins. In LIVE-Mode ist confirm_live=true zwingend erforderlich.
    """
    config = get_config()

    if config.trading_mode == TradingMode.LIVE and not body.confirm_live:
        log.warning(
            "trading_router.live_trigger_blocked",
            symbol_key=body.symbol_key,
            reason="confirm_live not set",
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "Live trading cycle requires explicit confirm_live=true. "
                "This is a deliberate safety gate, not an error."
            ),
        )

    result = await orchestrator.run_cycle(body.symbol_key, body.timeframe, body.regime)
    return result
