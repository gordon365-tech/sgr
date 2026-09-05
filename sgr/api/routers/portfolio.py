"""
SGR Portfolio Router
=====================
Read-only: liest Portfolio-Zustand ausschließlich über Repository/DB
(PortfolioSnapshotRepository, TradeRepository). Kein Zugriff mehr auf
PortfolioEngine als In-Memory-Engine - die API besitzt seit der
sgr-api/sgr-worker-Trennung keinen eigenen Trading Lifecycle mehr.

Wichtig für /overview: der zurückgegebene Snapshot ist der zuletzt vom
Worker geschriebene Stand, kein Live-Wert. Falls der Worker noch nie
einen Snapshot geschrieben hat (frisches Deployment), liefert dieser
Endpoint 503 - das ist beabsichtigt (fail-safe: lieber "noch kein Stand
bekannt" melden als eine erfundene/leere Antwort mit Erfolg quittieren).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sgr.api.dependencies import (
    TokenData,
    get_repos,
    get_trading_mode,
    require_auth,
)
from sgr.core.repositories import Repositories
from sgr.core.types import TradingMode

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
    repos: Annotated[Repositories, Depends(get_repos)],
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> PortfolioSummaryResponse:
    """
    Portfolio-Übersicht: Gesamtwert, Cash, unrealized PnL.
    Letzter vom Worker geschriebener PortfolioSnapshot, kein Live-Wert.
    """
    snapshot = await repos.portfolio_snapshots.get_latest(trading_mode)
    if snapshot is None:
        raise HTTPException(
            status_code=503,
            detail="No portfolio snapshot available yet",
        )
    return PortfolioSummaryResponse(
        portfolio_value=str(snapshot["portfolio_value"]),
        cash=str(snapshot["cash"]),
        unrealized_pnl=str(snapshot["unrealized_pnl"]),
        open_positions=snapshot["open_positions_count"],
        total_trades=snapshot["total_trades"],
        peak_value=str(snapshot["peak_value"]),
        drawdown=str(snapshot["drawdown"]),
        trading_mode=snapshot["trading_mode"],
    )


@router.get("/positions", response_model=list[PositionResponse])
async def get_positions(
    repos: Annotated[Repositories, Depends(get_repos)],
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[PositionResponse]:
    """Alle offenen Positionen (aus DB, vom Worker persistiert)."""
    positions = await repos.positions.get_open_positions(trading_mode)
    result = []
    for p in positions:
        entry = float(p["entry_price"])
        current = float(p["current_price"])
        pnl_pct = (current - entry) / entry if entry > 0 else 0.0
        if p["side"] == "short":
            pnl_pct = -pnl_pct
        notional_value = Decimal(str(p["quantity"])) * Decimal(str(p["current_price"]))

        result.append(
            PositionResponse(
                symbol=p["symbol"],
                side=p["side"],
                quantity=str(p["quantity"]),
                entry_price=str(p["entry_price"]),
                current_price=str(p["current_price"]),
                unrealized_pnl=str(p["unrealized_pnl"]),
                unrealized_pnl_pct=round(pnl_pct * 100, 2),
                notional_value=str(notional_value),
                strategy=p["strategy_name"],
            )
        )
    return result


@router.get("/trades", response_model=list[TradeResponse])
async def get_trades(
    repos: Annotated[Repositories, Depends(get_repos)],
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    user: Annotated[TokenData, Depends(require_auth)],
    limit: int = 50,
) -> list[TradeResponse]:
    """Trade-History (geschlossene Trades), neueste zuerst."""
    trades = await repos.trades.get_recent(trading_mode, limit=limit)
    return [TradeResponse(**t) for t in trades]


@router.get("/pnl")
async def get_pnl(
    repos: Annotated[Repositories, Depends(get_repos)],
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """
    PnL-Zusammenfassung. unrealized_pnl aus dem letzten Portfolio-Snapshot,
    realized-seitige Werte aus der vollständigen Trade-History.
    """
    snapshot = await repos.portfolio_snapshots.get_latest(trading_mode)
    if snapshot is None:
        raise HTTPException(
            status_code=503,
            detail="No portfolio snapshot available yet",
        )

    # Aggregation erfolgt in SQL (siehe TradeRepository.get_pnl_summary),
    # nicht durch Laden der gesamten Trade-Historie in den API-Prozess.
    summary = await repos.trades.get_pnl_summary(trading_mode)

    total_realized = summary["total_realized_pnl"]
    total_fees = summary["total_fees"]

    return {
        "unrealized_pnl": str(snapshot["unrealized_pnl"]),
        "realized_pnl": str(total_realized),
        "total_fees": str(total_fees),
        "net_pnl": str(total_realized - total_fees),
        "hit_rate": round(summary["hit_rate"], 4),
        "total_trades": summary["total_trades"],
        "winning_trades": summary["winning_trades"],
    }
