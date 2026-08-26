"""
SGR Reconciliation Router
==========================
Manuelles Auslösen eines Reconciliation-Laufs (Phase 7B).

Nur für Admins - Reconciliation ist rein lesend (kein Trading-Effekt),
aber die Ergebnisse sind sicherheitsrelevant (Split-Brain-Erkennung) und
sollen nicht öffentlich einsehbar sein.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from sgr.api.dependencies import TokenData, get_reconciliation_engine, require_admin
from sgr.core.types import ReconciliationResult
from sgr.reconciliation.engine import ReconciliationEngine

router = APIRouter()


@router.post("/run", response_model=ReconciliationResult)
async def trigger_reconciliation(
    user: Annotated[TokenData, Depends(require_admin)],
    engine: Annotated[ReconciliationEngine, Depends(get_reconciliation_engine)],
) -> ReconciliationResult:
    """
    Löst manuell einen Reconciliation-Lauf aus (Exchange- vs. lokaler State).

    In PAPER/DRY_RUN liefert dies immer SKIPPED_NOT_LIVE zurück (siehe
    Modul-Docstring von ReconciliationEngine) - kein Fehler, sondern
    beabsichtigtes Verhalten.
    """
    return await engine.reconcile()
