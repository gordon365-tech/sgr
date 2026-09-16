"""
SGR Strategy Symbol Validation Router
========================================
Read-only Endpunkte fuer die Ergebnisse aus
sgr/strategy/symbol_validation_runner.py (Autonomous-Strategy-Universe-
Rollout, Phase 15). Liest ausschliesslich aus
StrategySymbolValidationRepository (DB) - keine In-Memory-Abhaengigkeit
zu einem laufenden Batch-Prozess, analog zum bestehenden Muster in
strategy.py (StrategyRepository statt StrategyRegistry.get()).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from sgr.api.dependencies import TokenData, get_repos, require_auth
from sgr.core.repositories import Repositories

router = APIRouter()


@router.get("/overview")
async def get_overview(
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Gesamtzusammenfassung: Anzahl Symbole je Status + Strategie-
    Verteilung unter den ACTIVE-Symbolen (Phase 21 'Total/Validated/
    Active/...')."""
    status_counts = await repos.strategy_symbol_validations.get_status_counts(batch_id=batch_id)
    strategy_distribution = await repos.strategy_symbol_validations.get_strategy_distribution(
        batch_id=batch_id
    )
    total = sum(status_counts.values())
    active = status_counts.get("active", 0)
    return {
        "batch_id": batch_id,
        "total_symbols": total,
        "status_counts": status_counts,
        "strategy_distribution": strategy_distribution,
        "validation_success_rate_pct": round(active / total * 100, 2) if total else 0.0,
    }


@router.get("/by-symbol/{symbol:path}")
async def get_by_symbol(
    symbol: str,
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    exchange: str = "binance",
    timeframe: str = "1h",
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Alle getesteten Strategien fuer EIN Symbol, inkl. der nicht
    gewonnenen Kandidaten (fuer Nachvollziehbarkeit, warum eine
    bestimmte Strategie gewaehlt/nicht gewaehlt wurde)."""
    return await repos.strategy_symbol_validations.get_by_symbol(
        symbol=symbol, exchange=exchange, timeframe=timeframe, batch_id=batch_id
    )


@router.get("/best-by-symbol")
async def get_best_by_symbol(
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    batch_id: str | None = None,
    limit: int = Query(default=1000, le=5000),
) -> list[dict[str, Any]]:
    """Ein Ergebnis pro Symbol - die jeweils gewaehlte (oder als
    ungeeignet markierte) Strategie. Basis fuer die im Auftrag
    geforderte Tabelle (Symbol/Timeframe/Best Strategy/Score/Sharpe/
    Return/Drawdown/Trades/Status)."""
    return await repos.strategy_symbol_validations.get_best_by_symbol(
        batch_id=batch_id, limit=limit
    )


@router.get("/active")
async def get_active(
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Alle Symbole mit Status=ACTIVE (echte Produktions-Aktivierung,
    siehe sgr/strategy/symbol_gate.py)."""
    return await repos.strategy_symbol_validations.get_active(batch_id=batch_id)


@router.get("/failed")
async def get_failed(
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Symbole ohne geeignete Strategie (NO_VALID_STRATEGY) - fuer die
    im Auftrag geforderte 'failed validations'-Ansicht."""
    rows = await repos.strategy_symbol_validations.get_best_by_symbol(
        batch_id=batch_id, limit=5000
    )
    return [r for r in rows if r["status"] == "no_valid_strategy"]


@router.get("/data-quality-failures")
async def get_data_quality_failures(
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Symbole, die schon am Data Quality Gate gescheitert sind
    (INSUFFICIENT_DATA / INVALID_DATA) - wurden nie einer Strategie
    zugeordnet."""
    rows = await repos.strategy_symbol_validations.get_best_by_symbol(
        batch_id=batch_id, limit=5000
    )
    return [r for r in rows if r["status"] in ("insufficient_data", "invalid_data")]


@router.get("/top")
async def get_top(
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
    order_by: str = Query(default="score", pattern="^(score|sharpe|return|robustness)$"),
    limit: int = Query(default=20, le=100),
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Top-N nach Score/Sharpe/Return/Robustness (Phase 21 'Top 20
    Strategien nach ...')."""
    return await repos.strategy_symbol_validations.get_top_n(
        order_by=order_by, limit=limit, batch_id=batch_id
    )
