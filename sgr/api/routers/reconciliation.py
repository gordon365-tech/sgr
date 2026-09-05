"""
SGR Reconciliation Router
==========================
Manuelles Auslösen eines Reconciliation-Laufs - AKTUELL AUSSER BETRIEB (501).

Architektur-Hintergrund: ReconciliationEngine.reconcile() fragt den
tatsächlichen Exchange-State ab (Live-Netzwerk-Call, kein reiner
DB/Redis-Read) - dieselbe Kategorie Problem wie der Ticker- und der
manuelle Cycle-Trigger-Endpunkt. Ein Exchange-Adapter-Aufruf direkt aus
dem API-Prozess widerspricht der sgr-api Read-Only-Zielarchitektur.
Bewusst nicht ersatzlos entfernt (Reconciliation-Läufe bleiben ein
legitimes operatives Werkzeug), sondern als 501 markiert, bis derselbe
Command-Channel zum Worker (siehe trading.py) auch hierfür genutzt
werden kann.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from sgr.api.dependencies import TokenData, require_admin

router = APIRouter()


@router.post("/run")
async def trigger_reconciliation(
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict:
    """
    Löst manuell einen Reconciliation-Lauf aus (Exchange- vs. lokaler State).

    Noch nicht auf die sgr-api Read-Only-Zielarchitektur migriert (siehe
    Modul-Docstring) - liefert bewusst 501 statt einen Live-Exchange-Call
    direkt aus dem API-Prozess auszuführen.
    """
    raise HTTPException(
        status_code=501,
        detail=(
            "Manual reconciliation trigger not yet migrated to the "
            "read-only API architecture. Querying live exchange state "
            "directly from the API process is no longer permitted; a "
            "Redis-backed command channel to sgr-worker is planned as "
            "a follow-up (see trading.py)."
        ),
    )
