"""
Unit-Tests für PositionRepository.

Im Gegensatz zu tests/integration/test_repositories.py (echte PostgreSQL
noetig, DB_INTEGRATION_TESTS=1) mocken diese Tests get_session(), um ohne
laufende DB zu funktionieren und zur Standard-Coverage beizutragen.

Testet:
    1. upsert_open: Insert-Pfad (keine offene Position vorhanden)
    2. upsert_open: Update-Pfad (offene Position fuer Symbol existiert schon)
    3. close: markiert als geschlossen, idempotent
    4. get_open_positions: Filterung nach trading_mode (+ optional user_id)
    5. get_by_symbol: Einzelabfrage
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.repositories import PositionRepository
from sgr.core.types import TradingMode

if TYPE_CHECKING:
    import pytest_mock


class _FakeAsyncSession:
    """Minimaler Stand-in für AsyncSession, steuert execute()-Rückgabe."""

    def __init__(self, scalar_result=None, scalars_result=None) -> None:
        self._scalar_result = scalar_result
        self._scalars_result = scalars_result or []
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
        result.scalars.return_value.all.return_value = self._scalars_result
        return result


def _patch_get_session(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    """Patcht get_session() in sgr.core.repositories als Async-Context-Manager."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("sgr.core.repositories.get_session", return_value=cm)


def _make_position_row(**overrides) -> dict:
    base = {
        "id": "pos-1",
        "symbol": "BTC/USDT",
        "exchange": "binance",
        "side": "long",
        "quantity": Decimal("0.1"),
        "entry_price": Decimal("50000"),
        "current_price": Decimal("50000"),
        "leverage": Decimal("1"),
        "unrealized_pnl": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "opened_at": datetime.now(tz=UTC),
        "strategy_name": "test_strategy",
        "trading_mode": TradingMode.PAPER,
    }
    base.update(overrides)
    return base


class TestPositionRepositoryUpsert:
    async def test_upsert_creates_new_when_no_open_position_exists(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalar_result=None)  # keine offene Position -> Insert
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        position_id = await repo.upsert_open(_make_position_row())

        assert position_id == "pos-1"
        assert len(session.added) == 1

    async def test_upsert_updates_existing_open_position(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        existing = MagicMock()
        existing.id = "existing-pos-id"
        session = _FakeAsyncSession(scalar_result=existing)  # offene Position existiert -> Update
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        position_id = await repo.upsert_open(_make_position_row())

        assert position_id == "existing-pos-id"
        assert len(session.added) == 0  # kein neuer Insert
        assert len(session.executed_statements) == 2  # select + update

    async def test_upsert_generates_id_when_not_provided(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalar_result=None)
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        row = _make_position_row()
        del row["id"]
        position_id = await repo.upsert_open(row)

        assert position_id  # nicht leer, UUID generiert
        assert len(session.added) == 1


class TestPositionRepositoryClose:
    async def test_close_sets_is_open_false(self, mocker: pytest_mock.MockerFixture) -> None:
        session = _FakeAsyncSession()
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        await repo.close("pos-1", closed_at=datetime.now(tz=UTC), realized_pnl=Decimal("100"))

        assert len(session.executed_statements) == 1

    async def test_close_without_realized_pnl_still_works(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """realized_pnl ist optional (z.B. wenn bereits vorher gesetzt)."""
        session = _FakeAsyncSession()
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        await repo.close("pos-1", closed_at=datetime.now(tz=UTC))

        assert len(session.executed_statements) == 1


class TestPositionRepositoryQueries:
    async def test_get_open_positions_returns_mapped_dicts(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        row = MagicMock()
        row.id = "pos-1"
        row.symbol = "BTC/USDT"
        row.exchange = "binance"
        row.side = "long"
        row.quantity = Decimal("0.1")
        row.entry_price = Decimal("50000")
        row.current_price = Decimal("51000")
        row.leverage = Decimal("1")
        row.unrealized_pnl = Decimal("100")
        row.realized_pnl = Decimal("0")
        row.is_open = True
        row.opened_at = datetime.now(tz=UTC)
        row.closed_at = None
        row.strategy_name = "test"
        row.trading_mode = "paper"
        row.user_id = None

        session = _FakeAsyncSession(scalars_result=[row])
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        results = await repo.get_open_positions(TradingMode.PAPER)

        assert len(results) == 1
        assert results[0]["symbol"] == "BTC/USDT"
        assert results[0]["id"] == "pos-1"

    async def test_get_open_positions_empty_returns_empty_list(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalars_result=[])
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        results = await repo.get_open_positions(TradingMode.PAPER)

        assert results == []

    async def test_get_by_symbol_found(self, mocker: pytest_mock.MockerFixture) -> None:
        row = MagicMock()
        row.id = "pos-1"
        row.symbol = "BTC/USDT"
        row.exchange = "binance"
        row.side = "long"
        row.quantity = Decimal("0.1")
        row.entry_price = Decimal("50000")
        row.current_price = Decimal("51000")
        row.leverage = Decimal("1")
        row.unrealized_pnl = Decimal("100")
        row.realized_pnl = Decimal("0")
        row.is_open = True
        row.opened_at = datetime.now(tz=UTC)
        row.closed_at = None
        row.strategy_name = "test"
        row.trading_mode = "paper"
        row.user_id = None

        session = _FakeAsyncSession(scalar_result=row)
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        result = await repo.get_by_symbol("BTC/USDT", "binance", TradingMode.PAPER)

        assert result is not None
        assert result["symbol"] == "BTC/USDT"

    async def test_get_by_symbol_not_found_returns_none(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(scalar_result=None)
        _patch_get_session(mocker, session)

        repo = PositionRepository()
        result = await repo.get_by_symbol("BTC/USDT", "binance", TradingMode.PAPER)

        assert result is None


class TestRepositoriesBundle:
    def test_positions_repository_registered_in_bundle(self) -> None:
        """PositionRepository muss im zentralen Repositories-Bundle registriert sein."""
        from sgr.core.repositories import Repositories

        repos = Repositories()
        assert isinstance(repos.positions, PositionRepository)
