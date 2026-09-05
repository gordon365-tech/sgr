"""
SGR Trading Router
===================
Manuelles Auslösen eines Trading-Zyklus - AKTUELL AUSSER BETRIEB (501).

Architektur-Hintergrund (sgr-api Read-Only-Zielarchitektur):
    Der bisherige Code führte orchestrator.run_cycle() DIREKT im
    API-Prozess aus - der klarste Fall von "Trading Lifecycle Logik in
    der API", die laut Zielarchitektur vollständig entfallen muss.
    sgr-worker ist alleiniger Owner des Trading Lifecycle; die API darf
    keinen eigenen Orchestrator mehr besitzen oder aufrufen.

    Ein manueller Cycle-Trigger bleibt ein legitimes operatives
    Werkzeug (z.B. nach einem Deployment gezielt testen, statt auf den
    nächsten automatischen Zyklus zu warten) - daher wird der Endpoint
    NICHT ersatzlos entfernt, sondern bewusst als 501 beantwortet, bis
    ein Folge-Commit einen echten Command-Channel (Redis Pub/Sub oder
    Stream, inkl. Ack/Timeout-Semantik) zum Worker einführt. Das ist
    mehr als ein Router-Datenquellen-Wechsel und verdient einen eigenen,
    fokussierten Commit mit eigenen Tests (analog zur Kill-Switch-
    Umstellung in Commit 2).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sgr.api.dependencies import TokenData, require_admin
from sgr.core.types import MarketRegime

router = APIRouter()


class TriggerCycleRequest(BaseModel):
    symbol_key: str
    timeframe: str
    regime: MarketRegime = MarketRegime.UNKNOWN
    confirm_live: bool = False


@router.post("/cycle")
async def trigger_cycle(
    body: TriggerCycleRequest,
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict:
    """
    Löst manuell einen einzelnen Trading-Zyklus aus.

    Noch nicht auf die sgr-api Read-Only-Zielarchitektur migriert (siehe
    Modul-Docstring) - liefert bewusst 501 statt Trading-Lifecycle-Logik
    direkt im API-Prozess auszuführen.
    """
    raise HTTPException(
        status_code=501,
        detail=(
            "Manual cycle trigger not yet migrated to the read-only API "
            "architecture. Running the trading orchestrator directly in "
            "the API process is no longer permitted; a Redis-backed "
            "command channel to sgr-worker is planned as a follow-up."
        ),
    )
