"""
Tests für Order Safety: Ambiguous Submission Handling

Testszenarios:
1. Exchange Timeout nach Submission (ambiguous)
2. Connection Reset nach Submission  
3. HTTP Response Loss
4. Normal Rejection
5. Normal Success
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
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
from sgr.execution.engine import ExecutionEngine


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)


@pytest.fixture
def order_request(symbol: Symbol) -> OrderRequest:
    return OrderRequest(
        signal_id=uuid4(),
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.1"),
        limit_price=Decimal("50000"),
        trading_mode=TradingMode.PAPER,
    )


class TestAmbiguousOrderSubmission:
    """Orders die ambiguous nach Submission hinterlassen wurden."""

    @pytest.mark.asyncio
    async def test_exchange_timeout_no_blind_retry(
        self, order_request: OrderRequest
    ) -> None:
        """Exchange timeout nach Submission wird nicht blind resubmittet."""
        pool = MagicMock()
        adapter = AsyncMock()
        adapter.place_order = AsyncMock(side_effect=TimeoutError("Exchange timeout"))
        pool.get = MagicMock(return_value=adapter)

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        result = await engine.execute(order_request)

        # REJECTED ist conservative: "wir wissen nicht ob die Order ankam"
        assert result.status == OrderStatus.REJECTED

        # KRITISCH: place_order wurde genau 1x aufgerufen (KEIN blind retry)
        assert adapter.place_order.await_count == 1

    @pytest.mark.asyncio
    async def test_connection_reset_no_blind_retry(
        self, order_request: OrderRequest
    ) -> None:
        """Connection reset wird nicht blind resubmittet."""
        pool = MagicMock()
        adapter = AsyncMock()
        adapter.place_order = AsyncMock(
            side_effect=ConnectionResetError("Connection reset by peer")
        )
        pool.get = MagicMock(return_value=adapter)

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        result = await engine.execute(order_request)

        assert result.status == OrderStatus.REJECTED
        assert adapter.place_order.await_count == 1

    @pytest.mark.asyncio
    async def test_normal_rejection_returns_rejected_status(
        self, order_request: OrderRequest
    ) -> None:
        """Normal Rejection von der Exchange wird korrekt reportet."""
        pool = MagicMock()
        adapter = AsyncMock()

        rejected_result = OrderResult(
            request_id=order_request.id,
            exchange_order_id="",
            symbol=order_request.symbol,
            status=OrderStatus.REJECTED,
            submitted_at=datetime.now(tz=UTC),
            trading_mode=TradingMode.PAPER,
            raw_response={"error": "Insufficient balance"},
        )
        adapter.place_order = AsyncMock(return_value=rejected_result)
        # get_order wird nicht aufgerufen wenn place_order REJECTED returnt
        adapter.get_order = AsyncMock(return_value=rejected_result)
        pool.get = MagicMock(return_value=adapter)

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        result = await engine.execute(order_request)

        assert result.status == OrderStatus.REJECTED
        assert "Insufficient balance" in result.raw_response.get("error", "")

    @pytest.mark.asyncio
    async def test_successful_submission_filled(
        self, order_request: OrderRequest
    ) -> None:
        """Normale Success-Submission wird sofort gefüllt (Paper Mode)."""
        pool = MagicMock()
        adapter = AsyncMock()

        filled_result = OrderResult(
            request_id=order_request.id,
            exchange_order_id="ex_123",
            symbol=order_request.symbol,
            status=OrderStatus.FILLED,
            filled_quantity=order_request.quantity,
            average_fill_price=order_request.limit_price,
            submitted_at=datetime.now(tz=UTC),
            trading_mode=TradingMode.PAPER,
        )
        adapter.place_order = AsyncMock(return_value=filled_result)
        pool.get = MagicMock(return_value=adapter)

        with patch("sgr.execution.engine.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = AsyncMock()

            engine = ExecutionEngine(pool, TradingMode.PAPER)
            result = await engine.execute(order_request)

        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == order_request.quantity
