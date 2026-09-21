"""
Tests für sgr.api.dependencies.require_live_2fa.

BUG-FIX-Kontext (Produktions-Audit): require_live_2fa() prüfte bisher nur,
ob der X-TOTP-Code Header gesetzt war - der Code selbst wurde nie gegen
das echte TOTP-Secret des Users verifiziert. Diese Tests decken die neu
verdrahtete echte Verifikation ab (analog zu tests/saas/test_auth.py's
Login-2FA-Tests, gleiches Mock-Muster für repos.users).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from sgr.api.dependencies import TokenData, require_live_2fa
from sgr.core.types import TradingMode
from sgr.saas.auth import AuthService


def _make_db_user(
    *,
    is_2fa_enabled: bool = True,
    totp_secret: str | None,
) -> dict:
    return {
        "id": "user-1",
        "email": "trader@example.com",
        "is_2fa_enabled": is_2fa_enabled,
        "totp_secret": totp_secret,
    }


def _paper_token() -> TokenData:
    return TokenData(user_id="user-1", trading_mode=TradingMode.PAPER)


def _live_token() -> TokenData:
    return TokenData(user_id="user-1", trading_mode=TradingMode.LIVE)


class TestRequireLive2FA:
    async def test_paper_mode_skips_2fa_entirely(self) -> None:
        """Nicht-LIVE Trading Mode braucht kein 2FA - repos wird nicht
        einmal angefragt."""
        repos = MagicMock()
        repos.users.get_by_id = AsyncMock()

        result = await require_live_2fa(user=_paper_token(), repos=repos, x_totp_code=None)

        assert result.user_id == "user-1"
        repos.users.get_by_id.assert_not_called()

    async def test_live_mode_without_header_rejected(self) -> None:
        repos = MagicMock()
        repos.users.get_by_id = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await require_live_2fa(user=_live_token(), repos=repos, x_totp_code=None)

        assert exc_info.value.status_code == 403
        assert "2FA required" in exc_info.value.detail
        repos.users.get_by_id.assert_not_called()

    async def test_live_mode_2fa_not_configured_rejected(self) -> None:
        """Frueher (Placeholder-Bug): jeder nicht-leere Header wurde
        akzeptiert, selbst wenn der User gar kein 2FA eingerichtet hatte.
        Jetzt: fail-closed."""
        repos = MagicMock()
        repos.users.get_by_id = AsyncMock(
            return_value=_make_db_user(is_2fa_enabled=False, totp_secret=None)
        )

        with pytest.raises(HTTPException) as exc_info:
            await require_live_2fa(user=_live_token(), repos=repos, x_totp_code="123456")

        assert exc_info.value.status_code == 403
        assert "not set up" in exc_info.value.detail

    async def test_live_mode_unknown_user_rejected(self) -> None:
        repos = MagicMock()
        repos.users.get_by_id = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await require_live_2fa(user=_live_token(), repos=repos, x_totp_code="123456")

        assert exc_info.value.status_code == 403

    async def test_live_mode_wrong_code_rejected(self) -> None:
        """Kern des Bugs: frueher wurde JEDER beliebige Code akzeptiert,
        solange der Header nur gesetzt war."""
        auth = AuthService()
        secret = auth.generate_totp_secret()
        encrypted = auth.encrypt_totp_secret(secret)

        repos = MagicMock()
        repos.users.get_by_id = AsyncMock(
            return_value=_make_db_user(totp_secret=encrypted)
        )

        with pytest.raises(HTTPException) as exc_info:
            await require_live_2fa(user=_live_token(), repos=repos, x_totp_code="000000")

        assert exc_info.value.status_code == 403
        assert "Invalid 2FA code" in exc_info.value.detail

    async def test_live_mode_correct_code_accepted(self) -> None:
        import pyotp

        auth = AuthService()
        secret = auth.generate_totp_secret()
        encrypted = auth.encrypt_totp_secret(secret)
        code = pyotp.TOTP(secret).now()

        repos = MagicMock()
        repos.users.get_by_id = AsyncMock(
            return_value=_make_db_user(totp_secret=encrypted)
        )

        result = await require_live_2fa(user=_live_token(), repos=repos, x_totp_code=code)

        assert result.user_id == "user-1"
