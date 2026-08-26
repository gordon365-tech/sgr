"""
SGR Reconciliation Engine (Phase 7B)
=====================================
Gleicht den lokalen Portfolio-State (PortfolioEngine, DB-gestützt via
PositionRepository) gegen den tatsächlichen Positions-State auf der Exchange
ab.

Warum dieses Modul nötig war:
    PositionRepository (Persistenz) und ExchangeAdapter.get_positions()
    (Exchange-Abfrage) existierten beide bereits vollständig implementiert,
    aber nichts im System verglich die beiden Quellen miteinander. Ein
    Split-Brain-Fall (Order auf der Exchange ausgeführt, aber lokaler
    DB-Write fehlgeschlagen) wäre unentdeckt geblieben - die dokumentierte
    execution_engine.SPLIT_BRAIN_RISK-Klassifizierung existierte nur als
    Kommentar, nirgends als tatsächlicher Log-Eintrag.

Warum nur in LIVE sinnvoll:
    Pionex besitzt kein Testnet. Paper Mode verwendet Dummy-Credentials
    ohne authentifizierte Session (siehe pionex.py). Ein Abgleich gegen
    Exchange-Positionen im Paper Mode würde entweder eine leere Liste
    liefern (Spot: get_positions() gibt [] zurück) oder mit
    Authentifizierungsfehlern fehlschlagen - beides ohne Aussagekraft.
    Reconciliation ist daher fail-safe deaktiviert außerhalb von LIVE
    (Status SKIPPED_NOT_LIVE), nicht implizit übersprungen.

Architekturprinzip: rein lesend, keine Korrektur
    Dieses Modul VERÄNDERT niemals State - weder lokal noch auf der
    Exchange. Es erkennt und meldet Abweichungen (Event + strukturiertes
    Log), überlässt die Entscheidung über eine Korrektur aber bewusst
    einer höheren Instanz (Operator / künftige automatisierte Korrektur-
    Policy). Automatische Selbstkorrektur ohne menschliche Bestätigung
    wäre bei widersprüchlichen Datenquellen selbst ein Risiko (z.B.
    könnte eine fälschlich als "missing locally" erkannte Position durch
    einen zwischenzeitlich veralteten Snapshot entstehen).

Fail-Safe:
    Jede Exception (Exchange-Fehler, Timeout, etc.) führt zu
    ReconciliationStatus.FAILED, niemals zu einer unbehandelten Exception.
    Reconciliation-Fehler dürfen laut Projektgrundsatz den laufenden
    Trading-Betrieb nicht beeinflussen - dieses Modul wird additiv
    aufgerufen (Scheduler/manueller Trigger), nie im kritischen Pfad von
    run_cycle().

Ablauf:
    1. LIVE-Check (sonst: SKIPPED_NOT_LIVE)
    2. Lokale offene Positionen laden (PortfolioEngine, aktueller State)
    3. Exchange-Positionen abfragen (ExchangeAdapter.get_positions())
    4. Pro Symbol vergleichen: MATCHED / QUANTITY_MISMATCH /
       MISSING_LOCALLY / MISSING_ON_EXCHANGE
    5. ReconciliationCompletedEvent publizieren (additiv, fail-safe isoliert)
    6. Bei MISSING_LOCALLY: strukturiertes SPLIT_BRAIN_RISK-Log
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sgr.core.event_bus import get_event_bus
from sgr.core.logging import get_logger
from sgr.core.types import (
    DiscrepancyType,
    ExchangeID,
    PositionDiscrepancy,
    ReconciliationCompletedEvent,
    ReconciliationResult,
    ReconciliationStatus,
    TradingMode,
)

log = get_logger(__name__)

# Toleranz für Mengen-Vergleich: Rundungsdifferenzen durch Exchange-Precision
# (z.B. Lot-Size-Rundung) sollen nicht als Abweichung gemeldet werden.
_QUANTITY_TOLERANCE = Decimal("0.00000001")


class ReconciliationEngine:
    """
    Gleicht lokale Positionen gegen Exchange-Positionen ab.

    Usage:
        engine = ReconciliationEngine(
            exchange_pool=pool,
            portfolio_engine=portfolio_engine,
            trading_mode=TradingMode.LIVE,
            exchange_id=ExchangeID.PIONEX,
        )
        result = await engine.reconcile()
    """

    def __init__(
        self,
        exchange_pool: Any,
        portfolio_engine: Any,
        trading_mode: TradingMode,
        exchange_id: ExchangeID = ExchangeID.PIONEX,
    ) -> None:
        self._pool = exchange_pool
        self._portfolio_engine = portfolio_engine
        self._trading_mode = trading_mode
        self._exchange_id = exchange_id

    # ------------------------------------------------------------------
    # Main Entry Point
    # ------------------------------------------------------------------

    async def reconcile(self) -> ReconciliationResult:
        """
        Führt einen vollständigen Reconciliation-Lauf aus.
        Fail-Safe: jede unerwartete Exception -> ReconciliationStatus.FAILED,
        niemals eine unbehandelte Exception nach außen.
        """
        started_at = datetime.now(tz=UTC)

        if self._trading_mode != TradingMode.LIVE:
            log.info(
                "reconciliation.skipped_not_live",
                trading_mode=self._trading_mode.value,
            )
            result = ReconciliationResult(
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
                status=ReconciliationStatus.SKIPPED_NOT_LIVE,
                trading_mode=self._trading_mode,
                exchange=self._exchange_id,
            )
            await self._publish_completed(result)
            return result

        try:
            result = await self._reconcile_internal(started_at)
        except Exception as e:
            log.error(
                "reconciliation.unexpected_error",
                exchange=self._exchange_id.value,
                error=str(e),
                exc_info=True,
            )
            result = ReconciliationResult(
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
                status=ReconciliationStatus.FAILED,
                trading_mode=self._trading_mode,
                exchange=self._exchange_id,
                error=f"Reconciliation error: {e}",
            )

        await self._publish_completed(result)
        self._log_split_brain_risk(result)
        return result

    async def _reconcile_internal(self, started_at: datetime) -> ReconciliationResult:
        # 1. Lokaler State (aktueller in-memory Stand der PortfolioEngine -
        #    bereits per restore_from_persistence() beim Startup mit der
        #    DB synchronisiert, daher hier keine zusätzliche DB-Abfrage
        #    nötig; single source of truth bleibt die PortfolioEngine).
        local_positions = {
            str(p.symbol): p for p in self._portfolio_engine.positions
        }

        # 2. Exchange State
        adapter = self._pool.get(self._exchange_id, self._trading_mode)
        exchange_positions_list = await adapter.get_positions()
        exchange_positions = {str(p.symbol): p for p in exchange_positions_list}

        # 3. Vergleich (Union aller Symbole aus beiden Quellen)
        all_symbol_keys = set(local_positions) | set(exchange_positions)
        discrepancies: list[PositionDiscrepancy] = []

        for symbol_key in sorted(all_symbol_keys):
            local = local_positions.get(symbol_key)
            remote = exchange_positions.get(symbol_key)
            discrepancy = self._compare(symbol_key, local, remote)
            if discrepancy is not None:
                discrepancies.append(discrepancy)

        status = (
            ReconciliationStatus.DISCREPANCIES_FOUND
            if discrepancies
            else ReconciliationStatus.CLEAN
        )

        return ReconciliationResult(
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            status=status,
            trading_mode=self._trading_mode,
            exchange=self._exchange_id,
            checked_symbols=len(all_symbol_keys),
            discrepancies=discrepancies,
        )

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def _compare(
        self,
        symbol_key: str,
        local: Any,
        remote: Any,
    ) -> PositionDiscrepancy | None:
        """
        Vergleicht eine lokale und eine Exchange-Position für ein Symbol.
        Returns: None bei MATCHED (keine Abweichung wird nicht in die
        Ergebnisliste aufgenommen, um sie kurz zu halten - checked_symbols
        zählt trotzdem alle geprüften Symbole).
        """
        if local is not None and remote is None:
            return PositionDiscrepancy(
                symbol_key=symbol_key,
                discrepancy_type=DiscrepancyType.MISSING_ON_EXCHANGE,
                local_quantity=local.quantity,
                local_side=local.side,
            )

        if local is None and remote is not None:
            return PositionDiscrepancy(
                symbol_key=symbol_key,
                discrepancy_type=DiscrepancyType.MISSING_LOCALLY,
                exchange_quantity=remote.quantity,
                exchange_side=remote.side,
            )

        if local is None and remote is None:
            # Kann durch die Union-Konstruktion nicht auftreten, defensiv.
            return None

        # Beide vorhanden: Menge (und Side) vergleichen
        qty_diff = abs(local.quantity - remote.quantity)
        if qty_diff > _QUANTITY_TOLERANCE or local.side != remote.side:
            return PositionDiscrepancy(
                symbol_key=symbol_key,
                discrepancy_type=DiscrepancyType.QUANTITY_MISMATCH,
                local_quantity=local.quantity,
                exchange_quantity=remote.quantity,
                local_side=local.side,
                exchange_side=remote.side,
            )

        return None

    # ------------------------------------------------------------------
    # Logging & Events
    # ------------------------------------------------------------------

    def _log_split_brain_risk(self, result: ReconciliationResult) -> None:
        """
        Strukturiertes Log für jede MISSING_LOCALLY-Abweichung - der Fall,
        in dem eine Exchange-Order existiert, aber der lokale DB/In-Memory-
        State nie davon erfahren hat. Bisher nur in Kommentaren/Docstrings
        dokumentiert (siehe execution/engine.py Modul-Docstring), aber nie
        tatsächlich geloggt.
        """
        for d in result.discrepancies:
            if d.discrepancy_type == DiscrepancyType.MISSING_LOCALLY:
                log.error(
                    "execution_engine.SPLIT_BRAIN_RISK",
                    symbol=d.symbol_key,
                    exchange_quantity=str(d.exchange_quantity),
                    exchange_side=d.exchange_side.value if d.exchange_side else None,
                    trading_mode=self._trading_mode.value,
                    exchange=self._exchange_id.value,
                    detail=(
                        "Position existiert auf der Exchange, ist aber lokal "
                        "unbekannt. Möglicher DB-Write-Fehler nach Order-Fill "
                        "oder verpasstes Event. Manuelle Prüfung erforderlich."
                    ),
                )
            elif d.discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH:
                log.warning(
                    "reconciliation.quantity_mismatch",
                    symbol=d.symbol_key,
                    local_quantity=str(d.local_quantity),
                    exchange_quantity=str(d.exchange_quantity),
                    trading_mode=self._trading_mode.value,
                )
            elif d.discrepancy_type == DiscrepancyType.MISSING_ON_EXCHANGE:
                log.warning(
                    "reconciliation.missing_on_exchange",
                    symbol=d.symbol_key,
                    local_quantity=str(d.local_quantity),
                    trading_mode=self._trading_mode.value,
                    detail=(
                        "Position lokal offen, aber nicht mehr auf der "
                        "Exchange. Möglicherweise außerhalb SGR geschlossen."
                    ),
                )

    async def _publish_completed(self, result: ReconciliationResult) -> None:
        try:
            event = ReconciliationCompletedEvent(
                timestamp=datetime.now(tz=UTC),
                result=result,
            )
            await get_event_bus().publish(event)
        except Exception as e:
            log.error("reconciliation.publish_completed_failed", error=str(e))
