"""
Tests für sgr.execution.order_safety.SafeOrderExecutor (Baustein 7).

Testet die In-Process Duplicate-Detection- und Unknown-State-Middleware
isoliert von ExecutionEngine (siehe test_execution_engine.py für die
Integration).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sgr.core.types import (
    ExchangeID,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    TradingMode,
)
from sgr.execution.order_safety import SafeOrderExecutor


def _make_order_request() -> OrderRequest:
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
    return OrderRequest(
        id=uuid4(),
        signal_id=uuid4(),
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        trading_mode=TradingMode.PAPER,
    )


def _make_order_result(
    order: OrderRequest,
    status: OrderStatus = OrderStatus.FILLED,
    exchange_order_id: str = "EX-1",
) -> OrderResult:
    return OrderResult(
        request_id=order.id,
        exchange_order_id=exchange_order_id,
        symbol=order.symbol,
        status=status,
        filled_quantity=order.quantity if status == OrderStatus.FILLED else Decimal("0"),
        average_fill_price=Decimal("50000") if status == OrderStatus.FILLED else None,
        fees=Decimal("5"),
        submitted_at=datetime.now(tz=UTC),
        trading_mode=order.trading_mode,
        raw_response={},
    )


@pytest.fixture
def executor() -> SafeOrderExecutor:
    return SafeOrderExecutor()


class TestSuccessfulSubmission:
    async def test_submits_and_returns_exchange_result(self, executor: SafeOrderExecutor) -> None:
        order = _make_order_request()
        submit_fn = AsyncMock(return_value=_make_order_result(order))

        result = await executor.execute_safely(order, submit_fn)

        assert result.status == OrderStatus.FILLED
        assert result.exchange_order_id == "EX-1"
        submit_fn.assert_awaited_once_with(order)

    async def test_tracks_order_as_inflight_after_successful_submission(
        self, executor: SafeOrderExecutor
    ) -> None:
        order = _make_order_request()
        submit_fn = AsyncMock(
            return_value=_make_order_result(order, status=OrderStatus.SUBMITTED)
        )

        await executor.execute_safely(order, submit_fn)

        tracked = executor.get_inflight(order)
        assert tracked is not None
        assert tracked.status == OrderStatus.SUBMITTED


class TestDuplicateDetection:
    async def test_second_submission_of_same_order_id_is_blocked(
        self, executor: SafeOrderExecutor
    ) -> None:
        """order.id ist der Idempotency-Key: eine zweite Submission fuer
        dieselbe order.id wird geblockt, bevor exchange_submit_fn ueberhaupt
        aufgerufen wird."""
        order = _make_order_request()
        submit_fn = AsyncMock(
            return_value=_make_order_result(order, status=OrderStatus.SUBMITTED)
        )
        await executor.execute_safely(order, submit_fn)

        second_result = await executor.execute_safely(order, submit_fn)

        assert second_result.status == OrderStatus.REJECTED
        assert second_result.raw_response["duplicate"] is True
        submit_fn.assert_awaited_once()  # nicht ein zweites Mal aufgerufen

    async def test_different_order_ids_are_not_blocked_even_with_same_signal_symbol_side(
        self, executor: SafeOrderExecutor
    ) -> None:
        """Zwei unterschiedliche OrderRequests mit demselben Signal/Symbol/
        Side (z.B. Scale-in) sind KEINE Duplikate - der Key ist order.id,
        nicht signal_id/symbol/side (siehe Modul-Docstring, Punkt 1)."""
        signal_id = uuid4()
        symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
        order_a = OrderRequest(
            id=uuid4(),
            signal_id=signal_id,
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            trading_mode=TradingMode.PAPER,
        )
        order_b = OrderRequest(
            id=uuid4(),
            signal_id=signal_id,
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            trading_mode=TradingMode.PAPER,
        )
        submit_fn = AsyncMock(
            side_effect=[
                _make_order_result(order_a, exchange_order_id="EX-A"),
                _make_order_result(order_b, exchange_order_id="EX-B"),
            ]
        )

        result_a = await executor.execute_safely(order_a, submit_fn)
        result_b = await executor.execute_safely(order_b, submit_fn)

        assert result_a.exchange_order_id == "EX-A"
        assert result_b.exchange_order_id == "EX-B"
        assert submit_fn.await_count == 2

    async def test_released_order_can_be_resubmitted(self, executor: SafeOrderExecutor) -> None:
        """Nach release() (Order terminiert, siehe ExecutionEngine-Integration)
        ist dieselbe order.id NICHT mehr blockiert - relevant fuer einen
        spaeteren, unabhaengigen Retry-Zyklus."""
        order = _make_order_request()
        submit_fn = AsyncMock(
            return_value=_make_order_result(order, status=OrderStatus.FILLED)
        )
        await executor.execute_safely(order, submit_fn)
        executor.release(order)

        second_result = await executor.execute_safely(order, submit_fn)

        assert second_result.status == OrderStatus.FILLED
        assert submit_fn.await_count == 2


class TestUnknownStateHandling:
    async def test_submit_error_returns_rejected_with_unknown_marker(
        self, executor: SafeOrderExecutor
    ) -> None:
        """Wenn exchange_submit_fn selbst fehlschlaegt (Netzwerkfehler,
        Timeout), ist der tatsaechliche Order-Status unklar - kein
        automatischer Retry, sondern REJECTED mit raw_response["unknown"]."""
        order = _make_order_request()
        submit_fn = AsyncMock(side_effect=RuntimeError("connection reset"))

        result = await executor.execute_safely(order, submit_fn)

        assert result.status == OrderStatus.REJECTED
        assert result.raw_response["unknown"] is True
        assert "connection reset" in result.raw_response["error"]

    async def test_submit_error_finalizes_pending_row_instead_of_orphaning_it(self) -> None:
        """Reproduces the 2026-09-16/17 production incident: 1,496 orders
        were left permanently stuck at status='pending' because
        _persist_pending() commits the row in its own transaction before
        the exchange call, and a submit failure returned early without
        ever updating it - see sgr/execution/order_safety.py BUG-FIX
        comment in execute_safely()'s except-block. A ticker/exchange
        failure after the pending row is already committed must result in
        the row being finalized (e.g. REJECTED with an unknown marker),
        never left at PENDING forever."""
        repo = AsyncMock()
        repo.get_by_id = AsyncMock(return_value=None)
        repo.create = AsyncMock(return_value="order-id")
        repo.update_status = AsyncMock()

        executor = SafeOrderExecutor(order_repository=repo, tenant_id="tenant-x")
        order = _make_order_request()
        submit_fn = AsyncMock(side_effect=RuntimeError("ticker fetch failed"))

        result = await executor.execute_safely(order, submit_fn)

        repo.create.assert_awaited_once()
        repo.update_status.assert_awaited_once()
        _, kwargs = repo.update_status.await_args
        assert kwargs["status"] != OrderStatus.PENDING.value
        assert result.raw_response["unknown"] is True

    async def test_unknown_state_order_is_released_and_can_be_retried(
        self, executor: SafeOrderExecutor
    ) -> None:
        """Ein Unknown-State-Fehler blockt spaetere Retries NICHT - der
        Placeholder wird sofort wieder freigegeben, da die Duplicate-Guard
        nur laufende/erfolgreiche Submissions blocken soll, kein
        fehlgeschlagenes exchange_submit_fn."""
        order = _make_order_request()
        submit_fn = AsyncMock(
            side_effect=[RuntimeError("timeout"), _make_order_result(order)]
        )

        first_result = await executor.execute_safely(order, submit_fn)
        second_result = await executor.execute_safely(order, submit_fn)

        assert first_result.raw_response["unknown"] is True
        assert second_result.status == OrderStatus.FILLED
        assert submit_fn.await_count == 2


class TestInflightTrackingHelpers:
    async def test_update_inflight_refreshes_tracked_result(
        self, executor: SafeOrderExecutor
    ) -> None:
        order = _make_order_request()
        submit_fn = AsyncMock(
            return_value=_make_order_result(order, status=OrderStatus.SUBMITTED)
        )
        await executor.execute_safely(order, submit_fn)

        updated = _make_order_result(order, status=OrderStatus.PARTIALLY_FILLED)
        executor.update_inflight(order, updated)

        assert executor.get_inflight(order).status == OrderStatus.PARTIALLY_FILLED

    async def test_update_inflight_is_noop_for_untracked_order(
        self, executor: SafeOrderExecutor
    ) -> None:
        """update_inflight() darf keinen Eintrag anlegen, wenn die Order
        gar nicht (mehr) getrackt ist (z.B. nach release())."""
        order = _make_order_request()

        executor.update_inflight(order, _make_order_result(order))

        assert executor.get_inflight(order) is None

    async def test_all_inflight_returns_snapshot_of_tracked_orders(
        self, executor: SafeOrderExecutor
    ) -> None:
        order_a = _make_order_request()
        order_b = _make_order_request()
        submit_fn = AsyncMock(
            side_effect=[
                _make_order_result(order_a, status=OrderStatus.SUBMITTED),
                _make_order_result(order_b, status=OrderStatus.SUBMITTED),
            ]
        )
        await executor.execute_safely(order_a, submit_fn)
        await executor.execute_safely(order_b, submit_fn)

        snapshot = executor.all_inflight()

        assert set(snapshot.keys()) == {str(order_a.id), str(order_b.id)}

    async def test_clear_removes_all_tracked_orders(self, executor: SafeOrderExecutor) -> None:
        order = _make_order_request()
        submit_fn = AsyncMock(
            return_value=_make_order_result(order, status=OrderStatus.SUBMITTED)
        )
        await executor.execute_safely(order, submit_fn)

        executor.clear()

        assert executor.all_inflight() == {}


class TestDbBackedIdempotency:
    """
    Root-Cause-Fix (Docker-Crash-Test-Audit 2026-09-16, siehe
    order_safety.py Modul-Docstring Punkt 5): PAPER-Order-Requests hatten
    vor diesem Fix KEINE prozessuebergreifende Idempotenz - nur der
    LIVE-Zweig von ccxt_base.py::place_order prueft clientOrderId gegen
    die Exchange, PAPER nimmt ueber _simulate_order() einen fruehen
    Sonderpfad, der diese Pruefung nie erreicht. Nach einem Prozess-
    Neustart (frischer In-Memory-State) waere ein erneutes
    execute_safely() mit identischer order.id ein zweiter echter Fill
    gewesen. Diese Tests decken den DB-gestuetzten Ersatz-Check ab.
    """

    def _make_repo(self, row: dict | None) -> AsyncMock:
        repo = AsyncMock()
        repo.get_by_id.return_value = row
        return repo

    async def test_no_repository_behaves_exactly_like_before_the_fix(self) -> None:
        """Ohne injiziertes OrderRepository (z.B. isolierte Unit-Tests)
        bleibt das Verhalten unveraendert - reines In-Memory-Tracking."""
        executor = SafeOrderExecutor()
        order = _make_order_request()
        submit_fn = AsyncMock(return_value=_make_order_result(order))

        result = await executor.execute_safely(order, submit_fn)

        assert result.status == OrderStatus.FILLED
        submit_fn.assert_awaited_once()

    async def test_no_persisted_record_proceeds_with_normal_submission(self) -> None:
        order = _make_order_request()
        repo = self._make_repo(row=None)
        executor = SafeOrderExecutor(order_repository=repo)
        submit_fn = AsyncMock(return_value=_make_order_result(order))

        result = await executor.execute_safely(order, submit_fn)

        assert result.status == OrderStatus.FILLED
        submit_fn.assert_awaited_once()

    async def test_terminal_status_in_db_blocks_resubmission_after_restart(self) -> None:
        """Simuliert einen Prozess-Neustart: FRISCHER SafeOrderExecutor
        (leerer In-Memory-State, wie nach Worker-Kill/-Restart), aber die
        DB hat den Fill von der vorherigen Prozessinstanz bereits
        persistiert. Muss den Fill aus der DB rekonstruieren, OHNE
        submit_fn ein zweites Mal aufzurufen - das ist genau der Fall,
        der vor diesem Fix eine echte Doppel-Order verursacht haette."""
        order = _make_order_request()
        repo = self._make_repo(
            row={
                "status": "filled",
                "exchange_order_id": "PAPER-already-filled",
                "filled_quantity": order.quantity,
                "average_fill_price": Decimal("50000"),
                "fees": Decimal("5"),
                "submitted_at": datetime.now(tz=UTC),
                "filled_at": datetime.now(tz=UTC),
                "raw_response": {"strategy": "test_strategy"},
            }
        )
        executor = SafeOrderExecutor(order_repository=repo)  # frischer Prozess
        submit_fn = AsyncMock(return_value=_make_order_result(order))

        result = await executor.execute_safely(order, submit_fn)

        assert result.status == OrderStatus.FILLED
        assert result.raw_response["duplicate"] is True
        assert result.exchange_order_id == "PAPER-already-filled"
        submit_fn.assert_not_awaited()  # KEIN zweiter Fill

    async def test_non_terminal_status_in_db_is_treated_as_unknown_not_resubmitted(
        self,
    ) -> None:
        """Die vorherige Prozessinstanz ist gestorben, WAEHREND die Order
        noch 'submitted' war (kein Terminalstatus persistiert) - unklar,
        ob der Fill vor dem Crash noch stattfand. Fail-safe: kein blindes
        Neu-Submitten, sondern Unknown-State wie bei einem Submit-Fehler."""
        order = _make_order_request()
        repo = self._make_repo(
            row={
                "status": "submitted",
                "exchange_order_id": "PAPER-in-flight-at-crash",
                "filled_quantity": Decimal("0"),
                "average_fill_price": None,
                "fees": Decimal("0"),
                "submitted_at": datetime.now(tz=UTC),
                "filled_at": None,
                "raw_response": {},
            }
        )
        executor = SafeOrderExecutor(order_repository=repo)
        submit_fn = AsyncMock(return_value=_make_order_result(order))

        result = await executor.execute_safely(order, submit_fn)

        assert result.status == OrderStatus.REJECTED
        assert result.raw_response["unknown"] is True
        submit_fn.assert_not_awaited()

    async def test_db_check_failure_fails_open_and_proceeds_with_submission(self) -> None:
        """DB nicht erreichbar waehrend der Idempotenz-Pruefung darf die
        Order-Verarbeitung nicht blockieren (Fail-Safe-Prinzip: DB-
        Fehler duerfen Trading-Ergebnisse nie beeinflussen) - der Check
        wird einfach uebersprungen, normale Submission laeuft weiter."""
        order = _make_order_request()
        repo = AsyncMock()
        repo.get_by_id.side_effect = RuntimeError("db connection lost")
        executor = SafeOrderExecutor(order_repository=repo)
        submit_fn = AsyncMock(return_value=_make_order_result(order))

        result = await executor.execute_safely(order, submit_fn)

        assert result.status == OrderStatus.FILLED
        submit_fn.assert_awaited_once()


class TestConcurrentSubmissionRace:
    async def test_second_concurrent_submission_is_blocked_while_first_still_in_flight(
        self, executor: SafeOrderExecutor
    ) -> None:
        """Der Placeholder wird VOR dem Exchange-Call gesetzt: der Guard
        greift bereits waehrend exchange_submit_fn noch laeuft, nicht erst
        nachdem es zurueckgekehrt ist (das ist exakt die Race, die dieser
        Schutz verhindern soll)."""
        import asyncio

        order = _make_order_request()
        entered = asyncio.Event()
        release_event = asyncio.Event()

        async def slow_submit(_order: OrderRequest) -> OrderResult:
            entered.set()
            await release_event.wait()
            return _make_order_result(_order, status=OrderStatus.FILLED)

        first_task = asyncio.create_task(executor.execute_safely(order, slow_submit))
        await asyncio.wait_for(entered.wait(), timeout=5)

        second_result = await executor.execute_safely(order, slow_submit)

        assert second_result.raw_response.get("duplicate") is True

        release_event.set()
        first_result = await asyncio.wait_for(first_task, timeout=5)
        assert first_result.status == OrderStatus.FILLED
