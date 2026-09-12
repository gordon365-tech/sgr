"""
Tests für sgr.strategy.validation_runner.StrategyValidationRunner.

Deckt ab:
    - Kein Exchange Pool -> alle pending Strategien bleiben unvalidiert
      (skipped), kein Crash.
    - Erfolgreicher Backtest + konsistenter Walk-Forward -> is_validated
      True, can_go_live True (paper_trading_passed Platzhalter greift).
    - Backtest nicht akzeptabel (is_acceptable False) -> is_validated
      False, in skipped einsortiert.
    - Walk-Forward inkonsistent -> is_validated False trotz gutem
      Backtest.
    - Kein Walk-Forward-Ergebnis (None) -> zählt als nicht bestanden.
    - Bereits validierte Strategien werden nicht erneut gebacktestet.
    - Exception während run_full_validation für eine Strategie blockiert
      nicht die Validierung der übrigen Strategien.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sgr.backtesting.engine import FullValidationReport
from sgr.backtesting.types import BacktestResult, BacktestStatus, WalkForwardResult
from sgr.core.types import MarketRegime
from sgr.strategy.registry import StrategyRegistry
from sgr.strategy.validation_runner import StrategyValidationRunner

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class FakeStrategy:
    def __init__(self, name: str) -> None:
        self.name = name
        self.version = "1.0.0"
        self.supported_regimes = [MarketRegime.TRENDING_UP]

    def generate_signal(self, context):
        return None

    def get_parameters(self):
        return {}


def make_backtest_result(*, acceptable: bool = True) -> BacktestResult:
    if acceptable:
        kwargs = dict(
            sharpe_ratio=1.5,
            profit_factor=1.5,
            max_drawdown_pct=10.0,
            hit_rate_pct=55.0,
            total_trades=40,
        )
    else:
        kwargs = dict(
            sharpe_ratio=0.2,
            profit_factor=0.9,
            max_drawdown_pct=40.0,
            hit_rate_pct=20.0,
            total_trades=5,
        )
    return BacktestResult(
        config_summary={},
        status=BacktestStatus.COMPLETED,
        start_date="2025-01-01",
        end_date="2025-06-30",
        duration_days=180,
        initial_capital="10000",
        final_capital="15000",
        total_return_pct=50.0,
        cagr_pct=20.0,
        sortino_ratio=1.8,
        calmar_ratio=2.0,
        max_drawdown_duration_days=15,
        expected_value_per_trade="50",
        winning_trades=17,
        losing_trades=13,
        avg_winner="100",
        avg_loser="60",
        avg_holding_bars=12.0,
        total_fees="20",
        total_slippage="5",
        **kwargs,
    )


def make_walk_forward_result(*, is_consistent: bool = True) -> WalkForwardResult:
    return WalkForwardResult(
        n_splits=6,
        split_results=[],
        is_consistent=is_consistent,
        consistency_score=0.8,
        in_sample_sharpe=1.6,
        out_of_sample_sharpe=1.4,
        degradation_factor=0.9,
        recommendation="Proceed",
    )


def make_report(
    *, backtest: BacktestResult, walk_forward: WalkForwardResult | None
) -> FullValidationReport:
    return FullValidationReport(
        strategy_names=["fake_strategy"],
        symbols=["BTC/USDT"],
        timeframe="1h",
        start_date="2025-01-01T00:00:00",
        end_date="2025-06-30T00:00:00",
        backtest=backtest,
        walk_forward=walk_forward,
        monte_carlo=None,
        go_live_decision="GO" if backtest.is_acceptable else "NO-GO",
        decision_summary="test summary",
    )


@pytest.fixture(autouse=True)
def clean_registry():
    StrategyRegistry.get().clear()
    yield
    StrategyRegistry.get().clear()


@pytest.fixture
def fake_pool():
    from sgr.core.types import ExchangeID, TradingMode

    pool = MagicMock()
    pool._adapters = {(ExchangeID.PIONEX, TradingMode.PAPER): object()}
    return pool


# ---------------------------------------------------------------------
# No exchange pool
# ---------------------------------------------------------------------


class TestNoExchangePool:
    async def test_none_pool_skips_all_pending(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        runner = StrategyValidationRunner(exchange_pool=None)
        summary = await runner.validate_pending_strategies()

        assert summary.skipped == ["s1"]
        assert summary.validated == []
        entry = registry.get_entry("s1")
        assert entry is not None
        assert entry.is_validated is False

    async def test_pool_without_adapters_skips_all_pending(self) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        pool = MagicMock()
        pool._adapters = {}
        runner = StrategyValidationRunner(exchange_pool=pool)
        summary = await runner.validate_pending_strategies()

        assert summary.skipped == ["s1"]

    async def test_pool_connected_for_different_exchange_skips_all_pending(self) -> None:
        """
        Regression: Multi-Tenant-Worker, der z.B. für Binance statt Pionex
        initialisiert ist (siehe main.py primary_exchange). Ein nicht-leeres
        _adapters-dict darf NICHT automatisch als 'Exchange verfügbar'
        gewertet werden - der spezifische (exchange_id, PAPER)-Key muss
        existieren. Reproduziert den auf dem Server beobachteten Fehler
        'Exchange pionex (paper) not in pool' vor diesem Fix.
        """
        from sgr.core.types import ExchangeID, TradingMode

        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        pool = MagicMock()
        pool._adapters = {(ExchangeID.BINANCE, TradingMode.PAPER): object()}
        runner = StrategyValidationRunner(exchange_pool=pool, exchange_id=ExchangeID.PIONEX)
        runner._engine.run_full_validation = AsyncMock()

        summary = await runner.validate_pending_strategies()

        assert summary.skipped == ["s1"]
        runner._engine.run_full_validation.assert_not_awaited()

    async def test_pool_connected_for_matching_non_pionex_exchange_proceeds(
        self, monkeypatch
    ) -> None:
        """Gegenprobe: Binance-Worker MIT exchange_id=BINANCE validiert korrekt."""
        from sgr.core.types import ExchangeID, TradingMode

        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        pool = MagicMock()
        pool._adapters = {(ExchangeID.BINANCE, TradingMode.PAPER): object()}

        report = make_report(
            backtest=make_backtest_result(acceptable=True),
            walk_forward=make_walk_forward_result(is_consistent=True),
        )
        runner = StrategyValidationRunner(exchange_pool=pool, exchange_id=ExchangeID.BINANCE)
        runner._engine.run_full_validation = AsyncMock(return_value=report)

        summary = await runner.validate_pending_strategies()

        runner._engine.run_full_validation.assert_awaited_once()
        call_kwargs = runner._engine.run_full_validation.call_args.kwargs
        assert call_kwargs["exchange_id"] == ExchangeID.BINANCE
        assert summary.validated == ["s1"]


# ---------------------------------------------------------------------
# Successful validation
# ---------------------------------------------------------------------


class TestSuccessfulValidation:
    async def test_acceptable_backtest_and_consistent_wf_validates_strategy(
        self, fake_pool, monkeypatch
    ) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        report = make_report(
            backtest=make_backtest_result(acceptable=True),
            walk_forward=make_walk_forward_result(is_consistent=True),
        )
        runner = StrategyValidationRunner(exchange_pool=fake_pool)
        runner._engine.run_full_validation = AsyncMock(return_value=report)

        summary = await runner.validate_pending_strategies()

        assert summary.validated == ["s1"]
        entry = registry.get_entry("s1")
        assert entry.is_validated is True
        assert entry.validation_status.backtest_passed is True
        assert entry.validation_status.walk_forward_passed is True
        assert entry.validation_status.paper_trading_passed is True
        assert entry.validation_status.live_approved is False

    async def test_already_validated_strategy_is_not_rebacktested(
        self, fake_pool
    ) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))
        from sgr.strategy.base import ValidationStatus

        registry.mark_validated(
            "s1",
            ValidationStatus(
                backtest_passed=True,
                walk_forward_passed=True,
                paper_trading_passed=True,
            ),
        )

        runner = StrategyValidationRunner(exchange_pool=fake_pool)
        runner._engine.run_full_validation = AsyncMock()

        summary = await runner.validate_pending_strategies()

        runner._engine.run_full_validation.assert_not_awaited()
        assert summary.validated == []
        assert summary.skipped == []


# ---------------------------------------------------------------------
# Failing gates
# ---------------------------------------------------------------------


class TestFailingGates:
    async def test_unacceptable_backtest_does_not_validate(self, fake_pool) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        report = make_report(
            backtest=make_backtest_result(acceptable=False),
            walk_forward=make_walk_forward_result(is_consistent=True),
        )
        runner = StrategyValidationRunner(exchange_pool=fake_pool)
        runner._engine.run_full_validation = AsyncMock(return_value=report)

        summary = await runner.validate_pending_strategies()

        assert summary.skipped == ["s1"]
        entry = registry.get_entry("s1")
        assert entry.is_validated is False
        assert entry.validation_status.backtest_passed is False

    async def test_inconsistent_walk_forward_does_not_validate(self, fake_pool) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        report = make_report(
            backtest=make_backtest_result(acceptable=True),
            walk_forward=make_walk_forward_result(is_consistent=False),
        )
        runner = StrategyValidationRunner(exchange_pool=fake_pool)
        runner._engine.run_full_validation = AsyncMock(return_value=report)

        summary = await runner.validate_pending_strategies()

        assert summary.skipped == ["s1"]
        entry = registry.get_entry("s1")
        assert entry.is_validated is False
        assert entry.validation_status.backtest_passed is True
        assert entry.validation_status.walk_forward_passed is False

    async def test_missing_walk_forward_result_counts_as_not_passed(
        self, fake_pool
    ) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("s1"))

        report = make_report(
            backtest=make_backtest_result(acceptable=True),
            walk_forward=None,
        )
        runner = StrategyValidationRunner(exchange_pool=fake_pool)
        runner._engine.run_full_validation = AsyncMock(return_value=report)

        summary = await runner.validate_pending_strategies()

        assert summary.skipped == ["s1"]
        entry = registry.get_entry("s1")
        assert entry.is_validated is False


# ---------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------


class TestErrorHandling:
    async def test_exception_for_one_strategy_does_not_block_others(
        self, fake_pool
    ) -> None:
        registry = StrategyRegistry.get()
        registry.register_instance(FakeStrategy("broken"))
        registry.register_instance(FakeStrategy("healthy"))

        good_report = make_report(
            backtest=make_backtest_result(acceptable=True),
            walk_forward=make_walk_forward_result(is_consistent=True),
        )

        async def fake_run_full_validation(*, strategy_names, **kwargs):
            if strategy_names == ["broken"]:
                raise RuntimeError("data loading failed")
            return good_report

        runner = StrategyValidationRunner(exchange_pool=fake_pool)
        runner._engine.run_full_validation = AsyncMock(
            side_effect=fake_run_full_validation
        )

        summary = await runner.validate_pending_strategies()

        assert "broken" in summary.failed
        assert summary.validated == ["healthy"]
        assert registry.get_entry("broken").is_validated is False
        assert registry.get_entry("healthy").is_validated is True

    async def test_no_pending_strategies_returns_empty_summary(self, fake_pool) -> None:
        runner = StrategyValidationRunner(exchange_pool=fake_pool)
        runner._engine.run_full_validation = AsyncMock()

        summary = await runner.validate_pending_strategies()

        assert summary.validated == []
        assert summary.skipped == []
        assert summary.failed == {}
        runner._engine.run_full_validation.assert_not_awaited()
