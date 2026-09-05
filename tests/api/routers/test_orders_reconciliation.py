"""
Tests für sgr.api.routers.orders, sgr.api.routers.reconciliation, und
sgr.api.routers.portfolio.get_positions.

Migrationsstand (sgr-api Read-Only-Zielarchitektur, Commit 3):
    - orders.py liest jetzt über OrderRepository.get_by_user() statt
      PortfolioEngine.trade_history.
    - reconciliation.py liefert 501 (Live-Exchange-Call, nicht mehr aus
      der API zulässig - siehe Modul-Docstring, Folge-Commit geplant).
    - portfolio.get_positions liest Positions-Dicts aus
      PositionRepository.get_open_positions() statt Position-Objekte
      aus PortfolioEngine.positions.

Strategie: Handler-Coroutinen direkt aufrufen mit gemockten Dependencies,
analog zum bestehenden Muster in tests/api/routers/test_websocket.py.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from sgr.api.routers import (
    orders as orders_router,
    portfolio as portfolio_router,
    reconciliation as reconciliation_router,
)
from sgr.core.types import TradingMode


def _make_repos_with_orders(orders: list[dict]) -> MagicMock:
    repos = MagicMock()
    repos.orders.get_by_user = AsyncMock(return_value=orders)
    return repos


class TestGetOrderHistory:
    @pytest.mark.asyncio
    async def test_returns_orders_from_repository(self) -> None:
        orders = [{"id": str(i)} for i in range(5)]
        repos = _make_repos_with_orders(orders)
        mock_user = MagicMock(user_id="u1", trading_mode=TradingMode.PAPER)

        result = await orders_router.get_order_history(repos=repos, user=mock_user, limit=50)

        assert result == orders
        repos.orders.get_by_user.assert_awaited_once_with(
            "u1", TradingMode.PAPER, limit=50
        )

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self) -> None:
        repos = _make_repos_with_orders([{"id": "1"}])
        mock_user = MagicMock(user_id="u1", trading_mode=TradingMode.PAPER)

        await orders_router.get_order_history(repos=repos, user=mock_user, limit=3)

        repos.orders.get_by_user.assert_awaited_once_with(
            "u1", TradingMode.PAPER, limit=3
        )

    @pytest.mark.asyncio
    async def test_empty_history_returns_empty_list(self) -> None:
        repos = _make_repos_with_orders([])
        mock_user = MagicMock(user_id="u1", trading_mode=TradingMode.PAPER)

        result = await orders_router.get_order_history(repos=repos, user=mock_user, limit=50)

        assert result == []


class TestTriggerReconciliation:
    """
    reconciliation.py wurde in Commit 3 auf 501 umgestellt:
    ReconciliationEngine.reconcile() macht einen Live-Exchange-Call,
    was aus dem API-Prozess nicht mehr zulässig ist (siehe Modul-
    Docstring). Ein echter Command-Channel zum Worker ist ein
    Folge-Commit.
    """

    @pytest.mark.asyncio
    async def test_returns_501_not_yet_migrated(self) -> None:
        mock_user = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await reconciliation_router.trigger_reconciliation(user=mock_user)

        assert exc_info.value.status_code == 501


class TestGetPositions:
    def _make_position_dict(self, *, side: str, entry: str, current: str) -> dict:
        return {
            "symbol": "BTC/USDT",
            "side": side,
            "quantity": "1.5",
            "entry_price": entry,
            "current_price": current,
            "unrealized_pnl": "10",
            "strategy_name": "trend_v1",
        }

    def _make_repos_with_positions(self, positions: list[dict]) -> MagicMock:
        repos = MagicMock()
        repos.positions.get_open_positions = AsyncMock(return_value=positions)
        return repos

    @pytest.mark.asyncio
    async def test_long_position_pnl_pct_positive_direction(self) -> None:
        position = self._make_position_dict(side="long", entry="100", current="110")
        repos = self._make_repos_with_positions([position])
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(
            repos=repos, trading_mode=TradingMode.PAPER, user=mock_user
        )

        assert len(result) == 1
        assert result[0].unrealized_pnl_pct == 10.0
        assert result[0].side == "long"

    @pytest.mark.asyncio
    async def test_short_position_pnl_pct_is_inverted(self) -> None:
        position = self._make_position_dict(side="short", entry="100", current="110")
        repos = self._make_repos_with_positions([position])
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(
            repos=repos, trading_mode=TradingMode.PAPER, user=mock_user
        )

        assert len(result) == 1
        # Price went up, but this is a short -> negative pnl pct
        assert result[0].unrealized_pnl_pct == -10.0

    @pytest.mark.asyncio
    async def test_zero_entry_price_avoids_division_by_zero(self) -> None:
        position = self._make_position_dict(side="long", entry="0", current="50")
        repos = self._make_repos_with_positions([position])
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(
            repos=repos, trading_mode=TradingMode.PAPER, user=mock_user
        )

        assert result[0].unrealized_pnl_pct == 0.0

    @pytest.mark.asyncio
    async def test_no_positions_returns_empty_list(self) -> None:
        repos = self._make_repos_with_positions([])
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(
            repos=repos, trading_mode=TradingMode.PAPER, user=mock_user
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_notional_value_computed_from_quantity_and_current_price(self) -> None:
        position = self._make_position_dict(side="long", entry="100", current="110")
        repos = self._make_repos_with_positions([position])
        mock_user = MagicMock()

        result = await portfolio_router.get_positions(
            repos=repos, trading_mode=TradingMode.PAPER, user=mock_user
        )

        assert Decimal(result[0].notional_value) == Decimal("1.5") * Decimal("110")
