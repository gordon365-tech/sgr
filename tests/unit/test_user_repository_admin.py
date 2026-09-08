"""
Unit-Tests für UserRepository.set_admin_status() (siehe
scripts/grant_admin.py und alembic/versions/0004_user_is_admin.py).

Folgt dem Mocking-Muster aus test_order_strategy_repositories.py -
gemockte AsyncSession statt einer echten DB-Verbindung (die
Integration-Test-Variante liegt in
tests/integration/test_repositories.py, läuft nur mit
DB_INTEGRATION_TESTS=1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.repositories import UserRepository

if TYPE_CHECKING:
    import pytest_mock


class _FakeAsyncSession:
    """Minimaler Stand-in für AsyncSession, steuert execute()-Rückgabe."""

    def __init__(self, rowcount: int) -> None:
        self._rowcount = rowcount
        self.executed_statements: list = []

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        result = MagicMock()
        result.rowcount = self._rowcount
        return result


def _patch_get_session(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("sgr.core.repositories.get_session", return_value=cm)


class TestSetAdminStatus:
    async def test_returns_true_when_user_updated(self, mocker: pytest_mock.MockerFixture) -> None:
        session = _FakeAsyncSession(rowcount=1)
        _patch_get_session(mocker, session)

        repo = UserRepository()
        result = await repo.set_admin_status("gordon@sgr-trading.example", is_admin=True)

        assert result is True
        assert len(session.executed_statements) == 1

    async def test_returns_false_when_no_user_matched(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession(rowcount=0)
        _patch_get_session(mocker, session)

        repo = UserRepository()
        result = await repo.set_admin_status("nobody@never.com", is_admin=True)

        assert result is False

    async def test_handles_none_rowcount_as_false(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """Manche DBAPI-Treiber liefern rowcount=None statt 0 fuer
        UPDATE-Statements ohne Treffer - muss ebenfalls als 'kein User
        aktualisiert' gewertet werden, nicht als Fehler."""
        session = _FakeAsyncSession(rowcount=None)  # type: ignore[arg-type]
        _patch_get_session(mocker, session)

        repo = UserRepository()
        result = await repo.set_admin_status("nobody@never.com", is_admin=True)

        assert result is False

    async def test_revoke_also_executes_update(self, mocker: pytest_mock.MockerFixture) -> None:
        session = _FakeAsyncSession(rowcount=1)
        _patch_get_session(mocker, session)

        repo = UserRepository()
        result = await repo.set_admin_status("gordon@sgr-trading.example", is_admin=False)

        assert result is True
