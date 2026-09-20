"""
Unit-Tests für GridRepository (siehe tests/unit/test_position_repository.py
für das identische Mocking-Muster - get_session() wird gemockt, damit die
Tests ohne laufende DB funktionieren).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.grid_types import GridState
from sgr.core.repositories import GridRepository
from sgr.core.types import ExchangeID, GridDirection, GridStatus, Symbol, TradingMode

if TYPE_CHECKING:
    import pytest_mock


class _FakeAsyncSession:
    def __init__(self, scalar_result=None) -> None:
        self._scalar_result = scalar_result
        self.added: list = []
        self.executed_statements: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._scalar_result
        result.scalars.return_value.all.return_value = (
            [self._scalar_result] if self._scalar_result else []
        )
        return result


def _patch_get_session(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("sgr.core.repositories.get_session", return_value=cm)


def _make_grid_state() -> GridState:
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)
    return GridState(
        tenant_id="gordon",
        exchange=ExchangeID.PIONEX,
        symbol=symbol,
        strategy_name="futures_grid_long_v1",
        trading_mode=TradingMode.PAPER,
        direction=GridDirection.LONG,
        status=GridStatus.ACTIVE,
        parameters={"leverage": "2", "total_notional": "250"},
        opened_at=datetime.now(tz=UTC),
    )


class TestGridRepositoryUpsert:
    async def test_creates_new_row_when_not_found(self, mocker: pytest_mock.MockerFixture) -> None:
        session = _FakeAsyncSession(scalar_result=None)
        _patch_get_session(mocker, session)

        repo = GridRepository()
        grid = _make_grid_state()
        grid_id = await repo.upsert(grid)

        assert grid_id == str(grid.id)
        assert len(session.added) == 1

    async def test_updates_existing_row_when_found(self, mocker: pytest_mock.MockerFixture) -> None:
        existing = MagicMock()
        session = _FakeAsyncSession(scalar_result=existing)
        _patch_get_session(mocker, session)

        repo = GridRepository()
        grid = _make_grid_state()
        grid_id = await repo.upsert(grid)

        assert grid_id == str(grid.id)
        assert len(session.added) == 0
        assert len(session.executed_statements) == 2  # select + update

    async def test_upsert_maps_tenant_id_to_user_id(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalar_result=None)
        _patch_get_session(mocker, session)

        repo = GridRepository()
        grid = _make_grid_state()
        await repo.upsert(grid)

        row = session.added[0]
        assert row.user_id == "gordon"
        assert row.status == "active"
        assert row.direction == "long"


class TestGridRepositoryRecordLevelFill:
    async def test_records_a_fill_row(self, mocker: pytest_mock.MockerFixture) -> None:
        session = _FakeAsyncSession()
        _patch_get_session(mocker, session)

        repo = GridRepository()
        fill_id = await repo.record_level_fill(
            grid_id="grid-1",
            level_index=2,
            price=Decimal("100"),
            side="buy",
            quantity=Decimal("0.5"),
            is_opening=True,
            filled_at=datetime.now(tz=UTC),
        )

        assert fill_id
        assert len(session.added) == 1
        assert session.added[0].grid_id == "grid-1"
        assert session.added[0].level_index == 2


class TestGridRepositoryQueries:
    async def test_get_by_id_returns_none_when_missing(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalar_result=None)
        _patch_get_session(mocker, session)

        repo = GridRepository()
        result = await repo.get_by_id("missing-id")

        assert result is None
