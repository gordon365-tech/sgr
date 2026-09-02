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
