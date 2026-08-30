"""
Tests für sgr.risk.symbol_kill_switch.SymbolKillSwitch.

Neuer Baustein (Phase 2 - Live Trading Safety): Symbol/Strategy-Level
Kill Switch. Strategy-Level existierte bereits vollständig in
StrategyRegistry.deactivate() - dieses Modul schließt die Lücke für
Symbol-Level, analog zum selben Muster.
"""

from __future__ import annotations

import pytest

from sgr.risk.symbol_kill_switch import SymbolKillSwitch, get_symbol_kill_switch


@pytest.fixture
def sks() -> SymbolKillSwitch:
    """Frische Instanz statt Singleton, um Testisolation sicherzustellen."""
    return SymbolKillSwitch()


class TestDefaultState:
    def test_unknown_symbol_is_active_by_default(self, sks: SymbolKillSwitch) -> None:
        assert sks.is_active("pionex:BTC/USDT") is True

    def test_get_entry_returns_none_for_unknown_symbol(self, sks: SymbolKillSwitch) -> None:
        assert sks.get_entry("pionex:BTC/USDT") is None

    def test_get_all_empty_initially(self, sks: SymbolKillSwitch) -> None:
        assert sks.get_all() == {}

    def test_get_deactivated_empty_initially(self, sks: SymbolKillSwitch) -> None:
        assert sks.get_deactivated() == []


@pytest.mark.asyncio
class TestDeactivate:
    async def test_deactivate_makes_symbol_inactive(self, sks: SymbolKillSwitch) -> None:
        await sks.deactivate("pionex:BTC/USDT", "anomalous behavior")
        assert sks.is_active("pionex:BTC/USDT") is False

    async def test_deactivate_records_reason_and_actor(self, sks: SymbolKillSwitch) -> None:
        await sks.deactivate("pionex:BTC/USDT", "manual stop", deactivated_by="user:admin")

        entry = sks.get_entry("pionex:BTC/USDT")
        assert entry is not None
        assert entry.reason == "manual stop"
        assert entry.deactivated_by == "user:admin"
        assert entry.deactivated_at is not None

    async def test_deactivate_only_affects_targeted_symbol(self, sks: SymbolKillSwitch) -> None:
        await sks.deactivate("pionex:BTC/USDT", "reason")
        assert sks.is_active("pionex:ETH/USDT") is True

    async def test_deactivate_is_idempotent(self, sks: SymbolKillSwitch) -> None:
        await sks.deactivate("pionex:BTC/USDT", "first reason")
        await sks.deactivate("pionex:BTC/USDT", "second reason")

        entry = sks.get_entry("pionex:BTC/USDT")
        assert entry is not None
        assert entry.reason == "second reason"
        assert sks.is_active("pionex:BTC/USDT") is False

    async def test_get_deactivated_lists_only_inactive_symbols(
        self, sks: SymbolKillSwitch
    ) -> None:
        await sks.deactivate("pionex:BTC/USDT", "reason")
        await sks.activate("pionex:ETH/USDT")  # No-op, never deactivated.

        assert sks.get_deactivated() == ["pionex:BTC/USDT"]


@pytest.mark.asyncio
class TestActivate:
    async def test_activate_restores_trading(self, sks: SymbolKillSwitch) -> None:
        await sks.deactivate("pionex:BTC/USDT", "reason")
        await sks.activate("pionex:BTC/USDT")

        assert sks.is_active("pionex:BTC/USDT") is True

    async def test_activate_clears_reason_and_timestamp(self, sks: SymbolKillSwitch) -> None:
        await sks.deactivate("pionex:BTC/USDT", "reason")
        await sks.activate("pionex:BTC/USDT", activated_by="user:admin")

        entry = sks.get_entry("pionex:BTC/USDT")
        assert entry is not None
        assert entry.reason is None
        assert entry.deactivated_at is None
        assert entry.deactivated_by is None

    async def test_activate_on_never_deactivated_symbol_is_noop(
        self, sks: SymbolKillSwitch
    ) -> None:
        await sks.activate("pionex:BTC/USDT")  # Should not raise or create an entry.
        assert sks.get_entry("pionex:BTC/USDT") is None

    async def test_activate_on_already_active_symbol_is_noop(
        self, sks: SymbolKillSwitch
    ) -> None:
        await sks.deactivate("pionex:BTC/USDT", "reason")
        await sks.activate("pionex:BTC/USDT")
        await sks.activate("pionex:BTC/USDT")  # Second call: already active, no-op.

        assert sks.is_active("pionex:BTC/USDT") is True


@pytest.mark.asyncio
class TestPersistence:
    async def test_deactivate_calls_repository_when_injected(
        self, sks: SymbolKillSwitch
    ) -> None:
        from unittest.mock import AsyncMock

        repo = AsyncMock()
        sks.inject_repository(repo)

        await sks.deactivate("pionex:BTC/USDT", "reason")

        repo.set_symbol_active.assert_awaited_once_with("pionex:BTC/USDT", False, "reason")

    async def test_activate_calls_repository_when_injected(self, sks: SymbolKillSwitch) -> None:
        from unittest.mock import AsyncMock

        repo = AsyncMock()
        sks.inject_repository(repo)

        await sks.deactivate("pionex:BTC/USDT", "reason")
        await sks.activate("pionex:BTC/USDT")

        repo.set_symbol_active.assert_awaited_with("pionex:BTC/USDT", True, None)

    async def test_persist_failure_does_not_revert_in_memory_state(
        self, sks: SymbolKillSwitch
    ) -> None:
        from unittest.mock import AsyncMock

        repo = AsyncMock()
        repo.set_symbol_active = AsyncMock(side_effect=RuntimeError("db down"))
        sks.inject_repository(repo)

        await sks.deactivate("pionex:BTC/USDT", "reason")  # Should not raise.

        assert sks.is_active("pionex:BTC/USDT") is False

    async def test_no_repository_persist_is_noop(self, sks: SymbolKillSwitch) -> None:
        # No repository injected - _persist() should just return early.
        await sks.deactivate("pionex:BTC/USDT", "reason")
        assert sks.is_active("pionex:BTC/USDT") is False


class TestSingleton:
    def test_get_symbol_kill_switch_returns_singleton(self) -> None:
        first = get_symbol_kill_switch()
        second = get_symbol_kill_switch()
        assert first is second
