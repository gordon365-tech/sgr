"""
Graceful Shutdown & Circuit Breaker
====================================
Handles crash recovery & exchange outage resilience.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

from sgr.core.logging import get_logger

log = get_logger(__name__)


class CircuitBreakerState(str, Enum):
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

        except Exception as e:
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
        except asyncio.TimeoutError:
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
    Handles recovery after crashes.
    
    Restores:
    1. Position state from database
    2. Pending orders
    3. Strategy state
    """

    @staticmethod
    async def recover_after_crash() -> bool:
        """
        Recovers system state after unplanned shutdown.
        
        Returns:
            True if recovery successful
        """
        log.info("recovery.started")

        try:
            # 1. Restore positions from DB
            await RecoveryManager._restore_positions()

            # 2. Restore pending orders
            await RecoveryManager._restore_orders()

            # 3. Restore strategy state
            await RecoveryManager._restore_strategies()

            log.info("recovery.complete")
            return True

        except Exception as e:
            log.error("recovery.failed", error=str(e))
            return False

    @staticmethod
    async def _restore_positions() -> None:
        """Restores open positions from database."""
        log.info("recovery.restoring_positions")
        # Pseudo-code
        # await portfolio_engine.restore_from_db()

    @staticmethod
    async def _restore_orders() -> None:
        """Restores pending orders from database."""
        log.info("recovery.restoring_orders")
        # Pseudo-code
        # await execution_engine.restore_pending_orders()

    @staticmethod
    async def _restore_strategies() -> None:
        """Restores strategy state from database."""
        log.info("recovery.restoring_strategies")
        # Pseudo-code
        # await strategy_engine.restore_state()
