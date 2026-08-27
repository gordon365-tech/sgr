"""
Tests für sgr.core.resilience.

Kontext: Das Modul war zuvor 0% Coverage, aber nicht wegen eines
Import-Blockers (im Gegensatz zu security.py) - es wird schlicht
nirgends im Projekt importiert. CircuitBreaker ist vollstaendig und
korrekt implementiert und wird hier vollstaendig getestet.

RecoveryManager war urspruenglich reiner Platzhalter-Code
(auskommentierter Pseudo-Code) und duplizierte die bereits existierende,
echte PortfolioEngine.restore_from_persistence(), ohne selbst verdrahtet
zu sein. Wurde umgebaut: statt eigener Logik delegiert RecoveryManager
jetzt an die echten Komponenten (PortfolioEngine, OrderRepository,
StrategyRegistry) - siehe Klassendocstring in resilience.py fuer Details.

GracefulShutdownManager.close_all_positions() bleibt Platzhalter-Code
(Klassendocstring dort beschreibt die Absicht, aber keine Order-Storno-
Logik existiert an dieser Stelle - Kill Switch uebernimmt das bereits
an anderer Stelle). Bewusst NICHT getestet, als Deferred Finding
dokumentiert, analog zu core/app.py und monitoring/observability.py.

Getestet:
    1. CircuitBreaker: CLOSED -> erfolgreiche Calls bleiben CLOSED
    2. CircuitBreaker: Fehler unterhalb Threshold bleibt CLOSED
    3. CircuitBreaker: Fehler erreicht Threshold -> OPEN
    4. CircuitBreaker: OPEN lehnt Calls sofort ab (CircuitBreakerError)
    5. CircuitBreaker: OPEN -> nach Timeout -> HALF_OPEN -> Testanfrage
    6. CircuitBreaker: HALF_OPEN + genug Erfolge -> CLOSED (Reset)
    7. CircuitBreaker: HALF_OPEN + erneuter Fehler -> zurueck zu OPEN
    8. CircuitBreaker: get_state() liefert korrekten Snapshot
    9. GracefulShutdownManager: register_task + shutdown wartet auf Tasks
   10. GracefulShutdownManager: Timeout bei haengenden Tasks wird geloggt,
       nicht geworfen
   11. RecoveryManager: jeder _restore_*()-Schritt delegiert korrekt an
       die injizierte echte Komponente, fail-safe pro Schritt,
       recover_after_crash() aggregiert alle drei Ergebnisse
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sgr.core.resilience import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitBreakerState,
    GracefulShutdownManager,
    RecoveryManager,
)
from sgr.core.types import TradingMode

# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreakerClosedState:
    async def test_successful_call_stays_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)

        async def ok() -> str:
            return "result"

        result = await cb.call(ok)

        assert result == "result"
        assert cb.state == CircuitBreakerState.CLOSED
        assert cb.failure_count == 0

    async def test_failures_below_threshold_stay_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)

        async def fail() -> None:
            raise RuntimeError("boom")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(fail)

        assert cb.state == CircuitBreakerState.CLOSED
        assert cb.failure_count == 2

    async def test_reaching_threshold_opens_circuit(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)

        async def fail() -> None:
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(fail)

        assert cb.state == CircuitBreakerState.OPEN
        assert cb.failure_count == 3


class TestCircuitBreakerOpenState:
    async def test_open_circuit_rejects_immediately_without_calling_func(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout_seconds=999)
        called = {"count": 0}

        async def fail() -> None:
            called["count"] += 1
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.call(fail)
        assert cb.state == CircuitBreakerState.OPEN

        with pytest.raises(CircuitBreakerError):
            await cb.call(fail)

        # func wurde beim zweiten Call NICHT erneut aufgerufen - Circuit
        # lehnt sofort ab, ohne den nachgelagerten Service zu belasten.
        assert called["count"] == 1

    async def test_open_transitions_to_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout_seconds=0)

        async def fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.call(fail)
        assert cb.state == CircuitBreakerState.OPEN

        async def ok() -> str:
            return "recovered"

        # recovery_timeout_seconds=0 -> _should_attempt_reset() sofort True
        result = await cb.call(ok)

        assert result == "recovered"
        assert cb.state == CircuitBreakerState.HALF_OPEN


class TestCircuitBreakerHalfOpenState:
    async def test_enough_successes_closes_circuit(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=1, recovery_timeout_seconds=0, success_threshold=2
        )

        async def fail() -> None:
            raise RuntimeError("boom")

        async def ok() -> str:
            return "ok"

        with pytest.raises(RuntimeError):
            await cb.call(fail)
        assert cb.state == CircuitBreakerState.OPEN

        await cb.call(ok)  # 1. Erfolg -> HALF_OPEN, success_count=1
        assert cb.state == CircuitBreakerState.HALF_OPEN

        await cb.call(ok)  # 2. Erfolg -> success_threshold erreicht -> CLOSED
        assert cb.state == CircuitBreakerState.CLOSED
        assert cb.failure_count == 0

    async def test_failure_during_half_open_reopens(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout_seconds=0)

        async def fail() -> None:
            raise RuntimeError("boom")

        async def ok() -> str:
            return "ok"

        with pytest.raises(RuntimeError):
            await cb.call(fail)
        assert cb.state == CircuitBreakerState.OPEN

        await cb.call(ok)  # -> HALF_OPEN
        assert cb.state == CircuitBreakerState.HALF_OPEN

        with pytest.raises(RuntimeError):
            await cb.call(fail)  # erneuter Fehler waehrend HALF_OPEN

        assert cb.state == CircuitBreakerState.OPEN


class TestCircuitBreakerState:
    async def test_get_state_reflects_current_snapshot(self) -> None:
        cb = CircuitBreaker("exchange_pionex", failure_threshold=5)

        async def fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.call(fail)

        state = cb.get_state()

        assert state["name"] == "exchange_pionex"
        assert state["state"] == "closed"  # 1 Fehler < threshold=5
        assert state["failure_count"] == 1
        assert state["last_failure"] is not None

    def test_get_state_before_any_calls(self) -> None:
        cb = CircuitBreaker("fresh")
        state = cb.get_state()

        assert state["failure_count"] == 0
        assert state["last_failure"] is None

    def test_should_attempt_reset_false_when_never_failed(self) -> None:
        """
        Defensive Absicherung: _should_attempt_reset() vor jedem Fehler
        (last_failure_time=None) darf nicht crashen und muss False
        liefern - direkter Methodenaufruf, da ueber call() dieser
        Zustand nicht erreichbar ist (state wird nur OPEN nach einem
        registrierten Fehler, der last_failure_time immer setzt).
        """
        cb = CircuitBreaker("fresh")
        assert cb._should_attempt_reset() is False


# ---------------------------------------------------------------------------
# GracefulShutdownManager
# ---------------------------------------------------------------------------


class TestGracefulShutdownManager:
    async def test_shutdown_completes_with_no_registered_tasks(self) -> None:
        mgr = GracefulShutdownManager(grace_period_seconds=1)

        await mgr.shutdown()

        assert mgr.shutdown_event.is_set()

    async def test_shutdown_waits_for_registered_task_to_complete(self) -> None:
        mgr = GracefulShutdownManager(grace_period_seconds=5)

        async def quick_task() -> None:
            await asyncio.sleep(0.01)

        task = asyncio.ensure_future(quick_task())
        mgr.register_task(task)

        await mgr.shutdown()

        assert task.done()

    async def test_shutdown_logs_but_does_not_raise_on_timeout(self, mocker) -> None:
        """
        Ein haengender Task darf shutdown() nicht unbegrenzt blockieren
        oder mit einer Exception abbrechen lassen - nur ein Timeout-Log.
        """
        mgr = GracefulShutdownManager(grace_period_seconds=0)

        async def hanging_task() -> None:
            await asyncio.sleep(10)

        task = asyncio.ensure_future(hanging_task())
        mgr.register_task(task)

        mock_log = mocker.patch("sgr.core.resilience.log")
        await mgr.shutdown()

        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[0] == "shutdown.timeout"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_register_task_auto_discards_on_completion(self) -> None:
        mgr = GracefulShutdownManager()

        async def quick_task() -> None:
            pass

        task = asyncio.ensure_future(quick_task())
        mgr.register_task(task)
        assert task in mgr.active_tasks

        await task
        # done_callback ist synchron nach await task bereits gefeuert
        assert task not in mgr.active_tasks

    async def test_task_exception_during_wait_is_logged_not_raised(self, mocker) -> None:
        mgr = GracefulShutdownManager(grace_period_seconds=5)

        async def failing_task() -> None:
            raise RuntimeError("task failed")

        task = asyncio.ensure_future(failing_task())
        mgr.register_task(task)

        mock_log = mocker.patch("sgr.core.resilience.log")
        await mgr.shutdown()  # darf nicht raisen, obwohl der Task fehlschlaegt

        mock_log.error.assert_called_once()
        assert mock_log.error.call_args.args[0] == "shutdown.task_error"


# ---------------------------------------------------------------------------
# RecoveryManager
# ---------------------------------------------------------------------------


class TestRecoveryManager:
    """
    RecoveryManager delegiert jetzt an die echten Komponenten
    (PortfolioEngine.restore_from_persistence(), OrderRepository.
    get_open_orders(), StrategyRegistry.get_active_names_from_db() +
    activate()) statt Pseudo-Code auszufuehren. Hier werden alle drei
    Abhaengigkeiten gemockt - die Komponenten selbst haben eigene Tests
    (test_position_repository.py, test_strategy_portfolio.py).
    """

    def _make_manager(
        self,
        portfolio_engine=None,
        order_repository=None,
        strategy_registry=None,
    ) -> RecoveryManager:
        return RecoveryManager(
            portfolio_engine=portfolio_engine or AsyncMock(),
            order_repository=order_repository or AsyncMock(),
            strategy_registry=strategy_registry or AsyncMock(),
            trading_mode=TradingMode.LIVE,
        )

    async def test_recover_after_crash_returns_true_when_all_steps_succeed(self) -> None:
        portfolio = AsyncMock()
        order_repo = AsyncMock()
        order_repo.get_open_orders.return_value = []
        registry = AsyncMock()
        registry.get_active_names_from_db.return_value = []

        mgr = self._make_manager(portfolio, order_repo, registry)
        result = await mgr.recover_after_crash()

        assert result is True
        portfolio.restore_from_persistence.assert_called_once()
        order_repo.get_open_orders.assert_called_once_with(TradingMode.LIVE)

    async def test_restore_positions_delegates_to_portfolio_engine(self) -> None:
        """
        Kernpunkt des Fixes: KEINE eigene Restore-Logik, reiner Delegat
        an die bereits existierende, fail-closed Implementierung.
        """
        portfolio = AsyncMock()
        mgr = self._make_manager(portfolio_engine=portfolio)

        result = await mgr._restore_positions()

        assert result is True
        portfolio.restore_from_persistence.assert_called_once()

    async def test_restore_positions_failure_returns_false_not_raises(self) -> None:
        portfolio = AsyncMock()
        portfolio.restore_from_persistence.side_effect = RuntimeError("db down")
        mgr = self._make_manager(portfolio_engine=portfolio)

        result = await mgr._restore_positions()

        assert result is False

    async def test_restore_orders_reads_open_orders_no_auto_action(self) -> None:
        """
        Bewusste Design-Entscheidung: offene Orders werden nur gelesen/
        geloggt, nicht automatisch storniert oder erneut eingereicht
        (Duplicate-Order-Risiko). Abstimmung obliegt ReconciliationEngine.
        """
        order_repo = AsyncMock()
        order_repo.get_open_orders.return_value = [{"id": "order-1"}, {"id": "order-2"}]
        mgr = self._make_manager(order_repository=order_repo)

        result = await mgr._restore_orders()

        assert result is True
        order_repo.get_open_orders.assert_called_once_with(TradingMode.LIVE)

    async def test_restore_orders_failure_returns_false_not_raises(self) -> None:
        order_repo = AsyncMock()
        order_repo.get_open_orders.side_effect = RuntimeError("db down")
        mgr = self._make_manager(order_repository=order_repo)

        result = await mgr._restore_orders()

        assert result is False

    async def test_restore_strategies_reactivates_previously_active(self) -> None:
        registry = AsyncMock()
        registry.get_active_names_from_db.return_value = ["trend_following_v1"]
        registry.get_entry = lambda name: object()  # synchron auf echter Registry, existiert
        mgr = self._make_manager(strategy_registry=registry)

        result = await mgr._restore_strategies()

        assert result is True
        registry.activate.assert_called_once_with("trend_following_v1")

    async def test_restore_strategies_skips_no_longer_registered(self) -> None:
        """
        Eine zuvor aktive Strategie, die nach einem Code-Deploy nicht
        mehr registriert ist, darf recovery nicht scheitern lassen.
        """
        registry = AsyncMock()
        registry.get_active_names_from_db.return_value = ["removed_strategy"]
        # synchron auf echter Registry, nicht mehr registriert
        registry.get_entry = lambda name: None
        mgr = self._make_manager(strategy_registry=registry)

        result = await mgr._restore_strategies()

        assert result is True
        registry.activate.assert_not_called()

    async def test_restore_strategies_failure_returns_false_not_raises(self) -> None:
        registry = AsyncMock()
        registry.get_active_names_from_db.side_effect = RuntimeError("db down")
        mgr = self._make_manager(strategy_registry=registry)

        result = await mgr._restore_strategies()

        assert result is False

    async def test_recover_after_crash_returns_false_if_any_step_fails(self) -> None:
        portfolio = AsyncMock()
        portfolio.restore_from_persistence.side_effect = RuntimeError("db unreachable")
        order_repo = AsyncMock()
        order_repo.get_open_orders.return_value = []
        registry = AsyncMock()
        registry.get_active_names_from_db.return_value = []

        mgr = self._make_manager(portfolio, order_repo, registry)
        result = await mgr.recover_after_crash()

        assert result is False
        # Die anderen Schritte laufen trotzdem (kein Abbruch bei erstem Fehler)
        order_repo.get_open_orders.assert_called_once()
        registry.get_active_names_from_db.assert_called_once()
