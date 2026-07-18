"""SGR Orders Router"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from sgr.api.dependencies import TokenData, get_portfolio_engine, require_auth
from sgr.portfolio.engine import PortfolioEngine

router = APIRouter()


@router.get("/history")
async def get_order_history(
    portfolio: Annotated[PortfolioEngine, Depends(get_portfolio_engine)],
    user: Annotated[TokenData, Depends(require_auth)],
    limit: int = 50,
) -> list[dict]:
    """Trade-History als Order-Sicht."""
    trades = portfolio.trade_history[-limit:]
    return list(reversed(trades))
