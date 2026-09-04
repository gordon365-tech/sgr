"""
Unit-Tests für PortfolioSnapshotRepository.

Mockt get_session() (siehe test_position_repository.py Muster), keine
echte DB noetig. Testet create() und get_latest() inkl. user_id-Filterung
(user_id=None -> globaler/Nicht-Multi-Tenant-Snapshot; user_id gesetzt ->
pro-User Snapshot fuer die kommende Gordon/Sumo-Multi-Instance-Trennung).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.repositories import PortfolioSnapshotRepository
from sgr.core.types import TradingMode

if TYPE_CHECKING:
    import pytest_mock


class _FakeAsyncSession:
    """Minimaler Stand-in für AsyncSession, steuert execute()-Rückgabe."""

    def __init__(self, scalar_result=None, scalars_result=None) -> None:
        self._scalar_result = scalar_result
        self._scalars_result = scalars_result or []
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def execute(self, stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._scalar_result
        result.scalars.return_value.all.return_value = self._scalars_result
        return result


def _patch_get_session(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("sgr.core.repositories.get_session", return_value=cm)


def _make_row(user_id: str | None = None) -> MagicMock:
    row = MagicMock()
    row.id = "snap-1"
    row.user_id = user_id
    row.trading_mode = "paper"
    row.portfolio_value = Decimal("10500.25")
    row.cash = Decimal("5000.00")
    row.unrealized_pnl = Decimal("125.50")
    row.peak_value = Decimal("11000.00")
    row.drawdown = Decimal("0.045")
    row.open_positions_count = 3
    row.total_trades = 42
    row.created_at = datetime.now(tz=UTC)
    return row


class TestPortfolioSnapshotRepositoryCreate:
    async def test_create_inserts_snapshot_and_returns_id(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session(mocker, session)

        repo = PortfolioSnapshotRepository()
        snapshot_id = await repo.create(
            {
                "trading_mode": "paper",
                "portfolio_value": Decimal("10000"),
                "cash": Decimal("10000"),
                "unrealized_pnl": Decimal("0"),
                "peak_value": Decimal("10000"),
                "drawdown": Decimal("0"),
                "open_positions_count": 0,
                "total_trades": 0,
                "created_at": datetime.now(tz=UTC),
            }
        )

        assert snapshot_id is not None
        assert len(session.added) == 1

    async def test_create_uses_provided_id_when_given(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session(mocker, session)

        repo = PortfolioSnapshotRepository()
        snapshot_id = await repo.create(
            {
                "id": "explicit-id-123",
                "trading_mode": "paper",
                "portfolio_value": Decimal("10000"),
                "cash": Decimal("10000"),
                "unrealized_pnl": Decimal("0"),
                "peak_value": Decimal("10000"),
                "drawdown": Decimal("0"),
                "open_positions_count": 0,
                "total_trades": 0,
                "created_at": datetime.now(tz=UTC),
            }
        )

        assert snapshot_id == "explicit-id-123"


class TestPortfolioSnapshotRepositoryGetLatest:
    async def test_get_latest_returns_most_recent_snapshot(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        row = _make_row()
        session = _FakeAsyncSession(scalar_result=row)
        _patch_get_session(mocker, session)

        repo = PortfolioSnapshotRepository()
        result = await repo.get_latest(TradingMode.PAPER)

        assert result is not None
        assert result["portfolio_value"] == Decimal("10500.25")
        assert result["cash"] == Decimal("5000.00")
        assert result["drawdown"] == Decimal("0.045")
        assert result["open_positions_count"] == 3

    async def test_get_latest_returns_none_when_no_snapshot_exists(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """Erwartet z.B. direkt nach Deployment, bevor der Worker den ersten
        Snapshot geschrieben hat - Router muessen das sauber behandeln."""
        session = _FakeAsyncSession(scalar_result=None)
        _patch_get_session(mocker, session)

        repo = PortfolioSnapshotRepository()
        result = await repo.get_latest(TradingMode.PAPER)

        assert result is None

    async def test_get_latest_filters_by_user_id_when_given(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        row = _make_row(user_id="user-gordon")
        session = _FakeAsyncSession(scalar_result=row)
        _patch_get_session(mocker, session)

        repo = PortfolioSnapshotRepository()
        result = await repo.get_latest(TradingMode.PAPER, user_id="user-gordon")

        assert result is not None
        assert result["user_id"] == "user-gordon"

    async def test_get_latest_without_user_id_queries_null_user_id(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """Ohne user_id muss explizit nach user_id IS NULL gefiltert werden,
        NICHT einfach der user_id-Filter weggelassen - sonst wuerde bei
        Multi-Instance versehentlich irgendein/jeder User-Snapshot
        zurueckgegeben statt des globalen Legacy-Snapshots."""
        row = _make_row(user_id=None)
        session = _FakeAsyncSession(scalar_result=row)
        _patch_get_session(mocker, session)

        repo = PortfolioSnapshotRepository()
        result = await repo.get_latest(TradingMode.PAPER)

        assert result is not None
        assert result["user_id"] is None


class TestRepositoriesBundle:
    def test_portfolio_snapshots_repository_registered_in_bundle(self) -> None:
        """PortfolioSnapshotRepository muss im zentralen Repositories-Bundle
        registriert sein, damit Router/Worker sie einheitlich ueber
        get_repositories() beziehen koennen."""
        from sgr.core.repositories import PortfolioSnapshotRepository, Repositories

        repos = Repositories()
        assert isinstance(repos.portfolio_snapshots, PortfolioSnapshotRepository)
