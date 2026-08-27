"""
Unit-Tests für die neuen Repository-Methoden get_open_orders() und
get_active_names() - beide Voraussetzung für RecoveryManager, das zuvor
Pseudo-Code war (siehe test_resilience.py Modul-Docstring).

Folgt dem Mocking-Muster aus test_position_repository.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.repositories import OrderRepository, StrategyRepository
from sgr.core.types import TradingMode

if TYPE_CHECKING:
    import pytest_mock


class _FakeAsyncSession:
    """Minimaler Stand-in für AsyncSession, steuert execute()-Rückgabe."""

    def __init__(self, scalars_result=None) -> None:
        self._scalars_result = scalars_result or []
        self.executed_statements: list = []

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = self._scalars_result
        return result


def _patch_get_session(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("sgr.core.repositories.get_session", return_value=cm)


class _FakeOrderRow:
    """Minimaler Stand-in für eine OrderModel-Row."""

    def __init__(self, **kwargs) -> None:
        self.id = kwargs.get("id", "order-1")
        self.signal_id = kwargs.get("signal_id", "signal-1")
        self.exchange_order_id = kwargs.get("exchange_order_id", "EX-1")
        self.symbol = kwargs.get("symbol", "BTC/USDT")
        self.exchange = kwargs.get("exchange", "pionex")
        self.side = kwargs.get("side", "buy")
        self.order_type = kwargs.get("order_type", "market")
        self.quantity = kwargs.get("quantity", "0.1")
        self.status = kwargs.get("status", "pending")
        self.trading_mode = kwargs.get("trading_mode", "live")
        self.strategy_name = kwargs.get("strategy_name", "trend_v1")
        self.submitted_at = kwargs.get("submitted_at", "2026-01-01T00:00:00")


class TestGetOpenOrders:
    async def test_returns_open_orders_for_trading_mode(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        row = _FakeOrderRow(status="submitted")
        session = _FakeAsyncSession(scalars_result=[row])
        _patch_get_session(mocker, session)

        repo = OrderRepository()
        result = await repo.get_open_orders(TradingMode.LIVE)

        assert len(result) == 1
        assert result[0]["id"] == "order-1"
        assert result[0]["status"] == "submitted"

    async def test_returns_empty_list_when_no_open_orders(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalars_result=[])
        _patch_get_session(mocker, session)

        repo = OrderRepository()
        result = await repo.get_open_orders(TradingMode.PAPER)

        assert result == []


class TestGetActiveStrategyNames:
    async def test_returns_names_of_active_strategies(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalars_result=["trend_following_v1", "mean_reversion_v1"])
        _patch_get_session(mocker, session)

        repo = StrategyRepository()
        result = await repo.get_active_names()

        assert result == ["trend_following_v1", "mean_reversion_v1"]

    async def test_returns_empty_list_when_none_active(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalars_result=[])
        _patch_get_session(mocker, session)

        repo = StrategyRepository()
        result = await repo.get_active_names()

        assert result == []
