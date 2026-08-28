"""
Tests für sgr.saas.tenant – TenantSession / TenantManager.
Coverage-Ziel: 27% -> 100%.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sgr.core.types import ExchangeID, TradingMode
from sgr.saas import tenant as tenant_module
from sgr.saas.tenant import (
    TenantManager,
    TenantSession,
    get_tenant_manager,
)
from sgr.saas.types import TenantConfig

# ===========================================================================
# TenantSession
# ===========================================================================


class TestTenantSession:
    def test_init_creates_per_tenant_engines(self) -> None:
        config = TenantConfig(user_id="u1")
        session = TenantSession("u1", TradingMode.PAPER, config)

        assert session.user_id == "u1"
        assert session.trading_mode == TradingMode.PAPER
        assert session.config is config
        assert session.portfolio_engine is not None
        assert session.risk_engine is not None
        assert session.kill_switch is not None
        assert session._adapters == {}

    def test_touch_updates_last_activity(self) -> None:
        config = TenantConfig(user_id="u1")
        session = TenantSession("u1", TradingMode.PAPER, config)
        old_activity = session.last_activity - timedelta(minutes=5)
        session.last_activity = old_activity

        session.touch()

        assert session.last_activity > old_activity

    def test_is_idle_false_when_recently_active(self) -> None:
        config = TenantConfig(user_id="u1")
        session = TenantSession("u1", TradingMode.PAPER, config)
        session.last_activity = datetime.now(tz=UTC)

        assert session.is_idle is False

    def test_is_idle_true_when_past_timeout(self) -> None:
        config = TenantConfig(user_id="u1")
        session = TenantSession("u1", TradingMode.PAPER, config)
        session.last_activity = datetime.now(tz=UTC) - timedelta(minutes=31)

        assert session.is_idle is True

    @pytest.mark.asyncio
    async def test_initialize_calls_risk_engine_initialize(self) -> None:
        config = TenantConfig(user_id="u1")
        session = TenantSession("u1", TradingMode.PAPER, config)
        session.risk_engine.initialize = AsyncMock()  # type: ignore[method-assign]

        await session.initialize()

        session.risk_engine.initialize.assert_awaited_once()


# ===========================================================================
# TenantManager – Lifecycle (start/stop)
# ===========================================================================


class TestTenantManagerLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_cleanup_task(self) -> None:
        manager = TenantManager()
        await manager.start()

        assert manager._cleanup_task is not None
        assert not manager._cleanup_task.done()

        await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_cleanup_task(self) -> None:
        manager = TenantManager()
        await manager.start()
        task = manager._cleanup_task

        await manager.stop()

        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self) -> None:
        manager = TenantManager()
        # Should not raise even though no cleanup task was ever started
        await manager.stop()


# ===========================================================================
# TenantManager – Session Management
# ===========================================================================


class TestSessionManagement:
    @pytest.mark.asyncio
    async def test_get_or_create_session_creates_new(self) -> None:
        manager = TenantManager()

        session = await manager.get_or_create_session("u1", TradingMode.PAPER)

        assert session.user_id == "u1"
        assert manager.active_sessions == 1

    @pytest.mark.asyncio
    async def test_get_or_create_session_returns_existing(self) -> None:
        manager = TenantManager()
        first = await manager.get_or_create_session("u1", TradingMode.PAPER)
        old_activity = first.last_activity - timedelta(minutes=5)
        first.last_activity = old_activity

        second = await manager.get_or_create_session("u1", TradingMode.PAPER)

        assert second is first
        assert manager.active_sessions == 1
        assert second.last_activity > old_activity

    @pytest.mark.asyncio
    async def test_get_or_create_session_uses_provided_config(self) -> None:
        manager = TenantManager()
        config = TenantConfig(user_id="u1", max_open_positions=42)

        session = await manager.get_or_create_session("u1", TradingMode.PAPER, tenant_config=config)

        assert session.config.max_open_positions == 42

    @pytest.mark.asyncio
    async def test_get_or_create_session_default_config_when_none(self) -> None:
        manager = TenantManager()

        session = await manager.get_or_create_session("u1", TradingMode.PAPER)

        assert session.config.user_id == "u1"

    @pytest.mark.asyncio
    async def test_get_or_create_session_evicts_when_cache_full(self) -> None:
        manager = TenantManager()
        with patch.object(tenant_module, "_MAX_TENANT_CACHE", 1):
            await manager.get_or_create_session("u1", TradingMode.PAPER)
            assert manager.active_sessions == 1

            await manager.get_or_create_session("u2", TradingMode.PAPER)

            # Oldest (u1) evicted, cache limit respected, u2 present
            assert manager.active_sessions == 1
            assert manager.get_session("u2") is not None
            assert "u1" not in manager._sessions

    def test_get_session_returns_none_when_missing(self) -> None:
        manager = TenantManager()

        assert manager.get_session("ghost") is None

    @pytest.mark.asyncio
    async def test_get_session_touches_existing(self) -> None:
        manager = TenantManager()
        session = await manager.get_or_create_session("u1", TradingMode.PAPER)
        old_activity = session.last_activity - timedelta(minutes=5)
        session.last_activity = old_activity

        found = manager.get_session("u1")

        assert found is session
        assert found.last_activity > old_activity

    @pytest.mark.asyncio
    async def test_destroy_session_removes_existing(self) -> None:
        manager = TenantManager()
        await manager.get_or_create_session("u1", TradingMode.PAPER)

        await manager.destroy_session("u1")

        assert manager.get_session("u1") is None
        assert manager.active_sessions == 0

    @pytest.mark.asyncio
    async def test_destroy_session_noop_when_missing(self) -> None:
        manager = TenantManager()
        # Should not raise
        await manager.destroy_session("ghost")
        assert manager.active_sessions == 0


# ===========================================================================
# TenantManager – Exchange Adapter / API Key Management
# ===========================================================================


class TestExchangeAdapterManagement:
    @pytest.mark.asyncio
    async def test_get_exchange_adapter_raises_without_session(self) -> None:
        manager = TenantManager()

        with pytest.raises(ValueError, match="No session for user"):
            await manager.get_exchange_adapter("ghost", ExchangeID.PIONEX, TradingMode.PAPER)

    @pytest.mark.asyncio
    async def test_get_exchange_adapter_returns_cached(self) -> None:
        manager = TenantManager()
        session = await manager.get_or_create_session("u1", TradingMode.PAPER)
        cached_adapter = MagicMock()
        session._adapters["pionex:paper"] = cached_adapter

        adapter = await manager.get_exchange_adapter("u1", ExchangeID.PIONEX, TradingMode.PAPER)

        assert adapter is cached_adapter

    @pytest.mark.asyncio
    async def test_get_exchange_adapter_raises_when_no_key_record(self) -> None:
        manager = TenantManager()
        await manager.get_or_create_session("u1", TradingMode.PAPER)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("sgr.core.database.get_session", return_value=mock_db_ctx):
            with pytest.raises(ValueError, match="No API keys configured"):
                await manager.get_exchange_adapter("u1", ExchangeID.PIONEX, TradingMode.PAPER)

    @pytest.mark.asyncio
    async def test_get_exchange_adapter_creates_and_caches(self) -> None:
        manager = TenantManager()
        session = await manager.get_or_create_session("u1", TradingMode.PAPER)

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
        mock_cipher.decrypt.side_effect = ["plain-key", "plain-secret"]

        mock_adapter = AsyncMock()
        mock_adapter.connect = AsyncMock()

        mock_factory = MagicMock()
        mock_factory.create_with_credentials.return_value = mock_adapter

        with (
            patch("sgr.core.database.get_session", return_value=mock_db_ctx),
            patch("sgr.saas.tenant.get_cipher", return_value=mock_cipher),
            patch("sgr.exchanges.factory.ExchangeFactory", mock_factory),
        ):
            adapter = await manager.get_exchange_adapter("u1", ExchangeID.PIONEX, TradingMode.PAPER)

        assert adapter is mock_adapter
        mock_adapter.connect.assert_awaited_once()
        mock_factory.create_with_credentials.assert_called_once_with(
            exchange_id=ExchangeID.PIONEX,
            trading_mode=TradingMode.PAPER,
            api_key="plain-key",
            secret="plain-secret",
        )
        # Cached für nächsten Aufruf
        assert session._adapters["pionex:paper"] is mock_adapter

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


# ===========================================================================
# TenantManager – Stats
# ===========================================================================


class TestStats:
    @pytest.mark.asyncio
    async def test_active_sessions_reflects_count(self) -> None:
        manager = TenantManager()
        assert manager.active_sessions == 0

        await manager.get_or_create_session("u1", TradingMode.PAPER)
        await manager.get_or_create_session("u2", TradingMode.PAPER)

        assert manager.active_sessions == 2

    def test_get_stats_returns_expected_shape(self) -> None:
        manager = TenantManager()

        stats = manager.get_stats()

        assert stats == {
            "active_sessions": 0,
            "max_sessions": 1000,
            "idle_timeout_minutes": 30,
        }


# ===========================================================================
# TenantManager – Cleanup
# ===========================================================================


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_idle_removes_idle_sessions(self) -> None:
        manager = TenantManager()
        session = await manager.get_or_create_session("u1", TradingMode.PAPER)
        session.last_activity = datetime.now(tz=UTC) - timedelta(minutes=31)
        await manager.get_or_create_session("u2", TradingMode.PAPER)

        await manager._cleanup_idle()

        assert manager.get_session("u1") is None
        assert manager.get_session("u2") is not None

    @pytest.mark.asyncio
    async def test_cleanup_idle_noop_when_none_idle(self) -> None:
        manager = TenantManager()
        await manager.get_or_create_session("u1", TradingMode.PAPER)

        await manager._cleanup_idle()

        assert manager.active_sessions == 1

    @pytest.mark.asyncio
    async def test_evict_oldest_removes_least_recently_active(self) -> None:
        manager = TenantManager()
        s1 = await manager.get_or_create_session("u1", TradingMode.PAPER)
        s1.last_activity = datetime.now(tz=UTC) - timedelta(minutes=10)
        await manager.get_or_create_session("u2", TradingMode.PAPER)

        await manager._evict_oldest()

        assert manager.get_session("u1") is None
        assert manager.get_session("u2") is not None

    @pytest.mark.asyncio
    async def test_evict_oldest_noop_when_empty(self) -> None:
        manager = TenantManager()
        # Should not raise on empty session dict
        await manager._evict_oldest()
        assert manager.active_sessions == 0

    @pytest.mark.asyncio
    async def test_cleanup_loop_runs_and_survives_exception(self) -> None:
        manager = TenantManager()
        call_count = 0

        async def fake_sleep(_seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(manager, "_cleanup_idle", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            with pytest.raises(asyncio.CancelledError):
                await manager._cleanup_loop()

        assert call_count == 2


# ===========================================================================
# Singleton
# ===========================================================================


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
