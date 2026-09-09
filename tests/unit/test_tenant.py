"""
Tests für sgr.saas.tenant – TenantManager.store_api_key() / Singleton.

Historie: enthielt urspruenglich zusaetzlich TenantSession und
get_or_create_session/get_exchange_adapter-Tests fuer ein
Pro-Request-In-Memory-Engine-Modell im API-Prozess. Nach Audit-
Entscheidung (Option 1, Commit nach Idempotency-Review) wurde dieser
tote Code vollstaendig entfernt statt nur markiert - siehe
sgr/saas/tenant.py Modul-Docstring fuer die vollstaendige Begruendung.
Nur store_api_key() (aktiv genutzt vom apikey_router) bleibt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sgr.core.types import TradingMode
from sgr.saas import tenant as tenant_module
from sgr.saas.tenant import TenantManager, get_tenant_manager


class TestStoreApiKey:
    @pytest.mark.asyncio
    async def test_store_api_key_encrypts_and_persists(self) -> None:
        manager = TenantManager()

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()

        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_cipher = MagicMock()
        mock_cipher.encrypt.side_effect = ["enc-key", "enc-secret"]

        with (
            patch("sgr.core.database.get_session", return_value=mock_db_ctx),
            patch("sgr.saas.tenant.get_cipher", return_value=mock_cipher),
        ):
            key_id = await manager.store_api_key(
                user_id="u1",
                exchange_id="pionex",
                trading_mode=TradingMode.PAPER,
                api_key="raw-key",
                secret="raw-secret",
                label="main",
            )

        assert isinstance(key_id, str)
        assert len(key_id) > 0
        mock_db.execute.assert_awaited_once()
        assert mock_cipher.encrypt.call_count == 2

    @pytest.mark.asyncio
    async def test_store_api_key_default_label(self) -> None:
        manager = TenantManager()

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_cipher = MagicMock()
        mock_cipher.encrypt.side_effect = ["enc-key", "enc-secret"]

        with (
            patch("sgr.core.database.get_session", return_value=mock_db_ctx),
            patch("sgr.saas.tenant.get_cipher", return_value=mock_cipher),
        ):
            key_id = await manager.store_api_key(
                user_id="u1",
                exchange_id="pionex",
                trading_mode=TradingMode.LIVE,
                api_key="raw-key",
                secret="raw-secret",
            )

        assert isinstance(key_id, str)

    @pytest.mark.asyncio
    async def test_store_api_key_uses_user_id_as_associated_data(self) -> None:
        """AAD muss die user_id sein - sonst koennte ein falsch
        zugeordneter Key unbemerkt entschluesselt werden (siehe
        Cipher.decrypt AAD-Pruefung, tests/unit/test_encryption.py)."""
        manager = TenantManager()

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_cipher = MagicMock()
        mock_cipher.encrypt.side_effect = ["enc-key", "enc-secret"]

        with (
            patch("sgr.core.database.get_session", return_value=mock_db_ctx),
            patch("sgr.saas.tenant.get_cipher", return_value=mock_cipher),
        ):
            await manager.store_api_key(
                user_id="gordon-uuid",
                exchange_id="binance",
                trading_mode=TradingMode.PAPER,
                api_key="raw-key",
                secret="raw-secret",
            )

        calls = mock_cipher.encrypt.call_args_list
        assert calls[0].kwargs["associated_data"] == b"gordon-uuid"
        assert calls[1].kwargs["associated_data"] == b"gordon-uuid"


class TestSingleton:
    def test_get_tenant_manager_returns_singleton(self) -> None:
        tenant_module._manager = None
        try:
            first = get_tenant_manager()
            second = get_tenant_manager()
            assert first is second
            assert isinstance(first, TenantManager)
        finally:
            tenant_module._manager = None
