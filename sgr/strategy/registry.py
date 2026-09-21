"""
SGR Strategy Registry
=====================
Zentrales Plugin-System für alle Strategien.

Wie Strategien registriert werden:
    @StrategyRegistry.register
    class TrendFollowingV1(BaseStrategy):
        name = "trend_following_v1"
        ...

Wie Strategien aktiviert/deaktiviert werden:
    registry.activate("trend_following_v1")
    registry.deactivate("trend_following_v1", reason="underperformance")

Automatische Deaktivierung durch Learning Loop:
    registry.deactivate_if_underperforming(performance)

Registry ist ein Singleton – alle Module teilen dieselbe Instanz.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sgr.core.logging import get_logger
from sgr.core.types import MarketRegime
from sgr.strategy.base import (
    StrategyPerformance,
    TradingStrategy,
    ValidationStatus,
)

if TYPE_CHECKING:
    from sgr.backtesting.types import BacktestResult

log = get_logger(__name__)


class StrategyEntry:
    """Registrierter Eintrag im Strategy Registry."""

    __slots__ = (
        "strategy",
        "is_active",
        "is_validated",
        "validation_status",
        "last_validation_result",
        "performance",
        "registered_at",
        "deactivation_reason",
    )

    def __init__(self, strategy: TradingStrategy) -> None:
        self.strategy = strategy
        self.is_active = False
        self.is_validated = False
        self.validation_status = ValidationStatus()
        # Rohwerte (Sharpe, Return, Drawdown, Trade-Count, ...) aus dem
        # letzten Validierungslauf - siehe StrategyValidationRunner. Getrennt
        # von validation_status (reine Boolean-Gates), da MonitoringEngine
        # diese konkreten Zahlen fuer Prometheus-Gauges braucht (Schritt 6:
        # "Sharpe/Return duerfen nicht nur geloggt, muessen als echte Metrik
        # sichtbar sein"). None bis zum ersten Validierungslauf.
        self.last_validation_result: BacktestResult | None = None
        self.performance: StrategyPerformance | None = None
        self.registered_at = datetime.now(tz=UTC)
        self.deactivation_reason: str | None = None


class StrategyRegistry:
    """
    Singleton Registry aller Strategien. Eine Instanz pro Tenant - siehe
    get_strategy_registry() weiter unten.

    BUG-FIX (Produktions-Audit, Tenant-Scoping): _entries war zuvor ein
    KLASSEN-Attribut (dict[str, StrategyEntry] = {}), nicht in __init__
    gesetzt - dadurch haetten sich SAEMTLICHE StrategyRegistry()-Instanzen
    (nicht nur die ueber .get() gecachte) denselben Dict geteilt, unabhaengig
    davon, ob sie fuer denselben Tenant gedacht waren oder nicht. Praktisch
    folgenlos bisher, weil ausnahmslos ueber .get()/StrategyRegistry.register
    zugegriffen wird (kein Call-Site konstruiert die Klasse direkt) - aber
    ein Instanz-Attribut ist trotzdem die korrekte Grundlage fuer
    Tenant-Scoping unten, analog zum bereits vorhandenen SymbolKillSwitch-
    Fix (siehe sgr/risk/symbol_kill_switch.py).

    Tenant-Scoping-Grenze (bewusst NICHT weiter aufgeloest): @register wird
    als Klassen-Decorator zur Modul-Importzeit ausgefuehrt, also BEVOR ein
    Tenant-Kontext ueberhaupt existiert - welche Strategie-KLASSEN existieren
    ist daher unvermeidbar prozessweiter Code-Zustand, kein Tenant-Datum.
    Nur der MUTABLE Zustand pro Eintrag (is_active, performance,
    validation_status) waere tenant-spezifisch. Dutzende bestehende
    Call-Sites (StrategyEngine, ExecutionEngine, ML-Engine,
    Validation-Runner, ...) rufen StrategyRegistry.get() ohne tenant_id auf
    - sie alle auf tenant_id umzustellen waere eine groessere
    Architekturaenderung (siehe analoge Design-Entscheidung im
    Leverage-Guard-Commit, nicht Teil dieses fokussierten Fixes) und ist
    heute durch OS-Prozesstrennung pro Tenant (ein Worker-Prozess pro
    Tenant, siehe SGRConfig.tenant_id Docstring) ohnehin sicher.
    get_strategy_registry(tenant_id=...) steht additiv fuer zukuenftigen,
    tenant-bewussten Code bereit; StrategyRegistry.get() bleibt unveraendert
    das Default-Tenant-(None)-Singleton, das @register befuellt.

    Thread-Safety: nur aus einem asyncio Event Loop verwenden.
    Strategien sind immutable nach Registrierung.
    """

    def __init__(self) -> None:
        self._entries: dict[str, StrategyEntry] = {}
        # Optional: StrategyRepository fuer Persistenz von
        # Aktivierung/Deaktivierung. None = rein in-memory (Tests,
        # Backtesting) - additiv, kein Pflichtfeld.
        self._strategy_repo: Any = None

    def inject_repository(self, repository: Any) -> None:
        """Injiziert StrategyRepository fuer Persistenz. Additiv, optional."""
        self._strategy_repo = repository

    async def get_active_names_from_db(self) -> list[str]:
        """
        Liest die Namen der beim letzten Shutdown aktiven Strategien aus
        der DB. Fuer RecoveryManager._restore_strategies(). Liefert eine
        leere Liste, wenn kein Repository injiziert wurde (z.B. Tests,
        Backtesting) - kein Fehler, einfach nichts wiederherzustellen.
        """
        if self._strategy_repo is None:
            return []
        result: list[str] = await self._strategy_repo.get_active_names()
        return result

    @classmethod
    def get(cls) -> StrategyRegistry:
        """Default-Tenant-(None)-Singleton - das @register befuellt, siehe
        Klassen-Docstring. Unveraendertes Verhalten fuer alle bestehenden
        Call-Sites; identisch zu get_strategy_registry() ohne tenant_id."""
        return get_strategy_registry(tenant_id=None)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    @classmethod
    def register(cls, strategy_class: type) -> type:
        """
        Decorator für Strategie-Registrierung.

        Usage:
            @StrategyRegistry.register
            class MyStrategy(BaseStrategy):
                name = "my_strategy"
                ...
        """
        instance = strategy_class()
        registry = cls.get()
        registry._register(instance)
        return strategy_class

    def _register(self, strategy: TradingStrategy) -> None:
        if strategy.name in self._entries:
            log.warning(
                "strategy_registry.duplicate",
                name=strategy.name,
                note="Overwriting existing registration",
            )

        self._entries[strategy.name] = StrategyEntry(strategy)
        log.info(
            "strategy_registry.registered",
            name=strategy.name,
            version=strategy.version,
            regimes=[r.value for r in strategy.supported_regimes],
        )

    def register_instance(self, strategy: TradingStrategy) -> None:
        """Registriert eine Strategie-Instanz direkt (für Tests)."""
        self._register(strategy)

    async def sync_registrations_to_db(self) -> None:
        """
        Schreibt alle aktuell registrierten Strategien in die DB
        (upsert). Explizit im Lifespan aufzurufen, NACH allen
        @StrategyRegistry.register-Decorators (die synchron zur
        Modul-Importzeit laufen, i.d.R. vor einem laufenden Event Loop -
        Persistenz kann dort nicht inline passieren).

        Best-effort pro Strategie: ein einzelner DB-Fehler blockiert
        nicht die Registrierung der uebrigen Strategien.
        """
        if self._strategy_repo is None:
            return
        for entry in self._entries.values():
            strategy = entry.strategy
            try:
                await self._strategy_repo.upsert(
                    name=strategy.name,
                    version=strategy.version,
                    supported_regimes=[r.value for r in strategy.supported_regimes],
                )
            except Exception as e:
                log.error(
                    "strategy_registry.sync_upsert_failed",
                    name=strategy.name,
                    error=str(e),
                )

    # ------------------------------------------------------------------
    # Activation / Deactivation
    # ------------------------------------------------------------------

    async def activate(self, name: str) -> None:
        """
        Aktiviert Strategie für Trading.
        Nur Strategien die Validierung bestanden haben sollten aktiviert werden.
        Persistiert best-effort in der DB (falls Repository injiziert) -
        Voraussetzung fuer Recovery nach einem Neustart.
        """
        entry = self._get_entry(name)
        entry.is_active = True
        entry.deactivation_reason = None
        log.info("strategy_registry.activated", name=name)
        await self._persist_active(name, True)

    async def deactivate(self, name: str, reason: str) -> None:
        """
        Deaktiviert Strategie. Kein Trading mehr bis manuelle Re-Aktivierung.
        reason wird für Audit-Trail gespeichert.
        """
        entry = self._get_entry(name)
        entry.is_active = False
        entry.deactivation_reason = reason
        log.warning(
            "strategy_registry.deactivated",
            name=name,
            reason=reason,
        )
        await self._persist_active(name, False, reason)

    async def _persist_active(self, name: str, is_active: bool, reason: str | None = None) -> None:
        """Best-effort DB-Persistenz des Aktivierungsstatus. Fail-safe."""
        if self._strategy_repo is None:
            return
        try:
            await self._strategy_repo.set_active(name, is_active, reason)
        except Exception as e:
            log.error(
                "strategy_registry.persist_active_failed",
                name=name,
                is_active=is_active,
                error=str(e),
            )

    def mark_validated(
        self,
        name: str,
        validation_status: ValidationStatus,
        backtest_result: BacktestResult | None = None,
    ) -> None:
        """
        Setzt Validierungsstatus (nach bestandenem Backtest etc.).

        backtest_result: optional, die vollen Rohwerte (Sharpe, Return,
        Drawdown, Trade-Count, ...) aus dem zugrundeliegenden Backtest -
        siehe StrategyEntry.last_validation_result Docstring. Optional statt
        Pflichtparameter, um bestehende Aufrufer (z.B. Tests, die nur den
        Status setzen wollen) nicht zu brechen.
        """
        entry = self._get_entry(name)
        entry.validation_status = validation_status
        entry.is_validated = validation_status.can_go_live
        entry.last_validation_result = backtest_result
        log.info(
            "strategy_registry.validated",
            name=name,
            can_go_live=validation_status.can_go_live,
        )

    async def update_performance(
        self,
        name: str,
        performance: StrategyPerformance,
    ) -> None:
        """
        Aktualisiert Performance-Metriken.
        Automatische Deaktivierung bei should_deactivate.
        """
        entry = self._get_entry(name)
        entry.performance = performance

        if performance.should_deactivate and entry.is_active:
            reason = (
                f"Auto-deactivated: Sharpe={performance.sharpe_ratio:.2f}, "
                f"HitRate={performance.hit_rate:.1%}, "
                f"PF={performance.profit_factor:.2f}"
            )
            await self.deactivate(name, reason)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_active(
        self,
        regime: MarketRegime | None = None,
    ) -> list[TradingStrategy]:
        """
        Gibt alle aktiven Strategien zurück.
        Bei regime != None: nur Strategien die dieses Regime unterstützen.
        """
        strategies = []
        for entry in self._entries.values():
            if not entry.is_active:
                continue
            if regime is not None and regime not in entry.strategy.supported_regimes:
                continue
            strategies.append(entry.strategy)
        return strategies

    def get_all(self) -> dict[str, StrategyEntry]:
        """Gibt alle registrierten Strategien zurück (aktiv + inaktiv)."""
        return dict(self._entries)

    def get_entry(self, name: str) -> StrategyEntry | None:
        return self._entries.get(name)

    def is_active(self, name: str) -> bool:
        entry = self._entries.get(name)
        return entry.is_active if entry else False

    def _get_entry(self, name: str) -> StrategyEntry:
        if name not in self._entries:
            raise KeyError(f"Strategy '{name}' not registered.")
        return self._entries[name]

    def clear(self) -> None:
        """Löscht alle Registrierungen (nur für Tests!)."""
        self._entries.clear()
        self._strategy_repo = None
        log.warning("strategy_registry.cleared", note="FOR TESTING ONLY")

    def unregister(self, name: str) -> None:
        """
        Entfernt EINEN Eintrag vollstaendig (im Unterschied zu
        deactivate(), das den Eintrag nur inaktiv setzt, aber behaelt).

        Fuer sgr.strategy.parameter_optimizer: eine Parameter-Suche
        registriert pro Kandidaten-Parametersatz eine temporaere Trial-
        Instanz unter einem synthetischen Namen (z.B.
        "mean_reversion_v1__trial_a1b2c3"), backtestet sie ueber den
        bestehenden BacktestingEngine-Pfad, und muss den Eintrag danach
        wieder entfernen - sonst wuerde jeder Optimierungslauf (9+
        Kandidaten pro Strategie, 718 Symbole) die Registry unbegrenzt
        mit Leichen-Eintraegen anwachsen lassen. clear() ist dafuer nicht
        geeignet (loescht ALLE Strategien, auch die echten produktiven).

        Kein Fehler, wenn name nicht existiert (idempotent - ein
        Cleanup-Aufruf nach einem bereits fehlgeschlagenen Trial darf
        nicht selbst scheitern)."""
        self._entries.pop(name, None)


# ---------------------------------------------------------------------------
# Singletons (eine Instanz pro Tenant - siehe Klassen-Docstring oben und
# das identische Muster in sgr/risk/kill_switch.py._kill_switches /
# sgr/risk/symbol_kill_switch.py._symbol_kill_switches)
# ---------------------------------------------------------------------------

_registries: dict[str | None, StrategyRegistry] = {}


def get_strategy_registry(tenant_id: str | None = None) -> StrategyRegistry:
    if tenant_id not in _registries:
        _registries[tenant_id] = StrategyRegistry()
    return _registries[tenant_id]
