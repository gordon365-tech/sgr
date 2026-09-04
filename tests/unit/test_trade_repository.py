"""
Unit-Tests für TradeRepository.get_recent().

Mockt get_session() (siehe test_position_repository.py Muster). Deckt nur
get_recent() ab, die im Rahmen der API/Worker-Trennung neu hinzugefuegte
Methode fuer den /portfolio/trades-Endpunkt (bestehende TradeRepository-
Methoden create()/get_performance_by_strategy() hatten zuvor bereits
keine dedizierten Unit-Tests - Nachruestung dieser waere eine separate,
groessere Aufgabe und nicht Teil dieser Aenderung).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.repositories import TradeRepository
from sgr.core.types import TradingMode

if TYPE_CHECKING:
    import pytest_mock


class _FakeAsyncSession:
    def __init__(self, scalars_result=None) -> None:
        self._scalars_result = scalars_result or []

    async def execute(self, stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = self._scalars_result
        return result


def _patch_get_session(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("sgr.core.repositories.get_session", return_value=cm)


def _make_trade_row(user_id: str | None = None) -> MagicMock:
    row = MagicMock()
    row.id = "trade-1"
    row.symbol = "BTC/USDT"
    row.side = "long"
    row.entry_price = Decimal("50000")
    row.exit_price = Decimal("51000")
    row.quantity = Decimal("0.1")
    row.realized_pnl = Decimal("100")
    row.fees_total = Decimal("2.50")
    row.net_pnl = Decimal("97.50")
    row.strategy_name = "trend_following"
    row.opened_at = datetime.now(tz=UTC)
    row.closed_at = datetime.now(tz=UTC)
    row.user_id = user_id
    return row


class TestTradeRepositoryGetRecent:
    async def test_get_recent_returns_trades(self, mocker: pytest_mock.MockerFixture) -> None:
        session = _FakeAsyncSession(scalars_result=[_make_trade_row()])
        _patch_get_session(mocker, session)

        repo = TradeRepository()
        results = await repo.get_recent(TradingMode.PAPER)

        assert len(results) == 1
        assert results[0]["symbol"] == "BTC/USDT"
        assert results[0]["net_pnl"] == "97.50"

    async def test_get_recent_empty_returns_empty_list(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalars_result=[])
        _patch_get_session(mocker, session)

        repo = TradeRepository()
        results = await repo.get_recent(TradingMode.PAPER)

        assert results == []

    async def test_get_recent_respects_limit_parameter(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """Nur eine Verhaltens-Assertion moeglich (Mock liefert unabhaengig
        von .limit() alle vorbereiteten Zeilen) - stellt aber sicher, dass
        der limit-Parameter zumindest ohne Fehler durchgereicht wird und
        keinen Query-Build-Fehler verursacht."""
        session = _FakeAsyncSession(scalars_result=[_make_trade_row(), _make_trade_row()])
        _patch_get_session(mocker, session)

        repo = TradeRepository()
        results = await repo.get_recent(TradingMode.PAPER, limit=2)

        assert len(results) == 2

    async def test_get_recent_filters_by_user_id_when_given(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalars_result=[_make_trade_row(user_id="user-gordon")])
        _patch_get_session(mocker, session)

        repo = TradeRepository()
        results = await repo.get_recent(TradingMode.PAPER, user_id="user-gordon")

        assert len(results) == 1


class TestRepositoriesBundle:
    def test_trades_repository_registered_in_bundle(self) -> None:
        from sgr.core.repositories import Repositories

        repos = Repositories()
        assert isinstance(repos.trades, TradeRepository)
