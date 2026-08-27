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
    ) -> None:
        self._portfolio_engine = portfolio_engine
        self._order_repo = order_repository
        self._registry = strategy_registry
        self._trading_mode = trading_mode

    async def recover_after_crash(self) -> bool:
        """
        Stellt Systemzustand nach ungeplantem Neustart wieder her.
        Fail-safe insgesamt: jeder einzelne Schritt wird versucht, ein
        Fehler in einem Schritt bricht recover_after_crash() nicht
        vollstaendig ab, wird aber im Rueckgabewert reflektiert.

        Returns:
            True nur wenn ALLE drei Schritte erfolgreich waren.
        """
        log.info("recovery.started")

        positions_ok = await self._restore_positions()
        orders_ok = await self._restore_orders()
        strategies_ok = await self._restore_strategies()

        success = positions_ok and orders_ok and strategies_ok
        if success:
            log.info("recovery.complete")
        else:
            log.warning(
                "recovery.partial_or_failed",
                positions_ok=positions_ok,
                orders_ok=orders_ok,
                strategies_ok=strategies_ok,
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
        """
        log.info("recovery.restoring_strategies")
        try:
            active_names = await self._registry.get_active_names_from_db()
            for name in active_names:
                if self._registry.get_entry(name) is None:
                    log.warning(
                        "recovery.strategy_no_longer_registered",
                        name=name,
                    )
                    continue
                await self._registry.activate(name)
            log.info("recovery.strategies_restored", count=len(active_names))
            return True
        except Exception as e:
            log.error("recovery.restore_strategies_failed", error=str(e))
            return False
