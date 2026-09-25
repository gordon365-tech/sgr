"""
SGR Symbol Kill Switch
======================
Deaktiviert ein einzelnes Handelssymbol, ohne das gesamte System zu
stoppen (im Gegensatz zum globalen KillSwitch in kill_switch.py, der
den kompletten Trading Mode stoppt).

Warum getrennt vom globalen KillSwitch?
    Der globale KillSwitch ist ein Notfall-Stopp für alle Trading-
    Aktivitäten (Portfolio-Drawdown, Daily Loss, Leverage-Überschreitung
    etc.) - er trifft absichtlich das gesamte System. Für lokalisierte
    Probleme (ein einzelnes Symbol zeigt anomales Verhalten, eine
    Exchange-spezifische Störung, ein manueller Trading-Stopp für ein
    bestimmtes Paar) wäre ein globaler Stopp unverhältnismäßig - alle
    anderen Symbole würden unnötig blockiert.

Analogie zu StrategyRegistry:
    Dieses Modul folgt bewusst demselben Muster wie
    sgr/strategy/registry.py (In-Memory-Singleton, optionale DB-
    Persistenz via injiziertem Repository, Audit-Log bei jeder
    Zustandsänderung, kein Auto-Reset) - für Konsistenz im Codebase
    und weil sich das Muster dort bereits bewährt hat.

Integrationspunkt:
    TradingOrchestrator.run_cycle() prüft is_active(symbol_key) als
    ALLERERSTEN Schritt, noch vor der Signal-Generierung - ein
    deaktiviertes Symbol erzeugt gar nicht erst ein Signal, das später
    durch die Risk Engine laufen müsste. Kein Trading-Pfad kann diesen
    Check umgehen, da run_cycle() der einzige Einstiegspunkt für einen
    Trading-Zyklus ist.

Persistenz:
    Wie bei StrategyRegistry: rein in-memory per Default (ausreichend
    für Tests/Backtesting), mit optionaler, best-effort DB-Persistenz
    über ein injiziertes Repository für Restart-Sicherheit. Ohne
    injiziertes Repository ist ein deaktiviertes Symbol nach einem
    Neustart wieder aktiv - das ist ein bewusster Trade-off (wie bei
    StrategyRegistry), kein Bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sgr.core.logging import audit_log, get_logger

log = get_logger(__name__)


@dataclass
class SymbolKillSwitchEntry:
    """Zustand des Kill Switch für ein einzelnes Symbol."""

    symbol_key: str
    is_active: bool = True  # True = Trading erlaubt (Default: kein Block)
    deactivated_at: datetime | None = None
    reason: str | None = None
    deactivated_by: str | None = None


class SymbolKillSwitch:
    """
    Registry für symbolspezifische Trading-Deaktivierung. Eine Instanz pro
    (Tenant, Prozess) - siehe get_symbol_kill_switch() weiter unten.

    Tenant-Scoping (Produktions-Audit-Fix): frueher ein reines Prozess-
    Singleton ohne tenant_id, analog zur urspruenglichen KillSwitch-
    Implementierung vor deren eigenem Tenant-Scoping-Fix (siehe
    sgr/risk/kill_switch.py Kommentar bei _kill_switches). Sicher nur
    dank OS-Prozesstrennung pro Tenant (siehe SGRConfig.tenant_id
    Docstring) - kein aktiver Exploit heute, aber ein Instanz-Leak WUERDE
    entstehen, falls ein Prozess jemals mehrere Tenants gleichzeitig
    bedienen wuerde. Wie beim KillSwitch-Fix: additiv, kein bestehender
    Call-Site muss tenant_id angeben (Default None = unveraendertes
    Verhalten, siehe get_symbol_kill_switch()).

    Thread-Safety: nur aus einem asyncio Event Loop verwenden (wie
    StrategyRegistry - keine expliziten Locks nötig, da Python's GIL
    einzelne dict-Operationen atomar macht und hier keine Multi-Step-
    Read-Modify-Write-Race vorliegt, die durch fehlendes Lock zu
    einem inkonsistenten Zustand führen könnte).
    """

    def __init__(self, tenant_id: str | None = None) -> None:
        self._tenant_id = tenant_id
        self._entries: dict[str, SymbolKillSwitchEntry] = {}
        # Optional: Repository für Persistenz. None = rein in-memory,
        # additiv wie bei StrategyRegistry.inject_repository().
        self._repo: Any = None
        # Deaktivierungs-Hooks (2026-09-23, Phase G - Grid-Architekturbericht):
        # SymbolKillSwitch selbst kennt weder GridController noch
        # ExecutionEngine (identisches Entkopplungsprinzip wie KillSwitch/
        # PositionLiquidator, siehe dortigen Modul-Docstring "circular dep
        # vermeiden"). Ein registrierter Hook wird bei jeder deactivate()
        # best-effort aufgerufen (symbol_key, reason) - GridController.
        # close_all_grids_for_symbol() ist der einzige aktuelle Konsument
        # (siehe Aufrufstelle in sgr/api/main.py), aber der Mechanismus
        # selbst ist generisch/additiv, kein Grid-Spezialfall im Kern.
        self._on_deactivated_hooks: list[Any] = []

    def inject_repository(self, repository: Any) -> None:
        """Injiziert ein Repository für Persistenz. Additiv, optional."""
        self._repo = repository

    def register_deactivation_hook(self, hook: Any) -> None:
        """
        Registriert eine async Callback (symbol_key: str, reason: str) ->
        None, die bei JEDER deactivate() best-effort aufgerufen wird -
        NACH dem In-Memory-State-Change und der Persistenz, damit ein
        Hook-Fehler den eigentlichen Deaktivierungs-Vorgang nicht
        beeintraechtigt (identisches Fail-Safe-Prinzip wie ueberall sonst
        in dieser Klasse).
        """
        self._on_deactivated_hooks.append(hook)

    # ------------------------------------------------------------------
    # Query (synchron, hot path)
    # ------------------------------------------------------------------

    def is_active(self, symbol_key: str) -> bool:
        """
        True = Trading für dieses Symbol erlaubt.
        Symbole ohne expliziten Eintrag sind per Default aktiv (kein
        Opt-in nötig, um ein neues Symbol zu handeln).
        """
        entry = self._entries.get(symbol_key)
        return entry.is_active if entry else True

    def get_entry(self, symbol_key: str) -> SymbolKillSwitchEntry | None:
        return self._entries.get(symbol_key)

    def get_all(self) -> dict[str, SymbolKillSwitchEntry]:
        """Gibt alle Symbole mit explizitem Eintrag zurück (aktiv + inaktiv)."""
        return dict(self._entries)

    def get_deactivated(self) -> list[str]:
        """Gibt alle aktuell deaktivierten Symbol-Keys zurück."""
        return [key for key, entry in self._entries.items() if not entry.is_active]

    # ------------------------------------------------------------------
    # Mutation (async wegen optionaler DB-Persistenz)
    # ------------------------------------------------------------------

    async def deactivate(
        self,
        symbol_key: str,
        reason: str,
        deactivated_by: str = "system",
    ) -> None:
        """
        Deaktiviert Trading für ein Symbol. Kein automatischer Trading-
        Zyklus wird mehr für dieses Symbol ausgeführt, bis manuelle
        Re-Aktivierung. Idempotent: erneutes Deaktivieren aktualisiert
        nur reason/deactivated_by, kein Fehler.
        """
        entry = self._entries.get(symbol_key)
        if entry is None:
            entry = SymbolKillSwitchEntry(symbol_key=symbol_key)
            self._entries[symbol_key] = entry

        entry.is_active = False
        entry.deactivated_at = datetime.now(tz=UTC)
        entry.reason = reason
        entry.deactivated_by = deactivated_by

        log.warning(
            "symbol_kill_switch.deactivated",
            symbol_key=symbol_key,
            reason=reason,
            deactivated_by=deactivated_by,
        )
        audit_log.log_auth_event(
            event="symbol_kill_switch_deactivated",
            user_id=deactivated_by,
            ip_address="system",
            success=True,
        )
        await self._persist(symbol_key, False, reason)

        for hook in self._on_deactivated_hooks:
            try:
                await hook(symbol_key, reason)
            except Exception as e:
                log.error(
                    "symbol_kill_switch.deactivation_hook_failed",
                    symbol_key=symbol_key,
                    error=str(e),
                )

    async def activate(self, symbol_key: str, activated_by: str = "system") -> None:
        """
        Re-aktiviert Trading für ein zuvor deaktiviertes Symbol.
        Kein Effekt, wenn das Symbol bereits aktiv ist (oder nie
        deaktiviert wurde).
        """
        entry = self._entries.get(symbol_key)
        if entry is None or entry.is_active:
            log.info("symbol_kill_switch.already_active", symbol_key=symbol_key)
            return

        entry.is_active = True
        entry.deactivated_at = None
        entry.reason = None
        entry.deactivated_by = None

        log.warning(
            "symbol_kill_switch.reactivated",
            symbol_key=symbol_key,
            activated_by=activated_by,
        )
        audit_log.log_auth_event(
            event="symbol_kill_switch_reactivated",
            user_id=activated_by,
            ip_address="system",
            success=True,
        )
        await self._persist(symbol_key, True, None)

    async def _persist(self, symbol_key: str, is_active: bool, reason: str | None) -> None:
        """Best-effort DB-Persistenz. Fail-safe: Fehler hier dürfen den
        In-Memory-State (bereits gesetzt) nicht rückgängig machen."""
        if self._repo is None:
            return
        try:
            await self._repo.set_symbol_active(symbol_key, is_active, reason)
        except Exception as e:
            log.error(
                "symbol_kill_switch.persist_failed",
                symbol_key=symbol_key,
                is_active=is_active,
                error=str(e),
            )


# ---------------------------------------------------------------------------
# Singletons (eine Instanz pro Tenant - siehe Klassen-Docstring oben und
# das identische Muster in sgr/risk/kill_switch.py._kill_switches)
# ---------------------------------------------------------------------------

_symbol_kill_switches: dict[str | None, SymbolKillSwitch] = {}


def get_symbol_kill_switch(tenant_id: str | None = None) -> SymbolKillSwitch:
    if tenant_id not in _symbol_kill_switches:
        _symbol_kill_switches[tenant_id] = SymbolKillSwitch(tenant_id=tenant_id)
    return _symbol_kill_switches[tenant_id]
