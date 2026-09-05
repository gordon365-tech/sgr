"""
SGR Orders Router
==================
Read-only: liest Order-History über OrderRepository/DB statt über
PortfolioEngine.trade_history (In-Memory, nur geschlossene Trades als
Behelfslösung für "Order-Ansicht"). OrderRepository.get_by_user() bildet
tatsächliche Order-Objekte ab (inkl. offener/teilgefüllter Orders, nicht
nur abgeschlossener Trades) - eine sachlich bessere Antwort auf dieselbe
Frage, kein Workaround.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from sgr.api.dependencies import TokenData, get_repos, require_auth
from sgr.core.repositories import Repositories

router = APIRouter()


@router.get("/history")
async def get_order_history(
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    limit: int = 50,
) -> list[dict]:
    """Order-History für den authentifizierten User, neueste zuerst."""
    return await repos.orders.get_by_user(user.user_id, user.trading_mode, limit=limit)
