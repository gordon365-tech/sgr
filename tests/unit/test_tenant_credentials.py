"""
Tests für sgr.core.tenant_credentials.load_tenant_credentials().

Mock-Muster analog zu tests/unit/test_tenant.py
(TenantManager.get_exchange_adapter) - dieselbe DB-Read +
get_cipher()-Entschlüsselungslogik, hier fuer den worker-seitigen
lifespan()-Startup-Pfad (Commit 5, Option A) statt den Pro-Request-Pfad
im API-Prozess.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sgr.core.tenant_credentials import load_tenant_credentials
from sgr.core.types import ExchangeID, TradingMode

# TENANT_ID muss die tatsaechliche users.id-UUID sein (APIKeyModel.user_id
# ist PG_UUID(as_uuid=False)) - siehe Deployment-Vorfall: TENANT_ID=sumo
# (Klartext-Name statt UUID) fuehrte zu asyncpg.exceptions.DataError statt
# einer verstaendlichen Fehlermeldung. Tests unten nutzen deshalb echte
# UUID-Strings, keine sprechenden Namen.
_GORDON_UUID = "11111111-1111-4111-8111-111111111111"
_SUMO_UUID = "22222222-2222-4222-8222-222222222222"


def _mock_db_ctx(key_record: MagicMock | None) -> AsyncMock:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = key_record
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_db_ctx = AsyncMock()
    mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_db_ctx


class TestLoadTenantCredentialsInvalidTenantId:
    """tenant_id muss eine gueltige UUID sein - siehe
    docker-compose.prod.yml TENANT_ID-Kommentar und der reale
    Deployment-Vorfall, der diesen Test motiviert hat."""

    async def test_non_uuid_tenant_id_raises_before_any_db_query(self) -> None:
        mock_db_ctx = _mock_db_ctx(None)

        with patch("sgr.core.database.get_session", return_value=mock_db_ctx):
            with pytest.raises(ValueError, match="not a valid UUID"):
                await load_tenant_credentials("gordon", ExchangeID.BINANCE, TradingMode.PAPER)

        # Kein DB-Roundtrip fuer einen von vornherein ungueltigen Wert.
        mock_db_ctx.__aenter__.assert_not_awaited()

    async def test_error_message_names_the_env_var_and_offending_value(self) -> None:
        with patch("sgr.core.database.get_session", return_value=_mock_db_ctx(None)):
            with pytest.raises(ValueError, match="TENANT_ID") as exc_info:
                await load_tenant_credentials("sumo", ExchangeID.PIONEX, TradingMode.PAPER)
        assert "'sumo'" in str(exc_info.value)

    async def test_valid_uuid_passes_validation_and_reaches_db(self) -> None:
        mock_db_ctx = _mock_db_ctx(None)

        with patch("sgr.core.database.get_session", return_value=mock_db_ctx):
            with pytest.raises(ValueError, match="No API keys configured"):
                await load_tenant_credentials(_GORDON_UUID, ExchangeID.PIONEX, TradingMode.PAPER)

        mock_db_ctx.__aenter__.assert_awaited_once()


class TestLoadTenantCredentials:
    async def test_raises_when_no_key_record(self) -> None:
        with patch("sgr.core.database.get_session", return_value=_mock_db_ctx(None)):
            with pytest.raises(ValueError, match="No API keys configured"):
                await load_tenant_credentials(_GORDON_UUID, ExchangeID.PIONEX, TradingMode.PAPER)

    async def test_error_message_includes_tenant_and_exchange(self) -> None:
        with patch("sgr.core.database.get_session", return_value=_mock_db_ctx(None)):
            with pytest.raises(ValueError, match=_GORDON_UUID) as exc_info:
                await load_tenant_credentials(
                    _GORDON_UUID, ExchangeID.BINANCE, TradingMode.LIVE
                )
        assert "binance" in str(exc_info.value)
        assert "live" in str(exc_info.value)

    async def test_decrypts_and_returns_credentials(self) -> None:
        key_record = MagicMock()
        key_record.encrypted_api_key = "enc-key"
        key_record.encrypted_secret = "enc-secret"

        mock_cipher = MagicMock()
        mock_cipher.decrypt.side_effect = ["plain-key", "plain-secret"]

        with (
            patch("sgr.core.database.get_session", return_value=_mock_db_ctx(key_record)),
            patch("sgr.core.tenant_credentials.get_cipher", return_value=mock_cipher),
        ):
            result = await load_tenant_credentials(
                _GORDON_UUID, ExchangeID.PIONEX, TradingMode.PAPER
            )

        assert result == {"apiKey": "plain-key", "secret": "plain-secret"}

    async def test_decrypt_uses_tenant_id_as_associated_data(self) -> None:
        """AAD muss die tenant_id sein, nicht user-agnostisch - sonst
        koennte ein falsch zugeordneter DB-Eintrag unbemerkt entschluesselt
        werden (siehe Cipher.decrypt AAD-Pruefung, tests/unit/test_encryption.py)."""
        key_record = MagicMock()
        key_record.encrypted_api_key = "enc-key"
        key_record.encrypted_secret = "enc-secret"

        mock_cipher = MagicMock()
        mock_cipher.decrypt.side_effect = ["plain-key", "plain-secret"]

        with (
            patch("sgr.core.database.get_session", return_value=_mock_db_ctx(key_record)),
            patch("sgr.core.tenant_credentials.get_cipher", return_value=mock_cipher),
        ):
            await load_tenant_credentials(_SUMO_UUID, ExchangeID.PIONEX, TradingMode.PAPER)

        calls = mock_cipher.decrypt.call_args_list
        assert calls[0].kwargs["associated_data"] == _SUMO_UUID.encode()
        assert calls[1].kwargs["associated_data"] == _SUMO_UUID.encode()

    async def test_queries_filter_by_tenant_exchange_mode_and_active(self) -> None:
        key_record = MagicMock()
        key_record.encrypted_api_key = "enc-key"
        key_record.encrypted_secret = "enc-secret"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key_record
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_cipher = MagicMock()
        mock_cipher.decrypt.side_effect = ["k", "s"]

        with (
            patch("sgr.core.database.get_session", return_value=mock_db_ctx),
            patch("sgr.core.tenant_credentials.get_cipher", return_value=mock_cipher),
        ):
            await load_tenant_credentials(_GORDON_UUID, ExchangeID.BINANCE, TradingMode.LIVE)

        mock_db.execute.assert_awaited_once()
