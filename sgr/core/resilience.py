"""
Graceful Shutdown & Circuit Breaker
====================================
Handles crash recovery & exchange outage resilience.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sgr.core.logging import get_logger
from sgr.core.types import TradingMode

log = get_logger(__name__)


class CircuitBreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Prevents cascading failures when external service (Exchange) is down.

    States:
        CLOSED: Normal operation, all requests pass through
        OPEN: Service failed threshold, reject all requests immediately
        HALF_OPEN: Testing if service recovered, allow one request
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: int = 60,
        success_threshold: int = 2,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.success_threshold = success_threshold

        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: datetime | None = None

    async def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """
        Executes function through circuit breaker.

        Args:
            func: Async function to call
            *args, **kwargs: Arguments to pass

        Returns:
            Function result or CircuitBreakerError
        """
        if self.state == CircuitBreakerState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitBreakerState.HALF_OPEN
                self.success_count = 0
                log.info(
                    f"circuitbreaker.{self.name}.half_open",
                    failure_count=self.failure_count,
                )
            else:
                raise CircuitBreakerError(f"Circuit breaker '{self.name}' is OPEN")

        try:
            result = await func(*args, **kwargs)

            if self.state == CircuitBreakerState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    self._reset()

            return result

        except Exception:
            self.failure_count += 1
            self.last_failure_time = datetime.utcnow()

            if self.state == CircuitBreakerState.HALF_OPEN:
                self.state = CircuitBreakerState.OPEN
                log.error(
                    f"circuitbreaker.{self.name}.reopened",
                    failure_count=self.failure_count,
                )
            elif self.failure_count >= self.failure_threshold:
                self.state = CircuitBreakerState.OPEN
                log.error(
                    f"circuitbreaker.{self.name}.opened",
                    failure_count=self.failure_count,
                    threshold=self.failure_threshold,
                )

            raise

    def _should_attempt_reset(self) -> bool:
        """Check if timeout has elapsed since last failure."""
        if not self.last_failure_time:
            return False
        elapsed = datetime.utcnow() - self.last_failure_time
        return elapsed >= timedelta(seconds=self.recovery_timeout_seconds)

    def _reset(self) -> None:
        """Reset circuit breaker to closed state."""
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        log.info(f"circuitbreaker.{self.name}.closed")

    def get_state(self) -> dict[str, Any]:
        """Returns circuit breaker state for monitoring."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None,
        }


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open."""
    pass


class GracefulShutdownManager:
    """
    Manages graceful shutdown of services.

    Sequence:
    1. Stop accepting new requests
    2. Wait for in-flight requests to complete (30s timeout)
    3. Close all positions (trading only)
    4. Flush metrics
    5. Shutdown
    """

    def __init__(self, grace_period_seconds: int = 30) -> None:
        self.grace_period_seconds = grace_period_seconds
        self.shutdown_event = asyncio.Event()
        self.active_tasks: set[asyncio.Task] = set()

    async def shutdown(self) -> None:
        """Initiates graceful shutdown."""
        log.info("shutdown.initiated", grace_period=self.grace_period_seconds)

        self.shutdown_event.set()

        try:
            await asyncio.wait_for(
                self._wait_for_active_tasks(),
                timeout=self.grace_period_seconds,
            )
        except TimeoutError:
            log.warning(
                "shutdown.timeout",
                remaining_tasks=len(self.active_tasks),
            )

        log.info("shutdown.complete")

    async def _wait_for_active_tasks(self) -> None:
        """Waits for all active tasks to complete."""
        while self.active_tasks:
            done, self.active_tasks = await asyncio.wait(
                self.active_tasks,
                timeout=1,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                try:
                    task.result()
                except Exception as e:
                    log.error("shutdown.task_error", error=str(e))

    def register_task(self, task: asyncio.Task) -> None:
        """Registers a task for graceful shutdown tracking."""
        self.active_tasks.add(task)
        task.add_done_callback(self.active_tasks.discard)

    async def close_all_positions(self) -> None:
        """Closes all open positions before shutdown."""
        log.info("shutdown.closing_positions")
        # Implementation would close all positions
        # Imported from portfolio engine
        await asyncio.sleep(1)  # Placeholder


class RecoveryManager:
    """
    Koordiniert Wiederherstellung des Systemzustands nach einem Crash /
    ungeplanten Neustart.

    Delegiert an die bereits existierenden, echten Restore-Mechanismen
    statt sie zu duplizieren:
        1. Positionen: PortfolioEngine.restore_from_persistence()
           (fail-closed, existiert bereits, ist im Lifespan verdrahtet -
           dieser Schritt ruft dieselbe Instanz auf, dupliziert sie nicht)
        2. Offene Orders: OrderRepository.get_open_orders() liest den
           zuletzt bekannten Order-State. SGR kann Orders nicht "wieder-
           herstellen" im Sinne von erneut einreichen (das waere ein
           Duplicate-Order-Risiko) - stattdessen werden offene Orders
           geloggt und sollten per naechstem ReconciliationEngine-Lauf
           (Phase 7B) gegen den tatsaechlichen Exchange-Status abgeglichen
           werden. Reine Bestandsaufnahme, keine automatische Aktion.
        3. Strategien: StrategyRepository.get_active_names() + jeweils
           StrategyRegistry.activate() fuer jede zuvor aktive Strategie.

    Frueher (vor diesem Fix) waren alle drei Schritte auskommentierter
    Pseudo-Code ohne Wirkung - dieser Fund wurde dokumentiert und jetzt
    aufgeloest.
    """

    def __init__(
        self,
        portfolio_engine: Any,
        order_repository: Any,
        strategy_registry: Any,
        trading_mode: TradingMode,
        redis_client: Any = None,
        tenant_id: str | None = None,
    ) -> None:
        self._portfolio_engine = portfolio_engine
        self._order_repo = order_repository
        self._registry = strategy_registry
        self._trading_mode = trading_mode
        # Fuer _restore_kill_switch() (siehe dortigen Docstring) - optional
        # wie die drei anderen Injektionen: None = dieser Schritt wird
        # uebersprungen (z.B. isolierte Tests ohne Redis), kein Pflichtfeld.
        self._redis_client = redis_client
        self._tenant_id = tenant_id

    async def recover_after_crash(self) -> bool:
        """
        Stellt Systemzustand nach ungeplantem Neustart wieder her.
        Fail-safe insgesamt: jeder einzelne Schritt wird versucht, ein
        Fehler in einem Schritt bricht recover_after_crash() nicht
        vollstaendig ab, wird aber im Rueckgabewert reflektiert.

        Returns:
            True nur wenn ALLE vier Schritte erfolgreich waren.
        """
        log.info("recovery.started")

        positions_ok = await self._restore_positions()
        orders_ok = await self._restore_orders()
        strategies_ok = await self._restore_strategies()
        kill_switch_ok = await self._restore_kill_switch()

        success = positions_ok and orders_ok and strategies_ok and kill_switch_ok
        if success:
            log.info("recovery.complete")
        else:
            log.warning(
                "recovery.partial_or_failed",
                positions_ok=positions_ok,
                orders_ok=orders_ok,
                strategies_ok=strategies_ok,
                kill_switch_ok=kill_switch_ok,
            )
        return success

    async def _restore_positions(self) -> bool:
        """
        Delegiert an PortfolioEngine.restore_from_persistence() - die
        echte, fail-closed Implementierung. Wird hier NICHT dupliziert.
        """
        log.info("recovery.restoring_positions")
        try:
            await self._portfolio_engine.restore_from_persistence()
            return True
        except Exception as e:
            log.error("recovery.restore_positions_failed", error=str(e))
            return False

    async def _restore_orders(self) -> bool:
        """
        Liest offene Orders aus der DB (Bestandsaufnahme, keine
        automatische Aktion - siehe Klassendocstring). Die eigentliche
        Abstimmung mit dem tatsaechlichen Exchange-Status obliegt der
        ReconciliationEngine (Phase 7B), nicht diesem Schritt.
        """
        log.info("recovery.restoring_orders")
        try:
            open_orders = await self._order_repo.get_open_orders(self._trading_mode)
            log.info("recovery.open_orders_found", count=len(open_orders))
            return True
        except Exception as e:
            log.error("recovery.restore_orders_failed", error=str(e))
            return False

    async def _restore_strategies(self) -> bool:
        """
        Liest zuletzt aktive Strategien aus der DB und aktiviert sie
        erneut in der (rein in-memory startenden) StrategyRegistry.
        Strategien, die vor dem Crash aktiv waren, aber inzwischen nicht
        mehr registriert sind (z.B. Code-Deploy hat sie entfernt), werden
        uebersprungen und geloggt statt einen Fehler zu werfen.

        Wichtig: Recovery darf das Go-Live-Gate nicht umgehen. Der
        StrategyValidationRunner (siehe main.py Lifespan) laeuft VOR
        diesem Schritt und setzt entry.is_validated fuer jede
        registrierte Strategie neu, basierend auf einem frischen
        Backtest+Walk-Forward. Ein "war vor dem letzten Neustart aktiv"
        Zustand in der DB ist kein Ersatz fuer eine bestandene
        Validierung - andernfalls koennte ein einmal (fehlerhaft oder
        veraltet) aktivierter Zustand das Gate dauerhaft umgehen, selbst
        wenn ein aktueller Backtest NO-GO ergibt. Uebersprungene
        Strategien werden hier explizit deaktiviert (nicht nur
        ignoriert), damit is_active und is_validated konsistent bleiben
        und die naechste Persistenz keinen stillen Widerspruch schreibt.
        """
        log.info("recovery.restoring_strategies")
        try:
            active_names = await self._registry.get_active_names_from_db()
            restored = 0
            for name in active_names:
                entry = self._registry.get_entry(name)
                if entry is None:
                    log.warning(
                        "recovery.strategy_no_longer_registered",
                        name=name,
                    )
                    continue
                if not entry.is_validated:
                    log.warning(
                        "recovery.strategy_not_validated_skipped",
                        name=name,
                        note="War vor Neustart aktiv, aktueller Validation-Run "
                        "ergab jedoch kein GO - Go-Live-Gate hat Vorrang.",
                    )
                    await self._registry.deactivate(
                        name, reason="recovery: validation gate failed on restart"
                    )
                    continue
                await self._registry.activate(name)
                restored += 1
            log.info(
                "recovery.strategies_restored",
                count=restored,
                skipped=len(active_names) - restored,
            )
            return True
        except Exception as e:
            log.error("recovery.restore_strategies_failed", error=str(e))
            return False

    async def _restore_kill_switch(self) -> bool:
        """
        Synchronisiert den lokalen Kill-Switch-State mit dem in Redis
        persistierten Zustand.

        Root-Cause-Fix (live am Server reproduziert, 2026-09-17): eine
        frisch konstruierte KillSwitch-Instanz (get_kill_switch(), siehe
        sgr/risk/kill_switch.py) startet nach jedem Prozessneustart IMMER
        lokal mit is_active=False - unabhaengig davon, ob zuvor ein
        Trigger nach Redis persistiert wurde. Der bestehende Startup-
        Check _check_kill_switch_not_preactivated() (sgr/core/
        startup_checks.py) deckt das NICHT ab: er laeuft laut eigenem
        Modul-Docstring bewusst VOR jeder Redis-Verbindung und prueft
        deshalb ausschliesslich denselben frisch-lokalen State (per
        Docstring dort: "nur bei In-Memory-State aus demselben
        Prozesslauf relevant", z.B. Test-Leck) - er kann einen ueber
        einen echten Neustart hinweg persistierten Kill Switch
        strukturell nicht erkennen.

        Live-Konsequenz (Gordon, 2026-09-17 17:42-17:43 Uhr): Kill Switch
        war seit 2026-09-16 in Redis als aktiv persistiert. Nach einem
        Worker-Neustart lief `execution_engine.blocked_by_kill_switch`
        nicht - eine neue Order wurde faelschlich durchgelassen, bevor
        RiskEngines eigener Hard-Limit-Check (max_open_positions) den
        Switch 8 Sekunden spaeter zufaellig erneut ausloeste. Ohne diesen
        Fix wiederholt sich das bei JEDEM Neustart waehrend ein Kill
        Switch bewusst aktiv gehalten werden soll.

        Fix-Ort bewusst hier (RecoveryManager), nicht in
        startup_checks.py: dieser Schritt laeuft im Lifespan NACH Redis-
        Verbindungsaufbau (siehe sgr/api/main.py Schritt 8d, vor
        Market-Data-Start) - identisches zeitliches Muster wie
        _restore_strategies() oben. Bewusst fail-safe wie die anderen
        drei _restore_*()-Schritte (nicht fail-fast wie startup_checks.py):
        ein Redis-Fehler hier darf den Server-Start nicht verhindern,
        siehe Klassendocstring - im Fehlerfall bleibt lediglich der
        bisherige, bereits bestehende Luecken-Zustand erhalten, kein
        NEUES Risiko wird dadurch eingefuehrt.

        Setzt den lokalen State nur, wenn Redis tatsaechlich einen
        aktiven Trigger zeigt - ein inaktiver oder fehlender Redis-
        Eintrag aendert nichts (die lokale Instanz startet ohnehin
        inaktiv, siehe oben).
        """
        if self._redis_client is None:
            log.info(
                "recovery.kill_switch_sync_skipped",
                reason="no redis client injected",
            )
            return True

        from sgr.risk.kill_switch import get_kill_switch, read_kill_switch_state_from_redis

        try:
            current = await read_kill_switch_state_from_redis(
                self._redis_client, self._trading_mode, tenant_id=self._tenant_id
            )
            if current is None or not current.get("is_active"):
                log.info("recovery.kill_switch_synced", was_active=False)
                return True

            kill_switch = get_kill_switch(self._trading_mode, tenant_id=self._tenant_id)
            kill_switch.inject_redis(self._redis_client)
            # Identisches Muster wie der gefixte POST /risk/kill-switch/
            # reset-Endpoint (sgr/api/routers/risk.py): den lokalen State
            # direkt auf den echten, persistierten Zustand setzen (kein
            # erneuter trigger()-Aufruf noetig/gewuenscht - der Trigger
            # ist bereits in Redis persistiert, hier geht es nur um den
            # lokalen In-Memory-Spiegel dieses Prozesses).
            kill_switch._state.trigger(  # noqa: SLF001
                current.get("reason") or "unknown", self._trading_mode
            )
            log.warning(
                "recovery.kill_switch_restored_active",
                reason=current.get("reason"),
                triggered_at=current.get("triggered_at"),
            )
            return True
        except Exception as e:
            log.error("recovery.restore_kill_switch_failed", error=str(e))
            return False
