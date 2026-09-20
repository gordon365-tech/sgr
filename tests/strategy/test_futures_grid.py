"""Tests für sgr.strategy.futures_grid (Long/Short/Adaptive Futures Grid)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sgr.core.types import ExchangeID, GridDirection, MarketRegime, ProductType, Symbol
from sgr.market_data.types import FeatureSet, IndicatorValues, MarketContext
from sgr.strategy.futures_grid import (
    AdaptiveFuturesGridStrategy,
    GridTradingStrategy,
    LongFuturesGridStrategy,
    ShortFuturesGridStrategy,
    get_active_grid_strategies,
)
from sgr.strategy.registry import StrategyRegistry


def _context(
    adx=12.0, atr_pct=0.04, bb_width=0.03, di_plus=20.0, di_minus=20.0, close=Decimal("100")
) -> MarketContext:
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)
    indicators = IndicatorValues(
        adx_14=adx,
        atr_pct=atr_pct,
        bb_width=bb_width,
        di_plus=di_plus,
        di_minus=di_minus,
        atr_14=close * Decimal(str(atr_pct)) if atr_pct else None,
    )
    features = FeatureSet(
        symbol=symbol,
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=close,
        volume=Decimal("5000000"),
        indicators=indicators,
    )
    return MarketContext(
        symbol=symbol, timestamp=features.timestamp, primary=features, regime=MarketRegime.RANGING
    )


class TestGenerateSignalIsNoOp:
    def test_long_grid_generate_signal_always_none(self) -> None:
        strategy = LongFuturesGridStrategy()
        assert strategy.generate_signal(_context()) is None

    def test_short_grid_generate_signal_always_none(self) -> None:
        strategy = ShortFuturesGridStrategy()
        assert strategy.generate_signal(_context()) is None

    def test_adaptive_grid_generate_signal_always_none(self) -> None:
        strategy = AdaptiveFuturesGridStrategy()
        assert strategy.generate_signal(_context()) is None


class TestGridTradingStrategyProtocol:
    def test_all_three_strategies_satisfy_protocol(self) -> None:
        for strategy in (
            LongFuturesGridStrategy(),
            ShortFuturesGridStrategy(),
            AdaptiveFuturesGridStrategy(),
        ):
            assert isinstance(strategy, GridTradingStrategy)

    def test_declares_supported_product_types_and_exchanges(self) -> None:
        strategy = LongFuturesGridStrategy()
        assert ProductType.FUTURES_GRID in strategy.supported_product_types
        assert ExchangeID.PIONEX in strategy.supported_exchanges
        assert ExchangeID.BINANCE in strategy.supported_exchanges


class TestLongFuturesGridStrategy:
    def test_suitable_market_produces_long_decision(self) -> None:
        strategy = LongFuturesGridStrategy()
        decision = strategy.evaluate(_context())

        assert decision.direction == GridDirection.LONG
        assert decision.parameters is not None
        assert decision.parameters.long_or_short == GridDirection.LONG

    def test_unsuitable_market_produces_neutral(self) -> None:
        strategy = LongFuturesGridStrategy()
        decision = strategy.evaluate(_context(adx=50.0, di_plus=45.0, di_minus=5.0))

        assert decision.direction == GridDirection.NEUTRAL
        assert decision.parameters is None

    def test_missing_atr_yields_neutral_not_crash(self) -> None:
        strategy = LongFuturesGridStrategy()
        decision = strategy.evaluate(_context(atr_pct=None))

        assert decision.direction == GridDirection.NEUTRAL


class TestShortFuturesGridStrategy:
    def test_suitable_market_produces_short_decision(self) -> None:
        strategy = ShortFuturesGridStrategy()
        decision = strategy.evaluate(_context())

        assert decision.direction == GridDirection.SHORT
        assert decision.parameters.long_or_short == GridDirection.SHORT
        # Fuer Short: stop_loss oberhalb, take_profit unterhalb des Preises.
        assert decision.parameters.stop_loss > decision.parameters.take_profit


class TestAdaptiveFuturesGridStrategy:
    def test_adapts_to_bullish_bias(self) -> None:
        strategy = AdaptiveFuturesGridStrategy()
        decision = strategy.evaluate(_context(adx=20.0, di_plus=25.0, di_minus=10.0))

        assert decision.direction == GridDirection.LONG

    def test_adapts_to_bearish_bias(self) -> None:
        strategy = AdaptiveFuturesGridStrategy()
        decision = strategy.evaluate(_context(adx=20.0, di_plus=10.0, di_minus=25.0))

        assert decision.direction == GridDirection.SHORT

    def test_no_grid_in_unsuitable_conditions(self) -> None:
        strategy = AdaptiveFuturesGridStrategy()
        decision = strategy.evaluate(_context(adx=50.0, atr_pct=0.2))

        assert decision.direction == GridDirection.NEUTRAL
        assert len(decision.reasons) > 0


class TestRegistryIntegration:
    """
    Registriert die Grid-Strategien HIER explizit (statt sich auf den
    Modul-Import-Seiteneffekt von @StrategyRegistry.register zu
    verlassen) - andere Testmodule (siehe tests/unit/test_strategy_engine.py
    _fresh_registry()) rufen StrategyRegistry.clear() auf, was JEDE
    global registrierte Strategie fuer den Rest der Testsession entfernt,
    unabhaengig von der Ausfuehrungsreihenfolge. Konsistent mit dem
    bestehenden Test-Isolationsmuster dieser Codebase.
    """

    def setup_method(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(LongFuturesGridStrategy())
        registry.register_instance(ShortFuturesGridStrategy())
        registry.register_instance(AdaptiveFuturesGridStrategy())

    def test_grid_strategies_are_registered(self) -> None:
        registry = StrategyRegistry.get()
        all_entries = registry.get_all()

        assert "futures_grid_long_v1" in all_entries
        assert "futures_grid_short_v1" in all_entries
        assert "futures_grid_adaptive_v1" in all_entries

    async def test_get_active_grid_strategies_filters_by_protocol_and_activation(self) -> None:
        registry = StrategyRegistry.get()
        # Directional Strategien (falls registriert) duerfen NICHT als
        # Grid-Strategien zurueckkommen.
        await registry.activate("futures_grid_long_v1")
        try:
            active_grids = get_active_grid_strategies()
            names = {s.name for s in active_grids}
            assert "futures_grid_long_v1" in names
            assert all(isinstance(s, GridTradingStrategy) for s in active_grids)
        finally:
            await registry.deactivate("futures_grid_long_v1", reason="test cleanup")
