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

    async def test_activate_deactivate(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        await registry.activate("trend_following_v1")
        assert registry.is_active("trend_following_v1")
        await registry.deactivate("trend_following_v1", "test")
        assert not registry.is_active("trend_following_v1")

    async def test_get_active_filters_by_regime(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        registry.register_instance(MeanReversionStrategy())
        await registry.activate("trend_following_v1")
        await registry.activate("mean_reversion_v1")

        trending = registry.get_active(MarketRegime.TRENDING_UP)
        ranging = registry.get_active(MarketRegime.RANGING)

        assert any(s.name == "trend_following_v1" for s in trending)
        assert not any(s.name == "mean_reversion_v1" for s in trending)
        assert any(s.name == "mean_reversion_v1" for s in ranging)

    async def test_auto_deactivate_underperforming(self) -> None:
        from sgr.strategy.base import StrategyPerformance

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        await registry.activate("trend_following_v1")

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
        await registry.update_performance("trend_following_v1", bad_perf)
        assert not registry.is_active("trend_following_v1")

    async def test_deactivation_reason_stored(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        await registry.deactivate("trend_following_v1", "underperformance")
        entry = registry.get_entry("trend_following_v1")
        assert entry is not None
        assert entry.deactivation_reason == "underperformance"

    async def test_get_missing_raises_keyerror(self) -> None:
        registry = StrategyRegistry.get()
        with pytest.raises(KeyError):
            await registry.activate("nonexistent_strategy")


class TestStrategyRegistryPersistence:
    """
    Persistenz-Hooks der Registry (inject_repository, sync_registrations_
    to_db, get_active_names_from_db, _persist_active). Zuvor hielt die
    Registry is_active rein in-memory, obwohl StrategyRepository bereits
    upsert()/set_active()/get_active_names() implementiert hatte - beide
    Seiten existierten, waren aber nie verbunden.
    """

    @pytest.fixture(autouse=True)
    def reset_registry(self):
        StrategyRegistry.get().clear()
        yield
        StrategyRegistry.get().clear()

    async def test_activate_persists_via_injected_repository(self) -> None:
        from unittest.mock import AsyncMock

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        repo = AsyncMock()
        registry.inject_repository(repo)

        await registry.activate("trend_following_v1")

        repo.set_active.assert_called_once_with("trend_following_v1", True, None)

    async def test_deactivate_persists_reason_via_injected_repository(self) -> None:
        from unittest.mock import AsyncMock

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        repo = AsyncMock()
        registry.inject_repository(repo)

        await registry.deactivate("trend_following_v1", "manual stop")

        repo.set_active.assert_called_once_with("trend_following_v1", False, "manual stop")

    async def test_persist_failure_does_not_raise(self) -> None:
        """Fail-safe: In-Memory-Aktivierung darf trotz DB-Fehler gelingen."""
        from unittest.mock import AsyncMock

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        repo = AsyncMock()
        repo.set_active.side_effect = RuntimeError("db down")
        registry.inject_repository(repo)

        await registry.activate("trend_following_v1")

        assert registry.is_active("trend_following_v1")  # In-Memory-State trotzdem gesetzt

    async def test_no_repository_injected_is_safe_noop(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())

        await registry.activate("trend_following_v1")  # kein inject_repository() zuvor

        assert registry.is_active("trend_following_v1")

    async def test_sync_registrations_to_db_upserts_all_entries(self) -> None:
        from unittest.mock import AsyncMock

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        registry.register_instance(MeanReversionStrategy())
        repo = AsyncMock()
        registry.inject_repository(repo)

        await registry.sync_registrations_to_db()

        assert repo.upsert.call_count == 2

    async def test_sync_registrations_noop_without_repository(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())

        await registry.sync_registrations_to_db()  # darf nicht crashen

    async def test_sync_registrations_one_failure_does_not_block_others(self) -> None:
        from unittest.mock import AsyncMock

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        registry.register_instance(MeanReversionStrategy())
        repo = AsyncMock()
        repo.upsert.side_effect = [RuntimeError("db down"), None]
        registry.inject_repository(repo)

        await registry.sync_registrations_to_db()  # darf nicht crashen

        assert repo.upsert.call_count == 2

    async def test_get_active_names_from_db_delegates_to_repository(self) -> None:
        from unittest.mock import AsyncMock

        registry = StrategyRegistry.get()
        repo = AsyncMock()
        repo.get_active_names.return_value = ["trend_following_v1"]
        registry.inject_repository(repo)

        result = await registry.get_active_names_from_db()

        assert result == ["trend_following_v1"]

    async def test_get_active_names_from_db_empty_without_repository(self) -> None:
        registry = StrategyRegistry.get()

        result = await registry.get_active_names_from_db()

        assert result == []


# ===========================================================================
# Strategy Force-Activate Override (STRATEGY_FORCE_ACTIVATE)
#
# sgr.api.main.apply_strategy_force_activate_override() - aktiviert eine
# Strategie trotz NO-GO aus der automatischen Validierung, ausschliesslich
# auf explizite operative Anweisung fuer einen Paper-Trading-Pipeline-
# Testlauf. Siehe dessen Docstring fuer die vollstaendige Begruendung.
# ===========================================================================


class TestStrategyForceActivateOverride:
    @pytest.fixture(autouse=True)
    def reset_registry(self):
        StrategyRegistry.get().clear()
        yield
        StrategyRegistry.get().clear()

    async def test_override_sets_is_validated_true_despite_no_go(self) -> None:
        from unittest.mock import AsyncMock

        from sgr.api.main import apply_strategy_force_activate_override

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        real_no_go = ValidationStatus(
            backtest_passed=False, walk_forward_passed=False, notes="NO-GO: Sharpe -7.2"
        )
        registry.mark_validated("trend_following_v1", real_no_go)
        assert registry.get_entry("trend_following_v1").is_validated is False

        repo = AsyncMock()
        overridden = await apply_strategy_force_activate_override(
            registry=registry, strategy_repo=repo, names=["trend_following_v1"]
        )

        assert overridden == ["trend_following_v1"]
        entry = registry.get_entry("trend_following_v1")
        assert entry.is_validated is True
        # Das echte NO-GO-Ergebnis bleibt erhalten, nur ueberschrieben durch
        # den klar gekennzeichneten Override-Status - nicht verloren.
        assert "NO-GO" in entry.validation_status.notes
        assert "MANUAL OVERRIDE" in entry.validation_status.notes
        repo.set_validated.assert_awaited_once_with("trend_following_v1", True)

    async def test_override_activates_via_normal_activation_loop(self) -> None:
        """Nach dem Override muss der bestehende
        `for entry in registry.get_all().values(): if entry.is_validated:
        await registry.activate(...)`-Loop in lifespan() die Strategie
        normal aktivieren - kein Sonderpfad noetig."""
        from unittest.mock import AsyncMock

        from sgr.api.main import apply_strategy_force_activate_override

        registry = StrategyRegistry.get()
        registry.register_instance(MeanReversionStrategy())
        registry.mark_validated(
            "mean_reversion_v1",
            ValidationStatus(backtest_passed=False, walk_forward_passed=False, notes="C"),
        )

        await apply_strategy_force_activate_override(
            registry=registry, strategy_repo=AsyncMock(), names=["mean_reversion_v1"]
        )
        for entry in registry.get_all().values():
            if entry.is_validated:
                await registry.activate(entry.strategy.name)

        assert registry.is_active("mean_reversion_v1")

    async def test_unknown_name_is_skipped_not_raised(self) -> None:
        from unittest.mock import AsyncMock

        from sgr.api.main import apply_strategy_force_activate_override

        registry = StrategyRegistry.get()
        repo = AsyncMock()

        overridden = await apply_strategy_force_activate_override(
            registry=registry, strategy_repo=repo, names=["does_not_exist"]
        )

        assert overridden == []
        repo.set_validated.assert_not_awaited()

    async def test_empty_names_is_noop(self) -> None:
        from unittest.mock import AsyncMock

        from sgr.api.main import apply_strategy_force_activate_override

        registry = StrategyRegistry.get()
        registry.register_instance(TrendFollowingStrategy())
        repo = AsyncMock()

        overridden = await apply_strategy_force_activate_override(
            registry=registry, strategy_repo=repo, names=[]
        )

        assert overridden == []
        assert registry.get_entry("trend_following_v1").is_validated is False
        repo.set_validated.assert_not_awaited()


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
        # Entry 50000, Exit 55000, Qty 1 -> Gross 5000, minus Entry-Fee (50)
        # UND Exit-Fee (55) - beide Fees fliessen in net_pnl ein (siehe
        # PortfolioEngine._entry_fees).
        pnl = Decimal(trade["realized_pnl"])
        assert pnl == Decimal("4895.0000")

    async def test_update_prices_updates_unrealized(self) -> None:
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

        await engine.update_prices({"BTC/USDT": Decimal("55000")})
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


# ===========================================================================
# Portfolio Engine - Short-Position Cash Accounting Regression
#
# Analog zu Commit 1584e08 (fix(backtesting): correct short-position cash
# accounting in BacktestSimulator): PortfolioEngine._open_position()/
# _update_position() und PortfolioState.portfolio_value hatten denselben
# Bug (Short mit der Long-Formel gebucht: Cash sank beim Open statt zu
# steigen, offene Short-Notional zaehlte als Aktivum statt Verbindlichkeit).
# Diese Tests verifizieren die symmetrische Korrektur im Live/Paper-Pfad
# (nicht nur im Backtest-Simulator).
# ===========================================================================


class TestPortfolioEngineShortCashAccounting:
    async def test_cash_increases_on_short_open(self) -> None:
        """Short-Open verkauft zuerst -> Cash steigt um Notional - Fee."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        qty = Decimal("0.1")
        price = Decimal("50000")
        result = _make_order_result(qty=qty, price=price, side="sell")
        await engine.on_order_filled(result)

        assert len(engine.positions) == 1
        assert engine.positions[0].side == PositionSide.SHORT

        expected_cash = Decimal("10000") + qty * price - result.fees
        assert engine.cash == pytest.approx(float(expected_cash), rel=1e-9)

    async def test_cash_decreases_on_short_close_buyback(self) -> None:
        """Short-Close kauft zurueck -> Cash sinkt um Exit-Notional + Fee."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        sym = _make_symbol()

        open_qty = Decimal("1.0")
        open_price = Decimal("50000")
        short_open = _make_order_result(symbol=sym, qty=open_qty, price=open_price, side="sell")
        await engine.on_order_filled(short_open)
        cash_after_open = engine.cash

        close_price = Decimal("48000")  # Preis gefallen -> Short gewinnt
        buyback = _make_order_result(symbol=sym, qty=open_qty, price=close_price, side="buy")
        await engine.on_order_filled(buyback)

        expected_cash_after_close = cash_after_open - (open_qty * close_price + buyback.fees)
        assert engine.cash == pytest.approx(float(expected_cash_after_close), rel=1e-9)
        assert len(engine.positions) == 0

    async def test_short_round_trip_cash_delta_equals_net_pnl(self) -> None:
        """cash_after - cash_before muss fuer eine profitable Short-Position
        exakt dem net_pnl (realized_pnl inkl. beider Fees) entsprechen -
        dieselbe Invariante wie in den Backtest-Regressionstests (Schritt
        17)."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        sym = _make_symbol()
        cash_before = engine.cash

        open_qty = Decimal("1.0")
        short_open = _make_order_result(
            symbol=sym, qty=open_qty, price=Decimal("50000"), side="sell"
        )
        await engine.on_order_filled(short_open)

        buyback = _make_order_result(
            symbol=sym, qty=open_qty, price=Decimal("48000"), side="buy"
        )
        await engine.on_order_filled(buyback)

        net_pnl = Decimal(engine.trade_history[0]["net_pnl"])
        cash_delta = engine.cash - cash_before
        assert cash_delta == pytest.approx(float(net_pnl), rel=1e-9)

    async def test_short_position_value_is_liability_not_asset(self) -> None:
        """Eine offene Short-Position darf portfolio_value NICHT erhoehen
        (Verkaufserloes bereits in Cash gebucht) - der Rueckkaufbedarf muss
        als Minus gefuehrt werden, sonst Doppelzaehlung."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        cash_before = engine.cash

        short_open = _make_order_result(qty=Decimal("1.0"), price=Decimal("50000"), side="sell")
        await engine.on_order_filled(short_open)

        # Sofort nach Open (Marktpreis == Entry-Preis): portfolio_value
        # darf sich nur um die Fee veraendert haben, nicht um +2x Notional.
        expected_value = cash_before - short_open.fees
        assert engine.portfolio_value == pytest.approx(float(expected_value), rel=1e-9)

    async def test_alternating_long_short_cash_matches_cumulative_net_pnl(self) -> None:
        """4 Trades (long win, short win, long loss, short loss) -
        cash == initial_capital + cumulative_net_pnl muss nach jedem Close
        gelten, exakt wie im Backtest-Invarianten-Test aus Commit 1584e08."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        initial = engine.cash
        cumulative_net_pnl = Decimal("0")

        specs = [
            ("buy", Decimal("50000"), "sell", Decimal("51000")),  # long win
            ("sell", Decimal("51000"), "buy", Decimal("50000")),  # short win
            ("buy", Decimal("50000"), "sell", Decimal("49000")),  # long loss
            ("sell", Decimal("49000"), "buy", Decimal("50000")),  # short loss
        ]

        for open_side, open_price, close_side, close_price in specs:
            sym = _make_symbol()
            open_result = _make_order_result(
                symbol=sym, qty=Decimal("1.0"), price=open_price, side=open_side
            )
            await engine.on_order_filled(open_result)
            close_result = _make_order_result(
                symbol=sym, qty=Decimal("1.0"), price=close_price, side=close_side
            )
            await engine.on_order_filled(close_result)

            cumulative_net_pnl += Decimal(engine.trade_history[-1]["net_pnl"])
            assert engine.cash == pytest.approx(
                float(initial + cumulative_net_pnl), rel=1e-9
            )


# ===========================================================================
# Portfolio Persistence (PositionRepository Integration)
# ===========================================================================


class _FakePositionRepo:
    """
    Test-Double für PositionRepository. Kein Mock-Framework noetig, da das
    Verhalten (Erfolg/Fehler-Simulation) explizit steuerbar sein muss.
    """

    def __init__(self) -> None:
        self.upserted: list[dict] = []
        self.closed: list[dict] = []
        self._open_rows: list[dict] = []
        self.raise_on_get_open: Exception | None = None
        self.raise_on_upsert: Exception | None = None
        self.raise_on_close: Exception | None = None

    async def get_open_positions(self, trading_mode, user_id=None):
        if self.raise_on_get_open is not None:
            raise self.raise_on_get_open
        return self._open_rows

    async def upsert_open(self, position_data: dict) -> str:
        if self.raise_on_upsert is not None:
            raise self.raise_on_upsert
        self.upserted.append(position_data)
        return position_data.get("id", "generated-id")

    async def close(self, position_id, closed_at, realized_pnl=None) -> None:
        if self.raise_on_close is not None:
            raise self.raise_on_close
        self.closed.append(
            {"position_id": position_id, "closed_at": closed_at, "realized_pnl": realized_pnl}
        )


class TestPortfolioRestoreFromPersistence:
    async def test_raises_without_injected_repository(self) -> None:
        """Fail-closed: kein Repo injiziert -> RuntimeError, kein impliziter Empty-Start."""
        engine = PortfolioEngine(TradingMode.PAPER)
        with pytest.raises(RuntimeError, match="ohne injiziertes"):
            await engine.restore_from_persistence()

    async def test_db_error_propagates_fail_closed(self) -> None:
        """Fail-closed: DB-Fehler beim Restore wird NICHT geschluckt."""
        repo = _FakePositionRepo()
        repo.raise_on_get_open = ConnectionError("db unreachable")
        engine = PortfolioEngine(TradingMode.PAPER, position_repository=repo)

        with pytest.raises(ConnectionError):
            await engine.restore_from_persistence()

        # State darf nicht heimlich als "leer aber gueltig" markiert werden
        assert len(engine.positions) == 0

    async def test_restores_open_positions_into_state(self) -> None:
        """Erfolgreicher Restore rekonstruiert Position-Objekte korrekt aus DB-Rows."""
        repo = _FakePositionRepo()
        repo._open_rows = [
            {
                "id": str(uuid4()),
                "symbol": "BTC/USDT",
                "exchange": "binance",
                "side": "long",
                "quantity": Decimal("0.5"),
                "entry_price": Decimal("48000"),
                "current_price": Decimal("49000"),
                "leverage": Decimal("1"),
                "unrealized_pnl": Decimal("500"),
                "realized_pnl": Decimal("0"),
                "opened_at": datetime.now(tz=UTC),
                "closed_at": None,
                "strategy_name": "trend_following",
                "trading_mode": "paper",
                "user_id": None,
            }
        ]
        engine = PortfolioEngine(TradingMode.PAPER, position_repository=repo)

        restored = await engine.restore_from_persistence()

        assert restored == 1
        assert len(engine.positions) == 1
        pos = engine.positions[0]
        assert pos.symbol.base == "BTC"
        assert pos.symbol.quote == "USDT"
        assert pos.side == PositionSide.LONG
        assert pos.quantity == Decimal("0.5")
        assert pos.entry_price == Decimal("48000")

    async def test_restore_empty_db_is_valid_zero_positions(self) -> None:
        """Kein DB-Fehler, aber auch keine offenen Positionen -> gueltiger Start mit 0."""
        repo = _FakePositionRepo()
        repo._open_rows = []
        engine = PortfolioEngine(TradingMode.PAPER, position_repository=repo)

        restored = await engine.restore_from_persistence()

        assert restored == 0
        assert len(engine.positions) == 0

    async def test_restore_debits_cash_for_a_restored_long_position(self) -> None:
        """Regressionstest (Bug gefunden 2026-09-15, live beobachtet:
        portfolio_value $9996 -> $14014 nach einem Neustart mit 10
        offenen Positionen): eine wiederhergestellte LONG-Position muss
        ihr Entry-Notional vom Cash abziehen, genau wie beim
        urspruenglichen Open in _open_position() - sonst wird das
        gebundene Kapital nach jedem Neustart doppelt gezaehlt (einmal
        als "noch verfuegbares" Cash, einmal als Positionswert)."""
        repo = _FakePositionRepo()
        repo._open_rows = [
            {
                "id": str(uuid4()),
                "symbol": "BTC/USDT",
                "exchange": "binance",
                "side": "long",
                "quantity": Decimal("0.5"),
                "entry_price": Decimal("48000"),
                "current_price": Decimal("49000"),
                "leverage": Decimal("1"),
                "unrealized_pnl": Decimal("500"),
                "realized_pnl": Decimal("0"),
                "opened_at": datetime.now(tz=UTC),
                "closed_at": None,
                "strategy_name": "trend_following",
                "trading_mode": "paper",
                "user_id": None,
            }
        ]
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )

        await engine.restore_from_persistence()

        # 0.5 * 48000 = 24000 Notional wurde beim urspruenglichen Open
        # bereits gezahlt - Cash muss das jetzt widerspiegeln, nicht den
        # vollen initial_cash.
        assert engine._state.cash == Decimal("10000") - Decimal("24000")

    async def test_restore_credits_cash_for_a_restored_short_position(self) -> None:
        """Symmetrischer Fall: SHORT erhielt beim Open den Verkaufserloes
        gutgeschrieben - muss beim Restore ebenfalls gutgeschrieben
        werden, nicht ignoriert."""
        repo = _FakePositionRepo()
        repo._open_rows = [
            {
                "id": str(uuid4()),
                "symbol": "BTC/USDT",
                "exchange": "binance",
                "side": "short",
                "quantity": Decimal("0.5"),
                "entry_price": Decimal("48000"),
                "current_price": Decimal("47000"),
                "leverage": Decimal("1"),
                "unrealized_pnl": Decimal("500"),
                "realized_pnl": Decimal("0"),
                "opened_at": datetime.now(tz=UTC),
                "closed_at": None,
                "strategy_name": "trend_following",
                "trading_mode": "paper",
                "user_id": None,
            }
        ]
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )

        await engine.restore_from_persistence()

        assert engine._state.cash == Decimal("10000") + Decimal("24000")

    async def test_restore_of_multiple_positions_does_not_inflate_portfolio_value(self) -> None:
        """End-to-end Regressionstest fuer den live beobachteten Effekt:
        portfolio_value nach dem Restore muss cash + Positionswert sein,
        NICHT initial_cash + Positionswert (die urspruengliche
        Doppelzaehlung)."""
        repo = _FakePositionRepo()

        def _row(symbol: str, side: str, qty: str, price: str) -> dict:
            return {
                "id": str(uuid4()),
                "symbol": symbol,
                "exchange": "binance",
                "side": side,
                "quantity": Decimal(qty),
                "entry_price": Decimal(price),
                "current_price": Decimal(price),
                "leverage": Decimal("1"),
                "unrealized_pnl": Decimal("0"),
                "realized_pnl": Decimal("0"),
                "opened_at": datetime.now(tz=UTC),
                "closed_at": None,
                "strategy_name": "trend_following",
                "trading_mode": "paper",
                "user_id": None,
            }

        repo._open_rows = [
            _row("BTC/USDT", "short", "0.01", "50000"),  # 500 notional
            _row("ETH/USDT", "short", "1", "500"),  # 500 notional
        ]
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )

        await engine.restore_from_persistence()

        # Beide SHORT: cash = 10000 + 500 + 500 = 11000 (Verkaufserloes
        # gutgeschrieben). portfolio_value zieht den Positionswert fuer
        # SHORT wieder ab (Rueckkaufverbindlichkeit, siehe
        # PortfolioState.portfolio_value Docstring) -> 11000 - 1000 =
        # 10000, wirtschaftlich korrekt unveraendert gegenueber
        # initial_cash (kein Preisunterschied seit Entry). Die Bug-
        # Symptomatik waere hier portfolio_value = initial_cash(10000) -
        # 1000 = 9000 (Cash NICHT gutgeschrieben, Positionswert aber
        # bereits abgezogen) oder je nach Messpunkt eine andere falsche
        # Zahl - in jedem Fall nicht der wirtschaftlich korrekte,
        # unveraenderte Wert.
        assert engine._state.cash == Decimal("11000")
        assert engine._state.portfolio_value == Decimal("10000")


class TestPortfolioPersistenceWriteThrough:
    async def test_open_position_persists_when_repo_injected(self) -> None:
        repo = _FakePositionRepo()
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )
        result = _make_order_result(qty=Decimal("0.1"), price=Decimal("50000"), side="buy")

        await engine.on_order_filled(result)

        assert len(repo.upserted) == 1
        assert repo.upserted[0]["symbol"] == "BTC/USDT"
        assert repo.upserted[0]["side"] == "long"

    async def test_no_persistence_without_injected_repo(self) -> None:
        """Ohne injiziertes Repo (position_repository=None): reines in-memory
        Verhalten, kein Crash."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        result = _make_order_result(qty=Decimal("0.1"), price=Decimal("50000"), side="buy")

        await engine.on_order_filled(result)  # darf nicht crashen

        assert len(engine.positions) == 1

    async def test_persist_failure_does_not_block_trading(self) -> None:
        """
        Best-effort: DB-Fehler beim Schreiben darf den Trading-Betrieb NICHT
        stoppen (gleiches Fail-Safe-Muster wie KillSwitch._cancel_all_orders).
        """
        repo = _FakePositionRepo()
        repo.raise_on_upsert = RuntimeError("db write failed")
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )
        result = _make_order_result(qty=Decimal("0.1"), price=Decimal("50000"), side="buy")

        # Darf trotz DB-Fehler nicht crashen
        await engine.on_order_filled(result)

        # In-memory State ist trotzdem korrekt aktualisiert
        assert len(engine.positions) == 1

    async def test_close_position_persists_close(self) -> None:
        repo = _FakePositionRepo()
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )
        buy = _make_order_result(qty=Decimal("0.1"), price=Decimal("50000"), side="buy")
        await engine.on_order_filled(buy)
        repo.upserted.clear()

        sell = _make_order_result(qty=Decimal("0.1"), price=Decimal("55000"), side="sell")
        await engine.on_order_filled(sell)

        assert len(repo.closed) == 1
        assert len(engine.positions) == 0

    async def test_partial_close_persists_upsert_not_close(self) -> None:
        """Teilclose aktualisiert die Position (upsert), schliesst sie nicht (close)."""
        repo = _FakePositionRepo()
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )
        buy = _make_order_result(qty=Decimal("0.2"), price=Decimal("50000"), side="buy")
        await engine.on_order_filled(buy)
        repo.upserted.clear()

        partial_sell = _make_order_result(qty=Decimal("0.1"), price=Decimal("55000"), side="sell")
        await engine.on_order_filled(partial_sell)

        assert len(repo.closed) == 0
        assert len(repo.upserted) == 1
        assert len(engine.positions) == 1
        assert engine.positions[0].quantity == Decimal("0.1")

    async def test_close_persist_failure_does_not_block_trading(self) -> None:
        """Best-effort auch beim Close-Pfad: DB-Fehler blockiert nicht den In-Memory-Close."""
        repo = _FakePositionRepo()
        engine = PortfolioEngine(
            TradingMode.PAPER, initial_cash=Decimal("10000"), position_repository=repo
        )
        buy = _make_order_result(qty=Decimal("0.1"), price=Decimal("50000"), side="buy")
        await engine.on_order_filled(buy)

        repo.raise_on_close = RuntimeError("db write failed")
        sell = _make_order_result(qty=Decimal("0.1"), price=Decimal("55000"), side="sell")
        await engine.on_order_filled(sell)  # darf nicht crashen

        assert len(engine.positions) == 0  # in-memory trotzdem korrekt geschlossen
