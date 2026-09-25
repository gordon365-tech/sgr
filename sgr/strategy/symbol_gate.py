"""
SGR Symbol Strategy Gate
===========================
Production-Integration (Autonomous-Strategy-Universe-Rollout, Phase 12)
fuer die pro-Symbol-Validierungsergebnisse aus
sgr/strategy/symbol_validation_runner.py.

Additiver Zusatzfilter in StrategyEngine.process() (siehe dortige
Verwendung): die bestehende Kandidatenliste (bereits global aktive
Strategien via StrategyRegistry.get_active(regime=...)) wird zusaetzlich
gegen die per-Symbol-Validierungsergebnisse geprueft.

Fallback-Prinzip (WICHTIG - kein Bruch der laufenden Produktion):
    - Kein Batch-Ergebnis fuer dieses Symbol vorhanden -> Gate erlaubt
      alles (bestehendes Verhalten unveraendert). Ein frisch entdecktes
      oder noch nicht batch-validiertes Symbol darf nicht durch
      fehlende Daten blockiert werden.
    - Ergebnis vorhanden, Status=ACTIVE mit Strategie X -> NUR X wird
      fuer dieses Symbol erlaubt, alle anderen global aktiven
      Strategien werden fuer DIESES Symbol blockiert (nicht global).
    - Ergebnis vorhanden, aber kein Status=ACTIVE (z.B.
      NO_VALID_STRATEGY/INSUFFICIENT_DATA) -> alle Strategien fuer
      dieses Symbol blockiert. Das ist die eigentliche Wirkung von
      Phase 12: "keine Umgehung von mark_validated()" bedeutet hier,
      dass ein Symbol ohne bestandene Validierung keine Signale
      generieren darf, selbst wenn die Strategie GLOBAL (fuer andere
      Symbole) aktiv ist.

In-Memory-Cache mit periodischem Refresh (analog FeatureStore/
ticker_cache-Pattern) statt einer DB-Query pro
StrategyEngine.process()-Aufruf (der bei 500+ Symbolen sehr haeufig
feuert) - ein Cache-Miss/veralteter Cache blockiert NIE den Live-Pfad
(fail-open, siehe refresh() Fehlerbehandlung).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sgr.core.logging import get_logger

log = get_logger(__name__)

_REFRESH_INTERVAL = timedelta(minutes=15)


class SymbolStrategyGate:
    """Singleton-artige Instanz (pro Worker-Prozess) - siehe get()."""

    _instance: SymbolStrategyGate | None = None

    def __init__(self) -> None:
        # symbol (ccxt-Form, z.B. "BTC/USDT") -> Name der aktiven
        # Strategie, oder None wenn validiert aber KEINE Strategie
        # geeignet war. Ein Symbol, das NICHT in diesem Dict vorkommt,
        # wurde noch nie batch-validiert (permissiver Fallback).
        self._active_strategy_by_symbol: dict[str, str | None] = {}
        self._last_refreshed_at: datetime | None = None
        self._exchange = "binance"
        self._timeframe = "1h"

    @classmethod
    def get(cls) -> SymbolStrategyGate:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def is_allowed(self, symbol: str, strategy_name: str) -> bool:
        """
        Synchron, in-memory - sicher aus dem heissen Pfad von
        StrategyEngine.process() aufrufbar (kein I/O).

        symbol: settle-freie kanonische Form ("BTC/USDT", siehe
        Symbol.ccxt_symbol) - identisch zur Form in
        StrategySymbolValidationModel.symbol.
        """
        if symbol not in self._active_strategy_by_symbol:
            return True  # kein Batch-Ergebnis -> bestehendes Verhalten
        return self._active_strategy_by_symbol[symbol] == strategy_name

    def has_result_for(self, symbol: str) -> bool:
        return symbol in self._active_strategy_by_symbol

    def stale(self) -> bool:
        if self._last_refreshed_at is None:
            return True
        return datetime.now(tz=UTC) - self._last_refreshed_at > _REFRESH_INTERVAL

    async def refresh_if_stale(self) -> None:
        if not self.stale():
            return
        await self.refresh()

    async def refresh(self) -> None:
        """Fail-open: ein DB-Fehler beim Refresh darf den bestehenden
        (moeglicherweise leicht veralteten) Cache-Stand NICHT loeschen -
        das wuerde alle Symbole ploetzlich als "unbekannt" (permissiv)
        erscheinen lassen bzw. im schlimmsten Fall den gesamten
        Live-Pfad blockieren, wenn hier stattdessen faelschlich
        restriktiv reagiert wuerde."""
        try:
            from sgr.core.repositories import get_repositories

            repo = get_repositories().strategy_symbol_validations
            rows = await repo.get_best_by_symbol(limit=5000)
            new_map: dict[str, str | None] = {}
            for row in rows:
                if row["exchange"] != self._exchange or row["timeframe"] != self._timeframe:
                    continue
                new_map[row["symbol"]] = row["strategy"] if row["status"] == "active" else None
            self._active_strategy_by_symbol = new_map
            self._last_refreshed_at = datetime.now(tz=UTC)
            log.info("symbol_gate.refreshed", symbols_known=len(new_map))
        except Exception as e:
            log.warning("symbol_gate.refresh_failed", error=str(e))
