"""SGR Portfolio Router"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sgr.api.dependencies import TokenData, get_portfolio_engine, require_auth
from sgr.portfolio.engine import PortfolioEngine

router = APIRouter()


class PositionResponse(BaseModel):
    symbol: str
    side: str
    quantity: str
    entry_price: str
    current_price: str
    unrealized_pnl: str
    unrealized_pnl_pct: float
    notional_value: str
    strategy: str


class PortfolioSummaryResponse(BaseModel):
    portfolio_value: str
    cash: str
    unrealized_pnl: str
    open_positions: int
    total_trades: int
    peak_value: str
    drawdown: str
    trading_mode: str


class TradeResponse(BaseModel):
    id: str
    symbol: str
    side: str
    entry_price: str
    exit_price: str
    quantity: str
    realized_pnl: str
    fees: str
    net_pnl: str
    strategy: str
    opened_at: str
    closed_at: str


@router.get("/overview", response_model=PortfolioSummaryResponse)
async def get_overview(
    portfolio: Annotated[PortfolioEngine, Depends(get_portfolio_engine)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> PortfolioSummaryResponse:
    """Portfolio-Übersicht: Gesamtwert, Cash, unrealized PnL."""
    summary = portfolio.summary()
    return PortfolioSummaryResponse(**summary)


@router.get("/positions", response_model=list[PositionResponse])
async def get_positions(
    portfolio: Annotated[PortfolioEngine, Depends(get_portfolio_engine)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[PositionResponse]:
    """Alle offenen Positionen."""
    positions = portfolio.positions
    result = []
    for p in positions:
        entry = float(p.entry_price)
        current = float(p.current_price)
        pnl_pct = (current - entry) / entry if entry > 0 else 0.0
        if p.side.value == "short":
            pnl_pct = -pnl_pct

        result.append(
            PositionResponse(
                symbol=p.symbol.ccxt_symbol,
                side=p.side.value,
                quantity=str(p.quantity),
                entry_price=str(p.entry_price),
                current_price=str(p.current_price),
                unrealized_pnl=str(p.unrealized_pnl),
                unrealized_pnl_pct=round(pnl_pct * 100, 2),
                notional_value=str(p.notional_value),
                strategy=p.strategy_name,
            )
        )
    return result


@router.get("/trades", response_model=list[TradeResponse])
async def get_trades(
    portfolio: Annotated[PortfolioEngine, Depends(get_portfolio_engine)],
    user: Annotated[TokenData, Depends(require_auth)],
    limit: int = 50,
) -> list[TradeResponse]:
    """Trade-History (geschlossene Trades)."""
    trades = portfolio.trade_history[-limit:]
    return [TradeResponse(**t) for t in reversed(trades)]


@router.get("/pnl")
async def get_pnl(
    portfolio: Annotated[PortfolioEngine, Depends(get_portfolio_engine)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """PnL-Zusammenfassung."""
    trades = portfolio.trade_history
    total_realized = sum(Decimal(t["realized_pnl"]) for t in trades)
    total_fees = sum(Decimal(t["fees"]) for t in trades)
    win_trades = [t for t in trades if Decimal(t["realized_pnl"]) > 0]
    hit_rate = len(win_trades) / len(trades) if trades else 0.0

    return {
        "unrealized_pnl": str(portfolio._state.unrealized_pnl),
        "realized_pnl": str(total_realized),
        "total_fees": str(total_fees),
        "net_pnl": str(total_realized - total_fees),
        "hit_rate": round(hit_rate, 4),
        "total_trades": len(trades),
        "winning_trades": len(win_trades),
    }
