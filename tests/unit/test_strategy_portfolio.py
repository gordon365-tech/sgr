"""
Tests für Strategy Engine, Strategien, Execution Engine und Portfolio Engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from sgr.core.types import (
    ExchangeID,
    MarketRegime,
    OrderResult,
    OrderStatus,
    Position,
    PositionSide,
    SignalDirection,
    Symbol,
    TradingMode,
)
from sgr.market_data.types import FeatureSet, IndicatorValues, MarketContext
from sgr.portfolio.engine import PortfolioEngine
from sgr.strategy.base import ValidationStatus
from sgr.strategy.mean_reversion import MeanReversionStrategy
from sgr.strategy.registry import StrategyRegistry
from sgr.strategy.trend_following import TrendFollowingStrategy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_symbol(base: str = "BTC") -> Symbol:
    return Symbol(base=base, quote="USDT", exchange=ExchangeID.BINANCE)


def _make_indicators(**overrides) -> IndicatorValues:
    defaults = dict(
        rsi_14=55.0,
        rsi_7=58.0,
        macd_line=100.0,
        macd_signal=90.0,
        macd_histogram=10.0,
        adx_14=30.0,
        di_plus=28.0,
        di_minus=15.0,
        atr_14=Decimal("500"),
        atr_pct=0.01,
        bb_upper=Decimal("52000"),
        bb_middle=Decimal("50000"),
        bb_lower=Decimal("48000"),
        bb_width=0.04,
        bb_position=0.5,
        ema_9=Decimal("50500"),
        ema_21=Decimal("50200"),
        ema_50=Decimal("49500"),
        vwap=Decimal("49800"),
        volume_ratio=1.2,
        obv=0.05,
    )
    defaults.update(overrides)
    return IndicatorValues(**defaults)


def _make_feature_set(
    symbol: Symbol | None = None,
    regime: MarketRegime = MarketRegime.TRENDING_UP,
    indicators: IndicatorValues | None = None,
    close: float = 50000.0,
) -> FeatureSet:
    sym = symbol or _make_symbol()
    return FeatureSet(
        symbol=sym,
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal(str(close)),
        volume=Decimal("1000"),
        indicators=indicators or _make_indicators(),
        regime=regime,
        returns_1=0.005,
        returns_5=0.02,
    )


def _make_context(
    regime: MarketRegime = MarketRegime.TRENDING_UP,
    indicators: IndicatorValues | None = None,
) -> MarketContext:
    sym = _make_symbol()
    fs = _make_feature_set(sym, regime, indicators)
    return MarketContext(
        symbol=sym,
        timestamp=datetime.now(tz=UTC),
        primary=fs,
        regime=regime,
    )


def _make_order_result(
    symbol: Symbol | None = None,
    status: OrderStatus = OrderStatus.FILLED,
    qty: Decimal = Decimal("0.1"),
    price: Decimal = Decimal("50000"),
    side: str = "buy",
) -> OrderResult:
    sym = symbol or _make_symbol()
    now = datetime.now(tz=UTC)
    return OrderResult(
        request_id=uuid4(),
        exchange_order_id=f"TEST-{uuid4().hex[:8]}",
        symbol=sym,
        status=status,
        filled_quantity=qty,
        average_fill_price=price,
        fees=qty * price * Decimal("0.001"),
        submitted_at=now,
        filled_at=now,
        trading_mode=TradingMode.PAPER,
        raw_response={"side": side},
    )


# ===========================================================================
# Strategy Base + Protocol
# ===========================================================================


class TestStrategyProtocol:
    def test_base_strategy_validate_context_true(self) -> None:
        """BaseStrategy.validate_context: True wenn Basisdaten vorhanden."""
        strategy = TrendFollowingStrategy()
        context = _make_context(MarketRegime.TRENDING_UP)
        assert strategy.validate_context(context) is True

    def test_base_strategy_validate_context_false_missing_indicators(self) -> None:
        """validate_context: False wenn RSI fehlt."""
        strategy = TrendFollowingStrategy()
        ind = _make_indicators(rsi_14=None, adx_14=None, ema_9=None, ema_21=None)
        context = _make_context(indicators=ind)
        assert strategy.validate_context(context) is False

    def test_strategy_parameters_with_variation(self) -> None:
        strategy = TrendFollowingStrategy()
        params = strategy.get_parameters()
        varied = params.with_variation(1.2)
        # Numerische Werte sollten 20% höher sein
        for k, v in params.params.items():
            if isinstance(v, float):
                assert varied.params[k] == pytest.approx(v * 1.2, rel=1e-6)

    def test_validation_status_can_go_live(self) -> None:
        v = ValidationStatus(
            backtest_passed=True,
            walk_forward_passed=True,
            paper_trading_passed=True,
        )
        assert v.can_go_live is True

    def test_validation_status_cannot_go_live_incomplete(self) -> None:
        v = ValidationStatus(
            backtest_passed=True,
            walk_forward_passed=False,  # fehlt
        )
        assert v.can_go_live is False


# ===========================================================================
# TrendFollowingStrategy
# ===========================================================================


class TestTrendFollowingStrategy:
    def test_long_signal_in_trending_up(self) -> None:
        """Starker Aufwärtstrend → Long Signal."""
        strategy = TrendFollowingStrategy()
        # RSI > 50, EMA alignment, ADX > 25, volume > 1
        ind = _make_indicators(
            rsi_14=62.0,
            adx_14=32.0,
            volume_ratio=1.5,
            ema_9=Decimal("51000"),
            ema_21=Decimal("50500"),
            ema_50=Decimal("49000"),
        )
        context = _make_context(MarketRegime.TRENDING_UP, ind)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG
        assert 0.5 <= signal.confidence <= 1.0

    def test_no_signal_in_ranging(self) -> None:
        """Ranging Regime → kein Signal (Strategie unterstützt es nicht)."""
        strategy = TrendFollowingStrategy()
        context = _make_context(MarketRegime.RANGING)
        signal = strategy.generate_signal(context)
        assert signal is None

    def test_no_signal_low_adx(self) -> None:
        """ADX < 25 (schwacher Trend) → Konfidenz zu niedrig für Signal."""
        strategy = TrendFollowingStrategy()
        ind = _make_indicators(rsi_14=55.0, adx_14=15.0)  # Schwacher Trend
        context = _make_context(MarketRegime.TRENDING_UP, ind)
        signal = strategy.generate_signal(context)
        # Kein Signal oder sehr niedrige Konfidenz
        if signal is not None:
            assert signal.confidence < 0.70

    def test_short_signal_in_trending_down(self) -> None:
        """Starker Abwärtstrend → Short Signal."""
        strategy = TrendFollowingStrategy()
        ind = _make_indicators(
            rsi_14=38.0,
            adx_14=30.0,
            volume_ratio=1.3,
            ema_9=Decimal("49000"),
            ema_21=Decimal("49500"),
            ema_50=Decimal("50500"),
            di_plus=12.0,
            di_minus=30.0,
            vwap=Decimal("50200"),
        )
        context = _make_context(MarketRegime.TRENDING_DOWN, ind)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    def test_signal_metadata_present(self) -> None:
        """Signal enthält Metadata für Audit-Trail."""
        strategy = TrendFollowingStrategy()
        ind = _make_indicators(rsi_14=65.0, adx_14=35.0)
        context = _make_context(MarketRegime.TRENDING_UP, ind)
        signal = strategy.generate_signal(context)
        if signal:
            assert "rsi_14" in signal.metadata
            assert "adx_14" in signal.metadata

    def test_get_parameters(self) -> None:
        strategy = TrendFollowingStrategy()
        params = strategy.get_parameters()
        assert params.name == "trend_following_v1"
        assert "adx_min" in params.params


# ===========================================================================
# MeanReversionStrategy
# ===========================================================================


class TestMeanReversionStrategy:
    def test_long_signal_oversold(self) -> None:
        """RSI < 35 + BB Lower Touch → Long Signal."""
        strategy = MeanReversionStrategy()
        ind = _make_indicators(
            rsi_14=28.0,
            bb_position=0.05,  # nahe Lower Band
            adx_14=15.0,
            macd_histogram=-50.0,
        )
        context = _make_context(MarketRegime.RANGING, ind)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_short_signal_overbought(self) -> None:
        """RSI > 65 + BB Upper Touch → Short Signal."""
        strategy = MeanReversionStrategy()
        ind = _make_indicators(
            rsi_14=72.0,
            bb_position=0.95,  # nahe Upper Band
            adx_14=12.0,
            macd_histogram=80.0,
        )
        context = _make_context(MarketRegime.RANGING, ind)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    def test_no_signal_trending_regime(self) -> None:
        """Trend-Regime → kein Signal."""
        strategy = MeanReversionStrategy()
        ind = _make_indicators(rsi_14=28.0, bb_position=0.05)
        context = _make_context(MarketRegime.TRENDING_UP, ind)
        signal = strategy.generate_signal(context)
        assert signal is None

    def test_no_signal_neutral_conditions(self) -> None:
        """RSI neutral, BB mittig → kein Signal."""
        strategy = MeanReversionStrategy()
        ind = _make_indicators(rsi_14=50.0, bb_position=0.50)
        context = _make_context(MarketRegime.RANGING, ind)
        signal = strategy.generate_signal(context)
        assert signal is None

    def test_signal_contains_target_stop(self) -> None:
        """Signal Metadata enthält target_price und stop_price."""
        strategy = MeanReversionStrategy()
        ind = _make_indicators(
            rsi_14=28.0,
            bb_position=0.05,
            adx_14=12.0,
            macd_histogram=-50.0,
            atr_14=Decimal("500"),
            bb_middle=Decimal("50000"),
        )
        context = _make_context(MarketRegime.RANGING, ind)
        signal = strategy.generate_signal(context)
        if signal:
            assert "target_price" in signal.metadata
            assert "stop_price" in signal.metadata


# ===========================================================================
# Strategy Registry
# ===========================================================================


class TestStrategyRegistry:
    @pytest.fixture(autouse=True)
    def reset_registry(self) -> None:
        """Jeder Test bekommt eine frische Registry."""
        StrategyRegistry.get().clear()
        yield
        StrategyRegistry.get().clear()

    def test_register_and_retrieve(self) -> None:
        registry = StrategyRegistry.get()
        strategy = TrendFollowingStrategy()
        registry.register_instance(strategy)
        assert "trend_following_v1" in registry.get_all()

    def test_activate_deactivate(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        registry.activate("trend_following_v1")
        assert registry.is_active("trend_following_v1")
        registry.deactivate("trend_following_v1", "test")
        assert not registry.is_active("trend_following_v1")

    def test_get_active_filters_by_regime(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        registry.register_instance(MeanReversionStrategy())
        registry.activate("trend_following_v1")
        registry.activate("mean_reversion_v1")

        trending = registry.get_active(MarketRegime.TRENDING_UP)
        ranging = registry.get_active(MarketRegime.RANGING)

        assert any(s.name == "trend_following_v1" for s in trending)
        assert not any(s.name == "mean_reversion_v1" for s in trending)
        assert any(s.name == "mean_reversion_v1" for s in ranging)

    def test_auto_deactivate_underperforming(self) -> None:
        from sgr.strategy.base import StrategyPerformance

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        registry.activate("trend_following_v1")

        bad_perf = StrategyPerformance(
            strategy_name="trend_following_v1",
            period_days=30,
            total_trades=50,
            win_rate=0.30,
            profit_factor=0.80,
            sharpe_ratio=0.20,
            sortino_ratio=0.15,
            max_drawdown=0.25,
            cagr=-0.05,
            hit_rate=0.30,
            expected_value=-10.0,
            computed_at=datetime.now(tz=UTC),
        )
        registry.update_performance("trend_following_v1", bad_perf)
        assert not registry.is_active("trend_following_v1")

    def test_deactivation_reason_stored(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        registry.deactivate("trend_following_v1", "underperformance")
        entry = registry.get_entry("trend_following_v1")
        assert entry is not None
        assert entry.deactivation_reason == "underperformance"

    def test_get_missing_raises_keyerror(self) -> None:
        registry = StrategyRegistry.get()
        with pytest.raises(KeyError):
            registry.activate("nonexistent_strategy")


# ===========================================================================
# Portfolio Engine
# ===========================================================================


class TestPortfolioEngine:
    def test_initial_state(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        assert engine.cash == Decimal("10000")
        assert len(engine.positions) == 0
        assert engine.portfolio_value == Decimal("10000")

    async def test_open_position_on_buy_fill(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        result = _make_order_result(qty=Decimal("0.1"), price=Decimal("50000"), side="buy")
        await engine.on_order_filled(result)

        assert len(engine.positions) == 1
        pos = engine.positions[0]
        assert pos.side == PositionSide.LONG
        assert pos.quantity == Decimal("0.1")

    async def test_cash_reduced_on_buy(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        qty = Decimal("0.1")
        price = Decimal("50000")
        fees = qty * price * Decimal("0.001")
        result = _make_order_result(qty=qty, price=price, side="buy")
        await engine.on_order_filled(result)

        expected_cash = Decimal("10000") - qty * price - fees
        assert engine.cash == pytest.approx(float(expected_cash), rel=1e-6)

    async def test_close_position_on_sell(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        # Open
        buy = _make_order_result(qty=Decimal("0.1"), price=Decimal("50000"), side="buy")
        await engine.on_order_filled(buy)
        assert len(engine.positions) == 1

        # Close
        sell = _make_order_result(qty=Decimal("0.1"), price=Decimal("52000"), side="sell")
        sell = sell.model_copy(update={"symbol": buy.symbol})
        await engine.on_order_filled(sell)
        assert len(engine.positions) == 0

    async def test_realized_pnl_recorded(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        sym = _make_symbol()
        buy = _make_order_result(symbol=sym, qty=Decimal("1.0"), price=Decimal("50000"), side="buy")
        await engine.on_order_filled(buy)

        sell = _make_order_result(
            symbol=sym, qty=Decimal("1.0"), price=Decimal("55000"), side="sell"
        )
        await engine.on_order_filled(sell)

        assert len(engine.trade_history) == 1
        trade = engine.trade_history[0]
        # Entry 50000, Exit 55000, Qty 1 → PnL ≈ 5000 - fees
        pnl = Decimal(trade["realized_pnl"])
        assert pnl > Decimal("4900")

    def test_update_prices_updates_unrealized(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("100000"))
        # Manually insert position
        sym = _make_symbol()
        pos = Position(
            symbol=sym,
            side=PositionSide.LONG,
            quantity=Decimal("1.0"),
            entry_price=Decimal("50000"),
            current_price=Decimal("50000"),
            opened_at=datetime.now(tz=UTC),
            strategy_name="test",
            trading_mode=TradingMode.PAPER,
        )
        engine._state._positions[str(sym)] = pos

        engine.update_prices({"BTC/USDT": Decimal("55000")})
        updated = engine.positions[0]
        assert updated.current_price == Decimal("55000")
        assert updated.unrealized_pnl == Decimal("5000")  # +10%

    def test_summary_structure(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER)
        summary = engine.summary()
        assert "portfolio_value" in summary
        assert "cash" in summary
        assert "open_positions" in summary
        assert "trading_mode" in summary
        assert summary["trading_mode"] == "paper"
