"""
Tests für sgr.orchestrator.grid_scheduler.GridScheduler (Phase D - bisher
fehlendes Live/Paper-Wiring fuer Futures Grid).

Kernanforderung (siehe Modul-Docstring): der Scheduler darf NIEMALS
automatisch Kapital/Grids fuer Gordon/Sumo aktivieren, ausser er wurde
explizit ueber GRID_SCHEDULER_ENABLED=true (oder enabled=True im Test)
freigeschaltet - UND selbst dann nur fuer bereits is_active=True
registrierte Grid-Strategien mit einem injizierten
account_eligibility_provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sgr.core.grid_types import GridDecision, GridState
from sgr.core.types import (
    Candle,
    CandleEvent,
    ExchangeID,
    GridDirection,
    GridStatus,
    Symbol,
    TradingMode,
)
from sgr.market_data.types import FeatureSet
from sgr.orchestrator.grid_scheduler import GridScheduler

pytestmark = pytest.mark.asyncio


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _feature_set() -> FeatureSet:
    return FeatureSet(
        symbol=_symbol(),
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("50000"),
        volume=Decimal("100"),
    )


def _candle_event(price: str = "50000", timeframe: str = "1h") -> CandleEvent:
    candle = Candle(
        symbol=_symbol(),
        timestamp=datetime.now(tz=UTC),
        timeframe=timeframe,
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=Decimal("1"),
    )
    return CandleEvent(timestamp=datetime.now(tz=UTC), source="test", candle=candle)


def _grid_controller_mock(active_grids: list[Any] | None = None) -> MagicMock:
    controller = MagicMock()
    controller.active_grids.return_value = active_grids or []
    controller.on_price_tick = AsyncMock()
    controller.open_grid = AsyncMock()
    return controller


def _feature_store_mock(features: Any = "SENTINEL") -> MagicMock:
    store = MagicMock()
    if isinstance(features, str) and features == "SENTINEL":
        features = _feature_set()
    store.get_latest = AsyncMock(return_value=features)
    store.get_latest_regime = AsyncMock(return_value=None)
    return store


class TestDisabledByDefault:
    async def test_scheduler_disabled_without_env_flag_is_noop(self, monkeypatch) -> None:
        monkeypatch.delenv("GRID_SCHEDULER_ENABLED", raising=False)
        controller = _grid_controller_mock()
        scheduler = GridScheduler(controller, _feature_store_mock(), TradingMode.PAPER)

        assert scheduler.enabled is False
        await scheduler.on_candle_event(_candle_event())

        controller.active_grids.assert_not_called()
        controller.open_grid.assert_not_awaited()

    async def test_scheduler_disabled_even_with_active_grid_strategies(self, monkeypatch) -> None:
        """Selbst wenn eine Grid-Strategie is_active=True in der Registry
        ist, bleibt der Scheduler ohne das Flag komplett inert."""
        monkeypatch.delenv("GRID_SCHEDULER_ENABLED", raising=False)
        controller = _grid_controller_mock()
        scheduler = GridScheduler(controller, _feature_store_mock(), TradingMode.PAPER)

        await scheduler.on_candle_event(_candle_event())

        controller.open_grid.assert_not_awaited()

    async def test_explicit_enabled_false_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("GRID_SCHEDULER_ENABLED", "true")
        controller = _grid_controller_mock()
        scheduler = GridScheduler(
            controller, _feature_store_mock(), TradingMode.PAPER, enabled=False
        )

        assert scheduler.enabled is False

    async def test_env_flag_enables_scheduler(self, monkeypatch) -> None:
        monkeypatch.setenv("GRID_SCHEDULER_ENABLED", "true")
        controller = _grid_controller_mock()
        scheduler = GridScheduler(controller, _feature_store_mock(), TradingMode.PAPER)

        assert scheduler.enabled is True


class TestEnabledBehavior:
    async def test_existing_active_grid_gets_price_tick_not_reopened(self) -> None:
        grid = GridState(
            id="11111111-1111-1111-1111-111111111111",
            exchange=ExchangeID.BINANCE,
            symbol=_symbol(),
            strategy_name="futures_grid_long_v1",
            trading_mode=TradingMode.PAPER,
            direction=GridDirection.LONG,
            status=GridStatus.ACTIVE,
            opened_at=datetime.now(tz=UTC),
        )
        controller = _grid_controller_mock(active_grids=[grid])
        scheduler = GridScheduler(
            controller, _feature_store_mock(), TradingMode.PAPER, enabled=True
        )

        await scheduler.on_candle_event(_candle_event(price="49500"))

        controller.on_price_tick.assert_awaited_once_with(str(grid.id), Decimal("49500"))
        controller.open_grid.assert_not_awaited()

    async def test_symbol_kill_switch_blocks_scheduler(self, monkeypatch) -> None:
        from sgr.risk.symbol_kill_switch import get_symbol_kill_switch

        sks = get_symbol_kill_switch(tenant_id="scheduler-test-tenant")
        await sks.deactivate("binance:BTC/USDT", reason="test")
        try:
            controller = _grid_controller_mock()
            scheduler = GridScheduler(
                controller,
                _feature_store_mock(),
                TradingMode.PAPER,
                tenant_id="scheduler-test-tenant",
                enabled=True,
            )

            await scheduler.on_candle_event(_candle_event())

            controller.active_grids.assert_not_called()
        finally:
            await sks.activate("binance:BTC/USDT")

    async def test_no_active_grid_strategies_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sgr.orchestrator.grid_scheduler.get_active_grid_strategies", lambda: []
        )
        controller = _grid_controller_mock()
        scheduler = GridScheduler(
            controller, _feature_store_mock(), TradingMode.PAPER, enabled=True
        )

        await scheduler.on_candle_event(_candle_event())

        controller.open_grid.assert_not_awaited()

    async def test_neutral_decision_does_not_open_grid(self, monkeypatch) -> None:
        strategy = MagicMock()
        strategy.name = "futures_grid_adaptive_v1"
        strategy.evaluate.return_value = GridDecision(
            direction=GridDirection.NEUTRAL, parameters=None, confidence=0.1, reasons=["flat"]
        )
        monkeypatch.setattr(
            "sgr.orchestrator.grid_scheduler.get_active_grid_strategies", lambda: [strategy]
        )
        controller = _grid_controller_mock()
        scheduler = GridScheduler(
            controller, _feature_store_mock(), TradingMode.PAPER, enabled=True
        )

        await scheduler.on_candle_event(_candle_event())

        controller.open_grid.assert_not_awaited()

    async def test_no_account_provider_blocks_open_even_with_signal(self, monkeypatch) -> None:
        """Fail-closed: ohne injizierten account_eligibility_provider wird
        NIEMALS ein Grid eroeffnet, selbst wenn eine Strategie eine
        konkrete Richtung vorschlaegt - keine erfundene Account-Freigabe."""
        from sgr.core.grid_types import FuturesGridParameters

        strategy = MagicMock()
        strategy.name = "futures_grid_long_v1"
        strategy.evaluate.return_value = GridDecision(
            direction=GridDirection.LONG,
            parameters=FuturesGridParameters(
                grid_lower_price=Decimal("49000"),
                grid_upper_price=Decimal("51000"),
                grid_count=5,
                long_or_short=GridDirection.LONG,
                position_size=Decimal("50"),
            ),
            confidence=0.8,
            reasons=["ranging"],
        )
        monkeypatch.setattr(
            "sgr.orchestrator.grid_scheduler.get_active_grid_strategies", lambda: [strategy]
        )
        controller = _grid_controller_mock()
        scheduler = GridScheduler(
            controller, _feature_store_mock(), TradingMode.PAPER, enabled=True
        )  # kein account_eligibility_provider

        await scheduler.on_candle_event(_candle_event())

        controller.open_grid.assert_not_awaited()

    async def test_missing_features_is_noop(self) -> None:
        controller = _grid_controller_mock()
        store = _feature_store_mock(features=None)
        scheduler = GridScheduler(controller, store, TradingMode.PAPER, enabled=True)

        await scheduler.on_candle_event(_candle_event())

        controller.open_grid.assert_not_awaited()

    async def test_bad_event_does_not_raise(self) -> None:
        controller = _grid_controller_mock()
        scheduler = GridScheduler(
            controller, _feature_store_mock(), TradingMode.PAPER, enabled=True
        )

        class BadEvent:
            pass

        await scheduler.on_candle_event(BadEvent())  # darf nicht crashen

    async def test_idempotent_does_not_reopen_same_symbol_strategy_twice(self, monkeypatch) -> None:
        from sgr.core.grid_types import FuturesGridParameters
        from sgr.execution.grid_controller import GridOpenResult

        strategy = MagicMock()
        strategy.name = "futures_grid_long_v1"
        strategy.evaluate.return_value = GridDecision(
            direction=GridDirection.LONG,
            parameters=FuturesGridParameters(
                grid_lower_price=Decimal("49000"),
                grid_upper_price=Decimal("51000"),
                grid_count=5,
                long_or_short=GridDirection.LONG,
                position_size=Decimal("50"),
            ),
            confidence=0.8,
            reasons=["ranging"],
        )
        monkeypatch.setattr(
            "sgr.orchestrator.grid_scheduler.get_active_grid_strategies", lambda: [strategy]
        )
        controller = _grid_controller_mock()
        controller.open_grid = AsyncMock(
            return_value=GridOpenResult(grid=MagicMock(id="fake"), approved=True, reason="")
        )
        scheduler = GridScheduler(
            controller,
            _feature_store_mock(),
            TradingMode.PAPER,
            enabled=True,
            account_eligibility_provider=lambda: MagicMock(),
        )

        await scheduler.on_candle_event(_candle_event())
        assert controller.open_grid.await_count == 1

        # Zweiter Candle fuer dasselbe Symbol: aktives Grid existiert laut
        # Mock aber NICHT (active_grids() bleibt leer im Mock) - die
        # interne _open_grid_keys-Idempotenz muss trotzdem ein zweites
        # open_grid() fuer dieselbe (Symbol, Strategie)-Kombination
        # verhindern.
        await scheduler.on_candle_event(_candle_event())
        assert controller.open_grid.await_count == 1
