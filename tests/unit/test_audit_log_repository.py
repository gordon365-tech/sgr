"""
Unit-Tests für AuditLogRepository.

Backing-Store für sgr.core.security.audit_log() - ersetzt das zuvor
kaputte rohe SQL-Statement (siehe test_security.py Modul-Docstring) durch
eine reguläre Repository-Methode, konsistent mit RiskEventRepository.

Folgt dem Mocking-Muster aus test_position_repository.py (mockt
get_session(), keine echte DB noetig).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sgr.core.repositories import AuditLogRepository, Repositories

if TYPE_CHECKING:
    import pytest_mock


class _FakeAsyncSession:
    """Minimaler Stand-in für AsyncSession - nur add() wird gebraucht."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)


def _patch_get_session(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("sgr.core.repositories.get_session", return_value=cm)


class TestAuditLogRepository:
    async def test_log_action_adds_entry_with_given_fields(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session(mocker, session)

        repo = AuditLogRepository()
        await repo.log_action(
            action="login",
            user_id="user-1",
            details={"ip": "1.2.3.4"},
        )

        assert len(session.added) == 1
        entry = session.added[0]
        assert entry.action == "login"
        assert entry.user_id == "user-1"
        assert entry.details == {"ip": "1.2.3.4"}

    async def test_log_action_defaults_user_id_to_system(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session(mocker, session)

        repo = AuditLogRepository()
        await repo.log_action(action="config_changed")

        entry = session.added[0]
        assert entry.user_id == "system"
        assert entry.details == {}


class TestRepositoriesBundleAuditLog:
    def test_audit_log_repository_registered_in_bundle(self) -> None:
        repos = Repositories()
        assert isinstance(repos.audit_log, AuditLogRepository)
