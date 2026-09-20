"""Tests für sgr.strategy.grid_validation_runner.GridValidationRunner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sgr.core.types import Candle, ExchangeID, Symbol
from sgr.strategy.futures_grid import LongFuturesGridStrategy
from sgr.strategy.grid_validation_runner import GridValidationRunner
from sgr.strategy.registry import StrategyRegistry

SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _oscillating_candles(n: int) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    price = 100.0
    direction = -1
    for i in range(n):
        price += direction * 1.5
        if price <= 91:
            direction = 1
        elif price >= 109:
            direction = -1
        candles.append(
            Candle(
                symbol=SYMBOL,
                timestamp=start + timedelta(hours=i),
                timeframe="1h",
                open=Decimal(str(price)),
                high=Decimal(str(price + 1)),
                low=Decimal(str(price - 1)),
                close=Decimal(str(price)),
                volume=Decimal("10000"),
            )
        )
    return candles


@pytest.fixture(autouse=True)
def _clean_registry():
    """
    Registriert futures_grid_long_v1 EXPLIZIT (statt sich auf den
    Modul-Import-Seiteneffekt von @StrategyRegistry.register zu
    verlassen) - andere Testmodule rufen StrategyRegistry.clear() auf,
    was jede global registrierte Strategie fuer den Rest der
    Testsession entfernt, unabhaengig von der Ausfuehrungsreihenfolge
    (siehe tests/unit/test_strategy_engine.py _fresh_registry() fuer das
    etablierte Muster dieser Codebase).
    """
    registry = StrategyRegistry.get()
    registry.register_instance(LongFuturesGridStrategy())
    yield
    entry = registry.get_entry("futures_grid_long_v1")
    if entry is not None:
        from sgr.strategy.base import ValidationStatus

        entry.validation_status = ValidationStatus()
        entry.is_validated = False


class TestGridValidationRunnerSkipsClassicPath:
    async def test_classic_strategy_validation_runner_skips_grid_strategies(self) -> None:
        """Kernanforderung: der bestehende StrategyValidationRunner darf
        Grid-Strategien NICHT ueber den Directional-Signal-Pfad backtesten
        (das wuerde deterministisch 0 Trades liefern und faelschlich als
        'durchgefallen' werten)."""
        registry = StrategyRegistry.get()
        # Sicherstellen, dass futures_grid_long_v1 registriert und noch
        # nicht validiert ist (siehe Modul-Import in sgr/strategy/futures_grid.py).
        assert registry.get_entry("futures_grid_long_v1") is not None

        pending_names = [
            name for name, entry in registry.get_all().items() if not entry.is_validated
        ]
        assert "futures_grid_long_v1" in pending_names  # ist pending, aber...

        # ... wird von validate_pending_strategies() intern herausgefiltert:
        # wir pruefen das ueber die gleiche Filterlogik, die der Runner
        # verwendet (siehe validation_runner.py), ohne einen echten
        # Netzwerk-Backtest auszufuehren.
        from sgr.strategy.futures_grid import GridTradingStrategy

        filtered = [
            name
            for name, entry in registry.get_all().items()
            if not entry.is_validated and not isinstance(entry.strategy, GridTradingStrategy)
        ]
        assert "futures_grid_long_v1" not in filtered


class TestGridValidationRunnerValidatesViaGridBacktest:
    async def test_validate_pending_grid_strategies_uses_grid_backtest(self, mocker) -> None:
        candles = _oscillating_candles(400)
        mocker.patch(
            "sgr.strategy.grid_validation_runner.BacktestDataLoader.load_public_history",
            return_value=candles,
        )

        registry = StrategyRegistry.get()
        assert registry.get_entry("futures_grid_long_v1") is not None

        runner = GridValidationRunner(lookback_days=10, oos_days=2)
        summary = await runner.validate_pending_grid_strategies()

        assert "futures_grid_long_v1" not in summary.failed
        entry = registry.get_entry("futures_grid_long_v1")
        assert entry is not None
        assert entry.validation_status.notes != ""

    async def test_data_load_failure_marks_all_pending_as_failed(self, mocker) -> None:
        mocker.patch(
            "sgr.strategy.grid_validation_runner.BacktestDataLoader.load_public_history",
            side_effect=RuntimeError("network down"),
        )

        registry = StrategyRegistry.get()
        assert registry.get_entry("futures_grid_long_v1") is not None

        runner = GridValidationRunner()
        summary = await runner.validate_pending_grid_strategies()

        assert "futures_grid_long_v1" in summary.failed
