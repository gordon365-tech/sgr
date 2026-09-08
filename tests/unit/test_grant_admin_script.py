"""
Tests für scripts/grant_admin.py.

Nicht Teil des sgr-Pakets (--cov=sgr erfasst scripts/ nicht), aber die
Kernlogik (_main()) wird trotzdem verifiziert, da ein fehlerhaftes
Admin-Vergabe-Skript ein Sicherheits-relevantes Werkzeug waere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# scripts/ liegt nicht im Package-Pfad - direkt importierbar machen.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import grant_admin  # noqa: E402


class TestGrantAdminMain:
    async def test_grants_admin_by_default(self) -> None:
        repos = MagicMock()
        repos.users.set_admin_status = AsyncMock(return_value=True)

        with (
            patch("sgr.core.database.init_db", new=AsyncMock()),
            patch("sgr.core.database.close_db", new=AsyncMock()),
            patch("sgr.core.repositories.get_repositories", return_value=repos),
        ):
            exit_code = await grant_admin._main("gordon@sgr-trading.example", revoke=False)

        assert exit_code == 0
        repos.users.set_admin_status.assert_awaited_once_with(
            "gordon@sgr-trading.example", is_admin=True
        )

    async def test_revoke_flag_sets_is_admin_false(self) -> None:
        repos = MagicMock()
        repos.users.set_admin_status = AsyncMock(return_value=True)

        with (
            patch("sgr.core.database.init_db", new=AsyncMock()),
            patch("sgr.core.database.close_db", new=AsyncMock()),
            patch("sgr.core.repositories.get_repositories", return_value=repos),
        ):
            exit_code = await grant_admin._main("gordon@sgr-trading.example", revoke=True)

        assert exit_code == 0
        repos.users.set_admin_status.assert_awaited_once_with(
            "gordon@sgr-trading.example", is_admin=False
        )

    async def test_unknown_email_returns_nonzero_exit_code(self) -> None:
        repos = MagicMock()
        repos.users.set_admin_status = AsyncMock(return_value=False)

        with (
            patch("sgr.core.database.init_db", new=AsyncMock()),
            patch("sgr.core.database.close_db", new=AsyncMock()),
            patch("sgr.core.repositories.get_repositories", return_value=repos),
        ):
            exit_code = await grant_admin._main("nobody@never.com", revoke=False)

        assert exit_code == 1

    async def test_closes_db_even_if_update_raises(self) -> None:
        """close_db() muss auch bei einem Fehler aufgerufen werden (finally-
        Block) - keine haengenden DB-Connections bei einem fehlgeschlagenen
        Admin-Grant."""
        repos = MagicMock()
        repos.users.set_admin_status = AsyncMock(side_effect=RuntimeError("db error"))
        close_db_mock = AsyncMock()

        with (
            patch("sgr.core.database.init_db", new=AsyncMock()),
            patch("sgr.core.database.close_db", new=close_db_mock),
            patch("sgr.core.repositories.get_repositories", return_value=repos),
            pytest.raises(RuntimeError, match="db error"),
        ):
            await grant_admin._main("gordon@sgr-trading.example", revoke=False)

        close_db_mock.assert_awaited_once()
