"""
Tests für sgr.api.routers.orders und sgr.api.routers.reconciliation.
Coverage-Ziel: orders.py 80% -> 100%, reconciliation.py 90% -> 100%.

Strategie: Handler-Coroutinen direkt aufrufen mit gemockten Dependencies,
analog zum bestehenden Muster in tests/api/routers/test_websocket.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sgr.api.routers import (
    orders as orders_router,
    portfolio as portfolio_router,
    reconciliation as reconciliation_router,
)
from sgr.core.types import (
    AssetClass,
    ExchangeID,
    Position,
    PositionSide,
    ReconciliationResult,
    ReconciliationStatus,
    Symbol,
    TradingMode,
)


class TestGetOrderHistory:
    @pytest.mark.asyncio
    async def test_returns_reversed_trade_history_within_limit(self) -> None:
        mock_portfolio = MagicMock()
        mock_portfolio.trade_history = [{"id": i} for i in range(5)]
        mock_user = MagicMock()

        result = await orders_router.get_order_history(
            portfolio=mock_portfolio, user=mock_user, limit=50
        )

        assert result == [{"id": 4}, {"id": 3}, {"id": 2}, {"id": 1}, {"id": 0}]

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self) -> None:
        mock_portfolio = MagicMock()
        mock_portfolio.trade_history = [{"id": i} for i in range(10)]
        mock_user = MagicMock()

        result = await orders_router.get_order_history(
            portfolio=mock_portfolio, user=mock_user, limit=3
        )

        # Only the last 3 entries, reversed
        assert result == [{"id": 9}, {"id": 8}, {"id": 7}]

    @pytest.mark.asyncio
    async def test_empty_history_returns_empty_list(self) -> None:
        mock_portfolio = MagicMock()
        mock_portfolio.trade_history = []
        mock_user = MagicMock()

        result = await orders_router.get_order_history(
            portfolio=mock_portfolio, user=mock_user, limit=50
        )

        assert result == []


class TestTriggerReconciliation:
    @pytest.mark.asyncio
    async def test_delegates_to_engine_and_returns_result(self) -> None:
        expected_result = ReconciliationResult(
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            status=ReconciliationStatus.CLEAN,
            trading_mode=TradingMode.PAPER,
            exchange=ExchangeID.PIONEX,
            checked_symbols=3,
        )
        mock_engine = AsyncMock()
        mock_engine.reconcile = AsyncMock(return_value=expected_result)
        mock_user = MagicMock()

        result = await reconciliation_router.trigger_reconciliation(
            user=mock_user, engine=mock_engine
        )

        assert result is expected_result
        mock_engine.reconcile.assert_awaited_once()


class TestGetPositions:
    def _make_position(self, *, side: PositionSide, entry: str, current: str) -> Position:
        return Position(
            symbol=Symbol(
                base="BTC", quote="USDT", exchange=ExchangeID.PIONEX, asset_class=AssetClass.SPOT
            ),
            side=side,
            quantity="1.5",
            entry_price=entry,
            current_price=current,
            unrealized_pnl="10",
            opened_at=datetime.now(tz=UTC),
            strategy_name="trend_v1",
            trading_mode=TradingMode.PAPER,
        )

    @pytest.mark.asyncio
    async def test_long_position_pnl_pct_positive_direction(self) -> None:
        position = self._make_position(side=PositionSide.LONG, entry="100", current="110")
        mock_portfolio = MagicMock()
        mock_portfolio.positions = [position]
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(portfolio=mock_portfolio, user=mock_user)

        assert len(result) == 1
        assert result[0].unrealized_pnl_pct == 10.0
        assert result[0].side == "long"

    @pytest.mark.asyncio
    async def test_short_position_pnl_pct_is_inverted(self) -> None:
        position = self._make_position(side=PositionSide.SHORT, entry="100", current="110")
        mock_portfolio = MagicMock()
        mock_portfolio.positions = [position]
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(portfolio=mock_portfolio, user=mock_user)

        assert len(result) == 1
        # Price went up, but this is a short -> negative pnl pct
        assert result[0].unrealized_pnl_pct == -10.0

    @pytest.mark.asyncio
    async def test_zero_entry_price_avoids_division_by_zero(self) -> None:
        position = self._make_position(side=PositionSide.LONG, entry="0", current="50")
        mock_portfolio = MagicMock()
        mock_portfolio.positions = [position]
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(portfolio=mock_portfolio, user=mock_user)

        assert result[0].unrealized_pnl_pct == 0.0

    @pytest.mark.asyncio
    async def test_no_positions_returns_empty_list(self) -> None:
        mock_portfolio = MagicMock()
        mock_portfolio.positions = []
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(portfolio=mock_portfolio, user=mock_user)

        assert result == []
