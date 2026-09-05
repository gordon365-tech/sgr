"""
SGR Strategy Router
=====================
Read-only: liest Strategy-Status ausschließlich aus StrategyRepository/DB,
nicht mehr aus dem In-Memory-Singleton StrategyRegistry.get(). Eine
StrategyRegistry-Instanz im API-Prozess wüsste ohnehin nichts von den
tatsächlich im Worker laufenden Strategien (siehe StrategyRepository.
get_all()-Docstring für den Hintergrund).

WICHTIG - Aktivieren/Deaktivieren (aktuelle Einschränkung):
    activate()/deactivate() schreiben korrekt in die DB (einzige von
    beiden Prozessen geteilte Quelle), aber es existiert noch KEIN
    Live-Push-Mechanismus von der DB zum laufenden Worker-Prozess -
    StrategyRegistry im Worker liest den DB-Status nur beim Start
    (get_active_names_from_db(), für Crash-Recovery). Eine über die API
    vorgenommene Aktivierung/Deaktivierung wird also persistiert, aber
    erst beim nächsten Worker-Neustart tatsächlich wirksam. Das ist eine
    bekannte, bewusst nicht in diesem Commit gelöste Lücke (analog zum
    Symbol Kill Switch) - die Response macht das explizit, statt einen
    sofortigen Live-Effekt vorzutäuschen.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sgr.api.dependencies import TokenData, get_repos, require_admin, require_auth
from sgr.core.repositories import Repositories

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
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[StrategyStatusResponse]:
    """Alle registrierten Strategien mit Status (aus DB)."""
    rows = await repos.strategies.get_all()
    result = []
    for row in rows:
        perf = None
        if row["sharpe_ratio"] is not None or row["total_trades"] > 0:
            perf = {
                "sharpe_ratio": row["sharpe_ratio"],
                "sortino_ratio": row["sortino_ratio"],
                "max_drawdown_pct": (
                    row["max_drawdown"] * 100 if row["max_drawdown"] is not None else None
                ),
                "hit_rate_pct": row["hit_rate"] * 100 if row["hit_rate"] is not None else None,
                "total_trades": row["total_trades"],
            }
        result.append(
            StrategyStatusResponse(
                name=row["name"],
                version=row["version"],
                is_active=row["is_active"],
                is_validated=row["is_validated"],
                supported_regimes=row["supported_regimes"],
                deactivation_reason=row["deactivation_reason"],
                performance=perf,
            )
        )
    return result


@router.post("/{name}/activate")
async def activate_strategy(
    name: str,
    repos: Annotated[Repositories, Depends(get_repos)],
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict:
    """
    Strategie aktivieren (Admin only).

    Schreibt in die DB. Wird erst beim nächsten Neustart des sgr-worker-
    Prozesses tatsächlich wirksam (siehe Modul-Docstring) - noch kein
    Live-Sync-Mechanismus zum laufenden Worker vorhanden.
    """
    entry = await repos.strategies.get_by_name(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")
    if not entry["is_validated"]:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{name}' has not passed validation. Cannot activate.",
        )
    await repos.strategies.set_active(name, is_active=True)
    return {
        "activated": name,
        "note": "Persisted to DB. Takes effect on next sgr-worker restart.",
    }


@router.post("/{name}/deactivate")
async def deactivate_strategy(
    name: str,
    user: Annotated[TokenData, Depends(require_auth)],
    repos: Annotated[Repositories, Depends(get_repos)],
    reason: str = "Manual deactivation",
) -> dict:
    """Strategie deaktivieren.

    Bewusst require_auth statt require_admin: Deaktivieren ist die
    defensive/sichere Richtung (eine Strategy vom Trading auszuschliessen
    darf niedrigschwelliger sein als sie zu aktivieren). Parameter-
    Reihenfolge: `user` (kein Default) muss vor `reason` (mit Default)
    stehen, sonst SyntaxError. FastAPI löst Dependencies über den
    Parameternamen auf, nicht über die Position, daher ist die Umsortierung
    ohne Verhaltensänderung möglich.

    Schreibt in die DB. Wird erst beim nächsten Neustart des sgr-worker-
    Prozesses tatsächlich wirksam (siehe Modul-Docstring).
    """
    entry = await repos.strategies.get_by_name(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")
    await repos.strategies.set_active(name, is_active=False, reason=reason)
    return {
        "deactivated": name,
        "reason": reason,
        "note": "Persisted to DB. Takes effect on next sgr-worker restart.",
    }
